"""Latency and bootstrap statistics for retrieval evaluation."""

from __future__ import annotations

import math
import random
import statistics
from typing import Sequence


def percentile(values: Sequence[float], quantile: float) -> float:
    if not values:
        raise ValueError("Cannot compute a percentile of an empty sequence")
    if not 0 <= quantile <= 1:
        raise ValueError("quantile must be between 0 and 1")
    ordered = sorted(float(value) for value in values)
    position = (len(ordered) - 1) * quantile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def bootstrap_mean_ci(
    values: Sequence[float],
    *,
    samples: int = 2_000,
    seed: int = 42,
    confidence: float = 0.95,
) -> dict[str, float]:
    if not values:
        raise ValueError("Cannot bootstrap an empty sequence")
    if samples <= 0:
        raise ValueError("samples must be positive")
    if not 0 < confidence < 1:
        raise ValueError("confidence must be between 0 and 1")
    source = [float(value) for value in values]
    generator = random.Random(seed)
    means = [
        statistics.fmean(generator.choice(source) for _ in source)
        for _ in range(samples)
    ]
    tail = (1 - confidence) / 2
    return {
        "mean": statistics.fmean(source),
        "lower": percentile(means, tail),
        "upper": percentile(means, 1 - tail),
        "confidence": confidence,
        "samples": samples,
        "seed": seed,
    }
