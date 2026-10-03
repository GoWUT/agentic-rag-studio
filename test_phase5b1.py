"""Offline Phase 5B-1 policy, compatibility and migration safety tests."""
import asyncio
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from server.tasks import Task, TaskStore, TaskConflict
from server.worker_runtime import classify_failure, FailureKind
from server.config import load_config
from server.db.runtime import driver_url
from scripts.migrate_sqlite_to_postgres import read_sources, transform, MigrationBlocked, checksum


class PolicyTests(unittest.TestCase):
    def test_transient_mcp_transport_classified(self):
        from server.tool_registry import ToolTransportUnavailable
        self.assertEqual(classify_failure(ToolTransportUnavailable()), FailureKind.RETRYABLE)
    def test_mcp_configuration_not_retried(self):
        from server.tool_registry import ToolUnavailable
        self.assertEqual(classify_failure(ToolUnavailable()), FailureKind.NON_RETRYABLE)
    def test_grouped_mcp_timeout_identified(self):
        from server.mcp_client import transient_transport_error
        self.assertTrue(transient_transport_error(ExceptionGroup('transport', [TimeoutError()])))
    def test_mixed_mcp_error_group_not_retried(self):
        from server.mcp_client import transient_transport_error
        self.assertFalse(transient_transport_error(ExceptionGroup('mixed', [TimeoutError(), ValueError()])))
    def test_transient_read(self):
        self.assertEqual(classify_failure(TimeoutError()), FailureKind.RETRYABLE)
    def test_invalid_input(self):
        self.assertEqual(classify_failure(ValueError()), FailureKind.NON_RETRYABLE)
    def test_permission_denied(self):
        self.assertEqual(classify_failure(PermissionError()), FailureKind.NON_RETRYABLE)
    def test_missing_dataset(self):
        self.assertEqual(classify_failure(KeyError()), FailureKind.NON_RETRYABLE)
    def test_write_ambiguity_overrides_timeout(self):
        self.assertEqual(classify_failure(TimeoutError(), ambiguous=True), FailureKind.AMBIGUOUS_SIDE_EFFECT)
    def test_one_driver(self):
        self.assertEqual(driver_url('postgresql://u@localhost/db').drivername,'postgresql+psycopg')
    def test_reject_second_driver(self):
        with self.assertRaises(ValueError): driver_url('postgresql+asyncpg://u@localhost/db')
    def test_worker_requires_postgres(self):
        with patch.dict('os.environ',{'DATABASE_BACKEND':'sqlite','TASK_EXECUTION_MODE':'worker','TASK_QUEUE_ENABLED':'true'}):
            with self.assertRaises(ValueError): load_config()
    def test_worker_requires_queue(self):
        with patch.dict('os.environ',{'DATABASE_BACKEND':'postgresql','DATABASE_URL':'postgresql://localhost/db','TASK_EXECUTION_MODE':'worker','TASK_QUEUE_ENABLED':'false'}):
            with self.assertRaises(ValueError): load_config()
    def test_model_legacy_defaults(self):
        task=Task(workspace_id='w',session_id='s',goal='Fixture')
        self.assertEqual((task.execution_backend,task.version,task.attempt_count),('inline',0,0))
    def test_reconciliation_state(self):
        self.assertEqual(Task(workspace_id='w',session_id='s',goal='Fixture',status='REQUIRES_RECONCILIATION').status,'REQUIRES_RECONCILIATION')


class SQLiteCompatibility(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/'tasks.sqlite3'; self.store=TaskStore(self.path)
        self.task=self.store.create(Task(workspace_id='w',session_id='s',goal='Fixture'))
    def test_old_inline_transition(self):
        self.store.transition(self.task.id,'RUNNING'); self.store.transition(self.task.id,'COMPLETED')
        self.assertEqual(self.store.get(self.task.id).status,'COMPLETED')
    def test_event_and_status_rollback(self):
        with self.assertRaises(RuntimeError):
            with self.store.connect() as db:
                task=self.store.get(self.task.id); task.status='RUNNING'
                self.store._save(db,task); self.store._event(db,task.id,'TEST')
                raise RuntimeError('rollback')
        self.assertEqual(self.store.get(self.task.id).status,'PENDING')
        self.assertEqual(len(self.store.events(self.task.id)),1)
    def test_queued_cancel(self):
        with self.store.connect() as db:
            self.task.status='QUEUED'; self.store._save(db,self.task)
        self.assertEqual(self.store.transition(self.task.id,'CANCELLED').status,'CANCELLED')
    def test_terminal_cannot_requeue_inline(self):
        self.store.transition(self.task.id,'CANCELLED')
        with self.assertRaises(TaskConflict): self.store.transition(self.task.id,'RUNNING')
    def test_step_dedupe(self):
        self.store.set_plan(self.task.id,{'steps':[{'description':'read'}]})
        self.store.transition(self.task.id,'RUNNING'); self.store.start_step(self.task.id,0)
        self.store.checkpoint(self.task.id,0,{'result':1}); self.store.checkpoint(self.task.id,0,{'result':2})
        self.assertEqual(self.store.steps(self.task.id)[0].output_json,{'result':1})
    def test_migration_source_untouched(self):
        before=self.path.read_bytes(); data,versions,warnings=read_sources([self.path])
        self.assertEqual(before,self.path.read_bytes()); self.assertEqual(len(data['tasks']),1)
    def test_orphan_rejected_before_import(self):
        data,_,warnings=read_sources([self.path])
        with self.assertRaises(MigrationBlocked): transform(data,warnings)
    def test_checksum_order_independent(self):
        self.assertEqual(checksum([{'id':'a'},{'id':'b'}],['id']),checksum([{'id':'b'},{'id':'a'}],['id']))
    def test_checkpoint_tables_excluded(self):
        with self.store.connect() as db: db.execute('CREATE TABLE checkpoints (id TEXT)')
        data,_,warnings=read_sources([self.path])
        self.assertNotIn('checkpoints',data); self.assertTrue(any('checkpoints' in w for w in warnings))


class FrameworkTest(unittest.TestCase):
    def test_native_in_memory_queueing_lock(self):
        import procrastinate
        from procrastinate.testing import InMemoryConnector
        from procrastinate.exceptions import AlreadyEnqueued
        app=procrastinate.App(connector=InMemoryConnector())
        @app.task
        async def fixture(task_id): pass
        async def run():
            await fixture.configure(queueing_lock='same').defer_async(task_id='one')
            with self.assertRaises(AlreadyEnqueued):
                await fixture.configure(queueing_lock='same').defer_async(task_id='one')
        asyncio.run(run())


if __name__=='__main__': unittest.main()
