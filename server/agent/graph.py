import httpx
from langchain_openai import ChatOpenAI
from langgraph.graph import END, START, StateGraph

from server.agent.context_harness import ContextHarness
from server.agent.nodes import AgentWorkflowNodes
from server.agent.state import AgentState


def build_agent(
    model_name: str,
    deepseek_api_key: str,
    tools,
    *,
    context_harness: ContextHarness,
    max_output_tokens: int,
    request_timeout_seconds: float,
    max_retrieval_retries: int = 1,
    model_trust_env: bool = True,
    workflow_config=None,
    services=None,
):
    """Build the bounded, explicit Agent Graph V2 state machine."""
    client_options = {}
    if not model_trust_env:
        client_options = {
            "http_client": httpx.Client(trust_env=False),
            "http_async_client": httpx.AsyncClient(trust_env=False),
        }
    base_llm = ChatOpenAI(
        model=model_name,
        temperature=0,
        api_key=deepseek_api_key,
        base_url="https://api.deepseek.com",
        max_tokens=max_output_tokens,
        timeout=request_timeout_seconds,
        max_retries=0,
        **client_options,
    )
    node_class = AgentWorkflowNodes
    options = {}
    if workflow_config is not None:
        from server.agent.research_workflow import ResearchWorkflowNodes
        node_class = ResearchWorkflowNodes
        options["config"] = workflow_config
        if services is not None:
            from server.agent.persistent_workflow import PersistentWorkflowNodes
            node_class = PersistentWorkflowNodes
            options["services"] = services
            if getattr(services, 'phase4', None) is not None and workflow_config.get('TOOL_REGISTRY_ENABLED', True):
                from server.agent.tool_workflow import ToolWorkflowNodes
                node_class = ToolWorkflowNodes
                if getattr(services, 'phase5', None):
                    from server.agent.orchestration import OrchestrationNodes
                    node_class = OrchestrationNodes
                    if workflow_config.get('MULTI_AGENT_EFFICIENCY_ENABLED',False):
                        from server.agent.efficient_orchestration import EfficientOrchestrationNodes
                        node_class = EfficientOrchestrationNodes
    nodes = node_class(
        base_llm,
        tools,
        context_harness,
        max_retrieval_retries=max_retrieval_retries,
        **options,
    )

    if workflow_config is not None:
        return _build_research_graph(nodes)

    graph = StateGraph(AgentState)
    graph.add_node("query_analyzer", nodes.query_analyzer)
    graph.add_node("planner", nodes.planner)
    graph.add_node("retriever", nodes.retriever)
    graph.add_node("evidence_grader", nodes.evidence_grader)
    graph.add_node("query_refiner", nodes.query_refiner)
    graph.add_node("generator", nodes.generator)

    graph.add_edge(START, "query_analyzer")
    graph.add_conditional_edges(
        "query_analyzer",
        nodes.route_after_analysis,
        {"planner": "planner", "generator": "generator"},
    )
    graph.add_edge("planner", "retriever")
    graph.add_edge("retriever", "evidence_grader")
    graph.add_conditional_edges(
        "evidence_grader",
        nodes.route_after_grading,
        {"query_refiner": "query_refiner", "generator": "generator"},
    )
    graph.add_edge("query_refiner", "retriever")
    graph.add_edge("generator", END)

    return graph.compile(name="agent-graph-v2")


def _build_research_graph(nodes):
    """Wire the explicit sequential planner/executor and correction paths."""
    graph = StateGraph(AgentState)
    for name in ("query_analyzer", "simple_plan", "planner", "executor", "step_evaluator",
                 "evidence_grader", "query_refiner", "replanner", "external_fallback", "generator",
                 "verifier", "answer_revision", "citation_validator"):
        graph.add_node(name, getattr(nodes, name))
    if hasattr(nodes, "retrieve_memory"):
        graph.add_node("retrieve_memory", nodes.retrieve_memory)
        graph.add_node("extract_memory", nodes.extract_memory)
        graph.add_edge(START, "retrieve_memory")
        graph.add_edge("retrieve_memory", "query_analyzer")
    else:
        graph.add_edge(START, "query_analyzer")
    if hasattr(nodes, 'orchestration_router'):
        for name in ('orchestration_router', 'supervisor_plan', 'dispatch_agents', 'agent_worker', 'merge_agents',
                     'supervisor_actions', 'draft_synthesis', 'review_agents', 'redelegate', 'orchestration_complete', 'orchestration_stop'):
            graph.add_node(name, getattr(nodes, name))
        graph.add_edge('query_analyzer', 'orchestration_router')
        graph.add_conditional_edges('orchestration_router', nodes.route_orchestration,
            {name: name for name in ('supervisor_plan', 'simple_plan', 'planner', 'generator', 'executor')})
        graph.add_edge('supervisor_plan', 'dispatch_agents')
        graph.add_conditional_edges('dispatch_agents', nodes.send_agents, ['agent_worker', 'orchestration_stop'])
        graph.add_edge('agent_worker', 'merge_agents')
        graph.add_conditional_edges('merge_agents', nodes.after_merge, {name: name for name in ('dispatch_agents', 'supervisor_actions', 'orchestration_stop')})
        graph.add_edge('supervisor_actions', 'draft_synthesis')
        graph.add_edge('draft_synthesis', 'review_agents')
        graph.add_edge('review_agents', 'redelegate')
        graph.add_conditional_edges('redelegate', nodes.after_review, {'dispatch_agents': 'dispatch_agents', 'orchestration_complete': 'orchestration_complete'})
        graph.add_edge('orchestration_complete', 'generator')
        graph.add_edge('orchestration_stop', 'generator')
    else:
        graph.add_conditional_edges("query_analyzer", nodes.route_after_analysis,
            {name: name for name in ("simple_plan", "planner", "generator", "executor")})
    for name in ("simple_plan", "planner", "query_refiner", "replanner", "external_fallback"):
        graph.add_edge(name, "executor")
    graph.add_edge("executor", "step_evaluator")
    graph.add_conditional_edges("step_evaluator", nodes.route_after_step,
        {name: name for name in ("executor", "evidence_grader", "generator")})
    graph.add_conditional_edges("evidence_grader", nodes.route_after_grading,
        {name: name for name in ("query_refiner", "replanner", "external_fallback", "generator")})
    graph.add_edge("generator", "verifier")
    graph.add_conditional_edges("verifier", nodes.route_after_verification,
        {name: name for name in ("answer_revision", "citation_validator")})
    graph.add_edge("answer_revision", "verifier")
    if hasattr(nodes, "extract_memory"):
        graph.add_edge("citation_validator", "extract_memory")
        graph.add_edge("extract_memory", END)
    else:
        graph.add_edge("citation_validator", END)
    phase4 = getattr(getattr(nodes, 'services', None), 'phase4', None)
    compiled = graph.compile(name="research-workspace", checkpointer=phase4.checkpointer if phase4 else None)
    if hasattr(nodes, 'registry'):
        compiled.tool_registry = nodes.registry
    if hasattr(nodes, 'phase5'):
        compiled.phase5 = nodes.phase5
    return compiled
