"""Top-level strategy configuration composing Phase 2–11 configs."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Tuple

from entry_config import COMPARE_ENTRY_MODES, DEFAULT_ENTRY_CONFIG
from execution_config import (
    DEFAULT_BIAS_CONFIG,
    DEFAULT_EXECUTION_TIMEFRAME_CONFIG,
    BiasConfig,
    ExecutionTimeframeConfig,
)
from expiry_config import DEFAULT_EXPIRY_CONFIG, ExpiryConfig
from fvg_config import DEFAULT_FVG_CONFIG
from htf_bias_config import DEFAULT_HTF_BIAS_CONFIG, HTFBiasConfig
from models import (
    EntryConfig,
    FVGConfig,
    RiskConfig,
    SweepRule,
    TargetConfig,
)
from risk_config import DEFAULT_RISK_CONFIG, DEFAULT_TARGET_CONFIG
from sessions_config import SESSION_CONFIDENCE, SESSION_DST_UNCERTAINTY, SESSION_DEFINITIONS
from trading_day_config import (
    DEFAULT_TRADING_DAY_CONFIG,
    TradingDayConfig,
    load_confirmed_trading_day_from_evidence,
)


@dataclass(frozen=True)
class StrategyConfig:
    """Compose existing module configs; orchestrator distributes sub-configs."""

    sweep_rule: str = SweepRule.WICK_ONLY.value
    entry_modes: Tuple[str, ...] = COMPARE_ENTRY_MODES
    fvg: FVGConfig = DEFAULT_FVG_CONFIG
    entry: EntryConfig = DEFAULT_ENTRY_CONFIG
    risk: RiskConfig = DEFAULT_RISK_CONFIG
    target: TargetConfig = DEFAULT_TARGET_CONFIG
    expiry: ExpiryConfig = DEFAULT_EXPIRY_CONFIG
    execution: ExecutionTimeframeConfig = DEFAULT_EXECUTION_TIMEFRAME_CONFIG
    bias: BiasConfig = DEFAULT_BIAS_CONFIG
    htf_bias: HTFBiasConfig = DEFAULT_HTF_BIAS_CONFIG
    trading_day: TradingDayConfig = field(
        default_factory=load_confirmed_trading_day_from_evidence
    )
    prefer_completed_sessions_only: bool = True
    session_confidence: dict = field(default_factory=lambda: dict(SESSION_CONFIDENCE))
    dst_uncertainty: str = SESSION_DST_UNCERTAINTY
    extras: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "sweep_rule": self.sweep_rule,
            "entry_modes": list(self.entry_modes),
            "fvg": self.fvg.to_dict(),
            "entry": self.entry.to_dict(),
            "risk": self.risk.to_dict(),
            "target": self.target.to_dict(),
            "expiry": self.expiry.to_dict(),
            "execution": self.execution.to_dict(),
            "bias": self.bias.to_dict(),
            "htf_bias": self.htf_bias.to_dict(),
            "trading_day": self.trading_day.to_dict(),
            "prefer_completed_sessions_only": self.prefer_completed_sessions_only,
            "session_confidence": dict(self.session_confidence),
            "dst_uncertainty": self.dst_uncertainty,
            "session_definitions": {k: v.to_dict() for k, v in SESSION_DEFINITIONS.items()},
            "extras": dict(self.extras),
        }


DEFAULT_STRATEGY_CONFIG = StrategyConfig()
