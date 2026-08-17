"""Phase 20 LuxAlgo ↔ internal CHoCH matching (behavioral fidelity only)."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Optional, Sequence

from models import StructureConfirmation


EXACT_MATCH = "EXACT_MATCH"
NEAR_TIME_MATCH = "NEAR_TIME_MATCH"
DIRECTION_ONLY_MATCH = "DIRECTION_ONLY_MATCH"
LEVEL_ONLY_MATCH = "LEVEL_ONLY_MATCH"
LUXALGO_ONLY = "LUXALGO_ONLY"
TIMING_UNRESOLVED = "TIMING_UNRESOLVED"
MATCHED = "MATCHED"
INTERNAL_ONLY = "INTERNAL_ONLY"

# Fixed tolerances — not tuned to observed match rate.
DEFAULT_LEVEL_TOLERANCE = 5.0


@dataclass(frozen=True)
class MatchTolerances:
    level_tolerance: float = DEFAULT_LEVEL_TOLERANCE
    max_bar_distance: int = 2  # evaluate 0,1,2 separately
    period_sec: int = 300

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _bar_index(ts: Optional[int], period_sec: int) -> Optional[int]:
    if ts is None:
        return None
    return int(ts) // int(period_sec)


def _level_delta(a: Optional[float], b: Optional[float]) -> Optional[float]:
    if a is None or b is None:
        return None
    return abs(float(a) - float(b))


def classify_luxalgo_match(
    lux: StructureConfirmation,
    internal_events: Sequence[StructureConfirmation],
    *,
    tolerances: MatchTolerances,
) -> dict[str, Any]:
    """Classify one LuxAlgo event against internal pool."""
    if lux.timing_confidence not in ("exact", "derived") or lux.event_timestamp is None:
        return {
            "category": TIMING_UNRESOLVED,
            "luxalgo": _event_brief(lux),
            "matched_internal": None,
        }

    same_dir = [
        e
        for e in internal_events
        if e.direction == lux.direction and e.event_timestamp is not None
    ]
    opp_dir = [
        e
        for e in internal_events
        if e.direction != lux.direction
        and e.event_timestamp is not None
        and abs(int(e.event_timestamp) - int(lux.event_timestamp))
        <= tolerances.max_bar_distance * tolerances.period_sec
    ]

    best = None
    best_score = None
    for ie in same_dir:
        dt = abs(int(ie.event_timestamp) - int(lux.event_timestamp))
        bar_delta = abs(
            (_bar_index(ie.event_timestamp, tolerances.period_sec) or 0)
            - (_bar_index(lux.event_timestamp, tolerances.period_sec) or 0)
        )
        dl = _level_delta(ie.level, lux.level)
        if bar_delta > tolerances.max_bar_distance:
            continue
        score = dt + (dl or 0)
        if best_score is None or score < best_score:
            best_score = score
            best = {
                "internal": _event_brief(ie),
                "time_delta_sec": dt,
                "bar_delta": bar_delta,
                "level_delta": dl,
                "level_ok": dl is not None and dl <= tolerances.level_tolerance,
            }

    if best is None:
        # Direction-only or level-only loose scans (diagnostic)
        level_only = None
        for ie in internal_events:
            if ie.event_timestamp is None:
                continue
            dl = _level_delta(ie.level, lux.level)
            if dl is not None and dl <= tolerances.level_tolerance:
                level_only = _event_brief(ie)
                break
        if opp_dir:
            return {
                "category": LUXALGO_ONLY,
                "note": "nearby_opposite_direction_internal_exists",
                "luxalgo": _event_brief(lux),
                "matched_internal": None,
                "opposite_nearby": [_event_brief(e) for e in opp_dir[:3]],
            }
        if level_only and not same_dir:
            return {
                "category": LEVEL_ONLY_MATCH,
                "luxalgo": _event_brief(lux),
                "matched_internal": level_only,
            }
        return {
            "category": LUXALGO_ONLY,
            "luxalgo": _event_brief(lux),
            "matched_internal": None,
        }

    if best["bar_delta"] == 0 and best["level_ok"]:
        cat = EXACT_MATCH
    elif best["bar_delta"] <= tolerances.max_bar_distance and best["level_ok"]:
        cat = NEAR_TIME_MATCH
    elif best["bar_delta"] <= tolerances.max_bar_distance and not best["level_ok"]:
        cat = DIRECTION_ONLY_MATCH
    else:
        cat = LUXALGO_ONLY

    return {
        "category": cat,
        "luxalgo": _event_brief(lux),
        "matched_internal": best["internal"],
        "time_delta_sec": best["time_delta_sec"],
        "bar_delta": best["bar_delta"],
        "level_delta": best["level_delta"],
    }


def _event_brief(e: StructureConfirmation) -> dict[str, Any]:
    return {
        "direction": e.direction,
        "level": e.level,
        "event_timestamp": e.event_timestamp,
        "event_bar_index": e.event_bar_index,
        "timing_confidence": e.timing_confidence,
        "source": e.source,
        "raw_id": e.raw_id,
        "extras": dict(e.extras or {}),
    }


def match_overlap(
    luxalgo_events: Sequence[StructureConfirmation],
    internal_events: Sequence[StructureConfirmation],
    *,
    timeframe: str,
    period_sec: int,
    level_tolerance: float = DEFAULT_LEVEL_TOLERANCE,
) -> dict[str, Any]:
    """Full overlap report with multi-threshold bar distances."""
    tol = MatchTolerances(
        level_tolerance=level_tolerance,
        max_bar_distance=2,
        period_sec=period_sec,
    )
    reliable = [
        e
        for e in luxalgo_events
        if e.kind == "CHoCH"
        and e.timing_confidence in ("exact", "derived")
        and e.event_timestamp is not None
    ]
    unreliable = [
        e
        for e in luxalgo_events
        if e.kind == "CHoCH"
        and e not in reliable
    ]
    internal = [e for e in internal_events if e.kind == "CHoCH"]

    classifications = [
        classify_luxalgo_match(le, internal, tolerances=tol) for le in reliable
    ]
    # Also mark timing unresolved for unreliable
    for ue in unreliable:
        classifications.append(
            {
                "category": TIMING_UNRESOLVED,
                "luxalgo": _event_brief(ue),
                "matched_internal": None,
            }
        )

    by_cat: dict[str, int] = {}
    for row in classifications:
        by_cat[row["category"]] = by_cat.get(row["category"], 0) + 1

    # Threshold breakdown among reliable only
    def count_within(max_bars: int, *, require_level: bool) -> int:
        n = 0
        for le in reliable:
            row = classify_luxalgo_match(
                le,
                internal,
                tolerances=MatchTolerances(
                    level_tolerance=level_tolerance,
                    max_bar_distance=max_bars,
                    period_sec=period_sec,
                ),
            )
            if row["category"] in (EXACT_MATCH, NEAR_TIME_MATCH):
                if max_bars == 0 and row["category"] != EXACT_MATCH:
                    continue
                if require_level and row.get("level_delta") is not None:
                    if float(row["level_delta"]) > level_tolerance:
                        continue
                n += 1
            elif max_bars == 0 and row["category"] == EXACT_MATCH:
                n += 1
        return n

    exact = sum(1 for r in classifications if r["category"] == EXACT_MATCH)
    near1 = sum(
        1
        for r in classifications
        if r["category"] in (EXACT_MATCH, NEAR_TIME_MATCH)
        and (r.get("bar_delta") is None or int(r.get("bar_delta") or 99) <= 1)
    )
    near2 = sum(
        1
        for r in classifications
        if r["category"] in (EXACT_MATCH, NEAR_TIME_MATCH)
        and (r.get("bar_delta") is None or int(r.get("bar_delta") or 99) <= 2)
    )

    matched_internal_ids = set()
    for r in classifications:
        mi = r.get("matched_internal")
        if mi and r["category"] in (EXACT_MATCH, NEAR_TIME_MATCH, DIRECTION_ONLY_MATCH):
            matched_internal_ids.add(
                (mi.get("event_timestamp"), mi.get("direction"), mi.get("level"))
            )

    internal_only = []
    for ie in internal:
        key = (ie.event_timestamp, ie.direction, ie.level)
        if key not in matched_internal_ids:
            # Restrict internal-only to overlap window of reliable lux times
            if reliable:
                tmin = min(int(e.event_timestamp) for e in reliable) - 2 * period_sec
                tmax = max(int(e.event_timestamp) for e in reliable) + 2 * period_sec
                if ie.event_timestamp is None or not (tmin <= int(ie.event_timestamp) <= tmax):
                    continue
            internal_only.append(_event_brief(ie))

    lux_matched = exact + sum(
        1 for r in classifications if r["category"] == NEAR_TIME_MATCH
    )
    luxalgo_coverage = (lux_matched / len(reliable)) if reliable else None
    internal_in_window = len(matched_internal_ids) + len(internal_only)
    internal_precision = (
        (len(matched_internal_ids) / internal_in_window) if internal_in_window else None
    )

    level_deltas = [
        float(r["level_delta"])
        for r in classifications
        if r.get("level_delta") is not None
        and r["category"] in (EXACT_MATCH, NEAR_TIME_MATCH, DIRECTION_ONLY_MATCH)
    ]

    return {
        "timeframe": timeframe,
        "tolerances": tol.to_dict(),
        "luxalgo_total": len(luxalgo_events),
        "luxalgo_reliable": len(reliable),
        "luxalgo_timing_unresolved": len(unreliable),
        "internal_count": len(internal),
        "by_category": by_cat,
        "exact_matches": exact,
        "within_1_bar_matches": near1,
        "within_2_bar_matches": near2,
        "direction_only_matches": by_cat.get(DIRECTION_ONLY_MATCH, 0),
        "level_only_matches": by_cat.get(LEVEL_ONLY_MATCH, 0),
        "luxalgo_only": by_cat.get(LUXALGO_ONLY, 0),
        "timing_unresolved": by_cat.get(TIMING_UNRESOLVED, 0),
        "internal_only_count": len(internal_only),
        "internal_only": internal_only,
        "luxalgo_coverage": luxalgo_coverage,
        "internal_precision": internal_precision,
        "level_delta": {
            "n": len(level_deltas),
            "median": sorted(level_deltas)[len(level_deltas) // 2] if level_deltas else None,
            "mean": sum(level_deltas) / len(level_deltas) if level_deltas else None,
            "max": max(level_deltas) if level_deltas else None,
        },
        "classifications": classifications,
        "direction_mismatches_nearby": sum(
            1
            for r in classifications
            if r["category"] == LUXALGO_ONLY and r.get("opposite_nearby")
        ),
    }


def classify_equivalence_status_phase20(
    *,
    reliable_n: int,
    exact: int,
    near2: int,
    luxalgo_only: int,
    internal_only: int,
) -> tuple[str, str]:
    """
    Returns (status, confidence_note).
    Statuses: UNVALIDATED | LOW_EQUIVALENCE | PARTIAL_EQUIVALENCE | HIGH_EQUIVALENCE
    """
    if reliable_n < 20:
        return (
            "UNVALIDATED",
            f"reliable_n={reliable_n} < 20; sample too small for equivalence claim",
        )
    coverage = near2 / reliable_n
    if reliable_n >= 50 and coverage >= 0.75 and luxalgo_only <= 0.2 * reliable_n:
        return (
            "HIGH_EQUIVALENCE",
            f"near2_coverage={coverage:.2f} reliable_n={reliable_n}",
        )
    if coverage >= 0.4 and reliable_n >= 20:
        return (
            "PARTIAL_EQUIVALENCE",
            f"near2_coverage={coverage:.2f} luxalgo_only={luxalgo_only} internal_only={internal_only}",
        )
    if coverage < 0.35 or luxalgo_only > 0.5 * reliable_n:
        return (
            "LOW_EQUIVALENCE",
            f"near2_coverage={coverage:.2f} luxalgo_only={luxalgo_only} internal_only={internal_only}",
        )
    return (
        "PARTIAL_EQUIVALENCE",
        f"near2_coverage={coverage:.2f} reliable_n={reliable_n}",
    )
