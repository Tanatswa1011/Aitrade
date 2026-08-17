"""HTF swing detection + structure-break bias (separate from LTF CHoCH)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional, Sequence

from bias_models import BiasDirection, TimeframeBias
from closed_candles import bar_close_ts, filter_closed_bars
from htf_bias_config import DEFAULT_HTF_BIAS_CONFIG, HTFBiasConfig
from models import Bar
from timeframe import normalize_timeframe


@dataclass(frozen=True)
class SwingPoint:
    index: int
    time: int
    price: float
    kind: str  # high | low


@dataclass(frozen=True)
class StructureBreak:
    direction: str  # bullish | bearish
    level: float
    timestamp: int
    bar_index: int
    swing: SwingPoint


def _is_swing_high(bars: Sequence[Bar], i: int, left: int, right: int) -> bool:
    h = bars[i].high
    for j in range(i - left, i):
        if bars[j].high >= h:
            return False
    for j in range(i + 1, i + right + 1):
        if bars[j].high > h:
            return False
    return True


def _is_swing_low(bars: Sequence[Bar], i: int, left: int, right: int) -> bool:
    lo = bars[i].low
    for j in range(i - left, i):
        if bars[j].low <= lo:
            return False
    for j in range(i + 1, i + right + 1):
        if bars[j].low < lo:
            return False
    return True


def detect_confirmed_swings(
    bars: Sequence[Bar],
    *,
    left: int,
    right: int,
) -> list[SwingPoint]:
    """Fractal swings confirmed only after `right` bars exist."""
    left = max(1, int(left))
    right = max(1, int(right))
    ordered = sorted(bars, key=lambda b: int(b.time))
    n = len(ordered)
    out: list[SwingPoint] = []
    if n < left + right + 1:
        return out
    for i in range(left, n - right):
        if _is_swing_high(ordered, i, left, right):
            out.append(
                SwingPoint(
                    index=i,
                    time=int(ordered[i].time),
                    price=float(ordered[i].high),
                    kind="high",
                )
            )
        if _is_swing_low(ordered, i, left, right):
            out.append(
                SwingPoint(
                    index=i,
                    time=int(ordered[i].time),
                    price=float(ordered[i].low),
                    kind="low",
                )
            )
    out.sort(key=lambda s: (s.index, 0 if s.kind == "high" else 1))
    return out


def detect_structure_breaks(
    bars: Sequence[Bar],
    swings: Sequence[SwingPoint],
    *,
    left: int,
    right: int,
    require_close_break: bool = True,
) -> list[StructureBreak]:
    """
    Emit close-breaks of the most recent confirmed opposing swing.

    Wick-only breaks are ignored when require_close_break=True.
    """
    ordered = sorted(bars, key=lambda b: int(b.time))
    n = len(ordered)
    if not swings or n == 0:
        return []

    breaks: list[StructureBreak] = []
    last_sh: Optional[SwingPoint] = None
    last_sl: Optional[SwingPoint] = None
    sh_i = 0
    sl_i = 0
    highs = [s for s in swings if s.kind == "high"]
    lows = [s for s in swings if s.kind == "low"]
    broken_high_index: Optional[int] = None
    broken_low_index: Optional[int] = None

    for i in range(n):
        # Swing is usable only after right-side confirmation bar index.
        while sh_i < len(highs) and highs[sh_i].index + right <= i:
            last_sh = highs[sh_i]
            sh_i += 1
        while sl_i < len(lows) and lows[sl_i].index + right <= i:
            last_sl = lows[sl_i]
            sl_i += 1

        bar = ordered[i]
        if (
            last_sh is not None
            and i > last_sh.index + right
            and broken_high_index != last_sh.index
        ):
            level = last_sh.price
            broke = bar.close > level if require_close_break else bar.high > level
            # Wick-only explicitly excluded when require_close_break
            if require_close_break and not (bar.close > level):
                broke = False
            if broke:
                breaks.append(
                    StructureBreak(
                        direction=BiasDirection.BULLISH.value,
                        level=level,
                        timestamp=int(bar.time),
                        bar_index=i,
                        swing=last_sh,
                    )
                )
                broken_high_index = last_sh.index
        if (
            last_sl is not None
            and i > last_sl.index + right
            and broken_low_index != last_sl.index
        ):
            level = last_sl.price
            broke = bar.close < level if require_close_break else bar.low < level
            if require_close_break and not (bar.close < level):
                broke = False
            if broke:
                breaks.append(
                    StructureBreak(
                        direction=BiasDirection.BEARISH.value,
                        level=level,
                        timestamp=int(bar.time),
                        bar_index=i,
                        swing=last_sl,
                    )
                )
                broken_low_index = last_sl.index
    return breaks


def previous_day_levels(closed_daily: Sequence[Bar]) -> dict[str, Any]:
    """PDH/PDL/open/close from the latest fully closed Daily bar before as-of."""
    if len(closed_daily) < 1:
        return {}
    # "Previous day" relative to latest closed = that bar itself when evaluating
    # at a timestamp after it closed; expose last completed day OHLC.
    d = closed_daily[-1]
    out: dict[str, Any] = {
        "previous_day_open": float(d.open),
        "previous_day_high": float(d.high),
        "previous_day_low": float(d.low),
        "previous_day_close": float(d.close),
        "previous_day_timestamp": int(d.time),
    }
    if len(closed_daily) >= 2:
        # Prior completed day when latest closed is "today" relative to as_of —
        # still expose last two for context.
        p = closed_daily[-2]
        out["prior_day_open"] = float(p.open)
        out["prior_day_high"] = float(p.high)
        out["prior_day_low"] = float(p.low)
        out["prior_day_close"] = float(p.close)
        out["prior_day_timestamp"] = int(p.time)
    return out


def _confidence(
    *,
    bars_since_break: Optional[int],
    swing_highs: int,
    swing_lows: int,
    config: HTFBiasConfig,
) -> str:
    if bars_since_break is None:
        return "unknown"
    if swing_highs < 1 or swing_lows < 1:
        return "low"
    if bars_since_break <= config.confidence_high_max_bars:
        return "high"
    if bars_since_break <= config.confidence_medium_max_bars:
        return "medium"
    return "low"


def compute_timeframe_structure_bias(
    bars: Sequence[Bar],
    *,
    timeframe: str,
    as_of_ts: int,
    config: Optional[HTFBiasConfig] = None,
) -> TimeframeBias:
    """
    Structure-break bias for one HTF using only bars closed by as_of_ts.
    """
    cfg = config or DEFAULT_HTF_BIAS_CONFIG
    tf = normalize_timeframe(timeframe) or timeframe
    closed = filter_closed_bars(bars, as_of_ts=as_of_ts, timeframe=tf)
    left = cfg.swing_left(tf if tf in ("1D", "4H") else "4H")
    right = cfg.swing_right(tf if tf in ("1D", "4H") else "4H")
    # Use daily/h4 knobs explicitly
    if tf == "1D":
        left, right = cfg.daily_swing_left, cfg.daily_swing_right
    elif tf == "4H":
        left, right = cfg.h4_swing_left, cfg.h4_swing_right

    base_evidence: dict[str, Any] = {
        "closed_bars": len(closed),
        "as_of_ts": int(as_of_ts),
        "swing_left": left,
        "swing_right": right,
        "require_close_break": cfg.require_close_break,
        "algorithm_version": cfg.algorithm_version,
    }

    if len(closed) < cfg.min_closed_bars:
        return TimeframeBias(
            timeframe=tf,
            direction=BiasDirection.UNKNOWN.value,
            timestamp=as_of_ts,
            source="structure_bias",
            method=cfg.algorithm_version,
            confidence="unknown",
            evidence={**base_evidence, "reason": "insufficient_closed_bars"},
            valid=True,
        )

    swings = detect_confirmed_swings(closed, left=left, right=right)
    highs = [s for s in swings if s.kind == "high"]
    lows = [s for s in swings if s.kind == "low"]
    last_sh = highs[-1] if highs else None
    last_sl = lows[-1] if lows else None
    base_evidence.update(
        {
            "swing_high_count": len(highs),
            "swing_low_count": len(lows),
            "last_confirmed_swing_high": None if last_sh is None else last_sh.price,
            "last_confirmed_swing_high_time": None if last_sh is None else last_sh.time,
            "last_confirmed_swing_low": None if last_sl is None else last_sl.price,
            "last_confirmed_swing_low_time": None if last_sl is None else last_sl.time,
        }
    )

    if cfg.include_liquidity_context and tf == "1D":
        base_evidence["previous_day_levels"] = previous_day_levels(closed)
    if cfg.include_liquidity_context:
        base_evidence["recent_daily_swing_high" if tf == "1D" else "recent_h4_swing_high"] = (
            None if last_sh is None else last_sh.price
        )
        base_evidence["recent_daily_swing_low" if tf == "1D" else "recent_h4_swing_low"] = (
            None if last_sl is None else last_sl.price
        )

    if not highs and not lows:
        return TimeframeBias(
            timeframe=tf,
            direction=BiasDirection.UNKNOWN.value,
            timestamp=as_of_ts,
            source="structure_bias",
            method=cfg.algorithm_version,
            confidence="unknown",
            evidence={**base_evidence, "reason": "no_confirmed_swings"},
            valid=True,
        )

    breaks = detect_structure_breaks(
        closed,
        swings,
        left=left,
        right=right,
        require_close_break=cfg.require_close_break,
    )
    if not breaks:
        direction = (
            BiasDirection.NEUTRAL.value
            if cfg.neutral_when_unclear
            else BiasDirection.UNKNOWN.value
        )
        return TimeframeBias(
            timeframe=tf,
            direction=direction,
            timestamp=as_of_ts,
            source="structure_bias",
            method=cfg.algorithm_version,
            confidence="low" if direction == BiasDirection.NEUTRAL.value else "unknown",
            evidence={
                **base_evidence,
                "reason": "no_clear_structure_break",
                "bars_since_break": None,
            },
            valid=True,
        )

    last = breaks[-1]
    bars_since = len(closed) - 1 - last.bar_index
    conf = _confidence(
        bars_since_break=bars_since,
        swing_highs=len(highs),
        swing_lows=len(lows),
        config=cfg,
    )
    reason = (
        f"closed above confirmed {tf} swing high {last.level}"
        if last.direction == BiasDirection.BULLISH.value
        else f"closed below confirmed {tf} swing low {last.level}"
    )
    return TimeframeBias(
        timeframe=tf,
        direction=last.direction,
        timestamp=as_of_ts,
        source="structure_bias",
        method=cfg.algorithm_version,
        confidence=conf,
        evidence={
            **base_evidence,
            "reason": reason,
            "last_break_direction": last.direction,
            "last_break_level": last.level,
            "last_break_timestamp": last.timestamp,
            "last_break_bar_index": last.bar_index,
            "bars_since_break": bars_since,
            "structure_break_count": len(breaks),
            "latest_closed_bar_time": int(closed[-1].time),
            "latest_closed_bar_close": float(closed[-1].close),
        },
        valid=True,
        extras={"break_close_ts": bar_close_ts(closed[last.bar_index], tf)},
    )
