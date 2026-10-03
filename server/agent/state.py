from typing import Annotated, Literal, TypedDict

from langchain_core.messages import BaseMessage
from langgraph.graph.message import add_messages
from server.agent.orchestration_schemas import combine_results


QueryType = Literal["direct", "document", "web", "arxiv", "research"]
SourceName = Literal["pdf", "workspace", "web", "arxiv"]
EvidenceStatus = Literal["success", "unavailable", "error"]


class RetrievalStepState(TypedDict):
    query: str
    sources: list[SourceName]


class EvidenceItem(TypedDict):
    source: Literal['pdf', 'workspace', 'web', 'arxiv', 'analysis', 'mcp', 'github']
    query: str
    content: str
    round: int
    status: EvidenceStatus


class AgentState(TypedDict, total=False):
    """Research state; durable graph checkpoints also retain tool snapshots."""

    messages: Annotated[list[BaseMessage], add_messages]

    original_query: str
    standalone_query: str
    query_type: QueryType
    needs_retrieval: bool

    plan: list[RetrievalStepState]
    selected_sources: list[SourceName]
    evidence_pool: list[EvidenceItem]

    evidence_sufficient: bool
    evidence_relevance: float
    evidence_coverage: float
    missing_information: list[str]
    grading_failed: bool

    retry_count: int
    refined_query: str

    final_answer: str
    workspace_id: str | None
    source_scope: Literal["workspace_only", "workspace_and_external", "external"]
    task_complexity: Literal["simple", "complex"]
    task_goal: str
    task_plan: dict | None
    current_step: int
    step_results: list[dict]
    retrieval_attempts: int
    rewrite_count: int
    replan_count: int
    tool_calls: int
    tools_used: list[str]
    evidence: list[dict]
    citations: list[dict]
    verification_result: dict | None
    revision_count: int
    external_fallback_done: bool
    trace_summary: dict
    document_registry: dict[str, int | None]
    document_metadata: dict
    plan_is_task_plan: bool
    retrieved_memories: list[dict]
    owner_id: str
    session_id: str
    task_id: str | None
    task_status: str
    task_step_id: str | None
    task_resume: dict
    task_step_limit: int | None
    task_steps_executed: int
    task_boundary_stop: bool
    data_asset_ids: list[str]
    dataset_inspection: dict
    analysis_execution_id: str
    analysis_result: dict | None
    artifacts: list[dict]
    memory_candidates: list[dict]
    memory_written: int
    synthesis_completed: bool
    step_error_type: str
    tool_snapshot: dict
    tool_run_id: str
    action_results: list[dict]
    orchestration_decision: dict
    supervisor_run_id: str
    agent_snapshot: dict
    supervisor_plan: dict
    shared_context: dict
    orchestration_groups: list[list[str]]
    orchestration_group: int
    agent_outcomes: Annotated[list[dict], combine_results]
    orchestration_draft: str
    review_result: dict | None
    review_round: int
    redelegation_count: int
    revision_pending: bool
    pre_orchestration_llm_calls: int
    pre_orchestration_usage: list[dict | None]
    post_revision_check: dict
