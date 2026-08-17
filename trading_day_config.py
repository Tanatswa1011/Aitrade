"""NY trading-day Daily bar boundaries (DST-aware; no naive +86400)."""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Optional, Sequence
from zoneinfo import ZoneInfo

from models import Bar

ALGORITHM_VERSION = "ny_trading_day_v1"

# Common FX/CFD daily roll; confirmed/overridden from native TV opens when available.
DEFAULT_DAY_ROLL_TIME = "17:00"
DEFAULT_REFERENCE_TIMEZONE = "America/New_York"


def _parse_hhmm(token: str) -> time:
    raw = (token or "").strip()
    if len(raw) == 5 and raw[2] == ":":
        hour, minute = int(raw[:2]), int(raw[3:])
    elif len(raw) == 4 and raw.isdigit():
        hour, minute = int(raw[:2]), int(raw[2:])
    else:
        raise ValueError(f"Invalid HH:MM / HHMM time: {token!r}")
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f"Out-of-range time: {token!r}")
    return time(hour=hour, minute=minute)


@dataclass(frozen=True)
class TradingDayConfig:
    """
    Canonical Daily trading-day boundary.

    reference_timezone + day_roll_time define open/close via ZoneInfo (DST-safe).
    weekend_policy:
      - skip_fabricate: never invent Sat/Sun Daily bars without observed data
      - roll_calendar: successive rolls advance one local calendar day (Fri→Sat→Sun→Mon)
    """

    reference_timezone: str = DEFAULT_REFERENCE_TIMEZONE
    day_roll_time: str = DEFAULT_DAY_ROLL_TIME
    weekend_policy: str = "skip_fabricate"
    source: str = "fx_session_hypothesis"
    algorithm_version: str = ALGORITHM_VERSION
    extras: dict[str, Any] = field(default_factory=dict)

    @property
    def roll_clock(self) -> time:
        return _parse_hhmm(self.day_roll_time)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


DEFAULT_TRADING_DAY_CONFIG = TradingDayConfig(
    source="fx_session_hypothesis_pending_native_confirm",
    extras={
        "note": (
            "Default 17:00 America/New_York matches typical FX/CFD Daily rolls. "
            "Prefer infer_trading_day_config_from_native_bars() / "
            "daily_boundary_evidence when native 1D opens exist."
        ),
    },
)


def load_confirmed_trading_day_from_evidence(
    path: str | Path = "data/daily_boundary_evidence.json",
) -> TradingDayConfig:
    """Load confirmed evidence file if present; else return provisional default."""
    p = Path(path)
    if not p.exists():
        return DEFAULT_TRADING_DAY_CONFIG
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return DEFAULT_TRADING_DAY_CONFIG
    if raw.get("status") != "confirmed" or not raw.get("inferred_roll_time"):
        return TradingDayConfig(
            day_roll_time=raw.get("inferred_roll_time") or DEFAULT_DAY_ROLL_TIME,
            source="fx_session_hypothesis_pending_native_confirm",
            extras={"evidence_status": raw.get("status"), "loaded_from": str(p)},
        )
    return TradingDayConfig(
        reference_timezone=raw.get("inferred_timezone") or DEFAULT_REFERENCE_TIMEZONE,
        day_roll_time=str(raw["inferred_roll_time"]),
        weekend_policy="skip_fabricate",
        source="native_tv_daily_opens_confirmed",
        extras={"evidence_status": "confirmed", "loaded_from": str(p)},
    )


def _tz(name: str) -> ZoneInfo:
    if name.upper() in {"GMT", "UTC"}:
        return ZoneInfo("UTC")
    return ZoneInfo(name)


def local_roll_datetime(cfg: TradingDayConfig, on_date: date) -> datetime:
    """Local wall-clock roll instant on a calendar date (DST handled by ZoneInfo)."""
    tz = _tz(cfg.reference_timezone)
    return datetime(
        on_date.year,
        on_date.month,
        on_date.day,
        cfg.roll_clock.hour,
        cfg.roll_clock.minute,
        tzinfo=tz,
    )


def trading_day_open_utc(cfg: TradingDayConfig, trading_date: date) -> int:
    """UTC unix open for the Daily bar labeled by trading_date (roll on that date)."""
    return int(local_roll_datetime(cfg, trading_date).astimezone(timezone.utc).timestamp())


def trading_day_close_utc(cfg: TradingDayConfig, trading_date: date) -> int:
    """
    UTC unix close for the Daily bar that opened on trading_date.

    Close = next calendar day's roll (not +86400). Automatically DST-aware:
    spring-forward days are ~23h UTC; fall-back ~25h UTC.
    """
    nxt = trading_date + timedelta(days=1)
    return trading_day_open_utc(cfg, nxt)


def is_weekend(d: date) -> bool:
    return d.weekday() >= 5  # Sat=5 Sun=6


def iter_trading_dates(
    start: date,
    end: date,
    *,
    cfg: TradingDayConfig = DEFAULT_TRADING_DAY_CONFIG,
) -> list[date]:
    """
    Calendar dates that may host a Daily open roll.

    With skip_fabricate we still list Mon–Fri opens by default for iteration helpers;
    Sat/Sun are omitted so callers do not fabricate weekend Daily bars.
    """
    out: list[date] = []
    d = start
    while d <= end:
        if cfg.weekend_policy == "skip_fabricate" and is_weekend(d):
            d += timedelta(days=1)
            continue
        out.append(d)
        d += timedelta(days=1)
    return out


def trading_date_for_ts(ts: int, cfg: TradingDayConfig = DEFAULT_TRADING_DAY_CONFIG) -> date:
    """
    Which trading-day bar is active at unix ts?

    Bar for date D covers [roll(D), roll(D+1)).
    A timestamp exactly on roll(D) belongs to day D.
    """
    tz = _tz(cfg.reference_timezone)
    local = datetime.fromtimestamp(int(ts), tz=timezone.utc).astimezone(tz)
    roll_today = local_roll_datetime(cfg, local.date())
    if local < roll_today:
        return local.date() - timedelta(days=1)
    return local.date()


def daily_bar_close_ts(
    bar_open_ts: int,
    *,
    cfg: TradingDayConfig = DEFAULT_TRADING_DAY_CONFIG,
    next_bar_open_ts: Optional[int] = None,
) -> int:
    """
    Canonical Daily close for a bar with given open timestamp.

    Prefer next native bar open when provided (matches TV series boundaries).
    Otherwise resolve via NY roll on the bar's trading date → next calendar roll.
    """
    if next_bar_open_ts is not None:
        return int(next_bar_open_ts)
    td = trading_date_for_ts(int(bar_open_ts), cfg)
    # If bar open is exactly on a roll, trading_date_for_ts returns that date.
    # Close is always the following calendar day's roll (DST-aware duration).
    open_local = datetime.fromtimestamp(int(bar_open_ts), tz=timezone.utc).astimezone(
        _tz(cfg.reference_timezone)
    )
    # Snap to the roll that matches this bar open when within 1 minute of roll.
    roll = local_roll_datetime(cfg, open_local.date())
    if abs((open_local - roll).total_seconds()) <= 60:
        td = open_local.date()
    return trading_day_close_utc(cfg, td)


def infer_trading_day_config_from_native_bars(
    bars: Sequence[Bar],
    *,
    reference_timezone: str = DEFAULT_REFERENCE_TIMEZONE,
    min_samples: int = 5,
) -> TradingDayConfig:
    """
    Infer day_roll_time from native Daily bar open timestamps (mode of local HH:MM).

    Does not invent a timezone; uses the provided IANA zone for interpretation.
    """
    tz = _tz(reference_timezone)
    counts: Counter[str] = Counter()
    samples: list[dict[str, Any]] = []
    ordered = sorted(bars, key=lambda b: int(b.time))
    for b in ordered:
        local = datetime.fromtimestamp(int(b.time), tz=timezone.utc).astimezone(tz)
        hhmm = f"{local.hour:02d}:{local.minute:02d}"
        counts[hhmm] += 1
        samples.append(
            {
                "time": int(b.time),
                "local": local.isoformat(),
                "hhmm": hhmm,
                "utc_offset": local.utcoffset().total_seconds() if local.utcoffset() else None,
            }
        )

    if sum(counts.values()) < min_samples or not counts:
        return TradingDayConfig(
            reference_timezone=reference_timezone,
            day_roll_time=DEFAULT_DAY_ROLL_TIME,
            source="insufficient_native_samples",
            extras={
                "sample_count": sum(counts.values()),
                "fallback_roll": DEFAULT_DAY_ROLL_TIME,
                "samples_head": samples[:5],
            },
        )

    mode_hhmm, mode_n = counts.most_common(1)[0]
    # Spot DST: same HH:MM local across EST/EDT implies fixed local roll.
    offsets = {
        s["utc_offset"]
        for s in samples
        if s["hhmm"] == mode_hhmm and s["utc_offset"] is not None
    }
    return TradingDayConfig(
        reference_timezone=reference_timezone,
        day_roll_time=mode_hhmm,
        source="native_tv_daily_opens",
        extras={
            "sample_count": sum(counts.values()),
            "mode_count": mode_n,
            "hhmm_histogram": dict(counts),
            "distinct_utc_offsets_at_mode": sorted(offsets),
            "dst_aware_local_roll": len(offsets) > 1,
            "samples_head": samples[:3],
            "samples_tail": samples[-3:],
        },
    )


def describe_daily_boundaries(
    bars: Sequence[Bar],
    *,
    cfg: Optional[TradingDayConfig] = None,
) -> dict[str, Any]:
    """Diagnostics for native Daily open/close relationships."""
    ordered = sorted(bars, key=lambda b: int(b.time))
    cfg = cfg or infer_trading_day_config_from_native_bars(ordered)
    rows = []
    for i, b in enumerate(ordered):
        nxt = int(ordered[i + 1].time) if i + 1 < len(ordered) else None
        close_native = nxt
        close_cfg = daily_bar_close_ts(int(b.time), cfg=cfg, next_bar_open_ts=None)
        local_open = datetime.fromtimestamp(int(b.time), tz=timezone.utc).astimezone(
            _tz(cfg.reference_timezone)
        )
        duration = None if nxt is None else int(nxt) - int(b.time)
        rows.append(
            {
                "open_ts": int(b.time),
                "open_local": local_open.isoformat(),
                "close_native_next_open": close_native,
                "close_cfg_next_roll": close_cfg,
                "duration_sec_native": duration,
                "weekday": local_open.strftime("%A"),
            }
        )
    return {
        "config": cfg.to_dict(),
        "bar_count": len(rows),
        "rows": rows,
        "weekend_bars": [
            r for r in rows if datetime.fromisoformat(r["open_local"]).weekday() >= 5
        ],
    }
