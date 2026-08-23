"""Fail-closed validation for the read-only NinjaTrader order snapshot."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

ORDER_STATE_STALE_SEC = 5.0
TERMINAL_ORDER_STATES = frozenset({"CANCELLED", "FILLED", "REJECTED"})
ACTIVE_ORDER_STATES = frozenset({
    "ACCEPTED", "INITIALIZED", "PARTFILLED", "CANCELSUBMITTED",
    "CHANGESUBMITTED", "SUBMITTED", "TRIGGERPENDING", "WORKING",
    "CANCELPENDING", "CHANGEPENDING", "SUSPENDED", "ACCEPTEDBYRISK",
})


def _parse_ts(raw: Any) -> Optional[datetime]:
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def evaluate_order_snapshot(
    doc: Any,
    *,
    expected_account: str,
    expected_contract: str,
    position_flat: bool,
    local_oif_count: int,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    errors: list[str] = []
    if not isinstance(doc, dict):
        return {"ok": False, "errors": ["ORDER_STATE_MISSING"], "age_sec": None}
    now = now or datetime.now(timezone.utc)
    ts = _parse_ts(doc.get("timestamp"))
    age = None
    if ts is None:
        errors.append("ORDER_STATE_MISSING")
    else:
        age = (now.astimezone(timezone.utc) - ts.astimezone(timezone.utc)).total_seconds()
        if age < -2.0 or age > ORDER_STATE_STALE_SEC:
            errors.append("ORDER_STATE_STALE")
    if doc.get("available") is not True or doc.get("collection_available") is not True:
        errors.append("ORDER_STATE_MISSING")
    if str(doc.get("connection_status") or "").upper() != "CONNECTED":
        errors.append("ORDER_STATE_MISSING")
    if str(doc.get("account_id") or "") != expected_account:
        errors.append("ORDER_ACCOUNT_MISMATCH")

    required_counts = ("total_observed", "active_count", "pending_count", "partial_active_count", "orphan_candidate_count", "unknown_count")
    counts: dict[str, int] = {}
    for key in required_counts:
        value = doc.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            errors.append("ORDER_STATE_MISSING")
        else:
            counts[key] = value
    active = doc.get("active_orders")
    if not isinstance(active, list):
        errors.append("ORDER_STATE_MISSING")
        active = []
    if counts.get("active_count") != len(active):
        errors.append("ORDER_STATE_MISSING")
    if counts.get("active_count", 0) > 0:
        errors.append("WORKING_ORDER_PRESENT")
    if counts.get("pending_count", 0) > 0:
        errors.append("WORKING_ORDER_PRESENT")
    if counts.get("partial_active_count", 0) > 0:
        errors.append("PARTIAL_ORDER_REMAINS")
    if counts.get("orphan_candidate_count", 0) > 0:
        errors.append("ORPHAN_ORDER_CANDIDATE")
    if counts.get("unknown_count", 0) > 0:
        errors.append("UNKNOWN_ORDER_STATE")

    for order in active:
        if not isinstance(order, dict):
            errors.append("UNKNOWN_ORDER_STATE")
            continue
        state = str(order.get("state") or "").upper()
        if state not in ACTIVE_ORDER_STATES:
            errors.append("UNKNOWN_ORDER_STATE")
        if str(order.get("account_id") or "") != expected_account:
            errors.append("ORDER_ACCOUNT_MISMATCH")
        if str(order.get("instrument") or "") != expected_contract:
            errors.append("ORDER_POSITION_RECONCILIATION_FAILED")
        qty, filled, remaining = order.get("quantity"), order.get("filled_quantity"), order.get("remaining_quantity")
        if not all(isinstance(x, int) and not isinstance(x, bool) and x >= 0 for x in (qty, filled, remaining)):
            errors.append("UNKNOWN_ORDER_STATE")
        elif remaining != max(0, qty - filled):
            errors.append("ORDER_POSITION_RECONCILIATION_FAILED")
        if state == "PARTFILLED" and remaining > 0:
            errors.append("PARTIAL_ORDER_REMAINS")
        if order.get("potential_orphan") is not False:
            errors.append("ORPHAN_ORDER_CANDIDATE")
        if (order.get("oco_id") or order.get("protective")) and position_flat:
            errors.append("ORPHAN_ORDER_CANDIDATE")
    if not position_flat and counts.get("active_count", 0) == 0:
        errors.append("ORDER_POSITION_RECONCILIATION_FAILED")
    if int(local_oif_count or 0) > 0:
        errors.append("LOCAL_OIF_PENDING")
    uniq = list(dict.fromkeys(errors))
    return {"ok": not uniq, "errors": uniq, "age_sec": age, "counts": counts}
