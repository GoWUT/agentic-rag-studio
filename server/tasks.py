"""Durable task checkpoints; reasoning and execution stay in the existing graph."""
from threading import Lock
import json
from typing import Literal
from pydantic import BaseModel, Field
from server.persistence import Database, RecordNotFound, encode, identity, now
from server.auth.context import principal_service
from server.observability.runtime import task_span


class TaskConflict(ValueError):
    pass


class Task(BaseModel):
    id: str = Field(default_factory=identity)
    owner_id: str = 'local_default'
    created_by_user_id: str | None = None
    workspace_id: str
    session_id: str
    goal: str = Field(min_length=1, max_length=6000)
    description: str = ''
    status: Literal['PENDING','QUEUED','RUNNING','PAUSED','WAITING_USER','COMPLETED','FAILED','CANCELLED','REQUIRES_RECONCILIATION'] = 'PENDING'
    source_scope: Literal['workspace_only','workspace_and_external','external'] = 'workspace_only'
    plan_json: dict | None = None
    current_step_id: str | None = None
    created_at: str = Field(default_factory=now)
    updated_at: str = Field(default_factory=now)
    started_at: str | None = None
    completed_at: str | None = None
    last_error: str | None = None
    metadata: dict = Field(default_factory=dict)
    queue_job_id: int | None = None
    queued_at: str | None = None
    worker_started_at: str | None = None
    attempt_count: int = 0
    execution_backend: Literal['inline', 'worker'] = 'inline'
    version: int = 0
    pause_requested: bool = False
    cancel_requested: bool = False


class TaskStep(BaseModel):
    id: str = Field(default_factory=identity)
    task_id: str
    step_index: int
    description: str
    preferred_tool: str | None = None
    status: str = 'PENDING'
    input_json: dict = Field(default_factory=dict)
    output_json: dict = Field(default_factory=dict)
    attempt_count: int = 0
    started_at: str | None = None
    completed_at: str | None = None
    error: str | None = None


class TaskEvent(BaseModel):
    id: str = Field(default_factory=identity)
    task_id: str
    event_type: str
    message: str = ''
    payload_json: dict = Field(default_factory=dict)
    created_at: str = Field(default_factory=now)


class TaskStore(Database):
    def __init__(self, path, max_attempts=2):
        super().__init__(path)
        self.max_attempts = max_attempts
        if self.backend == 'postgresql':
            return  # Application DDL is owned by Alembic.
        with self.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS tasks (id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL, session_id TEXT NOT NULL, status TEXT NOT NULL, payload TEXT NOT NULL)')
            db.execute('CREATE INDEX IF NOT EXISTS task_status ON tasks(status)')
            db.execute('CREATE INDEX IF NOT EXISTS task_workspace ON tasks(workspace_id)')
            db.execute('CREATE TABLE IF NOT EXISTS task_steps (id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id), step_index INTEGER NOT NULL, status TEXT NOT NULL, payload TEXT NOT NULL, UNIQUE(task_id,step_index))')
            db.execute('CREATE TABLE IF NOT EXISTS task_events (id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id), event_type TEXT NOT NULL, payload TEXT NOT NULL)')
            db.execute('CREATE INDEX IF NOT EXISTS task_event_task ON task_events(task_id)')

    @staticmethod
    def _event(db, task_id, kind, payload=None):
        event = TaskEvent(task_id=task_id,event_type=kind,payload_json=payload or {})
        db.execute('INSERT INTO task_events VALUES (?,?,?,?)', (event.id,task_id,kind,event.model_dump_json()))

    @staticmethod
    def _save(db, task):
        task.updated_at = now()
        expected = task.version
        task.version += 1
        if hasattr(db, 'connection'):
            cursor = db.execute('UPDATE tasks SET status=?,payload=?,version=?,queue_job_id=?,queued_at=?,worker_started_at=?,attempt_count=?,execution_backend=? WHERE id=? AND version=?',
                (task.status,task.model_dump_json(),task.version,task.queue_job_id,task.queued_at,task.worker_started_at,
                 task.attempt_count,task.execution_backend,task.id,expected))
            if cursor.rowcount != 1:
                raise TaskConflict('Task was updated concurrently')
            return
        db.execute('UPDATE tasks SET status=?,payload=? WHERE id=?', (task.status,task.model_dump_json(),task.id))

    def create(self, task):
        with self.connect() as db:
            db.execute('INSERT INTO tasks (id,workspace_id,session_id,status,payload) VALUES (?,?,?,?,?)', (task.id,task.workspace_id,task.session_id,task.status,task.model_dump_json()))
            if task.created_by_user_id:
                db.execute('UPDATE tasks SET created_by_user_id=? WHERE id=?',(task.created_by_user_id,task.id))
            self._event(db,task.id,'TASK_CREATED')
        return task

    def get(self, task_id):
        with self.connect() as db:
            row = db.execute('SELECT payload FROM tasks WHERE id=?', (task_id,)).fetchone()
        if not row:
            raise RecordNotFound(task_id)
        return Task.model_validate_json(row[0])

    def list(self, workspace_id=None, status=None):
        with self.connect() as db:
            tasks = [Task.model_validate_json(row[0]) for row in db.execute('SELECT payload FROM tasks ORDER BY rowid DESC')]
        return [t for t in tasks if (not workspace_id or t.workspace_id == workspace_id) and (not status or t.status == status)]

    def steps(self, task_id):
        self.get(task_id)
        with self.connect() as db:
            return [TaskStep.model_validate_json(row[0]) for row in db.execute('SELECT payload FROM task_steps WHERE task_id=? ORDER BY step_index', (task_id,))]

    def events(self, task_id):
        self.get(task_id)
        with self.connect() as db:
            return [TaskEvent.model_validate_json(row[0]) for row in db.execute('SELECT payload FROM task_events WHERE task_id=? ORDER BY rowid', (task_id,))]

    def set_plan(self, task_id, plan):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            task = Task.model_validate_json(db.execute('SELECT payload FROM tasks WHERE id=?', (task_id,)).fetchone()[0])
            if task.plan_json:
                return
            task.plan_json = plan
            for index, value in enumerate(plan['steps']):
                step = TaskStep(task_id=task_id,step_index=index,description=value['description'],preferred_tool=value.get('preferred_tool'),input_json=value)
                db.execute('INSERT INTO task_steps VALUES (?,?,?,?,?)', (step.id,task_id,index,step.status,step.model_dump_json()))
            self._save(db,task)
            self._event(db,task_id,'PLAN_CREATED',{'step_count':len(plan['steps'])})

    def transition(self, task_id, status, *, error=None):
        legal = {'RUNNING':{'PENDING','QUEUED','PAUSED','WAITING_USER','FAILED'},'PAUSED':{'RUNNING','PENDING','QUEUED'},
                 'CANCELLED':{'PENDING','QUEUED','RUNNING','PAUSED','WAITING_USER','FAILED'},
                 'COMPLETED':{'RUNNING'},'FAILED':{'RUNNING'},'WAITING_USER':{'RUNNING'}}
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT payload FROM tasks WHERE id=?',(task_id,)).fetchone()
            if not row:
                raise RecordNotFound(task_id)
            task = Task.model_validate_json(row[0])
            if task.status not in legal.get(status,set()):
                raise TaskConflict(f'Cannot transition {task.status} to {status}')
            task.status, task.last_error = status, error
            if status == 'RUNNING':
                task.started_at = task.started_at or now()
            if status == 'COMPLETED':
                task.completed_at = now()
            self._save(db,task)
            self._event(db,task_id,{'RUNNING':'TASK_RESUMED' if task.plan_json else 'TASK_STARTED','PAUSED':'TASK_PAUSED','CANCELLED':'TASK_CANCELLED','COMPLETED':'TASK_COMPLETED','FAILED':'TASK_FAILED','WAITING_USER':'TASK_WAITING_USER'}[status])
        return task

    def start_step(self, task_id, index):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            task = Task.model_validate_json(db.execute('SELECT payload FROM tasks WHERE id=?',(task_id,)).fetchone()[0])
            if task.status != 'RUNNING':
                return None
            row = db.execute('SELECT payload FROM task_steps WHERE task_id=? AND step_index=?',(task_id,index)).fetchone()
            if not row:
                raise RecordNotFound('step')
            step = TaskStep.model_validate_json(row[0])
            if step.status == 'WAITING_USER':
                # LangGraph re-enters an interrupted node; this is the same attempt.
                return step
            if step.status in {'COMPLETED','SKIPPED'}:
                return None
            if step.attempt_count >= self.max_attempts:
                raise TaskConflict('Task step retry exhausted')
            step.status,step.started_at = 'RUNNING',now()
            step.attempt_count += 1
            db.execute('UPDATE task_steps SET status=?,payload=? WHERE id=?',(step.status,step.model_dump_json(),step.id))
            task.current_step_id = step.id
            self._save(db,task)
            self._event(db,task_id,'STEP_STARTED',{'step_id':step.id,'attempt':step.attempt_count})
            if step.preferred_tool:
                self._event(db,task_id,'TOOL_CALLED',{'step_id':step.id,'tool':step.preferred_tool})
        return step

    def wait_for_approval(self, task_id, step_id):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            task = Task.model_validate_json(db.execute('SELECT payload FROM tasks WHERE id=?', (task_id,)).fetchone()[0])
            step = TaskStep.model_validate_json(db.execute('SELECT payload FROM task_steps WHERE id=? AND task_id=?', (step_id, task_id)).fetchone()[0])
            task.status, step.status = 'WAITING_USER', 'WAITING_USER'
            self._save(db, task)
            db.execute('UPDATE task_steps SET status=?,payload=? WHERE id=?', (step.status, step.model_dump_json(), step.id))
            self._event(db, task_id, 'TASK_WAITING_USER', {'step_id': step.id, 'reason': 'tool_approval'})

    def checkpoint(self, task_id, index, output, *, success=True):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT payload FROM task_steps WHERE task_id=? AND step_index=?',(task_id,index)).fetchone()
            step = TaskStep.model_validate_json(row[0])
            if step.status == 'COMPLETED':
                return
            step.status = 'COMPLETED' if success else 'FAILED'
            step.output_json,step.completed_at = output,now()
            step.error = None if success else 'Step execution failed'
            db.execute('UPDATE task_steps SET status=?,payload=? WHERE id=?',(step.status,step.model_dump_json(),step.id))
            self._event(db,task_id,'STEP_COMPLETED' if success else 'STEP_FAILED',{'step_id':step.id})

    def recover(self):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            for row in db.execute('SELECT payload FROM tasks WHERE status=?',('RUNNING',)).fetchall():
                task = Task.model_validate_json(row[0])
                task.status = 'PAUSED'
                self._save(db,task)
                self._event(db,task.id,'TASK_RECOVERED_AFTER_RESTART')
                for entry in db.execute('SELECT payload FROM task_steps WHERE task_id=? AND status=?',(task.id,'RUNNING')).fetchall():
                    step = TaskStep.model_validate_json(entry[0])
                    step.status = 'PENDING'
                    db.execute('UPDATE task_steps SET status=?,payload=? WHERE id=?', (step.status,step.model_dump_json(),step.id))


class TaskService:
    def __init__(self, manager, store):
        self.manager,self.store = manager,store
        self.locks = {}
        self.registry_lock = Lock()

    @principal_service
    def create(self, workspace_id, goal, source_scope='workspace_only', session_id=None):
        if getattr(self.manager, 'phase4', None):
            self.manager.phase4.policy.secrets.require_safe({'goal': goal})
        self.manager.workspaces.get(workspace_id)
        if session_id:
            session = self.manager.store.get(session_id)
            if not session or session.workspace_id != workspace_id:
                raise ValueError('Task session does not belong to workspace')
        else:
            session_id = self.manager.create_workspace_session(workspace_id)['session_id']
        return self.store.create(Task(workspace_id=workspace_id,session_id=session_id,goal=goal,source_scope=source_scope))

    @principal_service
    @task_span
    def run(self, task_id, *, resume=False, max_steps=None, worker_claimed=False):
        with self.registry_lock:
            lock = self.locks.setdefault(task_id,Lock())
        if not lock.acquire(blocking=False):
            raise TaskConflict('Task already executing')
        try:
            task = self.store.get(task_id)
            if resume and not worker_claimed and task.status not in {'PAUSED','WAITING_USER','FAILED'}:
                raise TaskConflict('Task is not resumable')
            if not resume and not worker_claimed and task.status != 'PENDING':
                raise TaskConflict('Use resume for an existing task')
            phase4 = getattr(self.manager, 'phase4', None)
            approvals = phase4.actions.approvals(task_id) if phase4 else []
            outstanding = [a for a in approvals if not a['consumed_at'] and a['status'] in {'PENDING','APPROVED','EDITED','REJECTED'}]
            if outstanding and any(a['status'] == 'PENDING' for a in outstanding):
                raise TaskConflict('Task has a pending human approval')
            if not worker_claimed:
                self.store.transition(task_id,'RUNNING')
            elif task.status != 'RUNNING':
                raise TaskConflict('Worker has no execution claim')
            scope = {'task_id':task_id,'task_step_limit':max_steps,'owner_id':task.owner_id}
            if outstanding:
                scope['approval_resume_id'] = outstanding[0]['id']
            if task.plan_json:
                steps = self.store.steps(task_id)
                completed = [s for s in steps if s.status in {'COMPLETED','SKIPPED'}]
                next_index = next((s.step_index for s in steps if s.status not in {'COMPLETED','SKIPPED'}),len(steps))
                restored = completed[-1].output_json if completed else {}
                scope['task_resume'] = {**restored,'task_plan':task.plan_json,'plan':task.plan_json['steps'],
                                        'current_step':next_index,'plan_is_task_plan':True,
                                        'original_query':task.goal,'standalone_query':task.goal,'task_goal':task.goal,
                                        'task_complexity':'complex','needs_retrieval':True,'query_type':'research'}
            try:
                reply = self.manager.ask(task.session_id,task.goal,workspace_id=task.workspace_id,
                                         source_scope=task.source_scope,runtime_context=scope)
                task = self.store.get(task_id)
                if task.status == 'RUNNING':
                    steps = self.store.steps(task_id)
                    if any(step.status == 'FAILED' for step in steps):
                        self.store.transition(task_id,'FAILED',error='A task step failed')
                    elif any(step.status not in {'COMPLETED','SKIPPED'} for step in steps):
                        self.store.transition(task_id,'PAUSED')
                    else:
                        self.store.transition(task_id,'COMPLETED')
                task = self.store.get(task_id)
                if task.status == 'COMPLETED':
                    task.metadata['answer'] = reply.answer
                    with self.store.connect() as db:
                        self.store._save(db,task)
                return task
            except Exception as error:
                if self.store.get(task_id).status == 'RUNNING':
                    self.store.transition(task_id,'FAILED',error=type(error).__name__)
                raise
        finally:
            lock.release()
