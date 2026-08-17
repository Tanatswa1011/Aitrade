"""Pure setup-linked Fair Value Gap detector (no TradingView I/O)."""

from __future__ import annotations

from typing import Optional, Sequence

from models import (
    Bar,
    FVGConfig,
    FVGDetectionResult,
    FVGZone,
    LiquiditySweep,
    StructureConfirmation,
    StructureDirection,
    StructureKind,
    SweepSide,
    TimingConfidence,
)
from structure_confirm import required_direction_for_sweep


def _point_size(config: FVGConfig) -> float:
    ps = float(config.point_size)
    return ps if ps > 0 else 1.0


def _gap_points(gap_size: float, config: FVGConfig) -> float:
    return float(gap_size) / _point_size(config)


def _passes_min_gap(gap_size: float, config: FVGConfig) -> bool:
    if gap_size < float(config.min_gap):
        return False
    if _gap_points(gap_size, config) < float(config.min_gap_points):
        return False
    return True


def _body(bar: Bar) -> float:
    return abs(float(bar.close) - float(bar.open))


def _range(bar: Bar) -> float:
    return max(0.0, float(bar.high) - float(bar.low))


def _displacement_ok(
    bars: Sequence[Bar],
    candle2_index: int,
    direction: str,
    config: FVGConfig,
) -> bool:
    """
    Optional simple displacement filter (not mandatory for v1).

    Rules when require_displacement=True:
    1. Candle 2 closes in setup direction.
    2. Body/range >= displacement_min_body_ratio (if range > 0).
    3. If displacement_body_lookback > 0: body >= mult * avg body of prior N bars.
    """
    if not config.require_displacement:
        return True

    c2 = bars[candle2_index]
    if direction == StructureDirection.BULLISH.value:
        if float(c2.close) < float(c2.open):
            return False
    else:
        if float(c2.close) > float(c2.open):
            return False

    rng = _range(c2)
    body = _body(c2)
    if rng > 0 and (body / rng) < float(config.displacement_min_body_ratio):
        return False

    lookback = int(config.displacement_body_lookback or 0)
    if lookback > 0 and candle2_index > 0:
        start = max(0, candle2_index - lookback)
        prior = bars[start:candle2_index]
        if prior:
            avg = sum(_body(b) for b in prior) / float(len(prior))
            if body < avg * float(config.displacement_body_vs_avg_mult):
                return False
    return True


def _bullish_fvg(c1: Bar, c3: Bar) -> Optional[tuple[float, float]]:
    # Candle 1 high < Candle 3 low
    if float(c1.high) < float(c3.low):
        low = float(c1.high)
        high = float(c3.low)
        return low, high
    return None


def _bearish_fvg(c1: Bar, c3: Bar) -> Optional[tuple[float, float]]:
    # Candle 1 low > Candle 3 high
    if float(c1.low) > float(c3.high):
        low = float(c3.high)
        high = float(c1.low)
        return low, high
    return None


def _mitigation_and_fill(
    direction: str,
    zone_low: float,
    zone_high: float,
    bars_after: Sequence[Bar],
) -> tuple[bool, Optional[int], bool, Optional[int]]:
    """
    Track first touch into the zone (mitigation) and first full fill.

    Bullish: mitigation when low <= zone_high (trades into/from above);
             full fill when low <= zone_low.
    Bearish: mitigation when high >= zone_low;
             full fill when high >= zone_high.
    """
    mitigated = False
    first_mit: Optional[int] = None
    fully = False
    first_fill: Optional[int] = None

    for bar in bars_after:
        if direction == StructureDirection.BULLISH.value:
            # Into zone from above: any trade at or below zone high.
            if float(bar.low) <= zone_high:
                if not mitigated:
                    mitigated = True
                    first_mit = int(bar.time)
                if float(bar.low) <= zone_low and not fully:
                    fully = True
                    first_fill = int(bar.time)
        else:
            if float(bar.high) >= zone_low:
                if not mitigated:
                    mitigated = True
                    first_mit = int(bar.time)
                if float(bar.high) >= zone_high and not fully:
                    fully = True
                    first_fill = int(bar.time)

        if fully:
            break

    return mitigated, first_mit, fully, first_fill


def _confirmation_anchor_time(confirmation: StructureConfirmation) -> Optional[int]:
    """FVG search starts after CHoCH; requires a usable event timestamp."""
    if confirmation.event_timestamp is not None:
        if confirmation.timing_confidence == TimingConfidence.UNAVAILABLE.value:
            return None
        return int(confirmation.event_timestamp)
    return None


def _build_setup_reference(
    sweep: LiquiditySweep,
    confirmation: StructureConfirmation,
) -> dict:
    return {
        "sequence": "sweep→CHoCH→FVG",
        "session": sweep.session,
        "sweep_side": sweep.side,
        "sweep_level": sweep.level,
        "sweep_timestamp": sweep.sweep_timestamp,
        "confirmation_kind": confirmation.kind,
        "confirmation_direction": confirmation.direction,
        "confirmation_level": confirmation.level,
        "confirmation_timestamp": confirmation.event_timestamp,
        "confirmation_raw_id": confirmation.raw_id,
        "confirmation_source": confirmation.source,
        "narrative": (
            f"{sweep.session} {sweep.side} swept @ {sweep.level} → "
            f"{confirmation.direction} {confirmation.kind} @ {confirmation.level} → "
            f"{confirmation.direction} FVG"
        ),
    }


def _count_bars_between(
    bars: Sequence[Bar], start_ts: int, end_ts: int
) -> Optional[int]:
    """Number of bars with start_ts < bar.time <= end_ts."""
    if end_ts <= start_ts:
        return 0
    return sum(1 for b in bars if start_ts < int(b.time) <= end_ts)


def detect_fvg(
    sweep: LiquiditySweep,
    confirmation: StructureConfirmation,
    bars: Sequence[Bar],
    config: Optional[FVGConfig] = None,
) -> FVGDetectionResult:
    """
    Detect Fair Value Gaps linked to an already-confirmed sweep → CHoCH setup.

    Fail closed if confirmation timing is unreliable or direction mismatches.
    FVG timestamps come only from OHLC bars.
    """
    cfg = config or FVGConfig()
    cfg_dict = cfg.to_dict()

    if confirmation.kind != StructureKind.CHOCH.value:
        return FVGDetectionResult(
            found=False,
            zones=[],
            reason="confirmation_not_choch",
            config=cfg_dict,
        )

    required = required_direction_for_sweep(sweep)
    if confirmation.direction != required:
        return FVGDetectionResult(
            found=False,
            zones=[],
            reason="confirmation_direction_mismatch",
            required_direction=required,
            config=cfg_dict,
        )

    # Fail closed: must prove CHoCH after sweep with a usable timestamp for FVG search.
    choch_ts = _confirmation_anchor_time(confirmation)
    if choch_ts is None:
        return FVGDetectionResult(
            found=False,
            zones=[],
            reason="confirmation_timing_unreliable",
            required_direction=required,
            config=cfg_dict,
        )

    if choch_ts <= sweep.sweep_timestamp:
        return FVGDetectionResult(
            found=False,
            zones=[],
            reason="confirmation_not_after_sweep",
            required_direction=required,
            config=cfg_dict,
        )

    if confirmation.direction not in (
        StructureDirection.BULLISH.value,
        StructureDirection.BEARISH.value,
    ):
        return FVGDetectionResult(
            found=False,
            zones=[],
            reason="invalid_direction",
            required_direction=required,
            config=cfg_dict,
        )

    direction = confirmation.direction
    sorted_bars = sorted(bars, key=lambda b: b.time)
    if len(sorted_bars) < 3:
        return FVGDetectionResult(
            found=False,
            zones=[],
            reason="insufficient_bars",
            required_direction=direction,
            config=cfg_dict,
        )

    # All three FVG candles must form after the CHoCH (and therefore after the sweep).
    post_conf = [
        b
        for b in sorted_bars
        if int(b.time) > choch_ts and int(b.time) > sweep.sweep_timestamp
    ]
    if cfg.max_bars_after_confirmation is not None:
        post_conf = post_conf[: int(cfg.max_bars_after_confirmation)]

    if len(post_conf) < 3:
        return FVGDetectionResult(
            found=False,
            zones=[],
            reason="no_bars_after_confirmation"
            if not post_conf
            else "insufficient_bars_after_confirmation",
            required_direction=direction,
            config=cfg_dict,
        )

    setup_ref = _build_setup_reference(sweep, confirmation)
    found_zones: list[FVGZone] = []

    for i in range(0, len(post_conf) - 2):
        c1 = post_conf[i]
        c2 = post_conf[i + 1]
        c3 = post_conf[i + 2]

        zone = None
        if direction == StructureDirection.BULLISH.value:
            zone = _bullish_fvg(c1, c3)
        else:
            zone = _bearish_fvg(c1, c3)

        if zone is None:
            continue

        zone_low, zone_high = zone
        gap_size = zone_high - zone_low
        if gap_size <= 0 or not _passes_min_gap(gap_size, cfg):
            continue

        # Displacement uses full series context around candle2.
        try:
            c2_global = next(
                j for j, b in enumerate(sorted_bars) if int(b.time) == int(c2.time)
            )
        except StopIteration:
            continue
        if not _displacement_ok(sorted_bars, c2_global, direction, cfg):
            continue

        bars_after = [b for b in sorted_bars if int(b.time) > int(c3.time)]
        mitigated, first_mit, fully, first_fill = _mitigation_and_fill(
            direction, zone_low, zone_high, bars_after
        )

        created = int(c3.time)
        fvg = FVGZone(
            direction=direction,
            low=zone_low,
            high=zone_high,
            midpoint=(zone_high + zone_low) / 2.0,
            created_timestamp=created,
            candle1_timestamp=int(c1.time),
            candle2_timestamp=int(c2.time),
            candle3_timestamp=int(c3.time),
            gap_size=gap_size,
            gap_points=_gap_points(gap_size, cfg),
            mitigated=mitigated,
            first_mitigation_timestamp=first_mit,
            fully_filled=fully,
            first_full_fill_timestamp=first_fill,
            bars_after_sweep=_count_bars_between(
                sorted_bars, sweep.sweep_timestamp, created
            ),
            bars_after_confirmation=_count_bars_between(
                sorted_bars, choch_ts, created
            ),
            setup_reference=setup_ref,
            source="internal_ohlc",
            extras={
                "candle1": c1.to_dict(),
                "candle2": c2.to_dict(),
                "candle3": c3.to_dict(),
                "post_conf_triplet_start": i,
            },
        )
        found_zones.append(fvg)

        if cfg.first_only:
            break

    if not found_zones:
        return FVGDetectionResult(
            found=False,
            zones=[],
            reason="no_fvg_after_confirmation",
            required_direction=direction,
            config=cfg_dict,
        )

    return FVGDetectionResult(
        found=True,
        zones=found_zones,
        reason="found",
        required_direction=direction,
        config=cfg_dict,
    )


def detect_first_fvg(
    sweep: LiquiditySweep,
    confirmation: StructureConfirmation,
    bars: Sequence[Bar],
    config: Optional[FVGConfig] = None,
) -> Optional[FVGZone]:
    """Convenience: first setup-linked FVG or None."""
    base = config or FVGConfig()
    result = detect_fvg(
        sweep,
        confirmation,
        bars,
        FVGConfig(**{**base.to_dict(), "first_only": True}),
    )
    return result.zones[0] if result.found else None
