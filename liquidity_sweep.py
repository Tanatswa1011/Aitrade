"""Pure session-liquidity sweep detector (no TradingView I/O)."""

from __future__ import annotations

from typing import Optional, Sequence

from models import (
    PRIMARY_SESSIONS,
    Bar,
    LiquiditySweep,
    SessionRange,
    SweepRule,
    SweepSide,
)


def _is_primary(session: SessionRange) -> bool:
    return session.name in PRIMARY_SESSIONS and session.high is not None and session.low is not None


def _high_touch(bar: Bar, level: float) -> bool:
    return bar.high >= level


def _low_touch(bar: Bar, level: float) -> bool:
    return bar.low <= level


def _high_wick_only(bar: Bar, level: float) -> bool:
    # Took liquidity above the level, closed back at/below it.
    return bar.high > level and bar.close <= level


def _low_wick_only(bar: Bar, level: float) -> bool:
    return bar.low < level and bar.close >= level


def _high_reclaim(bar: Bar, level: float) -> bool:
    # Phase 2 skeleton: same-candle reclaim (wick beyond, close back inside).
    return _high_wick_only(bar, level)


def _low_reclaim(bar: Bar, level: float) -> bool:
    return _low_wick_only(bar, level)


def _matches(rule: SweepRule, side: SweepSide, bar: Bar, level: float) -> bool:
    if side == SweepSide.HIGH:
        if rule == SweepRule.TOUCH:
            return _high_touch(bar, level)
        if rule == SweepRule.WICK_ONLY:
            return _high_wick_only(bar, level)
        if rule == SweepRule.RECLAIM:
            return _high_reclaim(bar, level)
    else:
        if rule == SweepRule.TOUCH:
            return _low_touch(bar, level)
        if rule == SweepRule.WICK_ONLY:
            return _low_wick_only(bar, level)
        if rule == SweepRule.RECLAIM:
            return _low_reclaim(bar, level)
    return False


def _excursion(side: SweepSide, bar: Bar, level: float) -> float:
    if side == SweepSide.HIGH:
        return float(bar.high - level)
    return float(level - bar.low)


def _sweep_price(side: SweepSide, bar: Bar) -> float:
    return float(bar.high if side == SweepSide.HIGH else bar.low)


def detect_sweeps(
    session: SessionRange,
    bars: Sequence[Bar],
    *,
    rule: SweepRule | str = SweepRule.WICK_ONLY,
    sides: Optional[Sequence[SweepSide | str]] = None,
    search_from_ts: Optional[int] = None,
    search_to_ts: Optional[int] = None,
    first_only: bool = False,
) -> list[LiquiditySweep]:
    """
    Detect session High/Low liquidity sweeps from OHLC bars.

    Only Asia/London ranges with valid high/low are eligible.
    Search defaults to bars at/after session.end (or session.start if end unknown).
    """
    if not _is_primary(session):
        return []

    rule_e = SweepRule(rule)
    side_list = list(sides) if sides is not None else [SweepSide.HIGH, SweepSide.LOW]
    side_enums = [SweepSide(s) for s in side_list]

    if search_from_ts is None:
        # Sweeps usually happen after the session prints its range.
        search_from_ts = session.end if session.end is not None else session.start
    if search_from_ts is None:
        search_from_ts = bars[0].time if bars else 0

    sorted_bars = sorted(bars, key=lambda b: b.time)
    candidates = [
        b
        for b in sorted_bars
        if b.time >= search_from_ts and (search_to_ts is None or b.time <= search_to_ts)
    ]

    found: list[LiquiditySweep] = []
    for side in side_enums:
        level = float(session.high if side == SweepSide.HIGH else session.low)
        for bar in candidates:
            if not _matches(rule_e, side, bar, level):
                continue
            reclaim = bool(
                _high_reclaim(bar, level)
                if side == SweepSide.HIGH
                else _low_reclaim(bar, level)
            )
            found.append(
                LiquiditySweep(
                    session=session.name,
                    side=side.value,
                    level=level,
                    sweep_timestamp=int(bar.time),
                    sweep_price=_sweep_price(side, bar),
                    maximum_excursion=_excursion(side, bar, level),
                    reclaim_status=reclaim,
                    rule=rule_e.value,
                    sweep_candle=bar,
                    session_range=session.to_dict(),
                )
            )

    found.sort(key=lambda s: s.sweep_timestamp)
    if first_only:
        return found[:1]
    return found


def detect_first_sweep(
    session: SessionRange,
    bars: Sequence[Bar],
    *,
    rule: SweepRule | str = SweepRule.WICK_ONLY,
    side: Optional[SweepSide | str] = None,
) -> Optional[LiquiditySweep]:
    sides = [side] if side is not None else None
    sweeps = detect_sweeps(session, bars, rule=rule, sides=sides, first_only=True)
    return sweeps[0] if sweeps else None
