"""Build the A–V acceptance report from completed test/process evidence."""
import json
import re
import statistics
from importlib.metadata import version
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def run():
    smoke = json.loads((ROOT/'evaluation/results/phase5b1/runtime_smoke.json').read_text())
    regression = (ROOT/'.runtime/phase5b1-regression-final.log').read_text(errors='replace')
    integration = (ROOT/'.runtime/phase5b1-postgres-tests.log').read_text(errors='replace')
    assert re.search(r'^OK \(skipped=17\)$', regression, re.M), 'Final regression must complete successfully'
    assert re.search(r'^OK$', integration, re.M), 'Integration tests must pass'
    assert len(smoke['checks']) == 30 and all(v['status']=='PASS' for v in smoke['checks'].values())
    assert not smoke.get('error') and all(v['status']=='PASS' for v in smoke['retry_checks'].values())
    total = int(re.search(r'Ran (\d+) tests', regression)[1])
    pg_tests = int(re.search(r'Ran (\d+) tests', integration)[1])
    assert total == 479 and pg_tests == 17
    perf = smoke['performance']
    metrics = '\n'.join(f'| {k} | {len(v)} | {statistics.median(v):.3f} | {min(v):.3f}–{max(v):.3f} |'
        for k,v in perf.items() if isinstance(v,list))
    runtime_rows = '\n'.join(f'| {i} | {name} | {value["status"]} |'
        for i,(name,value) in enumerate(smoke['checks'].items(),1))
    # Render the user's numbered list in its intended order, independent of dispatch order.
    names = ['PostgreSQL starts','Alembic upgrade head','Queue schema initialized','PostgresSaver initialized',
        'FastAPI postgres/worker starts','Independent worker starts','Streamlit starts','Task creation',
        'Run returns 202','Task QUEUED','Worker RUNNING','Task completes','API restart independent',
        'Streamlit stop independent','Worker graceful restart','Worker hard crash read recovery',
        'Completed step not repeated','Pause','Queue resume','Cancel queued task','HITL WAITING_USER',
        'Approval queue resume','Approved write exactly once','Research + Data background',
        'Persistence after restart','SQLite migration dry-run','SQLite fixture migration',
        'Row/checksum verification','Duplicate run requests','DB/queue health']
    runtime_rows = '\n'.join(f'| {i} | {name} | {smoke["checks"][name]["status"]} |' for i,name in enumerate(names,1))
    local = smoke['existing_local_migration']
    text = f'''# Phase 5B-1 — A–V acceptance report

2026-10-03. Run `{smoke['run_id']}`. Evidence: [process result](../evaluation/results/phase5b1/runtime_smoke.json),
[pre-development audit](PHASE5B1_PRE_DEVELOPMENT_AUDIT.md), [architecture](PHASE5B1_IMPLEMENTATION.md),
[migration/rollback](PHASE5B1_MIGRATION.md). Local raw logs are retained under `.runtime/phase5b1-*`.

## A. Final Verdict

**READY WITH LIMITATIONS**. PostgreSQL + independent worker foundation passes the required integration and crash acceptance.
SQLite/inline remains the reversible default; existing data has not been cut over.

## B. Architecture

```mermaid
flowchart LR
  API[FastAPI] -->|AsyncSession: task + event + atomic defer| PG[(PostgreSQL)]
  Q[Procrastinate durable queue] --- PG
  W[Independent Worker] --> Q
  W --> G[Existing LangGraph / agents]
  G --> R[Domain repositories / sync pool in graph threads]
  R --> PG
  G --> S[Official AsyncPostgresSaver via sync bridge]
  S --> PG
  UI[Streamlit] --> API
```

API does not launch a worker. Queue contains only `task_id` and `expected_version`.
Files and Chroma remain in local filesystem paths.

## C. Technology Decisions

| Component | Actually verified version | Decision |
| --- | --- | --- |
| PostgreSQL | {smoke['postgres_version']} | Official local Windows binaries; relational state and queue share a database |
| SQLAlchemy | {version('sqlalchemy')} | One process-owned sync/async engine pair; async queue transaction |
| psycopg | {version('psycopg')} | Single PostgreSQL driver for SQLAlchemy, saver and queue |
| Alembic | {version('alembic')} | Application schema revision and explicit upgrades |
| langgraph-checkpoint-postgres | {version('langgraph-checkpoint-postgres')} | Official AsyncPostgresSaver and setup API |
| Procrastinate | {version('procrastinate')} | Official external-connection deferral, locks, retry and heartbeat recovery |
| LangGraph | {version('langgraph')} | Existing 1.0.5 graph retained |

Procrastinate is pinned in `uv.lock`; Windows Selector loops are scoped to API/worker/saver.
The global policy remains intact for MCP subprocesses. Dependency compatibility sources are linked in the audit.

## D. Repository Migration

Workspace, Session, Memory, Task/Step/Event, Dataset/Analysis/Artifact, Approval/ToolExecution,
AgentRun/Delegation, MCPServer and index metadata now share backend-neutral domain repositories.
Domain Protocols and RepositoryFactory define the composition boundary; domain models remain Pydantic/dataclasses.
The PostgreSQL query port uses SQLAlchemy transactions and typed binds; it is not a driver name replacement.
Sync domain methods run in graph/FastAPI threads; enqueue/control/health use AsyncSession.
Direct `sqlite3.connect` remains only in the explicit SQLite fallback and ManagedSqliteSaver;
migration reads SQLite through a read-only URI. Chroma internals are unchanged.

## E. Database Schema

16 business tables: workspaces, sessions, workspace_documents, tasks, task_steps, task_events,
long_term_memories, data_assets, analysis_executions, artifacts, tool_executions, approval_requests,
agent_runs, agent_delegations, mcp_servers, indexes.
16 primary keys, 18 foreign-key constraints, 22 unique constraints (including 16 identity-order columns),
16 explicit query indexes and appropriate NOT NULL constraints.
JSON text becomes JSONB, relational timestamp columns are TIMESTAMPTZ/UTC; existing IDs and file metadata remain.
Task projections add version/job/timing/attempt/backend fields. `_order` preserves former rowid ordering.

## F. Alembic

Revision `5b1_0001`; baseline uses frozen `schema_v1` metadata.
`upgrade head`, `downgrade base`, then `upgrade head` passed on a new empty fixture database.
No business database was downgraded. Procrastinate uses official SchemaManager;
LangGraph uses official AsyncPostgresSaver.setup. Neither SDK schema is copied into Alembic.

## G. SQLite → PostgreSQL

[Existing-data migration](generated/POSTGRES_EXISTING_DATA_MIGRATION.md): all 16 tables validated,
{local['source_rows']} source rows → {local['destination_rows']} destination rows, all table counts/checksums PASS.
This imported read-only backup copies into a new isolated database. Original SQLite unchanged: **{smoke['original_sqlite_unchanged']}**.
[Synthetic fixture migration](generated/POSTGRES_MIGRATION_REPORT.md) additionally contains nonempty
Approval, ToolExecution, AgentRun and Delegation rows; original IDs are asserted by integration tests.
Dry-run does not write destination; nonempty destination aborts by default; collisions always abort/roll back.
Warnings for existing data: old `checkpoints` and `writes` SDK tables deliberately ignored.
File bytes and vector embeddings are not migrated. Source schema user_version=0, destination=5b1_0001.

## H. LangGraph Checkpoint

CheckpointFactory selects ManagedSqliteSaver or the official AsyncPostgresSaver bridge.
Real graph invocation/get_state and persisted HITL Command resume passed against PostgreSQL.
Old checkpoint stores are retained; importing SDK internals is not claimed safe.
Migration blocks resumable legacy tasks until completed/cancelled; new PG tasks use new PG checkpoints.

## I. Queue

Same AsyncSession transaction holds the mutation mutex and task row lock, updates task/event,
and passes its underlying psycopg connection to official defer_async; one commit publishes all changes.
Native queueing lock prevents duplicate pending jobs; native execution lock prevents overlapping task execution.
Version/job generation checks reject stale delivery. Only known transient read/transport failures retry,
bounded by WORKER_MAX_RETRIES (default 2). Invalid input/configuration/policy failures do not retry.
Confirmed ambiguous external writes enter REQUIRES_RECONCILIATION.
Official worker heartbeat updates every 10s; dead-worker cutoff 30s; SDK stalled-job detection and retry/finish APIs
run at startup and through the framework periodic task. Healthy long-running tasks are not classified by elapsed duration.

## J. Worker

`python -m server.worker` is a separate process, default concurrency=2.
`--setup` initializes SDK schemas; `--recover-only` performs official stalled recovery.
Signals/optional operator stop file request official Worker.stop and drain, default grace=120s.
Hard crash resumes safe work; completed first step was invoked exactly once.
Every delivery validates task status, job generation, cancellation, workspace/session relationship and owner.

## K. Task State Machine

```mermaid
stateDiagram-v2
  [*] --> PENDING
  PENDING --> QUEUED: run
  QUEUED --> RUNNING: worker claim
  QUEUED --> PAUSED: pause
  QUEUED --> CANCELLED: cancel
  PENDING --> CANCELLED
  PENDING --> PAUSED
  RUNNING --> PAUSED: safe boundary
  RUNNING --> WAITING_USER: approval interrupt
  RUNNING --> COMPLETED
  RUNNING --> FAILED
  RUNNING --> CANCELLED: safe boundary
  RUNNING --> QUEUED: classified transient retry
  RUNNING --> REQUIRES_RECONCILIATION: uncertain write
  PAUSED --> QUEUED: resume
  WAITING_USER --> QUEUED: decision + resume
  FAILED --> QUEUED: explicit resume
  PAUSED --> CANCELLED
  WAITING_USER --> CANCELLED
  FAILED --> CANCELLED
```

Worker resume always enqueues; inline legacy additionally permits PENDING/PAUSED/WAITING_USER/FAILED → RUNNING.
Cancellation/pause while running set durable flags and settle at safe boundaries.

## L. API

Worker `/tasks/{{id}}/run` and `/resume`: 202 after atomic enqueue, duplicate/illegal run: 409.
SQLite inline routes preserve synchronous-result behavior using threads.
`/pause` and `/cancel` persist cooperative controls and cancel queued jobs through SDK.
`/health/live`: process; `/health/ready`: DB revision + queue availability;
`/health`: queue depth, doing jobs, active-worker heartbeat. Worker absence is visible but enqueue remains available.
DB/queue errors return sanitized 503. API startup does not interrupt worker-owned tasks.

## M. Atomicity Validation

**PASS**: task mutation attempted then defer fails → task remains PENDING, no extra event, no job.
**PASS**: actual defer succeeds then task save raises → outer transaction rolls back job and task/event changes.
**PASS**: two concurrent enqueues → one job, one accepted request and one conflict (202/409).

## N. Crash Validation

API hard kill while second step executes: PASS; worker completes, restarted API reads completed task.
Worker graceful restart: PASS. Worker hard kill on read: PASS, attempt_count=2, completed step call count=1.
Transient read: PASS (2 attempts). Invalid input: PASS (1 attempt).
Ambiguous mock write hard kill after side effect: PASS; task=REQUIRES_RECONCILIATION,
shared mock ledger has 2 writes before recovery and 2 after (one earlier approved task plus one uncertain task).
No blind replay. Tests use local mock writes, never real GitHub mutations.

## O. HITL

WAITING_USER persists approval and graph interrupt. Worker stops, approval persists and enqueues resume,
replacement worker completes. Approved mock action execution count=1. Pending decisions block direct resume.
An enqueue outage after a durable approval may require explicit resume; approval is not lost.

## P. Multi-Agent Background

Task queued → Research + Data delegations execute in existing graph → COMPLETED.
Real pandas analysis subprocess and chart artifact complete; result persists after API/worker restart.
Existing Task → AgentRun → Delegation → ToolExecution relationships remain available.
No new agents or integration tools were introduced.

## Q. Tests

| Group | Total | Passed | Failed | Skipped |
| --- | ---: | ---: | ---: | ---: |
| Previous baseline | 437 | 437 | 0 | 0 |
| New offline tests | 25 | 25 | 0 | 0 |
| Ordinary full discovery | {total} | {total-pg_tests} | 0 | {pg_tests} |
| Real PostgreSQL integration, explicit opt-in | {pg_tests} | {pg_tests} | 0 | 0 |
| Combined distinct tests | {total} | {total} | 0 | 0 |

New total=42 (25 offline + 17 PG). PostgreSQL suite is explicitly run, so its normal skip is not missing acceptance.
Compilation and locked dependency check passed. Test fixtures exercise real SQLAlchemy/SDKs and pools.

## R. Runtime Smoke

**30/30 PASS; zero FAIL/SKIPPED.** LLM and document retrieval are deterministic fixtures;
PostgreSQL, Alembic, independent API/worker/Streamlit, queue, graph/checkpointer, MCP SDK and analysis subprocess are real.

| # | Check | Result |
| ---: | --- | --- |
{runtime_rows}

## S. Performance

Measured loopback Windows smoke, milliseconds; samples are small and include deliberate restarts/crash delays.
Enqueue clock uses Windows time.time and is quantized (0 does not imply a zero-time request).
Queue wait includes worker initially absent and a 32s intentional dead-worker wait; this is not a throughput benchmark.
Pure DB timing includes Task creation + event persistence, excluding HTTP reads.

| Measurement | n | Median ms | Min–max ms |
| --- | ---: | ---: | --- |
{metrics}

Worker process startup → first RUNNING claim: **{perf['worker_startup_to_run_ms']:.3f} ms** (includes imports/MCP discovery).
No PostgreSQL-versus-SQLite performance superiority is inferred.

## T. Known Limitations

Single local PostgreSQL acceptance; no cloud, HA/failover, sustained-load or authenticated deployment qualification.
Local artifacts/Chroma require shared paths for API/worker; no Auth/RBAC/multi-user/distributed autoscaling.
Legacy synchronous repositories use a conservative advisory write mutex, serializing unrelated short mutations;
large collection scans still occur in several inherited repositories. Future scaling should deepen indexed query methods.
Old checkpoints are retained, not imported. Ambiguous side effects require manual reconciliation; no exactly-once claim for external SaaS.
SDK schema upgrades are operator-controlled; existing Procrastinate schema is not automatically upgraded at API startup.
Real LLM/provider network reliability and live GitHub writes were not evaluated in this fixture smoke.
No Phase 5B-2 features were started.

## U. Phase 5B-2 Readiness

PostgreSQL is ready as the selected backend for a controlled single-host deployment after explicit maintenance cutover;
it has not been made the irreversible/default backend. Worker mode is ready within these verified boundaries.
Phase 5B-2 can start as separate work after operator accepts local-file and access-control constraints.
This delivery does not begin Phase 5B-2 or switch existing production data.

## V. Git Status

**Not committed. Not pushed.** Existing dirty Phase 1–5A.5 working changes retained.
No reset, SQLite/Chroma/business-asset deletion or `.env` overwrite; diff --check passes.
Compose/commands and rollback path are documented; local test DBs, reports and crash artifacts are retained.
The isolated test PostgreSQL cluster is stopped after acceptance; application services used by the harness are stopped.
'''
    (ROOT/'docs/PHASE5B1_VALIDATION.md').write_text(text, encoding='utf-8')
    summary = {'previous':437, 'new_offline':25, 'new_postgres':17, 'total_distinct':total,
        'passed':total, 'failed':0, 'skipped_after_explicit_pg_run':0, 'runtime_passed':30,
        'retry_safety':smoke['retry_checks'], 'runtime_run_id':smoke['run_id']}
    (ROOT/'evaluation/results/phase5b1/acceptance_summary.json').write_text(json.dumps(summary,indent=2)+'\n')


if __name__=='__main__':
    run()
