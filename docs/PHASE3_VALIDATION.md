# Phase 3 validation — 2026-10-02

Result: **implemented and validated for a single-process local deployment, with
the limitations documented below**. No commit/push; existing `.env`, sessions,
databases and PDF indexes retained.

## Regression and local execution

| Check | Result |
| --- | --- |
| Previous tests | 140 |
| New Phase 3 tests | 96 |
| Total / passed / failed / skipped | 236 / 236 / 0 / 0 |
| Python compile check | PASS |
| `git diff --check` | PASS |
| `uv lock --check` | PASS |
| `pip check` | PASS |

The regression suite runs against a temporary runtime workspace so imports and
API construction cannot interact with live smoke tasks. Tests use scripted model
outputs for graph decisions, with real pandas/openpyxl parsing and real Python
subprocesses for execution, PNG generation, output/artifact limits and timeout.

Raw results: `evaluation/results/phase3/regression.json`.

## Real smoke — actual HTTP, subprocesses and configured provider

FastAPI runs on `127.0.0.1:8001`, Streamlit on `127.0.0.1:8501`. Provider is the
existing configured DeepSeek model, without replacing `.env` or mocking responses.
Only a dedicated synthetic Workspace was sent to the provider: one test PDF,
three experiment rows, two-sheet XLSX and ordinary Markdown format preferences.

| Real action | Final result |
| --- | --- |
| Start FastAPI / Streamlit and render sidebar | PASS |
| Create Workspace / new sessions | PASS |
| Upload synthetic PDF | PASS |
| Upload CSV and multi-sheet XLSX | PASS |
| Create Long-Term Memory | PASS |
| Retrieve memory in a new session | PASS |
| Real LLM restores Markdown preference | PASS |
| Explicit Remember followed by another session | PASS |
| Real generated-code data analysis | PASS |
| Best mIoU under 3M: A, 2.5M, 73.27 | PASS |
| Real PNG generation and download | PASS |
| Create durable task / persist plan and first step | PASS |
| Boundary pause | PASS |
| Stop and restart the actual backend process | PASS |
| Resume to completion | PASS |
| Completed steps are not repeated | PASS |
| Task artifacts, filenames and dataset provenance | PASS |
| Preserve PDF and analysis citations after checkpoint | PASS |
| Open/download task PNG | PASS |
| Legacy `/upload_pdf` + `/chat`, correct physical page 1 | PASS |

The final completed task is `a6950e64-6c54-4032-843c-6c7429691b0d`.
All its steps completed with one attempt each. Step evidence connects the synthetic
PDF with `experiment.xlsx`, the analysis execution and registered artifacts. Final
source citations survived checkpoint/resume; verification used the supplied evidence.
The earlier successful task records and failed attempts remain available in SQLite.

Raw results: `evaluation/results/phase3/smoke.json`, `chat_memory.json`,
`chat_explicit_memory_recall.json`, `chat_data.json`, `chat_legacy.json`.
`smoke.json` retains chronological checks, including failures encountered during
development; **`latest_checks` is the final outcome per check**, not a claim that
every earlier attempt passed.

Streamlit was inspected in the actual browser: Memory view/edit/delete controls,
CSV/XLSX counts, task status/steps, PNG preview and JSON/download controls rendered.
Task status updated from RUNNING to COMPLETED with the refresh control.

## Security tests

Before spawning the child process, AST rejects os/subprocess/socket/requests and
other forbidden imports, eval/exec/compile/import/globals/locals/getattr/open,
dunder/private attributes, absolute paths, traversal and arbitrary host reads.
Additional tests reject library escapes via `collections.sys.modules`, imports
of exposed internal modules, pandas Styler environment access and matplotlib
canvas file writers. Real HTTP smoke confirms os/socket/host-path/dunder rejection.
Allowed pandas calculations, NumPy reductions, annotated charts, and guarded JSON/
TXT output run in a real child process. Syntax/NameError repair is capped at one.

Timeout kills the child; output is truncated; oversized/excess artifacts are rejected;
artifact downloads require a registered ID and managed path. These controls reduce
risk and are **not a complete OS security boundary**.

## Phase 2 compatibility

Original 140 regression tests remain passing. Additionally, all three existing
paper indexes (GCNet, TDA-YOLO, PViGS) were queried using actual local embedding and
reranker models with `HF_HUB_OFFLINE=1`. Five final hits covered all three papers;
all had real rerank scores. No LLM request or network access was used for this check.
Raw result: `phase2_retrieval_compatibility.json`.

Phase 2.5's broader real-paper external comparison was interrupted by the Phase 3
request and was previously rejected by automatic approval review because its
external disclosure authorization was considered insufficient. It is not counted
as completed here; Phase 3 smoke instead used expressly identified synthetic data.

## Development failures and fixes

- Concurrent transformer construction left meta tensors in two document retrievers:
  cache embedders and serialize model loading; resolve cached snapshot paths for
  truly local tokenizer/reranker initialization.
- AST initially over-rejected `pd.to_numeric`, `plt.text` and `_` loop placeholders:
  distinguish safe conversions/chart APIs and plain variable names from private access.
- NumPy C reductions needed a framework-only internal import; generated imports of
  implementation modules remain rejected.
- Generated JSON writing used forbidden `open()`: supply a guarded writer and explicit
  generation protocol, retaining the open prohibition.
- Store construction incorrectly triggered restart recovery during live tests:
  register recovery only on actual backend startup and isolate test runtime storage.
- Completed synthesis was rendered twice, stripping public PDF citations:
  preserve the already validated checkpointed final answer/citations.
- Analysis evidence lacked dataset filenames/schema and artifact metadata:
  include authoritative lineage so the verifier can identify the actual XLSX source.

## Fixture evaluation and limits

`benchmark_phase3` is a small deterministic fixture runner: four memories, ten unsafe
snippets, one real analysis/chart, one checkpoint/recovery task. Fixture memory
precision/recall were 1.0, duplicate/irrelevant rates 0; unsafe rejection was 10/10;
analysis/chart and checkpoint recovery succeeded. This is not a production benchmark.
The two-case tool-selection rubric scored 2/2 against recorded real data/legacy smoke
traces; it is null if those files are unavailable. This is a small required/forbidden
tool check, not a statistically valid production accuracy estimate.

Limits: one backend process per DB, no background worker/Auth/RBAC, conservative
secret heuristics, no Windows kernel quotas, retained failed-run directories,
UTF-8 CSV only, and possible repetition of an interrupted action that ran before its
checkpoint commit. Completed checkpoints are protected against repeat execution.
