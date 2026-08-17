"""Live read-only GC ORB analysis (fails closed unless chart is GC futures)."""

from __future__ import annotations

from typing import Any

from gc_orb_engine import (
    build_opening_range,
    detect_roll_gap_timestamps,
    find_first_breakouts,
    find_retest,
    trading_dates_in_bars,
)
from gc_orb_models import OR_ANCHOR_LOCAL, OR_ANCHOR_NOTE, STRATEGY_FAMILY, STRATEGY_VERSION
from models import Bar


def _is_gc_symbol(symbol: str) -> bool:
    s = (symbol or "").upper().replace(":", "")
    if "XAUUSD" in s or "XAU" in s and "GC" not in s:
        return False
    return (
        s.endswith("GC")
        or "GC=F" in s
        or s.startswith("GC")
        or "/GC" in s
        or "GC1!" in s
        or "MGC" in s
    )


async def analyze_live_gc_orb(*, or_minutes: int = 30) -> dict[str, Any]:
    from cdp import get_chart_info
    from bars import fetch_bars
    import asyncio

    info = await get_chart_info()
    symbol = str((info or {}).get("symbol") or "")
    if not _is_gc_symbol(symbol):
        return {
            "ok": False,
            "error": "symbol_not_gc_futures",
            "symbol": symbol,
            "note": "Refuse XAUUSD/spot charts for Phase 22 GC ORB tool.",
        }

    payload = await fetch_bars()
    if not payload.get("ok"):
        return {"ok": False, "error": payload.get("error") or "bars_unavailable", "symbol": symbol}

    raw = payload.get("bars") or []
    bars = []
    for b in raw:
        if isinstance(b, Bar):
            bars.append(b)
        else:
            bars.append(
                Bar(
                    time=int(b["time"]),
                    open=float(b["open"]),
                    high=float(b["high"]),
                    low=float(b["low"]),
                    close=float(b["close"]),
                    volume=None if b.get("volume") is None else float(b["volume"]),
                )
            )
    if not bars:
        return {"ok": False, "error": "empty_bars", "symbol": symbol}

    dates = trading_dates_in_bars(bars)
    td = dates[-1]
    orng = build_opening_range(bars, td, or_minutes=or_minutes)
    roll = detect_roll_gap_timestamps(bars)
    events = find_first_breakouts(bars, orng, roll_flags=roll) if orng.complete else []
    out_events = []
    for ev in events:
        rt = find_retest(bars, ev)
        out_events.append({**ev.to_dict(), "retest": rt})

    return {
        "ok": True,
        "strategy_family": STRATEGY_FAMILY,
        "strategy_version": STRATEGY_VERSION,
        "symbol": symbol,
        "trading_date": td,
        "opening_range": orng.to_dict(),
        "anchor": {"local": OR_ANCHOR_LOCAL, "note": OR_ANCHOR_NOTE},
        "breakouts": out_events,
        "note": "Read-only; no orders.",
    }
