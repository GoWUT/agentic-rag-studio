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


def _boolean(name: str, default: bool) -> bool:
    value = os.getenv(name, str(default)).strip().lower()
    if value not in {"true", "false", "1", "0"}:
        raise ValueError(f"{name} must be true or false")
    return value in {"true", "1"}


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
        "HYBRID_RETRIEVAL_ENABLED": _boolean("HYBRID_RETRIEVAL_ENABLED", True),
        "RETRIEVAL_TOP_K": _positive_int("RETRIEVAL_TOP_K", 5),
        "RETRIEVAL_CANDIDATE_K": _positive_int("RETRIEVAL_CANDIDATE_K", 20),
        "RERANKER_ENABLED": _boolean("RERANKER_ENABLED", True),
        "RERANKER_MODEL": os.getenv("RERANKER_MODEL", "BAAI/bge-reranker-base"),
        "RERANKER_DEVICE": os.getenv("RERANKER_DEVICE", "cpu"),
        "RERANKER_BATCH_SIZE": _positive_int("RERANKER_BATCH_SIZE", 8),
        "RERANKER_FALLBACK": _boolean("RERANKER_FALLBACK", True),
        "PDF_OCR_MODE": os.getenv("PDF_OCR_MODE", "auto").strip().lower(),
        "PDF_OCR_LANG": os.getenv("PDF_OCR_LANG", "ch"),
        "PDF_OCR_DEVICE": os.getenv("PDF_OCR_DEVICE", "cpu"),
        "PDF_OCR_DPI": _positive_int("PDF_OCR_DPI", 200),
        "PDF_OCR_MIN_TEXT_CHARS": _positive_int("PDF_OCR_MIN_TEXT_CHARS", 40),
        "PDF_OCR_MIN_CONFIDENCE": _non_negative_float("PDF_OCR_MIN_CONFIDENCE", 0.5),
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

    if config["RETRIEVAL_CANDIDATE_K"] < config["RETRIEVAL_TOP_K"]:
        raise ValueError("RETRIEVAL_CANDIDATE_K must be >= RETRIEVAL_TOP_K")

    from server.rag.ocr import OCRSettings

    OCRSettings.from_config(config)
    return config


CONFIG = load_config()
