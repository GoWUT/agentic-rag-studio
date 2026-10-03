# Phase 5A.5 — Cost-aware Multi-Agent Orchestration

This extends Phase 5A in place. The five identities, frozen capability catalog,
SQLite task/checkpoint stores, MCP discovery, tool policy, approval interrupts,
workspace/session APIs and root memory ownership remain the existing services.
No dependencies, external integrations or new agents were added in this phase.
Earlier Phase 1–5A changes were already present in the uncommitted worktree.

## Audit and retained baseline

Read [the pre-optimization audit](PHASE5A5_PRE_OPTIMIZATION_AUDIT.md) for all 19
observed starts, namespaces and matched usage records. The retained single/multi
answers and measurement records are in
`evaluation/results/phase5a5/baseline_reference.json`. The audit separates the
19-call comparison task from the earlier explicit gap-injection scenario with
two unsuccessful supplementary delegations. They are different tasks.

## Routing and deterministic work

`server/agent/efficient_orchestration.py` extends the existing nodes; disabling
`MULTI_AGENT_EFFICIENCY_ENABLED` restores the Phase 5A implementation. Disabling
`MULTI_AGENT_ENABLED` retains the original single workflow. Clear capabilities
use deterministic analysis/routing; ambiguous intent uses one structured router
call. Two necessary distinct specialties can run in parallel. An explicit
extract-then-compute dependency uses the existing sequential workflow. Query
length never adds a specialty. The expanded decision records scope, dependency,
estimated agents, value, cost level and a brief public reason.

A clear uploaded-paper/dataset request constructs delegation skeletons in code.
Complex decomposition and external actions retain the bounded supervisor planner.
The worker plans obvious local retrieval or inspect/analyze steps deterministically.
`server/deterministic_analysis.py` validates supported operations against actual
column names and units. Ranking under an explicitly named parameter budget,
simple min/max/mean/count/sort operations use the existing restricted pandas
runtime. Unsupported or compound operations retain constrained code generation.
Typed Data results come only from successful deterministic execution, with actual
execution, evidence and artifact references. Arbitrary generated JSON output is
not treated as a successful deterministic result.

## Adaptive review

Policies: `always`, `multi_agent_only`, `risk_based` (default), `disabled`.
Pre-review checks completion, evidence/artifact identities, findings, contradictory
claims and open questions. Multiple workers, low confidence, a requirement gap or
an external action trigger review. A valid simple single-specialty result can skip
Reviewer; final semantic grounding and citation validation still run afterward.
Pre-review identity validation does not assert semantic groundedness.

Reviewer receives bounded typed summaries, important findings, identifiers,
artifact metadata and open questions. It receives no raw PDF chunks, dataset
outputs or worker conversations. Ordinary typed merging and a provisional draft
need no additional model call. A review failure cannot silently satisfy a
configured mandatory Reviewer. Required reviews override optional-review skips.

## Targeted repair and scope ownership

`SupplementaryDelegationRequest` is embedded in the ordinary durable request,
which continues to hold identity, capability/tool allowlists and per-worker limits.
It specifies exact gaps, target sources, existing evidence, work not to repeat and
expected fields. Existing valid evidence can satisfy a gap without dispatch.
Local retrieval accepts a validated document-ID subset **before** scheduling index
reads. A named paper-C latency request never searches papers A/B.

Authoritative source-failure records take priority over a Reviewer suggestion that
mixes other specialties into a generic gap. Worker model context projects the
delegated goal; the original user goal remains in the durable request. Research
does not classify dataset/chart responsibilities as its own incomplete work.
Research context contains research evidence; Data contains analysis evidence.
Missing facts that sources do not report remain explicit source limitations.
A failed Data contract with existing calculated evidence can repair the contract
without another inspection or calculation. At most one round/two supplementary
delegations is retained; unknown capabilities are rejected without crashing.

## Cache and deduplication

Task-local cache keys include task/supervisor identity, worker, normalized objective,
selected context, document/dataset fingerprints, index version, contract/schema
version, frozen tools, permissions and request limits. New equivalent requests
reuse only COMPLETED results; identity references are remapped to the new request.
No worker starts or tool/model calls occur on a hit. PARTIAL/FAILED, changed
sources/context/output contract and explicit force refresh bypass reuse.
Checkpoint replay of an already completed delegation remains the existing frozen
task behavior; it is separate from lookup for a newly created request.

Mutable external sources are conservatively ineligible for semantic reuse. A
requested SHA is recorded, but it is not proof that the MCP transport actually read
that revision. Cross-task/global semantic caching is not implemented.

The overlap detector merges only sufficiently similar objectives with identical
capability/dependency boundaries. Different roles/permissions remain separate.
Evidence-aware repair suppression requires a supported known fact with live IDs;
an evidence ID alone does not demonstrate that a requested fact is covered.

## Budget and telemetry

Default aggregate limits: 8 delegations, 3 concurrent workers, 24 model calls,
12 tool calls, 300 seconds, one review round and two supplementary requests.
The 24-call ceiling retains repair/finalization headroom over the observed 19-call
baseline; 300 seconds exceeds its measured 212.906 seconds. These are configurable
ceilings, not efficiency targets. Token ceiling defaults to absent because usage
availability depends on the provider. Soft utilization defaults to 80%.

Reservations use the existing SQLite `BEGIN IMMEDIATE` transaction. Denials are
committed before raising a handled `BudgetExhausted`; parallel workers share one
ledger. Four calls are reserved from new optional model work for finalization.
Soft limits skip an otherwise optional review and low-value repair. Hard limits
stop new work, preserve completed branches, expose unresolved requirements and
produce a bounded partial answer. Unverified output is never marked grounded.
External actions use the same counted policy/approval path; budget failures do
not bypass authorization. Root memory is claimed once; workers never extract it.

Aggregate and per-agent model/tool attempts, provider prompt/completion/total
usage, retrieval/analysis calls, delegation/review/repair/cache counts and group
duration/critical-path worker live in existing run metadata. Unknown provider
usage is `null`/`unavailable`; a known subtotal is separate. Actual token caps can
overshoot by in-flight responses (including concurrent requests); wall checks are cooperative between safe
steps, with existing worker/provider timeouts. Completed task duration is fixed
at completion rather than growing whenever a trace is opened.
Explicit durable pauses and approval interrupts suspend the execution-time
budget; resuming retains accumulated usage and excludes the human waiting period.
Reported wall latency still includes elapsed waiting time.

## Context and UI

`AgentContextBuilder` ranks evidence by goal and target source, keeps at most four
700-character snippets and two selected memories, and supplies dataset metadata.
Canonical evidence and execution output remain durable and unmodified. Final
synthesis gets typed result projections and a bounded authoritative evidence map;
the root semantic verifier, optional single revision and deterministic citation
validator remain in place.

Context measurements compare actual projections with `count_tokens_approximately`.
They are labeled estimates and kept separate from actual provider usage. Reviewer
projection includes measured before/after counts. Initial delegations often have
no preexisting evidence, so no reduction is claimed for those inputs.
Conversational follow-ups with unresolved pronouns retain the original query
rewriting model; deterministic shortcuts are for self-contained goals.

The semantic verifier can return exact unsupported sentences. A bounded extractive
revision removes only literal matching unsupported caveats without numeric claims;
unknown spans or numerical corrections retain model revision. The original second
semantic verification still runs. A single unsupported absence claim therefore
need not discard an otherwise supported calculation. No model reasoning is exposed.

When a hard budget prevents full semantic verification, citation finalization
preserves a bounded partial execution report and authoritative deterministic Data
calculations. It keeps `grounded=false`, lists remaining requirements and never
publishes the unverified model draft. The final isolated budget-only probe is saved
separately in `budget_runtime.json`, leaving the two complete benchmark runs intact.

`client/phase5_ui.py` adds one efficiency expander: mode/short reason, specialist
count, model/tool/tokens, cache, review/skip reason and budget utilization. Public
metrics omit private progress/messages. The existing task API refreshes aggregate
metrics for the panel.

## Reproduction

```powershell
.\.venv\Scripts\python.exe -m evaluation.multi_agent_efficiency.regression
.\.venv\Scripts\python.exe -m evaluation.multi_agent_efficiency.runner
.\.venv\Scripts\python.exe -m evaluation.multi_agent_efficiency.comparison
```

The runner uses real configured DeepSeek calls and identical Phase 5A synthetic
PDF/XLSX generators. Its local fault controls/usage observer are evaluation-only;
no production fault-control endpoint or raw prompt logging is introduced. It runs
two fresh optimized tasks, an enabled-Reviewer paper-gap repair, simple Data,
hard-budget containment and a fresh existing single workflow. Earlier iterations
remain in `evaluation/results/phase5a5/iterations/` and their SQLite workspaces.
Quality criteria check the explicit best choice, parameter exclusion, descriptive
limitations, all required source types/artifacts, valid references and grounding;
citation counts are descriptive metrics, not answer-quality scores.

Approval persistence continues to use the existing checkpoint and `Command`
integration, consistent with [LangGraph interrupt documentation](https://docs.langchain.com/oss/python/langgraph/interrupts).
