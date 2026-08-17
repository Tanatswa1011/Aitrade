"""Bias providers — Manual / Unknown / Structure / Historical."""

from __future__ import annotations

from typing import Optional, Protocol, Sequence, Tuple

from bias_models import (
    BiasDirection,
    HigherTimeframeContext,
    TimeframeBias,
    compute_htf_alignment,
    unknown_htf_context,
    unknown_timeframe_bias,
)
from closed_candles import filter_closed_bars
from htf_bias_config import DEFAULT_HTF_BIAS_CONFIG, HTFBiasConfig
from htf_structure import compute_timeframe_structure_bias
from models import Bar


class BiasProvider(Protocol):
    name: str

    def get_context(
        self,
        *,
        as_of_ts: int,
        daily_bars: Sequence[Bar] = (),
        h4_bars: Sequence[Bar] = (),
    ) -> HigherTimeframeContext:
        ...


class UnknownBiasProvider:
    """Fallback: Daily/4H unknown — setup engine still runs."""

    name = "unknown"

    def get_context(
        self,
        *,
        as_of_ts: int,
        daily_bars: Sequence[Bar] = (),
        h4_bars: Sequence[Bar] = (),
    ) -> HigherTimeframeContext:
        del daily_bars, h4_bars
        return unknown_htf_context(evaluated_at=as_of_ts)


class ManualBiasProvider:
    """Deterministic injected Daily/4H bias for tests."""

    name = "manual"

    def __init__(
        self,
        *,
        daily: str = BiasDirection.UNKNOWN.value,
        h4: str = BiasDirection.UNKNOWN.value,
        confidence: str = "manual",
    ):
        self.daily = daily.lower()
        self.h4 = h4.lower()
        self.confidence = confidence

    def get_context(
        self,
        *,
        as_of_ts: int,
        daily_bars: Sequence[Bar] = (),
        h4_bars: Sequence[Bar] = (),
    ) -> HigherTimeframeContext:
        closed_d = filter_closed_bars(daily_bars, as_of_ts=as_of_ts, timeframe="1D")
        closed_h4 = filter_closed_bars(h4_bars, as_of_ts=as_of_ts, timeframe="4H")
        daily_bias = TimeframeBias(
            timeframe="1D",
            direction=self.daily,
            timestamp=as_of_ts,
            source="manual",
            method="manual_injection",
            confidence=self.confidence,
            evidence={
                "closed_daily_bars_available": len(closed_d),
                "latest_closed_daily_ts": None if not closed_d else int(closed_d[-1].time),
            },
            valid=True,
        )
        h4_bias = TimeframeBias(
            timeframe="4H",
            direction=self.h4,
            timestamp=as_of_ts,
            source="manual",
            method="manual_injection",
            confidence=self.confidence,
            evidence={
                "closed_h4_bars_available": len(closed_h4),
                "latest_closed_h4_ts": None if not closed_h4 else int(closed_h4[-1].time),
            },
            valid=True,
        )
        return HigherTimeframeContext(
            daily_bias=daily_bias,
            h4_bias=h4_bias,
            alignment=compute_htf_alignment(self.daily, self.h4),
            evaluated_at=as_of_ts,
            source_metadata={"provider": self.name},
            extras={},
        )


class StructureBiasProvider:
    """
    Explicit Daily + 4H market-structure bias (structure_break_v1).

    Soft context only — never rejects setups.
    """

    name = "structure_bias"

    def __init__(self, config: Optional[HTFBiasConfig] = None):
        self.config = config or DEFAULT_HTF_BIAS_CONFIG
        self._cache: dict[Tuple[str, int, str], TimeframeBias] = {}

    def _cached_bias(
        self,
        *,
        timeframe: str,
        as_of_ts: int,
        bars: Sequence[Bar],
    ) -> TimeframeBias:
        key = (
            timeframe,
            int(as_of_ts),
            self.config.algorithm_version,
        )
        # Include bar span in key to avoid wrong cache across datasets
        if bars:
            key = (
                timeframe,
                int(as_of_ts),
                f"{self.config.algorithm_version}:{len(bars)}:{int(bars[0].time)}:{int(bars[-1].time)}",
            )
        hit = self._cache.get(key)
        if hit is not None:
            return hit
        bias = compute_timeframe_structure_bias(
            bars,
            timeframe=timeframe,
            as_of_ts=as_of_ts,
            config=self.config,
        )
        self._cache[key] = bias
        return bias

    def get_context(
        self,
        *,
        as_of_ts: int,
        daily_bars: Sequence[Bar] = (),
        h4_bars: Sequence[Bar] = (),
    ) -> HigherTimeframeContext:
        daily = self._cached_bias(
            timeframe="1D", as_of_ts=as_of_ts, bars=daily_bars
        )
        h4 = self._cached_bias(timeframe="4H", as_of_ts=as_of_ts, bars=h4_bars)
        return HigherTimeframeContext(
            daily_bias=daily,
            h4_bias=h4,
            alignment=compute_htf_alignment(daily.direction, h4.direction),
            evaluated_at=as_of_ts,
            source_metadata={
                "provider": self.name,
                "algorithm_version": self.config.algorithm_version,
                "config": self.config.to_dict(),
            },
            extras={},
        )


class HistoricalBiasProvider:
    """Historical replay bias via structure_break_v1 (closed bars only)."""

    name = "historical_structure"

    def __init__(self, config: Optional[HTFBiasConfig] = None):
        self._inner = StructureBiasProvider(config)

    def get_context(
        self,
        *,
        as_of_ts: int,
        daily_bars: Sequence[Bar] = (),
        h4_bars: Sequence[Bar] = (),
    ) -> HigherTimeframeContext:
        ctx = self._inner.get_context(
            as_of_ts=as_of_ts, daily_bars=daily_bars, h4_bars=h4_bars
        )
        meta = dict(ctx.source_metadata)
        meta["provider"] = self.name
        return HigherTimeframeContext(
            daily_bias=ctx.daily_bias,
            h4_bias=ctx.h4_bias,
            alignment=ctx.alignment,
            evaluated_at=ctx.evaluated_at,
            source_metadata=meta,
            extras=dict(ctx.extras),
        )


def resolve_bias_provider(
    name: str = "structure",
    *,
    config: Optional[HTFBiasConfig] = None,
) -> BiasProvider:
    key = (name or "structure").strip().lower()
    if key in ("structure", "structure_bias", "structure_break_v1"):
        return StructureBiasProvider(config)
    if key in ("historical", "historical_structure", "historical_placeholder"):
        return HistoricalBiasProvider(config)
    if key == "manual":
        return ManualBiasProvider()
    if key == "unknown":
        return UnknownBiasProvider()
    return StructureBiasProvider(config)
