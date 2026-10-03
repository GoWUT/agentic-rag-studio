"""Two fresh-workspace actual runs, targeted repair and real budget containment."""
from io import BytesIO
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from uuid import uuid4
import requests
from evaluation.multi_agent_efficiency.fixtures import GOAL, CSV, paper_fixture
from evaluation.multi_agent_efficiency.metrics import quality

ROOT=Path(__file__).resolve().parents[2]
RESULTS=ROOT/'evaluation/results/phase5a5'


def run():
    folder=ROOT/'.runtime/phase5a5'/str(uuid4());folder.mkdir(parents=True)
    RESULTS.mkdir(parents=True,exist_ok=True)
    env={**os.environ,'WORKSPACE_DIR':str(folder/'workspace'),'PHASE5A_SMOKE_FOLDER':str(folder),
        'MULTI_AGENT_ENABLED':'true','MULTI_AGENT_EFFICIENCY_ENABLED':'true','MULTI_AGENT_REVIEW_POLICY':'risk_based',
        'GITHUB_MCP_ENABLED':'false','MCP_SERVERS_FILE':'','MODEL_TRUST_ENV':'false','ANONYMIZED_TELEMETRY':'false',
        'MEMORY_ENABLED':'true','MEMORY_AUTO_EXTRACT':'true','GROUNDING_CHECK_ENABLED':'true',
        'AGENT_EXECUTION_TIMEOUT_SECONDS':'600','MULTI_AGENT_MAX_WALL_TIME_SECONDS':'300','AGENT_MAX_GRAPH_STEPS':'55'}
    base='http://127.0.0.1:8025';session=requests.Session();session.trust_env=False
    handle=(folder/'backend.log').open('w',encoding='utf-8')
    process=subprocess.Popen([sys.executable,'-u','-m','uvicorn','evaluation.multi_agent_efficiency.smoke_app:app','--host','127.0.0.1','--port','8025'],cwd=ROOT,env=env,stdout=handle,stderr=subprocess.STDOUT,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
    smoke={'fixture_folder':str(folder),'provider':'REAL configured provider; synthetic PDFs/XLSX','checks':{},'runs':[]}
    def api(method,path,**kwargs):
        response=session.request(method,base+path,timeout=650,**kwargs);response.raise_for_status();return response.json()
    def save():
        name='budget_runtime.json' if os.environ.get('PHASE5A5_RUN_SET')=='budget_only' else 'runtime_smoke.json'
        (RESULTS/name).write_text(json.dumps(smoke,indent=2,ensure_ascii=False),encoding='utf-8')
    def check(name,value,details=None):
        smoke['checks'][name]={'status':'PASS' if value else 'FAIL','details':details};save();print(name+': '+smoke['checks'][name]['status'],flush=True)
    def prepare():
        wid=api('POST','/workspaces',json={'name':'Phase5A5 SYNTHETIC benchmark'})['id']
        documents=api('POST',f'/workspaces/{wid}/documents',files=[('files',(f'paper_{i}.pdf',paper_fixture(i),'application/pdf')) for i in range(1,4)])
        if any(d['status']!='ready' for d in documents['documents']):raise RuntimeError('PDF indexing failed')
        import pandas as pd
        xlsx=BytesIO();pd.read_csv(BytesIO(CSV)).to_excel(xlsx,index=False,engine='openpyxl')
        api('POST',f'/workspaces/{wid}/datasets',files={'file':('experiment.xlsx',xlsx.getvalue(),'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')})
        return wid
    def task(wid,goal=GOAL):
        t=api('POST','/tasks',json={'workspace_id':wid,'goal':goal,'source_scope':'workspace_only'})
        start=time.monotonic();t=api('POST',f"/tasks/{t['id']}/run",json={});latency=time.monotonic()-start
        existing=json.loads((folder/'control.json').read_text()).get('scenario')=='existing_single'
        runs=api('GET',f"/tasks/{t['id']}/agents")['agents'] if not existing else []
        delegations=api('GET',f"/tasks/{t['id']}/delegations")['delegations'] if not existing else []
        root=next((r for r in runs if r['agent_id']=='supervisor'),None)
        final=api('GET',f"/tasks/{t['id']}");outputs=[s['output_json'] for s in final['steps'] if s['output_json']];last=outputs[-1] if outputs else {}
        evidence=last.get('evidence',[]);artifacts=api('GET',f"/tasks/{t['id']}/artifacts")
        events=[json.loads(l) for l in (folder/'usage.jsonl').read_text().splitlines()] if (folder/'usage.jsonl').exists() else []
        starts=[e for e in events if e.get('task_id')==t['id'] and e['event']=='llm_start'];ends=[e for e in events if e.get('task_id')==t['id'] and e['event']=='llm_end']
        tokens=sum(e['tokens'] for e in ends) if len(starts)==len(ends) and all(e['tokens'] is not None for e in ends) else None
        value={'task_id':t['id'],'status':t['status'],'goal':goal,'latency_seconds':round(latency,3),'llm_calls':len(starts),
            'tool_calls':sum(r['tool_calls'] for r in runs) if runs else last.get('tool_calls'),'tokens':tokens,'evidence_count':len(evidence),'citations':last.get('citations',[]),
            'answer':t['metadata'].get('answer',''),'grounded':(last.get('verification_result') or {}).get('grounded'),
            'cost_trace':root['metadata'].get('cost_trace') if root else None,'runs':runs,'delegations':delegations,
            'artifacts':artifacts,'evidence':evidence}
        value['quality']=quality(value['answer'],evidence,value['citations'],artifacts,value['grounded'])
        return value
    try:
        deadline=time.monotonic()+90
        while time.monotonic()<deadline:
            if process.poll() is not None:raise RuntimeError('Backend exited; inspect isolated log')
            try:
                if session.get(base+'/health',timeout=1).ok:break
            except requests.RequestException:pass
            time.sleep(.5)
        else:raise TimeoutError('Backend startup')
        check('fastapi_start',True)
        check('streamlit_start',session.get('http://127.0.0.1:8501/_stcore/health',timeout=5).ok)
        check('five_agents_only',len(api('GET','/agents')['agents'])==5)
        if os.environ.get('PHASE5A5_RUN_SET')=='budget_only':
            (folder/'control.json').write_text(json.dumps({'scenario':'hard_budget'}))
            bounded=task(prepare());smoke['hard_budget']=bounded
            check('hard_budget_no_500',bounded['status']=='COMPLETED' and bounded['cost_trace']['budget_exhausted'] and bounded['llm_calls']<=2)
            check('budget_retains_completed_data',any(r['agent_id']=='data' and r['status']=='COMPLETED' for r in bounded['runs']))
            check('budget_partial_calculation_visible','Budget exhausted' in bounded['answer'] and '73.27' in bounded['answer'] and bounded['grounded'] is False)
            return smoke
        for i in range(1,3):
            (folder/'control.json').write_text(json.dumps({'scenario':'none'}))
            value=task(prepare());smoke['runs'].append(value)
            (RESULTS/f'optimized_run_{i}.json').write_text(json.dumps(value,indent=2,ensure_ascii=False),encoding='utf-8')
            q=value['quality']
            check(f'optimized_run_{i}',value['status']=='COMPLETED' and q['core_answer_correct'] and q['grounded'] and q['citation_validity'] and all(q['required_evidence_coverage'].values()),{'quality':q,'latency':value['latency_seconds'],'calls':value['llm_calls'],'tokens':value['tokens']})
        wid=prepare();(folder/'control.json').write_text(json.dumps({'scenario':'target_gap'}))
        repair=task(wid);smoke['targeted_repair']=repair
        revised=[d for d in repair['delegations'] if any(r['delegation_id']==d['id'] and r['metadata'].get('revision_of') for r in repair['runs'])]
        stats={'requested':len(revised),'completed':sum(d['status']=='COMPLETED' for d in revised),'failed':sum(d['status']!='COMPLETED' for d in revised),
            'failure_reasons':[d['result_json']['unresolved_questions'] for d in revised if d['status']!='COMPLETED']}
        smoke['supplementary_stats']=stats
        check('targeted_repair',stats['requested']>0 and stats['completed']==stats['requested'],stats)
        check('review_not_disabled',any(r['agent_id']=='reviewer' and r['llm_calls'] for r in repair['runs']))
        check('completed_data_not_repeated',sum(r['agent_id']=='data' for r in repair['runs'])==1)
        # Actual new targeted tool call must restrict retrieval to paper_3, not all papers.
        from server.agent_runs import AgentRunStore
        from server.tool_policy import SecretFilter
        store=AgentRunStore(folder/'workspace/sessions.sqlite3',SecretFilter())
        calls=[store.run(d['agent_run_id'])['metadata']['progress'].get('work_plan',{}).get('steps',[]) for d in store.delegations(task_id=repair['task_id']) if d['request'].get('revision_of') and d['agent_id']=='research']
        check('target_document_only',bool(calls) and all(len(s.get('arguments',{}).get('document_ids',[]))==1 for steps in calls for s in steps if s['capability']=='workspace.search'),calls)
        (folder/'control.json').write_text(json.dumps({'scenario':'none'}))
        simple=task(wid,'Find the best configuration under 3M parameters in experiment.xlsx and generate a chart')
        smoke['single_data']=simple
        check('clean_data_review_skipped',simple['cost_trace']['review_status']=='skipped')
        check('deterministic_data',all(r['llm_calls']==0 for r in simple['runs'] if r['agent_id']=='data'))
        (folder/'control.json').write_text(json.dumps({'scenario':'hard_budget'}))
        bounded=task(prepare());smoke['hard_budget']=bounded
        check('hard_budget_no_500',bounded['status']=='COMPLETED' and bounded['cost_trace']['budget_exhausted'] and bounded['llm_calls']<=2)
        check('budget_retains_completed_data',any(r['agent_id']=='data' and r['status']=='COMPLETED' for r in bounded['runs']))
        (folder/'control.json').write_text(json.dumps({'scenario':'existing_single'}))
        single=task(prepare());smoke['existing_single']=single
        (RESULTS/'existing_single_current.json').write_text(json.dumps(single,indent=2,ensure_ascii=False),encoding='utf-8')
        q=single['quality']
        check('existing_single_current',single['status']=='COMPLETED' and q['core_answer_correct'] and q['grounded'] and q['citation_validity'] and all(q['required_evidence_coverage'].values()),q)
        save();print('Saved actual runs to '+str(RESULTS),flush=True)
        return smoke
    finally:
        save()
        if process.poll() is None:
            process.terminate()
            try:process.wait(timeout=20)
            except subprocess.TimeoutExpired:process.kill();process.wait(timeout=10)
        handle.close()


if __name__=='__main__':
    result=run()
    raise SystemExit(0 if all(c['status']=='PASS' for c in result['checks'].values()) else 1)
