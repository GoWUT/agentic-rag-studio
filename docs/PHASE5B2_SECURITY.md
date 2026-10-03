# Phase 5B-2 security model

Authentication identifies a user; authorization checks the current resource
relationship. A valid JWT never grants access to all workspaces.

## Accounts and tokens

Accounts use UUID IDs and unique trimmed/lowercase email. `pwdlib`'s recommended
Argon2id hasher stores encoded salted hashes; password length defaults to 12–128.
Unknown-email and wrong-password login return `Invalid credentials` with a dummy
Argon2 verification for unknown accounts. Default lockout is five attempts for
15 minutes; success clears the failure counter. No default administrator exists.
Registration creates only an account and can be disabled. Reset is operator-assisted
CLI only: no email verification, reset emails, SSO or OAuth in this phase.

JWT access lasts 20 minutes by default. HS256 is fixed by the server; required
claims are sub/jti/type/iat/exp/iss/aud, with signature, expiry, issuer, audience,
timestamp and `type=access` validation. There are no JWT workspace roles. Each
protected request looks up the User; disabled accounts fail immediately. JWT secret
comes from the environment and must contain at least 32 bytes. Operators should
generate a cryptographically random secret rather than merely meeting length.

Refresh tokens are random opaque values; only SHA-256 token digests are stored.
They default to seven days, rotate transactionally and link a parent/family.
Replaying a used token revokes the complete family, including its replacement.
Concurrent refresh has at most one successful rotation; a duplicate then revokes
the family. UI refreshes once per request; concurrent browser operations may still
trigger this fail-closed family revocation and require login again.

Logout revokes the submitted refresh token. Logout-all/password change revoke all
user refresh tokens. Password change verifies the old password. Operator reset and
disable also revoke refresh tokens. Access JWTs are not individually blacklisted:
logout/reset/password change may leave an already-issued access token usable until
expiry unless the account is disabled. JWT signing-secret rotation invalidates
access tokens globally; coordinate all API instances and Worker configuration.

## Role matrix

| Capability | OWNER | EDITOR | VIEWER |
| --- | --- | --- | --- |
| Workspace/assets read; external read | Yes | Yes | Yes |
| Own private sessions/read-only tasks | Yes | Yes | Yes |
| Workspace task read | All | All | Own |
| Task execute/pause/cancel | Workspace | Own | Own read-only |
| Documents/datasets create/delete | Yes | Yes | No |
| Personal Memory read/write | Own only | Own only | Own only |
| Workspace Memory write; analysis | Yes | Yes | No |
| Workspace update/delete; member management | Yes | No | No |
| MCP external write | OWNER + HITL | No | No |

OWNER/EDITOR do not receive access to another user's private session. A workspace
member's task visibility does not expose that owner's session history. VIEWER
tasks cannot bypass analysis/write guards. The last OWNER cannot be removed or
demoted, including simultaneous demotions. Membership changes and live status
take effect without issuing a new access JWT.

Workspace/Document/Dataset authorization checks their workspace. Session checks
its creator plus current membership. Task checks creator and role; Artifact checks
workspace plus linked task; personal Memory checks owner/scope, shared Memory checks
membership. Approval, ToolExecution, AgentRun and Delegation resolve their task or
actor-bearing supervisor before access. Lists filter inaccessible resources.
Known IDs with no permission return 403; absent IDs may return 404, so resource
existence hiding is not a universal guarantee. No content is returned on denial.

## Worker and external actions

Enqueue, execution and side effects each authorize independently. The Worker uses
the durable task creator, never a JWT or unrestricted service principal. Missing
membership/disabled actor fails before graph execution. External RBAC precedes
ToolPolicy and HITL; EDITOR cannot create a write approval. Approval decisions
require OWNER write permission. A previously approved write cannot execute after
its original actor loses that permission. Rejected/pending decisions never call
the provider. Authorization is rechecked just before claim/call; the database check
and a remote server's receipt are not one distributed transaction. Revocation after
dispatch cannot cancel an already-sent remote request.

Security audit includes auth, lockout, refresh/replay, logout/password, workspace,
membership, authorization denial, approval decisions and successful external write.
Audit metadata accepts only fixed operational fields. Email/password/tokens/headers/
DB credentials are excluded. Audit is append-only by application convention, not a
cryptographically tamper-proof external log; DB operators can modify it.

## Deployment privacy and limits

Production requires auth, explicit trusted hosts and explicit CORS origins.
Credentialed wildcard CORS is absent; API docs are configurable. Responses add
request ID, nosniff, DENY framing and no-referrer headers. Pydantic validation
responses omit input/context so bad passwords are not echoed. Logs redact known
environment secrets, sensitive keys, bearer/JWTs, DSN passwords and URL queries;
exception formatting emits types. Exported OTel spans drop unapproved attributes,
events and error descriptions. LangSmith inputs/outputs remain hidden by default;
operators must preserve those settings when enabling it. No cloud tracing credentials
or real provider calls were used in this acceptance run.

Metrics/health are unauthenticated internal operational endpoints. Keep them behind
the internal network/reverse proxy rather than expose them publicly. Compose binds
API/UI/monitoring ports to loopback and does not publish PostgreSQL. Configure TLS,
host/origin routing and operational access before a public demo. Login lockout is
per account, not an IP/global rate limiter; registration can cause Argon2 load.

Data/code processes share local filesystem/Chroma, and the constrained analysis
runner is explicitly **not an OS security sandbox**. Non-root containers reduce
privilege but do not isolate hostile user analysis on a shared writable volume.
This first version is suitable for trusted-team deployment after container checks;
untrusted public multi-tenant code execution requires stronger execution isolation.
No HA, email ownership verification, external audit retention or automated backup
rotation is claimed. Container runtime/security acceptance remains deferred.

Implementation references: [FastAPI pwdlib/Argon2 JWT](https://fastapi.tiangolo.com/tutorial/security/oauth2-jwt/),
[PyJWT validation](https://pyjwt.readthedocs.io/en/latest/api.html),
[OTel Python instrumentation](https://opentelemetry.io/docs/languages/python/instrumentation/).
