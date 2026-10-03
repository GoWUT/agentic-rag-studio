"""HTTP/worker lifecycle. FastAPI never launches a production worker."""
import asyncio
from server.task_queue import InlineTaskQueue, PostgresTaskQueue


def configure_runtime(manager):
    from server.observability.runtime import configure_telemetry
    configure_telemetry(manager)
    manager.task_queue = PostgresTaskQueue(manager) if manager.config.get('TASK_EXECUTION_MODE','inline') == 'worker' else InlineTaskQueue(manager)


async def startup_runtime(manager):
    try:
        if manager.database:
            await manager.database.startup()
            if manager.phase4:
                await asyncio.to_thread(manager.phase4.initialize_servers)
                if manager.config.get('MCP_ENABLED', True):
                    await asyncio.to_thread(manager.phase4.refresh)
                await asyncio.to_thread(manager.phase4.checkpointer.open)
        if isinstance(manager.task_queue, PostgresTaskQueue):
            await manager.task_queue.open()
            if (await manager.task_queue.health())['queue'] != 'ok':
                raise RuntimeError('Queue schema unavailable; initialize SDK schema first')
    except Exception:
        await shutdown_runtime(manager)
        raise


async def shutdown_runtime(manager):
    if isinstance(manager.task_queue, PostgresTaskQueue):
        await manager.task_queue.close()
    if manager.phase4:
        await asyncio.to_thread(manager.phase4.close)
    if manager.database:
        await manager.database.close()
    if getattr(manager,'telemetry',None):
        await asyncio.to_thread(manager.telemetry.close)


def enqueue_sync(manager, task_id, *, resume=False):
    # Approval endpoints are synchronous FastAPI routes. Submit atomic async
    # deferral to the same application loop/pool, never a per-request new engine.
    return asyncio.run_coroutine_threadsafe(manager.task_queue.enqueue(task_id, resume=resume), manager.runtime_loop).result()


def register_runtime(app, manager):
    configure_runtime(manager)
    async def start():
        manager.runtime_loop = asyncio.get_running_loop()
        await startup_runtime(manager)
    app.add_event_handler('startup', start)
    async def stop():
        await shutdown_runtime(manager)
    app.add_event_handler('shutdown', stop)

    @app.get('/health/live')
    async def live():
        return {'status': 'ok'}

    @app.get('/health/ready')
    async def ready():
        from fastapi.responses import JSONResponse
        database = await manager.database.health() if manager.database else 'sqlite'
        queue = await manager.task_queue.health()
        ok = database in {'ok','sqlite'} and queue['queue'] in {'ok','inline'}
        return JSONResponse(status_code=200 if ok else 503, content={'database': database,
            'auth':'enabled' if manager.config.get('AUTH_ENABLED') else 'development_disabled', **queue})

    from sqlalchemy.exc import SQLAlchemyError
    from psycopg import Error
    from server.db.runtime import PersistenceUnavailable
    from procrastinate.exceptions import ConnectorException, AlreadyEnqueued
    async def unavailable(request, error):
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=503, content={'detail': 'Persistence or queue unavailable'})
    for exception in (SQLAlchemyError, Error, PersistenceUnavailable, ConnectorException):
        app.add_exception_handler(exception, unavailable)
    async def duplicate(request, error):
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=409, content={'detail': 'Task already enqueued'})
    app.add_exception_handler(AlreadyEnqueued, duplicate)
