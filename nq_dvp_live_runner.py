"""Phase 31 — Frozen NQ DVP live runner → Sim101 MNQ (DRY_RUN by default).

Commands:
  python nq_dvp_live_runner.py
  python nq_dvp_live_runner.py --enable-sim-execution
  python nq_dvp_live_runner.py --status
  python nq_dvp_live_runner.py --halt
  python nq_dvp_live_runner.py --resume
  python nq_dvp_live_runner.py --once
"""

from __future__ import annotations

import argparse
import json
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

from bar_dataset import load_dataset
from nq_databento import DATA_ROOT, aggregate_1m_to_ny
from nq_drift_vwap_engine import index_bars_by_ny_date, trading_dates_ny
from nq_dvp_freeze import (
    FROZEN_JSON,
    FROZEN_STRATEGY_VERSION,
    load_frozen_document,
    load_frozen_strategy_config,
    semantic_payload,
    frozen_config_hash,
)
from nq_dvp_live_signal import STALE_5M_SECONDS, evaluate_completed_bars, extract_signal_entries_for_day
from execution_status import execution_summary, is_execution_paused
from nq_dvp_nt_exec import (
    EXEC_ACCOUNT,
    EXEC_INSTRUMENT,
    flatten_dvp_owned,
    frozen_risk_for_direction,
    plan_dvp_entry,
    submit_dvp_bracket,
)
import nt_ati as nt

NY = ZoneInfo("America/New_York")
PHASE26_HASH = "0695d7a881b6247033426bceb077b6895b96405b826c21f32a109a02f44b2d43"
PHASE26_FROZEN = Path("strategy_frozen") / "gc_vwap_v2_phase26.json"

JOURNAL_DIR = Path("journal") / "phase31_nq_dvp_sim"
LIVE_EVENTS = JOURNAL_DIR / "live_events.jsonl"
EXECUTIONS = JOURNAL_DIR / "executions.jsonl"
STATE_PATH = JOURNAL_DIR / "runner_state.json"
HALT_PATH = JOURNAL_DIR / "HALT"


def ensure_dirs() -> None:
    JOURNAL_DIR.mkdir(parents=True, exist_ok=True)
    if not LIVE_EVENTS.exists():
        LIVE_EVENTS.write_text("", encoding="utf-8")
    if not EXECUTIONS.exists():
        EXECUTIONS.write_text("", encoding="utf-8")


def append_jsonl(path: Path, row: dict[str, Any]) -> None:
    ensure_dirs()
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, default=str) + "\n")


def load_state() -> dict[str, Any]:
    ensure_dirs()
    if not STATE_PATH.exists():
        return {
            "mode": "DRY_RUN",
            "state": "WAITING_FOR_SESSION",
            "seen_triggers": [],
            "daily": {},
            "open_trade": None,
            "last_event": None,
            "halted": False,
        }
    return json.loads(STATE_PATH.read_text(encoding="utf-8"))


def save_state(state: dict[str, Any]) -> None:
    ensure_dirs()
    STATE_PATH.write_text(json.dumps(state, indent=2, default=str), encoding="utf-8")


def is_halted(state: Optional[dict[str, Any]] = None) -> bool:
    if HALT_PATH.exists():
        return True
    st = state or load_state()
    return bool(st.get("halted"))


def set_halt(halted: bool) -> dict[str, Any]:
    ensure_dirs()
    st = load_state()
    st["halted"] = halted
    if halted:
        HALT_PATH.write_text("HALT\n", encoding="utf-8")
        st["state"] = "ERROR_SAFE_HALT"
    elif HALT_PATH.exists():
        HALT_PATH.unlink()
    save_state(st)
    append_jsonl(
        LIVE_EVENTS,
        {
            "ts": datetime.now(tz=NY).isoformat(),
            "event": "HALT_ON" if halted else "HALT_OFF",
            "state": st.get("state"),
        },
    )
    return st


def assert_frozen_immutable() -> dict[str, Any]:
    if not FROZEN_JSON.exists():
        raise FileNotFoundError("missing_frozen_phase30")
    doc = load_frozen_document()
    cfg = load_frozen_strategy_config(doc)
    expected = frozen_config_hash(semantic_payload(cfg))
    ok = doc.get("frozen_config_hash") == expected
    if doc.get("strategy_version") != FROZEN_STRATEGY_VERSION:
        ok = False
    return {
        "ok": ok,
        "frozen_config_hash": doc.get("frozen_config_hash"),
        "recomputed": expected,
        "strategy_version": doc.get("strategy_version"),
        "semantic_mutation": not ok,
    }


def load_nq_signal_bars() -> dict[str, Any]:
    """Preferred research/reference bars: Databento stitched NQ."""
    root = DATA_ROOT / "stitched"
    b1 = load_dataset("databento_NQ_stitched", "1m", root=root)
    b5 = load_dataset("databento_NQ_stitched", "5m", root=root)
    b15 = load_dataset("databento_NQ_stitched", "15m", root=root)
    bars_1m = list(b1.get("bars") or [])
    bars_5m = list(b5.get("bars") or [])
    bars_15m = list(b15.get("bars") or [])
    if not bars_1m:
        return {"ok": False, "error_code": "SIGNAL_DATA_UNAVAILABLE", "source": None}
    if not bars_5m:
        bars_5m = aggregate_1m_to_ny(bars_1m, 5)
    if not bars_15m:
        bars_15m = aggregate_1m_to_ny(bars_1m, 15)
    return {
        "ok": True,
        "source": "databento:GLBX.MDP3:NQ_stitched",
        "bars_1m": bars_1m,
        "bars_5m": bars_5m,
        "bars_15m": bars_15m,
        "note": "Historical/reference NQ bars — not a substitute for live forward validation feed",
    }


def _daily_bucket(state: dict[str, Any], trading_date: str) -> dict[str, Any]:
    daily = state.setdefault("daily", {})
    if trading_date not in daily:
        daily[trading_date] = {"trades": 0, "losses": 0}
    return daily[trading_date]


def build_status(*, enable_sim: bool = False) -> dict[str, Any]:
    freeze = assert_frozen_immutable()
    st = load_state()
    data = load_nq_signal_bars()
    snap = None
    trading_date = datetime.now(tz=NY).date().isoformat()
    if data.get("ok"):
        dates = trading_dates_ny(data["bars_5m"])
        trading_date = dates[-1] if dates else trading_date
        by1 = index_bars_by_ny_date(data["bars_1m"])
        by5 = index_bars_by_ny_date(data["bars_5m"])
        by15 = index_bars_by_ny_date(data["bars_15m"])
        bucket = _daily_bucket(st, trading_date)
        snap = evaluate_completed_bars(
            bars_1m=by1.get(trading_date, []),
            bars_5m=by5.get(trading_date, []),
            bars_15m=by15.get(trading_date, []),
            trading_date=trading_date,
            daily_trades=int(bucket.get("trades") or 0),
            daily_losses=int(bucket.get("losses") or 0),
            seen_triggers=set(st.get("seen_triggers") or []),
        )
    pos = nt.parse_mnq_sim_position()
    open_trade = st.get("open_trade")
    paused = is_execution_paused()
    sim_allowed = bool(enable_sim) and not is_halted(st) and not paused
    return {
        "ok": True,
        "execution_status": "PAUSED" if paused else ("SIM_ONLY" if sim_allowed else "DRY_RUN"),
        "mode": "SIM_EXECUTION" if sim_allowed else "DRY_RUN",
        "execution_enabled": sim_allowed,
        "project_paused": paused,
        "halted": is_halted(st),
        "pause": execution_summary(),
        "frozen_strategy_hash": freeze.get("frozen_config_hash"),
        "strategy_version": freeze.get("strategy_version"),
        "semantic_mutation": freeze.get("semantic_mutation"),
        "nq_signal_contract": "NQ (stitched Databento reference / live adapter TBD)",
        "mnq_execution_contract": EXEC_INSTRUMENT,
        "account": EXEC_ACCOUNT,
        "quantity": 1,
        "et_time": datetime.now(tz=NY).isoformat(),
        "signal_source": data.get("source"),
        "signal_ok": data.get("ok"),
        "snapshot": None if snap is None else snap.to_dict(),
        "daily_trades": (st.get("daily") or {}).get(trading_date, {}).get("trades"),
        "daily_losses": (st.get("daily") or {}).get(trading_date, {}).get("losses"),
        "open_position": pos,
        "open_trade": open_trade,
        "stop": None if not open_trade else open_trade.get("stop_price"),
        "target": None if not open_trade else open_trade.get("target_price"),
        "last_event": st.get("last_event"),
        "data_freshness_sec": None if snap is None else snap.data_freshness_sec,
        "stale_threshold_sec": STALE_5M_SECONDS,
        "bridge": "OIF_FILL_THEN_OCO_CHILDREN",
        "note": "Default DRY_RUN. Pass --enable-sim-execution to allow Sim101 OIF submission.",
    }


def run_once(*, enable_sim: bool) -> dict[str, Any]:
    """Single evaluation cycle. Never submits unless enable_sim and not halted."""
    if is_execution_paused() and enable_sim:
        return {
            "ok": False,
            "status": "PROJECT_PAUSED",
            "execution_status": "PAUSED",
            "error_code": "PROJECT_PAUSED",
            "note": "Phase 32 pause — SIM execution blocked until resume gate cleared",
        }
    freeze = assert_frozen_immutable()
    if freeze.get("semantic_mutation"):
        return {"ok": False, "error_code": "FROZEN_CONFIG_MISMATCH", "freeze": freeze}

    st = load_state()
    if is_halted(st):
        out = {"ok": True, "status": "HALTED", "mode": "DRY_RUN"}
        return out

    data = load_nq_signal_bars()
    if not data.get("ok"):
        append_jsonl(
            LIVE_EVENTS,
            {"ts": datetime.now(tz=NY).isoformat(), "event": "SIGNAL_DRY_RUN", "reason": data.get("error_code")},
        )
        return {
            "ok": True,
            "status": "SIGNAL_DRY_RUN",
            "reason": data.get("error_code"),
            "execution_enabled": False,
            "note": "No NQ signal bars — journal only, no orders",
        }

    dates = trading_dates_ny(data["bars_5m"])
    trading_date = dates[-1]
    by1 = index_bars_by_ny_date(data["bars_1m"])
    by5 = index_bars_by_ny_date(data["bars_5m"])
    by15 = index_bars_by_ny_date(data["bars_15m"])
    bucket = _daily_bucket(st, trading_date)
    seen = set(st.get("seen_triggers") or [])

    snap = evaluate_completed_bars(
        bars_1m=by1.get(trading_date, []),
        bars_5m=by5.get(trading_date, []),
        bars_15m=by15.get(trading_date, []),
        trading_date=trading_date,
        daily_trades=int(bucket.get("trades") or 0),
        daily_losses=int(bucket.get("losses") or 0),
        seen_triggers=seen,
    )
    st["state"] = snap.state
    st["last_event"] = {
        "ts": datetime.now(tz=NY).isoformat(),
        "state": snap.state,
        "drift": snap.drift,
        "intended": snap.intended_order,
    }
    append_jsonl(
        LIVE_EVENTS,
        {
            "ts": datetime.now(tz=NY).isoformat(),
            "event": "STATE",
            "trading_date": trading_date,
            "state": snap.state,
            "drift": snap.drift,
            "vwap": snap.vwap,
            "hour_return": snap.hour_return,
            "blocked": snap.blocked_reason,
            "intended": snap.intended_order,
            "mode": "SIM_EXECUTION" if enable_sim else "DRY_RUN",
        },
    )

    intended = snap.intended_order
    result: dict[str, Any] = {
        "ok": True,
        "mode": "SIM_EXECUTION" if enable_sim else "DRY_RUN",
        "snapshot": snap.to_dict(),
        "freeze": freeze,
        "submitted": False,
    }

    if not intended:
        save_state(st)
        return result

    trade_id = intended["trade_id"]
    trigger_key = intended["trigger_key"]
    if trigger_key in seen:
        result["status"] = "DUPLICATE_TRIGGER_BLOCKED"
        save_state(st)
        return result

    if st.get("open_trade"):
        result["status"] = "POSITION_STATE_UNSAFE"
        result["error_code"] = "one_position_at_a_time"
        save_state(st)
        return result

    stop_pts, tgt_pts = frozen_risk_for_direction(intended["direction"])
    plan = plan_dvp_entry(
        direction=intended["direction"],
        trade_id=trade_id,
        stop_points=stop_pts,
        target_points=tgt_pts,
    )
    result["planned_order"] = plan

    if not enable_sim:
        append_jsonl(
            LIVE_EVENTS,
            {
                "ts": datetime.now(tz=NY).isoformat(),
                "event": "INTENDED_ORDER_DRY_RUN",
                "trade_id": trade_id,
                "plan": plan,
                "signal_entry_price": intended.get("signal_entry_price"),
            },
        )
        # Mark trigger seen in dry-run so repeated --once does not spam; optional — user may want re-print
        # Do NOT mark seen in dry-run so status can re-show; only mark on submit
        result["status"] = "DRY_RUN_ORDER_PRINTED"
        save_state(st)
        return result

    # Live sim path
    pos = nt.parse_mnq_sim_position()
    if pos.get("flat") is not True:
        result["ok"] = False
        result["error_code"] = "POSITION_STATE_UNSAFE"
        result["position"] = pos
        st["state"] = "ERROR_SAFE_HALT"
        save_state(st)
        return result

    exec_out = submit_dvp_bracket(
        direction=intended["direction"],
        trade_id=trade_id,
        stop_points=stop_pts,
        target_points=tgt_pts,
        submit=True,
    )
    result["execution"] = exec_out
    result["submitted"] = bool(exec_out.get("submitted"))
    if exec_out.get("ok") and exec_out.get("status") == "BRACKET_ARMED":
        seen.add(trigger_key)
        st["seen_triggers"] = sorted(seen)[-500:]
        bucket["trades"] = int(bucket.get("trades") or 0) + 1
        st["open_trade"] = {
            "trade_id": trade_id,
            "direction": intended["direction"],
            "entry_fill": exec_out.get("entry_fill"),
            "stop_price": exec_out.get("stop_price"),
            "target_price": exec_out.get("target_price"),
            "oco_id": exec_out.get("oco_id"),
            "entry_order_id": exec_out.get("entry_order_id"),
            "stop_order_id": exec_out.get("stop_order_id"),
            "target_order_id": exec_out.get("target_order_id"),
            "nt_entry_order_id": exec_out.get("nt_entry_order_id"),
            "trigger_key": trigger_key,
            "frozen_config_hash": freeze.get("frozen_config_hash"),
        }
        st["state"] = "POSITION_OPEN"
        append_jsonl(
            EXECUTIONS,
            {
                "trade_id": trade_id,
                "frozen_config_hash": freeze.get("frozen_config_hash"),
                "nq_signal_contract": "NQ",
                "mnq_execution_contract": EXEC_INSTRUMENT,
                "direction": intended["direction"],
                "signal_timestamp": intended.get("entry_timestamp"),
                "trigger_candle": intended.get("trigger_ts"),
                "entry_fill": exec_out.get("entry_fill"),
                "stop": exec_out.get("stop_price"),
                "target": exec_out.get("target_price"),
                "daily_trade_number": bucket["trades"],
                "daily_loss_count": bucket.get("losses"),
                "oco_id": exec_out.get("oco_id"),
                "status": "BRACKET_ARMED",
            },
        )
        append_jsonl(
            LIVE_EVENTS,
            {
                "ts": datetime.now(tz=NY).isoformat(),
                "event": "ENTRY_SUBMITTED_ARMED",
                "trade_id": trade_id,
                "execution": {
                    k: exec_out.get(k)
                    for k in (
                        "entry_fill",
                        "stop_price",
                        "target_price",
                        "oco_id",
                        "status",
                    )
                },
            },
        )
        result["status"] = "BRACKET_ARMED"
    else:
        st["state"] = "ERROR_SAFE_HALT" if exec_out.get("status") == "MANUAL_FLATTEN_REQUIRED" else st.get("state")
        result["status"] = exec_out.get("status")
        result["ok"] = False
        append_jsonl(
            LIVE_EVENTS,
            {
                "ts": datetime.now(tz=NY).isoformat(),
                "event": "EXECUTION_FAILED",
                "trade_id": trade_id,
                "execution": exec_out,
            },
        )

    save_state(st)
    return result


def historical_live_equivalence(*, max_days: int = 5) -> dict[str, Any]:
    """Compare Phase29 replay entries vs Phase31 signal extractor on identical bars."""
    from nq_drift_vwap_engine import replay_dvp_day
    from nq_dvp_freeze import load_frozen_strategy_config

    data = load_nq_signal_bars()
    if not data.get("ok"):
        return {"ok": False, "error_code": "SIGNAL_DATA_UNAVAILABLE", "verdict": "LIVE_EQUIVALENCE_FAIL"}

    cfg = load_frozen_strategy_config()
    by1 = index_bars_by_ny_date(data["bars_1m"])
    by5 = index_bars_by_ny_date(data["bars_5m"])
    by15 = index_bars_by_ny_date(data["bars_15m"])
    dates = sorted(set(by5) & set(by15) & set(by1))[-max_days:]
    mismatches = []
    matched = 0
    compared = 0
    for td in dates:
        hist = replay_dvp_day(
            trading_date=td,
            bars_1m=by1.get(td, []),
            bars_5m=by5.get(td, []),
            bars_15m=by15.get(td, []),
            cfg=cfg,
        )
        live_entries = extract_signal_entries_for_day(
            bars_1m=by1.get(td, []),
            bars_5m=by5.get(td, []),
            bars_15m=by15.get(td, []),
            trading_date=td,
            cfg=cfg,
        )
        hist_entries = []
        for t in hist["trades"]:
            hist_entries.append(
                {
                    "direction": "LONG" if t.direction == "bullish" else "SHORT",
                    "entry_timestamp": t.entry_timestamp,
                    "entry_price": t.entry_price,
                    "stop_price": t.stop_price,
                    "target_price": t.target_price,
                    "trigger_ts": (t.extras or {}).get("pullback_5m_ts"),
                }
            )
        # Compare by entry_timestamp
        hmap = {e["entry_timestamp"]: e for e in hist_entries}
        lmap = {e["entry_timestamp"]: e for e in live_entries}
        keys = sorted(set(hmap) | set(lmap))
        for k in keys:
            compared += 1
            h = hmap.get(k)
            l = lmap.get(k)
            if h is None or l is None:
                mismatches.append({"date": td, "entry_ts": k, "reason": "missing_side", "hist": h, "live": l})
                continue
            if (
                h["direction"] != l["direction"]
                or abs(float(h["entry_price"]) - float(l["entry_price"])) > 1e-9
                or abs(float(h["stop_price"]) - float(l["stop_price"])) > 1e-9
                or abs(float(h["target_price"]) - float(l["target_price"])) > 1e-9
            ):
                mismatches.append({"date": td, "entry_ts": k, "reason": "field_mismatch", "hist": h, "live": l})
            else:
                matched += 1

    ok = len(mismatches) == 0 and compared > 0
    return {
        "ok": ok,
        "verdict": "LIVE_EQUIVALENCE_OK" if ok else "LIVE_EQUIVALENCE_FAIL",
        "days": dates,
        "compared": compared,
        "matched": matched,
        "mismatches": mismatches[:20],
        "mismatch_count": len(mismatches),
    }


def nq_mnq_equivalence_note() -> dict[str, Any]:
    return {
        "ok": False,
        "verdict": "MNQ_OVERLAP_UNAVAILABLE",
        "nq_signal": "Databento NQ stitched available",
        "mnq_data": "No MNQ stitched dataset in repo",
        "note": (
            "Phase 31 executes MNQ from NQ signal prices/fill. Stop/target use index points on MNQ fill. "
            "Material NQ↔MNQ bar mismatch not yet measured — execution QA pending MNQ overlap sample."
        ),
        "forward_validation_counts": False,
    }


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Phase 31 frozen NQ DVP → Sim101 MNQ runner")
    parser.add_argument("--enable-sim-execution", action="store_true", help="Allow Sim101 OIF submission")
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--halt", action="store_true")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--once", action="store_true", help="Single evaluation cycle (default action)")
    parser.add_argument("--equivalence", action="store_true", help="Run hist/live equivalence check")
    args = parser.parse_args(argv)

    ensure_dirs()

    if args.halt:
        print(json.dumps(set_halt(True), indent=2, default=str))
        return 0
    if args.resume:
        print(json.dumps(set_halt(False), indent=2, default=str))
        return 0
    if args.status:
        print(json.dumps(build_status(enable_sim=args.enable_sim_execution), indent=2, default=str))
        return 0
    if args.enable_sim_execution and is_execution_paused():
        print(
            json.dumps(
                {
                    "ok": False,
                    "error_code": "PROJECT_PAUSED",
                    "execution_status": "PAUSED",
                    "pause": execution_summary(),
                },
                indent=2,
            )
        )
        return 1
    if args.equivalence:
        print(json.dumps(historical_live_equivalence(), indent=2, default=str))
        return 0

    # Default: one dry-run cycle
    out = run_once(enable_sim=bool(args.enable_sim_execution))
    print(json.dumps(out, indent=2, default=str))
    return 0 if out.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
