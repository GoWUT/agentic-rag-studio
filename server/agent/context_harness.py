from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import math
from typing import Sequence

from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.messages.utils import count_tokens_approximately


TRUNCATION_MARKER = "\n[... truncated by Context Harness ...]\n"
MEMORY_HEADER = "[Context Harness memory: older conversation compacted]"


class ContextBudgetError(RuntimeError):
    """Raised when message protocol overhead cannot fit the input budget."""


@dataclass(frozen=True)
class ContextReport:
    model_context_window_tokens: int
    input_budget_tokens: int
    max_output_tokens: int
    safety_tokens: int
    estimated_tokens_before: int
    estimated_tokens_after: int
    compacted_messages: int
    truncated_messages: int
    strategy: str

    def as_dict(self) -> dict[str, int | str]:
        return asdict(self)


@dataclass(frozen=True)
class PreparedContext:
    messages: list[BaseMessage]
    report: ContextReport


class ContextHarness:
    """Build a bounded model context while preserving canonical history.

    Token counts are conservative estimates. The provider remains the source of
    truth because OpenAI-compatible APIs do not expose their tokenizer locally.
    """

    def __init__(
        self,
        *,
        context_window_tokens: int,
        input_budget_tokens: int,
        max_output_tokens: int,
        safety_tokens: int,
        summary_tokens: int,
        recent_turns: int,
        chars_per_token: float = 2.0,
    ):
        if context_window_tokens <= max_output_tokens + safety_tokens:
            raise ValueError(
                "Context window must exceed output and safety reservations"
            )
        if input_budget_tokens <= 0:
            raise ValueError("Input token budget must be positive")
        if summary_tokens <= 0:
            raise ValueError("Summary token budget must be positive")
        if recent_turns <= 0:
            raise ValueError("Recent turn count must be positive")
        if chars_per_token <= 0:
            raise ValueError("Characters per token must be positive")

        provider_input_limit = (
            context_window_tokens - max_output_tokens - safety_tokens
        )
        self.context_window_tokens = context_window_tokens
        self.input_budget_tokens = min(
            input_budget_tokens,
            provider_input_limit,
        )
        self.max_output_tokens = max_output_tokens
        self.safety_tokens = safety_tokens
        self.summary_tokens = min(
            summary_tokens,
            self.input_budget_tokens // 2,
        )
        self.recent_turns = recent_turns
        self.chars_per_token = chars_per_token
        self.last_report: ContextReport | None = None

    def prepare(self, messages: Sequence[BaseMessage]) -> PreparedContext:
        source = list(messages)
        estimated_before = self._count(source)

        if estimated_before <= self.input_budget_tokens:
            report = self._report(
                before=estimated_before,
                after=estimated_before,
                compacted=0,
                truncated=0,
                strategy="full",
            )
            self.last_report = report
            return PreparedContext(messages=source, report=report)

        system_messages = [
            message for message in source if isinstance(message, SystemMessage)
        ]
        conversation_messages = [
            message
            for message in source
            if not isinstance(message, SystemMessage)
        ]
        turns = _group_turns(conversation_messages)

        recent_start = max(0, len(turns) - self.recent_turns)
        recent_candidates = turns[recent_start:]
        selected_start = len(recent_candidates)
        used_tokens = self._count(system_messages)

        for index in range(len(recent_candidates) - 1, -1, -1):
            turn = recent_candidates[index]
            turn_tokens = self._count(turn)
            has_older_messages = (recent_start + index) > 0
            summary_reserve = (
                min(
                    self.summary_tokens,
                    max(0, (self.input_budget_tokens - used_tokens) // 3),
                )
                if has_older_messages
                else 0
            )
            available = (
                self.input_budget_tokens - used_tokens - summary_reserve
            )

            if turn_tokens <= available:
                selected_start = index
                used_tokens += turn_tokens
                continue

            # The current turn is protocol-critical. Keep its message structure;
            # oversized content is reduced later without deleting tool-call pairs.
            if selected_start == len(recent_candidates):
                selected_start = index
            break

        selected_turns = recent_candidates[selected_start:]
        omitted_turns = turns[: recent_start + selected_start]
        selected_messages = [
            message for turn in selected_turns for message in turn
        ]

        summary_message: SystemMessage | None = None
        remaining_for_summary = max(
            0,
            self.input_budget_tokens
            - self._count([*system_messages, *selected_messages]),
        )
        if omitted_turns and remaining_for_summary > 12:
            summary_message = self._build_memory(
                omitted_turns,
                min(self.summary_tokens, remaining_for_summary),
            )

        prepared_messages = [*system_messages]
        if summary_message is not None:
            prepared_messages.append(summary_message)
        prepared_messages.extend(selected_messages)

        prepared_messages, truncated = self._fit_messages(
            prepared_messages,
            self.input_budget_tokens,
        )
        estimated_after = self._count(prepared_messages)
        compacted = sum(len(turn) for turn in omitted_turns)
        report = self._report(
            before=estimated_before,
            after=estimated_after,
            compacted=compacted,
            truncated=truncated,
            strategy="compacted",
        )
        self.last_report = report
        return PreparedContext(messages=prepared_messages, report=report)

    def _build_memory(
        self,
        turns: Sequence[Sequence[BaseMessage]],
        token_budget: int,
    ) -> SystemMessage | None:
        lines = [MEMORY_HEADER]
        for turn in turns:
            for message in turn:
                text = _content_text(message).strip()
                if not text:
                    continue
                if isinstance(message, HumanMessage):
                    label = "User"
                elif isinstance(message, AIMessage) and not message.tool_calls:
                    label = "Assistant"
                else:
                    # Internal tool chatter is intentionally omitted. Its durable
                    # conclusions should already be present in the final answer.
                    continue
                lines.append(f"- {label}: {_excerpt(text, 360)}")

        if len(lines) == 1:
            return None

        memory = SystemMessage(content="\n".join(lines))
        fitted, _ = self._fit_messages([memory], token_budget)
        return fitted[0] if fitted else None

    def _fit_messages(
        self,
        messages: Sequence[BaseMessage],
        token_budget: int,
    ) -> tuple[list[BaseMessage], int]:
        fitted = list(messages)
        truncated_indexes: set[int] = set()

        for _ in range(max(1, len(fitted) * 3)):
            current_tokens = self._count(fitted)
            if current_tokens <= token_budget:
                return fitted, len(truncated_indexes)

            candidates = [
                (len(_content_text(message)), index)
                for index, message in enumerate(fitted)
                if len(_content_text(message)) > len(TRUNCATION_MARKER) + 12
            ]
            if not candidates:
                break

            _, index = max(candidates)
            message = fitted[index]
            text = _content_text(message)
            excess = current_tokens - token_budget
            content_tokens = math.ceil(len(text) / self.chars_per_token)
            target_tokens = max(24, content_tokens - excess - 4)
            shortened = _truncate_text(
                text,
                target_tokens,
                self.chars_per_token,
            )
            if shortened == text:
                break
            fitted[index] = message.model_copy(update={"content": shortened})
            truncated_indexes.add(index)

        if self._count(fitted) > token_budget:
            raise ContextBudgetError(
                "Message protocol overhead exceeds the configured input budget"
            )
        return fitted, len(truncated_indexes)

    def _count(self, messages: Sequence[BaseMessage]) -> int:
        if not messages:
            return 0
        return count_tokens_approximately(
            messages,
            chars_per_token=self.chars_per_token,
        )

    def _report(
        self,
        *,
        before: int,
        after: int,
        compacted: int,
        truncated: int,
        strategy: str,
    ) -> ContextReport:
        return ContextReport(
            model_context_window_tokens=self.context_window_tokens,
            input_budget_tokens=self.input_budget_tokens,
            max_output_tokens=self.max_output_tokens,
            safety_tokens=self.safety_tokens,
            estimated_tokens_before=before,
            estimated_tokens_after=after,
            compacted_messages=compacted,
            truncated_messages=truncated,
            strategy=strategy,
        )


def _group_turns(messages: Sequence[BaseMessage]) -> list[list[BaseMessage]]:
    turns: list[list[BaseMessage]] = []
    current: list[BaseMessage] = []

    for message in messages:
        if isinstance(message, HumanMessage) and current:
            turns.append(current)
            current = []
        current.append(message)

    if current:
        turns.append(current)
    return turns


def _content_text(message: BaseMessage) -> str:
    if isinstance(message.content, str):
        return message.content
    return json.dumps(message.content, ensure_ascii=False, default=str)


def _excerpt(text: str, max_chars: int) -> str:
    compact = " ".join(text.split())
    if len(compact) <= max_chars:
        return compact
    return compact[: max_chars - 1].rstrip() + "…"


def _truncate_text(
    text: str,
    target_tokens: int,
    chars_per_token: float,
) -> str:
    target_chars = max(
        len(TRUNCATION_MARKER) + 12,
        int(target_tokens * chars_per_token),
    )
    if len(text) <= target_chars:
        return text

    remaining = target_chars - len(TRUNCATION_MARKER)
    prefix_chars = max(6, int(remaining * 0.65))
    suffix_chars = max(6, remaining - prefix_chars)
    return (
        text[:prefix_chars].rstrip()
        + TRUNCATION_MARKER
        + text[-suffix_chars:].lstrip()
    )
