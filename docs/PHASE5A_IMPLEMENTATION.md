# Phase 5A — bounded supervisor-worker orchestration

Phase 5A adds isolated specialist subgraphs to the existing root graph. It does not
add distributed workers, agent-to-agent messaging, arbitrary prompt registration,
browser automation, RBAC, remote agents, or new SaaS providers.

## Architecture

```text
Memory Retrieval → Analyzer → Orchestration Router
                              ├─ Existing workflow → existing Planner/Executor
                              └─ Single/Multi specialization
                                   Supervisor Plan + frozen Agent Registry
                                   ↓ execution groups / bounded Send fan-out
                                   Research    Data    Coding
                                      independent private subgraphs
                                   ↓ result envelopes / durable ledger
                                   Context Merger → optional GitHub action gate
                                   ↓                Phase 4 interrupt/HITL
                                   Draft → Reviewer
                                   ↓ up to one revision round / two gap delegations
                                   Final Synthesis → Grounding → Citation Validation
                                   → Root Memory Extraction → END
```

The root preserves source_scope, retrieval policies, graph budgets, native analysis,
and grounding. The router leaves simple QA/single PDF on the existing path. Data-only,
paper comparisons and code-only can use one specialist. Independent research+data or
research+code uses multiple specialists. Available datasets alone do not activate Data:
"experimental results in papers" is research, not an implicit request to analyze XLSX.
English code keywords use word boundaries: encoder/decoder text does not imply code
inspection. Chinese paper/model architecture descriptions alone do not imply repository
work. A paused legacy task retains its original plan after orchestration is enabled.

## Agent Registry and responsibilities

`server/agent_registry.py` owns fixed identities and frozen task snapshots. Registration,
get/list/capability lookup, enable/disable and validation are developer/operator APIs,
not exposed mutation endpoints. Only Supervisor has can_delegate=true.

| Agent | Declared capabilities | Visible tool capabilities |
|---|---|---|
| Supervisor | orchestrate.route/delegate/synthesize | no bulk retrieval; separate explicit action gate |
| Research | research.document/web/academic/github/compare | workspace.search, document.search, web.search, arxiv.search; known GitHub read capabilities |
| Data | data.inspect/analyze/visualize/compare | dataset.inspect, data.analyze |
| Coding | code.inspect/explain/review/propose_patch | github.repo.read/file.read/code.search |
| Reviewer | review.evidence/completeness/consistency/requirements | existing evidence/artifact references only; no external tool calls |

Capability declarations describe the specialist role, not provider availability.
Tool visibility is the intersection of frozen tool snapshot, descriptor allowlist,
delegation allowlist and source scope. Unknown/missing/disabled capabilities are
explicit failures. Coding patch proposals are text and are never applied.

## Delegation protocol and context isolation

`orchestration_schemas.py` defines extra-forbidden Pydantic contracts for descriptors,
snapshots, requests, context, typed worker results, ReviewerResult and SharedTaskContext.
Requests carry identity, objective, required capability, tool allowlist, limits,
execution group and optional revision_of. Registry validation runs both before
persistence and at the worker boundary. Workers cannot register agents or delegate.

The root creates a new worker brief with goal, selected memory IDs/snippets, selected
evidence IDs/snippets, authorized dataset metadata and constraints. Root conversation
and sibling private messages are never passed. Each Research/Data/Coding graph has a
private WorkerState and private_messages; Reviewer has its own ReviewState. Only typed
result envelopes, evidence IDs and artifact IDs enter shared context. Private prompts
or hidden state are absent from public agent/delegation APIs.
Evidence views retain authoritative document names/pages, execution and artifact IDs,
and source metadata. Keeping only content/ID incorrectly makes a Reviewer believe that
retrieved document provenance is absent; worker, Reviewer and synthesis projections
now preserve these fields.

Final synthesis projects SharedTaskContext into bounded summaries, selected findings,
small metric mappings and code proposals before JSON serialization. It avoids passing
the full nested payload and then truncating serialized JSON. Synthesis, answer revision
and grounding use the same eight evidence excerpts (up to 2400 characters each), so a
revision cannot introduce a claim from a fragment hidden from the next verifier.

## Specialized subgraphs and shared infrastructure

Each worker executes prepare → bounded plan → tool steps → typed result contract.
Research results contain findings with evidence IDs. Data adds computed metrics,
analysis execution IDs and artifacts. Coding adds examined files/findings/proposals.
Invalid/fabricated result references become a partial result retaining real evidence.

There is one shared Tool Registry, policy, task store, memory/data/artifact infrastructure.
ContextVar native bindings allow Data to invoke the **existing** Phase 3 _analyze and
AnalysisRuntime without overwriting another worker's session binding. No second Python
runtime or separate per-specialist registry is created.

Data plan normalization removes planner-supplied Python from tool arguments: the
existing constrained GeneratedCode boundary owns generation and validation. The Phase 3
row-count/column/mean shortcut now applies only to simple structural questions, so a
compound goal mentioning row count still performs filtering and requested chart work.

Context Merger validates references and detects conflicting results/content. Repeated
retrieval of a stable Evidence ID may update ranking scores; it cannot change source
identity, page, content or authoritative metadata. Evidence is deduplicated before
result synthesis. Completed revision results identify the original gap via revision_of;
their predecessor's resolved open questions are omitted from active shared questions.

## Parallelism and limits

LangGraph Send executes independent delegations in a group concurrently. Dependency
groups execute sequentially; dependencies must be in an earlier group. Runnable
Supervisor creates one initial delegation per required specialist; cross-specialty
comparison belongs to its final synthesis. Additional specialist work is gap-specific
re-delegation. Multiple capability aliases for one role are normalized before planning.
max_concurrency and a shared BoundedSemaphore enforce MULTI_AGENT_MAX_PARALLEL across
worker invocations. There is no unbounded gather or worker-created recursion.

Defaults: eight total delegations (including Reviewer/revisions), parallelism three,
depth one, one revision round, two supplemental delegations, 8000 context tokens per
model context, ten Supervisor LLM calls. Research/Data/Coding iteration/tool ceilings
are 6/8, 4/4 and 5/6; Reviewer has two iterations and zero external calls. Initial worker
plans prefer one broad workspace retrieval, at most two research/data steps or three
code reads. Exact duplicate tool requests are removed. Counts persist **before** calls,
so failures/recovery also consume budgets. Root graph and provider/runtime limits apply.

Timeouts are cooperative at worker boundaries, combined with existing model/transport/
analysis timeouts. They cannot forcibly kill an arbitrary in-flight Python thread.
The outer ExecutionHarness also retains its execution timeout. No unlimited retries.
Real-provider smoke sets a 600-second total task ceiling in its isolated subprocess,
while retaining the 90-second cooperative worker budget and model-call limits. A
240-second smoke attempt timed out with the configured provider. Production operators
must size AGENT_EXECUTION_TIMEOUT_SECONDS to their model latency and task scope.
Disabling MULTI_AGENT_ENABLED stops new orchestration while existing frozen tasks can
still resume their checkpointed specialist graph.

## Reviewer and bounded re-delegation

Supervisor first creates a compact draft. Reviewer sees goal, constraints, delegation
summaries, bounded evidence snippets, artifact IDs and draft, never private chats. It
checks coverage, evidence support, contradiction and requirements. Deterministic checks
prevent an LLM PASS from waiving missing evidence/failed work. Only Supervisor accepts
suggestions that correspond to reported gaps and available specialist capabilities.

At most one revision round and two gap-specific requests are created. Completed
siblings are not rerun. A deterministic post-revision check is retained; no second
unbounded Reviewer/research loop occurs. Reviewer failure defaults to a recorded warning
and deterministic checks; MULTI_AGENT_REVIEW_REQUIRED=true makes it a graceful failure.
Reviewer is independent of the existing final grounding and citation validators.

Grounding receives the user goal as selection criteria and a compact operational
context for artifact availability/delegation status. Neither supplies measured facts:
computed values and source claims still require tool evidence. The final TaskStep stores
verification_result and revision_count alongside citations, evidence and artifacts.

## Persistence, restart and cancellation

`agent_runs` and `agent_delegations` are additive SQLite tables on sessions.sqlite3,
indexed by task/status with a unique delegation→run binding. Existing TaskEvent is
reused for orchestration, delegation, review and revision events. No old tables/data
are reset. A Supervisor AgentRun retains frozen registry, plan, groups and merged context.
Worker progress persists cursor, bounded authoritative evidence/artifacts and tool receipts,
not raw model reasoning. Counters, summaries, timing and errors are durable.

Tasks retain orchestration and finalization TaskSteps. Official LangGraph checkpoints
also retain private subgraph state/namespaces. Startup recovers RUNNING tasks to PAUSED
and running AgentRuns to INTERRUPTED. Resume continues checkpointed work; completed
delegation records return cached envelopes, including a completed Reviewer whose result
was saved before the Supervisor checkpoint. Interrupted read calls can safely retry
within budgets. An interrupted execution with uncertain side effects is refused and
needs reconciliation; restart does not promise remote exactly-once.

Cancellation changes the existing Task and cancels unfinished delegations. Worker
boundaries recheck task status; success from completed siblings remains in the ledger.
The unfinished Supervisor run is cancelled too; root extraction checks cancellation
before claiming memory and cannot label an observed cancelled task as completed.
Root-only memory extraction uses a durable claim to avoid repeated extraction attempts.
A crash inside that extraction can leave an incomplete attempt, intentionally not
automatically repeated; MemoryStore still applies its existing deduplication rules.

## Tool policy and HITL

Read-only specialists have no GitHub writes, deletion, unrestricted filesystem or shell.
An explicitly requested GitHub issue/comment can be proposed in SupervisorPlan.actions,
only on a Persistent Task and through the same Phase 4 Policy/Approval/ToolExecution.
The root action node enters WAITING_USER and interrupt, then resumes using Command with
durable approved/edited/rejected decisions. Cached completed specialists are preserved.
Read-only/tool/repository allowlists remain authoritative. Multiple parallel read approvals
wait until all pending decisions are recorded and resume their interrupt-ID mapping.

## API and UI

- GET /agents; GET /agents/{agent_id}
- GET /tasks/{task_id}/agents; GET /tasks/{task_id}/delegations
- Existing Task, ToolExecution and Approval APIs remain available.

Streamlit's Persistent Task panel shows Supervisor/specialist status and expandable
objective, timing, counters, evidence/artifact counts and compact public output. Public
APIs have no arbitrary agent creation, prompt editing or private-state endpoint.

## Observability and tokens

Per-agent counters include LLM calls, tool calls, iterations, elapsed time and tokens.
Known provider usage accumulates; missing usage is explicitly unavailable or partial,
never zero. Total token usage is unavailable if any participating model call lacks usage;
a known subtotal is separately labeled. LangGraph/LangSmith run names, agent/delegation/
group metadata and subgraph namespaces identify the tree. ToolExecution metadata links
agent_id, delegation_id and supervisor_run_id. Root memory extraction remains once.

## Evaluation and validation

`evaluation/multi_agent/cases.json` defines research, data and mixed fixtures.
`python -m evaluation.multi_agent.benchmark` runs the existing and orchestrated workflows
against scripted-model fixtures with actual SQLite/data/chart execution. This isolates
contracts/selection/cost; it does **not** validate provider quality.

`python -m evaluation.multi_agent.smoke` starts real FastAPI processes, uses the configured
real LLM, actual PDF/XLSX indexing, official-SDK local MCP, approval and process restart.
It also runs the existing workflow with the same synthetic sources in a separate backend.
The final real comparison creates fresh workspaces for both modes after the runtime
cases, rather than treating an earlier mixed task as the final implementation sample.
Fault injection lives solely in evaluation/multi_agent/smoke_app.py for gap/deadline/branch
failure cases; it is never a production API/config feature. Usage callbacks log counts,
timing and token usage only, no prompts or model reasoning. Real GitHub is separately
SKIPPED when credentials are absent; mock actions never create a real issue.

An interrupted evaluation can set PHASE5A_SMOKE_RESUME_FOLDER to its existing isolated
fixture folder. The report must match that folder. Passed A/B/C tasks retain their
original IDs and timings; failed acceptance checks are rerun against the updated code.
The runner records evaluation resumptions explicitly and preserves earlier task data.

Measured metrics include routing/selection, completion/success, reviewer gap detection,
revision success, repeated completed work, interval overlap/parallel utilization, per-agent
calls, latency, usage, grounding and citations. Parallel interval duration sum is a measured
comparison proxy, not a fabricated sequential benchmark. Small n=1 samples cannot establish
that multiple agents are universally better; extra planning/review commonly increases cost.

## Known limitations

Single local user/backend; no distributed scheduler, multi-node exactly-once, RBAC or OAuth.
No real GitHub validation without credentials. Coding inspection needs discovered GitHub
read tools; no arbitrary local repository/shell access. Router uses conservative intent
guards with structured model routing, not a learned general classifier. Semantic duplicate
searches are not completely eliminated. Reviews/grounding remain model-dependent and can
correctly return insufficient evidence. Context snippets are bounded; large workflows may
need explicit decomposition. Timeouts are cooperative. Remote uncertain outcomes and memory
extraction crashes need operator review. Benchmark fixtures are synthetic, not research claims.

## Primary implementation references

- [LangGraph subgraphs](https://docs.langchain.com/oss/python/langgraph/use-subgraphs)
- [LangGraph interrupts](https://docs.langchain.com/oss/python/langgraph/interrupts)
- [LangGraph graph API](https://docs.langchain.com/oss/python/langgraph/graph-api)
