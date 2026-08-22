"""Phase 54D — Tradovate read-only FundedNext account telemetry.

Never places, cancels, modifies, flattens, or liquidates orders.
Allowlisted HTTP paths only. Tokens are never logged or returned in snapshots.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

UTC = timezone.utc
ROOT = Path(__file__).resolve().parent
DEVICE_PATH = ROOT / "state" / "tradovate_device_id.txt"

ENV_LIVE = "https://live.tradovateapi.com/v1"
ENV_DEMO = "https://demo.tradovateapi.com/v1"

ALLOWED_GET = frozenset({
    "/auth/me",
    "/account/list",
    "/account/find",
    "/position/list",
    "/cashBalance/list",
})
ALLOWED_POST = frozenset({
    "/auth/accesstokenrequest",
    "/cashBalance/getcashbalancesnapshot",
})

FORBIDDEN_METHODS = frozenset({
    "place_order",
    "submit_order",
    "cancel_order",
    "modify_order",
    "flatten",
    "liquidate",
    "close_position",
    "drop_oif",
})

SECRET_KEYS = frozenset({
    "password", "token", "accesstoken", "refreshtoken", "sec", "secret",
    "authorization", "cid",
})

PROP_EXECUTION = False


class TradovateReadOnlyError(RuntimeError):
    pass


class TradovateReadOnlyViolation(RuntimeError):
    """Raised if a non-allowlisted path is requested."""


def _now() -> datetime:
    return datetime.now(tz=UTC)


def _iso(ts: Optional[datetime] = None) -> str:
    return (ts or _now()).isoformat()


def _load_dotenv() -> bool:
    env_path = ROOT / ".env"
    if not env_path.exists():
        return False
    try:
        from dotenv import load_dotenv

        load_dotenv(env_path, override=False)
        return True
    except Exception:
        for line in env_path.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip().strip('"').strip("'")
            if k and v and k not in os.environ:
                os.environ[k] = v
        return True


def credentials_present() -> dict[str, Any]:
    _load_dotenv()
    keys = (
        "TRADOVATE_USERNAME",
        "TRADOVATE_PASSWORD",
        "TRADOVATE_APP_ID",
        "TRADOVATE_CID",
        "TRADOVATE_SEC",
    )
    present = {k: bool(os.environ.get(k, "").strip()) for k in keys}
    return {
        "credential_required": True,
        "credential_present": all(present.values()),
        "fields_present": present,
        "app_version_present": bool(os.environ.get("TRADOVATE_APP_VERSION", "").strip()),
        "loaded_dotenv": (ROOT / ".env").exists(),
    }


def _env_base(explicit: Optional[str] = None) -> str:
    raw = (explicit or os.environ.get("TRADOVATE_ENV") or "live").strip().lower()
    if raw in ("demo", "simulation", "sim"):
        return ENV_DEMO
    return ENV_LIVE


def _device_id() -> str:
    env = os.environ.get("TRADOVATE_DEVICE_ID", "").strip()
    if env:
        return env
    if DEVICE_PATH.exists():
        existing = DEVICE_PATH.read_text(encoding="utf-8").strip()
        if existing:
            return existing
    DEVICE_PATH.parent.mkdir(parents=True, exist_ok=True)
    created = str(uuid.uuid4())
    DEVICE_PATH.write_text(created, encoding="utf-8")
    return created


def _redact(obj: Any) -> Any:
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            lk = str(k).lower().replace("_", "")
            if lk in SECRET_KEYS or str(k) in {"accessToken", "refreshToken", "password", "sec"}:
                out[k] = "[redacted]"
            else:
                out[k] = _redact(v)
        return out
    if isinstance(obj, list):
        return [_redact(x) for x in obj]
    return obj


def _money(v: Any) -> Optional[float]:
    if v is None or v == "":
        return None
    try:
        n = float(v)
    except (TypeError, ValueError):
        return None
    if n != n or n in (float("inf"), float("-inf")):
        return None
    return n


def match_fundednext_account(
    accounts: list[dict[str, Any]],
    expected_name: str,
) -> dict[str, Any]:
    expected = (expected_name or "").strip()
    rows = [a for a in accounts if isinstance(a, dict)]
    if not expected or expected in ("AUTO", "AUTO_FUNDEDNEXT"):
        fn_like = [
            a for a in rows
            if str(a.get("name") or "").upper().startswith("FN")
            and not str(a.get("name") or "").upper().startswith("SIM")
        ]
        if len(fn_like) == 1:
            a = fn_like[0]
            return {
                "fundednext_account_name": a.get("name"),
                "tradovate_account_id": a.get("id"),
                "matched": True,
                "match_method": "unique_fn_prefix",
                "account": a,
            }
        return {
            "fundednext_account_name": expected or None,
            "tradovate_account_id": None,
            "matched": False,
            "match_method": "unmatched_auto",
            "account": None,
        }
    exact = [a for a in rows if str(a.get("name") or "") == expected]
    if len(exact) == 1:
        a = exact[0]
        return {
            "fundednext_account_name": a.get("name"),
            "tradovate_account_id": a.get("id"),
            "matched": True,
            "match_method": "name_exact",
            "account": a,
        }
    contains = [a for a in rows if expected in str(a.get("name") or "")]
    if len(contains) == 1:
        a = contains[0]
        return {
            "fundednext_account_name": a.get("name"),
            "tradovate_account_id": a.get("id"),
            "matched": True,
            "match_method": "name_contains",
            "account": a,
        }
    return {
        "fundednext_account_name": expected,
        "tradovate_account_id": None,
        "matched": False,
        "match_method": "unmatched",
        "account": None,
    }


def normalize_position(row: dict[str, Any]) -> dict[str, Any]:
    qty = int(row.get("netPos") or 0)
    if qty > 0:
        side = "LONG"
    elif qty < 0:
        side = "SHORT"
    else:
        side = "FLAT"
    return {
        "account_id": row.get("accountId"),
        "contract_id": row.get("contractId"),
        "side": side,
        "quantity": abs(qty),
        "average_price": _money(row.get("netPrice")) if side != "FLAT" else None,
        "flat": side == "FLAT",
        "source": "TRADOVATE_READ_ONLY",
        "known": True,
    }


def aggregate_positions(rows: list[dict[str, Any]]) -> dict[str, Any]:
    open_legs = [p for p in rows if p.get("side") != "FLAT"]
    if not open_legs:
        return {
            "side": "FLAT",
            "quantity": 0,
            "average_price": None,
            "flat": True,
            "known": True,
            "source": "TRADOVATE_READ_ONLY",
            "legs": rows,
        }
    if len(open_legs) == 1:
        return {**open_legs[0], "legs": rows, "known": True}
    return {
        "side": "MULTI",
        "quantity": sum(p["quantity"] for p in open_legs),
        "average_price": None,
        "flat": False,
        "known": True,
        "source": "TRADOVATE_READ_ONLY",
        "legs": rows,
    }


def normalize_money(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Map Tradovate CashBalanceSnapshot fields. netLiq is equity."""
    err = snapshot.get("errorText")
    net = _money(snapshot.get("netLiq"))
    cash = _money(snapshot.get("totalCashValue"))
    if err:
        return {
            "equity": None,
            "net_liquidation": None,
            "cash_balance": None,
            "realized_pnl": None,
            "unrealized_pnl": None,
            "total_pnl": None,
            "error": str(err),
        }
    if net is None and cash is None:
        equity = None
    elif net is not None and abs(net) < 1e-12 and (cash is None or abs(cash) < 1e-12):
        equity = None
    else:
        equity = net if net is not None else cash
    return {
        "equity": equity,
        "net_liquidation": net,
        "cash_balance": cash,
        "realized_pnl": _money(snapshot.get("realizedPnL")),
        "unrealized_pnl": _money(snapshot.get("openPnL")),
        "total_pnl": _money(snapshot.get("totalPnL")),
        "error": None,
    }


class TradovateReadOnlyAccountAdapter:
    """Read-only Tradovate HTTP adapter. No order methods exist on this class."""

    def __init__(
        self,
        *,
        expected_account_name: str = "FNFTCHTANATSWAPHILMU92044",
        env: Optional[str] = None,
        stale_sec: float = 60.0,
        http_post=None,
        http_get=None,
    ):
        self.expected_account_name = expected_account_name
        self.base = _env_base(env)
        self.stale_sec = float(stale_sec)
        self._token: Optional[str] = None
        self._token_exp: Optional[str] = None
        self._http_post = http_post
        self._http_get = http_get
        self.PROP_EXECUTION = False

    def exposed_methods(self) -> list[str]:
        return sorted(n for n in dir(self) if not n.startswith("_") and callable(getattr(self, n)))

    def has_trading_methods(self) -> bool:
        return bool(set(self.exposed_methods()) & FORBIDDEN_METHODS)

    def _headers(self, *, auth: bool) -> dict[str, str]:
        h = {"Content-Type": "application/json", "Accept": "application/json"}
        if auth:
            if not self._token:
                raise TradovateReadOnlyError("not_authenticated")
            h["Authorization"] = "Bearer " + self._token
        return h

    def _url(self, path: str) -> str:
        if not path.startswith("/"):
            path = "/" + path
        return self.base + path

    def _post(self, path: str, body: dict[str, Any], *, auth: bool) -> Any:
        if path not in ALLOWED_POST:
            raise TradovateReadOnlyViolation(f"blocked_path:{path}")
        if self._http_post is not None:
            return self._http_post(path, body, auth)
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(self._url(path), data=data, headers=self._headers(auth=auth), method="POST")
        return self._send(req, redact=path != "/auth/accesstokenrequest")

    def _get(self, path: str, *, auth: bool = True) -> Any:
        clean = path.split("?")[0]
        if clean not in ALLOWED_GET:
            raise TradovateReadOnlyViolation(f"blocked_path:{path}")
        if self._http_get is not None:
            return self._http_get(path, auth)
        req = urllib.request.Request(self._url(path), headers=self._headers(auth=auth), method="GET")
        return self._send(req, redact=True)

    def _send(self, req: urllib.request.Request, *, redact: bool) -> Any:
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                raw = resp.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            raise TradovateReadOnlyError(f"http_{exc.code}") from None
        except urllib.error.URLError as exc:
            raise TradovateReadOnlyError("disconnected") from exc
        if not raw:
            return {}
        doc = json.loads(raw)
        if redact:
            return _redact(doc) if isinstance(doc, (dict, list)) else doc
        return doc

    def authenticate(self) -> dict[str, Any]:
        if self._token:
            return {
                "ok": True,
                "status": "LIVE",
                "authenticated": True,
                "expirationTime": self._token_exp,
            }
        cred = credentials_present()
        if not cred["credential_present"] and self._http_post is None:
            self._token = None
            return {
                "ok": False,
                "status": "AUTH_FAILED",
                "reason": "credentials_missing",
                "authenticated": False,
            }
        cid_raw = os.environ.get("TRADOVATE_CID", "").strip()
        try:
            cid_val: Any = int(cid_raw) if cid_raw else 0
        except ValueError:
            cid_val = cid_raw
        payload = {
            "name": os.environ.get("TRADOVATE_USERNAME", "").strip(),
            "password": os.environ.get("TRADOVATE_PASSWORD", "").strip(),
            "appId": os.environ.get("TRADOVATE_APP_ID", "").strip(),
            "appVersion": os.environ.get("TRADOVATE_APP_VERSION", "").strip() or "1.0.0",
            "cid": cid_val,
            "sec": os.environ.get("TRADOVATE_SEC", "").strip(),
            "deviceId": _device_id(),
        }
        try:
            doc = self._post("/auth/accesstokenrequest", payload, auth=False)
        except TradovateReadOnlyError as exc:
            self._token = None
            return {
                "ok": False,
                "status": "DISCONNECTED" if str(exc) == "disconnected" else "AUTH_FAILED",
                "reason": str(exc),
                "authenticated": False,
            }
        if not isinstance(doc, dict) or doc.get("errorText"):
            self._token = None
            return {"ok": False, "status": "AUTH_FAILED", "reason": "auth_rejected", "authenticated": False}
        token = doc.get("accessToken")
        if not token or token == "[redacted]":
            self._token = None
            return {"ok": False, "status": "AUTH_FAILED", "reason": "no_access_token", "authenticated": False}
        self._token = str(token)
        self._token_exp = doc.get("expirationTime")
        return {
            "ok": True,
            "status": "LIVE",
            "authenticated": True,
            "user_status": doc.get("userStatus"),
            "expirationTime": doc.get("expirationTime"),
        }

    def connection_status(self) -> dict[str, Any]:
        auth = self.authenticate()
        return {
            "connected": bool(auth.get("ok")),
            "authenticated": bool(auth.get("authenticated")),
            "status": auth.get("status"),
            "reason": auth.get("reason"),
            "permission": "READ_ONLY",
            "PROP_EXECUTION": False,
            "base": self.base,
        }

    def accounts(self) -> list[dict[str, Any]]:
        if not self._token:
            auth = self.authenticate()
            if not auth.get("ok"):
                return []
        doc = self._get("/account/list", auth=True)
        if isinstance(doc, list):
            return [a for a in doc if isinstance(a, dict)]
        return []

    def positions(self, account_id: Any) -> dict[str, Any]:
        if not self._token:
            auth = self.authenticate()
            if not auth.get("ok"):
                return {
                    "side": "FLAT",
                    "quantity": 0,
                    "known": False,
                    "source": "TRADOVATE_READ_ONLY",
                    "error": "not_authenticated",
                }
        doc = self._get("/position/list", auth=True)
        rows = doc if isinstance(doc, list) else []
        own = [r for r in rows if isinstance(r, dict) and str(r.get("accountId")) == str(account_id)]
        return aggregate_positions([normalize_position(r) for r in own])

    def account_snapshot(self, account_id: Any) -> dict[str, Any]:
        ts = _iso()
        try:
            raw = self._post("/cashBalance/getcashbalancesnapshot", {"accountId": int(account_id)}, auth=True)
        except (TradovateReadOnlyError, TypeError, ValueError) as exc:
            return {
                "source": "TRADOVATE_READ_ONLY",
                "timestamp": ts,
                "fresh": False,
                "money": normalize_money({}),
                "error": str(exc),
                "raw_source_fields": {},
            }
        if not isinstance(raw, dict):
            raw = {}
        money = normalize_money(raw)
        return {
            "source": "TRADOVATE_READ_ONLY",
            "timestamp": ts,
            "fresh": money.get("equity") is not None,
            "money": money,
            "error": money.get("error"),
            "raw_source_fields": {
                "netLiq": raw.get("netLiq"),
                "totalCashValue": raw.get("totalCashValue"),
                "realizedPnL": raw.get("realizedPnL"),
                "openPnL": raw.get("openPnL"),
                "totalPnL": raw.get("totalPnL"),
            },
        }

    def fundednext_snapshot(self) -> dict[str, Any]:
        started = time.time()
        auth = self.authenticate()
        if not auth.get("ok"):
            return {
                "source": "TRADOVATE_READ_ONLY",
                "timestamp": _iso(),
                "fresh": False,
                "connected": False,
                "authenticated": False,
                "status": auth.get("status") or "AUTH_FAILED",
                "reason": auth.get("reason"),
                "account": {"name": self.expected_account_name, "id": None, "status": None},
                "money": {
                    "equity": None,
                    "net_liquidation": None,
                    "cash_balance": None,
                    "realized_pnl": None,
                    "unrealized_pnl": None,
                },
                "positions": [],
                "position": {"side": "FLAT", "quantity": 0, "known": False, "source": "TRADOVATE_READ_ONLY"},
                "match": {
                    "fundednext_account_name": self.expected_account_name,
                    "tradovate_account_id": None,
                    "matched": False,
                    "match_method": "auth_failed",
                },
                "age_sec": 0.0,
                "PROP_EXECUTION": False,
                "raw_source_fields": {},
            }
        accts = self.accounts()
        match = match_fundednext_account(accts, self.expected_account_name)
        if not match.get("matched"):
            return {
                "source": "TRADOVATE_READ_ONLY",
                "timestamp": _iso(),
                "fresh": False,
                "connected": True,
                "authenticated": True,
                "status": "UNAVAILABLE",
                "reason": "account_unmatched",
                "account": {"name": self.expected_account_name, "id": None, "status": None},
                "money": {
                    "equity": None,
                    "net_liquidation": None,
                    "cash_balance": None,
                    "realized_pnl": None,
                    "unrealized_pnl": None,
                },
                "positions": [],
                "position": {"side": "FLAT", "quantity": 0, "known": False, "source": "TRADOVATE_READ_ONLY"},
                "match": match,
                "age_sec": max(0.0, time.time() - started),
                "PROP_EXECUTION": False,
                "raw_source_fields": {},
            }
        acct = match.get("account") or {}
        snap = self.account_snapshot(match["tradovate_account_id"])
        pos = self.positions(match["tradovate_account_id"])
        money = snap.get("money") or {}
        age = max(0.0, time.time() - started)
        live_ok = bool(money.get("equity") is not None) and age <= self.stale_sec
        if live_ok:
            status = "LIVE"
        elif money.get("equity") is not None:
            status = "STALE"
        else:
            status = "UNAVAILABLE"
        return {
            "source": "TRADOVATE_READ_ONLY",
            "timestamp": snap.get("timestamp") or _iso(),
            "fresh": live_ok,
            "connected": True,
            "authenticated": True,
            "status": status,
            "account": {
                "name": acct.get("name"),
                "id": acct.get("id"),
                "status": "active" if acct.get("active") else "inactive",
                "accountType": acct.get("accountType"),
            },
            "money": money,
            "positions": pos.get("legs") if isinstance(pos.get("legs"), list) else [],
            "position": pos,
            "match": {
                "fundednext_account_name": match.get("fundednext_account_name"),
                "tradovate_account_id": match.get("tradovate_account_id"),
                "matched": True,
                "match_method": match.get("match_method"),
            },
            "age_sec": age,
            "stale_sec": self.stale_sec,
            "PROP_EXECUTION": False,
            "raw_source_fields": snap.get("raw_source_fields") or {},
        }
