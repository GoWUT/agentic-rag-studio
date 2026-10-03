"""Scoped Phase 3 APIs registered on the existing application."""
import logging
import sqlite3
from fastapi import APIRouter, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field
from typing import Literal
from server.memory import LongTermMemory
from server.persistence import RecordNotFound
from server.tasks import TaskConflict
from server.analysis_runtime import DataAnalysisRequest

LOGGER = logging.getLogger(__name__)


class MemoryCreate(BaseModel):
    content: str = Field(min_length=1,max_length=3000)
    memory_type: Literal['preference','project','decision','instruction','episodic'] = 'instruction'
    workspace_id: str | None = None
    importance: float = Field(default=.8,ge=0,le=1)


class MemoryUpdate(BaseModel):
    content: str = Field(min_length=1,max_length=3000)


class TaskCreate(BaseModel):
    workspace_id: str
    goal: str = Field(min_length=1,max_length=6000)
    session_id: str | None = None
    source_scope: Literal['workspace_only','workspace_and_external','external'] = 'workspace_only'


class TaskRun(BaseModel):
    max_steps: int | None = Field(default=None,ge=1,le=10)


def register_phase3_api(app,manager):
    services = manager.phase3
    # Recovery belongs to a backend startup, not to constructing a store/manager
    # (which also happens in tests and maintenance tools).
    worker_mode = manager.config.get('TASK_EXECUTION_MODE', 'inline') == 'worker'
    if not worker_mode:
        app.add_event_handler('startup', services.tasks.recover)
    router = APIRouter()

    @app.exception_handler(RecordNotFound)
    async def missing(request,error):
        return JSONResponse(status_code=404,content={'detail':'Record not found'})

    @app.exception_handler(TaskConflict)
    async def conflict(request,error):
        return JSONResponse(status_code=409,content={'detail':str(error)})

    @app.exception_handler(sqlite3.Error)
    async def database_error(request,error):
        LOGGER.error('Persistence failure error_type=%s',type(error).__name__)
        return JSONResponse(status_code=503,content={'detail':'Local persistence unavailable'})

    @app.exception_handler(ValueError)
    async def invalid(request,error):
        return JSONResponse(status_code=400,content={'detail':str(error)[:200]})

    def enabled(key):
        if not manager.config.get(key,True):
            raise HTTPException(status_code=503,detail='Feature disabled')

    @router.get('/memories')
    def memories(workspace_id:str|None=None,memory_type:str|None=None,status:str='active'):
        enabled('MEMORY_ENABLED')
        if workspace_id:
            manager.workspaces.get(workspace_id)
        return services.memory.list(workspace_id=workspace_id,memory_type=memory_type,status=status)

    @router.post('/memories')
    def create_memory(req:MemoryCreate):
        enabled('MEMORY_ENABLED')
        if req.workspace_id:
            manager.workspaces.get(req.workspace_id)
        try:
            return services.memory.create(LongTermMemory(content=req.content,memory_type=req.memory_type,importance=req.importance,
                scope_type='workspace' if req.workspace_id else 'user',scope_id=req.workspace_id or 'local_default'))
        except ValueError as error:
            raise HTTPException(status_code=400,detail=str(error)) from error

    @router.patch('/memories/{memory_id}')
    def edit_memory(memory_id:str,req:MemoryUpdate):
        enabled('MEMORY_ENABLED')
        try:
            return services.memory.update(memory_id,req.content)
        except ValueError as error:
            raise HTTPException(status_code=400,detail=str(error)) from error

    @router.delete('/memories/{memory_id}',status_code=204)
    def delete_memory(memory_id:str):
        enabled('MEMORY_ENABLED')
        services.memory.delete(memory_id)

    @router.get('/memories/retrieve')
    def retrieve_memories(query:str,workspace_id:str|None=None):
        enabled('MEMORY_ENABLED')
        if workspace_id:
            manager.workspaces.get(workspace_id)
        return services.memory.retrieve_memories(query,workspace_id)

    # Static /memories/retrieve is registered before this ID route.
    @router.get('/memories/{memory_id}')
    def memory(memory_id:str):
        enabled('MEMORY_ENABLED')
        return services.memory.get(memory_id)

    @router.post('/workspaces/{workspace_id}/datasets')
    def upload_dataset(workspace_id:str,file:UploadFile=File(...)):
        enabled('DATA_ANALYSIS_ENABLED')
        manager.workspaces.get(workspace_id)
        try:
            return services.data.upload(workspace_id,file.filename or '',file.file,file.content_type or 'application/octet-stream').model_dump(exclude={'file_path'})
        except ValueError as error:
            raise HTTPException(status_code=400,detail=str(error)) from error

    @router.get('/workspaces/{workspace_id}/datasets')
    def datasets(workspace_id:str):
        enabled('DATA_ANALYSIS_ENABLED')
        manager.workspaces.get(workspace_id)
        return [a.model_dump(exclude={'file_path'}) for a in services.data.list(workspace_id)]

    @router.get('/workspaces/{workspace_id}/datasets/{dataset_id}')
    def dataset(workspace_id:str,dataset_id:str):
        enabled('DATA_ANALYSIS_ENABLED')
        manager.workspaces.get(workspace_id)
        return services.data.get(workspace_id,dataset_id).model_dump(exclude={'file_path'})

    @router.delete('/workspaces/{workspace_id}/datasets/{dataset_id}',status_code=204)
    def delete_dataset(workspace_id:str,dataset_id:str):
        enabled('DATA_ANALYSIS_ENABLED')
        manager.workspaces.get(workspace_id)
        services.data.delete(workspace_id,dataset_id)

    @router.post('/workspaces/{workspace_id}/analysis')
    def analyze(workspace_id:str,req:DataAnalysisRequest):
        enabled('DATA_ANALYSIS_ENABLED')
        manager.workspaces.get(workspace_id)
        if not req.code.strip():
            raise HTTPException(status_code=400,detail='Use /chat for agent-generated code or supply code here')
        return services.analysis.run(workspace_id,req)

    @router.get('/artifacts/{artifact_id}')
    def artifact(artifact_id:str):
        record = services.data.artifact(artifact_id)
        manager.workspaces.get(record.workspace_id)
        return FileResponse(services.data.checked_path(record.file_path),media_type=record.mime_type,filename=record.filename)

    @router.post('/tasks')
    def create_task(req:TaskCreate):
        enabled('TASK_SYSTEM_ENABLED')
        return services.task_service.create(**req.model_dump())

    @router.get('/tasks')
    def tasks(workspace_id:str|None=None,status:str|None=None):
        enabled('TASK_SYSTEM_ENABLED')
        return services.tasks.list(workspace_id,status)

    @router.get('/tasks/{task_id}')
    def task(task_id:str):
        enabled('TASK_SYSTEM_ENABLED')
        record = services.tasks.get(task_id).model_dump()
        return {**record,'steps':[s.model_dump() for s in services.tasks.steps(task_id)]}

    @router.post('/tasks/{task_id}/run')
    async def run_task(task_id:str,req:TaskRun=TaskRun()):
        enabled('TASK_SYSTEM_ENABLED')
        if worker_mode:
            task = await manager.task_queue.enqueue(task_id, max_steps=req.max_steps)
            return JSONResponse(status_code=202, content=task.model_dump())
        import asyncio
        return await asyncio.to_thread(services.task_service.run, task_id, max_steps=req.max_steps)

    @router.post('/tasks/{task_id}/resume')
    async def resume_task(task_id:str,req:TaskRun=TaskRun()):
        enabled('TASK_SYSTEM_ENABLED')
        if worker_mode:
            task = await manager.task_queue.enqueue(task_id, resume=True, max_steps=req.max_steps)
            return JSONResponse(status_code=202, content=task.model_dump())
        import asyncio
        return await asyncio.to_thread(services.task_service.run, task_id, resume=True, max_steps=req.max_steps)

    @router.post('/tasks/{task_id}/pause')
    async def pause_task(task_id:str):
        enabled('TASK_SYSTEM_ENABLED')
        if worker_mode:
            return await manager.task_queue.request_control(task_id)
        import asyncio
        return await asyncio.to_thread(services.tasks.transition, task_id, 'PAUSED')

    @router.post('/tasks/{task_id}/cancel')
    async def cancel_task(task_id:str):
        import asyncio
        if getattr(manager, 'phase4', None):
            await asyncio.to_thread(manager.phase4.actions.cancel_pending, task_id)
        enabled('TASK_SYSTEM_ENABLED')
        result = await manager.task_queue.cancel(task_id) if worker_mode else await asyncio.to_thread(services.tasks.transition, task_id,'CANCELLED')
        if getattr(manager, 'phase5', None):
            if result.status == 'CANCELLED':
                await asyncio.to_thread(manager.phase5.store.cancel, task_id)
        return result

    @router.get('/tasks/{task_id}/events')
    def task_events(task_id:str):
        return services.tasks.events(task_id)

    @router.get('/tasks/{task_id}/artifacts')
    def task_artifacts(task_id:str):
        services.tasks.get(task_id)
        return [a.model_dump(exclude={'file_path'}) for a in services.data.artifacts(task_id)]

    app.include_router(router)
