"""45 acceptance entries: native real processes; Docker-only checks explicitly deferred.

No external account/API: deterministic LLM/retrieval/index adapters and local MCP.
Unique PostgreSQL database/workspace retained. Never delete existing business data.
"""
from io import BytesIO
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import time
from uuid import uuid4
import requests
import yaml
from sqlalchemy import create_engine,text
from sqlalchemy.pool import NullPool
from server.db.runtime import driver_url
from evaluation.phase5b2.collector import Collector

ROOT=Path(__file__).resolve().parents[2]
NAMES=['PostgreSQL healthy','Migrations complete','API starts','Worker starts','Streamlit starts','Register Alice',
 'Login Alice','Authenticated /me','Alice creates Workspace','Alice uploads PDF','Alice creates Dataset','Alice queues Task',
 'Worker completes Task','Register Bob','Bob cannot access Alice Workspace','Bob cannot access Alice Task',
 'Bob cannot download Alice Artifact','Bob cannot read Alice personal Memory','Alice adds Bob as Viewer',
 'Bob can read Workspace','Bob cannot upload','Bob cannot delete','Alice promotes Bob Editor','Bob can upload',
 'Bob external write denied','Alice external write reaches HITL','Last owner protection','Role change without login',
 'Refresh rotation','Refresh replay protection','Logout invalidates refresh','Disabled user rejected','Failed login lockout',
 'FastAPI restart','Worker restart','Task data persists','Audit events written','OTel trace emitted','Metrics emitted',
 'Observability outage does not break API','Docker app processes non-root','No secret in logs','No secret in image',
 'CI workflow syntax valid','Full legacy AUTH_DISABLED mode works']


def run():
    identifier=uuid4().hex; folder=ROOT/'.runtime/phase5b2_smoke'/identifier; folder.mkdir(parents=True)
    output=ROOT/'evaluation/results/phase5b2/runtime_smoke.json'; output.parent.mkdir(parents=True,exist_ok=True)
    report={'run_id':identifier,'folder':str(folder),'mode':'native processes; Docker/WSL deferred by user',
      'fixtures':'LLM, retrieval, embeddings deterministic; external writes only local MCP',
      'real':'PostgreSQL, Alembic, Argon2, JWT, HTTP, PDF validation/filesystem, queue, independent API/Worker/Streamlit, LangGraph/PostgresSaver, MCP, analysis subprocess, OTLP protobuf',
      'checks':{name:{'status':'SKIPPED','details':'not reached'} for name in NAMES},'performance':{},'extra':{}}
    processes=[]; handles=[]; collector=Collector(); base='http://127.0.0.1:8037'; sensitive=[]; engine=None
    def save():output.write_text(json.dumps(report,indent=2,ensure_ascii=False),encoding='utf-8')
    def record(index,condition,details=None):
        report['checks'][NAMES[index-1]]={'status':'PASS' if condition else 'FAIL','details':details}; save()
        print(str(index)+'. '+NAMES[index-1]+': '+report['checks'][NAMES[index-1]]['status'],flush=True)
        if not condition:raise AssertionError(NAMES[index-1])
    def command(args,environment):
        file=(folder/f'command-{len(handles)}.log').open('w',encoding='utf-8'); handles.append(file)
        result=subprocess.run([sys.executable,*args],cwd=ROOT,env=environment,stdout=file,stderr=subprocess.STDOUT,
          timeout=90,creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
        if result.returncode:raise RuntimeError('Fixture command failed; inspect '+str(file.name))
    def start(args,environment):
        file=(folder/f'process-{len(processes)}.log').open('w',encoding='utf-8'); handles.append(file)
        process=subprocess.Popen([sys.executable,*args],cwd=ROOT,env=environment,stdout=file,stderr=subprocess.STDOUT,
          creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
        processes.append(process); return process
    def stop(process):
        if process.poll() is None:process.terminate(); process.wait(timeout=20)
    def wait(predicate,timeout=90):
        deadline=time.monotonic()+timeout
        while time.monotonic()<deadline:
            value=predicate()
            if value:return value
            time.sleep(.2)
        raise TimeoutError('Runtime acceptance timed out; inspect retained logs')
    def ready(port=8037):
        try:return requests.get(f'http://127.0.0.1:{port}/health/ready',timeout=1).ok
        except requests.RequestException:return False
    def request(method,path,actor=None,status=200,**kwargs):
        response=requests.request(method,base+path,headers=actor,timeout=120,**kwargs)
        if response.status_code!=status:raise AssertionError(f'{method} {path}: expected {status}, received {response.status_code}: {response.text[:300]}')
        return response.json() if response.content and response.headers.get('content-type','').startswith('application/json') else response
    def register(email):return request('POST','/auth/register',status=201,json={'email':email,'password':password,'display_name':email.split('@')[0]})
    def login(email):
        value=request('POST','/auth/login',json={'email':email,'password':password}); sensitive.extend([value['access_token'],value['refresh_token']]); return value
    def actor(tokens):return {'Authorization':'Bearer '+tokens['access_token']}
    def task(task_id):return request('GET','/tasks/'+task_id,a)
    def complete(task_id):
        value=task(task_id)
        if value['status'] in {'FAILED','REQUIRES_RECONCILIATION'}:raise AssertionError('Task failed: '+str(value.get('last_error')))
        return value if value['status']=='COMPLETED' else None
    def create(goal,who=None,scope='workspace_only'):
        return request('POST','/tasks',who or a,json={'workspace_id':workspace_id,'goal':goal,'source_scope':scope})['id']
    worker_number=0
    def start_worker():
        nonlocal worker_number
        worker_number+=1; path=folder/f'worker-stop-{worker_number}'
        health=folder/f'worker-health-{worker_number}.json'
        worker=start(['-m','evaluation.phase5b2.fixture_process','worker'],{**env,'OTEL_SERVICE_NAME':'fixture-worker','WORKER_STOP_FILE':str(path),'WORKER_HEALTH_FILE':str(health)})
        wait(lambda:request('GET','/health/ready')['worker']=='active'); wait(health.exists)
        command(['-m','server.healthcheck','worker'],{**env,'WORKER_HEALTH_FILE':str(health)})
        return worker,path
    def stop_worker(worker,path):
        path.write_text('stop owned fixture worker'); worker.wait(timeout=40)
        if worker.returncode:raise AssertionError('Worker graceful shutdown failed')
    try:
        url=driver_url(os.environ['PHASE5B1_TEST_DATABASE_URL'])
        admin=create_engine(url,poolclass=NullPool,isolation_level='AUTOCOMMIT')
        database='phase5b2_smoke_'+identifier
        with admin.connect() as db:
            version=db.scalar(text('SHOW server_version')); db.execute(text('CREATE DATABASE '+database))
        admin.dispose(); url=url.set(database=database).render_as_string(hide_password=False)
        report['postgres_version']=version; record(1,True,version)
        password=secrets.token_urlsafe(24); jwt_secret=secrets.token_urlsafe(48); provider_secret=secrets.token_urlsafe(40)
        sensitive.extend([password,jwt_secret,provider_secret])
        mcp=folder/'mcp.json'; mcp.write_text(json.dumps([{'id':'github','name':'Synthetic local MCP','transport':'stdio',
          'command':sys.executable,'args':['-m','evaluation.phase5b1.mcp_server'],'env_keys':['PHASE5B1_SMOKE_FOLDER']}]))
        env={**os.environ,'DATABASE_BACKEND':'postgresql','DATABASE_URL':url,'TASK_EXECUTION_MODE':'worker','TASK_QUEUE_ENABLED':'true',
          'WORKSPACE_DIR':str(folder/'workspace'),'PHASE5B1_SMOKE_FOLDER':str(folder),'API_PORT':'8037','API_BASE':base,
          'WORKER_CONCURRENCY':'2','MCP_SERVERS_FILE':str(mcp),'GITHUB_MCP_ENABLED':'false','GITHUB_MCP_READ_ONLY':'false',
          'GITHUB_ALLOWED_REPOSITORIES':'fixture/test','MEMORY_ENABLED':'true','MEMORY_AUTO_EXTRACT':'false',
          'SELF_CORRECT_RAG_ENABLED':'false','GROUNDING_CHECK_ENABLED':'false','MULTI_AGENT_REVIEW_POLICY':'disabled',
          'AGENT_MAX_GRAPH_STEPS':'60','LANGSMITH_TRACING':'false','LANGCHAIN_TRACING_V2':'false','AUTH_ENABLED':'true',
          'AUTH_ALLOW_REGISTRATION':'true','AUTH_JWT_SECRET':jwt_secret,'AUTH_JWT_ISSUER':'fixture','AUTH_JWT_AUDIENCE':'fixture-api',
          'AUTH_MAX_FAILED_LOGIN_ATTEMPTS':'3','APP_ENV':'production','TRUSTED_HOSTS':'127.0.0.1,localhost',
          'CORS_ALLOWED_ORIGINS':'http://localhost:8537','API_DOCS_ENABLED':'false','OTEL_ENABLED':'true','LOG_FORMAT':'json',
          'OTEL_EXPORTER_OTLP_ENDPOINT':f'http://127.0.0.1:{collector.port}','OTEL_SERVICE_NAME':'fixture-api',
          'DEEPSEEK_API_KEY':provider_secret,'GITHUB_PERSONAL_ACCESS_TOKEN':provider_secret}
        command(['-m','server.migrate'],env); record(2,True,'Alembic 5b2_0001 + official queue/saver schema initialization')
        engine=create_engine(url,poolclass=NullPool,hide_parameters=True)
        api=start(['-m','evaluation.phase5b2.fixture_process','api'],env); wait(ready); record(3,True)
        worker,stopfile=start_worker(); record(4,True)
        ui=start(['-m','streamlit','run','client/app.py','--server.headless=true','--server.port=8537'],env)
        def ui_ready():
            try:return requests.get('http://127.0.0.1:8537/_stcore/health',timeout=1).ok
            except requests.RequestException:return False
        wait(ui_ready); record(5,True,'real Streamlit health; token client behavior separately tested')
        alice=register('alice@example.test'); record(6,True)
        alice_tokens=login(alice['email']); a=actor(alice_tokens); record(7,True)
        record(8,request('GET','/auth/me',a)['id']==alice['id'])
        workspace_id=request('POST','/workspaces',a,json={'name':'Alice private fixture'})['id']; record(9,True)
        from pypdf import PdfWriter
        pdf=BytesIO(); writer=PdfWriter(); writer.add_blank_page(200,200); writer.write(pdf); pdf_bytes=pdf.getvalue()
        uploaded=request('POST',f'/workspaces/{workspace_id}/documents',a,files=[('files',('paper.pdf',pdf_bytes,'application/pdf'))])
        document=uploaded['documents'][0]; record(10,document['status']=='ready','real valid PDF; synthetic embedding adapter')
        (folder/'control.json').write_text(json.dumps({'document_id':document['id'],'delay':.3}))
        asset=request('POST',f'/workspaces/{workspace_id}/datasets',a,files={'file':('experiment.csv',b'model,params_m,miou\nA,2.5,73.27\nB,3.2,76\nC,2,70\n','text/csv')}); record(11,asset['row_count']==3)
        first=create('Read persistent fixture twice'); queued=request('POST',f'/tasks/{first}/run',a,status=202,json={}); record(12,queued['status']=='QUEUED')
        wait(lambda:complete(first)); record(13,True)
        bob=register('bob@example.test'); bob_tokens=login(bob['email']); b=actor(bob_tokens); record(14,True)
        request('GET','/workspaces/'+workspace_id,b,status=403); record(15,True)
        request('GET','/tasks/'+first,b,status=403); record(16,True)
        analysis=request('POST',f'/workspaces/{workspace_id}/analysis',a,json={'dataset_ids':[asset['id']],'objective':'Fixture artifact',
          'code':"import matplotlib.pyplot as plt\nplt.bar(df['model'], df['miou'])\nplt.savefig('fixture.png')\nplt.close()"})
        artifact=analysis['artifacts'][0]['id']; request('GET','/artifacts/'+artifact,b,status=403); record(17,True,'real constrained analysis output file')
        memory=request('POST','/memories',a,json={'content':'Alice private preference'}); request('GET','/memories/'+memory['id'],b,status=403); record(18,True)
        request('POST',f'/workspaces/{workspace_id}/members',a,status=201,json={'email':bob['email'],'role':'VIEWER'}); record(19,True)
        record(20,request('GET','/workspaces/'+workspace_id,b)['id']==workspace_id)
        request('POST',f'/workspaces/{workspace_id}/documents',b,status=403,files=[('files',('bob.pdf',pdf_bytes,'application/pdf'))]); record(21,True)
        request('DELETE',f'/workspaces/{workspace_id}/documents/'+document['id'],b,status=403); record(22,True)
        member=f'/workspaces/{workspace_id}/members/'+bob['id']
        request('PATCH',member,a,json={'role':'EDITOR'}); record(23,True)
        result=request('POST',f'/workspaces/{workspace_id}/documents',b,files=[('files',('bob.pdf',pdf_bytes,'application/pdf'))]); record(24,result['documents'][0]['status']=='ready')
        write_bob=create('Create synthetic issue',b,'external'); request('POST',f'/tasks/{write_bob}/run',b,status=202,json={})
        wait(lambda:task(write_bob)['status']=='FAILED')
        with engine.connect() as db:count=db.scalar(text('SELECT count(*) FROM approval_requests WHERE task_id=:id'),{'id':write_bob})
        record(25,count==0,'forbidden role fails before ApprovalRequest or external call')
        write_alice=create('Create synthetic issue',scope='external'); request('POST',f'/tasks/{write_alice}/run',a,status=202,json={})
        wait(lambda:task(write_alice)['status']=='WAITING_USER'); approval=request('GET','/approvals',a,params={'task_id':write_alice})['approvals'][0]
        record(26,approval['status']=='PENDING')
        request('DELETE',f'/workspaces/{workspace_id}/members/'+alice['id'],a,status=409); record(27,True)
        request('PATCH',member,a,json={'role':'VIEWER'})
        request('POST',f'/workspaces/{workspace_id}/datasets',b,status=403,files={'file':('denied.csv',b'x\n1\n','text/csv')}); record(28,True,'unchanged access JWT')
        fresh=request('POST','/auth/refresh',json={'refresh_token':alice_tokens['refresh_token']}); sensitive.extend([fresh['access_token'],fresh['refresh_token']]); a=actor(fresh)
        newer=request('POST','/auth/refresh',json={'refresh_token':fresh['refresh_token']}); sensitive.extend([newer['access_token'],newer['refresh_token']]); a=actor(newer)
        record(29,newer['refresh_token']!=fresh['refresh_token'],'two consecutive rotations')
        request('POST','/auth/refresh',status=401,json={'refresh_token':fresh['refresh_token']})
        request('POST','/auth/refresh',status=401,json={'refresh_token':newer['refresh_token']}); record(30,True)
        alice_tokens=login(alice['email']); a=actor(alice_tokens)
        request('POST','/auth/logout',status=204,json={'refresh_token':alice_tokens['refresh_token']})
        request('POST','/auth/refresh',status=401,json={'refresh_token':alice_tokens['refresh_token']}); record(31,True)
        with engine.begin() as db:db.execute(text("UPDATE users SET status='DISABLED' WHERE id=:id"),{'id':bob['id']})
        request('GET','/auth/me',b,status=401); request('POST','/auth/refresh',status=401,json={'refresh_token':bob_tokens['refresh_token']}); record(32,True)
        with engine.begin() as db:db.execute(text("UPDATE users SET status='ACTIVE' WHERE id=:id"),{'id':bob['id']})
        for _ in range(3):request('POST','/auth/login',status=401,json={'email':bob['email'],'password':'incorrect fixture password'})
        request('POST','/auth/login',status=401,json={'email':bob['email'],'password':password})
        with engine.connect() as db:locked=db.scalar(text('SELECT locked_until FROM users WHERE id=:id'),{'id':bob['id']})
        record(33,locked is not None)
        with engine.begin() as db:db.execute(text('UPDATE users SET failed_login_count=0,locked_until=NULL WHERE id=:id'),{'id':bob['id']})
        # Actual approval and write receipt, once, using an independent Worker.
        request('POST','/approvals/'+approval['id']+'/approve',a,json={}); wait(lambda:complete(write_alice))
        report['extra']['approved_write_once']=len(json.loads((folder/'actions.json').read_text()))==1
        # Queue original actor is revoked before the Worker starts.
        stop_worker(worker,stopfile); request('PATCH',member,a,json={'role':'EDITOR'})
        revoked=create('Read revocation fixture',b); request('POST',f'/tasks/{revoked}/run',b,status=202,json={})
        request('DELETE',member,a,status=204); worker,stopfile=start_worker(); wait(lambda:task(revoked)['status']=='FAILED')
        report['extra']['queued_revocation_no_execution']=task(revoked)['attempt_count']==0
        request('POST',f'/workspaces/{workspace_id}/members',a,status=201,json={'email':bob['email'],'role':'EDITOR'})
        # Authenticated Research + Data actual graph, saver, analysis and AgentRun records.
        multi=create('Compare papers with experiment.csv data and find best configuration under 3M parameters and generate a chart')
        request('POST',f'/tasks/{multi}/run',a,status=202,json={}); wait(lambda:complete(multi),180)
        delegated=request('GET',f'/tasks/{multi}/delegations',a)['delegations']
        report['multi_task_id']=multi; report['extra']['research_data_agents']={'research','data'}.issubset({d['agent_id'] for d in delegated})
        stop(api); api=start(['-m','evaluation.phase5b2.fixture_process','api'],env); wait(ready); record(34,True)
        stop_worker(worker,stopfile); worker,stopfile=start_worker(); record(35,True)
        record(36,task(first)['status']=='COMPLETED' and task(multi)['status']=='COMPLETED')
        with engine.connect() as db:
            audit=dict(db.execute(text('SELECT event_type,count(*) FROM audit_events GROUP BY event_type')).all())
        report['audit_counts']=audit
        required={'USER_REGISTERED','LOGIN_SUCCEEDED','LOGIN_FAILED','USER_LOCKED','TOKEN_REFRESHED','TOKEN_REPLAY_DETECTED','LOGOUT',
          'WORKSPACE_CREATED','MEMBER_ADDED','MEMBER_ROLE_CHANGED','MEMBER_REMOVED','AUTHORIZATION_DENIED','APPROVAL_APPROVED','EXTERNAL_WRITE_EXECUTED'}
        record(37,required.issubset(audit),audit)
        def chain():
            candidates=[s for s in collector.spans if s['attributes'].get('task_id')==multi]
            traces={s['trace_id'] for s in candidates if s['name']=='queue.task.execute'}
            return next(([s for s in collector.spans if s['trace_id']==trace_id] for trace_id in traces
              if {'http.request','queue.task.execute','agent.task','agent.research','agent.data','tool.call'}.issubset(
                {s['name'] for s in collector.spans if s['trace_id']==trace_id})),None)
        linked=wait(chain,30)
        report['trace_chain']={'trace_id':linked[0]['trace_id'],'task_id':multi,'spans':[{'name':s['name'],'span_id':s['span_id'],
          'parent_span_id':s['parent_span_id'],'attributes':s['attributes']} for s in linked]}
        record(38,True,'HTTP -> durable Task metadata traceparent -> queue/Worker -> Agents -> Tools, same trace_id')
        names={'auth_login_success_total','auth_login_failure_total','tasks_completed_total','tasks_failed_total','tool_calls_total','approvals_requested_total','approvals_approved_total'}
        def grew():return names.issubset({m['name'] for m in collector.metrics if any(v>0 for v in m['values'])})
        wait(grew,20); metrics=request('GET','/metrics').text
        invalid_labels=any(any(key in labels for key in ('task_id','user_id','workspace_id','session_id','email','query')) for m in collector.metrics for labels in m['attributes'])
        report['metrics']={'positive_names':sorted({m['name'] for m in collector.metrics if any(v>0 for v in m['values'])}),'high_cardinality_labels':invalid_labels}
        record(39,not invalid_labels and 'http_requests_total' in metrics)
        collector.close()
        record(40,request('GET','/auth/me',a)['id']==alice['id'] and request('GET','/workspaces/'+workspace_id,a)['id']==workspace_id,'OTLP receiver stopped; core API continues')
        for index in (41,43):report['checks'][NAMES[index-1]]={'status':'SKIPPED','details':'Docker/WSL runtime explicitly deferred by user; source config only'}
        # Restart preserves active refresh rows and memberships; expired family remains revoked.
        restored=login(alice['email']); report['extra']['refresh_persistence']=request('POST','/auth/refresh',json={'refresh_token':restored['refresh_token']})['token_type']=='bearer'
        for path,who in [('/health/live',None),('/auth/me',a),('/workspaces/'+workspace_id,a)]:
            samples=[]
            for _ in range(10):
                start_time=time.perf_counter(); request('GET',path,who); samples.append(round((time.perf_counter()-start_time)*1000,3))
            report['performance'][path]=samples
        samples=[]
        for _ in range(3):
            start_time=time.perf_counter(); login(alice['email']); samples.append(round((time.perf_counter()-start_time)*1000,3))
        report['performance']['login_ms']=samples
        # CI workflow local syntax/config verification. Remote jobs are not submitted.
        for file in (ROOT/'.github/workflows').glob('*.yml'):
            workflow=yaml.safe_load(file.read_text()); assert workflow.get('jobs') and ('on' in workflow or True in workflow)
        record(44,True,'YAML/config static parsing; GitHub execution not run (no push)')
        # AUTH_DISABLED remains a real separate SQLite/inline application.
        legacy_env={**env,'APP_ENV':'development','AUTH_ENABLED':'false','DATABASE_BACKEND':'sqlite','DATABASE_URL':'',
          'TASK_EXECUTION_MODE':'inline','TASK_QUEUE_ENABLED':'false','WORKSPACE_DIR':str(folder/'legacy-workspace'),'API_PORT':'8038','OTEL_ENABLED':'false'}
        legacy=start(['-m','evaluation.phase5b2.fixture_process','api'],legacy_env); wait(lambda:ready(8038))
        legacy_base='http://127.0.0.1:8038'
        response=requests.post(legacy_base+'/workspaces',json={'name':'Legacy isolated fixture'},timeout=10); response.raise_for_status()
        legacy_id=response.json()['id']
        response=requests.post(legacy_base+f'/workspaces/{legacy_id}/documents',files=[('files',('legacy.pdf',pdf_bytes,'application/pdf'))],timeout=10); response.raise_for_status()
        (folder/'control.json').write_text(json.dumps({'document_id':response.json()['documents'][0]['id'],'delay':.1}))
        response=requests.post(legacy_base+'/tasks',json={'workspace_id':legacy_id,'goal':'Read legacy fixture twice','source_scope':'workspace_only'},timeout=10); response.raise_for_status()
        response=requests.post(legacy_base+'/tasks/'+response.json()['id']+'/run',json={},timeout=90); response.raise_for_status()
        record(45,response.json()['status']=='COMPLETED','SQLite/inline, no bearer, actual graph and legacy stores')
        stop(legacy); stop_worker(worker,stopfile); stop(api); stop(ui)
        for handle in handles:handle.flush()
        logs='\n'.join(p.read_text(encoding='utf-8',errors='replace') for p in folder.glob('*.log'))
        leak=any(value and value in logs for value in sensitive)
        trace_leak=any(value and value in json.dumps(collector.spans) for value in sensitive)
        structured=[]
        for line in logs.splitlines():
            try:entry=json.loads(line)
            except ValueError:continue
            if entry.get('logger') in {'server.http','server.worker'}:structured.append(entry)
        report['extra']['correlated_structured_logs']=any(e.get('request_id') and e.get('trace_id') and e.get('user_id') for e in structured)
        record(42,not leak and not trace_leak and all(not s['events'] for s in collector.spans),'password/access/refresh/JWT/provider canaries absent from raw process logs and exported traces')
        report['extra']['all_passed']=all(report['extra'].values())
        if not report['extra']['all_passed']:raise AssertionError('Additional security/runtime checks failed')
        report['outcome']='PASS WITH DOCKER CHECKS DEFERRED'; save()
    except Exception as error:
        report['outcome']='FAIL'; report['error_type']=type(error).__name__; save(); raise
    finally:
        for process in processes:
            if process.poll() is None:
                process.terminate()
                try:process.wait(timeout=15)
                except subprocess.TimeoutExpired:process.kill(); process.wait(timeout=5)
        for handle in handles:handle.close()
        if collector.thread.is_alive():collector.close()
        if engine:engine.dispose()
        save()


if __name__=='__main__':run()
