"""Canonical session-liquidity, structure-confirmation, and FVG models."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Literal, Optional


class SessionName(str, Enum):
    ASIA = "Asia"
    LONDON = "London"


class SweepSide(str, Enum):
    HIGH = "high"
    LOW = "low"


class SweepRule(str, Enum):
    WICK_ONLY = "wick_only"
    RECLAIM = "reclaim"
    TOUCH = "touch"


class CoverageStatus(str, Enum):
    FULL = "full"
    PARTIAL_START = "partial_start"
    PARTIAL_END = "partial_end"
    PARTIAL = "partial"
    PRICE_ONLY = "price_only"
    MISSING = "missing"
    UNKNOWN = "unknown"


class StructureKind(str, Enum):
    CHOCH = "CHoCH"
    BOS = "BOS"  # available later; not a Phase 3/4 trigger
    IDM = "IDM"


class StructureDirection(str, Enum):
    BULLISH = "bullish"
    BEARISH = "bearish"


class TimingConfidence(str, Enum):
    EXACT = "exact"
    DERIVED = "derived"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class Bar:
    """Single OHLC bar. time is unix seconds (UTC)."""

    time: int
    open: float
    high: float
    low: float
    close: float
    volume: Optional[float] = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class SessionRange:
    """Canonical session high/low range from any source."""

    name: str
    timezone: str
    start: Optional[int]
    end: Optional[int]
    high: Optional[float]
    low: Optional[float]
    high_timestamp: Optional[int]
    low_timestamp: Optional[int]
    complete: bool
    source: Literal["ict_sessions", "internal_ohlc"]
    coverage_status: str
    identity: Optional[str] = None
    extras: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def is_tradeable_level_source(self) -> bool:
        return self.high is not None and self.low is not None


@dataclass(frozen=True)
class LiquiditySweep:
    """Canonical session-liquidity sweep event."""

    session: str
    side: str  # high | low
    level: float
    sweep_timestamp: int
    sweep_price: float
    maximum_excursion: float
    reclaim_status: bool
    rule: str
    sweep_candle: Bar
    session_range: Optional[dict[str, Any]] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "session": self.session,
            "side": self.side,
            "level": self.level,
            "sweep_timestamp": self.sweep_timestamp,
            "sweep_price": self.sweep_price,
            "maximum_excursion": self.maximum_excursion,
            "reclaim_status": self.reclaim_status,
            "rule": self.rule,
            "sweep_candle": self.sweep_candle.to_dict(),
            "session_range": self.session_range,
        }


@dataclass(frozen=True)
class StructureConfirmation:
    """Normalized market-structure event (LuxAlgo CHoCH for Phase 3)."""

    kind: str  # CHoCH (BOS/IDM reserved, not used as trigger)
    direction: str  # bullish | bearish
    level: float
    event_timestamp: Optional[int]
    event_bar_index: Optional[int]
    source: str
    study_id: Optional[str]
    raw_id: Optional[str]
    timing_confidence: str  # exact | derived | unavailable
    extras: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def has_reliable_timing(self) -> bool:
        return (
            self.timing_confidence
            in (TimingConfidence.EXACT.value, TimingConfidence.DERIVED.value)
            and (
                self.event_timestamp is not None
                or self.event_bar_index is not None
            )
        )


@dataclass(frozen=True)
class ConfirmationDecision:
    """Result of confirm_after_sweep (explicit confirm / no-confirm)."""

    confirmed: bool
    confirmation: Optional[StructureConfirmation]
    reason: str
    required_direction: Optional[str] = None
    candidates_seen: int = 0
    rejected: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "confirmed": self.confirmed,
            "confirmation": None
            if self.confirmation is None
            else self.confirmation.to_dict(),
            "reason": self.reason,
            "required_direction": self.required_direction,
            "candidates_seen": self.candidates_seen,
            "rejected": list(self.rejected),
        }


@dataclass(frozen=True)
class FVGConfig:
    """Configurable FVG search / filter options."""

    first_only: bool = True
    max_bars_after_confirmation: Optional[int] = None
    min_gap: float = 0.0
    min_gap_points: float = 0.0
    point_size: float = 1.0
    require_displacement: bool = False
    # Simple optional displacement: candle-2 body >= fraction of its range.
    displacement_min_body_ratio: float = 0.5
    # Lookback for optional body-vs-average filter (0 disables average check).
    displacement_body_lookback: int = 0
    displacement_body_vs_avg_mult: float = 1.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class FVGZone:
    """Setup-linked Fair Value Gap (3-candle imbalance)."""

    direction: str  # bullish | bearish
    low: float
    high: float
    midpoint: float
    created_timestamp: int
    candle1_timestamp: int
    candle2_timestamp: int
    candle3_timestamp: int
    gap_size: float
    gap_points: float
    mitigated: bool
    first_mitigation_timestamp: Optional[int]
    fully_filled: bool
    first_full_fill_timestamp: Optional[int]
    bars_after_sweep: Optional[int]
    bars_after_confirmation: Optional[int]
    setup_reference: dict[str, Any]
    source: str = "internal_ohlc"
    extras: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class FVGDetectionResult:
    """Explicit FVG detect outcome (zero or more setup-linked zones)."""

    found: bool
    zones: list[FVGZone]
    reason: str
    required_direction: Optional[str] = None
    config: Optional[dict[str, Any]] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "found": self.found,
            "zones": [z.to_dict() for z in self.zones],
            "first": None if not self.zones else self.zones[0].to_dict(),
            "reason": self.reason,
            "required_direction": self.required_direction,
            "config": self.config,
        }


class EntryMode(str, Enum):
    FIRST_TOUCH = "first_touch"
    CE = "ce"
    BOUNDARY = "boundary"


class EntryStatus(str, Enum):
    WAITING = "waiting"
    TRIGGERED = "triggered"
    MISSED = "missed"
    INVALID = "invalid"


@dataclass(frozen=True)
class EntryConfig:
    """Configurable FVG entry-candidate options (no risk / sizing)."""

    mode: str = EntryMode.FIRST_TOUCH.value
    allow_full_fill: bool = True
    max_bars_after_fvg: Optional[int] = None
    extras: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class EntryCandidate:
    """Deterministic entry candidate for a setup-linked FVG (no orders)."""

    mode: str
    direction: str
    price: Optional[float]
    triggered: bool
    trigger_timestamp: Optional[int]
    trigger_bar_index: Optional[int]
    fvg_reference: dict[str, Any]
    setup_reference: dict[str, Any]
    entry_depth: Optional[float]
    max_retrace_depth: Optional[float]
    bars_after_fvg: Optional[int]
    status: str  # waiting | triggered | missed | invalid
    extras: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class StopMode(str, Enum):
    BEYOND_SWEEP = "beyond_sweep"
    BEYOND_FVG = "beyond_fvg"
    # Reserved for later phases:
    BEYOND_STRUCTURE = "beyond_structure"
    FIXED_DISTANCE = "fixed_distance"
    ATR = "atr"


@dataclass(frozen=True)
class RiskConfig:
    """Stop / invalidation configuration (analysis only)."""

    stop_mode: str = StopMode.BEYOND_SWEEP.value
    stop_buffer_price: float = 0.0
    stop_buffer_points: float = 0.0
    point_size: float = 1.0
    invalidate_before_entry: bool = True
    extras: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def buffer_absolute(self) -> float:
        ps = float(self.point_size) if float(self.point_size) > 0 else 1.0
        return float(self.stop_buffer_price) + float(self.stop_buffer_points) * ps


@dataclass(frozen=True)
class TargetConfig:
    """Target / RR configuration (analysis only)."""

    fixed_rr: tuple[float, ...] = (1.0, 2.0, 3.0)
    use_opposite_liquidity: bool = True
    opposite_liquidity_mode: str = "same_session"
    extras: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["fixed_rr"] = list(self.fixed_rr)
        return d


@dataclass(frozen=True)
class RiskPlan:
    """Stop / invalidation plan for one triggered EntryCandidate."""

    direction: str
    stop_mode: str
    entry_price: float
    stop_price: Optional[float]
    risk_distance: Optional[float]
    risk_points: Optional[float]
    buffer: float
    valid: bool
    invalidation_reason: Optional[str]
    setup_reference: dict[str, Any]
    extras: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class FixedRRTarget:
    rr: float
    price: float
    distance: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class TargetPlan:
    """Fixed-RR and opposite-liquidity targets for one RiskPlan."""

    fixed_rr_targets: list[FixedRRTarget]
    opposite_liquidity: bool
    opposite_liquidity_label: Optional[str]
    opposite_liquidity_price: Optional[float]
    rr_to_opposite: Optional[float]
    opposite_target_valid: bool
    valid: bool
    setup_reference: dict[str, Any]
    extras: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "fixed_rr_targets": [t.to_dict() for t in self.fixed_rr_targets],
            "opposite_liquidity": self.opposite_liquidity,
            "opposite_liquidity_label": self.opposite_liquidity_label,
            "opposite_liquidity_price": self.opposite_liquidity_price,
            "rr_to_opposite": self.rr_to_opposite,
            "opposite_target_valid": self.opposite_target_valid,
            "valid": self.valid,
            "setup_reference": self.setup_reference,
            "extras": dict(self.extras),
        }


class SetupStatus(str, Enum):
    WAITING_FOR_SESSION = "WAITING_FOR_SESSION"
    SESSION_RANGE_COMPLETE = "SESSION_RANGE_COMPLETE"
    WAITING_FOR_SWEEP = "WAITING_FOR_SWEEP"
    LIQUIDITY_SWEPT = "LIQUIDITY_SWEPT"
    WAITING_FOR_CONFIRMATION = "WAITING_FOR_CONFIRMATION"
    CONFIRMATION_FOUND = "CONFIRMATION_FOUND"
    WAITING_FOR_FVG = "WAITING_FOR_FVG"
    FVG_FOUND = "FVG_FOUND"
    WAITING_FOR_RETRACE = "WAITING_FOR_RETRACE"
    ENTRY_READY = "ENTRY_READY"
    INVALIDATED = "INVALIDATED"
    EXPIRED = "EXPIRED"
    NO_SETUP = "NO_SETUP"


# Documented Phase 6/7 ambiguity — do not invent OHLC intrabar ordering.
TRIGGER_BAR_STOP_AMBIGUITY = (
    "Pre-entry invalidation uses bars with "
    "FVG_creation < timestamp < entry_trigger. The trigger bar itself is not "
    "treated as pre-entry invalidation. A bar that both fills entry and trades "
    "through the stop cannot be classified as 'entry then stopped' vs "
    "'invalid before fill' from OHLC alone (TRIGGER_BAR_STOP_AMBIGUITY)."
)


@dataclass(frozen=True)
class EntryAnalysis:
    """One entry mode with its dedicated risk and target plans."""

    entry: EntryCandidate
    risk: Optional[RiskPlan]
    target: Optional[TargetPlan]

    def to_dict(self) -> dict[str, Any]:
        return {
            "entry": self.entry.to_dict(),
            "risk": None if self.risk is None else self.risk.to_dict(),
            "target": None if self.target is None else self.target.to_dict(),
        }


@dataclass(frozen=True)
class TradeSetup:
    """Canonical read-only AITRADE session setup analysis (no broker state)."""

    id: str
    symbol: str
    timeframe: str
    trading_date: Optional[str]
    session: str
    direction: Optional[str]
    session_range: Optional[dict[str, Any]]
    sweep: Optional[dict[str, Any]]
    confirmation: Optional[dict[str, Any]]
    fvg: Optional[dict[str, Any]]
    entries: list[EntryAnalysis]
    status: str
    setup_quality: Optional[dict[str, Any]]
    created_at: str
    updated_at: str
    expiry_reason: Optional[str]
    invalidation_reason: Optional[str]
    source_metadata: dict[str, Any]
    explanation: str
    # Phase 11 multi-timeframe context (bias is context, not a hard filter)
    execution_timeframe: Optional[str] = None
    higher_timeframe_context: Optional[dict[str, Any]] = None
    setup_vs_daily: Optional[str] = None
    setup_vs_h4: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "trading_date": self.trading_date,
            "session": self.session,
            "direction": self.direction,
            "session_range": self.session_range,
            "sweep": self.sweep,
            "confirmation": self.confirmation,
            "fvg": self.fvg,
            "entries": [e.to_dict() for e in self.entries],
            "status": self.status,
            "setup_quality": self.setup_quality,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "expiry_reason": self.expiry_reason,
            "invalidation_reason": self.invalidation_reason,
            "source_metadata": dict(self.source_metadata),
            "explanation": self.explanation,
            "execution_timeframe": self.execution_timeframe,
            "higher_timeframe_context": self.higher_timeframe_context,
            "setup_vs_daily": self.setup_vs_daily,
            "setup_vs_h4": self.setup_vs_h4,
        }


def replace_trade_setup(setup: TradeSetup, **changes: Any) -> TradeSetup:
    """Immutable field update helper for TradeSetup."""
    data = {
        "id": setup.id,
        "symbol": setup.symbol,
        "timeframe": setup.timeframe,
        "trading_date": setup.trading_date,
        "session": setup.session,
        "direction": setup.direction,
        "session_range": setup.session_range,
        "sweep": setup.sweep,
        "confirmation": setup.confirmation,
        "fvg": setup.fvg,
        "entries": list(setup.entries),
        "status": setup.status,
        "setup_quality": setup.setup_quality,
        "created_at": setup.created_at,
        "updated_at": setup.updated_at,
        "expiry_reason": setup.expiry_reason,
        "invalidation_reason": setup.invalidation_reason,
        "source_metadata": dict(setup.source_metadata or {}),
        "explanation": setup.explanation,
        "execution_timeframe": setup.execution_timeframe,
        "higher_timeframe_context": setup.higher_timeframe_context,
        "setup_vs_daily": setup.setup_vs_daily,
        "setup_vs_h4": setup.setup_vs_h4,
    }
    data.update(changes)
    return TradeSetup(**data)


PRIMARY_SESSIONS = (SessionName.ASIA.value, SessionName.LONDON.value)
