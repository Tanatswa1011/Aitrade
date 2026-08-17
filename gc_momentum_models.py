"""Phase 28 — GC NY momentum / continuation models (isolated family)."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Optional

STRATEGY_FAMILY = "gc_ny_momentum_continuation_v1"
STRATEGY_VERSION = "v1.phase28"
INSTRUMENT = "GC"

OR_TIMEZONE = "America/New_York"
SESSION_START_LOCAL = "08:20"
SESSION_END_LOCAL = "13:30"
NO_NEW_SETUP_AFTER_LOCAL = "12:30"
SESSION_NOTE = (
    "NY research window 08:20–13:30 America/New_York — same clock as frozen V2 for comparison; "
    "momentum/continuation falsification, not mean reversion."
)

RANGE_MULTIPLIER = 1.5
RVOL_LOOKBACK = 20
RVOL_THRESHOLD = 1.5
MAX_ENTRY_BARS = 4
CLOSE_LOCATION_PCT = 0.25  # upper/lower 25% of range


class PullbackMode(str, Enum):
    NONE = "NONE"  # C0 control
    P1_HALF_RETRACE = "P1_HALF_RETRACE"
    P2_IMPULSE_OPEN = "P2_IMPULSE_OPEN"
    P3_BREAKOUT_RETEST = "P3_BREAKOUT_RETEST"


class EntryMode(str, Enum):
    IMPULSE_CLOSE = "IMPULSE_CLOSE"  # C0
    CONFIRMATION_CLOSE = "CONFIRMATION_CLOSE"  # M1
    CONFIRMATION_MIDPOINT_RETEST = "CONFIRMATION_MIDPOINT_RETEST"  # M2
    BREAKOUT_LEVEL_RETEST = "BREAKOUT_LEVEL_RETEST"  # M3


@dataclass(frozen=True)
class GCMomentumStrategyConfig:
    strategy_family: str = STRATEGY_FAMILY
    candidate_id: str = "C0"
    pullback_mode: str = PullbackMode.NONE.value
    entry_mode: str = EntryMode.IMPULSE_CLOSE.value
    volume_filter: bool = False
    rvol_threshold: float = RVOL_THRESHOLD
    range_multiplier: float = RANGE_MULTIPLIER
    max_entry_bars: int = MAX_ENTRY_BARS
    execution_timeframe: str = "5m"
    extras: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class GCMomentumImpulse:
    impulse_id: str
    trading_date: str
    direction: str  # bullish | bearish
    timestamp: int
    open: float
    high: float
    low: float
    close: float
    range_size: float
    median_range_20: float
    range_ratio: float
    rvol: Optional[float]
    breakout_level: float  # prior session high/low broken
    session_high_before: float
    session_low_before: float
    roll_artifact: bool = False
    extras: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class GCMomentumSetup:
    strategy_family: str
    setup_id: str
    impulse_id: str
    candidate_id: str
    trading_date: str
    direction: str
    pullback_mode: str
    entry_mode: str
    entry_price: Optional[float]
    entry_timestamp: Optional[int]
    entry_triggered: bool
    stop_price: Optional[float]
    risk_distance: Optional[float]
    risk_valid: bool
    risk_invalidation_reason: Optional[str]
    targets: list[dict[str, Any]] = field(default_factory=list)
    impulse: Optional[dict[str, Any]] = None
    state: str = "NO_SETUP"
    reason: Optional[str] = None
    extras: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# Max 8 candidates — base 5 + 3 RVOL duplicates of structural (C1–C3)
PHASE28_CANDIDATES: tuple[GCMomentumStrategyConfig, ...] = (
    GCMomentumStrategyConfig(
        candidate_id="C0_IMPULSE_IMMEDIATE",
        pullback_mode=PullbackMode.NONE.value,
        entry_mode=EntryMode.IMPULSE_CLOSE.value,
        volume_filter=False,
        extras={"role": "control"},
    ),
    GCMomentumStrategyConfig(
        candidate_id="C1_P1_CONFIRM_CLOSE",
        pullback_mode=PullbackMode.P1_HALF_RETRACE.value,
        entry_mode=EntryMode.CONFIRMATION_CLOSE.value,
        volume_filter=False,
    ),
    GCMomentumStrategyConfig(
        candidate_id="C2_P1_MIDPOINT_RETEST",
        pullback_mode=PullbackMode.P1_HALF_RETRACE.value,
        entry_mode=EntryMode.CONFIRMATION_MIDPOINT_RETEST.value,
        volume_filter=False,
    ),
    GCMomentumStrategyConfig(
        candidate_id="C3_P3_CONFIRM_CLOSE",
        pullback_mode=PullbackMode.P3_BREAKOUT_RETEST.value,
        entry_mode=EntryMode.CONFIRMATION_CLOSE.value,
        volume_filter=False,
    ),
    GCMomentumStrategyConfig(
        candidate_id="C4_P3_BREAKOUT_RETEST",
        pullback_mode=PullbackMode.P3_BREAKOUT_RETEST.value,
        entry_mode=EntryMode.BREAKOUT_LEVEL_RETEST.value,
        volume_filter=False,
    ),
    GCMomentumStrategyConfig(
        candidate_id="C1_P1_CONFIRM_CLOSE_RVOL15",
        pullback_mode=PullbackMode.P1_HALF_RETRACE.value,
        entry_mode=EntryMode.CONFIRMATION_CLOSE.value,
        volume_filter=True,
        extras={"role": "rvol_variant", "base": "C1_P1_CONFIRM_CLOSE"},
    ),
    GCMomentumStrategyConfig(
        candidate_id="C2_P1_MIDPOINT_RETEST_RVOL15",
        pullback_mode=PullbackMode.P1_HALF_RETRACE.value,
        entry_mode=EntryMode.CONFIRMATION_MIDPOINT_RETEST.value,
        volume_filter=True,
        extras={"role": "rvol_variant", "base": "C2_P1_MIDPOINT_RETEST"},
    ),
    GCMomentumStrategyConfig(
        candidate_id="C3_P3_CONFIRM_CLOSE_RVOL15",
        pullback_mode=PullbackMode.P3_BREAKOUT_RETEST.value,
        entry_mode=EntryMode.CONFIRMATION_CLOSE.value,
        volume_filter=True,
        extras={"role": "rvol_variant", "base": "C3_P3_CONFIRM_CLOSE"},
    ),
)
