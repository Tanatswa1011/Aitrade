"""Gap classification for OHLC datasets (no interpolation)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional, Sequence
from zoneinfo import ZoneInfo

from models import Bar
from trading_day_config import DEFAULT_TRADING_DAY_CONFIG, TradingDayConfig, is_weekend


# Typical FX/CFD daily maintenance window around NY roll (heuristic; not a hard rule).
_DEFAULT_MAINTENANCE_MAX_SEC = 2 * 3600


def classify_gap(
    from_ts: int,
    to_ts: int,
    *,
    expected_period_sec: int,
    trading_day: Optional[TradingDayConfig] = None,
    tz_name: str = "America/New_York",
    maintenance_max_sec: int = _DEFAULT_MAINTENANCE_MAX_SEC,
) -> dict[str, Any]:
    """
    Classify a timestamp gap between consecutive bars.

    Categories:
      - expected_weekend_or_market_closure
      - known_daily_maintenance_break
      - unexpected_missing_interval
    """
    cfg = trading_day or DEFAULT_TRADING_DAY_CONFIG
    delta = int(to_ts) - int(from_ts)
    missing = max(0, (delta // expected_period_sec) - 1) if expected_period_sec > 0 else 0
    tz = ZoneInfo(tz_name)
    start = datetime.fromtimestamp(int(from_ts), tz=timezone.utc).astimezone(tz)
    end = datetime.fromtimestamp(int(to_ts), tz=timezone.utc).astimezone(tz)

    # Weekend: Friday evening → Sunday / Monday open spans Sat/Sun.
    crosses_weekend = False
    if start.date() != end.date():
        d = start.date()
        while d <= end.date():
            if is_weekend(d):
                crosses_weekend = True
                break
            from datetime import timedelta

            d = d + timedelta(days=1)
    if start.weekday() >= 4 and end.weekday() <= 1 and delta >= 24 * 3600:
        crosses_weekend = True

    roll_h, roll_m = cfg.roll_clock.hour, cfg.roll_clock.minute
    near_roll = (
        abs((start.hour * 60 + start.minute) - (roll_h * 60 + roll_m)) <= 90
        or abs((end.hour * 60 + end.minute) - (roll_h * 60 + roll_m)) <= 90
    )

    if crosses_weekend and delta >= 12 * 3600:
        category = "expected_weekend_or_market_closure"
    elif (
        near_roll
        and expected_period_sec <= 900
        and expected_period_sec < delta <= maintenance_max_sec
    ):
        category = "known_daily_maintenance_break"
    elif delta == expected_period_sec:
        category = "none"
    else:
        category = "unexpected_missing_interval"

    return {
        "from": int(from_ts),
        "to": int(to_ts),
        "delta_sec": delta,
        "expected_period_sec": expected_period_sec,
        "missing_intervals": missing,
        "category": category,
        "quality_failure": category == "unexpected_missing_interval",
        "local_from": start.isoformat(),
        "local_to": end.isoformat(),
    }


def classify_bar_gaps(
    bars: Sequence[Bar],
    *,
    expected_period_sec: int,
    trading_day: Optional[TradingDayConfig] = None,
    tz_name: str = "America/New_York",
) -> dict[str, Any]:
    ordered = sorted(bars, key=lambda b: int(b.time))
    gaps: list[dict[str, Any]] = []
    for i in range(1, len(ordered)):
        dt = int(ordered[i].time) - int(ordered[i - 1].time)
        if dt == expected_period_sec:
            continue
        if dt <= 0:
            gaps.append(
                {
                    "from": int(ordered[i - 1].time),
                    "to": int(ordered[i].time),
                    "delta_sec": dt,
                    "expected_period_sec": expected_period_sec,
                    "category": "unexpected_missing_interval",
                    "quality_failure": True,
                    "note": "non_positive_delta",
                }
            )
            continue
        gaps.append(
            classify_gap(
                int(ordered[i - 1].time),
                int(ordered[i].time),
                expected_period_sec=expected_period_sec,
                trading_day=trading_day,
                tz_name=tz_name,
            )
        )

    by_cat: dict[str, int] = {}
    for g in gaps:
        by_cat[g["category"]] = by_cat.get(g["category"], 0) + 1
    unexpected = [g for g in gaps if g.get("quality_failure")]
    return {
        "gap_count": len(gaps),
        "by_category": by_cat,
        "unexpected_count": len(unexpected),
        "quality_ok": len(unexpected) == 0,
        "gaps": gaps,
        "unexpected_head": unexpected[:30],
    }
