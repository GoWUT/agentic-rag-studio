"""Opt-in real PostgreSQL concurrency, upgrade and Worker authorization tests."""
import asyncio
from contextvars import copy_context
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import secrets
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import uuid4
from fastapi import HTTPException
from sqlalchemy import create_engine,text
from sqlalchemy.pool import NullPool
from server.config import CONFIG
from server.db.runtime import driver_url,REVISION
from server.db.schema import metadata
from server.sessions import AgentSessionManager
from server.auth.context import principal_scope
from server.runtime_services import configure_runtime,shutdown_runtime
from server.worker_runtime import execute_task

URL=os.getenv('PHASE5B1_TEST_DATABASE_URL')


def schema_url():
    schema='security_'+uuid4().hex
    url=driver_url(URL)
    engine=create_engine(url,poolclass=NullPool)
    with engine.begin() as db:db.execute(text('CREATE SCHEMA '+schema))
    engine.dispose()
    return url.update_query_dict({'options':'-csearch_path='+schema}).render_as_string(hide_password=False)


@unittest.skipUnless(URL,'real PostgreSQL test URL not configured')
class PostgresSecurityTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.url=schema_url(); engine=create_engine(self.url,poolclass=NullPool)
        with engine.begin() as db:
            metadata.create_all(db)
            db.execute(text('CREATE TABLE alembic_version (version_num VARCHAR(32) PRIMARY KEY)'))
            db.execute(text('INSERT INTO alembic_version VALUES (:v)'),{'v':REVISION})
        engine.dispose()
        config={**CONFIG,'WORKSPACE_DIR':self.temp.name,'DATABASE_BACKEND':'postgresql','DATABASE_URL':self.url,
            'TASK_EXECUTION_MODE':'worker','TASK_QUEUE_ENABLED':True,'AUTH_ENABLED':True,
            'AUTH_JWT_SECRET':secrets.token_urlsafe(48),'AUTH_JWT_ISSUER':'fixture','AUTH_JWT_AUDIENCE':'fixture',
            'MCP_SERVERS_FILE':'','GITHUB_MCP_ENABLED':False,'MEMORY_ENABLED':False,'OTEL_ENABLED':False}
        self.manager=AgentSessionManager(config); configure_runtime(self.manager)
        self.runner=asyncio.Runner(loop_factory=asyncio.SelectorEventLoop); self.addCleanup(self.close)
        self.runner.run(self.manager.task_queue.open()); self.runner.run(self.manager.task_queue.app.schema_manager.apply_schema_async())
        self.alice=self.manager.auth.register('alice@example.test','long secure fixture password')
        self.bob=self.manager.auth.register('bob@example.test','long secure fixture password')
        self.a=self.manager.auth.principal_for_user(self.alice['id']); self.b=self.manager.auth.principal_for_user(self.bob['id'])
        with principal_scope(self.a):
            self.workspace=self.manager.workspaces.create('Private fixture')
            self.task=self.manager.phase3.task_service.create(self.workspace.id,'Read fixture')

    def close(self):
        self.runner.run(shutdown_runtime(self.manager)); self.runner.close()

    def add(self,role='EDITOR'):
        self.manager.security.change_member(self.workspace.id,email=self.bob['email'],role=role,add=True,principal=self.a)
    def bob_task(self):
        with principal_scope(self.b):return self.manager.phase3.task_service.create(self.workspace.id,'Read fixture')
    def enqueue(self,task,actor):
        with principal_scope(actor):return self.runner.run(self.manager.task_queue.enqueue(task.id),context=copy_context())
    def deliver(self,task):
        context=SimpleNamespace(job=SimpleNamespace(id=task.queue_job_id),worker_name='fixture-worker')
        return self.runner.run(execute_task(self.manager.task_queue,context,task.id,task.metadata['queued_version']))

    def test_transaction_creates_workspace_owner_and_audit(self):
        with self.manager.auth.store.connect() as db:
            self.assertEqual(db.execute('SELECT created_by_user_id FROM workspaces WHERE id=?',(self.workspace.id,)).fetchone()[0],self.a.user_id)
            self.assertEqual(db.execute('SELECT role FROM workspace_memberships WHERE workspace_id=?',(self.workspace.id,)).fetchone()[0],'OWNER')
            self.assertTrue(db.execute("SELECT 1 FROM audit_events WHERE event_type='WORKSPACE_CREATED'").fetchone())
    def test_analysis_receipt_precedes_real_artifact_registration(self):
        from io import BytesIO
        from server.analysis_runtime import DataAnalysisRequest
        with principal_scope(self.a):
            asset=self.manager.phase3.data.upload(self.workspace.id,'fixture.csv',BytesIO(b'x\n1\n'),'text/csv')
            result=self.manager.phase3.analysis.run(self.workspace.id,DataAnalysisRequest(dataset_ids=[asset.id],objective='Fixture receipt',code="write_artifact('fixture.txt','synthetic fixture')"))
            self.assertEqual(result.status,'completed',result.error_type); self.assertEqual(len(result.artifacts),1)
        with self.manager.database.sync_engine.connect() as db:
            self.assertEqual(db.scalar(text('SELECT count(*) FROM analysis_executions')),1)
            self.assertEqual(db.scalar(text('SELECT count(*) FROM artifacts')),1)
    def test_workspace_read_uses_one_membership_query(self):
        from sqlalchemy import event
        queries=[]
        def capture(connection,cursor,statement,parameters,context,many):
            if 'workspace_memberships' in statement:queries.append(statement)
        event.listen(self.manager.database.sync_engine,'before_cursor_execute',capture)
        try:
            with principal_scope(self.a):self.manager.workspaces.get(self.workspace.id)
        finally:event.remove(self.manager.database.sync_engine,'before_cursor_execute',capture)
        self.assertEqual(len(queries),1)
    def test_task_list_batches_membership_lookup_and_obeys_current_role(self):
        from sqlalchemy import event
        self.add('EDITOR')
        with principal_scope(self.a):
            for _ in range(12):self.manager.phase3.task_service.create(self.workspace.id,'List fixture')
        queries=[]
        def capture(connection,cursor,statement,parameters,context,many):
            if 'workspace_memberships' in statement:queries.append(statement)
        event.listen(self.manager.database.sync_engine,'before_cursor_execute',capture)
        try:
            with principal_scope(self.b):self.assertEqual(len(self.manager.phase3.tasks.list(workspace_id=self.workspace.id)),13)
            self.assertEqual(len(queries),1)
            self.manager.security.change_member(self.workspace.id,user_id=self.b.user_id,role='VIEWER',principal=self.a)
            with principal_scope(self.b):self.assertEqual(self.manager.phase3.tasks.list(workspace_id=self.workspace.id),[])
        finally:event.remove(self.manager.database.sync_engine,'before_cursor_execute',capture)
    def test_concurrent_refresh_rotates_once_and_replay_revokes_family(self):
        old=self.manager.auth.login(self.alice['email'],'long secure fixture password')['refresh_token']
        def refresh():
            try:return self.manager.auth.refresh(old)
            except HTTPException as error:return error.status_code
        with ThreadPoolExecutor(2) as pool:results=list(pool.map(lambda _:refresh(),range(2)))
        self.assertEqual(sum(isinstance(r,dict) for r in results),1); self.assertIn(401,results)
        new=next(r for r in results if isinstance(r,dict))['refresh_token']
        with self.assertRaises(HTTPException):self.manager.auth.refresh(new)
        with self.manager.auth.store.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM refresh_tokens WHERE revoked_at IS NULL').fetchone()[0],0)
    def test_concurrent_last_owner_demotion_retains_one(self):
        self.add('OWNER')
        def demote(actor):
            try:return self.manager.security.change_member(self.workspace.id,user_id=actor.user_id,role='EDITOR',principal=actor)
            except HTTPException as error:return error.status_code
        with ThreadPoolExecutor(2) as pool:results=list(pool.map(demote,[self.a,self.b]))
        self.assertEqual(sum(isinstance(r,dict) for r in results),1)
        with self.manager.auth.store.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM workspace_memberships WHERE workspace_id=? AND role='OWNER'",(self.workspace.id,)).fetchone()[0],1)
    def test_worker_removed_member_never_calls_graph(self):
        self.add(); queued=self.enqueue(self.bob_task(),self.b)
        self.manager.security.change_member(self.workspace.id,user_id=self.b.user_id,remove=True,principal=self.a)
        with patch.object(self.manager.phase3.task_service,'run') as graph:self.deliver(queued); graph.assert_not_called()
        with self.manager.database.sync_engine.connect() as db:
            value=db.scalar(text('SELECT payload FROM tasks WHERE id=:id'),{'id':queued.id})
        self.assertEqual(value['status'],'FAILED'); self.assertEqual(value['attempt_count'],0)
        self.assertIn('Authorization denied',value['last_error'])
    def test_worker_disabled_actor_never_calls_graph(self):
        queued=self.enqueue(self.task,self.a)
        with self.manager.auth.store.connect() as db:db.execute("UPDATE users SET status='DISABLED' WHERE id=?",(self.a.user_id,))
        with patch.object(self.manager.phase3.task_service,'run') as graph:self.deliver(queued); graph.assert_not_called()
    def test_enqueue_requires_membership_even_with_valid_principal(self):
        with principal_scope(self.b):
            with self.assertRaises(HTTPException):self.runner.run(self.manager.task_queue.enqueue(self.task.id))
    def test_previously_approved_write_rechecks_live_role_before_call(self):
        from server.tool_registry import ToolDescriptor
        self.add('OWNER'); task=self.bob_task()
        descriptor=ToolDescriptor(name='mcp.fixture.write',provider='fixture',provider_type='mcp',capability='github.issue.create',
            operation_type='write',risk_level='high',input_schema={'type':'object'})
        with principal_scope(self.b):
            self.manager.phase3.tasks.set_plan(task.id,{'steps':[{'description':'Write fixture'}]})
            step=self.manager.phase3.tasks.steps(task.id)[0]
            state={'task_id':task.id,'task_step_id':step.id,'session_id':task.session_id,'workspace_id':self.workspace.id,'run_id':'fixture'}
            execution=self.manager.phase4.actions.request(descriptor,{},state,SimpleNamespace(requires_approval=True,reason='fixture'))
            self.manager.phase4.actions.decide(execution['approval_request_id'],'APPROVED',policy=SimpleNamespace(evaluate=lambda *args:None))
        self.manager.security.change_member(self.workspace.id,user_id=self.b.user_id,role='EDITOR',principal=self.a)
        registry=SimpleNamespace(call_tool=unittest.mock.AsyncMock())
        with principal_scope(self.b):
            with self.assertRaises(HTTPException):self.manager.phase4.execute(registry,descriptor,{},state)
        registry.call_tool.assert_not_called()
        with self.manager.auth.store.connect() as db:
            self.assertEqual(db.execute('SELECT status FROM tool_executions WHERE id=?',(execution['id'],)).fetchone()[0],'WAITING_APPROVAL')
    def test_session_task_creator_foreign_keys_and_private_session(self):
        self.add('EDITOR')
        with principal_scope(self.b):
            self.assertEqual(self.manager.phase3.tasks.get(self.task.id).id,self.task.id)
            with self.assertRaises(HTTPException):self.manager.store.get(self.task.session_id)
        with self.manager.auth.store.connect() as db:
            self.assertEqual(db.execute('SELECT created_by_user_id FROM sessions WHERE session_id=?',(self.task.session_id,)).fetchone()[0],self.a.user_id)
    def test_legacy_assignment_dry_run_and_explicit_apply(self):
        from server.manage import assign_legacy
        legacy=self.manager.workspaces.repository.create('Unclaimed fixture')
        report=assign_legacy(self.manager,self.alice['email'],True); self.assertEqual(report['workspaces'],1)
        self.assertIsNone(self.manager.security.role(self.a,legacy.id))
        report=assign_legacy(self.manager,self.alice['email'],False); self.assertEqual(report['owner_memberships'],1)
        self.assertEqual(self.manager.security.role(self.a,legacy.id),'OWNER')


@unittest.skipUnless(URL,'real PostgreSQL test URL not configured')
class MigrationTests(unittest.TestCase):
    def test_populated_phase5b1_upgrade_preserves_business_rows_and_ids(self):
        from alembic.config import Config
        from alembic import command
        url=schema_url(); config=Config('alembic.ini')
        with patch.dict(CONFIG,{'DATABASE_URL':url}):command.upgrade(config,'5b1_0001')
        engine=create_engine(url,poolclass=NullPool); identifier=str(uuid4())
        with engine.begin() as db:
            db.execute(text("INSERT INTO workspaces (id,name,description,created_at,updated_at) VALUES (:id,'Legacy preserved','fixture',now(),now())"),{'id':identifier})
        from server.db.runtime import PostgresRuntime
        from server.sessions import SessionStore
        from server.tasks import TaskStore,Task
        runtime=PostgresRuntime({**CONFIG,'DATABASE_URL':url})
        session=SessionStore(runtime).create(session_id=str(uuid4()),file_id='',file_name='Legacy fixture',pdf_path='',chroma_dir='',embedding_model='fixture',workspace_id=identifier)
        task=TaskStore(runtime).create(Task(workspace_id=identifier,session_id=session.session_id,goal='Preserved legacy goal'))
        with engine.connect() as db:before=db.scalar(text('SELECT payload FROM tasks WHERE id=:id'),{'id':task.id})
        with patch.dict(CONFIG,{'DATABASE_URL':url}):command.upgrade(config,'head')
        with engine.connect() as db:
            row=db.execute(text('SELECT id,name,created_by_user_id FROM workspaces')).one()
            self.assertEqual(tuple(row),(identifier,'Legacy preserved',None))
            self.assertEqual(db.scalar(text('SELECT count(*) FROM users')),0)
            self.assertEqual(db.scalar(text('SELECT count(*) FROM workspace_memberships')),0)
            self.assertEqual(db.scalar(text('SELECT version_num FROM alembic_version')),REVISION)
            self.assertEqual(db.scalar(text('SELECT session_id FROM sessions')),session.session_id)
            self.assertEqual(db.scalar(text('SELECT payload FROM tasks WHERE id=:id'),{'id':task.id}),before)
        # Only this disposable schema with no accounts: reverse auth DDL and re-upgrade.
        with patch.dict(CONFIG,{'DATABASE_URL':url}):
            command.downgrade(config,'5b1_0001'); command.upgrade(config,'head')
        with engine.connect() as db:self.assertEqual(db.scalar(text('SELECT id FROM workspaces')),identifier)
        engine.dispose()
        runtime.sync_engine.dispose()


if __name__=='__main__':unittest.main()
