from __future__ import annotations

import os
from typing import Any

import requests
import streamlit as st


API_BASE = os.getenv("API_BASE", "http://127.0.0.1:8001")

st.set_page_config(
    page_title="Agentic RAG Studio",
    page_icon="🧠",
    layout="wide",
)

st.markdown(
    """
    <style>
    .stApp {
        background:
            radial-gradient(circle at 10% 10%, #eef5ff 0, transparent 28%),
            radial-gradient(circle at 90% 0%, #f5efff 0, transparent 25%),
            #f8fafc;
    }
    .block-container {
        max-width: 1080px;
        padding-top: 2.2rem;
        padding-bottom: 5rem;
    }
    [data-testid="stSidebar"] {
        background: rgba(255, 255, 255, 0.92);
        border-right: 1px solid #e2e8f0;
    }
    [data-testid="stChatMessage"] {
        background: rgba(255, 255, 255, 0.88);
        border: 1px solid #e2e8f0;
        border-radius: 16px;
        padding: 0.35rem 0.75rem;
        box-shadow: 0 8px 24px rgba(15, 23, 42, 0.04);
    }
    .rag-hero {
        padding: 1.3rem 1.5rem;
        border-radius: 20px;
        color: white;
        background: linear-gradient(135deg, #111827 0%, #312e81 100%);
        box-shadow: 0 18px 45px rgba(49, 46, 129, 0.18);
        margin-bottom: 1.2rem;
    }
    .rag-hero h1 {
        margin: 0;
        font-size: 2rem;
    }
    .rag-hero p {
        margin: 0.45rem 0 0;
        color: #dbeafe;
    }
    .empty-state {
        padding: 3rem 2rem;
        text-align: center;
        border: 1px dashed #94a3b8;
        border-radius: 18px;
        background: rgba(255, 255, 255, 0.68);
        color: #475569;
    }
    </style>
    """,
    unsafe_allow_html=True,
)


class BackendError(RuntimeError):
    pass


def _response_error(response: requests.Response) -> str:
    try:
        payload = response.json()
        detail = payload.get("detail")
        if detail:
            return str(detail)
    except ValueError:
        pass
    return response.text or f"HTTP {response.status_code}"


def fetch_sessions() -> list[dict[str, Any]]:
    response = requests.get(f"{API_BASE}/sessions", timeout=5)
    if response.status_code != 200:
        raise BackendError(_response_error(response))

    payload = response.json()
    sessions = payload.get("sessions")
    if not isinstance(sessions, list):
        raise BackendError("Backend returned an invalid session list")
    return sessions


def fetch_history(session_id: str) -> list[tuple[str, str]]:
    response = requests.get(
        f"{API_BASE}/sessions/{session_id}/messages",
        timeout=5,
    )
    if response.status_code != 200:
        raise BackendError(_response_error(response))

    payload = response.json()
    messages = payload.get("messages")
    if not isinstance(messages, list):
        raise BackendError("Backend returned an invalid message history")

    history: list[tuple[str, str]] = []
    for item in messages:
        role = item.get("role")
        content = item.get("content")
        if role in {"user", "assistant"} and isinstance(content, str):
            history.append((role, content))
    return history


def context_caption(report: dict[str, Any] | None) -> str | None:
    if not isinstance(report, dict):
        return None
    try:
        used = int(report["estimated_tokens_after"])
        before = int(report["estimated_tokens_before"])
        budget = int(report["input_budget_tokens"])
        compacted = int(report["compacted_messages"])
        truncated = int(report["truncated_messages"])
    except (KeyError, TypeError, ValueError):
        return None

    utilization = (used / budget * 100) if budget else 0
    strategy = (
        "已压缩"
        if report.get("strategy") == "compacted"
        else "完整保留"
    )
    return (
        f"Context Harness · {used:,}/{budget:,} tokens "
        f"({utilization:.1f}%) · 调用前 {before:,} · {strategy} · "
        f"压缩 {compacted} 条 / 截断 {truncated} 条"
    )


def execution_caption(report: dict[str, Any] | None) -> str | None:
    if not isinstance(report, dict):
        return None
    try:
        run_id = str(report["run_id"])
        attempts = int(report["attempts"])
        duration_ms = float(report["duration_ms"])
        max_steps = int(report["max_graph_steps"])
    except (KeyError, TypeError, ValueError):
        return None

    return (
        f"Execution Harness · run {run_id[:8]} · "
        f"{attempts} 次尝试 · {duration_ms:,.0f} ms · "
        f"最多 {max_steps} graph steps"
    )


def activate_session(session_id: str) -> None:
    st.session_state.session_id = session_id
    st.session_state.chat = fetch_history(session_id)
    st.session_state.context_report = None
    st.session_state.execution_report = None


def current_session() -> dict[str, Any] | None:
    return next(
        (
            item
            for item in st.session_state.sessions
            if item.get("session_id") == st.session_state.session_id
        ),
        None,
    )


defaults = {
    "session_id": None,
    "chat": [],
    "sessions": [],
    "sessions_loaded": False,
    "startup_error": None,
    "context_report": None,
    "execution_report": None,
}
for key, value in defaults.items():
    if key not in st.session_state:
        st.session_state[key] = value


if not st.session_state.sessions_loaded:
    try:
        st.session_state.sessions = fetch_sessions()
        if st.session_state.sessions:
            activate_session(st.session_state.sessions[0]["session_id"])
    except (requests.RequestException, ValueError, BackendError) as error:
        st.session_state.startup_error = str(error)
    finally:
        st.session_state.sessions_loaded = True


with st.sidebar:
    st.title("🧠 Agentic RAG")
    st.caption("持久化 PDF 知识库与多工具 Agent")

    if st.session_state.sessions:
        session_ids = [item["session_id"] for item in st.session_state.sessions]
        labels = {
            item["session_id"]: (
                f"{item['file_name']} · {item.get('turn_count', 0)} 轮"
            )
            for item in st.session_state.sessions
        }
        selected_index = (
            session_ids.index(st.session_state.session_id)
            if st.session_state.session_id in session_ids
            else 0
        )
        selected_session = st.selectbox(
            "历史会话",
            options=session_ids,
            index=selected_index,
            format_func=lambda session_id: labels[session_id],
        )
        if selected_session != st.session_state.session_id:
            try:
                activate_session(selected_session)
                st.rerun()
            except (requests.RequestException, ValueError, BackendError) as error:
                st.error(f"无法恢复历史会话：{error}")
    else:
        st.info("还没有持久化会话，请上传第一份 PDF。")

    if st.button("刷新历史", use_container_width=True):
        try:
            st.session_state.sessions = fetch_sessions()
            known_ids = {
                item["session_id"] for item in st.session_state.sessions
            }
            if st.session_state.session_id in known_ids:
                activate_session(st.session_state.session_id)
            elif st.session_state.sessions:
                activate_session(st.session_state.sessions[0]["session_id"])
            else:
                st.session_state.session_id = None
                st.session_state.chat = []
                st.session_state.context_report = None
                st.session_state.execution_report = None
            st.session_state.startup_error = None
            st.rerun()
        except (requests.RequestException, ValueError, BackendError) as error:
            st.error(f"刷新失败：{error}")

    st.divider()
    st.subheader("创建知识库")
    uploaded = st.file_uploader(
        "选择 PDF",
        type=["pdf"],
        help="相同 PDF 会复用已经持久化的 Chroma 索引。",
    )

    upload_clicked = st.button(
        "上传并创建会话",
        type="primary",
        use_container_width=True,
        disabled=uploaded is None,
    )

    if upload_clicked and uploaded is not None:
        with st.spinner("正在解析、切分并建立索引……"):
            try:
                response = requests.post(
                    f"{API_BASE}/upload_pdf",
                    files={
                        "file": (
                            uploaded.name,
                            uploaded,
                            "application/pdf",
                        )
                    },
                    timeout=300,
                )
                if response.status_code != 200:
                    raise BackendError(_response_error(response))

                created = response.json()
                session_id = created.get("session_id")
                if not isinstance(session_id, str):
                    raise BackendError(
                        "Backend did not return a valid session_id"
                    )

                st.session_state.sessions = fetch_sessions()
                st.session_state.session_id = session_id
                st.session_state.chat = []
                st.session_state.context_report = None
                st.session_state.execution_report = None
                st.session_state.startup_error = None
                st.rerun()
            except (requests.RequestException, ValueError, BackendError) as error:
                st.error(f"上传失败：{error}")

    st.caption("PDF、Chroma 索引和聊天记录均保存在本机工作区。")


st.markdown(
    """
    <div class="rag-hero">
      <h1>Agentic RAG Studio</h1>
      <p>让 Agent 在你的 PDF、Web 与 arXiv 之间选择工具并组织答案。</p>
    </div>
    """,
    unsafe_allow_html=True,
)

if st.session_state.startup_error:
    st.warning(
        "暂时无法连接后端，仍可查看当前页面。"
        f"请确认 FastAPI 已启动：{st.session_state.startup_error}"
    )

active = current_session()
if active is not None:
    st.subheader(active["file_name"])
    st.caption(
        f"会话 {active['session_id'][:8]} · "
        f"文件指纹 {active['file_id']} · "
        f"已保存 {active.get('turn_count', 0)} 轮对话"
    )
    latest_context_caption = context_caption(
        st.session_state.context_report
    )
    if latest_context_caption:
        st.caption(latest_context_caption)
    latest_execution_caption = execution_caption(
        st.session_state.execution_report
    )
    if latest_execution_caption:
        st.caption(latest_execution_caption)
else:
    st.markdown(
        """
        <div class="empty-state">
          <h3>从一份 PDF 开始</h3>
          <p>在左侧上传文档。再次打开项目时，可直接恢复知识库与历史对话。</p>
        </div>
        """,
        unsafe_allow_html=True,
    )


for role, content in st.session_state.chat:
    with st.chat_message(role):
        st.markdown(content)


prompt = st.chat_input(
    "选择一个历史会话或上传 PDF 后开始提问",
    disabled=not bool(st.session_state.session_id),
)

if prompt and st.session_state.session_id:
    st.session_state.chat.append(("user", prompt))
    with st.chat_message("user"):
        st.markdown(prompt)

    with st.chat_message("assistant"):
        with st.spinner("Agent 正在选择工具并组织答案……"):
            current_context_caption = None
            current_execution_caption = None
            try:
                response = requests.post(
                    f"{API_BASE}/chat",
                    json={
                        "session_id": st.session_state.session_id,
                        "message": prompt,
                    },
                    timeout=300,
                )
                if response.status_code != 200:
                    answer = f"请求失败：{_response_error(response)}"
                else:
                    payload = response.json()
                    answer = payload.get("answer")
                    if not isinstance(answer, str):
                        answer = "后端没有返回有效的 answer 字段。"
                    context_report = payload.get("context")
                    if isinstance(context_report, dict):
                        st.session_state.context_report = context_report
                        current_context_caption = context_caption(
                            context_report
                        )
                    execution_report = payload.get("execution")
                    if isinstance(execution_report, dict):
                        st.session_state.execution_report = execution_report
                        current_execution_caption = execution_caption(
                            execution_report
                        )
            except requests.RequestException as error:
                answer = f"无法连接 FastAPI 后端：{error}"
            except ValueError:
                answer = "后端返回了无效的 JSON。"

            st.session_state.chat.append(("assistant", answer))
            for item in st.session_state.sessions:
                if item.get("session_id") == st.session_state.session_id:
                    item["turn_count"] = item.get("turn_count", 0) + 1
                    break
            st.markdown(answer)
            if current_context_caption:
                st.caption(current_context_caption)
            if current_execution_caption:
                st.caption(current_execution_caption)
