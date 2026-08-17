"""Divergence cause classification for LuxAlgo vs internal CHoCH mismatches."""

from __future__ import annotations

from typing import Any, Optional, Sequence

from models import StructureConfirmation
from phase20_matching import (
    DIRECTION_ONLY_MATCH,
    EXACT_MATCH,
    LUXALGO_ONLY,
    NEAR_TIME_MATCH,
    TIMING_UNRESOLVED,
)


SWING_DEFINITION_DIFFERENCE = "SWING_DEFINITION_DIFFERENCE"
CLOSE_VS_WICK_BREAK = "CLOSE_VS_WICK_BREAK"
INDUCEMENT_DEPENDENCY = "INDUCEMENT_DEPENDENCY"
PIVOT_CONFIRMATION_DELAY = "PIVOT_CONFIRMATION_DELAY"
STRUCTURE_STATE_DIFFERENCE = "STRUCTURE_STATE_DIFFERENCE"
FIRST_BREAK_VS_LATER_BREAK = "FIRST_BREAK_VS_LATER_BREAK"
LEVEL_SELECTION_DIFFERENCE = "LEVEL_SELECTION_DIFFERENCE"
UNKNOWN = "UNKNOWN"


def classify_divergence(
    row: dict[str, Any],
    *,
    nearby_luxalgo_context: Optional[Sequence[dict[str, Any]]] = None,
    internal_wick_would_match: Optional[bool] = None,
) -> dict[str, Any]:
    """
    Conservative cause labels. Prefer UNKNOWN over guessing.
    """
    cat = row.get("category")
    if cat in (EXACT_MATCH, NEAR_TIME_MATCH):
        return {"cause": None, "note": "matched"}
    if cat == TIMING_UNRESOLVED:
        return {"cause": UNKNOWN, "note": "luxalgo_timing_unresolved"}

    level_delta = row.get("level_delta")
    bar_delta = row.get("bar_delta")
    opposite = row.get("opposite_nearby") or []

    if cat == DIRECTION_ONLY_MATCH and level_delta is not None and level_delta > 5.0:
        return {
            "cause": LEVEL_SELECTION_DIFFERENCE,
            "note": "same_direction_near_time_but_level_differs",
            "level_delta": level_delta,
        }

    if cat == LUXALGO_ONLY and opposite:
        return {
            "cause": STRUCTURE_STATE_DIFFERENCE,
            "note": "internal_has_opposite_direction_nearby",
        }

    if cat == LUXALGO_ONLY and bar_delta is None:
        # Check context labels
        ctx = list(nearby_luxalgo_context or [])
        texts = [str(x.get("t") or x.get("label_text") or "") for x in ctx]
        if any(t == "IDM" for t in texts):
            return {
                "cause": INDUCEMENT_DEPENDENCY,
                "note": "idm_present_near_unmatched_choch_but_not_proven_required",
                "confidence": "low",
            }
        if any(t == "BOS" for t in texts):
            return {
                "cause": STRUCTURE_STATE_DIFFERENCE,
                "note": "bos_present_near_unmatched_choch",
                "confidence": "low",
            }

    if internal_wick_would_match is True:
        return {
            "cause": CLOSE_VS_WICK_BREAK,
            "note": "wick_break_would_align_internal_close_misses",
        }

    if cat == LUXALGO_ONLY and row.get("matched_internal") is None:
        return {"cause": UNKNOWN, "note": "no_same_direction_internal_within_tolerance"}

    if bar_delta is not None and int(bar_delta) >= 1 and cat == DIRECTION_ONLY_MATCH:
        return {
            "cause": PIVOT_CONFIRMATION_DELAY,
            "note": "possible_confirmation_lag",
            "confidence": "low",
        }

    return {"cause": UNKNOWN, "note": "insufficient_evidence"}


def summarize_divergences(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    by_cause: dict[str, int] = {}
    detailed = []
    for r in rows:
        if r.get("category") in (EXACT_MATCH, NEAR_TIME_MATCH):
            continue
        div = r.get("divergence") or classify_divergence(r)
        cause = div.get("cause") or UNKNOWN
        by_cause[cause] = by_cause.get(cause, 0) + 1
        detailed.append({**r, "divergence": div})
    dominant = None
    if by_cause:
        dominant = max(by_cause.items(), key=lambda kv: kv[1])
        if dominant[0] == UNKNOWN and len(by_cause) > 1:
            # pick next if UNKNOWN dominates weakly
            others = [(k, v) for k, v in by_cause.items() if k != UNKNOWN]
            if others and max(others, key=lambda kv: kv[1])[1] >= dominant[1]:
                dominant = max(others, key=lambda kv: kv[1])
    return {
        "by_cause": by_cause,
        "dominant_cause": None if not dominant else dominant[0],
        "dominant_count": None if not dominant else dominant[1],
        "rows": detailed,
    }
