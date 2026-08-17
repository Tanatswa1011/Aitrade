"""Replay Phase 25 VWAP mean-reversion setups into journal rows."""

from __future__ import annotations

from typing import Sequence

from gc_vwap_engine import (
    analyze_candidate,
    collect_all_sequences,
    config_hash,
    evaluate_vwap_touch_after_entry,
    session_window,
    setup_to_entry_analysis,
)
from gc_vwap_models import PHASE25_CANDIDATES, STRATEGY_FAMILY, STRATEGY_VERSION, GCVWAPStrategyConfig
from journal_models import SetupJournalRecord
from models import Bar
from outcome_engine import evaluate_entry_outcome


def setup_to_journal(setup, bars: Sequence[Bar], cfg: GCVWAPStrategyConfig) -> SetupJournalRecord:
    analysis = setup_to_entry_analysis(setup)
    _, session_end, _ = session_window(setup.trading_date)
    horizon = session_end
    er = evaluate_entry_outcome(
        analysis, bars, direction=setup.direction, horizon_end_ts=horizon
    )
    vwap_touch = {"vwap_hit": False}
    if setup.entry_triggered and setup.entry_timestamp and setup.stop_price is not None:
        vwap_touch = evaluate_vwap_touch_after_entry(
            bars=bars,
            trading_date=setup.trading_date,
            entry_ts=int(setup.entry_timestamp),
            direction=setup.direction,
            stop_price=float(setup.stop_price),
            session_end=session_end,
        )
    ev = setup.event or {}
    return SetupJournalRecord(
        setup_id=setup.setup_id,
        symbol="GC",
        timeframe=cfg.execution_timeframe,
        trading_date=setup.trading_date,
        session="GC_VWAP_0820_1330",
        direction=setup.direction,
        swept_side=setup.direction,
        session_high=None,
        session_low=None,
        sweep_level=ev.get("frozen_2sig_band"),
        sweep_extreme=ev.get("extension_extreme"),
        sweep_timestamp=ev.get("first_extension_timestamp"),
        confirmation_source="gc_vwap_mean_reversion",
        confirmation_algorithm=cfg.confirmation_mode,
        confirmation_timestamp=(setup.extras or {}).get("confirmation_timestamp"),
        confirmation_level=ev.get("frozen_2sig_band"),
        confirmation_equivalence_status="not_applicable_vwap",
        fvg_low=None,
        fvg_high=None,
        fvg_midpoint=None,
        fvg_created_timestamp=None,
        status=setup.state,
        expiry_reason=setup.reason if setup.state == "EXPIRED" else None,
        invalidation_reason=setup.reason if setup.state == "INVALIDATED" else setup.risk_invalidation_reason,
        reliability_flags=["STRATEGY_FAMILY_GC_VWAP_MEAN_REVERSION_V1"]
        + (["ROLL_ARTIFACT"] if ev.get("roll_artifact") else []),
        entry_results=[er],
        strategy_version=STRATEGY_VERSION,
        config_hash=config_hash(cfg),
        structure_algorithm_version=None,
        liquidity_event_id=setup.vwap_extension_event_id,
        execution_timeframe=cfg.execution_timeframe,
        extras={
            "strategy_family": STRATEGY_FAMILY,
            "candidate_id": cfg.candidate_id,
            "vwap_extension_event_id": setup.vwap_extension_event_id,
            "entry_mode": cfg.entry_mode,
            "confirmation_mode": cfg.confirmation_mode,
            "max_abs_z": (setup.extras or {}).get("max_abs_z") or ev.get("max_abs_z"),
            "vwap_distance_at_entry": (setup.extras or {}).get("vwap_distance_at_entry"),
            "vwap_distance_r": (setup.extras or {}).get("vwap_distance_r"),
            "vwap_touch": vwap_touch,
            "extension_side": ev.get("extension_side"),
            "phase": 25,
            "instrument": "GC",
            "provider": "databento:GLBX.MDP3",
        },
    )


def replay_all_candidates(
    bars: Sequence[Bar],
    candidates=PHASE25_CANDIDATES,
) -> dict[str, list[SetupJournalRecord]]:
    seqs = collect_all_sequences(bars)
    out: dict[str, list[SetupJournalRecord]] = {}
    for cfg in candidates:
        recs = []
        for seq in seqs:
            setup = analyze_candidate(seq, cfg)
            recs.append(setup_to_journal(setup, bars, cfg))
        out[cfg.candidate_id] = recs
    return out
