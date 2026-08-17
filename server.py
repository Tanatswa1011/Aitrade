"""Local MCP server bridging Cursor Agent to TradingView Desktop via CDP."""

from __future__ import annotations

import sys
from typing import Any

from mcp.server.mcpserver import Image, MCPServer

from cdp import (
    CdpError,
    clear_drawings,
    draw_horizontal_line,
    get_chart_info,
    health_check,
    take_screenshot,
)

mcp = MCPServer("tradingview")


@mcp.tool()
def tv_health_check() -> dict[str, Any]:
    """Check CDP on localhost:9222 and report the TradingView chart target.

    Confirms whether TradingView Desktop is reachable through Chrome DevTools
    Protocol and returns the chart page target id, title, and URL.
    """
    try:
        return health_check()
    except Exception as exc:  # noqa: BLE001 - never crash the MCP process
        return {
            "cdp_connected": False,
            "tradingview_found": False,
            "error": f"Unexpected health-check failure: {exc}",
        }


@mcp.tool()
async def tv_get_chart_info() -> dict[str, Any]:
    """Read the active TradingView chart symbol and timeframe via CDP.

    Injects JavaScript that calls TradingViewApi.activeChart() and returns
    the current symbol and resolution when that API is exposed.
    """
    try:
        result = await get_chart_info()
        return {
            "ok": True,
            **result,
        }
    except CdpError as exc:
        return {
            "ok": False,
            "available": False,
            "error": exc.message,
            "code": exc.code,
        }
    except Exception as exc:  # noqa: BLE001 - never crash the MCP process
        return {
            "ok": False,
            "available": False,
            "error": f"Unexpected chart-info failure: {exc}",
        }


def _tool_error(exc: Exception, prefix: str) -> dict[str, Any]:
    if isinstance(exc, CdpError):
        return {"ok": False, "error": exc.message, "code": exc.code}
    return {"ok": False, "error": f"{prefix}: {exc}"}


@mcp.tool()
async def tv_take_screenshot() -> Any:
    """Capture a PNG screenshot of the active TradingView chart.

    Uses TradingViewApi.takeClientScreenshot() over CDP and saves the image
    under the local screenshots folder. Returns the file path plus the image.
    """
    try:
        result = await take_screenshot()
        return [result, Image(path=result["path"])]
    except Exception as exc:  # noqa: BLE001 - never crash the MCP process
        return _tool_error(exc, "Unexpected screenshot failure")


@mcp.tool()
async def tv_draw_horizontal_line(price: float, label: str) -> dict[str, Any]:
    """Draw a labeled horizontal line on the active TradingView chart.

    Args:
        price: Price level for the line.
        label: Text shown on the line.
    """
    try:
        return await draw_horizontal_line(price, label)
    except Exception as exc:  # noqa: BLE001 - never crash the MCP process
        return _tool_error(exc, "Unexpected draw failure")


@mcp.tool()
async def tv_clear_drawings() -> dict[str, Any]:
    """Remove drawings previously created by this MCP bridge.

    Clears tracked horizontal lines drawn via tv_draw_horizontal_line.
    Does not delete unrelated user drawings already on the chart.
    """
    try:
        return await clear_drawings()
    except Exception as exc:  # noqa: BLE001 - never crash the MCP process
        return _tool_error(exc, "Unexpected clear-drawings failure")


@mcp.tool()
async def tv_analyze_session_setup(
    session: str = "auto",
    sweep_rule: str = "wick_only",
    stop_mode: str = "beyond_sweep",
    execution_timeframe: str = "5m",
    bias_provider: str = "structure",
) -> dict[str, Any]:
    """Read-only AITRADE session setup analysis for the active TradingView chart.

    Assembles Asia/London session levels, liquidity sweep, LuxAlgo CHoCH,
    setup-linked FVG, entry candidates, risk, and targets into one TradeSetup.
    Attaches HigherTimeframeContext from the selected bias provider
    (default: structure_break_v1). Mixed/opposed HTF bias does not reject setups.
    Does not place orders or modify broker state.

    Args:
        session: "asia", "london", or "auto".
        sweep_rule: Sweep detection rule (default wick_only).
        stop_mode: Risk stop mode (default beyond_sweep).
        execution_timeframe: "5m" or "15m".
        bias_provider: structure | unknown | historical | manual
    """
    try:
        from setup_analyze import analyze_live_session_setup

        return await analyze_live_session_setup(
            session=session,
            sweep_rule=sweep_rule,
            stop_mode=stop_mode,
            execution_timeframe=execution_timeframe,
            bias_provider=bias_provider,
        )
    except Exception as exc:  # noqa: BLE001 - never crash the MCP process
        return _tool_error(exc, "Unexpected setup-analysis failure")


@mcp.tool()
async def tv_annotate_session_setup(
    session: str = "auto",
    sweep_rule: str = "wick_only",
    stop_mode: str = "beyond_sweep",
    entry_mode: str = "all",
    show_fixed_rr: bool = True,
    show_opposite_liquidity: bool = True,
    take_screenshot: bool = False,
    execution_timeframe: str = "5m",
) -> dict[str, Any]:
    """Analyze the active chart setup and draw AITRADE annotations on TradingView.

    Visualizes the canonical TradeSetup only (no new strategy calculations).
    Idempotent per setup_id. Does not place orders.

    Args:
        session: asia | london | auto
        sweep_rule: Sweep rule passed to analysis
        stop_mode: Stop mode for risk plans
        entry_mode: all | first_touch | boundary | ce
        show_fixed_rr: Draw 1R/2R/3R targets when present
        show_opposite_liquidity: Draw same-session opposing liquidity
        take_screenshot: Capture chart after drawing (reuses existing screenshot path)
        execution_timeframe: 5m | 15m
    """
    try:
        from setup_analyze import analyze_live_session_setup
        from setup_annotate import annotate_trade_setup

        analysis = await analyze_live_session_setup(
            session=session,
            sweep_rule=sweep_rule,
            stop_mode=stop_mode,
            execution_timeframe=execution_timeframe,
            bias_provider="structure",
        )
        if not analysis.get("ok"):
            return analysis
        setup = analysis.get("setup")
        if not setup:
            return {
                **analysis,
                "annotated": False,
                "reason": "no_setup_payload_to_annotate",
            }
        drawn = await annotate_trade_setup(
            setup,
            entry_mode=entry_mode,
            show_fixed_rr=show_fixed_rr,
            show_opposite_liquidity=show_opposite_liquidity,
            take_screenshot_after=take_screenshot,
        )
        return {
            "ok": True,
            "analysis_status": analysis.get("status"),
            "explanation": analysis.get("explanation"),
            "partial": analysis.get("partial"),
            **drawn,
        }
    except Exception as exc:  # noqa: BLE001
        return _tool_error(exc, "Unexpected annotate-setup failure")


@mcp.tool()
async def tv_clear_setup_annotation(setup_id: str = "") -> dict[str, Any]:
    """Clear AITRADE setup annotations only (not personal/indicator drawings).

    Args:
        setup_id: Optional deterministic setup id. Empty clears all AITRADE setup annotations.
    """
    try:
        from setup_annotate import clear_setup_annotations

        sid = setup_id.strip() or None
        return await clear_setup_annotations(sid)
    except Exception as exc:  # noqa: BLE001
        return _tool_error(exc, "Unexpected clear-setup-annotation failure")


@mcp.tool()
async def tv_capture_luxalgo_choch(
    timeframes: str = "5m,15m",
    include_unreliable: bool = True,
) -> dict[str, Any]:
    """Capture LuxAlgo CHoCH events from the active chart (read-only; persists JSONL).

    Switches chart resolution for each requested timeframe, extracts CHoCH only
    (BOS/IDM/x are not confirmation), maps timing when possible, and dedupes to
    data/luxalgo_captures/choch_events.jsonl. Does not place orders or signals.

    Args:
        timeframes: Comma-separated list, e.g. "5m,15m".
        include_unreliable: Persist timing-unavailable rows for diagnostics.
    """
    try:
        from phase20_capture import capture_luxalgo_choch_once

        tfs = [t.strip() for t in timeframes.split(",") if t.strip()]
        return await capture_luxalgo_choch_once(
            timeframes=tfs or ("5m", "15m"),
            include_unreliable=include_unreliable,
        )
    except Exception as exc:  # noqa: BLE001
        return _tool_error(exc, "Unexpected LuxAlgo capture failure")


@mcp.tool()
def tv_get_luxalgo_events(
    timeframe: str = "",
    reliable_only: bool = False,
) -> dict[str, Any]:
    """List persisted LuxAlgo CHoCH capture rows (read-only).

    Args:
        timeframe: Optional filter "5m" or "15m". Empty = all.
        reliable_only: If true, only exact/derived timed events.
    """
    try:
        from luxalgo_capture import load_luxalgo_captures
        from phase20_capture import summarize_capture_store

        tf = timeframe.strip() or None
        rows = load_luxalgo_captures(
            symbol="OANDA:XAUUSD",
            timeframe=tf,
            reliable_only=reliable_only,
        )
        return {
            "ok": True,
            "count": len(rows),
            "summary": summarize_capture_store(),
            "events": rows[:500],
            "truncated": len(rows) > 500,
        }
    except Exception as exc:  # noqa: BLE001
        return _tool_error(exc, "Unexpected LuxAlgo events read failure")


@mcp.tool()
def tv_compare_internal_luxalgo(timeframe: str = "5m") -> dict[str, Any]:
    """Compare internal historical CHoCH vs persisted LuxAlgo captures (read-only).

    Uses TradingView/OANDA local bars when available. Does not change strategy rules.

    Args:
        timeframe: "5m" or "15m".
    """
    try:
        from bar_dataset import load_dataset
        from luxalgo_capture import load_luxalgo_captures
        from phase20_validate import analyze_timeframe

        tf = timeframe.strip() or "5m"
        tv = load_dataset("OANDA:XAUUSD", tf)
        rows = load_luxalgo_captures(symbol="OANDA:XAUUSD", timeframe=tf)
        analysis = analyze_timeframe(
            timeframe=tf,
            capture_rows=rows,
            tv_bars=tv.get("bars") or [],
        )
        # Slim response for MCP
        ov = dict(analysis.get("overlap") or {})
        ov.pop("classifications", None)
        ov.pop("internal_only", None)
        return {
            "ok": True,
            "timeframe": tf,
            "luxalgo_reliable": analysis.get("luxalgo_reliable"),
            "internal_event_count": analysis.get("internal_event_count"),
            "equivalence_status": analysis.get("equivalence_status"),
            "equivalence_confidence": analysis.get("equivalence_confidence"),
            "overlap": ov,
            "divergence": (analysis.get("overlap") or {}).get("divergence"),
        }
    except Exception as exc:  # noqa: BLE001
        return _tool_error(exc, "Unexpected LuxAlgo compare failure")


@mcp.tool()
async def tv_analyze_liquidity_reclaim(
    session: str = "auto",
    timeframe: str = "5m",
    confirmation_mode: str = "immediate_reclaim",
    entry_mode: str = "confirmation_close",
    break_mode: str = "close_break",
) -> dict[str, Any]:
    """Read-only liquidity sweep+reclaim analysis (Phase 21; OHLC only).

    No LuxAlgo/CHoCH/FVG. Returns current setup state for Asia/London levels.
    Does not place orders or emit broker signals.

    Args:
        session: asia | london | auto
        timeframe: 5m | 15m
        confirmation_mode: immediate_reclaim | confirmation_candle | sweep_candle_break
        entry_mode: confirmation_close | liquidity_retest | sweep_midpoint
        break_mode: close_break | wick_break (sweep_candle_break only)
    """
    try:
        from liquidity_reclaim_live import analyze_live_liquidity_reclaim

        return await analyze_live_liquidity_reclaim(
            session=session,
            timeframe=timeframe,
            confirmation_mode=confirmation_mode,
            entry_mode=entry_mode,
            break_mode=break_mode,
        )
    except Exception as exc:  # noqa: BLE001
        return _tool_error(exc, "Unexpected liquidity-reclaim analysis failure")


@mcp.tool()
async def tv_analyze_gc_orb(
    or_minutes: int = 30,
    confirmation_note: str = "read_only",
) -> dict[str, Any]:
    """Read-only GC futures opening-range analysis if the active chart is GC.

    Fails closed unless the chart symbol looks like COMEX gold futures (GC).
    Does not place orders. Volume/RVOL only if bar volume is present.

    Args:
        or_minutes: Opening range length (15/30/60).
        confirmation_note: Unused placeholder for API stability.
    """
    try:
        from gc_orb_live import analyze_live_gc_orb

        _ = confirmation_note
        return await analyze_live_gc_orb(or_minutes=or_minutes)
    except Exception as exc:  # noqa: BLE001
        return _tool_error(exc, "Unexpected GC ORB analysis failure")


@mcp.tool()
async def tv_analyze_gc_orb15_retest(
    confirmation_note: str = "read_only",
) -> dict[str, Any]:
    """Read-only GC OR15 breakout + boundary/FVG retest status (Phase 24).

    Fails closed unless the active chart looks like COMEX gold futures (GC).
    Returns OR15 state, breakout, retest/FVG status, and candidate entry levels.
    Does not place orders or emit BUY/SELL language.

    Args:
        confirmation_note: Unused placeholder for API stability.
    """
    try:
        from gc_orb15_live import analyze_live_gc_orb15_retest

        _ = confirmation_note
        return await analyze_live_gc_orb15_retest()
    except Exception as exc:  # noqa: BLE001
        return _tool_error(exc, "Unexpected GC OR15 retest analysis failure")


@mcp.tool()
async def tv_analyze_gc_vwap_reversion(
    confirmation_note: str = "read_only",
) -> dict[str, Any]:
    """Read-only GC session VWAP / 2σ extension / reclaim status (Phase 25/26).

    Fails closed unless the active chart looks like COMEX gold futures (GC).
    Returns VWAP, σ bands, z-score, extension/reclaim status, candidate levels,
    and frozen V2 paper snapshot when strategy_frozen/gc_vwap_v2_phase26.json exists.
    Does not place orders or emit BUY/SELL language.

    Args:
        confirmation_note: Unused placeholder for API stability.
    """
    try:
        from gc_vwap_live import analyze_live_gc_vwap_reversion

        _ = confirmation_note
        return await analyze_live_gc_vwap_reversion()
    except Exception as exc:  # noqa: BLE001
        return _tool_error(exc, "Unexpected GC VWAP reversion analysis failure")


@mcp.tool()
async def tv_gc_vwap_v2_paper_state(
    confirmation_note: str = "read_only",
) -> dict[str, Any]:
    """Read-only frozen Phase 26 V2 paper-validation state (no tunable params).

    Loads strategy_frozen/gc_vwap_v2_phase26.json only. Refuses XAUUSD/spot charts.
    Returns frozen_config_hash, VWAP/σ bands, extension/reclaim/entry/stop/targets,
    and paper observation state. No broker orders. No BUY/SELL language.

    Args:
        confirmation_note: Unused placeholder for API stability.
    """
    try:
        from gc_vwap_live import analyze_frozen_gc_vwap_v2_paper_state

        _ = confirmation_note
        return await analyze_frozen_gc_vwap_v2_paper_state()
    except Exception as exc:  # noqa: BLE001
        return _tool_error(exc, "Unexpected frozen GC VWAP V2 paper-state failure")


@mcp.tool()
async def tv_nq_dvp_paper_state(
    confirmation_note: str = "read_only",
) -> dict[str, Any]:
    """Read-only frozen Phase 30 NQ Drift VWAP Pullback paper state (no tunable params).

    Loads strategy_frozen/nq_dvp_phase30.json only. Requires CME NQ futures chart.
    Returns frozen hash, VWAP/drift/pullback state, daily caps, campaign N.
    No broker orders. Technical LONG_SETUP_STATE / SHORT_SETUP_STATE only.

    Args:
        confirmation_note: Unused placeholder for API stability.
    """
    try:
        from nq_dvp_live import analyze_frozen_nq_dvp_paper_state

        _ = confirmation_note
        return await analyze_frozen_nq_dvp_paper_state()
    except Exception as exc:  # noqa: BLE001
        return _tool_error(exc, "Unexpected frozen NQ DVP paper-state failure")


if __name__ == "__main__":
    # MCP stdio must stay clean; keep accidental prints off stdout.
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    mcp.run(transport="stdio")
