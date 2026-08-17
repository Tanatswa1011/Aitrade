"""Execution timeframe configuration — v1 requires confirmation/FVG/entry equality."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Optional

from timeframe import EXECUTION_TIMEFRAMES, Timeframe, normalize_timeframe


class ExecutionTimeframeConfigError(ValueError):
    """Invalid execution timeframe combination for Phase 11."""


@dataclass(frozen=True)
class ExecutionTimeframeConfig:
    """
    v1: confirmation_timeframe == fvg_timeframe == entry_timeframe == timeframe.

    Mixed combinations (e.g. 15m confirmation + 5m entry) are rejected.
    """

    timeframe: str = Timeframe.M5.value
    confirmation_timeframe: Optional[str] = None
    fvg_timeframe: Optional[str] = None
    entry_timeframe: Optional[str] = None
    extras: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        tf = normalize_timeframe(self.timeframe)
        if tf is None or tf not in EXECUTION_TIMEFRAMES:
            raise ExecutionTimeframeConfigError(
                f"execution timeframe must be one of {EXECUTION_TIMEFRAMES}, got {self.timeframe!r}"
            )
        conf = normalize_timeframe(self.confirmation_timeframe) if self.confirmation_timeframe else tf
        fvg = normalize_timeframe(self.fvg_timeframe) if self.fvg_timeframe else tf
        entry = normalize_timeframe(self.entry_timeframe) if self.entry_timeframe else tf
        if None in (conf, fvg, entry):
            raise ExecutionTimeframeConfigError("could not normalize confirmation/fvg/entry timeframe")
        if not (conf == fvg == entry == tf):
            raise ExecutionTimeframeConfigError(
                "Phase 11 requires confirmation_timeframe == fvg_timeframe == "
                f"entry_timeframe == timeframe; got conf={conf}, fvg={fvg}, "
                f"entry={entry}, timeframe={tf}"
            )
        object.__setattr__(self, "timeframe", tf)
        object.__setattr__(self, "confirmation_timeframe", conf)
        object.__setattr__(self, "fvg_timeframe", fvg)
        object.__setattr__(self, "entry_timeframe", entry)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


DEFAULT_EXECUTION_TIMEFRAME_CONFIG = ExecutionTimeframeConfig()


@dataclass(frozen=True)
class BiasConfig:
    """Bias provider selection for StrategyConfig."""

    provider: str = "structure"  # structure | manual | unknown | historical
    method: str = "structure_break_v1"
    extras: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


DEFAULT_BIAS_CONFIG = BiasConfig()
