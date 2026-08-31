from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from threading import RLock
from typing import Any, BinaryIO, Iterator, Mapping, Sequence
import uuid

from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    messages_from_dict,
    messages_to_dict,
)

from server.agent.context_harness import ContextHarness, ContextReport
from server.agent.execution_harness import (
    ExecutionHarness,
    ExecutionReport,
)
from server.agent.graph import build_agent
from server.agent.tools import build_tools
from server.rag.embeddings import get_embedder
from server.rag.ingestion import (
    ChromaIndexAdapter,
    DocumentIngestionPipeline,
    IndexStatus,
)
from server.rag.vectorstore import load_vectorstore


LEGACY_EMBEDDING_MODEL = "sentence-transformers/all-MiniLM-L6-v2"


class SessionNotFoundError(KeyError):
    """Raised when a requested persisted session does not exist."""


class SessionUnavailableError(RuntimeError):
    """Raised when session metadata exists but its index cannot be restored."""


@dataclass(frozen=True)
class AgentReply:
    answer: str
    context: ContextReport
    execution: ExecutionReport


@dataclass(frozen=True)
class SessionRecord:
    session_id: str
    file_id: str
    file_name: str
    pdf_path: str
    chroma_dir: str
    embedding_model: str
    created_at: str
    updated_at: str
    turn_count: int = 0

    def public_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data.pop("pdf_path")
        data.pop("chroma_dir")
        return data


class SessionStore:
    """Persist session metadata and LangChain messages in SQLite."""

    def __init__(self, database_path: str | Path):
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.database_path, timeout=30)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    session_id TEXT PRIMARY KEY,
                    file_id TEXT NOT NULL,
                    file_name TEXT NOT NULL,
                    pdf_path TEXT NOT NULL,
                    chroma_dir TEXT NOT NULL,
                    embedding_model TEXT NOT NULL,
                    messages_json TEXT NOT NULL DEFAULT '[]',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            columns = {
                row["name"]
                for row in connection.execute(
                    "PRAGMA table_info(sessions)"
                ).fetchall()
            }
            if "embedding_model" not in columns:
                connection.execute(
                    "ALTER TABLE sessions ADD COLUMN embedding_model "
                    f"TEXT NOT NULL DEFAULT '{LEGACY_EMBEDDING_MODEL}'"
                )

    def create(
        self,
        *,
        session_id: str,
        file_id: str,
        file_name: str,
        pdf_path: str,
        chroma_dir: str,
        embedding_model: str = LEGACY_EMBEDDING_MODEL,
    ) -> SessionRecord:
        now = _utc_now()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO sessions (
                    session_id,
                    file_id,
                    file_name,
                    pdf_path,
                    chroma_dir,
                    embedding_model,
                    messages_json,
                    created_at,
                    updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, '[]', ?, ?)
                """,
                (
                    session_id,
                    file_id,
                    file_name,
                    pdf_path,
                    chroma_dir,
                    embedding_model,
                    now,
                    now,
                ),
            )

        record = self.get(session_id)
        if record is None:  # pragma: no cover - SQLite insert contract
            raise RuntimeError("Session was not persisted")
        return record

    def get(self, session_id: str) -> SessionRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()

        if row is None:
            return None
        return _record_from_row(row)

    def list(self) -> list[SessionRecord]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM sessions ORDER BY updated_at DESC"
            ).fetchall()
        return [_record_from_row(row) for row in rows]

    def load_messages(self, session_id: str) -> list[BaseMessage]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT messages_json FROM sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()

        if row is None:
            raise SessionNotFoundError(session_id)

        payload = json.loads(row["messages_json"])
        return list(messages_from_dict(payload))

    def save_messages(
        self,
        session_id: str,
        messages: Sequence[BaseMessage],
    ) -> None:
        payload = json.dumps(
            messages_to_dict(list(messages)),
            ensure_ascii=False,
        )
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE sessions
                SET messages_json = ?, updated_at = ?
                WHERE session_id = ?
                """,
                (payload, _utc_now(), session_id),
            )

        if cursor.rowcount == 0:
            raise SessionNotFoundError(session_id)


class AgentSessionManager:
    """Own persistent conversations and lazily restore their Agent runtime."""

    def __init__(
        self,
        config: Mapping[str, Any],
        store: SessionStore | None = None,
        ingestion_pipeline: DocumentIngestionPipeline | None = None,
    ):
        self.config = config
        self.workspace = Path(config["WORKSPACE_DIR"]).resolve()
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.store = store or SessionStore(
            self.workspace / "sessions.sqlite3"
        )
        self.ingestion_pipeline = (
            ingestion_pipeline
            or DocumentIngestionPipeline(
                workspace=self.workspace,
                max_upload_bytes=config.get(
                    "MAX_PDF_UPLOAD_BYTES",
                    25 * 1024 * 1024,
                ),
                index_adapter=ChromaIndexAdapter(
                    config["EMBEDDING_MODEL"]
                ),
            )
        )
        self._runtimes: dict[str, dict[str, Any]] = {}
        self._lock = RLock()
        self._session_locks: dict[str, RLock] = {}
        self.execution_harness = ExecutionHarness(
            max_graph_steps=config.get("AGENT_MAX_GRAPH_STEPS", 20),
            max_attempts=config.get("AGENT_MAX_ATTEMPTS", 2),
            retry_base_seconds=config.get(
                "AGENT_RETRY_BASE_SECONDS",
                0.5,
            ),
            execution_timeout_seconds=config.get(
                "AGENT_EXECUTION_TIMEOUT_SECONDS",
                120,
            ),
        )

    def create_session(
        self,
        file_name: str,
        file_stream: BinaryIO,
    ) -> dict[str, Any]:
        result = self.ingestion_pipeline.ingest(
            file_name=file_name,
            stream=file_stream,
        )
        session_id = str(uuid.uuid4())
        runtime = self._make_runtime(result.vectorstore, messages=[])
        record = self.store.create(
            session_id=session_id,
            file_id=result.file_id,
            file_name=result.file_name,
            pdf_path=str(result.pdf_path),
            chroma_dir=str(result.index_dir),
            embedding_model=self.config["EMBEDDING_MODEL"],
        )
        with self._lock:
            self._runtimes[session_id] = runtime

        response = record.public_dict()
        response.update(
            {
                "index_status": result.status,
                "index_reused": result.reused,
                "size_bytes": result.size_bytes,
            }
        )
        return response

    def get_index_status(self, file_id: str) -> IndexStatus | None:
        return self.ingestion_pipeline.get_status(file_id)

    def list_sessions(self) -> list[dict[str, Any]]:
        return [record.public_dict() for record in self.store.list()]

    def get_history(self, session_id: str) -> list[dict[str, str]]:
        messages = self.store.load_messages(session_id)
        history: list[dict[str, str]] = []

        for message in messages:
            if isinstance(message, HumanMessage):
                history.append(
                    {"role": "user", "content": _message_text(message)}
                )
            elif (
                isinstance(message, AIMessage)
                and not message.tool_calls
                and _message_text(message).strip()
            ):
                history.append(
                    {
                        "role": "assistant",
                        "content": _message_text(message),
                    }
                )

        return history

    def ask(self, session_id: str, message: str) -> AgentReply:
        question = message.strip()
        if not question:
            raise ValueError("Message cannot be empty")

        session_lock = self._get_session_lock(session_id)
        with session_lock:
            runtime = self._get_or_restore_runtime(session_id)
            messages = [
                *runtime["messages"],
                HumanMessage(content=question),
            ]

            execution_result = self.execution_harness.run(
                session_id=session_id,
                agent=runtime["agent"],
                messages=messages,
            )
            result = execution_result.output
            updated_messages = list(result["messages"])
            self.store.save_messages(session_id, updated_messages)
            runtime["messages"] = updated_messages

            last_ai = next(
                (
                    item
                    for item in reversed(updated_messages)
                    if isinstance(item, AIMessage)
                    and not item.tool_calls
                    and _message_text(item).strip()
                ),
                None,
            )
            if last_ai is None:
                raise RuntimeError("Agent did not return a final answer")
            context_report = runtime["context_harness"].last_report
            if context_report is None:  # pragma: no cover - agent contract
                raise RuntimeError("Agent did not produce a context report")
            return AgentReply(
                answer=_message_text(last_ai),
                context=context_report,
                execution=execution_result.report,
            )

    def _get_session_lock(self, session_id: str) -> RLock:
        with self._lock:
            return self._session_locks.setdefault(session_id, RLock())

    def _get_or_restore_runtime(self, session_id: str) -> dict[str, Any]:
        with self._lock:
            runtime = self._runtimes.get(session_id)
        if runtime is not None:
            return runtime

        record = self.store.get(session_id)
        if record is None:
            raise SessionNotFoundError(session_id)

        try:
            embedder = get_embedder(record.embedding_model)
            vectordb = load_vectorstore(embedder, record.chroma_dir)
        except (FileNotFoundError, OSError) as error:
            raise SessionUnavailableError(
                "The PDF index for this session is unavailable"
            ) from error

        runtime = self._make_runtime(
            vectordb,
            messages=self.store.load_messages(session_id),
        )
        with self._lock:
            self._runtimes[session_id] = runtime
        return runtime

    def _make_runtime(
        self,
        vectordb,
        *,
        messages: list[BaseMessage],
    ) -> dict[str, Any]:
        retriever = vectordb.as_retriever(search_kwargs={"k": 5})
        tools = build_tools(retriever, self.config["SERPER_API_KEY"])
        context_harness = ContextHarness(
            context_window_tokens=self.config[
                "MODEL_CONTEXT_WINDOW_TOKENS"
            ],
            input_budget_tokens=self.config[
                "CONTEXT_INPUT_BUDGET_TOKENS"
            ],
            max_output_tokens=self.config["MAX_OUTPUT_TOKENS"],
            safety_tokens=self.config["CONTEXT_SAFETY_TOKENS"],
            summary_tokens=self.config["CONTEXT_SUMMARY_TOKENS"],
            recent_turns=self.config["CONTEXT_RECENT_TURNS"],
        )
        agent = build_agent(
            self.config["MODEL_NAME"],
            self.config["DEEPSEEK_API_KEY"],
            tools,
            context_harness=context_harness,
            max_output_tokens=self.config["MAX_OUTPUT_TOKENS"],
            request_timeout_seconds=self.config.get(
                "MODEL_REQUEST_TIMEOUT_SECONDS",
                60,
            ),
        )
        return {
            "agent": agent,
            "messages": messages,
            "context_harness": context_harness,
        }


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _record_from_row(row: sqlite3.Row) -> SessionRecord:
    raw_messages = json.loads(row["messages_json"])
    turn_count = sum(
        1 for message in raw_messages if message.get("type") == "human"
    )
    return SessionRecord(
        session_id=row["session_id"],
        file_id=row["file_id"],
        file_name=row["file_name"],
        pdf_path=row["pdf_path"],
        chroma_dir=row["chroma_dir"],
        embedding_model=row["embedding_model"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        turn_count=turn_count,
    )


def _message_text(message: BaseMessage) -> str:
    if isinstance(message.content, str):
        return message.content
    return json.dumps(message.content, ensure_ascii=False)
