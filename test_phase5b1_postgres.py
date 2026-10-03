"""Opt-in real PostgreSQL tests. PHASE5B1_TEST_DATABASE_URL names a test cluster.

Every test retains a unique isolated schema; no existing business data is deleted.
Ordinary regression skips these tests without PostgreSQL.
"""
import asyncio
from io import BytesIO
import os
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch, AsyncMock
from uuid import uuid4
from sqlalchemy import create_engine, text, select, func
from sqlalchemy.pool import NullPool
from server.db.schema import metadata, tasks, events
from server.db.runtime import driver_url, REVISION
from server.config import CONFIG
from server.sessions import AgentSessionManager, SessionStore
from server.workspaces import WorkspaceStore
from server.tasks import TaskStore, Task, TaskConflict
from server.memory import MemoryStore, LongTermMemory
from server.tool_actions import ToolActionStore
from server.agent_runs import AgentRunStore
from server.runtime_services import configure_runtime, startup_runtime, shutdown_runtime
from server.worker_runtime import execute_task
from scripts.migrate_sqlite_to_postgres import migrate, MigrationBlocked


@unittest.skipUnless(os.getenv('PHASE5B1_TEST_DATABASE_URL'), 'real PostgreSQL test URL not configured')
class PostgresTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        url=driver_url(os.environ['PHASE5B1_TEST_DATABASE_URL'])
        schema='fixture_'+uuid4().hex
        engine=create_engine(url,poolclass=NullPool)
        with engine.begin() as db: db.execute(text('CREATE SCHEMA '+schema))
        engine.dispose()
        self.url=url.update_query_dict({'options':'-csearch_path='+schema}).render_as_string(hide_password=False)
        engine=create_engine(self.url,poolclass=NullPool,hide_parameters=True)
        with engine.begin() as db:
            metadata.create_all(db)
            db.execute(text('CREATE TABLE alembic_version (version_num VARCHAR(32) PRIMARY KEY)'))
            db.execute(text('INSERT INTO alembic_version VALUES (:v)'),{'v':REVISION})
        engine.dispose()
        self.config={**CONFIG,'DATABASE_BACKEND':'postgresql','DATABASE_URL':self.url,
            'TASK_EXECUTION_MODE':'worker','TASK_QUEUE_ENABLED':True,'WORKSPACE_DIR':str(self.root),
            'MCP_SERVERS_FILE':'','GITHUB_MCP_ENABLED':False,'MEMORY_ENABLED':False,'MEMORY_AUTO_EXTRACT':False}
        self.manager=AgentSessionManager(self.config); configure_runtime(self.manager)
        self.runner=asyncio.Runner(loop_factory=asyncio.SelectorEventLoop)
        self.addCleanup(self.close)
        self.runner.run(self.manager.task_queue.open())
        self.runner.run(self.manager.task_queue.app.schema_manager.apply_schema_async())
        self.manager.phase4.checkpointer.open(setup=True)
        self.workspace=self.manager.workspaces.create('PostgreSQL fixture')
        self.task=self.manager.phase3.task_service.create(self.workspace.id,'Synthetic fixture')

    def close(self):
        self.runner.run(shutdown_runtime(self.manager)); self.runner.close()

    def run_async(self, coroutine):
        return self.runner.run(coroutine)

    def count_jobs(self):
        with self.manager.database.sync_engine.connect() as db:
            return db.scalar(text("SELECT count(*) FROM procrastinate_jobs WHERE task_name='agent.execute'"))

    def test_workspace_session_task_step_event_crud(self):
        self.assertEqual(self.manager.store.get(self.task.session_id).workspace_id,self.workspace.id)
        store=self.manager.phase3.tasks
        store.set_plan(self.task.id,{'steps':[{'description':'read'}]})
        store.transition(self.task.id,'RUNNING'); store.start_step(self.task.id,0)
        store.checkpoint(self.task.id,0,{'nested':{'value':[1,2]}}); store.transition(self.task.id,'COMPLETED')
        self.assertEqual(store.steps(self.task.id)[0].output_json,{'nested':{'value':[1,2]}})
        self.assertGreaterEqual(len(store.events(self.task.id)),5)

    def test_memory_dataset_analysis_artifact_crud(self):
        memory=MemoryStore(self.manager.database,{})
        record=memory.create(LongTermMemory(content='Use concise tables',memory_type='instruction',scope_type='workspace',scope_id=self.workspace.id))
        self.assertEqual(memory.get(record.id).content,record.content)
        data=self.manager.phase3.data
        asset=data.upload(self.workspace.id,'fixture.csv',BytesIO(b'x,y\n1,2\n'),'text/csv')
        self.assertEqual(data.get(self.workspace.id,asset.id).row_count,1)
        execution_id='analysis-'+uuid4().hex
        data.save_execution({'execution_id':execution_id,'workspace_id':self.workspace.id,'task_id':self.task.id})
        from server.data_assets import Artifact
        artifact_path=data.root/'fixture.txt'; artifact_path.write_text('x')
        artifact=Artifact(workspace_id=self.workspace.id,task_id=self.task.id,execution_id=execution_id,
            filename='fixture.txt',artifact_type='text',mime_type='text/plain',file_path=str(artifact_path),size_bytes=1)
        data.register_artifact(artifact)
        self.assertEqual(data.artifact(artifact.id).execution_id,execution_id)

    def test_approval_tool_execution_agent_run_delegation_crud(self):
        from server.tool_registry import ToolDescriptor
        tool=ToolDescriptor(name='mcp.fixture.create',provider='fixture',provider_type='mcp',capability='github.issue.create',
            input_schema={'type':'object'},operation_type='write',risk_level='high')
        actions=self.manager.phase4.actions
        self.manager.phase3.tasks.set_plan(self.task.id,{'steps':[{'description':'write'}]})
        step=self.manager.phase3.tasks.steps(self.task.id)[0]
        record=actions.request(tool,{}, {'task_id':self.task.id,'task_step_id':step.id,'run_id':'fixture'},
            SimpleNamespace(requires_approval=True,reason='Synthetic fixture'))
        approval=actions.approval(record['approval_request_id'])
        actions.decide(approval['id'],'REJECTED',policy=self.manager.phase4.policy)
        self.assertFalse(actions.claim(record['id'])[0])
        store=self.manager.phase5.store
        supervisor=store.create_supervisor(self.task.id,'Fixture','single_agent',{})
        from test_phase5a import sample_request
        request=sample_request(self.manager.phase5.registry,task_id=self.task.id,supervisor_run_id=supervisor['id'])
        delegation=store.create_delegation(request,8)
        self.assertEqual(store.delegation(delegation['id'])['supervisor_run_id'],supervisor['id'])
        self.assertEqual(store.run(delegation['agent_run_id'])['agent_id'],'research')

    def test_atomic_enqueue_and_minimal_payload(self):
        queued=self.run_async(self.manager.task_queue.enqueue(self.task.id))
        self.assertEqual(queued.status,'QUEUED'); self.assertEqual(self.count_jobs(),1)
        with self.manager.database.sync_engine.connect() as db:
            args=db.scalar(text('SELECT args FROM procrastinate_jobs WHERE id=:id'),{'id':queued.queue_job_id})
        self.assertEqual(set(args),{'task_id','expected_version'})

    def test_defer_failure_rolls_back_task_event(self):
        before=len(self.manager.phase3.tasks.events(self.task.id))
        with patch.object(self.manager.task_queue.job,'configure', side_effect=RuntimeError('queue unavailable')):
            with self.assertRaises(RuntimeError): self.run_async(self.manager.task_queue.enqueue(self.task.id))
        self.assertEqual(self.manager.phase3.tasks.get(self.task.id).status,'PENDING')
        self.assertEqual(len(self.manager.phase3.tasks.events(self.task.id)),before)
        self.assertEqual(self.count_jobs(),0)

    def test_task_write_failure_rolls_back_deferred_job(self):
        async def fail(*args,**kwargs): raise RuntimeError('task write failed after actual defer')
        with patch('server.task_queue.save_task',side_effect=fail):
            with self.assertRaises(RuntimeError): self.run_async(self.manager.task_queue.enqueue(self.task.id))
        self.assertEqual(self.count_jobs(),0)
        self.assertEqual(self.manager.phase3.tasks.get(self.task.id).status,'PENDING')

    def test_concurrent_double_enqueue(self):
        async def run():
            return await asyncio.gather(self.manager.task_queue.enqueue(self.task.id),
                self.manager.task_queue.enqueue(self.task.id),return_exceptions=True)
        result=self.run_async(run())
        self.assertEqual(sum(isinstance(v,TaskConflict) for v in result),1)
        self.assertEqual(self.count_jobs(),1)

    def test_cancel_queued_job(self):
        self.run_async(self.manager.task_queue.enqueue(self.task.id))
        self.assertEqual(self.run_async(self.manager.task_queue.cancel(self.task.id)).status,'CANCELLED')
        with self.manager.database.sync_engine.connect() as db:
            self.assertEqual(db.scalar(text('SELECT status FROM procrastinate_jobs')),'cancelled')

    def test_stale_job_no_execution(self):
        queued=self.run_async(self.manager.task_queue.enqueue(self.task.id))
        context=SimpleNamespace(job=SimpleNamespace(id=queued.queue_job_id+1),worker_name='fixture')
        with patch.object(self.manager.phase3.task_service,'run') as run:
            self.run_async(execute_task(self.manager.task_queue,context,self.task.id,queued.version))
        run.assert_not_called()

    def test_real_saver_graph_roundtrip(self):
        from langgraph.graph import StateGraph, START, END
        from typing import TypedDict
        class State(TypedDict): value:int
        graph=StateGraph(State); graph.add_node('step',lambda s:{'value':s['value']+1})
        graph.add_edge(START,'step'); graph.add_edge('step',END)
        compiled=graph.compile(checkpointer=self.manager.phase4.checkpointer)
        config={'configurable':{'thread_id':'roundtrip'}}
        self.assertEqual(compiled.invoke({'value':1},config)['value'],2)
        self.assertEqual(compiled.get_state(config).values['value'],2)

    def test_20_concurrent_pool_operations(self):
        async def one():
            async with self.manager.database.sessions() as session:
                return await session.scalar(text('SELECT 1'))
        async def run(): return await asyncio.gather(*(one() for _ in range(20)))
        self.assertEqual(self.run_async(run()),[1]*20)

    def test_status_version_prevents_stale_save(self):
        stale=self.manager.phase3.tasks.get(self.task.id)
        self.run_async(self.manager.task_queue.enqueue(self.task.id))
        with self.assertRaises(TaskConflict):
            with self.manager.phase3.tasks.connect() as db: self.manager.phase3.tasks._save(db,stale)

    def test_health(self):
        self.assertEqual(self.run_async(self.manager.database.health()),'ok')
        self.assertEqual(self.run_async(self.manager.task_queue.health())['queue'],'ok')

    def test_event_timestamp_default_evaluated_per_transaction(self):
        with self.manager.database.sync_engine.connect() as db:
            first = db.scalar(select(func.max(events.c.created_at)))
        time.sleep(.02)
        self.manager.phase3.tasks.set_plan(self.task.id, {'steps':[{'description':'Read'}]})
        with self.manager.database.sync_engine.connect() as db:
            last = db.scalar(select(func.max(events.c.created_at)))
        self.assertGreater(last, first)
        self.assertIsNotNone(last.tzinfo)

    def test_migration_dry_run_does_not_write(self):
        # Source IDs match existing fixture only to prove dry-run does no writes.
        source=self.root/'source.sqlite3'; workspace=WorkspaceStore(source).create('Source')
        sessions=SessionStore(source); sessions.create(session_id='s',file_id='',file_name='',pdf_path='',chroma_dir='',workspace_id=workspace.id)
        store=TaskStore(source); task=store.create(Task(workspace_id=workspace.id,session_id='s',goal='Source fixture'))
        before=source.read_bytes()
        report=migrate([source],self.url,dry_run=True)
        self.assertEqual(report['outcome'],'DRY RUN VALIDATED'); self.assertEqual(before,source.read_bytes())
        self.assertEqual(len(self.manager.phase3.tasks.list()),1)

    def test_migration_non_empty_and_active_abort(self):
        source=self.root/'source.sqlite3'; workspace=WorkspaceStore(source).create('Source')
        SessionStore(source).create(session_id='s',file_id='',file_name='',pdf_path='',chroma_dir='',workspace_id=workspace.id)
        store=TaskStore(source); task=store.create(Task(workspace_id=workspace.id,session_id='s',goal='Source fixture'))
        with self.assertRaises(MigrationBlocked): migrate([source],self.url)
        store.transition(task.id,'RUNNING'); store.transition(task.id,'PAUSED')
        with self.assertRaises(MigrationBlocked): migrate([source],self.url,allow_non_empty=True)

    def test_migration_preserves_ids_json_utc_counts(self):
        source=self.root/'source.sqlite3'; workspace=WorkspaceStore(source).create('Source')
        SessionStore(source).create(session_id='s',file_id='',file_name='',pdf_path='',chroma_dir='',workspace_id=workspace.id)
        store=TaskStore(source); task=store.create(Task(workspace_id=workspace.id,session_id='s',goal='Source fixture'))
        store.set_plan(task.id,{'steps':[{'description':'Read fixture'}]})
        MemoryStore(source,{}).create(LongTermMemory(content='Use concise tables',memory_type='instruction',scope_type='workspace',scope_id=workspace.id))
        from evaluation.phase5b1.migration_fixture import add_approval_and_delegation
        ids = add_approval_and_delegation(source, task, self.manager.phase5.registry,
                                          self.manager.phase4.policy.secrets)
        report=migrate([source],self.url,allow_non_empty=True)
        self.assertEqual(report['outcome'],'VERIFIED')
        self.assertTrue(all(r['verified']=='PASS' for r in report['tables'].values()))
        self.assertEqual(self.manager.phase3.tasks.get(task.id).id,task.id)
        self.assertEqual(self.manager.phase3.tasks.steps(task.id)[0].description,'Read fixture')
        self.assertEqual(self.manager.phase4.actions.approval(ids['approval'])['id'], ids['approval'])
        self.assertEqual(self.manager.phase5.store.run(ids['supervisor'])['id'], ids['supervisor'])
        self.assertEqual(self.manager.phase5.store.delegation(ids['delegation'])['id'], ids['delegation'])
        with self.assertRaises(MigrationBlocked): migrate([source],self.url,allow_non_empty=True)
        self.assertEqual(len(self.manager.phase3.tasks.list()),2)


if __name__=='__main__': unittest.main()
