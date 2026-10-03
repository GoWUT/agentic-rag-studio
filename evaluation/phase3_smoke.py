"""Real HTTP/provider smoke on synthetic, non-sensitive fixtures only."""
from io import BytesIO
import argparse
import json
from pathlib import Path
import time
import requests
from pypdf import PdfWriter
from pypdf.generic import DictionaryObject, NameObject, DecodedStreamObject

ROOT = Path(__file__).resolve().parents[1]
RESULTS = ROOT/'evaluation'/'results'/'phase3'
CSV = b'model,params_m,miou\nA,2.5,73.27\nB,3.2,76.0\nC,2.0,70.0\n'


def fixture_pdf():
    writer=PdfWriter()
    page=writer.add_blank_page(width=500,height=400)
    font=DictionaryObject({NameObject('/Type'):NameObject('/Font'),NameObject('/Subtype'):NameObject('/Type1'),NameObject('/BaseFont'):NameObject('/Helvetica')})
    page[NameObject('/Resources')]=DictionaryObject({NameObject('/Font'):DictionaryObject({NameObject('/F1'):writer._add_object(font)})})
    stream=DecodedStreamObject()
    stream.set_data(b'BT /F1 12 Tf 40 350 Td (Synthetic Phase 3 Research Fixture) Tj 0 -20 Td (Model A: 2.5 million parameters; mIoU 73.27 percent.) Tj 0 -20 Td (Model B: 3.2 million parameters; mIoU 76.0 percent.) Tj 0 -20 Td (Model C: 2.0 million parameters; mIoU 70.0 percent.) Tj 0 -20 Td (These are synthetic test values, not published research claims.) Tj ET')
    page[NameObject('/Contents')]=writer._add_object(stream)
    output=BytesIO()
    writer.write(output)
    return output.getvalue()


def save(name,payload):
    RESULTS.mkdir(parents=True,exist_ok=True)
    if name == 'smoke.json':
        payload['latest_checks'] = list({item['check']:item for item in payload.get('checks',[])}.values())
    (RESULTS/name).write_text(json.dumps(payload,ensure_ascii=False,indent=2),encoding='utf-8')


def run(mode,base):
    session=requests.Session()
    session.trust_env=False
    def call(method,path,**kwargs):
        response=session.request(method,base+path,timeout=300,**kwargs)
        if not response.ok:
            raise RuntimeError(f'{path}: HTTP {response.status_code}: {response.text[:400]}')
        return response.json() if 'application/json' in response.headers.get('content-type','') else response
    if mode=='prepare':
        checks=[]
        def check(name,value):
            checks.append({'check':name,'status':'PASS' if value else 'FAIL'})
        check('FastAPI health',call('GET','/health')['status']=='ok')
        workspace=call('POST','/workspaces',json={'name':'phase3_synthetic_smoke','description':'Synthetic non-sensitive fixtures for real provider smoke'})
        wid=workspace['id']
        pdf=call('POST',f'/workspaces/{wid}/documents',files=[('files',('synthetic_research.pdf',fixture_pdf(),'application/pdf'))])
        check('PDF upload',pdf['documents'][0]['status']=='ready')
        csv=call('POST',f'/workspaces/{wid}/datasets',files={'file':('experiment.csv',CSV,'text/csv')})
        check('CSV upload',csv['row_count']==3)
        import pandas as pd
        excel=BytesIO()
        with pd.ExcelWriter(excel,engine='openpyxl') as writer:
            pd.read_csv(BytesIO(CSV)).to_excel(writer,index=False,sheet_name='Experiments')
            pd.DataFrame({'note':['synthetic fixture']}).to_excel(writer,index=False,sheet_name='Notes')
        xlsx=call('POST',f'/workspaces/{wid}/datasets',files={'file':('experiment.xlsx',excel.getvalue(),'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')})
        check('XLSX multi-sheet upload',len(xlsx['schema_metadata']['sheet_names'])==2)
        memory=call('POST','/memories',json={'content':'Preferred report format is markdown.','memory_type':'preference','workspace_id':wid})
        first=call('POST',f'/workspaces/{wid}/sessions')
        retrieved=call('GET','/memories/retrieve',params={'query':'preferred report format','workspace_id':wid})
        check('Cross-session memory retrieval',any(m['id']==memory['id'] for m in retrieved))
        code="import matplotlib.pyplot as plt\nbest=df[df['params_m']<3].sort_values('miou',ascending=False).iloc[0]\nprint(best['model'], best['miou'])\nplt.bar(df['model'],df['miou'])\nplt.savefig('comparison.png')\nplt.close()"
        analysis=call('POST',f'/workspaces/{wid}/analysis',json={'dataset_ids':[xlsx['id']],'objective':'Choose best mIoU under 3M parameters and chart','code':code})
        check('Real subprocess data analysis',analysis['status']=='completed' and 'A 73.27' in analysis['stdout'])
        artifact=call('GET','/artifacts/'+analysis['artifacts'][0]['id'])
        check('Real PNG artifact download',artifact.content[:8]==b'\x89PNG\r\n\x1a\n')
        unsafe={}
        for name,bad in {'os':'import os','socket':'import socket','host_path':"df.to_csv('C:/Windows/phase3.csv')",'dunder':'df.__class__.__mro__'}.items():
            denied=call('POST',f'/workspaces/{wid}/analysis',json={'dataset_ids':[csv['id']],'objective':'security test','code':bad})
            unsafe[name]=denied['error_type']
            check('Unsafe rejection '+name,denied['error_type']=='UnsafeCode')
        state={'workspace':workspace,'session':first,'csv':csv,'xlsx':xlsx,'memory':memory,'analysis':analysis,'checks':checks,'unsafe':unsafe,'kind':'real_http_and_real_subprocess_synthetic_fixture'}
        save('smoke.json',state)
        print(json.dumps({'checks':checks,'workspace_id':wid},ensure_ascii=False))
    else:
        state=json.loads((RESULTS/'smoke.json').read_text(encoding='utf-8'))
        wid=state['workspace']['id']
        if mode == 'legacy':
            created=call('POST','/upload_pdf',files={'file':('synthetic_research.pdf',fixture_pdf(),'application/pdf')})
            result=call('POST','/chat',json={'session_id':created['session_id'],'source_scope':'workspace_only',
                'message':"According to this uploaded synthetic paper, what are Model A's mIoU and parameter count? Cite the paper."})
            save('chat_legacy.json',result)
            check={'check':'Legacy single-PDF real LLM compatibility','status':'PASS' if '73.27' in result['answer'] and '2.5' in result['answer'] and result['citations'] and result['citations'][0]['page']==1 else 'FAIL'}
            state['checks'].append(check)
            save('smoke.json',state)
            print(json.dumps(check))
            return
        if mode in {'llm','final','data','task'}:
            check_results=[]
            def chat(label,question):
                created=call('POST',f'/workspaces/{wid}/sessions')
                result=call('POST','/chat',json={'session_id':created['session_id'],'workspace_id':wid,'source_scope':'workspace_only','message':question})
                save('chat_'+label+'.json',result)
                return result
            if mode == 'llm':
                memory=chat('memory','What is my preferred report format?')
                check_results.append({'check':'Real LLM memory continuity','status':'PASS' if 'markdown' in memory['answer'].casefold() else 'FAIL'})
                remembered=chat('explicit_memory','Remember my preferred report format is markdown.')
                new_session=chat('explicit_memory_recall','What is my preferred report format?')
                check_results.append({'check':'Explicit remember across sessions','status':'PASS' if 'markdown' in new_session['answer'].casefold() else 'FAIL'})
            if mode != 'task':
                answer=chat('data','Analyze experiment.xlsx: which model has the highest mIoU under 3M parameters? Generate a comparison chart and cite the computed result.')
                check_results.append({'check':'Real LLM code generation, analysis evidence and chart','status':'PASS' if answer['artifacts'] and any(e['source_type']=='analysis' and '73.27' in e['content'] for e in answer['evidence']) and '73.27' in answer['answer'] else 'FAIL'})
            if mode == 'data':
                state['checks']+=check_results
                save('smoke.json',state)
                print(json.dumps({'checks':check_results},ensure_ascii=False))
                return
            task=call('POST','/tasks',json={'workspace_id':wid,'goal':'Compare the synthetic research PDF with experiment.xlsx. Inspect datasets, analyze model results under 3M parameters, generate a chart, then synthesize a concise report.','source_scope':'workspace_only'})
            paused=call('POST',f"/tasks/{task['id']}/run",json={'max_steps':1})
            details=call('GET',f"/tasks/{task['id']}")
            check_results.append({'check':'Task real planner + one step + pause','status':'PASS' if paused['status']=='PAUSED' and details['steps'][0]['status']=='COMPLETED' else 'FAIL'})
            state['task_id']=task['id']
            state['before_restart']=details
            state['checks']+=check_results
            save('smoke.json',state)
            print(json.dumps({'checks':check_results,'task_id':task['id'],'status':paused['status']},ensure_ascii=False))
        elif mode=='resume':
            task_id=state['task_id']
            before=call('GET','/tasks/'+task_id)
            resumed=call('POST','/tasks/'+task_id+'/resume')
            after=call('GET','/tasks/'+task_id)
            artifacts=call('GET',f'/tasks/{task_id}/artifacts')
            checks=[{'check':'Task survives actual backend restart','status':'PASS' if before['status']=='PAUSED' else 'FAIL'},
                    {'check':'Resume completes','status':'PASS' if resumed['status']=='COMPLETED' else 'FAIL'},
                    {'check':'Completed step not repeated','status':'PASS' if after['steps'][0]['attempt_count']==state['before_restart']['steps'][0]['attempt_count'] else 'FAIL'},
                    {'check':'Task artifact lineage','status':'PASS' if artifacts and all(a['task_id']==task_id for a in artifacts) else 'FAIL'}]
            final_output=after['steps'][-1]['output_json'] if after['steps'] else {}
            analysis_evidence=[e for e in final_output.get('evidence',[]) if e.get('source_type')=='analysis']
            checks.append({'check':'Task dataset filename and artifact provenance','status':'PASS' if analysis_evidence and any('experiment.xlsx' in e['content'] and 'comparison.png' in e['content'] for e in analysis_evidence) else 'FAIL'})
            checks.append({'check':'Task checkpoint preserves PDF and analysis citations','status':'PASS' if len(final_output.get('citations',[]))>=2 else 'FAIL'})
            if artifacts:
                image=next((a for a in artifacts if a['mime_type']=='image/png'),None)
                checks.append({'check':'Task artifact opened','status':'PASS' if image and call('GET','/artifacts/'+image['id']).content[:8]==b'\x89PNG\r\n\x1a\n' else 'FAIL'})
            state['checks']+=checks
            state['after_restart']=after
            state['task_artifacts']=artifacts
            save('smoke.json',state)
            print(json.dumps({'checks':checks,'task_status':resumed['status']},ensure_ascii=False))


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('mode',choices=['prepare','llm','final','data','task','resume','legacy'])
    parser.add_argument('--base',default='http://127.0.0.1:8001')
    args=parser.parse_args()
    run(args.mode,args.base)
