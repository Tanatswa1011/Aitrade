"""Convert journal JSONL dicts ↔ SetupJournalRecord for Phase 18 analysis."""

from __future__ import annotations

from typing import Any

from journal_models import HistoricalEntryResult, SetupJournalRecord


_ENTRY_FIELDS = {
    "mode",
    "triggered",
    "entry_price",
    "entry_timestamp",
    "entry_depth",
    "max_retrace_depth",
    "stop_price",
    "risk_distance",
    "fixed_rr_targets",
    "opposite_liquidity_price",
    "rr_to_opposite",
    "outcome",
    "max_favorable_excursion",
    "max_adverse_excursion",
    "mfe_r",
    "mae_r",
    "mfe_points",
    "mae_points",
    "exit_timestamp",
    "ambiguity_flags",
    "event_timestamps",
    "extras",
}

_RECORD_FIELDS = {
    "setup_id",
    "symbol",
    "timeframe",
    "trading_date",
    "session",
    "direction",
    "swept_side",
    "session_high",
    "session_low",
    "sweep_level",
    "sweep_extreme",
    "sweep_timestamp",
    "confirmation_source",
    "confirmation_algorithm",
    "confirmation_timestamp",
    "confirmation_level",
    "confirmation_equivalence_status",
    "fvg_low",
    "fvg_high",
    "fvg_midpoint",
    "fvg_created_timestamp",
    "status",
    "expiry_reason",
    "invalidation_reason",
    "reliability_flags",
    "strategy_version",
    "config_hash",
    "structure_algorithm_version",
    "bars_sweep_to_choch",
    "bars_choch_to_fvg",
    "bars_fvg_to_entry",
    "daily_bias",
    "h4_bias",
    "htf_alignment",
    "execution_timeframe",
    "setup_vs_daily",
    "setup_vs_h4",
    "daily_bias_confidence",
    "h4_bias_confidence",
    "daily_structure_break_time",
    "h4_structure_break_time",
    "daily_bars_since_break",
    "h4_bars_since_break",
    "bias_algorithm_version",
    "liquidity_event_id",
    "extras",
}


def entry_from_dict(d: dict[str, Any]) -> HistoricalEntryResult:
    kwargs = {k: d.get(k) for k in _ENTRY_FIELDS if k in d or k in (
        "mode", "triggered", "outcome"
    )}
    kwargs.setdefault("mode", d.get("mode") or "unknown")
    kwargs.setdefault("triggered", bool(d.get("triggered")))
    kwargs.setdefault("outcome", d.get("outcome") or "NOT_TRIGGERED")
    kwargs.setdefault("fixed_rr_targets", d.get("fixed_rr_targets") or [])
    kwargs.setdefault("ambiguity_flags", d.get("ambiguity_flags") or [])
    kwargs.setdefault("event_timestamps", d.get("event_timestamps") or {})
    kwargs.setdefault("extras", d.get("extras") or {})
    # required optionals defaulted
    for k in (
        "entry_price",
        "entry_timestamp",
        "entry_depth",
        "max_retrace_depth",
        "stop_price",
        "risk_distance",
        "opposite_liquidity_price",
        "rr_to_opposite",
        "max_favorable_excursion",
        "max_adverse_excursion",
        "mfe_r",
        "mae_r",
        "mfe_points",
        "mae_points",
        "exit_timestamp",
    ):
        kwargs.setdefault(k, d.get(k))
    return HistoricalEntryResult(**kwargs)


def record_from_dict(d: dict[str, Any]) -> SetupJournalRecord:
    entries = [entry_from_dict(e) for e in (d.get("entry_results") or [])]
    kwargs: dict[str, Any] = {}
    for k in _RECORD_FIELDS:
        if k == "entry_results":
            continue
        if k in d:
            kwargs[k] = d[k]
    kwargs.setdefault("setup_id", d.get("setup_id") or "")
    kwargs.setdefault("symbol", d.get("symbol") or "")
    kwargs.setdefault("timeframe", d.get("timeframe") or d.get("execution_timeframe") or "")
    kwargs.setdefault("session", d.get("session") or "")
    kwargs.setdefault("status", d.get("status") or "")
    kwargs.setdefault("reliability_flags", d.get("reliability_flags") or [])
    kwargs.setdefault("strategy_version", d.get("strategy_version") or "")
    kwargs.setdefault("config_hash", d.get("config_hash") or "")
    kwargs.setdefault("extras", d.get("extras") or {})
    kwargs["entry_results"] = entries
    return SetupJournalRecord(**kwargs)


def records_from_dicts(rows: list[dict[str, Any]]) -> list[SetupJournalRecord]:
    return [record_from_dict(r) for r in rows]
