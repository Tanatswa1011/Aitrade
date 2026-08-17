"""Phase 24 — GC OR15 retest / FVG entry models (isolated family)."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Optional

STRATEGY_FAMILY = "gc_orb15_retest_fvg_v1"
STRATEGY_VERSION = "v1.phase24"
INSTRUMENT = "GC"

OR_TIMEZONE = "America/New_York"
OR_ANCHOR_LOCAL = "08:20"
OR_MINUTES = 15
OR_ANCHOR_NOTE = (
    "Research anchor 08:20 America/New_York; OR15 = 08:20→08:35. "
    "Not asserted as exclusive Globex open."
)

MAX_RETEST_BARS = 12  # 60m
MAX_FVG_CREATION_BARS = 6  # 30m
MAX_FVG_RETRACE_BARS = 12  # 60m after FVG creation
HORIZON_BARS = 78  # ~6.5h of 5m — same session horizon family as Phase 22/23


class EntryMode(str, Enum):
    BREAKOUT_CLOSE = "BREAKOUT_CLOSE"
    RETEST_TOUCH = "RETEST_TOUCH"
    RETEST_CLOSE = "RETEST_CLOSE"
    FVG_TOUCH = "FVG_TOUCH"
    FVG_CE = "FVG_CE"


class StopMode(str, Enum):
    OR_MIDPOINT = "OR_MIDPOINT"
    RETEST_EXTREME = "RETEST_EXTREME"
    OR_MIDPOINT_NO_LOOKAHEAD = "OR_MIDPOINT_NO_LOOKAHEAD"
    BEYOND_FVG = "beyond_fvg"


@dataclass(frozen=True)
class ORB15StrategyConfig:
    strategy_family: str = STRATEGY_FAMILY
    candidate_id: str = "A"
    or_minutes: int = OR_MINUTES
    entry_mode: str = EntryMode.BREAKOUT_CLOSE.value
    stop_mode: str = StopMode.OR_MIDPOINT.value
    max_retest_bars: int = MAX_RETEST_BARS
    max_fvg_creation_bars: int = MAX_FVG_CREATION_BARS
    max_fvg_retrace_bars: int = MAX_FVG_RETRACE_BARS
    volume_filter: bool = False
    displacement_filter: bool = False
    execution_timeframe: str = "5m"
    extras: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ORB15BreakoutEvent:
    event_id: str
    trading_date: str
    contract: str
    direction: str
    or_high: float
    or_low: float
    or_mid: float
    or_size: float
    breakout_timestamp: int
    breakout_open: float
    breakout_high: float
    breakout_low: float
    breakout_close: float
    distance_beyond_or: float
    roll_artifact: bool = False
    body_or_ratio: Optional[float] = None
    opposite_break_after_first: bool = False
    opposite_break_timestamp: Optional[int] = None
    extras: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ORB15FVGZone:
    direction: str
    low: float
    high: float
    ce: float
    created_timestamp: int
    c1_time: int
    c2_time: int
    c3_time: int
    bars_after_breakout: int
    gap_size: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ORB15Setup:
    strategy_family: str
    setup_id: str
    orb_breakout_event_id: str
    candidate_id: str
    trading_date: str
    direction: str
    entry_mode: str
    entry_price: Optional[float]
    entry_timestamp: Optional[int]
    entry_triggered: bool
    stop_price: Optional[float]
    stop_mode: str
    risk_distance: Optional[float]
    risk_valid: bool
    risk_invalidation_reason: Optional[str]
    targets: list[dict[str, Any]] = field(default_factory=list)
    event: Optional[dict[str, Any]] = None
    fvg: Optional[dict[str, Any]] = None
    retest_timestamp: Optional[int] = None
    state: str = "NO_SETUP"
    reason: Optional[str] = None
    extras: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


PHASE24_CANDIDATES: tuple[ORB15StrategyConfig, ...] = (
    ORB15StrategyConfig(
        candidate_id="A_ORB15_BREAKOUT_CLOSE",
        entry_mode=EntryMode.BREAKOUT_CLOSE.value,
        stop_mode=StopMode.OR_MIDPOINT.value,
    ),
    ORB15StrategyConfig(
        candidate_id="B1_ORB15_RETEST_TOUCH",
        entry_mode=EntryMode.RETEST_TOUCH.value,
        stop_mode=StopMode.OR_MIDPOINT_NO_LOOKAHEAD.value,
    ),
    ORB15StrategyConfig(
        candidate_id="B2_ORB15_RETEST_CLOSE",
        entry_mode=EntryMode.RETEST_CLOSE.value,
        stop_mode=StopMode.RETEST_EXTREME.value,
    ),
    ORB15StrategyConfig(
        candidate_id="C_ORB15_FVG_TOUCH",
        entry_mode=EntryMode.FVG_TOUCH.value,
        stop_mode=StopMode.BEYOND_FVG.value,
    ),
    ORB15StrategyConfig(
        candidate_id="D_ORB15_FVG_CE",
        entry_mode=EntryMode.FVG_CE.value,
        stop_mode=StopMode.BEYOND_FVG.value,
    ),
)
