"""Read-only live/paper analyzer for frozen NQ Drift VWAP Pullback (Phase 30).

No strategy parameters. No broker orders. Technical setup state only.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional, Sequence
from zoneinfo import ZoneInfo

from models import Bar
from nq_databento import aggregate_1m_to_ny
from nq_drift_vwap_engine import (
    classify_drift_15m,
    compute_session_vwap_by_ts,
    session_anchors,
    trading_dates_ny,
    vwap_at_or_before,
)
from nq_drift_vwap_models import NQ_TICK_SIZE, OR_TIMEZONE
from nq_dvp_freeze import (
    FROZEN_JSON,
    FROZEN_STRATEGY_VERSION,
    assert_runtime_matches_frozen,
    load_frozen_document,
    load_frozen_strategy_config,
)
from nq_dvp_paper import (
    paper_campaign_status,
    recover_daily_state_from_journal,
    sample_label,
    summarize_paper_journal,
)

NY = ZoneInfo(OR_TIMEZONE)


def _is_nq_symbol(symbol: str) -> bool:
    s = (symbol or "").upper().replace(":", "")
    if "MNQ" in s or "CFD" in s or "XAU" in s:
        return False
    if any(tok in s for tok in ("ES1!", "ES=F", "/ES", "GC1!", "GC=F", "/GC")) and "NQ" not in s:
        return False
    return (
        "NQ1!" in s
        or "NQ=F" in s
        or "/NQ" in s
        or s.endswith("NQ")
        or s.startswith("NQ")
        or "CME_MININQ" in s
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


async def _fetch_nq_bars() -> dict[str, Any]:
    from bars import fetch_bars
    from cdp import get_chart_info

    info = await get_chart_info()
    symbol = str((info or {}).get("symbol") or "")
    if not _is_nq_symbol(symbol):
        return {
            "ok": False,
            "error_code": "SYMBOL_NOT_NQ_FUTURES",
            "error": "symbol_not_nq_futures",
            "symbol": symbol,
            "note": "Frozen Phase 30 requires CME NQ futures chart (not GC/ES/MNQ/CFD/cash).",
        }
    payload = await fetch_bars()
    if not payload.get("ok"):
        return {"ok": False, "error": payload.get("error") or "bars_unavailable", "symbol": symbol}
    bars = _to_bars(payload.get("bars") or [])
    if not bars:
        return {"ok": False, "error": "empty_bars", "symbol": symbol}
    return {"ok": True, "symbol": symbol, "bars": bars, "chart_info": info or {}}


def _setup_language(drift: Optional[str], pullback_ready: bool) -> dict[str, Any]:
    """Technical state only — no discretionary BUY/SELL."""
    out: dict[str, Any] = {
        "LONG_SETUP_STATE": "INACTIVE",
        "SHORT_SETUP_STATE": "INACTIVE",
    }
    if drift == "POSITIVE_DRIFT":
        out["LONG_SETUP_STATE"] = "WAITING_FOR_PULLBACK" if not pullback_ready else "ENTRY_TRIGGERED"
        out["current_drift_state"] = "POSITIVE_DRIFT"
    elif drift == "NEGATIVE_DRIFT":
        out["SHORT_SETUP_STATE"] = "WAITING_FOR_PULLBACK" if not pullback_ready else "ENTRY_TRIGGERED"
        out["current_drift_state"] = "NEGATIVE_DRIFT"
    else:
        out["current_drift_state"] = "WAITING_FOR_DRIFT"
    return out


def compute_paper_state_from_bars(
    bars: Sequence[Bar],
    *,
    symbol: str = "NQ",
    doc: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    doc = doc or load_frozen_document()
    cfg = load_frozen_strategy_config(doc)
    check = assert_runtime_matches_frozen(cfg, doc)
    if not check.get("ok"):
        return {"ok": False, "error_code": "FROZEN_CONFIG_MISMATCH", "check": check}

    ordered = sorted(bars, key=lambda b: int(b.time))
    # If chart is not 1m, treat as execution bars and still aggregate 15m from them when possible
    # Prefer detecting resolution by median spacing
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
        bars_1m = ordered  # best-effort VWAP from 5m
        bars_5m = ordered
        bars_15m = aggregate_1m_to_ny(ordered, 15)
    else:
        bars_1m = ordered
        bars_5m = ordered
        bars_15m = ordered

    dates = trading_dates_ny(ordered)
    if not dates:
        return {"ok": False, "error": "no_trading_dates", "symbol": symbol}
    td = dates[-1]
    anchors = session_anchors(td)
    now_et = datetime.now(tz=NY)
    now_ts = int(now_et.timestamp())

    vwap_map = compute_session_vwap_by_ts(bars_1m, td)
    vwap_keys = sorted(vwap_map)
    b15 = [b for b in bars_15m if int(b.time) >= anchors["vwap_reset"]]
    b5 = [b for b in bars_5m if int(b.time) >= anchors["vwap_reset"]]

    drift = None
    vwap_now = vwap_prev = close_15 = hour_ret = None
    if b15:
        idx = len(b15) - 1
        drift = classify_drift_15m(b15, idx, vwap_map, threshold=cfg.hour_return_threshold, vwap_keys=vwap_keys)
        bar = b15[idx]
        close_15 = float(bar.close)
        vwap_now = vwap_at_or_before(vwap_map, int(bar.time) + 14 * 60, vwap_keys)
        if idx >= 1:
            prev = b15[idx - 1]
            vwap_prev = vwap_at_or_before(vwap_map, int(prev.time) + 14 * 60, vwap_keys)
        if idx >= 4 and float(b15[idx - 4].close) != 0:
            hour_ret = close_15 / float(b15[idx - 4].close) - 1.0

    pullback_ready = False
    next_entry = None
    stop = target = None
    if b5 and drift in ("POSITIVE_DRIFT", "NEGATIVE_DRIFT"):
        last5 = b5[-1]
        red = float(last5.close) < float(last5.open)
        green = float(last5.close) > float(last5.open)
        if drift == "POSITIVE_DRIFT" and red:
            pullback_ready = True
        if drift == "NEGATIVE_DRIFT" and green:
            pullback_ready = True
        if pullback_ready:
            # next bar open unknown until forms — report theoretical levels from last close as illustrative only
            entry_ref = float(last5.close)
            if drift == "POSITIVE_DRIFT":
                stop = entry_ref - cfg.long_stop_points
                target = entry_ref + cfg.long_target_points
                next_entry = {"direction": "bullish", "note": "enter open of next completed 5m bar"}
            else:
                stop = entry_ref + cfg.short_stop_points
                target = entry_ref - cfg.short_target_points
                next_entry = {"direction": "bearish", "note": "enter open of next completed 5m bar"}

    daily = recover_daily_state_from_journal(td)
    journal = summarize_paper_journal()
    n = int(journal.get("resolved") or 0)

    session_status = "WAITING_FOR_SESSION"
    if now_ts < anchors["vwap_reset"]:
        session_status = "WAITING_FOR_SESSION"
    elif now_ts < anchors["trade_start"]:
        session_status = "WAITING_FOR_DRIFT"
    elif now_ts >= anchors["force_close"]:
        session_status = "SESSION_CLOSED"
    elif daily.get("hit_loss_cap"):
        session_status = "DAILY_LOSS_CAP"
    elif daily.get("hit_trade_cap"):
        session_status = "DAILY_TRADE_CAP"
    elif drift == "POSITIVE_DRIFT":
        session_status = "WAITING_FOR_PULLBACK" if not pullback_ready else "ENTRY_TRIGGERED"
    elif drift == "NEGATIVE_DRIFT":
        session_status = "WAITING_FOR_PULLBACK" if not pullback_ready else "ENTRY_TRIGGERED"
    else:
        session_status = "WAITING_FOR_DRIFT"

    setup = _setup_language(drift, pullback_ready)
    cutoff_secs = max(0, anchors["no_new"] - now_ts)

    tick = float((doc.get("cost_model_assumptions") or {}).get("tick_size_research") or NQ_TICK_SIZE)

    return {
        "ok": True,
        "frozen_config_hash": doc.get("frozen_config_hash"),
        "strategy_version": FROZEN_STRATEGY_VERSION,
        "current_nq_contract": symbol,
        "et_time": now_et.isoformat(),
        "trading_date": td,
        "vwap": vwap_now,
        "previous_15m_vwap": vwap_prev,
        "current_15m_close": close_15,
        "hour_return": hour_ret,
        "current_drift_state": setup.get("current_drift_state"),
        "pullback_trigger_state": "READY" if pullback_ready else "WAITING",
        "next_allowed_entry": next_entry,
        "stop": stop,
        "target": target,
        "daily_trade_count": daily.get("daily_trade_count"),
        "daily_loss_count": daily.get("daily_loss_count"),
        "time_until_cutoff_seconds": cutoff_secs,
        "paper_campaign_n": n,
        "campaign_status": paper_campaign_status(n),
        "sample_label": sample_label(n),
        "paper_observation_state": {"status": session_status},
        "LONG_SETUP_STATE": setup["LONG_SETUP_STATE"],
        "SHORT_SETUP_STATE": setup["SHORT_SETUP_STATE"],
        "tick_size": tick,
        "primary_fill_assumption": "1_TICK_ADVERSE",
        "broker_execution": False,
        "note": "Frozen Phase 30 NQ DVP only. Read-only paper observation. No broker orders.",
    }


async def analyze_frozen_nq_dvp_paper_state() -> dict[str, Any]:
    """ONLY frozen Phase 30 — no custom strategy parameters accepted."""
    if not FROZEN_JSON.exists():
        return {"ok": False, "error_code": "MISSING_FROZEN_FILE"}

    doc = load_frozen_document()
    cfg = load_frozen_strategy_config(doc)
    check = assert_runtime_matches_frozen(cfg, doc)
    if not check.get("ok"):
        return {"ok": False, "error_code": "FROZEN_CONFIG_MISMATCH", "check": check}

    fetched = await _fetch_nq_bars()
    if not fetched.get("ok"):
        # Still return campaign status without live bars
        journal = summarize_paper_journal()
        n = int(journal.get("resolved") or 0)
        return {
            "ok": False,
            "error_code": fetched.get("error_code") or "BARS_UNAVAILABLE",
            "error": fetched.get("error"),
            "symbol": fetched.get("symbol"),
            "frozen_config_hash": doc.get("frozen_config_hash"),
            "strategy_version": FROZEN_STRATEGY_VERSION,
            "paper_campaign_n": n,
            "campaign_status": paper_campaign_status(n),
            "sample_label": sample_label(n),
            "LONG_SETUP_STATE": "INACTIVE",
            "SHORT_SETUP_STATE": "INACTIVE",
            "broker_execution": False,
            "note": fetched.get("note") or "Chart bars unavailable; frozen campaign metadata only.",
        }

    return compute_paper_state_from_bars(fetched["bars"], symbol=fetched["symbol"], doc=doc)
