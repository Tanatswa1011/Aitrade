"""Unattended FundedNext one-shot canary — operational autonomy layer.

Does not change frozen NQ DVP. Does not enable general PROP_EXECUTION.
Requires ``AITRADE_UNATTENDED_PROP_CANARY`` plus preflight; the attended
``AITRADE_PROP_CANARY_EXECUTION`` flag alone cannot enter this mode.
"""
from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Optional
from zoneinfo import ZoneInfo

from execution_instrument import EXEC_INSTRUMENT_DISPLAY, EXEC_INSTRUMENT_NT
from execution_status import NQ_FROZEN_HASH
from nq_drift_vwap_models import NO_NEW_TRADES_AFTER_LOCAL, TRADE_START_LOCAL
from prop_canary import (
    CANARY_ACCOUNT_ID,
    CANARY_LOGIN,
    CANARY_NT_ACCOUNT,
    LIVE_PROVENANCE,
    PROP_FLAT_SAFE,
    TZ_REQUIRED,
    CanaryContext,
    evaluate_identity,
    evaluate_qty_instrument,
    evaluate_signal,
    genuine_signal,
    passing_context,
    _build_payload,
)
from phase52_policy import UNIT_RISK_USD
from prop_canary_nt_exec import (
    CANARY_QTY,
    SIGNAL_INSTRUMENT,
    SIM101_ACCOUNT,
    child_oifs_from_fill,
    drop_canary_oif_lines,
    parse_oif_account,
    validate_canary_oif_line,
)
from unattended_watchdog import WatchdogObservation, crash_surface, observe as watchdog_observe
from broker_acknowledgements import (
    ExpectedLifecycle,
    ProtectionLifecycle,
    PROTECTED_CONFIRMED,
    EXPECTED_SOURCE,
)

ROOT = Path(__file__).resolve().parent
ENV_FLAG = "AITRADE_UNATTENDED_PROP_CANARY"
ENV_STATE = "AITRADE_UNATTENDED_STATE"
DEFAULT_STATE_PATH = ROOT / "state" / "unattended_prop_canary.json"
NY = ZoneInfo("America/New_York")
PROTECTION_TIMEOUT_SEC = 15.0
ADDON_SCHEMA = "AITRADE_NT_READONLY_V1"

UNATTENDED_DISABLED = "UNATTENDED_DISABLED"
UNATTENDED_PREFLIGHT = "UNATTENDED_PREFLIGHT"
UNATTENDED_WAITING_LIVE_DATA = "UNATTENDED_WAITING_LIVE_DATA"
UNATTENDED_WAITING_SESSION = "UNATTENDED_WAITING_SESSION"
UNATTENDED_WAITING_DVP = "UNATTENDED_WAITING_DVP"
UNATTENDED_ENTRY_PENDING = "UNATTENDED_ENTRY_PENDING"
UNATTENDED_POSITION_OPEN = "UNATTENDED_POSITION_OPEN"
UNATTENDED_EXIT_PENDING = "UNATTENDED_EXIT_PENDING"
UNATTENDED_COMPLETE = "UNATTENDED_COMPLETE"
UNATTENDED_COMPLETE_NO_TRADE = "UNATTENDED_COMPLETE_NO_TRADE"
UNATTENDED_BLOCKED = "UNATTENDED_BLOCKED"
UNATTENDED_BLOCKED_RESTART = "UNATTENDED_BLOCKED_RESTART"

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


def unattended_flag_enabled() -> bool:
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
        return mutable_path("state", "unattended_prop_canary.json")
    # Production ignores arbitrary environment path overrides.
    return DEFAULT_STATE_PATH


def _ny(ts: Optional[datetime] = None) -> datetime:
    cur = ts or _utc_now()
    if cur.tzinfo is None:
        cur = cur.replace(tzinfo=timezone.utc)
    return cur.astimezone(NY)


def _clock() -> datetime:
    mem = _MEM if _MEM else {}
    clk = mem.get("clock")
    if isinstance(clk, datetime):
        return clk
    return _utc_now()


def _trading_day(ts: Optional[datetime] = None) -> str:
    return _ny(ts or _clock()).date().isoformat()


def _session_bounds(day: str) -> tuple[datetime, datetime]:
    sh, sm = (int(x) for x in TRADE_START_LOCAL.split(":"))
    eh, em = (int(x) for x in NO_NEW_TRADES_AFTER_LOCAL.split(":"))
    d = datetime.fromisoformat(day).date()
    start = datetime(d.year, d.month, d.day, sh, sm, tzinfo=NY)
    end = datetime(d.year, d.month, d.day, eh, em, tzinfo=NY)
    return start, end


def in_session(ts: Optional[datetime] = None) -> bool:
    now = _ny(ts)
    start, end = _session_bounds(now.date().isoformat())
    return start <= now < end


def session_closed_no_entry(ts: Optional[datetime] = None) -> bool:
    now = _ny(ts)
    _start, end = _session_bounds(now.date().isoformat())
    return now >= end


def _default_mem() -> dict[str, Any]:
    return {
        "boot_id": uuid.uuid4().hex,
        "state": UNATTENDED_DISABLED,
        "enabled": False,
        "readiness_at": None,
        "preflight_ok": False,
        "phase_55b": False,
        "blocked": False,
        "blocked_reason": None,
        "critical": False,
        "complete": False,
        "no_trade": False,
        "in_flight": False,
        "position_open": False,
        "stop_confirmed": False,
        "target_confirmed": False,
        "last_trade_id": None,
        "protection_native": True,
        "notify_failures": 0,
        "last_change": _iso(),
    }


def _default_persist() -> dict[str, Any]:
    return {
        "schema": "AITRADE_UNATTENDED_V1",
        "PROP_EXECUTION": False,
        "trading_day": None,
        "entry_attempt_used": False,
        "locked_for_day": False,
        "lock_reason": None,
        "operator_enabled_day": None,
        "was_enabled": False,
        "restart_block": False,
        "last_state": UNATTENDED_DISABLED,
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
    today = _trading_day()
    stored = out.get("trading_day")
    if stored and str(stored) < str(today):
        out["entry_attempt_used"] = False
        out["locked_for_day"] = False
        out["lock_reason"] = None
        out["operator_enabled_day"] = None
        out["was_enabled"] = False
        out["restart_block"] = False
        out["trading_day"] = today
        out["last_state"] = UNATTENDED_DISABLED
    return out


def _save_persist(**over: Any) -> dict[str, Any]:
    doc = _load_persist()
    doc.update(over)
    doc["PROP_EXECUTION"] = False
    doc["updated_at"] = _iso()
    if not doc.get("trading_day"):
        doc["trading_day"] = _trading_day()
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
        if persist.get("restart_block") or persist.get("was_enabled"):
            _MEM["state"] = UNATTENDED_BLOCKED_RESTART
            _MEM["blocked"] = True
            _MEM["blocked_reason"] = "UNATTENDED_BLOCKED_RESTART"
            _MEM["enabled"] = False
        if persist.get("entry_attempt_used") or persist.get("locked_for_day"):
            _MEM["blocked"] = True
            _MEM["complete"] = True
            _MEM["blocked_reason"] = persist.get("lock_reason") or "DAILY_LATCH"
            if persist.get("lock_reason") == "NO_VALID_DVP_EVENT":
                _MEM["state"] = UNATTENDED_COMPLETE_NO_TRADE
                _MEM["no_trade"] = True
            else:
                _MEM["state"] = UNATTENDED_BLOCKED if persist.get("locked_for_day") else UNATTENDED_COMPLETE
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


def simulate_process_restart() -> None:
    global _MEM
    persist = _load_persist()
    persist["restart_block"] = True
    persist["was_enabled"] = bool(persist.get("was_enabled") or persist.get("operator_enabled_day"))
    persist["last_state"] = UNATTENDED_DISABLED
    _save_persist(**{k: persist[k] for k in persist})
    _MEM = {}
    _ensure_mem()


def daily_latch_used() -> bool:
    return bool(_load_persist().get("entry_attempt_used"))


def burn_daily_latch(reason: str = "ENTRY_ATTEMPT") -> None:
    mem = _ensure_mem()
    _save_persist(
        trading_day=_trading_day(),
        entry_attempt_used=True,
        locked_for_day=True,
        lock_reason=reason,
        restart_block=True,
    )
    mem["complete"] = True


def _notify(kind: str, body: str, *, critical: bool = False) -> None:
    mem = _ensure_mem()
    try:
        from aitrade_notifications import EventType, notify_safe

        mapping = {
            "UNATTENDED_PREFLIGHT_PASS": EventType.UNATTENDED_PREFLIGHT_PASS,
            "UNATTENDED_PREFLIGHT_FAIL": EventType.UNATTENDED_PREFLIGHT_FAIL,
            "LIVE_BAR_VALIDATION_PASS": EventType.LIVE_BAR_VALIDATION_PASS,
            "UNATTENDED_WAITING_DVP": EventType.UNATTENDED_WAITING_DVP,
            "UNATTENDED_BLOCKED": EventType.UNATTENDED_BLOCKED,
            "UNATTENDED_DVP_DETECTED": EventType.UNATTENDED_DVP_DETECTED,
            "UNATTENDED_ORDER_SUBMITTED": EventType.UNATTENDED_ORDER_SUBMITTED,
            "UNATTENDED_ORDER_ACCEPTED": EventType.UNATTENDED_ORDER_ACCEPTED,
            "UNATTENDED_ORDER_REJECTED": EventType.UNATTENDED_ORDER_REJECTED,
            "UNATTENDED_POSITION_OPENED": EventType.UNATTENDED_POSITION_OPENED,
            "UNATTENDED_STOP_CONFIRMED": EventType.UNATTENDED_STOP_CONFIRMED,
            "UNATTENDED_TARGET_CONFIRMED": EventType.UNATTENDED_TARGET_CONFIRMED,
            "UNATTENDED_ENGINE_LOST_POSITION_OPEN": EventType.UNATTENDED_ENGINE_LOST_POSITION_OPEN,
            "UNATTENDED_PROTECTION_FAILURE": EventType.UNATTENDED_PROTECTION_FAILURE,
            "UNATTENDED_POSITION_CLOSED": EventType.UNATTENDED_POSITION_CLOSED,
            "UNATTENDED_COMPLETE": EventType.UNATTENDED_COMPLETE,
            "UNATTENDED_COMPLETE_NO_TRADE": EventType.UNATTENDED_COMPLETE_NO_TRADE,
        }
        et = mapping.get(kind)
        if et is None:
            return
        title = kind.replace("_", " ")
        extra = f"\nAccount: {CANARY_NT_ACCOUNT}\nQty: 1 MNQ\nGENERAL PROP: LOCKED"
        notify_safe(
            et,
            title=title,
            body=body + extra,
            account=CANARY_NT_ACCOUNT,
            metadata={"route": "UNATTENDED_PROP_CANARY", "critical": critical},
            force=True,
        )
    except Exception:
        mem["notify_failures"] = int(mem.get("notify_failures") or 0) + 1


def _set_state(state: str, *, reason: Optional[str] = None, notify: Optional[str] = None, body: str = "") -> None:
    mem = _ensure_mem()
    prev = mem.get("state")
    mem["state"] = state
    mem["last_change"] = _iso()
    if reason:
        mem["blocked_reason"] = reason
    if state in {UNATTENDED_BLOCKED, UNATTENDED_BLOCKED_RESTART}:
        mem["blocked"] = True
        mem["enabled"] = False
    if notify and state != prev:
        _notify(notify, body or (reason or state), critical=state in {UNATTENDED_BLOCKED, UNATTENDED_BLOCKED_RESTART} or "CRITICAL" in str(reason or ""))


@dataclass
class UnattendedContext:
    canary: CanaryContext = field(default_factory=passing_context)
    nt_process_available: bool = True
    addon_schema: str = ADDON_SCHEMA
    telemetry_age_sec: float = 0.5
    python_desk_healthy: bool = True
    watchdog_healthy: bool = True
    nq_bars: list = field(default_factory=list)
    nq_bars_1m_status: str = "LIVE"
    nq_1m_count: int = 12
    nq_1m_count_prev: int = 8
    timestamps_monotonic: bool = True
    duplicate_bar_ids: bool = False
    unexpected_gaps: bool = False
    bars_fresh: bool = True
    agg_5m_advancing: bool = True
    agg_15m_advancing: bool = True
    market_quality: str = "LIVE"
    delayed_feed: bool = False
    eod_feed: bool = False
    oif_incoming_writable: bool = True
    stale_oif_replay: bool = False
    orphan_protective: bool = False
    unexpected_other_position: bool = False
    frozen_hash_match: bool = True
    frozen_config_match: bool = True
    strategy_source_unmodified: bool = True
    policy_hash_expected: bool = True
    daily_lockout: bool = False
    apprise_configured: bool = True
    last_notify_ok: bool = True
    engine_state: str = "RUNNING"
    engine_intentional_stop: bool = False
    engine_heartbeat_age_sec: float = 1.0
    fill_qty: int = 1
    entry_fill_price: float = 24800.0
    entry_ack: Optional[dict[str, Any]] = None
    stop_ack: Any = None
    target_ack: Any = None
    protection_timeout: bool = False
    now: Optional[datetime] = None
    trading_day: Optional[str] = None


def passing_unattended(**over: Any) -> UnattendedContext:
    canary_over = over.pop("canary_over", {}) if "canary_over" in over else {}
    bars = over.pop("nq_bars", None)
    ctx = UnattendedContext(
        canary=passing_context(**canary_over) if canary_over else passing_context(),
        nq_bars=bars or [
            {"id": f"b{i}", "ts": 1_000_000 + i * 60, "iso_et": f"2026-08-24T10:{30+i:02d}:00-04:00"}
            for i in range(8)
        ],
    )
    return replace(ctx, **over) if over else ctx


def evaluate_automated_phase_55b(ctx: UnattendedContext) -> dict[str, Any]:
    """Real-runtime live-bar proof. Production must not invent LIVE."""
    errors: list[str] = []
    c = ctx.canary
    if ctx.delayed_feed or str(ctx.market_quality).upper() in {"DELAYED", "SIMULATED", "PLAYBACK", "EOD"}:
        errors.append("MARKET_DELAYED")
    if ctx.eod_feed:
        errors.append("MARKET_DELAYED")
    if not c.market_live or c.market_stale:
        errors.append("MARKET_NOT_LIVE")
    if ctx.nq_1m_count <= 0:
        errors.append("NO_NQ_BARS")
    if ctx.nq_1m_count <= ctx.nq_1m_count_prev:
        errors.append("BARS_NOT_INCREASING")
    if not ctx.timestamps_monotonic:
        errors.append("TIMESTAMPS_NOT_ADVANCING")
    if ctx.duplicate_bar_ids:
        errors.append("DUPLICATE_BARS")
    if ctx.unexpected_gaps:
        errors.append("UNEXPECTED_GAPS")
    if not ctx.bars_fresh:
        errors.append("BARS_STALE")
    if str(ctx.nq_bars_1m_status).upper() not in {"LIVE", "OK", "READY"}:
        errors.append("NQ_BARS_NOT_LIVE")
    if not ctx.agg_5m_advancing or not c.agg_5m_healthy:
        errors.append("AGG_5M_BROKEN")
    if not ctx.agg_15m_advancing or not c.agg_15m_healthy:
        errors.append("AGG_15M_BROKEN")
    if c.timezone != TZ_REQUIRED:
        errors.append("TIMEZONE_BLOCKED")
    if not c.parser_ok:
        errors.append("PARSER_SCHEMA_ERROR")
    if not c.warmup_complete:
        errors.append("WARMUP_INCOMPLETE")
    ids = [str(b.get("id")) for b in (ctx.nq_bars or []) if isinstance(b, dict)]
    if ids and len(ids) != len(set(ids)):
        errors.append("DUPLICATE_BARS")
    ts_list = [int(b.get("ts") or 0) for b in (ctx.nq_bars or []) if isinstance(b, dict)]
    if ts_list and ts_list != sorted(ts_list):
        errors.append("TIMESTAMPS_NOT_ADVANCING")
    ok = not errors
    return {
        "ok": ok,
        "verdict": "AUTOMATED_PHASE_55B_PASS" if ok else "AUTOMATED_PHASE_55B_FAIL",
        "errors": sorted(set(errors)),
    }


def evaluate_preflight(ctx: UnattendedContext) -> dict[str, Any]:
    errors: list[str] = []
    c = ctx.canary
    if not unattended_flag_enabled():
        errors.append("UNATTENDED_FLAG_DISABLED")
    if c.prop_execution:
        errors.append("GENERAL_PROP_MUST_REMAIN_LOCKED")
    if c.sim_only_armed:
        errors.append("SIM101_MUST_REMAIN_DISARMED")
    if not ctx.nt_process_available:
        errors.append("NT_RUNTIME_UNAVAILABLE")
    if ctx.addon_schema != ADDON_SCHEMA:
        errors.append("ADDON_SCHEMA_MISMATCH")
    if ctx.telemetry_age_sec is None or float(ctx.telemetry_age_sec) > 5.0:
        errors.append("TELEMETRY_STALE")
    if not ctx.python_desk_healthy:
        errors.append("DESK_UNHEALTHY")
    if not ctx.watchdog_healthy:
        errors.append("WATCHDOG_UNHEALTHY")
    ident = evaluate_identity(c)
    qi = evaluate_qty_instrument(c)
    errors.extend(ident.get("errors") or [])
    errors.extend(qi.get("errors") or [])
    if not c.connected or not c.trade_enabled:
        errors.append("ACCOUNT_NOT_TRADE_ENABLED")
    if c.account_age_sec is None or float(c.account_age_sec) > 60:
        errors.append("STALE_MCP_STATE")
    if c.equity is None:
        errors.append("EQUITY_UNKNOWN")
    if c.balance is None:
        errors.append("BALANCE_UNKNOWN")
    if c.mll is None:
        errors.append("MLL_UNKNOWN")
    if c.remaining_dd is None or float(c.remaining_dd) < float(UNIT_RISK_USD):
        errors.append("REMAINING_DD_UNSAFE")
    if not c.position_known or c.position_qty != 0:
        errors.append("ACCOUNT_NOT_FLAT")
    if int(c.working_orders or 0) > 0:
        errors.append("WORKING_ORDER_PRESENT")
    if ctx.orphan_protective:
        errors.append("ORPHAN_PROTECTIVE")
    if ctx.unexpected_other_position:
        errors.append("UNEXPECTED_POSITION")
    if c.recon_status != PROP_FLAT_SAFE:
        errors.append("UNSAFE_RECONCILIATION")
    if str(c.policy_verdict or "").upper() not in {"ALLOW", "APPROVED"}:
        errors.append("POLICY_BLOCKED")
    if ctx.daily_lockout:
        errors.append("DAILY_LOCKOUT")
    if not ctx.frozen_hash_match or c.signal and str((c.signal or {}).get("strategy_hash") or NQ_FROZEN_HASH) != NQ_FROZEN_HASH:
        if not ctx.frozen_hash_match:
            errors.append("FROZEN_HASH_MISMATCH")
    if not ctx.frozen_config_match:
        errors.append("FROZEN_CONFIG_MISMATCH")
    if not ctx.strategy_source_unmodified:
        errors.append("STRATEGY_SOURCE_MODIFIED")
    if not ctx.policy_hash_expected:
        errors.append("POLICY_HASH_UNEXPECTED")
    if not ctx.oif_incoming_writable:
        errors.append("OIF_ROUTE_UNAVAILABLE")
    if ctx.stale_oif_replay:
        errors.append("STALE_OIF_REPLAY")
    if not ctx.apprise_configured or not ctx.last_notify_ok or not c.notifications_healthy:
        errors.append("NOTIFICATIONS_UNHEALTHY")
    if daily_latch_used() or _load_persist().get("locked_for_day"):
        errors.append("DAILY_LATCH_USED")
    uniq: list[str] = []
    for e in errors:
        if e not in uniq:
            uniq.append(e)
    ok = not uniq
    return {
        "ok": ok,
        "verdict": "UNATTENDED_PREFLIGHT_PASS" if ok else "UNATTENDED_PREFLIGHT_FAIL",
        "errors": uniq,
        "PROP_EXECUTION": False,
        "account": CANARY_NT_ACCOUNT,
        "qty": CANARY_QTY,
    }


def jit_revalidate(ctx: UnattendedContext) -> dict[str, Any]:
    """Just-in-time re-read. Do not trust hours-old preflight."""
    pf = evaluate_preflight(ctx)
    live = evaluate_automated_phase_55b(ctx)
    errors = list(pf["errors"]) + list(live["errors"])
    c = ctx.canary
    if not c.nt_connected:
        errors.append("NT_DISCONNECTED")
    if daily_latch_used():
        errors.append("DAILY_LATCH_USED")
    if not ctx.watchdog_healthy:
        errors.append("WATCHDOG_UNHEALTHY")
    uniq: list[str] = []
    for e in errors:
        if e not in uniq:
            uniq.append(e)
    return {"ok": not uniq, "errors": uniq}


def broker_protection_survival() -> dict[str, Any]:
    """Structural proof: protective orders are NT-native OCO, not Python-held."""
    from nq_dvp_nt_exec import frozen_risk_for_direction
    from prop_canary_nt_exec import plan_canary_bracket

    stop_pts, tgt_pts = frozen_risk_for_direction("LONG")
    plan = plan_canary_bracket(direction="LONG", trade_id="SURVIVAL", stop_points=stop_pts, target_points=tgt_pts)
    kids = child_oifs_from_fill(plan, 24800.0)
    validate_canary_oif_line(kids["stop_line"])
    validate_canary_oif_line(kids["target_line"])
    crash_open = crash_surface(kind="python_engine", position_open_flag=True, stop_working=True)
    crash_desk = crash_surface(kind="dashboard", position_open_flag=True, stop_working=True)
    crash_tg = crash_surface(kind="telegram", position_open_flag=True, stop_working=True)
    native = (
        "STOPMARKET" in kids["stop_line"]
        and "LIMIT" in kids["target_line"]
        and parse_oif_account(kids["stop_line"]) == CANARY_NT_ACCOUNT
        and kids["stop_qty"] == 1
        and kids["stop_instrument"] == EXEC_INSTRUMENT_NT
        and SIM101_ACCOUNT not in kids["stop_line"]
        and plan["mechanism"] == "OIF_FILL_THEN_OCO_CHILDREN"
        and crash_open["cancel_stop"] is False
        and crash_desk["cancel_stop"] is False
        and crash_tg["cancel_stop"] is False
        and crash_open["broker_native_stop_survives"] is True
    )
    return {
        "ok": native,
        "verdict": "BROKER_PROTECTIVE_ORDER_SURVIVAL_PASS" if native else "BROKER_PROTECTIVE_ORDER_SURVIVAL_NOT_PROVEN",
        "mechanism": plan["mechanism"],
        "stop_line": kids["stop_line"],
        "target_line": kids["target_line"],
        "stop_account": kids["stop_account"],
        "python_held_stop": False,
        "crash_does_not_cancel": True,
        "PROP_EXECUTION": False,
    }


def enable(ctx: UnattendedContext) -> dict[str, Any]:
    """Operator activation for the current trading day. Not an order."""
    mem = _ensure_mem()
    if not unattended_flag_enabled():
        _set_state(UNATTENDED_DISABLED, reason="UNATTENDED_FLAG_DISABLED")
        return {"ok": False, "state": UNATTENDED_DISABLED, "errors": ["UNATTENDED_FLAG_DISABLED"], "PROP_EXECUTION": False}
    persist = _load_persist()
    if persist.get("entry_attempt_used") or persist.get("locked_for_day"):
        _set_state(UNATTENDED_BLOCKED, reason="DAILY_LATCH_USED", notify="UNATTENDED_BLOCKED", body="Daily latch already used")
        return {"ok": False, "state": UNATTENDED_BLOCKED, "errors": ["DAILY_LATCH_USED"], "PROP_EXECUTION": False}
    pf = evaluate_preflight(ctx)
    if not pf["ok"]:
        _set_state(UNATTENDED_BLOCKED, reason="UNATTENDED_PREFLIGHT_FAIL", notify="UNATTENDED_PREFLIGHT_FAIL", body="; ".join(pf["errors"]))
        return {"ok": False, "state": UNATTENDED_BLOCKED, "verdict": "UNATTENDED_PREFLIGHT_FAIL", "errors": pf["errors"], "PROP_EXECUTION": False}
    mem["enabled"] = True
    mem["blocked"] = False
    mem["blocked_reason"] = None
    mem["preflight_ok"] = True
    mem["readiness_at"] = ctx.now or _utc_now()
    if ctx.now is not None:
        mem["clock"] = ctx.now
    _save_persist(
        trading_day=_trading_day(ctx.now),
        operator_enabled_day=_trading_day(ctx.now),
        was_enabled=True,
        restart_block=False,
        entry_attempt_used=False,
        locked_for_day=False,
        last_state=UNATTENDED_PREFLIGHT,
    )
    _set_state(UNATTENDED_PREFLIGHT, notify="UNATTENDED_PREFLIGHT_PASS", body="UNATTENDED_PREFLIGHT_PASS")
    live = evaluate_automated_phase_55b(ctx)
    if live["ok"]:
        mem["phase_55b"] = True
        _notify("LIVE_BAR_VALIDATION_PASS", "AUTOMATED_PHASE_55B_PASS")
        if in_session(ctx.now):
            _set_state(UNATTENDED_WAITING_DVP, notify="UNATTENDED_WAITING_DVP", body="Waiting for genuine phase54_live DVP")
        else:
            _set_state(UNATTENDED_WAITING_SESSION)
    else:
        _set_state(UNATTENDED_WAITING_LIVE_DATA)
    return {
        "ok": True,
        "state": mem["state"],
        "verdict": "UNATTENDED_PREFLIGHT_PASS",
        "phase_55b": live,
        "PROP_EXECUTION": False,
        "note": "Enable is not an order. System waits for live data / session / genuine DVP.",
    }


def _block(reason: str, *, persist_lock: bool = True) -> dict[str, Any]:
    mem = _ensure_mem()
    mem["enabled"] = False
    mem["blocked"] = True
    if persist_lock:
        _save_persist(locked_for_day=True, lock_reason=reason, restart_block=True, trading_day=_trading_day())
    _set_state(UNATTENDED_BLOCKED, reason=reason, notify="UNATTENDED_BLOCKED", body=reason)
    return {"ok": False, "state": UNATTENDED_BLOCKED, "reason": reason, "submitted": False, "PROP_EXECUTION": False}


def _expected_lifecycle(plan: dict[str, Any]) -> ExpectedLifecycle:
    return ExpectedLifecycle(
        account_id=CANARY_NT_ACCOUNT,
        instrument=EXEC_INSTRUMENT_NT,
        contract_month="09-26",
        entry_action=str(plan["action"]),
        protective_action=str(plan["exit_action"]),
        quantity=CANARY_QTY,
        entry_order_id=str(plan["entry_order_id"]),
        oco_id=str(plan["oco_id"]),
        correlation_id=str(plan["trade_id"]),
    )


def structured_fixture_acknowledgements(
    plan: dict[str, Any], *, fill_qty: int = CANARY_QTY, now: Optional[datetime] = None
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Synthetic broker evidence for isolated dry runs; never a live acknowledgement."""
    ts = (now or _utc_now()).astimezone(timezone.utc).isoformat()
    base = {
        "account_id": CANARY_NT_ACCOUNT,
        "instrument": EXEC_INSTRUMENT_NT,
        "contract_month": "09-26",
        "quantity": CANARY_QTY,
        "correlation_id": str(plan["trade_id"]),
        "broker_event_timestamp": ts,
        "local_receipt_timestamp": ts,
        "source": EXPECTED_SOURCE,
    }
    entry = {
        **base, "ack_type": "ENTRY_FILL", "action": str(plan["action"]),
        "filled_quantity": fill_qty, "broker_order_id": str(plan["entry_order_id"]),
        "parent_entry_id": str(plan["entry_order_id"]), "oco_id": None,
        "order_state": "FILLED" if fill_qty == CANARY_QTY else "PARTFILLED",
    }
    stop = {
        **base, "ack_type": "STOP_ACKNOWLEDGED", "action": str(plan["exit_action"]),
        "quantity": fill_qty, "filled_quantity": 0,
        "broker_order_id": str(plan["stop_order_id"]),
        "parent_entry_id": str(plan["entry_order_id"]), "oco_id": str(plan["oco_id"]),
        "order_state": "WORKING",
    }
    target = {
        **base, "ack_type": "TARGET_ACKNOWLEDGED", "action": str(plan["exit_action"]),
        "quantity": fill_qty, "filled_quantity": 0,
        "broker_order_id": str(plan["target_order_id"]),
        "parent_entry_id": str(plan["entry_order_id"]), "oco_id": str(plan["oco_id"]),
        "order_state": "WORKING",
    }
    return entry, stop, target


def confirm_protection(ctx: UnattendedContext, plan: dict[str, Any], fill: float) -> dict[str, Any]:
    lifecycle = ProtectionLifecycle(_expected_lifecycle(plan))
    now = ctx.now or _utc_now()
    entry_result = lifecycle.apply(ctx.entry_ack, now=now)
    if not entry_result.get("ok") or ctx.protection_timeout:
        _notify("UNATTENDED_PROTECTION_FAILURE", "ENTRY_ACK_INVALID", critical=True)
        _ensure_mem()["critical"] = True
        return {"ok": False, "verdict": "PROTECTION_FAILURE_CRITICAL", "state": lifecycle.state, "errors": lifecycle.errors}
    kids = child_oifs_from_fill(plan, fill)
    validate_canary_oif_line(kids["stop_line"])
    if ctx.fill_qty != CANARY_QTY:
        return {"ok": False, "verdict": "PROTECTION_FAILURE_CRITICAL", "reason": "QTY_MISMATCH"}
    stop_result = lifecycle.apply(ctx.stop_ack, now=now)
    target_result = lifecycle.apply(ctx.target_ack, now=now)
    if not stop_result.get("ok") or not target_result.get("ok") or lifecycle.state != PROTECTED_CONFIRMED:
        _notify("UNATTENDED_PROTECTION_FAILURE", "STRUCTURED_PROTECTION_UNCONFIRMED", critical=True)
        _ensure_mem()["critical"] = True
        return {
            "ok": False, "verdict": "PROTECTION_FAILURE_CRITICAL",
            "state": lifecycle.state, "protected": lifecycle.protected,
            "errors": lifecycle.errors, "escalation_required": lifecycle.escalation_required,
        }
    mem = _ensure_mem()
    mem["stop_confirmed"] = lifecycle.stop_valid
    mem["target_confirmed"] = lifecycle.target_valid
    _notify("UNATTENDED_STOP_CONFIRMED", f"STOPMARKET working · {kids['stop_price']}")
    _notify("UNATTENDED_TARGET_CONFIRMED", f"LIMIT working · {kids['target_price']}")
    return {
        "ok": True, "children": kids, "verdict": "PROTECTION_CONFIRMED",
        "state": lifecycle.state, "protected": lifecycle.protected,
        "acknowledgements": {"entry": ctx.entry_ack, "stop": ctx.stop_ack, "target": ctx.target_ack},
    }


def attempt_entry(
    ctx: UnattendedContext,
    *,
    transmit: bool = False,
    transmitter: Optional[TransmitFn] = None,
) -> dict[str, Any]:
    mem = _ensure_mem()
    if daily_latch_used():
        return {"ok": False, "submitted": False, "error_code": "DAILY_LATCH_USED", "state": mem["state"], "PROP_EXECUTION": False}
    if mem.get("state") not in {UNATTENDED_WAITING_DVP, UNATTENDED_ENTRY_PENDING}:
        return {"ok": False, "submitted": False, "error_code": "NOT_WAITING_DVP", "state": mem["state"], "PROP_EXECUTION": False}
    if not in_session(ctx.now):
        return {"ok": False, "submitted": False, "error_code": "OUTSIDE_SESSION", "state": mem["state"], "PROP_EXECUTION": False}
    sig_eval = evaluate_signal(ctx.canary, require_newer_than_arm=False)
    floor = mem.get("readiness_at")
    from prop_canary import _parse_ts

    ts = _parse_ts((ctx.canary.signal or {}).get("ts"))
    if floor is not None:
        floor_ts = floor if isinstance(floor, datetime) else _parse_ts(floor)
        if ts is None or (floor_ts and ts <= floor_ts):
            sig_eval = {"ok": False, "errors": list(sig_eval.get("errors") or []) + ["DVP_BEFORE_READINESS"]}
    jit = jit_revalidate(ctx)
    if not jit["ok"] or not sig_eval.get("ok"):
        reason = (jit["errors"] or sig_eval.get("errors") or ["JIT_FAIL"])[0]
        return _block(reason)
    _notify("UNATTENDED_DVP_DETECTED", f"Genuine phase54_live {(ctx.canary.signal or {}).get('direction')}")
    direction = str((ctx.canary.signal or {}).get("direction") or "")
    trade_id = "AITRADE_UNATTENDED_" + str((ctx.canary.signal or {}).get("signal_id") or uuid.uuid4().hex[:10])
    payload = _build_payload(ctx.canary, direction=direction, trade_id=trade_id)
    _set_state(UNATTENDED_ENTRY_PENDING)
    tx = transmitter or drop_canary_oif_lines
    # Crossing the broker boundary burns the day latch even if the attempt fails.
    try:
        result = tx([payload["entry_line"]], transmit=transmit)
    except Exception as exc:
        burn_daily_latch("OIF_EXCEPTION")
        _notify("UNATTENDED_ORDER_REJECTED", str(exc), critical=True)
        return {**_block("OIF_WRITE_EXCEPTION"), "error_code": "OIF_WRITE_EXCEPTION"}
    burn_daily_latch("ENTRY_ATTEMPT")
    submitted = bool(result.get("submitted") or result.get("transmitted"))
    rejected = (not result.get("ok", True)) or str(result.get("status") or "").upper() in {"REJECTED", "ORDER_REJECTED"}
    if transmit:
        _notify("UNATTENDED_ORDER_SUBMITTED", f"{direction} 1 MNQ")
    if rejected:
        _notify("UNATTENDED_ORDER_REJECTED", str(result.get("status") or "rejected"))
        return {**_block("ORDER_REJECTED"), "result": result, "payload": payload}
    if ctx.fill_qty != CANARY_QTY:
        return {**_block("PARTIAL_FILL"), "result": result, "payload": payload}
    if not transmit:
        # Dry-run: simulate fill + native OCO + exit + flat + lock.
        _notify("UNATTENDED_ORDER_ACCEPTED", "dry-run simulated accept · not a broker ack")
        _notify("UNATTENDED_POSITION_OPENED", "dry-run simulated fill")
        prot = confirm_protection(ctx, payload["plan"], ctx.entry_fill_price)
        if not prot.get("ok"):
            return {**_block("PROTECTION_FAILURE_CRITICAL"), "payload": payload, "protection": prot}
        _set_state(UNATTENDED_POSITION_OPEN)
        _set_state(UNATTENDED_EXIT_PENDING)
        _notify("UNATTENDED_POSITION_CLOSED", "dry-run simulated flat")
        mem["position_open"] = False
        mem["complete"] = True
        _set_state(UNATTENDED_COMPLETE, notify="UNATTENDED_COMPLETE", body="One attempt used · locked for day")
        _save_persist(locked_for_day=True, lock_reason="COMPLETE", entry_attempt_used=True)
        return {
            "ok": True,
            "submitted": False,
            "transmitted": False,
            "verdict": "UNATTENDED_DRY_RUN_PASS",
            "payload": payload,
            "protection": prot,
            "state": UNATTENDED_COMPLETE,
            "PROP_EXECUTION": False,
            "broker_ack": None,
        }
    _notify("UNATTENDED_ORDER_ACCEPTED", trade_id)
    _notify("UNATTENDED_POSITION_OPENED", f"fill qty {ctx.fill_qty}")
    mem["in_flight"] = True
    mem["position_open"] = True
    mem["last_trade_id"] = trade_id
    prot = confirm_protection(ctx, payload["plan"], ctx.entry_fill_price)
    if not prot.get("ok"):
        from prop_canary import emergency_flatten

        emergency_flatten(transmit=transmit, transmitter=transmitter)
        return {**_block("PROTECTION_FAILURE_CRITICAL"), "payload": payload, "protection": prot}
    _set_state(UNATTENDED_POSITION_OPEN)
    return {
        "ok": True,
        "submitted": True,
        "transmitted": True,
        "state": UNATTENDED_POSITION_OPEN,
        "payload": payload,
        "protection": prot,
        "PROP_EXECUTION": False,
    }


def tick(
    ctx: UnattendedContext,
    *,
    transmit: bool = False,
    transmitter: Optional[TransmitFn] = None,
    allow_entry: bool = True,
) -> dict[str, Any]:
    """Progress unattended mode. Fail closed. Never silently resume after serious failure."""
    mem = _ensure_mem()
    if ctx.now is not None:
        mem["clock"] = ctx.now
    if not unattended_flag_enabled():
        mem["state"] = UNATTENDED_DISABLED
        return {"state": UNATTENDED_DISABLED, "PROP_EXECUTION": False}
    persist = _load_persist()
    if persist.get("restart_block") and mem.get("state") == UNATTENDED_BLOCKED_RESTART:
        return {"state": UNATTENDED_BLOCKED_RESTART, "reason": "operator revalidation required", "PROP_EXECUTION": False}
    if not mem.get("enabled"):
        return {"state": mem.get("state") or UNATTENDED_DISABLED, "PROP_EXECUTION": False}
    if persist.get("entry_attempt_used") and mem.get("state") not in {UNATTENDED_POSITION_OPEN, UNATTENDED_EXIT_PENDING}:
        if mem.get("state") != UNATTENDED_COMPLETE:
            _set_state(UNATTENDED_COMPLETE)
        return {"state": mem["state"], "PROP_EXECUTION": False}

    wd = watchdog_observe(
        WatchdogObservation(
            engine_state=ctx.engine_state,
            engine_intentional_stop=ctx.engine_intentional_stop,
            engine_heartbeat_age_sec=ctx.engine_heartbeat_age_sec,
            desk_healthy=ctx.python_desk_healthy,
            nt_telemetry_age_sec=ctx.telemetry_age_sec,
            nq_bar_age_sec=0.0 if ctx.bars_fresh else 999.0,
            mcp_age_sec=ctx.canary.account_age_sec,
            canary_state=str(mem.get("state")),
            position_side=ctx.canary.position_side,
            position_qty=ctx.canary.position_qty if mem.get("position_open") else 0,
            position_known=ctx.canary.position_known,
            stop_working=mem.get("stop_confirmed") or False,
            target_working=mem.get("target_confirmed") or False,
            recon=ctx.canary.recon_status,
            notifications_healthy=ctx.canary.notifications_healthy,
            account=ctx.canary.requested_account or CANARY_NT_ACCOUNT,
        )
    )
    if "BLOCK_DAY" in wd["actions"] and not mem.get("position_open"):
        return _block(str(wd.get("reason") or "WATCHDOG_BLOCK"))
    if "EMERGENCY_FLATTEN" in wd["actions"]:
        _notify("UNATTENDED_PROTECTION_FAILURE", str(wd.get("reason")), critical=True)
        return _block("PROTECTION_FAILURE")
    if "UNATTENDED_ENGINE_LOST_POSITION_OPEN" in wd.get("alerts", []):
        _notify("UNATTENDED_ENGINE_LOST_POSITION_OPEN", "Broker-native stop must remain", critical=True)

    if ctx.canary.nt_connected is False and not mem.get("position_open"):
        return _block("NT_DISCONNECT_WHILE_FLAT")
    if ctx.canary.nt_connected is False and mem.get("position_open"):
        _notify("UNATTENDED_BLOCKED", "NT disconnect while OPEN · monitoring only · no second trade")
        return {"state": UNATTENDED_POSITION_OPEN, "note": "reconnect is recon only", "PROP_EXECUTION": False}

    live = evaluate_automated_phase_55b(ctx)
    state = mem.get("state")
    if state == UNATTENDED_WAITING_LIVE_DATA:
        if live["ok"]:
            mem["phase_55b"] = True
            _notify("LIVE_BAR_VALIDATION_PASS", "AUTOMATED_PHASE_55B_PASS")
            if in_session(ctx.now):
                _set_state(UNATTENDED_WAITING_DVP, notify="UNATTENDED_WAITING_DVP", body="Waiting for genuine phase54_live")
            else:
                _set_state(UNATTENDED_WAITING_SESSION)
        return {"state": mem["state"], "phase_55b": live, "PROP_EXECUTION": False}

    if state == UNATTENDED_WAITING_SESSION:
        if session_closed_no_entry(ctx.now):
            _save_persist(locked_for_day=True, lock_reason="NO_VALID_DVP_EVENT", entry_attempt_used=False)
            mem["no_trade"] = True
            mem["complete"] = True
            mem["enabled"] = False
            _set_state(UNATTENDED_COMPLETE_NO_TRADE, reason="NO_VALID_DVP_EVENT", notify="UNATTENDED_COMPLETE_NO_TRADE", body="NO_VALID_DVP_EVENT")
            return {"state": UNATTENDED_COMPLETE_NO_TRADE, "PROP_EXECUTION": False}
        if in_session(ctx.now) and live["ok"]:
            _set_state(UNATTENDED_WAITING_DVP, notify="UNATTENDED_WAITING_DVP", body="Session open")
        return {"state": mem["state"], "PROP_EXECUTION": False}

    if state == UNATTENDED_WAITING_DVP:
        if session_closed_no_entry(ctx.now):
            _save_persist(locked_for_day=True, lock_reason="NO_VALID_DVP_EVENT")
            mem["no_trade"] = True
            mem["complete"] = True
            mem["enabled"] = False
            _set_state(UNATTENDED_COMPLETE_NO_TRADE, reason="NO_VALID_DVP_EVENT", notify="UNATTENDED_COMPLETE_NO_TRADE", body="NO_VALID_DVP_EVENT")
            return {"state": UNATTENDED_COMPLETE_NO_TRADE, "PROP_EXECUTION": False}
        if ctx.canary.signal and allow_entry:
            return attempt_entry(ctx, transmit=transmit, transmitter=transmitter)
        return {"state": UNATTENDED_WAITING_DVP, "PROP_EXECUTION": False}

    return {"state": mem.get("state"), "watchdog": wd, "PROP_EXECUTION": False}


def unattended_dry_run(ctx: UnattendedContext) -> dict[str, Any]:
    """Full lifecycle without OIF write."""
    reset_note = None
    en = enable(ctx)
    if not en.get("ok"):
        return {"ok": False, "verdict": "UNATTENDED_DRY_RUN_FAIL", "errors": en.get("errors"), "PROP_EXECUTION": False}
    # Force session + live + genuine signal newer than readiness.
    now = ctx.now or datetime(2026, 8, 24, 14, 45, tzinfo=NY)
    mem = _ensure_mem()
    mem["state"] = UNATTENDED_WAITING_DVP
    floor = mem.get("readiness_at") or now
    if isinstance(floor, datetime):
        sig_ts = (floor + timedelta(seconds=5)).isoformat()
    else:
        sig_ts = _iso(now)
    ctx2 = replace(
        ctx,
        now=now,
        canary=replace(ctx.canary, signal=genuine_signal(ts=sig_ts, signal_id="unattended-dry")),
    )
    fixture_plan = _build_payload(
        ctx2.canary, direction="LONG", trade_id="AITRADE_UNATTENDED_unattended-dry"
    )["plan"]
    entry_ack, stop_ack, target_ack = structured_fixture_acknowledgements(
        fixture_plan, fill_qty=ctx2.fill_qty, now=now
    )
    ctx2 = replace(ctx2, entry_ack=entry_ack, stop_ack=stop_ack, target_ack=target_ack)
    out = attempt_entry(ctx2, transmit=False)
    second = attempt_entry(ctx2, transmit=False)
    survival = broker_protection_survival()
    ok = (
        out.get("verdict") == "UNATTENDED_DRY_RUN_PASS"
        and second.get("error_code") in {"DAILY_LATCH_USED", None}
        and (second.get("error_code") == "DAILY_LATCH_USED" or second.get("reason") == "DAILY_LATCH_USED" or not second.get("ok"))
        and not out.get("transmitted")
        and out.get("payload", {}).get("account") == CANARY_NT_ACCOUNT
        and out.get("payload", {}).get("quantity") == 1
        and SIM101_ACCOUNT not in str(out.get("payload", {}).get("entry_line"))
        and survival.get("ok")
    )
    return {
        "ok": ok,
        "verdict": "UNATTENDED_DRY_RUN_PASS" if ok else "UNATTENDED_DRY_RUN_FAIL",
        "first": out,
        "second_blocked": not second.get("ok"),
        "protection": survival,
        "PROP_EXECUTION": False,
        "transmitted": False,
        "note": reset_note,
    }


def disable(reason: str = "OPERATOR") -> dict[str, Any]:
    mem = _ensure_mem()
    mem["enabled"] = False
    if mem.get("state") not in {UNATTENDED_COMPLETE, UNATTENDED_COMPLETE_NO_TRADE, UNATTENDED_BLOCKED, UNATTENDED_BLOCKED_RESTART}:
        _set_state(UNATTENDED_DISABLED, reason=reason)
    return {"ok": True, "state": mem.get("state"), "PROP_EXECUTION": False}


def public_snapshot(ctx: Optional[UnattendedContext] = None) -> dict[str, Any]:
    mem = _ensure_mem()
    persist = _load_persist()
    return {
        "mode": "UNATTENDED_PROP_CANARY",
        "state": mem.get("state") or UNATTENDED_DISABLED,
        "flag": unattended_flag_enabled(),
        "enabled": bool(mem.get("enabled")),
        "preflight_ok": bool(mem.get("preflight_ok")),
        "automated_phase_55b": bool(mem.get("phase_55b")),
        "daily_attempt_used": bool(persist.get("entry_attempt_used")),
        "locked_for_day": bool(persist.get("locked_for_day")),
        "account": CANARY_NT_ACCOUNT,
        "platform_login": CANARY_LOGIN,
        "fundednext_account_id": CANARY_ACCOUNT_ID,
        "qty_cap": CANARY_QTY,
        "signal_instrument": SIGNAL_INSTRUMENT,
        "execution_instrument": EXEC_INSTRUMENT_DISPLAY,
        "session": f"{TRADE_START_LOCAL}–{NO_NEW_TRADES_AFTER_LOCAL} ET",
        "stop_confirmed": bool(mem.get("stop_confirmed")),
        "target_confirmed": bool(mem.get("target_confirmed")),
        "watchdog": "HEALTHY" if (ctx.watchdog_healthy if ctx else True) else "UNHEALTHY",
        "general_prop": "LOCKED",
        "PROP_EXECUTION": False,
        "sim101": False,
        "blocked_reason": mem.get("blocked_reason"),
        "last_change": mem.get("last_change"),
        "remaining_live_gate": "AUTOMATED_PHASE_55B_PASS",
        "recon": (ctx.canary.recon_status if ctx else None),
        "mll": (ctx.canary.mll if ctx else None),
        "remaining_dd": (ctx.canary.remaining_dd if ctx else None),
        "position": (ctx.canary.position_side if ctx else "FLAT"),
        "market_live": (ctx.canary.market_live if ctx else False),
    }


def context_from_ops_snapshot(snap: dict[str, Any]) -> UnattendedContext:
    from prop_canary import context_from_ops_snapshot as canary_from_snap

    c = canary_from_snap(snap)
    dump = snap.get("telemetry_dump") if isinstance(snap.get("telemetry_dump"), dict) else {}
    live = snap.get("live_dvp") if isinstance(snap.get("live_dvp"), dict) else {}
    ntf = snap.get("notifications") if isinstance(snap.get("notifications"), dict) else {}
    mdq = str(snap.get("market_data_quality") or "").upper()
    return UnattendedContext(
        canary=c,
        nt_process_available=bool((dump.get("alive") if dump else False) or c.nt_connected),
        addon_schema=str((snap.get("market") or {}).get("schema") or dump.get("schema") or ADDON_SCHEMA),
        telemetry_age_sec=float(dump.get("age_sec") or 999.0),
        python_desk_healthy=True,
        nq_bars_1m_status=str(dump.get("nq_bars_1m_status") or "WAITING"),
        nq_1m_count=int(dump.get("nq_bars_1m_count") or 0),
        nq_1m_count_prev=0,
        market_quality=mdq or "UNKNOWN",
        delayed_feed=mdq in {"DELAYED", "SIMULATED", "PLAYBACK"},
        bars_fresh=bool(dump.get("last_nq_bar_ts")) and c.market_live,
        agg_5m_advancing=bool(live.get("agg_5m_healthy") or c.agg_5m_healthy),
        agg_15m_advancing=bool(live.get("agg_15m_healthy") or c.agg_15m_healthy),
        apprise_configured=bool(ntf.get("configured")),
        last_notify_ok=str(ntf.get("delivery_status") or "").upper() in {"HEALTHY", "READY", "OK", "LIVE"},
        engine_state=str(snap.get("engine") or "STOPPED"),
        frozen_hash_match=bool((snap.get("hashes") or {}).get("nq_match", True)),
    )
