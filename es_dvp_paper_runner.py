"""Phase 47 — DRY_RUN paper runner for locked ES DVP.

No broker execution. No historical backfill. Restart-safe and idempotent.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from typing import Any, Optional, Sequence
from zoneinfo import ZoneInfo

from es_dvp_lock import LOCKED_CFG, LOCKED_VERSION, load_locked_document, locked_config_hash
from es_dvp_paper import (
    COMMISSION_POINTS,
    ESDVPForwardTrade,
    MES_POINT_USD,
    POINT_USD,
    PRIMARY_ADVERSE_TICKS,
    PRIMARY_FILL,
    TICK,
    append_paper_trade,
    append_setup_diagnostic,
    counts_toward_forward,
    fill_overlays,
    load_runner_state,
    overlay_net,
    paper_trade_id,
    refuse_custom_strategy_params,
    restore_runner_from_journal,
    save_runner_state,
    setup_key,
)
from family_port_engine import in_news_blackout, resolve_path
from models import Bar
from nq_databento import aggregate_1m_to_ny
from nq_drift_vwap_engine import session_anchors, trading_dates_ny
from nq_dvp_live_signal import evaluate_completed_bars

NY = ZoneInfo("America/New_York")


def _dir_label(direction: str) -> str:
    d = str(direction).upper()
    if d in ("BEARISH", "SHORT"):
        return "SHORT"
    if d in ("BULLISH", "LONG"):
        return "LONG"
    return d


def _hist_dir(direction: str) -> str:
    return "bearish" if _dir_label(direction) == "SHORT" else "bullish"


def process_completed_bars(
    bars_1m: Sequence[Bar],
    *,
    bars_5m: Optional[Sequence[Bar]] = None,
    bars_15m: Optional[Sequence[Bar]] = None,
    now_ts: Optional[int] = None,
    persist: bool = True,
    **forbidden: Any,
) -> dict[str, Any]:
    """Advance paper state from completed bars. Never places broker orders."""
    refuse_custom_strategy_params(**forbidden)
    doc = load_locked_document()
    lock_hash = locked_config_hash()
    if doc.get("locked_config_hash") != lock_hash:
        return {"ok": False, "error_code": "LOCKED_CONFIG_MISMATCH"}
    lock_ts = str(doc.get("lock_timestamp") or "")
    st = restore_runner_from_journal(lock_hash)
    st["broker_execution"] = False
    st["mode"] = "DRY_RUN"

    b1 = list(bars_1m)
    b5 = list(bars_5m) if bars_5m is not None else aggregate_1m_to_ny(b1, 5)
    b15 = list(bars_15m) if bars_15m is not None else aggregate_1m_to_ny(b1, 15)
    dates = trading_dates_ny(b1)
    if not dates:
        return {"ok": False, "error_code": "NO_TRADING_DATES"}
    td = dates[-1]
    if st.get("session_date") != td:
        st["session_date"] = td
        st["daily_trades"] = 0
        st["daily_losses"] = 0
        st["armed_setup"] = None
        if st.get("open_position") is None:
            st["state"] = "NO_SETUP"

    now = int(now_ts if now_ts is not None else datetime.now(tz=NY).timestamp())
    snap = evaluate_completed_bars(
        bars_1m=b1,
        bars_5m=b5,
        bars_15m=b15,
        trading_date=td,
        cfg=LOCKED_CFG,
        daily_trades=int(st.get("daily_trades") or 0),
        daily_losses=int(st.get("daily_losses") or 0),
        seen_triggers=set(st.get("seen_triggers") or []),
        now_ts=now,
    )

    events: list[str] = []
    written = 0
    setup_written = 0

    pending = snap.pending_entry or {}
    intended = snap.intended_order
    if pending:
        direction = _dir_label(str(pending.get("direction") or ""))
        trig = int(pending.get("trigger_ts") or 0)
        key = setup_key(td, trig, direction)
        diag = {
            "setup_key": key,
            "strategy_family": "nq_drift_vwap_pullback_v1",
            "instrument": "ES",
            "session_date": td,
            "setup_timestamp": trig,
            "direction": direction,
            "state": "SETUP_ARMED" if not intended else "ENTRY_PENDING",
            "lock_timestamp": lock_ts,
            "counts_toward_forward": counts_toward_forward(trig, lock_ts),
            "note": "non-entered diagnostic; does not increment forward N",
        }
        if persist and append_setup_diagnostic(diag):
            setup_written += 1
        seen = set(st.get("seen_setup_keys") or [])
        seen.add(key)
        st["seen_setup_keys"] = sorted(seen)
        st["armed_setup"] = pending
        st["state"] = "ENTRY_PENDING" if intended else "SETUP_ARMED"
        if not counts_toward_forward(trig, lock_ts):
            events.append("SETUP_BEFORE_LOCK_IGNORED")
            st["state"] = "INVALIDATED_BEFORE_ENTRY"

    # Resolve an already-open paper position from 1m path
    open_pos = st.get("open_position")
    if open_pos:
        anchors = session_anchors(td)
        path = resolve_path(
            b1,
            entry_ts=int(open_pos["entry_timestamp"]),
            direction=_hist_dir(open_pos["direction"]),
            entry=float(open_pos["theoretical_entry_price"]),
            stop=float(open_pos["stop_price"]),
            target=float(open_pos["target_price"]),
            flatten_ts=int(anchors["force_close"]),
        )
        outcome = path.get("outcome")
        if outcome in ("TARGET_HIT", "STOP_HIT", "FORCE_CLOSE", "TIME_EXIT", "AMBIGUOUS"):
            reason = {
                "TARGET_HIT": "TARGET",
                "STOP_HIT": "STOP",
                "FORCE_CLOSE": "FORCE_CLOSE",
                "TIME_EXIT": "FORCE_CLOSE",
                "AMBIGUOUS": "AMBIGUOUS",
            }.get(str(outcome), str(outcome))
            raw = None
            theo = float(open_pos["theoretical_entry_price"])
            exit_px = path.get("exit")
            if exit_px is not None:
                if _dir_label(open_pos["direction"]) == "SHORT":
                    raw = theo - float(exit_px)
                else:
                    raw = float(exit_px) - theo
            net = overlay_net(raw, PRIMARY_ADVERSE_TICKS)
            risk = abs(theo - float(open_pos["stop_price"])) or 18.0
            trade = ESDVPForwardTrade(
                paper_trade_id=str(open_pos["paper_trade_id"]),
                strategy_family="nq_drift_vwap_pullback_v1",
                strategy_version=LOCKED_VERSION,
                config_hash=lock_hash,
                instrument="ES",
                contract=str(open_pos.get("contract") or "ES"),
                session_date=td,
                timezone="America/New_York",
                direction=_dir_label(open_pos["direction"]),
                setup_timestamp=open_pos.get("setup_timestamp"),
                signal_timestamp=open_pos.get("signal_timestamp"),
                entry_timestamp=open_pos.get("entry_timestamp"),
                entry_price=open_pos.get("entry_price"),
                theoretical_entry_price=theo,
                stop_price=open_pos.get("stop_price"),
                target_price=open_pos.get("target_price"),
                exit_timestamp=path.get("exit_ts"),
                exit_price=exit_px,
                exit_reason=reason,
                raw_pnl_points=raw,
                net_pnl_points=net,
                pnl_dollars_es=None if net is None else net * POINT_USD,
                pnl_dollars_mes=None if net is None else net * MES_POINT_USD,
                r_result=None if net is None else net / risk,
                slippage_assumption=PRIMARY_FILL,
                news_blackout=bool(open_pos.get("news_blackout")),
                daily_trade_number=int(open_pos.get("daily_trade_number") or 0),
                daily_prior_losses=int(open_pos.get("daily_prior_losses") or 0),
                mfe_points=path.get("mfe"),
                mae_points=path.get("mae"),
                state=reason if reason in ("TARGET", "STOP", "FORCE_CLOSE") else "OPEN_POSITION",
                notes="DRY_RUN paper resolve; Phase 46 cost overlay 2-way 1-tick + 0.08 commission",
                created_at=str(open_pos.get("created_at") or datetime.now(tz=timezone.utc).isoformat()),
                updated_at=datetime.now(tz=timezone.utc).isoformat(),
                extras={
                    "fill_overlays": fill_overlays(theo, open_pos["direction"]),
                    "commission_points": COMMISSION_POINTS,
                    "micro_contract": "MES",
                    "cost_points_primary": 2.0 * PRIMARY_ADVERSE_TICKS * TICK + COMMISSION_POINTS,
                },
            )
            if persist and counts_toward_forward(int(open_pos.get("setup_timestamp") or 0), lock_ts):
                if append_paper_trade(trade):
                    written += 1
                    events.append("TRADE_RESOLVED")
                    if net is not None and net <= 0:
                        st["daily_losses"] = int(st.get("daily_losses") or 0) + 1
            elif persist:
                events.append("RESOLVE_BEFORE_LOCK_NOT_JOURNALED")
            st["open_position"] = None
            st["armed_setup"] = None
            st["state"] = reason if reason in ("TARGET", "STOP", "FORCE_CLOSE") else "NO_SETUP"
            seen_ids = set(st.get("seen_trade_ids") or [])
            seen_ids.add(trade.paper_trade_id)
            st["seen_trade_ids"] = sorted(seen_ids)
        else:
            st["state"] = "OPEN_POSITION"

    # Arm a new entry only if no open position and setup is after lock
    if st.get("open_position") is None and intended and counts_toward_forward(int(intended.get("trigger_ts") or 0), lock_ts):
        direction = _dir_label(str(intended.get("direction") or ""))
        trig = int(intended["trigger_ts"])
        tid = paper_trade_id(td, direction, trig)
        if tid not in set(st.get("seen_trade_ids") or []):
            entry_ts = int(intended["entry_timestamp"])
            theo = float(intended["signal_entry_price"])
            news = in_news_blackout(entry_ts, "ES")
            if news:
                events.append("NEWS_BLACKOUT")
                st["state"] = "INVALIDATED_BEFORE_ENTRY"
            else:
                delta = PRIMARY_ADVERSE_TICKS * TICK
                fill = theo - delta if direction == "SHORT" else theo + delta
                st["open_position"] = {
                    "paper_trade_id": tid,
                    "contract": "ES",
                    "direction": direction,
                    "setup_timestamp": trig,
                    "signal_timestamp": trig,
                    "entry_timestamp": entry_ts,
                    "theoretical_entry_price": theo,
                    "entry_price": fill,
                    "stop_price": float(intended["theoretical_stop"]),
                    "target_price": float(intended["theoretical_target"]),
                    "news_blackout": False,
                    "daily_trade_number": int(st.get("daily_trades") or 0) + 1,
                    "daily_prior_losses": int(st.get("daily_losses") or 0),
                    "created_at": datetime.now(tz=timezone.utc).isoformat(),
                }
                st["state"] = "OPEN_POSITION"
                st["daily_trades"] = int(st.get("daily_trades") or 0) + 1
                events.append("ENTRY_ARMED")
                seen_ids = set(st.get("seen_trade_ids") or [])
                seen_ids.add(tid)
                st["seen_trade_ids"] = sorted(seen_ids)
                trig_seen = set(st.get("seen_triggers") or [])
                if intended.get("trigger_key"):
                    trig_seen.add(str(intended["trigger_key"]))
                st["seen_triggers"] = sorted(trig_seen)

    st["last_event"] = {
        "ts": datetime.now(tz=NY).isoformat(),
        "engine_state": snap.state,
        "forward_state": st.get("state"),
        "events": events,
    }
    if persist:
        save_runner_state(st)
    return {
        "ok": True,
        "mode": "DRY_RUN",
        "broker_execution": False,
        "locked_config_hash": lock_hash,
        "session_date": td,
        "state": st.get("state"),
        "engine_state": snap.state,
        "events": events,
        "persisted_trades": written,
        "persisted_setups": setup_written,
        "open_position": st.get("open_position"),
        "daily_trades": st.get("daily_trades"),
        "daily_losses": st.get("daily_losses"),
        "blocked_reason": snap.blocked_reason,
    }


def status() -> dict[str, Any]:
    doc = load_locked_document()
    st = load_runner_state()
    from es_dvp_paper import summarize_paper_journal

    journal = summarize_paper_journal()
    n = int(journal.get("resolved") or 0)
    return {
        "ok": True,
        "mode": "DRY_RUN",
        "broker_execution": False,
        "locked_config_hash": doc.get("locked_config_hash"),
        "lock_timestamp": doc.get("lock_timestamp"),
        "runner": {k: st.get(k) for k in ("session_date", "state", "daily_trades", "daily_losses", "open_position")},
        "forward_n": n,
        "progress": f"ES_FORWARD_N = {n} / 30",
        "NOT_PRODUCTION": True,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="ES DVP locked paper runner (DRY_RUN only)")
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--enable-sim-execution", action="store_true", help="rejected — Phase 47 has no broker path")
    args = parser.parse_args()
    if args.enable_sim_execution:
        raise SystemExit("BROKER_EXECUTION_FORBIDDEN: Phase 47 ES DVP is DRY_RUN_ONLY")
    if args.status:
        print(status())
        return 0
    print(status())
    print("No live bars in CLI. Attach CME ES chart and call analyze_locked_es_dvp_paper_state / process_completed_bars.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
