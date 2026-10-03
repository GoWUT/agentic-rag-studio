"""Render the production efficiency component against actual task HTTP metrics."""
import json
from pathlib import Path
import requests
from streamlit.testing.v1 import AppTest

ROOT=Path(__file__).resolve().parents[2]
def run():
    result_dir=ROOT/'evaluation/results/phase5a5'
    measured=json.loads((result_dir/'optimized_run_1.json').read_text(encoding='utf-8'))
    base='http://127.0.0.1:8025'; task=measured['task_id']
    session=requests.Session();session.trust_env=False
    response=session.get(base+'/tasks/'+task+'/agents',timeout=10);response.raise_for_status()
    root=next(r for r in response.json()['agents'] if r['agent_id']=='supervisor')
    cost=root['metadata']['cost_trace']
    script='from client.phase5_ui import show_task_agents\nshow_task_agents('+repr(base)+','+repr(task)+')'
    app=AppTest.from_string(script).run(timeout=20)
    metrics={m.label:m.value for m in app.metric}
    passed=not app.exception and metrics.get('LLM calls')==str(cost['total_llm_calls']) and metrics.get('Tokens')==str(cost['tokens_total'])
    value={'status':'PASS' if passed else 'FAIL','method':'Streamlit AppTest renders production component using real HTTP task metrics',
        'metrics':metrics,'expanders':[e.label for e in app.expander], 'captions':[c.value for c in app.caption]}
    (result_dir/'ui_smoke.json').write_text(json.dumps(value,indent=2,ensure_ascii=False),encoding='utf-8')
    print(json.dumps(value))
    return passed

if __name__=='__main__':raise SystemExit(0 if run() else 1)
