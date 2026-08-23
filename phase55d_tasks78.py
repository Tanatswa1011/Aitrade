"""Isolated Phase 55D Tasks 7/8 alert and recovery validation harness."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from aitrade_notifications import EventType, NotificationEvent, Severity, build_event

DRY_RUN_PREFIX = "[AITRADE PHASE 55D DRY RUN]"
ACCOUNT = "FNFTCHTANATSWAPHILMU92044"
INSTRUMENT = "MNQ 09-26"
QUANTITY = 1

ALERT_SCENARIOS: dict[str, tuple[EventType, Severity]] = {
    "runtime_started": (EventType.ENGINE_START, Severity.INFO),
    "runtime_stopped": (EventType.ENGINE_STOP, Severity.INFO),
    "ninjatrader_disconnected": (EventType.NINJATRADER_DISCONNECTED, Severity.CRITICAL),
    "fundednext_auth_failed": (EventType.SAFE_START_FAILED, Severity.WARNING),
    "market_data_stale": (EventType.MARKET_DATA_STALE, Severity.WARNING),
    "valid_fixture_dvp": (EventType.LIVE_DVP_DETECTED, Severity.INFO),
    "canary_order_requested": (EventType.UNATTENDED_ORDER_SUBMITTED, Severity.INFO),
    "broker_ack_received": (EventType.UNATTENDED_ORDER_ACCEPTED, Severity.INFO),
    "protective_stop_confirmed": (EventType.UNATTENDED_STOP_CONFIRMED, Severity.INFO),
    "protective_target_confirmed": (EventType.UNATTENDED_TARGET_CONFIRMED, Severity.INFO),
    "stop_loss_triggered": (EventType.POSITION_CLOSED, Severity.INFO),
    "target_reached": (EventType.POSITION_CLOSED, Severity.INFO),
    "emergency_flatten_requested": (EventType.EMERGENCY_FLATTEN, Severity.CRITICAL),
    "canary_completed_disarmed": (EventType.UNATTENDED_COMPLETE, Severity.INFO),
    "second_attempt_blocked": (EventType.UNATTENDED_BLOCKED, Severity.WARNING),
    "unexpected_engine_failure": (EventType.ENGINE_UNEXPECTED_EXIT, Severity.CRITICAL),
}


def build_dry_run_alert(name: str, *, correlation_id: str) -> NotificationEvent:
    if name not in ALERT_SCENARIOS:
        raise ValueError(f"unknown_dry_run_alert:{name}")
    event_type, expected_severity = ALERT_SCENARIOS[name]
    body = "\n".join((
        f"{DRY_RUN_PREFIX} SIMULATED EVENT — NO TRADE",
        f"Account: {ACCOUNT}", f"Instrument: {INSTRUMENT}", f"Quantity: {QUANTITY}",
        "Engine state: DISARMED", "Canary state: DISARMED",
        f"Event: {name}", f"Correlation: {correlation_id}",
    ))
    event = build_event(
        event_type,
        title=f"{DRY_RUN_PREFIX} {name.replace('_', ' ').upper()}",
        body=body,
        account=ACCOUNT,
        instrument=INSTRUMENT,
        provenance="PHASE55D_ISOLATED_FIXTURE",
        metadata={
            "correlation_id": correlation_id,
            "quantity": QUANTITY,
            "engine_state": "DISARMED",
            "canary_state": "DISARMED",
            "simulated": True,
            "delivery_claim": "CONSTRUCTED_ONLY",
        },
    )
    if event.severity != expected_severity:
        raise RuntimeError("severity_mapping_mismatch")
    return event


SAFE_STATES = {"DISARMED", "BLOCKED", "MANUAL_REVALIDATION_REQUIRED", "COMPLETE"}


@dataclass
class RecoveryHarness:
    state_path: Path
    state: str = "DISARMED"
    prop_execution: bool = False
    one_shot_used: bool = False
    serious_failure_latch: bool = False
    market_fresh: bool = False
    risk_fresh: bool = False
    authenticated: bool = False
    nt_connected: bool = False
    position_reconciled: bool = False
    orders_reconciled: bool = False
    protected_confirmed: bool = False
    flat_confirmed: bool = False
    reasons: list[str] = field(default_factory=list)

    def save(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        doc = {
            "schema": "PHASE55D_RECOVERY_FIXTURE_V1", "state": self.state,
            "PROP_EXECUTION": False, "one_shot_used": self.one_shot_used,
            "serious_failure_latch": self.serious_failure_latch,
        }
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(doc, sort_keys=True), encoding="utf-8")
        tmp.replace(self.state_path)

    def load(self) -> None:
        try:
            doc = json.loads(self.state_path.read_text(encoding="utf-8"))
            if not isinstance(doc, dict) or doc.get("schema") != "PHASE55D_RECOVERY_FIXTURE_V1":
                raise ValueError("unknown_schema")
            persisted = str(doc.get("state") or "")
            if persisted not in SAFE_STATES or doc.get("PROP_EXECUTION") is not False:
                raise ValueError("unsafe_persisted_state")
            self.one_shot_used = bool(doc.get("one_shot_used"))
            self.serious_failure_latch = bool(doc.get("serious_failure_latch"))
            self.state = "MANUAL_REVALIDATION_REQUIRED" if self.serious_failure_latch else "DISARMED"
        except (OSError, ValueError, json.JSONDecodeError):
            self.state = "BLOCKED"
            self.serious_failure_latch = True
            self.reasons.append("PERSISTED_STATE_INVALID")
        self.prop_execution = False

    def restart(self, component: str) -> None:
        self.state = "MANUAL_REVALIDATION_REQUIRED" if self.serious_failure_latch else "DISARMED"
        self.prop_execution = False
        self.market_fresh = False
        self.risk_fresh = False
        self.authenticated = False
        self.position_reconciled = False
        self.orders_reconciled = False
        self.protected_confirmed = False
        self.flat_confirmed = False
        self.reasons.append(f"{component.upper()}_RESTART_REVALIDATION_REQUIRED")

    def interrupt(self, dependency: str, *, serious: bool = False) -> None:
        setattr(self, {
            "market": "market_fresh", "risk": "risk_fresh", "auth": "authenticated",
            "ninjatrader": "nt_connected",
        }[dependency], False)
        self.state = "MANUAL_REVALIDATION_REQUIRED" if serious else "BLOCKED"
        self.serious_failure_latch = self.serious_failure_latch or serious
        self.prop_execution = False
        self.reasons.append(f"{dependency.upper()}_UNAVAILABLE")

    def restore(self, dependency: str) -> None:
        setattr(self, {
            "market": "market_fresh", "risk": "risk_fresh", "auth": "authenticated",
            "ninjatrader": "nt_connected",
        }[dependency], True)
        # Dependency recovery never clears a failure latch or arms execution.
        self.state = "MANUAL_REVALIDATION_REQUIRED" if self.serious_failure_latch else "BLOCKED"
        self.prop_execution = False
        self.reasons.append(f"{dependency.upper()}_RESTORED_DISARMED")

    def serious_failure(self, reason: str) -> None:
        self.serious_failure_latch = True
        self.state = "MANUAL_REVALIDATION_REQUIRED"
        self.prop_execution = False
        self.protected_confirmed = False
        self.flat_confirmed = False
        self.reasons.append(reason)

    def snapshot(self) -> dict[str, Any]:
        return {
            "state": self.state, "PROP_EXECUTION": self.prop_execution,
            "one_shot_used": self.one_shot_used,
            "serious_failure_latch": self.serious_failure_latch,
            "protected_confirmed": self.protected_confirmed,
            "flat_confirmed": self.flat_confirmed,
            "reasons": list(self.reasons), "timestamp": datetime.now(timezone.utc).isoformat(),
        }
