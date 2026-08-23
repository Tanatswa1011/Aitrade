"""Local, single-use authorization envelope for the Phase 55D Monday canary.

This module grants permission to run preflight only.  It cannot enable general
prop execution and it never creates an OIF.  Production state is deliberately
separate from both canary persistence files.

Two clocks are bound into every envelope:

* ``claim_valid_until`` — short-lived claim lease, max 45 minutes after
  ``issued_at``.  After CONSUMED it is no longer the runtime expiry.
* ``session_valid_until`` — frozen no-new-entry deadline for the authorized
  session date, derived from NQ DVP session configuration (not operator input).
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

from fundednext_mcp_oauth import _current_windows_sid, _restrict_windows_acl
from nq_drift_vwap_models import NO_NEW_TRADES_AFTER_LOCAL, OR_TIMEZONE, TRADE_START_LOCAL

ROOT = Path(__file__).resolve().parent
COMMAND = "AUTHORIZE_PHASE55D_ONE_SHOT_CANARY"
EXPECTED_ACCOUNT = "FNFTCHTANATSWAPHILMU92044"
SIGNAL_CONTRACT = "NQ 09-26"
EXECUTION_CONTRACT = "MNQ 09-26"
SESSION_DATE = "2026-08-24"
MAX_CLAIM_LIFETIME_SEC = 45 * 60
MAX_LIFETIME_SEC = MAX_CLAIM_LIFETIME_SEC  # backward-compatible alias
DEFAULT_PATH = ROOT / "state" / "phase55d_session_authorization.json"


def _path() -> Path:
    from test_workspace import mutable_path, test_mode, test_root
    if test_mode():
        if not os.environ.get("AITRADE_TEST_ROOT"):
            raise RuntimeError("authoritative_test_root_required")
        override = os.environ.get("AITRADE_PHASE55D_AUTH_STATE")
        p = Path(override).resolve() if override else mutable_path("state", "phase55d_session_authorization.json")
        root = test_root()
        if p != root and root not in p.parents:
            raise RuntimeError("test_path_escaped_workspace")
        return p
    return DEFAULT_PATH


def _now(now: Optional[datetime] = None) -> datetime:
    """Authoritative clock. Production is timezone-aware UTC; tests may inject ``now``."""
    if now is None:
        return datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError("AUTH_TIMESTAMP_NAIVE")
    return now.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("AUTH_TIMESTAMP_NAIVE")
    return value.astimezone(timezone.utc).isoformat()


def _parse_utc(raw: Any) -> datetime:
    """Parse a timezone-aware UTC timestamp. Naive or malformed values raise."""
    parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("AUTH_TIMESTAMP_NAIVE")
    return parsed.astimezone(timezone.utc)


def frozen_session_window(session_date: str = SESSION_DATE) -> tuple[datetime, datetime]:
    """UTC start/end of the frozen NQ DVP new-entry window for ``session_date``."""
    tz = ZoneInfo(OR_TIMEZONE)
    if OR_TIMEZONE != "America/New_York":
        raise RuntimeError("SESSION_TIMEZONE_MISMATCH")
    if TRADE_START_LOCAL != "10:30" or NO_NEW_TRADES_AFTER_LOCAL != "15:30":
        raise RuntimeError("SESSION_WINDOW_MISMATCH")
    d = date.fromisoformat(session_date)
    sh, sm = (int(x) for x in TRADE_START_LOCAL.split(":"))
    eh, em = (int(x) for x in NO_NEW_TRADES_AFTER_LOCAL.split(":"))
    start = datetime(d.year, d.month, d.day, sh, sm, tzinfo=tz).astimezone(timezone.utc)
    end = datetime(d.year, d.month, d.day, eh, em, tzinfo=tz).astimezone(timezone.utc)
    return start, end


def session_entry_start_utc(session_date: str = SESSION_DATE) -> datetime:
    return frozen_session_window(session_date)[0]


def session_valid_until_utc(session_date: str = SESSION_DATE) -> datetime:
    return frozen_session_window(session_date)[1]


def _read() -> dict[str, Any]:
    try:
        doc = json.loads(_path().read_text(encoding="utf-8"))
        return doc if isinstance(doc, dict) else {}
    except (OSError, ValueError):
        return {}


def _write(doc: dict[str, Any]) -> None:
    p = _path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(doc, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, p)
    if os.name == "nt":
        _restrict_windows_acl(p)


def expected_payload(
    *,
    claim_valid_until: Optional[str] = None,
    valid_until: Optional[str] = None,
    issued_at: Optional[str] = None,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """Bound Monday payload. Session bounds come from frozen DVP, never the operator."""
    cur = _now(now)
    issued = issued_at or _iso(cur)
    claim = claim_valid_until or valid_until or _iso(cur + timedelta(seconds=MAX_CLAIM_LIFETIME_SEC))
    start, end = frozen_session_window(SESSION_DATE)
    return {
        "command": COMMAND,
        "expected_account": EXPECTED_ACCOUNT,
        "signal_contract": SIGNAL_CONTRACT,
        "execution_contract": EXECUTION_CONTRACT,
        "maximum_quantity": 1,
        "session_date": SESSION_DATE,
        "authorized_session_date": SESSION_DATE,
        "issued_at": issued,
        "claim_valid_until": claim,
        "valid_until": claim,  # compatibility: claim lease only, never session end
        "session_entry_start": _iso(start),
        "session_valid_until": _iso(end),
        "require_all_live_gates": True,
        "synthetic_replayed_signals_forbidden": True,
        "second_attempt_forbidden": True,
        "automatic_disarm_required": True,
    }


def validate_payload(
    payload: dict[str, Any],
    *,
    now: Optional[datetime] = None,
    for_claim: bool = True,
) -> list[str]:
    try:
        cur = _now(now)
    except ValueError:
        return ["AUTH_TIMESTAMP_NAIVE"]
    errors: list[str] = []
    claim = str(payload.get("claim_valid_until") or payload.get("valid_until") or "")
    issued = str(payload.get("issued_at") or "")
    expected = expected_payload(claim_valid_until=claim, issued_at=issued or None, now=cur)
    for key, value in expected.items():
        if key in {"claim_valid_until", "valid_until", "issued_at"}:
            continue
        if payload.get(key) != value:
            errors.append("AUTH_PAYLOAD_MISMATCH:" + key)
    if payload.get("valid_until") not in {None, "", claim}:
        if str(payload.get("valid_until")) != claim:
            errors.append("AUTH_PAYLOAD_MISMATCH:valid_until")
    try:
        start = _parse_utc(expected["session_entry_start"])
        end = _parse_utc(expected["session_valid_until"])
        frozen_start, frozen_end = frozen_session_window(SESSION_DATE)
        if start != frozen_start or end != frozen_end:
            errors.append("AUTH_SESSION_WINDOW_MISMATCH")
    except (TypeError, ValueError):
        errors.append("AUTH_SESSION_WINDOW_MISMATCH")
    if str(payload.get("authorized_session_date") or payload.get("session_date") or "") != SESSION_DATE:
        errors.append("AUTH_PAYLOAD_MISMATCH:authorized_session_date")
    try:
        claim_at = _parse_utc(claim)
        issued_at = _parse_utc(issued) if issued else cur
    except (TypeError, ValueError):
        errors.append("AUTH_EXPIRY_MALFORMED")
        return errors
    if issued_at > cur + timedelta(seconds=1):
        errors.append("AUTH_ISSUED_AT_FUTURE")
    if claim_at <= issued_at:
        errors.append("AUTH_EXPIRED")
    if (claim_at - issued_at).total_seconds() > MAX_CLAIM_LIFETIME_SEC + 1:
        errors.append("AUTH_NOT_SHORT_LIVED")
    if for_claim:
        if claim_at <= cur:
            errors.append("AUTH_EXPIRED")
        if (claim_at - cur).total_seconds() > MAX_CLAIM_LIFETIME_SEC:
            errors.append("AUTH_NOT_SHORT_LIVED")
    try:
        session_end = _parse_utc(payload.get("session_valid_until") or expected["session_valid_until"])
    except (TypeError, ValueError):
        errors.append("AUTH_SESSION_WINDOW_MISMATCH")
        return errors
    if session_end <= issued_at:
        errors.append("AUTH_SESSION_EXPIRED")
    return errors


def issue_request(payload: dict[str, Any], *, now: Optional[datetime] = None, authorization_id: Optional[str] = None) -> dict[str, Any]:
    try:
        cur = _now(now)
    except ValueError:
        return {"ok": False, "status": "REJECTED", "errors": ["AUTH_TIMESTAMP_NAIVE"], "PROP_EXECUTION": False}
    # issued_at is stamped from the issuance clock. Operator/CLI input is ignored.
    incoming_claim = str(payload.get("claim_valid_until") or payload.get("valid_until") or "")
    bound = expected_payload(
        claim_valid_until=incoming_claim,
        issued_at=_iso(cur),
        now=cur,
    )
    conflicts: list[str] = []
    for key in ("session_entry_start", "session_valid_until"):
        raw = payload.get(key)
        if raw in (None, ""):
            continue
        try:
            if _parse_utc(raw) != _parse_utc(bound[key]):
                conflicts.append("AUTH_SESSION_EXPIRY_CONFLICT")
        except (TypeError, ValueError):
            conflicts.append("AUTH_SESSION_WINDOW_MISMATCH")
    for key, expected in (("authorized_session_date", SESSION_DATE), ("session_date", SESSION_DATE)):
        raw = payload.get(key)
        if raw not in (None, "", expected):
            conflicts.append("AUTH_PAYLOAD_MISMATCH:" + key)
    if conflicts:
        return {"ok": False, "status": "REJECTED", "errors": conflicts, "PROP_EXECUTION": False}
    # Operator cannot override frozen session bounds.
    incoming = dict(payload)
    incoming.pop("issued_at", None)
    incoming.update({
        "session_entry_start": bound["session_entry_start"],
        "session_valid_until": bound["session_valid_until"],
        "authorized_session_date": SESSION_DATE,
        "session_date": SESSION_DATE,
        "issued_at": bound["issued_at"],
        "claim_valid_until": bound["claim_valid_until"],
        "valid_until": bound["claim_valid_until"],
    })
    try:
        errors = validate_payload(incoming, now=cur, for_claim=True)
    except ValueError:
        return {"ok": False, "status": "REJECTED", "errors": ["AUTH_TIMESTAMP_NAIVE"], "PROP_EXECUTION": False}
    sid = _current_windows_sid() if os.name == "nt" else "NON_WINDOWS_TEST_PRINCIPAL"
    if not sid:
        errors.append("PROCESS_SID_UNAVAILABLE")
    previous = _read()
    if previous and previous.get("status") in {"PENDING", "ACCEPTED", "CONSUMED"}:
        errors.append("AUTH_DUPLICATE_OR_REUSED")
    auth_id = authorization_id or secrets.token_urlsafe(24)
    if len(auth_id) < 24:
        errors.append("AUTH_ID_TOO_SHORT")
    if errors:
        return {"ok": False, "status": "REJECTED", "errors": errors, "PROP_EXECUTION": False}
    doc = {
        "schema": "AITRADE_PHASE55D_SESSION_AUTH_V2",
        "authorization_id": auth_id,
        "status": "PENDING",
        "issued_at_utc": bound["issued_at"],
        "requester_sid": sid,
        "payload": incoming,
        "runtime_pid": None,
        "consumed_at_utc": None,
        "audit": [{"event": "AUTHORIZATION_REQUESTED", "at_utc": _iso(cur)}],
        "PROP_EXECUTION": False,
    }
    _write(doc)
    return {
        "ok": True,
        "status": "PENDING_PREFLIGHT",
        "authorization_id": auth_id,
        "issued_at": incoming["issued_at"],
        "claim_valid_until": incoming["claim_valid_until"],
        "session_entry_start": incoming["session_entry_start"],
        "session_valid_until": incoming["session_valid_until"],
        "PROP_EXECUTION": False,
    }


def reject_pending(reason: str, *, now: Optional[datetime] = None) -> None:
    doc = _read()
    if doc.get("status") == "PENDING":
        doc["status"] = "REJECTED"
        doc.setdefault("audit", []).append({"event": "AUTHORIZATION_REJECTED", "at_utc": _iso(_now(now)), "errors": [reason]})
        _write(doc)


def expire_unclaimed(*, now: Optional[datetime] = None) -> bool:
    """PENDING envelopes whose claim lease has elapsed become EXPIRED."""
    try:
        cur = _now(now)
    except ValueError:
        return False
    doc = _read()
    if doc.get("status") != "PENDING":
        return False
    payload = doc.get("payload") or {}
    try:
        claim_at = _parse_utc(payload.get("claim_valid_until") or payload.get("valid_until"))
    except (TypeError, ValueError):
        doc["status"] = "EXPIRED"
        doc.setdefault("audit", []).append({"event": "CLAIM_LEASE_EXPIRED", "at_utc": _iso(cur)})
        _write(doc)
        return True
    if claim_at <= cur:
        doc["status"] = "EXPIRED"
        doc.setdefault("audit", []).append({"event": "CLAIM_LEASE_EXPIRED", "at_utc": _iso(cur)})
        _write(doc)
        return True
    return False


def expire_session(*, now: Optional[datetime] = None) -> bool:
    """CONSUMED envelopes past session_valid_until become EXPIRED."""
    try:
        cur = _now(now)
    except ValueError:
        return False
    doc = _read()
    if doc.get("status") != "CONSUMED":
        return False
    payload = doc.get("payload") or {}
    try:
        session_end = _parse_utc(payload.get("session_valid_until"))
    except (TypeError, ValueError):
        doc["status"] = "EXPIRED"
        doc.setdefault("audit", []).append({"event": "SESSION_PERMISSION_EXPIRED", "at_utc": _iso(cur)})
        _write(doc)
        return True
    if session_end <= cur:
        doc["status"] = "EXPIRED"
        doc.setdefault("audit", []).append({"event": "SESSION_PERMISSION_EXPIRED", "at_utc": _iso(cur)})
        _write(doc)
        return True
    return False


def claim_for_runtime(*, now: Optional[datetime] = None) -> dict[str, Any]:
    try:
        cur = _now(now)
    except ValueError:
        return {"ok": False, "error": "AUTH_TIMESTAMP_NAIVE", "status": "REJECTED"}
    expire_unclaimed(now=cur)
    doc = _read()
    if doc.get("status") != "PENDING":
        return {"ok": False, "error": "NO_PENDING_AUTHORIZATION", "status": doc.get("status")}
    errors = validate_payload(doc.get("payload") or {}, now=cur, for_claim=True)
    sid = _current_windows_sid() if os.name == "nt" else "NON_WINDOWS_TEST_PRINCIPAL"
    if sid != doc.get("requester_sid"):
        errors.append("AUTH_PRINCIPAL_MISMATCH")
    if errors:
        doc["status"] = "EXPIRED" if "AUTH_EXPIRED" in errors else "REJECTED"
        doc["audit"].append({"event": "AUTHORIZATION_REJECTED", "at_utc": _iso(cur), "errors": errors})
        _write(doc)
        return {"ok": False, "error": errors[0], "errors": errors, "status": doc["status"]}
    doc["status"] = "ACCEPTED"
    doc["runtime_pid"] = os.getpid()
    doc["accepted_at_utc"] = _iso(cur)
    doc["audit"].append({"event": "AUTHORIZATION_ACCEPTED_PREFLIGHT_STARTED", "at_utc": _iso(cur)})
    _write(doc)
    return {"ok": True, "authorization_id": doc["authorization_id"], "payload": doc["payload"]}


def mark_consumed(gates: list[dict[str, Any]], *, now: Optional[datetime] = None) -> dict[str, Any]:
    try:
        cur = _now(now)
    except ValueError:
        return {"ok": False, "error": "AUTH_TIMESTAMP_NAIVE"}
    doc = _read()
    if doc.get("status") != "ACCEPTED" or doc.get("runtime_pid") != os.getpid():
        return {"ok": False, "error": "AUTH_NOT_CLAIMED_BY_RUNTIME"}
    if not gates or not all(bool(g.get("ok")) for g in gates):
        doc["status"] = "REJECTED"
        doc["audit"].append({"event": "PREFLIGHT_BLOCKED", "at_utc": _iso(cur), "gates": gates})
        _write(doc)
        return {"ok": False, "error": "PREFLIGHT_BLOCKED"}
    doc["status"] = "CONSUMED"
    doc["consumed_at_utc"] = _iso(cur)
    doc["runtime_pid"] = os.getpid()
    doc["audit"].append({"event": "AUTHORIZATION_CONSUMED", "at_utc": _iso(cur), "gates": gates})
    _write(doc)
    return {"ok": True, "authorization_id": doc["authorization_id"]}


def runtime_permission_active(*, now: Optional[datetime] = None) -> bool:
    """Post-consume session permission. Claim lease is not consulted."""
    try:
        cur = _now(now)
    except ValueError:
        return False
    doc = _read()
    if doc.get("status") != "CONSUMED" or doc.get("runtime_pid") != os.getpid():
        return False
    payload = doc.get("payload") or {}
    expected = expected_payload(
        claim_valid_until=str(payload.get("claim_valid_until") or payload.get("valid_until") or ""),
        issued_at=str(payload.get("issued_at") or ""),
        now=cur,
    )
    for key, value in expected.items():
        if key in {"claim_valid_until", "valid_until", "issued_at"}:
            continue
        if payload.get(key) != value:
            return False
    try:
        session_end = _parse_utc(payload.get("session_valid_until") or expected["session_valid_until"])
        session_date = str(payload.get("authorized_session_date") or payload.get("session_date") or "")
    except (TypeError, ValueError):
        return False
    if session_date != SESSION_DATE:
        return False
    return session_end > cur


def invalidate_on_restart(*, now: Optional[datetime] = None) -> None:
    try:
        cur = _now(now)
    except ValueError:
        return
    doc = _read()
    if doc.get("status") in {"PENDING", "ACCEPTED", "CONSUMED"}:
        doc["status"] = "INVALIDATED_RESTART"
        doc["runtime_pid"] = None
        doc.setdefault("audit", []).append({"event": "AUTHORIZATION_INVALIDATED_RESTART", "at_utc": _iso(cur)})
        _write(doc)


def status(*, now: Optional[datetime] = None) -> dict[str, Any]:
    from test_workspace import test_mode
    if now is not None or not test_mode():
        expire_unclaimed(now=now)
        expire_session(now=now)
    doc = _read()
    payload = doc.get("payload") or {}
    return {
        "schema": doc.get("schema"),
        "status": doc.get("status", "NOT_AUTHORIZED"),
        "authorization_id": doc.get("authorization_id"),
        "issued_at_utc": doc.get("issued_at_utc"),
        "consumed_at_utc": doc.get("consumed_at_utc"),
        "runtime_bound": doc.get("runtime_pid") == os.getpid(),
        "claim_valid_until": payload.get("claim_valid_until"),
        "session_entry_start": payload.get("session_entry_start"),
        "session_valid_until": payload.get("session_valid_until"),
        "authorized_session_date": payload.get("authorized_session_date"),
        "PROP_EXECUTION": False,
    }


def _resolve_claim_until(args: argparse.Namespace) -> str:
    claim = getattr(args, "claim_valid_until", None)
    legacy = getattr(args, "valid_until", None)
    if claim and legacy and claim != legacy:
        raise SystemExit("conflicting --claim-valid-until and --valid-until")
    value = claim or legacy
    if not value:
        raise SystemExit("--claim-valid-until is required (legacy --valid-until is claim-only)")
    return value


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("status")
    auth = sub.add_parser(COMMAND)
    auth.add_argument(
        "--claim-valid-until",
        dest="claim_valid_until",
        help="UTC claim lease deadline (max 45 minutes). Does not set session end.",
    )
    auth.add_argument(
        "--valid-until",
        dest="valid_until",
        help="Deprecated alias of --claim-valid-until. Never overrides frozen session_valid_until.",
    )
    args = parser.parse_args()
    if args.action == "status":
        print(json.dumps(status(), indent=2))
        return 0
    # Bind claim lease to one issuance-clock sample. issued_at is never CLI input.
    cur = _now(None)
    result = issue_request(
        expected_payload(claim_valid_until=_resolve_claim_until(args), now=cur),
        now=cur,
    )
    if result.get("ok"):
        try:
            req = urllib.request.Request("http://127.0.0.1:8765/control/start", data=b"", method="POST")
            with urllib.request.urlopen(req, timeout=30) as response:
                start = json.load(response)
            result["runtime_start"] = start
            if start.get("ok") is not True:
                reject_pending("SAFE_START_BLOCKED")
                result.update(ok=False, status="BLOCKED", errors=["SAFE_START_BLOCKED"])
        except (OSError, ValueError, urllib.error.URLError) as exc:
            reject_pending("RUNTIME_UNAVAILABLE")
            result.update(ok=False, status="BLOCKED", errors=["RUNTIME_UNAVAILABLE"], detail=str(exc)[:160])
    print(json.dumps(result, indent=2))
    return 0 if result.get("ok") else 2


# Compatibility aliases for existing Phase 55D tests and callers.
expected_payload = expected_payload
issue_request = issue_request
validate_payload = validate_payload
mark_consumed = mark_consumed
claim_for_runtime = claim_for_runtime


if __name__ == "__main__":
    raise SystemExit(main())
