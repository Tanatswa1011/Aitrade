"""Deterministic OHLC resampling utility (optional; tagged source=resampled)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional, Sequence
from zoneinfo import ZoneInfo

from models import Bar
from multi_tf_bars import TimeframeBarSeries
from timeframe import Timeframe, normalize_timeframe, timeframe_seconds
from trading_day_config import (
    DEFAULT_TRADING_DAY_CONFIG,
    TradingDayConfig,
    daily_bar_close_ts,
    is_weekend,
    trading_date_for_ts,
    trading_day_open_utc,
)


def _bucket_start(
    ts: int,
    period_sec: int,
    *,
    tz_name: str = "UTC",
    trading_day: Optional[TradingDayConfig] = None,
) -> int:
    """Align timestamp down to period boundary in the given timezone."""
    cfg = trading_day or DEFAULT_TRADING_DAY_CONFIG
    if period_sec == 86400:
        td = trading_date_for_ts(int(ts), cfg)
        # Do not start a fabricated weekend bucket unless data lands there;
        # weekend_policy skip is enforced by caller omitting empty buckets.
        return trading_day_open_utc(cfg, td)
    # 4H: explicit NY trading-day open anchor (17:00 America/New_York), not
    # calendar-midnight / pandas default buckets.
    if period_sec == 14400:
        td = trading_date_for_ts(int(ts), cfg)
        day_open = trading_day_open_utc(cfg, td)
        offset = int(ts) - day_open
        if offset < 0:
            # Guard: belong to previous trading-day open.
            prev = trading_date_for_ts(day_open - 1, cfg)
            day_open = trading_day_open_utc(cfg, prev)
            offset = int(ts) - day_open
        return day_open + (offset // period_sec) * period_sec
    if tz_name.upper() in ("UTC", "GMT"):
        return (int(ts) // period_sec) * period_sec
    tz = ZoneInfo(tz_name)
    dt = datetime.fromtimestamp(int(ts), tz=timezone.utc).astimezone(tz)
    local_midnight = datetime(dt.year, dt.month, dt.day, tzinfo=tz)
    midnight_utc = int(local_midnight.astimezone(timezone.utc).timestamp())
    offset = int(ts) - midnight_utc
    aligned = midnight_utc + (offset // period_sec) * period_sec
    return aligned


def resample_ohlc(
    bars: Sequence[Bar],
    target_timeframe: str,
    *,
    source_timeframe: str = "5m",
    tz_name: str = "America/New_York",
    as_of_ts: Optional[int] = None,
    trading_day: Optional[TradingDayConfig] = None,
) -> TimeframeBarSeries:
    """
    Aggregate lower-TF OHLC into higher TF.

    Daily aggregation uses TradingDayConfig roll (not calendar midnight)
    unless a different boundary is proven. Incomplete final bucket omitted
    when as_of_ts is set. Weekend Daily bars are not fabricated without data.
    """
    target = normalize_timeframe(target_timeframe)
    if target is None:
        raise ValueError(f"unknown target timeframe: {target_timeframe!r}")
    period = timeframe_seconds(target)
    if period is None:
        raise ValueError(f"no period for {target}")

    cfg = trading_day or DEFAULT_TRADING_DAY_CONFIG
    buckets: dict[int, list[Bar]] = {}
    for b in sorted(bars, key=lambda x: int(x.time)):
        start = _bucket_start(
            int(b.time), period, tz_name=tz_name, trading_day=cfg
        )
        buckets.setdefault(start, []).append(b)

    out: list[Bar] = []
    incomplete = 0
    skipped_weekend_empty = 0
    for start in sorted(buckets):
        group = buckets[start]
        if target == Timeframe.D1.value:
            close_at = daily_bar_close_ts(start, cfg=cfg, next_bar_open_ts=None)
            # skip_fabricate: if somehow only weekend-labeled with no real session
            # we still keep observed buckets (data existed); do not invent empties.
            local = datetime.fromtimestamp(start, tz=timezone.utc).astimezone(
                ZoneInfo(cfg.reference_timezone)
            )
            if (
                cfg.weekend_policy == "skip_fabricate"
                and is_weekend(local.date())
                and not group
            ):
                skipped_weekend_empty += 1
                continue
        else:
            close_at = start + period
        if as_of_ts is not None and close_at > int(as_of_ts):
            incomplete += 1
            continue
        out.append(
            Bar(
                time=start,
                open=float(group[0].open),
                high=max(float(b.high) for b in group),
                low=min(float(b.low) for b in group),
                close=float(group[-1].close),
                volume=None,
            )
        )

    h4_anchor = None
    if target == Timeframe.H4.value:
        h4_anchor = {
            "mode": "ny_trading_day_open",
            "reference_timezone": cfg.reference_timezone,
            "day_roll_time": cfg.day_roll_time,
            "note": (
                "4H buckets align to trading-day open (default 17:00 America/New_York) "
                "then step +4H; not calendar midnight."
            ),
        }

    return TimeframeBarSeries(
        timeframe=target,
        bars=tuple(out),
        source="resampled",
        extras={
            "source_timeframe": normalize_timeframe(source_timeframe) or source_timeframe,
            "tz_name": tz_name,
            "incomplete_buckets_omitted": incomplete,
            "skipped_weekend_empty": skipped_weekend_empty,
            "as_of_ts": as_of_ts,
            "trading_day": cfg.to_dict(),
            "daily_boundary": "ny_trading_day" if target == Timeframe.D1.value else None,
            "h4_anchor": h4_anchor,
        },
    )
