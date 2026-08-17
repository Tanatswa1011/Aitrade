"""Default risk / target configuration (Phase 6)."""

from __future__ import annotations

from models import RiskConfig, StopMode, TargetConfig

DEFAULT_RISK_CONFIG = RiskConfig(
    stop_mode=StopMode.BEYOND_SWEEP.value,
    stop_buffer_price=0.0,
    stop_buffer_points=0.0,
    point_size=1.0,
    invalidate_before_entry=True,
)

DEFAULT_TARGET_CONFIG = TargetConfig(
    fixed_rr=(1.0, 2.0, 3.0),
    use_opposite_liquidity=True,
    opposite_liquidity_mode="same_session",
)
