"""Higher-timeframe structure-bias configuration (Phase 12)."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


ALGORITHM_VERSION = "structure_break_v1"


@dataclass(frozen=True)
class HTFBiasConfig:
    """
    Deterministic HTF market-structure bias knobs.

    Defaults are conservative fractals + close breaks.
    Not performance-tuned.
    """

    daily_swing_left: int = 2
    daily_swing_right: int = 2
    h4_swing_left: int = 2
    h4_swing_right: int = 2
    break_mode: str = "close"  # close only for default
    require_close_break: bool = True
    neutral_when_unclear: bool = True
    include_liquidity_context: bool = True
    # Confidence thresholds (bars since break on that TF)
    confidence_high_max_bars: int = 5
    confidence_medium_max_bars: int = 20
    # Minimum closed bars before attempting structure
    min_closed_bars: int = 8
    algorithm_version: str = ALGORITHM_VERSION
    extras: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def swing_left(self, timeframe: str) -> int:
        return self.daily_swing_left if timeframe == "1D" else self.h4_swing_left

    def swing_right(self, timeframe: str) -> int:
        return self.daily_swing_right if timeframe == "1D" else self.h4_swing_right


DEFAULT_HTF_BIAS_CONFIG = HTFBiasConfig()
