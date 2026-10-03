"""Agent-level evaluation of recorded responses or a clearly labelled offline demo."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import statistics
import time
from typing import Any
from unittest.mock import patch

from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import StructuredTool

from server.agent.context_harness import ContextHarness
from server.agent.evidence import Citation, Evidence, validate_citations
from server.agent.graph import build_agent

DEFAULT_CASES = Path(__file__).parent / "datasets" / "agent_workspace_examples.jsonl"


def evaluate(cases: list[dict[str, Any]], responses: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Score every case, including missing results; latency is measured in milliseconds."""
    rows = []
    for case in cases:
        response = responses.get(case["id"], {})
        trace = response.get("trace_summary", {})
        evidence = [Evidence.model_validate(item) for item in response.get("evidence", [])]
        citations = [Citation.model_validate(item) for item in response.get("citations", [])]
        present = {item.document_id for item in evidence}
        steps = (response.get("plan") or {}).get("steps", [])
        rows.append({
            "id": case["id"], "missing_response": not bool(response),
            "tool_selection_accuracy": set(trace.get("tools_used", [])) == set(case["expected_tools"]),
            "retrieval_success": set(case["expected_documents"]) <= present,
            "planner_success": bool(response) and (bool(steps) and all(step["status"] == "completed" for step in steps) if case["expected_complexity"] == "complex" else not steps),
            "citation_validity": bool(citations) and len(validate_citations(citations, evidence)) == len(citations),
            "grounded_answer": bool(trace.get("verification_passed")),
            "tool_calls": trace.get("tool_calls", 0),
            "retrieval_attempts": trace.get("retrieval_attempts", 0),
            "latency_ms": trace.get("latency", 0),
        })
    if not rows:
        raise ValueError("Evaluation requires at least one case")
    metrics = {key: statistics.mean(float(row[key]) for row in rows) for key in
               ("tool_selection_accuracy", "retrieval_success", "planner_success", "citation_validity", "grounded_answer", "tool_calls", "retrieval_attempts", "latency_ms")}
    return {"case_count": len(rows), "metrics": metrics, "cases": rows,
            "groundedness_measurement": "Verifier pass rate; requires independent human/benchmark validation for factual accuracy."}


class DemoModel:
    """Deterministic fixtures exercise graph wiring; they do not estimate model quality."""
    def __init__(self, **kwargs: Any):
        pass

    def with_structured_output(self, schema, **kwargs):
        class Structured:
            def invoke(self, messages):
                query = str(messages[-1].content)
                if schema.__name__ == "QueryAnalysis":
                    return schema(standalone_query=query, query_type="document", needs_retrieval=True, selected_sources=["workspace"])
                if schema.__name__ == "TaskPlan":
                    return schema(goal="Compare methods", steps=[{"id": "step_1", "description": "Retrieve methods from three papers", "query": "compare methods", "sources": ["workspace"]}])
                if schema.__name__ == "RetrievalGrade":
                    return schema(relevant=True, sufficient=True, confidence=1, reason="Fixture passages cover methods")
                if schema.__name__ == "VerificationResult":
                    return schema(grounded=True, unsupported_claims=[], confidence=1)
                raise ValueError("Unexpected demo model call")
        return Structured()

    def invoke(self, messages):
        content = "\n".join(str(message.content) for message in messages)
        cited = [identity for identity in ("a", "b", "c") if f'"evidence_id": "ev_{identity}"' in content]
        return AIMessage(content="\n".join(f"Paper {identity} uses method {identity.upper()} [ev_{identity}]" for identity in cited))


def demo_response(case: dict[str, Any]) -> dict[str, Any]:
    def search(query: str) -> dict[str, Any]:
        return {"evidence": [Evidence(evidence_id=f"ev_{identity}", source_type="workspace", document_id=identity,
                 document_name=f"paper_{identity}.pdf", page=1, page_count=1, chunk_id=identity,
                 content=f"Paper {identity} uses method {identity.upper()}.").model_dump() for identity in case["expected_documents"]]}
    tool = StructuredTool.from_function(search, name="search_workspace", description="Search fixture documents")
    harness = ContextHarness(context_window_tokens=20000, input_budget_tokens=10000, max_output_tokens=512,
                             safety_tokens=512, summary_tokens=256, recent_turns=4)
    started = time.perf_counter()
    with patch("server.agent.graph.ChatOpenAI", DemoModel):
        graph = build_agent("offline-fixture", "unused", [tool], context_harness=harness, max_output_tokens=512,
                            request_timeout_seconds=10, workflow_config={})
        result = graph.invoke({"messages": [HumanMessage(content=case["question"])], "source_scope": case["source_scope"],
                               "workspace_id": "fixture", "document_registry": {identity: 1 for identity in case["expected_documents"]}}, {"recursion_limit": 60})
    return {"answer": result["final_answer"], "evidence": result["evidence"], "citations": result["citations"],
            "plan": result["task_plan"], "trace_summary": {**result["trace_summary"], "latency": (time.perf_counter() - started) * 1000}}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--demo", action="store_true")
    mode.add_argument("--responses", type=Path, help="JSONL rows: {id, response: <chat API response>}")
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    cases = [json.loads(line) for line in args.cases.read_text(encoding="utf-8").splitlines() if line.strip()]
    if args.demo:
        responses = {case["id"]: demo_response(case) for case in cases}
    else:
        records = [json.loads(line) for line in args.responses.read_text(encoding="utf-8").splitlines() if line.strip()]
        responses = {record["id"]: record["response"] for record in records}
    result = evaluate(cases, responses)
    result["mode"] = "synthetic_offline_demo_not_a_quality_benchmark" if args.demo else "recorded_agent_responses"
    rendered = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered)


if __name__ == "__main__":
    main()
