"""Replay Phase 24 OR15 setups into journal rows."""

from __future__ import annotations

from typing import Optional, Sequence

from gc_orb15_engine import (
    analyze_candidate,
    collect_or15_events,
    config_hash,
    setup_to_entry_analysis,
)
from gc_orb15_models import (
    HORIZON_BARS,
    PHASE24_CANDIDATES,
    STRATEGY_FAMILY,
    STRATEGY_VERSION,
    ORB15StrategyConfig,
)
from journal_models import SetupJournalRecord
from models import Bar
from outcome_engine import evaluate_entry_outcome


def _horizon_end(entry_ts: int, bars: Sequence[Bar], *, max_bars: int = HORIZON_BARS) -> Optional[int]:
    after = [b for b in sorted(bars, key=lambda x: int(x.time)) if int(b.time) >= int(entry_ts)]
    if not after:
        return None
    idx = min(len(after) - 1, max_bars)
    return int(after[idx].time)


def setup_to_journal(setup, bars: Sequence[Bar], cfg: ORB15StrategyConfig) -> SetupJournalRecord:
    analysis = setup_to_entry_analysis(setup)
    horizon = None
    if setup.entry_timestamp:
        horizon = _horizon_end(int(setup.entry_timestamp), bars)
    er = evaluate_entry_outcome(
        analysis, bars, direction=setup.direction, horizon_end_ts=horizon
    )
    ev = setup.event or {}
    return SetupJournalRecord(
        setup_id=setup.setup_id,
        symbol="GC",
        timeframe=cfg.execution_timeframe,
        trading_date=setup.trading_date,
        session="GC_OR15",
        direction=setup.direction,
        swept_side=setup.direction,
        session_high=ev.get("or_high"),
        session_low=ev.get("or_low"),
        sweep_level=ev.get("or_high") if setup.direction == "bullish" else ev.get("or_low"),
        sweep_extreme=ev.get("breakout_low") if setup.direction == "bullish" else ev.get("breakout_high"),
        sweep_timestamp=ev.get("breakout_timestamp"),
        confirmation_source="gc_orb15_ohlc",
        confirmation_algorithm="OR15",
        confirmation_timestamp=ev.get("breakout_timestamp"),
        confirmation_level=ev.get("or_high") if setup.direction == "bullish" else ev.get("or_low"),
        confirmation_equivalence_status="not_applicable_ohlc",
        fvg_low=None if not setup.fvg else setup.fvg.get("low"),
        fvg_high=None if not setup.fvg else setup.fvg.get("high"),
        fvg_midpoint=None if not setup.fvg else setup.fvg.get("ce"),
        fvg_created_timestamp=None if not setup.fvg else setup.fvg.get("created_timestamp"),
        status=setup.state,
        expiry_reason=setup.reason if setup.state == "EXPIRED" else None,
        invalidation_reason=setup.reason if setup.state == "INVALIDATED" else setup.risk_invalidation_reason,
        reliability_flags=["STRATEGY_FAMILY_GC_ORB15_RETEST_FVG_V1"]
        + (["ROLL_ARTIFACT"] if ev.get("roll_artifact") else []),
        entry_results=[er],
        strategy_version=STRATEGY_VERSION,
        config_hash=config_hash(cfg),
        structure_algorithm_version=None,
        liquidity_event_id=setup.orb_breakout_event_id,
        execution_timeframe=cfg.execution_timeframe,
        extras={
            "strategy_family": STRATEGY_FAMILY,
            "candidate_id": cfg.candidate_id,
            "orb_breakout_event_id": setup.orb_breakout_event_id,
            "entry_mode": cfg.entry_mode,
            "stop_mode": cfg.stop_mode,
            "or_minutes": 15,
            "body_or_ratio": ev.get("body_or_ratio"),
            "excursion_before_entry": (setup.extras or {}).get("excursion_before_entry"),
            "retest_timestamp": setup.retest_timestamp,
            "fvg": setup.fvg,
            "phase": 24,
            "instrument": "GC",
            "contract": ev.get("contract"),
            "provider": "databento:GLBX.MDP3",
            "opposite_break_after_first": ev.get("opposite_break_after_first"),
        },
    )


def replay_all_candidates(
    bars: Sequence[Bar],
    candidates=PHASE24_CANDIDATES,
) -> dict[str, list[SetupJournalRecord]]:
    ordered = sorted(bars, key=lambda b: int(b.time))
    _, events, _ = collect_or15_events(ordered)
    out: dict[str, list[SetupJournalRecord]] = {}
    for cfg in candidates:
        recs = []
        for ev in events:
            setup = analyze_candidate(ev, ordered, cfg)
            recs.append(setup_to_journal(setup, ordered, cfg))
        out[cfg.candidate_id] = recs
    return out
