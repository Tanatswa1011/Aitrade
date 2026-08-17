"""Invalid directional-stop diagnostics (no stop-rule changes)."""

from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any, Optional, Sequence

from journal_models import SetupJournalRecord


INVALID_STOP_CATEGORIES = (
    "entry_already_beyond_sweep_extreme",
    "entry_already_beyond_fvg_invalidation",
    "sweep_extreme_after_assumed_event",
    "same_trigger_bar_contains_stop_level",
    "bad_event_ordering",
    "wrong_direction_propagation",
    "data_artifact",
    "other",
)


def categorize_invalid_directional_stop(
    *,
    direction: Optional[str],
    entry_price: Optional[float],
    stop_price: Optional[float],
    sweep_extreme: Optional[float],
    sweep_level: Optional[float],
    fvg_low: Optional[float],
    fvg_high: Optional[float],
    stop_mode: Optional[str],
    entry_timestamp: Optional[int] = None,
    sweep_timestamp: Optional[int] = None,
    fvg_created_timestamp: Optional[int] = None,
    trigger_bar_also_hits_stop: bool = False,
) -> str:
    """
    Classify why stop_not_directional / invalid directional stop occurred.

    Does not change strategy rules — diagnostics only.
    """
    d = (direction or "").lower()
    mode = (stop_mode or "").lower()
    if entry_price is None or stop_price is None:
        return "other"

    ep = float(entry_price)
    sp = float(stop_price)

    # Ordering bugs / future extreme
    if (
        entry_timestamp is not None
        and sweep_timestamp is not None
        and int(entry_timestamp) < int(sweep_timestamp)
    ):
        return "bad_event_ordering"
    if (
        sweep_timestamp is not None
        and fvg_created_timestamp is not None
        and int(fvg_created_timestamp) < int(sweep_timestamp)
    ):
        return "sweep_extreme_after_assumed_event"

    # Direction vs geometric expectation
    if d == "bullish" and sp < ep:
        return "wrong_direction_propagation"  # shouldn't be labeled invalid if ok
    if d == "bearish" and sp > ep:
        return "wrong_direction_propagation"

    if d == "bullish":
        # Stop should be below entry; failure means stop >= entry
        if mode == "beyond_sweep" or sweep_extreme is not None:
            ex = float(sweep_extreme) if sweep_extreme is not None else None
            if ex is not None and ep <= ex:
                return "entry_already_beyond_sweep_extreme"
        if mode == "beyond_fvg" or fvg_low is not None:
            if fvg_low is not None and ep <= float(fvg_low):
                return "entry_already_beyond_fvg_invalidation"
        if sweep_extreme is not None and ep <= float(sweep_extreme):
            return "entry_already_beyond_sweep_extreme"
        if fvg_low is not None and ep <= float(fvg_low):
            return "entry_already_beyond_fvg_invalidation"
    elif d == "bearish":
        if mode == "beyond_sweep" or sweep_extreme is not None:
            ex = float(sweep_extreme) if sweep_extreme is not None else None
            if ex is not None and ep >= ex:
                return "entry_already_beyond_sweep_extreme"
        if mode == "beyond_fvg" or fvg_high is not None:
            if fvg_high is not None and ep >= float(fvg_high):
                return "entry_already_beyond_fvg_invalidation"
        if sweep_extreme is not None and ep >= float(sweep_extreme):
            return "entry_already_beyond_sweep_extreme"
        if fvg_high is not None and ep >= float(fvg_high):
            return "entry_already_beyond_fvg_invalidation"
    else:
        return "wrong_direction_propagation"

    # Ambiguity on the trigger bar is secondary to geometric stop failure.
    if trigger_bar_also_hits_stop:
        return "same_trigger_bar_contains_stop_level"

    # Entry between FVG bounds but stop still not directional (buffer / extreme mismatch)
    if sweep_level is not None and sweep_extreme is not None:
        if abs(float(sweep_extreme) - float(sweep_level)) > 500:  # absurd gold move → artifact
            return "data_artifact"

    return "other"


def diagnose_invalid_stops(
    records: Sequence[SetupJournalRecord],
    *,
    default_stop_mode: str = "beyond_sweep",
) -> dict[str, Any]:
    """Build per-case diagnostics for invalid directional stops."""
    cases: list[dict[str, Any]] = []
    for r in records:
        reason = (r.invalidation_reason or "").lower()
        status = (r.status or "").upper()
        is_inv = status == "INVALIDATED" and (
            "stop_not_directional" in reason or "directional" in reason
        )
        # Also catch per-mode reasons embedded like "first_touch:stop_not_directional"
        mode_hits = [
            e
            for e in r.entry_results
            if e.triggered
            and (
                is_inv
                or (e.outcome == "NO_RISK_PLAN" and "stop_not_directional" in reason)
                or f"{e.mode}:stop_not_directional" in reason
            )
        ]
        if not mode_hits and is_inv:
            mode_hits = list(r.entry_results) or [None]  # type: ignore[list-item]

        stop_mode = default_stop_mode
        extras = r.extras or {}
        if extras.get("stop_mode"):
            stop_mode = str(extras["stop_mode"])

        for e in mode_hits:
            if e is None:
                continue
            # Prefer cases where this mode is implicated
            if e.mode and f"{e.mode}:" in reason and "stop_not_directional" not in reason.split(e.mode)[-1][:40]:
                # still include if overall invalid directional
                if "stop_not_directional" not in reason:
                    continue
            trig_amb = "TRIGGER_BAR_STOP_AMBIGUITY" in (e.ambiguity_flags or []) or (
                "TRIGGER_BAR_STOP_AMBIGUITY" in (r.reliability_flags or [])
            )
            cat = categorize_invalid_directional_stop(
                direction=r.direction,
                entry_price=e.entry_price,
                stop_price=e.stop_price,
                sweep_extreme=r.sweep_extreme,
                sweep_level=r.sweep_level,
                fvg_low=r.fvg_low,
                fvg_high=r.fvg_high,
                stop_mode=stop_mode,
                entry_timestamp=e.entry_timestamp,
                sweep_timestamp=r.sweep_timestamp,
                fvg_created_timestamp=r.fvg_created_timestamp,
                trigger_bar_also_hits_stop=trig_amb and e.stop_price is not None,
            )
            # Infer stop from geometry when missing on NO_RISK_PLAN path
            planned_stop = e.stop_price
            if planned_stop is None and r.sweep_extreme is not None and r.direction == "bullish":
                planned_stop = float(r.sweep_extreme)  # approximate; buffer omitted
            if planned_stop is None and r.sweep_extreme is not None and r.direction == "bearish":
                planned_stop = float(r.sweep_extreme)

            if "stop_not_directional" not in reason and not is_inv:
                continue

            cases.append(
                {
                    "setup_id": r.setup_id,
                    "liquidity_event_id": r.liquidity_event_id,
                    "session": r.session,
                    "direction": r.direction,
                    "execution_timeframe": r.execution_timeframe or r.timeframe,
                    "entry_mode": e.mode,
                    "stop_mode": stop_mode,
                    "entry_price": e.entry_price,
                    "fvg_low": r.fvg_low,
                    "fvg_high": r.fvg_high,
                    "sweep_level": r.sweep_level,
                    "sweep_extreme": r.sweep_extreme,
                    "planned_stop_price": planned_stop,
                    "reason_directional_validation_failed": "stop_not_directional",
                    "category": cat,
                    "bars_involved": {
                        "sweep_timestamp": r.sweep_timestamp,
                        "confirmation_timestamp": r.confirmation_timestamp,
                        "fvg_created_timestamp": r.fvg_created_timestamp,
                        "entry_timestamp": e.entry_timestamp,
                    },
                    "timestamps": {
                        "sweep": r.sweep_timestamp,
                        "choch": r.confirmation_timestamp,
                        "fvg": r.fvg_created_timestamp,
                        "entry": e.entry_timestamp,
                    },
                    "invalidation_reason_raw": r.invalidation_reason,
                }
            )

    by_cat = Counter(c["category"] for c in cases)
    by_session = defaultdict(Counter)
    by_tf = defaultdict(Counter)
    by_mode = defaultdict(Counter)
    for c in cases:
        by_session[str(c["session"])][c["category"]] += 1
        by_tf[str(c["execution_timeframe"])][c["category"]] += 1
        by_mode[str(c["entry_mode"])][c["category"]] += 1

    return {
        "case_count": len(cases),
        "by_category": dict(by_cat),
        "by_session": {k: dict(v) for k, v in by_session.items()},
        "by_execution_timeframe": {k: dict(v) for k, v in by_tf.items()},
        "by_entry_mode": {k: dict(v) for k, v in by_mode.items()},
        "cases": cases,
        "implementation_bug_suspected": any(
            c["category"]
            in (
                "wrong_direction_propagation",
                "bad_event_ordering",
                "sweep_extreme_after_assumed_event",
            )
            for c in cases
        ),
        "note": (
            "Categories are descriptive. Strategy stop rules unchanged unless a "
            "confirmed coding defect is proven."
        ),
    }
