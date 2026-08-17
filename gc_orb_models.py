"""Phase 22 GC ORB + volume models (isolated from XAUUSD strategies)."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Optional

STRATEGY_FAMILY = "gc_orb_volume_v1"
STRATEGY_VERSION = "v1.phase22"
INSTRUMENT = "GC"

# Predeclared research hypotheses (frozen before HOLDOUT)
DISPLACEMENT_BODY_OR_RATIO = 0.50
VOLUME_RVOL_THRESHOLD = 1.5
MAX_RETEST_BARS = 6
RVOL_LOOKBACK = 20

# Session anchor: US morning gold activity window research hypothesis.
# NOT claimed as the sole COMEX electronic open (Globex trades nearly 23h).
# 08:20 America/New_York aligns with the traditional COMEX open / primary
# US cash-gold activity period used as a fixed research clock.
OR_TIMEZONE = "America/New_York"
OR_ANCHOR_LOCAL = "08:20"
OR_ANCHOR_NOTE = (
    "Research anchor 08:20 America/New_York for primary US gold activity; "
    "not asserted as exclusive Globex session open."
)


class EntryMode(str, Enum):
    BREAKOUT_CLOSE = "BREAKOUT_CLOSE"
    RETEST_CLOSE = "RETEST_CLOSE"
    RETEST_BOUNDARY = "RETEST_BOUNDARY"


class StopMode(str, Enum):
    RETEST_EXTREME = "RETEST_EXTREME"
    OR_MIDPOINT = "OR_MIDPOINT"
    OR_OPPOSITE = "OR_OPPOSITE"  # bullish stop = OR low; bearish stop = OR high
    BREAKOUT_EXTREME = "BREAKOUT_EXTREME"  # deprecated for same-bar entries


@dataclass(frozen=True)
class OpeningRange:
    trading_date: str
    start_timestamp: int
    end_timestamp: int
    high: float
    low: float
    midpoint: float
    range_size: float
    bar_count: int
    complete: bool
    or_minutes: int
    timezone: str = OR_TIMEZONE
    total_volume: Optional[float] = None
    median_bar_volume: Optional[float] = None
    extras: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class GCORBStrategyConfig:
    strategy_family: str = STRATEGY_FAMILY
    candidate_id: str = "G0"
    or_minutes: int = 30
    volume_filter: bool = False
    displacement_filter: bool = False
    rvol_threshold: float = VOLUME_RVOL_THRESHOLD
    displacement_body_or_ratio: float = DISPLACEMENT_BODY_OR_RATIO
    entry_mode: str = EntryMode.BREAKOUT_CLOSE.value
    stop_mode: str = StopMode.BREAKOUT_EXTREME.value
    max_retest_bars: int = MAX_RETEST_BARS
    rvol_lookback: int = RVOL_LOOKBACK
    execution_timeframe: str = "5m"
    extras: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class GCORBEvent:
    breakout_id: str
    instrument: str
    contract: str
    trading_date: str
    side: str  # bullish | bearish
    or_minutes: int
    or_high: float
    or_low: float
    or_midpoint: float
    or_range_size: float
    breakout_timestamp: int
    breakout_close: float
    breakout_high: float
    breakout_low: float
    breakout_open: float
    distance_beyond_range: float
    body: float
    candle_range: float
    body_or_ratio: float
    range_or_ratio: float
    volume: Optional[float]
    reference_volume: Optional[float]
    rvol: Optional[float]
    displacement_ok: bool
    volume_ok: bool
    roll_artifact: bool
    extras: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class GCORBSetup:
    strategy_family: str
    setup_id: str
    breakout_id: str
    candidate_id: str
    trading_date: str
    direction: str
    entry_mode: str
    entry_price: Optional[float]
    entry_timestamp: Optional[int]
    entry_triggered: bool
    stop_price: Optional[float]
    risk_distance: Optional[float]
    risk_valid: bool
    risk_invalidation_reason: Optional[str]
    targets: list[dict[str, Any]] = field(default_factory=list)
    event: Optional[dict[str, Any]] = None
    retest_timestamp: Optional[int] = None
    state: str = "NO_SETUP"
    reason: Optional[str] = None
    extras: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# Frozen candidate matrix (OR30). Written before HOLDOUT.
PHASE22_CANDIDATES: tuple[GCORBStrategyConfig, ...] = (
    GCORBStrategyConfig(
        candidate_id="G1_OR30_bo_volOFF_dispOFF",
        volume_filter=False,
        displacement_filter=False,
        entry_mode=EntryMode.BREAKOUT_CLOSE.value,
        stop_mode=StopMode.OR_OPPOSITE.value,
    ),
    GCORBStrategyConfig(
        candidate_id="G2_OR30_bo_volON_dispOFF",
        volume_filter=True,
        displacement_filter=False,
        entry_mode=EntryMode.BREAKOUT_CLOSE.value,
        stop_mode=StopMode.OR_OPPOSITE.value,
    ),
    GCORBStrategyConfig(
        candidate_id="G3_OR30_bo_volOFF_dispON",
        volume_filter=False,
        displacement_filter=True,
        entry_mode=EntryMode.BREAKOUT_CLOSE.value,
        stop_mode=StopMode.OR_OPPOSITE.value,
    ),
    GCORBStrategyConfig(
        candidate_id="G4_OR30_bo_volON_dispON",
        volume_filter=True,
        displacement_filter=True,
        entry_mode=EntryMode.BREAKOUT_CLOSE.value,
        stop_mode=StopMode.OR_OPPOSITE.value,
    ),
    GCORBStrategyConfig(
        candidate_id="G5_OR30_rt_volOFF_dispOFF",
        volume_filter=False,
        displacement_filter=False,
        entry_mode=EntryMode.RETEST_CLOSE.value,
        stop_mode=StopMode.RETEST_EXTREME.value,
    ),
    GCORBStrategyConfig(
        candidate_id="G6_OR30_rt_volON_dispOFF",
        volume_filter=True,
        displacement_filter=False,
        entry_mode=EntryMode.RETEST_CLOSE.value,
        stop_mode=StopMode.RETEST_EXTREME.value,
    ),
    GCORBStrategyConfig(
        candidate_id="G7_OR30_rt_volON_dispON",
        volume_filter=True,
        displacement_filter=True,
        entry_mode=EntryMode.RETEST_CLOSE.value,
        stop_mode=StopMode.RETEST_EXTREME.value,
    ),
    GCORBStrategyConfig(
        candidate_id="G8_OR30_rb_volON_dispON",
        volume_filter=True,
        displacement_filter=True,
        entry_mode=EntryMode.RETEST_BOUNDARY.value,
        stop_mode=StopMode.RETEST_EXTREME.value,
    ),
)
