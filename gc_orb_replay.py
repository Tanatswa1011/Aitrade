"""Replay GC ORB setups into journal rows."""

from __future__ import annotations

from typing import Any, Optional, Sequence

from gc_orb_engine import (
    analyze_breakout_for_candidate,
    build_opening_range,
    config_hash,
    detect_roll_gap_timestamps,
    find_first_breakouts,
    setup_to_entry_analysis,
    trading_dates_in_bars,
)
from gc_orb_models import (
    PHASE22_CANDIDATES,
    GCORBStrategyConfig,
    STRATEGY_FAMILY,
    STRATEGY_VERSION,
)
from journal_models import HistoricalEntryResult, SetupJournalRecord
from models import Bar
from outcome_engine import evaluate_entry_outcome


def _horizon_end(entry_ts: int, bars: Sequence[Bar], *, max_bars: int = 78) -> Optional[int]:
    """~6.5 hours of 5m bars after entry, or last available bar."""
    after = [b for b in sorted(bars, key=lambda x: int(x.time)) if int(b.time) >= int(entry_ts)]
    if not after:
        return None
    idx = min(len(after) - 1, max_bars)
    return int(after[idx].time)


def setup_to_journal(setup, bars: Sequence[Bar], cfg: GCORBStrategyConfig) -> SetupJournalRecord:
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
        session="GC_OR",
        direction=setup.direction,
        swept_side=setup.direction,
        session_high=ev.get("or_high"),
        session_low=ev.get("or_low"),
        sweep_level=ev.get("or_high") if setup.direction == "bullish" else ev.get("or_low"),
        sweep_extreme=ev.get("breakout_low") if setup.direction == "bullish" else ev.get("breakout_high"),
        sweep_timestamp=ev.get("breakout_timestamp"),
        confirmation_source="gc_orb_ohlc_volume",
        confirmation_algorithm=f"OR{cfg.or_minutes}",
        confirmation_timestamp=ev.get("breakout_timestamp"),
        confirmation_level=ev.get("or_high") if setup.direction == "bullish" else ev.get("or_low"),
        confirmation_equivalence_status="not_applicable_ohlc_volume",
        fvg_low=None,
        fvg_high=None,
        fvg_midpoint=None,
        fvg_created_timestamp=None,
        status=setup.state,
        expiry_reason=setup.reason if setup.state == "EXPIRED" else None,
        invalidation_reason=setup.reason if setup.state == "INVALIDATED" else setup.risk_invalidation_reason,
        reliability_flags=["STRATEGY_FAMILY_GC_ORB_VOLUME_V1"]
        + (["ROLL_ARTIFACT"] if ev.get("roll_artifact") else []),
        entry_results=[er],
        strategy_version=STRATEGY_VERSION,
        config_hash=config_hash(cfg),
        structure_algorithm_version=None,
        liquidity_event_id=setup.breakout_id,
        execution_timeframe=cfg.execution_timeframe,
        extras={
            "strategy_family": STRATEGY_FAMILY,
            "candidate_id": cfg.candidate_id,
            "breakout_id": setup.breakout_id,
            "rvol": ev.get("rvol"),
            "body_or_ratio": ev.get("body_or_ratio"),
            "volume_ok": ev.get("volume_ok"),
            "displacement_ok": ev.get("displacement_ok"),
            "roll_artifact": ev.get("roll_artifact"),
            "volume_filter": cfg.volume_filter,
            "displacement_filter": cfg.displacement_filter,
            "entry_mode": cfg.entry_mode,
            "or_minutes": cfg.or_minutes,
            "retest_timestamp": setup.retest_timestamp,
            "phase": 22,
            "instrument": "GC",
            "contract": ev.get("contract"),
            "provider": "openbb:yfinance",
        },
    )


def collect_or30_events(bars: Sequence[Bar]) -> tuple[list, list, set[int]]:
    ordered = sorted(bars, key=lambda b: int(b.time))
    roll_flags = detect_roll_gap_timestamps(ordered)
    dates = trading_dates_in_bars(ordered)
    opening_ranges = []
    events = []
    for td in dates:
        orng = build_opening_range(ordered, td, or_minutes=30)
        opening_ranges.append(orng)
        if not orng.complete:
            continue
        events.extend(find_first_breakouts(ordered, orng, roll_flags=roll_flags))
    return opening_ranges, events, roll_flags


def replay_candidate(
    bars: Sequence[Bar],
    cfg: GCORBStrategyConfig,
    *,
    events: Optional[list] = None,
    roll_flags: Optional[set[int]] = None,
) -> list[SetupJournalRecord]:
    ordered = sorted(bars, key=lambda b: int(b.time))
    if events is None:
        _, events, roll_flags = collect_or30_events(ordered)
    records = []
    for ev in events:
        # Recompute with cfg or_minutes if needed — baseline is OR30 events
        setup = analyze_breakout_for_candidate(ev, ordered, cfg)
        records.append(setup_to_journal(setup, ordered, cfg))
    return records


def replay_all_candidates(bars: Sequence[Bar], candidates=PHASE22_CANDIDATES) -> dict[str, list[SetupJournalRecord]]:
    _, events, _ = collect_or30_events(bars)
    out = {}
    for cfg in candidates:
        # If candidate uses different OR length, rebuild events
        if cfg.or_minutes != 30:
            ordered = sorted(bars, key=lambda b: int(b.time))
            roll = detect_roll_gap_timestamps(ordered)
            evs = []
            for td in trading_dates_in_bars(ordered):
                orng = build_opening_range(ordered, td, or_minutes=cfg.or_minutes)
                if orng.complete:
                    evs.extend(find_first_breakouts(ordered, orng, roll_flags=roll))
            out[cfg.candidate_id] = replay_candidate(bars, cfg, events=evs)
        else:
            out[cfg.candidate_id] = replay_candidate(bars, cfg, events=events)
    return out
