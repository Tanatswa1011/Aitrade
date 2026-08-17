"""Strategy / config versioning for historical journal records."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Optional

from historical_structure_config import (
    DEFAULT_HISTORICAL_STRUCTURE_CONFIG,
    HistoricalStructureConfig,
)
from strategy_config import DEFAULT_STRATEGY_CONFIG, StrategyConfig

STRATEGY_VERSION = "v1.phase17"


def config_payload_for_hash(
    strategy_config: Optional[StrategyConfig] = None,
    structure_config: Optional[HistoricalStructureConfig] = None,
) -> dict[str, Any]:
    """Relevant knobs only — exclude display/runtime noise."""
    sc = strategy_config or DEFAULT_STRATEGY_CONFIG
    hc = structure_config or DEFAULT_HISTORICAL_STRUCTURE_CONFIG
    return {
        "strategy_version": STRATEGY_VERSION,
        "sweep_rule": sc.sweep_rule,
        "entry_modes": list(sc.entry_modes),
        "fvg": sc.fvg.to_dict(),
        "entry": sc.entry.to_dict(),
        "risk": sc.risk.to_dict(),
        "target": sc.target.to_dict(),
        "expiry": sc.expiry.to_dict(),
        "execution": sc.execution.to_dict(),
        "bias": sc.bias.to_dict(),
        "htf_bias": sc.htf_bias.to_dict(),
        "trading_day": sc.trading_day.to_dict(),
        "prefer_completed_sessions_only": sc.prefer_completed_sessions_only,
        "historical_structure": {
            "swing_left": hc.swing_left,
            "swing_right": hc.swing_right,
            "break_mode": hc.break_mode,
            "require_close_break": hc.require_close_break,
            "minimum_break": hc.minimum_break,
            "algorithm_version": hc.algorithm_version,
        },
    }


def compute_config_hash(
    strategy_config: Optional[StrategyConfig] = None,
    structure_config: Optional[HistoricalStructureConfig] = None,
) -> str:
    payload = config_payload_for_hash(strategy_config, structure_config)
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
