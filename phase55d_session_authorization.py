"""Local, single-use authorization envelope for the Phase 55D Monday canary.

This module grants permission to run preflight only.  It cannot enable general
prop execution and it never creates an OIF.  Production state is deliberately
separate from both canary persistence files.
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from fundednext_mcp_oauth import _current_windows_sid, _restrict_windows_acl

ROOT = Path(__file__).resolve().parent
COMMAND = "AUTHORIZE_PHASE55D_ONE_SHOT_CANARY"
EXPECTED_ACCOUNT = "FNFTCHTANATSWAPHILMU92044"
SIGNAL_CONTRACT = "NQ 09-26"
EXECUTION_CONTRACT = "MNQ 09-26"
SESSION_DATE = "2026-08-24"
MAX_LIFETIME_SEC = 45 * 60
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
    value = now or datetime.now(timezone.utc)
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


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


def expected_payload(*, valid_until: str) -> dict[str, Any]:
    return {
        "command": COMMAND,
        "expected_account": EXPECTED_ACCOUNT,
        "signal_contract": SIGNAL_CONTRACT,
        "execution_contract": EXECUTION_CONTRACT,
        "maximum_quantity": 1,
        "session_date": SESSION_DATE,
        "valid_until": valid_until,
        "require_all_live_gates": True,
        "synthetic_replayed_signals_forbidden": True,
        "second_attempt_forbidden": True,
        "automatic_disarm_required": True,
    }


def validate_payload(payload: dict[str, Any], *, now: Optional[datetime] = None) -> list[str]:
    cur = _now(now)
    errors: list[str] = []
    expected = expected_payload(valid_until=str(payload.get("valid_until") or ""))
    for key, value in expected.items():
        if payload.get(key) != value:
            errors.append("AUTH_PAYLOAD_MISMATCH:" + key)
    try:
        expiry = datetime.fromisoformat(str(payload.get("valid_until")).replace("Z", "+00:00")).astimezone(timezone.utc)
    except (TypeError, ValueError):
        errors.append("AUTH_EXPIRY_MALFORMED")
        return errors
    if expiry <= cur:
        errors.append("AUTH_EXPIRED")
    if (expiry - cur).total_seconds() > MAX_LIFETIME_SEC:
        errors.append("AUTH_NOT_SHORT_LIVED")
    return errors


def issue_request(payload: dict[str, Any], *, now: Optional[datetime] = None, authorization_id: Optional[str] = None) -> dict[str, Any]:
    cur = _now(now)
    errors = validate_payload(payload, now=cur)
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
        "schema": "AITRADE_PHASE55D_SESSION_AUTH_V1",
        "authorization_id": auth_id,
        "status": "PENDING",
        "issued_at_utc": _iso(cur),
        "requester_sid": sid,
        "payload": payload,
        "runtime_pid": None,
        "consumed_at_utc": None,
        "audit": [{"event": "AUTHORIZATION_REQUESTED", "at_utc": _iso(cur)}],
        "PROP_EXECUTION": False,
    }
    _write(doc)
    return {"ok": True, "status": "PENDING_PREFLIGHT", "authorization_id": auth_id, "PROP_EXECUTION": False}


def reject_pending(reason: str, *, now: Optional[datetime] = None) -> None:
    doc = _read()
    if doc.get("status") == "PENDING":
        doc["status"] = "REJECTED"
        doc.setdefault("audit", []).append({"event": "AUTHORIZATION_REJECTED", "at_utc": _iso(_now(now)), "errors": [reason]})
        _write(doc)


def claim_for_runtime(*, now: Optional[datetime] = None) -> dict[str, Any]:
    cur = _now(now)
    doc = _read()
    if doc.get("status") != "PENDING":
        return {"ok": False, "error": "NO_PENDING_AUTHORIZATION"}
    errors = validate_payload(doc.get("payload") or {}, now=cur)
    sid = _current_windows_sid() if os.name == "nt" else "NON_WINDOWS_TEST_PRINCIPAL"
    if sid != doc.get("requester_sid"):
        errors.append("AUTH_PRINCIPAL_MISMATCH")
    if errors:
        doc["status"] = "REJECTED"
        doc["audit"].append({"event": "AUTHORIZATION_REJECTED", "at_utc": _iso(cur), "errors": errors})
        _write(doc)
        return {"ok": False, "error": errors[0], "errors": errors}
    doc["status"] = "ACCEPTED"
    doc["runtime_pid"] = os.getpid()
    doc["accepted_at_utc"] = _iso(cur)
    doc["audit"].append({"event": "AUTHORIZATION_ACCEPTED_PREFLIGHT_STARTED", "at_utc": _iso(cur)})
    _write(doc)
    return {"ok": True, "authorization_id": doc["authorization_id"], "payload": doc["payload"]}


def mark_consumed(gates: list[dict[str, Any]], *, now: Optional[datetime] = None) -> dict[str, Any]:
    cur = _now(now)
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
    doc["audit"].append({"event": "AUTHORIZATION_CONSUMED", "at_utc": _iso(cur), "gates": gates})
    _write(doc)
    return {"ok": True, "authorization_id": doc["authorization_id"]}


def runtime_permission_active(*, now: Optional[datetime] = None) -> bool:
    doc = _read()
    if doc.get("status") != "CONSUMED" or doc.get("runtime_pid") != os.getpid():
        return False
    payload = doc.get("payload") or {}
    expected = expected_payload(valid_until=str(payload.get("valid_until") or ""))
    if any(payload.get(k) != v for k, v in expected.items()):
        return False
    try:
        expiry = datetime.fromisoformat(str(payload["valid_until"]).replace("Z", "+00:00")).astimezone(timezone.utc)
    except (KeyError, TypeError, ValueError):
        return False
    return expiry > _now(now)


def invalidate_on_restart(*, now: Optional[datetime] = None) -> None:
    doc = _read()
    if doc.get("status") in {"PENDING", "ACCEPTED", "CONSUMED"}:
        doc["status"] = "INVALIDATED_RESTART"
        doc.setdefault("audit", []).append({"event": "AUTHORIZATION_INVALIDATED_RESTART", "at_utc": _iso(_now(now))})
        _write(doc)


def status() -> dict[str, Any]:
    doc = _read()
    return {
        "schema": doc.get("schema"), "status": doc.get("status", "NOT_AUTHORIZED"),
        "authorization_id": doc.get("authorization_id"), "issued_at_utc": doc.get("issued_at_utc"),
        "consumed_at_utc": doc.get("consumed_at_utc"), "runtime_bound": doc.get("runtime_pid") == os.getpid(),
        "PROP_EXECUTION": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("status")
    auth = sub.add_parser(COMMAND)
    auth.add_argument("--valid-until", required=True)
    args = parser.parse_args()
    if args.action == "status":
        print(json.dumps(status(), indent=2)); return 0
    result = issue_request(expected_payload(valid_until=args.valid_until))
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


if __name__ == "__main__":
    raise SystemExit(main())
