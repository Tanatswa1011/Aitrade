"""Phase 25 — GC session VWAP mean-reversion models (isolated family)."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Optional

STRATEGY_FAMILY = "gc_vwap_mean_reversion_v1"
STRATEGY_VERSION = "v1.phase25"
INSTRUMENT = "GC"

OR_TIMEZONE = "America/New_York"
SESSION_START_LOCAL = "08:20"
SESSION_END_LOCAL = "13:30"
NO_NEW_SETUP_AFTER_LOCAL = "12:30"
SESSION_NOTE = (
    "Research session 08:20–13:30 America/New_York for primary US gold window; "
    "not claimed as the only valid GC trading session."
)

MIN_VWAP_BARS = 6
SIGMA_THRESHOLD = 2.0
MAX_ENTRY_BARS = 6


@dataclass(frozen=True)
class VwapSessionSpec:
    """Session clock for VWAP reset / expiry. Defaults = Phase 25 NY research window."""

    timezone: str = OR_TIMEZONE
    start_local: str = SESSION_START_LOCAL
    end_local: str = SESSION_END_LOCAL
    no_new_setups_after: str = NO_NEW_SETUP_AFTER_LOCAL
    min_vwap_bars: int = MIN_VWAP_BARS
    event_prefix: str = "VWAP2S"
    session_note: str = SESSION_NOTE

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


DEFAULT_NY_SESSION = VwapSessionSpec()


class ConfirmationMode(str, Enum):
    NONE = "NONE"  # V0 naive
    BAND_RECLAIM = "BAND_RECLAIM"
    RECLAIM_CANDLE_BREAK = "RECLAIM_CANDLE_BREAK"
    TWO_BAR_RETURN = "TWO_BAR_RETURN"


class EntryMode(str, Enum):
    CONFIRMATION_CLOSE = "CONFIRMATION_CLOSE"
    FROZEN_2SIG_RETEST = "FROZEN_2SIG_RETEST"
    EXTENSION_MIDPOINT = "EXTENSION_MIDPOINT"
    IMMEDIATE_2SIG_CLOSE = "IMMEDIATE_2SIG_CLOSE"  # V0


@dataclass(frozen=True)
class GCVWAPStrategyConfig:
    strategy_family: str = STRATEGY_FAMILY
    candidate_id: str = "V0"
    confirmation_mode: str = ConfirmationMode.NONE.value
    entry_mode: str = EntryMode.IMMEDIATE_2SIG_CLOSE.value
    sigma_threshold: float = SIGMA_THRESHOLD
    max_entry_bars: int = MAX_ENTRY_BARS
    min_vwap_bars: int = MIN_VWAP_BARS
    volume_filter: bool = False
    execution_timeframe: str = "5m"
    extras: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class SessionVWAPState:
    trading_date: str
    session_start: int
    session_end: int
    timestamp: int
    vwap: Optional[float]
    cumulative_volume: float
    bars_used: int
    session_std: Optional[float]
    band_1: Optional[tuple[float, float]]
    band_2: Optional[tuple[float, float]]
    band_3: Optional[tuple[float, float]]
    z_vwap: Optional[float]
    valid: bool
    extras: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class GCVWAPExtensionEvent:
    event_id: str
    trading_date: str
    direction: str  # short (upper ext) | long (lower ext)
    extension_side: str  # above | below
    first_extension_timestamp: int
    confirmation_timestamp: Optional[int]
    extension_extreme: float
    frozen_2sig_band: Optional[float]
    extension_midpoint: Optional[float]
    max_abs_z: float
    first_extension_z: float
    roll_artifact: bool = False
    extras: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class GCVWAPSetup:
    strategy_family: str
    setup_id: str
    vwap_extension_event_id: str
    candidate_id: str
    trading_date: str
    direction: str
    entry_mode: str
    confirmation_mode: str
    entry_price: Optional[float]
    entry_timestamp: Optional[int]
    entry_triggered: bool
    stop_price: Optional[float]
    risk_distance: Optional[float]
    risk_valid: bool
    risk_invalidation_reason: Optional[str]
    targets: list[dict[str, Any]] = field(default_factory=list)
    event: Optional[dict[str, Any]] = None
    state: str = "NO_SETUP"
    reason: Optional[str] = None
    extras: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


PHASE25_CANDIDATES: tuple[GCVWAPStrategyConfig, ...] = (
    GCVWAPStrategyConfig(
        candidate_id="V0_NAIVE_2SIG_FADE",
        confirmation_mode=ConfirmationMode.NONE.value,
        entry_mode=EntryMode.IMMEDIATE_2SIG_CLOSE.value,
        extras={"role": "control"},
    ),
    GCVWAPStrategyConfig(
        candidate_id="V1_BAND_RECLAIM_CLOSE",
        confirmation_mode=ConfirmationMode.BAND_RECLAIM.value,
        entry_mode=EntryMode.CONFIRMATION_CLOSE.value,
    ),
    GCVWAPStrategyConfig(
        candidate_id="V2_BAND_RECLAIM_2SIG_RETEST",
        confirmation_mode=ConfirmationMode.BAND_RECLAIM.value,
        entry_mode=EntryMode.FROZEN_2SIG_RETEST.value,
    ),
    GCVWAPStrategyConfig(
        candidate_id="V3_BAND_RECLAIM_EXT_MID",
        confirmation_mode=ConfirmationMode.BAND_RECLAIM.value,
        entry_mode=EntryMode.EXTENSION_MIDPOINT.value,
    ),
    GCVWAPStrategyConfig(
        candidate_id="V4_RECLAIM_CANDLE_BREAK",
        confirmation_mode=ConfirmationMode.RECLAIM_CANDLE_BREAK.value,
        entry_mode=EntryMode.CONFIRMATION_CLOSE.value,
    ),
    GCVWAPStrategyConfig(
        candidate_id="V5_TWO_BAR_RETURN",
        confirmation_mode=ConfirmationMode.TWO_BAR_RETURN.value,
        entry_mode=EntryMode.CONFIRMATION_CLOSE.value,
    ),
)
