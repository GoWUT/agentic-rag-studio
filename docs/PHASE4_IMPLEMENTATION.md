# Phase 4 — Policy-controlled external tools

## Development audit and integration decisions

The Phase 1–3 baseline was 236 tests. Existing uncommitted work was retained. No
database/index reset, `.env` replacement, Git commit or push was performed.

| Existing boundary | Audit finding | Phase 4 change |
|---|---|---|
| Graph | Explicit bounded sequential research graph | Extend its nodes; no second agent |
| Tool registration | Native LangChain tools constructed per session | Wrap those implementations with NativeToolProvider |
| Planner | `preferred_tool` and research sources | Add `preferred_capability` and schema-validated arguments |
| Executor | Tool map invokes native implementations | Runtime registry, immutable snapshot, mandatory policy guard |
| Tasks | SQLite steps, boundary pause/resume; WAITING_USER exists | Approval pauses the same task and step |
| Checkpoints | Step output persisted, Graph has no saver | Official LangGraph SQLite saver and stable task thread ID |
| API | Existing chat/workspace/task routes | Add discovery and explicit decision routes |
| UI | Existing Streamlit sidebar | Tools/MCP and Pending Approvals panels |
| Database | Shared SQLite sessions/workspaces/Phase 3 stores | Additive tables/indexes; short-lived saver connections |

## Architecture

```text
Memory → Analyzer → Planner (capability + arguments)
                         ↓
                  Frozen tool snapshot
                         ↓
                 Capability resolution
                         ↓
                   Tool Registry
                         ↓
                  Local Policy Guard
                    /           \
             low-risk read    approval required
                   |          persist request + execution intent
                   |          Task/Step WAITING_USER
                   |          LangGraph interrupt + SQLite checkpoint
                   |          approve / edit / reject
                   |          Command(resume=approval_id), same task thread
                   |          validate schema + re-evaluate local policy
                   |                 |
                   +----------- atomic execution claim
                                     ↓
                       Native provider / official MCP SDK
                                     ↓
                       Evidence (read) / ActionResult (write)
                                     ↓
                       step checkpoint → synthesis → grounding
                                     ↓
                       citation validation → memory extraction
```

Discovery is `tools/list` through MCP 2.2.0. Input schemas are validated and retained
in descriptors and `raw_input_schema`; SDK objects never enter Graph State. Names
are `native.<tool>` or `mcp.<operator-server-id>.<remote-name>`. A duplicate canonical
name rejects a refresh rather than overwriting another provider. Unknown capabilities
produce ToolUnavailable. Runtime definitions are copied at run start; task checkpoints
preserve them across pause/resume. Refresh affects subsequent runs, not a paused plan.
Provider availability and the current local policy remain live safety checks.

Native catalogs are visible before a chat. Implementations bind to that chat's
authorized workspace/session. Their registration does not grant access to every
workspace. Research tools keep their original names and coverage query configuration.
Inspect and constrained analysis also execute through the registry; Phase 3 retains
its AST, process, file, timeout and artifact checks.

## SDK and lifecycle

- `mcp==2.2.0`, Python 3.11; `langgraph==1.0.5` and
  `langgraph-checkpoint-sqlite==3.0.3` are locked in pyproject/uv.lock.
- stdio uses official `StdioServerParameters`, `stdio_client` and `Client`.
- Streamable HTTP uses official `streamable_http_client`, `Client` and httpx2.
- Each operation opens/initializes/closes an SDK client. Connect/list/reconnect,
  explicit disconnect and health are available; this release does not pool connections.
- Discovery pagination is bounded. Invalid tool names/schemas are excluded; external
  JSON Schema references are prohibited. Timeouts and crashed providers become
  ToolUnavailable without discarding native tools. Refresh reconnects.
- Outputs normalize to JSON-safe content/structured content, strip binary payloads,
  redact secrets and bound text/structured material. Large multimodal outputs are not
  fully supported in this release.

## Local risk policy and GitHub

| Operation | Default |
|---|---|
| Known GitHub/native read, low risk | Execute automatically |
| Write/delete, high/critical risk | Persistent human approval; fail closed if approval disabled |
| Unknown MCP execute | Conservative high risk; approval |
| Native `analyze_data` | Medium-risk constrained execution authorized by the user's analysis request |

`TOOL_AUTO_APPROVE_READ=false` can require approval for reads too. Sensitive calls
never become automatic writes when approval is disabled: setting
`TOOL_APPROVE_WRITES=false` or `TOOL_APPROVE_DELETES=false` disables those operations.
Sensitive chat calls require **Run as Task**; no remote write occurs from an ordinary chat alone.
`workspace_only` excludes MCP tools. Disabling MCP does not disable native RAG.

GitHub calls use its official MCP server, not hand-written GitHub REST tools. A local
permission profile maps discovered names to capabilities; it contains no GitHub
call implementations. Reads include repository search, files/code, issues/comments
and PR data only when actually discovered. The local write profile permits only
`create_issue`, `issue_write(method=create)` and `add_issue_comment`. Repository
allowlists are exact case-insensitive `owner/repo` matches. Issue update, assignees,
labels, parent repositories, merge, branch deletion, force push, releases, settings
and secret administration are excluded.

Defaults: GitHub disabled, read-only and lockdown enabled, toolsets
`context,repos,issues,pull_requests`; `all`/unlisted toolsets are rejected. Remote
headers and stdio environment flags apply read-only/toolsets/lockdown at the server;
the local profile and optional tool allowlist filter again before routing. Read-only
removes writes before an approval can be requested. Server annotations cannot waive
the local classification. Lockdown is a best-effort content filter, not authorization.

## Approval, restart and replay

ApprovalRequest records identity, task/step/session, tool/provider/capability,
operation/risk, arguments, reason, status, timestamps, decision, edited arguments,
consumed timestamp and execution ID. The approval UI shows the repository, proposed
title/body and risk. Edit accepts only title/body patches, validates against the
frozen schema, and rechecks the repository/current policy. Reject never calls the
provider and returns a rejected action to synthesis.

The interrupt is propagated explicitly through native/data/MCP exception handlers.
Task and step enter WAITING_USER. LangGraph persists the suspended executor under
`task:<task-id>`. The decision API commits the decision, then uses Command with the
approval ID; arguments are loaded from SQLite, not from the resume payload. If the
backend stops after the decision commit, an explicit task resume can continue.
Startup recovery leaves waiting tasks intact. Re-entering a waiting step keeps its
attempt count instead of creating a second execution attempt.

An intent binds task + step + canonical tool + requested arguments hash. SQLite
BEGIN IMMEDIATE and a unique intent key enforce one claim. Edited arguments have a
new hash while retaining the original requested hash in metadata. Approvals are
consumed in that claim. Duplicate decisions, pending approval resumes and completed
task resumes return 409. Replayed successful actions return the receipt without
calling the provider again. Reads/native constrained calculations can have fresh
safe execution attempts when auto-approved.

**No distributed exactly-once guarantee:** a process can die after a remote server
performs an action but before its receipt reaches SQLite. A RUNNING/FAILED sensitive
execution is never blindly retried. The operator must inspect the remote repository
and reconcile the uncertain result. There is no automatic reconciliation UI/API in
this release. Pause/cancel remain step-boundary operations and cannot undo a remote
call that has already started. One backend process per SQLite database is supported.

## Security boundaries

Credentials come from the process environment. SQLite stores only environment
variable names, never their values. Known environment/config secrets and recognized
token prefixes are removed from discovered metadata, results, snapshots and checkpoint
serialization. Secret-bearing action arguments fail before persistence; redaction is
not used to silently execute a different payload. MCP stderr is suppressed rather
than copied into application logs. Task events/traces contain IDs/status/timing,
not full action arguments or repository content. Existing LangSmith input/output
hiding remains enabled. Arbitrary unknown secret formats cannot be recognized perfectly.

Tool descriptions, schemas, repository files and comments are untrusted data. They
do not change policy, repository allowlists, source scope or credential handling.
External content never becomes long-term memory in MCP runs. Prompt instruction
framing reduces injection risk; local policy and durable approval enforce the action
boundary even if a model follows injected text. Reads still use the credential's
actual permissions; there is no multi-user RBAC or complete prompt-injection guarantee.

MCP registrations come only from operator configuration. No LLM-facing or public
server-registration/arbitrary-call endpoint exists. HTTPS or explicit loopback
endpoints are accepted; credential-bearing/query URLs are rejected. HTTP headers
reference environment names. Official SDK redirects are restricted to the configured
origin. This is not a generic SSRF firewall against malicious operator configuration.
Decision endpoints reject unexpected browser Origins; CLI/local operator access remains
trusted. Do not expose this single-user backend to untrusted networks.

## Database and audit

Additive `CREATE TABLE/INDEX IF NOT EXISTS` migrations:

| Table | Purpose/index |
|---|---|
| `mcp_servers` | Operator configs; primary server ID |
| `approval_requests` | Unique execution ID; task/status index |
| `tool_executions` | Unique intent key; task index |
| `checkpoints`, `writes` | Official LangGraph saver tables and compound primary keys |

Execution states: REQUESTED, WAITING_APPROVAL, RUNNING, SUCCEEDED, FAILED, REJECTED,
CANCELLED. Approval states additionally model EDITED and EXPIRED (no TTL scheduler
yet). TaskEvents include TOOL_DISCOVERED/REQUESTED, APPROVAL_REQUESTED/APPROVED/
EDITED/REJECTED, TOOL_STARTED/COMPLETED/FAILED. Audit retains hashes and small receipts,
not full GitHub read results. Graph evidence/checkpoints necessarily contain bounded
read content for durable synthesis. Trace summaries include execution IDs, providers,
capabilities, operation/risk, approval IDs, statuses, timing and MCP server IDs.

Read evidence supports `mcp`/`github` source types and repository/path/SHA/issue/PR/
URL metadata. Citation markers render only from returned read evidence. Write requests
and their bodies are not factual evidence; action receipts are reported separately.

## API and configuration

GET `/tools`, `/tools/providers`; POST `/tools/providers/{id}/refresh`.
GET `/mcp/servers`, `/mcp/servers/{id}/tools`; POST `/mcp/servers/{id}/refresh`.
GET `/approvals` (task_id/status filters), `/approvals/{id}`.
POST `/approvals/{id}/approve`, `/reject`, `/edit` (`{"edits":{"body":"..."}}`).
GET `/tool-executions` (task_id filter). Existing task run/resume/cancel routes remain.
There is no unrestricted `/tools/call` endpoint and no secret-setting API.

All defaults appear in `.env.example`; `.env` was preserved. To enable official
GitHub reads, set `GITHUB_MCP_ENABLED=true` and the PAT in the local environment.
Keep read-only and use only the necessary credential permissions. A write additionally
needs read-only false, a nonempty repository allowlist, a Persistent Task and its
explicit approval. Feature configuration itself is not authorization to write GitHub.

Other operator-owned servers use `MCP_SERVERS_FILE` containing a JSON list:

```json
[{"id":"example","name":"Operator-approved MCP","transport":"streamable_http",
  "url":"https://mcp.example.org/mcp","enabled":true,
  "header_env_keys":{"Authorization":"EXAMPLE_MCP_AUTHORIZATION"}}]
```

For stdio supply `command`, `args`, and `env_keys` (names only). Secrets must never
be command arguments. Generic servers use conservative execute/approval classification;
this release has no configurable trusted read-profile editor for generic servers.

## Validation and limitations

See PHASE4_VALIDATION.md and `evaluation/results/phase4/` for actual measurements.
Run `python -m unittest discover`, `python -m evaluation.phase4_smoke`,
`python -m evaluation.phase4_github_smoke`, and `python -m evaluation.benchmark_phase4`.
The runtime smoke uses fresh synthetic fixtures, actual FastAPI processes, actual
configured LLM calls, official-SDK stdio/HTTP MCP and a real backend restart. It never
connects its mock GitHub profile to GitHub. The separate GitHub runner is READ-only
and reports SKIPPED when no credential exists. Real write smoke remains SKIPPED
without `PHASE4_GITHUB_WRITE_SMOKE=true`, a named `PHASE4_GITHUB_TEST_REPOSITORY`, and
explicit durable approval. The current environment provides none of those settings.

Metrics are tiny acceptance fixtures with stated denominators, not production
accuracy. There is no Multi-Agent product, extra SaaS integration, distributed worker,
OAuth platform or broader authorization redesign in this phase.

Primary references checked during implementation:
[MCP tools specification](https://modelcontextprotocol.io/specification/2026-07-28/server/tools),
[official Python SDK transports](https://py.sdk.modelcontextprotocol.io/client/transports/),
[LangGraph interrupts](https://docs.langchain.com/oss/python/langgraph/interrupts),
[GitHub MCP server](https://github.com/github/github-mcp-server),
[GitHub remote configuration](https://github.com/github/github-mcp-server/blob/main/docs/remote-server.md).
