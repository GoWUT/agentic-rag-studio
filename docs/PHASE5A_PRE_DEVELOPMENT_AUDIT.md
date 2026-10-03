# Phase 5A pre-development audit — 2026-10-02

Git HEAD: `24f01d5f2901191fbdcde8a2117250b0b728c93c`. Existing Phase 1–4/user
changes were already uncommitted. `git status`, `git diff --stat`, and
`git diff --check` were inspected before implementation; no reset/clean/removal.

1. Root graph: build_agent selects ToolWorkflowNodes on top of PersistentWorkflowNodes
   and ResearchWorkflowNodes. Memory retrieval → query analyzer → sequential
   plan/executor → synthesis → verifier/revision → citations → memory extraction.
2. Planner/Executor: TaskPlan is a structured plan; Executor alone invokes tools.
   Existing source_scope and research correction paths remain authoritative.
3. Task persistence: tasks/task_steps/task_events are SQLite records. Steps retain
   output_json; TaskService reconstructs completed outputs and skips completed work.
4. Tool capability: discovered descriptor/schema/provider snapshots, capability
   lookup, schema validation, production guard, local policy, then official SDK/native.
5. Memory: retrieve_memory runs before analysis; extract_memory after finalization.
   MCP/action runs cannot turn untrusted output into enduring instructions.
6. Evidence: authoritative tool metadata produces stable IDs. Root collects Evidence,
   verifies claims and deterministically validates/renders citation markers.
7. HITL: ApprovalRequest/ToolExecution persist before interrupt; task/step WAITING_USER;
   external decisions resume the same task thread using Command. No blocking input().
8. Checkpoints: official SqliteSaver with short-lived SQLite connections and a
   redacting serializer. The same database also holds task/tool/approval records.
9. Budgets: graph recursion, planner steps/iterations/replan, retrieval rewriting,
   request/execution timeouts, context budgets, data-code/output/artifact limits.
   Per-worker/delegation/concurrency/review limits are missing and are Phase 5A work.
10. Tracing: existing LangSmith, root trace summary, TaskEvent, tool/action timings.
    Agent identity, delegation and per-agent counters require additive integration.

Boundary chosen: one operator-owned Agent Registry, Supervisor-only scheduling,
private LangGraph workers, structured ID-based shared context, additive run/delegation
ledger. Reuse Phase 3 analysis and Phase 4 policy/approval; no distributed infrastructure.
