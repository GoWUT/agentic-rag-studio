import os

from dotenv import load_dotenv


def _positive_int(name: str, default: int) -> int:
    raw_value = os.getenv(name, str(default))
    try:
        value = int(raw_value)
    except ValueError as error:
        raise ValueError(f"{name} must be an integer") from error
    if value <= 0:
        raise ValueError(f"{name} must be positive")
    return value


def _positive_float(name: str, default: float) -> float:
    raw_value = os.getenv(name, str(default))
    try:
        value = float(raw_value)
    except ValueError as error:
        raise ValueError(f"{name} must be a number") from error
    if value <= 0:
        raise ValueError(f"{name} must be positive")
    return value


def _non_negative_float(name: str, default: float) -> float:
    raw_value = os.getenv(name, str(default))
    try:
        value = float(raw_value)
    except ValueError as error:
        raise ValueError(f"{name} must be a number") from error
    if value < 0:
        raise ValueError(f"{name} cannot be negative")
    return value


def load_config():
    load_dotenv()

    config = {
        "DEEPSEEK_API_KEY": os.getenv("DEEPSEEK_API_KEY"),
        "SERPER_API_KEY": os.getenv("SERPER_API_KEY"),
        "MODEL_NAME": os.getenv(
            "MODEL_NAME",
            "deepseek-v4-flash"
        ),
        "MODEL_CONTEXT_WINDOW_TOKENS": _positive_int(
            "MODEL_CONTEXT_WINDOW_TOKENS",
            1_000_000,
        ),
        "CONTEXT_INPUT_BUDGET_TOKENS": _positive_int(
            "CONTEXT_INPUT_BUDGET_TOKENS",
            60_000,
        ),
        "MAX_OUTPUT_TOKENS": _positive_int(
            "MAX_OUTPUT_TOKENS",
            4_096,
        ),
        "CONTEXT_SAFETY_TOKENS": _positive_int(
            "CONTEXT_SAFETY_TOKENS",
            4_096,
        ),
        "CONTEXT_SUMMARY_TOKENS": _positive_int(
            "CONTEXT_SUMMARY_TOKENS",
            2_048,
        ),
        "CONTEXT_RECENT_TURNS": _positive_int(
            "CONTEXT_RECENT_TURNS",
            4,
        ),
        "AGENT_MAX_GRAPH_STEPS": _positive_int(
            "AGENT_MAX_GRAPH_STEPS",
            20,
        ),
        "AGENT_MAX_ATTEMPTS": _positive_int(
            "AGENT_MAX_ATTEMPTS",
            2,
        ),
        "AGENT_RETRY_BASE_SECONDS": _non_negative_float(
            "AGENT_RETRY_BASE_SECONDS",
            0.5,
        ),
        "AGENT_EXECUTION_TIMEOUT_SECONDS": _positive_float(
            "AGENT_EXECUTION_TIMEOUT_SECONDS",
            120,
        ),
        "MODEL_REQUEST_TIMEOUT_SECONDS": _positive_float(
            "MODEL_REQUEST_TIMEOUT_SECONDS",
            60,
        ),
        "EMBEDDING_MODEL": os.getenv(
            "EMBEDDING_MODEL",
            "BAAI/bge-small-zh-v1.5",
        ),
        "MAX_PDF_UPLOAD_BYTES": _positive_int(
            "MAX_PDF_UPLOAD_BYTES",
            25 * 1024 * 1024,
        ),
        "WORKSPACE_DIR": os.getenv("WORKSPACE_DIR", ".rag_workspace"),
        "FRONTEND_URL": os.getenv(
            "FRONTEND_URL",
            "http://127.0.0.1:8501",
        ),
    }

    if (
        config["MODEL_REQUEST_TIMEOUT_SECONDS"]
        >= config["AGENT_EXECUTION_TIMEOUT_SECONDS"]
    ):
        raise ValueError(
            "MODEL_REQUEST_TIMEOUT_SECONDS must be less than "
            "AGENT_EXECUTION_TIMEOUT_SECONDS"
        )

    return config


CONFIG = load_config()
