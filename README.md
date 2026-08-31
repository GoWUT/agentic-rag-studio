# Agentic RAG Studio

Agentic RAG Studio is a full-stack application for chatting with PDF documents. A LangGraph agent chooses between the uploaded knowledge base, Google search, and arXiv, while FastAPI and Streamlit provide the API and user interface.

## Features

- Persistent PDF indexes and chat sessions across restarts
- Agentic routing between PDF retrieval, Serper web search, and arXiv
- Context budgeting, history compaction, and safe tool-message handling
- Execution limits, timeouts, retries, and per-session concurrency control
- Validated PDF ingestion with isolated index builds and atomic publishing
- LangSmith tracing, offline evaluation utilities, and automated tests

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
uv sync
```

Alternatively:

```bash
python -m venv .venv
pip install -e .
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
uv run uvicorn server.main:app --reload --port 8001
```

### 4. Start the client

```bash
uv run streamlit run client/app.py --server.port 8501
```

Open `http://127.0.0.1:8501`, upload a text-based PDF, and start chatting.

## How It Works

1. The backend validates the uploaded PDF and creates or reuses a persistent Chroma index.
2. A session-specific LangGraph agent is restored or created.
3. For each question, the agent decides whether to search the PDF, the web, or arXiv.
4. Messages and index metadata are stored locally so sessions survive application restarts.

## Tests

```bash
uv run python -m unittest discover -v
```

## Acknowledgements

This project is based on [IbraahimLab/Agentic-RAG-with-FastAPI-and-Streamlit](https://github.com/IbraahimLab/Agentic-RAG-with-FastAPI-and-Streamlit) and extends it with persistence, recovery, safer ingestion, context and execution controls, evaluation tooling, and broader test coverage.
