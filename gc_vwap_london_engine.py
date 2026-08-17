"""Phase 27 London VWAP engine — thin wrappers over shared Phase 25 math."""

from __future__ import annotations

from typing import Any, Optional, Sequence

from gc_vwap_engine import (
    analyze_candidate,
    collect_all_sequences,
    collect_extension_sequences,
    compute_session_vwap_series,
    config_hash,
    evaluate_vwap_touch_after_entry,
    session_window,
    setup_to_entry_analysis,
    time_to_vwap_touch,
    trading_dates_for_session,
    typical_price,
)
from gc_vwap_london_models import LONDON_SESSION, STRATEGY_FAMILY
from gc_vwap_models import GCVWAPStrategyConfig, SessionVWAPState
from models import Bar

# Re-export for callers
__all__ = [
    "LONDON_SESSION",
    "analyze_candidate",
    "collect_all_london_sequences",
    "collect_london_extension_sequences",
    "compute_london_vwap_series",
    "config_hash",
    "evaluate_london_vwap_touch_after_entry",
    "london_session_window",
    "london_trading_dates",
    "setup_to_entry_analysis",
    "time_to_vwap_touch",
    "typical_price",
]


def london_session_window(trading_date: str) -> tuple[int, int, int]:
    return session_window(trading_date, LONDON_SESSION)


def london_trading_dates(bars: Sequence[Bar]) -> list[str]:
    return trading_dates_for_session(bars, LONDON_SESSION)


def compute_london_vwap_series(bars: Sequence[Bar], trading_date: str) -> list[SessionVWAPState]:
    return compute_session_vwap_series(bars, trading_date, session=LONDON_SESSION)


def collect_london_extension_sequences(
    bars: Sequence[Bar],
    trading_date: str,
    *,
    roll_flags: Optional[set[int]] = None,
    sigma: float = 2.0,
) -> list[dict[str, Any]]:
    return collect_extension_sequences(
        bars, trading_date, roll_flags=roll_flags, sigma=sigma, session=LONDON_SESSION
    )


def collect_all_london_sequences(bars: Sequence[Bar]) -> list[dict[str, Any]]:
    return collect_all_sequences(bars, session=LONDON_SESSION)


def evaluate_london_vwap_touch_after_entry(
    *,
    bars: Sequence[Bar],
    trading_date: str,
    entry_ts: int,
    direction: str,
    stop_price: float,
    session_end: int,
) -> dict[str, Any]:
    return evaluate_vwap_touch_after_entry(
        bars=bars,
        trading_date=trading_date,
        entry_ts=entry_ts,
        direction=direction,
        stop_price=stop_price,
        session_end=session_end,
        session=LONDON_SESSION,
    )


def analyze_london_candidate(seq: dict[str, Any], cfg: GCVWAPStrategyConfig):
    """Ensure London family tag on config; reuse shared analyze_candidate."""
    if cfg.strategy_family != STRATEGY_FAMILY:
        # frozen dataclass — callers should pass PHASE27 configs
        pass
    return analyze_candidate(seq, cfg)
