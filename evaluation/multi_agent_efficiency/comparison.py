"""Compare retained actual baselines with fresh measurements and explicit quality criteria."""
import json
from pathlib import Path
import sqlite3
from statistics import median
from evaluation.multi_agent_efficiency.metrics import quality
from evaluation.multi_agent_efficiency.fixtures import GOAL

ROOT=Path(__file__).resolve().parents[2]
RESULTS=ROOT/'evaluation/results/phase5a5'
BASELINE_FOLDER=ROOT/'.runtime/phase5a_smoke/92e51059-6332-44de-ae4b-b1ef6d44d907'

def read(name):return json.loads((RESULTS/name).read_text(encoding='utf-8'))
def baseline_result(value,folder):
    path=folder/'workspace/sessions.sqlite3'
    with sqlite3.connect('file:'+path.as_posix()+'?mode=ro',uri=True) as db:
        outputs=[json.loads(row[0]).get('output_json') for row in db.execute('SELECT payload FROM task_steps WHERE task_id=? ORDER BY step_index',(value['task_id'],))]
        task=json.loads(db.execute('SELECT payload FROM tasks WHERE id=?',(value['task_id'],)).fetchone()[0])
    last=next(o for o in reversed(outputs) if o)
    answer=value['answer_result']; evidence=last.get('evidence',[]);artifacts=last.get('artifacts',[])
    q=quality(answer,evidence,value['citations'],artifacts,value['grounding'])
    return {**value,'answer':answer,'evidence':evidence,'artifacts':artifacts,'grounded':value['grounding'],'quality':q,'goal':task['goal']}

def project(value):
    q=quality(value['answer'],value['evidence'],value['citations'],value['artifacts'],value['grounded'])
    cost=value.get('cost_trace') or {}
    workers=value.get('runs',[])
    delegations=value.get('delegations',[])
    revisions=sum(bool(r['metadata'].get('revision_of')) for r in workers)
    return {'task_id':value['task_id'],'status':value['status'],'latency_seconds':value['latency_seconds'],
        'llm_calls':value['llm_calls'],'tool_calls':value['tool_calls'],'tokens':value['tokens'],
        'valid_citations':len(value['citations']) if q['citation_validity'] else None,'evidence_count':len(value['evidence']),
        'grounded':value['grounded'],'core_answer_correct':q['core_answer_correct'],
        'evidence_coverage':q['required_evidence_coverage'],'citation_validity':q['citation_validity'],
        'review_rounds':cost.get('review_rounds',int(any(r['agent_id']=='reviewer' and r['llm_calls'] for r in workers))),
        'redelegations':cost.get('redelegations',revisions), 'cache_hits':cost.get('cache_hits',0),
        'supplementary_completed':sum(d['status']=='COMPLETED' and any(r.get('delegation_id')==d['id'] and r['metadata'].get('revision_of') for r in workers) for d in delegations)}

def quality_pass(row):
    return row['status']=='COMPLETED' and row['grounded'] is True and row['core_answer_correct'] and row['citation_validity'] and all(row['evidence_coverage'].values())

def run():
    retained=read('baseline_reference.json')['baseline']
    previous_single=baseline_result(retained['single_agent'],BASELINE_FOLDER/'single')
    previous_multi=baseline_result(retained['multi_agent'],BASELINE_FOLDER)
    old_single=project(previous_single)
    old_multi=project(previous_multi)
    raw=[read('optimized_run_'+str(i)+'.json') for i in (1,2)]
    optimized=[project(v) for v in raw]
    single=project(read('existing_single_current.json')) if (RESULTS/'existing_single_current.json').exists() else old_single
    aggregate={'status':'COMPLETED' if all(v['status']=='COMPLETED' for v in optimized) else 'INCOMPLETE',
        'grounded':all(v['grounded'] is True for v in optimized),'core_answer_correct':all(v['core_answer_correct'] for v in optimized),
        'citation_validity':all(v['citation_validity'] for v in optimized),
        'evidence_coverage':{k:all(v['evidence_coverage'][k] for v in optimized) for k in optimized[0]['evidence_coverage']},
        'samples':len(optimized),'aggregation':'Median for numeric metrics; all runs must pass every quality requirement.'}
    for key in ('latency_seconds','llm_calls','tool_calls','tokens','valid_citations','evidence_count','review_rounds','redelegations','cache_hits'):
        aggregate[key]=median(v[key] for v in optimized) if all(v[key] is not None for v in optimized) else None
    change={k:{'before':old_multi[k],'after':aggregate[k], 'absolute_change':round(aggregate[k]-old_multi[k],3),
        'percent_change':round(100*(aggregate[k]-old_multi[k])/old_multi[k],3)} for k in ('llm_calls','tokens','latency_seconds') if aggregate[k] is not None}
    improved=any(c['absolute_change']<0 for c in change.values())
    reductions=[]
    for value in raw:
        for r in value['runs']:
            if r['metadata'].get('context_reduction'):
                reductions.append({'task_id':value['task_id'],'agent':r['agent_id'],**r['metadata']['context_reduction']})
            if r['agent_id']=='supervisor' and r['metadata'].get('review_context_reduction'):
                reductions.append({'task_id':value['task_id'],'agent':'reviewer',**r['metadata']['review_context_reduction']})
    smoke=read('runtime_smoke.json')
    verdict='READY WITH LIMITATIONS' if quality_pass(aggregate) and quality_pass(single) and improved else 'NOT READY'
    value={'verdict':verdict,'optimization_effective':improved and quality_pass(aggregate),
        'benchmark_goal':GOAL, 'same_question_verified':all(v['goal']==GOAL for v in [*raw,previous_single,previous_multi]),
        'model':'deepseek-v4-flash (same unchanged .env configuration; provider base https://api.deepseek.com; temperature 0)',
        'quality_rule_version':3,'quality_rule_correction':'Earlier strict classifiers missed explicit Best: Method A and best configuration is **A (... )**. Raw runs preserve original decisions; this comparison parses explicit best-choice predicates, rejects Best B despite A appearing elsewhere, and separately checks source coverage and grounding.',
        'retained_single_agent':old_single,'single_agent':single,'phase5a_multi_agent':old_multi,
        'optimized_runs':optimized,'optimized_median':aggregate,'change_vs_phase5a_multi':change,
        'context_reductions':reductions,'supplementary':smoke.get('supplementary_stats'),
        'limitations':['Synthetic three-configuration benchmark; two optimized runs and one current single run, not a representative performance distribution.',
            'Retained Phase5A trace may contain user-scope memory history; new modes share the isolated process memory store. API/network scheduling and provider outputs vary.',
            'Provider is the configured DeepSeek model, temperature 0, identical fixture generators; aggregate orchestration budget is an intended configuration difference.',
            'Context counts use count_tokens_approximately; provider usage totals are measured separately.',
            'No live GitHub write is executed; existing durable approval tests cover the preserved interrupt integration.']}
    (RESULTS/'comparison.json').write_text(json.dumps(value,indent=2,ensure_ascii=False),encoding='utf-8')
    print(json.dumps({'verdict':verdict,'optimized_median':aggregate,'change':change}))
    return value

if __name__=='__main__':run()
