"""Deterministic page-level retrieval metrics."""

from __future__ import annotations

import math
import statistics
from typing import Iterable, Sequence


def relevance_gains(
    retrieved_pages: Sequence[int],
    gold_pages: set[int],
    *,
    cutoff: int = 5,
) -> list[int]:
    """Return binary gains while counting each gold page only once."""
    seen_gold: set[int] = set()
    gains: list[int] = []
    for page in retrieved_pages[:cutoff]:
        relevant = page in gold_pages and page not in seen_gold
        gains.append(int(relevant))
        if relevant:
            seen_gold.add(page)
    return gains


def evaluate_ranking(
    retrieved_pages: Sequence[int],
    gold_pages: set[int],
    *,
    cutoff: int = 5,
) -> dict[str, float | int | None]:
    if cutoff <= 0:
        raise ValueError("cutoff must be positive")
    if not gold_pages:
        raise ValueError("gold_pages cannot be empty")

    gains = relevance_gains(retrieved_pages, gold_pages, cutoff=cutoff)
    first_relevant_rank = next(
        (rank for rank, gain in enumerate(gains, start=1) if gain),
        None,
    )
    retrieved_gold = set(retrieved_pages[:cutoff]) & gold_pages
    dcg = sum(
        gain / math.log2(rank + 1)
        for rank, gain in enumerate(gains, start=1)
    )
    ideal_count = min(len(gold_pages), cutoff)
    idcg = sum(
        1.0 / math.log2(rank + 1)
        for rank in range(1, ideal_count + 1)
    )
    return {
        "hit_at_1": float(bool(set(retrieved_pages[:1]) & gold_pages)),
        "hit_at_3": float(bool(set(retrieved_pages[:3]) & gold_pages)),
        "hit_at_5": float(bool(set(retrieved_pages[:5]) & gold_pages)),
        "recall_at_5": len(retrieved_gold) / len(gold_pages),
        "mrr_at_5": 0.0 if first_relevant_rank is None else 1.0 / first_relevant_rank,
        "ndcg_at_5": 0.0 if idcg == 0 else dcg / idcg,
        "first_relevant_rank": first_relevant_rank,
    }


def summarize_rankings(
    rankings: Iterable[tuple[Sequence[int], set[int]]],
) -> tuple[dict[str, float], list[dict[str, float | int | None]]]:
    case_metrics = [
        evaluate_ranking(retrieved, gold)
        for retrieved, gold in rankings
    ]
    if not case_metrics:
        raise ValueError("At least one ranking is required")
    keys = (
        "hit_at_1",
        "hit_at_3",
        "hit_at_5",
        "recall_at_5",
        "mrr_at_5",
        "ndcg_at_5",
    )
    aggregate = {
        key: statistics.fmean(float(item[key]) for item in case_metrics)
        for key in keys
    }
    return aggregate, case_metrics
