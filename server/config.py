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


def _non_negative_int(name: str, default: int) -> int:
    raw_value = os.getenv(name, str(default))
    try:
        value = int(raw_value)
    except ValueError as error:
        raise ValueError(f"{name} must be an integer") from error
    if value < 0:
        raise ValueError(f"{name} cannot be negative")
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
            35,
        ),
        "AGENT_MAX_RETRIEVAL_RETRIES": _non_negative_int(
            "AGENT_MAX_RETRIEVAL_RETRIES",
            1,
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
        "MODEL_TRUST_ENV": _boolean("MODEL_TRUST_ENV", True),
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
        "WORKSPACE_MAX_DOCUMENTS": _positive_int("WORKSPACE_MAX_DOCUMENTS", 20),
        "WORKSPACE_RETRIEVAL_PER_DOC_K": _positive_int("WORKSPACE_RETRIEVAL_PER_DOC_K", 8),
        "WORKSPACE_RETRIEVAL_GLOBAL_K": _positive_int("WORKSPACE_RETRIEVAL_GLOBAL_K", 5),
        "WORKSPACE_RETRIEVAL_CONCURRENCY": _positive_int("WORKSPACE_RETRIEVAL_CONCURRENCY", 4),
        "WORKSPACE_RETRIEVAL_TIMEOUT_SECONDS": _positive_float("WORKSPACE_RETRIEVAL_TIMEOUT_SECONDS", 30),
        "PLANNER_ENABLED": _boolean("PLANNER_ENABLED", True),
        "PLANNER_MAX_STEPS": _positive_int("PLANNER_MAX_STEPS", 6),
        "PLANNER_MAX_ITERATIONS": _positive_int("PLANNER_MAX_ITERATIONS", 8),
        "PLANNER_MAX_REPLAN": _non_negative_int("PLANNER_MAX_REPLAN", 1),
        "SELF_CORRECT_RAG_ENABLED": _boolean("SELF_CORRECT_RAG_ENABLED", True),
        "RETRIEVAL_REWRITE_MAX": _non_negative_int("RETRIEVAL_REWRITE_MAX", 2),
        "GROUNDING_CHECK_ENABLED": _boolean("GROUNDING_CHECK_ENABLED", True),
        "ANSWER_REVISION_MAX": _non_negative_int("ANSWER_REVISION_MAX", 1),
        "FRONTEND_URL": os.getenv(
            "FRONTEND_URL",
            "http://127.0.0.1:8501",
        ),
    }

    for name, default in {"MEMORY_ENABLED": True, "MEMORY_AUTO_EXTRACT": True,
                          "DATA_ANALYSIS_ENABLED": True, "TASK_SYSTEM_ENABLED": True}.items():
        config[name] = _boolean(name, default)
    for name, default in {"MEMORY_MAX_ITEMS": 6, "MEMORY_TOKEN_BUDGET": 1200,
                          "DATASET_MAX_FILE_MB": 50, "ANALYSIS_TIMEOUT_SECONDS": 30,
                          "ANALYSIS_MAX_CODE_CHARS": 12000, "ANALYSIS_MAX_OUTPUT_CHARS": 20000,
                          "ANALYSIS_MAX_ARTIFACT_MB": 20, "ANALYSIS_MAX_ARTIFACTS": 10,
                          "TASK_MAX_STEPS": 10, "TASK_MAX_STEP_ATTEMPTS": 2}.items():
        config[name] = _positive_int(name, default)
    config["ANALYSIS_CODE_REPAIR_MAX"] = min(1, _non_negative_int("ANALYSIS_CODE_REPAIR_MAX", 1))
    for name, default in {"MEMORY_MIN_CONFIDENCE": .75, "MEMORY_MIN_IMPORTANCE": .5,
                          "MEMORY_DEDUP_SIMILARITY": .94}.items():
        value = _non_negative_float(name, default)
        if value > 1:
            raise ValueError(name + " must be between 0 and 1")
        config[name] = value

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

    for name, default in {'TOOL_REGISTRY_ENABLED': True, 'MCP_ENABLED': True,
        'TOOL_APPROVAL_ENABLED': True, 'TOOL_AUTO_APPROVE_READ': True,
        'TOOL_APPROVE_WRITES': True, 'TOOL_APPROVE_DELETES': True,
        'GITHUB_MCP_ENABLED': False, 'GITHUB_MCP_READ_ONLY': True, 'GITHUB_MCP_LOCKDOWN': True}.items():
        config[name] = _boolean(name, default)
    for name, default in {'MCP_CONNECT_TIMEOUT_SECONDS': 10, 'MCP_CALL_TIMEOUT_SECONDS': 30}.items():
        config[name] = _positive_float(name, default)
    for name, default in {'GITHUB_ALLOWED_REPOSITORIES': '', 'MCP_GITHUB_TOOL_ALLOWLIST': '',
        'MCP_GITHUB_TOOLSETS': 'context,repos,issues,pull_requests', 'MCP_SERVERS_FILE': ''}.items():
        config[name] = os.getenv(name, default).strip()
    if set(config['MCP_GITHUB_TOOLSETS'].split(',')) - {'context','repos','issues','pull_requests'}:
        raise ValueError('Only Phase 4 minimal GitHub toolsets are supported')

    for name, default in {'MULTI_AGENT_ENABLED': True, 'MULTI_AGENT_REVIEW_ENABLED': True,
                          'MULTI_AGENT_REVIEW_REQUIRED': False}.items():
        config[name] = _boolean(name, default)
    for name, default in {'MULTI_AGENT_MAX_DELEGATIONS': 8, 'MULTI_AGENT_MAX_PARALLEL': 3,
        'MULTI_AGENT_MAX_DEPTH': 1, 'MULTI_AGENT_CONTEXT_TOKEN_BUDGET': 8000,
        'MULTI_AGENT_SUPERVISOR_MAX_LLM_CALLS': 10,
        'RESEARCH_AGENT_MAX_ITERATIONS': 6, 'RESEARCH_AGENT_MAX_TOOL_CALLS': 8,
        'DATA_AGENT_MAX_ITERATIONS': 4, 'DATA_AGENT_MAX_TOOL_CALLS': 4,
        'CODING_AGENT_MAX_ITERATIONS': 5, 'CODING_AGENT_MAX_TOOL_CALLS': 6,
        'REVIEWER_AGENT_MAX_ITERATIONS': 2}.items():
        config[name] = _positive_int(name, default)
    config['MULTI_AGENT_MAX_DEPTH'] = min(1, config['MULTI_AGENT_MAX_DEPTH'])
    config['MULTI_AGENT_REVIEW_MAX_ROUNDS'] = min(1, _non_negative_int('MULTI_AGENT_REVIEW_MAX_ROUNDS', 1))
    config['MULTI_AGENT_REDELEGATION_MAX'] = min(2, _non_negative_int('MULTI_AGENT_REDELEGATION_MAX', 2))
    config['REVIEWER_AGENT_MAX_TOOL_CALLS'] = 0
    config['MULTI_AGENT_AGENT_TIMEOUT_SECONDS'] = _positive_float('MULTI_AGENT_AGENT_TIMEOUT_SECONDS', 90)
    for name, default in {'MULTI_AGENT_EFFICIENCY_ENABLED': True, 'MULTI_AGENT_COST_AWARE_ROUTING': True,
        'MULTI_AGENT_DELEGATION_CACHE': True, 'MULTI_AGENT_DUPLICATE_DETECTION': True}.items():
        config[name] = _boolean(name, default)
    config['MULTI_AGENT_REVIEW_POLICY'] = os.getenv('MULTI_AGENT_REVIEW_POLICY', 'risk_based').strip()
    if config['MULTI_AGENT_REVIEW_POLICY'] not in {'always','multi_agent_only','risk_based','disabled'}:
        raise ValueError('Invalid MULTI_AGENT_REVIEW_POLICY')
    config['MULTI_AGENT_MAX_LLM_CALLS'] = _positive_int('MULTI_AGENT_MAX_LLM_CALLS', 24)
    config['MULTI_AGENT_MAX_TOOL_CALLS'] = _positive_int('MULTI_AGENT_MAX_TOOL_CALLS', 12)
    config['MULTI_AGENT_MAX_WALL_TIME_SECONDS'] = _positive_float('MULTI_AGENT_MAX_WALL_TIME_SECONDS', 300)
    config['MULTI_AGENT_MAX_TOTAL_TOKENS'] = _positive_int('MULTI_AGENT_MAX_TOTAL_TOKENS', 1) if os.getenv('MULTI_AGENT_MAX_TOTAL_TOKENS','').strip() else None
    config['MULTI_AGENT_SOFT_BUDGET_RATIO'] = _positive_float('MULTI_AGENT_SOFT_BUDGET_RATIO', .8)
    if config['MULTI_AGENT_SOFT_BUDGET_RATIO'] > 1:
        raise ValueError('Soft budget ratio must be <=1')

    from server.rag.ocr import OCRSettings

    config['DATABASE_BACKEND'] = os.getenv('DATABASE_BACKEND', 'sqlite').strip().lower()
    config['DATABASE_URL'] = os.getenv('DATABASE_URL', '').strip()
    config['TASK_EXECUTION_MODE'] = os.getenv('TASK_EXECUTION_MODE', 'inline').strip().lower()
    for name, default in {'DB_POOL_SIZE': 5, 'DB_POOL_TIMEOUT': 30, 'DB_POOL_RECYCLE': 1800,
                          'WORKER_CONCURRENCY': 2, 'WORKER_GRACEFUL_SHUTDOWN_SECONDS': 120}.items():
        config[name] = _positive_int(name, default)
    for name, default in {'DB_MAX_OVERFLOW': 5, 'WORKER_MAX_RETRIES': 2}.items():
        config[name] = _non_negative_int(name, default)
    for name, default in {'DB_POOL_PRE_PING': True, 'TASK_QUEUE_ENABLED': False,
                          'WORKER_STALLED_JOB_RECOVERY': True}.items():
        config[name] = _boolean(name, default)
    config['WORKER_QUEUE'] = os.getenv('WORKER_QUEUE', 'agent').strip()
    if config['DATABASE_BACKEND'] not in {'sqlite', 'postgresql'}:
        raise ValueError('Invalid DATABASE_BACKEND')
    if config['TASK_EXECUTION_MODE'] not in {'inline', 'worker'}:
        raise ValueError('Invalid TASK_EXECUTION_MODE')
    if config['DATABASE_BACKEND'] == 'postgresql' and not config['DATABASE_URL']:
        raise ValueError('DATABASE_URL is required for PostgreSQL')
    if config['TASK_EXECUTION_MODE'] == 'worker' and (config['DATABASE_BACKEND'] != 'postgresql' or not config['TASK_QUEUE_ENABLED']):
        raise ValueError('Worker mode requires PostgreSQL and TASK_QUEUE_ENABLED=true')

    from server.auth.config import load_security_config
    load_security_config(config, _boolean, _positive_int)
    OCRSettings.from_config(config)
    return config


CONFIG = load_config()
