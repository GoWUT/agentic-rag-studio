from __future__ import annotations

import os
from typing import Any

from client.auth_ui import api_requests as requests, login_gate, workspace_role, membership_panel
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
    st.session_state.research_details = {}
    for item in messages:
        role = item.get("role")
        content = item.get("content")
        if role in {"user", "assistant"} and isinstance(content, str):
            if item.get("research"):
                st.session_state.research_details[len(history)] = item["research"]
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
    record = current_session()
    if record and record.get("workspace_id"):
        st.session_state.workspace_id = record["workspace_id"]


def current_session() -> dict[str, Any] | None:
    return next(
        (
            item
            for item in st.session_state.sessions
            if item.get("session_id") == st.session_state.session_id
        ),
        None,
    )


login_gate()

defaults = {
    "session_id": None,
    "chat": [],
    "sessions": [],
    "sessions_loaded": False,
    "startup_error": None,
    "context_report": None,
    "execution_report": None,
    "workspace_id": None,
    "source_scope": "workspace_and_external",
    "research_details": {},
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

    modes=["Workspace"] if st.session_state.get('_auth_enabled') else ["单 PDF", "Workspace"]
    mode = st.radio("工作模式", modes, index=1 if len(modes)>1 and (current_session() or {}).get("workspace_id") else 0, key="work_mode", horizontal=True)
    if mode == "Workspace":
        try:
            response = requests.get(f"{API_BASE}/workspaces", timeout=5)
            response.raise_for_status()
            workspaces = response.json()
            with st.form("create_workspace"):
                workspace_name = st.text_input("Workspace 名称")
                description = st.text_input("描述（可选）")
                create_clicked = st.form_submit_button("创建 Workspace")
            if create_clicked:
                created = requests.post(f"{API_BASE}/workspaces", json={"name": workspace_name, "description": description}, timeout=10)
                if created.status_code != 200:
                    raise BackendError(_response_error(created))
                st.session_state.workspace_id = created.json()["id"]
                st.rerun()
            if workspaces:
                ids = [item["id"] for item in workspaces]
                workspace_labels = {item["id"]: item["name"] for item in workspaces}
                selected = st.selectbox("Workspace", ids, index=ids.index(st.session_state.workspace_id) if st.session_state.workspace_id in ids else 0, format_func=lambda identity: workspace_labels[identity])
                st.session_state.workspace_id = selected
                role=workspace_role(API_BASE,selected)
                membership_panel(API_BASE,selected,role)
                if (current_session() or {}).get("workspace_id") != selected:
                    associated = [item for item in st.session_state.sessions if item.get("workspace_id") == selected]
                    if associated:
                        activate_session(associated[0]["session_id"])
                    else:
                        st.session_state.session_id = None
                        st.session_state.chat = []
                        st.session_state.research_details = {}
                documents_response = requests.get(f"{API_BASE}/workspaces/{selected}/documents", timeout=5)
                documents_response.raise_for_status()
                documents = documents_response.json()
                for document in documents:
                    st.caption(f"{document['display_name']} · {document['status']} · {document.get('page_count', '?')} 页")
                    if st.button("移除文档", key=f"delete_{document['id']}",disabled=role=='VIEWER'):
                        deleted = requests.delete(f"{API_BASE}/workspaces/{selected}/documents/{document['id']}", timeout=10)
                        deleted.raise_for_status()
                        st.rerun()
                uploads = st.file_uploader("上传多个 PDF", type=["pdf"], accept_multiple_files=True)
                if st.button("上传文档", disabled=not uploads or role=='VIEWER'):
                    uploaded_response = requests.post(f"{API_BASE}/workspaces/{selected}/documents", files=[("files", (file.name, file, "application/pdf")) for file in uploads], timeout=660)
                    uploaded_response.raise_for_status()
                    for result in uploaded_response.json()["documents"]:
                        if result["status"] == "failed":
                            st.error(f"{result['filename']}: {result['error']}")
                        else:
                            st.success(f"{result['filename']} · {result['status']} · {result.get('page_count')} 页")
                if st.button("新建 Workspace 会话"):
                    created_session = requests.post(f"{API_BASE}/workspaces/{selected}/sessions", timeout=10)
                    created_session.raise_for_status()
                    st.session_state.sessions = fetch_sessions()
                    activate_session(created_session.json()["session_id"])
                    st.rerun()
                if st.button("删除 Workspace",disabled=role!='OWNER'):
                    deleted = requests.delete(f"{API_BASE}/workspaces/{selected}", timeout=10)
                    deleted.raise_for_status()
                    st.session_state.workspace_id = None
                    st.rerun()
        except (requests.RequestException, ValueError, BackendError) as error:
            st.error(f"Workspace 操作失败：{error}")

    scopes = ["workspace_and_external", "workspace_only", "external"]
    st.selectbox("信息来源", scopes, key="source_scope", format_func=lambda value: {"workspace_and_external": "优先文档，必要时外部检索", "workspace_only": "仅上传文档", "external": "仅外部检索"}[value])
    visible_sessions = [item for item in st.session_state.sessions if
                        (st.session_state.workspace_id and item.get("workspace_id") == st.session_state.workspace_id if mode == "Workspace" else not item.get("workspace_id"))]
    if visible_sessions:
        session_ids = [item["session_id"] for item in visible_sessions]
        labels = {
            item["session_id"]: (
                f"{item['file_name']} · {item.get('turn_count', 0)} 轮"
            )
            for item in visible_sessions
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
        if current_session() is not None:
            st.session_state.session_id = None
            st.session_state.chat = []
            st.session_state.research_details = {}
        st.info("当前 Workspace 还没有会话。" if mode == "Workspace" else "还没有单 PDF 会话，请上传第一份 PDF。")

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
                    timeout=660,
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
    from client.phase3_ui import sidebar as phase3_sidebar
    from client.phase4_ui import sidebar as phase4_sidebar
    phase4_sidebar(API_BASE)
    phase3_sidebar(API_BASE, st.session_state.workspace_id if mode == "Workspace" else None,
                   st.session_state.session_id, st.session_state.source_scope)


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


def show_research_details(details: dict[str, Any]) -> None:
    if details.get("artifacts"):
        from client.phase3_ui import show_artifacts
        show_artifacts(API_BASE, details["artifacts"], "chat_" + str(len(st.session_state.chat)))
    if details.get("evidence"):
        with st.expander("Sources / Evidence"):
            for item in details["evidence"]:
                st.write(item.get("document_name") or item.get("title") or item["source_type"])
                st.caption(f"Page: {item.get('page', '—')} · Retrieval: {item.get('retrieval_score')} · Rerank: {item.get('rerank_score')}")
                st.write(item["content"])
                if item.get("url"):
                    st.link_button("打开来源", item["url"])
    if details.get("plan"):
        with st.expander("Task Plan"):
            for step in details["plan"]["steps"]:
                st.write(f"{'✓' if step['status'] == 'completed' else '○'} {step['description']} ({step['status']})")


for index, (role, content) in enumerate(st.session_state.chat):
    with st.chat_message(role):
        st.markdown(content)
        show_research_details(st.session_state.research_details.get(index, {}))


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
                        "workspace_id": (current_session() or {}).get("workspace_id"),
                        "source_scope": st.session_state.source_scope,
                    },
                    timeout=300,
                )
                if response.status_code != 200:
                    answer = f"请求失败：{_response_error(response)}"
                else:
                    payload = response.json()
                    st.session_state.research_details[len(st.session_state.chat)] = payload
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
            show_research_details(st.session_state.research_details.get(len(st.session_state.chat) - 1, {}))
            if current_context_caption:
                st.caption(current_context_caption)
            if current_execution_caption:
                st.caption(current_execution_caption)
