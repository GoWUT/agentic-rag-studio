from __future__ import annotations

from dataclasses import asdict

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

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

app = FastAPI(title="Agentic RAG API")
SESSION_MANAGER = AgentSessionManager(CONFIG)


class ChatRequest(BaseModel):
    session_id: str
    message: str


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


class SessionSummary(BaseModel):
    session_id: str
    file_id: str
    file_name: str
    created_at: str
    updated_at: str
    turn_count: int


class UploadResponse(SessionSummary):
    index_status: str
    index_reused: bool
    size_bytes: int


class SessionListResponse(BaseModel):
    sessions: list[SessionSummary]


class HistoryMessage(BaseModel):
    role: str
    content: str


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
def health():
    return {
        "status": "ok",
        "service": "agentic-rag-deepseek",
        "port": 8001,
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
        reply = SESSION_MANAGER.ask(req.session_id, req.message)
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
    }
