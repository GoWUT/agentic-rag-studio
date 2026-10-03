"""Composition and policy-controlled calls shared by chat and durable tasks."""
import asyncio
import json
from pathlib import Path
import sqlite3
import time
from contextlib import contextmanager
from uuid import uuid4
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.types import interrupt
from langchain_core.tools import StructuredTool

from server.mcp_client import MCPServerConfig, MCPServerStore, MCPToolProvider
from server.tool_actions import ToolActionStore, arguments_hash
from server.tool_policy import ToolPolicy
from server.tool_registry import NativeToolProvider, ToolRegistry, ToolDescriptor, ToolExecutionResult, ToolUnavailable, validate_arguments
from server.tasks import TaskConflict
from server.observability.runtime import tool_span


class RedactedSerializer(JsonPlusSerializer):
    def __init__(self, secrets):
        super().__init__(pickle_fallback=False)
        self.secrets = secrets

    def dumps_typed(self, obj):
        return super().dumps_typed(self.secrets.clean(obj))


class ManagedSqliteSaver(SqliteSaver):
    """SDK saver with short-lived connections, safe for Windows temp files."""
    def __init__(self, path, serde):
        super().__init__(None, serde=serde)
        self.path = path

    @contextmanager
    def cursor(self, transaction=True):
        with self.lock:
            self.conn = sqlite3.connect(self.path, timeout=30)
            try:
                self.setup()
                cursor = self.conn.cursor()
                try:
                    yield cursor
                    if transaction:
                        self.conn.commit()
                finally:
                    cursor.close()
            finally:
                self.conn.close()
                self.conn = None


class Phase4Services:
    def __init__(self, manager):
        self.manager, self.config = manager, manager.config
        self.policy = ToolPolicy(self.config)
        self.actions = manager.repositories.build(ToolActionStore)
        self.registry = ToolRegistry(call_guard=self.guard_call)
        # Discover the native catalog before any chat; implementations are bound
        # to the authorized workspace/session when a runtime is constructed.
        def search(query: str):
            """Search the authorized source in a session-bound runtime."""
            raise ToolUnavailable('A session-bound runtime is required')
        def inspect_dataset(dataset_ids: list[str]):
            """Inspect authorized workspace data assets."""
            raise ToolUnavailable('A session-bound runtime is required')
        def analyze_data(dataset_ids: list[str], objective: str, code: str = ''):
            """Run the constrained data-analysis runtime."""
            raise ToolUnavailable('A session-bound runtime is required')
        catalog = [StructuredTool.from_function(search, name=name) for name in
            ('search_workspace', 'search_pdf', 'search_web', 'search_arxiv')]
        catalog += [StructuredTool.from_function(inspect_dataset), StructuredTool.from_function(analyze_data)]
        self.registry.register_provider(NativeToolProvider(catalog))
        self.registry.definitions.update({t.name: t.model_dump() for t in self.registry.providers['native'].descriptors()})
        self.registry.health['native'] = 'runtime_bound'
        self.servers = manager.repositories.build(MCPServerStore)
        from server.checkpoints import CheckpointFactory
        self.checkpointer = CheckpointFactory.create(manager.store.database_path, RedactedSerializer(self.policy.secrets))
        # Configuration is operator-owned. No graph node or public registration endpoint.
        configs = []
        config_path = self.config.get('MCP_SERVERS_FILE', '')
        if config_path:
            configs = [MCPServerConfig.model_validate(v) for v in json.loads(Path(config_path).read_text(encoding='utf-8'))]
        if self.config.get('GITHUB_MCP_ENABLED', False):
            configs = [v for v in configs if v.id != 'github']
            configs.append(MCPServerConfig(id='github', name='Official GitHub MCP',
                url='https://api.githubcopilot.com/mcp/', env_keys=['GITHUB_PERSONAL_ACCESS_TOKEN'],
                connect_timeout_seconds=self.config.get('MCP_CONNECT_TIMEOUT_SECONDS', 10),
                call_timeout_seconds=self.config.get('MCP_CALL_TIMEOUT_SECONDS', 30)))
        self.server_configs = configs
        if self.actions.backend == 'postgresql':
            return  # Connect/configure during process startup after DB health.
        self.initialize_servers()

    def initialize_servers(self):
        configs = self.server_configs
        active = {c.id: c for c in configs}
        # Persist names and schemas only; stale configuration is not auto-enabled.
        for config in self.servers.list():
            if config.id not in active:
                self.servers.save(config.model_copy(update={'enabled': False}))
        for config in configs:
            self.servers.save(config)
            if config.enabled and self.config.get('MCP_ENABLED', True):
                self.registry.register_provider(MCPToolProvider(config, self.config))

    def event(self, task_id, kind, payload=None):
        if task_id:
            with self.manager.phase3.tasks.connect() as db:
                self.manager.phase3.tasks._event(db, task_id, kind, self.policy.secrets.clean(payload or {}))

    def guard_call(self, descriptor, arguments, execution_id):
        decision = self.policy.evaluate(descriptor, arguments)
        if decision.requires_approval:
            if not execution_id:
                raise TaskConflict('Sensitive provider calls require a durable execution claim')
            execution = self.actions.execution(execution_id)
            if execution['status'] != 'RUNNING' or execution['tool_name'] != descriptor.name or execution['arguments_hash'] != arguments_hash(arguments):
                raise TaskConflict('Execution claim does not match the call')
            approval = self.actions.approval(execution['approval_request_id'])
            if not approval['consumed_at'] or approval['status'] not in {'APPROVED', 'EDITED'}:
                raise TaskConflict('Approval has not been consumed for this execution')

    def refresh(self, provider_id=None):
        if provider_id == 'native':
            return self.registry.list_tools()
        result = asyncio.run(self.registry.refresh(provider_id))
        return result

    def runtime_registry(self, tools):
        registry = ToolRegistry(call_guard=self.guard_call)
        registry.providers = {k: v for k, v in self.registry.providers.items() if k != 'native'}
        registry.health = dict(self.registry.health)
        registry.definitions = self.registry.snapshot()
        registry.register_provider(NativeToolProvider(tools))
        asyncio.run(registry.refresh('native'))
        with self.registry.lock:
            self.registry.definitions.update({n: d for n, d in registry.snapshot().items() if d['provider'] == 'native'})
            self.registry.health['native'] = 'runtime_bound'
        return registry

    @tool_span
    def execute(self, registry, descriptor, arguments, state):
        security=getattr(self.manager,'security',None)
        if security:security.authorize_tool(descriptor,state)
        validate_arguments(descriptor, arguments)
        decision = self.policy.evaluate(descriptor, arguments)
        context = {**state, 'run_id': state.get('tool_run_id', str(uuid4()))}
        execution = self.actions.request(descriptor, arguments, context, decision)
        task_id = state.get('task_id')
        if execution['status'] == 'REQUESTED':
            self.event(task_id, 'TOOL_REQUESTED', {'tool_execution_id': execution['id'], 'tool': descriptor.name, 'capability': descriptor.capability})
        elif execution['status'] == 'WAITING_APPROVAL':
            self.event(task_id, 'TOOL_REQUESTED', {'tool_execution_id': execution['id'], 'tool': descriptor.name, 'capability': descriptor.capability})
        if execution['approval_request_id']:
            approval = self.actions.approval(execution['approval_request_id'])
            if not approval['consumed_at']:
                tasks = self.manager.phase3.tasks
                if tasks.get(task_id).status == 'RUNNING' and approval['status'] == 'PENDING':
                    tasks.wait_for_approval(task_id, state['task_step_id'])
                    self.event(task_id, 'APPROVAL_REQUESTED', {'approval_request_id': approval['id'], 'tool_execution_id': execution['id']})
                # Always call interrupt at the same position. On Command resume this
                # returns the decision ID; decisions/arguments come only from SQLite.
                decision_id = interrupt({'approval_request_id': approval['id'], 'task_id': task_id,
                    'tool': descriptor.name, 'reason': approval['reason']})
                if decision_id != approval['id'] or approval['status'] == 'PENDING':
                    raise TaskConflict('A durable human decision is required')
                arguments = approval['edited_arguments'] or approval['arguments']
                validate_arguments(descriptor, arguments)
                self.policy.evaluate(descriptor, arguments)
        # Re-evaluate immediately before claiming. Server metadata cannot bypass it.
        if security:security.authorize_tool(descriptor,state)
        self.policy.evaluate(descriptor, arguments)
        claimed, execution = self.actions.claim(execution['id'])
        if not claimed:
            return ToolExecutionResult(success=execution['status'] == 'SUCCEEDED', structured_content=execution['result_summary'],
                error_type=None if execution['status'] == 'SUCCEEDED' else 'Rejected',
                metadata={'tool_execution_id': execution['id'], 'status': execution['status'], 'replayed': True})
        self.event(task_id, 'TOOL_STARTED', {'tool_execution_id': execution['id'], 'approval_request_id': execution['approval_request_id']})
        started = time.perf_counter()
        try:
            result = asyncio.run(registry.call_tool(descriptor.name, arguments, snapshot=state.get('tool_snapshot'), execution_id=execution['id']))
            result = ToolExecutionResult.model_validate(self.policy.secrets.clean(result.model_dump()))
        except Exception as error:
            result = ToolExecutionResult(success=False, error_type=type(error).__name__)
        duration = round((time.perf_counter() - started) * 1000, 2)
        # Only action receipts are persisted. Read content stays in graph evidence.
        summary = {'success': result.success, 'content_blocks': len(result.content)}
        if descriptor.operation_type != 'read':
            data = result.structured_content
            if not data:
                try:
                    data = json.loads(next((b.get('text', '') for b in result.content if b.get('type') == 'text'), '{}'))
                except ValueError:
                    data = {}
            if isinstance(data, dict):
                summary.update({k: data[k] for k in ('number', 'id', 'url', 'html_url') if k in data})
        execution = self.actions.finish(execution['id'], success=result.success, summary=summary, duration_ms=duration, error_type=result.error_type)
        if security and descriptor.provider_type=='mcp' and descriptor.operation_type!='read' and result.success:
            from server.auth.context import require_principal
            with security.store.connect() as db:
                security.store.audit(db,'EXTERNAL_WRITE_EXECUTED',actor=require_principal().user_id,
                    workspace=state.get('workspace_id'),resource_type='tool_execution',resource_id=execution['id'],
                    metadata={'tool_execution_id':execution['id'],'approval_id':execution.get('approval_request_id')})
        self.event(task_id, 'TOOL_COMPLETED' if result.success else 'TOOL_FAILED', {
            'tool_execution_id': execution['id'], 'approval_request_id': execution['approval_request_id'],
            'tool': descriptor.name, 'provider': descriptor.provider, 'capability': descriptor.capability,
            'operation_type': descriptor.operation_type, 'risk_level': descriptor.risk_level,
            'duration_ms': duration, 'status': execution['status'], 'error_type': result.error_type})
        result.metadata.update(tool_execution_id=execution['id'], status=execution['status'], action_receipt=summary)
        return result

    def decide(self, approval_id, decision, edits=None):
        security=getattr(self.manager,'security',None)
        if security:security.approval(approval_id,write=True)
        approval = self.actions.approval(approval_id)
        task = self.manager.phase3.tasks.get(approval['task_id'])
        if task.status != 'WAITING_USER':
            raise TaskConflict('Task is not waiting for approval')
        value = self.actions.decide(approval_id, decision, edits=edits, policy=self.policy)
        if security:
            from server.auth.context import require_principal
            with security.store.connect() as db:
                security.store.audit(db,'APPROVAL_REJECTED' if decision=='REJECTED' else 'APPROVAL_APPROVED',
                    actor=require_principal().user_id,workspace=task.workspace_id,resource_type='approval',resource_id=approval_id)
        self.event(task.id, {'APPROVED': 'APPROVAL_APPROVED', 'EDITED': 'APPROVAL_EDITED', 'REJECTED': 'APPROVAL_REJECTED'}[decision], {'approval_request_id': approval_id})
        if any(a['status'] == 'PENDING' for a in self.actions.approvals(task.id)):
            # Parallel read branches can each require a decision. Resume only
            # after every pending interrupt has a durable human decision.
            return value
        if self.config.get('TASK_EXECUTION_MODE', 'inline') == 'worker':
            # Decision is durable first; run/resume remains retriable if queue is down.
            from server.runtime_services import enqueue_sync
            enqueue_sync(self.manager, task.id, resume=True)
        else:
            self.manager.phase3.task_service.run(task.id, resume=True)
        return value

    def close(self):
        if hasattr(self.checkpointer, 'close'):
            self.checkpointer.close()
