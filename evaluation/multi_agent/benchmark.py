"""Repeatable offline comparison. Real-provider comparison is emitted by smoke.py."""
import json
from pathlib import Path
import time
from evaluation.multi_agent.metrics import measured_run

ROOT = Path(__file__).resolve().parents[2]


def run():
    from test_phase5a import GraphFixture, FixtureModel
    cases = json.loads((Path(__file__).parent/'cases.json').read_text())
    report = {'model':'SCRIPTED fixture, not a real provider','sources':'synthetic tool evidence and actual CSV/runtime/chart',
        'token_usage':'unavailable from scripted fixture','cases':[], 'real_comparison':'evaluation/results/phase5a/real_comparison.json'}
    for case in cases:
        comparison = {'name':case['name'],'goal':case['goal']}
        for multi in (False, True):
            fixture = GraphFixture()
            fixture.setUp()
            try:
                fixture.config['MULTI_AGENT_ENABLED'] = multi
                start = time.perf_counter()
                task = fixture.task(case['goal'])
                elapsed = time.perf_counter()-start
                store = fixture.manager.phase5.store
                runs = [store.public_run(r) for r in store.runs(task.id)]
                delegations = [store.public_delegation(d) for d in store.delegations(task_id=task.id)]
                events = [e.model_dump() for e in fixture.manager.phase3.tasks.events(task.id)]
                measured = measured_run(task.model_dump(),runs,delegations,events,[],elapsed)
                output = fixture.manager.phase3.tasks.steps(task.id)[-1].output_json
                evidence = output.get('evidence',[])
                actual = {r['agent_id'] for r in runs if r['agent_id'] not in {'supervisor','reviewer'}}
                measured.update(llm_calls=len(FixtureModel.captured),tool_calls=output.get('tool_calls'),
                    evidence_types=sorted({e['source_type'] for e in evidence}), citation_count=len(output.get('citations',[])),
                    grounded=(output.get('verification_result') or {}).get('grounded'),
                    selection_correct=actual==set(case['expected_agents']) if multi else None,
                    evidence_type_coverage=len(set(case['expected_evidence_types']) & {e['source_type'] for e in evidence})/len(case['expected_evidence_types']),
                    evidence_ids=[e['evidence_id'] for e in evidence], artifact_count=len(output.get('artifacts',[])))
                comparison['multi_agent' if multi else 'single_agent'] = measured
            finally:
                fixture.doCleanups()
        report['cases'].append(comparison)
    report['agent_selection_accuracy'] = sum(c['multi_agent']['selection_correct'] for c in report['cases'])/len(cases)
    report['routing_accuracy'] = report['agent_selection_accuracy']
    report['denominator'] = len(cases)
    report['interpretation'] = 'Mixed work adds isolation/review and cost. Same scripted fixture knowledge does not establish better answer quality. Latencies include actual analysis subprocess startup; n=1 each.'
    output = ROOT/'evaluation/results/phase5a/offline_comparison.json'
    output.parent.mkdir(parents=True,exist_ok=True)
    output.write_text(json.dumps(report,indent=2,ensure_ascii=False),encoding='utf-8')
    print(json.dumps({'cases':len(cases),'selection_accuracy':report['agent_selection_accuracy'],'output':str(output)}))
    return report


if __name__=='__main__':
    run()
