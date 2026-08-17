"""Phase 21 liquidity-reclaim models (indicator-free; isolated from CHoCH/FVG)."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Optional

STRATEGY_FAMILY = "liquidity_reclaim_v1"
STRATEGY_VERSION = "v1.phase21"
LEGACY_FAMILY = "session_sweep_choch_fvg"


class ConfirmationMode(str, Enum):
    IMMEDIATE_RECLAIM = "immediate_reclaim"
    CONFIRMATION_CANDLE = "confirmation_candle"
    SWEEP_CANDLE_BREAK = "sweep_candle_break"


class BreakMode(str, Enum):
    CLOSE_BREAK = "close_break"
    WICK_BREAK = "wick_break"


class EntryMode(str, Enum):
    CONFIRMATION_CLOSE = "confirmation_close"
    LIQUIDITY_RETEST = "liquidity_retest"
    SWEEP_MIDPOINT = "sweep_midpoint"


class ReclaimState(str, Enum):
    WAITING_FOR_SESSION = "WAITING_FOR_SESSION"
    SESSION_RANGE_COMPLETE = "SESSION_RANGE_COMPLETE"
    WAITING_FOR_SWEEP = "WAITING_FOR_SWEEP"
    LIQUIDITY_SWEPT = "LIQUIDITY_SWEPT"
    WAITING_FOR_RECLAIM = "WAITING_FOR_RECLAIM"
    RECLAIMED = "RECLAIMED"
    WAITING_FOR_CONFIRMATION = "WAITING_FOR_CONFIRMATION"
    CONFIRMED = "CONFIRMED"
    WAITING_FOR_ENTRY = "WAITING_FOR_ENTRY"
    ENTRY_READY = "ENTRY_READY"
    INVALIDATED = "INVALIDATED"
    EXPIRED = "EXPIRED"
    NO_SETUP = "NO_SETUP"


@dataclass(frozen=True)
class ReclaimStrategyConfig:
    """Frozen research config for one Phase 21 candidate."""

    strategy_family: str = STRATEGY_FAMILY
    confirmation_mode: str = ConfirmationMode.IMMEDIATE_RECLAIM.value
    break_mode: str = BreakMode.CLOSE_BREAK.value  # only for sweep_candle_break
    entry_mode: str = EntryMode.CONFIRMATION_CLOSE.value
    execution_timeframe: str = "5m"
    max_reclaim_bars: int = 3  # 0 = same bar only; 3 = sweep + next 3
    max_confirmation_bars: int = 12
    max_entry_bars: int = 12
    stop_mode: str = "beyond_sweep"
    stop_buffer: float = 0.0
    htf_policy: str = "no_hard_filter"
    candidate_id: str = "R0"
    extras: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class LiquidityReclaimEvent:
    liquidity_event_id: str
    symbol: str
    session: str
    trading_date: Optional[str]
    side: str  # high | low
    direction: str  # bullish | bearish
    liquidity_level: float
    sweep_timestamp: int
    sweep_extreme: float
    sweep_penetration: float
    reclaim_timestamp: Optional[int]
    reclaim_close: Optional[float]
    reclaim_bars_after_sweep: Optional[int]
    confirmation_mode: str
    confirmation_timestamp: Optional[int]
    confirmation_level: Optional[float]
    confirmation_break_mode: Optional[str]
    execution_timeframe: str
    source: str = "ohlc"
    session_range_high: Optional[float] = None
    session_range_low: Optional[float] = None
    sweep_candle: Optional[dict[str, Any]] = None
    reclaim_candle: Optional[dict[str, Any]] = None
    confirmation_candle: Optional[dict[str, Any]] = None
    state: str = ReclaimState.NO_SETUP.value
    reason: Optional[str] = None
    extras: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class LiquidityReclaimSetup:
    strategy_family: str
    setup_id: str
    liquidity_event_id: str
    symbol: str
    session: str
    trading_date: Optional[str]
    direction: Optional[str]
    execution_timeframe: str
    event: LiquidityReclaimEvent
    entry_mode: str
    entry_price: Optional[float]
    entry_timestamp: Optional[int]
    entry_triggered: bool
    stop_price: Optional[float]
    risk_distance: Optional[float]
    risk_valid: bool
    risk_invalidation_reason: Optional[str]
    targets: list[dict[str, Any]] = field(default_factory=list)
    opposite_liquidity_price: Optional[float] = None
    state: str = ReclaimState.NO_SETUP.value
    reason: Optional[str] = None
    htf_context: dict[str, Any] = field(default_factory=dict)
    candidate_id: Optional[str] = None
    extras: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["event"] = self.event.to_dict()
        return d


# Frozen Phase 21 candidate matrix (max 10). Defined before any HOLDOUT look.
PHASE21_CANDIDATES: tuple[ReclaimStrategyConfig, ...] = (
    ReclaimStrategyConfig(
        candidate_id="R1_5m_immediate_close",
        execution_timeframe="5m",
        confirmation_mode=ConfirmationMode.IMMEDIATE_RECLAIM.value,
        entry_mode=EntryMode.CONFIRMATION_CLOSE.value,
    ),
    ReclaimStrategyConfig(
        candidate_id="R2_5m_break_close",
        execution_timeframe="5m",
        confirmation_mode=ConfirmationMode.SWEEP_CANDLE_BREAK.value,
        break_mode=BreakMode.CLOSE_BREAK.value,
        entry_mode=EntryMode.CONFIRMATION_CLOSE.value,
    ),
    ReclaimStrategyConfig(
        candidate_id="R3_5m_break_retest",
        execution_timeframe="5m",
        confirmation_mode=ConfirmationMode.SWEEP_CANDLE_BREAK.value,
        break_mode=BreakMode.CLOSE_BREAK.value,
        entry_mode=EntryMode.LIQUIDITY_RETEST.value,
    ),
    ReclaimStrategyConfig(
        candidate_id="R4_5m_break_midpoint",
        execution_timeframe="5m",
        confirmation_mode=ConfirmationMode.SWEEP_CANDLE_BREAK.value,
        break_mode=BreakMode.CLOSE_BREAK.value,
        entry_mode=EntryMode.SWEEP_MIDPOINT.value,
    ),
    ReclaimStrategyConfig(
        candidate_id="R5_15m_immediate_close",
        execution_timeframe="15m",
        confirmation_mode=ConfirmationMode.IMMEDIATE_RECLAIM.value,
        entry_mode=EntryMode.CONFIRMATION_CLOSE.value,
    ),
    ReclaimStrategyConfig(
        candidate_id="R6_15m_break_close",
        execution_timeframe="15m",
        confirmation_mode=ConfirmationMode.SWEEP_CANDLE_BREAK.value,
        break_mode=BreakMode.CLOSE_BREAK.value,
        entry_mode=EntryMode.CONFIRMATION_CLOSE.value,
    ),
    ReclaimStrategyConfig(
        candidate_id="R7_15m_break_retest",
        execution_timeframe="15m",
        confirmation_mode=ConfirmationMode.SWEEP_CANDLE_BREAK.value,
        break_mode=BreakMode.CLOSE_BREAK.value,
        entry_mode=EntryMode.LIQUIDITY_RETEST.value,
    ),
    ReclaimStrategyConfig(
        candidate_id="R8_5m_confirm_candle",
        execution_timeframe="5m",
        confirmation_mode=ConfirmationMode.CONFIRMATION_CANDLE.value,
        entry_mode=EntryMode.CONFIRMATION_CLOSE.value,
    ),
)
