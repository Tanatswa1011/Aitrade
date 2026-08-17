"""Live read-only GC VWAP mean-reversion analysis (GC futures only).

Phase 26: frozen V2 paper-state tool with no tunable strategy parameters.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional
from zoneinfo import ZoneInfo

from gc_orb_engine import detect_roll_gap_timestamps as _roll
from gc_orb_engine import trading_dates_in_bars
from gc_orb_live import _is_gc_symbol
from gc_vwap_engine import (
    analyze_candidate,
    collect_extension_sequences,
    compute_session_vwap_series,
    config_hash,
)
from gc_vwap_freeze import (
    FROZEN_JSON,
    FROZEN_STRATEGY_VERSION,
    assert_runtime_matches_frozen,
    load_frozen_document,
    load_frozen_strategy_config,
)
from gc_vwap_models import (
    OR_TIMEZONE,
    PHASE25_CANDIDATES,
    SESSION_END_LOCAL,
    SESSION_NOTE,
    SESSION_START_LOCAL,
    STRATEGY_FAMILY,
    STRATEGY_VERSION,
)
from gc_vwap_paper import (
    fill_sensitivity_overlay,
    paper_campaign_status,
    sample_label,
    summarize_paper_journal,
)
from models import Bar

NY = ZoneInfo(OR_TIMEZONE)


def _to_bars(raw: list) -> list[Bar]:
    bars: list[Bar] = []
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
    return bars


def _band_dict(latest) -> dict[str, Any]:
    if latest is None:
        return {}
    b1 = latest.band_1
    b2 = latest.band_2
    b3 = latest.band_3
    return {
        "vwap": latest.vwap,
        "sigma": latest.session_std,
        "plus_1sigma": None if not b1 else b1[1],
        "minus_1sigma": None if not b1 else b1[0],
        "plus_2sigma": None if not b2 else b2[1],
        "minus_2sigma": None if not b2 else b2[0],
        "plus_3sigma": None if not b3 else b3[1],
        "minus_3sigma": None if not b3 else b3[0],
        "current_z": latest.z_vwap,
        "bars_used": latest.bars_used,
        "valid": latest.valid,
    }


def _setup_state_label(setup) -> str:
    if setup.direction in ("bullish", "long"):
        return "LONG_SETUP_STATE"
    if setup.direction in ("bearish", "short"):
        return "SHORT_SETUP_STATE"
    return "NO_DIRECTION_STATE"


def _paper_observation_from_setup(setup, seq: Optional[dict]) -> dict[str, Any]:
    if seq is None:
        return {"status": "OBSERVING"}
    if setup.entry_triggered:
        status = "ENTRY_TRIGGERED"
    elif setup.reason == "no_confirmation":
        status = "EXTENSION_FOUND"
    elif setup.reason == "entry_timeout":
        status = "WAITING_FOR_RETEST"
    elif setup.extras and setup.extras.get("confirmation_timestamp") and not setup.entry_triggered:
        status = "WAITING_FOR_RETEST"
    elif setup.state == "EXPIRED":
        status = "EXPIRED"
    else:
        status = "OBSERVING"
    if setup.extras and setup.extras.get("confirmation_timestamp") and not setup.entry_triggered:
        if setup.reason != "entry_timeout":
            status = "RECLAIM_CONFIRMED" if status == "OBSERVING" else status
    return {
        "status": status,
        "setup_state_label": _setup_state_label(setup),
        "entry_triggered": setup.entry_triggered,
        "reason": setup.reason,
    }


def _bars_remaining(seq: Optional[dict], setup) -> Optional[int]:
    if seq is None or not setup.extras:
        return None
    conf_ts = setup.extras.get("confirmation_timestamp")
    if conf_ts is None:
        return None
    if setup.entry_triggered:
        return 0
    session_bars = seq.get("session_bars") or []
    conf_idx = None
    for i, b in enumerate(session_bars):
        if int(b.time) == int(conf_ts):
            conf_idx = i
            break
    if conf_idx is None:
        return None
    # bars after confirmation that have already elapsed within timeout window
    max_bars = 6
    elapsed = 0
    for k in range(conf_idx + 1, min(len(session_bars), conf_idx + 1 + max_bars)):
        elapsed += 1
    return max(0, max_bars - elapsed)


async def _fetch_gc_bars() -> dict[str, Any]:
    from bars import fetch_bars
    from cdp import get_chart_info

    info = await get_chart_info()
    symbol = str((info or {}).get("symbol") or "")
    if not _is_gc_symbol(symbol):
        return {
            "ok": False,
            "error": "symbol_not_gc_futures",
            "symbol": symbol,
            "note": "Refuse XAUUSD/spot charts for GC VWAP tools.",
        }
    payload = await fetch_bars()
    if not payload.get("ok"):
        return {"ok": False, "error": payload.get("error") or "bars_unavailable", "symbol": symbol}
    bars = _to_bars(payload.get("bars") or [])
    if not bars:
        return {"ok": False, "error": "empty_bars", "symbol": symbol}
    return {"ok": True, "symbol": symbol, "bars": bars, "chart_info": info or {}}


async def analyze_live_gc_vwap_reversion() -> dict[str, Any]:
    fetched = await _fetch_gc_bars()
    if not fetched.get("ok"):
        return fetched

    symbol = fetched["symbol"]
    bars = fetched["bars"]
    dates = trading_dates_in_bars(bars)
    td = dates[-1]
    states = compute_session_vwap_series(bars, td)
    latest = states[-1] if states else None
    roll = _roll(bars)
    seqs = collect_extension_sequences(bars, td, roll_flags=roll)
    last_seq = seqs[-1] if seqs else None
    candidates = []
    frozen_payload = None
    if FROZEN_JSON.exists():
        try:
            doc = load_frozen_document()
            cfg = load_frozen_strategy_config(doc)
            check = assert_runtime_matches_frozen(cfg, doc)
            if last_seq is not None and check.get("ok"):
                setup = analyze_candidate(last_seq, cfg)
                frozen_payload = _frozen_v2_snapshot(doc, cfg, latest, last_seq, setup, symbol, td)
        except Exception as exc:  # noqa: BLE001
            frozen_payload = {"ok": False, "error": str(exc)}

    if last_seq is not None:
        for cfg in PHASE25_CANDIDATES:
            setup = analyze_candidate(last_seq, cfg)
            candidates.append(
                {
                    "candidate_id": cfg.candidate_id,
                    "state": setup.state,
                    "reason": setup.reason,
                    "entry_price": setup.entry_price,
                    "stop_price": setup.stop_price,
                    "triggered": setup.entry_triggered,
                    "setup_state_label": _setup_state_label(setup),
                }
            )

    return {
        "ok": True,
        "strategy_family": STRATEGY_FAMILY,
        "strategy_version": STRATEGY_VERSION,
        "symbol": symbol,
        "trading_date": td,
        "session": {
            "start": SESSION_START_LOCAL,
            "end": SESSION_END_LOCAL,
            "note": SESSION_NOTE,
            "timezone": OR_TIMEZONE,
        },
        "vwap_state": None if latest is None else latest.to_dict(),
        "bands": _band_dict(latest),
        "extension_sequences_today": len(seqs),
        "latest_extension": None
        if last_seq is None
        else {
            "side": last_seq["side"],
            "direction": last_seq["direction"],
            "first_ts": last_seq["first_ts"],
            "extreme": last_seq["extreme"],
            "reclaim": last_seq["reclaim_bar"] is not None,
            "max_abs_z": last_seq["max_abs_z"],
            "frozen_2sig": last_seq.get("frozen_2sig"),
        },
        "candidates": candidates,
        "frozen_v2": frozen_payload,
        "note": "Read-only descriptive levels; not an order signal. No BUY/SELL language.",
    }


def _frozen_v2_snapshot(doc, cfg, latest, last_seq, setup, symbol: str, td: str) -> dict[str, Any]:
    journal = summarize_paper_journal()
    targets = {float(t["rr"]): float(t["price"]) for t in (setup.targets or [])}
    fill_rows = []
    if setup.entry_price is not None and setup.risk_distance:
        fill_rows = fill_sensitivity_overlay(
            float(setup.entry_price), setup.direction, float(setup.risk_distance)
        )
    conf_ts = (setup.extras or {}).get("confirmation_timestamp")
    return {
        "ok": True,
        "frozen_config_hash": doc.get("frozen_config_hash"),
        "engine_config_hash": config_hash(cfg),
        "strategy_version": FROZEN_STRATEGY_VERSION,
        "candidate_id": cfg.candidate_id,
        "symbol": symbol,
        "contract": symbol,
        "trading_date": td,
        "observed_at_utc": datetime.now(tz=timezone.utc).isoformat(),
        "session_status": {
            "timezone": OR_TIMEZONE,
            "start": SESSION_START_LOCAL,
            "end": SESSION_END_LOCAL,
            "no_new_setups_after": doc.get("session", {}).get("no_new_setups_after"),
        },
        "bands": _band_dict(latest),
        "extension_state": {
            "active": last_seq is not None,
            "direction": None if last_seq is None else last_seq["direction"],
            "side": None if last_seq is None else last_seq["side"],
            "extreme": None if last_seq is None else last_seq["extreme"],
            "first_ts": None if last_seq is None else last_seq["first_ts"],
            "max_abs_z": None if last_seq is None else last_seq["max_abs_z"],
            "reclaim": None if last_seq is None else last_seq["reclaim_bar"] is not None,
        },
        "reclaim_state": {
            "confirmed": conf_ts is not None,
            "confirmation_timestamp": conf_ts,
        },
        "frozen_entry_band": last_seq.get("frozen_2sig") if last_seq else None,
        "bars_remaining_before_entry_expiry": _bars_remaining(last_seq, setup),
        "entry_state": {
            "mode": cfg.entry_mode,
            "triggered": setup.entry_triggered,
            "entry_price": setup.entry_price,
            "entry_timestamp": setup.entry_timestamp,
            "setup_state_label": _setup_state_label(setup),
        },
        "stop": setup.stop_price,
        "targets": {
            "1R": targets.get(1.0),
            "1.5R": targets.get(1.5),
            "2R": targets.get(2.0),
            "3R": targets.get(3.0),
        },
        "fill_overlays": fill_rows,
        "paper_observation_state": _paper_observation_from_setup(setup, last_seq),
        "paper_campaign": {
            "resolved_n": journal.get("resolved", 0),
            "sample_label": journal.get("sample_label"),
            "status": journal.get("campaign_status") or paper_campaign_status(0),
            "minimum_resolved": 30,
            "preferred_resolved": 50,
            "strong_resolved": 100,
        },
        "note": "Frozen Phase 26 V2 only. Read-only paper observation. No broker orders.",
    }


async def analyze_frozen_gc_vwap_v2_paper_state() -> dict[str, Any]:
    """ONLY frozen V2 — no custom strategy parameters accepted."""
    if not FROZEN_JSON.exists():
        return {"ok": False, "error_code": "MISSING_FROZEN_FILE"}

    doc = load_frozen_document()
    cfg = load_frozen_strategy_config(doc)
    check = assert_runtime_matches_frozen(cfg, doc)
    if not check.get("ok"):
        return {
            "ok": False,
            "error_code": "FROZEN_CONFIG_MISMATCH",
            "check": check,
        }

    fetched = await _fetch_gc_bars()
    if not fetched.get("ok"):
        return fetched

    symbol = fetched["symbol"]
    bars = fetched["bars"]
    dates = trading_dates_in_bars(bars)
    td = dates[-1]
    states = compute_session_vwap_series(bars, td)
    latest = states[-1] if states else None
    roll = _roll(bars)
    seqs = collect_extension_sequences(bars, td, roll_flags=roll)
    last_seq = seqs[-1] if seqs else None
    if last_seq is None:
        journal = summarize_paper_journal()
        return {
            "ok": True,
            "frozen_config_hash": doc.get("frozen_config_hash"),
            "strategy_version": FROZEN_STRATEGY_VERSION,
            "symbol": symbol,
            "trading_date": td,
            "bands": _band_dict(latest),
            "extension_state": {"active": False},
            "paper_observation_state": {"status": "OBSERVING"},
            "paper_campaign": {
                "resolved_n": journal.get("resolved", 0),
                "sample_label": sample_label(int(journal.get("resolved") or 0)),
                "status": paper_campaign_status(int(journal.get("resolved") or 0)),
            },
            "note": "No extension sequence yet today under frozen V2 rules.",
        }

    setup = analyze_candidate(last_seq, cfg)
    return _frozen_v2_snapshot(doc, cfg, latest, last_seq, setup, symbol, td)
