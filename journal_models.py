"""Historical setup journal models (analysis-only; no broker state)."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Optional


# Outcome labels — not win/loss.
OUTCOME_STOP_HIT = "STOP_HIT"
OUTCOME_1R_HIT = "1R_HIT"
OUTCOME_2R_HIT = "2R_HIT"
OUTCOME_3R_HIT = "3R_HIT"
OUTCOME_OPPOSITE_LIQUIDITY_HIT = "OPPOSITE_LIQUIDITY_HIT"
OUTCOME_EXPIRED_WITHOUT_EXIT = "EXPIRED_WITHOUT_EXIT"
OUTCOME_AMBIGUOUS_INTRABAR = "AMBIGUOUS_INTRABAR"
OUTCOME_NOT_TRIGGERED = "NOT_TRIGGERED"
OUTCOME_NO_RISK_PLAN = "NO_RISK_PLAN"


@dataclass(frozen=True)
class HistoricalEntryResult:
    """Per-entry-mode historical result for one setup liquidity event."""

    mode: str
    triggered: bool
    entry_price: Optional[float]
    entry_timestamp: Optional[int]
    entry_depth: Optional[float]
    max_retrace_depth: Optional[float]
    stop_price: Optional[float]
    risk_distance: Optional[float]
    fixed_rr_targets: list[dict[str, Any]] = field(default_factory=list)
    opposite_liquidity_price: Optional[float] = None
    rr_to_opposite: Optional[float] = None
    outcome: str = OUTCOME_NOT_TRIGGERED
    max_favorable_excursion: Optional[float] = None
    max_adverse_excursion: Optional[float] = None
    mfe_r: Optional[float] = None
    mae_r: Optional[float] = None
    mfe_points: Optional[float] = None
    mae_points: Optional[float] = None
    exit_timestamp: Optional[int] = None
    ambiguity_flags: list[str] = field(default_factory=list)
    event_timestamps: dict[str, Any] = field(default_factory=dict)
    extras: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class SetupJournalRecord:
    """One deterministic historical TradeSetup journal row."""

    setup_id: str
    symbol: str
    timeframe: str
    trading_date: Optional[str]
    session: str
    direction: Optional[str]
    swept_side: Optional[str]
    session_high: Optional[float]
    session_low: Optional[float]
    sweep_level: Optional[float]
    sweep_extreme: Optional[float]
    sweep_timestamp: Optional[int]
    confirmation_source: Optional[str]
    confirmation_algorithm: Optional[str]
    confirmation_timestamp: Optional[int]
    confirmation_level: Optional[float]
    confirmation_equivalence_status: Optional[str]
    fvg_low: Optional[float]
    fvg_high: Optional[float]
    fvg_midpoint: Optional[float]
    fvg_created_timestamp: Optional[int]
    status: str
    expiry_reason: Optional[str]
    invalidation_reason: Optional[str]
    reliability_flags: list[str]
    entry_results: list[HistoricalEntryResult]
    strategy_version: str
    config_hash: str
    structure_algorithm_version: Optional[str]
    bars_sweep_to_choch: Optional[int] = None
    bars_choch_to_fvg: Optional[int] = None
    bars_fvg_to_entry: Optional[dict[str, Optional[int]]] = None
    # Phase 11 MTF fields (unknown when bias provider absent)
    daily_bias: Optional[str] = None
    h4_bias: Optional[str] = None
    htf_alignment: Optional[str] = None
    execution_timeframe: Optional[str] = None
    setup_vs_daily: Optional[str] = None
    setup_vs_h4: Optional[str] = None
    daily_bias_confidence: Optional[str] = None
    h4_bias_confidence: Optional[str] = None
    daily_structure_break_time: Optional[int] = None
    h4_structure_break_time: Optional[int] = None
    daily_bars_since_break: Optional[int] = None
    h4_bars_since_break: Optional[int] = None
    bias_algorithm_version: Optional[str] = None
    liquidity_event_id: Optional[str] = None
    extras: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["entry_results"] = [e.to_dict() for e in self.entry_results]
        # Phase 13 reporting aliases (canonical fields retained).
        d["daily_confidence"] = self.daily_bias_confidence or "unknown"
        d["h4_confidence"] = self.h4_bias_confidence or "unknown"
        d["daily_break_timestamp"] = self.daily_structure_break_time
        d["h4_break_timestamp"] = self.h4_structure_break_time
        return d


@dataclass(frozen=True)
class ReplayCoverage:
    expected_sessions: int
    complete_sessions: int
    incomplete_sessions: int
    missing_bars_sessions: int
    skipped_sessions: int
    details: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ReplayResult:
    symbol: str
    timeframe: str
    period_start: Optional[int]
    period_end: Optional[int]
    total_sessions: int
    total_sweeps: int
    total_setups: int
    journal_records: list[SetupJournalRecord]
    coverage: ReplayCoverage
    errors: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "period_start": self.period_start,
            "period_end": self.period_end,
            "total_sessions": self.total_sessions,
            "total_sweeps": self.total_sweeps,
            "total_setups": self.total_setups,
            "journal_records": [r.to_dict() for r in self.journal_records],
            "coverage": self.coverage.to_dict(),
            "errors": list(self.errors),
            "warnings": list(self.warnings),
            "metadata": dict(self.metadata),
        }
