"""Phase 34 — PDH/PDL sweep models (research-only; not frozen).

Outcome labels are declared here before any microstructure feature is computed.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Optional

STRATEGY_FAMILY = "nq_liquidity_microstructure_reversal_v1"
STRATEGY_VERSION = "v1.phase34"
INSTRUMENT = "NQ"
OR_TIMEZONE = "America/New_York"
RTH_START = "09:30"
RTH_END = "16:00"
NO_NEW_SWEEP_AFTER = "15:45"
NQ_TICK = 0.25

# --- Predeclared outcome definition (do not retune after seeing results) ---
PRIMARY_HORIZON_SEC = 300
AUX_HORIZONS_SEC = (30, 60, 180, 300, 900)
REVERSAL_TARGET_POINTS = 8.0  # 32 ticks
CONTINUATION_EXT_POINTS = 12.0  # 48 ticks
# Reclaim: trade back through the structural level (price crosses PDH/PDL back inside).

FROZEN_GC_HASH = "0695d7a881b6247033426bceb077b6895b96405b826c21f32a109a02f44b2d43"
FROZEN_NQ_HASH = "935e3a616351b09dbfa8d2d0e2b5d6850be803fb385ec5e5f8e3593856a1212a"


@dataclass(frozen=True)
class SweepEvent:
    event_id: str
    trading_date: str
    side: str  # pdh_sweep | pdl_sweep
    level: float
    sweep_bar_time: int
    sweep_ts: int  # first known time through level (bar open until trades refine)
    extreme: float
    penetration_points: float
    rth_open_ts: int
    seconds_from_rth_open: int
    atr_1m_14: Optional[float]
    volume_sweep_bar: Optional[float]
    prior_rth_high: float
    prior_rth_low: float
    extras: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class SweepOutcome:
    event_id: str
    horizon_sec: int
    label: str  # REVERSAL | CONTINUATION | NEITHER | AMBIGUOUS
    reclaim_ts: Optional[int]
    seconds_to_reclaim: Optional[float]
    mfe_points: Optional[float]
    mae_points: Optional[float]
    further_extension_points: Optional[float]
    extras: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
