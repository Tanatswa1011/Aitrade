"""Read-only live/paper analyzer for locked ES DVP. No broker orders. DRY_RUN only."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional, Sequence
from zoneinfo import ZoneInfo

from es_dvp_lock import (
    ES_CLOCK,
    LOCKED_CFG,
    LOCKED_VERSION,
    load_locked_document,
    locked_config_hash,
)
from es_dvp_paper import (
    campaign_status,
    recover_daily_state_from_journal,
    sample_label,
    summarize_paper_journal,
)
from models import Bar
from nq_databento import aggregate_1m_to_ny
from nq_drift_vwap_engine import trading_dates_ny
from nq_dvp_live_signal import evaluate_completed_bars

NY = ZoneInfo("America/New_York")


def _is_es_symbol(symbol: str) -> bool:
    s = (symbol or "").upper().replace(":", "")
    if "MES" in s and "ES" not in s.replace("MES", ""):
        return False
    if any(tok in s for tok in ("NQ1!", "NQ=F", "/NQ", "GC1!", "GC=F", "/GC")) and "ES" not in s:
        return False
    return (
        "ES1!" in s
        or "ES=F" in s
        or "/ES" in s
        or s.endswith("ES")
        or s.startswith("ES")
        or "CME_MINIES" in s
        or "CME_ES" in s
    )


def _to_bars(raw: list) -> list[Bar]:
    bars: list[Bar] = []
    for b in raw:
        if isinstance(b, Bar):
            bars.append(b)
            continue
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
    return bars


async def _fetch_es_bars() -> dict[str, Any]:
    from bars import fetch_bars
    from cdp import get_chart_info

    info = await get_chart_info()
    symbol = str((info or {}).get("symbol") or "")
    if not _is_es_symbol(symbol):
        return {
            "ok": False,
            "error_code": "SYMBOL_NOT_ES_FUTURES",
            "error": "symbol_not_es_futures",
            "symbol": symbol,
            "status": "FORWARD_DATA_BLOCKED",
            "missing": "CME ES futures chart (not NQ/GC/MES-as-signal/CFD/cash)",
            "note": "Locked Phase 47 ES DVP requires a CME ES futures chart for live bars.",
        }
    payload = await fetch_bars()
    if not payload.get("ok"):
        return {
            "ok": False,
            "error_code": "BARS_UNAVAILABLE",
            "error": payload.get("error") or "bars_unavailable",
            "symbol": symbol,
            "status": "FORWARD_DATA_BLOCKED",
            "missing": "real-time CME ES OHLCV via CDP fetch_bars",
        }
    bars = _to_bars(payload.get("bars") or [])
    if not bars:
        return {
            "ok": False,
            "error_code": "EMPTY_BARS",
            "symbol": symbol,
            "status": "FORWARD_DATA_BLOCKED",
            "missing": "non-empty real-time ES bars",
        }
    return {"ok": True, "symbol": symbol, "bars": bars, "chart_info": info or {}, "status": "FORWARD_DATA_READY"}


def _state_machine(snap_state: str, *, open_position: bool) -> str:
    if open_position:
        return "OPEN_POSITION"
    mapping = {
        "WAITING_FOR_SESSION": "NO_SETUP",
        "VWAP_BUILDING": "NO_SETUP",
        "WAITING_FOR_1030": "NO_SETUP",
        "WAITING_FOR_DRIFT": "NO_SETUP",
        "SESSION_CLOSED": "SESSION_CANCEL",
        "DAILY_LOSS_CAP": "NO_SETUP",
        "DAILY_TRADE_CAP": "NO_SETUP",
        "POSITIVE_DRIFT": "SETUP_ARMED",
        "NEGATIVE_DRIFT": "SETUP_ARMED",
        "WAITING_FOR_PULLBACK": "SETUP_ARMED",
        "ENTRY_PENDING_NEXT_BAR": "ENTRY_PENDING",
        "TARGET_HIT": "TARGET",
        "STOP_HIT": "STOP",
        "TIME_EXIT": "FORCE_CLOSE",
        "ERROR_SAFE_HALT": "NO_SETUP",
    }
    return mapping.get(snap_state, "NO_SETUP")


def compute_paper_state_from_bars(
    bars: Sequence[Bar],
    *,
    symbol: str = "ES",
    doc: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    doc = doc or load_locked_document()
    expected = locked_config_hash()
    if doc.get("locked_config_hash") != expected:
        return {"ok": False, "error_code": "LOCKED_CONFIG_MISMATCH", "stored": doc.get("locked_config_hash"), "recomputed": expected}

    ordered = sorted(bars, key=lambda b: int(b.time))
    if len(ordered) >= 2:
        gaps = sorted(int(ordered[i + 1].time) - int(ordered[i].time) for i in range(min(50, len(ordered) - 1)))
        med = gaps[len(gaps) // 2] if gaps else 60
    else:
        med = 60
    if med <= 90:
        bars_1m = ordered
        bars_5m = aggregate_1m_to_ny(bars_1m, 5)
        bars_15m = aggregate_1m_to_ny(bars_1m, 15)
    elif med <= 360:
        bars_1m = ordered
        bars_5m = ordered
        bars_15m = aggregate_1m_to_ny(ordered, 15)
    else:
        bars_1m = bars_5m = bars_15m = ordered

    dates = trading_dates_ny(ordered)
    if not dates:
        return {"ok": False, "error": "no_trading_dates", "symbol": symbol}
    td = dates[-1]
    daily = recover_daily_state_from_journal(td)
    snap = evaluate_completed_bars(
        bars_1m=bars_1m,
        bars_5m=bars_5m,
        bars_15m=bars_15m,
        trading_date=td,
        cfg=LOCKED_CFG,
        daily_trades=int(daily.get("daily_trade_count") or 0),
        daily_losses=int(daily.get("daily_loss_count") or 0),
    )
    journal = summarize_paper_journal()
    n = int(journal.get("resolved") or 0)
    machine = _state_machine(snap.state, open_position=False)
    now_et = datetime.now(tz=NY)
    return {
        "ok": True,
        "locked_config_hash": doc.get("locked_config_hash"),
        "strategy_version": LOCKED_VERSION,
        "instrument": "ES",
        "current_es_contract": symbol,
        "micro_reference": "MES",
        "et_time": now_et.isoformat(),
        "trading_date": td,
        "session": ES_CLOCK,
        "vwap": snap.vwap,
        "current_15m_close": snap.current_15m_close,
        "hour_return": snap.hour_return,
        "drift": snap.drift,
        "engine_state": snap.state,
        "forward_state": machine,
        "pending_entry": snap.pending_entry,
        "intended_order": snap.intended_order,
        "daily_trade_count": daily.get("daily_trade_count"),
        "daily_loss_count": daily.get("daily_loss_count"),
        "paper_campaign_n": n,
        "campaign_status": campaign_status(n),
        "sample_label": sample_label(n),
        "progress": f"ES_FORWARD_N = {n} / 30",
        "primary_fill_assumption": "1_TICK_ADVERSE",
        "broker_execution": False,
        "DRY_RUN_ONLY": True,
        "NOT_PRODUCTION": True,
        "blocked_reason": snap.blocked_reason,
        "data_freshness_sec": snap.data_freshness_sec,
        "note": "Locked Phase 47 ES DVP. Read-only paper observation. No broker orders. Signals from ES, not MES.",
    }


async def analyze_locked_es_dvp_paper_state() -> dict[str, Any]:
    doc = load_locked_document()
    check_hash = locked_config_hash()
    if doc.get("locked_config_hash") != check_hash:
        return {"ok": False, "error_code": "LOCKED_CONFIG_MISMATCH"}
    journal = summarize_paper_journal()
    n = int(journal.get("resolved") or 0)
    try:
        fetched = await _fetch_es_bars()
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "error_code": "CHART_FEED_UNAVAILABLE",
            "status": "FORWARD_DATA_BLOCKED",
            "missing": "real-time CME ES futures chart via CDP (get_chart_info + fetch_bars)",
            "locked_config_hash": doc.get("locked_config_hash"),
            "paper_campaign_n": n,
            "campaign_status": campaign_status(n, data_blocked=True),
            "broker_execution": False,
            "error": str(exc),
        }
    if not fetched.get("ok"):
        return {
            "ok": False,
            "error_code": fetched.get("error_code") or "BARS_UNAVAILABLE",
            "status": fetched.get("status") or "FORWARD_DATA_BLOCKED",
            "missing": fetched.get("missing"),
            "error": fetched.get("error"),
            "symbol": fetched.get("symbol"),
            "locked_config_hash": doc.get("locked_config_hash"),
            "strategy_version": LOCKED_VERSION,
            "paper_campaign_n": n,
            "campaign_status": campaign_status(n, data_blocked=True),
            "broker_execution": False,
            "note": fetched.get("note") or "Live ES bars unavailable; locked campaign metadata only. No invented trades.",
        }
    return compute_paper_state_from_bars(fetched["bars"], symbol=fetched["symbol"], doc=doc)
