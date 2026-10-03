import unittest
from threading import Barrier, Event, Lock, Thread

import httpx
from langchain_core.messages import HumanMessage
from langgraph.errors import GraphRecursionError
from openai import (
    APIConnectionError,
    AuthenticationError,
    BadRequestError,
    InternalServerError,
    RateLimitError,
)

from server.agent.execution_harness import (
    ExecutionConfigurationError,
    ExecutionHarness,
    ExecutionLimitError,
    ExecutionTimeoutError,
    ExecutionUnavailableError,
)


class EndlessAgent:
    def invoke(self, inputs, config):
        raise GraphRecursionError("recursion limit reached")


class RecoveringAgent:
    def __init__(self):
        self.failed_once = False

    def invoke(self, inputs, config):
        if not self.failed_once:
            self.failed_once = True
            raise APIConnectionError(
                request=httpx.Request(
                    "POST",
                    "https://api.deepseek.com/chat/completions",
                )
            )
        return {"messages": [HumanMessage(content="recovered")]}


class OfflineAgent:
    def invoke(self, inputs, config):
        raise APIConnectionError(
            request=httpx.Request(
                "POST",
                "https://api.deepseek.com/chat/completions",
            )
        )


class AuthenticationThenSuccessAgent:
    def __init__(self):
        self.failed_once = False

    def invoke(self, inputs, config):
        if not self.failed_once:
            self.failed_once = True
            request = httpx.Request(
                "POST",
                "https://api.deepseek.com/chat/completions",
            )
            raise AuthenticationError(
                "invalid API key",
                response=httpx.Response(401, request=request),
                body={"error": "invalid API key"},
            )
        return {"messages": [HumanMessage(content="should not retry")]}


class BadRequestThenSuccessAgent:
    def __init__(self):
        self.failed_once = False

    def invoke(self, inputs, config):
        if not self.failed_once:
            self.failed_once = True
            request = httpx.Request(
                "POST",
                "https://api.deepseek.com/chat/completions",
            )
            raise BadRequestError(
                "invalid model",
                response=httpx.Response(400, request=request),
                body={"error": "invalid model"},
            )
        return {"messages": [HumanMessage(content="should not retry")]}


class RateLimitedThenSuccessAgent:
    def __init__(self):
        self.failed_once = False

    def invoke(self, inputs, config):
        if not self.failed_once:
            self.failed_once = True
            request = httpx.Request(
                "POST",
                "https://api.deepseek.com/chat/completions",
            )
            raise RateLimitError(
                "rate limited",
                response=httpx.Response(429, request=request),
                body={"error": "rate limited"},
            )
        return {"messages": [HumanMessage(content="rate limit recovered")]}


class ServerErrorThenSuccessAgent:
    def __init__(self):
        self.failed_once = False

    def invoke(self, inputs, config):
        if not self.failed_once:
            self.failed_once = True
            request = httpx.Request(
                "POST",
                "https://api.deepseek.com/chat/completions",
            )
            raise InternalServerError(
                "provider error",
                response=httpx.Response(500, request=request),
                body={"error": "provider error"},
            )
        return {"messages": [HumanMessage(content="server recovered")]}


class ConcurrentEntryProbeAgent:
    def __init__(self):
        self._state_lock = Lock()
        self._entries = 0
        self.first_entered = Event()
        self.second_entered = Event()
        self.release_first = Event()

    def invoke(self, inputs, config):
        with self._state_lock:
            self._entries += 1
            entry = self._entries

        if entry == 1:
            self.first_entered.set()
            self.release_first.wait(timeout=2)
        else:
            self.second_entered.set()

        return {"messages": [HumanMessage(content=f"entry-{entry}")]}


class BlockingAgent:
    def __init__(self):
        self.started = Event()
        self.release = Event()

    def invoke(self, inputs, config):
        self.started.set()
        self.release.wait(timeout=2)
        return {"messages": [HumanMessage(content="too late")]}


class LateFailureAgent:
    def __init__(self):
        self.first_started = Event()
        self.release_first = Event()
        self.second_attempted = Event()
        self._attempts = 0

    def invoke(self, inputs, config):
        self._attempts += 1
        if self._attempts == 1:
            self.first_started.set()
            self.release_first.wait(timeout=2)
            raise APIConnectionError(
                request=httpx.Request(
                    "POST",
                    "https://api.deepseek.com/chat/completions",
                )
            )
        self.second_attempted.set()
        return {"messages": [HumanMessage(content="late retry")]}


class ParallelSessionsAgent:
    def __init__(self):
        self.rendezvous = Barrier(2)

    def invoke(self, inputs, config):
        self.rendezvous.wait(timeout=1)
        return {"messages": [HumanMessage(content="parallel")]}


class ExecutionHarnessTest(unittest.TestCase):
    def test_runaway_agent_becomes_an_explicit_limit_error(self):
        harness = ExecutionHarness(max_graph_steps=6)

        with self.assertRaises(ExecutionLimitError) as captured:
            harness.run(
                session_id="session-1",
                agent=EndlessAgent(),
                messages=[HumanMessage(content="keep using tools forever")],
            )

        self.assertIn("6", str(captured.exception))

    def test_transient_connection_failure_can_recover(self):
        harness = ExecutionHarness(
            max_graph_steps=6,
            max_attempts=2,
            retry_base_seconds=0,
        )

        result = harness.run(
            session_id="session-1",
            agent=RecoveringAgent(),
            messages=[HumanMessage(content="question")],
        )

        self.assertEqual(result.output["messages"][-1].content, "recovered")
        self.assertEqual(result.report.attempts, 2)
        self.assertEqual(result.report.max_graph_steps, 6)
        self.assertTrue(result.report.run_id)

    def test_exhausted_connection_retries_become_unavailable(self):
        harness = ExecutionHarness(
            max_graph_steps=6,
            max_attempts=2,
            retry_base_seconds=0,
        )

        with self.assertRaises(ExecutionUnavailableError):
            harness.run(
                session_id="session-1",
                agent=OfflineAgent(),
                messages=[HumanMessage(content="question")],
            )

    def test_authentication_failure_is_not_retried(self):
        harness = ExecutionHarness(
            max_graph_steps=6,
            max_attempts=3,
            retry_base_seconds=0,
        )

        with self.assertRaises(ExecutionConfigurationError):
            harness.run(
                session_id="session-1",
                agent=AuthenticationThenSuccessAgent(),
                messages=[HumanMessage(content="question")],
            )

    def test_bad_provider_request_is_not_retried(self):
        harness = ExecutionHarness(
            max_graph_steps=6,
            max_attempts=3,
            retry_base_seconds=0,
        )

        with self.assertRaises(ExecutionConfigurationError):
            harness.run(
                session_id="session-1",
                agent=BadRequestThenSuccessAgent(),
                messages=[HumanMessage(content="question")],
            )

    def test_rate_limit_can_recover_within_retry_budget(self):
        harness = ExecutionHarness(
            max_graph_steps=6,
            max_attempts=2,
            retry_base_seconds=0,
        )

        result = harness.run(
            session_id="session-1",
            agent=RateLimitedThenSuccessAgent(),
            messages=[HumanMessage(content="question")],
        )

        self.assertEqual(
            result.output["messages"][-1].content,
            "rate limit recovered",
        )

    def test_provider_server_error_can_recover_within_retry_budget(self):
        harness = ExecutionHarness(
            max_graph_steps=6,
            max_attempts=2,
            retry_base_seconds=0,
        )

        result = harness.run(
            session_id="session-1",
            agent=ServerErrorThenSuccessAgent(),
            messages=[HumanMessage(content="question")],
        )

        self.assertEqual(
            result.output["messages"][-1].content,
            "server recovered",
        )

    def test_same_session_executions_do_not_overlap(self):
        harness = ExecutionHarness(max_graph_steps=6)
        agent = ConcurrentEntryProbeAgent()

        def execute():
            harness.run(
                session_id="same-session",
                agent=agent,
                messages=[HumanMessage(content="question")],
            )

        first = Thread(target=execute)
        second = Thread(target=execute)
        first.start()
        self.assertTrue(agent.first_entered.wait(timeout=1))
        second.start()

        try:
            overlapped = agent.second_entered.wait(timeout=0.2)
        finally:
            agent.release_first.set()
            first.join(timeout=2)
            second.join(timeout=2)

        self.assertFalse(overlapped)

    def test_blocked_agent_exceeds_total_execution_timeout(self):
        harness = ExecutionHarness(
            max_graph_steps=6,
            execution_timeout_seconds=0.05,
        )
        agent = BlockingAgent()

        try:
            with self.assertRaises(ExecutionTimeoutError):
                harness.run(
                    session_id="session-1",
                    agent=agent,
                    messages=[HumanMessage(content="question")],
                )
        finally:
            agent.release.set()

    def test_timeout_prevents_a_late_retry_from_starting(self):
        harness = ExecutionHarness(
            max_graph_steps=6,
            max_attempts=2,
            retry_base_seconds=0,
            execution_timeout_seconds=0.05,
        )
        agent = LateFailureAgent()

        with self.assertRaises(ExecutionTimeoutError):
            harness.run(
                session_id="session-1",
                agent=agent,
                messages=[HumanMessage(content="question")],
            )

        agent.release_first.set()
        self.assertFalse(agent.second_attempted.wait(timeout=0.2))

    def test_different_sessions_can_execute_in_parallel(self):
        harness = ExecutionHarness(
            max_graph_steps=6,
            execution_timeout_seconds=2,
        )
        agent = ParallelSessionsAgent()
        results = []

        def execute(session_id):
            results.append(
                harness.run(
                    session_id=session_id,
                    agent=agent,
                    messages=[HumanMessage(content="question")],
                )
            )

        first = Thread(target=execute, args=("session-a",))
        second = Thread(target=execute, args=("session-b",))
        first.start()
        second.start()
        first.join(timeout=2)
        second.join(timeout=2)

        self.assertEqual(len(results), 2)


if __name__ == "__main__":
    unittest.main()
