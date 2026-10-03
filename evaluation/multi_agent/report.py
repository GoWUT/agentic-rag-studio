"""Derive acceptance/metrics from saved measurements, without model or tool calls."""
import json
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RESULTS = ROOT / 'evaluation/results/phase5a'

CHECKS = [
    ('FastAPI start', ['1_fastapi_start']), ('Streamlit start', ['2_streamlit_start']),
    ('Agent Registry', ['3_agent_registry']), ('Simple query bypass', ['4_simple_bypasses_orchestration']),
    ('Data only', ['5_data_only']), ('Research only', ['6_research_only']),
    ('Research + Data', ['7_multi_agent']), ('Parallel overlap', ['8_parallel_overlap']),
    ('Private context isolation', ['9_private_context_isolation']), ('Evidence merge', ['10_evidence_merge']),
    ('Reviewer structured input', ['11_reviewer_structured_input']), ('Reviewer PASS', ['12_reviewer_pass']),
    ('Reviewer revision', ['13_reviewer_revision']), ('Bounded re-delegation', ['14_bounded_redelegation']),
    ('Completed work not repeated', ['15_completed_work_not_repeated']), ('Agent timeout', ['16_agent_timeout']),
    ('Parallel branch failure', ['17_parallel_branch_failure']), ('HITL', ['18_hitl_waiting', '18_hitl_resume']),
    ('Restart + resume', ['19_restart_paused', '19_restart_resume']), ('Grounding', ['20_grounding']),
    ('Citations', ['21_citations']), ('Memory once', ['22_memory_once']), ('Single/multi runner', ['23_single_multi_runner']),
]


def build(runtime, tests, comparison):
    checks = [{'number': i, 'name': name, 'status': 'PASS' if all(runtime['checks'].get(k, {}).get('status') == 'PASS' for k in keys) else 'FAIL',
        'measurement_keys': keys} for i, (name, keys) in enumerate(CHECKS, 1)]
    expected = {'A_research_only': {'research'}, 'B_data_only': {'data'}, 'C_research_data': {'research', 'data'},
        'D_code_review_MOCK': {'coding'}, 'E_research_code_MOCK': {'research', 'coding'}}
    correct = sum({r['agent_id'] for r in runtime['scenarios'].get(name, {}).get('runs', []) if r['agent_id'] not in {'supervisor', 'reviewer'}} == roles
        for name, roles in expected.items())
    mixed = runtime['scenarios']['C_research_data']
    initial = [r for r in mixed['runs'] if r['agent_id'] in {'research', 'data'} and r['metadata'].get('execution_group') == 0 and not r['metadata'].get('revision_of')]
    intervals = [(datetime.fromisoformat(r['started_at']), datetime.fromisoformat(r['completed_at'])) for r in initial]
    gap = runtime['scenarios']['F_injected_research_gap']
    revisions = [r for r in gap['runs'] if r['metadata'].get('revision_of')]
    review = next(d['result_json']['payload'] for d in gap['delegations'] if d['agent_id'] == 'reviewer')
    repeated = sum(round(s['duplicate_work_rate'] * s['duplicate_work_denominator']) for s in runtime['scenarios'].values()
        if s.get('duplicate_work_rate') is not None)
    denominator = sum(s.get('duplicate_work_denominator', 0) for s in runtime['scenarios'].values())
    measured_scenarios = {name: s for name, s in runtime['scenarios'].items() if s.get('task_id')}
    delegations = [d for s in measured_scenarios.values() for d in s.get('delegations', [])]
    succeeded = sum(d['status'] == 'COMPLETED' for d in delegations)
    terminated = sum(d['status'] in {'COMPLETED', 'PARTIAL', 'FAILED', 'CANCELLED'} for d in delegations)
    return {
        'verdict': 'READY WITH LIMITATIONS' if all(c['status'] == 'PASS' for c in checks) and tests['failed'] == tests['errors'] == 0 else 'NOT READY',
        'runtime_run_id': runtime['run_id'], 'checks': checks, 'tests': tests,
        'agent_selection_accuracy': {'value': correct / len(expected), 'correct': correct, 'denominator': len(expected)},
        'routing_accuracy': {'value': (correct + (checks[3]['status'] == 'PASS')) / (len(expected) + 1), 'denominator': len(expected) + 1},
        'reviewer_gap_detection': {'value': int(review['verdict'] == 'needs_revision'), 'denominator': 1, 'fixture': 'explicit F research evidence-gap injection'},
        'redelegation_success': {'completed': sum(r['status'] == 'COMPLETED' for r in revisions), 'denominator': len(revisions)},
        'duplicate_completed_work': {'repeated': repeated, 'denominator': denominator, 'rate': repeated / denominator if denominator else None},
        'delegation_success': {'completed': succeeded, 'denominator': len(delegations),
            'rate': succeeded / len(delegations) if delegations else None},
        'delegation_completion': {'terminal': terminated, 'denominator': len(delegations),
            'rate': terminated / len(delegations) if delegations else None},
        'per_scenario_metrics': {name: {k: s.get(k) for k in ('status', 'latency_seconds', 'llm_calls', 'tool_calls',
            'tokens', 'token_usage_status', 'delegation_success_rate', 'delegation_completion_rate',
            'parallelism_utilization', 'grounding', 'evidence_count')} for name, s in measured_scenarios.items()},
        'per_agent_metrics': {name: [{k: r.get(k) for k in ('agent_id', 'delegation_id', 'status',
            'started_at', 'completed_at', 'iterations', 'tool_calls', 'llm_calls', 'tokens', 'token_usage_status')}
            for r in s.get('runs', [])] for name, s in measured_scenarios.items()},
        'initial_parallel_group': {'duration_sum_seconds': round(sum((b - a).total_seconds() for a, b in intervals), 3),
            'span_seconds': round((max(b for _, b in intervals) - min(a for a, _ in intervals)).total_seconds(), 3),
            'intervals': [{'agent': r['agent_id'], 'start': r['started_at'], 'end': r['completed_at']} for r in initial],
            'interpretation': 'Measured interval sum is a sequential proxy, not an independently run sequential benchmark.'},
        'single_vs_multi': {mode: {k: comparison[mode].get(k) for k in ('status', 'answer_result', 'evidence_coverage', 'grounding', 'citations', 'latency_seconds', 'llm_calls', 'tool_calls', 'tokens', 'token_usage_status')}
            for mode in ('single_agent', 'multi_agent')},
        'comparison_limits': 'Same synthetic PDFs/XLSX and goal; n=1 per mode, different plans and prior task/memory history. No causal quality or general speed claim.',
        'restart_research_execution_count': runtime['checks']['19_restart_resume']['details']['research_execution_count'],
        'real_github': runtime['real_github'], 'external_writes': runtime['external_writes'],
        'evaluation_resumptions': runtime.get('evaluation_resumptions', []),
    }


def run():
    report = build(*[json.loads((RESULTS / f).read_text(encoding='utf-8')) for f in ('runtime_smoke.json', 'tests.json', 'real_comparison.json')])
    (RESULTS / 'metrics_summary.json').write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding='utf-8')
    print(json.dumps({'verdict': report['verdict'], 'runtime_passed': sum(c['status'] == 'PASS' for c in report['checks']), 'runtime_total': len(report['checks'])}))
    return report


if __name__ == '__main__':
    run()
