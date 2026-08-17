"""Deterministic OHLC fixtures for historical replay (no CDP)."""

from __future__ import annotations

from datetime import date, timedelta
from typing import List

from models import Bar
from session_time import resolve_session_window
from sessions_config import SESSION_DEFINITIONS


def _bars_covering_window(utc_start: int, utc_end: int, step: int = 300) -> list[Bar]:
    """Flat-ish bars covering [start, end) so session coverage is FULL."""
    out: list[Bar] = []
    t = utc_start
    px = 4330.0
    while t < utc_end:
        out.append(Bar(time=t, open=px, high=px + 1, low=px - 1, close=px))
        t += step
        px += 0.05
    # Ensure last bar near end
    if not out or out[-1].time + step < utc_end:
        out.append(
            Bar(
                time=max(utc_start, utc_end - step),
                open=px,
                high=px + 1,
                low=px - 1,
                close=px,
            )
        )
    return out


def build_multi_day_fixture_bars(
    start: date = date(2026, 8, 12), days: int = 3
) -> List[Bar]:
    """
    Bars spanning several Asia/London windows with FULL coverage.

    Includes one explicit post-Asia low sweep wick after first Asia end.
    """
    bars: list[Bar] = []
    for offset in range(days):
        d = start + timedelta(days=offset)
        asia = resolve_session_window(SESSION_DEFINITIONS["Asia"], d)
        london = resolve_session_window(
            SESSION_DEFINITIONS["London"], d + timedelta(days=1)
        )
        # Also include London on calendar day d for overlap completeness
        london_d = resolve_session_window(SESSION_DEFINITIONS["London"], d)
        for w in (asia, london_d, london):
            bars.extend(_bars_covering_window(w.utc_start, w.utc_end))

    # Deduplicate by time
    by_t = {b.time: b for b in bars}
    ordered = [by_t[t] for t in sorted(by_t)]

    # Inject a low sweep after first Asia
    asia0 = resolve_session_window(SESSION_DEFINITIONS["Asia"], start)
    session_low = min(
        b.low for b in ordered if asia0.utc_start <= b.time < asia0.utc_end
    )
    sweep_t = asia0.utc_end + 300
    ordered.append(
        Bar(
            time=sweep_t,
            open=session_low + 2,
            high=session_low + 3,
            low=session_low - 2,
            close=session_low + 1,
        )
    )
    ordered.sort(key=lambda b: b.time)
    return ordered


def bullish_chain_bars(base_ts: int = 1_000_000) -> list[Bar]:
    """Classic bullish sweep→structure→FVG→retrace path (legacy relative times)."""
    return [
        Bar(time=base_ts + 0, open=4320, high=4325, low=4315, close=4322),
        Bar(time=base_ts + 1000, open=4312, high=4313, low=4310, close=4312),
        Bar(time=base_ts + 2000, open=4315, high=4322, low=4314, close=4320),
        Bar(time=base_ts + 3000, open=4321, high=4325, low=4320, close=4324),
        Bar(time=base_ts + 4000, open=4324, high=4340, low=4323, close=4338),
        Bar(time=base_ts + 5000, open=4338, high=4345, low=4330, close=4342),
        Bar(time=base_ts + 6000, open=4342, high=4343, low=4328, close=4329),
        Bar(time=base_ts + 7000, open=4329, high=4330, low=4326, close=4327),
    ]
