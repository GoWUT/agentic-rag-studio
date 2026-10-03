"""Durable task queue port. Only IDs cross the queue boundary."""
import asyncio
from datetime import datetime, timezone
from typing import Protocol
from sqlalchemy import select, update, insert, text, func
from server.db.schema import tasks, events, approvals
from server.db.runtime import MUTATION_LOCK, psycopg_dsn, connection_options
from server.tasks import Task, TaskEvent, TaskConflict
from server.persistence import RecordNotFound


class TaskQueue(Protocol):
    async def enqueue(self, task_id: str, *, resume=False, max_steps=None) -> Task: ...
    async def cancel(self, task_id: str) -> Task: ...
    async def get_status(self, task_id: str) -> dict: ...
    async def health(self) -> dict: ...


class InlineTaskQueue:
    def __init__(self, manager):
        self.manager = manager

    async def enqueue(self, task_id, *, resume=False, max_steps=None):
        return await asyncio.to_thread(self.manager.phase3.task_service.run, task_id,
                                       resume=resume, max_steps=max_steps)

    async def cancel(self, task_id):
        return await asyncio.to_thread(self.manager.phase3.tasks.transition, task_id, 'CANCELLED')

    async def get_status(self, task_id):
        value = await asyncio.to_thread(self.manager.phase3.tasks.get, task_id)
        return {'status': value.status, 'queue_job_id': None}

    async def health(self):
        return {'queue': 'inline', 'worker': 'inline', 'queue_depth': 0, 'running_jobs': 0}


async def locked_task(session, task_id):
    # Same lock order as the synchronous legacy mutation port.
    await session.execute(text('SELECT pg_advisory_xact_lock(:key)'), {'key': MUTATION_LOCK})
    row = (await session.execute(select(tasks.c.payload).where(tasks.c.id == task_id).with_for_update())).first()
    if not row:
        raise RecordNotFound(task_id)
    return Task.model_validate(row[0])


async def save_task(session, task, kind, payload=None):
    task.version += 1
    task.updated_at = datetime.now(timezone.utc).isoformat()
    await session.execute(update(tasks).where(tasks.c.id == task.id).values(
        status=task.status, payload=task.model_dump(), version=task.version,
        queue_job_id=task.queue_job_id, queued_at=as_time(task.queued_at),
        worker_started_at=as_time(task.worker_started_at), attempt_count=task.attempt_count,
        execution_backend=task.execution_backend))
    event = TaskEvent(task_id=task.id, event_type=kind, payload_json=payload or {})
    await session.execute(insert(events).values(id=event.id, task_id=task.id,
        event_type=kind, payload=event.model_dump(), created_at=as_time(event.created_at)))


def as_time(value):
    return datetime.fromisoformat(value).astimezone(timezone.utc) if value else None


class PostgresTaskQueue:
    def __init__(self, manager, app=None):
        import procrastinate
        self.manager, self.runtime = manager, manager.database
        self.app = app or procrastinate.App(connector=procrastinate.PsycopgConnector(
            conninfo=psycopg_dsn(manager.config['DATABASE_URL']),
            min_size=1, max_size=manager.config.get('DB_POOL_SIZE', 5),
            kwargs={'options': connection_options(manager.config['DATABASE_URL'])}))
        # Registration is shared by producer and independent worker; the producer
        # never starts a worker. Retry only the application's classified exception.
        from server.worker_runtime import RetryableTaskError, execute_task
        @self.app.task(name='agent.execute', queue=manager.config.get('WORKER_QUEUE', 'agent'),
            pass_context=True, retry=procrastinate.RetryStrategy(
                max_attempts=manager.config.get('WORKER_MAX_RETRIES', 2) + 1,
                wait=1, exponential_wait=2, retry_exceptions=[RetryableTaskError]))
        async def execute(context, task_id: str, expected_version: int):
            await execute_task(self, context, task_id, expected_version)
        self.job = execute

    async def open(self):
        await self.app.open_async()

    async def close(self):
        await self.app.close_async()

    @staticmethod
    async def external_connection(session):
        connection = await session.connection()
        raw = await connection.get_raw_connection()
        return raw.driver_connection

    async def enqueue(self, task_id, *, resume=False, max_steps=None):
        if getattr(self.manager,'security',None):
            await asyncio.to_thread(self.manager.security.task,task_id,'task.execute')
        async with self.runtime.sessions() as session, session.begin():
            task = await locked_task(session, task_id)
            legal = {'PAUSED','WAITING_USER','FAILED'} if resume else {'PENDING'}
            if task.status not in legal:
                raise TaskConflict('Task is already queued/running or not resumable')
            if task.cancel_requested:
                raise TaskConflict('Task cancellation was requested')
            if self.manager.phase4:
                pending = await session.scalar(select(func.count()).select_from(approvals).where(
                    approvals.c.task_id == task_id, approvals.c.status == 'PENDING'))
                if pending:
                    raise TaskConflict('Task has a pending human approval')
            task.status, task.execution_backend = 'QUEUED', 'worker'
            task.queued_at = datetime.now(timezone.utc).isoformat()
            task.pause_requested = False
            task.metadata['worker_max_steps'] = max_steps
            task.metadata['queued_version'] = task.version + 1
            connection = await self.external_connection(session)
            job_id = await self.job.configure(connection=connection, lock='task:' + task_id,
                queueing_lock='task:' + task_id).defer_async(task_id=task_id, expected_version=task.version + 1)
            task.queue_job_id = job_id
            await save_task(session, task, 'TASK_ENQUEUED', {'queue_job_id': job_id, 'resume': resume})
        return task

    async def request_control(self, task_id, *, cancel=False):
        if getattr(self.manager,'security',None):
            await asyncio.to_thread(self.manager.security.task,task_id,'task.cancel' if cancel else 'task.execute')
        async with self.runtime.sessions() as session, session.begin():
            task = await locked_task(session, task_id)
            if task.status not in {'PENDING','QUEUED','RUNNING','PAUSED','WAITING_USER','FAILED'}:
                raise TaskConflict('Task is terminal')
            if cancel:
                task.cancel_requested = True
            else:
                task.pause_requested = True
            if task.status != 'RUNNING':
                if task.queue_job_id:
                    await self.app.job_manager.cancel_job_by_id_async(task.queue_job_id,
                        connection=await self.external_connection(session))
                task.status = 'CANCELLED' if cancel else 'PAUSED'
            await save_task(session, task, 'TASK_CANCEL_REQUESTED' if cancel else 'TASK_PAUSE_REQUESTED')
        return task

    async def cancel(self, task_id):
        return await self.request_control(task_id, cancel=True)

    async def get_status(self, task_id):
        async with self.runtime.sessions() as session:
            row = (await session.execute(select(tasks.c.payload).where(tasks.c.id == task_id))).first()
            if not row:
                raise RecordNotFound(task_id)
            task = Task.model_validate(row[0])
            return {'status': task.status, 'queue_job_id': task.queue_job_id}

    async def health(self):
        try:
            async with self.runtime.sessions() as session:
                rows = await session.execute(text('SELECT status, count(*) FROM procrastinate_jobs WHERE queue_name=:queue GROUP BY status'),
                    {'queue': self.manager.config.get('WORKER_QUEUE', 'agent')})
                counts = dict(rows.all())
                row = (await session.execute(text("SELECT count(*), max(last_heartbeat) FROM procrastinate_workers WHERE last_heartbeat > now() - interval '30 seconds'"))).one()
            return {'queue': 'ok', 'worker': 'active' if row[0] else 'unavailable',
                    'last_heartbeat': row[1].isoformat() if row[1] else None,
                    'queue_depth': counts.get('todo', 0), 'running_jobs': counts.get('doing', 0)}
        except Exception:
            return {'queue': 'unavailable', 'worker': 'unknown'}
