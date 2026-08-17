"""Live read-only GC OR15 retest/FVG analysis (fails closed unless chart is GC)."""

from __future__ import annotations

from typing import Any

from gc_orb15_engine import (
    analyze_candidate,
    find_boundary_retest,
    find_first_breakout_fvg,
    find_first_or15_breakout,
    find_fvg_retrace_entry,
)
from gc_orb15_models import (
    OR_ANCHOR_LOCAL,
    OR_ANCHOR_NOTE,
    OR_MINUTES,
    PHASE24_CANDIDATES,
    STRATEGY_FAMILY,
    STRATEGY_VERSION,
    EntryMode,
)
from gc_orb_engine import build_opening_range, detect_roll_gap_timestamps, trading_dates_in_bars
from gc_orb_live import _is_gc_symbol
from models import Bar


async def analyze_live_gc_orb15_retest() -> dict[str, Any]:
    from bars import fetch_bars
    from cdp import get_chart_info

    info = await get_chart_info()
    symbol = str((info or {}).get("symbol") or "")
    if not _is_gc_symbol(symbol):
        return {
            "ok": False,
            "error": "symbol_not_gc_futures",
            "symbol": symbol,
            "note": "Refuse XAUUSD/spot charts for Phase 24 GC OR15 tool.",
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
    orng = build_opening_range(bars, td, or_minutes=OR_MINUTES)
    roll = detect_roll_gap_timestamps(bars)
    event = find_first_or15_breakout(bars, orng, roll_flags=roll) if orng.complete else None

    boundary = None
    fvg = None
    levels = {}
    if event is not None:
        boundary = find_boundary_retest(bars, event, require_hold=True)
        fvg_z = find_first_breakout_fvg(bars, event)
        fvg = None if fvg_z is None else fvg_z.to_dict()
        if fvg_z is not None:
            touch = find_fvg_retrace_entry(bars, fvg_z, mode=EntryMode.FVG_TOUCH.value)
            ce = find_fvg_retrace_entry(bars, fvg_z, mode=EntryMode.FVG_CE.value)
            levels = {
                "fvg_touch": None if not touch else touch.get("entry_price"),
                "fvg_ce": None if not ce else ce.get("entry_price"),
                "or_boundary": event.or_high if event.direction == "bullish" else event.or_low,
                "or_mid": event.or_mid,
            }
        else:
            levels = {
                "or_boundary": event.or_high if event.direction == "bullish" else event.or_low,
                "or_mid": event.or_mid,
            }
        candidates = []
        for cfg in PHASE24_CANDIDATES:
            setup = analyze_candidate(event, bars, cfg)
            candidates.append(
                {
                    "candidate_id": cfg.candidate_id,
                    "entry_mode": cfg.entry_mode,
                    "state": setup.state,
                    "reason": setup.reason,
                    "entry_price": setup.entry_price,
                    "stop_price": setup.stop_price,
                    "triggered": setup.entry_triggered,
                }
            )
    else:
        candidates = []

    return {
        "ok": True,
        "strategy_family": STRATEGY_FAMILY,
        "strategy_version": STRATEGY_VERSION,
        "symbol": symbol,
        "trading_date": td,
        "opening_range": orng.to_dict(),
        "anchor": {"local": OR_ANCHOR_LOCAL, "or_minutes": OR_MINUTES, "note": OR_ANCHOR_NOTE},
        "breakout": None if event is None else event.to_dict(),
        "boundary_retest": boundary,
        "fvg": fvg,
        "candidate_entry_levels": levels,
        "candidates": candidates,
        "note": "Read-only descriptive levels; not an order signal.",
    }
