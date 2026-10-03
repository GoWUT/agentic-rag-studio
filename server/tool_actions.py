"""Durable intent, approval and single-claim execution ledger for external tools."""
import hashlib
import json
from typing import Literal
from pydantic import BaseModel, Field
from server.persistence import Database, RecordNotFound, encode, identity, now
from server.tasks import TaskConflict
from server.tool_registry import validate_arguments


class ApprovalRequest(BaseModel):
    id: str = Field(default_factory=identity)
    task_id: str
    task_step_id: str
    session_id: str | None = None
    tool_name: str
    provider: str
    capability: str
    operation_type: str
    risk_level: str
    arguments: dict
    reason: str
    status: Literal['PENDING','APPROVED','REJECTED','EDITED','EXPIRED','CANCELLED'] = 'PENDING'
    requested_at: str = Field(default_factory=now)
    decided_at: str | None = None
    decision: str | None = None
    edited_arguments: dict | None = None
    consumed_at: str | None = None
    tool_execution_id: str
    descriptor: dict


def arguments_hash(arguments):
    return hashlib.sha256(json.dumps(arguments, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()


class ToolActionStore(Database):
    def __init__(self, path):
        super().__init__(path)
        if self.backend == 'postgresql':
            return  # Application DDL is owned by Alembic.
        with self.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS tool_executions (id TEXT PRIMARY KEY, intent_key TEXT NOT NULL UNIQUE, task_id TEXT, status TEXT NOT NULL, payload TEXT NOT NULL)')
            db.execute('CREATE INDEX IF NOT EXISTS tool_execution_task ON tool_executions(task_id)')
            db.execute('CREATE TABLE IF NOT EXISTS approval_requests (id TEXT PRIMARY KEY, tool_execution_id TEXT NOT NULL UNIQUE REFERENCES tool_executions(id), task_id TEXT NOT NULL, status TEXT NOT NULL, payload TEXT NOT NULL)')
            db.execute('CREATE INDEX IF NOT EXISTS approval_task_status ON approval_requests(task_id,status)')

    @staticmethod
    def _save(db, table, value):
        db.execute(f'UPDATE {table} SET status=?,payload=? WHERE id=?', (value['status'], encode(value), value['id']))

    @staticmethod
    def _get(db, table, record_id):
        row = db.execute(f'SELECT payload FROM {table} WHERE id=?', (record_id,)).fetchone()
        if not row:
            raise RecordNotFound(record_id)
        return json.loads(row[0])

    def execution(self, record_id):
        with self.connect() as db:
            return self._get(db, 'tool_executions', record_id)

    def approval(self, record_id):
        with self.connect() as db:
            return self._get(db, 'approval_requests', record_id)

    def executions(self, task_id=None):
        with self.connect() as db:
            rows = [json.loads(r[0]) for r in db.execute('SELECT payload FROM tool_executions ORDER BY rowid DESC')]
        return [r for r in rows if not task_id or r['task_id'] == task_id]

    def approvals(self, task_id=None, status=None):
        with self.connect() as db:
            rows = [json.loads(r[0]) for r in db.execute('SELECT payload FROM approval_requests ORDER BY rowid DESC')]
        return [r for r in rows if (not task_id or r['task_id'] == task_id) and (not status or r['status'] == status)]

    def request(self, descriptor, arguments, context, decision):
        digest = arguments_hash(arguments)
        key = arguments_hash([context.get('task_id') or context['run_id'], context.get('task_step_id'), descriptor.name, digest,
                              identity() if not decision.requires_approval and (descriptor.operation_type == 'read' or descriptor.provider_type == 'native') else None])
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            old = db.execute('SELECT payload FROM tool_executions WHERE intent_key=?', (key,)).fetchone()
            if old:
                return json.loads(old[0])
            value = dict(id=identity(), task_id=context.get('task_id'), task_step_id=context.get('task_step_id'),
                session_id=context.get('session_id'), tool_name=descriptor.name, provider=descriptor.provider,
                capability=descriptor.capability, operation_type=descriptor.operation_type, risk_level=descriptor.risk_level,
                arguments_hash=digest, approval_request_id=None, status='REQUESTED', requested_at=now(),
                started_at=None, completed_at=None, duration_ms=None, result_summary=None, error_type=None,
                metadata={'run_id': context.get('tool_run_id', context.get('run_id')),
                    **{k: context[k] for k in ('agent_id', 'delegation_id', 'supervisor_run_id') if context.get(k)}})
            db.execute('INSERT INTO tool_executions VALUES (?,?,?,?,?)', (value['id'], key, value['task_id'], value['status'], encode(value)))
            if decision.requires_approval:
                if not value['task_id'] or not value['task_step_id']:
                    raise TaskConflict('Sensitive tools require a Persistent Task')
                approval = dict(id=identity(), task_id=value['task_id'], task_step_id=value['task_step_id'],
                    session_id=value['session_id'], tool_name=descriptor.name, provider=descriptor.provider,
                    capability=descriptor.capability, operation_type=descriptor.operation_type, risk_level=descriptor.risk_level,
                    arguments=arguments, reason=decision.reason, status='PENDING', requested_at=now(),
                    decided_at=None, decision=None, edited_arguments=None, consumed_at=None,
                    tool_execution_id=value['id'], descriptor=descriptor.model_dump())
                approval = ApprovalRequest.model_validate(approval).model_dump()
                db.execute('INSERT INTO approval_requests VALUES (?,?,?,?,?)', (approval['id'], value['id'], value['task_id'], 'PENDING', encode(approval)))
                value.update(approval_request_id=approval['id'], status='WAITING_APPROVAL')
                self._save(db, 'tool_executions', value)
        return value

    def decide(self, approval_id, decision, *, edits=None, policy=None):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            approval = self._get(db, 'approval_requests', approval_id)
            if approval['status'] != 'PENDING' or approval['consumed_at']:
                raise TaskConflict('Approval already decided or consumed')
            if decision not in {'APPROVED', 'EDITED', 'REJECTED'}:
                raise ValueError('Invalid approval decision')
            from server.tool_registry import ToolDescriptor
            descriptor = ToolDescriptor.model_validate(approval['descriptor'])
            arguments = approval['arguments']
            if decision == 'EDITED':
                if not edits or set(edits) - {'title', 'body'}:
                    raise ValueError('Only title/body may be edited')
                arguments = {**arguments, **edits}
                validate_arguments(descriptor, arguments)
                policy.evaluate(descriptor, arguments)
                approval['edited_arguments'] = arguments
            elif decision == 'APPROVED':
                validate_arguments(descriptor, arguments)
                policy.evaluate(descriptor, arguments)
            approval.update(status=decision, decision=decision, decided_at=now())
            self._save(db, 'approval_requests', approval)
        return approval

    def claim(self, execution_id):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            value = self._get(db, 'tool_executions', execution_id)
            if value['status'] in {'SUCCEEDED', 'REJECTED', 'CANCELLED'}:
                return False, value
            if value['status'] in {'RUNNING', 'FAILED'}:
                raise TaskConflict('Prior action may have executed; reconcile before retrying')
            if value['approval_request_id']:
                approval = self._get(db, 'approval_requests', value['approval_request_id'])
                if approval['consumed_at'] or approval['status'] == 'PENDING':
                    raise TaskConflict('Approval unavailable for consumption')
                if approval['status'] not in {'APPROVED', 'EDITED', 'REJECTED'}:
                    raise TaskConflict('Approval cancelled or expired')
                approval['consumed_at'] = now()
                if approval['edited_arguments']:
                    value['metadata']['requested_arguments_hash'] = value['arguments_hash']
                    value['arguments_hash'] = arguments_hash(approval['edited_arguments'])
                self._save(db, 'approval_requests', approval)
                if approval['status'] == 'REJECTED':
                    value.update(status='REJECTED', completed_at=now(), result_summary={'status': 'REJECTED'})
                    self._save(db, 'tool_executions', value)
                    return False, value
            value.update(status='RUNNING', started_at=now())
            self._save(db, 'tool_executions', value)
        return True, value

    def finish(self, execution_id, *, success, summary, duration_ms, error_type=None):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            value = self._get(db, 'tool_executions', execution_id)
            if value['status'] != 'RUNNING':
                raise TaskConflict('Execution has no active claim')
            value.update(status='SUCCEEDED' if success else 'FAILED', completed_at=now(), result_summary=summary,
                duration_ms=duration_ms, error_type=error_type)
            self._save(db, 'tool_executions', value)
        return value

    def cancel_pending(self, task_id):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            for approval in [json.loads(r[0]) for r in db.execute('SELECT payload FROM approval_requests WHERE task_id=?', (task_id,))]:
                if approval['consumed_at'] or approval['status'] not in {'PENDING', 'APPROVED', 'EDITED', 'REJECTED'}:
                    continue
                approval.update(status='CANCELLED', decided_at=now())
                self._save(db, 'approval_requests', approval)
                value = self._get(db, 'tool_executions', approval['tool_execution_id'])
                if value['status'] != 'RUNNING':
                    value.update(status='CANCELLED', completed_at=now())
                    self._save(db, 'tool_executions', value)
