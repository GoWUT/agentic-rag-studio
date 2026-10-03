# Agentic RAG Studio

**An evidence-grounded research workspace with persistent agents, governed tools, and multi-user access.**

面向研究与数据分析的 Agent 工作台：跨文档问答、可恢复任务、多 Agent 协作，以及工作区级权限管理。

[![Validation](https://github.com/GoWUT/agentic-rag-studio/actions/workflows/ci.yml/badge.svg)](https://github.com/GoWUT/agentic-rag-studio/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/Python-3.11-3776AB?logo=python&logoColor=white)
![API](https://img.shields.io/badge/API-FastAPI-009688?logo=fastapi&logoColor=white)
![Agents](https://img.shields.io/badge/Agents-LangGraph-4338CA)
![Persistence](https://img.shields.io/badge/Persistence-PostgreSQL-4169E1?logo=postgresql&logoColor=white)

[Architecture](docs/ARCHITECTURE.md) · [Quick start](#quick-start) · [Evaluation](#evaluation-and-validation) · [Deployment](docs/PHASE5B2_DEPLOYMENT.md) · [Security](docs/PHASE5B2_SECURITY.md)

Upload papers, compare their evidence, analyze a dataset, and follow a task from planning through review. Answers retain document and physical-page citations; analysis produces downloadable artifacts; external writes require both permission and human approval. Workspaces, task events, memory, and graph checkpoints persist across restarts.

The current version targets local research and trusted teams. PostgreSQL, independent API/Worker/Streamlit processes, authentication, and RBAC have been exercised on Windows. Docker and Compose definitions are included; their container acceptance remains pending. See the [validation snapshot](docs/PHASE5B2_VALIDATION.md) for the precise scope.

## What you can do

| Workflow | Implemented behavior |
| --- | --- |
| Research across PDFs | Workspace-scoped retrieval, dense + BM25 fusion, optional reranking and OCR, document/page citations, bounded query rewrites and answer revision |
| Plan and delegate | Supervisor routes to Research, Data, Coding, and Reviewer agents; bounded parallelism, selective review, task-local caching, and aggregate budgets |
| Analyze datasets | CSV/XLSX/JSON ingestion, constrained Python execution, downloadable plots/tables, and artifact lineage |
| Resume durable work | Task state/events, graph checkpoints, pause/resume/cancel, and independent PostgreSQL-backed worker execution |
| Govern external tools | MCP tool registry, capability policy, OWNER-only external writes, human approval, execution receipts, and reconciliation for ambiguous writes |
| Collaborate with boundaries | Argon2id passwords, JWT access, rotating refresh tokens, live OWNER/EDITOR/VIEWER checks, private sessions, and scoped memory |
| Inspect execution | Structured redacted logs, OpenTelemetry traces/metrics, optional LangSmith, and provisioned monitoring configuration |

For example: create a workspace, upload two papers and a CSV, ask for an evidence-backed comparison, then request a chart. Inspect the plan, source citations, agent runs, task events, and generated artifacts in Streamlit. Optional GitHub MCP actions use the same permission and approval path.

## Architecture

![Agentic RAG Studio system architecture](docs/assets/architecture.svg)

The diagram shows PostgreSQL worker mode. Local development can use SQLite and inline execution. [Editable diagrams and execution flows](docs/ARCHITECTURE.md) explain storage, authorization, and recovery.

- **Bounded execution:** retrieval retries, delegations, review, tool calls, and context have explicit limits. Simple questions can bypass multi-agent orchestration.
- **Durable state:** PostgreSQL stores business data, queue jobs, and checkpoints; a shared filesystem stores documents, Chroma indexes, datasets, and artifacts. Recovery requires both stores.
- **Live authority:** API access, queued execution, and external side effects recheck the original actor's current permissions. Approval also needs permission at execution time.

## Quick start

Requires Python 3.11 and [uv](https://docs.astral.sh/uv/). Run commands from the repository root.

```bash
git clone https://github.com/GoWUT/agentic-rag-studio.git
cd agentic-rag-studio
uv sync --frozen
```

On a fresh clone, copy `.env.example` to `.env` and edit it. Keep an existing `.env` intact.

```bash
# macOS / Linux
cp .env.example .env
```

```powershell
# Windows PowerShell
Copy-Item .env.example .env
```

For a local text-PDF demo, set:

```dotenv
DEEPSEEK_API_KEY=your_deepseek_api_key
MODEL_NAME=your_available_deepseek_model
PDF_OCR_MODE=off
DATABASE_BACKEND=sqlite
TASK_EXECUTION_MODE=inline
AUTH_ENABLED=false
```

Choose a model available to your provider account. `SERPER_API_KEY` is optional for web search; document-only research does not require it. Embedding/reranking models download on first use and need network access and local storage. Disable optional reranking with `RERANKER_ENABLED=false` if its CPU latency is unsuitable.

For scanned PDFs, install the OCR extra with `uv sync --frozen --extra ocr`, then set `PDF_OCR_MODE=auto`. OCR also downloads model weights on first use.

Start the API and UI in separate terminals:

```bash
# Terminal 1
uv run --no-sync python -m server.api
```

```bash
# Terminal 2
uv run --no-sync streamlit run client/app.py --server.port 8501
```

Open [Streamlit](http://127.0.0.1:8501). The default local API is at `http://127.0.0.1:8001`; [interactive API docs](http://127.0.0.1:8001/docs) are enabled in development. `API_BASE` changes the UI's backend address.

This auth-disabled configuration is for loopback development. For shared use, enable authentication in both API and UI and follow the deployment runbook.

## PostgreSQL and background workers

For a native deployment, provide a PostgreSQL connection and explicitly configure:

```dotenv
DATABASE_BACKEND=postgresql
DATABASE_URL=postgresql+psycopg://USER:URL_ENCODED_PASSWORD@HOST:5432/DB
TASK_EXECUTION_MODE=worker
TASK_QUEUE_ENABLED=true
APP_ENV=production
AUTH_ENABLED=true
AUTH_JWT_SECRET=YOUR_RANDOM_SECRET_OF_AT_LEAST_32_BYTES
AUTH_ALLOW_REGISTRATION=false
TRUSTED_HOSTS=localhost,127.0.0.1
CORS_ALLOWED_ORIGINS=http://127.0.0.1:8501
```

Generate the JWT secret privately; keep credentials outside version control. All processes need consistent backend/auth settings and access to the same workspace directory. Configure hosts/origins for your actual access boundary.

```bash
uv run --no-sync python -m server.migrate
uv run --no-sync python -m server.manage create-user --email owner@example.org
# Password is prompted without echo; no default administrator is shipped.
uv run --no-sync python -m server.api
```

```bash
# Separate worker process
uv run --no-sync python -m server.worker
```

```bash
# Separate UI process; AUTH_ENABLED=true must also reach Streamlit
uv run --no-sync streamlit run client/app.py --server.port 8501
```

Task run/resume requests enqueue work and return HTTP 202 in worker mode. The API does not start a worker automatically. Role/status changes are checked again when a job begins. Uncertain external-write outcomes require reconciliation before retry.

[`compose.yaml`](compose.yaml) defines PostgreSQL, one-shot migration, API, Worker, Streamlit, and an optional observability profile. The [deployment runbook](docs/PHASE5B2_DEPLOYMENT.md) covers container commands, health checks, backups, secrets, and upgrades. Docker/WSL installation and container execution were deferred; the configuration is not evidence of a successful container deployment.

## Access model

| Capability | OWNER | EDITOR | VIEWER |
| --- | --- | --- | --- |
| Read workspace assets; external read tools | Yes | Yes | Yes |
| Read workspace tasks | All | All | Own |
| Execute/pause/cancel tasks | Workspace | Own | Own read-only |
| Upload/delete documents and datasets; run analysis | Yes | Yes | No |
| Update shared workspace memory | Yes | Yes | No |
| Manage workspace and members | Yes | No | No |
| External writes | With human approval | No | No |

Sessions and personal memory remain private to their creator, including against other workspace owners. Membership is checked live rather than embedded in JWT roles. Existing unclaimed data requires explicit operator assignment; registering the first account does not claim it. See the [security model](docs/PHASE5B2_SECURITY.md), including token revocation behavior and deployment limits.

## Evaluation and validation

The latest local acceptance snapshot, **2026-10-03**, records **531 distinct regression tests passed**, with zero failed or skipped test cases. The native integration scenario records **43 PASS / 0 FAIL / 2 SKIPPED**; both skipped entries are container-only checks. Full discovery covered 529 tests; a final 52-test security run included two subsequently added tests, yielding 531 distinct cases. [Machine-readable summary](evaluation/results/phase5b2/validation_summary.json) · [Detailed report](docs/PHASE5B2_VALIDATION.md).

That scenario uses real PostgreSQL, migrations, Argon2/JWT, HTTP, filesystem ingestion, independent processes, LangGraph checkpoints, analysis subprocesses, and OTLP export. LLM, embedding, retrieval, and external-provider behavior use declared deterministic fixtures/local mock MCP. It verifies isolation and execution plumbing; it does not measure live model answer quality or public deployment capacity.

### Retrieval ablation

A separate recorded experiment covers **3 papers, 60 manually anchored queries, 46 pages, and 344 chunks**. Retrieval-only measurements on the recorded CPU host:

| Method | Hit@5 | MRR@5 | P95 latency (ms) |
| --- | ---: | ---: | ---: |
| Dense | 0.6833 | 0.4483 | 8.26 |
| BM25 | 0.9333 | 0.6836 | 0.21 |
| Hybrid RRF | 0.8333 | 0.5914 | 9.29 |
| Hybrid + reranker | 0.8833 | 0.6750 | 1883.45 |

BM25 performs best overall on this small page-anchored corpus. Reranking improves some hybrid misses but adds substantial CPU latency and introduces other regressions. These results support configurable retrieval, with no claim of a universal winner. [Full report and confidence intervals](evaluation/results/retrieval_v2_multi_report.md) · [Dataset manifest and reproduction requirements](evaluation/datasets/README.md).

### Run checks

```bash
uv lock --check
uv run --no-sync python -m unittest test_phase5b2 -v
uv run --no-sync python -m unittest discover -v
```

Full regression expects the OCR extra. PostgreSQL integration tests opt in through `PHASE5B1_TEST_DATABASE_URL`; without it, their cases skip. Provider network smoke scripts require explicit execution. [GitHub Actions](.github/workflows/ci.yml) defines quality, regression, auth/RBAC, PostgreSQL integration/migration/worker, and Docker-build jobs. The badge reflects remote CI; recorded local acceptance is reported separately above.

## Project map

```text
client/                 Streamlit workspace, auth, task and agent interfaces
server/auth/            Accounts, token lifecycle, live RBAC, scoped access
server/agent/           LangGraph workflows, specialists, harness and budgets
server/rag/             PDF extraction/OCR, indexes and workspace retrieval
server/db/              SQLAlchemy persistence and versioned schemas
server/observability/   Redacted logging, metrics, tracing and LangSmith
server/api.py           Cross-platform API launcher
server/worker.py        Independent Procrastinate worker
alembic/                Business-schema migrations
evaluation/             Fixtures, benchmarks and recorded validation evidence
observability/          Collector, Tempo, Prometheus and Grafana configuration
docs/                   Architecture, security, deployment and phase reports
```

## Further reading and boundaries

- [System architecture and task lifecycle](docs/ARCHITECTURE.md)
- [Research pipeline](docs/PHASE2_IMPLEMENTATION.md), [memory/data/tasks](docs/PHASE3_IMPLEMENTATION.md), and [MCP approval flow](docs/PHASE4_IMPLEMENTATION.md)
- [Multi-agent orchestration](docs/PHASE5A_IMPLEMENTATION.md) and [efficiency decisions](docs/PHASE5A5_IMPLEMENTATION.md)
- [PostgreSQL migration](docs/PHASE5B1_MIGRATION.md), [auth implementation](docs/PHASE5B2_IMPLEMENTATION.md), and [deployment operations](docs/PHASE5B2_DEPLOYMENT.md)

The analysis runner constrains imports, time, outputs, and artifacts, but is **not an OS security sandbox**. Shared local files/Chroma and code execution assume trusted users. Public untrusted multi-tenant execution needs stronger isolation. HA, SSO, email verification/reset delivery, global rate limiting, and container runtime acceptance are outside the validated scope. Historical phase reports describe their own checkpoints; this README describes the current assembled system.

## Acknowledgment

This project builds on [IbraahimLab/Agentic-RAG-with-FastAPI-and-Streamlit](https://github.com/IbraahimLab/Agentic-RAG-with-FastAPI-and-Streamlit), extending the original FastAPI/Streamlit RAG foundation with persistent workspaces, evaluated retrieval, durable execution, multi-agent orchestration, and multi-user governance.
