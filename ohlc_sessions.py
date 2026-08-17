"""Internal deterministic Asia/London session ranges from OHLC bars."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Dict, Iterable, Optional, Sequence

from models import Bar, CoverageStatus, PRIMARY_SESSIONS, SessionRange
from session_time import (
    SessionDefinition,
    resolve_session_window,
    resolve_windows_overlapping_span,
)
from sessions_config import SESSION_DEFINITIONS


def compute_session_ranges(
    bars: Sequence[Bar],
    *,
    definitions: Optional[Dict[str, SessionDefinition]] = None,
    resolution_minutes: int = 5,
    now_ts: Optional[int] = None,
    names: Iterable[str] = PRIMARY_SESSIONS,
) -> list[SessionRange]:
    """
    Build SessionRange objects from OHLC bars using DST-aware SessionDefinitions.

    complete=True only when:
      - the session end is in the past, AND
      - loaded bars cover the resolved UTC window (start and end)
    Incomplete loaded-bar coverage is never treated as a completed session.

    Bars are treated as UTC timestamps. Chart display timezone is ignored.
    """
    if not bars:
        return []

    definitions = definitions or SESSION_DEFINITIONS
    now_ts = now_ts or int(datetime.now(tz=timezone.utc).timestamp())
    res_sec = max(1, int(resolution_minutes)) * 60
    sorted_bars = sorted(bars, key=lambda b: b.time)
    span_start, span_end = sorted_bars[0].time, sorted_bars[-1].time

    results: list[SessionRange] = []
    for name in names:
        definition = definitions.get(name)
        if definition is None:
            continue
        for window in resolve_windows_overlapping_span(
            definition, span_start, span_end + res_sec
        ):
            start_ts, end_ts = window.utc_start, window.utc_end
            session_bars = [b for b in sorted_bars if start_ts <= b.time < end_ts]

            if not session_bars:
                if end_ts <= span_start or start_ts >= span_end + res_sec:
                    continue
                results.append(
                    SessionRange(
                        name=name,
                        timezone=definition.reference_timezone,
                        start=start_ts,
                        end=end_ts,
                        high=None,
                        low=None,
                        high_timestamp=None,
                        low_timestamp=None,
                        complete=False,
                        source="internal_ohlc",
                        coverage_status=CoverageStatus.MISSING.value,
                        identity=f"{name}:{start_ts}",
                        extras={
                            "bar_count": 0,
                            "resolved_window": window.to_dict(),
                            "definition": definition.to_dict(),
                        },
                    )
                )
                continue

            high_bar = max(session_bars, key=lambda b: (b.high, -b.time))
            low_bar = min(session_bars, key=lambda b: (b.low, b.time))

            covers_start = session_bars[0].time <= start_ts + res_sec
            covers_end = session_bars[-1].time + res_sec >= end_ts

            if covers_start and covers_end:
                coverage = CoverageStatus.FULL.value
            elif not covers_start and not covers_end:
                coverage = CoverageStatus.PARTIAL.value
            elif not covers_start:
                coverage = CoverageStatus.PARTIAL_START.value
            else:
                coverage = CoverageStatus.PARTIAL_END.value

            session_ended = now_ts >= end_ts
            complete = bool(session_ended and coverage == CoverageStatus.FULL.value)

            results.append(
                SessionRange(
                    name=name,
                    timezone=definition.reference_timezone,
                    start=start_ts,
                    end=end_ts,
                    high=float(high_bar.high),
                    low=float(low_bar.low),
                    high_timestamp=int(high_bar.time),
                    low_timestamp=int(low_bar.time),
                    complete=complete,
                    source="internal_ohlc",
                    coverage_status=coverage,
                    identity=f"{name}:{start_ts}",
                    extras={
                        "bar_count": len(session_bars),
                        "covers_start": covers_start,
                        "covers_end": covers_end,
                        "session_ended": session_ended,
                        "resolution_minutes": resolution_minutes,
                        "resolved_window": window.to_dict(),
                        "definition": definition.to_dict(),
                    },
                )
            )

    results.sort(key=lambda r: (r.start or 0, r.name))
    return results


def latest_completed(
    ranges: Sequence[SessionRange], name: str
) -> Optional[SessionRange]:
    named = [r for r in ranges if r.name == name and r.complete and r.high is not None]
    return named[-1] if named else None


def latest_any(ranges: Sequence[SessionRange], name: str) -> Optional[SessionRange]:
    named = [r for r in ranges if r.name == name and r.high is not None]
    return named[-1] if named else None


def range_for_definition_date(
    bars: Sequence[Bar],
    definition: SessionDefinition,
    trading_date,
    *,
    resolution_minutes: int = 5,
    now_ts: Optional[int] = None,
) -> Optional[SessionRange]:
    """Compute one SessionRange for an explicit local trading date."""
    window = resolve_session_window(definition, trading_date)
    fake_span_bars = [
        Bar(time=window.utc_start - 1, open=0, high=0, low=0, close=0),
        Bar(time=window.utc_end + 1, open=0, high=0, low=0, close=0),
    ]
    # Use real bars only; temporary approach: filter compute results
    ranges = compute_session_ranges(
        bars,
        definitions={definition.name: definition},
        resolution_minutes=resolution_minutes,
        now_ts=now_ts,
        names=(definition.name,),
    )
    for r in ranges:
        if r.start == window.utc_start and r.end == window.utc_end:
            return r
    # If missing entirely due to no overlap, synthesize missing coverage row
    return SessionRange(
        name=definition.name,
        timezone=definition.reference_timezone,
        start=window.utc_start,
        end=window.utc_end,
        high=None,
        low=None,
        high_timestamp=None,
        low_timestamp=None,
        complete=False,
        source="internal_ohlc",
        coverage_status=CoverageStatus.MISSING.value,
        identity=f"{definition.name}:{window.utc_start}",
        extras={"resolved_window": window.to_dict(), "synthetic": True},
    )
