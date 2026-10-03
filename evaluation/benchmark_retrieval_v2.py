"""Run a reproducible four-way ablation of the production PDF retriever."""

from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
from importlib.metadata import PackageNotFoundError, version
from io import BytesIO
import json
import os
from pathlib import Path
import platform
import statistics
import subprocess
import time
import unicodedata
import re

from langchain_core.documents import Document

from evaluation.retrieval_eval.metrics import evaluate_ranking
from evaluation.retrieval_eval.reporting import write_outputs
from evaluation.retrieval_eval.statistics import bootstrap_mean_ci, percentile
from evaluation.retrieval_eval.strategies import (
    BM25Strategy,
    DenseStrategy,
    HybridRerankStrategy,
    HybridRRFStrategy,
    RetrievalStrategy,
    synchronize_device,
)
from server.config import CONFIG
from server.rag.ingestion import ChromaIndexAdapter, DocumentIngestionPipeline
from server.rag.loaders import load_pdf
from server.rag.ocr import OCRSettings
from server.rag.retrieval import BM25Index, CrossEncoderReranker


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATASET = Path(__file__).parent / "datasets" / "retrieval_v2_gcnet.jsonl"
DEFAULT_OUTPUT = Path(__file__).parent / "results" / "retrieval_v2_ablation.json"
DEFAULT_WORKSPACE = PROJECT_ROOT / ".runtime" / "retrieval-v2"
ALLOWED_CATEGORIES = {"lexical", "semantic", "mixed", "numeric"}
RRF_CONSTANT = 60
METHOD_ORDER = ("dense", "bm25", "hybrid_rrf", "hybrid_reranker")


@dataclass(frozen=True)
class RetrievalCase:
    id: str
    query: str
    anchor: str
    category: str | None = None


def load_cases(path: Path) -> list[RetrievalCase]:
    cases: list[RetrievalCase] = []
    with path.open("r", encoding="utf-8") as source:
        for line_number, line in enumerate(source, start=1):
            if not line.strip():
                continue
            payload = json.loads(line)
            try:
                case = RetrievalCase(**payload)
            except TypeError as error:
                raise ValueError(
                    f"Invalid retrieval case at line {line_number}"
                ) from error
            if not case.id.strip() or not case.query.strip() or not case.anchor.strip():
                raise ValueError(f"Empty field in retrieval case {case.id!r}")
            if case.category is not None and case.category not in ALLOWED_CATEGORIES:
                raise ValueError(
                    f"Unsupported category {case.category!r} in case {case.id!r}"
                )
            cases.append(case)
    if not cases:
        raise ValueError("Retrieval V2 dataset is empty")
    if len({case.id for case in cases}) != len(cases):
        raise ValueError("Retrieval V2 case IDs must be unique")
    return cases


def normalize_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return re.sub(r"\s+", "", normalized)


def locate_gold_pages(
    documents: list[Document],
    cases: list[RetrievalCase],
) -> tuple[dict[str, list[int]], list[dict[str, str]]]:
    normalized_pages = [normalize_text(document.page_content) for document in documents]
    gold_pages: dict[str, list[int]] = {}
    failures: list[dict[str, str]] = []
    for case in cases:
        needle = normalize_text(case.anchor)
        pages = [
            int(documents[index].metadata.get("page", index))
            for index, text in enumerate(normalized_pages)
            if needle in text
        ]
        if pages:
            gold_pages[case.id] = sorted(set(pages))
        else:
            failures.append({"id": case.id, "anchor": case.anchor})
    return gold_pages, failures


def dataset_audit(
    path: Path,
    cases: list[RetrievalCase],
    gold_pages: dict[str, list[int]],
    failures: list[dict[str, str]],
) -> dict:
    query_counts = Counter(normalize_text(case.query) for case in cases)
    anchor_counts = Counter(normalize_text(case.anchor) for case in cases)
    categories = Counter(case.category or "uncategorized" for case in cases)
    page_distribution = Counter(
        page + 1 for pages in gold_pages.values() for page in pages
    )
    return {
        "path": _display_path(path),
        "sha256": _sha256(path),
        "query_count": len(cases),
        "gold_label_method": "human-selected answer anchor located in native PDF text",
        "category_distribution": dict(sorted(categories.items())),
        "gold_page_distribution": {
            str(page): count for page, count in sorted(page_distribution.items())
        },
        "duplicate_queries": sorted(
            value for value, count in query_counts.items() if count > 1
        ),
        "duplicate_anchors": sorted(
            value for value, count in anchor_counts.items() if count > 1
        ),
        "anchor_location_failures": failures,
    }


def _document_pages(documents: list[Document]) -> list[int]:
    return [int(document.metadata["page"]) for document in documents]


def _document_identity(document: Document) -> tuple[str, int]:
    return (
        document.page_content,
        int(document.metadata.get("page", -1)),
    )


def evaluate_strategy(
    strategy: RetrievalStrategy,
    cases: list[RetrievalCase],
    gold_pages: dict[str, list[int]],
    *,
    repeats: int,
    bootstrap_samples: int,
    seed: int,
) -> tuple[dict, dict[str, dict], dict[str, dict[str, list[int]]]]:
    all_latencies: list[float] = []
    all_stage_latencies: dict[str, list[float]] = {}
    case_results: dict[str, dict] = {}
    candidate_pages: dict[str, dict[str, list[int]]] = {}

    for case in cases:
        first = None
        first_identity = None
        case_latencies: list[float] = []
        stable = True
        for _ in range(repeats):
            result = strategy.retrieve(case.query)
            identity = tuple(_document_identity(doc) for doc in result.documents)
            if first is None:
                first = result
                first_identity = identity
            elif identity != first_identity:
                stable = False
            total_ms = result.stage_ms["total_ms"]
            case_latencies.append(total_ms)
            all_latencies.append(total_ms)
            for stage, value in result.stage_ms.items():
                all_stage_latencies.setdefault(stage, []).append(value)

        if first is None:
            raise RuntimeError(f"Strategy {strategy.name} returned no measurement")
        pages = _document_pages(first.documents)
        gold = set(gold_pages[case.id])
        metrics = evaluate_ranking(pages, gold)
        case_results[case.id] = {
            "retrieved_pages_1_based": [page + 1 for page in pages],
            **metrics,
            "latency_ms": [round(value, 4) for value in case_latencies],
            "latency_ms_median": round(statistics.median(case_latencies), 4),
            "ranking_stable": stable,
        }
        candidate_pages[case.id] = {
            "dense": [page + 1 for page in _document_pages(first.dense_candidates)],
            "hybrid": [page + 1 for page in _document_pages(first.fused_candidates)],
        }

    quality_keys = (
        "hit_at_1",
        "hit_at_3",
        "hit_at_5",
        "recall_at_5",
        "mrr_at_5",
        "ndcg_at_5",
    )
    method = {
        key: statistics.fmean(float(case_results[case.id][key]) for case in cases)
        for key in quality_keys
    }
    method.update({
        "latency_ms_p50": percentile(all_latencies, 0.50),
        "latency_ms_p95": percentile(all_latencies, 0.95),
        "latency_ms_mean": statistics.fmean(all_latencies),
        "latency_ms_min": min(all_latencies),
        "latency_ms_max": max(all_latencies),
        "ranking_stable": all(
            case_results[case.id]["ranking_stable"] for case in cases
        ),
        "stage_latency_ms": {
            f"{stage}_p50": percentile(values, 0.50)
            for stage, values in all_stage_latencies.items()
        },
        "confidence_intervals_95": {
            key: bootstrap_mean_ci(
                [float(case_results[case.id][key]) for case in cases],
                samples=bootstrap_samples,
                seed=seed,
            )
            for key in ("hit_at_5", "mrr_at_5", "ndcg_at_5")
        },
    })
    return method, case_results, candidate_pages


def run_benchmark(args: argparse.Namespace) -> dict:
    pdf_path = args.pdf.resolve()
    dataset_path = args.dataset.resolve()
    output_path = args.output.resolve()
    workspace = args.workspace.resolve()
    if not pdf_path.is_file():
        raise FileNotFoundError(pdf_path)
    if not dataset_path.is_file():
        raise FileNotFoundError(dataset_path)

    cases = load_cases(dataset_path)
    ocr_settings = OCRSettings.from_config(CONFIG)
    pages = load_pdf(str(pdf_path), ocr_settings=ocr_settings)
    gold_pages, anchor_failures = locate_gold_pages(pages, cases)
    audit = dataset_audit(
        dataset_path,
        cases,
        gold_pages,
        anchor_failures,
    )
    if anchor_failures:
        failed_ids = ", ".join(item["id"] for item in anchor_failures)
        raise ValueError(f"Could not locate gold anchors for: {failed_ids}")

    workspace.mkdir(parents=True, exist_ok=True)
    pipeline = DocumentIngestionPipeline(
        workspace=workspace,
        max_upload_bytes=max(pdf_path.stat().st_size + 1, 25 * 1024 * 1024),
        index_adapter=ChromaIndexAdapter(
            args.embedding_model,
            chunk_size=args.chunk_size,
            chunk_overlap=args.chunk_overlap,
            ocr_settings=ocr_settings,
        ),
    )
    index_started = time.perf_counter()
    ingestion = pipeline.ingest(
        file_name=pdf_path.name,
        stream=BytesIO(pdf_path.read_bytes()),
    )
    index_load_or_build_ms = (time.perf_counter() - index_started) * 1_000
    vectorstore = ingestion.vectorstore
    records = vectorstore.get(include=["documents", "metadatas"])
    corpus_documents = [
        Document(page_content=text, metadata=metadata or {})
        for text, metadata in zip(records["documents"], records["metadatas"])
        if text and text.strip()
    ]

    bm25_started = time.perf_counter()
    bm25 = BM25Index(corpus_documents)
    bm25_build_ms = (time.perf_counter() - bm25_started) * 1_000
    dense = DenseStrategy(vectorstore, top_k=args.top_k)
    lexical = BM25Strategy(bm25, top_k=args.top_k)
    hybrid = HybridRRFStrategy(
        vectorstore,
        bm25,
        candidate_k=args.candidate_k,
        top_k=args.top_k,
    )

    reranker = CrossEncoderReranker(
        args.reranker_model,
        device=args.reranker_device,
        batch_size=args.reranker_batch_size,
    )
    cold_candidates = hybrid.retrieve(cases[0].query).fused_candidates
    synchronize_device(args.reranker_device)
    reranker_cold_started = time.perf_counter()
    reranker.rerank(cases[0].query, cold_candidates)
    synchronize_device(args.reranker_device)
    reranker_cold_start_ms = (
        time.perf_counter() - reranker_cold_started
    ) * 1_000
    hybrid_reranker = HybridRerankStrategy(
        vectorstore,
        bm25,
        reranker,
        candidate_k=args.candidate_k,
        top_k=args.top_k,
        device=args.reranker_device,
    )
    strategies: list[RetrievalStrategy] = [
        dense,
        lexical,
        hybrid,
        hybrid_reranker,
    ]

    warmup_cases = cases[:3]
    for strategy in strategies:
        for case in warmup_cases:
            strategy.retrieve(case.query)

    methods: dict[str, dict] = {}
    per_method_cases: dict[str, dict[str, dict]] = {}
    candidate_results: dict[str, dict[str, dict[str, list[int]]]] = {}
    for strategy in strategies:
        method, details, candidate_pages = evaluate_strategy(
            strategy,
            cases,
            gold_pages,
            repeats=args.repeats,
            bootstrap_samples=args.bootstrap_samples,
            seed=args.seed,
        )
        methods[strategy.name] = _rounded(method)
        per_method_cases[strategy.name] = details
        candidate_results[strategy.name] = candidate_pages

    case_rows = []
    for case in cases:
        case_rows.append({
            **asdict(case),
            "gold_pages_1_based": [page + 1 for page in gold_pages[case.id]],
            "methods": {
                name: _rounded(per_method_cases[name][case.id])
                for name in METHOD_ORDER
            },
        })

    candidate_dense_hits = []
    candidate_hybrid_hits = []
    hybrid_candidates = candidate_results["hybrid_rrf"]
    for case in cases:
        gold_one_based = {page + 1 for page in gold_pages[case.id]}
        candidate_dense_hits.append(
            float(bool(set(hybrid_candidates[case.id]["dense"]) & gold_one_based))
        )
        candidate_hybrid_hits.append(
            float(bool(set(hybrid_candidates[case.id]["hybrid"]) & gold_one_based))
        )

    result = {
        "run_id": datetime.now(timezone.utc).strftime("retrieval-v2-%Y%m%dT%H%M%SZ"),
        "environment": _environment_metadata(args.reranker_device),
        "corpus": {
            "pdf_path": _display_path(pdf_path),
            "pdf_size_bytes": pdf_path.stat().st_size,
            "pdf_sha256": _sha256(pdf_path),
            "pages": len(pages),
            "chunks": len(corpus_documents),
            "index_path": _display_path(ingestion.index_dir),
            "index_reused": ingestion.reused,
        },
        "dataset": audit,
        "config": {
            "embedding_model": args.embedding_model,
            "reranker_model": args.reranker_model,
            "reranker_device": args.reranker_device,
            "reranker_batch_size": args.reranker_batch_size,
            "reranker_fallback": False,
            "ocr": asdict(ocr_settings),
            "chunk_size": args.chunk_size,
            "chunk_overlap": args.chunk_overlap,
            "top_k": args.top_k,
            "candidate_k": args.candidate_k,
            "rrf_constant": RRF_CONSTANT,
            "repeats": args.repeats,
            "warmup_queries_per_method": len(warmup_cases),
            "bootstrap_samples": args.bootstrap_samples,
            "bootstrap_seed": args.seed,
        },
        "initialization": {
            "index_load_or_build_ms": round(index_load_or_build_ms, 4),
            "index_reused": ingestion.reused,
            "bm25_index_build_ms": round(bm25_build_ms, 4),
            "reranker_cold_start_ms": round(reranker_cold_start_ms, 4),
            "reranker_warmup_queries": len(warmup_cases),
        },
        "methods": methods,
        "candidate_recall": {
            "candidate_k": args.candidate_k,
            "dense_candidate_hit_at_20": statistics.fmean(candidate_dense_hits),
            "hybrid_candidate_hit_at_20": statistics.fmean(candidate_hybrid_hits),
        },
        "comparison": _comparisons(methods),
        "per_category": _per_category(cases, per_method_cases),
        "failed_cases": _failed_cases(cases, per_method_cases),
        "cases": case_rows,
    }
    result = _rounded(result)
    paths = write_outputs(result, output_path)
    result["outputs"] = {key: str(path) for key, path in paths.items()}
    return result


def _comparisons(methods: dict[str, dict]) -> dict[str, dict[str, float]]:
    pairs = {
        "hybrid_vs_dense": ("hybrid_rrf", "dense"),
        "hybrid_reranker_vs_hybrid": ("hybrid_reranker", "hybrid_rrf"),
        "hybrid_reranker_vs_dense": ("hybrid_reranker", "dense"),
    }
    return {
        label: {
            "hit_at_5_delta": methods[right]["hit_at_5"] - methods[left]["hit_at_5"],
            "mrr_at_5_delta": methods[right]["mrr_at_5"] - methods[left]["mrr_at_5"],
            "ndcg_at_5_delta": methods[right]["ndcg_at_5"] - methods[left]["ndcg_at_5"],
            "p95_latency_delta_ms": (
                methods[right]["latency_ms_p95"]
                - methods[left]["latency_ms_p95"]
            ),
        }
        for label, (right, left) in pairs.items()
    }


def _per_category(
    cases: list[RetrievalCase],
    per_method: dict[str, dict[str, dict]],
) -> dict[str, dict[str, dict[str, float]]]:
    categories = sorted({case.category for case in cases if case.category})
    result = {}
    for category in categories:
        members = [case for case in cases if case.category == category]
        result[category] = {
            method: {
                "query_count": len(members),
                "hit_at_5": statistics.fmean(
                    per_method[method][case.id]["hit_at_5"] for case in members
                ),
                "mrr_at_5": statistics.fmean(
                    per_method[method][case.id]["mrr_at_5"] for case in members
                ),
                "ndcg_at_5": statistics.fmean(
                    per_method[method][case.id]["ndcg_at_5"] for case in members
                ),
            }
            for method in METHOD_ORDER
        }
    return result


def _failed_cases(
    cases: list[RetrievalCase],
    per_method: dict[str, dict[str, dict]],
) -> dict[str, list[dict[str, str]]]:
    rules = {
        "dense_miss_hybrid_success": ("dense", 0.0, "hybrid_rrf", 1.0),
        "hybrid_miss_reranker_success": (
            "hybrid_rrf", 0.0, "hybrid_reranker", 1.0,
        ),
        "dense_success_hybrid_fail": ("dense", 1.0, "hybrid_rrf", 0.0),
        "hybrid_success_reranker_fail": (
            "hybrid_rrf", 1.0, "hybrid_reranker", 0.0,
        ),
    }
    return {
        label: [
            {"id": case.id, "query": case.query, "analysis": "requires manual analysis"}
            for case in cases
            if per_method[first][case.id]["hit_at_5"] == first_value
            and per_method[second][case.id]["hit_at_5"] == second_value
        ]
        for label, (first, first_value, second, second_value) in rules.items()
    }


def _environment_metadata(device: str) -> dict:
    import torch

    cuda_available = torch.cuda.is_available()
    return {
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "git_commit": _git_commit(),
        "git_worktree_dirty": _git_dirty(),
        "python_version": platform.python_version(),
        "platform": platform.platform(),
        "cpu": os.getenv("PROCESSOR_IDENTIFIER") or platform.processor() or "unknown",
        "cuda_available": cuda_available,
        "gpu": torch.cuda.get_device_name(0) if cuda_available else "none",
        "torch_version": _package_version("torch"),
        "sentence_transformers_version": _package_version("sentence-transformers"),
        "chromadb_version": _package_version("chromadb"),
        "device": device,
    }


def _package_version(name: str) -> str:
    try:
        return version(name)
    except PackageNotFoundError:
        return "unavailable"


def _git_commit() -> str:
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    return completed.stdout.strip() if completed.returncode == 0 else "unavailable"


def _git_dirty() -> bool | None:
    completed = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    return bool(completed.stdout.strip()) if completed.returncode == 0 else None


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _display_path(path: Path) -> str:
    resolved = path.resolve()
    try:
        return str(resolved.relative_to(PROJECT_ROOT))
    except ValueError:
        return str(resolved)


def _rounded(value):
    if isinstance(value, float):
        return round(value, 6)
    if isinstance(value, dict):
        return {key: _rounded(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_rounded(item) for item in value]
    return value


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pdf", type=Path, required=True)
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--workspace", type=Path, default=DEFAULT_WORKSPACE)
    parser.add_argument("--embedding-model", default=CONFIG["EMBEDDING_MODEL"])
    parser.add_argument("--reranker-model", default=CONFIG["RERANKER_MODEL"])
    parser.add_argument("--reranker-device", default=CONFIG["RERANKER_DEVICE"])
    parser.add_argument(
        "--reranker-batch-size",
        type=int,
        default=CONFIG["RERANKER_BATCH_SIZE"],
    )
    parser.add_argument("--chunk-size", type=int, default=800)
    parser.add_argument("--chunk-overlap", type=int, default=150)
    parser.add_argument("--top-k", type=int, default=CONFIG["RETRIEVAL_TOP_K"])
    parser.add_argument(
        "--candidate-k",
        type=int,
        default=CONFIG["RETRIEVAL_CANDIDATE_K"],
    )
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--bootstrap-samples", type=int, default=2_000)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()
    if args.chunk_size <= 0:
        parser.error("--chunk-size must be positive")
    if not 0 <= args.chunk_overlap < args.chunk_size:
        parser.error("--chunk-overlap must be non-negative and below chunk size")
    if args.top_k < 5:
        parser.error("--top-k must be at least 5")
    if args.candidate_k < args.top_k:
        parser.error("--candidate-k must be at least --top-k")
    if args.repeats < 5:
        parser.error("--repeats must be at least 5")
    if args.bootstrap_samples <= 0:
        parser.error("--bootstrap-samples must be positive")
    if args.reranker_batch_size <= 0:
        parser.error("--reranker-batch-size must be positive")
    return args


def main() -> None:
    args = parse_args()
    result = run_benchmark(args)
    print(json.dumps({
        "run_id": result["run_id"],
        "corpus": result["corpus"],
        "dataset": result["dataset"],
        "methods": result["methods"],
        "candidate_recall": result["candidate_recall"],
        "comparison": result["comparison"],
        "outputs": result["outputs"],
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
