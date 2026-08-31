from __future__ import annotations

from concurrent.futures import (
    ThreadPoolExecutor,
    TimeoutError as FutureTimeoutError,
)
from dataclasses import asdict, dataclass
import time
from threading import Event, Lock
from typing import Any, Mapping, Protocol, Sequence
import uuid

from langchain_core.messages import BaseMessage
from langgraph.errors import GraphRecursionError
from openai import (
    APIConnectionError,
    AuthenticationError,
    BadRequestError,
    InternalServerError,
    NotFoundError,
    PermissionDeniedError,
    RateLimitError,
)


class AgentInvoker(Protocol):
    def invoke(
        self,
        inputs: Mapping[str, Any],
        config: Mapping[str, Any],
    ) -> Mapping[str, Any]: ...


class ExecutionLimitError(RuntimeError):
    """Raised when an Agent exceeds its configured graph-step budget."""


class ExecutionUnavailableError(RuntimeError):
    """Raised when a transient upstream failure exhausts its retry budget."""


class ExecutionConfigurationError(RuntimeError):
    """Raised when credentials or provider configuration are invalid."""


class ExecutionTimeoutError(RuntimeError):
    """Raised when an Agent run exceeds its total elapsed-time budget."""


@dataclass(frozen=True)
class ExecutionReport:
    run_id: str
    attempts: int
    duration_ms: float
    max_graph_steps: int

    def as_dict(self) -> dict[str, str | int | float]:
        return asdict(self)


@dataclass(frozen=True)
class ExecutionResult:
    output: Mapping[str, Any]
    report: ExecutionReport


class ExecutionHarness:
    """Execute an Agent under explicit operational limits."""

    def __init__(
        self,
        *,
        max_graph_steps: int,
        max_attempts: int = 1,
        retry_base_seconds: float = 0.25,
        execution_timeout_seconds: float = 120,
    ):
        if max_graph_steps <= 0:
            raise ValueError("Maximum graph steps must be positive")
        if max_attempts <= 0:
            raise ValueError("Maximum attempts must be positive")
        if retry_base_seconds < 0:
            raise ValueError("Retry delay cannot be negative")
        if execution_timeout_seconds <= 0:
            raise ValueError("Execution timeout must be positive")
        self.max_graph_steps = max_graph_steps
        self.max_attempts = max_attempts
        self.retry_base_seconds = retry_base_seconds
        self.execution_timeout_seconds = execution_timeout_seconds
        self._session_locks: dict[str, Lock] = {}
        self._session_locks_guard = Lock()
        self._executor = ThreadPoolExecutor(
            max_workers=8,
            thread_name_prefix="agent-execution",
        )

    def run(
        self,
        *,
        session_id: str,
        agent: AgentInvoker,
        messages: Sequence[BaseMessage],
    ) -> ExecutionResult:
        if not session_id:
            raise ValueError("Session ID is required")

        run_id = str(uuid.uuid4())
        started = time.perf_counter()
        cancelled = Event()
        with self._session_locks_guard:
            session_lock = self._session_locks.setdefault(
                session_id,
                Lock(),
            )

        future = self._executor.submit(
            self._run_for_session,
            session_lock,
            agent,
            list(messages),
            cancelled,
        )
        try:
            output, attempts = future.result(
                timeout=self.execution_timeout_seconds
            )
        except FutureTimeoutError as error:
            cancelled.set()
            future.cancel()
            raise ExecutionTimeoutError(
                "Agent exceeded the total execution timeout of "
                f"{self.execution_timeout_seconds:g} seconds"
            ) from error

        return ExecutionResult(
            output=output,
            report=ExecutionReport(
                run_id=run_id,
                attempts=attempts,
                duration_ms=round(
                    (time.perf_counter() - started) * 1000,
                    2,
                ),
                max_graph_steps=self.max_graph_steps,
            ),
        )

    def _run_for_session(
        self,
        session_lock: Lock,
        agent: AgentInvoker,
        messages: Sequence[BaseMessage],
        cancelled: Event,
    ) -> tuple[Mapping[str, Any], int]:
        with session_lock:
            if cancelled.is_set():
                raise ExecutionTimeoutError(
                    "Agent execution was cancelled before it started"
                )
            return self._run_locked(agent, messages, cancelled)

    def _run_locked(
        self,
        agent: AgentInvoker,
        messages: Sequence[BaseMessage],
        cancelled: Event,
    ) -> tuple[Mapping[str, Any], int]:
        for attempt in range(1, self.max_attempts + 1):
            if cancelled.is_set():
                raise ExecutionTimeoutError(
                    "Agent execution was cancelled after its timeout"
                )
            try:
                output = agent.invoke(
                    {"messages": list(messages)},
                    {"recursion_limit": self.max_graph_steps},
                )
                return output, attempt
            except GraphRecursionError as error:
                raise ExecutionLimitError(
                    "Agent exceeded the execution budget of "
                    f"{self.max_graph_steps} graph steps"
                ) from error
            except (
                AuthenticationError,
                BadRequestError,
                NotFoundError,
                PermissionDeniedError,
            ) as error:
                raise ExecutionConfigurationError(
                    "Agent provider rejected its configuration"
                ) from error
            except (
                APIConnectionError,
                InternalServerError,
                RateLimitError,
            ) as error:
                if cancelled.is_set():
                    raise ExecutionTimeoutError(
                        "Agent execution was cancelled after its timeout"
                    ) from error
                if attempt >= self.max_attempts:
                    raise ExecutionUnavailableError(
                        "Agent upstream remained unavailable after "
                        f"{self.max_attempts} attempts"
                    ) from error
                delay = self.retry_base_seconds * (2 ** (attempt - 1))
                if delay:
                    time.sleep(delay)

        raise RuntimeError("Execution attempt loop ended unexpectedly")
