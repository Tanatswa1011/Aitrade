"""Phase 29 — Drift VWAP Pullback (DVP) exact replication models for NQ."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Optional

STRATEGY_FAMILY = "nq_drift_vwap_pullback_v1"
STRATEGY_VERSION = "v1.phase29"
CANDIDATE_ID = "DVP_ORIGINAL"
INSTRUMENT = "NQ"

OR_TIMEZONE = "America/New_York"
VWAP_RESET_LOCAL = "09:30"
TRADE_START_LOCAL = "10:30"
NO_NEW_TRADES_AFTER_LOCAL = "15:30"
FORCE_CLOSE_LOCAL = "15:55"

HOUR_RETURN_THRESHOLD = 0.001  # 0.10%
LONG_STOP_POINTS = 80.0
LONG_TARGET_POINTS = 40.0
SHORT_STOP_POINTS = 80.0
SHORT_TARGET_POINTS = 50.0
MAX_TRADES_PER_DAY = 4
MAX_LOSSES_PER_DAY = 2  # INTERPRETATION: any two losing trades in the day (not only consecutive)
NQ_TICK_SIZE = 0.25

VWAP_PRICE_BASIS = "typical_price=(H+L+C)/3"
VWAP_BASIS_STATUS = "IMPLEMENTATION_ASSUMPTION"  # source did not specify alternate basis

SOURCE_CLAIMED_WIN_RATE = 0.645  # ~64–65% claimed — not treated as ground truth


@dataclass(frozen=True)
class DVPStrategyConfig:
    strategy_family: str = STRATEGY_FAMILY
    candidate_id: str = CANDIDATE_ID
    hour_return_threshold: float = HOUR_RETURN_THRESHOLD
    long_stop_points: float = LONG_STOP_POINTS
    long_target_points: float = LONG_TARGET_POINTS
    short_stop_points: float = SHORT_STOP_POINTS
    short_target_points: float = SHORT_TARGET_POINTS
    max_trades_per_day: int = MAX_TRADES_PER_DAY
    max_losses_per_day: int = MAX_LOSSES_PER_DAY
    extras: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class DVPTrade:
    trade_id: str
    trading_date: str
    direction: str
    entry_timestamp: int
    entry_price: float
    stop_price: float
    target_price: float
    exit_timestamp: Optional[int]
    exit_price: Optional[float]
    outcome: str  # TARGET_HIT | STOP_HIT | FORCE_CLOSE | AMBIGUOUS | OPEN
    points: Optional[float]
    r_multiple: Optional[float]  # vs 80-point risk
    suppressed_reason: Optional[str] = None
    extras: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


DVP_ORIGINAL = DVPStrategyConfig()
