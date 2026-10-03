"""Background execution, classified retries and heartbeat-based crash recovery."""
import asyncio
from datetime import datetime, timezone
from enum import Enum
from sqlalchemy import select, update
from server.db.schema import tasks, executions, workspaces
from server.task_queue import locked_task, save_task
from server.tasks import Task
from server.persistence import RecordNotFound
from server.observability.runtime import worker_instrument


class FailureKind(str, Enum):
    RETRYABLE = 'RETRYABLE'
    NON_RETRYABLE = 'NON_RETRYABLE'
    AMBIGUOUS_SIDE_EFFECT = 'AMBIGUOUS_SIDE_EFFECT'


class RetryableTaskError(RuntimeError):
    """Only this sanitized exception enables framework retries."""


def classify_failure(error, *, ambiguous=False):
    if ambiguous:
        return FailureKind.AMBIGUOUS_SIDE_EFFECT
    from server.agent.execution_harness import ExecutionUnavailableError
    from sqlalchemy.exc import OperationalError, TimeoutError as PoolTimeout
    from openai import APIConnectionError, InternalServerError, RateLimitError
    from server.tool_registry import ToolTransportUnavailable
    if isinstance(error, (TimeoutError, ConnectionError, ExecutionUnavailableError,
                          OperationalError, PoolTimeout, APIConnectionError, InternalServerError, RateLimitError,
                          ToolTransportUnavailable)):
        return FailureKind.RETRYABLE
    return FailureKind.NON_RETRYABLE


async def ambiguous_write(session, task_id):
    values = (await session.execute(select(executions.c.payload).where(
        executions.c.task_id == task_id, executions.c.status.in_(['RUNNING','FAILED'])))).scalars()
    return any(v.get('operation_type') != 'read' and v.get('provider') != 'native' for v in values)


async def retryable_read_failure(session, task):
    rows = (await session.execute(select(executions.c.payload).where(
        executions.c.task_id == task.id, executions.c.status == 'FAILED'))).scalars()
    transient = {'TimeoutError','ReadTimeout','ConnectTimeout','APIConnectionError','InternalServerError',
                 'RateLimitError','ExecutionUnavailableError','ConnectionError','ToolTransportUnavailable'}
    return any(v.get('task_step_id') == task.current_step_id and v.get('operation_type') == 'read'
               and v.get('error_type') in transient for v in rows)


@worker_instrument
async def execute_task(queue, context, task_id, expected_version):
    manager, runtime = queue.manager, queue.runtime
    principal=None
    # Every delivery revalidates durable task and ownership; native execution lock
    # prevents overlap with the same queue job after retry or worker replacement.
    async with runtime.sessions() as session, session.begin():
        try:
            task = await locked_task(session, task_id)
        except RecordNotFound:
            return
        if task.queue_job_id != context.job.id or task.metadata.get('queued_version') != expected_version:
            return  # A stale delivery must never run a newer generation.
        if task.status not in {'QUEUED','RUNNING'}:
            return
        if task.cancel_requested or task.pause_requested:
            task.status = 'CANCELLED' if task.cancel_requested else 'PAUSED'
            await save_task(session, task, 'TASK_CANCELLED' if task.cancel_requested else 'TASK_PAUSED')
            return
        if await ambiguous_write(session, task_id):
            task.status = 'REQUIRES_RECONCILIATION'
            await save_task(session, task, 'TASK_REQUIRES_RECONCILIATION')
            return
        workspace = await session.scalar(select(workspaces.c.id).where(workspaces.c.id == task.workspace_id))
        from server.db.schema import sessions
        scope = (await session.execute(select(sessions.c.workspace_id).where(sessions.c.session_id == task.session_id))).first()
        authenticated=bool(getattr(manager,'security',None))
        if not workspace or not scope or scope[0] != task.workspace_id or (not authenticated and task.owner_id != 'local_default'):
            task.status, task.last_error = 'FAILED', 'Invalid task owner/workspace/session relation'
            await save_task(session, task, 'TASK_FAILED')
            return
        if authenticated:
            from fastapi import HTTPException
            try:
                if not task.created_by_user_id or task.owner_id!=task.created_by_user_id:
                    raise HTTPException(403,'Invalid actor')
                principal=await asyncio.to_thread(manager.auth.principal_for_user,task.created_by_user_id)
                await asyncio.to_thread(manager.security.task,task.id,'task.execute',principal)
            except HTTPException:
                task.status,task.last_error='FAILED','Authorization denied before execution'
                await save_task(session,task,'AUTHORIZATION_DENIED',{'action':'task.execute'})
                return
        task.status = 'RUNNING'
        task.started_at = task.started_at or datetime.now(timezone.utc).isoformat()
        task.worker_started_at = datetime.now(timezone.utc).isoformat()
        task.attempt_count += 1
        queue_wait = (datetime.now(timezone.utc)-datetime.fromisoformat(task.queued_at)).total_seconds()*1000
        task.metadata.update(worker_id=context.worker_name, attempt=task.attempt_count, queue_wait_ms=round(queue_wait, 3))
        await save_task(session, task, 'WORKER_STARTED', {'worker_id': context.worker_name,
            'queue_job_id': context.job.id, 'attempt': task.attempt_count, 'queue_wait_ms': round(queue_wait, 3)})
    from server.auth.context import principal_context
    principal_token=principal_context.set(principal)
    try:
        from server.auth.context import SystemExecutionContext
        result = await asyncio.to_thread(manager.phase3.task_service.run, task_id,
            worker_claimed=True, max_steps=task.metadata.get('worker_max_steps'),
            execution_context=SystemExecutionContext(principal,task_id) if principal else None)
        if result.status == 'CANCELLED' and manager.phase5:
            await asyncio.to_thread(manager.phase5.store.cancel, task_id)
        if result.status == 'FAILED':
            raise ValueError(result.last_error or 'Task execution failed')
    except Exception as error:
        async with runtime.sessions() as session, session.begin():
            current = await locked_task(session, task_id)
            kind = classify_failure(error, ambiguous=await ambiguous_write(session, task_id))
            if kind == FailureKind.NON_RETRYABLE and await retryable_read_failure(session, current):
                kind = FailureKind.RETRYABLE
            if current.cancel_requested:
                current.status = 'CANCELLED'
            elif kind == FailureKind.AMBIGUOUS_SIDE_EFFECT:
                current.status = 'REQUIRES_RECONCILIATION'
            elif kind == FailureKind.RETRYABLE and current.attempt_count <= manager.config.get('WORKER_MAX_RETRIES', 2):
                current.status = 'QUEUED'
            elif current.status not in {'PAUSED','WAITING_USER','COMPLETED'}:
                current.status = 'FAILED'
            current.last_error = type(error).__name__  # never log exception message/secrets
            current.metadata['retry_reason'] = kind.value
            await save_task(session, current, 'WORKER_FAILURE', {'classification': kind.value,
                'error_type': type(error).__name__, 'attempt': current.attempt_count})
        if kind == FailureKind.RETRYABLE and current.status == 'QUEUED':
            raise RetryableTaskError(type(error).__name__) from None
        if current.status == 'FAILED':
            raise RuntimeError('Task failed without automatic retry') from None
    finally:
        principal_context.reset(principal_token)


async def recover_stalled(queue):
    """Framework heartbeats, never elapsed task duration, decide stalled status."""
    jobs = await queue.app.job_manager.get_stalled_jobs(queue=queue.manager.config.get('WORKER_QUEUE','agent'),
                                                       task_name='agent.execute')
    counts = {'retried': 0, 'reconciliation': 0, 'ignored': 0}
    for job in jobs:
        task_id = job.task_kwargs.get('task_id')
        async with queue.runtime.sessions() as session, session.begin():
            try:
                task = await locked_task(session, task_id)
            except RecordNotFound:
                task = None
            retry = bool(task and task.queue_job_id == job.id and task.status in {'QUEUED','RUNNING'})
            if retry and await ambiguous_write(session, task_id):
                task.status = 'REQUIRES_RECONCILIATION'
                await save_task(session, task, 'TASK_REQUIRES_RECONCILIATION')
                counts['reconciliation'] += 1
                retry = False
            if retry and task.attempt_count > queue.manager.config.get('WORKER_MAX_RETRIES',2):
                task.status, task.last_error = 'FAILED', 'Worker retry exhausted'
                await save_task(session, task, 'WORKER_RETRY_EXHAUSTED')
                retry = False
        # Official retry API. The task remains RUNNING until a replacement worker
        # claims it, avoiding an application state with no durable queue job.
        if retry:
            await queue.app.job_manager.retry_job(job)
            counts['retried'] += 1
        else:
            # This job is confirmed stalled by SDK heartbeats; finish it through
            # the official API, preserving history and releasing execution locks.
            from procrastinate.jobs import Status
            await queue.app.job_manager.finish_job(job, Status.FAILED, delete_job=False)
            counts['ignored'] += 1
    return counts
