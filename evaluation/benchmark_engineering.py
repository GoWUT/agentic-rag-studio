"""Offline benchmarks for context control and session-level concurrency."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
from pathlib import Path
import statistics
import time

from langchain_core.messages import (
    AIMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)

from server.agent.context_harness import ContextHarness
from server.agent.execution_harness import ExecutionHarness


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = Path(__file__).parent / "results" / "engineering_benchmark.json"


class SleepingAgent:
    def __init__(self, delay_seconds: float):
        self.delay_seconds = delay_seconds

    def invoke(self, inputs, config):
        time.sleep(self.delay_seconds)
        return {"messages": list(inputs["messages"])}


def build_long_history(turns: int = 60):
    messages = [SystemMessage(content="你是一个文档问答助手。")]
    for index in range(turns):
        messages.append(
            HumanMessage(
                content=f"第 {index} 轮问题：" + "需要结合前文条件回答。" * 80
            )
        )
        if index % 5 == 0:
            call_id = f"call-{index}"
            messages.append(
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "pdf_retriever",
                            "args": {"query": f"第 {index} 轮检索"},
                            "id": call_id,
                            "type": "tool_call",
                        }
                    ],
                )
            )
            messages.append(
                ToolMessage(
                    content="检索结果：" + "文档证据与上下文。" * 350,
                    tool_call_id=call_id,
                )
            )
        messages.append(
            AIMessage(content=f"第 {index} 轮回答：" + "结论与解释。" * 120)
        )
    messages.append(HumanMessage(content="请回答最后一个必须保留的问题。"))
    return messages


def tool_protocol_is_valid(messages) -> bool:
    requested_ids = {
        call["id"]
        for message in messages
        if isinstance(message, AIMessage)
        for call in message.tool_calls
    }
    returned_ids = {
        message.tool_call_id
        for message in messages
        if isinstance(message, ToolMessage)
    }
    return requested_ids == returned_ids


def benchmark_context(repeats: int = 100) -> dict:
    messages = build_long_history()
    harness = ContextHarness(
        context_window_tokens=1_000_000,
        input_budget_tokens=60_000,
        max_output_tokens=4_096,
        safety_tokens=4_096,
        summary_tokens=2_048,
        recent_turns=4,
    )
    latencies_ms = []
    prepared = None
    for _ in range(repeats):
        started = time.perf_counter()
        prepared = harness.prepare(messages)
        latencies_ms.append((time.perf_counter() - started) * 1000)
    if prepared is None:
        raise RuntimeError("Context benchmark produced no result")

    latest_preserved = any(
        isinstance(message, HumanMessage)
        and "最后一个必须保留的问题" in str(message.content)
        for message in prepared.messages
    )
    before = prepared.report.estimated_tokens_before
    after = prepared.report.estimated_tokens_after
    return {
        "source_messages": len(messages),
        "prepared_messages": len(prepared.messages),
        "estimated_tokens_before": before,
        "estimated_tokens_after": after,
        "token_reduction_percent": round((before - after) / before * 100, 2),
        "within_60k_budget": after <= 60_000,
        "latest_question_preserved": latest_preserved,
        "tool_protocol_valid": tool_protocol_is_valid(prepared.messages),
        "compacted_messages": prepared.report.compacted_messages,
        "truncated_messages": prepared.report.truncated_messages,
        "latency_ms_p50": round(statistics.median(latencies_ms), 2),
        "latency_ms_p95": round(sorted(latencies_ms)[int(0.95 * (repeats - 1))], 2),
    }


def _run_concurrent(session_ids: list[str], delay_seconds: float) -> float:
    harness = ExecutionHarness(
        max_graph_steps=20,
        execution_timeout_seconds=5,
    )
    agent = SleepingAgent(delay_seconds)
    started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=len(session_ids)) as executor:
        futures = [
            executor.submit(
                harness.run,
                session_id=session_id,
                agent=agent,
                messages=[HumanMessage(content="benchmark")],
            )
            for session_id in session_ids
        ]
        for future in futures:
            future.result()
    return time.perf_counter() - started


def benchmark_concurrency(requests: int = 4, delay_seconds: float = 0.1) -> dict:
    same_session_seconds = _run_concurrent(
        ["same-session"] * requests,
        delay_seconds,
    )
    cross_session_seconds = _run_concurrent(
        [f"session-{index}" for index in range(requests)],
        delay_seconds,
    )
    return {
        "requests": requests,
        "synthetic_agent_delay_ms": round(delay_seconds * 1000, 2),
        "same_session_seconds": round(same_session_seconds, 3),
        "cross_session_seconds": round(cross_session_seconds, 3),
        "parallel_speedup": round(same_session_seconds / cross_session_seconds, 2),
        "same_session_serialized": same_session_seconds >= delay_seconds * requests * 0.9,
        "cross_sessions_parallel": cross_session_seconds < same_session_seconds * 0.6,
    }


def main() -> None:
    result = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "context": benchmark_context(),
        "concurrency": benchmark_concurrency(),
    }
    DEFAULT_OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    DEFAULT_OUTPUT.write_text(
        json.dumps(result, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
