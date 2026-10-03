# PHASE 5B-1 PRE-DEVELOPMENT AUDIT

Audited 2026-10-03, before implementation. Existing working changes retained;
`git status`, `git diff --stat`, `git diff --check` completed. No commits/pushes.

1. **Five SQLite connection creation sites**: `persistence.Database.connect`,
   `SessionStore._connect`, `WorkspaceStore.connect`, `_IndexStatusStore._connect`,
   `ManagedSqliteSaver.cursor`. Chroma owns its separate internal database.
2. **Direct application SQL**: persistence, sessions, workspaces, memory, tasks,
   data_assets, tool_actions, agent_runs, mcp_client, rag/ingestion; service code
   in tasks, phase4 and phase5 also opens transactions for events/metadata.
3. **Nine independently initialized application schemas**: SessionStore,
   WorkspaceStore, MemoryStore, TaskStore, DataStore, ToolActionStore,
   AgentRunStore, MCPServerStore, _IndexStatusStore. Checkpointer initializes
   its own SDK schema. There are 16 application tables.
4. **Transactions**: context managers commit/rollback via SQLite connection
   context; mutation paths use BEGIN IMMEDIATE to serialize read-modify-write.
   Task status/step and events share transactions. Request execution locks are
   process-local; they do not prevent cross-process duplicate execution.
5. **Checkpoint**: Phase4Services constructs ManagedSqliteSaver using the session
   database and RedactedSerializer, initializes via SDK setup in cursor. Graph
   uses synchronous invoke and public get_state, including Command resume.
6. **Run API**: synchronous route runs the entire TaskService.run in a FastAPI
   thread; response waits for the agent to finish or interrupt.
7. **Task states**: PENDING, RUNNING, PAUSED, WAITING_USER, COMPLETED, FAILED,
   CANCELLED. RUNNING is reached from PENDING/PAUSED/WAITING_USER/FAILED.
8. **Restart recovery**: Phase3 API startup changes every RUNNING task to PAUSED
   and RUNNING steps to PENDING. Phase5 API startup interrupts runs. Both must
   be disabled in worker mode: restarting API must not interrupt a live worker.
9. **HITL**: WAITING_USER persists a tool approval and LangGraph interrupt.
   Phase4.decide persists/validates decision, consumes approval once in claim,
   then synchronously resumes graph through TaskService and Command. Existing
   RUNNING/FAILED write claims already reject blind replay.
10. **Paths**: WORKSPACE_DIR defaults to `.rag_workspace`; application/session/
    checkpoint stores use `sessions.sqlite3`; index registry `indexes.sqlite3`;
    Chroma collections remain filesystem assets with their own SQLite files.
11. **JSON text**: sessions.messages_json, workspace_documents.metadata;
    payload in tasks/steps/events/memories/assets/analysis/artifacts/actions/
    approvals/runs/delegations/MCP; long_term_memories.embedding. Nested plan,
    result and metadata are in Pydantic payloads, not separate SQL columns.
12. **File metadata**: sessions PDF/Chroma paths; document fingerprint/index ID;
    dataset file_path/fingerprint/schema; artifact file_path/MIME/filename;
    analysis execution results. Migration must preserve paths and IDs, never
    copy bytes or Chroma internal tables.

## Compatibility audit

Python 3.11.16; SQLAlchemy already installed at 2.0.45; FastAPI 0.124.4;
LangGraph 1.0.5; SQLite saver 3.0.3. psycopg/Alembic/Postgres saver previously
absent. Procrastinate 3.10.0 accepts Python >=3.10 and psycopg 3, supports ASGI,
external-connection atomic deferral, native queueing/execution locks and worker
heartbeats. Windows requires a Selector event loop for psycopg async; graceful
shutdown needs explicit application signal handling (no Unix add_signal_handler).
No unacceptable framework blocker identified; actual runtime verification follows.

Sources: [Procrastinate release](https://pypi.org/project/procrastinate/3.10.0/),
[public API](https://procrastinate.readthedocs.io/en/stable/reference.html),
[Windows](https://procrastinate.readthedocs.io/en/stable/howto/basics/windows.html),
[stalled recovery](https://procrastinate.readthedocs.io/en/stable/howto/production/retry_stalled_jobs.html).

## Cutover decision

Retain SQLite checkpoint data. Public saver APIs expose checkpoint tuples and
pending writes, but importing completed and pending subgraph state safely across
these serializers/versions is not verified. Do not hand-copy internal tables.
Migration blocks RUNNING, QUEUED, PAUSED, WAITING_USER (and interrupted legacy
tasks) until completed/cancelled. New PostgreSQL tasks use the official SDK saver.
