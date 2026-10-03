"""Thirty real PostgreSQL + API + worker process acceptance checks.

LLM/retrieval are declared deterministic fixtures; real agent graphs/MCP/analysis
execute. Local data migration uses read-only backups, never changes original DBs.
"""
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
from uuid import uuid4
import requests
from sqlalchemy import create_engine, text
from sqlalchemy.pool import NullPool

ROOT=Path(__file__).resolve().parents[2]
NAMES=['PostgreSQL starts','Alembic upgrade head','Queue schema initialized','PostgresSaver initialized',
       'FastAPI postgres/worker starts','Independent worker starts','Streamlit starts','Task creation',
       'Run returns 202','Task QUEUED','Worker RUNNING','Task completes','API restart independent',
       'Streamlit stop independent','Worker graceful restart','Worker hard crash read recovery',
       'Completed step not repeated','Pause','Queue resume','Cancel queued task','HITL WAITING_USER',
       'Approval queue resume','Approved write exactly once','Research + Data background',
       'Persistence after restart','SQLite migration dry-run','SQLite fixture migration',
       'Row/checksum verification','Duplicate run requests','DB/queue health']


def run():
    from server.db.runtime import driver_url
    from server.db.schema import metadata
    base_url=os.environ['PHASE5B1_TEST_DATABASE_URL']
    run_id=uuid4().hex
    folder=ROOT/'.runtime'/'phase5b1_smoke'/run_id; folder.mkdir(parents=True)
    output=ROOT/'evaluation/results/phase5b1/runtime_smoke.json'; output.parent.mkdir(parents=True,exist_ok=True)
    report={'run_id':run_id,'folder':str(folder),'provider':'deterministic LLM and synthetic retrieval fixture',
        'runtime':'real PostgreSQL, Alembic, queue, independent API/worker, LangGraph, MCP SDK and analysis subprocess',
        'checks':{name:{'status':'SKIPPED','details':'not reached'} for name in NAMES},'performance':{},'task_ids':[]}
    processes=[]; handles=[]; base='http://127.0.0.1:8027'; manager=None
    def save(): output.write_text(json.dumps(report,indent=2,ensure_ascii=False),encoding='utf-8')
    def record(index,passed,details=None):
        name=NAMES[index-1]; report['checks'][name]={'status':'PASS' if passed else 'FAIL','details':details}; save()
        print(f'{index}. {name}: '+report['checks'][name]['status'],flush=True)
        if not passed: raise AssertionError(name)
    def command(args,env):
        log=folder/f'command-{len(handles)}.log'; file=log.open('w',encoding='utf-8'); handles.append(file)
        result=subprocess.run([sys.executable,*args],cwd=ROOT,env=env,stdout=file,stderr=subprocess.STDOUT,timeout=90,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
        if result.returncode: raise RuntimeError('Command failed; inspect '+str(log))
    def start(args,env):
        file=(folder/f'process-{len(processes)}.log').open('w',encoding='utf-8'); handles.append(file)
        process=subprocess.Popen([sys.executable,*args],cwd=ROOT,env=env,stdout=file,stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
        processes.append(process); return process
    def wait(predicate,timeout=60):
        deadline=time.monotonic()+timeout
        while time.monotonic()<deadline:
            value=predicate()
            if value: return value
            time.sleep(.15)
        raise TimeoutError('Acceptance condition timed out; inspect fixture logs')
    def request(method,path,**kwargs):
        response=requests.request(method,base+path,timeout=10,**kwargs); response.raise_for_status(); return response
    def api_ready():
        try:return requests.get(base+'/health/ready',timeout=1).ok
        except requests.RequestException:return False
    def task(task_id): return request('GET','/tasks/'+task_id).json()
    def complete(task_id):
        result=task(task_id)
        if result['status'] in {'FAILED','REQUIRES_RECONCILIATION'}: raise AssertionError('Task failed: '+str(result.get('last_error')))
        return result if result['status']=='COMPLETED' else None
    def fixture_control(**fields):
        nonlocal settings
        settings={**settings,**fields}; tmp=folder/'control.tmp'; tmp.write_text(json.dumps(settings)); tmp.replace(folder/'control.json')
    def create(goal, source_scope='workspace_only'):
        result=request('POST','/tasks',json={'workspace_id':workspace_id,'goal':goal,'source_scope':source_scope}).json()
        report['task_ids'].append(result['id']); save(); return result['id']
    def enqueue(task_id,resume=False):
        started=time.monotonic(); response=request('POST',f'/tasks/{task_id}/'+('resume' if resume else 'run'),json={})
        report['performance'].setdefault('enqueue_ms',[]).append(round((time.monotonic()-started)*1000,3)); save()
        if response.status_code != 202: raise AssertionError('Expected 202')
        return response.json()
    def step_started(task_id,index):
        return any(s['step_index']==index and s['status']=='RUNNING' for s in task(task_id)['steps'])
    worker_index=0
    worker_starts=[]
    def start_worker():
        nonlocal worker_index
        worker_index+=1; stopfile=folder/f'worker-stop-{worker_index}'
        worker_starts.append(time.time())
        process=start(['-m','evaluation.phase5b1.fixture_process','worker'],{**env,'WORKER_STOP_FILE':str(stopfile)})
        return process,stopfile
    def stop_worker(process,stopfile):
        stopfile.write_text('operator graceful stop'); process.wait(timeout=30)
        if process.returncode: raise AssertionError('Worker failed graceful shutdown')
    try:
        url=driver_url(base_url); admin=create_engine(url,poolclass=NullPool,isolation_level='AUTOCOMMIT')
        database='phase5b1_smoke_'+run_id
        with admin.connect() as db:
            version=db.scalar(text('SHOW server_version')); db.execute(text('CREATE DATABASE '+database))
        admin.dispose(); url=url.set(database=database).render_as_string(hide_password=False)
        report['postgres_version']=version; record(1,True,version)
        mcp=folder/'mcp.json'; mcp.write_text(json.dumps([{'id':'github','name':'Local synthetic MCP only','transport':'stdio',
            'command':sys.executable,'args':['-m','evaluation.phase5b1.mcp_server'], 'env_keys':['PHASE5B1_SMOKE_FOLDER']}]))
        env={**os.environ,'DATABASE_BACKEND':'postgresql','DATABASE_URL':url,'TASK_EXECUTION_MODE':'worker','TASK_QUEUE_ENABLED':'true',
            'WORKSPACE_DIR':str(folder/'workspace'),'PHASE5B1_SMOKE_FOLDER':str(folder),'API_PORT':'8027',
            'WORKER_CONCURRENCY':'2','MCP_SERVERS_FILE':str(mcp),'GITHUB_MCP_ENABLED':'false','GITHUB_MCP_READ_ONLY':'false',
            'GITHUB_ALLOWED_REPOSITORIES':'fixture/test','MEMORY_ENABLED':'false','MEMORY_AUTO_EXTRACT':'false',
            'SELF_CORRECT_RAG_ENABLED':'false','GROUNDING_CHECK_ENABLED':'false','MULTI_AGENT_REVIEW_POLICY':'disabled',
            'AGENT_MAX_GRAPH_STEPS':'60','LANGSMITH_TRACING':'false'}
        command(['-m','alembic','upgrade','head'],env)
        # Empty test database only: execute downgrade and re-upgrade, preserving all real local data.
        command(['-m','alembic','downgrade','base'],env); command(['-m','alembic','upgrade','head'],env)
        record(2,True,'5b1_0001; empty-fixture downgrade/upgrade passed')
        command(['-m','server.worker','--setup'],env); record(3,True,'official SchemaManager'); record(4,True,'official AsyncPostgresSaver.setup')
        api=start(['-m','evaluation.phase5b1.fixture_process','api'],env); wait(api_ready); record(5,True)
        streamlit=start(['-m','streamlit','run','client/app.py','--server.headless=true','--server.port=8527'],env)
        def streamlit_ready():
            try:return requests.get('http://127.0.0.1:8527/_stcore/health',timeout=1).ok
            except requests.RequestException:return False
        wait(streamlit_ready); record(7,True)
        workspace_id=request('POST','/workspaces',json={'name':'Phase5B1 synthetic fixture'}).json()['id']
        from server.db.runtime import PostgresRuntime
        from server.workspaces import WorkspaceStore,DocumentRecord
        runtime=PostgresRuntime({'DATABASE_URL':url}); manager=runtime
        from server.persistence import now
        document_id=uuid4().hex
        WorkspaceStore(runtime).register(DocumentRecord(id=document_id,workspace_id=workspace_id,filename='paper.pdf',display_name='paper.pdf',
            fingerprint='synthetic-'+run_id,index_id=str(folder/'synthetic-index'),status='ready',page_count=2,created_at=now(),
            metadata={'embedding_model':'synthetic fixture'}))
        settings={'document_id':document_id,'delay':8}; fixture_control()
        long=create('Read persistent fixture twice'); record(8,True,long)
        queued=enqueue(long); record(9,True,report['performance']['enqueue_ms'][-1]); record(10,queued['status']=='QUEUED')
        worker,stopfile=start_worker(); wait(lambda: task(long)['status']=='RUNNING'); record(6,True); record(11,True)
        wait(lambda: step_started(long,1)); streamlit.kill(); streamlit.wait(timeout=15)
        api.kill(); api.wait(timeout=15)
        from server.tasks import TaskStore
        wait(lambda:TaskStore(runtime).get(long).status=='COMPLETED'); record(12,True)
        api=start(['-m','evaluation.phase5b1.fixture_process','api'],env); wait(api_ready)
        record(13,task(long)['status']=='COMPLETED','API was killed while second step ran')
        record(14,task(long)['status']=='COMPLETED','Streamlit process stopped while task ran')
        stop_worker(worker,stopfile); worker,stopfile=start_worker(); wait(lambda:request('GET','/health').json()['worker']=='active'); record(15,True)
        fixture_control(delay=20)
        crashed=create('Read crash recovery fixture twice'); enqueue(crashed); wait(lambda:step_started(crashed,1))
        worker.kill(); worker.wait(timeout=15)
        # Framework defaults: 30-second dead-worker heartbeat cutoff.
        time.sleep(32); fixture_control(delay=1); worker,stopfile=start_worker()
        recovered=wait(lambda:complete(crashed),timeout=90); record(16,recovered['attempt_count']==2)
        ledger=[json.loads(line) for line in (folder/'fixture_events.jsonl').read_text().splitlines()]
        first_count=sum(v['kind']=='research_call' and v['query']=='first Read crash recovery fixture twice' for v in ledger)
        record(17,first_count==1,{'completed_first_step_calls':first_count})
        fixture_control(delay=5)
        paused=create('Read pause fixture twice'); enqueue(paused); wait(lambda:step_started(paused,0) or step_started(paused,1))
        request('POST',f'/tasks/{paused}/pause',json={}); wait(lambda:task(paused)['status']=='PAUSED'); record(18,True)
        enqueue(paused,resume=True); wait(lambda:complete(paused)); record(19,True)
        stop_worker(worker,stopfile)
        cancelled=create('Read cancelled fixture'); enqueue(cancelled); request('POST',f'/tasks/{cancelled}/cancel',json={})
        worker,stopfile=start_worker(); time.sleep(1); record(20,task(cancelled)['status']=='CANCELLED' and task(cancelled)['attempt_count']==0)
        write=create('Create synthetic issue',source_scope='external'); enqueue(write); wait(lambda:task(write)['status']=='WAITING_USER'); record(21,True)
        approvals=request('GET','/approvals',params={'task_id':write}).json()['approvals']; approval=approvals[0]
        stop_worker(worker,stopfile)
        response=request('POST','/approvals/'+approval['id']+'/approve',json={})
        record(22,task(write)['status']=='QUEUED'); worker,stopfile=start_worker(); wait(lambda:complete(write))
        action_data=json.loads((folder/'actions.json').read_text()); record(23,len(action_data)==1,{'write_execution_count':len(action_data)})
        asset=request('POST',f'/workspaces/{workspace_id}/datasets',files={'file':('experiment.csv',b'model,params_m,miou\nA,2.5,73.27\nB,3.2,76\nC,2,70\n','text/csv')}).json()
        multi=create('Compare papers with experiment.csv data and find best configuration under 3M parameters and generate a chart')
        enqueue(multi); result=wait(lambda:complete(multi),timeout=120)
        delegations=request('GET',f'/tasks/{multi}/delegations').json()
        agents=delegations.get('delegations',delegations) if isinstance(delegations,dict) else delegations
        record(24,{'research','data'}.issubset({v['agent_id'] for v in agents}),{'agents':[v['agent_id'] for v in agents],'status':result['status']})
        stop_worker(worker,stopfile); api.kill(); api.wait(timeout=15)
        api=start(['-m','evaluation.phase5b1.fixture_process','api'],env); wait(api_ready); worker,stopfile=start_worker()
        record(25,task(multi)['status']=='COMPLETED' and task(write)['status']=='COMPLETED')
        with runtime.sync_engine.connect() as db:
            report['performance']['queue_wait_ms']=[v[0].get('queue_wait_ms') for v in db.execute(text("SELECT payload->'metadata' FROM tasks WHERE status='COMPLETED'"))]
        # Full migration fixture into another new empty DB; preserve every source ID.
        from server.sessions import SessionStore
        from server.memory import MemoryStore,LongTermMemory
        from server.tool_actions import ToolActionStore
        from server.agent_runs import AgentRunStore
        source=folder/'migration-source.sqlite3'; source_ws=WorkspaceStore(source).create('Migration fixture')
        SessionStore(source).create(session_id='session-fixture',file_id='',file_name='',pdf_path='',chroma_dir='',workspace_id=source_ws.id)
        source_tasks=TaskStore(source); source_task=source_tasks.create(__import__('server.tasks',fromlist=['Task']).Task(workspace_id=source_ws.id,session_id='session-fixture',goal='Migration fixture'))
        source_tasks.set_plan(source_task.id,{'steps':[{'description':'Read fixture'}]})
        MemoryStore(source,{}).create(LongTermMemory(content='Use concise tables',memory_type='instruction',scope_type='workspace',scope_id=source_ws.id))
        from evaluation.phase5b1.migration_fixture import add_approval_and_delegation
        from server.agent_registry import default_registry
        add_approval_and_delegation(source, source_task, default_registry({}),
                                   __import__('server.tool_policy',fromlist=['SecretFilter']).SecretFilter())
        migration_db='phase5b1_migration_'+run_id
        admin=create_engine(driver_url(base_url),poolclass=NullPool,isolation_level='AUTOCOMMIT')
        with admin.connect() as db: db.execute(text('CREATE DATABASE '+migration_db))
        admin.dispose(); migration_url=driver_url(base_url).set(database=migration_db).render_as_string(hide_password=False)
        migration_env={**env,'DATABASE_URL':migration_url}; command(['-m','alembic','upgrade','head'],migration_env)
        from scripts.migrate_sqlite_to_postgres import migrate
        before=source.read_bytes(); dry=migrate([source],migration_url,dry_run=True,report_path=folder/'fixture-dry-run.md'); record(26,dry['outcome']=='DRY RUN VALIDATED' and source.read_bytes()==before)
        imported=migrate([source],migration_url,report_path=ROOT/'docs/generated/POSTGRES_MIGRATION_REPORT.md'); record(27,imported['outcome']=='VERIFIED')
        record(28,all(row['verified']=='PASS' for row in imported['tables'].values()),{'source_rows':sum(row['source_rows'] for row in imported['tables'].values()),'tables':len(imported['tables'])})
        stop_worker(worker,stopfile)
        duplicate=create('Read double enqueue fixture')
        with ThreadPoolExecutor(2) as pool:
            responses=list(pool.map(lambda _:requests.post(base+f'/tasks/{duplicate}/run',json={},timeout=10),range(2)))
        record(29,sorted(r.status_code for r in responses)==[202,409])
        worker,stopfile=start_worker(); wait(lambda:request('GET','/health').json()['worker']=='active')
        health=request('GET','/health/ready').json(); record(30,health['database']=='ok' and health['queue']=='ok' and health['worker']=='active',health)
        wait(lambda:complete(duplicate))
        # Classified retry and external-write crash are additional required safety checks.
        fixture_control(delay=.2)
        retry=create('Transient fixture read twice'); enqueue(retry)
        retried=wait(lambda:complete(retry))
        invalid=create('Invalid fixture input'); enqueue(invalid)
        wait(lambda:task(invalid)['status']=='FAILED')
        time.sleep(3)
        report['retry_checks']={'transient_read':{'status':'PASS' if retried['attempt_count']==2 else 'FAIL','attempts':retried['attempt_count']},
            'invalid_input':{'status':'PASS' if task(invalid)['attempt_count']==1 else 'FAIL','attempts':task(invalid)['attempt_count']}}
        uncertain=create('Create ambiguous synthetic issue',source_scope='external'); enqueue(uncertain)
        wait(lambda:task(uncertain)['status']=='WAITING_USER')
        pending=request('GET','/approvals',params={'task_id':uncertain}).json()['approvals'][0]
        stop_worker(worker,stopfile); fixture_control(ambiguous_write=True)
        request('POST','/approvals/'+pending['id']+'/approve',json={}); worker,stopfile=start_worker()
        wait(lambda:(folder/'write_sent').exists())
        before_recovery=len(json.loads((folder/'actions.json').read_text()))
        worker.kill(); worker.wait(timeout=15); time.sleep(32); fixture_control(ambiguous_write=False)
        worker,stopfile=start_worker(); wait(lambda:task(uncertain)['status']=='REQUIRES_RECONCILIATION')
        time.sleep(2)
        writes=len(json.loads((folder/'actions.json').read_text()))
        report['retry_checks']['ambiguous_write']={'status':'PASS' if writes==before_recovery else 'FAIL',
            'task_status':task(uncertain)['status'],'writes_before_recovery':before_recovery,'writes_after_recovery':writes}
        if any(row['status']!='PASS' for row in report['retry_checks'].values()): raise AssertionError('Retry safety validation failed')
        # Measured smoke latency only; no performance superiority claim.
        from datetime import datetime
        report['performance']['worker_startup_to_run_ms']=round((datetime.fromisoformat(task(long)['worker_started_at']).timestamp()-worker_starts[0])*1000,3)
        sqlite_bench=TaskStore(folder/'sqlite-benchmark.sqlite3'); from server.tasks import Task
        samples=[]
        for _ in range(10):
            started=time.perf_counter(); sqlite_bench.create(Task(workspace_id='benchmark',session_id='benchmark',goal='SQLite latency fixture'))
            samples.append(round((time.perf_counter()-started)*1000,3))
        report['performance']['sqlite_task_create_ms']=samples
        samples=[]
        benchmark_session_id = task(long)['session_id']
        for _ in range(10):
            started=time.perf_counter(); TaskStore(runtime).create(Task(workspace_id=workspace_id,session_id=benchmark_session_id,goal='Postgres latency fixture'))
            samples.append(round((time.perf_counter()-started)*1000,3))
        report['performance']['postgres_task_create_ms']=samples
        # Existing local data: snapshot into copies using SQLite backup, then dry-run.
        existing=ROOT/'.rag_workspace'; copied=[]; original_hashes={}
        import hashlib
        for name in ('sessions.sqlite3','indexes.sqlite3'):
            path=existing/name
            if path.exists():
                original_hashes[name]=hashlib.sha256(path.read_bytes()).hexdigest()
                backup=folder/('existing-'+name)
                src=sqlite3.connect(path.as_uri()+'?mode=ro',uri=True); dst=sqlite3.connect(backup)
                src.backup(dst); dst.close(); src.close(); copied.append(backup)
        if copied:
            try:
                local_db='phase5b1_local_copy_'+run_id
                admin=create_engine(driver_url(base_url),poolclass=NullPool,isolation_level='AUTOCOMMIT')
                with admin.connect() as db: db.execute(text('CREATE DATABASE '+local_db))
                admin.dispose(); local_url=driver_url(base_url).set(database=local_db).render_as_string(hide_password=False)
                command(['-m','alembic','upgrade','head'],{**env,'DATABASE_URL':local_url})
                local=migrate(copied,local_url,dry_run=True,report_path=ROOT/'docs/generated/POSTGRES_EXISTING_DATA_DRY_RUN.md')
                report['existing_local_migration']={'outcome':local['outcome'],'warnings':local['warnings']}
                if local['outcome']=='DRY RUN VALIDATED':
                    local_import=migrate(copied,local_url,report_path=ROOT/'docs/generated/POSTGRES_EXISTING_DATA_MIGRATION.md')
                    report['existing_local_migration'].update(import_outcome=local_import['outcome'],
                        source_rows=sum(row['source_rows'] for row in local_import['tables'].values()),
                        destination_rows=sum(row['destination_rows'] for row in local_import['tables'].values()),
                        all_tables_verified=all(row['verified']=='PASS' for row in local_import['tables'].values()))
            except Exception as error:
                report['existing_local_migration']={'outcome':'BLOCKED','reason':str(error)}
            report['original_sqlite_unchanged']=all(hashlib.sha256((existing/n).read_bytes()).hexdigest()==h for n,h in original_hashes.items())
        save()
    except Exception as error:
        report['error_type']=type(error).__name__; report['error']=str(error); save(); raise
    finally:
        for process in processes:
            if process.poll() is None: process.kill(); process.wait(timeout=15)
        for file in handles: file.close()
        if manager: manager.sync_engine.dispose()
        save()


if __name__=='__main__': run()
