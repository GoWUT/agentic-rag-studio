# Phase 2 Implementation

## 1. Goal

Extend the existing single-PDF application into a Multi-Document Agentic Research
Workspace. Preserve DeepSeek, SQLite, Chroma, PaddleOCR, hybrid retrieval,
context/execution harnesses, LangSmith and existing evaluation. No multi-agent,
MCP, database replacement, external workers or new dependencies were introduced.

## 2. Architecture

```text
FastAPI / Streamlit
    -> persisted Session -> trusted Workspace / source scope
    -> query_analyzer (conversation resolution + complexity router)
       direct ---------------------------> generator
       simple -> simple_plan -> executor
       complex -> planner ----> executor
                                  -> step_evaluator -> next executor step
                                  -> evidence_grader
                                     -> sufficient -> generator
                                     -> query_refiner -> executor (max 2)
                                     -> replanner -> executor (bounded)
                                     -> external_fallback -> executor (once)
    -> generator -> verifier -> answer_revision -> verifier (max 1 revision)
    -> citation_validator -> END
```

The earlier Graph V2 remains the default `build_agent` interface when no
`workflow_config` is provided. Production sessions pass config and use the new
research graph. Both legacy and Workspace sessions use the production path.

## 3. Database changes

`sessions.sqlite3` receives two `CREATE TABLE IF NOT EXISTS` tables: `workspaces`
and `workspace_documents`. Document rows have a foreign key to their workspace,
and a unique `(workspace_id, fingerprint, index_id)` registration identity.
`SessionStore` adds nullable `sessions.workspace_id` with a guarded `ALTER TABLE`.
Existing rows/messages/embedding identities are retained. Ingestion keeps its
existing index-status SQLite database; the registry does not replace it.

Workspace registration uses `BEGIN IMMEDIATE` to enforce capacity under concurrent
uploads. Foreign-key enforcement cascades document associations on workspace
deletion. Sessions/history and shared PDF/Chroma files are retained. A conversation
whose Workspace has been deleted returns 404 when resumed; history remains readable.

## 4. Workspace design

One Workspace contains many DocumentRecords and Sessions. A document record
references the ingestion pipeline's fingerprint/index directory and remembers
the embedding model and physical page count. It owns no second Chroma collection.
Identical PDFs reuse the existing ingestion fingerprint/configuration namespace
across workspaces and legacy sessions. Repeated registration is idempotent.

Multipart upload handles each document independently and returns status/errors
for every upload. Failed files are not registered as searchable documents.
Removing a document removes its association only; shared indexes are retained.
Workspace retrievers re-read the registry for every request so additions/removals
take effect without rebuilding a session or re-embedding.

## 5. Retrieval design

Each document uses the existing PDFRetriever with CrossEncoder disabled:
Dense + BM25 -> RRF -> per-document candidates. A bounded executor shares a
single admission/deadline budget per query. Failures/timeouts are logged by
document identity while successful documents continue. Round-robin candidate
merge deduplicates `(document_id, chunk_id)` and preserves fair ordering for
fallback. One global CrossEncoder pass selects global Top K. No sorting by
cross-index raw scores occurs.

RRF and rerank scores are opt-in metadata to preserve existing retrieval API
semantics. Workspace calls enable them. Chunks retain source, physical page,
document identity/name, chunk identity, extraction method and available scores.

## 6. Planner design

The analyzer still resolves follow-up questions through bounded conversation
context. Comparison/synthesis keywords and research classifications select the
complex path. Simple retrieval skips the planning model. `TaskPlan` and `PlanStep`
use Pydantic JSON-mode outputs, with concrete queries, descriptions, allowed sources,
status and preferred tool. `reasoning_summary` is limited to a brief task rationale.

The sequential executor processes one step, records tool results, and the step
evaluator marks completion/failure before continuing. Plan size and total executor
iterations are clamped to configuration. Replanning is bounded and shares the
same total iteration budget. Parsing/provider failure in the planner falls back
to one retrieval step. No autonomous tool-calling loop or parallel agents exist.

## 7. Self-correction design

`RetrievalGrade` separates relevant/sufficient/confidence/reason. Empty results
cannot become sufficient. Only insufficient evidence starts an intent-preserving
query refinement using original query, prior evidence and missing information.
Source policy is reapplied after model output. Maximum rewrites are hard-capped
at two; optional complex-task replanning shares the executor budget.

After local correction, academic requests prefer arXiv, other requests prefer Web.
External fallback runs once and never runs for `workspace_only`. `external` skips
local tools. Explicit document-only wording narrows a broader API scope.
An unavailable chosen external tool yields an evidence limitation rather than
switching to a prohibited source. Grader parsing failure stops reflection and
preserves uncertainty.

The generator is still a plain LLM call behind ContextHarness. Groundedness
verification returns supported/unsupported claims and confidence. At most one
answer revision removes or qualifies unsupported assertions. The revised answer
is checked again; persistent failure returns an explicit limitation. The final
message replaces the draft by LangGraph message ID; drafts are not persisted.

## 8. Evidence and citations

Evidence comes only from tools and is accumulated explicitly within a turn.
Previous conversation evidence is not loaded into the current evidence store.
PDF zero-based pages become one-based public physical pages. Runtime document
registry/page counts and legacy filename metadata are injected by the application.
LLM-generated source identities are never used to construct Evidence.

The generator cites exact `[ev_<id>]` markers. The validator checks identity,
document membership, exact metadata and page range, then renders public labels.
Unknown markers, arbitrary numeric/PDF citation labels and mismatched references
are removed. URLs must match retrieved evidence; table references require explicit
table-label metadata. General OCR currently supplies no table labels, so named
table references are suppressed. This validates provenance, not the semantic
truth of every sentence; the separate verifier handles claim support.

Evidence/source/plan metadata is saved in the final AIMessage's `additional_kwargs`
and exposed as optional history `research`, restoring UI panels across restarts.

## 9. API and UI

* `POST/GET /workspaces`, `GET/DELETE /workspaces/{workspace_id}`
* `POST/GET /workspaces/{workspace_id}/documents` (multipart `files`)
* `DELETE /workspaces/{workspace_id}/documents/{document_id}`
* `POST /workspaces/{workspace_id}/sessions`
* Existing `/chat` accepts optional `workspace_id` and `source_scope`; association
  mismatches are rejected without changing the conversation's document scope.
* Chat response adds `citations`, `evidence`, `plan`, `trace_summary` alongside
  `answer`, `context`, `execution`.

Streamlit retains its chat/history/single-upload flow. A Workspace mode adds
create/select/delete, document list/page counts, multi-upload, removal and new
Workspace conversations. Source/evidence and task-plan expanders show public
metadata only. Model prompts/private reasoning/raw graph state are not shown.

## 10. Configuration and observability

All new operational knobs are in `server/config.py` and `.env.example`:
WORKSPACE_MAX_DOCUMENTS; WORKSPACE_RETRIEVAL_PER_DOC_K/GLOBAL_K/CONCURRENCY/
TIMEOUT_SECONDS; PLANNER_ENABLED/MAX_STEPS/MAX_ITERATIONS/MAX_REPLAN;
SELF_CORRECT_RAG_ENABLED; RETRIEVAL_REWRITE_MAX; GROUNDING_CHECK_ENABLED;
ANSWER_REVISION_MAX. The default overall graph budget is increased from 20 to 60.
Explicit existing environment values are respected. Rewrites/revisions also have
hard limits of two/one. Existing model request/total execution timeouts remain.

Trace metadata includes workspace/document scope. Final structured log/API summary
includes complexity, planner steps, tools/calls, retrieval attempts, rewrite count,
evidence/citation counts, verification status and execution latency. LangSmith
input/output bodies are hidden by default to retain timings without transmitting
document bodies. Credentials are not added to traces or summaries.

## 11. Tests and evaluation

```powershell
.\.venv\Scripts\python.exe -m unittest discover -v
.\.venv\Scripts\python.exe -m unittest test_phase2 -v
.\.venv\Scripts\python.exe -m evaluation.benchmark_agent_workspace --demo
```

`test_phase2.py` includes registry CRUD/capacity/restart, safe migration,
cross-workspace ingestion index reuse, multi-document retrieval/global rerank,
failed document/reranker isolation, planner parsing/bypass/step budgets, grader,
rewrite limits, source policy, external fallback, verifier/revision limits,
forged/stale citations and multi-upload/API compatibility. Existing local
single-PDF/session/context/execution/OCR/retrieval tests remain in discovery.

Local Windows validation on 2026-10-01: **135 tests passed, 0 failed**
(94 existing regression tests and 41 Phase 2 tests). The Agent demo runner also
completed with the checked-in synthetic example cases. Provider smoke-test
entry points were not invoked; they remain explicit/manual commands.

Offline acceptance includes three-document comparison with real fixture-bound
citations, simple title routing, document-only external prohibition, insufficient
retrieval rewrites, invalid citation removal and persisted Workspace restoration.
Fixtures mock all providers/models; no private keys or real network calls are used.

`evaluation/benchmark_agent_workspace.py` scores recorded API responses against
example cases. It reports tool selection, retrieval, planner, citation validity,
verifier pass rate, average calls/attempts and latency. `--demo` executes the graph
with deterministic synthetic fixtures, explicitly labelled as a wiring check.
No real benchmark quality claim is made. Existing retrieval evaluations are preserved.

## 12. Migration compatibility

No database reset, index rebuild, destructive Git operation, commit or push is
required. Single-PDF `/upload_pdf` -> session -> `/chat` remains available.
Legacy fingerprint resolution and Chroma/OCR namespaces remain unchanged.
Source-aware response fields are additive. Optional history fields are omitted
when absent to preserve the previous history response shape.

## 13. Known limitations

* Router heuristics and LLM sufficiency/grounding grades need real benchmark and
  human validation. Synthetic acceptance checks do not establish research accuracy.
* Final global Top K can omit a document from broad comparisons; per-step queries
  help but there is no automatic all-document coverage guarantee.
* Timed-out native embedding/Chroma inference cannot be force-killed in a Python
  thread. Bounded admission prevents unbounded queued retrieval work.
* BM25/runtime caches are process-local; each active Workspace session owns its
  cache and bounded executor. Very large numbers of active sessions may need
  future eviction/sharing. Removed documents' cached indexes remain until restart.
* The registry retains shared files after deletion; disk garbage collection is
  intentionally outside this phase. Failed uploads remain in ingestion status,
  not in the ready-document registry.
* General OCR does not reconstruct table identities/structure. Named table
  citations are suppressed when explicit metadata is absent.
* Serper/arXiv availability and actual model behavior were not network-tested;
  all automated tests use mocks. Multi-PDF indexing is synchronous and sequential.
