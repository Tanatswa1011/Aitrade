"""Phase 31 — NinjaTrader Sim101 execution adapter for frozen NQ DVP (MNQ only)."""

from __future__ import annotations

import time
from typing import Any, Optional

import nt_ati as nt
from execution_instrument import EXEC_INSTRUMENT_NT
from nq_dvp_freeze import load_frozen_document, load_frozen_strategy_config

EXEC_ACCOUNT = "Sim101"
EXEC_INSTRUMENT = EXEC_INSTRUMENT_NT
SIGNAL_ROOT = "NQ"
OWNED_PREFIX = "AITRADE_DVP_"


def assert_execution_locks(*, account: str, quantity: int, instrument: str = EXEC_INSTRUMENT) -> None:
    if account != EXEC_ACCOUNT:
        raise PermissionError(f"LIVE_ACCOUNT_BLOCKED:{account}")
    if int(quantity) != 1:
        raise PermissionError(f"QUANTITY_BLOCKED:{quantity}")
    nt.assert_mnq_instrument(instrument)


def plan_dvp_entry(
    *,
    direction: str,
    trade_id: str,
    stop_points: float,
    target_points: float,
) -> dict[str, Any]:
    """Build intended OIF plan without submitting."""
    assert_execution_locks(account=EXEC_ACCOUNT, quantity=1)
    d = direction.upper()
    action = "BUY" if d == "LONG" else "SELL"
    entry_oid = f"{trade_id}_ENTRY"
    stop_oid = f"{trade_id}_STOP"
    tgt_oid = f"{trade_id}_TGT"
    oco_id = f"AITRADE_DVP_OCO_{trade_id}"
    entry_line = nt.build_place_oif(
        account=EXEC_ACCOUNT,
        instrument=EXEC_INSTRUMENT,
        action=action,
        quantity=1,
        order_type="MARKET",
        order_id=entry_oid,
    )
    return {
        "ok": True,
        "submitted": False,
        "account": EXEC_ACCOUNT,
        "instrument": EXEC_INSTRUMENT,
        "signal_root": SIGNAL_ROOT,
        "direction": d,
        "action": action,
        "quantity": 1,
        "trade_id": trade_id,
        "entry_order_id": entry_oid,
        "stop_order_id": stop_oid,
        "target_order_id": tgt_oid,
        "oco_id": oco_id,
        "stop_points": float(stop_points),
        "target_points": float(target_points),
        "entry_line": entry_line,
        "mechanism": nt.BRACKET_MECHANISM,
    }


def submit_dvp_bracket(
    *,
    direction: str,
    trade_id: str,
    stop_points: float,
    target_points: float,
    submit: bool,
    fill_timeout_sec: float = 20.0,
) -> dict[str, Any]:
    """
    Entry MARKET → fill from NT log → OCO STOPMARKET+LIMIT with frozen DVP distances.
    submit=False returns plan only (no OIF).
    """
    plan = plan_dvp_entry(
        direction=direction,
        trade_id=trade_id,
        stop_points=stop_points,
        target_points=target_points,
    )
    if not submit:
        # Illustrative children from placeholder — never used as live prices
        kids = nt.build_bracket_child_oifs(
            direction=direction,
            entry_fill=9000.0,
            oco_id=plan["oco_id"],
            stop_order_id=plan["stop_order_id"],
            target_order_id=plan["target_order_id"],
            stop_points=stop_points,
            target_points=target_points,
        )
        plan["example_children_from_9000"] = kids
        plan["status"] = "DRY_RUN_PLAN"
        return plan

    # Pre-flight position
    pos = nt.parse_mnq_sim_position()
    if pos.get("flat") is not True:
        return {
            **plan,
            "ok": False,
            "error_code": "POSITION_STATE_UNSAFE",
            "position": pos,
            "status": "POSITION_STATE_UNSAFE",
        }

    entry_drop = nt.drop_oif(plan["entry_line"])
    entry_wait = nt.wait_for_oif_consumed(entry_drop["path"], timeout_sec=8.0)
    fill = nt.wait_for_entry_fill(plan["entry_order_id"], timeout_sec=fill_timeout_sec)
    if not fill.get("ok"):
        flat = flatten_dvp_owned(plan, submit=True)
        return {
            **plan,
            "ok": False,
            "submitted": True,
            "entry_drop": entry_drop,
            "entry_wait": entry_wait,
            "fill": fill,
            "flatten": flat,
            "status": "ENTRY_FILL_TIMEOUT_FLATTENED",
        }

    entry_fill = float(fill["fill_price"])
    children = nt.build_bracket_child_oifs(
        direction=direction,
        entry_fill=entry_fill,
        oco_id=plan["oco_id"],
        stop_order_id=plan["stop_order_id"],
        target_order_id=plan["target_order_id"],
        stop_points=stop_points,
        target_points=target_points,
    )
    child_drop = nt.drop_oif_lines([children["stop_line"], children["target_line"]])
    child_wait = nt.wait_for_oif_consumed(child_drop["path"], timeout_sec=8.0)
    time.sleep(1.0)
    orphans = nt.detect_orphan_aitrade_orders(
        [plan["stop_order_id"], plan["target_order_id"]],
        oco_id=plan["oco_id"],
    )
    armed = orphans.get("oco_live_count", 0) >= 1 or orphans.get("orphan_count", 0) >= 1
    if not child_wait.get("consumed") or not armed:
        flat = flatten_dvp_owned(
            {
                **plan,
                "nt_entry_order_id": fill.get("nt_order_id"),
                "entry_fill": entry_fill,
                **children["prices"],
            },
            submit=True,
        )
        status = (
            "PROTECTION_FAILURE_FLATTENED"
            if flat.get("status") == "FLATTENED"
            else "MANUAL_FLATTEN_REQUIRED"
        )
        return {
            **plan,
            "ok": False,
            "submitted": True,
            "entry_fill": entry_fill,
            "children": children,
            "child_drop": child_drop,
            "child_wait": child_wait,
            "flatten": flat,
            "status": status,
        }

    return {
        **plan,
        "ok": True,
        "submitted": True,
        "entry_drop": entry_drop,
        "entry_wait": entry_wait,
        "entry_fill": entry_fill,
        "nt_entry_order_id": fill.get("nt_order_id"),
        "stop_price": children["prices"]["stop"],
        "target_price": children["prices"]["target"],
        "children": children,
        "child_drop": child_drop,
        "child_wait": child_wait,
        "working_exits": orphans,
        "status": "BRACKET_ARMED",
        "frozen_config_hash": load_frozen_document().get("frozen_config_hash"),
    }


def flatten_dvp_owned(active: dict[str, Any], *, submit: bool) -> dict[str, Any]:
    """Cancel only DVP-owned order IDs then CLOSEPOSITION Sim101 MNQ."""
    assert_execution_locks(account=EXEC_ACCOUNT, quantity=1)
    cancel_ids = [
        x
        for x in (
            active.get("nt_entry_order_id"),
            active.get("nt_stop_order_id"),
            active.get("nt_target_order_id"),
            active.get("entry_order_id"),
            active.get("stop_order_id"),
            active.get("target_order_id"),
        )
        if x and (str(x).startswith(OWNED_PREFIX) or str(x).startswith("AITRADE_DVP") or len(str(x)) == 32)
    ]
    # Always allow CLOSEPOSITION for Sim101 MNQ as last resort for DVP-owned position
    lines = [nt.build_cancel_oif(oid) for oid in cancel_ids]
    lines.append(nt.build_close_position_oif())
    out: dict[str, Any] = {
        "ok": True,
        "submitted": False,
        "cancel_order_ids": cancel_ids,
        "oif_lines": lines,
        "status": "FLATTEN_PLANNED",
    }
    if not submit:
        return out
    drop = nt.drop_oif_lines(lines)
    wait = nt.wait_for_oif_consumed(drop["path"], timeout_sec=8.0)
    time.sleep(0.8)
    pos = nt.parse_mnq_sim_position()
    orphans = nt.detect_orphan_aitrade_orders(cancel_ids, oco_id=active.get("oco_id"))
    flat_ok = bool(pos.get("flat"))
    orphan_ok = orphans.get("orphan_count", 0) == 0
    status = "FLATTENED" if flat_ok and orphan_ok else (
        "MANUAL_FLATTEN_REQUIRED" if not flat_ok else "OCO_FAILURE"
    )
    out.update(
        {
            "drop": drop,
            "wait": wait,
            "submitted": True,
            "position_after": pos,
            "orphans": orphans,
            "status": status,
            "ok": status == "FLATTENED",
        }
    )
    return out


def frozen_risk_for_direction(direction: str) -> tuple[float, float]:
    cfg = load_frozen_strategy_config()
    d = direction.upper()
    if d == "LONG":
        return float(cfg.long_stop_points), float(cfg.long_target_points)
    return float(cfg.short_stop_points), float(cfg.short_target_points)
