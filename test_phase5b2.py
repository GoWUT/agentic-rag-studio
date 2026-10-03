"""Real local account/API policy, privacy and ownership boundary tests."""
from datetime import datetime,timedelta,timezone
from io import BytesIO
import importlib.util
import json
import os
from pathlib import Path
import secrets
import sys
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4
import jwt
from fastapi import HTTPException
from fastapi.testclient import TestClient
from server.config import CONFIG,load_config
from server.auth.context import principal_scope
from server.auth.authorization import ROLE_MATRIX
from server.workspaces import DocumentRecord
from server.data_assets import Artifact
from server.persistence import now
from server.tool_registry import ToolDescriptor


class SecurityTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.config={**CONFIG,'WORKSPACE_DIR':self.temp.name,'DATABASE_BACKEND':'sqlite','DATABASE_URL':'',
            'TASK_EXECUTION_MODE':'inline','AUTH_ENABLED':True,'AUTH_JWT_SECRET':secrets.token_urlsafe(48),
            'AUTH_JWT_ISSUER':'fixture','AUTH_JWT_AUDIENCE':'fixture-api','AUTH_MAX_FAILED_LOGIN_ATTEMPTS':3,
            'AUTH_ALLOW_REGISTRATION':True,'MCP_SERVERS_FILE':'','GITHUB_MCP_ENABLED':False,
            'OTEL_ENABLED':False,'MEMORY_ENABLED':True,'MEMORY_AUTO_EXTRACT':False}
        name='phase5b2_app_'+uuid4().hex
        spec=importlib.util.spec_from_file_location(name,Path(__file__).parent/'server/main.py')
        module=importlib.util.module_from_spec(spec); sys.modules[name]=module
        self.addCleanup(lambda:sys.modules.pop(name,None))
        with patch('server.config.CONFIG',self.config):spec.loader.exec_module(module)
        self.manager=module.SESSION_MANAGER
        self.client=TestClient(module.app); self.client.__enter__(); self.addCleanup(self.client.__exit__,None,None,None)
        self.alice=self.register('alice@example.test'); self.bob=self.register('bob@example.test')
        self.alice_tokens=self.login(self.alice['email']); self.bob_tokens=self.login(self.bob['email'])
        self.a={'Authorization':'Bearer '+self.alice_tokens['access_token']}; self.b={'Authorization':'Bearer '+self.bob_tokens['access_token']}
        self.workspace=self.client.post('/workspaces',json={'name':'Private Alice workspace'},headers=self.a).json()
        self.task=self.client.post('/tasks',json={'workspace_id':self.workspace['id'],'goal':'Read fixture'},headers=self.a).json()

    def register(self,email):
        response=self.client.post('/auth/register',json={'email':email,'password':'safe lengthy fixture password','display_name':email.split('@')[0]})
        self.assertEqual(response.status_code,201,response.text); return response.json()
    def login(self,email):
        response=self.client.post('/auth/login',json={'email':email,'password':'safe lengthy fixture password'})
        self.assertEqual(response.status_code,200,response.text); return response.json()
    def add_bob(self,role='VIEWER'):
        response=self.client.post(f"/workspaces/{self.workspace['id']}/members",json={'email':self.bob['email'],'role':role},headers=self.a)
        self.assertEqual(response.status_code,201,response.text)
    def change_bob(self,role):
        return self.client.patch(f"/workspaces/{self.workspace['id']}/members/{self.bob['id']}",json={'role':role},headers=self.a)
    def actor(self,user):return self.manager.auth.principal_for_user(user['id'])

    def test_argon2_hash_and_no_password_response(self):
        user=self.manager.auth.store.principal_user(self.alice['id'])
        self.assertTrue(user['password_hash'].startswith('$argon2id$'))
        self.assertNotIn('password_hash',self.alice)
        self.assertTrue(self.manager.auth.passwords.verify_password('safe lengthy fixture password',user['password_hash']))
    def test_email_normalization_duplicate(self):
        response=self.client.post('/auth/register',json={'email':' ALICE@EXAMPLE.TEST ','password':'safe lengthy fixture password'})
        self.assertEqual(response.status_code,409)
    def test_short_password_rejected(self):
        self.assertEqual(self.client.post('/auth/register',json={'email':'new@example.test','password':'short'}).status_code,422)
    def test_login_unknown_and_wrong_password_same_error(self):
        responses=[self.client.post('/auth/login',json={'email':email,'password':'wrong password value'}) for email in (self.alice['email'],'missing@example.test')]
        self.assertEqual(responses[0].json(),responses[1].json()); self.assertEqual(responses[0].status_code,401)
    def test_lockout_and_correct_password_still_rejected(self):
        for _ in range(3):self.client.post('/auth/login',json={'email':self.alice['email'],'password':'wrong password value'})
        self.assertEqual(self.client.post('/auth/login',json={'email':self.alice['email'],'password':'safe lengthy fixture password'}).status_code,401)
        self.assertIsNotNone(self.manager.auth.store.principal_user(self.alice['id'])['locked_until'])
    def test_disabled_existing_access_token_denied(self):
        with self.manager.auth.store.connect() as db:db.execute('UPDATE users SET status=? WHERE id=?',('DISABLED',self.alice['id']))
        self.assertEqual(self.client.get('/auth/me',headers=self.a).status_code,401)
        self.assertEqual(self.client.post('/auth/refresh',json={'refresh_token':self.alice_tokens['refresh_token']}).status_code,401)
    def test_jwt_claim_validation(self):
        auth=self.manager.auth; valid=jwt.decode(self.alice_tokens['access_token'],self.config['AUTH_JWT_SECRET'],algorithms=['HS256'],audience='fixture-api')
        cases=[{'exp':int((datetime.now(timezone.utc)-timedelta(seconds=5)).timestamp())},
            {'iss':'other'},{'aud':'other'},{'type':'refresh'},{'sub':123},{'iat':int((datetime.now(timezone.utc)+timedelta(hours=1)).timestamp())}]
        for changed in cases:
            with self.subTest(changed=changed):
                token=jwt.encode({**valid,**changed},self.config['AUTH_JWT_SECRET'],algorithm='HS256')
                self.assertEqual(self.client.get('/auth/me',headers={'Authorization':'Bearer '+token}).status_code,401)
        for algorithm,key in [('HS256',secrets.token_urlsafe(48)),('HS512',self.config['AUTH_JWT_SECRET'])]:
            token=jwt.encode(valid,key,algorithm=algorithm)
            self.assertEqual(self.client.get('/auth/me',headers={'Authorization':'Bearer '+token}).status_code,401)
        del valid['jti']; token=jwt.encode(valid,self.config['AUTH_JWT_SECRET'],algorithm='HS256')
        self.assertEqual(self.client.get('/auth/me',headers={'Authorization':'Bearer '+token}).status_code,401)
    def test_missing_bearer_all_business_routes(self):
        for path in ('/sessions','/workspaces','/tasks','/memories','/approvals','/tools','/agents'):
            with self.subTest(path=path):self.assertEqual(self.client.get(path).status_code,401)
    def test_refresh_rotation_replay_revokes_family(self):
        old=self.alice_tokens['refresh_token']; new=self.client.post('/auth/refresh',json={'refresh_token':old})
        self.assertEqual(new.status_code,200); replacement=new.json()['refresh_token']; self.assertNotEqual(old,replacement)
        self.assertEqual(self.client.post('/auth/refresh',json={'refresh_token':old}).status_code,401)
        self.assertEqual(self.client.post('/auth/refresh',json={'refresh_token':replacement}).status_code,401)
        with self.manager.auth.store.connect() as db:
            stored=[r[0] for r in db.execute('SELECT token_hash FROM refresh_tokens')]
            self.assertNotIn(old,stored); self.assertNotIn(replacement,stored)
            self.assertTrue(db.execute("SELECT 1 FROM audit_events WHERE event_type='TOKEN_REPLAY_DETECTED'").fetchone())
    def test_logout_and_logout_all(self):
        self.assertEqual(self.client.post('/auth/logout',json={'refresh_token':self.alice_tokens['refresh_token']}).status_code,204)
        self.assertEqual(self.client.post('/auth/refresh',json={'refresh_token':self.alice_tokens['refresh_token']}).status_code,401)
        other=self.login(self.alice['email']); self.assertEqual(self.client.post('/auth/logout-all',headers=self.a).status_code,204)
        self.assertEqual(self.client.post('/auth/refresh',json={'refresh_token':other['refresh_token']}).status_code,401)
    def test_password_change_revokes_refresh(self):
        response=self.client.post('/auth/change-password',json={'old_password':'safe lengthy fixture password','new_password':'new lengthy fixture password'},headers=self.a)
        self.assertEqual(response.status_code,204)
        self.assertEqual(self.client.post('/auth/refresh',json={'refresh_token':self.alice_tokens['refresh_token']}).status_code,401)
        self.assertEqual(self.client.post('/auth/login',json={'email':self.alice['email'],'password':'new lengthy fixture password'}).status_code,200)
    def test_workspace_membership_and_creator_atomic(self):
        with self.manager.auth.store.connect() as db:
            row=db.execute('SELECT created_by_user_id FROM workspaces WHERE id=?',(self.workspace['id'],)).fetchone()
        self.assertEqual(row[0],self.alice['id'])
        self.assertEqual(self.manager.security.role(self.actor(self.alice),self.workspace['id']),'OWNER')
        self.assertEqual(self.task['created_by_user_id'],self.alice['id'])
    def test_workspace_create_failure_rolls_back_membership(self):
        before=self.client.get('/workspaces',headers=self.a).json()
        store=self.manager.auth.store
        original=store.audit
        def fail(db,kind,*args,**kwargs):
            if kind=='WORKSPACE_CREATED':raise RuntimeError('fixture audit failure')
            return original(db,kind,*args,**kwargs)
        with principal_scope(self.actor(self.alice)),patch.object(store,'audit',side_effect=fail):
            with self.assertRaises(RuntimeError):self.manager.workspaces.create('Rolled back')
        self.assertEqual(self.client.get('/workspaces',headers=self.a).json(),before)
    def test_idor_workspace_task_session_agents(self):
        for path in (f"/workspaces/{self.workspace['id']}",f"/tasks/{self.task['id']}",
            f"/sessions/{self.task['session_id']}/messages",f"/tasks/{self.task['id']}/events",
            f"/tasks/{self.task['id']}/agents",f"/tasks/{self.task['id']}/delegations"):
            with self.subTest(path=path):self.assertIn(self.client.get(path,headers=self.b).status_code,(403,404))
        self.assertEqual(self.client.get('/tasks',headers=self.b).json(),[])
        self.assertEqual(self.client.get('/sessions',headers=self.b).json()['sessions'],[])
    def test_idor_document_dataset_artifact_memory(self):
        manager=self.manager
        with principal_scope(self.actor(self.alice)):
            asset=manager.phase3.data.upload(self.workspace['id'],'fixture.csv',BytesIO(b'x,y\n1,2\n'))
            document=DocumentRecord(id=uuid4().hex,workspace_id=self.workspace['id'],filename='fixture.pdf',display_name='fixture',
                fingerprint='fixture-fingerprint',index_id='fixture-index',status='ready',page_count=1,created_at=now())
            manager.workspaces.register(document)
            manager.phase3.data.save_execution({'execution_id':'analysis-fixture','workspace_id':self.workspace['id'],'task_id':self.task['id']})
            path=manager.phase3.data.root/'result.txt'; path.write_text('private artifact')
            artifact=Artifact(workspace_id=self.workspace['id'],task_id=self.task['id'],execution_id='analysis-fixture',
                filename='result.txt',artifact_type='text',mime_type='text/plain',file_path=str(path),size_bytes=16)
            manager.phase3.data.register_artifact(artifact)
        memory=self.client.post('/memories',json={'content':'Private Alice preference'},headers=self.a).json()
        for route in (f"/workspaces/{self.workspace['id']}/documents",f"/workspaces/{self.workspace['id']}/datasets/{asset.id}",
                      f'/artifacts/{artifact.id}',f"/memories/{memory['id']}"):
            self.assertIn(self.client.get(route,headers=self.b).status_code,(403,404))
        self.assertEqual(self.client.get('/memories',headers=self.b).json(),[])
    def test_role_change_without_new_login_and_last_owner(self):
        self.add_bob()
        self.assertEqual(self.client.get(f"/workspaces/{self.workspace['id']}",headers=self.b).status_code,200)
        upload=lambda:self.client.post(f"/workspaces/{self.workspace['id']}/datasets",files={'file':('fixture.csv',b'x\n1\n','text/csv')},headers=self.b)
        self.assertEqual(upload().status_code,403)
        self.assertEqual(self.change_bob('EDITOR').status_code,200); self.assertEqual(upload().status_code,200)
        self.assertEqual(self.change_bob('VIEWER').status_code,200); self.assertEqual(upload().status_code,403)
        self.assertEqual(self.client.delete(f"/workspaces/{self.workspace['id']}/members/{self.alice['id']}",headers=self.a).status_code,409)
        self.assertEqual(self.client.patch(f"/workspaces/{self.workspace['id']}/members/{self.alice['id']}",json={'role':'EDITOR'},headers=self.a).status_code,409)
        self.assertEqual(self.client.post(f"/workspaces/{self.workspace['id']}/members",json={'email':self.alice['email'],'role':'OWNER'},headers=self.b).status_code,403)
    def test_shared_workspace_sessions_remain_private(self):
        self.add_bob('EDITOR')
        self.assertEqual(self.client.get(f"/sessions/{self.task['session_id']}/messages",headers=self.b).status_code,403)
        own=self.client.post(f"/workspaces/{self.workspace['id']}/sessions",headers=self.b)
        self.assertEqual(own.status_code,200)
        self.assertEqual(self.client.get('/sessions',headers=self.b).json()['sessions'][0]['session_id'],own.json()['session_id'])
    def test_editor_cannot_cancel_another_task(self):
        self.add_bob('EDITOR')
        self.assertEqual(self.client.get(f"/tasks/{self.task['id']}",headers=self.b).status_code,200)
        self.assertEqual(self.client.post(f"/tasks/{self.task['id']}/cancel",headers=self.b).status_code,403)
    def test_viewer_own_task_only_and_no_analysis(self):
        self.add_bob('VIEWER')
        self.assertEqual(self.client.get(f"/tasks/{self.task['id']}",headers=self.b).status_code,403)
        own=self.client.post('/tasks',json={'workspace_id':self.workspace['id'],'goal':'Read-only research'},headers=self.b)
        self.assertEqual(own.status_code,200)
        self.assertEqual(self.client.get('/tasks',headers=self.b).json()[0]['id'],own.json()['id'])
        with principal_scope(self.actor(self.bob)):
            with self.assertRaises(HTTPException):self.manager.security.authorize(self.actor(self.bob),'analysis.execute',self.workspace['id'])
    def test_role_matrix(self):
        self.add_bob()
        expected={'workspace.members.manage':{'OWNER'},'external.write':{'OWNER'},
            'dataset.create':{'OWNER','EDITOR'},'document.delete':{'OWNER','EDITOR'},'analysis.execute':{'OWNER','EDITOR'},
            'workspace.read':{'OWNER','EDITOR','VIEWER'},'external.read':{'OWNER','EDITOR','VIEWER'}}
        for role in ('OWNER','EDITOR','VIEWER'):
            self.change_bob(role)
            for action,roles in expected.items():
                with self.subTest(role=role,action=action):self.assertEqual(self.manager.security.can(self.actor(self.bob),action,self.workspace['id']),role in roles)
    def test_editor_write_denied_before_approval(self):
        self.add_bob('EDITOR')
        task=self.client.post('/tasks',json={'workspace_id':self.workspace['id'],'goal':'Propose issue'},headers=self.b).json()
        descriptor=ToolDescriptor(name='mcp.fixture.create_issue',provider='fixture',provider_type='mcp',
            capability='github.issue.create',operation_type='write',risk_level='high',input_schema={'type':'object'})
        with principal_scope(self.actor(self.bob)):
            with self.assertRaises(HTTPException) as error:self.manager.phase4.execute(None,descriptor,{},
                {'task_id':task['id'],'workspace_id':self.workspace['id'],'session_id':task['session_id']})
        self.assertEqual(error.exception.status_code,403)
        with self.manager.auth.store.connect() as db:self.assertEqual(db.execute('SELECT count(*) FROM approval_requests').fetchone()[0],0)
    def test_service_calls_require_principal(self):
        with self.assertRaises(HTTPException):self.manager.phase3.task_service.create(self.workspace['id'],'No principal')
        task=self.manager.phase3.task_service.create(self.workspace['id'],'Explicit principal',principal=self.actor(self.alice))
        self.assertEqual(task.created_by_user_id,self.alice['id'])
    def test_viewer_pdf_is_denied_before_ingestion_or_filesystem_work(self):
        self.add_bob('VIEWER')
        with patch.object(self.manager.ingestion_pipeline,'ingest') as ingest:
            response=self.client.post(f"/workspaces/{self.workspace['id']}/documents",headers=self.b,
                files=[('files',('private.pdf',b'fixture','application/pdf'))])
            self.assertEqual(response.status_code,403); ingest.assert_not_called()
    def test_viewer_analysis_is_denied_before_subprocess_or_receipt(self):
        self.add_bob('VIEWER')
        with patch('server.analysis_runtime.subprocess.Popen') as execute:
            response=self.client.post(f"/workspaces/{self.workspace['id']}/analysis",headers=self.b,
                json={'dataset_ids':['fixture'],'objective':'Unauthorized analysis','code':'print(1)'})
            self.assertEqual(response.status_code,403); execute.assert_not_called()
        with self.manager.auth.store.connect() as db:self.assertEqual(db.execute('SELECT count(*) FROM analysis_executions').fetchone()[0],0)
    def test_invalid_jwt_is_counted_as_http_error(self):
        self.client.get('/auth/me',headers={'Authorization':'Bearer invalid'})
        from prometheus_client import generate_latest
        metrics=generate_latest(self.manager.telemetry.registry)
        self.assertIn(b'http_errors_total 1.0',metrics)
    def test_request_id_security_headers_and_validation_privacy(self):
        response=self.client.get('/auth/me',headers={**self.a,'X-Request-ID':'request-fixture'})
        self.assertEqual(response.headers['X-Request-ID'],'request-fixture'); self.assertEqual(response.headers['X-Content-Type-Options'],'nosniff')
        secret='password-that-is-way-too-long-'*20
        response=self.client.post('/auth/login',json={'email':self.alice['email'],'password':secret})
        self.assertEqual(response.status_code,422); self.assertNotIn(secret,response.text)
    def test_legacy_ownership_explicit_and_dry_run(self):
        from server.manage import assign_legacy
        raw=self.manager.workspaces.repository; legacy=raw.create('Unclaimed legacy')
        with self.manager.auth.store.connect() as db:before=db.execute('SELECT count(*) FROM workspace_memberships').fetchone()[0]
        report=assign_legacy(self.manager,self.alice['email'],True)
        self.assertEqual(report['workspaces'],1)
        with self.manager.auth.store.connect() as db:self.assertEqual(before,db.execute('SELECT count(*) FROM workspace_memberships').fetchone()[0])
        self.assertNotIn(legacy.id,{r['id'] for r in self.client.get('/workspaces',headers=self.a).json()})
        report=assign_legacy(self.manager,self.alice['email'],False)
        self.assertEqual(report['owner_memberships'],1); self.assertEqual(self.manager.security.role(self.actor(self.alice),legacy.id),'OWNER')
    def test_audit_metadata_does_not_store_secrets(self):
        with self.manager.auth.store.connect() as db:
            self.manager.auth.store.audit(db,'FIXTURE',actor=self.alice['id'],metadata={'password':'do-not-store','refresh_token':'do-not-store','action':'fixture'})
            values=' '.join(r[0] for r in db.execute('SELECT metadata FROM audit_events'))
        self.assertNotIn('do-not-store',values)


class GuardAndPrivacyTests(unittest.TestCase):
    def test_trace_export_drops_headers_sql_bodies_events_and_status_description(self):
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import SimpleSpanProcessor
        from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
        from opentelemetry.trace import Status,StatusCode
        from server.observability.runtime import PrivacyExporter
        destination=InMemorySpanExporter(); provider=TracerProvider(); provider.add_span_processor(SimpleSpanProcessor(PrivacyExporter(destination,{})))
        with provider.get_tracer('fixture').start_as_current_span('fixture') as span:
            span.set_attribute('task_id','safe-correlation-id'); span.set_attribute('db.statement','SELECT private password body')
            span.set_attribute('http.request.header.authorization','Bearer unsafe-fixture')
            span.add_event('exception',{'exception.message':'unsafe-fixture'}); span.set_status(Status(StatusCode.ERROR,'unsafe-fixture'))
        exported=destination.get_finished_spans()[0]
        self.assertEqual(dict(exported.attributes),{'task_id':'safe-correlation-id'}); self.assertFalse(exported.events)
        self.assertIsNone(exported.status.description); provider.shutdown()
    def test_execution_threads_propagate_actor_request_and_trace_without_cross_user_leak(self):
        from concurrent.futures import ThreadPoolExecutor
        from types import SimpleNamespace
        from server.agent.execution_harness import ExecutionHarness
        from server.auth.context import AuthenticatedPrincipal,principal_context,request_id_context
        harness=ExecutionHarness(max_graph_steps=10,max_attempts=1,retry_base_seconds=0,execution_timeout_seconds=3)
        def run(user):
            actor=AuthenticatedPrincipal(user); token=request_id_context.set('request-'+user)
            try:
                with principal_scope(actor):
                    agent=SimpleNamespace(invoke=lambda *args:{'actor':principal_context.get().user_id,'request':request_id_context.get()})
                    return harness.run(session_id=user,agent=agent,messages=[]).output
            finally:request_id_context.reset(token)
        with ThreadPoolExecutor(2) as pool:results=list(pool.map(run,['alice','bob']))
        self.assertEqual(results,[{'actor':'alice','request':'request-alice'},{'actor':'bob','request':'request-bob'}])
        harness._executor.shutdown()
    def test_docker_compose_has_non_root_persistent_volumes_and_fail_closed_migration_order(self):
        import yaml
        compose=yaml.safe_load((Path(__file__).parent/'compose.yaml').read_text())
        services=compose['services']
        for name in ('api','worker','streamlit','migrate'):
            self.assertEqual(services[name]['user'],'10001:10001'); self.assertTrue(services[name]['read_only'])
            self.assertIn('app_data:/data',services[name]['volumes'])
        for name in ('api','worker'):
            self.assertEqual(services[name]['depends_on']['migrate']['condition'],'service_completed_successfully')
        self.assertEqual(services['migrate']['restart'],'no'); self.assertTrue(services['migrate']['healthcheck']['disable'])
        self.assertNotIn('ports',services['postgres']); self.assertEqual(services['api']['environment']['AUTH_ENABLED'],'true')
        dockerfile=(Path(__file__).parent/'Dockerfile').read_text(); self.assertIn('uv sync --frozen',dockerfile)
        self.assertIn('USER 10001:10001',dockerfile); self.assertNotIn('COPY . ',dockerfile)
    def test_ci_config_has_postgres_security_migration_worker_and_docker_jobs(self):
        import yaml
        workflow=yaml.safe_load((Path(__file__).parent/'.github/workflows/ci.yml').read_text())
        self.assertTrue({'quality','unit','auth-rbac','postgres','docker-build'}.issubset(workflow['jobs']))
        self.assertEqual(set(workflow['jobs']['postgres']['strategy']['matrix']['suite']),{'integration','migration','worker'})
        self.assertEqual(workflow['permissions']['contents'],'read')
    def test_ui_rotates_once_and_restores_file_position_before_retry(self):
        from types import SimpleNamespace
        from client.auth_ui import APIRequests,API_BASE
        state={'_access_token':'old-access','_refresh_token':'old-refresh'}; stream=BytesIO(b'fixture')
        def first(*args,**kwargs):stream.read(); return SimpleNamespace(status_code=401)
        def second(*args,**kwargs):
            self.assertEqual(stream.tell(),0); self.assertEqual(kwargs['headers']['Authorization'],'Bearer new-access')
            return SimpleNamespace(status_code=200)
        rotated=SimpleNamespace(ok=True,json=lambda:{'access_token':'new-access','refresh_token':'new-refresh'})
        sequence=iter([first,lambda *args,**kwargs:rotated,second])
        with patch('client.auth_ui.st.session_state',state),patch('client.auth_ui.raw_requests.post',side_effect=lambda *args,**kwargs:next(sequence)(*args,**kwargs)) as post:
            response=APIRequests().post(API_BASE+'/workspaces/fixture/documents',files={'file':('fixture',stream,'application/pdf')})
        self.assertEqual(response.status_code,200); self.assertEqual(post.call_count,3); self.assertEqual(state['_refresh_token'],'new-refresh')
    def test_ui_failed_refresh_clears_tokens_workspace_and_history(self):
        from types import SimpleNamespace
        from client.auth_ui import APIRequests,API_BASE
        state={'_access_token':'access','_refresh_token':'refresh','workspace_id':'alice','chat':['private']}
        with patch('client.auth_ui.st.session_state',state),patch('client.auth_ui.raw_requests.get',return_value=SimpleNamespace(status_code=401)),patch('client.auth_ui.raw_requests.post',return_value=SimpleNamespace(ok=False)):
            APIRequests().get(API_BASE+'/auth/me')
        self.assertEqual(state,{})
    def test_ui_never_sends_token_to_other_origin(self):
        from types import SimpleNamespace
        from client.auth_ui import APIRequests
        with patch('client.auth_ui.st.session_state',{'_access_token':'private'}),patch('client.auth_ui.raw_requests.get',return_value=SimpleNamespace(status_code=200)) as get:
            APIRequests().get('https://other.example.test/fixture')
        self.assertNotIn('Authorization',get.call_args.kwargs['headers'])
    def test_production_auth_guard(self):
        with patch.dict(os.environ,{'APP_ENV':'production','AUTH_ENABLED':'false','ALLOW_INSECURE_PRODUCTION_AUTH':'false'}):
            with self.assertRaises(ValueError):load_config()
    def test_production_wildcard_guards_strip_comma_whitespace(self):
        for key in ('TRUSTED_HOSTS','CORS_ALLOWED_ORIGINS'):
            values={'APP_ENV':'production','AUTH_ENABLED':'true','AUTH_JWT_SECRET':secrets.token_urlsafe(48),
                    'TRUSTED_HOSTS':'localhost','CORS_ALLOWED_ORIGINS':'http://localhost'}
            values[key]='localhost, *'
            with self.subTest(key=key),patch.dict(os.environ,values):
                with self.assertRaises(ValueError):load_config()
    def test_missing_signing_secret_fails_closed(self):
        with patch.dict(os.environ,{'AUTH_ENABLED':'true','AUTH_JWT_SECRET':''}):
            with self.assertRaises(ValueError):load_config()
    def test_secret_redaction_and_metric_cardinality(self):
        from server.observability.runtime import SecretRedactor,Telemetry,LABELS
        redactor=SecretRedactor({})
        value=redactor.clean({'email':'alice@example.test','refresh_token':'opaque-private','Authorization':'Bearer secret-token','x':'postgresql://user:db-password@localhost/test'})
        self.assertNotIn('opaque-private',str(value)); self.assertNotIn('db-password',str(value)); self.assertNotIn('alice@',str(value))
        self.assertFalse(LABELS&{'user_id','workspace_id','task_id','session_id','email','query'})
        telemetry=Telemetry({'OTEL_ENABLED':False,'METRICS_ENABLED':True}); telemetry.record('auth_denied_total')
        from prometheus_client import generate_latest
        self.assertIn(b'auth_denied_total 1.0',generate_latest(telemetry.registry))


if __name__=='__main__':unittest.main()
