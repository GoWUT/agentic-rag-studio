"""Public supervisor-worker contracts. No conversation or executable agent definitions."""
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field
from server.persistence import identity, now


class Contract(BaseModel):
    model_config = ConfigDict(extra='forbid')


class AgentDescriptor(Contract):
    agent_id: str
    name: str
    description: str
    capabilities: list[str]
    tool_capabilities: list[str] = Field(default_factory=list)
    can_delegate: bool = False
    enabled: bool = True
    max_iterations: int = Field(default=4, ge=1, le=12)
    max_tool_calls: int = Field(default=4, ge=0, le=16)
    context_policy: str = 'selected_structured_context'
    output_contract: str
    metadata: dict = Field(default_factory=dict)


class AgentRegistrySnapshot(Contract):
    agents: dict[str, AgentDescriptor]
    created_at: str = Field(default_factory=now)


class DelegationContext(Contract):
    user_goal: str
    constraints: list[str] = Field(default_factory=list)
    workspace_id: str | None = None
    source_scope: Literal['workspace_only', 'workspace_and_external', 'external'] = 'workspace_only'
    relevant_memory_ids: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    dataset_ids: list[str] = Field(default_factory=list)
    artifact_ids: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class DelegationRequest(Contract):
    delegation_id: str = Field(default_factory=identity)
    task_id: str | None = None
    parent_step_id: str | None = None
    supervisor_run_id: str
    agent_id: str
    objective: str = Field(min_length=1, max_length=6000)
    required_capabilities: list[str]
    context: DelegationContext
    expected_output: str
    priority: int = Field(default=0, ge=0, le=10)
    allowed_tool_capabilities: list[str] = Field(default_factory=list)
    max_iterations: int = Field(default=4, ge=1)
    max_tool_calls: int = Field(default=4, ge=0)
    depth: int = Field(default=1, ge=1)
    execution_group: int = Field(default=0, ge=0)
    revision_of: str | None = None
    supplementary: dict | None = None
    force_refresh: bool = False


class Finding(Contract):
    claim: str
    evidence_ids: list[str] = Field(default_factory=list)
    confidence: float = Field(default=0, ge=0, le=1)


class ResearchAgentResult(Contract):
    delegation_id: str
    status: Literal['completed', 'partial', 'failed']
    summary: str
    findings: list[Finding] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    unresolved_questions: list[str] = Field(default_factory=list)
    confidence: float = Field(default=0, ge=0, le=1)


class DataAgentResult(ResearchAgentResult):
    metrics: dict = Field(default_factory=dict)
    artifact_ids: list[str] = Field(default_factory=list)
    execution_ids: list[str] = Field(default_factory=list)


class CodeFinding(Finding):
    file: str | None = None
    severity: Literal['low', 'medium', 'high'] = 'medium'


class ProposedChange(Contract):
    file: str
    description: str
    patch_proposal: str = ''
    evidence_ids: list[str] = Field(default_factory=list)


class CodingAgentResult(ResearchAgentResult):
    files_examined: list[str] = Field(default_factory=list)
    code_findings: list[CodeFinding] = Field(default_factory=list)
    proposed_changes: list[ProposedChange] = Field(default_factory=list)


class SuggestedDelegation(Contract):
    capability: str
    objective: str = Field(min_length=1, max_length=2000)
    missing_item: str


class ReviewerResult(Contract):
    verdict: Literal['pass', 'needs_revision', 'insufficient']
    missing_items: list[str] = Field(default_factory=list)
    contradictions: list[str] = Field(default_factory=list)
    unsupported_claims: list[str] = Field(default_factory=list)
    suggested_delegations: list[SuggestedDelegation] = Field(default_factory=list)
    confidence: float = Field(default=0, ge=0, le=1)


class DelegationResult(Contract):
    delegation_id: str
    agent_id: str
    revision_of: str | None = None
    status: Literal['completed', 'partial', 'failed', 'cancelled']
    summary: str = Field(max_length=3000)
    evidence_ids: list[str] = Field(default_factory=list)
    artifact_ids: list[str] = Field(default_factory=list)
    unresolved_questions: list[str] = Field(default_factory=list)
    confidence: float = Field(default=0, ge=0, le=1)
    payload: dict = Field(default_factory=dict)


class SharedTaskContext(Contract):
    task_id: str | None = None
    goal: str
    constraints: list[str] = Field(default_factory=list)
    workspace_id: str | None = None
    source_scope: Literal['workspace_only', 'workspace_and_external', 'external'] = 'workspace_only'
    known_facts: list[Finding] = Field(default_factory=list)
    available_assets: list[dict] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    artifact_ids: list[str] = Field(default_factory=list)
    delegation_results: list[DelegationResult] = Field(default_factory=list)
    completed_delegations: list[str] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)
    created_at: str = Field(default_factory=now)
    updated_at: str = Field(default_factory=now)


class OrchestrationDecision(Contract):
    mode: Literal['existing_workflow', 'single_agent', 'multi_agent']
    reason_summary: str = Field(max_length=600)
    required_capabilities: list[str] = Field(default_factory=list)
    suggested_agents: list[str] = Field(default_factory=list)
    independent_subtasks: int = Field(default=0, ge=0)
    dependency_type: Literal['none', 'parallel', 'sequential', 'mixed'] = 'none'
    estimated_agent_count: int = Field(default=0, ge=0)
    expected_multi_agent_value: Literal['low', 'medium', 'high'] = 'low'
    estimated_cost_level: Literal['low', 'medium', 'high'] = 'low'


class DelegationSpec(Contract):
    key: str
    capability: str
    objective: str = Field(min_length=1, max_length=3000)
    depends_on: list[str] = Field(default_factory=list)


class AgentToolCall(Contract):
    capability: str
    arguments: dict = Field(default_factory=dict)


class AgentWorkPlan(Contract):
    steps: list[AgentToolCall] = Field(default_factory=list)


class SupervisorPlan(Contract):
    goal: str
    delegations: list[DelegationSpec]
    execution_groups: list[list[str]]
    reviewer_required: bool = True
    actions: list[AgentToolCall] = Field(default_factory=list)


RESULT_CONTRACTS = {'research': ResearchAgentResult, 'data': DataAgentResult,
                    'coding': CodingAgentResult, 'reviewer': ReviewerResult}


def combine_results(left, right):
    """Identity merge for parallel worker updates."""
    by_id = {r['delegation_id']: r for r in left or []}
    for result in right or []:
        if result['delegation_id'] in by_id and by_id[result['delegation_id']] != result:
            raise ValueError('Conflicting parallel results')
        by_id[result['delegation_id']] = result
    return sorted(by_id.values(), key=lambda r: r['delegation_id'])
