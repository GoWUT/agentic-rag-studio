"""Measured metrics with explicit denominators and unavailable values."""
from datetime import datetime


def measured_run(task, runs, delegations, events, usage, latency):
    workers = [r for r in runs if r['agent_id'] not in {'supervisor','reviewer'}]
    intervals = [(datetime.fromisoformat(r['started_at']).timestamp(),datetime.fromisoformat(r['completed_at']).timestamp())
        for r in workers if r['started_at'] and r['completed_at']]
    duration_sum = sum(end-start for start,end in intervals)
    duration_span = max((end for _,end in intervals),default=0)-min((start for start,_ in intervals),default=0)
    llm_starts = [e for e in usage if e.get('task_id') == task['id'] and e['event']=='llm_start']
    llm_ends = [e for e in usage if e.get('task_id') == task['id'] and e['event']=='llm_end']
    tokens = sum(e['tokens'] for e in llm_ends) if llm_starts and len(llm_starts)==len(llm_ends) and all(e['tokens'] is not None for e in llm_ends) else None
    terminal = set()
    repeated_completed = 0
    start_count = 0
    for event in events:
        delegation_id = event.get('payload_json', {}).get('delegation_id')
        if event['event_type'] == 'DELEGATION_STARTED':
            start_count += 1
            repeated_completed += delegation_id in terminal
        elif event['event_type'] == 'DELEGATION_COMPLETED':
            terminal.add(delegation_id)
    return {'answer_result':task.get('metadata',{}).get('answer',''),'status':task['status'],
        'evidence_coverage': {'evidence_ids':sorted({i for d in delegations for i in (d.get('result_json') or {}).get('evidence_ids',[])}),
            'completed_specialties':sorted({r['agent_id'] for r in workers if r['status']=='COMPLETED'})},
        'latency_seconds':round(latency,3),'llm_calls':len(llm_starts) if llm_starts else None,
        'tool_calls':sum(r['tool_calls'] for r in workers) if runs else None,'tokens':tokens,
        'token_usage_status':'available' if tokens is not None else 'unavailable',
        'delegation_success_rate':sum(d['status']=='COMPLETED' for d in delegations)/len(delegations) if delegations else None,
        'delegation_completion_rate':sum(d['status'] in {'COMPLETED','PARTIAL','FAILED','CANCELLED'} for d in delegations)/len(delegations) if delegations else None,
        'duplicate_work_rate':repeated_completed/start_count if start_count else None,
        'duplicate_work_denominator':start_count,
        'agent_duration_sum_seconds':round(duration_sum,3),'agent_duration_span_seconds':round(duration_span,3),
        'parallelism_utilization':round(duration_sum/duration_span,3) if duration_span else None}
