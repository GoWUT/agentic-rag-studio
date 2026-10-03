"""Actual-provider complete-result Reviewer contract probe, distinct from end-to-end C."""
from pathlib import Path
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
import httpx
from langgraph.graph import StateGraph, START, END
from typing import TypedDict
from server.config import load_config
from server.tool_policy import SecretFilter
from server.agent_runs import AgentRunStore
from server.agent.specialized_agents import BoundedModel, private_harness
from server.agent.orchestration_schemas import ReviewerResult


def run(path):
    config = load_config()
    # The probe is an explicitly controlled contract fixture, no provider tools/actions.
    from server.tasks import TaskStore
    TaskStore(path)
    store = AgentRunStore(path, SecretFilter())
    row = store.create_supervisor(None,'Review the supplied numeric results only.','single_agent',{})
    llm = ChatOpenAI(model=config['MODEL_NAME'],api_key=config['DEEPSEEK_API_KEY'],base_url='https://api.deepseek.com',
        temperature=0,max_tokens=1024,timeout=config['MODEL_REQUEST_TIMEOUT_SECONDS'],max_retries=0,
        http_client=httpx.Client(trust_env=False),http_async_client=httpx.AsyncClient(trust_env=False))
    model = BoundedModel(llm,private_harness(config),store,row['id'],2,SecretFilter())
    class ProbeState(TypedDict):
        result: dict
    def review(state):
        value = model.invoke([SystemMessage(content='Review only the stated goal using the supplied complete result and evidence. Return ReviewerResult. Do not demand work outside the explicit goal. No hidden reasoning. This is a controlled synthetic result-contract probe.'),
            HumanMessage(content='{"goal":"Check that reported values A=73.27 and C=70.0 match the supplied evidence, and that their difference is 3.27. No chart, architecture, causal or uncertainty claim is requested.","results":[{"agent_id":"research","status":"completed","summary":"The synthetic paper reports A=73.27 and C=70.0.","evidence_ids":["ev_fixture_paper"]},{"agent_id":"data","status":"completed","summary":"The synthetic table has A=73.27 and C=70.0; computed difference=3.27.","evidence_ids":["ev_fixture_table"]}],"evidence":[{"evidence_id":"ev_fixture_paper","content":"Synthetic paper: A=73.27; C=70.0."},{"evidence_id":"ev_fixture_table","content":"Synthetic table: A=73.27; C=70.0; subtraction 73.27-70.0=3.27."}],"draft_synthesis":"The paper and table report A=73.27 and C=70.0, a descriptive difference of 3.27. These are synthetic values, no causal or independent-validation claim."}')],ReviewerResult)
        return {'result':value.model_dump()}
    graph=StateGraph(ProbeState)
    graph.add_node('reviewer',review)
    graph.add_edge(START,'reviewer')
    graph.add_edge('reviewer',END)
    return {**graph.compile(name='reviewer-complete-result-probe').invoke({})['result'],
        'fixture':'controlled complete-result contract, actual LLM; distinct from end-to-end Task C',
        'llm_calls':store.run(row['id'])['llm_calls'],'tokens':store.run(row['id'])['tokens']}
