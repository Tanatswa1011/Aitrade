"""Pure risk / stop / pre-entry invalidation planning (no TradingView I/O)."""

from __future__ import annotations

from typing import Optional, Sequence

from models import (
    Bar,
    EntryCandidate,
    EntryStatus,
    FVGZone,
    LiquiditySweep,
    RiskConfig,
    RiskPlan,
    StopMode,
    StructureDirection,
    SweepSide,
)


IMPLEMENTED_STOP_MODES = (
    StopMode.BEYOND_SWEEP.value,
    StopMode.BEYOND_FVG.value,
)


def sweep_extreme(sweep: LiquiditySweep) -> float:
    """True excursion extreme of the sweep candle (not merely the session level)."""
    candle = sweep.sweep_candle
    if sweep.side == SweepSide.LOW.value or sweep.side == "low":
        return min(
            float(sweep.sweep_price),
            float(candle.low),
            float(sweep.level),
        )
    return max(
        float(sweep.sweep_price),
        float(candle.high),
        float(sweep.level),
    )


def _point_size(config: RiskConfig) -> float:
    ps = float(config.point_size)
    return ps if ps > 0 else 1.0


def compute_stop_price(
    sweep: LiquiditySweep,
    fvg: FVGZone,
    *,
    direction: str,
    stop_mode: str,
    buffer: float,
) -> tuple[Optional[float], Optional[str]]:
    """Return (stop_price, error_reason)."""
    if stop_mode == StopMode.BEYOND_SWEEP.value:
        extreme = sweep_extreme(sweep)
        if direction == StructureDirection.BULLISH.value:
            return extreme - buffer, None
        if direction == StructureDirection.BEARISH.value:
            return extreme + buffer, None
        return None, "invalid_direction"

    if stop_mode == StopMode.BEYOND_FVG.value:
        if direction == StructureDirection.BULLISH.value:
            return float(fvg.low) - buffer, None
        if direction == StructureDirection.BEARISH.value:
            return float(fvg.high) + buffer, None
        return None, "invalid_direction"

    if stop_mode in (
        StopMode.BEYOND_STRUCTURE.value,
        StopMode.FIXED_DISTANCE.value,
        StopMode.ATR.value,
    ):
        return None, f"stop_mode_not_implemented:{stop_mode}"

    return None, f"unknown_stop_mode:{stop_mode}"


def _directional_stop_ok(direction: str, entry: float, stop: float) -> bool:
    if direction == StructureDirection.BULLISH.value:
        return stop < entry
    if direction == StructureDirection.BEARISH.value:
        return stop > entry
    return False


def _violates_stop(direction: str, bar: Bar, stop_price: float) -> bool:
    if direction == StructureDirection.BULLISH.value:
        return float(bar.low) <= float(stop_price)
    return float(bar.high) >= float(stop_price)


def pre_entry_invalidation(
    *,
    direction: str,
    stop_price: float,
    fvg: FVGZone,
    entry: EntryCandidate,
    bars: Sequence[Bar],
) -> tuple[bool, Optional[int], Optional[dict]]:
    """
    True if any bar strictly after FVG creation and strictly before entry
    trigger violates the planned invalidation/stop level.
    """
    if entry.trigger_timestamp is None:
        return False, None, None

    created = int(fvg.created_timestamp)
    trigger = int(entry.trigger_timestamp)
    for bar in sorted(bars, key=lambda b: b.time):
        t = int(bar.time)
        if t <= created or t >= trigger:
            continue
        if _violates_stop(direction, bar, stop_price):
            return True, t, bar.to_dict()
    return False, None, None


def build_risk_plan(
    sweep: LiquiditySweep,
    fvg: FVGZone,
    entry_candidate: EntryCandidate,
    bars: Sequence[Bar],
    config: Optional[RiskConfig] = None,
) -> RiskPlan:
    """
    Build a stop/invalidation plan for one triggered EntryCandidate.

    Fail closed on untriggered entries, bad direction, or nonsensical stops.
    """
    cfg = config or RiskConfig()
    buffer = cfg.buffer_absolute
    setup_ref = dict(entry_candidate.setup_reference or fvg.setup_reference or {})
    setup_ref = {
        **setup_ref,
        "entry_mode": entry_candidate.mode,
        "entry_price": entry_candidate.price,
        "entry_trigger_timestamp": entry_candidate.trigger_timestamp,
        "fvg_fully_filled_meta": (entry_candidate.extras or {}).get(
            "fully_filled_before_or_at_entry"
        ),
    }

    def _invalid(
        reason: str,
        *,
        entry_price: Optional[float] = None,
        stop_price: Optional[float] = None,
        extras: Optional[dict] = None,
    ) -> RiskPlan:
        ep = (
            float(entry_price)
            if entry_price is not None
            else (
                float(entry_candidate.price)
                if entry_candidate.price is not None
                else 0.0
            )
        )
        return RiskPlan(
            direction=entry_candidate.direction or fvg.direction,
            stop_mode=str(cfg.stop_mode),
            entry_price=ep,
            stop_price=stop_price,
            risk_distance=None,
            risk_points=None,
            buffer=buffer,
            valid=False,
            invalidation_reason=reason,
            setup_reference=setup_ref,
            extras={
                "config": cfg.to_dict(),
                "sweep_extreme": sweep_extreme(sweep),
                **(extras or {}),
            },
        )

    if not entry_candidate.triggered or entry_candidate.status != EntryStatus.TRIGGERED.value:
        return _invalid("entry_not_triggered")

    if entry_candidate.price is None:
        return _invalid("missing_entry_price")

    direction = entry_candidate.direction
    if direction not in (
        StructureDirection.BULLISH.value,
        StructureDirection.BEARISH.value,
    ):
        return _invalid("invalid_direction")

    # Direction must align with sweep → confirmation chain.
    if direction == StructureDirection.BULLISH.value and sweep.side not in (
        SweepSide.LOW.value,
        "low",
    ):
        return _invalid("direction_sweep_mismatch")
    if direction == StructureDirection.BEARISH.value and sweep.side not in (
        SweepSide.HIGH.value,
        "high",
    ):
        return _invalid("direction_sweep_mismatch")

    entry_price = float(entry_candidate.price)
    stop_price, stop_err = compute_stop_price(
        sweep,
        fvg,
        direction=direction,
        stop_mode=str(cfg.stop_mode),
        buffer=buffer,
    )
    if stop_err or stop_price is None:
        return _invalid(stop_err or "stop_unavailable", entry_price=entry_price)

    if not _directional_stop_ok(direction, entry_price, float(stop_price)):
        return _invalid(
            "stop_not_directional",
            entry_price=entry_price,
            stop_price=float(stop_price),
            extras={"detail": "bullish requires stop < entry; bearish stop > entry"},
        )

    risk_distance = abs(entry_price - float(stop_price))
    if risk_distance <= 0:
        return _invalid(
            "non_positive_risk",
            entry_price=entry_price,
            stop_price=float(stop_price),
        )

    invalidated = False
    inv_ts = None
    inv_bar = None
    if cfg.invalidate_before_entry:
        invalidated, inv_ts, inv_bar = pre_entry_invalidation(
            direction=direction,
            stop_price=float(stop_price),
            fvg=fvg,
            entry=entry_candidate,
            bars=bars,
        )

    if invalidated:
        return RiskPlan(
            direction=direction,
            stop_mode=str(cfg.stop_mode),
            entry_price=entry_price,
            stop_price=float(stop_price),
            risk_distance=None,
            risk_points=None,
            buffer=buffer,
            valid=False,
            invalidation_reason="invalidated_before_entry",
            setup_reference=setup_ref,
            extras={
                "config": cfg.to_dict(),
                "sweep_extreme": sweep_extreme(sweep),
                "invalidation_timestamp": inv_ts,
                "invalidation_bar": inv_bar,
                "planned_stop_price": float(stop_price),
                "note": (
                    "Stop/invalidation level was violated after FVG creation and "
                    "before entry trigger; entry must not be treated as tradeable."
                ),
            },
        )

    return RiskPlan(
        direction=direction,
        stop_mode=str(cfg.stop_mode),
        entry_price=entry_price,
        stop_price=float(stop_price),
        risk_distance=risk_distance,
        risk_points=risk_distance / _point_size(cfg),
        buffer=buffer,
        valid=True,
        invalidation_reason=None,
        setup_reference=setup_ref,
        extras={
            "config": cfg.to_dict(),
            "sweep_extreme": sweep_extreme(sweep),
            "fvg_low": fvg.low,
            "fvg_high": fvg.high,
            "entry_mode": entry_candidate.mode,
            "entry_depth": entry_candidate.entry_depth,
            "max_retrace_depth": entry_candidate.max_retrace_depth,
            "fully_filled_before_or_at_entry": (entry_candidate.extras or {}).get(
                "fully_filled_before_or_at_entry"
            ),
        },
    )
