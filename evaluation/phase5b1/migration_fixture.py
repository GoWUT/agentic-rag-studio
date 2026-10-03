"""Synthetic migration rows created through the existing domain repositories."""
from types import SimpleNamespace
from server.tool_actions import ToolActionStore
from server.tool_registry import ToolDescriptor
from server.tasks import TaskStore
from server.agent_runs import AgentRunStore
from server.agent.orchestration_schemas import DelegationRequest, DelegationContext


def add_approval_and_delegation(source, task, registry, secrets):
    actions = ToolActionStore(source)
    step = TaskStore(source).steps(task.id)[0]
    tool = ToolDescriptor(name='mcp.fixture.create', provider='fixture', provider_type='mcp',
                          capability='github.issue.create', input_schema={'type':'object'},
                          operation_type='write', risk_level='high')
    execution = actions.request(tool, {}, {'task_id':task.id, 'task_step_id':step.id, 'run_id':'fixture'},
                                SimpleNamespace(requires_approval=True, reason='Synthetic fixture'))
    actions.decide(execution['approval_request_id'], 'REJECTED')
    runs = AgentRunStore(source, secrets)
    supervisor = runs.create_supervisor(task.id, task.goal, 'single_agent', {})
    agent = registry.get('research')
    request = DelegationRequest(task_id=task.id, supervisor_run_id=supervisor['id'], agent_id='research',
        objective='Collect synthetic evidence', required_capabilities=['research.document'],
        context=DelegationContext(user_goal=task.goal), expected_output=agent.output_contract,
        allowed_tool_capabilities=agent.tool_capabilities, max_iterations=agent.max_iterations,
        max_tool_calls=agent.max_tool_calls)
    delegation = runs.create_delegation(request, 8)
    return {'approval':execution['approval_request_id'], 'supervisor':supervisor['id'],
            'delegation':delegation['id']}
