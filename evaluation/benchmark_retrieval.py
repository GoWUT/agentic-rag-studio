"""Reproducible end-to-end retrieval benchmark for the project.

The benchmark uses a real PDF, the configured embedding model, the real
DocumentIngestionPipeline, and a persisted Chroma index.  Relevance labels are
the PDF pages containing manually selected answer anchors.  This makes Hit@K
and MRR deterministic without requiring an LLM judge.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from io import BytesIO
import json
import math
from pathlib import Path
import platform
import re
import statistics
import time
import unicodedata
import uuid

from langchain_text_splitters import RecursiveCharacterTextSplitter

from server.rag.ingestion import (
    ChromaIndexAdapter,
    DocumentIngestionPipeline,
)
from server.rag.loaders import load_pdf


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = Path(__file__).parent / "results" / "retrieval_benchmark.json"


@dataclass(frozen=True)
class RetrievalCase:
    id: str
    query: str
    anchor: str


def load_cases(path: Path) -> list[RetrievalCase]:
    cases: list[RetrievalCase] = []
    with path.open("r", encoding="utf-8") as source:
        for line_number, line in enumerate(source, start=1):
            if not line.strip():
                continue
            payload = json.loads(line)
            try:
                cases.append(RetrievalCase(**payload))
            except TypeError as error:
                raise ValueError(
                    f"Invalid retrieval case at line {line_number}"
                ) from error
    if not cases:
        raise ValueError("Retrieval benchmark dataset is empty")
    if len({case.id for case in cases}) != len(cases):
        raise ValueError("Retrieval benchmark case IDs must be unique")
    return cases


def normalize_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).lower()
    return re.sub(r"\s+", "", normalized)


def find_gold_pages(documents, cases: list[RetrievalCase]) -> dict[str, list[int]]:
    normalized_pages = [normalize_text(document.page_content) for document in documents]
    gold_pages: dict[str, list[int]] = {}
    for case in cases:
        needle = normalize_text(case.anchor)
        pages = [
            index
            for index, page_content in enumerate(normalized_pages)
            if needle in page_content
        ]
        if not pages:
            raise ValueError(
                f"Could not locate anchor in PDF for case {case.id!r}: "
                f"{case.anchor!r}"
            )
        gold_pages[case.id] = pages
    return gold_pages


def reciprocal_rank(retrieved_pages: list[int], gold_pages: set[int]) -> float:
    for rank, page in enumerate(retrieved_pages, start=1):
        if page in gold_pages:
            return 1.0 / rank
    return 0.0


def ranking_metrics(
    rankings: list[tuple[list[int], set[int]]],
    *,
    cutoffs: tuple[int, ...] = (1, 3, 5),
) -> dict[str, float]:
    if not rankings:
        raise ValueError("At least one ranking is required")
    metrics: dict[str, float] = {}
    for cutoff in cutoffs:
        hits = sum(
            bool(set(retrieved[:cutoff]) & gold)
            for retrieved, gold in rankings
        )
        metrics[f"hit_at_{cutoff}"] = round(hits / len(rankings), 4)
    metrics["mrr_at_5"] = round(
        statistics.fmean(
            reciprocal_rank(retrieved[:5], gold)
            for retrieved, gold in rankings
        ),
        4,
    )
    return metrics


def percentile(values: list[float], percentile_value: float) -> float:
    if not values:
        raise ValueError("Cannot compute a percentile of an empty sequence")
    ordered = sorted(values)
    position = (len(ordered) - 1) * percentile_value
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def run_benchmark(args: argparse.Namespace) -> dict:
    pdf_path = args.pdf.resolve()
    dataset_path = args.dataset.resolve()
    output_path = args.output.resolve()
    if not pdf_path.is_file():
        raise FileNotFoundError(pdf_path)
    try:
        corpus_path = str(pdf_path.relative_to(PROJECT_ROOT))
    except ValueError:
        corpus_path = str(pdf_path)

    cases = load_cases(dataset_path)
    documents = load_pdf(str(pdf_path))
    gold_pages = find_gold_pages(documents, cases)
    splits = RecursiveCharacterTextSplitter(
        chunk_size=args.chunk_size,
        chunk_overlap=args.chunk_overlap,
    ).split_documents(documents)

    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    pdf_bytes = pdf_path.read_bytes()
    workspaces: list[Path] = []
    cold_samples: list[float] = []
    warm_samples: list[float] = []
    warm_result = None
    for sample_index in range(args.index_repeats):
        workspace = (
            PROJECT_ROOT
            / ".runtime"
            / "benchmarks"
            / (
                f"retrieval-{run_id}-{sample_index + 1}-"
                f"{uuid.uuid4().hex[:8]}"
            )
        )
        workspace.mkdir(parents=True, exist_ok=False)
        workspaces.append(workspace)
        pipeline = DocumentIngestionPipeline(
            workspace=workspace,
            max_upload_bytes=max(
                25 * 1024 * 1024,
                pdf_path.stat().st_size + 1,
            ),
            index_adapter=ChromaIndexAdapter(args.embedding_model),
        )

        cold_started = time.perf_counter()
        cold_result = pipeline.ingest(
            file_name=pdf_path.name,
            stream=BytesIO(pdf_bytes),
        )
        cold_samples.append(time.perf_counter() - cold_started)

        warm_started = time.perf_counter()
        warm_result = pipeline.ingest(
            file_name=pdf_path.name,
            stream=BytesIO(pdf_bytes),
        )
        warm_samples.append(time.perf_counter() - warm_started)
        if cold_result.reused or not warm_result.reused:
            raise RuntimeError("Index build/reuse contract was not observed")

    if warm_result is None:
        raise RuntimeError("Index benchmark produced no result")
    cold_seconds = statistics.median(cold_samples)
    warm_seconds = statistics.median(warm_samples)

    vectorstore = warm_result.vectorstore
    for case in cases[: min(3, len(cases))]:
        vectorstore.similarity_search(case.query, k=args.top_k)

    latencies_ms: list[float] = []
    rankings: list[tuple[list[int], set[int]]] = []
    details: list[dict] = []
    for case in cases:
        first_documents = None
        case_latencies: list[float] = []
        for _ in range(args.repeats):
            started = time.perf_counter()
            retrieved = vectorstore.similarity_search(
                case.query,
                k=args.top_k,
            )
            elapsed_ms = (time.perf_counter() - started) * 1000
            case_latencies.append(elapsed_ms)
            latencies_ms.append(elapsed_ms)
            if first_documents is None:
                first_documents = retrieved

        retrieved_pages = [
            int(document.metadata["page"])
            for document in first_documents or []
        ]
        gold = set(gold_pages[case.id])
        rankings.append((retrieved_pages, gold))
        details.append(
            {
                **asdict(case),
                "gold_pages_1_based": [page + 1 for page in sorted(gold)],
                "retrieved_pages_1_based": [page + 1 for page in retrieved_pages],
                "reciprocal_rank_at_5": round(
                    reciprocal_rank(retrieved_pages[:5], gold),
                    4,
                ),
                "latency_ms_median": round(statistics.median(case_latencies), 2),
            }
        )

    result = {
        "run_id": run_id,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "environment": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "embedding_model": args.embedding_model,
            "chunk_size": args.chunk_size,
            "chunk_overlap": args.chunk_overlap,
            "top_k": args.top_k,
            "repeats": args.repeats,
            "index_repeats": args.index_repeats,
        },
        "corpus": {
            "pdf": corpus_path,
            "size_bytes": pdf_path.stat().st_size,
            "pages": len(documents),
            "chunks": len(splits),
            "evaluation_queries": len(cases),
        },
        "indexing": {
            "cold_build_seconds": round(cold_seconds, 3),
            "content_hash_reuse_seconds": round(warm_seconds, 3),
            "reuse_speedup": round(cold_seconds / warm_seconds, 2),
            "cold_build_samples_seconds": [
                round(value, 3) for value in cold_samples
            ],
            "content_hash_reuse_samples_seconds": [
                round(value, 3) for value in warm_samples
            ],
            "index_reused": warm_result.reused,
        },
        "retrieval": {
            **ranking_metrics(rankings),
            "latency_ms_p50": round(statistics.median(latencies_ms), 2),
            "latency_ms_p95": round(percentile(latencies_ms, 0.95), 2),
        },
        "cases": details,
        "benchmark_workspaces": [
            str(workspace.relative_to(PROJECT_ROOT))
            for workspace in workspaces
        ],
    }

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pdf", type=Path, required=True)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--embedding-model",
        default="BAAI/bge-small-zh-v1.5",
    )
    parser.add_argument("--chunk-size", type=int, default=800)
    parser.add_argument("--chunk-overlap", type=int, default=150)
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--index-repeats", type=int, default=3)
    args = parser.parse_args()
    if args.chunk_size <= 0:
        parser.error("--chunk-size must be positive")
    if not 0 <= args.chunk_overlap < args.chunk_size:
        parser.error("--chunk-overlap must be non-negative and below chunk size")
    if args.top_k < 5:
        parser.error("--top-k must be at least 5 for Hit@5/MRR@5")
    if args.repeats <= 0:
        parser.error("--repeats must be positive")
    if args.index_repeats <= 0:
        parser.error("--index-repeats must be positive")
    return args


def main() -> None:
    args = parse_args()
    result = run_benchmark(args)
    summary = {
        "corpus": result["corpus"],
        "indexing": result["indexing"],
        "retrieval": result["retrieval"],
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"Detailed result: {args.output.resolve()}")


if __name__ == "__main__":
    main()
