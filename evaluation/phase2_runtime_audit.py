"""Reproducible Phase 2.5 audit runner; real models/HTTP, no provider mocks."""
from __future__ import annotations

import argparse
from contextvars import ContextVar
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import time
from typing import Any
from uuid import uuid4

import httpx
from langchain_core.messages import HumanMessage
from langchain_core.tools import StructuredTool
from langchain_openai import ChatOpenAI
import requests
from langchain_core.callbacks import BaseCallbackHandler

from server.agent.context_harness import ContextHarness
from server.agent.research_workflow import ResearchWorkflowNodes
from server.config import load_config

ROOT = Path(__file__).resolve().parents[1]
RESULTS = ROOT / "evaluation" / "results" / "phase2_5"
ACTIVE_AUDIT: ContextVar[Any] = ContextVar("phase2_audit", default=None)
ROUTING_CASES = [
    ("Direct", "你好"), ("Direct", "谢谢你的帮助"),
    ("Direct", "什么是 RAG？请给一个简短的通用定义，不需要查询论文。"),
    ("Direct", "Explain what an embedding is without searching documents."),
    ("Direct", "Compare RAG and fine-tuning conceptually, without searching my documents."),
    ("Simple", "What is the proposed method called in the GCNet paper?"),
    ("Simple", "这篇 GCNet 论文的作者是谁？"),
    ("Simple", "What datasets are used in the GCNet paper?"),
    ("Simple", "模型参数量是多少？请查看 GCNet 论文。"),
    ("Simple", "What is the title of the uploaded PViGS paper?"),
    ("Simple", "Which backbone is used in TDA-YOLO?"),
    ("Simple", "Summarize the GCNet paper's main contribution in one sentence."),
    ("Complex", "Compare the methodology, datasets and experimental results of all papers in this workspace."),
    ("Complex", "对比 Workspace 中三篇论文的核心方法、实验设置、性能、局限性和创新点。"),
    ("Complex", "Compare the main methods proposed in these three papers."),
    ("Complex", "Across documents, explain how each paper addresses efficiency and accuracy, then synthesize the trade-offs."),
    ("Complex", "分别提取每篇论文的方法、数据集和结果，并给出对比结论。"),
    ("Complex", "For each paper, identify the assumptions, evaluate the evidence and compare limitations."),
    ("Complex", "Build a comparison of all papers covering architecture, evaluation metrics and failure cases."),
    ("Complex", "综合分析三篇论文的创新点和适用条件，提出有证据支持的研究方向。"),
]


def save(name: str, payload: Any) -> None:
    RESULTS.mkdir(parents=True, exist_ok=True)
    (RESULTS / name).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def routing(label: str) -> None:
    config = load_config()
    if not config.get("DEEPSEEK_API_KEY"):
        result = {"status": "SKIPPED: No API key available", "cases": []}
        save(f"routing_{label}.json", result)
        print(result["status"], flush=True)
        return
    def run(case: tuple[str, str]) -> dict[str, Any]:
        expected, query = case
        def never_called(query: str) -> str:
            raise RuntimeError("Routing-only audit must never invoke a tool")
        tools = [StructuredTool.from_function(never_called, name=name, description=name.replace("_", " "))
                 for name in ("search_workspace", "search_web", "search_arxiv")]
        harness = ContextHarness(context_window_tokens=config["MODEL_CONTEXT_WINDOW_TOKENS"], input_budget_tokens=config["CONTEXT_INPUT_BUDGET_TOKENS"],
            max_output_tokens=config["MAX_OUTPUT_TOKENS"], safety_tokens=config["CONTEXT_SAFETY_TOKENS"],
            summary_tokens=config["CONTEXT_SUMMARY_TOKENS"], recent_turns=config["CONTEXT_RECENT_TURNS"])
        llm = ChatOpenAI(model=config["MODEL_NAME"], api_key=config["DEEPSEEK_API_KEY"], base_url="https://api.deepseek.com",
            temperature=0, timeout=config["MODEL_REQUEST_TIMEOUT_SECONDS"], max_tokens=config["MAX_OUTPUT_TOKENS"], max_retries=0,
            http_client=httpx.Client(trust_env=config["MODEL_TRUST_ENV"]), http_async_client=httpx.AsyncClient(trust_env=config["MODEL_TRUST_ENV"]))
        nodes = ResearchWorkflowNodes(llm, tools, harness, config=config)
        started = time.perf_counter()
        try:
            state = nodes.query_analyzer({"messages": [HumanMessage(content=query)], "workspace_id": "routing-audit", "source_scope": "workspace_and_external"})
            route = nodes.route_after_analysis(state)
            actual = {"generator": "Direct", "simple_plan": "Simple", "planner": "Complex"}[route]
            result = {"query": query, "expected": expected, "actual": actual, "passed": actual == expected,
                      "query_type": state["query_type"], "latency_ms": round((time.perf_counter() - started) * 1000, 2)}
        except Exception as error:
            result = {"query": query, "expected": expected, "actual": "ERROR", "passed": False, "error_type": type(error).__name__}
        print(f"routing {label}: {expected} -> {result['actual']} {'PASS' if result['passed'] else 'FAIL'}", flush=True)
        return result
    with ThreadPoolExecutor(max_workers=2) as pool:
        rows = list(pool.map(run, ROUTING_CASES))
    result = {"mode": "real_llm_analyzer_and_production_router", "model": config["MODEL_NAME"], "total": len(rows),
              "passed": sum(row["passed"] for row in rows), "errors": sum(row["actual"] == "ERROR" for row in rows), "cases": rows}
    save(f"routing_{label}.json", result)
    print(json.dumps({key: value for key, value in result.items() if key != "cases"}), flush=True)


def http_request(session: requests.Session, method: str, base: str, endpoint: str, **kwargs: Any) -> tuple[Any, float]:
    started = time.perf_counter()
    response = session.request(method, base + endpoint, timeout=kwargs.pop("timeout", 660), **kwargs)
    response.raise_for_status()
    return response.json() if response.content else None, round((time.perf_counter() - started) * 1000, 2)


def uploads(base: str) -> None:
    session = requests.Session()
    session.trust_env = False
    workspace, _ = http_request(session, "POST", base, "/workspaces", json={"name": "phase2_validation", "description": "Phase 2.5 real runtime smoke test"})
    second, _ = http_request(session, "POST", base, "/workspaces", json={"name": "phase2_validation_reuse"})
    corpus = json.loads((ROOT / "evaluation/datasets/retrieval_v2_corpora.json").read_text(encoding="utf-8"))["corpora"]
    records = []
    for paper in corpus:
        with (ROOT / paper["pdf"]).open("rb") as stream:
            payload, latency = http_request(session, "POST", base, f"/workspaces/{workspace['id']}/documents", files=[("files", (paper["id"] + ".pdf", stream, "application/pdf"))])
        document = payload["documents"][0]
        records.append({"corpus": paper["id"], "source_pdf": paper["pdf"], "first_upload_ms": latency, "document": document})
        save("uploads.json", {"workspace": workspace, "reuse_workspace": second, "records": records})
        print(f"uploaded {paper['id']} status={document['status']} reused={document.get('index_reused')} ms={latency}", flush=True)
    paper = corpus[0]
    with (ROOT / paper["pdf"]).open("rb") as stream:
        payload, latency = http_request(session, "POST", base, f"/workspaces/{second['id']}/documents", files=[("files", (paper["id"] + ".pdf", stream, "application/pdf"))])
    records[0]["second_upload_ms"] = latency
    records[0]["reuse_document"] = payload["documents"][0]
    chat_session, _ = http_request(session, "POST", base, f"/workspaces/{workspace['id']}/sessions")
    save("uploads.json", {"workspace": workspace, "reuse_workspace": second, "records": records, "session": chat_session})
    print(f"reuse status={records[0]['reuse_document']['status']} reused={records[0]['reuse_document'].get('index_reused')} ms={latency}", flush=True)


def chat(base: str, name: str, question: str, scope: str, legacy: bool = False) -> None:
    session = requests.Session()
    session.trust_env = False
    uploads = json.loads((RESULTS / "uploads.json").read_text(encoding="utf-8"))
    if legacy:
        source = ROOT / uploads["records"][0]["source_pdf"]
        with source.open("rb") as stream:
            chat_session, upload_ms = http_request(session, "POST", base, "/upload_pdf", files={"file": ("gcnet.pdf", stream, "application/pdf")})
        request = {"session_id": chat_session["session_id"], "message": question, "source_scope": scope}
    else:
        chat_session, _ = http_request(session, "POST", base, f"/workspaces/{uploads['workspace']['id']}/sessions")
        request = {"session_id": chat_session["session_id"], "workspace_id": uploads["workspace"]["id"], "message": question, "source_scope": scope}
    started = time.perf_counter()
    try:
        response, latency = http_request(session, "POST", base, "/chat", json=request, timeout=240)
        result = {"name": name, "question": question, "request": request, "status": "PASS", "total_http_ms": latency, "response": response}
        print(f"chat {name}: PASS ms={latency} citations={len(response['citations'])} trace={json.dumps(response['trace_summary'])}", flush=True)
    except Exception as error:
        status_code = getattr(getattr(error, "response", None), "status_code", None)
        result = {"name": name, "question": question, "request": request, "status": "FAIL", "status_code": status_code, "error_type": type(error).__name__, "total_http_ms": round((time.perf_counter() - started) * 1000, 2)}
        print(f"chat {name}: FAIL {type(error).__name__} HTTP={status_code}", flush=True)
    save(f"chat_{name}.json", result)


class RuntimeCallbacks(BaseCallbackHandler):
    """Use LangChain's callbacks for timing/counts without recording prompts or documents."""
    def __init__(self):
        self.started: dict[Any, float] = {}
        self.names: dict[Any, str] = {}
        self.node_events: list[dict[str, Any]] = []
        self.models: list[dict[str, Any]] = []
        self.tools: list[dict[str, Any]] = []
        self.reranks: list[dict[str, Any]] = []

    def on_chain_start(self, serialized, inputs, *, run_id, **kwargs):
        name = kwargs.get("name", "")
        if name in {"query_analyzer", "simple_plan", "planner", "executor", "step_evaluator", "evidence_grader", "query_refiner", "replanner", "external_fallback", "generator", "verifier", "answer_revision", "citation_validator"}:
            self.started[run_id] = time.perf_counter()
            self.names[run_id] = name

    def on_chain_end(self, outputs, *, run_id, **kwargs):
        if run_id not in self.names:
            return
        event = {"node": self.names.pop(run_id), "duration_ms": (time.perf_counter() - self.started.pop(run_id)) * 1000}
        if isinstance(outputs, dict):
            for key in ("evidence_sufficient", "grading_failed", "rewrite_count", "retrieval_attempts", "revision_count", "refined_query"):
                if key in outputs:
                    event[key] = outputs[key]
            if outputs.get("verification_result"):
                event["grounded"] = outputs["verification_result"]["grounded"]
                event["unsupported_claim_count"] = len(outputs["verification_result"]["unsupported_claims"])
        self.node_events.append(event)

    def on_chat_model_start(self, serialized, messages, *, run_id, **kwargs):
        self.started[run_id] = time.perf_counter()
        self.names[run_id] = kwargs.get("metadata", {}).get("langgraph_node", "model")

    def on_llm_end(self, response, *, run_id, **kwargs):
        event = {"node": self.names.pop(run_id, "model"), "duration_ms": (time.perf_counter() - self.started.pop(run_id, time.perf_counter())) * 1000}
        if response.generations:
            message = getattr(response.generations[0][0], "message", None)
            event["usage"] = getattr(message, "usage_metadata", None)
        self.models.append(event)

    def on_llm_error(self, error, *, run_id, **kwargs):
        self.models.append({"node": self.names.pop(run_id, "model"), "duration_ms": (time.perf_counter() - self.started.pop(run_id, time.perf_counter())) * 1000, "error_type": type(error).__name__})

    def on_tool_start(self, serialized, input_str, *, run_id, **kwargs):
        self.started[run_id] = time.perf_counter()
        self.names[run_id] = serialized.get("name", "tool")

    def on_tool_end(self, output, *, run_id, **kwargs):
        self.tools.append({"tool": self.names.pop(run_id, "tool"), "duration_ms": (time.perf_counter() - self.started.pop(run_id, time.perf_counter())) * 1000})

    def on_tool_error(self, error, *, run_id, **kwargs):
        self.on_tool_end(None, run_id=run_id)
        self.tools[-1]["error_type"] = type(error).__name__

    def summary(self) -> dict[str, Any]:
        return {"node_events": self.node_events, "node_count": len(self.node_events), "models": self.models, "llm_calls": len(self.models),
                "tools": self.tools, "tool_calls_observed": len(self.tools), "reranks": self.reranks,
                "reranker_calls": len(self.reranks), "llm_ms": sum(item["duration_ms"] for item in self.models),
                "tool_ms": sum(item["duration_ms"] for item in self.tools), "reranker_ms": sum(item["duration_ms"] for item in self.reranks)}


def serve(port: int) -> None:
    """Run the actual FastAPI application with audit-only callbacks, no behavioral mocks."""
    import logging
    import uvicorn
    from server.sessions import _ScopedAgent
    from server.rag.retrieval import CrossEncoderReranker
    from server.main import app
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    original_invoke, original_rerank = _ScopedAgent.invoke, CrossEncoderReranker.rerank
    def invoke(self, inputs, config):
        observer = RuntimeCallbacks()
        token = ACTIVE_AUDIT.set(observer)
        try:
            result = dict(original_invoke(self, inputs, {**config, "callbacks": [observer]}))
            result["trace_summary"] = {**result.get("trace_summary", {}), "audit_runtime": observer.summary()}
            return result
        finally:
            ACTIVE_AUDIT.reset(token)
    def rerank(self, query, documents):
        started = time.perf_counter()
        try:
            return original_rerank(self, query, documents)
        finally:
            observer = ACTIVE_AUDIT.get()
            if observer:
                observer.reranks.append({"candidate_count": len(documents), "duration_ms": (time.perf_counter() - started) * 1000})
    _ScopedAgent.invoke = invoke
    CrossEncoderReranker.rerank = rerank
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="info")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["routing", "uploads", "chat", "serve"])
    parser.add_argument("--label", default="final")
    parser.add_argument("--base", default="http://127.0.0.1:8001")
    parser.add_argument("--question", default="What is the method proposed in the GCNet paper called?")
    parser.add_argument("--scope", default="workspace_only")
    parser.add_argument("--legacy", action="store_true")
    parser.add_argument("--port", type=int, default=8001)
    args = parser.parse_args()
    if args.mode == "routing":
        routing(args.label)
    elif args.mode == "uploads":
        uploads(args.base)
    elif args.mode == "serve":
        serve(args.port)
    else:
        chat(args.base, args.label, args.question, args.scope, args.legacy)


if __name__ == "__main__":
    main()
