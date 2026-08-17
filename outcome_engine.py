"""Historical entry outcome evaluation (OHLC only; fail-closed on ambiguity)."""

from __future__ import annotations

from typing import Any, Optional, Sequence

from journal_models import (
    OUTCOME_1R_HIT,
    OUTCOME_2R_HIT,
    OUTCOME_3R_HIT,
    OUTCOME_AMBIGUOUS_INTRABAR,
    OUTCOME_EXPIRED_WITHOUT_EXIT,
    OUTCOME_NO_RISK_PLAN,
    OUTCOME_NOT_TRIGGERED,
    OUTCOME_OPPOSITE_LIQUIDITY_HIT,
    OUTCOME_STOP_HIT,
    HistoricalEntryResult,
)
from models import TRIGGER_BAR_STOP_AMBIGUITY, Bar, EntryAnalysis

_RR_OUTCOME = {1.0: OUTCOME_1R_HIT, 2.0: OUTCOME_2R_HIT, 3.0: OUTCOME_3R_HIT}
_RR_RANK = {OUTCOME_1R_HIT: 1, OUTCOME_2R_HIT: 2, OUTCOME_3R_HIT: 3}


def _window(
    bars: Sequence[Bar], start_ts: int, end_ts: Optional[int]
) -> list[Bar]:
    ordered = sorted(bars, key=lambda b: int(b.time))
    out: list[Bar] = []
    for b in ordered:
        t = int(b.time)
        if t < int(start_ts):
            continue
        if end_ts is not None and t > int(end_ts):
            break
        out.append(b)
    return out


def _hit_stop(direction: str, bar: Bar, stop: float) -> bool:
    return bar.low <= stop if direction == "bullish" else bar.high >= stop


def _hit_target(direction: str, bar: Bar, price: float) -> bool:
    return bar.high >= price if direction == "bullish" else bar.low <= price


def _rr_label(rr: float) -> str:
    return _RR_OUTCOME.get(float(rr), f"{rr:g}R_HIT")


def evaluate_entry_outcome(
    analysis: EntryAnalysis,
    bars: Sequence[Bar],
    *,
    direction: str,
    horizon_end_ts: Optional[int],
    point_size: float = 1.0,
) -> HistoricalEntryResult:
    """
    Evaluate post-entry path on OHLC.

    Rules:
    - Same bar hits stop and any target/opposite → AMBIGUOUS_INTRABAR
    - Trigger bar also hits stop → AMBIGUOUS_INTRABAR + TRIGGER_BAR_STOP_AMBIGUITY
    - Otherwise first decisive event wins; RR may progress to a higher level
      before stop/opposite if hit on later bars
    - Horizon end / data end without exit → EXPIRED_WITHOUT_EXIT
    """
    entry = analysis.entry
    risk = analysis.risk
    target = analysis.target
    direction = direction or entry.direction

    fixed = []
    if target is not None:
        for ft in target.fixed_rr_targets:
            fixed.append(ft.to_dict() if hasattr(ft, "to_dict") else dict(ft))

    base_kwargs: dict[str, Any] = {
        "mode": entry.mode,
        "triggered": bool(entry.triggered and entry.status == "triggered"),
        "entry_price": entry.price,
        "entry_timestamp": entry.trigger_timestamp,
        "entry_depth": entry.entry_depth,
        "max_retrace_depth": entry.max_retrace_depth,
        "stop_price": None if risk is None else risk.stop_price,
        "risk_distance": None if risk is None else risk.risk_distance,
        "fixed_rr_targets": fixed,
        "opposite_liquidity_price": None
        if target is None
        else target.opposite_liquidity_price,
        "rr_to_opposite": None if target is None else target.rr_to_opposite,
    }

    if not base_kwargs["triggered"] or base_kwargs["entry_price"] is None:
        return HistoricalEntryResult(**base_kwargs, outcome=OUTCOME_NOT_TRIGGERED)

    if (
        risk is None
        or not risk.valid
        or risk.stop_price is None
        or not risk.risk_distance
        or base_kwargs["entry_timestamp"] is None
    ):
        flags = []
        return HistoricalEntryResult(
            **base_kwargs, outcome=OUTCOME_NO_RISK_PLAN, ambiguity_flags=flags
        )

    entry_px = float(base_kwargs["entry_price"])
    stop = float(risk.stop_price)
    risk_dist = float(risk.risk_distance)
    ps = float(point_size) if point_size > 0 else 1.0
    opposite = base_kwargs["opposite_liquidity_price"]

    rr_levels: list[tuple[str, float]] = []
    for ft in fixed:
        if ft.get("price") is None or ft.get("rr") is None:
            continue
        rr_levels.append((_rr_label(float(ft["rr"])), float(ft["price"])))
    # Near → far for progression tracking
    rr_levels.sort(key=lambda x: abs(x[1] - entry_px))

    bars_w = _window(bars, int(base_kwargs["entry_timestamp"]), horizon_end_ts)
    if not bars_w:
        return HistoricalEntryResult(
            **base_kwargs, outcome=OUTCOME_EXPIRED_WITHOUT_EXIT
        )

    trigger = bars_w[0]
    if int(trigger.time) == int(base_kwargs["entry_timestamp"]) and _hit_stop(
        direction, trigger, stop
    ):
        return HistoricalEntryResult(
            **base_kwargs,
            outcome=OUTCOME_AMBIGUOUS_INTRABAR,
            exit_timestamp=int(trigger.time),
            ambiguity_flags=["TRIGGER_BAR_STOP_AMBIGUITY"],
            event_timestamps={"ambiguous_at": int(trigger.time)},
            extras={"note": TRIGGER_BAR_STOP_AMBIGUITY},
        )

    mfe = 0.0
    mae = 0.0
    events: dict[str, Any] = {}
    highest_rr: Optional[str] = None
    exit_ts: Optional[int] = None
    outcome = OUTCOME_EXPIRED_WITHOUT_EXIT
    ambiguity: list[str] = []

    for bar in bars_w:
        if direction == "bullish":
            mfe = max(mfe, float(bar.high) - entry_px)
            mae = max(mae, entry_px - float(bar.low))
        else:
            mfe = max(mfe, entry_px - float(bar.low))
            mae = max(mae, float(bar.high) - entry_px)

        stop_hit = _hit_stop(direction, bar, stop)
        hit_labels = [lab for lab, px in rr_levels if _hit_target(direction, bar, px)]
        opp_hit = opposite is not None and _hit_target(
            direction, bar, float(opposite)
        )

        if stop_hit and (hit_labels or opp_hit):
            ambiguity.append("AMBIGUOUS_INTRABAR_STOP_AND_TARGET")
            outcome = OUTCOME_AMBIGUOUS_INTRABAR
            exit_ts = int(bar.time)
            events["ambiguous_at"] = exit_ts
            break

        if stop_hit:
            # If we already banked RR on earlier bars, still report STOP as final
            # path end — prior RR timestamps remain in events.
            if highest_rr is not None:
                events["stopped_after_partial_rr"] = highest_rr
            outcome = OUTCOME_STOP_HIT
            exit_ts = int(bar.time)
            events["stop_hit_at"] = exit_ts
            break

        if opp_hit:
            outcome = OUTCOME_OPPOSITE_LIQUIDITY_HIT
            exit_ts = int(bar.time)
            events["opposite_hit_at"] = exit_ts
            for lab in hit_labels:
                events.setdefault("rr_hits", {})[lab] = exit_ts
            break

        if hit_labels:
            best = max(hit_labels, key=lambda lab: _RR_RANK.get(lab, 0))
            for lab in hit_labels:
                events.setdefault("rr_hits", {})[lab] = int(bar.time)
            if highest_rr is None or _RR_RANK.get(best, 0) >= _RR_RANK.get(
                highest_rr, 0
            ):
                highest_rr = best
                outcome = best
                exit_ts = int(bar.time)
            # Continue — may reach higher RR or stop later

    return HistoricalEntryResult(
        **base_kwargs,
        outcome=outcome,
        max_favorable_excursion=mfe,
        max_adverse_excursion=mae,
        mfe_r=(mfe / risk_dist) if risk_dist else None,
        mae_r=(mae / risk_dist) if risk_dist else None,
        mfe_points=mfe / ps,
        mae_points=mae / ps,
        exit_timestamp=exit_ts,
        ambiguity_flags=ambiguity,
        event_timestamps=events,
    )
