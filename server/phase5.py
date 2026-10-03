"""Phase 5A composition shares the existing SQLite, policy and task services."""
from threading import BoundedSemaphore
from server.agent_registry import default_registry
from server.agent_runs import AgentRunStore


class Phase5Services:
    def __init__(self, manager):
        self.manager, self.config = manager, manager.config
        self.registry = default_registry(self.config)
        self.store = manager.repositories.build(AgentRunStore, manager.phase4.policy.secrets)
        self.store.efficiency_config = self.config
        self.semaphore = BoundedSemaphore(self.config.get('MULTI_AGENT_MAX_PARALLEL', 3))

    def event(self, task_id, kind, payload):
        self.manager.phase4.event(task_id, kind, payload)

    def metrics(self, supervisor_run_id):
        supervisor = self.store.run(supervisor_run_id)
        runs = [self.store.run(d['agent_run_id']) for d in self.store.delegations(supervisor_run_id)]
        runs = [supervisor, *runs]
        usage_available = all(r['token_usage_status'] == 'available' for r in runs if r['llm_calls'])
        from server.agent.efficiency import BudgetManager
        from datetime import datetime
        metadata = supervisor['metadata']
        budget = BudgetManager.snapshot({**metadata['budget'], 'completed_at': supervisor['completed_at']}, self.config.get('MULTI_AGENT_SOFT_BUDGET_RATIO', .8)) if metadata.get('budget') else None
        phases = metadata.get('phase_calls', {})
        finalizer_calls = sum(phases.get(p, 0) for p in BudgetManager.FINALIZER_PHASES)
        ended_at = datetime.fromisoformat(supervisor['completed_at']) if supervisor['completed_at'] else datetime.now().astimezone()
        cost = {'orchestration_mode':metadata['orchestration_mode'],
            'supervisor_llm_calls':supervisor['llm_calls']-finalizer_calls, 'finalizer_llm_calls':finalizer_calls,
            **{agent+'_llm_calls':sum(r['llm_calls'] for r in runs if r['agent_id']==agent) for agent in ('research','data','coding','reviewer')},
            'total_llm_calls':sum(r['llm_calls'] for r in runs), 'total_tool_calls':sum(r['tool_calls'] for r in runs),
            'tokens_total':sum(r['tokens'] or 0 for r in runs) if usage_available else None,
            'retrieval_calls':sum(o['capability'] in {'workspace.search','document.search','web.search','arxiv.search'} for r in runs if not r['metadata'].get('cache_hit') for o in r['metadata'].get('progress',{}).get('outputs',[])),
            'analysis_calls':sum(o['capability']=='data.analyze' for r in runs if not r['metadata'].get('cache_hit') for o in r['metadata'].get('progress',{}).get('outputs',[])),
            'delegations_created':len(runs)-1, 'delegations_completed':sum(r['status']=='COMPLETED' for r in runs[1:]),
            'redelegations':metadata.get('redelegation_count',0), 'review_rounds':int(any(r['agent_id']=='reviewer' and r['llm_calls'] for r in runs)),
            'wall_time_ms':round((ended_at-datetime.fromisoformat(supervisor['started_at'])).total_seconds()*1000,2),
            'cache_hits':sum(bool(r['metadata'].get('cache_hit')) for r in runs),
            'duplicate_delegations_prevented':metadata.get('duplicate_delegations_prevented',0),
            'review_status':metadata.get('review_status','not_requested'), 'review_skip_reason':metadata.get('review_skip_reason'),
            'budget_exhausted':bool(budget and budget['budget_exhausted']), 'budget':budget}
        for field in ('tokens_prompt','tokens_completion'):
            cost[field] = sum(r['metadata'].get(field,0) for r in runs) if all(r['metadata'].get(field+'_samples',0)==r['llm_calls'] for r in runs) else None
        groups = {}
        for r in runs[1:]:
            if r['agent_id']=='reviewer' or not r['started_at'] or not r['completed_at']:
                continue
            groups.setdefault(r['metadata'].get('execution_group',0),[]).append(r)
        cost['execution_groups'] = [{'execution_group':g, 'duration_ms':round((max(datetime.fromisoformat(r['completed_at']) for r in members)-min(datetime.fromisoformat(r['started_at']) for r in members)).total_seconds()*1000,2),
            'critical_path_agent':max(members,key=lambda r:r['completed_at'])['agent_id']} for g,members in groups.items()]
        self.store.update_supervisor(supervisor_run_id, cost_trace=cost)
        return {'cost_trace': cost, 'budget_exhausted':cost['budget_exhausted'], 'agent_metrics': [self.store.public_run(r) for r in runs],
            'total_llm_calls': sum(r['llm_calls'] for r in runs), 'total_tool_calls': sum(r['tool_calls'] for r in runs),
            'total_iterations': sum(r['iterations'] for r in runs), 'delegation_count': len(runs) - 1,
            'total_tokens': sum(r['tokens'] or 0 for r in runs) if usage_available else None,
            'token_usage_status': 'available' if usage_available else 'unavailable',
            'known_token_subtotal': sum(r['tokens'] or 0 for r in runs)}
