"""DST-aware session definitions and resolution."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Optional
from zoneinfo import ZoneInfo


def _parse_hhmm(token: str) -> time:
    raw = token.strip()
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
class SessionDefinition:
    """Canonical local-clock session definition (timezone-aware, DST-safe)."""

    name: str
    reference_timezone: str  # IANA, e.g. America/New_York
    local_start: str  # "HH:MM" or "HHMM"
    local_end: str
    source: str = "aitrade"
    notes: str = ""

    @property
    def start_time(self) -> time:
        return _parse_hhmm(self.local_start)

    @property
    def end_time(self) -> time:
        return _parse_hhmm(self.local_end)

    @property
    def crosses_midnight(self) -> bool:
        return self.end_time <= self.start_time

    def to_dict(self) -> dict[str, Any]:
        return {
            **asdict(self),
            "crosses_midnight": self.crosses_midnight,
        }


@dataclass(frozen=True)
class ResolvedSessionWindow:
    """One trading-date realization of a SessionDefinition in local + UTC."""

    session: str
    trading_date: str  # ISO date of the session's local start calendar day
    reference_timezone: str
    local_start_datetime: datetime
    local_end_datetime: datetime
    utc_start: int
    utc_end: int
    utc_offset_start: str
    utc_offset_end: str
    dst_active: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "session": self.session,
            "trading_date": self.trading_date,
            "reference_timezone": self.reference_timezone,
            "local_start_datetime": self.local_start_datetime.isoformat(),
            "local_end_datetime": self.local_end_datetime.isoformat(),
            "utc_start": self.utc_start,
            "utc_end": self.utc_end,
            "utc_offset_start": self.utc_offset_start,
            "utc_offset_end": self.utc_offset_end,
            "dst_active": self.dst_active,
        }


def _tz(name: str) -> ZoneInfo:
    if name.upper() in {"GMT", "UTC"}:
        return ZoneInfo("UTC")
    return ZoneInfo(name)


def _format_offset(dt: datetime) -> str:
    off = dt.utcoffset()
    if off is None:
        return "+00:00"
    total = int(off.total_seconds())
    sign = "+" if total >= 0 else "-"
    total = abs(total)
    hours, rem = divmod(total, 3600)
    minutes = rem // 60
    return f"{sign}{hours:02d}:{minutes:02d}"


def _is_dst(dt: datetime) -> bool:
    """True when DST is active (dst timedelta non-zero)."""
    dst = dt.dst()
    return bool(dst and dst.total_seconds() != 0)


def resolve_session_window(
    definition: SessionDefinition,
    trading_date: date,
) -> ResolvedSessionWindow:
    """
    Resolve a local session definition for a specific calendar date.

    trading_date is the local date of the session START.
    Conversion uses ZoneInfo rules for that exact date (no frozen offsets).
    Window is half-open [start, end) in UTC seconds.
    """
    tz = _tz(definition.reference_timezone)
    local_start = datetime.combine(trading_date, definition.start_time, tzinfo=tz)
    end_date = trading_date
    if definition.crosses_midnight:
        end_date = trading_date + timedelta(days=1)
    local_end = datetime.combine(end_date, definition.end_time, tzinfo=tz)

    utc_start_dt = local_start.astimezone(timezone.utc)
    utc_end_dt = local_end.astimezone(timezone.utc)

    return ResolvedSessionWindow(
        session=definition.name,
        trading_date=trading_date.isoformat(),
        reference_timezone=definition.reference_timezone,
        local_start_datetime=local_start,
        local_end_datetime=local_end,
        utc_start=int(utc_start_dt.timestamp()),
        utc_end=int(utc_end_dt.timestamp()),
        utc_offset_start=_format_offset(local_start),
        utc_offset_end=_format_offset(local_end),
        dst_active=_is_dst(local_start),
    )


def iter_trading_dates_for_bars(
    bars_start_ts: int,
    bars_end_ts: int,
    definition: SessionDefinition,
) -> list[date]:
    """Local start dates whose resolved windows may intersect the bar span."""
    tz = _tz(definition.reference_timezone)
    first = datetime.fromtimestamp(bars_start_ts, tz=timezone.utc).astimezone(tz).date()
    last = datetime.fromtimestamp(bars_end_ts, tz=timezone.utc).astimezone(tz).date()
    # Pad for midnight-crossing sessions.
    cursor = first - timedelta(days=2)
    end = last + timedelta(days=1)
    out: list[date] = []
    while cursor <= end:
        out.append(cursor)
        cursor += timedelta(days=1)
    return out


def resolve_windows_overlapping_span(
    definition: SessionDefinition,
    bars_start_ts: int,
    bars_end_ts: int,
) -> list[ResolvedSessionWindow]:
    windows = []
    for d in iter_trading_dates_for_bars(bars_start_ts, bars_end_ts, definition):
        w = resolve_session_window(definition, d)
        if w.utc_end <= bars_start_ts or w.utc_start >= bars_end_ts:
            continue
        windows.append(w)
    return windows
