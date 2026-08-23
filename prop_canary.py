"""Phase 55C — one-shot FundedNext Flex 50K MNQ canary.

Separate from Sim101 SIM_ONLY and from general PROP_EXECUTION.
Default DISARMED. ARMED lives only in process memory. Never transmits unless
``submit_once(..., transmit=True)`` is called after an explicit operator arm.

MCP remains the authoritative money/risk source. Order submission is NinjaTrader
ATI OIF to the exact NT account name. MCP cannot place orders.
"""
from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from execution_instrument import EXEC_INSTRUMENT_DISPLAY, EXEC_INSTRUMENT_NT
from execution_status import NQ_FROZEN_HASH
from nq_dvp_nt_exec import frozen_risk_for_direction
from phase52_policy import MAX_LOSS, UNIT_RISK_USD
from prop_canary_nt_exec import (
    CANARY_NT_ACCOUNT,
    CANARY_QTY,
    SIGNAL_INSTRUMENT,
    SIM101_ACCOUNT,
    assert_canary_account,
    assert_canary_exec_instrument,
    assert_canary_qty,
    assert_canary_signal_instrument,
    build_canary_close_oif,
    child_oifs_from_fill,
    drop_canary_oif_lines,
    parse_oif_account,
    plan_canary_bracket,
    validate_canary_oif_line,
)

ROOT = Path(__file__).resolve().parent
ENV_FLAG = "AITRADE_PROP_CANARY_EXECUTION"
ENV_STATE = "AITRADE_PROP_CANARY_STATE"
DEFAULT_STATE_PATH = ROOT / "state" / "prop_canary.json"

CANARY_LOGIN = "962841277"
CANARY_ACCOUNT_ID = 3969349
CANARY_PLAN = "Futures Flex Challenge | 50K"
STALE_ACCOUNT_SEC = 60.0
TZ_REQUIRED = "America/New_York"
LIVE_PROVENANCE = "phase54_live"

PROP_LOCKED = "PROP_LOCKED"
PROP_CANARY_READY = "PROP_CANARY_READY"
PROP_CANARY_ARMED = "PROP_CANARY_ARMED"
PROP_CANARY_IN_FLIGHT = "PROP_CANARY_IN_FLIGHT"
PROP_CANARY_COMPLETE = "PROP_CANARY_COMPLETE"
PROP_CANARY_BLOCKED = "PROP_CANARY_BLOCKED"
PROP_CANARY_DISARMED = "PROP_CANARY_DISARMED"
PROP_FLAT_SAFE = "PROP_FLAT_SAFE"

_TRUTHY = {"1", "true", "yes", "on"}
TransmitFn = Callable[..., dict[str, Any]]

_MEM: dict[str, Any] = {}


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(ts: Optional[datetime] = None) -> str:
    return (ts or _utc_now()).isoformat()


def _truthy_env(name: str) -> bool:
    raw = os.environ.get(name)
    if raw is None or str(raw).strip() == "":
        return False
    return str(raw).strip().lower() in _TRUTHY


def canary_flag_enabled() -> bool:
    """Dedicated canary control. Missing/unset/false = DISARMED. Independent of PROP_EXECUTION."""
    return _truthy_env(ENV_FLAG)


def _state_path() -> Path:
    from test_workspace import mutable_path, test_mode, test_root
    if test_mode():
        if not os.environ.get("AITRADE_TEST_ROOT"):
            raise RuntimeError("authoritative_test_root_required")
        root = test_root()
        override = os.environ.get(ENV_STATE)
        if override:
            path = Path(override).resolve()
            if path != root and root not in path.parents:
                raise RuntimeError(f"test_path_escaped_workspace:{path}")
            return path
        return mutable_path("state", "prop_canary.json")
    # Production ignores arbitrary environment path overrides.
    return DEFAULT_STATE_PATH


def _default_mem() -> dict[str, Any]:
    return {
        "boot_id": uuid.uuid4().hex,
        "armed_at": None,
        "armed_event_floor": None,
        "in_flight": False,
        "one_shot_consumed": False,
        "complete": False,
        "blocked": False,
        "blocked_reason": None,
        "critical": False,
        "last_disarm_reason": None,
        "last_error": None,
        "last_trade_id": None,
        "last_signal_id": None,
        "last_payload": None,
        "notify_failures": 0,
    }


def _default_persist() -> dict[str, Any]:
    return {
        "schema": "AITRADE_PROP_CANARY_V1",
        "PROP_EXECUTION": False,
        "general_prop_execution": False,
        "one_shot_consumed": False,
        "was_in_flight": False,
        "last_persisted_mode": PROP_CANARY_DISARMED,
        "last_result": None,
        "last_error": None,
        "updated_at": _iso(),
    }


def _load_persist() -> dict[str, Any]:
    path = _state_path()
    if not path.exists():
        return _default_persist()
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return _default_persist()
    if not isinstance(doc, dict):
        return _default_persist()
    out = _default_persist()
    out.update(doc)
    out["PROP_EXECUTION"] = False
    out["general_prop_execution"] = False
    mode = str(out.get("last_persisted_mode") or "")
    if mode in {PROP_CANARY_ARMED, PROP_CANARY_IN_FLIGHT, PROP_CANARY_READY}:
        out["last_persisted_mode"] = PROP_CANARY_DISARMED
    return out


def _save_persist(**over: Any) -> dict[str, Any]:
    doc = _load_persist()
    doc.update(over)
    doc["PROP_EXECUTION"] = False
    doc["general_prop_execution"] = False
    mode = str(doc.get("last_persisted_mode") or PROP_CANARY_DISARMED)
    if mode in {PROP_CANARY_ARMED, PROP_CANARY_IN_FLIGHT}:
        doc["last_persisted_mode"] = PROP_CANARY_DISARMED
        doc["was_in_flight"] = doc.get("was_in_flight") or mode == PROP_CANARY_IN_FLIGHT
    doc["updated_at"] = _iso()
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(doc, indent=2, default=str), encoding="utf-8")
    tmp.replace(path)
    return doc


def _ensure_mem() -> dict[str, Any]:
    global _MEM
    if not _MEM:
        _MEM = _default_mem()
        persist = _load_persist()
        if persist.get("one_shot_consumed"):
            _MEM["one_shot_consumed"] = True
            _MEM["complete"] = True
        if persist.get("was_in_flight"):
            _MEM["blocked"] = True
            _MEM["blocked_reason"] = "RESTART_WHILE_IN_FLIGHT"
            _MEM["critical"] = True
    return _MEM


def reset_for_tests(*, clear_persist: bool = True) -> None:
    global _MEM
    _MEM = _default_mem()
    if clear_persist:
        path = _state_path()
        if path.exists():
            try:
                path.unlink()
            except OSError:
                pass


def canary_in_flight() -> bool:
    return bool(_ensure_mem().get("in_flight"))


def canary_is_armed() -> bool:
    return bool(_ensure_mem().get("armed_at"))


def simulate_process_restart() -> None:
    """Drop in-memory ARM. Do not restore ARMED. In-flight persist becomes BLOCKED."""
    global _MEM
    persist = _load_persist()
    _MEM = _default_mem()
    if persist.get("one_shot_consumed"):
        _MEM["one_shot_consumed"] = True
        _MEM["complete"] = True
    if persist.get("was_in_flight"):
        _MEM["blocked"] = True
        _MEM["blocked_reason"] = "RESTART_WHILE_IN_FLIGHT"
        _MEM["critical"] = True


def _parse_ts(raw: Any) -> Optional[datetime]:
    if raw is None:
        return None
    if isinstance(raw, datetime):
        ts = raw
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return ts
    s = str(raw).strip()
    if not s:
        return None
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def _notify_safe(kind: str, **payload: Any) -> None:
    mem = _ensure_mem()
    try:
        from aitrade_notifications import EventType, notify_safe

        mapping = {
            PROP_CANARY_READY: EventType.PROP_CANARY_READY,
            PROP_CANARY_ARMED: EventType.PROP_CANARY_ARMED,
            PROP_CANARY_DISARMED: EventType.PROP_CANARY_DISARMED,
            PROP_CANARY_BLOCKED: EventType.PROP_CANARY_BLOCKED,
            "ORDER_SUBMITTED": EventType.ORDER_SUBMITTED,
            "ORDER_ACCEPTED": EventType.ORDER_ACCEPTED,
            "ORDER_REJECTED": EventType.ORDER_REJECTED,
            "POSITION_OPENED": EventType.POSITION_OPENED,
            "STOP_ACTIVE": EventType.STOP_ACTIVE,
            "TARGET_ACTIVE": EventType.TARGET_ACTIVE,
            "POSITION_CLOSED": EventType.POSITION_CLOSED,
            "EXECUTION_FAILURE": EventType.EXECUTION_FAILURE,
            "EMERGENCY_FLATTEN": EventType.EMERGENCY_FLATTEN,
        }
        et = mapping.get(kind)
        if et is None:
            return
        title = str(payload.pop("title", kind.replace("_", " ")))
        body = str(payload.pop("body", kind))
        notify_safe(et, title=title, body=body, account=CANARY_NT_ACCOUNT, **payload)
    except Exception:
        mem["notify_failures"] = int(mem.get("notify_failures") or 0) + 1


@dataclass
class CanaryContext:
    nt_account: Optional[str] = CANARY_NT_ACCOUNT
    platform_login: Optional[str] = CANARY_LOGIN
    fundednext_account_id: Optional[Any] = CANARY_ACCOUNT_ID
    extra_nt_fn_accounts: tuple[str, ...] = ()
    extra_mcp_ids: tuple[Any, ...] = ()
    config_expected_account: Optional[str] = CANARY_NT_ACCOUNT
    connected: bool = False
    trade_enabled: bool = False
    account_age_sec: Optional[float] = None
    equity: Optional[float] = None
    balance: Optional[float] = None
    mll: Optional[float] = None
    remaining_dd: Optional[float] = None
    breached: bool = False
    account_status: str = "ACTIVE"
    position_known: bool = False
    position_side: str = "FLAT"
    position_qty: int = 0
    working_orders: int = 0
    order_state_errors: tuple[str, ...] = ()
    recon_status: str = "UNKNOWN"
    policy_verdict: str = "BLOCK"
    calendar_status: str = "OK"
    news_blocked: bool = False
    session_permitted: bool = True
    consistency_lockout: bool = False
    phase_55b_0_pass: bool = False
    market_live: bool = False
    nq_1m_advancing: bool = False
    agg_5m_healthy: bool = False
    agg_15m_healthy: bool = False
    warmup_complete: bool = False
    parser_ok: bool = True
    timezone: str = TZ_REQUIRED
    nt_connected: bool = True
    market_stale: bool = False
    notifications_healthy: bool = True
    flatten_path_available: bool = True
    kill_path_available: bool = True
    signal: Optional[dict[str, Any]] = None
    requested_qty: int = CANARY_QTY
    requested_account: str = CANARY_NT_ACCOUNT
    requested_exec_instrument: str = EXEC_INSTRUMENT_NT
    requested_signal_instrument: str = SIGNAL_INSTRUMENT
    prop_execution: bool = False
    sim_only_armed: bool = False
    now: Optional[datetime] = None
    require_alerts: bool = True


def passing_context(**over: Any) -> CanaryContext:
    """Structural fixture: every pre-arm gate true. Not a live-market claim."""
    ctx = CanaryContext(
        connected=True,
        trade_enabled=True,
        account_age_sec=1.0,
        equity=49950.0,
        balance=49950.0,
        mll=48500.0,
        remaining_dd=162.0,
        breached=False,
        account_status="ACTIVE",
        position_known=True,
        position_side="FLAT",
        position_qty=0,
        working_orders=0,
        recon_status=PROP_FLAT_SAFE,
        policy_verdict="ALLOW",
        calendar_status="OK",
        news_blocked=False,
        session_permitted=True,
        phase_55b_0_pass=True,
        market_live=True,
        nq_1m_advancing=True,
        agg_5m_healthy=True,
        agg_15m_healthy=True,
        warmup_complete=True,
        parser_ok=True,
        timezone=TZ_REQUIRED,
        nt_connected=True,
        market_stale=False,
        notifications_healthy=True,
        flatten_path_available=True,
        kill_path_available=True,
    )
    return replace(ctx, **over) if over else ctx


def genuine_signal(**over: Any) -> dict[str, Any]:
    sig = {
        "source": LIVE_PROVENANCE,
        "live_bar": True,
        "executable": True,
        "direction": "LONG",
        "ts": _iso(),
        "signal_id": "phase54_live_" + uuid.uuid4().hex[:12],
        "strategy_hash": NQ_FROZEN_HASH,
        "kind": "DVP",
        "intended_entry": 24800.0,
        "trading_date": "2026-08-24",
    }
    sig.update(over)
    return sig


def _is_genuine(sig: Optional[dict[str, Any]]) -> bool:
    try:
        from aitrade_notifications import is_genuine_live_dvp

        return bool(is_genuine_live_dvp(sig))
    except Exception:
        if not isinstance(sig, dict) or not sig:
            return False
        if str(sig.get("source") or "") != LIVE_PROVENANCE:
            return False
        if sig.get("live_bar") is False or sig.get("executable") is False:
            return False
        return bool(sig.get("direction"))


def evaluate_identity(ctx: CanaryContext) -> dict[str, Any]:
    errors: list[str] = []
    expected = (ctx.config_expected_account or "").strip()
    if not expected or expected.upper() in {"AUTO", "AUTO_FUNDEDNEXT", "*"}:
        errors.append("ACCOUNT_IDENTITY_AMBIGUOUS")
    nt = (ctx.nt_account or "").strip()
    login = str(ctx.platform_login or "").strip()
    try:
        aid = int(ctx.fundednext_account_id) if ctx.fundednext_account_id not in (None, "") else None
    except (TypeError, ValueError):
        aid = None
        errors.append("ACCOUNT_IDENTITY_MISSING")
    if not nt or not login or aid is None:
        errors.append("ACCOUNT_IDENTITY_MISSING")
    if nt == SIM101_ACCOUNT or nt.lower().startswith("sim"):
        errors.append("SIM101_BLOCKED_FROM_CANARY")
    if nt and nt != CANARY_NT_ACCOUNT:
        errors.append("WRONG_ACCOUNT")
    if login and login != CANARY_LOGIN:
        errors.append("WRONG_ACCOUNT")
    if aid is not None and aid != CANARY_ACCOUNT_ID:
        errors.append("WRONG_ACCOUNT")
    extras = [a for a in ctx.extra_nt_fn_accounts if str(a).strip() and str(a).strip() != CANARY_NT_ACCOUNT]
    extra_ids = [i for i in ctx.extra_mcp_ids if i not in (None, "", CANARY_ACCOUNT_ID, str(CANARY_ACCOUNT_ID))]
    if extras or extra_ids:
        errors.append("SECOND_FUNDEDNEXT_ACCOUNT_BLOCKED")
    if expected and expected not in {"AUTO", "AUTO_FUNDEDNEXT"} and expected != CANARY_NT_ACCOUNT:
        errors.append("CONFIG_ALLOWLIST_MISMATCH")
    return {
        "ok": not errors,
        "errors": sorted(set(errors)),
        "nt_account": CANARY_NT_ACCOUNT if not errors else nt,
        "platform_login": CANARY_LOGIN if not errors else login,
        "fundednext_account_id": CANARY_ACCOUNT_ID if not errors else aid,
    }


def evaluate_qty_instrument(ctx: CanaryContext) -> dict[str, Any]:
    errors: list[str] = []
    try:
        assert_canary_qty(ctx.requested_qty)
    except PermissionError as exc:
        errors.append(str(exc).split(":")[0] if ":" in str(exc) else str(exc))
        if int(ctx.requested_qty) != CANARY_QTY:
            errors.append("CANARY_QTY_REJECTED")
    try:
        assert_canary_exec_instrument(ctx.requested_exec_instrument)
    except (PermissionError, ValueError) as exc:
        msg = str(exc)
        if "FULL_SIZE_NQ" in msg or (str(ctx.requested_exec_instrument).upper().startswith("NQ") and not str(ctx.requested_exec_instrument).upper().startswith("MNQ")):
            errors.append("NQ_EXECUTION_BLOCKED")
        else:
            errors.append("WRONG_INSTRUMENT")
    try:
        assert_canary_signal_instrument(ctx.requested_signal_instrument)
    except PermissionError:
        errors.append("WRONG_INSTRUMENT")
    try:
        assert_canary_account(ctx.requested_account)
    except PermissionError as exc:
        code = str(exc)
        if "SIM101" in code:
            errors.append("SIM101_BLOCKED_FROM_CANARY")
        elif "MISSING" in code:
            errors.append("ACCOUNT_IDENTITY_MISSING")
        else:
            errors.append("WRONG_ACCOUNT")
    return {"ok": not errors, "errors": sorted(set(errors)), "qty": CANARY_QTY}


def evaluate_live_gates(ctx: CanaryContext) -> dict[str, Any]:
    errors: list[str] = []
    if not ctx.phase_55b_0_pass:
        errors.append("PHASE_55B_0_REQUIRED")
    if not ctx.market_live:
        errors.append("MARKET_NOT_LIVE")
    if ctx.market_stale:
        errors.append("STALE_MARKET")
    if not ctx.nq_1m_advancing:
        errors.append("NQ_1M_NOT_ADVANCING")
    if not ctx.agg_5m_healthy:
        errors.append("AGG_5M_UNHEALTHY")
    if not ctx.agg_15m_healthy:
        errors.append("AGG_15M_UNHEALTHY")
    if not ctx.warmup_complete:
        errors.append("WARMUP_BLOCKED")
    if not ctx.parser_ok:
        errors.append("PARSER_SCHEMA_ERROR")
    if ctx.timezone != TZ_REQUIRED:
        errors.append("TIMEZONE_BLOCKED")
    if not ctx.nt_connected:
        errors.append("NT_DISCONNECTED")
    return {"ok": not errors, "errors": errors}


def evaluate_account_gates(ctx: CanaryContext) -> dict[str, Any]:
    errors: list[str] = []
    if not ctx.connected:
        errors.append("FUNDEDNEXT_NOT_CONNECTED")
    if not ctx.trade_enabled:
        errors.append("ACCOUNT_NOT_TRADE_ENABLED")
    if ctx.account_age_sec is None or float(ctx.account_age_sec) > STALE_ACCOUNT_SEC:
        errors.append("STALE_ACCOUNT_STATE")
    if ctx.equity is None:
        errors.append("EQUITY_UNKNOWN")
    if ctx.balance is None:
        errors.append("BALANCE_UNKNOWN")
    if ctx.mll is None:
        errors.append("MLL_UNKNOWN")
    if ctx.remaining_dd is None:
        errors.append("REMAINING_DD_UNKNOWN")
    elif float(ctx.remaining_dd) < float(UNIT_RISK_USD):
        errors.append("REMAINING_DD_INSUFFICIENT")
    if ctx.breached or str(ctx.account_status or "").upper() not in {"ACTIVE", "ENABLED"}:
        errors.append("POLICY_LOCKOUT")
    if not ctx.position_known:
        errors.append("POSITION_UNKNOWN")
    if ctx.position_qty != 0 or str(ctx.position_side or "").upper() not in {"FLAT", "FLAT_SAFE", ""}:
        errors.append("OPEN_POSITION_BLOCKED")
    if int(ctx.working_orders or 0) > 0:
        errors.append("WORKING_ORDER_BLOCKED")
    errors.extend(str(e) for e in ctx.order_state_errors if str(e))
    if ctx.recon_status != PROP_FLAT_SAFE:
        errors.append("UNSAFE_RECONCILIATION")
    if str(ctx.policy_verdict or "").upper() not in {"ALLOW", "APPROVED"}:
        errors.append("POLICY_BLOCKED")
    if ctx.calendar_status != "OK" or ctx.news_blocked or not ctx.session_permitted:
        errors.append("SESSION_OR_NEWS_BLOCKED")
    if ctx.consistency_lockout:
        errors.append("CONSISTENCY_LOCKOUT")
    if ctx.require_alerts and not ctx.notifications_healthy:
        errors.append("TELEGRAM_UNAVAILABLE")
    if not ctx.flatten_path_available or not ctx.kill_path_available:
        errors.append("EMERGENCY_PATH_UNAVAILABLE")
    if ctx.sim_only_armed:
        errors.append("SIM101_MUST_REMAIN_DISARMED")
    return {"ok": not errors, "errors": errors}


def evaluate_signal(ctx: CanaryContext, *, require_newer_than_arm: bool) -> dict[str, Any]:
    sig = ctx.signal
    if not sig:
        return {"ok": False, "errors": ["NO_VALID_DVP_EVENT"], "genuine": False}
    errors: list[str] = []
    source = str(sig.get("source") or "")
    kind = str(sig.get("kind") or "").upper()
    note = str(sig.get("note") or "").lower()
    if source in {"phase53_shadow", "SHADOW"} or kind == "SHADOW" or sig.get("live_bar") is False:
        errors.append("SHADOW_BLOCKED")
    if source in {"HISTORICAL", "HISTORICAL_WARMUP"} or kind == "HISTORICAL":
        errors.append("HISTORICAL_BLOCKED")
    if "replay" in note or kind == "REPLAY":
        errors.append("REPLAY_BLOCKED")
    if "warmup" in note or kind == "WARMUP" or source == "WARMUP":
        errors.append("WARMUP_BLOCKED")
    if source != LIVE_PROVENANCE:
        errors.append("NON_PHASE54_LIVE_BLOCKED")
    if not _is_genuine(sig):
        errors.append("LIVE_DVP_REQUIRED")
    ts = _parse_ts(sig.get("ts") or sig.get("signal_timestamp"))
    if ts is None:
        errors.append("UNSTAMPED_SIGNAL")
    mem = _ensure_mem()
    floor = _parse_ts(mem.get("armed_event_floor") or mem.get("armed_at"))
    if require_newer_than_arm:
        if floor is None:
            errors.append("PRE_ARM_SIGNAL_BLOCKED")
        elif ts is not None and ts <= floor:
            errors.append("PRE_ARM_SIGNAL_BLOCKED")
    sid = str(sig.get("signal_id") or "")
    if sid and sid == str(mem.get("last_signal_id") or ""):
        errors.append("ONE_SHOT_LATCH")
    if mem.get("one_shot_consumed"):
        errors.append("ONE_SHOT_LATCH")
    return {"ok": not errors, "errors": sorted(set(errors)), "genuine": _is_genuine(sig), "ts": ts.isoformat() if ts else None}


def evaluate_preflight(ctx: CanaryContext, *, for_arm: bool = True) -> dict[str, Any]:
    mem = _ensure_mem()
    errors: list[str] = []
    gates: dict[str, bool] = {}
    if ctx.prop_execution:
        errors.append("GENERAL_PROP_MUST_REMAIN_LOCKED")
    if not canary_flag_enabled():
        errors.append("CANARY_FLAG_DISARMED")
        gates["canary_flag"] = False
    else:
        gates["canary_flag"] = True
    ident = evaluate_identity(ctx)
    qi = evaluate_qty_instrument(ctx)
    live = evaluate_live_gates(ctx)
    acct = evaluate_account_gates(ctx)
    for block in (ident, qi, live, acct):
        errors.extend(block.get("errors") or [])
    gates.update(
        {
            "account_allowlist": ident["ok"],
            "qty_hard_cap": qi["ok"] and ctx.requested_qty == CANARY_QTY,
            "instrument_allowlist": "WRONG_INSTRUMENT" not in (qi.get("errors") or []) and "NQ_EXECUTION_BLOCKED" not in (qi.get("errors") or []),
            "phase_55b_0": live["ok"] and ctx.phase_55b_0_pass,
            "market_live": ctx.market_live and not ctx.market_stale,
            "prop_flat_safe": ctx.recon_status == PROP_FLAT_SAFE,
            "mll_known": ctx.mll is not None,
            "live_dvp_required": True,
            "general_prop_locked": not ctx.prop_execution,
            "sim101_disarmed": not ctx.sim_only_armed,
        }
    )
    if mem.get("blocked"):
        errors.append(str(mem.get("blocked_reason") or "PROP_CANARY_BLOCKED"))
    if mem.get("one_shot_consumed") and for_arm:
        errors.append("ONE_SHOT_LATCH")
    uniq = []
    for e in errors:
        if e not in uniq:
            uniq.append(e)
    ok = not uniq and canary_flag_enabled() and not mem.get("blocked")
    return {
        "ok": ok,
        "errors": uniq,
        "gates": gates,
        "identity": ident,
        "PROP_EXECUTION": False,
        "general_prop": "LOCKED",
    }


def current_mode(ctx: Optional[CanaryContext] = None) -> str:
    mem = _ensure_mem()
    if mem.get("blocked") or mem.get("critical"):
        return PROP_CANARY_BLOCKED
    if mem.get("in_flight"):
        return PROP_CANARY_IN_FLIGHT
    if mem.get("complete") and mem.get("one_shot_consumed") and not mem.get("armed_at"):
        return PROP_CANARY_COMPLETE
    if mem.get("armed_at"):
        return PROP_CANARY_ARMED
    if not canary_flag_enabled():
        return PROP_LOCKED
    if ctx is not None:
        pf = evaluate_preflight(ctx, for_arm=True)
        if pf["ok"]:
            return PROP_CANARY_READY
        hard = {"STALE_ACCOUNT_STATE", "MLL_UNKNOWN", "OPEN_POSITION_BLOCKED", "WORKING_ORDER_BLOCKED", "UNSAFE_RECONCILIATION", "WRONG_ACCOUNT", "ACCOUNT_IDENTITY_MISSING"}
        if any(e in hard for e in pf["errors"]) and canary_flag_enabled():
            return PROP_CANARY_BLOCKED
        return PROP_CANARY_DISARMED
    return PROP_CANARY_DISARMED


def _disarm_internal(reason: str, *, complete: bool = False, blocked: bool = False, critical: bool = False) -> dict[str, Any]:
    mem = _ensure_mem()
    was_armed = bool(mem.get("armed_at") or mem.get("in_flight"))
    mem["armed_at"] = None
    mem["armed_event_floor"] = None
    mem["in_flight"] = False
    mem["last_disarm_reason"] = reason
    if complete:
        mem["complete"] = True
        mem["one_shot_consumed"] = True
    if blocked:
        mem["blocked"] = True
        mem["blocked_reason"] = reason
    if critical:
        mem["critical"] = True
        mem["blocked"] = True
        mem["blocked_reason"] = reason
    _save_persist(
        one_shot_consumed=bool(mem.get("one_shot_consumed")),
        was_in_flight=False,
        last_persisted_mode=PROP_CANARY_COMPLETE if complete else (PROP_CANARY_BLOCKED if mem.get("blocked") else PROP_CANARY_DISARMED),
        last_result=PROP_CANARY_COMPLETE if complete else reason,
        last_error=reason if (blocked or critical) else None,
    )
    if was_armed or complete or blocked:
        kind = PROP_CANARY_BLOCKED if (blocked or critical) else PROP_CANARY_DISARMED
        _notify_safe(
            kind,
            title=kind.replace("_", " "),
            body=f"Reason: {reason}\nGENERAL PROP: LOCKED\nAccount: {CANARY_NT_ACCOUNT}\nQty cap: 1 MNQ",
        )
    return {"ok": True, "state": current_mode(), "reason": reason, "PROP_EXECUTION": False}


def disarm(reason: str = "OPERATOR") -> dict[str, Any]:
    return _disarm_internal(reason)


def observe_runtime(ctx: CanaryContext) -> dict[str, Any]:
    """While armed, stale market / disconnect / unsafe account auto-disarm. Notify failures do not."""
    mem = _ensure_mem()
    if not mem.get("armed_at") and not mem.get("in_flight"):
        return {"changed": False, "state": current_mode(ctx)}
    if ctx.market_stale or not ctx.market_live:
        return {**_disarm_internal("STALE_MARKET", blocked=True), "changed": True}
    if not ctx.nt_connected:
        return {**_disarm_internal("NT_DISCONNECTED", blocked=True), "changed": True}
    if ctx.account_age_sec is None or float(ctx.account_age_sec) > STALE_ACCOUNT_SEC or ctx.mll is None:
        return {**_disarm_internal("STALE_OR_UNKNOWN_ACCOUNT", blocked=True), "changed": True}
    if ctx.position_qty != 0 and not mem.get("in_flight"):
        return {**_disarm_internal("OPEN_POSITION_BLOCKED", blocked=True), "changed": True}
    return {"changed": False, "state": current_mode(ctx)}


def arm(ctx: CanaryContext, *, now: Optional[datetime] = None) -> dict[str, Any]:
    """Permit the next genuine phase54_live event once. Does not place a trade."""
    mem = _ensure_mem()
    pf = evaluate_preflight(ctx, for_arm=True)
    if not pf["ok"]:
        state = PROP_CANARY_BLOCKED if canary_flag_enabled() else PROP_LOCKED
        return {
            "ok": False,
            "armed": False,
            "state": state,
            "errors": pf["errors"],
            "gates": pf["gates"],
            "PROP_EXECUTION": False,
        }
    ts = now or ctx.now or _utc_now()
    mem["armed_at"] = ts if isinstance(ts, datetime) else _parse_ts(ts) or _utc_now()
    mem["armed_event_floor"] = mem["armed_at"]
    mem["blocked"] = False
    mem["blocked_reason"] = None
    mem["complete"] = False
    _save_persist(last_persisted_mode=PROP_CANARY_DISARMED, was_in_flight=False)
    _notify_safe(
        PROP_CANARY_ARMED,
        title="PROP CANARY ARMED",
        body=(
            f"Next genuine phase54_live event may execute once\n"
            f"Account: {CANARY_NT_ACCOUNT}\n"
            f"Instrument: {EXEC_INSTRUMENT_DISPLAY}\n"
            f"Qty: 1 MNQ\nGENERAL PROP: LOCKED\nSIM101: not a destination"
        ),
    )
    return {
        "ok": True,
        "armed": True,
        "state": PROP_CANARY_ARMED,
        "armed_at": _iso(mem["armed_at"] if isinstance(mem["armed_at"], datetime) else None),
        "PROP_EXECUTION": False,
        "note": "Arming is not an order. Wait for a genuine phase54_live event newer than armed_at.",
    }


def _build_payload(ctx: CanaryContext, *, direction: str, trade_id: str) -> dict[str, Any]:
    stop_pts, tgt_pts = frozen_risk_for_direction(direction)
    plan = plan_canary_bracket(
        direction=direction,
        trade_id=trade_id,
        stop_points=stop_pts,
        target_points=tgt_pts,
        account=CANARY_NT_ACCOUNT,
    )
    validate_canary_oif_line(plan["entry_line"])
    acct = parse_oif_account(plan["entry_line"])
    if acct != CANARY_NT_ACCOUNT:
        raise PermissionError("WRONG_ACCOUNT")
    if acct == SIM101_ACCOUNT:
        raise PermissionError("SIM101_BLOCKED_FROM_CANARY")
    children_preview = {
        "mechanism": plan["mechanism"],
        "stop_account": CANARY_NT_ACCOUNT,
        "stop_instrument": EXEC_INSTRUMENT_NT,
        "stop_qty": CANARY_QTY,
        "stop_points": stop_pts,
        "target_points": tgt_pts,
        "confirmed": False,
        "note": "Child STOPMARKET+LIMIT after real fill. Prices not faked in dry-run.",
    }
    return {
        "account": CANARY_NT_ACCOUNT,
        "platform_login": CANARY_LOGIN,
        "fundednext_account_id": CANARY_ACCOUNT_ID,
        "signal_instrument": SIGNAL_INSTRUMENT,
        "execution_instrument": EXEC_INSTRUMENT_DISPLAY,
        "execution_instrument_nt": EXEC_INSTRUMENT_NT,
        "quantity": CANARY_QTY,
        "direction": direction.upper(),
        "order_type": "MARKET",
        "trade_id": trade_id,
        "entry_line": plan["entry_line"],
        "protective": children_preview,
        "plan": plan,
        "route": "NINJATRADER_ATI_FUNDEDNEXT_CANARY",
        "account_data_source": "FUNDEDNEXT_MCP",
        "order_submission": "NINJATRADER_ATI_OIF",
        "order_status_source": "NINJATRADER_LOG_AND_ACCOUNT",
        "position_reconciliation_source": "NINJATRADER_ACCOUNT_POSITION+FUNDEDNEXT_MCP",
        "PROP_EXECUTION": False,
        "sim101": False,
        "strategy_hash": NQ_FROZEN_HASH,
    }


def dry_run(ctx: CanaryContext) -> dict[str, Any]:
    """Every check + broker payload. Never writes incoming. Never fakes broker ack."""
    observe_runtime(ctx)
    pf = evaluate_preflight(ctx, for_arm=False)
    sig_eval = evaluate_signal(ctx, require_newer_than_arm=bool(_ensure_mem().get("armed_at")))
    direction = str((ctx.signal or {}).get("direction") or "LONG")
    trade_id = "AITRADE_FN_CANARY_" + str((ctx.signal or {}).get("signal_id") or "DRY")
    try:
        payload = _build_payload(ctx, direction=direction, trade_id=trade_id)
        drop = drop_canary_oif_lines([payload["entry_line"]], transmit=False)
    except Exception as exc:
        return {
            "ok": False,
            "verdict": "PROP_CANARY_DRY_RUN_FAIL",
            "submitted": False,
            "transmitted": False,
            "errors": pf["errors"] + sig_eval["errors"] + [str(exc)],
            "PROP_EXECUTION": False,
        }
    errors = list(pf["errors"])
    if ctx.signal is not None:
        errors.extend(sig_eval["errors"])
    ok = pf["ok"] and (ctx.signal is None or sig_eval["ok"])
    return {
        "ok": ok,
        "verdict": "PROP_CANARY_DRY_RUN_PASS" if ok else "PROP_CANARY_DRY_RUN_FAIL",
        "submitted": False,
        "transmitted": False,
        "broker_ack": None,
        "payload": payload,
        "drop": drop,
        "gates": pf["gates"],
        "errors": errors,
        "signal": sig_eval,
        "rule_state": {
            "equity": ctx.equity,
            "mll": ctx.mll,
            "remaining_dd": ctx.remaining_dd,
            "policy_verdict": ctx.policy_verdict,
            "calendar_status": ctx.calendar_status,
        },
        "PROP_EXECUTION": False,
        "account": CANARY_NT_ACCOUNT,
        "quantity": CANARY_QTY,
        "instrument": EXEC_INSTRUMENT_DISPLAY,
    }


def submit_once(
    ctx: CanaryContext,
    *,
    transmit: bool = False,
    transmitter: Optional[TransmitFn] = None,
) -> dict[str, Any]:
    mem = _ensure_mem()
    try:
        observe_runtime(ctx)
        if mem.get("one_shot_consumed"):
            return {"ok": False, "submitted": False, "error_code": "ONE_SHOT_LATCH", "state": current_mode(ctx), "PROP_EXECUTION": False}
        if not mem.get("armed_at"):
            return {"ok": False, "submitted": False, "error_code": "NOT_ARMED", "state": current_mode(ctx), "PROP_EXECUTION": False}
        pf = evaluate_preflight(ctx, for_arm=False)
        sig_eval = evaluate_signal(ctx, require_newer_than_arm=True)
        if not pf["ok"] or not sig_eval["ok"]:
            reason = (pf["errors"] or sig_eval["errors"] or ["PROP_CANARY_BLOCKED"])[0]
            _disarm_internal(reason, blocked=True)
            return {
                "ok": False,
                "submitted": False,
                "error_code": reason,
                "errors": pf["errors"] + sig_eval["errors"],
                "state": current_mode(ctx),
                "PROP_EXECUTION": False,
            }
        direction = str((ctx.signal or {}).get("direction") or "")
        trade_id = "AITRADE_FN_CANARY_" + str((ctx.signal or {}).get("signal_id") or uuid.uuid4().hex[:10])
        payload = _build_payload(ctx, direction=direction, trade_id=trade_id)
        mem["in_flight"] = True
        mem["last_trade_id"] = trade_id
        mem["last_payload"] = payload
        _save_persist(was_in_flight=True, last_persisted_mode=PROP_CANARY_DISARMED)
        tx = transmitter or drop_canary_oif_lines
        result = tx([payload["entry_line"]], transmit=transmit)
        submitted = bool(result.get("submitted") or result.get("transmitted"))
        rejected = (not result.get("ok", True)) or str(result.get("status") or "").upper() in {"REJECTED", "ORDER_REJECTED"}
        _notify_safe(
            "ORDER_SUBMITTED" if submitted else "ORDER_REJECTED",
            title="FUNDEDNEXT CANARY ORDER SUBMITTED" if submitted else "FUNDEDNEXT CANARY ORDER REJECTED",
            body=(
                f"Account: {CANARY_NT_ACCOUNT}\nMNQ 09-26 · qty 1 · {direction}\n"
                f"Transmitted: {submitted}\nGENERAL PROP: LOCKED"
            ),
            metadata={"route": "PROP_CANARY", "correlation_id": trade_id},
        )
        if rejected or not result.get("ok", True):
            _notify_safe(
                "ORDER_REJECTED",
                title="FUNDEDNEXT CANARY ORDER REJECTED",
                body=str(result.get("error_code") or result.get("status") or "rejected"),
            )
            mem["one_shot_consumed"] = True
            _disarm_internal("ORDER_REJECTED", complete=False, blocked=True)
            return {
                "ok": False,
                "submitted": submitted,
                "transmitted": bool(result.get("transmitted")),
                "error_code": "ORDER_REJECTED",
                "state": current_mode(ctx),
                "result": result,
                "payload": payload,
                "PROP_EXECUTION": False,
            }
        if not transmit:
            mem["in_flight"] = False
            _save_persist(was_in_flight=False)
            return {
                "ok": True,
                "submitted": False,
                "transmitted": False,
                "verdict": "PROP_CANARY_DRY_RUN_PASS",
                "state": PROP_CANARY_ARMED,
                "payload": payload,
                "result": result,
                "PROP_EXECUTION": False,
            }
        mem["last_signal_id"] = str((ctx.signal or {}).get("signal_id") or "")
        mem["one_shot_consumed"] = True
        _notify_safe("ORDER_ACCEPTED", title="FUNDEDNEXT CANARY ORDER ACCEPTED", body=f"ID {trade_id}")
        return {
            "ok": True,
            "submitted": True,
            "transmitted": True,
            "state": PROP_CANARY_IN_FLIGHT,
            "payload": payload,
            "result": result,
            "PROP_EXECUTION": False,
            "note": "Protective OCO children require a real fill. Stop rejection is CRITICAL.",
        }
    except Exception as exc:
        _notify_safe("EXECUTION_FAILURE", title="FUNDEDNEXT CANARY EXECUTION FAILURE", body=str(exc))
        mem["one_shot_consumed"] = True
        _disarm_internal("EXECUTION_EXCEPTION", blocked=True, critical=True)
        return {
            "ok": False,
            "submitted": False,
            "error_code": "EXECUTION_EXCEPTION",
            "detail": str(exc),
            "state": current_mode(ctx),
            "PROP_EXECUTION": False,
        }


def mark_stop_rejected() -> dict[str, Any]:
    mem = _ensure_mem()
    mem["one_shot_consumed"] = True
    _notify_safe("EXECUTION_FAILURE", title="FUNDEDNEXT CANARY STOP REJECTED", body="CRITICAL · stop not confirmed after entry")
    return _disarm_internal("STOP_REJECTED", blocked=True, critical=True)


def mark_round_trip_complete() -> dict[str, Any]:
    _notify_safe("POSITION_CLOSED", title="FUNDEDNEXT CANARY POSITION CLOSED", body="Flat · disarming")
    return _disarm_internal("ROUND_TRIP_COMPLETE", complete=True)


def emergency_flatten(*, transmit: bool = False, transmitter: Optional[TransmitFn] = None) -> dict[str, Any]:
    line = build_canary_close_oif(account=CANARY_NT_ACCOUNT)
    validate_canary_oif_line(line)
    tx = transmitter or drop_canary_oif_lines
    result = tx([line], transmit=transmit)
    _notify_safe(
        "EMERGENCY_FLATTEN",
        title="FUNDEDNEXT CANARY EMERGENCY FLATTEN",
        body=f"Transmitted: {bool(result.get('transmitted'))}\nAccount: {CANARY_NT_ACCOUNT}",
    )
    _disarm_internal("EMERGENCY_FLATTEN", blocked=True, critical=bool(result.get("transmitted")))
    return {"ok": bool(result.get("ok")), "submitted": bool(result.get("submitted")), "result": result, "account": CANARY_NT_ACCOUNT, "PROP_EXECUTION": False}


def public_snapshot(ctx: Optional[CanaryContext] = None) -> dict[str, Any]:
    mem = _ensure_mem()
    mode = current_mode(ctx)
    pf = evaluate_preflight(ctx, for_arm=True) if ctx is not None else {"ok": False, "errors": ["NO_CONTEXT"], "gates": {}}
    return {
        "state": mode,
        "label": mode,
        "flag": canary_flag_enabled(),
        "armed": bool(mem.get("armed_at")),
        "armed_at": _iso(mem["armed_at"]) if isinstance(mem.get("armed_at"), datetime) else mem.get("armed_at"),
        "in_flight": bool(mem.get("in_flight")),
        "complete": bool(mem.get("complete")),
        "blocked": bool(mem.get("blocked")),
        "blocked_reason": mem.get("blocked_reason") or mem.get("last_disarm_reason"),
        "critical": bool(mem.get("critical")),
        "one_shot_consumed": bool(mem.get("one_shot_consumed")),
        "general_prop": "LOCKED",
        "PROP_EXECUTION": False,
        "account": CANARY_NT_ACCOUNT,
        "platform_login": CANARY_LOGIN,
        "fundednext_account_id": CANARY_ACCOUNT_ID,
        "plan": CANARY_PLAN,
        "qty_cap": CANARY_QTY,
        "signal_instrument": SIGNAL_INSTRUMENT,
        "execution_instrument": EXEC_INSTRUMENT_DISPLAY,
        "recon": (ctx.recon_status if ctx else None) or "UNKNOWN",
        "preflight_ok": bool(pf.get("ok")),
        "errors": pf.get("errors") or [],
        "gates": pf.get("gates") or {},
        "boot_id": mem.get("boot_id"),
        "route": "NINJATRADER_ATI_FUNDEDNEXT_CANARY",
        "mcp_orders": False,
        "sim101": False,
        "remaining_live_gate": "PHASE_55B_0_PASS",
    }


def context_from_ops_snapshot(snap: dict[str, Any]) -> CanaryContext:
    """Map a phase54 snapshot into canary gates. Does not invent LIVE when the market is closed."""
    fn = snap.get("fundednext") if isinstance(snap.get("fundednext"), dict) else {}
    mcp = snap.get("fundednext_mcp") if isinstance(snap.get("fundednext_mcp"), dict) else {}
    match = (mcp.get("match") if isinstance(mcp.get("match"), dict) else None) or {}
    acct = snap.get("account") if isinstance(snap.get("account"), dict) else {}
    pos = snap.get("position") if isinstance(snap.get("position"), dict) else {}
    live = snap.get("live_dvp") if isinstance(snap.get("live_dvp"), dict) else {}
    md = snap.get("market_data") if isinstance(snap.get("market_data"), dict) else {}
    ntf = snap.get("notifications") if isinstance(snap.get("notifications"), dict) else {}
    dump = snap.get("telemetry_dump") if isinstance(snap.get("telemetry_dump"), dict) else {}
    sig = None
    live_sig = live.get("live_signal") if isinstance(live.get("live_signal"), dict) else None
    dec = snap.get("decision") if isinstance(snap.get("decision"), dict) else {}
    if isinstance(dec.get("last_live_signal"), dict):
        sig = dec.get("last_live_signal")
    elif live_sig:
        sig = live_sig
    market_live = str(snap.get("market_data_status") or "") == "LIVE" and str(snap.get("market_data_quality") or "").upper() in {"LIVE", ""}
    warmup = str(live.get("strategy_status") or "").upper() not in {"", "WAITING", "WARMING_UP", "WARMUP"}
    parser_ok = "error" not in str(dump.get("nq_bars_1m_status") or "").lower()
    nq_adv = int(dump.get("nq_bars_1m_count") or 0) > 0 and bool(dump.get("last_nq_bar_ts"))
    phase_55b = bool(live.get("phase_55b_0_pass") or snap.get("phase_55b_0_pass"))
    if not phase_55b:
        phase_55b = bool(
            market_live
            and nq_adv
            and warmup
            and parser_ok
            and str(live.get("pipeline") or "").upper() in {"LIVE", "PHASE54_LIVE", ""}
        )
    recon = PROP_FLAT_SAFE if (
        bool(pos.get("reconciled"))
        and int(pos.get("quantity") or pos.get("broker_qty") or 0) == 0
        and str(pos.get("side") or "FLAT").upper() in {"FLAT", "NONE", ""}
    ) else "UNSAFE"
    qty = int(pos.get("quantity") or pos.get("broker_qty") or 0)
    nt_name = str(fn.get("account_id") or match.get("fundednext_name") or acct.get("account_id") or "")
    login = str(fn.get("platform_login") or match.get("platform_login") or acct.get("platform_login") or "")
    aid = fn.get("fundednext_account_id") or match.get("account_id") or acct.get("fundednext_account_id")
    working = 0
    order_gate = snap.get("order_state_gate") if isinstance(snap.get("order_state_gate"), dict) else {"ok": False, "errors": ["ORDER_STATE_MISSING"]}
    order_errors = tuple(str(error) for error in (order_gate.get("errors") or ()) if str(error))
    if order_gate.get("ok") is not True and not order_errors:
        order_errors = ("ORDER_STATE_MISSING",)
    order_doc = snap.get("orders") if isinstance(snap.get("orders"), dict) else {}
    working = int(order_doc.get("active_count") or 0)
    age = mcp.get("age_sec")
    if age is None:
        age = acct.get("age_sec")
    policy = (dec.get("verdict") or (dec.get("policy") or {}).get("verdict") or "BLOCK")
    healthy = str(ntf.get("delivery_status") or "").upper() in {"HEALTHY", "READY", "OK", "LIVE"}
    return CanaryContext(
        nt_account=nt_name or None,
        platform_login=login or None,
        fundednext_account_id=aid,
        config_expected_account=CANARY_NT_ACCOUNT,
        connected=bool(fn.get("connected") or snap.get("fundednext_connection") == "CONNECTED"),
        trade_enabled=str(fn.get("account_status") or acct.get("account_status") or "ACTIVE").upper() == "ACTIVE" and not bool(fn.get("breached") or acct.get("breached")),
        account_age_sec=float(age) if age is not None else None,
        equity=fn.get("equity") if fn.get("equity") is not None else acct.get("equity"),
        balance=fn.get("balance") if fn.get("balance") is not None else acct.get("balance"),
        mll=fn.get("mll") if fn.get("mll") is not None else acct.get("mll"),
        remaining_dd=fn.get("remaining_loss_buffer") if fn.get("remaining_loss_buffer") is not None else acct.get("remaining_dd"),
        breached=bool(fn.get("breached") or acct.get("breached")),
        account_status=str(fn.get("account_status") or acct.get("account_status") or "ACTIVE"),
        position_known=bool(pos.get("ninjatrader", {}).get("known") if isinstance(pos.get("ninjatrader"), dict) else pos.get("reconciled")),
        position_side=str(pos.get("side") or "FLAT"),
        position_qty=qty,
        working_orders=working,
        order_state_errors=order_errors,
        recon_status=recon,
        policy_verdict=str(policy),
        calendar_status=str(dec.get("calendar_status") or "OK"),
        news_blocked="NEWS" in str(dec.get("code") or "").upper(),
        session_permitted=str(dec.get("calendar_status") or "OK") == "OK",
        phase_55b_0_pass=bool(phase_55b),
        market_live=market_live,
        nq_1m_advancing=nq_adv,
        agg_5m_healthy=bool(live.get("agg_5m_healthy") or (live.get("last_finalized_5m") and market_live)),
        agg_15m_healthy=bool(live.get("agg_15m_healthy") or market_live),
        warmup_complete=warmup,
        parser_ok=parser_ok,
        timezone=TZ_REQUIRED,
        nt_connected=bool((dump.get("alive") if dump else True)),
        market_stale=not market_live,
        notifications_healthy=healthy if ntf else False,
        flatten_path_available=True,
        kill_path_available=True,
        signal=sig if isinstance(sig, dict) else None,
        prop_execution=bool(snap.get("PROP_EXECUTION")),
        sim_only_armed="ARMED" in str(snap.get("execution_arm") or "") and "DISARMED" not in str(snap.get("execution_arm") or ""),
        require_alerts=True,
    )
