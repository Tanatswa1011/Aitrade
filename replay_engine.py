"""Historical session replay engine — reuses Phase 2–9 modules."""

from __future__ import annotations

from typing import Any, Optional, Sequence

from confirmation_provider import (
    ConfirmationProvider,
    HistoricalStructureProvider,
)
from historical_structure_config import (
    DEFAULT_HISTORICAL_STRUCTURE_CONFIG,
    HistoricalStructureConfig,
)
from journal_models import (
    ReplayCoverage,
    ReplayResult,
    SetupJournalRecord,
)
from models import Bar, CoverageStatus, PRIMARY_SESSIONS, SessionRange, SetupStatus
from ohlc_sessions import compute_session_ranges
from outcome_engine import evaluate_entry_outcome
from risk_plan import sweep_extreme
from sessions_config import SESSION_DST_UNCERTAINTY
from setup_engine import analyze_session_setup
from setup_expiry import resolve_setup_window
from strategy_config import DEFAULT_STRATEGY_CONFIG, StrategyConfig
from strategy_version import STRATEGY_VERSION, compute_config_hash


def _resolution_minutes(timeframe: str) -> int:
    raw = str(timeframe).strip().lower()
    if raw.isdigit():
        return max(1, int(raw))
    if raw.endswith("m") and raw[:-1].isdigit():
        return max(1, int(raw[:-1]))
    if raw in ("1h", "60"):
        return 60
    return 5


def _bars_between(bars: Sequence[Bar], t0: Optional[int], t1: Optional[int]) -> Optional[int]:
    if t0 is None or t1 is None:
        return None
    ordered = sorted(bars, key=lambda b: int(b.time))
    return sum(1 for b in ordered if int(t0) < int(b.time) <= int(t1))


def _horizon_end_for_setup(setup_session: SessionRange) -> Optional[int]:
    """
    Stop outcome evaluation when the next primary session after this one starts.

    Asia → following London; London → same-day Asia (20:00 NY).
    """
    from datetime import date, timedelta

    from session_time import resolve_session_window
    from sessions_config import SESSION_DEFINITIONS

    td = None
    extras = setup_session.extras or {}
    rw = extras.get("resolved_window") or {}
    if rw.get("trading_date"):
        td = date.fromisoformat(str(rw["trading_date"])[:10])
    if td is None:
        return setup_session.end

    if setup_session.name == "Asia":
        # Asia ends at London start on calendar day td+1.
        w = resolve_session_window(SESSION_DEFINITIONS["London"], td + timedelta(days=1))
        return int(w.utc_start)

    if setup_session.name == "London":
        w = resolve_session_window(SESSION_DEFINITIONS["Asia"], td)
        return int(w.utc_start)

    return setup_session.end


def _reliability_flags(setup) -> list[str]:
    flags = []
    meta = setup.source_metadata or {}
    if meta.get("dst_uncertainty"):
        flags.append("SESSION_DST_UNCERTAINTY")
    if meta.get("trigger_bar_stop_ambiguity"):
        flags.append("TRIGGER_BAR_STOP_AMBIGUITY")
    cov = meta.get("coverage_status")
    if cov and cov != CoverageStatus.FULL.value:
        flags.append(f"coverage:{cov}")
    conf = setup.confirmation or {}
    if conf.get("timing_confidence") == "unavailable":
        flags.append("choch_timing_unavailable")
    eq = (conf.get("extras") or {}).get("equivalence_status")
    if eq:
        flags.append(f"structure_equivalence:{eq}")
    if setup.expiry_reason:
        flags.append(f"expiry:{setup.expiry_reason}")
    return flags


def trade_setup_to_journal_record(
    setup,
    bars: Sequence[Bar],
    session_range: SessionRange,
    *,
    strategy_config: StrategyConfig,
    structure_config: HistoricalStructureConfig,
    config_hash: str,
) -> SetupJournalRecord:
    sr = setup.session_range or {}
    sweep = setup.sweep or {}
    conf = setup.confirmation or {}
    fvg = setup.fvg or {}
    meta = setup.source_metadata or {}

    horizon = _horizon_end_for_setup(session_range)
    # If setup already expired, use expiry timing via session context if present
    if setup.status == SetupStatus.EXPIRED.value and setup.expiry_reason:
        # Cap at active window start from metadata when available
        ctx = meta.get("session_context") or {}
        aw = ctx.get("active_window") or {}
        if aw.get("utc_start") is not None:
            horizon = int(aw["utc_start"])

    entry_results = []
    bars_fvg_to_entry: dict[str, Optional[int]] = {}
    for analysis in setup.entries:
        er = evaluate_entry_outcome(
            analysis,
            bars,
            direction=setup.direction or (analysis.entry.direction if analysis.entry else ""),
            horizon_end_ts=horizon,
            point_size=strategy_config.risk.point_size,
        )
        entry_results.append(er)
        if er.triggered and er.entry_timestamp and fvg.get("created_timestamp"):
            bars_fvg_to_entry[er.mode] = _bars_between(
                bars, fvg.get("created_timestamp"), er.entry_timestamp
            )

    conf_extras = conf.get("extras") or {}
    htf = setup.higher_timeframe_context or {}
    daily = (htf.get("daily_bias") or {}) if isinstance(htf, dict) else {}
    h4b = (htf.get("h4_bias") or {}) if isinstance(htf, dict) else {}
    return SetupJournalRecord(
        setup_id=setup.id,
        symbol=setup.symbol,
        timeframe=setup.timeframe,
        trading_date=setup.trading_date,
        session=setup.session,
        direction=setup.direction,
        swept_side=sweep.get("side"),
        session_high=sr.get("high"),
        session_low=sr.get("low"),
        sweep_level=sweep.get("level"),
        sweep_extreme=meta.get("sweep_extreme"),
        sweep_timestamp=sweep.get("sweep_timestamp"),
        confirmation_source=conf.get("source"),
        confirmation_algorithm=conf_extras.get("algorithm_version")
        or structure_config.algorithm_version,
        confirmation_timestamp=conf.get("event_timestamp"),
        confirmation_level=conf.get("level"),
        confirmation_equivalence_status=conf_extras.get("equivalence_status")
        or structure_config.equivalence_status,
        fvg_low=fvg.get("low"),
        fvg_high=fvg.get("high"),
        fvg_midpoint=fvg.get("midpoint"),
        fvg_created_timestamp=fvg.get("created_timestamp"),
        status=setup.status,
        expiry_reason=setup.expiry_reason,
        invalidation_reason=setup.invalidation_reason,
        reliability_flags=_reliability_flags(setup),
        entry_results=entry_results,
        strategy_version=STRATEGY_VERSION,
        config_hash=config_hash,
        structure_algorithm_version=structure_config.algorithm_version,
        bars_sweep_to_choch=_bars_between(
            bars, sweep.get("sweep_timestamp"), conf.get("event_timestamp")
        ),
        bars_choch_to_fvg=_bars_between(
            bars, conf.get("event_timestamp"), fvg.get("created_timestamp")
        ),
        bars_fvg_to_entry=bars_fvg_to_entry or None,
        daily_bias=daily.get("direction") or "unknown",
        h4_bias=h4b.get("direction") or "unknown",
        htf_alignment=(htf.get("alignment") if isinstance(htf, dict) else None)
        or meta.get("htf_alignment")
        or "unknown",
        execution_timeframe=setup.execution_timeframe or strategy_config.execution.timeframe,
        setup_vs_daily=setup.setup_vs_daily or "unknown",
        setup_vs_h4=setup.setup_vs_h4 or "unknown",
        daily_bias_confidence=daily.get("confidence"),
        h4_bias_confidence=h4b.get("confidence"),
        daily_structure_break_time=(daily.get("evidence") or {}).get(
            "last_break_timestamp"
        ),
        h4_structure_break_time=(h4b.get("evidence") or {}).get("last_break_timestamp"),
        daily_bars_since_break=(daily.get("evidence") or {}).get("bars_since_break"),
        h4_bars_since_break=(h4b.get("evidence") or {}).get("bars_since_break"),
        bias_algorithm_version=(
            (htf.get("source_metadata") or {}).get("algorithm_version")
            if isinstance(htf, dict)
            else None
        )
        or daily.get("method")
        or strategy_config.htf_bias.algorithm_version,
        liquidity_event_id=meta.get("liquidity_event_id") or setup.id.split("|exec:")[0],
        extras={
            "session_source": session_range.source,
            "coverage_status": session_range.coverage_status,
            "horizon_end_ts": horizon,
            "journal_unique_key": "|".join(
                [
                    str(meta.get("liquidity_event_id") or setup.id.split("|exec:")[0]),
                    str(
                        setup.execution_timeframe
                        or strategy_config.execution.timeframe
                    ),
                    str(config_hash),
                ]
            ),
        },
    )


def replay_historical_setups(
    bars: Sequence[Bar],
    *,
    symbol: str,
    timeframe: str,
    strategy_config: Optional[StrategyConfig] = None,
    structure_config: Optional[HistoricalStructureConfig] = None,
    confirmation_provider: Optional[ConfirmationProvider] = None,
    session_names: Sequence[str] = PRIMARY_SESSIONS,
    period_start: Optional[int] = None,
    period_end: Optional[int] = None,
    mtf_bars: Optional[Any] = None,
    bias_provider: Optional[Any] = None,
) -> ReplayResult:
    """
    Replay Asia/London liquidity events over OHLC bars.

    Uses internal OHLC sessions + HistoricalStructureProvider by default.
    """
    sc = strategy_config or DEFAULT_STRATEGY_CONFIG
    hc = structure_config or DEFAULT_HISTORICAL_STRUCTURE_CONFIG
    provider = confirmation_provider or HistoricalStructureProvider(hc)
    config_hash = compute_config_hash(sc, hc)

    ordered = sorted(bars, key=lambda b: int(b.time))
    if period_start is not None:
        ordered = [b for b in ordered if int(b.time) >= int(period_start)]
    if period_end is not None:
        ordered = [b for b in ordered if int(b.time) <= int(period_end)]

    warnings = [
        SESSION_DST_UNCERTAINTY,
        (
            "Historical CHoCH uses internal_structure "
            f"({hc.algorithm_version}); equivalence_status="
            f"{hc.equivalence_status} — not claimed equal to LuxAlgo."
        ),
    ]
    errors: list[dict[str, Any]] = []

    if not ordered:
        return ReplayResult(
            symbol=symbol,
            timeframe=timeframe,
            period_start=period_start,
            period_end=period_end,
            total_sessions=0,
            total_sweeps=0,
            total_setups=0,
            journal_records=[],
            coverage=ReplayCoverage(0, 0, 0, 0, 0),
            errors=[{"error": "no_bars"}],
            warnings=warnings,
            metadata={"config_hash": config_hash, "provider": provider.source_name},
        )

    now_ts = int(ordered[-1].time)
    sessions = compute_session_ranges(
        ordered,
        resolution_minutes=_resolution_minutes(timeframe),
        now_ts=now_ts,
        names=session_names,
    )

    choch_all = provider.get_confirmations(ordered)

    coverage_details: list[dict[str, Any]] = []
    complete = 0
    incomplete = 0
    missing = 0
    skipped = 0
    records: list[SetupJournalRecord] = []
    sweeps = 0

    for session in sessions:
        detail = {
            "identity": session.identity,
            "name": session.name,
            "coverage": session.coverage_status,
            "complete": session.complete,
        }
        if session.coverage_status == CoverageStatus.MISSING.value:
            missing += 1
            skipped += 1
            detail["action"] = "skipped_missing_bars"
            coverage_details.append(detail)
            continue

        if not session.complete or session.coverage_status != CoverageStatus.FULL.value:
            incomplete += 1
            skipped += 1
            detail["action"] = "skipped_incomplete_coverage"
            coverage_details.append(detail)
            continue

        complete += 1
        detail["action"] = "analyzed"
        coverage_details.append(detail)

        try:
            setup = analyze_session_setup(
                session,
                ordered,
                choch_all,
                sc,
                symbol=symbol,
                timeframe=timeframe,
                now_ts=now_ts,
                mtf_bars=mtf_bars,
                bias_provider=bias_provider,
            )
            if setup.sweep:
                sweeps += 1
            rec = trade_setup_to_journal_record(
                setup,
                ordered,
                session,
                strategy_config=sc,
                structure_config=hc,
                config_hash=config_hash,
            )
            records.append(rec)
        except Exception as exc:  # noqa: BLE001
            errors.append(
                {
                    "session": session.identity,
                    "error": str(exc),
                }
            )

    return ReplayResult(
        symbol=symbol,
        timeframe=timeframe,
        period_start=int(ordered[0].time) if ordered else period_start,
        period_end=int(ordered[-1].time) if ordered else period_end,
        total_sessions=len(sessions),
        total_sweeps=sweeps,
        total_setups=len(records),
        journal_records=records,
        coverage=ReplayCoverage(
            expected_sessions=len(sessions),
            complete_sessions=complete,
            incomplete_sessions=incomplete,
            missing_bars_sessions=missing,
            skipped_sessions=skipped,
            details=coverage_details,
        ),
        errors=errors,
        warnings=warnings,
        metadata={
            "config_hash": config_hash,
            "strategy_version": STRATEGY_VERSION,
            "structure_algorithm_version": hc.algorithm_version,
            "equivalence_status": hc.equivalence_status,
            "confirmation_provider": provider.source_name,
            "choch_event_count": len(choch_all),
        },
    )


def replay_historical_mtf_setups(
    bars_by_tf: dict[str, Sequence[Bar]],
    *,
    symbol: str,
    strategy_config: Optional[StrategyConfig] = None,
    structure_config: Optional[HistoricalStructureConfig] = None,
    confirmation_provider: Optional[ConfirmationProvider] = None,
    session_names: Sequence[str] = PRIMARY_SESSIONS,
    period_start: Optional[int] = None,
    period_end: Optional[int] = None,
    mtf_bars: Optional[Any] = None,
    bias_provider: Optional[Any] = None,
    execution_timeframes: Sequence[str] = ("5m", "15m"),
) -> ReplayResult:
    """
    Replay the same session liquidity events under multiple execution timeframes.

    Sessions / sweeps are derived from the primary (lowest) execution bars when
    possible; each execution TF produces distinct journal rows sharing
    liquidity_event_id.
    """
    from dataclasses import replace

    from execution_config import ExecutionTimeframeConfig
    from ohlc_resample import resample_ohlc
    from timeframe import normalize_timeframe

    sc = strategy_config or DEFAULT_STRATEGY_CONFIG
    all_records: list[SetupJournalRecord] = []
    errors: list[dict[str, Any]] = []
    warnings: list[str] = []
    total_sessions = 0
    total_sweeps = 0
    coverage = ReplayCoverage(0, 0, 0, 0, 0)
    meta: dict[str, Any] = {"execution_timeframes": list(execution_timeframes)}

    # Prefer native series; resample 15m from 5m if needed.
    series: dict[str, list[Bar]] = {}
    for tf in execution_timeframes:
        canon = normalize_timeframe(tf) or tf
        if canon in bars_by_tf and bars_by_tf[canon]:
            series[canon] = list(bars_by_tf[canon])
        elif canon == "15m" and bars_by_tf.get("5m"):
            series[canon] = list(
                resample_ohlc(
                    bars_by_tf["5m"],
                    "15m",
                    source_timeframe="5m",
                    trading_day=sc.trading_day,
                ).bars
            )
            warnings.append("15m bars resampled from 5m (source=resampled)")
        elif canon == "5m" and bars_by_tf.get("15m"):
            # Cannot upsample reliably — skip
            errors.append({"timeframe": canon, "error": "5m bars unavailable"})
        else:
            raw = bars_by_tf.get(tf) or bars_by_tf.get(canon)
            if raw:
                series[canon] = list(raw)

    # HTF bundle
    from multi_tf_bars import MultiTimeframeBars

    mtf = mtf_bars or MultiTimeframeBars()
    if bars_by_tf.get("1D"):
        mtf = mtf.with_series("1D", bars_by_tf["1D"], source="native")
    elif bars_by_tf.get("5m"):
        daily = resample_ohlc(
            bars_by_tf["5m"],
            "1D",
            source_timeframe="5m",
            trading_day=sc.trading_day,
        )
        mtf = mtf.with_series("1D", daily.bars, source="resampled")
        warnings.append("Daily bars resampled from 5m using NY trading-day boundary")
    if bars_by_tf.get("4H"):
        mtf = mtf.with_series("4H", bars_by_tf["4H"], source="native")
    elif bars_by_tf.get("5m"):
        h4 = resample_ohlc(
            bars_by_tf["5m"],
            "4H",
            source_timeframe="5m",
            trading_day=sc.trading_day,
        )
        mtf = mtf.with_series("4H", h4.bars, source="resampled")
        warnings.append("4H bars resampled from 5m")

    for tf in execution_timeframes:
        canon = normalize_timeframe(tf) or tf
        bars = series.get(canon)
        if not bars:
            errors.append({"timeframe": canon, "error": "no_bars"})
            continue
        cfg = replace(
            sc,
            execution=ExecutionTimeframeConfig(timeframe=canon),
        )
        result = replay_historical_setups(
            bars,
            symbol=symbol,
            timeframe=canon,
            strategy_config=cfg,
            structure_config=structure_config,
            confirmation_provider=confirmation_provider,
            session_names=session_names,
            period_start=period_start,
            period_end=period_end,
            mtf_bars=mtf,
            bias_provider=bias_provider,
        )
        all_records.extend(result.journal_records)
        total_sessions += result.total_sessions
        total_sweeps += result.total_sweeps
        errors.extend(result.errors)
        warnings.extend(result.warnings)
        coverage = result.coverage
        meta[f"replay_{canon}"] = {
            "total_setups": result.total_setups,
            "total_sweeps": result.total_sweeps,
            "config_hash": result.metadata.get("config_hash"),
        }

    period0 = None
    period1 = None
    for bars in series.values():
        if bars:
            t0, t1 = int(bars[0].time), int(bars[-1].time)
            period0 = t0 if period0 is None else min(period0, t0)
            period1 = t1 if period1 is None else max(period1, t1)

    return ReplayResult(
        symbol=symbol,
        timeframe=",".join(execution_timeframes),
        period_start=period0,
        period_end=period1,
        total_sessions=total_sessions,
        total_sweeps=total_sweeps,
        total_setups=len(all_records),
        journal_records=all_records,
        coverage=coverage,
        errors=errors,
        warnings=list(dict.fromkeys(warnings)),
        metadata=meta,
    )
