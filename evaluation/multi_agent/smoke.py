"""Real backend/LLM/SDK/restart smoke, with documented synthetic/fault fixtures."""
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from uuid import uuid4
import requests
from evaluation.phase3_smoke import fixture_pdf, CSV
from evaluation.multi_agent.metrics import measured_run
from pypdf import PdfReader, PdfWriter
from pypdf.generic import DecodedStreamObject, NameObject

ROOT = Path(__file__).resolve().parents[2]
OUTPUT = ROOT/'evaluation/results/phase5a/runtime_smoke.json'
COMPARE = ROOT/'evaluation/results/phase5a/real_comparison.json'


def paper_fixture(index):
    writer = PdfWriter()
    writer.clone_document_from_reader(PdfReader(BytesIO(fixture_pdf())))
    page = writer.pages[0]
    stream = DecodedStreamObject()
    method = {1:'Method A: convolutional segmentation, 2.5M parameters, mIoU 73.27.',
        2:'Method B: attention segmentation, 3.2M parameters, mIoU 76.0.',
        3:'Method C: compact convolution baseline, 2.0M parameters, mIoU 70.0.'}[index]
    text = page.get_contents().get_data() + (f'\nBT /F1 12 Tf 40 230 Td (Synthetic Paper {index}. {method}) Tj '
        '0 -20 Td (All synthetic methods use the same fixture benchmark and evaluation protocol.) Tj '
        '0 -20 Td (A improves mIoU over C by 3.27 points at 0.5M more parameters.) Tj '
        '0 -20 Td (B improves mIoU over A by 2.73 points at 0.7M more parameters.) Tj '
        '0 -20 Td (Claims: descriptive quality/cost tradeoff only; no causal or ablation claim.) Tj '
        '0 -20 Td (Three fixture configurations, no uncertainty estimates; not published research.) Tj ET').encode()
    stream.set_data(text)
    page[NameObject('/Contents')] = writer._add_object(stream)
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


def run():
    resume = os.environ.get('PHASE5A_SMOKE_RESUME_FOLDER')
    folder = Path(resume).resolve() if resume else ROOT/'.runtime/phase5a_smoke'/str(uuid4())
    if resume and (folder.parent != (ROOT/'.runtime/phase5a_smoke').resolve() or not folder.is_dir()):
        raise ValueError('Resume must reference an existing isolated Phase5A smoke folder')
    folder.mkdir(parents=True, exist_ok=True)
    OUTPUT.parent.mkdir(parents=True,exist_ok=True)
    ledger = folder/('actions-'+str(uuid4())+'.json' if resume else 'actions.json')
    config = folder/'mcp.json'
    restart_marker = folder/('data-started-'+str(uuid4()))
    config.write_text(json.dumps([{'id':'github','name':'LOCAL MOCK — never real GitHub','transport':'stdio',
        'command':sys.executable,'args':[str(ROOT/'evaluation/multi_agent/mock_server.py'),'--ledger',str(ledger)]}]),encoding='utf-8')
    env = {**os.environ,'WORKSPACE_DIR':str(folder/'workspace'),'MCP_SERVERS_FILE':str(config),
        'PHASE5A_SMOKE_FOLDER':str(folder),'PHASE5A_SMOKE_RESTART_MARKER':str(restart_marker),'GITHUB_MCP_ENABLED':'false','GITHUB_MCP_READ_ONLY':'false',
        'GITHUB_ALLOWED_REPOSITORIES':'fixture/test','MULTI_AGENT_ENABLED':'true','MODEL_TRUST_ENV':'false',
        'MEMORY_ENABLED':'true','MEMORY_AUTO_EXTRACT':'true','GROUNDING_CHECK_ENABLED':'true',
        'ANONYMIZED_TELEMETRY':'false','AGENT_EXECUTION_TIMEOUT_SECONDS':'600','AGENT_MAX_GRAPH_STEPS':'45'}
    base = 'http://127.0.0.1:8021'
    report = {'run_id':folder.name,'fixture_folder':str(folder),'llm':'configured REAL provider',
        'sources':'synthetic PDFs/XLSX; official-SDK LOCAL MOCK MCP (never GitHub)',
        'faults':'Explicit local injection for missing evidence, deadline and data-branch failure',
        'checks':{},'scenarios':{},'real_github':'SKIPPED: credentials absent; coding uses MOCK', 'external_writes':'LOCAL MOCK ONLY'}
    if resume:
        prior = json.loads(OUTPUT.read_text(encoding='utf-8'))
        if prior['run_id'] != folder.name:
            raise ValueError('Resume report does not match fixture folder')
        report = prior
        report.setdefault('evaluation_resumptions', []).append('Reuse measured scenarios only when their associated acceptance checks passed; retain original latency and task IDs. F completion-cache proof is measured separately in G.')
    processes, handles = [], []
    session = requests.Session()
    session.trust_env = False

    def save(): OUTPUT.write_text(json.dumps(report,indent=2,ensure_ascii=False),encoding='utf-8')
    def check(name, passed, details=None):
        report['checks'][name] = {'status':'PASS' if passed else 'FAIL','details':details}
        save()
        print(name+': '+report['checks'][name]['status'],flush=True)
        if not passed: raise AssertionError(name)

    def start(port=8021, process_env=env, subfolder=folder):
        subfolder.mkdir(parents=True,exist_ok=True)
        handle = (subfolder/f'backend-{len(processes)}.log').open('w',encoding='utf-8')
        handles.append(handle)
        process = subprocess.Popen([sys.executable,'-u','-m','uvicorn','evaluation.multi_agent.smoke_app:app',
            '--host','127.0.0.1','--port',str(port)],cwd=ROOT,env=process_env,stdout=handle,stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
        processes.append(process)
        deadline = time.monotonic()+90
        while time.monotonic()<deadline:
            if process.poll() is not None: raise RuntimeError('Smoke backend exited; inspect local log')
            try:
                if session.get(f'http://127.0.0.1:{port}/health',timeout=1).ok: return process
            except requests.RequestException: pass
            time.sleep(.5)
        raise TimeoutError('Smoke backend start')

    def api(method,path,url=base,**kwargs):
        response = session.request(method,url+path,timeout=650,**kwargs)
        if not response.ok:
            raise RuntimeError(f'{path}: HTTP {response.status_code}: {response.text[:250]}')
        response.raise_for_status()
        return response.json()

    def prepare(url=base):
        wid = api('POST','/workspaces',url=url,json={'name':'Phase5A SYNTHETIC smoke'})['id']
        files = [('files',(f'paper_{i}.pdf',paper_fixture(i),'application/pdf')) for i in range(1,4)]
        docs = api('POST',f'/workspaces/{wid}/documents',url=url,files=files)
        if any(d['status']!='ready' for d in docs['documents']): raise RuntimeError('Synthetic PDF indexing failed')
        import pandas as pd
        xlsx = BytesIO()
        pd.read_csv(BytesIO(CSV)).to_excel(xlsx,index=False,engine='openpyxl')
        asset = api('POST',f'/workspaces/{wid}/datasets',url=url,files={'file':('experiment.xlsx',xlsx.getvalue(),'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')})
        return wid,asset['id']

    def task(goal, scope='workspace_only', wid=None, url=base):
        value = api('POST','/tasks',url=url,json={'workspace_id':wid or workspace_id,'goal':goal,'source_scope':scope})
        started = time.monotonic()
        value = api('POST',f"/tasks/{value['id']}/run",url=url,json={})
        return value,time.monotonic()-started

    def details(value, latency, url=base, usage_folder=folder):
        runs = api('GET',f"/tasks/{value['id']}/agents",url=url)['agents'] if url==base else []
        delegations = api('GET',f"/tasks/{value['id']}/delegations",url=url)['delegations'] if url==base else []
        events = api('GET',f"/tasks/{value['id']}/events",url=url)
        usage = [json.loads(line) for line in (usage_folder/'usage.jsonl').read_text().splitlines()] if (usage_folder/'usage.jsonl').exists() else []
        measured = measured_run(value,runs,delegations,events,usage,latency)
        final = api('GET',f"/tasks/{value['id']}",url=url)
        outputs = [s['output_json'] for s in final['steps'] if s['output_json']]
        last = outputs[-1] if outputs else {}
        measured.update(task_id=value['id'],runs=runs,delegations=delegations,
            citations=last.get('citations',[]),grounding=(last.get('verification_result') or {}).get('grounded'),
            evidence_count=len(last.get('evidence',[])))
        if url!=base:
            measured['tool_calls']=last.get('tool_calls')
            measured['evidence_coverage']['evidence_ids']=[e['evidence_id'] for e in last.get('evidence',[])]
        return measured

    def control(scenario): (folder/'control.json').write_text(json.dumps({'scenario':scenario}),encoding='utf-8')
    def specialists(result): return {r['agent_id'] for r in result['runs'] if r['agent_id'] not in {'supervisor','reviewer'}}

    mixed = 'Compare the methods reported in the three uploaded papers with experiment.xlsx results. Find the best configuration under 3M parameters, generate a chart, and assess whether our results support the reported design advantages. Use only uploaded data and documents; these are synthetic fixtures.'
    try:
        process = start()
        control('none')
        check('1_fastapi_start',True)
        check('2_streamlit_start',session.get('http://127.0.0.1:8501/_stcore/health',timeout=5).ok)
        check('3_agent_registry',{a['agent_id'] for a in api('GET','/agents')['agents']}=={'supervisor','research','data','coding','reviewer'})
        if resume and 'A_research_only' in report['scenarios']:
            workspace_id = api('GET', '/tasks/'+report['scenarios']['A_research_only']['task_id'])['workspace_id']
            dataset_id = api('GET', f'/workspaces/{workspace_id}/datasets')[0]['id']
        else:
            workspace_id,dataset_id = prepare()
        simple,_ = task('Hello, respond briefly.')
        check('4_simple_bypasses_orchestration',not api('GET',f"/tasks/{simple['id']}/delegations")['delegations'])
        if resume and report['checks'].get('6_research_only',{}).get('status') == 'PASS':
            a = report['scenarios']['A_research_only']
            research,latency = api('GET','/tasks/'+a['task_id']),a['latency_seconds']
        else:
            research,latency = task('Compare the methods and experimental results of the papers in this workspace.')
        a = details(research,latency)
        report['scenarios']['A_research_only'] = a
        check('6_research_only',specialists(a)=={'research'} and research['status']=='COMPLETED')
        if resume and report['checks'].get('5_data_only',{}).get('status') == 'PASS':
            b = report['scenarios']['B_data_only']
            data,latency = api('GET','/tasks/'+b['task_id']),b['latency_seconds']
        else:
            data,latency = task('Using experiment.xlsx, find the best configuration under 3M parameters and generate a chart.')
        b = details(data,latency)
        report['scenarios']['B_data_only'] = b
        check('5_data_only',specialists(b)=={'data'} and bool(api('GET',f"/tasks/{data['id']}/artifacts")))
        if resume and report['checks'].get('20_grounding',{}).get('status') == 'PASS':
            c = report['scenarios']['C_research_data']
            value,latency = api('GET','/tasks/'+c['task_id']),c['latency_seconds']
        else:
            value,latency = task(mixed)
        c = details(value,latency)
        report['scenarios']['C_research_data'] = c
        check('7_multi_agent',specialists(c)=={'research','data'})
        intervals = [(r['agent_id'],r['started_at'],r['completed_at']) for r in c['runs'] if r['agent_id'] in {'research','data'} and r['metadata'].get('execution_group')==0 and not r['metadata'].get('revision_of')]
        overlap = len(intervals)==2 and max(i[1] for i in intervals)<min(i[2] for i in intervals)
        check('8_parallel_overlap',overlap,{'intervals':intervals,'measured_duration_sum_seconds':c['agent_duration_sum_seconds'],'measured_parallel_span_seconds':c['agent_duration_span_seconds']})
        check('10_evidence_merge',c['evidence_count']>=2 and bool(c['citations']))
        review = next(d for d in c['delegations'] if d['agent_id']=='reviewer')
        if review['result_json']['payload']['verdict'] == 'pass':
            check('12_reviewer_pass',True,{'case':'C_research_data'})
        else:
            from evaluation.multi_agent.review_probe import run as review_probe
            probe = review_probe(folder/'review_probe.sqlite3')
            report['scenarios']['complete_reviewer_contract_probe'] = probe
            check('12_reviewer_pass',probe['verdict']=='pass',{'case':'actual LLM controlled complete-result Reviewer subgraph; C retains its measured revision verdict'})
        check('20_grounding',c['grounding'] is True)
        check('21_citations',bool(c['citations']) and all(i['evidence_id'] in c['evidence_coverage']['evidence_ids'] for i in c['citations']))
        check('22_memory_once',all(r['agent_id']=='supervisor' or r['metadata'].get('memory_extraction_started') is None for r in c['runs']) and
            next(r for r in c['runs'] if r['agent_id']=='supervisor')['metadata'].get('memory_extraction_completed') is True)
        control('none')
        if resume and report['checks'].get('D_coding_mock_read',{}).get('status') == 'PASS':
            d = report['scenarios']['D_code_review_MOCK']
            coding,latency = api('GET','/tasks/'+d['task_id']),d['latency_seconds']
        else:
            coding,latency = task('Review the Agent workflow implementation at server/agent/graph.py in owner fixture repository test. Use LOCAL MOCK GitHub MCP reads only; identify architecture risks. Do not write.',scope='external')
        d = details(coding,latency)
        report['scenarios']['D_code_review_MOCK'] = d
        check('D_coding_mock_read',specialists(d)=={'coding'} and any(x['result_json']['evidence_ids'] for x in d['delegations']))
        if resume and report['checks'].get('E_mixed_research_code',{}).get('status') == 'PASS':
            e = report['scenarios']['E_research_code_MOCK']
            mixed_code,latency = api('GET','/tasks/'+e['task_id']),e['latency_seconds']
        else:
            mixed_code,latency = task('Compare the design described in the uploaded documentation/papers with the actual implementation in owner fixture repository test, path server/agent/graph.py. Use LOCAL MOCK MCP only for code; identify mismatches. Do not write.',scope='workspace_and_external')
        report['scenarios']['E_research_code_MOCK'] = details(mixed_code,latency)
        check('E_mixed_research_code',specialists(report['scenarios']['E_research_code_MOCK'])=={'research','coding'})
        control('gap')
        if resume and all(report['checks'].get(k,{}).get('status') == 'PASS' for k in ('13_reviewer_revision','14_bounded_redelegation')):
            f = report['scenarios']['F_injected_research_gap']
            gap,latency = api('GET','/tasks/'+f['task_id']),f['latency_seconds']
        else:
            gap,latency = task(mixed)
        f = details(gap,latency)
        report['scenarios']['F_injected_research_gap'] = f
        gap_review = next(x for x in f['delegations'] if x['agent_id']=='reviewer')
        revisions = [r for r in f['runs'] if r['metadata'].get('revision_of')]
        check('13_reviewer_revision',gap_review['result_json']['payload']['verdict']=='needs_revision')
        check('14_bounded_redelegation',0<len(revisions)<=2 and sum(r['agent_id']=='reviewer' for r in f['runs'])==1)
        control('timeout')
        if resume and report['checks'].get('16_agent_timeout',{}).get('status') == 'PASS':
            t = report['scenarios']['timeout_injected']
            timed,latency = api('GET','/tasks/'+t['task_id']),t['latency_seconds']
        else:
            timed,latency = task(mixed)
        t = details(timed,latency)
        report['scenarios']['timeout_injected'] = t
        check('16_agent_timeout',any(r['error_type']=='TimeoutError' for r in t['runs']) and timed['status']=='COMPLETED')
        control('branch_failure')
        if resume and report['checks'].get('17_parallel_branch_failure',{}).get('status') == 'PASS':
            partial = report['scenarios']['branch_failure_injected']
            failed,latency = api('GET','/tasks/'+partial['task_id']),partial['latency_seconds']
        else:
            failed,latency = task(mixed)
        partial = details(failed,latency)
        report['scenarios']['branch_failure_injected'] = partial
        check('17_parallel_branch_failure',any(r['agent_id']=='research' and r['status'] in {'COMPLETED','PARTIAL'} and r['metadata'].get('evidence_count',0)>0 for r in partial['runs']) and failed['status']=='COMPLETED',
            {'criterion':'Healthy research evidence retained; failed data work is disclosed; terminal partial research is allowed.'})
        control('none')
        action_goal = 'Compare the uploaded papers with experiment.xlsx data and then create ONE synthetic issue in owner fixture repository test using LOCAL MOCK MCP. Title: Phase5A smoke. Body: Synthetic local test only. This is an explicit requested issue creation; no other writes.'
        action,latency = task(action_goal,scope='workspace_and_external')
        approvals = api('GET','/approvals',params={'task_id':action['id']})['approvals']
        check('18_hitl_waiting',action['status']=='WAITING_USER' and len(approvals)==1 and not ledger.exists())
        process.terminate(); process.wait(timeout=20)
        process = start()
        api('POST',f"/approvals/{approvals[0]['id']}/approve")
        approved = api('GET',f"/tasks/{action['id']}")
        check('18_hitl_resume',approved['status']=='COMPLETED' and len(json.loads(ledger.read_text()))==1)
        control('restart')
        restart_goal = 'Compare only the reported parameter counts and mIoU values in the three synthetic papers, and independently analyze experiment.xlsx to select the highest mIoU with params_m < 3 and generate a chart. No architecture, training, causal or uncertainty claim is requested.'
        restart_task = api('POST','/tasks',json={'workspace_id':workspace_id,'goal':restart_goal,'source_scope':'workspace_only'})
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(api,'POST',f"/tasks/{restart_task['id']}/run",json={})
            deadline = time.monotonic()+100
            before = []
            while time.monotonic()<deadline:
                before = api('GET',f"/tasks/{restart_task['id']}/agents")['agents']
                if restart_marker.exists() and any(r['agent_id']=='research' and r['status']=='COMPLETED' for r in before): break
                if future.done(): raise RuntimeError('Restart target finished before interruption')
                time.sleep(.5)
            else: raise TimeoutError('Could not observe completed research + running data')
            process.terminate(); process.wait(timeout=20)
            try: future.result(timeout=10)
            except requests.RequestException: pass
        process = start()
        check('19_restart_paused',api('GET',f"/tasks/{restart_task['id']}")['status']=='PAUSED')
        resume_start = time.monotonic()
        resumed = api('POST',f"/tasks/{restart_task['id']}/resume",json={})
        g = details(resumed,time.monotonic()-resume_start)
        g['latency_scope'] = 'resume request only'
        report['scenarios']['G_restart'] = g
        starts = [e for e in api('GET',f"/tasks/{restart_task['id']}/events") if e['event_type']=='DELEGATION_STARTED' and e['payload_json'].get('agent_id')=='research']
        check('19_restart_resume',resumed['status']=='COMPLETED' and len(starts)==1,{'before':before,'research_execution_count':len(starts),'after':g['runs']})
        check('15_completed_work_not_repeated',f['duplicate_work_rate']==0 and len(starts)==1,
            {'F_data_statuses':[r['status'] for r in f['runs'] if r['agent_id']=='data'],
             'F_completed_delegation_repeats':f['duplicate_work_rate'],'G_completed_research_execution_count':len(starts),
             'criterion':'New gap delegations for PARTIAL work are permitted; completed delegation IDs must stay cached.'})
        control('none')
        # Scope keys and public API contracts are measured; no raw prompt logging.
        from server.agent_runs import AgentRunStore
        from server.tool_policy import SecretFilter
        store = AgentRunStore(folder/'workspace/sessions.sqlite3',SecretFilter())
        requests_saved = [x['request'] for x in store.delegations(task_id=value['id'])]
        private_probes = [json.loads(line) for line in (folder/'usage.jsonl').read_text().splitlines()]
        private_probes = [p for p in private_probes if p.get('task_id') in {value['id'],g['task_id']} and p['event']=='llm_start']
        isolated = any(p['agent_id']=='research' and p['contains_private_marker'] for p in private_probes) and all(
            not p['contains_private_marker'] for p in private_probes if p['agent_id']!='research')
        check('9_private_context_isolation',isolated and all(not {'messages','private_messages','all_agent_messages'} & set(r['context']) for r in requests_saved),
            {'private_research_sentinel_measured_in':'C_research_data/G_restart','other_agents_saw_sentinel':not isolated})
        check('11_reviewer_structured_input',all('private_messages' not in json.dumps(r) for r in requests_saved if r['agent_id']=='reviewer') and
            any(p['agent_id']=='reviewer' for p in private_probes) and all(not p['contains_private_marker'] for p in private_probes if p['agent_id']=='reviewer'))
        # Actual existing-workflow comparison against the same synthetic sources.
        single_folder = folder/'single'
        single_env = {**env,'WORKSPACE_DIR':str(single_folder/'workspace'),'PHASE5A_SMOKE_FOLDER':str(single_folder),'MULTI_AGENT_ENABLED':'false'}
        start(8022,single_env,single_folder)
        single_wid,_ = prepare('http://127.0.0.1:8022')
        baseline,latency = task(mixed,wid=single_wid,url='http://127.0.0.1:8022')
        single_result = details(baseline,latency,url='http://127.0.0.1:8022',usage_folder=single_folder)
        comparison_wid,_ = prepare()
        comparison_multi,latency = task(mixed,wid=comparison_wid)
        multi_result = details(comparison_multi,latency)
        report['scenarios']['fresh_multi_comparison'] = multi_result
        COMPARE.write_text(json.dumps({'fixture':'same synthetic 3 PDFs + experiment.xlsx; actual configured LLM; n=1 per mode',
            'single_agent':single_result,'multi_agent':multi_result,'interpretation':'Fresh workspace per comparison mode; different plans/call counts and possible user-scope memory history. Measured sample only; no general superiority claim.'},indent=2,ensure_ascii=False),encoding='utf-8')
        check('23_single_multi_runner',baseline['status']=='COMPLETED' and COMPARE.is_file())
        save()
        return report
    finally:
        save()
        for process in processes:
            if process.poll() is None:
                process.terminate()
                try: process.wait(timeout=20)
                except subprocess.TimeoutExpired: process.kill(); process.wait(timeout=10)
        for handle in handles: handle.close()


if __name__=='__main__':
    run()
