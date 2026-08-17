"""Live read-only liquidity reclaim analysis via TradingView CDP (Phase 21)."""

from __future__ import annotations

from typing import Any

from liquidity_reclaim_engine import analyze_session_liquidity_reclaim
from liquidity_reclaim_models import ReclaimStrategyConfig, STRATEGY_FAMILY, STRATEGY_VERSION
from models import PRIMARY_SESSIONS
from ohlc_sessions import compute_session_ranges
from timeframe import timeframe_seconds


async def analyze_live_liquidity_reclaim(
    *,
    session: str = "auto",
    timeframe: str = "5m",
    confirmation_mode: str = "immediate_reclaim",
    entry_mode: str = "confirmation_close",
    break_mode: str = "close_break",
) -> dict[str, Any]:
    from bars import fetch_bars
    from chart_timeframe import get_chart_resolution, set_chart_resolution
    from cdp import get_chart_info
    import asyncio

    info = await get_chart_info()
    symbol = (info or {}).get("symbol") or "OANDA:XAUUSD"
    prev = await get_chart_resolution()
    try:
        await set_chart_resolution(timeframe)
        await asyncio.sleep(1.0)
        payload = await fetch_bars()
    finally:
        if prev and prev.get("resolution") is not None:
            try:
                await set_chart_resolution(str(prev["resolution"]))
            except Exception:  # noqa: BLE001
                pass

    if not payload.get("ok"):
        return {"ok": False, "error": payload.get("error") or "bars_unavailable"}

    bars = payload.get("bars") or []
    # bars may be dicts
    from models import Bar

    bar_objs = []
    for b in bars:
        if isinstance(b, Bar):
            bar_objs.append(b)
        else:
            bar_objs.append(
                Bar(
                    time=int(b["time"]),
                    open=float(b["open"]),
                    high=float(b["high"]),
                    low=float(b["low"]),
                    close=float(b["close"]),
                    volume=b.get("volume"),
                )
            )

    period = timeframe_seconds(timeframe) or 300
    res_min = max(1, int(period // 60))
    now_ts = int(bar_objs[-1].time) if bar_objs else None
    sessions = compute_session_ranges(
        bar_objs, resolution_minutes=res_min, now_ts=now_ts, names=PRIMARY_SESSIONS
    )
    complete = [s for s in sessions if s.complete]
    if not complete:
        return {
            "ok": True,
            "strategy_family": STRATEGY_FAMILY,
            "status": "NO_COMPLETE_SESSION",
            "setups": [],
        }

    sess_filter = session.strip().lower()
    if sess_filter in ("asia", "london"):
        complete = [s for s in complete if s.name.lower() == sess_filter]
    # latest session of each name
    by_name: dict[str, Any] = {}
    for s in complete:
        by_name[s.name] = s
    targets = list(by_name.values())

    cfg = ReclaimStrategyConfig(
        candidate_id="LIVE",
        execution_timeframe=timeframe,
        confirmation_mode=confirmation_mode,
        entry_mode=entry_mode,
        break_mode=break_mode,
    )
    setups = []
    for s in targets:
        for side in ("high", "low"):
            setup = analyze_session_liquidity_reclaim(
                s, bar_objs, symbol=symbol, cfg=cfg, side=side
            )
            setups.append(setup.to_dict())

    return {
        "ok": True,
        "strategy_family": STRATEGY_FAMILY,
        "strategy_version": STRATEGY_VERSION,
        "symbol": symbol,
        "timeframe": timeframe,
        "confirmation_mode": confirmation_mode,
        "entry_mode": entry_mode,
        "break_mode": break_mode,
        "setups": setups,
        "note": "Read-only analysis; no orders or signals.",
    }
