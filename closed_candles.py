"""Closed-candle HTF helpers — prevent look-ahead in historical replay."""

from __future__ import annotations

from typing import Optional, Sequence

from models import Bar
from timeframe import normalize_timeframe, timeframe_seconds
from trading_day_config import (
    DEFAULT_TRADING_DAY_CONFIG,
    TradingDayConfig,
    daily_bar_close_ts,
)


def bar_close_ts(
    bar: Bar,
    timeframe: str,
    *,
    trading_day: Optional[TradingDayConfig] = None,
    next_bar_open_ts: Optional[int] = None,
) -> Optional[int]:
    """
    Earliest unix second when this bar is considered closed.

    For 1D: NY trading-day boundary (ZoneInfo), not open+86400.
    Prefer next_bar_open_ts when provided (native series).
    For other TFs: open + period seconds.
    """
    tf = normalize_timeframe(timeframe) or timeframe
    if tf in ("1D", "D", "1d"):
        cfg = trading_day or DEFAULT_TRADING_DAY_CONFIG
        return daily_bar_close_ts(
            int(bar.time),
            cfg=cfg,
            next_bar_open_ts=next_bar_open_ts,
        )
    period = timeframe_seconds(tf)
    if period is None:
        return None
    return int(bar.time) + int(period)


def filter_closed_bars(
    bars: Sequence[Bar],
    *,
    as_of_ts: int,
    timeframe: str,
    trading_day: Optional[TradingDayConfig] = None,
) -> list[Bar]:
    """
    Return bars fully closed at or before as_of_ts.

    Incomplete / still-forming candles are excluded (no look-ahead).
    For Daily, uses successive native opens as close when available.
    """
    tf = normalize_timeframe(timeframe) or timeframe
    ordered = sorted(bars, key=lambda x: int(x.time))
    out: list[Bar] = []
    for i, b in enumerate(ordered):
        nxt = int(ordered[i + 1].time) if i + 1 < len(ordered) else None
        close_at = bar_close_ts(
            b,
            tf,
            trading_day=trading_day,
            next_bar_open_ts=nxt if tf in ("1D", "D", "1d") else None,
        )
        if close_at is None:
            continue
        if close_at <= int(as_of_ts):
            out.append(b)
    return out


def latest_closed_bar(
    bars: Sequence[Bar],
    *,
    as_of_ts: int,
    timeframe: str,
    trading_day: Optional[TradingDayConfig] = None,
) -> Optional[Bar]:
    closed = filter_closed_bars(
        bars, as_of_ts=as_of_ts, timeframe=timeframe, trading_day=trading_day
    )
    return closed[-1] if closed else None
