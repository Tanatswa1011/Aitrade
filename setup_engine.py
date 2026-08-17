"""Pure read-only setup orchestration (reuses Phase 2–6 modules; no I/O)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional, Sequence

from entry_detect import evaluate_entry_modes
from fvg_detect import detect_fvg
from liquidity_sweep import detect_sweeps
from models import (
    TRIGGER_BAR_STOP_AMBIGUITY,
    Bar,
    EntryAnalysis,
    FVGZone,
    LiquiditySweep,
    SessionRange,
    SetupStatus,
    StructureConfirmation,
    StructureDirection,
    SweepSide,
    TradeSetup,
    replace_trade_setup,
)
from risk_plan import build_risk_plan, sweep_extreme
from setup_expiry import (
    SessionContext,
    apply_expiry_to_setup,
    build_session_context,
    evaluate_setup_expiry,
)
from strategy_config import DEFAULT_STRATEGY_CONFIG, StrategyConfig
from structure_confirm import confirm_after_sweep, required_direction_for_sweep
from target_plan import build_target_plan
from bias_models import (
    HigherTimeframeContext,
    setup_vs_bias,
    unknown_htf_context,
)
from bias_provider import BiasProvider, resolve_bias_provider
from multi_tf_bars import MultiTimeframeBars
from timeframe import normalize_timeframe



def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def make_liquidity_event_id(
    *,
    symbol: str,
    session: str,
    trading_date: Optional[str],
    sweep_side: Optional[str],
    sweep_timestamp: Optional[int],
) -> str:
    """Deterministic identity for one session liquidity event (TF-agnostic)."""
    sym = (symbol or "UNKNOWN").replace("|", "_")
    sess = session or "UNKNOWN"
    date = trading_date or "unknown-date"
    side = sweep_side or "none"
    ts = "none" if sweep_timestamp is None else str(int(sweep_timestamp))
    return f"{sym}|{sess}|{date}|{side}|{ts}"


def make_setup_id(
    *,
    symbol: str,
    session: str,
    trading_date: Optional[str],
    sweep_side: Optional[str],
    sweep_timestamp: Optional[int],
    execution_timeframe: Optional[str] = None,
) -> str:
    """
    Deterministic setup analysis id.

    Base = liquidity_event_id. When execution_timeframe is set, append it so
    5m vs 15m analyses of the same sweep remain distinct records.
    """
    base = make_liquidity_event_id(
        symbol=symbol,
        session=session,
        trading_date=trading_date,
        sweep_side=sweep_side,
        sweep_timestamp=sweep_timestamp,
    )
    if execution_timeframe:
        from timeframe import normalize_timeframe

        tf = normalize_timeframe(execution_timeframe) or str(execution_timeframe)
        return f"{base}|exec:{tf}"
    return base


def expected_direction_from_sweep(sweep: LiquiditySweep) -> str:
    if sweep.side in (SweepSide.LOW.value, "low"):
        return StructureDirection.BULLISH.value
    if sweep.side in (SweepSide.HIGH.value, "high"):
        return StructureDirection.BEARISH.value
    raise ValueError(f"Unknown sweep side: {sweep.side!r}")


def _trading_date_from_session(session: SessionRange) -> Optional[str]:
    extras = session.extras or {}
    rw = extras.get("resolved_window") or {}
    if rw.get("trading_date"):
        return str(rw["trading_date"])
    if session.start is not None:
        return datetime.fromtimestamp(int(session.start), tz=timezone.utc).date().isoformat()
    return None


def _session_complete(session: SessionRange, config: StrategyConfig) -> bool:
    if not session.is_tradeable_level_source:
        return False
    if config.prefer_completed_sessions_only:
        if not session.complete:
            return False
        cov = (session.coverage_status or "").lower()
        if cov in ("missing", "unknown"):
            return False
        # Allow full and partial ICT price-complete ranges; reject incomplete OHLC coverage.
        if cov in ("partial_start", "partial_end", "partial") and session.source == "internal_ohlc":
            return False
    return True


def _entry_comparison(analyses: Sequence[EntryAnalysis]) -> dict[str, Any]:
    rows = []
    for a in analyses:
        e = a.entry
        r = a.risk
        t = a.target
        fixed = {}
        if t is not None:
            for ft in t.fixed_rr_targets:
                fixed[f"{ft.rr:g}R"] = ft.price
        rows.append(
            {
                "mode": e.mode,
                "triggered": e.triggered,
                "entry_price": e.price,
                "entry_depth": e.entry_depth,
                "max_retrace_depth": e.max_retrace_depth,
                "risk_valid": None if r is None else r.valid,
                "risk_distance": None if r is None else r.risk_distance,
                "stop_price": None if r is None else r.stop_price,
                **fixed,
                "opposite_liquidity": None if t is None else t.opposite_liquidity_label,
                "opposite_price": None if t is None else t.opposite_liquidity_price,
                "rr_to_opposite": None if t is None else t.rr_to_opposite,
            }
        )
    return {"entry_mode_comparison": rows, "note": "Comparison only; no preferred mode selected."}


def _finalize(
    *,
    setup_id: str,
    symbol: str,
    timeframe: str,
    trading_date: Optional[str],
    session_name: str,
    direction: Optional[str],
    session_range: Optional[SessionRange],
    sweep: Optional[LiquiditySweep],
    confirmation: Optional[StructureConfirmation],
    fvg: Optional[FVGZone],
    entries: list[EntryAnalysis],
    status: str,
    invalidation_reason: Optional[str],
    expiry_reason: Optional[str],
    source_metadata: dict[str, Any],
    config: StrategyConfig,
    execution_timeframe: Optional[str] = None,
    higher_timeframe_context: Optional[HigherTimeframeContext] = None,
) -> TradeSetup:
    from setup_explain import explain_setup

    now = _now_iso()
    quality = _entry_comparison(entries) if entries else None
    exec_tf = (
        normalize_timeframe(execution_timeframe)
        or normalize_timeframe(config.execution.timeframe)
        or normalize_timeframe(timeframe)
        or config.execution.timeframe
    )
    htf = higher_timeframe_context or unknown_htf_context()
    vs_daily = setup_vs_bias(direction, htf.daily_bias.direction)
    vs_h4 = setup_vs_bias(direction, htf.h4_bias.direction)
    meta = {
        **source_metadata,
        "dst_uncertainty": config.dst_uncertainty,
        "session_confidence": (config.session_confidence or {}).get(session_name),
        "trigger_bar_stop_ambiguity": TRIGGER_BAR_STOP_AMBIGUITY,
        "strategy_config": config.to_dict(),
        "execution_timeframe": exec_tf,
        "htf_alignment": htf.alignment,
        "setup_vs_daily": vs_daily,
        "setup_vs_h4": vs_h4,
        # Mixed/partial HTF never auto-rejects (Phase 11).
        "htf_hard_filter": False,
    }
    draft = TradeSetup(
        id=setup_id,
        symbol=symbol,
        timeframe=timeframe,
        trading_date=trading_date,
        session=session_name,
        direction=direction,
        session_range=None if session_range is None else session_range.to_dict(),
        sweep=None if sweep is None else sweep.to_dict(),
        confirmation=None if confirmation is None else confirmation.to_dict(),
        fvg=None if fvg is None else fvg.to_dict(),
        entries=list(entries),
        status=status,
        setup_quality=quality,
        created_at=now,
        updated_at=now,
        expiry_reason=expiry_reason,
        invalidation_reason=invalidation_reason,
        source_metadata=meta,
        explanation="",
        execution_timeframe=exec_tf,
        higher_timeframe_context=htf.to_dict(),
        setup_vs_daily=vs_daily,
        setup_vs_h4=vs_h4,
    )
    return replace_trade_setup(draft, explanation=explain_setup(draft))


def _maybe_expire(
    setup: TradeSetup,
    bars: Sequence[Bar],
    config: StrategyConfig,
    *,
    now_ts: Optional[int],
    session_context: Optional[SessionContext],
    opposite_session_sweeps: Sequence[LiquiditySweep] = (),
) -> TradeSetup:
    """Apply lifecycle expiry after strategy analysis; preserve setup id."""
    eval_ts = now_ts
    if eval_ts is None and bars:
        eval_ts = max(int(b.time) for b in bars)
    ctx = session_context
    if ctx is None:
        ctx = build_session_context(
            setup_session_name=setup.session,
            setup_trading_date=setup.trading_date,
            session_range=setup.session_range,
            now_ts=eval_ts,
            opposite_session_sweeps=opposite_session_sweeps,
        )
    decision = evaluate_setup_expiry(setup, bars, ctx, config.expiry)
    meta = dict(setup.source_metadata or {})
    meta["expiry_evaluated"] = decision.to_dict()
    meta["session_context"] = ctx.to_dict()
    setup = replace_trade_setup(setup, source_metadata=meta)
    return apply_expiry_to_setup(setup, decision)


def analyze_session_setup(
    session_range: SessionRange,
    bars: Sequence[Bar],
    choch_observations: Sequence[StructureConfirmation],
    strategy_config: Optional[StrategyConfig] = None,
    *,
    symbol: str = "",
    timeframe: str = "",
    sweep_bar_index: Optional[int] = None,
    now_ts: Optional[int] = None,
    session_context: Optional[SessionContext] = None,
    opposite_session_sweeps: Sequence[LiquiditySweep] = (),
    execution_timeframe: Optional[str] = None,
    higher_timeframe_context: Optional[HigherTimeframeContext] = None,
    bias_provider: Optional[BiasProvider] = None,
    mtf_bars: Optional[MultiTimeframeBars] = None,
) -> TradeSetup:
    """
    Orchestrate sweep → CHoCH → FVG → entries → risk/targets into TradeSetup.

    Pure: no CDP/MCP. Progressive statuses with fail-closed reliability rules.
    Lifecycle expiry applied last (does not change setup id).

    HTF bias is attached as context only — never rejects mixed alignment.
    Caller must supply execution-TF bars/choch for the selected execution_timeframe.
    """
    config = strategy_config or DEFAULT_STRATEGY_CONFIG
    # Validate execution config eagerly (rejects mixed confirmation/entry TFs).
    exec_cfg = config.execution
    exec_tf = (
        normalize_timeframe(execution_timeframe)
        or exec_cfg.timeframe
    )
    if exec_tf != exec_cfg.timeframe and execution_timeframe:
        # Allow override via explicit arg by rebuilding validated config equality
        from execution_config import ExecutionTimeframeConfig

        exec_cfg = ExecutionTimeframeConfig(timeframe=exec_tf)
    session_name = session_range.name
    trading_date = _trading_date_from_session(session_range)
    base_meta = {
        "session_source": session_range.source,
        "coverage_status": session_range.coverage_status,
        "session_complete_flag": session_range.complete,
        "bar_count": len(bars),
        "execution_bars_timeframe": exec_tf,
    }

    provider: BiasProvider = bias_provider or resolve_bias_provider(
        config.bias.provider, config=config.htf_bias
    )
    mtf = mtf_bars or MultiTimeframeBars()

    def _resolve_htf(as_of: Optional[int]) -> HigherTimeframeContext:
        if higher_timeframe_context is not None:
            return higher_timeframe_context
        ts = int(as_of if as_of is not None else (now_ts or 0))
        return provider.get_context(
            as_of_ts=ts,
            daily_bars=mtf.bars_for("1D"),
            h4_bars=mtf.bars_for("4H"),
        )

    def _id(**kwargs) -> str:
        return make_setup_id(
            symbol=symbol,
            session=session_name,
            trading_date=trading_date,
            sweep_side=kwargs.get("sweep_side"),
            sweep_timestamp=kwargs.get("sweep_timestamp"),
            execution_timeframe=exec_tf,
        )

    def _liquidity_event_id(**kwargs) -> str:
        return make_liquidity_event_id(
            symbol=symbol,
            session=session_name,
            trading_date=trading_date,
            sweep_side=kwargs.get("sweep_side"),
            sweep_timestamp=kwargs.get("sweep_timestamp"),
        )

    def _done(setup: TradeSetup) -> TradeSetup:
        from setup_explain import explain_setup

        sweep = setup.sweep or {}
        as_of = sweep.get("sweep_timestamp")
        if as_of is None and session_range.end is not None:
            as_of = session_range.end
        if as_of is None:
            as_of = now_ts
        htf = _resolve_htf(as_of)
        vs_daily = setup_vs_bias(setup.direction, htf.daily_bias.direction)
        vs_h4 = setup_vs_bias(setup.direction, htf.h4_bias.direction)
        meta = dict(setup.source_metadata or {})
        meta["htf_alignment"] = htf.alignment
        meta["setup_vs_daily"] = vs_daily
        meta["setup_vs_h4"] = vs_h4
        meta["htf_evaluated_at"] = htf.evaluated_at
        meta["htf_hard_filter"] = False
        meta["bias_provider"] = getattr(provider, "name", "unknown")
        meta["execution_bars_timeframe"] = exec_tf
        sweep = setup.sweep or {}
        meta["liquidity_event_id"] = make_liquidity_event_id(
            symbol=symbol,
            session=session_name,
            trading_date=trading_date,
            sweep_side=sweep.get("side"),
            sweep_timestamp=sweep.get("sweep_timestamp"),
        )
        setup = replace_trade_setup(
            setup,
            execution_timeframe=exec_tf,
            timeframe=timeframe or exec_tf,
            higher_timeframe_context=htf.to_dict(),
            setup_vs_daily=vs_daily,
            setup_vs_h4=vs_h4,
            source_metadata=meta,
            explanation="",
        )
        setup = replace_trade_setup(setup, explanation=explain_setup(setup))
        return _maybe_expire(
            setup,
            bars,
            config,
            now_ts=now_ts,
            session_context=session_context,
            opposite_session_sweeps=opposite_session_sweeps,
        )

    # --- Session readiness ---
    if not session_range.is_tradeable_level_source:
        return _done(
            _finalize(
                setup_id=_id(),
                symbol=symbol,
                timeframe=timeframe,
                trading_date=trading_date,
                session_name=session_name,
                direction=None,
                session_range=session_range,
                sweep=None,
                confirmation=None,
                fvg=None,
                entries=[],
                status=SetupStatus.NO_SETUP.value,
                invalidation_reason="missing_session_high_low",
                expiry_reason=None,
                source_metadata=base_meta,
                config=config,
            )
        )

    if not _session_complete(session_range, config):
        return _done(
            _finalize(
                setup_id=_id(),
                symbol=symbol,
                timeframe=timeframe,
                trading_date=trading_date,
                session_name=session_name,
                direction=None,
                session_range=session_range,
                sweep=None,
                confirmation=None,
                fvg=None,
                entries=[],
                status=SetupStatus.WAITING_FOR_SESSION.value,
                invalidation_reason=None,
                expiry_reason=None,
                source_metadata={
                    **base_meta,
                    "reason": "incomplete_or_unconfirmed_session_coverage",
                },
                config=config,
            )
        )

    # --- Sweep ---
    sweeps = detect_sweeps(session_range, bars, rule=config.sweep_rule)
    if not sweeps:
        return _done(
            _finalize(
                setup_id=_id(),
                symbol=symbol,
                timeframe=timeframe,
                trading_date=trading_date,
                session_name=session_name,
                direction=None,
                session_range=session_range,
                sweep=None,
                confirmation=None,
                fvg=None,
                entries=[],
                status=SetupStatus.WAITING_FOR_SWEEP.value,
                invalidation_reason=None,
                expiry_reason=None,
                source_metadata={
                    **base_meta,
                    "reason": (
                        f"{session_name} session completed, but neither "
                        f"{session_name} High nor {session_name} Low has been swept "
                        f"(rule={config.sweep_rule})."
                    ),
                },
                config=config,
            )
        )

    # v1: first (earliest) liquidity event on this session range.
    sweep = sweeps[0]
    direction = expected_direction_from_sweep(sweep)
    setup_id = _id(sweep_side=sweep.side, sweep_timestamp=sweep.sweep_timestamp)

    # Resolve sweep bar index if not provided.
    sbi = sweep_bar_index
    if sbi is None:
        for i, b in enumerate(sorted(bars, key=lambda x: x.time)):
            if int(b.time) == int(sweep.sweep_timestamp):
                sbi = i
                break

    decision = confirm_after_sweep(
        sweep, choch_observations, sweep_bar_index=sbi
    )
    if not decision.confirmed or decision.confirmation is None:
        return _done(
            _finalize(
                setup_id=setup_id,
                symbol=symbol,
                timeframe=timeframe,
                trading_date=trading_date,
                session_name=session_name,
                direction=direction,
                session_range=session_range,
                sweep=sweep,
                confirmation=None,
                fvg=None,
                entries=[],
                status=SetupStatus.WAITING_FOR_CONFIRMATION.value,
                invalidation_reason=None,
                expiry_reason=None,
                source_metadata={
                    **base_meta,
                    "confirmation_reason": decision.reason,
                    "required_direction": decision.required_direction,
                    "sweep_extreme": sweep_extreme(sweep),
                    "reason": (
                        f"{session_name} {sweep.side} was swept, but no reliable "
                        f"{direction} LuxAlgo CHoCH has occurred afterward "
                        f"({decision.reason})."
                    ),
                },
                config=config,
            )
        )

    conf = decision.confirmation
    if conf.direction != direction:
        return _done(
            _finalize(
                setup_id=setup_id,
                symbol=symbol,
                timeframe=timeframe,
                trading_date=trading_date,
                session_name=session_name,
                direction=direction,
                session_range=session_range,
                sweep=sweep,
                confirmation=conf,
                fvg=None,
                entries=[],
                status=SetupStatus.INVALIDATED.value,
                invalidation_reason="confirmation_direction_conflict",
                expiry_reason=None,
                source_metadata={
                    **base_meta,
                    "confirmation_timing_confidence": conf.timing_confidence,
                },
                config=config,
            )
        )

    if conf.event_timestamp is None or conf.timing_confidence == "unavailable":
        return _done(
            _finalize(
                setup_id=setup_id,
                symbol=symbol,
                timeframe=timeframe,
                trading_date=trading_date,
                session_name=session_name,
                direction=direction,
                session_range=session_range,
                sweep=sweep,
                confirmation=conf,
                fvg=None,
                entries=[],
                status=SetupStatus.WAITING_FOR_CONFIRMATION.value,
                invalidation_reason=None,
                expiry_reason=None,
                source_metadata={
                    **base_meta,
                    "confirmation_timing_confidence": conf.timing_confidence,
                    "reason": "CHoCH timing unreliable; fail closed before FVG.",
                },
                config=config,
            )
        )

    # --- FVG ---
    fvg_result = detect_fvg(sweep, conf, bars, config.fvg)
    if not fvg_result.found or not fvg_result.zones:
        return _done(
            _finalize(
                setup_id=setup_id,
                symbol=symbol,
                timeframe=timeframe,
                trading_date=trading_date,
                session_name=session_name,
                direction=direction,
                session_range=session_range,
                sweep=sweep,
                confirmation=conf,
                fvg=None,
                entries=[],
                status=SetupStatus.WAITING_FOR_FVG.value,
                invalidation_reason=None,
                expiry_reason=None,
                source_metadata={
                    **base_meta,
                    "confirmation_timing_confidence": conf.timing_confidence,
                    "fvg_reason": fvg_result.reason,
                    "sweep_extreme": sweep_extreme(sweep),
                },
                config=config,
            )
        )

    fvg = fvg_result.zones[0]
    if fvg.direction != direction:
        return _done(
            _finalize(
                setup_id=setup_id,
                symbol=symbol,
                timeframe=timeframe,
                trading_date=trading_date,
                session_name=session_name,
                direction=direction,
                session_range=session_range,
                sweep=sweep,
                confirmation=conf,
                fvg=fvg,
                entries=[],
                status=SetupStatus.INVALIDATED.value,
                invalidation_reason="fvg_direction_conflict",
                expiry_reason=None,
                source_metadata=base_meta,
                config=config,
            )
        )

    # --- Entries ---
    mode_results = evaluate_entry_modes(
        fvg,
        bars,
        config.entry_modes,
        allow_full_fill=config.entry.allow_full_fill,
        max_bars_after_fvg=config.entry.max_bars_after_fvg,
    )
    triggered = [e for e in mode_results.values() if e.triggered and e.status == "triggered"]
    if not triggered:
        return _done(
            _finalize(
                setup_id=setup_id,
                symbol=symbol,
                timeframe=timeframe,
                trading_date=trading_date,
                session_name=session_name,
                direction=direction,
                session_range=session_range,
                sweep=sweep,
                confirmation=conf,
                fvg=fvg,
                entries=[
                    EntryAnalysis(entry=e, risk=None, target=None)
                    for e in mode_results.values()
                ],
                status=SetupStatus.WAITING_FOR_RETRACE.value,
                invalidation_reason=None,
                expiry_reason=None,
                source_metadata={
                    **base_meta,
                    "confirmation_timing_confidence": conf.timing_confidence,
                    "sweep_extreme": sweep_extreme(sweep),
                    "entry_statuses": {m: e.status for m, e in mode_results.items()},
                },
                config=config,
            )
        )

    analyses: list[EntryAnalysis] = []
    any_valid_risk = False
    invalid_reasons = []
    for mode in config.entry_modes:
        entry = mode_results[mode]
        if not entry.triggered or entry.status != "triggered":
            analyses.append(EntryAnalysis(entry=entry, risk=None, target=None))
            continue
        if entry.direction != direction:
            analyses.append(EntryAnalysis(entry=entry, risk=None, target=None))
            invalid_reasons.append(f"{mode}:entry_direction_conflict")
            continue

        risk = build_risk_plan(sweep, fvg, entry, bars, config.risk)
        target = None
        if risk.valid:
            target = build_target_plan(
                session_range, sweep, entry, risk, config.target
            )
            any_valid_risk = True
        else:
            invalid_reasons.append(f"{mode}:{risk.invalidation_reason}")
        analyses.append(EntryAnalysis(entry=entry, risk=risk, target=target))

    if not any_valid_risk:
        return _done(
            _finalize(
                setup_id=setup_id,
                symbol=symbol,
                timeframe=timeframe,
                trading_date=trading_date,
                session_name=session_name,
                direction=direction,
                session_range=session_range,
                sweep=sweep,
                confirmation=conf,
                fvg=fvg,
                entries=analyses,
                status=SetupStatus.INVALIDATED.value,
                invalidation_reason=";".join(invalid_reasons) or "all_entry_risks_invalid",
                expiry_reason=None,
                source_metadata={
                    **base_meta,
                    "confirmation_timing_confidence": conf.timing_confidence,
                    "sweep_extreme": sweep_extreme(sweep),
                },
                config=config,
            )
        )

    return _done(
        _finalize(
            setup_id=setup_id,
            symbol=symbol,
            timeframe=timeframe,
            trading_date=trading_date,
            session_name=session_name,
            direction=direction,
            session_range=session_range,
            sweep=sweep,
            confirmation=conf,
            fvg=fvg,
            entries=analyses,
            status=SetupStatus.ENTRY_READY.value,
            invalidation_reason=None,
            expiry_reason=None,
            source_metadata={
                **base_meta,
                "confirmation_timing_confidence": conf.timing_confidence,
                "sweep_extreme": sweep_extreme(sweep),
                "partial_invalid_entry_modes": invalid_reasons,
            },
            config=config,
        )
    )


def select_session_auto(
    sessions: dict[str, Optional[SessionRange]],
    *,
    prefer_completed: bool = True,
) -> dict[str, Any]:
    """
    Conservative auto session selection among Asia/London.

    Returns chosen session name or an ambiguity payload — never guesses.
    """
    candidates = []
    for name in ("Asia", "London"):
        s = sessions.get(name)
        if s is None or not s.is_tradeable_level_source:
            continue
        if prefer_completed and not s.complete:
            continue
        candidates.append(s)

    if not candidates:
        return {
            "ok": False,
            "session": None,
            "reason": "no_completed_tradeable_asia_or_london_session",
            "ambiguous": False,
        }
    if len(candidates) == 1:
        return {"ok": True, "session": candidates[0].name, "range": candidates[0], "ambiguous": False}

    # Prefer the most recently ended completed session.
    dated = [(s.end if s.end is not None else -1, s) for s in candidates]
    dated.sort(key=lambda x: x[0], reverse=True)
    if dated[0][0] == dated[1][0]:
        return {
            "ok": False,
            "session": None,
            "reason": "ambiguous_auto_session_same_end",
            "ambiguous": True,
            "candidates": [s.name for s in candidates],
        }
    return {"ok": True, "session": dated[0][1].name, "range": dated[0][1], "ambiguous": False}
