"""Descriptive sample-size labels only (not statistical significance)."""

from __future__ import annotations

from typing import Any


def sample_quality_label(n: int) -> str:
    """
    Descriptive labels for journal / report sample sizes.

    N < 20       = INSUFFICIENT_SAMPLE
    20–49        = SMALL_SAMPLE
    50–99        = MODERATE_SAMPLE
    100+         = LARGER_SAMPLE
    """
    if n < 20:
        return "INSUFFICIENT_SAMPLE"
    if n < 50:
        return "SMALL_SAMPLE"
    if n < 100:
        return "MODERATE_SAMPLE"
    return "LARGER_SAMPLE"


def mark_sample(n: int) -> dict[str, Any]:
    label = sample_quality_label(n)
    return {
        "n": n,
        "sample_quality": label,
        "sample_warning": "INSUFFICIENT_SAMPLE" if label == "INSUFFICIENT_SAMPLE" else None,
        "n_ge_30": n >= 30,
        "n_ge_50": n >= 50,
        "n_ge_100": n >= 100,
    }
