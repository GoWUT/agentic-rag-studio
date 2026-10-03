# Phase 3 — Persistent Research & Data Agent

## 1. Architecture and pre-development audit

The Phase 2 sequential LangGraph is retained. Direct/Simple/Complex routing,
planner/executor, evidence grading, bounded rewrites/replanning, verification and
citation validation remain the research workflow. State is ephemeral; canonical
messages are persisted through SessionStore. WorkspaceStore shares the existing
SQLite database with sessions and registers PDF/index identities. Chroma owns PDF
embeddings; workspace retrieval performs per-document hybrid retrieval followed by
one global rerank. Tools create evidence; the model cannot create authoritative
source metadata. FastAPI exposes legacy PDF and workspace APIs; Streamlit preserves
both modes and persisted source/plan panels.

Phase 3 enriches this graph with continuity and durable checkpoints:

```text
START -> Retrieve memory -> Query analyzer
                           | Direct -> Generator
                           | Simple -> Existing retrieval
                           | Complex/Task -> Planner -> Persist plan
                                                -> Executor
                                                     | Research tools
                                                     | Inspect dataset
                                                     | Analyze data
                                                -> Persist step -> Step evaluator
                                                     | next step
                                                     | pause/cancel/failure boundary
                                                -> Synthesis
                           -> Grounding -> bounded revision -> Citation validator
                           -> Memory extraction -> END
```

Task synthesis itself uses the existing generator, verifier, revision and citation
validator and is checkpointed. Final graph nodes reuse that result. Resume restores
only the necessary cumulative evidence/results and jumps past planning and completed
steps. Ordinary chat does not create Task rows.

## 2. Long-term memory

`server/memory.py` defines LongTermMemory and structured MemoryCandidates. Types:
preference, project, decision, instruction, episodic. Scope is user or workspace;
owner defaults to `local_default`, without implementing authentication. Messages
and memories have distinct tables. Relevant memories are included in analyzer,
planner and generator context, never converted to factual Evidence/Citation.

## 3. Memory lifecycle

```text
Stable user statement / important task outcome
 -> structured candidates (explicit Remember/记住 uses a deterministic candidate)
 -> sensitive filter -> confidence/importance policy
 -> normalized hash + canonical preference key + embedding similarity
 -> conflict supersession -> SQLite
 -> semantic/lexical relevance + importance + recency
 -> bounded top items -> global ContextHarness
```

The extractor runs for stable-preference/project/decision cues or task completion,
not ordinary small talk. Secrets are checked before extraction and storage.
Semantic vectors reuse the existing embedding model and are stored beside the
memory payload in SQLite. Local model failure degrades to lexical/canonical matching
with a safe warning. Canonical preference keys deduplicate Chinese/English language
preferences and supersede concise/detailed conflicts. Structured `conflict_key`
handles decisions about the same subject. Updates retain the superseded record;
DELETE removes the requested record. The memory item/character budget includes
serialized metadata; all model requests still pass ContextHarness.

## 4. Data assets

CSV (UTF-8/UTF-8-BOM), XLSX (multi-sheet), JSON records/tabular objects have a
separate DataAsset registry. Upload validates extension/MIME/size, hashes content,
parses the actual file and reports schema, dtype, missing values, statistics and
five sample rows. Excel expanded ZIP content is bounded; inspection rejects over
200000 rows or 1000 columns. Dataset deletion removes the registry association;
managed originals and historical execution copies are retained for lineage.
Named filenames in a request narrow the accessible analysis assets.

## 5. Code interpreter

```text
Workspace dataset IDs -> inspect -> structured GeneratedCode
 -> AST validation -> dedicated directory -> Python -I subprocess
 -> bounded stdout/stderr + status -> persisted execution -> registered artifacts
```

Dataframes are preloaded as `datasets[id]`, `sheets[id][sheet]`, and `df`. Row count,
column listing and a single explicitly named column's mean use deterministic code
without a code-generation call. Other objectives use the existing provider through
the same bounded structured-output interface. SyntaxError/NameError permit one code
repair; unsafe code and dependency errors do not trigger repair/install loops.
JSON/TXT output uses guarded `write_artifact(filename, string_content)`, with a
literal filename and size/path checks; unrestricted `open()` remains rejected.

## 6. Execution security

This is a **Constrained Python data-analysis runtime**, not a complete OS security
boundary. It is intended for local trusted users, not hostile public multi-tenancy.

- AST rejects forbidden imports/builtins, private/dunder access, dynamic output paths,
  host paths/URLs, unsafe serializers, pandas eval/query and unsupported structures.
- Allowed imports include pandas, numpy, matplotlib.pyplot and selected standard
  libraries. Framework-only NumPy reduction imports are supported; generated code
  cannot import those implementation modules directly.
- Dataset IDs are resolved within a workspace. Paths are never model inputs; files
  are copied into one dedicated execution directory.
- The subprocess uses `sys.executable -I`, `shell=False`, a minimal environment
  excluding credentials, Agg backend, capped output buffers and timeout/kill.
- Artifact count and aggregate byte limits are monitored during execution and checked
  afterwards. Only managed PNG/CSV/JSON/TXT files receive IDs. No runtime installation.
- This does not enforce Windows kernel memory/CPU/disk quotas or defeat every possible
  Python-library exploit. A future strong isolation boundary is separate work.

## 7. Artifacts and analysis evidence

Artifacts carry workspace/task/step/execution IDs, MIME, size and local path. Download
resolves an artifact record and validates its managed path; the API never accepts a
host path. PNG/CSV/JSON/TXT preview/download is added to chat and Task panels.
Successful execution creates `source_type=analysis` Evidence with dataset/execution/
artifact lineage. Citations render as `[Analysis <execution_id>]`, never PDF pages.
Evidence includes authoritative dataset filenames, inspection metadata and artifact
names/types/sizes, so synthesis and verification can identify the actual XLSX source.
Failed runs cannot establish factual evidence. Generated code is retained locally
for audit, including code rejected before execution.

## 8. Persistent tasks

Task, TaskStep and append-only TaskEvent records reuse the existing Planner/Executor.
`/run` and `/resume` execute synchronously; no worker, scheduler or queue is claimed.
Plans and each step result are committed separately. Completed steps cannot be
started again. Failed steps may be resumed up to the configured attempt cap.
Artifacts retain Task/Step lineage, including after cancellation.

## 9. Task lifecycle

```text
PENDING -> RUNNING -> COMPLETED
                  -> PAUSED -> RUNNING
                  -> WAITING_USER -> RUNNING
                  -> FAILED -> bounded resume
PENDING/RUNNING/PAUSED/WAITING_USER/FAILED -> CANCELLED
```

Pause/cancel stops new scheduling at a step boundary; the current operation may
finish and checkpoint. Missing required datasets enter WAITING_USER. Backend startup
recovers RUNNING tasks to PAUSED and resets interrupted steps to PENDING while
preserving attempt counts. Recovery is registered on backend startup, not store or
manager construction. A crash after executing a tool but before committing its
checkpoint can repeat that interrupted tool; completed checkpoints are never rerun.
Run one backend process against a database; multiple active worker processes and
cross-process leases are not supported in this phase.

## 10. APIs

| API | Purpose |
| --- | --- |
| GET/POST `/memories` | List/filter/create scoped memories |
| PATCH/DELETE `/memories/{id}` | Edit with history / remove record |
| GET `/memories/retrieve` | Retrieve continuity context |
| POST/GET `/workspaces/{id}/datasets` | Upload/list assets |
| GET/DELETE `/workspaces/{id}/datasets/{dataset_id}` | Inspect/delete association |
| POST `/workspaces/{id}/analysis` | Explicit constrained code; use chat for generation |
| GET `/artifacts/{id}` | ID-based attachment download |
| POST/GET `/tasks` | Create/list durable tasks |
| GET `/tasks/{id}` | Task and persisted steps |
| POST `/tasks/{id}/run`, `/pause`, `/resume`, `/cancel` | Lifecycle controls |
| GET `/tasks/{id}/events`, `/artifacts` | Audit history / deliverables |

Existing routes remain compatible. Chat adds an optional artifacts array. Feature
flags disable the corresponding entry points. Missing IDs return 404, conflicts
409, validation 400, database unavailability 503; provider/execution errors retain
the existing chat mapping.

## 11. Database

Additive `CREATE TABLE/INDEX IF NOT EXISTS` migrations preserve messages, sessions,
workspaces, documents and indexes. New tables: `long_term_memories`, `data_assets`,
`analysis_executions`, `artifacts`, `tasks`, `task_steps`, `task_events`. Typed model
payloads retain all lifecycle/provenance fields as JSON; query/scoping/status columns
are indexed separately. Indexes cover memory scope/status/hash, dataset workspace,
artifact task, task status/workspace, unique task step position and event task.
Transactions use `BEGIN IMMEDIATE` for dedup, lifecycle and checkpoint mutations.

## 12. Config

`.env.example` documents MEMORY_ENABLED/AUTO_EXTRACT/MAX_ITEMS/TOKEN_BUDGET/
MIN_CONFIDENCE/MIN_IMPORTANCE/DEDUP_SIMILARITY; DATA_ANALYSIS_ENABLED,
DATASET_MAX_FILE_MB; ANALYSIS_TIMEOUT_SECONDS/MAX_CODE_CHARS/MAX_OUTPUT_CHARS/
MAX_ARTIFACT_MB/MAX_ARTIFACTS/CODE_REPAIR_MAX; TASK_SYSTEM_ENABLED,
TASK_MAX_STEPS/MAX_STEP_ATTEMPTS. The two memory graph nodes raise the default
graph recursion budget from 33 to 35. Existing `.env` is never overwritten;
explicit user-configured graph/context/provider values still take precedence.

## 13. Tests and runnable evaluation

```powershell
.\.venv\Scripts\python.exe -m unittest discover -v
.\.venv\Scripts\python.exe -m compileall -q server client evaluation
.\.venv\Scripts\python.exe -m evaluation.benchmark_phase3
.\.venv\Scripts\python.exe -u -m evaluation.phase3_smoke prepare
.\.venv\Scripts\python.exe -u -m evaluation.phase3_smoke llm
# Restart the actual backend while the task is paused, then:
.\.venv\Scripts\python.exe -u -m evaluation.phase3_smoke resume
```

The previous baseline was 140 passing tests; the final suite has 96 new Phase 3
tests, 236 total, 236 passed, zero failures and zero skipped tests. New tests cover memory policy/CRUD/
scope/conflicts, real CSV/XLSX/JSON parsing, real subprocess computation and PNG,
unsafe code, timeout, output/artifact limits, bounded repair, ID downloads,
incremental migration, task checkpoint/recovery/resume and integrated graph flows.
`evaluation/results/phase3/fixture_evaluation.json` is a small deterministic fixture
evaluation (zero new provider calls), not a production accuracy benchmark. Tool-selection
uses a two-case required/forbidden-tool rubric against recorded real smoke traces;
it is null if those traces are unavailable. Real smoke uses the unchanged provider
and exclusively synthetic fixture documents/data; JSON results retain failure history
and expose `latest_checks` for the final result. Final totals are recorded in the
validation report after all checks finish.

## 14. Known limitations and future work

- No Auth/RBAC, cross-process task leasing, background worker, arbitrary Shell,
  browser automation, MCP, Multi-Agent, cloud redesign, Redis/Celery or PostgreSQL.
- Local SQLite JSON embeddings are suitable for modest memory volumes; retrieval
  currently scans scoped active memory. Semantic conflict recognition outside
  canonical preferences depends on the extractor supplying a stable conflict key.
- Sensitive filters are conservative heuristics, not universal secret detection.
- Upload inspection happens in the API process; large valid datasets can be costly
  despite file/ZIP/shape bounds. CSV encodings other than UTF-8 are rejected explicitly.
- Failed execution directories are retained for local audit; automatic retention/
  cleanup and stricter per-user storage quotas are future work.
- Synchronous Streamlit Run blocks that browser's current request. Boundary-limited
  execution and a separate pause request provide cooperative control.
- Phase 2 production audit was interrupted by the Phase 3 request. Its unexecuted
  external real-paper comparison is not represented as completed. Real local retrieval
  compatibility and synthetic-provider integration are reported separately.

## File manifest

Added (relative to the existing project root):

| Files | Responsibility |
| --- | --- |
| `server/persistence.py`, `server/memory.py` | SQLite primitives, memory lifecycle and retrieval |
| `server/data_assets.py` | DataAsset, execution and Artifact registry/inspection |
| `server/analysis_runtime.py`, `server/analysis_worker.py` | AST policy, subprocess orchestration, constrained child |
| `server/tasks.py` | Task/Step/Event models, checkpoints and synchronous service |
| `server/phase3.py`, `server/phase3_api.py` | Composition root and additive APIs |
| `server/agent/persistent_workflow.py` | Memory, data and task integration in existing planner/executor |
| `server/rag/model_runtime.py` | Shared model initialization lock and local snapshot resolution |
| `client/phase3_ui.py` | Memory/dataset/task panels and Artifact preview/download |
| `test_phase3.py` | 96 new acceptance/regression tests |
| `evaluation/benchmark_phase3.py`, `evaluation/phase3_smoke.py` | Fixture and real runtime runners |
| `evaluation/datasets/phase3_fixtures.json` | Synthetic fixture gold data and tool rubric |
| `docs/PHASE3_IMPLEMENTATION.md`, `docs/PHASE3_VALIDATION.md` | Architecture, limits and measured validation |
| `evaluation/results/phase3/` | Recorded smoke/regression/evaluation evidence |

Modified for Phase 3: `server/agent/graph.py`, `state.py`, `nodes.py`, `prompts.py`,
`research_workflow.py`, `evidence.py`; `server/sessions.py`, `config.py`, `main.py`;
`server/rag/embeddings.py`, `retrieval.py`; `client/app.py`; `.env.example`,
`.gitignore`, `README.md`, `pyproject.toml`, `uv.lock`. Earlier Phase 2/audit changes
and user evaluation assets remain in the working tree and are not discarded.
