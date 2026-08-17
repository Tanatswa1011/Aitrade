"""Multi-timeframe bar containers (native vs resampled tagged)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional, Sequence

from models import Bar
from timeframe import Timeframe, normalize_timeframe


@dataclass(frozen=True)
class TimeframeBarSeries:
    timeframe: str
    bars: tuple[Bar, ...]
    source: str = "native"  # native | resampled
    extras: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "timeframe": self.timeframe,
            "bar_count": len(self.bars),
            "source": self.source,
            "extras": dict(self.extras),
        }


@dataclass(frozen=True)
class MultiTimeframeBars:
    """
    Explicit D / 4H / execution bar sets — never an unlabeled mixed list.

    Keys use canonical timeframe ids (1D, 4H, 5m, 15m).
    """

    series: dict[str, TimeframeBarSeries] = field(default_factory=dict)

    def get(self, timeframe: str) -> Optional[TimeframeBarSeries]:
        key = normalize_timeframe(timeframe) or timeframe
        return self.series.get(key)

    def bars_for(self, timeframe: str) -> tuple[Bar, ...]:
        s = self.get(timeframe)
        return () if s is None else s.bars

    def with_series(
        self,
        timeframe: str,
        bars: Sequence[Bar],
        *,
        source: str = "native",
        extras: Optional[dict[str, Any]] = None,
    ) -> "MultiTimeframeBars":
        key = normalize_timeframe(timeframe) or timeframe
        series = dict(self.series)
        series[key] = TimeframeBarSeries(
            timeframe=key,
            bars=tuple(sorted(bars, key=lambda b: int(b.time))),
            source=source,
            extras=dict(extras or {}),
        )
        return MultiTimeframeBars(series=series)

    def to_dict(self) -> dict[str, Any]:
        return {k: v.to_dict() for k, v in self.series.items()}


def empty_mtf() -> MultiTimeframeBars:
    return MultiTimeframeBars()
