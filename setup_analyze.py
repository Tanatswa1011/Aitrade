"""I/O adapter: TradingView chart → pure setup_engine (read-only)."""

from __future__ import annotations

from typing import Any, Optional, Sequence

from bias_provider import resolve_bias_provider
from bars import fetch_bars
from chart_timeframe import get_chart_resolution
from closed_candles import latest_closed_bar
from execution_config import BiasConfig, ExecutionTimeframeConfig
from ict_sessions import fetch_ict_session_ranges
from liquidity_sweep import detect_sweeps
from luxalgo_structure import fetch_luxalgo_choch
from models import RiskConfig, SetupStatus
from multi_tf_bars import MultiTimeframeBars
from mtf_fetch import fetch_mtf_bar_bundle
from setup_engine import analyze_session_setup, select_session_auto
from strategy_config import DEFAULT_STRATEGY_CONFIG, StrategyConfig
from timeframe import normalize_timeframe


def _series_index_for_time(by_index: dict, ts: int) -> Optional[int]:
    hits = [i for i, t in by_index.items() if int(t) == int(ts)]
    return int(min(hits)) if hits else None


def _with_overrides(
    *,
    sweep_rule: Optional[str],
    entry_modes: Optional[Sequence[str]],
    stop_mode: Optional[str],
    execution_timeframe: Optional[str],
    bias_provider_name: Optional[str],
) -> StrategyConfig:
    base = DEFAULT_STRATEGY_CONFIG
    risk = base.risk
    if stop_mode:
        risk = RiskConfig(
            stop_mode=stop_mode,
            stop_buffer_price=risk.stop_buffer_price,
            stop_buffer_points=risk.stop_buffer_points,
            point_size=risk.point_size,
            invalidate_before_entry=risk.invalidate_before_entry,
            extras=dict(risk.extras),
        )
    exec_tf = normalize_timeframe(execution_timeframe) if execution_timeframe else None
    execution = (
        ExecutionTimeframeConfig(timeframe=exec_tf) if exec_tf else base.execution
    )
    bias_name = (bias_provider_name or base.bias.provider or "structure").lower()
    bias = BiasConfig(
        provider=bias_name,
        method="structure_break_v1" if "structure" in bias_name else base.bias.method,
    )
    return StrategyConfig(
        sweep_rule=sweep_rule or base.sweep_rule,
        entry_modes=tuple(entry_modes) if entry_modes else base.entry_modes,
        fvg=base.fvg,
        entry=base.entry,
        risk=risk,
        target=base.target,
        expiry=base.expiry,
        execution=execution,
        bias=bias,
        htf_bias=base.htf_bias,
        prefer_completed_sessions_only=base.prefer_completed_sessions_only,
        session_confidence=dict(base.session_confidence),
        dst_uncertainty=base.dst_uncertainty,
    )


async def analyze_live_session_setup(
    *,
    session: str = "auto",
    sweep_rule: Optional[str] = None,
    entry_modes: Optional[Sequence[str]] = None,
    stop_mode: Optional[str] = None,
    execution_timeframe: str = "5m",
    bias_provider: str = "structure",
) -> dict[str, Any]:
    """
    Fetch D/4H/exec bars (with chart restore), run orchestrator + structure bias.

    Read-only: never places orders.
    """
    try:
        config = _with_overrides(
            sweep_rule=sweep_rule,
            entry_modes=entry_modes,
            stop_mode=stop_mode,
            execution_timeframe=execution_timeframe,
            bias_provider_name=bias_provider,
        )
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc), "code": "INVALID_CONFIG"}

    exec_tf = config.execution.timeframe
    original = await get_chart_resolution()

    # Fetch HTF + execution bars; always restore original chart TF.
    bundle = await fetch_mtf_bar_bundle(("1D", "4H", exec_tf))
    mtf: MultiTimeframeBars = bundle.get("mtf") or MultiTimeframeBars()

    # Execution bars preferred from bundle; fallback current chart if missing.
    exec_series = mtf.get(exec_tf)
    if exec_series is None or not exec_series.bars:
        try:
            bar_payload = await fetch_bars()
        except Exception as exc:  # noqa: BLE001
            return {
                "ok": False,
                "error": str(exc),
                "code": "CDP_UNAVAILABLE",
                "original_chart_timeframe": original.get("resolution"),
                "chart_timeframe_after_analysis": (
                    await get_chart_resolution()
                ).get("resolution"),
                "restore_ok": bundle.get("restore_ok"),
            }
        if not bar_payload.get("ok"):
            return {
                "ok": False,
                "error": bar_payload.get("error"),
                "code": "BARS_UNAVAILABLE",
                "mtf_fetch": {k: bundle.get(k) for k in (
                    "fetched", "errors", "restore_ok", "study_ids_changed"
                )},
            }
        bars = bar_payload["bars"]
        by_index = bar_payload.get("bars_by_series_index") or {}
        symbol = bar_payload.get("symbol") or ""
        timeframe = str(bar_payload.get("resolution") or "")
        mtf = mtf.with_series(exec_tf, bars, source="native")
    else:
        bars = list(exec_series.bars)
        by_index = {}
        # Need symbol from a quick chart read without changing TF if possible
        try:
            meta = await fetch_bars(limit=1)
            symbol = meta.get("symbol") or ""
            timeframe = str(meta.get("resolution") or exec_tf)
        except Exception:  # noqa: BLE001
            symbol = ""
            timeframe = exec_tf

    daily_bars = list(mtf.bars_for("1D"))
    h4_bars = list(mtf.bars_for("4H"))

    ict = await fetch_ict_session_ranges(
        bars_by_series_index=by_index if by_index else None
    )
    lux = await fetch_luxalgo_choch(
        bars_by_series_index=by_index if by_index else None
    )
    choch = lux.get("events") or []

    latest_map = {
        "Asia": (ict.get("latest") or {}).get("Asia"),
        "London": (ict.get("latest") or {}).get("London"),
    }

    sess_key = (session or "auto").strip().lower()
    if sess_key == "auto":
        selection = select_session_auto(
            latest_map, prefer_completed=config.prefer_completed_sessions_only
        )
        if not selection.get("ok"):
            return {
                "ok": True,
                "partial": True,
                "status": SetupStatus.NO_SETUP.value,
                "auto_session": selection,
                "symbol": symbol,
                "timeframe": timeframe,
                "execution_timeframe": exec_tf,
                "bias_source": config.bias.provider,
                "original_chart_timeframe": bundle.get("original_chart_timeframe"),
                "chart_timeframe_after_analysis": bundle.get("chart_timeframe_after"),
                "restore_ok": bundle.get("restore_ok"),
                "mtf_fetched": bundle.get("fetched"),
                "explanation": (
                    "Auto session selection could not choose a unique completed "
                    f"Asia/London range: {selection.get('reason')}"
                ),
            }
        session_range = selection["range"]
    elif sess_key in ("asia", "london"):
        sess_name = "Asia" if sess_key == "asia" else "London"
        session_range = latest_map.get(sess_name)
        if session_range is None:
            return {
                "ok": True,
                "partial": True,
                "status": SetupStatus.WAITING_FOR_SESSION.value,
                "symbol": symbol,
                "timeframe": timeframe,
                "execution_timeframe": exec_tf,
                "session": sess_name,
                "bias_source": config.bias.provider,
                "original_chart_timeframe": bundle.get("original_chart_timeframe"),
                "chart_timeframe_after_analysis": bundle.get("chart_timeframe_after"),
                "restore_ok": bundle.get("restore_ok"),
                "explanation": f"No ICT {sess_name} session range available on chart.",
            }
    else:
        return {"ok": False, "error": f"invalid session param: {session!r}"}

    sweeps = detect_sweeps(session_range, bars, rule=config.sweep_rule)
    sbi = None
    if sweeps and by_index:
        sbi = _series_index_for_time(by_index, sweeps[0].sweep_timestamp)

    provider = resolve_bias_provider(
        config.bias.provider, config=config.htf_bias
    )
    # If HTF bars missing, fall back to unknown rather than inventing
    if not daily_bars and not h4_bars and config.bias.provider.startswith("structure"):
        # Still try structure — will return unknown from insufficient bars
        pass

    setup = analyze_session_setup(
        session_range,
        bars,
        choch,
        config,
        symbol=symbol,
        timeframe=timeframe,
        sweep_bar_index=sbi,
        now_ts=int(bars[-1].time) if bars else None,
        execution_timeframe=exec_tf,
        bias_provider=provider,
        mtf_bars=mtf,
    )

    htf = setup.higher_timeframe_context or {}
    daily = htf.get("daily_bias") or {}
    h4 = htf.get("h4_bias") or {}
    as_of = (setup.sweep or {}).get("sweep_timestamp") or (
        int(bars[-1].time) if bars else None
    )

    return {
        "ok": True,
        "partial": setup.status != SetupStatus.ENTRY_READY.value,
        "symbol": symbol,
        "timeframe": timeframe,
        "execution_timeframe": setup.execution_timeframe,
        "bias_source": (htf.get("source_metadata") or {}).get("provider")
        or config.bias.provider,
        "higher_timeframe_context": htf,
        "daily_bias_summary": {
            "bars_loaded": len(daily_bars),
            "latest_closed_bar": None
            if as_of is None
            else (
                None
                if latest_closed_bar(daily_bars, as_of_ts=int(as_of), timeframe="1D")
                is None
                else latest_closed_bar(
                    daily_bars, as_of_ts=int(as_of), timeframe="1D"
                ).to_dict()
            ),
            "bias": daily.get("direction"),
            "confidence": daily.get("confidence"),
            "evidence": daily.get("evidence"),
        },
        "h4_bias_summary": {
            "bars_loaded": len(h4_bars),
            "latest_closed_bar": None
            if as_of is None
            else (
                None
                if latest_closed_bar(h4_bars, as_of_ts=int(as_of), timeframe="4H")
                is None
                else latest_closed_bar(
                    h4_bars, as_of_ts=int(as_of), timeframe="4H"
                ).to_dict()
            ),
            "bias": h4.get("direction"),
            "confidence": h4.get("confidence"),
            "evidence": h4.get("evidence"),
        },
        "session_requested": session,
        "session_resolved": setup.session,
        "ict_ok": ict.get("ok"),
        "lux_ok": lux.get("ok"),
        "lux_counts": lux.get("counts"),
        "setup": setup.to_dict(),
        "explanation": setup.explanation,
        "status": setup.status,
        "bars_obtained": len(bars),
        "original_chart_timeframe": bundle.get("original_chart_timeframe"),
        "requested_execution_timeframe": exec_tf,
        "chart_timeframe_after_analysis": bundle.get("chart_timeframe_after"),
        "restore_ok": bundle.get("restore_ok"),
        "mtf_fetched": bundle.get("fetched"),
        "mtf_errors": bundle.get("errors"),
        "study_ids_changed": bundle.get("study_ids_changed"),
        "studies_before": bundle.get("studies_before"),
        "studies_after": bundle.get("studies_after"),
    }
