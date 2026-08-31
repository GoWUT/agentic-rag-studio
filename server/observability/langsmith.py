from __future__ import annotations

import os
from typing import Optional

from dotenv import load_dotenv


def _truthy(val: Optional[str]) -> bool:
    if val is None:
        return False
    return val.strip().lower() in {"1", "true", "yes", "y", "on"}


def init_langsmith(
    *,
    project_default: str = "default",
    endpoint_default: str = "https://api.smith.langchain.com",
    enable_background_upload: bool = True,
) -> None:
    """
    Initialise LangSmith tracing for LangChain/LangGraph.

    Best practice:
    - Call this ONCE at server startup (e.g. at top of server/main.py).
    - Use environment variables (preferably via .env).
    - Keep tracing configuration out of your agent/RAG logic.

    Supports both:
    - Standard LangChain env vars: LANGCHAIN_TRACING_V2, LANGCHAIN_API_KEY, LANGCHAIN_PROJECT, LANGCHAIN_ENDPOINT
    - Your provided vars: LANGSMITH_TRACING, LANGSMITH_API_KEY, LANGSMITH_PROJECT, LANGSMITH_ENDPOINT
    """
    load_dotenv()

    # Accept either naming scheme.
    tracing_flag = (
        os.getenv("LANGCHAIN_TRACING_V2")
        or os.getenv("LANGSMITH_TRACING")
        or os.getenv("LANGCHAIN_TRACING")
    )

    # If user set LANGSMITH_TRACING="true", ensure LANGCHAIN_TRACING_V2 is set.
    if tracing_flag is not None and "LANGCHAIN_TRACING_V2" not in os.environ:
        os.environ["LANGCHAIN_TRACING_V2"] = "true" if _truthy(tracing_flag) else "false"

    # API key: prefer LANGCHAIN_API_KEY, fallback to LANGSMITH_API_KEY
    if "LANGCHAIN_API_KEY" not in os.environ:
        api_key = os.getenv("LANGSMITH_API_KEY") or os.getenv("LANGCHAIN_API_KEY")
        if api_key:
            os.environ["LANGCHAIN_API_KEY"] = api_key

    # Project name
    if "LANGCHAIN_PROJECT" not in os.environ:
        project = os.getenv("LANGSMITH_PROJECT") or os.getenv("LANGCHAIN_PROJECT") or project_default
        os.environ["LANGCHAIN_PROJECT"] = project

    # Endpoint (cloud default is api.smith.langchain.com; keep override possible)
    if "LANGCHAIN_ENDPOINT" not in os.environ:
        endpoint = os.getenv("LANGSMITH_ENDPOINT") or os.getenv("LANGCHAIN_ENDPOINT") or endpoint_default
        os.environ["LANGCHAIN_ENDPOINT"] = endpoint

    # Optional: background upload (helps responsiveness in web servers)
    if enable_background_upload and "LANGCHAIN_CALLBACKS_BACKGROUND" not in os.environ:
        os.environ["LANGCHAIN_CALLBACKS_BACKGROUND"] = "true"


def is_tracing_enabled() -> bool:
    """Utility to check whether tracing is currently enabled."""
    return _truthy(os.getenv("LANGCHAIN_TRACING_V2"))


def get_langsmith_client():
    """
    Optional helper if you want to use the LangSmith Client directly later
    (datasets, evaluations, feedback, etc.).
    """
    try:
        from langsmith import Client  # type: ignore
    except Exception as e:
        raise RuntimeError(
            "LangSmith client is not available. Install with: pip install -U langsmith"
        ) from e

    return Client()
