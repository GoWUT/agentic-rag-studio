# Phase 5B-2 deployment and operator runbook

Container files are prepared; Docker/WSL installation and container execution are
deferred by explicit user instruction. The validated runtime is native Windows
PostgreSQL/API/Worker/Streamlit. Do not infer an image-build, Compose-runtime,
image-history or live container non-root PASS from configuration checks.

## Configuration

Keep the existing `.env` intact. Prepare a separate untracked `.env.deploy` or
environment injection for deployment. Never pass secrets as build arguments.
`.dockerignore` allowlists application source/locked dependency files only.

| Variable | Requirement |
| --- | --- |
| POSTGRES_USER / POSTGRES_PASSWORD / POSTGRES_DB | Explicit DB credentials/name; no shipped default password |
| DATABASE_URL | `postgresql+psycopg://USER:URL_ENCODED_PASSWORD@postgres:5432/DB`; Compose host is `postgres` |
| AUTH_JWT_SECRET | Cryptographically random, at least 32 bytes; API/Worker agree |
| AUTH_JWT_ISSUER / AUTH_JWT_AUDIENCE | Match all instances; Compose supplies public application identifiers |
| AUTH_ENABLED | True for production; Streamlit must receive the same setting |
| AUTH_ALLOW_REGISTRATION | Defaults false in Compose; create users through CLI |
| TRUSTED_HOSTS / CORS_ALLOWED_ORIGINS | Actual API hostnames and UI origins; replace loopback defaults for TLS proxy |
| DEEPSEEK_API_KEY / MODEL_NAME / SERPER_API_KEY | Existing providers, only when used; no real keys needed for tests |
| GITHUB_MCP_ENABLED / GITHUB_PERSONAL_ACCESS_TOKEN / GITHUB_ALLOWED_REPOSITORIES | Existing optional GitHub provider; OWNER + HITL still required |
| OTEL_ENABLED / OTEL_EXPORTER_OTLP_ENDPOINT | Optional; Collector HTTP OTLP endpoint, not a public cloud credential |
| GRAFANA_ADMIN_PASSWORD | Explicit nonempty password before enabling observability; Grafana startup refuses empty |
| INSTALL_OCR | False default image; true installs the locked optional PaddleOCR stack |

Generate the JWT secret in a private operator terminal and put it in deployment
environment storage, not source control:

```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

Access/refresh/password/lockout durations are configurable; defaults are 20 minutes,
seven days, 12–128 characters, five failed attempts and 15-minute lockout. Never
publish the development auth-disabled fallback. `APP_ENV=production` fails closed
without auth unless the explicit emergency override is set; do not use that override
for a public deployment. Production API docs default off in Compose.

## Container startup, when Docker validation is resumed

```bash
docker compose --env-file .env.deploy config --quiet
docker compose --env-file .env.deploy build
docker compose --env-file .env.deploy up -d postgres migrate api worker streamlit
docker compose --env-file .env.deploy ps
curl --fail http://localhost:8001/health/ready
docker compose --env-file .env.deploy run --rm --no-deps api python -m server.manage create-user --email owner@example.org --display-name Owner
```

The CLI prompts for the password without echo and has no default account. Do not
put a password in command arguments. Grant a workspace membership through its OWNER,
using an already-registered email. CLI reset/disable examples:

```bash
docker compose --env-file .env.deploy run --rm --no-deps api python -m server.manage reset-user-password --email owner@example.org
docker compose --env-file .env.deploy run --rm --no-deps api python -m server.manage set-user-status --email owner@example.org --status DISABLED
```

The image uses Python 3.11 and frozen `uv.lock`; uid/gid is 10001. API, Worker and
Streamlit share the image but run independently. Source is read-only; `/tmp` is
tmpfs; one named `/data` volume retains PDFs, Chroma, datasets, artifacts and model
cache. PostgreSQL uses a separate named data volume. The image creates owned `/data`
directories for an initial named volume. Bind mounts require operator-set ownership;
this phase does not change host-directory ownership automatically.

Order is PostgreSQL health → one-shot migration success → API/Worker → API readiness
→ Streamlit. The migration calls Alembic, then official queue/saver schema setup;
its nonzero exit prevents API/Worker startup. Business DDL uses Alembic transactions.
Queue/saver setup is a separate idempotent SDK operation, not a single transaction
with business DDL. Never rerun migration automatically in every API process.

Worker shutdown grace is 130 seconds, above its 120-second drain limit. Its probe
reads a tmpfs receipt containing its own SDK worker ID plus a recent DB heartbeat;
a healthy peer cannot mask a stopped worker. API/Streamlit probes use ready/health
endpoints. Container restart preserves the named volumes. Never use `down -v` on
retained data. `docker compose restart api worker` does not apply a new environment;
recreate services when changing configuration.

Text PDF dependencies are installed by default. Build with `INSTALL_OCR=true` for
scanned PDFs requiring OCR, then test representative documents/model downloads.
Linux Torch/OCR dependency downloads may produce a large image; image size,
build time, OCR runtime and Linux native-library compatibility remain unmeasured.
The model cache is persistent; do not bake private documents/model credentials into
an image. The tested native runtime used declared synthetic embeddings for the new
security scenario, while the existing ingestion/OCR regression still passed.

## Native production process mode

Set PostgreSQL, worker mode, auth and production host/origin environment explicitly;
use the existing virtual environment without replacing local extras:

```bash
uv sync --inexact
uv run --no-sync python -m server.migrate
uv run --no-sync python -m server.manage create-user --email owner@example.org
uv run --no-sync python -m server.api
# Separate process; API never starts this for you.
uv run --no-sync python -m server.worker
# Same AUTH_ENABLED setting and API_BASE as the server deployment.
uv run --no-sync streamlit run client/app.py
```

The cross-platform API launcher supplies psycopg-compatible Windows Selector loops
without changing the global MCP subprocess loop policy. Default `.env`/SQLite/inline
values were not changed in this task. Explicitly export `API_HOST=0.0.0.0` only behind
your intended access boundary; native default remains loopback.

## Upgrade and legacy ownership

Stop execution/traffic, take backups, then upgrade `5b1_0001` → `5b2_0001`. Existing
rows keep IDs/payloads/paths; their creator stays NULL. Existing data is never assigned
to the first registering account. An operator must choose the target explicitly:

```bash
python -m server.manage assign-legacy-data --user owner@example.org --dry-run
python -m server.manage assign-legacy-data --user owner@example.org --apply
```

Dry-run reports counts and the target UUID without changing ownership. Apply grants
OWNER membership for unclaimed workspaces, stamps their sessions/tasks and personal
memory ownership, and audits assignment in one transaction. Active/interrupted legacy
tasks block assignment; finish/cancel/reconcile them before cutover. No real user was
chosen and no existing local ownership was changed in this implementation run.

Do not downgrade production auth DDL after creating users: downgrade removes auth
tables/creator columns. The tested reverse migration used only disposable schemas
with no real accounts. Roll back application configuration/code while preserving
DB backups, and plan a deliberate data migration rather than deleting account state.
The original SQLite importer remains business-only; use read-only source backups
and its dry-run. It does not migrate authenticated SQLite account/refresh tables.

## Observability

```bash
# Set OTEL_ENABLED=true and GRAFANA_ADMIN_PASSWORD in the deployment environment.
docker compose --env-file .env.deploy --profile observability up -d
```

Collector receives HTTP OTLP from API/Worker, sends traces to Tempo and exposes
aggregated metrics to Prometheus. Grafana provisions Prometheus/Tempo plus a small
runtime dashboard. Do not expose Collector, DB, `/metrics` or tracing UI publicly.
Prometheus independently scrapes API gauges and Collector metrics; avoid summing
duplicate API counters across both jobs. Worker counters are in its own process and
arrive via Collector. OTel exporter queues/timeouts are bounded; a collector outage
does not fail core requests. Export warnings still consume log/storage capacity.

LangSmith is optional and preserves the existing hidden input/output defaults;
infrastructure telemetry does not replace existing agent observability. Token-usage
counters add only provider-reported usage; a zero counter from a fixture is not a
claim that real usage was known or free. Counters are process-lifetime measurements
and reset on restart. For durable historical totals, query tasks/audit separately.

## Backups and recovery

Back up PostgreSQL **and** the shared file/Chroma/artifact volume at a coordinated
execution boundary. A DB-only backup cannot restore referenced local files.
Use a protected dump destination; dump includes account hashes and token digests:

```bash
docker compose --env-file .env.deploy exec -T postgres sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' > protected-backup.dump
```

Keep secrets separately, restrict backup access, and restore into a new isolated
database/volume first. Do not print DATABASE_URL or expanded Compose environments.
Check revision/DB health, memberships/private creators, file paths, queue status
and saver resume after restore. Ambiguous external writes require manual receipt
reconciliation; do not force retry them to test recovery.

## CI and release

`ci.yml` checks the dependency lock, Python source compilation, diff whitespace,
and exclusion of local test files. Test sources and acceptance fixtures stay local
and are not available in a public clone. These source checks do not establish
runtime or container readiness. The optional tagged release workflow invokes these
source checks before building and publishing an image; no release tag is created
by a normal branch push.

Local `actionlint` verifies workflow schema/expressions and YAML checks cover Compose
and observability configs. Consult the current GitHub Actions run for remote CI.
Docker build/Compose config rendering,
runtime volumes, image history/secrets/non-root checks and the Grafana/Tempo dashboard
still need execution on a Docker host. Loopback configuration is not a public TLS
deployment. Perform those release checks before advertising container readiness.

Primary deployment references: [uv Docker guide](https://docs.astral.sh/uv/guides/integration/docker/),
[Compose startup conditions](https://docs.docker.com/compose/how-tos/startup-order/),
[actionlint release](https://github.com/rhysd/actionlint/releases/tag/v1.7.12).
