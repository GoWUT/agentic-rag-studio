from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import json
import logging
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
from server.rag.ocr import OCRSettings
from server.rag.ingestion import (
    ChromaIndexAdapter,
    DocumentIngestionPipeline,
    IndexStatus,
)
from server.rag.vectorstore import load_vectorstore
from server.workspaces import DocumentRecord, WorkspaceStore
from server.auth.context import principal_service


LEGACY_EMBEDDING_MODEL = "sentence-transformers/all-MiniLM-L6-v2"
LOGGER = logging.getLogger(__name__)


class SessionNotFoundError(KeyError):
    """Raised when a requested persisted session does not exist."""


class SessionUnavailableError(RuntimeError):
    """Raised when session metadata exists but its index cannot be restored."""


@dataclass(frozen=True)
class AgentReply:
    answer: str
    context: ContextReport
    execution: ExecutionReport
    citations: list[dict[str, Any]] = field(default_factory=list)
    evidence: list[dict[str, Any]] = field(default_factory=list)
    plan: dict[str, Any] | None = None
    trace_summary: dict[str, Any] = field(default_factory=dict)
    artifacts: list[dict[str, Any]] = field(default_factory=list)


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
    workspace_id: str | None = None
    created_by_user_id: str | None = None

    def public_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data.pop("pdf_path")
        data.pop("chroma_dir")
        return data


from server.persistence import Database


class SessionStore(Database):
    """Persist session metadata and LangChain messages in SQLite."""

    def __init__(self, database_path: str | Path):
        super().__init__(database_path)
        self.database_path = self.path
        if self.backend == 'sqlite':
            self._initialize()

    def _connect(self):
        return self.connect()

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
            if "workspace_id" not in columns:
                connection.execute("ALTER TABLE sessions ADD COLUMN workspace_id TEXT")

    def create(
        self,
        *,
        session_id: str,
        file_id: str,
        file_name: str,
        pdf_path: str,
        chroma_dir: str,
        embedding_model: str = LEGACY_EMBEDDING_MODEL,
        workspace_id: str | None = None,
        created_by_user_id: str | None = None,
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

            if workspace_id is not None:
                connection.execute("UPDATE sessions SET workspace_id=? WHERE session_id=?", (workspace_id, session_id))
            if created_by_user_id is not None:
                connection.execute("UPDATE sessions SET created_by_user_id=? WHERE session_id=?", (created_by_user_id, session_id))

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
        self.database = None
        if config.get('DATABASE_BACKEND', 'sqlite') == 'postgresql':
            from server.db.runtime import PostgresRuntime
            self.database = PostgresRuntime(config)
        source = self.database or (store.database_path if store is not None else self.workspace / 'sessions.sqlite3')
        from server.db.repositories import RepositoryFactory
        self.repositories = RepositoryFactory(source)
        self.store = store or self.repositories.build(SessionStore)
        self.ingestion_pipeline = (
            ingestion_pipeline
            or DocumentIngestionPipeline(
                workspace=self.workspace,
                persistence=self.database,
                max_upload_bytes=config.get(
                    "MAX_PDF_UPLOAD_BYTES",
                    25 * 1024 * 1024,
                ),
                index_adapter=ChromaIndexAdapter(
                    config["EMBEDDING_MODEL"],
                    ocr_settings=OCRSettings.from_config(config),
                ),
            )
        )
        self._runtimes: dict[str, dict[str, Any]] = {}
        self.workspaces = self.repositories.build(WorkspaceStore, config.get("WORKSPACE_MAX_DOCUMENTS", 20))
        self._lock = RLock()
        self._session_locks: dict[str, RLock] = {}
        from server.phase3 import Phase3Services
        self.phase3 = Phase3Services(self)
        self.phase4 = None
        if config.get('TOOL_REGISTRY_ENABLED', True):
            from server.phase4 import Phase4Services
            self.phase4 = Phase4Services(self)
            self.phase3.phase4 = self.phase4
        self.phase5 = None
        if self.phase4:
            from server.phase5 import Phase5Services
            self.phase5 = Phase5Services(self)
            self.phase3.phase5 = self.phase5
        self.security = None
        if config.get('AUTH_ENABLED', False):
            from server.auth.scoped import configure_security
            configure_security(self)
        self.execution_harness = ExecutionHarness(
            max_graph_steps=config.get("AGENT_MAX_GRAPH_STEPS", 35),
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

    def get_history(self, session_id: str) -> list[dict[str, Any]]:
        messages = self.store.load_messages(session_id)
        history: list[dict[str, Any]] = []

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
                if message.additional_kwargs.get("research"):
                    history[-1]["research"] = message.additional_kwargs["research"]

        return history

    def create_workspace_session(self, workspace_id: str) -> dict[str, Any]:
        workspace = self.workspaces.get(workspace_id)
        record = self.store.create(session_id=str(uuid.uuid4()), file_id="", file_name=workspace.name,
                                   pdf_path="", chroma_dir="", embedding_model=self.config["EMBEDDING_MODEL"],
                                   workspace_id=workspace_id)
        return record.public_dict()

    @principal_service
    def add_workspace_document(self, workspace_id: str, file_name: str, stream: BinaryIO) -> dict[str, Any]:
        from pypdf import PdfReader
        if self.security:
            from server.auth.context import require_principal
            self.security.authorize(require_principal(),'document.create',workspace_id)
        self.workspaces.get(workspace_id)
        result = self.ingestion_pipeline.ingest(file_name=file_name, stream=stream)
        page_count = len(PdfReader(str(result.pdf_path)).pages)
        record = DocumentRecord(id=str(uuid.uuid4()), workspace_id=workspace_id,
                                filename=result.file_name, display_name=result.file_name,
                                fingerprint=result.file_id, index_id=str(result.index_dir),
                                status=result.status, page_count=page_count, created_at=_utc_now(),
                                metadata={"pdf_path": str(result.pdf_path), "embedding_model": self.config["EMBEDDING_MODEL"]})
        registered = self.workspaces.register(record).model_dump()
        registered.update(index_reused=result.reused, size_bytes=result.size_bytes)
        return registered

    @principal_service
    def ask(self, session_id: str, message: str, *, workspace_id: str | None = None,
            source_scope: str = "workspace_and_external", runtime_context: Mapping[str, Any] | None = None) -> AgentReply:
        if source_scope not in {"workspace_only", "workspace_and_external", "external"}:
            raise ValueError("Invalid source scope")
        question = message.strip()
        if not question:
            raise ValueError("Message cannot be empty")

        session_lock = self._get_session_lock(session_id)
        with session_lock:
            record = self.store.get(session_id)
            if record is None:
                raise SessionNotFoundError(session_id)
            if workspace_id is not None and workspace_id != record.workspace_id:
                raise ValueError("Workspace does not match this session; create a workspace session first")
            if record.workspace_id:
                self.workspaces.get(record.workspace_id)
            runtime = self._get_or_restore_runtime(session_id)
            messages = [
                *runtime["messages"],
                HumanMessage(content=question),
            ]

            execution_result = self.execution_harness.run(
                session_id=session_id,
                agent=_ScopedAgent(runtime["agent"], {**self._runtime_scope(record, source_scope), **(runtime_context or {})}, self.phase4),
                messages=messages,
            )
            result = execution_result.output
            if result.get('__interrupt__'):
                from types import SimpleNamespace
                # An interrupt is a successful pause, not a missing-answer failure.
                return SimpleNamespace(answer='Task WAITING_USER: approval required',
                    trace_summary={'approval_pending': True}, execution=execution_result.report)
            updated_messages = list(result["messages"])
            if updated_messages and isinstance(updated_messages[-1], AIMessage) and "trace_summary" in result:
                updated_messages[-1].additional_kwargs["research"] = {
                    "citations": result.get("citations", []), "evidence": result.get("evidence", []),
                    "plan": result.get("task_plan"), "trace_summary": result.get("trace_summary", {})}
                updated_messages[-1].additional_kwargs["research"]["artifacts"] = result.get("artifacts", [])
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
                citations=result.get("citations", []),
                evidence=result.get("evidence", []),
                plan=result.get("task_plan"),
                trace_summary={**result.get("trace_summary", {}), "latency": execution_result.report.duration_ms},
                artifacts=result.get("artifacts", []),
            )

    def _runtime_scope(self, record: SessionRecord, source_scope: str) -> dict[str, Any]:
        from pypdf import PdfReader
        scope: dict[str, Any] = {"workspace_id": record.workspace_id, "source_scope": source_scope,
                                 "session_id": record.session_id, "owner_id": record.created_by_user_id or "local_default"}
        if record.workspace_id:
            scope["document_registry"] = {document.id: document.page_count for document in self.workspaces.documents(record.workspace_id)}
        elif record.pdf_path and Path(record.pdf_path).is_file():
            page_count = len(PdfReader(record.pdf_path).pages)
            scope["document_registry"] = {record.file_id: page_count}
            scope["document_metadata"] = {"document_id": record.file_id, "document_name": record.file_name, "page_count": page_count}
        return scope

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

        if record.workspace_id:
            self.workspaces.get(record.workspace_id)
            runtime = self._make_runtime(None, messages=self.store.load_messages(session_id), workspace_id=record.workspace_id)
            with self._lock:
                self._runtimes[session_id] = runtime
            return runtime

        try:
            embedder = get_embedder(record.embedding_model)
            vectordb = load_vectorstore(embedder, record.chroma_dir)
        except Exception as error:
            LOGGER.warning("Session index restoration failed session_id=%s error_type=%s", session_id, type(error).__name__)
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
        workspace_id: str | None = None,
    ) -> dict[str, Any]:
        from server.rag.retrieval import build_retriever

        retriever = build_retriever(vectordb, self.config) if vectordb is not None else None
        workspace_retriever = None
        if workspace_id:
            from server.rag.workspace_retrieval import WorkspaceRetriever
            workspace_retriever = WorkspaceRetriever(self.workspaces, workspace_id, self.config,
                lambda document: load_vectorstore(get_embedder(document.metadata["embedding_model"]), document.index_id))
        elif retriever:
            retriever = _DocumentRetriever(retriever, vectordb)
        tools = build_tools(retriever, self.config["SERPER_API_KEY"], workspace_retriever=workspace_retriever, structured=True)
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
            max_retrieval_retries=self.config.get(
                "AGENT_MAX_RETRIEVAL_RETRIES",
                1,
            ),
            model_trust_env=self.config.get("MODEL_TRUST_ENV", True),
            workflow_config=self.config,
            services=self.phase3,
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
        workspace_id=row["workspace_id"],
        created_by_user_id=row['created_by_user_id'] if 'created_by_user_id' in row.keys() else None,
    )


def _message_text(message: BaseMessage) -> str:
    if isinstance(message.content, str):
        return message.content
    return json.dumps(message.content, ensure_ascii=False)


class _ScopedAgent:
    """Inject trusted runtime scope without altering the execution harness contract."""
    def __init__(self, agent: Any, scope: Mapping[str, Any], phase4=None):
        self.agent, self.scope, self.phase4 = agent, scope, phase4

    def invoke(self, inputs: Mapping[str, Any], config: Mapping[str, Any]) -> Mapping[str, Any]:
        metadata = {**config.get("metadata", {}), "workspace_id": self.scope.get("workspace_id"),
                    "task_id": self.scope.get('task_id'),
                    "document_count": len(self.scope.get("document_registry", {})), "source_scope": self.scope.get("source_scope")}
        from opentelemetry import trace
        span=trace.get_current_span().get_span_context()
        if span.is_valid:metadata['otel_trace_id']=format(span.trace_id,'032x')
        from server.auth.context import request_id_context
        if request_id_context.get():metadata['request_id']=request_id_context.get()
        graph_config = {**config, 'metadata': metadata}
        phase5 = getattr(self.agent, 'phase5', None)
        if phase5:
            graph_config['max_concurrency'] = phase5.config.get('MULTI_AGENT_MAX_PARALLEL', 3)
        graph_inputs = {**inputs, **self.scope}
        if self.phase4:
            from uuid import uuid4
            from langgraph.types import Command
            task_id = self.scope.get('task_id')
            graph_config['configurable'] = {'thread_id': 'task:' + task_id if task_id else 'chat:' + str(uuid4())}
            if self.scope.get('approval_resume_id'):
                resume_value = self.scope['approval_resume_id']
                if phase5 and task_id and phase5.store.supervisor(task_id):
                    pending = self.agent.get_state(graph_config).interrupts
                    resume_value = {item.id: item.value['approval_request_id'] for item in pending}
                graph_inputs = Command(resume=resume_value)
            elif task_id and phase5 and phase5.store.supervisor(task_id) and self.agent.get_state(graph_config).next:
                # Continue saved fan-out/subgraph work; completed branches stay cached.
                graph_inputs = None
            else:
                registry = getattr(self.agent, 'tool_registry', None)
                if registry:
                    native = {n: d for n, d in registry.snapshot().items() if d['provider'] == 'native'}
                    current = {n: d for n, d in self.phase4.registry.snapshot().items() if d['provider'] != 'native'}
                    registry.definitions = {**current, **native}
                    registry.providers.update({k: v for k, v in self.phase4.registry.providers.items() if k != 'native'})
                    registry.health.update({k: v for k, v in self.phase4.registry.health.items() if k != 'native'})
                    graph_inputs['tool_snapshot'] = self.scope.get('task_resume', {}).get('tool_snapshot') or registry.snapshot()
                graph_inputs = self.phase4.policy.secrets.clean(graph_inputs)
        return self.agent.invoke(graph_inputs, graph_config)


class _DocumentRetriever:
    """Attach legacy PDF identity to retrieved chunks without re-embedding."""
    def __init__(self, retriever: Any, vectorstore: Any):
        self.retriever = retriever

    def invoke(self, query: str) -> list[Any]:
        from langchain_core.documents import Document
        hits = self.retriever.invoke(query)
        result = []
        for hit in hits:
            source = str(hit.metadata.get("source", ""))
            result.append(Document(page_content=hit.page_content, metadata={**hit.metadata,
                "document_id": source, "document_name": Path(source).name, "source_type": "pdf"}))
        return result
