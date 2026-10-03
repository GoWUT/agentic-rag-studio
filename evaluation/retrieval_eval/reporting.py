"""JSON, CSV, and Markdown output for Retrieval Evaluation V2."""

from __future__ import annotations

import csv
import json
from pathlib import Path


METHOD_LABELS = {
    "dense": "Dense",
    "bm25": "BM25",
    "hybrid_rrf": "Hybrid RRF",
    "hybrid_reranker": "Hybrid + Reranker",
}
QUALITY_KEYS = (
    "hit_at_1",
    "hit_at_3",
    "hit_at_5",
    "recall_at_5",
    "mrr_at_5",
    "ndcg_at_5",
)


def write_outputs(result: dict, output_path: Path) -> dict[str, Path]:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    csv_path = output_path.with_suffix(".csv")
    report_path = output_path.with_name("retrieval_v2_report.md")
    output_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    _write_csv(result, csv_path)
    report_path.write_text(_markdown_report(result), encoding="utf-8")
    return {"json": output_path, "csv": csv_path, "report": report_path}


def _write_csv(result: dict, path: Path) -> None:
    fields = [
        "method",
        *QUALITY_KEYS,
        "latency_p50_ms",
        "latency_p95_ms",
    ]
    with path.open("w", encoding="utf-8", newline="") as destination:
        writer = csv.DictWriter(destination, fieldnames=fields)
        writer.writeheader()
        for name, metrics in result["methods"].items():
            writer.writerow({
                "method": name,
                **{key: metrics[key] for key in QUALITY_KEYS},
                "latency_p50_ms": metrics["latency_ms_p50"],
                "latency_p95_ms": metrics["latency_ms_p95"],
            })


def _markdown_report(result: dict) -> str:
    environment = result["environment"]
    corpus = result["corpus"]
    dataset = result["dataset"]
    config = result["config"]
    methods = result["methods"]
    lines = [
        "# Retrieval Evaluation V2 Report",
        "",
        f"Run ID: `{result['run_id']}`",
        "",
        "## Environment",
        "",
        f"- Timestamp: {environment['timestamp_utc']}",
        f"- Git commit: `{environment['git_commit']}`",
        f"- Git worktree dirty: {environment.get('git_worktree_dirty', 'unknown')}",
        f"- Platform: {environment['platform']}",
        f"- CPU: {environment['cpu']}",
        f"- GPU: {environment.get('gpu', 'unknown')}",
        f"- CUDA available: {environment.get('cuda_available', 'unknown')}",
        f"- Python: {environment['python_version']}",
        f"- PyTorch: {environment['torch_version']}",
        f"- sentence-transformers: {environment['sentence_transformers_version']}",
        f"- Device: {environment['device']}",
        "",
        "## Corpus",
        "",
        f"- PDF: `{corpus['pdf_path']}`",
        f"- SHA-256: `{corpus['pdf_sha256']}`",
        f"- Size: {corpus['pdf_size_bytes']} bytes",
        f"- Pages: {corpus['pages']}",
        f"- Chunks: {corpus['chunks']}",
        f"- Shared index: `{corpus['index_path']}`",
        "",
        "## Evaluation Dataset",
        "",
        f"- Dataset: `{dataset['path']}`",
        f"- SHA-256: `{dataset['sha256']}`",
        f"- Queries: {dataset['query_count']}",
        f"- Gold label: {dataset['gold_label_method']}",
        f"- Categories: {json.dumps(dataset['category_distribution'], ensure_ascii=False)}",
        f"- Gold-page distribution: {json.dumps(dataset['gold_page_distribution'], ensure_ascii=False)}",
        f"- Duplicate queries: {len(dataset['duplicate_queries'])}",
        f"- Duplicate anchors: {len(dataset['duplicate_anchors'])}",
        f"- Anchor location failures: {len(dataset['anchor_location_failures'])}",
        "",
        "## Configuration",
        "",
        f"- Embedding: `{config['embedding_model']}`",
        f"- Reranker: `{config['reranker_model']}`",
        f"- Chunk size / overlap: {config['chunk_size']} / {config['chunk_overlap']}",
        f"- Top K / candidate K: {config['top_k']} / {config['candidate_k']}",
        f"- RRF constant: {config['rrf_constant']}",
        f"- Repeats: {config['repeats']}",
        f"- Bootstrap: {config['bootstrap_samples']} samples, seed {config['bootstrap_seed']}",
        "",
        "## Ablation Table",
        "",
        "| Method | Hit@1 | Hit@3 | Hit@5 | Recall@5 | MRR@5 | NDCG@5 | P50 ms | P95 ms |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for name, metrics in methods.items():
        lines.append(
            f"| {METHOD_LABELS[name]} | {metrics['hit_at_1']:.4f} | "
            f"{metrics['hit_at_3']:.4f} | {metrics['hit_at_5']:.4f} | "
            f"{metrics['recall_at_5']:.4f} | {metrics['mrr_at_5']:.4f} | "
            f"{metrics['ndcg_at_5']:.4f} | {metrics['latency_ms_p50']:.2f} | "
            f"{metrics['latency_ms_p95']:.2f} |"
        )

    lines.extend([
        "",
        "## 95% Bootstrap Confidence Intervals",
        "",
        "| Method | Hit@5 | MRR@5 | NDCG@5 |",
        "| --- | ---: | ---: | ---: |",
    ])
    for name, metrics in methods.items():
        intervals = metrics["confidence_intervals_95"]
        lines.append(
            f"| {METHOD_LABELS[name]} | {_format_interval(intervals['hit_at_5'])} | "
            f"{_format_interval(intervals['mrr_at_5'])} | "
            f"{_format_interval(intervals['ndcg_at_5'])} |"
        )

    lines.extend([
        "",
        "## Latency",
        "",
        f"- BM25 index build: {result['initialization']['bm25_index_build_ms']:.2f} ms",
        f"- Reranker cold start: {result['initialization']['reranker_cold_start_ms']:.2f} ms",
        f"- Reranker warm-up queries: {result['initialization']['reranker_warmup_queries']}",
        "",
        "| Method | Dense ms | BM25 ms | RRF ms | Reranker ms | Total ms |",
        "| --- | ---: | ---: | ---: | ---: | ---: |",
    ])
    for name, metrics in methods.items():
        stages = metrics["stage_latency_ms"]
        lines.append(
            f"| {METHOD_LABELS[name]} | {_format_optional(stages.get('dense_ms_p50'))} | "
            f"{_format_optional(stages.get('bm25_ms_p50'))} | "
            f"{_format_optional(stages.get('rrf_ms_p50'))} | "
            f"{_format_optional(stages.get('reranker_ms_p50'))} | "
            f"{metrics['latency_ms_p50']:.2f} |"
        )

    candidate = result["candidate_recall"]
    lines.extend([
        "",
        "## Candidate Recall",
        "",
        f"- Dense candidate Hit@{config['candidate_k']}: {candidate['dense_candidate_hit_at_20']:.4f}",
        f"- Hybrid RRF candidate Hit@{config['candidate_k']}: {candidate['hybrid_candidate_hit_at_20']:.4f}",
    ])

    if result["per_category"]:
        lines.extend([
            "",
            "## Per-category Results",
            "",
            "| Category | Dense MRR | BM25 MRR | Hybrid MRR | Hybrid + Reranker MRR |",
            "| --- | ---: | ---: | ---: | ---: |",
        ])
        for category, values in result["per_category"].items():
            lines.append(
                f"| {category} | {values['dense']['mrr_at_5']:.4f} | "
                f"{values['bm25']['mrr_at_5']:.4f} | "
                f"{values['hybrid_rrf']['mrr_at_5']:.4f} | "
                f"{values['hybrid_reranker']['mrr_at_5']:.4f} |"
            )

    lines.extend(["", "## Failed Cases", ""])
    for label, entries in result["failed_cases"].items():
        lines.append(f"### {label.replace('_', ' ').title()}")
        lines.append("")
        if entries:
            lines.extend(
                f"- `{item['id']}`: {item['query']} ({item['analysis']})"
                for item in entries
            )
        else:
            lines.append("- None observed.")
        lines.append("")

    lines.extend(["## Observations", ""])
    for observation in _observations(result):
        lines.append(f"- {observation}")
    lines.extend([
        "",
        "## Known Limitations",
        "",
        f"- The benchmark contains one {corpus['pages']}-page PDF and {dataset['query_count']} evaluation queries.",
        "- Relevance is binary and page-level; it does not judge passage completeness within a page.",
        "- Query categories and answer anchors were manually assigned for this dataset.",
        "- Warm latency was measured on this CPU host and should not be generalized to other hardware.",
        "- The recorded reranker cold start may include model download when weights are not cached.",
        "",
        "These measurements describe this corpus, dataset, hardware, and fixed production configuration. Confidence intervals are bootstrap intervals over evaluation queries and do not establish statistical significance between methods.",
        "",
    ])
    return "\n".join(lines)


def _observations(result: dict) -> list[str]:
    observations = []
    labels = {
        "hybrid_vs_dense": "Hybrid RRF versus Dense",
        "hybrid_reranker_vs_hybrid": "Hybrid + Reranker versus Hybrid RRF",
        "hybrid_reranker_vs_dense": "Hybrid + Reranker versus Dense",
    }
    for key, label in labels.items():
        delta = result["comparison"][key]
        observations.append(
            f"{label}: Hit@5 delta {delta['hit_at_5_delta']:+.4f}, "
            f"MRR@5 delta {delta['mrr_at_5_delta']:+.4f}, "
            f"NDCG@5 delta {delta['ndcg_at_5_delta']:+.4f}, and "
            f"P95 latency delta {delta['p95_latency_delta_ms']:+.2f} ms."
        )
    return observations


def _format_optional(value: float | None) -> str:
    return "—" if value is None else f"{value:.2f}"


def _format_interval(interval: dict[str, float]) -> str:
    return (
        f"{interval['mean']:.4f} "
        f"[{interval['lower']:.4f}, {interval['upper']:.4f}]"
    )
