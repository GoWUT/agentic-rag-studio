# Phase 5A validation — 2026-10-02

## A. Final verdict

READY WITH LIMITATIONS. The acceptance scope is bounded local orchestration, not
general autonomous research or production GitHub validation. Detailed measured
results below are generated from the saved evaluation artifacts. Failed development
attempts remain in isolated `.runtime/phase5a_smoke/` workspaces.

## B. Actual architecture

```text
START -> retrieve_memory -> query_analyzer -> orchestration_router
  | existing_workflow -> Phase 2/3/4 plan/executor/correction ----------+
  | single_agent / multi_agent                                        |
  v                                                                  |
supervisor_plan [frozen Agent/Tool snapshots, structured work groups]   |
  v                                                                  |
dispatch_agents -- Send --> private Research / Data / Coding graphs    |
  ^                          prepare -> plan -> work* -> contract      |
  |                              |                                    |
  +---- next dependency group <-- merge_agents                         |
                                 |                                    |
supervisor_actions [Policy -> WAITING_USER/interrupt -> decision]       |
  v                                                                  |
draft_synthesis -> review_agents [structured read-only Reviewer]        |
  v                                                                  |
redelegate -- at most 2 gap delegations / 1 revision round -> dispatch  |
  |                                                                  |
orchestration_complete / orchestration_stop ---------------------------+
  v
generator -> verifier <-> answer_revision -> citation_validator
  v
extract_memory [root durable claim, once] -> END
```

## C. Files and responsibilities

| File | Responsibility |
|---|---|
| `server/agent_registry.py` | Identities, capability lookup, enable/disable, immutable task snapshot, request validation |
| `server/agent/orchestration_schemas.py` | Typed delegation/result/context/plan contracts and reference validation |
| `server/agent/specialized_agents.py` | Private worker LangGraph, permitted tools, bounded model, authoritative result references |
| `server/agent/orchestration.py` | Structured routing, Supervisor, groups, merging, Reviewer, bounded revisions, root synthesis |
| `server/agent_runs.py` | Additive SQLite run/delegation ledger, counters, recovery, public projections |
| `server/phase5.py`, `server/phase5_api.py` | Shared services, bounded semaphore, catalog/read APIs |
| `server/agent/graph.py`, `state.py` | Root graph nodes, reducers and orchestration state |
| `server/agent/persistent_workflow.py`, `research_workflow.py`, `evidence.py` | Existing analysis/finalization integration and provenance preservation |
| `server/sessions.py`, `config.py` | Composition, configuration, recovery |
| `server/tool_registry.py`, `tool_actions.py`, `phase4.py`, `phase3_api.py` | Existing native bindings/policy/checkpoint/task integration |
| `client/phase5_ui.py`, `client/phase3_ui.py` | Agent status tree, expandable metrics, integration into persistent task view |
| `test_phase5a.py` | 78 new contract, boundary, concurrency, persistence and HITL tests |
| `evaluation/multi_agent/cases.json`, `metrics.py`, `benchmark.py` | Scripted-model comparison, actual constrained analysis and measured metrics |
| `evaluation/multi_agent/smoke.py`, `smoke_app.py`, `mock_server.py`, `review_probe.py` | Real LLM/API/indexing/restart, explicit isolated faults and SDK MOCK |
| `evaluation/multi_agent/report.py` | Saved-result aggregation without further model calls |
| `.env.example`, `.gitignore`, `README.md`, Phase 5A docs | Defaults, local runtime exclusion, architecture and validation |

Existing Phase 1–4 changes remain uncommitted. Phase 5A did not change dependency
files or overwrite `.env`; their existing diffs belong to earlier work.

## D. Registry

| Identity | Declared capabilities |
|---|---|
| Supervisor | `orchestrate.route`, `orchestrate.delegate`, `orchestrate.synthesize` |
| Research | `research.document`, `research.web`, `research.academic`, `research.github`, `research.compare` |
| Data | `data.inspect`, `data.analyze`, `data.visualize`, `data.compare` |
| Coding | `code.inspect`, `code.explain`, `code.review`, `code.propose_patch` |
| Reviewer | `review.evidence`, `review.completeness`, `review.consistency`, `review.requirements` |

Only Supervisor can delegate. Capability declarations do not manufacture missing
MCP tools. Frozen snapshots preserve running task behavior after operator changes.

## E. Delegation protocol

Request carries delegation/task/parent identity, objective, required capabilities,
permitted tool capabilities, expected result contract, depth, priority and budgets.
Context contains selected constraints/memory/evidence/dataset/artifact references.
Result is a typed envelope with status, summary, references, unresolved questions,
confidence and a specialized payload. Reject unknown/disabled identities, mismatches,
invalid budgets/depth, cancelled tasks, fabricated references and conflicting results.

## F. Isolation

Each worker has its own WorkerState/private_messages. Scoped context is rebuilt
from selected references, never copied root/sibling chat history. Evidence provenance
survives compressed projections. Only validated results pass through Context Merger.
Actual model-input observation measured a private Research sentinel: Research saw
it; Data, Coding, Reviewer and Supervisor did not. No raw prompts/reasoning were
logged by the observer. Public API projections omit private messages/progress.

## G. Supervisor

Simple QA and a single PDF retain the existing workflow. Conservative intent guards
and structured OrchestrationDecision activate one specialty or distinct capabilities.
Agent selection uses registry capability lookup. SupervisorPlan groups independent
work and validates dependency groups. LangGraph Send, a group barrier and bounded
semaphore enforce parallelism <=3. Successful siblings survive branch failure.

## H. Reviewer

Reviewer receives compressed results, selected authoritative evidence and artifacts.
It reports pass/needs_revision/insufficient, concrete missing requirements,
contradictions, unsupported claims and suggestions. Supervisor alone schedules
at most two supplemental delegations and one revision round; total delegations <=8,
depth <=1. Completed IDs are cached. PARTIAL work can receive a new gap delegation.

Check 12 is a real-model complete-result Reviewer subgraph probe, separately labeled;
the earlier end-to-end C sample retained its actual needs_revision verdict. F detected
the injected Research evidence gap. Its Data work was also PARTIAL, so supplemental
Data was permitted; neither supplementary branch is mislabeled successful.

## I. Persistence

Existing Task/TaskStep/TaskEvent and official SqliteSaver remain authoritative.
Additive agent_runs/agent_delegations preserve IDs, plan, progress cursor, evidence,
artifacts, counters, result contract and tool receipts. Startup RUNNING tasks become
PAUSED; unfinished runs become INTERRUPTED. Resume skips completed delegation IDs,
restores interrupted read work with bounded retries and preserves frozen snapshots.
Uncertain external execute outcomes require reconciliation rather than blind replay.

## J. Tools / HITL

All worker tools reuse the Phase 4 frozen Tool Registry, schema validation,
capability allowlists, Policy Engine and official MCP SDK. Data reuses the existing
constrained Python runtime. Research/Coding cannot write; Reviewer cannot invoke
external tools. Explicit root actions on Persistent Tasks persist approval and
WAITING_USER, interrupt, then resume by Command after approve/edit/reject.

Actual local MOCK issue creation waited for approval, survived backend restart and
created exactly one local ledger record after approval. No remote write occurred.
Real GitHub: SKIPPED (credentials absent). Coding/MCP checks: MOCK, not real GitHub.

## K. Parallel execution

C initial group: measured interval-duration sum **70.557s**, actual wall span **61.086s**.
The sum is a sequential proxy, not an independently executed sequential run.

| Agent | Start (UTC) | End (UTC) |
|---|---|---|
| research | 2026-10-02T09:26:40.139355+00:00 | 2026-10-02T09:26:49.625184+00:00 |
| data | 2026-10-02T09:26:40.154444+00:00 | 2026-10-02T09:27:41.225835+00:00 |

## L. Tests

| Previous | New Phase 5A | Total | Passed | Failed | Errors | Skipped |
|---:|---:|---:|---:|---:|---:|---:|
| 295 | 78 | 373 | 373 | 0 | 0 | 0 |

Latest regression: 373 tests in 123.697s, OK. Compileall, pip check and git diff --check passed.
LF/CRLF Git warnings are informational; existing dependency diffs were retained.

## M. Runtime smoke

| # | Check | Status |
|---:|---|---|
| 1 | FastAPI start | PASS |
| 2 | Streamlit start | PASS |
| 3 | Agent Registry | PASS |
| 4 | Simple query bypass | PASS |
| 5 | Data only | PASS |
| 6 | Research only | PASS |
| 7 | Research + Data | PASS |
| 8 | Parallel overlap | PASS |
| 9 | Private context isolation | PASS |
| 10 | Evidence merge | PASS |
| 11 | Reviewer structured input | PASS |
| 12 | Reviewer PASS | PASS |
| 13 | Reviewer revision | PASS |
| 14 | Bounded re-delegation | PASS |
| 15 | Completed work not repeated | PASS |
| 16 | Agent timeout | PASS |
| 17 | Parallel branch failure | PASS |
| 18 | HITL | PASS |
| 19 | Restart + resume | PASS |
| 20 | Grounding | PASS |
| 21 | Citations | PASS |
| 22 | Memory once | PASS |
| 23 | Single/multi runner | PASS |

Real configured provider; actual PDF/XLSX indexing, constrained analysis, artifacts,
official-SDK stdio MOCK and process restart. UI checked in an isolated 8512 preview;
Supervisor/worker status expanders, metrics and generated artifacts rendered without
Streamlit exceptions. Temporary preview closed; primary 8501/8001 remained healthy.

Reviewer PASS is a controlled complete-result subgraph, not a false PASS attributed to C.
Timeout/branch-fault checks validate containment, not guaranteed answer correctness.

Measured aggregate metrics (includes explicit fault cases; small fixture denominators):

- Routing: 100% (6/6); specialty selection: 5/5.
- Reviewer injected-gap detection: 1/1; F supplemental completion: 0/2 (both PARTIAL, not counted successful).
- Delegation success: 14/34; terminal completion: 34/34.
- Repeated completed work: 0/28 start events.
- Per-agent calls/tokens, scenario latency, span/utilization and groundedness: metrics_summary.json.

## N. Single vs multi-agent evaluation

Same goal, identical synthetic 3 PDFs and experiment.xlsx, fresh workspace per mode, real provider, n=1 each.

| Measure | Existing single-agent | Multi-agent |
|---|---|---|
| Task status | COMPLETED | COMPLETED |
| Latency seconds | 114.907 | 212.906 |
| LLM calls | 6 | 19 |
| Tool calls | 3 | 5 |
| Provider total tokens | 42738 | 108497 |
| Grounded | True | True |
| Evidence references | 4 | 7 |
| Validated citations | 4 | 7 |

Both actual answers selected **Model A (2.5M parameters, mIoU 73.27)** under 3M,
excluded B (3.2M), reported chart generation and described the identical synthetic
paper/spreadsheet values as descriptive consistency, not causal/independent validation.
Multi additionally reported efficiency/correlation calculations. No independent quality
score was assigned. Complete answers/evidence IDs/contracts are in real_comparison.json.

This sample provides more evidence references at higher latency, calls and tokens.
It does not establish quality superiority. The final multi Data result was PARTIAL
and Reviewer requested revision; root grounding still passed against actual tool evidence.
Routing trivial/single-specialty questions avoids unnecessary cross-specialty delegation,
but the actual A/B samples still incurred planning/root overhead. Parallel collection
and cached recovery are useful demonstrated mechanisms; final-quality activation value
needs broader, repeated real-data evaluation.

## O. Actual restart validation

Task `4842f488-ba27-4db7-87c9-b808e26d738d`.
Before interruption: Supervisor RUNNING, Research COMPLETED, Data RUNNING.
After backend restart: Task PAUSED, unfinished Data safely recovered,
Research reused its stored completed result. After resume: all four runs COMPLETED.

Research DELEGATION_STARTED = **1**. Data DELEGATION_STARTED = **2** (initial interrupted read + safe recovered attempt, same delegation ID).
Resume request latency: **110.578s**; excludes pre-restart runtime.

| UTC | Event | Agent | Status |
|---|---|---|---|
| 2026-10-02T10:04:50.118028+00:00 | DELEGATION_STARTED | research |  |
| 2026-10-02T10:04:50.128903+00:00 | DELEGATION_STARTED | data |  |
| 2026-10-02T10:05:00.473946+00:00 | DELEGATION_COMPLETED | research | completed |
| 2026-10-02T10:05:07.003240+00:00 | TASK_RESUMED | root |  |
| 2026-10-02T10:05:07.222933+00:00 | DELEGATION_STARTED | data |  |
| 2026-10-02T10:05:30.837074+00:00 | DELEGATION_COMPLETED | data | completed |

The separate approval/restart test completed a local MOCK action once. Evaluation
resumptions retained original accepted task IDs/latencies; they are listed in runtime_smoke.json.

## P. Known limitations

- Local single-user/backend scope; no distributed workers, RBAC, OAuth, A2A or new integrations.
- Model outputs/review/grounding can be partial or insufficient. Task COMPLETED means
  execution terminated, not that every scientific requirement was satisfied.
- Timeout injection retained the failure and ended the task, but final groundedness
  was false. Fault handling PASS does not assert answer quality PASS.
- Real GitHub credentials absent. Only official-SDK local MOCK transport/action was validated.
- Synthetic n=1 comparison per mode; plans and prior user-scope memory history differ.
  Evidence counts are measured references, not semantic relevance scores or coverage recall.
- Worker timeout is cooperative; an in-flight provider/thread operation cannot be forcibly
  cancelled. Smoke uses a 600s task budget; earlier 240s runs timed out. Operators should
  configure budgets appropriate to provider latency; `.env` was not changed.
- Context budgets use bounded projections/estimates, not provider-specific exact tokenization.
- No claim of semantic duplicate elimination or universal multi-agent quality/speed advantage.
- Remote uncertain outcomes and a crash inside a claimed root memory extraction require
  operator review. Exactly-once remote effects are not guaranteed by local checkpoints.
- Artifact existence/metadata were verified; scientific visual interpretation was not scored.
- Evaluation resumptions reused accepted measurements with original task IDs/latencies;
  final single/multi comparison and G restart used the final implementation.

## Q. Git status

Not committed. Not pushed. HEAD remains `24f01d5f2901191fbdcde8a2117250b0b728c93c`.
No reset, deletion of SQLite/indexes/workspaces/sessions/tasks/memory/MCP configuration,
or `.env` overwrite. Existing Phase 1–4 modifications retained.
