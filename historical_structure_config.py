"""Historical structure-confirmation configuration (backtest CHoCH approximation)."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Optional


EQUIVALENCE_UNVALIDATED = "unvalidated_against_luxalgo"
EQUIVALENCE_PARTIAL = "partially_validated"
EQUIVALENCE_VALIDATED = "validated"

ALGORITHM_VERSION_V1 = "internal_choch_v1"


@dataclass(frozen=True)
class HistoricalStructureConfig:
    """
    Parameters for internal historical CHoCH approximation.

    Defaults are conservative fractals + close breaks.
    Does NOT claim LuxAlgo equivalence (see equivalence_status).
    """

    swing_left: int = 2
    swing_right: int = 2
    break_mode: str = "close"  # close | wick
    require_close_break: bool = True
    minimum_break: float = 0.0
    algorithm_version: str = ALGORITHM_VERSION_V1
    equivalence_status: str = EQUIVALENCE_UNVALIDATED
    extras: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


DEFAULT_HISTORICAL_STRUCTURE_CONFIG = HistoricalStructureConfig()
