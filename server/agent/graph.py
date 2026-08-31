from langgraph.graph import StateGraph, END
from langgraph.prebuilt import ToolNode, tools_condition

from langchain_openai import ChatOpenAI
from langchain_core.messages import SystemMessage

from server.agent.context_harness import ContextHarness
from server.agent.state import AgentState


def build_agent(
    model_name: str,
    deepseek_api_key: str,
    tools,
    *,
    context_harness: ContextHarness,
    max_output_tokens: int,
    request_timeout_seconds: float,
):

    llm = ChatOpenAI(
        model=model_name,
        temperature=0,
        api_key=deepseek_api_key,
        base_url="https://api.deepseek.com",
        max_tokens=max_output_tokens,
        timeout=request_timeout_seconds,
        max_retries=0,
    ).bind_tools(tools)

    SYSTEM_PROMPT = """
    You are an agentic RAG assistant.

    Rules:
    - If a PDF search tool is available, you MUST use it for questions
      that can be answered from the uploaded PDF.
    - Use web search only if the PDF does not contain the answer.
    - Never claim you do not have a PDF if a PDF tool exists.
    - Use arxiv search only if the PDF does not contain the answer.
    - If you are not sure how to answer a question, you can use web search
      to find the answer.
    - If a tool result contains WEB_SEARCH_UNAVAILABLE, clearly tell the
      user that web search is unavailable and do not invent search results.
    """

    def llm_node(state: AgentState):
        messages = state["messages"]

        if not any(isinstance(m, SystemMessage) for m in messages):
            messages = [SystemMessage(content=SYSTEM_PROMPT)] + messages

        prepared = context_harness.prepare(messages)
        response = llm.invoke(prepared.messages)

        return {"messages": [response]}

    graph = StateGraph(AgentState)

    graph.add_node("llm", llm_node)

    graph.add_node("tools", ToolNode(tools))

    graph.set_entry_point("llm")

    graph.add_conditional_edges(
        "llm",
        tools_condition,
        {
            "tools": "tools",
            END: END,
        },
    )

    graph.add_edge("tools", "llm")

    return graph.compile()
