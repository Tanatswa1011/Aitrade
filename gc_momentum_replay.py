"""Replay Phase 28 momentum candidates into journal rows."""

from __future__ import annotations

from typing import Sequence

from gc_momentum_engine import (
    analyze_candidate,
    collect_all_impulses,
    config_hash,
    momentum_session_window,
    setup_to_entry_analysis,
    vwap_context_at_impulse,
)
from gc_momentum_models import PHASE28_CANDIDATES, STRATEGY_FAMILY, STRATEGY_VERSION, GCMomentumStrategyConfig
from journal_models import SetupJournalRecord
from models import Bar
from outcome_engine import evaluate_entry_outcome


def setup_to_journal(setup, bars: Sequence[Bar], cfg: GCMomentumStrategyConfig, seq: dict) -> SetupJournalRecord:
    analysis = setup_to_entry_analysis(setup)
    _, session_end, _ = momentum_session_window(setup.trading_date)
    er = evaluate_entry_outcome(
        analysis, bars, direction=setup.direction, horizon_end_ts=session_end
    )
    vctx = vwap_context_at_impulse(bars, seq)
    return SetupJournalRecord(
        setup_id=setup.setup_id,
        symbol="GC",
        timeframe=cfg.execution_timeframe,
        trading_date=setup.trading_date,
        session="GC_NY_MOMENTUM_0820_1330",
        direction=setup.direction,
        swept_side=setup.direction,
        session_high=None,
        session_low=None,
        sweep_level=seq.get("breakout_level"),
        sweep_extreme=seq.get("low") if setup.direction == "bullish" else seq.get("high"),
        sweep_timestamp=seq.get("timestamp"),
        confirmation_source="gc_ny_momentum_continuation",
        confirmation_algorithm=cfg.pullback_mode,
        confirmation_timestamp=(setup.extras or {}).get("confirmation_timestamp"),
        confirmation_level=(setup.extras or {}).get("frozen_level"),
        confirmation_equivalence_status="not_applicable_momentum",
        fvg_low=None,
        fvg_high=None,
        fvg_midpoint=None,
        fvg_created_timestamp=None,
        status=setup.state,
        expiry_reason=setup.reason if setup.state == "EXPIRED" else None,
        invalidation_reason=setup.reason if setup.state == "INVALIDATED" else setup.risk_invalidation_reason,
        reliability_flags=["STRATEGY_FAMILY_GC_NY_MOMENTUM_CONTINUATION_V1"]
        + (["ROLL_ARTIFACT"] if seq.get("roll_artifact") else []),
        entry_results=[er],
        strategy_version=STRATEGY_VERSION,
        config_hash=config_hash(cfg),
        structure_algorithm_version=None,
        liquidity_event_id=setup.impulse_id,
        execution_timeframe=cfg.execution_timeframe,
        extras={
            "strategy_family": STRATEGY_FAMILY,
            "candidate_id": cfg.candidate_id,
            "impulse_id": setup.impulse_id,
            "entry_mode": cfg.entry_mode,
            "pullback_mode": cfg.pullback_mode,
            "volume_filter": cfg.volume_filter,
            "rvol": seq.get("rvol"),
            "range_ratio": seq.get("range_ratio"),
            "vwap_context": vctx,
            "phase": 28,
            "instrument": "GC",
            "provider": "databento:GLBX.MDP3",
            "not_phase26_v2": True,
            "not_mean_reversion": True,
        },
    )


def replay_all_momentum_candidates(
    bars: Sequence[Bar],
    candidates=PHASE28_CANDIDATES,
) -> dict[str, list[SetupJournalRecord]]:
    impulses = collect_all_impulses(bars)
    out: dict[str, list[SetupJournalRecord]] = {}
    for cfg in candidates:
        recs = []
        for seq in impulses:
            setup = analyze_candidate(seq, cfg)
            recs.append(setup_to_journal(setup, bars, cfg, seq))
        out[cfg.candidate_id] = recs
    return out
