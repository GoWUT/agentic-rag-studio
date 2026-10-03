"""Read-only discovery and explicit human decisions; no arbitrary tool-call API."""
import asyncio
from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from jsonschema.exceptions import ValidationError as SchemaValidationError
from server.persistence import RecordNotFound
from server.tasks import TaskConflict
from server.tool_registry import ToolUnavailable


class ApprovalEdits(BaseModel):
    edits: dict = Field(default_factory=dict)


def register_phase4_api(app, manager):
    if not manager.phase4:
        return
    services = manager.phase4
    router = APIRouter()

    @app.middleware('http')
    async def approval_origin(request, call_next):
        if request.method == 'POST' and request.url.path.startswith('/approvals/'):
            origin = request.headers.get('origin')
            allowed = {manager.config.get('FRONTEND_URL', 'http://127.0.0.1:8501'),
                'http://127.0.0.1:8501', 'http://localhost:8501'}
            if origin is not None and origin not in allowed:
                return JSONResponse(status_code=403, content={'detail': 'Approval origin not allowed'})
        return await call_next(request)

    def guarded(call):
        try:
            return call()
        except RecordNotFound:
            raise HTTPException(404, 'Record not found') from None
        except TaskConflict as error:
            raise HTTPException(409, str(error)) from None
        except (ValueError, PermissionError, SchemaValidationError) as error:
            raise HTTPException(400, type(error).__name__) from None

    @router.get('/tools')
    def tools():
        return {'tools': [t.model_dump() for t in services.registry.list_tools()]}

    @router.get('/tools/providers')
    def providers():
        return {'providers': [{'id': key, 'status': value} for key, value in services.registry.health.items()]}

    @router.post('/tools/providers/{provider_id}/refresh')
    def refresh(provider_id: str):
        return guarded(lambda: {'tools': [t.model_dump() for t in services.refresh(provider_id)]})

    @router.get('/mcp/servers')
    def servers():
        return {'servers': [s.model_dump(exclude={'args', 'command', 'env_keys', 'header_env_keys'}) for s in services.servers.list()]}

    @router.get('/mcp/servers/{server_id}/tools')
    def server_tools(server_id: str):
        return {'tools': [t.model_dump() for t in services.registry.list_tools() if t.provider == server_id]}

    @router.post('/mcp/servers/{server_id}/refresh')
    def refresh_server(server_id: str):
        return refresh(server_id)

    @router.get('/approvals')
    def approvals(task_id: str | None = None, status: str | None = None):
        return {'approvals': services.actions.approvals(task_id, status)}

    @router.get('/approvals/{approval_id}')
    def approval(approval_id: str):
        return guarded(lambda: services.actions.approval(approval_id))

    @router.post('/approvals/{approval_id}/approve')
    def approve(approval_id: str):
        return guarded(lambda: services.decide(approval_id, 'APPROVED'))

    @router.post('/approvals/{approval_id}/reject')
    def reject(approval_id: str):
        return guarded(lambda: services.decide(approval_id, 'REJECTED'))

    @router.post('/approvals/{approval_id}/edit')
    def edit(approval_id: str, body: ApprovalEdits):
        return guarded(lambda: services.decide(approval_id, 'EDITED', body.edits))

    @router.get('/tool-executions')
    def executions(task_id: str | None = None):
        return {'executions': services.actions.executions(task_id)}

    async def startup():
        await services.registry.refresh()

    app.include_router(router)
    app.add_event_handler('startup', startup)
    app.add_event_handler('shutdown', services.close)
