"""Higher-timeframe bias models (architecture only — no final bias algorithm)."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Optional


class BiasDirection(str, Enum):
    BULLISH = "bullish"
    BEARISH = "bearish"
    NEUTRAL = "neutral"
    UNKNOWN = "unknown"


class HtfAlignment(str, Enum):
    ALIGNED_BULLISH = "aligned_bullish"
    ALIGNED_BEARISH = "aligned_bearish"
    MIXED = "mixed"
    PARTIAL = "partial"
    UNKNOWN = "unknown"


class DirectionRelation(str, Enum):
    ALIGNED = "aligned"
    OPPOSED = "opposed"
    NEUTRAL = "neutral"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class TimeframeBias:
    timeframe: str  # canonical 1D | 4H
    direction: str  # BiasDirection value
    timestamp: Optional[int]  # as-of / bar time used
    source: str
    method: str
    confidence: str = "unknown"
    evidence: dict[str, Any] = field(default_factory=dict)
    valid: bool = True
    extras: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class HigherTimeframeContext:
    daily_bias: TimeframeBias
    h4_bias: TimeframeBias
    alignment: str
    evaluated_at: Optional[int]
    source_metadata: dict[str, Any] = field(default_factory=dict)
    extras: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "daily_bias": self.daily_bias.to_dict(),
            "h4_bias": self.h4_bias.to_dict(),
            "alignment": self.alignment,
            "evaluated_at": self.evaluated_at,
            "source_metadata": dict(self.source_metadata),
            "extras": dict(self.extras),
        }


def compute_htf_alignment(daily: str, h4: str) -> str:
    d = (daily or BiasDirection.UNKNOWN.value).lower()
    h = (h4 or BiasDirection.UNKNOWN.value).lower()
    if d == BiasDirection.UNKNOWN.value or h == BiasDirection.UNKNOWN.value:
        if d == BiasDirection.UNKNOWN.value and h == BiasDirection.UNKNOWN.value:
            return HtfAlignment.UNKNOWN.value
        return HtfAlignment.PARTIAL.value
    if d == BiasDirection.NEUTRAL.value or h == BiasDirection.NEUTRAL.value:
        if d == h:
            return HtfAlignment.PARTIAL.value
        return HtfAlignment.PARTIAL.value
    if d == h == BiasDirection.BULLISH.value:
        return HtfAlignment.ALIGNED_BULLISH.value
    if d == h == BiasDirection.BEARISH.value:
        return HtfAlignment.ALIGNED_BEARISH.value
    return HtfAlignment.MIXED.value


def setup_vs_bias(setup_direction: Optional[str], bias_direction: str) -> str:
    s = (setup_direction or "").lower()
    b = (bias_direction or BiasDirection.UNKNOWN.value).lower()
    if not s or b in (BiasDirection.UNKNOWN.value, BiasDirection.NEUTRAL.value):
        return DirectionRelation.UNKNOWN.value if b == BiasDirection.UNKNOWN.value else DirectionRelation.NEUTRAL.value
    if s == b:
        return DirectionRelation.ALIGNED.value
    if s in (BiasDirection.BULLISH.value, BiasDirection.BEARISH.value) and b in (
        BiasDirection.BULLISH.value,
        BiasDirection.BEARISH.value,
    ):
        return DirectionRelation.OPPOSED.value
    return DirectionRelation.UNKNOWN.value


def unknown_timeframe_bias(timeframe: str, *, evaluated_at: Optional[int] = None) -> TimeframeBias:
    return TimeframeBias(
        timeframe=timeframe,
        direction=BiasDirection.UNKNOWN.value,
        timestamp=evaluated_at,
        source="none",
        method="unspecified",
        confidence="unknown",
        evidence={},
        valid=True,
        extras={"note": "Bias algorithm not defined in Phase 11"},
    )


def unknown_htf_context(evaluated_at: Optional[int] = None) -> HigherTimeframeContext:
    daily = unknown_timeframe_bias("1D", evaluated_at=evaluated_at)
    h4 = unknown_timeframe_bias("4H", evaluated_at=evaluated_at)
    return HigherTimeframeContext(
        daily_bias=daily,
        h4_bias=h4,
        alignment=HtfAlignment.UNKNOWN.value,
        evaluated_at=evaluated_at,
        source_metadata={"provider": "unknown"},
        extras={},
    )
