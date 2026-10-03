# Phase 4 validation — 2026-10-02

## A. Final verdict

**READY WITH LIMITATIONS**. Dynamic MCP routing and durable approvals are implemented.
Real GitHub authentication/reads/writes were not measured because credentials and
write-smoke authorization settings are absent. Local official-SDK fixtures are
identified separately throughout this report.

## B. Architecture

```text
Discovery → Normalization → Registry snapshot → Capability routing
                                                  ↓
                                             Local policy
                                      read / sensitive action
                                       |            |
                                       |     Approval persisted
                                       |     Task/Step WAITING_USER
                                       |     interrupt + SQLite checkpoint
                                       |     approve / edit / reject
                                       |     Command on same task thread
                                       +--------- execution claim
                                                  ↓
                                      Native / official MCP SDK
                                                  ↓
                                      Evidence / ActionResult
                                                  ↓
                         step checkpoint → synthesis → grounding/citations
```

## C. Phase 4 files

Added: `server/tool_registry.py` (provider/descriptors/snapshots/schema),
`server/tool_policy.py` (local policy/redaction), `server/tool_actions.py`
(approval/execution persistence), `server/mcp_client.py` (official SDK transports),
`server/phase4.py` (composition/saver/claim/interrupt), `server/phase4_api.py`
(discovery/decision routes), `server/agent/tool_workflow.py` (capability integration),
`client/phase4_ui.py`, `test_phase4.py`, and `evaluation/phase4_mock_server.py`,
`phase4_smoke.py`, `phase4_github_smoke.py`, `benchmark_phase4.py` plus this report
and PHASE4_IMPLEMENTATION.md.

Modified: graph/state/schemas/evidence/nodes/research_workflow/persistent_workflow,
sessions/tasks/config/main/phase3_api, client/app and phase3_ui, pyproject/uv.lock,
README/.env.example/.gitignore. Earlier Phase 2/3 and user edits are retained;
the Git diff contains those earlier changes as well.

## D. Registry

Discovery uses official tools/list, normalization retains schemas, internal names
are namespaced, and Planner outputs preferred_capability. Executor resolves the
frozen descriptor and validates arguments. A production registry guard prevents
direct sensitive provider calls without a matching durable approved execution claim.
Missing/disabled tools are explicit ToolUnavailable results. Native tools remain
workspace/session-bound and existing research/data behavior is covered.

## E. MCP

MCP SDK 2.2.0, LangGraph 1.0.5, SQLite saver 3.0.3; dependency checks passed.
Official SDK stdio and Streamable HTTP were both exercised against actual local
MCP server processes. Discovery/schema/output normalization, timeout/crash handling,
disconnect/reconnect and unavailable-provider isolation are tested. Connections
are per operation, not pooled; status reports last reachability.

## F. Human approval

Requests persist before interruption. Approve/edit/reject are external decisions,
with title/body edit limits and schema/policy revalidation. The same task's Graph
checkpoint resumes with Command; no substitute approval-only graph was built.
Pending tasks cannot bypass approval through generic resume. Rejection performs
zero provider writes. Waiting-step attempts remain at one across interruption.

## G. Risk policy

Known low-risk READ auto-executes by default. WRITE/DELETE/high-risk/unknown MCP
EXECUTE requires approval, with fail-closed behavior when approvals are disabled.
Native data analysis remains a medium-risk constrained execution authorized by
the analysis request. Manual approval for native reads is tested when read
auto-approval is disabled. Server annotations do not lower risk.

## H. GitHub

Official-server SDK integration is configured for necessary read capabilities and
only issue creation/comments as writes. Read-only filtering precedes approvals;
minimal toolsets and local tool/repository allowlists restrict routing. The actual
set of tools comes from discovery. No real GitHub connectivity claim is made.

## I. Security

Environment-only credentials, secret-free operator config, redacted outputs and
checkpoints, no raw arguments in events/traces, and secret-bearing goal/argument
rejection before persistence. Repository content/schema/description is untrusted,
cannot waive policy and never becomes MCP-run memory. Unexpected browser Origins
are rejected on approval routes. There is no public arbitrary-call or server-registration
route. The local single-user/operator trust boundary remains a limitation.

`audit_summary.json` records four fixture execution rows, APPROVED/EDITED/REJECTED
decisions and the corresponding task/tool events. Known configured secrets and the
fake smoke token were absent from both fixture logs and the fixture database.
External JSON Schema references are rejected before tool registration.

## J. Database

Additive tables: mcp_servers, approval_requests, tool_executions, plus official-saver
checkpoints/writes. Indexed task/status lookups, unique execution intent and unique
approval execution binding. No existing database, indexes, workspace or session was
deleted or reset. Short-lived SQLite saver connections avoid Windows file-lock leaks.

## K. APIs

GET /tools, /tools/providers; POST /tools/providers/{id}/refresh.
GET /mcp/servers, /mcp/servers/{id}/tools; POST /mcp/servers/{id}/refresh.
GET /approvals (task_id/status), /approvals/{id}; POST /approve, /reject, /edit below
/approvals/{id}. GET /tool-executions (task_id). Existing task APIs continue to apply.

## L. Tests and static checks

| Suite | Passed | Failed | Skipped |
|---|---:|---:|---:|
| Previous baseline | 236 | 0 | 0 |
| New Phase 4 | 59 | 0 | 0 |
| Total | **295** | **0** | **0** |

`evaluation/results/phase4/tests.json` records the final run. Tests include actual
SDK in-memory protocol and stdio subprocesses, actual LangGraph interrupt/checkpoints,
concurrent execution claims, restart persistence, native data/RAG integration, direct
registry bypass attempts, cross-origin decisions and secret rejection. Unit tests
use a scripted model; that is distinct from runtime smoke's actual configured LLM.
Compileall, pip check, uv lock --check and git diff --check passed. Existing CRLF
notices are not diff-check errors. Initial failures were repaired before this result.

## M. Runtime smoke

Source: `evaluation/results/phase4/runtime_smoke.json`. Actual processes, HTTP APIs,
configured real LLM, official SDK, synthetic data only.

| Required check | Result |
|---|---|
| 1 FastAPI starts | PASS |
| 2 Streamlit starts | PASS |
| 3 Native registry | PASS |
| 4 Mock MCP connects | PASS |
| 5 Dynamic discovery | PASS |
| 6 Read without approval | PASS |
| 7 Write creates request | PASS |
| 8 Task WAITING_USER | PASS |
| 9 Reject prevents execution | PASS |
| 10 Approve resumes | PASS |
| 11 Edit changes/revalidates args | PASS |
| 12 Restart pending approval | PASS |
| 13 Resume after restart | PASS |
| 14 Write exactly once in measured flow | PASS |
| 15 Injection cannot bypass policy | PASS |
| 16 Fake secret absent from logs | PASS |
| 17 Real GitHub connects | SKIPPED: no token |
| 18 Real GitHub discovery | SKIPPED: no token |
| 19 Real repository read | SKIPPED: no token |
| 20 Real issue/PR read | SKIPPED: no token |
| Additional real Streamable HTTP transport | PASS |

The browser also shows the six native capabilities, data.analyze at medium risk,
Tools/MCP and Pending Approvals with its empty state. Existing history/workspace
rendering remains available. The mock smoke keeps its temporary managed SQLite/
ledger/log fixtures separately from the user's existing workspace database.

## N. Real GitHub read/write

`github_smoke.json` explicitly records four SKIPPED read checks. No PAT is configured.
Real write smoke is SKIPPED: PHASE4_GITHUB_WRITE_SMOKE is not true and no test repo is
configured. No issue/comment was created on real GitHub. A passed local fixture is
not presented as real GitHub verification.

## O. HITL restart and execution count

Actual backend stopped with a PENDING approval and WAITING_USER task; a new process
read the same SQLite approval and graph checkpoint. Approve resumed to COMPLETED.
The fixture issue ledger contained **one** action at that point. Duplicate approve
and generic resume returned **409**, with the ledger still at one. A separate edit
case added one different approved action with the edited body. The rejected case
added zero. Tests also verify waiting-step attempt count one, atomic concurrent
claims, and refusal to retry an uncertain RUNNING action after reconstruction.

## P. Limits

- Real GitHub credential/protocol/permission behavior remains unverified here.
- Remote crash outcomes need operator reconciliation; no distributed exactly-once
  guarantee or automatic reconciliation workflow is claimed.
- Sensitive chat actions require Persistent Tasks; no automatic chat promotion.
- Single local user/backend; no RBAC/OAuth or distributed scheduling.
- Conservative approval for generic MCP tools; no generic trusted-read profile UI.
- Per-operation connections, bounded text output, no complete multimodal tool support.
- Approval EXPIRED is modeled, but no TTL scheduler is present.
- Lockdown/redaction/instruction framing reduce risk without establishing a complete
  security or prompt-injection boundary. Tiny measured metrics are not production
  benchmarks. See metrics.json for exact denominators.

## Q. Repository and services

**Not committed. Not pushed.** HEAD remains
`24f01d5f2901191fbdcde8a2117250b0b728c93c`. Existing `.env` values were not overwritten.
The current local FastAPI and Streamlit endpoints are 127.0.0.1:8001 and :8501.
