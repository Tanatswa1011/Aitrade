"""Structured, fail-closed broker acknowledgement validation for Phase 55D."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping, Optional

ACK_FRESH_SEC = 15.0
FUTURE_SKEW_SEC = 2.0
EXPECTED_SOURCE = "NINJATRADER_FUNDEDNEXT"

ENTRY_REQUESTED = "ENTRY_REQUESTED"
ENTRY_ACKNOWLEDGED = "ENTRY_ACKNOWLEDGED"
ENTRY_PARTIALLY_FILLED = "ENTRY_PARTIALLY_FILLED"
ENTRY_FILLED = "ENTRY_FILLED"
PROTECTION_PENDING = "PROTECTION_PENDING"
STOP_ACKNOWLEDGED = "STOP_ACKNOWLEDGED"
TARGET_ACKNOWLEDGED = "TARGET_ACKNOWLEDGED"
PROTECTED_CONFIRMED = "PROTECTED_CONFIRMED"
PROTECTION_UNKNOWN = "PROTECTION_UNKNOWN"
PROTECTION_REJECTED = "PROTECTION_REJECTED"
FLATTEN_REQUESTED = "FLATTEN_REQUESTED"
FLATTEN_ACKNOWLEDGED = "FLATTEN_ACKNOWLEDGED"
FLAT_CONFIRMED = "FLAT_CONFIRMED"
DISARMED_FAILURE = "DISARMED_FAILURE"

ENTRY_TYPES = {"ENTRY_ACKNOWLEDGED", "ENTRY_FILL", "ENTRY_PARTIAL_FILL"}
STOP_TYPES = {"STOP_ACKNOWLEDGED", "STOP_REJECTED", "STOP_CANCELLED"}
TARGET_TYPES = {"TARGET_ACKNOWLEDGED", "TARGET_REJECTED", "TARGET_CANCELLED"}
PROTECTIVE_TYPES = STOP_TYPES | TARGET_TYPES
KNOWN_TYPES = ENTRY_TYPES | PROTECTIVE_TYPES | {
    "EXIT_FILL", "FLATTEN_ACKNOWLEDGED", "POSITION_FLAT_CONFIRMED"
}


def _time(raw: Any) -> Optional[datetime]:
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=timezone.utc)
    try:
        value = str(raw or "")
        if value.endswith("Z"):
            value = value[:-1] + "+00:00"
        parsed = datetime.fromisoformat(value)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True)
class ExpectedLifecycle:
    account_id: str
    instrument: str
    contract_month: str
    entry_action: str
    protective_action: str
    quantity: int
    entry_order_id: str
    oco_id: str
    correlation_id: str
    source: str = EXPECTED_SOURCE


@dataclass
class ProtectionLifecycle:
    expected: ExpectedLifecycle
    state: str = ENTRY_REQUESTED
    filled_quantity: int = 0
    stop_quantity: int = 0
    target_quantity: int = 0
    entry_known: bool = False
    stop_valid: bool = False
    target_valid: bool = False
    connected: bool = True
    escalation_required: bool = False
    disarmed: bool = True
    flatten_requested: bool = False
    flatten_acknowledged: bool = False
    flat_confirmed: bool = False
    errors: list[str] = field(default_factory=list)
    _seen: set[tuple[str, str, str]] = field(default_factory=set)

    @property
    def protected(self) -> bool:
        return self.state == PROTECTED_CONFIRMED and self._coverage_complete()

    def _coverage_complete(self) -> bool:
        return (
            self.entry_known and self.filled_quantity > 0
            and self.stop_valid and self.target_valid
            and self.stop_quantity == self.filled_quantity
            and self.target_quantity == self.filled_quantity
        )

    def _fail(self, reason: str, *, rejected: bool = False) -> dict[str, Any]:
        if reason not in self.errors:
            self.errors.append(reason)
        self.stop_valid = False if reason.startswith("STOP_") or reason in {"CONNECTION_LOST", "ACK_STALE"} else self.stop_valid
        self.target_valid = False if reason.startswith("TARGET_") or reason in {"CONNECTION_LOST", "ACK_STALE"} else self.target_valid
        self.state = PROTECTION_REJECTED if rejected else PROTECTION_UNKNOWN
        self.escalation_required = self.filled_quantity > 0 or rejected
        self.disarmed = True
        return self.snapshot(ok=False)

    def _validate(self, ack: Any, now: datetime) -> tuple[Optional[dict[str, Any]], list[str]]:
        if not isinstance(ack, Mapping):
            return None, ["ACK_MALFORMED"]
        doc = dict(ack)
        required = (
            "ack_type", "account_id", "instrument", "contract_month", "action",
            "quantity", "filled_quantity", "broker_order_id", "parent_entry_id",
            "correlation_id", "order_state", "broker_event_timestamp",
            "local_receipt_timestamp", "source",
        )
        errors = [f"ACK_MISSING_{key.upper()}" for key in required if doc.get(key) in (None, "")]
        kind = str(doc.get("ack_type") or "").upper()
        if kind not in KNOWN_TYPES:
            errors.append("ACK_TYPE_UNKNOWN")
        if doc.get("account_id") != self.expected.account_id:
            errors.append("ACK_ACCOUNT_MISMATCH")
        if doc.get("instrument") != self.expected.instrument:
            errors.append("ACK_INSTRUMENT_MISMATCH")
        if doc.get("contract_month") != self.expected.contract_month:
            errors.append("ACK_CONTRACT_MISMATCH")
        if doc.get("correlation_id") != self.expected.correlation_id:
            errors.append("ACK_CORRELATION_MISMATCH")
        if doc.get("source") != self.expected.source:
            errors.append("ACK_SOURCE_MISMATCH")
        if kind in PROTECTIVE_TYPES:
            if doc.get("parent_entry_id") != self.expected.entry_order_id:
                errors.append("ACK_PARENT_MISMATCH")
            if doc.get("oco_id") != self.expected.oco_id:
                errors.append("ACK_OCO_MISMATCH")
            if str(doc.get("action") or "").upper() != self.expected.protective_action:
                errors.append("ACK_ACTION_MISMATCH")
        elif kind in ENTRY_TYPES and str(doc.get("action") or "").upper() != self.expected.entry_action:
            errors.append("ACK_ACTION_MISMATCH")
        for key in ("quantity", "filled_quantity"):
            value = doc.get(key)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0 or value > self.expected.quantity:
                errors.append("ACK_QUANTITY_MISMATCH")
        event_ts = _time(doc.get("broker_event_timestamp"))
        receipt_ts = _time(doc.get("local_receipt_timestamp"))
        if event_ts is None or receipt_ts is None:
            errors.append("ACK_TIMESTAMP_INVALID")
        else:
            event_age = (now.astimezone(timezone.utc) - event_ts.astimezone(timezone.utc)).total_seconds()
            receipt_age = (now.astimezone(timezone.utc) - receipt_ts.astimezone(timezone.utc)).total_seconds()
            if event_age < -FUTURE_SKEW_SEC or receipt_age < -FUTURE_SKEW_SEC:
                errors.append("ACK_FUTURE_TIMESTAMP")
            if event_age > ACK_FRESH_SEC or receipt_age > ACK_FRESH_SEC:
                errors.append("ACK_STALE")
            if event_ts > receipt_ts:
                errors.append("ACK_EVENT_ORDER_INVALID")
        key = (kind, str(doc.get("broker_order_id") or ""), str(doc.get("broker_event_timestamp") or ""))
        if key in self._seen:
            errors.append("ACK_DUPLICATE")
        return doc, errors

    def apply(self, ack: Any, *, now: Optional[datetime] = None) -> dict[str, Any]:
        current = now or datetime.now(timezone.utc)
        doc, errors = self._validate(ack, current)
        if errors:
            # A duplicate is idempotently rejected without erasing already proven protection.
            if errors == ["ACK_DUPLICATE"]:
                if "ACK_DUPLICATE" not in self.errors:
                    self.errors.append("ACK_DUPLICATE")
                return self.snapshot(ok=False)
            return self._fail(errors[0])
        assert doc is not None
        kind = str(doc["ack_type"]).upper()
        key = (kind, str(doc["broker_order_id"]), str(doc["broker_event_timestamp"]))
        self._seen.add(key)
        qty = int(doc["quantity"])
        filled = int(doc["filled_quantity"])
        state = str(doc["order_state"]).upper()

        if kind in ENTRY_TYPES:
            if state not in {"ACCEPTED", "WORKING", "PARTFILLED", "FILLED"}:
                return self._fail("ENTRY_STATE_INVALID")
            if kind == "ENTRY_ACKNOWLEDGED":
                self.entry_known = True
                self.state = ENTRY_ACKNOWLEDGED
            else:
                if filled <= 0 or filled < self.filled_quantity:
                    return self._fail("FILL_RECONCILIATION_FAILED")
                self.entry_known = True
                self.filled_quantity = filled
                self.state = ENTRY_FILLED if filled == self.expected.quantity else ENTRY_PARTIALLY_FILLED
                if self.stop_quantity < filled or self.target_quantity < filled:
                    self.state = PROTECTION_PENDING
                    self.escalation_required = True
            return self.snapshot(ok=True)

        if kind in PROTECTIVE_TYPES:
            if not self.entry_known or self.filled_quantity <= 0:
                return self._fail("ACK_OUT_OF_ORDER")
            if qty != self.filled_quantity or filled not in {0, self.filled_quantity}:
                return self._fail("ACK_QUANTITY_MISMATCH")
            if kind.endswith("REJECTED") or kind.endswith("CANCELLED"):
                return self._fail(kind, rejected=True)
            if state not in {"ACCEPTED", "WORKING"}:
                return self._fail("PROTECTIVE_STATE_INVALID")
            if kind == "STOP_ACKNOWLEDGED":
                self.stop_valid, self.stop_quantity = True, qty
                self.state = STOP_ACKNOWLEDGED
            else:
                self.target_valid, self.target_quantity = True, qty
                self.state = TARGET_ACKNOWLEDGED
            if self._coverage_complete():
                self.state = PROTECTED_CONFIRMED
                self.escalation_required = False
            return self.snapshot(ok=True)

        if kind == "FLATTEN_ACKNOWLEDGED":
            if not self.flatten_requested:
                return self._fail("FLATTEN_ACK_OUT_OF_ORDER")
            self.flatten_acknowledged = True
            self.state = FLATTEN_ACKNOWLEDGED
            return self.snapshot(ok=True)
        if kind == "POSITION_FLAT_CONFIRMED":
            if not self.flatten_acknowledged or state != "FLAT":
                return self._fail("FLAT_NOT_AUTHORITATIVE")
            self.flat_confirmed = True
            self.filled_quantity = 0
            self.state = FLAT_CONFIRMED
            return self.snapshot(ok=True)
        if kind == "EXIT_FILL":
            self.state = PROTECTION_PENDING
            return self.snapshot(ok=True)
        return self._fail("ACK_TYPE_UNKNOWN")

    def connection_lost(self) -> dict[str, Any]:
        self.connected = False
        return self._fail("CONNECTION_LOST")

    def connection_restored(self) -> dict[str, Any]:
        self.connected = True
        self.disarmed = True
        self.state = PROTECTION_UNKNOWN
        if "MANUAL_RECONCILIATION_REQUIRED" not in self.errors:
            self.errors.append("MANUAL_RECONCILIATION_REQUIRED")
        return self.snapshot(ok=False)

    def request_flatten(self) -> dict[str, Any]:
        self.flatten_requested = True
        self.state = FLATTEN_REQUESTED
        self.disarmed = True
        return self.snapshot(ok=True)

    def snapshot(self, *, ok: bool) -> dict[str, Any]:
        return {
            "ok": ok,
            "state": self.state,
            "protected": self.protected,
            "filled_quantity": self.filled_quantity,
            "stop_quantity": self.stop_quantity,
            "target_quantity": self.target_quantity,
            "escalation_required": self.escalation_required,
            "disarmed": self.disarmed,
            "flatten_requested": self.flatten_requested,
            "flatten_acknowledged": self.flatten_acknowledged,
            "flat_confirmed": self.flat_confirmed,
            "errors": list(self.errors),
            "PROP_EXECUTION": False,
        }
