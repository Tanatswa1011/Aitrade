"""LuxAlgo ↔ internal CHoCH equivalence diagnostics (conservative)."""

from __future__ import annotations

from typing import Any, Optional, Sequence

from models import StructureConfirmation


DEFAULT_TOLERANCES = {
    "5m": {"time_tolerance_sec": 300, "max_bar_distance": 1, "level_tolerance": 5.0},
    "15m": {"time_tolerance_sec": 900, "max_bar_distance": 1, "level_tolerance": 5.0},
}


def _bar_index(ts: Optional[int], period_sec: int) -> Optional[int]:
    if ts is None:
        return None
    return int(ts) // int(period_sec)


def compare_choch_overlap(
    internal_events: Sequence[StructureConfirmation],
    luxalgo_events: Sequence[StructureConfirmation],
    *,
    time_tolerance_sec: int = 900,
    level_tolerance: float = 5.0,
    max_bar_distance: Optional[int] = None,
    period_sec: Optional[int] = None,
    timeframe: Optional[str] = None,
) -> dict[str, Any]:
    """
    Match events by direction + approximate time + level (+ optional bar distance).

    Does not loosen tolerances to inflate match rate.
    Does not force equivalence.
    """
    lux = [
        e
        for e in luxalgo_events
        if e.event_timestamp is not None
        and e.timing_confidence in ("exact", "derived")
        and e.kind == "CHoCH"
    ]
    internal = [e for e in internal_events if e.kind == "CHoCH"]

    direction_matches = 0
    time_window_matches = 0
    level_window_matches = 0
    full_matches = []
    used_lux: set[int] = set()
    used_int: set[int] = set()

    for i, ie in enumerate(internal):
        if ie.event_timestamp is None:
            continue
        best = None
        best_j = None
        best_meta = None
        for j, le in enumerate(lux):
            if j in used_lux:
                continue
            same_dir = ie.direction == le.direction
            if same_dir:
                direction_matches += 1
            if not same_dir:
                continue
            dt = abs(int(ie.event_timestamp) - int(le.event_timestamp))
            time_ok = dt <= time_tolerance_sec
            if time_ok:
                time_window_matches += 1
            dl = abs(float(ie.level) - float(le.level))
            level_ok = dl <= level_tolerance
            if level_ok:
                level_window_matches += 1
            bar_ok = True
            bar_delta = None
            if max_bar_distance is not None and period_sec:
                bi = _bar_index(ie.event_timestamp, period_sec)
                bl = _bar_index(le.event_timestamp, period_sec)
                if bi is None or bl is None:
                    bar_ok = False
                else:
                    bar_delta = abs(bi - bl)
                    bar_ok = bar_delta <= max_bar_distance
            if not (time_ok and level_ok and bar_ok):
                continue
            score = dt + dl
            if best is None or score < best:
                best = score
                best_j = j
                best_meta = {"time_delta_sec": dt, "level_delta": dl, "bar_delta": bar_delta}
        if best_j is not None and best_meta is not None:
            le = lux[best_j]
            used_lux.add(best_j)
            used_int.add(i)
            full_matches.append(
                {
                    "direction": ie.direction,
                    "internal_ts": ie.event_timestamp,
                    "lux_ts": le.event_timestamp,
                    "internal_level": ie.level,
                    "lux_level": le.level,
                    **best_meta,
                }
            )

    lux_only = [
        {"direction": e.direction, "timestamp": e.event_timestamp, "level": e.level}
        for j, e in enumerate(lux)
        if j not in used_lux
    ]
    internal_only = [
        {"direction": e.direction, "timestamp": e.event_timestamp, "level": e.level}
        for i, e in enumerate(internal)
        if i not in used_int
    ]

    status = classify_equivalence_status(
        luxalgo_reliable_count=len(lux),
        matched_count=len(full_matches),
    )

    return {
        "timeframe": timeframe,
        "tolerances": {
            "time_tolerance_sec": time_tolerance_sec,
            "level_tolerance": level_tolerance,
            "max_bar_distance": max_bar_distance,
            "period_sec": period_sec,
            "note": "Tolerances fixed; not loosened to improve match rate",
        },
        "luxalgo_reliable_count": len(lux),
        "internal_count": len(internal),
        "direction_match_pairs_scanned": direction_matches,
        "time_window_match_pairs_scanned": time_window_matches,
        "level_window_match_pairs_scanned": level_window_matches,
        "full_matches": full_matches,
        "matched_count": len(full_matches),
        "matched": full_matches,  # backward compatible
        "luxalgo_only": lux_only,
        "luxalgo_only_count": len(lux_only),
        "internal_only": internal_only,
        "internal_only_count": len(internal_only),
        "missed_internal": internal_only,
        "missed_internal_count": len(internal_only),
        "missed_luxalgo": lux_only,
        "missed_luxalgo_count": len(lux_only),
        "equivalence_status": status,
        "note": (
            "Overlap diagnostic only. Do not treat internal CHoCH as LuxAlgo-equivalent "
            "without conservative status promotion."
        ),
    }


def classify_equivalence_status(
    *,
    luxalgo_reliable_count: int,
    matched_count: int,
) -> str:
    """
    Conservative status ladder:
    - unvalidated_against_luxalgo: default / tiny sample
    - partially_validated: meaningful reliable overlap with some matches
    - validated: reserved; not used without strong evidence
    """
    if luxalgo_reliable_count < 10 or matched_count < 5:
        return "unvalidated_against_luxalgo"
    match_rate = matched_count / max(luxalgo_reliable_count, 1)
    if luxalgo_reliable_count >= 30 and match_rate >= 0.7 and matched_count >= 20:
        # Still do not auto-promote to fully validated in Phase 19
        return "partially_validated"
    if luxalgo_reliable_count >= 10 and matched_count >= 5 and match_rate >= 0.4:
        return "partially_validated"
    return "unvalidated_against_luxalgo"


def tolerances_for_timeframe(tf: str) -> dict[str, Any]:
    return dict(DEFAULT_TOLERANCES.get(tf, DEFAULT_TOLERANCES["5m"]))
