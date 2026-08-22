"""FundedNext prop-canary NT ATI/OIF helpers.

Separate from Sim101 ``nt_ati`` / ``nq_dvp_nt_exec``. Never defaults to Sim101.
Does not call ``assert_sim101``. MCP cannot submit orders; the broker route is
NinjaTrader ATI PLACE into the exact FundedNext NT account name.
"""
from __future__ import annotations

from typing import Any, Optional

from execution_instrument import EXEC_INSTRUMENT_DISPLAY, EXEC_INSTRUMENT_NT, parse_execution_instrument
from nt_ati import ALLOWED_ACTIONS, MNQ_TICK_SIZE, round_to_tick, validate_tick_aligned

CANARY_NT_ACCOUNT = "FNFTCHTANATSWAPHILMU92044"
SIM101_ACCOUNT = "Sim101"
CANARY_QTY = 1
SIGNAL_INSTRUMENT = "NQ 09-26"
OWNED_PREFIX = "AITRADE_FN_CANARY_"


def assert_canary_account(account: str) -> None:
    name = (account or "").strip()
    if not name:
        raise PermissionError("ACCOUNT_IDENTITY_MISSING")
    if name == SIM101_ACCOUNT or name.lower().startswith("sim"):
        raise PermissionError("SIM101_BLOCKED_FROM_CANARY")
    if name != CANARY_NT_ACCOUNT:
        raise PermissionError(f"WRONG_ACCOUNT:{name}")


def assert_canary_qty(quantity: int) -> None:
    if int(quantity) != CANARY_QTY:
        raise PermissionError(f"CANARY_QTY_REJECTED:{quantity}")


def assert_canary_exec_instrument(instrument: str) -> None:
    inst = parse_execution_instrument(instrument)
    if inst.ninjatrader_oif() != EXEC_INSTRUMENT_NT:
        raise PermissionError(f"REFUSED_UNSUPPORTED_INSTRUMENT:{instrument}")


def assert_canary_signal_instrument(instrument: str) -> None:
    if (instrument or "").strip() != SIGNAL_INSTRUMENT:
        raise PermissionError(f"SIGNAL_INSTRUMENT_BLOCKED:{instrument}")


def assert_canary_locks(*, account: str, quantity: int, instrument: str = EXEC_INSTRUMENT_NT) -> None:
    assert_canary_account(account)
    assert_canary_qty(quantity)
    assert_canary_exec_instrument(instrument)


def parse_oif_account(line: str) -> str:
    parts = (line or "").split(";")
    if len(parts) < 3:
        return ""
    cmd = parts[0].strip().upper()
    if cmd in {"PLACE", "CLOSEPOSITION"}:
        return parts[1].strip()
    return ""


def parse_oif_qty(line: str) -> Optional[int]:
    parts = (line or "").split(";")
    if len(parts) < 5 or parts[0].strip().upper() != "PLACE":
        return None
    try:
        return int(parts[4])
    except (TypeError, ValueError):
        return None


def build_canary_place_oif(
    *,
    account: str,
    instrument: str = EXEC_INSTRUMENT_NT,
    action: str,
    quantity: int = CANARY_QTY,
    order_type: str = "MARKET",
    limit_price: float = 0.0,
    stop_price: float = 0.0,
    tif: str = "DAY",
    oco_id: str = "",
    order_id: str = "",
    strategy: str = "NQ_DRIFT_VWAP_PULLBACK",
    strategy_id: str = "",
) -> str:
    """PLACE line for the FundedNext canary account only."""
    assert_canary_locks(account=account, quantity=quantity, instrument=instrument)
    act = action.upper().strip()
    if act not in ALLOWED_ACTIONS:
        raise ValueError(f"action_must_be_BUY_or_SELL:{action}")
    otype = order_type.upper().strip()
    lim = 0 if limit_price in (None, "") else float(limit_price)
    stp = 0 if stop_price in (None, "") else float(stop_price)
    if otype in ("LIMIT", "STOPLIMIT") and lim != 0 and not validate_tick_aligned(lim):
        raise ValueError(f"limit_not_tick_aligned:{lim}")
    if otype in ("STOPMARKET", "STOPLIMIT") and stp != 0 and not validate_tick_aligned(stp):
        raise ValueError(f"stop_not_tick_aligned:{stp}")
    lim_s = "0" if float(lim) == 0.0 else str(lim)
    stp_s = "0" if float(stp) == 0.0 else str(stp)
    oid = order_id or "AITRADE_FN_CANARY_ENTRY"
    return (
        f"PLACE;{CANARY_NT_ACCOUNT};{EXEC_INSTRUMENT_NT};{act};{CANARY_QTY};{otype};"
        f"{lim_s};{stp_s};{tif.upper()};{oco_id};{oid};{strategy};{strategy_id}"
    )


def build_canary_close_oif(*, account: str, instrument: str = EXEC_INSTRUMENT_NT) -> str:
    assert_canary_locks(account=account, quantity=CANARY_QTY, instrument=instrument)
    return f"CLOSEPOSITION;{CANARY_NT_ACCOUNT};{EXEC_INSTRUMENT_NT};;;;;;;;;;"


def plan_canary_bracket(
    *,
    direction: str,
    trade_id: str,
    stop_points: float,
    target_points: float,
    account: str = CANARY_NT_ACCOUNT,
) -> dict[str, Any]:
    assert_canary_locks(account=account, quantity=CANARY_QTY)
    d = direction.upper()
    action = "BUY" if d == "LONG" else "SELL"
    exit_action = "SELL" if d == "LONG" else "BUY"
    entry_oid = f"{trade_id}_ENTRY"
    stop_oid = f"{trade_id}_STOP"
    tgt_oid = f"{trade_id}_TGT"
    oco_id = f"{OWNED_PREFIX}OCO_{trade_id}"
    entry_line = build_canary_place_oif(
        account=account,
        action=action,
        quantity=CANARY_QTY,
        order_type="MARKET",
        order_id=entry_oid,
        strategy_id=trade_id,
    )
    return {
        "ok": True,
        "submitted": False,
        "account": CANARY_NT_ACCOUNT,
        "instrument_nt": EXEC_INSTRUMENT_NT,
        "instrument_display": EXEC_INSTRUMENT_DISPLAY,
        "signal_instrument": SIGNAL_INSTRUMENT,
        "direction": d,
        "action": action,
        "exit_action": exit_action,
        "quantity": CANARY_QTY,
        "trade_id": trade_id,
        "entry_order_id": entry_oid,
        "stop_order_id": stop_oid,
        "target_order_id": tgt_oid,
        "oco_id": oco_id,
        "stop_points": float(stop_points),
        "target_points": float(target_points),
        "entry_line": entry_line,
        "tick": MNQ_TICK_SIZE,
        "mechanism": "OIF_FILL_THEN_OCO_CHILDREN",
        "route": "NINJATRADER_ATI_FUNDEDNEXT_CANARY",
        "PROP_EXECUTION": False,
        "sim101": False,
    }


def child_oifs_from_fill(plan: dict[str, Any], entry_fill: float) -> dict[str, Any]:
    """Protective STOPMARKET + LIMIT on the same FundedNext account / qty / MNQ."""
    d = str(plan.get("direction") or "").upper()
    fill = round_to_tick(float(entry_fill))
    stop_pts = float(plan["stop_points"])
    tgt_pts = float(plan["target_points"])
    if d == "LONG":
        stop_px = round_to_tick(fill - stop_pts)
        tgt_px = round_to_tick(fill + tgt_pts)
    else:
        stop_px = round_to_tick(fill + stop_pts)
        tgt_px = round_to_tick(fill - tgt_pts)
    stop_line = build_canary_place_oif(
        account=CANARY_NT_ACCOUNT,
        action=str(plan["exit_action"]),
        quantity=CANARY_QTY,
        order_type="STOPMARKET",
        stop_price=stop_px,
        oco_id=str(plan["oco_id"]),
        order_id=str(plan["stop_order_id"]),
        strategy_id=str(plan["trade_id"]),
    )
    tgt_line = build_canary_place_oif(
        account=CANARY_NT_ACCOUNT,
        action=str(plan["exit_action"]),
        quantity=CANARY_QTY,
        order_type="LIMIT",
        limit_price=tgt_px,
        oco_id=str(plan["oco_id"]),
        order_id=str(plan["target_order_id"]),
        strategy_id=str(plan["trade_id"]),
    )
    return {
        "stop_line": stop_line,
        "target_line": tgt_line,
        "stop_price": stop_px,
        "target_price": tgt_px,
        "entry_fill": fill,
        "stop_qty": CANARY_QTY,
        "stop_account": CANARY_NT_ACCOUNT,
        "stop_instrument": EXEC_INSTRUMENT_NT,
    }


def validate_canary_oif_line(line: str) -> None:
    """Last check immediately before any broker write."""
    acct = parse_oif_account(line)
    assert_canary_account(acct)
    if SIM101_ACCOUNT in (line or ""):
        raise PermissionError("SIM101_BLOCKED_FROM_CANARY")
    qty = parse_oif_qty(line)
    if line.startswith("PLACE;") and qty != CANARY_QTY:
        raise PermissionError(f"CANARY_QTY_REJECTED:{qty}")
    if "NQ " in line and "MNQ " not in line:
        raise PermissionError("REFUSED_FULL_SIZE_NQ")
    if "MNQ SEP26" not in line:
        raise PermissionError("REFUSED_UNSUPPORTED_INSTRUMENT")


def drop_canary_oif_lines(
    lines: list[str],
    *,
    transmit: bool,
    nt_root: Optional[Any] = None,
) -> dict[str, Any]:
    """Broker-boundary drop. Default is dry-run (no incoming write)."""
    from pathlib import Path

    cleaned = [str(x).strip() for x in (lines or []) if str(x).strip()]
    for line in cleaned:
        validate_canary_oif_line(line)
    payload = {
        "ok": True,
        "submitted": False,
        "transmitted": False,
        "account": CANARY_NT_ACCOUNT,
        "instrument_nt": EXEC_INSTRUMENT_NT,
        "quantity": CANARY_QTY,
        "lines": cleaned,
        "route": "NINJATRADER_ATI_FUNDEDNEXT_CANARY",
        "PROP_EXECUTION": False,
    }
    if not transmit:
        payload["status"] = "PROP_CANARY_DRY_RUN"
        return payload
    from nt_ati import drop_oif_lines

    dropped = drop_oif_lines(cleaned, nt_root=Path(nt_root) if nt_root else None, prefix="oif_fn_canary")
    payload.update(dropped)
    payload["submitted"] = True
    payload["transmitted"] = True
    payload["status"] = "SUBMITTED"
    return payload
