"""Run the unchanged Retrieval Evaluation V2 pipeline across multiple papers."""
from __future__ import annotations
import argparse, csv, hashlib, json, statistics
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from evaluation.benchmark_retrieval_v2 import (
    METHOD_ORDER, PROJECT_ROOT, RetrievalCase, _comparisons, _failed_cases,
    _per_category, load_cases, normalize_text, run_benchmark,
)
from evaluation.retrieval_eval.reporting import METHOD_LABELS, QUALITY_KEYS
from evaluation.retrieval_eval.statistics import bootstrap_mean_ci, percentile
from server.config import CONFIG

DEFAULT_MANIFEST = Path(__file__).parent / "datasets" / "retrieval_v2_corpora.json"
DEFAULT_OUTPUT = Path(__file__).parent / "results" / "retrieval_v2_multi_ablation.json"
DEFAULT_WORKSPACE = PROJECT_ROOT / ".runtime" / "retrieval-v2-phase2_1"
CHUNK_SIZE, CHUNK_OVERLAP, TOP_K, CANDIDATE_K = 800, 150, 5, 20
REPEATS, BOOTSTRAP_SAMPLES, BOOTSTRAP_SEED = 5, 2000, 42

def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()

def resolve(value):
    path = Path(value)
    return path.resolve() if path.is_absolute() else (PROJECT_ROOT / path).resolve()

def load_manifest(path):
    corpora = json.loads(path.read_text(encoding="utf-8"))["corpora"]
    if len(corpora) < 3:
        raise ValueError("Phase 2.1 requires at least 3 corpora")
    if len({item["id"] for item in corpora}) != len(corpora):
        raise ValueError("Corpus IDs must be unique")
    total = 0
    for item in corpora:
        item["pdf_path"], item["dataset_path"] = resolve(item["pdf"]), resolve(item["dataset"])
        if not item["pdf_path"].is_file() or not item["dataset_path"].is_file():
            raise FileNotFoundError(item["id"])
        if sha256(item["pdf_path"]).casefold() != item["pdf_sha256"].casefold():
            raise ValueError(f"SHA-256 mismatch for {item['id']}")
        item["query_count"] = len(load_cases(item["dataset_path"]))
        total += item["query_count"]
    if total < 60:
        raise ValueError("Phase 2.1 requires at least 60 queries")
    return corpora

def single_args(item, output, workspace):
    return argparse.Namespace(
        pdf=item["pdf_path"], dataset=item["dataset_path"], output=output,
        workspace=workspace, embedding_model=CONFIG["EMBEDDING_MODEL"],
        reranker_model=CONFIG["RERANKER_MODEL"],
        reranker_device=CONFIG["RERANKER_DEVICE"],
        reranker_batch_size=CONFIG["RERANKER_BATCH_SIZE"],
        chunk_size=CHUNK_SIZE, chunk_overlap=CHUNK_OVERLAP, top_k=TOP_K,
        candidate_k=CANDIDATE_K, repeats=REPEATS,
        bootstrap_samples=BOOTSTRAP_SAMPLES, seed=BOOTSTRAP_SEED,
    )

def aggregate_method(cases, method):
    rows = [case["methods"][method] for case in cases]
    latency = [value for row in rows for value in row["latency_ms"]]
    result = {key: statistics.fmean(row[key] for row in rows) for key in QUALITY_KEYS}
    result.update({
        "latency_ms_p50": percentile(latency, .5),
        "latency_ms_p95": percentile(latency, .95),
        "latency_ms_mean": statistics.fmean(latency),
        "latency_ms_min": min(latency), "latency_ms_max": max(latency),
        "ranking_stable": all(row["ranking_stable"] for row in rows),
        "confidence_intervals_95": {
            key: bootstrap_mean_ci([row[key] for row in rows],
                samples=BOOTSTRAP_SAMPLES, seed=BOOTSTRAP_SEED)
            for key in ("hit_at_5", "mrr_at_5", "ndcg_at_5")
        },
    })
    return result

def rounded(value):
    if isinstance(value, float):
        return round(value, 6)
    if isinstance(value, dict):
        return {key: rounded(item) for key, item in value.items()}
    if isinstance(value, list):
        return [rounded(item) for item in value]
    return value

def aggregate(manifest, corpora, runs):
    if len({json.dumps(run["config"], sort_keys=True) for run in runs}) != 1:
        raise ValueError("Per-corpus configurations differ")
    cases = [{**case, "corpus_id": item["id"], "corpus_title": item["title"]}
        for item, run in zip(corpora, runs) for case in run["cases"]]
    methods = {name: aggregate_method(cases, name) for name in METHOD_ORDER}
    typed_cases = [RetrievalCase(
        id=case["id"], query=case["query"], anchor=case["anchor"],
        category=case["category"]) for case in cases]
    per_method = {name: {case["id"]: case["methods"][name] for case in cases}
        for name in METHOD_ORDER}
    total = len(cases)
    queries = Counter(normalize_text(case["query"]) for case in cases)
    anchors = Counter(normalize_text(case["anchor"]) for case in cases)
    result = {
        "run_id": datetime.now(timezone.utc).strftime("retrieval-v2-phase2_1-%Y%m%dT%H%M%SZ"),
        "phase": "2.1", "environment": runs[0]["environment"],
        "execution": {
            "single_corpus_runner": "evaluation.benchmark_retrieval_v2.run_benchmark",
            "production_components": [
                "server.rag.retrieval.BM25Index",
                "server.rag.retrieval.reciprocal_rank_fusion",
                "server.rag.retrieval.CrossEncoderReranker"],
            "algorithm_or_parameter_changes": False},
        "corpus": {"count": len(runs),
            "pages": sum(run["corpus"]["pages"] for run in runs),
            "chunks": sum(run["corpus"]["chunks"] for run in runs)},
        "dataset": {
            "manifest_path": str(manifest.relative_to(PROJECT_ROOT)),
            "manifest_sha256": sha256(manifest), "query_count": total,
            "gold_label_method": "human-selected answer anchor located in native PDF text",
            "category_distribution": dict(sorted(Counter(
                case["category"] for case in cases).items())),
            "duplicate_queries": sorted(key for key, n in queries.items() if n > 1),
            "duplicate_anchors": sorted(key for key, n in anchors.items() if n > 1),
            "anchor_location_failures": []},
        "config": runs[0]["config"], "methods": methods,
        "candidate_recall": {
            "candidate_k": CANDIDATE_K,
            "dense_candidate_hit_at_20": sum(
                run["candidate_recall"]["dense_candidate_hit_at_20"]
                * run["dataset"]["query_count"] for run in runs) / total,
            "hybrid_candidate_hit_at_20": sum(
                run["candidate_recall"]["hybrid_candidate_hit_at_20"]
                * run["dataset"]["query_count"] for run in runs) / total},
        "comparison": _comparisons(methods),
        "per_category": _per_category(typed_cases, per_method),
        "failed_cases": _failed_cases(typed_cases, per_method),
        "per_corpus": [{"id": item["id"], "title": item["title"],
            "corpus": run["corpus"], "dataset": run["dataset"],
            "initialization": run["initialization"], "methods": run["methods"],
            "candidate_recall": run["candidate_recall"], "outputs": {key: str(Path(value).resolve().relative_to(PROJECT_ROOT)) for key, value in run["outputs"].items()}}
            for item, run in zip(corpora, runs)],
        "cases": cases,
    }
    for entries in result["failed_cases"].values():
        for entry in entries:
            entry["corpus_id"] = next(
                case["corpus_id"] for case in cases if case["id"] == entry["id"])
    return rounded(result)

def metric_rows(methods):
    lines = [
        "| Method | Hit@1 | Hit@3 | Hit@5 | Recall@5 | MRR@5 | NDCG@5 | P50 ms | P95 ms |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |"]
    for name in METHOD_ORDER:
        m = methods[name]
        lines.append(f"| {METHOD_LABELS[name]} | {m['hit_at_1']:.4f} | "
            f"{m['hit_at_3']:.4f} | {m['hit_at_5']:.4f} | {m['recall_at_5']:.4f} | "
            f"{m['mrr_at_5']:.4f} | {m['ndcg_at_5']:.4f} | "
            f"{m['latency_ms_p50']:.2f} | {m['latency_ms_p95']:.2f} |")
    return lines

def interval(value):
    return f"{value['mean']:.4f} [{value['lower']:.4f}, {value['upper']:.4f}]"

def markdown(result):
    env, config, data = result["environment"], result["config"], result["dataset"]
    lines = ["# Retrieval Evaluation V2 Phase 2.1 Report", "",
        f"Run ID: {result['run_id']}", "", "## Scope and Integrity", "",
        f"- Corpora: {result['corpus']['count']} papers, {result['corpus']['pages']} pages, "
        f"{result['corpus']['chunks']} chunks", f"- Queries: {data['query_count']}",
        f"- Categories: {json.dumps(data['category_distribution'])}",
        "- Gold labels: manually selected answer anchors located before retrieval",
        "- Algorithms and parameters: unchanged; every corpus uses the original single-corpus runner",
        f"- Duplicate queries / anchors: {len(data['duplicate_queries'])} / "
        f"{len(data['duplicate_anchors'])}",
        f"- Anchor location failures: {len(data['anchor_location_failures'])}",
        "", "## Environment", "", f"- Timestamp: {env['timestamp_utc']}",
        f"- Git commit: {env['git_commit']}", f"- Worktree dirty: {env.get('git_worktree_dirty')}",
        f"- Platform: {env['platform']}", f"- CPU: {env['cpu']}",
        f"- GPU: {env.get('gpu')}", f"- Python / PyTorch: {env['python_version']} / "
        f"{env['torch_version']}", "", "## Fixed Configuration", "",
        f"- Embedding: {config['embedding_model']}",
        f"- Reranker: {config['reranker_model']} on {config['reranker_device']}",
        f"- Chunk size / overlap: {config['chunk_size']} / {config['chunk_overlap']}",
        f"- Top K / candidate K / RRF constant: {config['top_k']} / "
        f"{config['candidate_k']} / {config['rrf_constant']}",
        f"- Repeats: {config['repeats']}",
        f"- Bootstrap: {config['bootstrap_samples']} samples, seed {config['bootstrap_seed']}",
        "", "## Aggregate Results", "", *metric_rows(result["methods"]),
        "", "## 95% Bootstrap Confidence Intervals Across 60 Queries", "",
        "| Method | Hit@5 | MRR@5 | NDCG@5 |", "| --- | ---: | ---: | ---: |"]
    for name in METHOD_ORDER:
        ci = result["methods"][name]["confidence_intervals_95"]
        lines.append(f"| {METHOD_LABELS[name]} | {interval(ci['hit_at_5'])} | "
            f"{interval(ci['mrr_at_5'])} | {interval(ci['ndcg_at_5'])} |")
    lines += ["", "## Per-paper Results", "",
        "| Corpus | Queries | Pages | Chunks | Method | Hit@5 | MRR@5 | NDCG@5 | P50 ms | P95 ms |",
        "| --- | ---: | ---: | ---: | --- | ---: | ---: | ---: | ---: | ---: |"]
    for corpus in result["per_corpus"]:
        for name in METHOD_ORDER:
            m = corpus["methods"][name]
            lines.append(f"| {corpus['title']} | {corpus['dataset']['query_count']} | "
                f"{corpus['corpus']['pages']} | {corpus['corpus']['chunks']} | "
                f"{METHOD_LABELS[name]} | {m['hit_at_5']:.4f} | {m['mrr_at_5']:.4f} | "
                f"{m['ndcg_at_5']:.4f} | {m['latency_ms_p50']:.2f} | "
                f"{m['latency_ms_p95']:.2f} |")
    candidate = result["candidate_recall"]
    lines += ["", "## Candidate Recall", "",
        f"- Dense candidate Hit@{candidate['candidate_k']}: "
        f"{candidate['dense_candidate_hit_at_20']:.4f}",
        f"- Hybrid candidate Hit@{candidate['candidate_k']}: "
        f"{candidate['hybrid_candidate_hit_at_20']:.4f}",
        "", "## Per-category MRR@5", "",
        "| Category | Dense | BM25 | Hybrid RRF | Hybrid + Reranker |",
        "| --- | ---: | ---: | ---: | ---: |"]
    for category, values in result["per_category"].items():
        lines.append(f"| {category} | {values['dense']['mrr_at_5']:.4f} | "
            f"{values['bm25']['mrr_at_5']:.4f} | "
            f"{values['hybrid_rrf']['mrr_at_5']:.4f} | "
            f"{values['hybrid_reranker']['mrr_at_5']:.4f} |")
    lines += ["", "## Retrieval Regressions and Recoveries", ""]
    for label, entries in result["failed_cases"].items():
        lines += [f"### {label.replace('_', ' ').title()}", ""]
        lines += ([f"- {e['id']} ({e['corpus_id']}): {e['query']}" for e in entries]
            if entries else ["- None observed."])
        lines.append("")
    lines += ["## Known Limitations", "",
        "- The benchmark covers three computer-vision papers; other domains and languages remain untested.",
        "- Relevance is binary and page-level; passage completeness within a page is not graded.",
        "- Queries, categories, and answer anchors were manually authored and checked.",
        "- Latency is host-specific and should not be generalized to other hardware.",
        "- Bootstrap intervals over queries do not establish pairwise significance.", ""]
    return "\n".join(lines)

def write_outputs(result, output):
    output.parent.mkdir(parents=True, exist_ok=True)
    csv_path, report_path = output.with_suffix(".csv"), output.with_name("retrieval_v2_multi_report.md")
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    fields = ["scope", "corpus_id", "method", *QUALITY_KEYS, "latency_p50_ms", "latency_p95_ms"]
    with csv_path.open("w", encoding="utf-8", newline="") as dest:
        writer = csv.DictWriter(dest, fieldnames=fields); writer.writeheader()
        groups = [("aggregate", "all", result["methods"])]
        groups += [("corpus", item["id"], item["methods"]) for item in result["per_corpus"]]
        for scope, corpus_id, methods in groups:
            for name, m in methods.items():
                writer.writerow({"scope": scope, "corpus_id": corpus_id, "method": name,
                    **{key: m[key] for key in QUALITY_KEYS},
                    "latency_p50_ms": m["latency_ms_p50"], "latency_p95_ms": m["latency_ms_p95"]})
    report_path.write_text(markdown(result), encoding="utf-8")
    return {"json": output, "csv": csv_path, "report": report_path}

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--workspace", type=Path, default=DEFAULT_WORKSPACE)
    args = parser.parse_args()
    manifest, output, workspace = args.manifest.resolve(), args.output.resolve(), args.workspace.resolve()
    corpora, runs = load_manifest(manifest), []
    for item in corpora:
        child = output.parent / "phase2_1" / item["id"] / "retrieval_v2_ablation.json"
        print(f"Running {item['id']}: {item['title']} ({item['query_count']} queries)", flush=True)
        runs.append(run_benchmark(single_args(item, child, workspace)))
    result = aggregate(manifest, corpora, runs)
    outputs = write_outputs(result, output)
    print(json.dumps({"run_id": result["run_id"], "corpus": result["corpus"],
        "dataset": result["dataset"], "methods": result["methods"],
        "candidate_recall": result["candidate_recall"],
        "outputs": {key: str(value) for key, value in outputs.items()}},
        ensure_ascii=False, indent=2))

if __name__ == "__main__":
    main()
