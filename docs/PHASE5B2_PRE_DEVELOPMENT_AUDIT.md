# PHASE 5B-2 PRE-DEVELOPMENT AUDIT

2026-10-03. Git safety completed; existing dirty changes retained.

| Method | Endpoint | Source | Line |
| --- | --- | --- | ---: |
| GET | / | server\main.py | 122 |
| GET | /health | server\main.py | 127 |
| GET | /sessions | server\main.py | 139 |
| GET | /indexes/{file_id} | server\main.py | 147 |
| GET | /sessions/{session_id}/messages | server\main.py | 159 |
| POST | /upload_pdf | server\main.py | 169 |
| POST | /chat | server\main.py | 187 |
| POST | /workspaces | server\main.py | 246 |
| GET | /workspaces | server\main.py | 254 |
| GET | /workspaces/{workspace_id} | server\main.py | 259 |
| DELETE | /workspaces/{workspace_id} | server\main.py | 264 |
| POST | /workspaces/{workspace_id}/sessions | server\main.py | 269 |
| GET | /workspaces/{workspace_id}/documents | server\main.py | 274 |
| POST | /workspaces/{workspace_id}/documents | server\main.py | 279 |
| DELETE | /workspaces/{workspace_id}/documents/{document_id} | server\main.py | 292 |
| GET | /memories | server\phase3_api.py | 69 |
| POST | /memories | server\phase3_api.py | 76 |
| PATCH | /memories/{memory_id} | server\phase3_api.py | 87 |
| DELETE | /memories/{memory_id} | server\phase3_api.py | 95 |
| GET | /memories/retrieve | server\phase3_api.py | 100 |
| POST | /workspaces/{workspace_id}/datasets | server\phase3_api.py | 107 |
| GET | /workspaces/{workspace_id}/datasets | server\phase3_api.py | 116 |
| GET | /workspaces/{workspace_id}/datasets/{dataset_id} | server\phase3_api.py | 122 |
| DELETE | /workspaces/{workspace_id}/datasets/{dataset_id} | server\phase3_api.py | 128 |
| POST | /workspaces/{workspace_id}/analysis | server\phase3_api.py | 134 |
| GET | /artifacts/{artifact_id} | server\phase3_api.py | 142 |
| POST | /tasks | server\phase3_api.py | 148 |
| GET | /tasks | server\phase3_api.py | 153 |
| GET | /tasks/{task_id} | server\phase3_api.py | 158 |
| POST | /tasks/{task_id}/run | server\phase3_api.py | 164 |
| POST | /tasks/{task_id}/resume | server\phase3_api.py | 173 |
| POST | /tasks/{task_id}/pause | server\phase3_api.py | 182 |
| POST | /tasks/{task_id}/cancel | server\phase3_api.py | 190 |
| GET | /tasks/{task_id}/events | server\phase3_api.py | 202 |
| GET | /tasks/{task_id}/artifacts | server\phase3_api.py | 206 |
| GET | /tools | server\phase4_api.py | 43 |
| GET | /tools/providers | server\phase4_api.py | 47 |
| POST | /tools/providers/{provider_id}/refresh | server\phase4_api.py | 51 |
| GET | /mcp/servers | server\phase4_api.py | 55 |
| GET | /mcp/servers/{server_id}/tools | server\phase4_api.py | 59 |
| POST | /mcp/servers/{server_id}/refresh | server\phase4_api.py | 63 |
| GET | /approvals | server\phase4_api.py | 67 |
| GET | /approvals/{approval_id} | server\phase4_api.py | 71 |
| POST | /approvals/{approval_id}/approve | server\phase4_api.py | 75 |
| POST | /approvals/{approval_id}/reject | server\phase4_api.py | 79 |
| POST | /approvals/{approval_id}/edit | server\phase4_api.py | 83 |
| GET | /tool-executions | server\phase4_api.py | 87 |
| GET | /agents | server\phase5_api.py | 17 |
| GET | /agents/{agent_id} | server\phase5_api.py | 22 |
| GET | /tasks/{task_id}/agents | server\phase5_api.py | 30 |
| GET | /tasks/{task_id}/delegations | server\phase5_api.py | 39 |
| GET | /health/live | server\runtime_services.py | 54 |
| GET | /health/ready | server\runtime_services.py | 58 |

## Single-user assumptions

- `server\memory.py:33` uses local_default.
- `server\memory.py:35` uses local_default.
- `server\memory.py:110` uses local_default.
- `server\memory.py:117` uses local_default.
- `server\memory.py:174` uses local_default.
- `server\phase3_api.py:82` uses local_default.
- `server\sessions.py:452` uses local_default.
- `server\tasks.py:15` uses local_default.
- `server\worker_runtime.py:75` uses local_default.
- `server\agent\orchestration.py:199` uses local_default.
- `server\agent\persistent_workflow.py:31` uses local_default.
- `server\agent\persistent_workflow.py:298` uses local_default.
- `server\db\repositories.py:23` uses local_default.

## Boundary findings

All business endpoints lack authentication/membership checks. Root/health should remain public. Workspace has no membership/creator. Session has workspace_id but no private creator. Task and Memory use local_default. Personal memory is shared by every local conversation. Worker validates literal local_default/job generation/workspace/session only. Approval decisions have no actor check; Artifact downloads confine paths but lack identity checks. MCP writes use ToolPolicy/HITL without RBAC. Streamlit uses raw requests, no tokens. Phase4 Origin middleware protects only approval origin; no unified auth, CORS or trusted host policy.

Logs are readable; optional LangSmith agent traces and persisted Phase5 run summaries exist. No infrastructure OTel/Prometheus instrumentation. Only PostgreSQL compose exists, no application image/full stack/CI. Secrets use environment/.env; .env remains untouched and must not enter image/logs/audit/telemetry.

## Implementation boundaries

Nullable creator fields; first registration never claims legacy data. Explicit dry-run owner CLI. Central workspace policy, owner-only Session privacy, personal Memory isolation, worker original actor checks, before-side-effect reauthorization. Services must fail closed without a principal in authenticated mode. AUTH_ENABLED=false remains development compatibility; production fails closed.

## Runtime environment

Windows/Python and isolated PostgreSQL binaries available. Docker absent; WSL reports not installed. True Compose/image acceptance awaits a usable Docker host; no native-process substitute will be labelled Compose PASS.

Primary references: [FastAPI Argon2/JWT](https://fastapi.tiangolo.com/tutorial/security/oauth2-jwt/), [PyJWT claims](https://pyjwt.readthedocs.io/en/latest/api.html), [OTel Python](https://opentelemetry.io/docs/languages/python/instrumentation/), [SQLAlchemy instrumentation](https://opentelemetry-python-contrib.readthedocs.io/en/latest/instrumentation/sqlalchemy/sqlalchemy.html).
