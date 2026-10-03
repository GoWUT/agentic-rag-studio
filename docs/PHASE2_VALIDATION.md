# Phase 2 Local Validation — 2026-10-01

Environment: Windows, project `.venv`, Python 3.11. No real provider calls.

## Commands and results

```powershell
.\.venv\Scripts\python.exe -m unittest discover -q
```

135 discovered tests: 135 passed, 0 failed, 0 skipped. This includes the 94
existing local regression tests and 41 tracked Phase 2 tests. The explicit
DeepSeek/tool smoke-test `main()` entry points were not invoked by discovery.

```powershell
.\.venv\Scripts\python.exe -m evaluation.benchmark_agent_workspace --demo --output evaluation/results/agent_workspace_demo.json
git diff --check
```

Both completed successfully. Agent demo metrics are synthetic graph wiring
measurements, not real model accuracy/grounding results.

## Acceptance scenarios

| Scenario | Offline validation |
| --- | --- |
| A: Three-paper comparison | Planner + Workspace tool fixture, three distinct evidence identities, three validated document citations |
| B: Simple title question | No TaskPlan model call; single retrieval path |
| C: Uploaded documents only | Both API scope and explicit user wording prohibit Web/arXiv |
| D: Insufficient retrieval | Grader/refiner loop stops after two rewrites; research rewriter cannot bypass fallback policy |
| E: Invented citation | Unknown/stale evidence identities, invented pages/tables/URLs and mismatched document metadata are rejected/removed |
| F: Restart | SQLite Workspace/document/session migration and reload, history/source/plan panel restoration, existing index reuse |

Global rerank is called once after per-document candidate merging. Failed
documents and reranker fallback preserve successful evidence. Replan and
executor budgets, empty evidence, legacy PDF chat and additive API schemas
are covered. Streamlit AppTest checks Workspace selection and restored panels.

## Files changed in this development

New modules:

* `server/workspaces.py`: Workspace and DocumentRecord models, transactional SQLite registry.
* `server/rag/workspace_retrieval.py`: bounded per-document candidates and global rerank.
* `server/agent/research_workflow.py`: sequential routing/planning/execution/correction/verification nodes.
* `server/agent/evidence.py`: tool evidence, citation validation and rendering.
* `evaluation/benchmark_agent_workspace.py`: recorded-response scoring and synthetic demo.
* `evaluation/datasets/agent_workspace_examples.jsonl`: example Agent cases.
* `evaluation/results/agent_workspace_demo.json`: labelled synthetic runner output.
* `test_phase2.py`: offline Phase 2 acceptance tests.
* `docs/PHASE2_IMPLEMENTATION.md`, `docs/PHASE2_VALIDATION.md`: design and validation records.

Extended existing modules (including pre-existing uncommitted Graph V2 files):

* `server/agent/graph.py`: new graph wiring, retaining prior build interface.
* `server/agent/state.py`: trusted scope and turn-local research state.
* `server/agent/schemas.py`: TaskPlan, PlanStep, RetrievalGrade, VerificationResult.
* `server/agent/nodes.py`: Workspace source support and bounded citation context.
* `server/agent/tools.py`: Workspace tool and structured PDF/Web/arXiv evidence.
* `server/rag/retrieval.py`: opt-in RRF/rerank score metadata.
* `server/sessions.py`: nullable Workspace association, runtime restoration/scope and persisted research panels.
* `server/main.py`: Workspace/document/session routes and additive chat/history schemas.
* `client/app.py`: Workspace sidebar, multi-upload, Sources and Task Plan panels.
* `server/config.py`, `.env.example`: Phase 2 limits and configuration.
* `server/observability/langsmith.py`: hidden trace bodies by default.
* `README.md`: architecture, APIs, configuration and run/evaluation instructions.
* `.gitignore`: retain `test_phase2.py` while preserving the existing local test exclusions.

Existing evaluation data/results and other pre-existing uncommitted changes were
retained. No commit, push, destructive Git operation or database reset was run.

## Practical limits

Real DeepSeek/Serper/arXiv research quality and real multi-paper answer coverage
need a separate benchmark. Global Top K may omit documents; OCR has no table
identity reconstruction. Shared-index garbage collection and process cache
eviction are outside this phase. See the implementation document for details.
