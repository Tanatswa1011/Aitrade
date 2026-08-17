"""Canonical DST-aware Asia/London session definitions.

ICT Sessions (VinceFxBT) exposes fixed-offset Timezone options (GMT, GMT+1, …)
and session strings Asia=0000-0700, London=0800-1330. Chart display timezone
must not drive strategy math.

Empirical matching (see phase2_5_calibration.json) shows ICT High/Low on
OANDA:XAUUSD are reproduced by America/New_York local clocks:

  Asia   20:00–03:00 America/New_York
  London 03:00–08:30 America/New_York

On a US-DST date these convert to UTC 00:00–07:00 and 07:00–12:30, which is
why London did not match labeled GMT 08:00–13:30 while Asia matched GMT
00:00–07:00. The NY model explains both with one DST-aware reference zone.
"""

from __future__ import annotations

from typing import Dict

from models import SessionName
from session_time import SessionDefinition


def parse_hhmm_range(text: str) -> tuple[int, int]:
    """Parse ICT-style '0800-1330' into (start_minute, end_minute)."""
    raw = (text or "").strip()
    left, _, right = raw.partition("-")
    if not left or not right:
        raise ValueError(f"Invalid session range string: {text!r}")

    def to_minute(token: str) -> int:
        token = token.strip()
        if len(token) != 4 or not token.isdigit():
            raise ValueError(f"Invalid HHMM token: {token!r}")
        hour = int(token[:2])
        minute = int(token[2:])
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError(f"Out-of-range HHMM token: {token!r}")
        return hour * 60 + minute

    return to_minute(left), to_minute(right)


# ICT indicator raw inputs (not the strategy canonical model).
ICT_INDICATOR_TIMEZONE_INPUT = "GMT"  # fixed-offset picker in VinceFxBT
ICT_INDICATOR_SESSION_STRINGS = {
    SessionName.ASIA.value: "0000-0700",
    SessionName.LONDON.value: "0800-1330",
    "New York": "1330-2100",
}

# Canonical AITRADE definitions (DST-aware via ZoneInfo).
SESSION_DEFINITIONS: Dict[str, SessionDefinition] = {
    SessionName.ASIA.value: SessionDefinition(
        name=SessionName.ASIA.value,
        reference_timezone="America/New_York",
        local_start="20:00",
        local_end="03:00",
        source="ict_empirical+classic_ict",
        notes=(
            "Reproduces ICT Asia High/Low. Equals UTC 00:00-07:00 only while "
            "US is on EDT; shifts with America/New_York DST."
        ),
    ),
    SessionName.LONDON.value: SessionDefinition(
        name=SessionName.LONDON.value,
        reference_timezone="America/New_York",
        local_start="03:00",
        local_end="08:30",
        source="ict_empirical+classic_ict",
        notes=(
            "Reproduces ICT London High/Low. Equals UTC 07:00-12:30 on US DST "
            "dates (not labeled GMT 08:00-13:30). Shifts with America/New_York DST."
        ),
    ),
}

SESSION_CONFIDENCE = {
    SessionName.ASIA.value: "high",
    SessionName.LONDON.value: "high",
}

# Phase 2.5 remaining uncertainty — do not remove.
SESSION_DST_UNCERTAINTY = (
    "Live ICT vs internal OHLC was validated on summer US-DST dates only "
    "(~3 Asia + 3 London with full bar coverage). Winter and US/EU DST-"
    "transition weeks were not live-confirmed against ICT drawings. "
    "The indicator timezone picker is fixed-offset GMT; the strategy uses "
    "America/New_York ZoneInfo. Re-validate ICT High/Low after loading "
    "history that spans winter and staggered US↔UK DST weeks before "
    "treating winter backtests as fully locked."
)
