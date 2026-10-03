from __future__ import annotations

from dataclasses import asdict

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import RedirectResponse, JSONResponse
from pydantic import BaseModel, Field
from typing import Any, Literal
from server.workspaces import Workspace, DocumentRecord, WorkspaceNotFoundError
from server.agent.evidence import Evidence, Citation
from server.agent.schemas import TaskPlan

from server.agent.execution_harness import (
    ExecutionConfigurationError,
    ExecutionLimitError,
    ExecutionTimeoutError,
    ExecutionUnavailableError,
)
from server.config import CONFIG
from server.observability.langsmith import init_langsmith
from server.rag.ingestion import IndexBuildError, UploadTooLargeError
from server.sessions import (
    AgentSessionManager,
    SessionNotFoundError,
    SessionUnavailableError,
)


init_langsmith()

app = FastAPI(title="Agentic RAG API",docs_url='/docs' if CONFIG.get('API_DOCS_ENABLED',True) else None,
              redoc_url='/redoc' if CONFIG.get('API_DOCS_ENABLED',True) else None,
              openapi_url='/openapi.json' if CONFIG.get('API_DOCS_ENABLED',True) else None)
SESSION_MANAGER = AgentSessionManager(CONFIG)
from server.runtime_services import register_runtime
register_runtime(app, SESSION_MANAGER)
from server.phase3_api import register_phase3_api
register_phase3_api(app, SESSION_MANAGER)
from server.phase4_api import register_phase4_api
register_phase4_api(app, SESSION_MANAGER)
from server.phase5_api import register_phase5_api
register_phase5_api(app, SESSION_MANAGER)
from server.auth.middleware import register_security
from server.observability.runtime import register_observability
register_security(app, SESSION_MANAGER)
register_observability(app, SESSION_MANAGER)


class ChatRequest(BaseModel):
    session_id: str
    message: str
    workspace_id: str | None = None
    source_scope: Literal["workspace_only", "workspace_and_external", "external"] = "workspace_and_external"


class ContextStats(BaseModel):
    model_context_window_tokens: int
    input_budget_tokens: int
    max_output_tokens: int
    safety_tokens: int
    estimated_tokens_before: int
    estimated_tokens_after: int
    compacted_messages: int
    truncated_messages: int
    strategy: str


class ExecutionStats(BaseModel):
    run_id: str
    attempts: int
    duration_ms: float
    max_graph_steps: int


class ChatResponse(BaseModel):
    answer: str
    context: ContextStats
    execution: ExecutionStats
    citations: list[Citation] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)
    plan: TaskPlan | None = None
    trace_summary: dict[str, Any] = Field(default_factory=dict)
    artifacts: list[dict[str, Any]] = Field(default_factory=list)


class SessionSummary(BaseModel):
    session_id: str
    file_id: str
    file_name: str
    created_at: str
    updated_at: str
    turn_count: int
    workspace_id: str | None = None


class UploadResponse(SessionSummary):
    index_status: str
    index_reused: bool
    size_bytes: int


class SessionListResponse(BaseModel):
    sessions: list[SessionSummary]


class HistoryMessage(BaseModel):
    role: str
    content: str
    research: dict[str, Any] | None = None


class SessionHistoryResponse(BaseModel):
    session_id: str
    messages: list[HistoryMessage]


class IndexStatusResponse(BaseModel):
    file_id: str
    file_name: str
    size_bytes: int
    status: str
    error: str | None
    created_at: str
    updated_at: str


@app.get("/", include_in_schema=False)
def root():
    return RedirectResponse(CONFIG["FRONTEND_URL"])


@app.get("/health")
async def health():
    database = await SESSION_MANAGER.database.health() if SESSION_MANAGER.database else 'sqlite'
    return {
        "status": "ok",
        "service": "agentic-rag-deepseek",
        "port": 8001,
        "database": database,
        **await SESSION_MANAGER.task_queue.health(),
    }


@app.get("/sessions", response_model=SessionListResponse)
def list_sessions():
    return {"sessions": SESSION_MANAGER.list_sessions()}


@app.get(
    "/indexes/{file_id}",
    response_model=IndexStatusResponse,
)
def index_status(file_id: str):
    status = SESSION_MANAGER.get_index_status(file_id)
    if status is None:
        raise HTTPException(status_code=404, detail="Index not found")
    return asdict(status)


@app.get(
    "/sessions/{session_id}/messages",
    response_model=SessionHistoryResponse,
    response_model_exclude_none=True,
)
def session_history(session_id: str):
    try:
        messages = SESSION_MANAGER.get_history(session_id)
    except SessionNotFoundError as error:
        raise HTTPException(status_code=404, detail="Session not found") from error

    return {"session_id": session_id, "messages": messages}


@app.post("/upload_pdf", response_model=UploadResponse)
def upload_pdf(file: UploadFile = File(...)):
    try:
        return SESSION_MANAGER.create_session(
            file.filename or "uploaded.pdf",
            file.file,
        )
    except UploadTooLargeError as error:
        raise HTTPException(status_code=413, detail=str(error)) from error
    except IndexBuildError as error:
        raise HTTPException(
            status_code=500,
            detail="PDF index build failed. Check the index status and logs.",
        ) from error
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@app.post("/chat", response_model=ChatResponse)
def chat(req: ChatRequest):
    try:
        if req.workspace_id is None and req.source_scope == "workspace_and_external":
            reply = SESSION_MANAGER.ask(req.session_id, req.message)
        else:
            reply = SESSION_MANAGER.ask(req.session_id, req.message, workspace_id=req.workspace_id, source_scope=req.source_scope)
    except SessionNotFoundError as error:
        raise HTTPException(status_code=404, detail="Session not found") from error
    except SessionUnavailableError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except ExecutionLimitError as error:
        raise HTTPException(
            status_code=422,
            detail="Agent exceeded its graph-step execution budget",
        ) from error
    except ExecutionConfigurationError as error:
        raise HTTPException(
            status_code=503,
            detail="Agent provider configuration is invalid",
        ) from error
    except ExecutionTimeoutError as error:
        raise HTTPException(
            status_code=504,
            detail="Agent execution exceeded its time budget",
        ) from error
    except ExecutionUnavailableError as error:
        raise HTTPException(
            status_code=503,
            detail=(
                "Unable to reach the language model service. "
                "Check the backend network connection and try again."
            ),
        ) from error
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error

    return {
        "answer": reply.answer,
        "context": reply.context.as_dict(),
        "execution": reply.execution.as_dict(),
        "citations": reply.citations,
        "evidence": reply.evidence,
        "plan": reply.plan,
        "trace_summary": reply.trace_summary,
        "artifacts": getattr(reply, "artifacts", []),
    }


class WorkspaceCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str = ""


@app.exception_handler(WorkspaceNotFoundError)
async def workspace_not_found(request, error):
    return JSONResponse(status_code=404, content={"detail": "Workspace or document not found"})


@app.post("/workspaces", response_model=Workspace)
def create_workspace(req: WorkspaceCreate):
    try:
        return SESSION_MANAGER.workspaces.create(req.name, req.description)
    except ValueError as error:
        raise HTTPException(400, detail="Workspace name cannot be blank") from error


@app.get("/workspaces", response_model=list[Workspace])
def list_workspaces():
    return SESSION_MANAGER.workspaces.list()


@app.get("/workspaces/{workspace_id}", response_model=Workspace)
def get_workspace(workspace_id: str):
    return SESSION_MANAGER.workspaces.get(workspace_id)


@app.delete("/workspaces/{workspace_id}", status_code=204)
def delete_workspace(workspace_id: str):
    SESSION_MANAGER.workspaces.delete(workspace_id)


@app.post("/workspaces/{workspace_id}/sessions", response_model=SessionSummary)
def create_workspace_session(workspace_id: str):
    return SESSION_MANAGER.create_workspace_session(workspace_id)


@app.get("/workspaces/{workspace_id}/documents", response_model=list[DocumentRecord])
def list_workspace_documents(workspace_id: str):
    return SESSION_MANAGER.workspaces.documents(workspace_id)


@app.post("/workspaces/{workspace_id}/documents")
def upload_workspace_documents(workspace_id: str, files: list[UploadFile] = File(...)):
    """Each upload is independent; one OCR/index failure does not discard the batch."""
    SESSION_MANAGER.workspaces.get(workspace_id)
    results = []
    for file in files:
        try:
            results.append(SESSION_MANAGER.add_workspace_document(workspace_id, file.filename or "uploaded.pdf", file.file))
        except (ValueError, IndexBuildError) as error:
            results.append({"filename": file.filename, "status": "failed", "error": str(error) if isinstance(error, ValueError) else "PDF indexing failed; check backend logs"})
    return {"documents": results}


@app.delete("/workspaces/{workspace_id}/documents/{document_id}", status_code=204)
def delete_workspace_document(workspace_id: str, document_id: str):
    SESSION_MANAGER.workspaces.delete_document(workspace_id, document_id)
