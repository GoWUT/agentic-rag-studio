"""Read-only agent catalog and public execution summaries; no agent creation endpoint."""
from fastapi import APIRouter, HTTPException
from server.agent_registry import AgentUnavailable


def register_phase5_api(app, manager):
    router = APIRouter()
    services = manager.phase5
    if services and manager.config.get('TASK_EXECUTION_MODE', 'inline') != 'worker':
        app.add_event_handler('startup', services.store.recover)

    def enabled():
        if not services or not manager.config.get('MULTI_AGENT_ENABLED', False):
            raise HTTPException(503, 'Multi-agent orchestration disabled')

    @router.get('/agents')
    def agents():
        enabled()
        return {'agents': [a.model_dump() for a in services.registry.list_agents()]}

    @router.get('/agents/{agent_id}')
    def agent(agent_id: str):
        enabled()
        try:
            return services.registry.get(agent_id).model_dump()
        except AgentUnavailable as error:
            raise HTTPException(404, 'Agent unavailable') from error

    @router.get('/tasks/{task_id}/agents')
    def task_agents(task_id: str):
        enabled()
        manager.phase3.tasks.get(task_id)
        supervisor=services.store.supervisor(task_id)
        if supervisor:
            services.metrics(supervisor['id'])
        return {'agents': [services.store.public_run(r) for r in services.store.runs(task_id)]}

    @router.get('/tasks/{task_id}/delegations')
    def delegations(task_id: str):
        enabled()
        manager.phase3.tasks.get(task_id)
        return {'delegations': [services.store.public_delegation(d) for d in services.store.delegations(task_id=task_id)]}

    app.include_router(router)
