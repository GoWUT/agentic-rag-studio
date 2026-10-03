"""Phase 4 tests use real SDK protocol handling and real durable LangGraph interrupts."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, Mock

from fastapi import FastAPI
from fastapi.testclient import TestClient
from langchain_core.tools import StructuredTool
from jsonschema.exceptions import ValidationError as SchemaValidationError
from langgraph.types import Command
from mcp.server import MCPServer

from server.config import load_config
from server.sessions import AgentSessionManager
from server.mcp_client import MCPToolProvider, MCPServerConfig, MCPServerStore
from server.tool_registry import ToolRegistry, ToolDescriptor, ToolExecutionResult, NativeToolProvider, ToolUnavailable, validate_arguments
from server.tool_policy import ToolPolicy, SecretFilter
from server.tool_actions import ToolActionStore
from server.tasks import TaskConflict
from server.phase3_api import register_phase3_api
from server.phase4_api import register_phase4_api
from evaluation.phase4_mock_server import build_server
from test_phase2 import ScriptedModel


class Fixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = {**load_config(), 'WORKSPACE_DIR': str(self.root), 'MEMORY_ENABLED': False,
            'MEMORY_AUTO_EXTRACT': False, 'GITHUB_MCP_ENABLED': False, 'GITHUB_MCP_READ_ONLY': False,
            'GITHUB_ALLOWED_REPOSITORIES': 'fixture/test', 'GROUNDING_CHECK_ENABLED': False}
        self.manager = AgentSessionManager(self.config)
        self.workspace = self.manager.workspaces.create('Synthetic Phase 4')
        self.phase4 = self.manager.phase4
        self.ledger = self.root/'ledger.json'
        self.server = build_server(self.ledger)
        self.provider = MCPToolProvider(MCPServerConfig(id='github', url='https://api.githubcopilot.com/mcp/'),
            self.config, in_memory_server=self.server)
        self.phase4.registry.register_provider(self.provider)
        asyncio.run(self.phase4.registry.refresh())
        self.tool = self.phase4.registry.find_by_capability('github.issue.create')[0]
        self.arguments = {'owner': 'fixture', 'repo': 'test', 'title': 'Synthetic approval', 'body': 'Synthetic fixture only'}

    def script(self, capability='github.issue.create', arguments=None):
        ScriptedModel.answer = 'Synthetic task result.'
        ScriptedModel.script = {'QueryAnalysis': [{'standalone_query': 'Create synthetic test issue',
            'query_type': 'research', 'needs_retrieval': True, 'selected_sources': []}],
            'TaskPlan': [{'goal': 'Synthetic test', 'steps': [{'id': 'one', 'description': 'Requested action',
                'query': 'Synthetic issue', 'sources': [], 'preferred_capability': capability, 'arguments': arguments or self.arguments}]}]}

    def waiting(self):
        self.script()
        task = self.manager.phase3.task_service.create(self.workspace.id, 'Create synthetic test issue', 'external')
        with patch('server.agent.graph.ChatOpenAI', ScriptedModel):
            result = self.manager.phase3.task_service.run(task.id)
        self.assertEqual(result.status, 'WAITING_USER')
        approval = self.phase4.actions.approvals(task.id)[0]
        self.assertFalse(self.ledger.exists())
        return task, approval

    def resume(self, task, approval, decision='APPROVED', edits=None):
        with patch('server.agent.graph.ChatOpenAI', ScriptedModel):
            self.phase4.decide(approval['id'], decision, edits)
        return self.manager.phase3.tasks.get(task.id)


class RegistryTests(Fixture):
    def test_dynamic_discovery_preserves_schema(self):
        tool = self.phase4.registry.get_tool('mcp.github.get_file_contents')
        self.assertIn('path', tool.input_schema['properties'])
        self.assertEqual(tool.input_schema, tool.metadata['raw_input_schema'])

    def test_snapshot_is_immutable_across_refresh(self):
        snapshot = self.phase4.registry.snapshot()
        self.config['GITHUB_MCP_READ_ONLY'] = True
        asyncio.run(self.phase4.registry.refresh())
        self.assertTrue(self.phase4.registry.get_tool(self.tool.name, snapshot).enabled)
        with self.assertRaises(ToolUnavailable):
            self.phase4.registry.get_tool(self.tool.name)

    def test_disambiguates_same_remote_names(self):
        second = MCPToolProvider(MCPServerConfig(id='other', url='https://example.com/mcp'), {}, in_memory_server=self.server)
        self.phase4.registry.register_provider(second)
        asyncio.run(self.phase4.registry.refresh())
        self.assertIn('mcp.other.create_issue', self.phase4.registry.snapshot())
        self.assertIn('mcp.github.create_issue', self.phase4.registry.snapshot())

    def test_unknown_capability_is_unavailable(self):
        with self.assertRaises(ToolUnavailable):
            self.phase4.registry.find_by_capability('github.merge')

    def test_forbidden_tools_are_disabled(self):
        with self.assertRaises(ToolUnavailable):
            self.phase4.registry.get_tool('mcp.github.delete_repository')

    def test_unknown_provider(self):
        with self.assertRaises(ToolUnavailable):
            asyncio.run(self.phase4.registry.refresh('missing'))

    def test_duplicate_provider_rejected(self):
        with self.assertRaises(ValueError):
            self.phase4.registry.register_provider(self.provider)

    def test_native_provider_round_trip(self):
        def search_workspace(query: str):
            """Search synthetic documents."""
            return {'query': query}
        registry = ToolRegistry()
        registry.register_provider(NativeToolProvider([StructuredTool.from_function(search_workspace)]))
        asyncio.run(registry.refresh())
        tool = registry.find_by_capability('workspace.search')[0]
        self.assertEqual(asyncio.run(registry.call_tool(tool.name, {'query': 'x'})).structured_content, {'query': 'x'})

    def test_schema_validation_precedes_call(self):
        with self.assertRaises(SchemaValidationError):
            asyncio.run(self.phase4.registry.call_tool(self.tool.name, {'title': 'Missing repository'}))
        self.assertFalse(self.ledger.exists())

    def test_provider_failure_does_not_remove_native(self):
        with patch.object(self.provider, 'list_tools', side_effect=RuntimeError('down')):
            asyncio.run(self.phase4.registry.refresh())
        self.assertEqual(self.phase4.registry.health['github'], 'unavailable')
        asyncio.run(self.phase4.registry.refresh())
        self.assertEqual(self.phase4.registry.health['github'], 'connected')


class PolicyTests(Fixture):
    def test_disabling_write_approvals_disables_writes(self):
        self.config['TOOL_APPROVE_WRITES'] = False
        with self.assertRaises(PermissionError):
            self.phase4.policy.evaluate(self.tool, self.arguments)
        self.assertFalse(self.ledger.exists())

    def test_read_auto_approved(self):
        read = self.phase4.registry.get_tool('mcp.github.get_file_contents')
        self.assertFalse(self.phase4.policy.evaluate(read, {'owner': 'fixture', 'repo': 'test'}).requires_approval)

    def test_write_requires_approval(self):
        self.assertTrue(self.phase4.policy.evaluate(self.tool, self.arguments).requires_approval)

    def test_delete_requires_approval(self):
        descriptor = ToolDescriptor(name='native.delete', provider='native', provider_type='native',
            capability='delete', operation_type='delete', risk_level='low', requires_approval=False)
        self.assertTrue(self.phase4.policy.evaluate(descriptor, {}).requires_approval)

    def test_annotations_cannot_waive_approval(self):
        descriptor = self.tool.model_copy(update={'operation_type': 'read', 'risk_level': 'low', 'requires_approval': False})
        self.assertTrue(self.phase4.policy.evaluate(descriptor, self.arguments).requires_approval)

    def test_repository_allowlist(self):
        with self.assertRaises(PermissionError):
            self.phase4.policy.evaluate(self.tool, {**self.arguments, 'repo': 'forbidden'})

    def test_read_only_precedes_approval(self):
        self.config['GITHUB_MCP_READ_ONLY'] = True
        with self.assertRaises(ToolUnavailable):
            self.phase4.policy.evaluate(self.tool, self.arguments)

    def test_approval_disabled_fails_closed(self):
        self.config['TOOL_APPROVAL_ENABLED'] = False
        with self.assertRaises(PermissionError):
            self.phase4.policy.evaluate(self.tool, self.arguments)

    def test_edits_cannot_change_repository(self):
        task, approval = self.waiting()
        with self.assertRaises(ValueError):
            self.phase4.actions.decide(approval['id'], 'EDITED', edits={'repo': 'other'}, policy=self.phase4.policy)

    def test_secret_arguments_rejected(self):
        with self.assertRaises(ValueError):
            self.phase4.policy.evaluate(self.tool, {**self.arguments, 'body': 'ghp_fakeTOKEN012345678901234567890'})

    def test_secret_filter_nested(self):
        secret = 'ghp_fakeTOKEN012345678901234567890'
        with patch.dict(os.environ, {'GITHUB_PERSONAL_ACCESS_TOKEN': secret}):
            value = SecretFilter().clean({'nested': [secret, {'Authorization': 'Bearer ' + secret}]})
        self.assertNotIn(secret, json.dumps(value))


class ApprovalTests(Fixture):
    def native_data_task(self, steps, source_scope='workspace_only'):
        from io import BytesIO
        self.manager.phase3.data.upload(self.workspace.id, 'synthetic.csv', BytesIO(b'model,miou\nA,70\nB,74\n'), 'text/csv')
        ScriptedModel.answer = 'Synthetic analysis complete.'
        ScriptedModel.script = {'QueryAnalysis': [{'standalone_query': 'Average miou from synthetic.csv',
            'query_type': 'research', 'needs_retrieval': True, 'selected_sources': []}],
            'TaskPlan': [{'goal': 'Synthetic analysis', 'steps': steps}]}
        task = self.manager.phase3.task_service.create(self.workspace.id, 'Average miou from synthetic.csv', source_scope)
        with patch('server.agent.graph.ChatOpenAI', ScriptedModel):
            result = self.manager.phase3.task_service.run(task.id)
        return task, result

    def test_native_data_analysis_uses_registry_and_preserves_phase3(self):
        task, final = self.native_data_task([
            {'id': 'inspect', 'description': 'Inspect', 'query': 'Inspect synthetic.csv', 'preferred_tool': 'inspect_dataset'},
            {'id': 'analyze', 'description': 'Analyze', 'query': 'Average miou', 'preferred_tool': 'analyze_data'}])
        self.assertEqual(final.status, 'COMPLETED')
        executions = self.phase4.actions.executions(task.id)
        self.assertEqual({e['capability'] for e in executions}, {'dataset.inspect','data.analyze'})
        self.assertTrue(all(e['status'] == 'SUCCEEDED' for e in executions))
        self.assertEqual(self.phase4.actions.approvals(task.id), [])

    def test_data_capabilities_remain_available_with_mcp_enabled(self):
        task, final = self.native_data_task([
            {'id': 'inspect', 'description': 'Inspect', 'query': 'Inspect synthetic.csv', 'preferred_capability': 'dataset.inspect'},
            {'id': 'analyze', 'description': 'Analyze', 'query': 'Average miou', 'preferred_capability': 'data.analyze'}], 'workspace_and_external')
        self.assertEqual(final.status, 'COMPLETED')
        self.assertEqual({e['capability'] for e in self.phase4.actions.executions(task.id)}, {'dataset.inspect','data.analyze'})

    def test_secret_task_goal_rejected_before_persistence(self):
        with self.assertRaises(ValueError):
            self.manager.phase3.task_service.create(self.workspace.id, 'Issue body ghp_FAKE_SECRET_123456789', 'external')
        self.assertEqual(self.manager.phase3.tasks.list(), [])

    def test_native_read_manual_approval_propagates_interrupt(self):
        self.config['TOOL_AUTO_APPROVE_READ'] = False
        task, final = self.native_data_task([
            {'id': 'inspect', 'description': 'Inspect', 'query': 'Inspect synthetic.csv', 'preferred_tool': 'inspect_dataset'}])
        self.assertEqual(final.status, 'WAITING_USER')
        approval = self.phase4.actions.approvals(task.id)[0]
        self.assertEqual(self.resume(task, approval).status, 'COMPLETED')
        self.assertEqual(len(self.phase4.actions.approvals(task.id)), 1)

    def test_registry_cannot_bypass_durable_approval(self):
        with self.assertRaises(TaskConflict):
            asyncio.run(self.phase4.registry.call_tool(self.tool.name, self.arguments))
        self.assertFalse(self.ledger.exists())

    def test_approval_origin_rejects_untrusted_web_page(self):
        task, approval = self.waiting()
        app = FastAPI()
        register_phase4_api(app, self.manager)
        response = TestClient(app).post('/approvals/'+approval['id']+'/approve', headers={'Origin': 'https://attacker.example'})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.phase4.actions.approval(approval['id'])['status'], 'PENDING')

    def test_actual_graph_interrupt_waiting_user(self):
        task, approval = self.waiting()
        runtime = self.manager._get_or_restore_runtime(task.session_id)
        checkpoint = runtime['agent'].get_state({'configurable': {'thread_id': 'task:' + task.id}})
        self.assertEqual(checkpoint.next, ('executor',))
        self.assertTrue(checkpoint.tasks[0].interrupts)
        self.assertEqual(approval['status'], 'PENDING')

    def test_approve_executes_once_and_completes(self):
        task, approval = self.waiting()
        self.assertEqual(self.resume(task, approval).status, 'COMPLETED')
        self.assertEqual(len(json.loads(self.ledger.read_text())), 1)
        self.assertEqual(self.manager.phase3.tasks.steps(task.id)[0].attempt_count, 1)
        self.assertEqual(self.phase4.actions.approval(approval['id'])['status'], 'APPROVED')

    def test_reject_never_calls_provider(self):
        task, approval = self.waiting()
        self.assertEqual(self.resume(task, approval, 'REJECTED').status, 'COMPLETED')
        self.assertFalse(self.ledger.exists())
        self.assertEqual(self.phase4.actions.execution(approval['tool_execution_id'])['status'], 'REJECTED')

    def test_edit_validates_and_executes_edited_body(self):
        task, approval = self.waiting()
        self.resume(task, approval, 'EDITED', {'body': 'Edited synthetic text'})
        self.assertEqual(json.loads(self.ledger.read_text())[0]['body'], 'Edited synthetic text')

    def test_edit_type_invalid_preserves_pending(self):
        task, approval = self.waiting()
        with self.assertRaises(SchemaValidationError):
            self.phase4.actions.decide(approval['id'], 'EDITED', edits={'body': 42}, policy=self.phase4.policy)
        self.assertEqual(self.phase4.actions.approval(approval['id'])['status'], 'PENDING')

    def test_pending_cannot_resume(self):
        task, approval = self.waiting()
        with self.assertRaises(TaskConflict):
            self.manager.phase3.task_service.run(task.id, resume=True)

    def test_duplicate_decision_rejected(self):
        task, approval = self.waiting()
        self.resume(task, approval)
        with self.assertRaises(TaskConflict):
            self.phase4.decide(approval['id'], 'APPROVED')
        self.assertEqual(len(json.loads(self.ledger.read_text())), 1)

    def test_duplicate_resume_rejected(self):
        task, approval = self.waiting()
        self.resume(task, approval)
        with self.assertRaises(TaskConflict):
            self.manager.phase3.task_service.run(task.id, resume=True)
        self.assertEqual(len(json.loads(self.ledger.read_text())), 1)

    def test_restart_retains_interrupt_and_decision(self):
        task, approval = self.waiting()
        restored = AgentSessionManager(self.config)
        provider = MCPToolProvider(self.provider.server, self.config, in_memory_server=self.server)
        restored.phase4.registry.register_provider(provider)
        asyncio.run(restored.phase4.registry.refresh())
        restored.phase3.tasks.recover()
        self.assertEqual(restored.phase3.tasks.get(task.id).status, 'WAITING_USER')
        with patch('server.agent.graph.ChatOpenAI', ScriptedModel):
            restored.phase4.decide(approval['id'], 'APPROVED')
        self.assertEqual(restored.phase3.tasks.get(task.id).status, 'COMPLETED')
        self.assertEqual(len(json.loads(self.ledger.read_text())), 1)

    def test_concurrent_claims_allow_one(self):
        task, approval = self.waiting()
        self.phase4.actions.decide(approval['id'], 'APPROVED', policy=self.phase4.policy)
        def claim():
            try:
                return self.phase4.actions.claim(approval['tool_execution_id'])[0]
            except TaskConflict:
                return False
        with ThreadPoolExecutor(2) as pool:
            self.assertEqual(sum(pool.map(lambda _: claim(), range(2))), 1)

    def test_uncertain_running_action_not_retried(self):
        task, approval = self.waiting()
        self.phase4.actions.decide(approval['id'], 'APPROVED', policy=self.phase4.policy)
        self.phase4.actions.claim(approval['tool_execution_id'])
        with self.assertRaises(TaskConflict):
            ToolActionStore(self.phase4.actions.path).claim(approval['tool_execution_id'])

    def test_cancel_pending_approval(self):
        task, approval = self.waiting()
        self.phase4.actions.cancel_pending(task.id)
        self.manager.phase3.tasks.transition(task.id, 'CANCELLED')
        with self.assertRaises(TaskConflict):
            self.phase4.decide(approval['id'], 'APPROVED')
        self.assertFalse(self.ledger.exists())

    def test_write_is_action_result_not_evidence(self):
        task, approval = self.waiting()
        final = self.resume(task, approval)
        runtime = self.manager._get_or_restore_runtime(task.session_id)
        state = runtime['agent'].get_state({'configurable': {'thread_id': 'task:' + task.id}}).values
        self.assertEqual(state.get('evidence', []), [])
        self.assertEqual(state['action_results'][0]['status'], 'SUCCEEDED')
        self.assertIn('SUCCEEDED', final.metadata['answer'])


class MCPTests(Fixture):
    def test_chat_read_uses_dynamic_capability_router(self):
        self.script('github.file.read', {'owner': 'fixture', 'repo': 'test', 'path': 'README.md'})
        session = self.manager.create_workspace_session(self.workspace.id)
        with patch('server.agent.graph.ChatOpenAI', ScriptedModel):
            reply = self.manager.ask(session['session_id'], 'Read synthetic README from GitHub fixture/test', source_scope='external')
        self.assertIn('mcp.github.get_file_contents', reply.trace_summary['tools_used'])
        self.assertFalse(self.ledger.exists())

    def test_real_stdio_sdk_transport(self):
        import sys
        provider = MCPToolProvider(MCPServerConfig(id='stdio_fixture', transport='stdio', command=sys.executable,
            args=[str(Path('evaluation/phase4_mock_server.py').resolve())]), {})
        tools = asyncio.run(provider.list_tools())
        self.assertTrue(any(t.name == 'mcp.stdio_fixture.create_issue' for t in tools))
        self.assertTrue(asyncio.run(provider.call_tool('get_file_contents', {'owner': 'fixture', 'repo': 'test'})).success)

    def test_stdio_call_timeout(self):
        import sys
        provider = MCPToolProvider(MCPServerConfig(id='timeout_fixture', transport='stdio', command=sys.executable,
            args=[str(Path('evaluation/phase4_mock_server.py').resolve())], connect_timeout_seconds=2, call_timeout_seconds=.1), {})
        with self.assertRaises(ToolUnavailable):
            asyncio.run(provider.call_tool('delay', {'seconds': 5}))

    def test_stdio_crash_marks_provider_unavailable(self):
        import sys
        provider = MCPToolProvider(MCPServerConfig(id='crash_fixture', transport='stdio', command=sys.executable,
            args=['-c', 'raise SystemExit(1)'], connect_timeout_seconds=.5, call_timeout_seconds=.5), {})
        with self.assertRaises(ToolUnavailable):
            asyncio.run(provider.list_tools())
        self.assertFalse(provider.health()['connected'])

    def test_schema_external_ref_rejected(self):
        from server.tool_registry import check_schema
        for keyword in ('$ref', '$dynamicRef', '$recursiveRef'):
            with self.assertRaises(ValueError):
                check_schema({'type': 'object', 'properties': {'value': {keyword: 'https://attacker.example/schema'}}})

    def test_unknown_mcp_execute_requires_approval(self):
        descriptor = ToolDescriptor(name='mcp.unknown.read', provider='unknown', provider_type='mcp',
            capability='unknown.read', operation_type='read', risk_level='low', requires_approval=False,
            metadata={'remote_name': 'read'})
        self.assertTrue(self.phase4.policy.evaluate(descriptor, {}).requires_approval)

    def test_database_contains_no_configured_secret(self):
        fake = 'ghp_FAKE_SECRET_FOR_REDACTION_123456789'
        with patch.dict(os.environ, {'GITHUB_PERSONAL_ACCESS_TOKEN': fake}):
            safe = SecretFilter().clean({'text': fake})
        with self.phase4.actions.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS redaction_fixture (value TEXT)')
            db.execute('INSERT INTO redaction_fixture VALUES (?)', (json.dumps(safe),))
        self.assertNotIn(fake.encode(), self.phase4.actions.path.read_bytes())

    def test_api_invalid_edit_keeps_pending(self):
        task, approval = self.waiting()
        app = FastAPI()
        register_phase4_api(app, self.manager)
        response = TestClient(app).post('/approvals/'+approval['id']+'/edit', json={'edits': {'repo': 'forbidden'}})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.phase4.actions.approval(approval['id'])['status'], 'PENDING')

    def test_config_disallows_all_toolset(self):
        with patch.dict(os.environ, {'MCP_GITHUB_TOOLSETS': 'all'}):
            with self.assertRaises(ValueError):
                load_config()

    def test_sdk_read_normalized(self):
        result = asyncio.run(self.phase4.registry.call_tool('mcp.github.get_file_contents', {'owner': 'fixture', 'repo': 'test'}))
        self.assertTrue(result.success)
        self.assertIsInstance(result.content[0], dict)
        self.assertTrue(result.metadata['untrusted'])

    def test_disconnect_reconnect(self):
        asyncio.run(self.provider.disconnect())
        self.assertFalse(self.provider.health()['connected'])
        asyncio.run(self.provider.reconnect())
        self.assertTrue(self.provider.health()['connected'])

    def test_sdk_error_normalized(self):
        result = asyncio.run(self.provider.call_tool('missing', {}))
        self.assertFalse(result.success)

    def test_content_injection_does_not_create_action(self):
        self.script('github.file.read', {'owner': 'fixture', 'repo': 'test', 'path': 'README.md'})
        task = self.manager.phase3.task_service.create(self.workspace.id, 'Read synthetic README only', 'external')
        with patch('server.agent.graph.ChatOpenAI', ScriptedModel):
            final = self.manager.phase3.task_service.run(task.id)
        self.assertEqual(final.status, 'COMPLETED')
        self.assertFalse(self.ledger.exists())
        self.assertEqual(self.phase4.actions.approvals(task.id), [])

    def test_server_config_rejects_literal_secret(self):
        with self.assertRaises(ValueError):
            MCPServerConfig(id='example', transport='stdio', command='python', args=['ghp_fakeTOKEN01234567890'])
        with self.assertRaises(ValueError):
            MCPServerConfig(id='example', name='ghp_fakeTOKEN01234567890', url='https://example.com/mcp')

    def test_server_config_rejects_credential_url(self):
        with self.assertRaises(ValueError):
            MCPServerConfig(id='example', url='https://user:password@example.com/mcp')

    def test_server_config_rejects_remote_http(self):
        with self.assertRaises(ValueError):
            MCPServerConfig(id='example', url='http://example.com/mcp')

    def test_server_store_contains_env_names_only(self):
        stored = self.phase4.servers.save(self.provider.server)
        self.assertNotIn('Authorization', stored.model_dump_json())

    def test_api_has_no_arbitrary_call_route(self):
        app = FastAPI()
        register_phase3_api(app, self.manager)
        register_phase4_api(app, self.manager)
        paths = {r.path for r in app.routes}
        self.assertNotIn('/tools/call', paths)
        client = TestClient(app)
        self.assertEqual(client.get('/tools').status_code, 200)
        self.assertEqual(client.get('/approvals/missing').status_code, 404)

    def test_api_reject_approval(self):
        task, approval = self.waiting()
        app = FastAPI()
        register_phase3_api(app, self.manager)
        register_phase4_api(app, self.manager)
        client = TestClient(app)
        with patch('server.agent.graph.ChatOpenAI', ScriptedModel):
            response = client.post('/approvals/' + approval['id'] + '/reject')
        self.assertEqual(response.status_code, 200)
        self.assertFalse(self.ledger.exists())


if __name__ == '__main__':
    unittest.main()
