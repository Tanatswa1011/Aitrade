"""Independent unattended watchdog — monitoring and escalation only.

Does not decide entries, does not arm, does not write OIF, does not cancel
broker-native protective orders. Flatten recommendation is FundedNext-only.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

CANARY_NT_ACCOUNT = "FNFTCHTANATSWAPHILMU92044"
SIM101_ACCOUNT = "Sim101"

ENGINE_STOPPED = "STOPPED"
ENGINE_RUNNING = "RUNNING"
ENGINE_ABSENT = "ABSENT"
ENGINE_LOST = "LOST"


@dataclass
class WatchdogObservation:
    engine_state: str = ENGINE_RUNNING
    engine_intentional_stop: bool = False
    engine_heartbeat_age_sec: Optional[float] = 1.0
    desk_healthy: bool = True
    desk_heartbeat_age_sec: Optional[float] = 1.0
    nt_telemetry_age_sec: Optional[float] = 0.5
    nq_bar_age_sec: Optional[float] = 5.0
    mcp_age_sec: Optional[float] = 2.0
    canary_state: str = "UNATTENDED_WAITING_DVP"
    position_side: str = "FLAT"
    position_qty: int = 0
    position_known: bool = True
    stop_working: bool = False
    target_working: bool = False
    recon: str = "PROP_FLAT_SAFE"
    notifications_healthy: bool = True
    account: str = CANARY_NT_ACCOUNT
    stale_engine_sec: float = 20.0
    stale_nt_sec: float = 15.0
    stale_bars_sec: float = 180.0
    stale_mcp_sec: float = 60.0
    extras: dict[str, Any] = field(default_factory=dict)


def position_open(obs: WatchdogObservation) -> bool:
    return bool(obs.position_known and obs.position_qty != 0 and str(obs.position_side or "").upper() not in {"FLAT", ""})


def observe(obs: WatchdogObservation) -> dict[str, Any]:
    """Pure monitor. Never returns cancel_stop / second_entry / sim101 flatten."""
    actions: list[str] = []
    alerts: list[str] = []
    severity = "INFO"
    reason = None
    engine = str(obs.engine_state or "")
    hb = obs.engine_heartbeat_age_sec
    engine_lost = (engine == ENGINE_ABSENT) or (engine == ENGINE_LOST) or (
        engine == ENGINE_RUNNING and hb is not None and float(hb) > float(obs.stale_engine_sec)
    )
    if engine == ENGINE_STOPPED and obs.engine_intentional_stop:
        engine_lost = False
        actions.append("ENGINE_STOPPED_INTENTIONAL")

    open_pos = position_open(obs)
    unknown = not obs.position_known
    nt_stale = obs.nt_telemetry_age_sec is None or float(obs.nt_telemetry_age_sec) > float(obs.stale_nt_sec)
    mcp_stale = obs.mcp_age_sec is None or float(obs.mcp_age_sec) > float(obs.stale_mcp_sec)
    bars_stale = obs.nq_bar_age_sec is None or float(obs.nq_bar_age_sec) > float(obs.stale_bars_sec)

    if obs.account == SIM101_ACCOUNT or str(obs.account).lower().startswith("sim"):
        return {
            "ok": False,
            "verdict": "WATCHDOG_FAIL",
            "reason": "SIM101_INELIGIBLE",
            "actions": ["BLOCK"],
            "alerts": ["UNATTENDED_BLOCKED"],
            "cancel_stop": False,
            "second_entry": False,
            "flatten_account": None,
            "PROP_EXECUTION": False,
        }

    if open_pos:
        if engine_lost:
            severity = "CRITICAL"
            reason = "ENGINE_LOST_POSITION_OPEN"
            alerts.append("UNATTENDED_ENGINE_LOST_POSITION_OPEN")
            actions.append("ALERT")
            # Broker-native stop must remain. Watchdog does not cancel it.
        if unknown:
            severity = "CRITICAL"
            reason = reason or "POSITION_UNKNOWN"
            alerts.append("UNATTENDED_BLOCKED")
            actions.append("ALERT")
        if not obs.stop_working:
            severity = "CRITICAL"
            reason = "PROTECTION_FAILURE"
            alerts.append("UNATTENDED_PROTECTION_FAILURE")
            actions.append("EMERGENCY_FLATTEN")
        if mcp_stale:
            severity = "WARNING" if severity != "CRITICAL" else severity
            reason = reason or "MCP_STALE_WHILE_OPEN"
            alerts.append("UNATTENDED_BLOCKED")
            actions.append("ALERT")
        if nt_stale:
            severity = "WARNING" if severity != "CRITICAL" else severity
            reason = reason or "NT_STALE_WHILE_OPEN"
            actions.append("ALERT")
    else:
        # FLAT: critical dependency failure locks the day. No resume-to-trade.
        if engine_lost and not obs.engine_intentional_stop:
            severity = "WARNING"
            reason = "ENGINE_LOST_WHILE_FLAT"
            actions.append("BLOCK_DAY")
            alerts.append("UNATTENDED_BLOCKED")
        if nt_stale:
            severity = "WARNING"
            reason = reason or "NT_DISCONNECT_WHILE_FLAT"
            actions.append("BLOCK_DAY")
            alerts.append("UNATTENDED_BLOCKED")
        if mcp_stale:
            severity = "WARNING"
            reason = reason or "MCP_STALE_WHILE_WAITING"
            actions.append("BLOCK_DAY")
            alerts.append("UNATTENDED_BLOCKED")
        if bars_stale and "WAITING" in str(obs.canary_state):
            severity = "WARNING"
            reason = reason or "BARS_STALE_WHILE_WAITING"
            actions.append("BLOCK_DAY")
            alerts.append("UNATTENDED_BLOCKED")
        if not obs.desk_healthy:
            actions.append("ALERT")

    if "EMERGENCY_FLATTEN" in actions:
        flatten_account = CANARY_NT_ACCOUNT
    else:
        flatten_account = None

    ok = "BLOCK_DAY" not in actions and "EMERGENCY_FLATTEN" not in actions and reason not in {
        "ENGINE_LOST_POSITION_OPEN",
        "PROTECTION_FAILURE",
        "POSITION_UNKNOWN",
    }
    return {
        "ok": ok,
        "verdict": "WATCHDOG_PASS" if ok or (open_pos and engine_lost and obs.stop_working and "EMERGENCY_FLATTEN" not in actions) else (
            "WATCHDOG_FAIL" if not ok else "WATCHDOG_PASS"
        ),
        "healthy": ok,
        "severity": severity,
        "reason": reason,
        "actions": actions,
        "alerts": alerts,
        "cancel_stop": False,
        "cancel_target": False,
        "second_entry": False,
        "write_oif": False,
        "flatten_account": flatten_account,
        "broker_stop_must_survive": True,
        "open_position": open_pos,
        "engine_lost": engine_lost,
        "PROP_EXECUTION": False,
        "account": CANARY_NT_ACCOUNT,
    }


def crash_surface(*, kind: str, position_open_flag: bool, stop_working: bool) -> dict[str, Any]:
    """What the process is allowed to do on crash. Must never cancel NT OCO children."""
    obs = WatchdogObservation(
        engine_state=ENGINE_ABSENT,
        engine_intentional_stop=False,
        engine_heartbeat_age_sec=999.0,
        position_side="LONG" if position_open_flag else "FLAT",
        position_qty=1 if position_open_flag else 0,
        position_known=True,
        stop_working=stop_working,
        target_working=stop_working and position_open_flag,
        canary_state="UNATTENDED_POSITION_OPEN" if position_open_flag else "UNATTENDED_WAITING_DVP",
    )
    out = observe(obs)
    out["crash_kind"] = kind
    out["cancel_stop"] = False
    out["cancel_target"] = False
    out["second_entry"] = False
    out["broker_native_stop_survives"] = bool(stop_working) if position_open_flag else True
    return out
