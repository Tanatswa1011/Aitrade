"""Canonical timeframe identifiers and TradingView resolution normalization."""

from __future__ import annotations

from enum import Enum
from typing import Optional


class Timeframe(str, Enum):
    M1 = "1m"
    M5 = "5m"
    M15 = "15m"
    H1 = "1H"
    H4 = "4H"
    D1 = "1D"


BIAS_TIMEFRAMES = (Timeframe.D1.value, Timeframe.H4.value)
EXECUTION_TIMEFRAMES = (Timeframe.M5.value, Timeframe.M15.value)

# Seconds for closed-candle checks (approximate for native TV bars; open-time based).
TIMEFRAME_SECONDS = {
    Timeframe.M1.value: 60,
    Timeframe.M5.value: 300,
    Timeframe.M15.value: 900,
    Timeframe.H1.value: 3600,
    Timeframe.H4.value: 14400,
    Timeframe.D1.value: 86400,
}


def normalize_timeframe(raw: Optional[str]) -> Optional[str]:
    """
    Normalize TradingView / user timeframe labels to canonical ids.

    Examples: "5" → "5m", "15" → "15m", "240" → "4H", "1D"/"D" → "1D"
    """
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    key = s.upper().replace(" ", "")

    aliases = {
        "1": Timeframe.M1.value,
        "1M": Timeframe.M1.value,
        "5": Timeframe.M5.value,
        "5M": Timeframe.M5.value,
        "15": Timeframe.M15.value,
        "15M": Timeframe.M15.value,
        "60": Timeframe.H1.value,
        "1H": Timeframe.H1.value,
        "H1": Timeframe.H1.value,
        "240": Timeframe.H4.value,
        "4H": Timeframe.H4.value,
        "H4": Timeframe.H4.value,
        "1D": Timeframe.D1.value,
        "D": Timeframe.D1.value,
        "D1": Timeframe.D1.value,
        "DAY": Timeframe.D1.value,
        "DAILY": Timeframe.D1.value,
    }
    if key in aliases:
        return aliases[key]
    # already canonical lowercase m / mixed
    low = s.lower()
    for tf in Timeframe:
        if low == tf.value.lower():
            return tf.value
    return None


def timeframe_seconds(tf: str) -> Optional[int]:
    canon = normalize_timeframe(tf) or tf
    return TIMEFRAME_SECONDS.get(canon)


def is_execution_timeframe(tf: str) -> bool:
    return normalize_timeframe(tf) in EXECUTION_TIMEFRAMES


def is_bias_timeframe(tf: str) -> bool:
    return normalize_timeframe(tf) in BIAS_TIMEFRAMES
