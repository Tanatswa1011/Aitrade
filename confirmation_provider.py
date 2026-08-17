"""Confirmation providers: live LuxAlgo vs historical internal structure."""

from __future__ import annotations

from typing import Optional, Protocol, Sequence

from historical_structure import detect_internal_choch
from historical_structure_config import (
    DEFAULT_HISTORICAL_STRUCTURE_CONFIG,
    HistoricalStructureConfig,
)
from models import Bar, StructureConfirmation


class ConfirmationProvider(Protocol):
    """Produces canonical StructureConfirmation events for the setup engine."""

    source_name: str

    def get_confirmations(
        self, bars: Sequence[Bar]
    ) -> list[StructureConfirmation]:
        ...


class LuxAlgoLiveProvider:
    """
    Live path wrapper: passes through already-fetched LuxAlgo events.

    Does not scrape history. Live CDP fetch remains in luxalgo_structure.py.
    """

    source_name = "luxalgo"

    def __init__(self, events: Sequence[StructureConfirmation] = ()):
        self._events = list(events)

    def get_confirmations(
        self, bars: Sequence[Bar]
    ) -> list[StructureConfirmation]:
        del bars  # live events are pre-aligned to chart bars
        return list(self._events)


class HistoricalStructureProvider:
    """
    Backtest structure confirmation from OHLC via internal_choch_v1.

    equivalence_status defaults to unvalidated_against_luxalgo.
    """

    source_name = "internal_structure"

    def __init__(
        self, config: Optional[HistoricalStructureConfig] = None
    ):
        self.config = config or DEFAULT_HISTORICAL_STRUCTURE_CONFIG

    def get_confirmations(
        self, bars: Sequence[Bar]
    ) -> list[StructureConfirmation]:
        return detect_internal_choch(bars, self.config)
