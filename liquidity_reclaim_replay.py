"""Historical replay for liquidity_reclaim_v1 (isolated journal)."""

from __future__ import annotations

from typing import Any, Optional, Sequence

from journal_models import (
    HistoricalEntryResult,
    ReplayCoverage,
    ReplayResult,
    SetupJournalRecord,
)
from liquidity_reclaim_engine import (
    analyze_session_liquidity_reclaim,
    config_hash,
    setup_to_entry_analysis,
)
from liquidity_reclaim_models import (
    PHASE21_CANDIDATES,
    ReclaimStrategyConfig,
    STRATEGY_FAMILY,
    STRATEGY_VERSION,
)
from models import PRIMARY_SESSIONS, Bar, CoverageStatus, SessionRange
from ohlc_sessions import compute_session_ranges
from outcome_engine import evaluate_entry_outcome
from replay_engine import _horizon_end_for_setup
from sessions_config import SESSION_DST_UNCERTAINTY
from timeframe import timeframe_seconds


def _resolution_minutes(tf: str) -> int:
    sec = timeframe_seconds(tf) or 300
    return max(1, int(sec // 60))


def setup_to_journal_record(
    setup,
    session: SessionRange,
    bars: Sequence[Bar],
    *,
    cfg: ReclaimStrategyConfig,
) -> SetupJournalRecord:
    horizon = _horizon_end_for_setup(session)
    analysis = setup_to_entry_analysis(setup)
    er = evaluate_entry_outcome(
        analysis,
        bars,
        direction=setup.direction or "",
        horizon_end_ts=horizon,
    )
    # Only count as triggered entry result when we attempted entry path
    entry_results = [er]
    ch = config_hash(cfg)
    ev = setup.event
    return SetupJournalRecord(
        setup_id=setup.setup_id,
        symbol=setup.symbol,
        timeframe=cfg.execution_timeframe,
        trading_date=setup.trading_date,
        session=setup.session,
        direction=setup.direction,
        swept_side=ev.side,
        session_high=ev.session_range_high,
        session_low=ev.session_range_low,
        sweep_level=ev.liquidity_level,
        sweep_extreme=ev.sweep_extreme,
        sweep_timestamp=ev.sweep_timestamp if ev.sweep_timestamp else None,
        confirmation_source="ohlc_reclaim",
        confirmation_algorithm=cfg.confirmation_mode,
        confirmation_timestamp=ev.confirmation_timestamp,
        confirmation_level=ev.confirmation_level,
        confirmation_equivalence_status="not_applicable_ohlc_only",
        fvg_low=None,
        fvg_high=None,
        fvg_midpoint=None,
        fvg_created_timestamp=None,
        status=setup.state,
        expiry_reason=setup.reason if setup.state == "EXPIRED" else None,
        invalidation_reason=setup.reason if setup.state == "INVALIDATED" else setup.risk_invalidation_reason,
        reliability_flags=["STRATEGY_FAMILY_LIQUIDITY_RECLAIM_V1"],
        entry_results=entry_results,
        strategy_version=STRATEGY_VERSION,
        config_hash=ch,
        structure_algorithm_version=None,
        bars_sweep_to_choch=ev.reclaim_bars_after_sweep,
        bars_choch_to_fvg=None
        if (ev.extras or {}).get("bars_from_reclaim") is None
        else int((ev.extras or {}).get("bars_from_reclaim")),
        bars_fvg_to_entry=None,
        daily_bias=None,
        h4_bias=None,
        htf_alignment="no_hard_filter",
        execution_timeframe=cfg.execution_timeframe,
        setup_vs_daily=None,
        setup_vs_h4=None,
        liquidity_event_id=setup.liquidity_event_id,
        extras={
            "strategy_family": STRATEGY_FAMILY,
            "candidate_id": cfg.candidate_id,
            "confirmation_mode": cfg.confirmation_mode,
            "break_mode": cfg.break_mode,
            "entry_mode": cfg.entry_mode,
            "sweep_penetration": ev.sweep_penetration,
            "reclaim_bars_after_sweep": ev.reclaim_bars_after_sweep,
            "bars_from_reclaim": (ev.extras or {}).get("bars_from_reclaim"),
            "horizon_end_ts": horizon,
            "phase": "phase21",
            "data_provider": "openbb",
            "underlying_provider": "tiingo",
            "source_symbol": "XAUUSD",
            "feed_equivalence_class": "CLOSE_EQUIVALENT",
        },
    )


def replay_liquidity_reclaim(
    bars: Sequence[Bar],
    *,
    symbol: str,
    cfg: ReclaimStrategyConfig,
    session_names: Sequence[str] = PRIMARY_SESSIONS,
) -> ReplayResult:
    ordered = sorted(bars, key=lambda b: int(b.time))
    warnings = [
        SESSION_DST_UNCERTAINTY,
        "Phase 21 liquidity_reclaim_v1 uses OHLC only — no LuxAlgo / CHoCH / FVG.",
    ]
    if not ordered:
        return ReplayResult(
            symbol=symbol,
            timeframe=cfg.execution_timeframe,
            period_start=None,
            period_end=None,
            total_sessions=0,
            total_sweeps=0,
            total_setups=0,
            journal_records=[],
            coverage=ReplayCoverage(0, 0, 0, 0, 0),
            warnings=warnings,
            metadata={"config_hash": config_hash(cfg), "strategy_family": STRATEGY_FAMILY},
        )

    now_ts = int(ordered[-1].time)
    sessions = compute_session_ranges(
        ordered,
        resolution_minutes=_resolution_minutes(cfg.execution_timeframe),
        now_ts=now_ts,
        names=session_names,
    )
    complete = sum(1 for s in sessions if s.complete)
    incomplete = sum(1 for s in sessions if not s.complete and s.coverage_status != CoverageStatus.MISSING.value)
    missing = sum(1 for s in sessions if s.coverage_status == CoverageStatus.MISSING.value)

    records: list[SetupJournalRecord] = []
    sweeps = 0
    setups = 0
    for session in sessions:
        if not session.complete:
            continue
        for side in ("high", "low"):
            setup = analyze_session_liquidity_reclaim(
                session, ordered, symbol=symbol, cfg=cfg, side=side
            )
            if setup.event.sweep_timestamp:
                sweeps += 1
            # Journal all attempted sweeps (including expired/invalid) for funnel
            if setup.event.sweep_timestamp and setup.event.sweep_timestamp > 0:
                setups += 1
                records.append(setup_to_journal_record(setup, session, ordered, cfg=cfg))

    return ReplayResult(
        symbol=symbol,
        timeframe=cfg.execution_timeframe,
        period_start=int(ordered[0].time),
        period_end=int(ordered[-1].time),
        total_sessions=len(sessions),
        total_sweeps=sweeps,
        total_setups=setups,
        journal_records=records,
        coverage=ReplayCoverage(
            expected_sessions=len(sessions),
            complete_sessions=complete,
            incomplete_sessions=incomplete,
            missing_bars_sessions=missing,
            skipped_sessions=0,
        ),
        warnings=warnings,
        metadata={
            "config_hash": config_hash(cfg),
            "strategy_family": STRATEGY_FAMILY,
            "candidate_id": cfg.candidate_id,
        },
    )


def replay_all_candidates(
    bars_by_tf: dict[str, Sequence[Bar]],
    *,
    symbol: str = "OANDA:XAUUSD",
    candidates: Sequence[ReclaimStrategyConfig] = PHASE21_CANDIDATES,
) -> dict[str, ReplayResult]:
    out: dict[str, ReplayResult] = {}
    for cfg in candidates:
        bars = bars_by_tf.get(cfg.execution_timeframe) or []
        out[cfg.candidate_id] = replay_liquidity_reclaim(bars, symbol=symbol, cfg=cfg)
    return out
