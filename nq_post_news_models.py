"""Phase 33 — Post-news macro repricing models (research-only; not frozen)."""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

STRATEGY_FAMILY = "nq_post_news_macro_repricing_v1"
STRATEGY_VERSION = "v1.phase33"
CANDIDATE_ID = "POST_NEWS_CONTINUATION_V0"
INSTRUMENT = "NQ"

OR_TIMEZONE = "America/New_York"
DEFAULT_RELEASE_LOCAL = "08:30"
FORCE_CLOSE_LOCAL = "15:55"
CASH_OPEN_LOCAL = "09:30"
PRIOR_SESSION_OPEN_LOCAL = "18:00"

NQ_TICK_SIZE = 0.25
GC_TICK_SIZE = 0.10
ES_TICK_SIZE = 0.25

# Predeclared research grid — not an exhaustive optimizer.
MIN_CLOSE_MOVE_ATR = 0.50
MIN_RANGE_ATR = 0.75
MIN_RETENTION = 0.50
ATR_PERIOD = 14
ATR_TIMEFRAME = "5m"
DEFAULT_BLACKOUT_BEFORE_MIN = 5
DEFAULT_BLACKOUT_AFTER_MIN = 5


@dataclass(frozen=True)
class PropFirmNewsProfile:
    """Configurable news blackout. Default is conservative, not universal."""

    profile_id: str = "DEFAULT_CONSERVATIVE_PM_5_5"
    blackout_before_minutes: int = DEFAULT_BLACKOUT_BEFORE_MIN
    blackout_after_minutes: int = DEFAULT_BLACKOUT_AFTER_MIN
    forbid_open: bool = True
    forbid_close: bool = True
    forbid_modify_stop: bool = True
    forbid_modify_target: bool = True
    forbid_size_change: bool = True
    forbid_discretionary: bool = True
    note: str = (
        "Conservative default for research. Must be replaced by a named prop-firm "
        "profile before any live/paper deployment. Not claimed as every firm's rule."
    )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


DEFAULT_NEWS_PROFILE = PropFirmNewsProfile()


@dataclass(frozen=True)
class PostNewsStrategyConfig:
    strategy_family: str = STRATEGY_FAMILY
    strategy_version: str = STRATEGY_VERSION
    candidate_id: str = CANDIDATE_ID
    instrument: str = INSTRUMENT
    entry_family: str = "C_5M_CLOSE_CONFIRM"  # A_RANGE_BREAKOUT | B_FIRST_PULLBACK | C_5M_CLOSE_CONFIRM | D_CASH_OPEN
    event_family: str = "ALL"  # CPI | NFP | ALL
    delay_minutes: int = 5  # minutes after official 08:30 (5 => 08:35)
    min_close_move_atr: float = MIN_CLOSE_MOVE_ATR
    min_range_atr: float = MIN_RANGE_ATR
    min_retention: float = MIN_RETENTION
    atr_period: int = ATR_PERIOD
    target_r: float = 1.0
    one_trade_per_event: bool = True
    flatten_local: str = FORCE_CLOSE_LOCAL
    news_profile: PropFirmNewsProfile = DEFAULT_NEWS_PROFILE
    extras: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        return d


def config_hash(cfg: PostNewsStrategyConfig) -> str:
    raw = "|".join(
        [
            cfg.strategy_family,
            cfg.candidate_id,
            cfg.instrument,
            cfg.entry_family,
            cfg.event_family,
            str(cfg.delay_minutes),
            str(cfg.min_close_move_atr),
            str(cfg.min_range_atr),
            str(cfg.min_retention),
            str(cfg.atr_period),
            str(cfg.target_r),
            cfg.news_profile.profile_id,
            str(cfg.news_profile.blackout_before_minutes),
            str(cfg.news_profile.blackout_after_minutes),
        ]
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True)
class MacroEvent:
    event_id: str
    event_family: str  # CPI | NFP
    publication_date: str  # YYYY-MM-DD America/New_York calendar date of release
    release_local: str
    source: str
    source_url: str
    embargo_line: Optional[str] = None
    reference_period: Optional[str] = None
    actuals: dict[str, Any] = field(default_factory=dict)
    consensus: dict[str, Any] = field(default_factory=dict)
    surprise: dict[str, Any] = field(default_factory=dict)
    data_quality: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class EventSnapshot:
    event_id: str
    event_family: str
    instrument: str
    trading_date: str
    release_ts: int
    blackout_start_ts: int
    blackout_end_ts: int
    ref_price: Optional[float]
    event_open: Optional[float]
    event_high: Optional[float]
    event_low: Optional[float]
    event_close: Optional[float]
    event_range: Optional[float]
    signed_move: Optional[float]
    atr: Optional[float]
    signed_move_atr: Optional[float]
    event_range_atr: Optional[float]
    retention: Optional[float]
    globex_vwap: Optional[float]
    close_vs_vwap: Optional[float]
    regime: str  # MACRO_BULLISH | MACRO_BEARISH | MACRO_NEUTRAL | DATA_INSUFFICIENT
    skip_reason: Optional[str] = None
    extras: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class PostNewsTrade:
    trade_id: str
    trading_date: str
    direction: str  # bullish | bearish
    entry_timestamp: int
    entry_price: float
    stop_price: float
    target_price: float
    exit_timestamp: Optional[int]
    exit_price: Optional[float]
    outcome: str
    points: Optional[float]
    r_multiple: Optional[float]
    suppressed_reason: Optional[str] = None
    extras: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
