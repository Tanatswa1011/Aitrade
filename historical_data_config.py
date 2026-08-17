"""Historical data infrastructure config (not strategy config)."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Optional


@dataclass(frozen=True)
class HistoricalDataConfig:
    """Data-infrastructure knobs only — isolated from StrategyConfig."""

    provider: str = "local"  # tradingview | openbb | local
    openbb_provider: Optional[str] = None  # tiingo | yfinance | fmp | ...
    symbol: str = "XAUUSD"
    timeframe: str = "5m"
    start: Optional[str] = None  # YYYY-MM-DD or unix
    end: Optional[str] = None
    chunk_days: int = 30
    cache: bool = True
    equivalence_required: bool = True
    instrument_type: str = "unknown"
    extras: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


DEFAULT_HISTORICAL_DATA_CONFIG = HistoricalDataConfig()
