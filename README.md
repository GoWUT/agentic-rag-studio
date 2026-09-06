# Agentic RAG Studio

Agentic RAG Studio is a full-stack application for chatting with PDF documents. A LangGraph agent chooses between the uploaded knowledge base, Google search, and arXiv, while FastAPI and Streamlit provide the API and user interface.

## Features

- Persistent PDF indexes and chat sessions across restarts
- Agentic routing between PDF retrieval, Serper web search, and arXiv
- Context budgeting, history compaction, and safe tool-message handling
- Execution limits, timeouts, retries, and per-session concurrency control
- Validated PDF ingestion with isolated index builds and atomic publishing
- LangSmith tracing and offline evaluation utilities

## Tech Stack

- FastAPI, Streamlit, and Pydantic
- LangChain, LangGraph, and LangSmith
- Chroma, SQLite, and Hugging Face embeddings
- DeepSeek, Google Serper, arXiv, and RAGAS

## Project Structure

```text
client/       Streamlit chat interface
server/       FastAPI API, agent workflow, sessions, and RAG pipeline
shared/       Shared models and utilities
evaluation/   Retrieval, engineering, and RAGAS evaluation tools
```

## Getting Started

### 1. Install dependencies

Python 3.11 or later is required.

```bash
uv sync --extra ocr
```

Alternatively:

```bash
python -m venv .venv
pip install -e ".[ocr]"
```

### 2. Configure environment variables

Copy `.env.example` to `.env` and provide at least:

```env
DEEPSEEK_API_KEY=your_deepseek_api_key
SERPER_API_KEY=your_serper_api_key
```

The remaining model, context, timeout, embedding, and tracing settings can be customized in `.env`.

### 3. Start the API

```bash
uv run --extra ocr uvicorn server.main:app --reload --port 8001
```

### 4. Start the client

```bash
uv run --extra ocr streamlit run client/app.py --server.port 8501
```

Open `http://127.0.0.1:8501`, upload a searchable or scanned PDF, and start chatting.

## How It Works

1. The backend validates the uploaded PDF and creates or reuses a persistent Chroma index.
2. A session-specific LangGraph agent is restored or created.
3. For each question, the agent decides whether to search the PDF, the web, or arXiv.
4. Messages and index metadata are stored locally so sessions survive application restarts.

## Acknowledgements

This project is based on [IbraahimLab/Agentic-RAG-with-FastAPI-and-Streamlit](https://github.com/IbraahimLab/Agentic-RAG-with-FastAPI-and-Streamlit) and extends it with persistence, recovery, safer ingestion, context and execution controls, evaluation tooling, and broader test coverage.

### Hybrid retrieval and reranking

PDF search now combines Chroma dense retrieval and a per-PDF BM25 index using
reciprocal rank fusion (equal weights, RRF constant 60). Each branch retrieves
`RETRIEVAL_CANDIDATE_K=20` chunks; fusion keeps 20 candidates, then a local
cross-encoder reranks them and returns `RETRIEVAL_TOP_K=5` chunks. Original page
metadata is preserved. BM25 uses English words and Chinese characters/bigrams.

The BM25 index is built once per session runtime from persisted Chroma chunks,
including when restoring old sessions. Existing PDFs need no re-embedding.
The corpus is held in memory and assumes the session's PDF index is immutable.

Defaults are listed in `.env.example`. Both `HYBRID_RETRIEVAL_ENABLED` and
`RERANKER_ENABLED` default to `true`; set both to `false` for vector-only search.
Restart the backend after changing configuration. `RETRIEVAL_CANDIDATE_K` must
be at least `RETRIEVAL_TOP_K`.

The default `BAAI/bge-reranker-base` uses the existing `sentence-transformers`
dependency. It loads lazily on the first search and is shared across sessions
within each server process. The first use requires a model download unless
cached; set `RERANKER_MODEL` to a local model directory for offline use.
`RERANKER_DEVICE=cpu` can be changed to `cuda` with a compatible PyTorch/GPU
installation. Inference uses batches of 8 and truncates each query/chunk pair
to 512 model tokens. CPU inference and initial download add latency.

With `RERANKER_FALLBACK=true`, model loading or inference failure logs a warning
and returns the pre-rerank results. Set it to `false` to surface failures (useful
for evaluation). The existing retrieval benchmark remains a vector-only baseline;
its historical metrics do not measure this new pipeline.

Implementation references: [Chroma Get](https://docs.trychroma.com/docs/querying-collections/query-and-get)
and [Sentence Transformers CrossEncoder](https://www.sbert.net/docs/package_reference/cross_encoder/model.html).
The regression test suite is maintained locally and excluded from this repository.


### PaddleOCR PDF extraction

Install the CPU OCR extra with `uv sync --extra ocr` (or `pip install -e ".[ocr]"`).
Keep `--extra ocr` on `uv run` commands so OCR dependencies remain available.
PaddleOCR 3.x / PP-OCRv5 runs locally in the existing isolated index worker.
The first OCR page downloads official models; allow extra time and network access.
The index worker has a 600-second build limit including model loading and embedding;
the frontend upload request waits up to 660 seconds.

```env
PDF_OCR_MODE=auto
PDF_OCR_LANG=ch
PDF_OCR_DEVICE=cpu
PDF_OCR_DPI=200
PDF_OCR_MIN_TEXT_CHARS=40
PDF_OCR_MIN_CONFIDENCE=0.5
```

- `auto` preserves native text on normal pages; it runs OCR when fewer than 40
  letters/digits remain, over 5% of visible characters look corrupt, or native
  extraction raises an error. Chinese characters count as letters.
- `always` renders and recognizes every page. Use it for bad hidden text layers
  or image text on otherwise text-heavy pages that the heuristic cannot detect.
- `off` extracts native text only and does not require the OCR extra.

OCR replaces the selected page's native text instead of appending duplicate text.
In auto mode, an empty OCR result retains any native text; entirely empty pages
are omitted without renumbering subsequent pages. Every chunk retains its physical
zero-based `page`, `source`, `total_pages`, and `extraction_method` metadata.
Recognition below the confidence threshold is discarded. Missing models or OCR
inference failures fail indexing and are logged, rather than silently dropping a
scanned page. Documents with no remaining text are rejected.

Rendering uses PDFium (no external Poppler installation), at 200 DPI by default,
with a 16-megapixel page cap. Document and text-line orientation correction are
enabled; document unwarping is disabled. oneDNN acceleration is disabled to
avoid the Paddle 3.3 CPU/PIR incompatibility observed on Windows. General OCR does not reconstruct table
structure or guarantee correct reading order for complex multi-column layouts.
Actual accuracy depends on the scan; no accuracy gain is claimed without evaluation.

Restart the backend after configuration changes, then upload the PDF again to
create an OCR-aware index and a new session. Extraction settings participate in
index fingerprints, so an old text-only index is not reused for new OCR settings.
Existing sessions continue using their original index. The retrieval benchmark
explicitly keeps OCR off as its native-text baseline and preserves physical page IDs.

Set `PADDLE_PDX_CACHE_HOME` in the server environment to choose the Paddle model
cache location. Models cached there can be reused on later uploads. For GPU use,
install a compatible PaddlePaddle GPU runtime separately and set
`PDF_OCR_DEVICE=gpu:0`; the provided extra installs the CPU runtime.

References: [PaddleOCR pipeline](https://paddlepaddle.github.io/PaddleOCR/main/en/version3.x/pipeline_usage/OCR.html),
[PaddlePaddle installation](https://www.paddlepaddle.org.cn/documentation/docs/zh/install/pip/windows-pip_en.html).
