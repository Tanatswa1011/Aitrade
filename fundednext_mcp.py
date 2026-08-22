"""Phase 54E — FundedNext MCP read-only account telemetry.

Calls https://mcp.fundednext.com over Streamable HTTP. Tokens live in
gitignored state/ or environment variables. Write-capable MCP tools are denied.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

UTC = timezone.utc
ROOT = Path(__file__).resolve().parent
from fundednext_mcp_oauth import (
    HTTP_USER_AGENT,
    OAUTH_PATH,
    invalidate_oauth_session,
    load_oauth_session,
    resolve_access_token,
)

MCP_URL = "https://mcp.fundednext.com"

READ_ALLOWLIST = frozenset({
    "get_accounts_v2",
    "get_accounts",
    "get_account_overview",
    "get_account_applicable_rules",
    "get_futures_trade_history",
    "resolve_account",
})

WRITE_DENYLIST = frozenset({
    "create_free_trial_account",
    "register_competition",
    "record_ai_feedback",
})

SECRET_KEYS = frozenset({
    "password", "investorpassword", "token", "accesstoken", "refreshtoken",
    "accesstoken", "authorization", "sec", "secret",
})

PROP_EXECUTION = False
EXPECTED_NAME = "FNFTCHTANATSWAPHILMU92044"
EXPECTED_LOGIN = "962841277"
EXPECTED_ACCOUNT_ID = 3969349
EXPECTED_PLAN = "Futures Flex Challenge | 50K"


class FundedNextMCPError(RuntimeError):
    pass


def _now() -> datetime:
    return datetime.now(tz=UTC)


def _iso(ts: Optional[datetime] = None) -> str:
    return (ts or _now()).isoformat()


def _load_dotenv() -> None:
    env_path = ROOT / ".env"
    if not env_path.exists():
        return
    try:
        from dotenv import load_dotenv

        load_dotenv(env_path, override=False)
        return
    except Exception:
        pass
    for line in env_path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip().strip('"').strip("'")
        if k and v and k not in os.environ:
            os.environ[k] = v


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


def _redact(obj: Any) -> Any:
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            lk = str(k).lower().replace("_", "")
            if lk in SECRET_KEYS:
                out[k] = "[redacted]"
            else:
                out[k] = _redact(v)
        return out
    if isinstance(obj, list):
        return [_redact(x) for x in obj]
    return obj


def assert_tool_allowed(name: str) -> str:
    tool = str(name or "").strip()
    if tool in WRITE_DENYLIST or tool not in READ_ALLOWLIST:
        raise PermissionError("FUNDEDNEXT_MCP_TOOL_DENIED:%s" % tool)
    return tool


def match_active_futures_account(
    accounts: list[dict[str, Any]],
    *,
    expected_name: str,
    expected_login: str,
    expected_account_id: int,
    expected_plan: str,
) -> dict[str, Any]:
    rows = [a for a in accounts if isinstance(a, dict)]
    hits = []
    for a in rows:
        nested = a.get("tradovate_account_name") if isinstance(a.get("tradovate_account_name"), dict) else {}
        name = str(nested.get("tradovate_account_name") or a.get("account_name") or a.get("name") or "")
        login = str(a.get("login") or "")
        aid = a.get("id")
        plan_obj = a.get("plan")
        plan = str(plan_obj.get("title") if isinstance(plan_obj, dict) else (a.get("type") or a.get("plan") or ""))
        active = int(a.get("breached") or 0) == 0
        try:
            aid_i = int(aid)
        except (TypeError, ValueError):
            continue
        if (
            name == expected_name
            and login == str(expected_login)
            and aid_i == int(expected_account_id)
            and expected_plan in plan
            and active
        ):
            hits.append(a)
    base = {
        "fundednext_name": expected_name,
        "platform_login": expected_login,
        "account_id": expected_account_id,
        "plan": expected_plan,
        "account": None,
    }
    if len(hits) != 1:
        return {**base, "matched": False, "match_method": "unmatched"}
    a = hits[0]
    nested = a.get("tradovate_account_name") if isinstance(a.get("tradovate_account_name"), dict) else {}
    return {
        "matched": True,
        "fundednext_name": str(nested.get("tradovate_account_name") or expected_name),
        "platform_login": str(a.get("login")),
        "account_id": a.get("id"),
        "plan": expected_plan,
        "match_method": "name+login+id+plan+active",
        "account": a,
    }


def normalize_running_trades(running: Any) -> dict[str, Any]:
    """Empty MCP runningTrades page (total=0, data=[]) means FLAT and known."""
    unknown = {
        "side": "FLAT", "quantity": 0, "known": False,
        "source": "FUNDEDNEXT_MCP", "flat": True,
    }
    if running is None:
        return unknown
    if isinstance(running, dict):
        rows = running.get("data") if isinstance(running.get("data"), list) else []
        total = running.get("total")
        if total == 0 and not rows:
            return {
                "side": "FLAT", "quantity": 0, "known": True,
                "source": "FUNDEDNEXT_MCP", "flat": True, "legs": [],
            }
    else:
        rows = running if isinstance(running, list) else []
        total = len(rows)
    open_legs = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        qty = int(row.get("qty") or row.get("quantity") or row.get("lots") or 0)
        if qty == 0:
            continue
        typ = str(row.get("type") or row.get("side") or "").lower()
        side = "SHORT" if typ in ("sell", "short", "1") else "LONG"
        open_legs.append({"side": side, "quantity": abs(qty), "symbol": row.get("symbol")})
    if not open_legs:
        return {
            "side": "FLAT", "quantity": 0, "known": True,
            "source": "FUNDEDNEXT_MCP", "flat": True, "legs": rows, "total": total,
        }
    if len(open_legs) == 1:
        return {**open_legs[0], "known": True, "source": "FUNDEDNEXT_MCP", "flat": False, "legs": rows}
    return {
        "side": "MULTI",
        "quantity": sum(p["quantity"] for p in open_legs),
        "known": True, "source": "FUNDEDNEXT_MCP", "flat": False, "legs": rows,
    }


def reconcile_rules_against_prop_v1(overview: dict[str, Any], profile_raw: dict[str, Any]) -> dict[str, Any]:
    eval_rules = profile_raw.get("evaluation") or {}
    details = overview.get("account_details") or {}
    stats = overview.get("stats") or {}
    objectives = overview.get("objectives") or {}
    overall = objectives.get("overall_loss") or {}
    profit = objectives.get("profit") or {}
    consistency = objectives.get("consistency") or {}
    daily = objectives.get("daily_loss") or []
    mismatches: list[dict[str, Any]] = []

    size = _money(stats.get("cycle_starting_balance") or details.get("initial_balance"))
    expected_size = _money(eval_rules.get("nominal_account_size"))
    if size is not None and expected_size is not None and abs(size - expected_size) > 0.01:
        mismatches.append({"field": "plan_size", "mcp": size, "prop_rules": expected_size, "critical": True})

    pt = _money(profit.get("profit_target"))
    expected_pt = _money(eval_rules.get("profit_target"))
    if pt is not None and expected_pt is not None and abs(pt - expected_pt) > 0.01:
        mismatches.append({"field": "profit_target", "mcp": pt, "prop_rules": expected_pt, "critical": True})

    permitted = _money(overall.get("permitted_loss"))
    expected_ml = _money(eval_rules.get("max_loss"))
    if permitted is not None and expected_ml is not None and abs(permitted - expected_ml) > 0.01:
        mismatches.append({"field": "max_loss", "mcp": permitted, "prop_rules": expected_ml, "critical": True})

    dll_none = str(eval_rules.get("daily_loss_limit") or "NONE").upper() in ("NONE", "NONE_STATED")
    dll_present = bool(daily) if isinstance(daily, list) else False
    if dll_none and dll_present:
        mismatches.append({"field": "daily_loss", "mcp": daily, "prop_rules": "NONE", "critical": True})

    cr = _money(consistency.get("consistency_rate"))
    expected_cr = _money(eval_rules.get("consistency_ratio_max"))
    if cr is not None and expected_cr is not None:
        cr_frac = cr / 100.0 if cr > 1 else cr
        if abs(cr_frac - expected_cr) > 0.001:
            mismatches.append({"field": "consistency", "mcp": cr, "prop_rules": expected_cr, "critical": True})

    min_days = eval_rules.get("minimum_trading_days")
    mtd = objectives.get("trading_days") or {}
    if str(min_days or "NONE").upper() in ("NONE", "NONE_STATED"):
        tv = _money(mtd.get("target_value"))
        if tv not in (None, 0.0):
            mismatches.append({"field": "minimum_trading_days", "mcp": tv, "prop_rules": min_days, "critical": False})

    critical = [m for m in mismatches if m.get("critical")]
    return {
        "rules_match": not critical,
        "mismatches": mismatches,
        "informational": [{
            "field": "news_addon",
            "mcp": "ANT_OFF_OR_UNREAD",
            "prop_rules": eval_rules.get("news_trading"),
            "critical": False,
            "note": "Flex base product allows news; MCP ANT add-on off is compatible.",
        }],
        "survival_critical_ok": not critical,
    }


def _parse_mcp_tool_result(doc: Any) -> Any:
    if isinstance(doc, dict) and isinstance(doc.get("result"), dict):
        doc = doc["result"]
    if isinstance(doc, dict) and isinstance(doc.get("content"), list):
        texts = []
        for item in doc["content"]:
            if isinstance(item, dict) and item.get("type") == "text":
                texts.append(str(item.get("text") or ""))
        blob = "\n".join(texts).strip()
        if blob:
            marker = "[INTERNAL"
            if marker in blob:
                blob = blob.split(marker, 1)[0].strip()
            try:
                return json.loads(blob)
            except json.JSONDecodeError:
                return {"raw_text": blob[:2000]}
    return doc


class FundedNextMCPReadOnlyAdapter:
    """Read-only FundedNext MCP adapter. No order methods exist on this class."""

    def __init__(
        self,
        *,
        expected_name: str = EXPECTED_NAME,
        expected_login: str = EXPECTED_LOGIN,
        expected_account_id: int = EXPECTED_ACCOUNT_ID,
        expected_plan: str = EXPECTED_PLAN,
        stale_sec: float = 60.0,
        tool_caller: Optional[Callable[[str, dict[str, Any]], Any]] = None,
        access_token: Optional[str] = None,
        mcp_url: str = MCP_URL,
    ):
        self.expected_name = expected_name
        self.expected_login = str(expected_login)
        self.expected_account_id = int(expected_account_id)
        self.expected_plan = expected_plan
        self.stale_sec = float(stale_sec)
        self._tool_caller = tool_caller
        self._token = access_token
        self._injected_token = bool(access_token)
        self.mcp_url = mcp_url
        self.PROP_EXECUTION = False
        self._rpc_id = 0
        self._session_id = None

    def exposed_methods(self) -> list[str]:
        return sorted(n for n in dir(self) if not n.startswith("_") and callable(getattr(self, n)))

    def has_trading_methods(self) -> bool:
        forbidden = {
            "place_order", "submit_order", "cancel_order", "modify_order",
            "flatten", "liquidate", "close_position", "drop_oif",
            "create_free_trial_account", "register_competition", "record_ai_feedback",
        }
        return bool(set(self.exposed_methods()) & forbidden)

    def call_tool(self, name: str, arguments: Optional[dict[str, Any]] = None) -> Any:
        tool = assert_tool_allowed(name)
        args = arguments or {}
        if self._tool_caller is not None:
            return self._tool_caller(tool, args)
        token = self._access_token()
        if not token:
            raise FundedNextMCPError("not_authenticated")
        try:
            self._ensure_session(token)
            self._rpc_id += 1
            return self._rpc("tools/call", {"name": tool, "arguments": args}, token)
        except FundedNextMCPError as exc:
            if str(exc) != "http_401":
                raise
            self._session_id = None
            token = self._access_token(force_refresh=True)
            if not token:
                raise FundedNextMCPError("not_authenticated") from None
            self._ensure_session(token)
            self._rpc_id += 1
            return self._rpc("tools/call", {"name": tool, "arguments": args}, token)

    def _rpc(self, method: str, params: Any, token: str) -> Any:
        payload = {
            "jsonrpc": "2.0",
            "id": self._rpc_id,
            "method": method,
            "params": params,
        }
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "Authorization": "Bearer " + token,
            "User-Agent": HTTP_USER_AGENT,
        }
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        req = urllib.request.Request(
            self.mcp_url,
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read().decode("utf-8")
                sid = resp.headers.get("Mcp-Session-Id") or resp.headers.get("mcp-session-id")
                if sid:
                    self._session_id = sid
        except urllib.error.HTTPError as exc:
            raise FundedNextMCPError("http_%s" % exc.code) from None
        except urllib.error.URLError as exc:
            raise FundedNextMCPError("disconnected") from exc
        if not raw:
            return {}
        if raw.startswith("event:") or "data:" in raw[:40]:
            chunks = [ln[5:].strip() for ln in raw.splitlines() if ln.startswith("data:")]
            raw = "\n".join(chunks) or raw
        try:
            doc = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise FundedNextMCPError("bad_json") from exc
        if isinstance(doc, dict) and doc.get("error"):
            raise FundedNextMCPError(str(doc.get("error")))
        return _parse_mcp_tool_result(doc)

    def _ensure_session(self, token: str) -> None:
        if self._session_id:
            return
        self._rpc_id += 1
        self._rpc(
            "initialize",
            {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "aitrade-phase54e", "version": "54E"},
            },
            token,
        )
        try:
            self._rpc_id += 1
            self._rpc("notifications/initialized", {}, token)
        except FundedNextMCPError:
            pass

    def _access_token(self, *, force_refresh: bool = False) -> Optional[str]:
        if self._injected_token and self._token and not force_refresh:
            return self._token
        _load_dotenv()
        token = resolve_access_token(force_refresh=force_refresh)
        if not token:
            self._token = None
            self._session_id = None
            return None
        self._token = token
        return token

    def connection_status(self) -> dict[str, Any]:
        snap = self.normalized_snapshot()
        return {
            "connected": bool(snap.get("authenticated") and snap.get("status") in ("LIVE", "STALE")),
            "authenticated": bool(snap.get("authenticated") and snap.get("status") == "LIVE"),
            "status": snap.get("status"),
            "permission": "READ_ONLY",
            "source": "FUNDEDNEXT_MCP",
            "PROP_EXECUTION": False,
        }

    def active_futures_account(self) -> dict[str, Any]:
        doc = self.call_tool("get_accounts_v2", {"type": "active", "tab": "futures", "limit": 20})
        rows = []
        if isinstance(doc, dict):
            data = doc.get("data")
            if isinstance(data, list):
                rows = data
            elif isinstance(data, dict) and isinstance(data.get("data"), list):
                rows = data["data"]
        return match_active_futures_account(
            rows,
            expected_name=self.expected_name,
            expected_login=self.expected_login,
            expected_account_id=self.expected_account_id,
            expected_plan=self.expected_plan,
        )

    def account_overview(self, account_id: int) -> dict[str, Any]:
        doc = self.call_tool("get_account_overview", {"account_id": int(account_id)})
        return doc if isinstance(doc, dict) else {}

    def applicable_rules(self, account_id: int) -> dict[str, Any]:
        doc = self.call_tool("get_account_applicable_rules", {"account_id": int(account_id)})
        return doc if isinstance(doc, dict) else {}

    def futures_trade_history(self, account_id: int) -> dict[str, Any]:
        doc = self.call_tool(
            "get_futures_trade_history",
            {"account_id": int(account_id), "page": 1, "per_page": 15},
        )
        return doc if isinstance(doc, dict) else {}

    def running_futures_trades(self, account_id: int) -> Any:
        hist = self.futures_trade_history(account_id)
        return hist.get("runningTrades") if isinstance(hist, dict) else None

    def normalized_snapshot(self) -> dict[str, Any]:
        started = time.time()
        unavailable = {
            "source": "FUNDEDNEXT_MCP",
            "timestamp": _iso(),
            "fresh": False,
            "authenticated": False,
            "connected": False,
            "status": "AUTH_FAILED",
            "account": {
                "name": self.expected_name,
                "platform_login": self.expected_login,
                "account_id": self.expected_account_id,
                "status": None,
                "breached": None,
                "plan": self.expected_plan,
            },
            "money": {"balance": None, "equity": None, "profit": None, "initial_balance": None},
            "risk": {"permitted_loss": None, "minimum_equity": None, "remaining_loss_buffer": None},
            "rules": {},
            "rules_reconciliation": {"rules_match": False, "mismatches": [{"field": "mcp_unavailable"}], "survival_critical_ok": False},
            "futures": {"running_trades": [], "trade_history": []},
            "position": {"side": "FLAT", "quantity": 0, "known": False, "source": "FUNDEDNEXT_MCP"},
            "match": {
                "matched": False,
                "fundednext_name": self.expected_name,
                "platform_login": self.expected_login,
                "account_id": self.expected_account_id,
                "plan": self.expected_plan,
                "match_method": "auth_failed",
            },
            "age_sec": 0.0,
            "PROP_EXECUTION": False,
        }
        if self._tool_caller is None and not self._access_token():
            unavailable["reason"] = "credentials_missing"
            return unavailable
        try:
            match = self.active_futures_account()
        except FundedNextMCPError as exc:
            unavailable["reason"] = str(exc)
            unavailable["status"] = "DISCONNECTED" if str(exc) == "disconnected" else "AUTH_FAILED"
            return unavailable
        if not match.get("matched"):
            unavailable["status"] = "UNAVAILABLE"
            unavailable["authenticated"] = True
            unavailable["connected"] = True
            unavailable["reason"] = "account_unmatched"
            unavailable["match"] = match
            return unavailable
        account_id = int(match["account_id"])
        try:
            overview = self.account_overview(account_id)
            rules = self.applicable_rules(account_id)
            history = self.futures_trade_history(account_id)
        except FundedNextMCPError as exc:
            unavailable["reason"] = str(exc)
            unavailable["status"] = "UNAVAILABLE"
            unavailable["authenticated"] = True
            unavailable["match"] = match
            return unavailable
        details = overview.get("account_details") or {}
        stats = overview.get("stats") or {}
        objectives = overview.get("objectives") or {}
        overall = objectives.get("overall_loss") or {}
        profit_raw = stats.get("profit")
        if profit_raw is None:
            profit_raw = details.get("profit")
        money = {
            "balance": _money(stats.get("balance") if stats.get("balance") is not None else details.get("balance")),
            "equity": _money(stats.get("equity") if stats.get("equity") is not None else (details.get("equity") if details.get("equity") is not None else stats.get("balance"))),
            "profit": _money(profit_raw),
            "initial_balance": _money(details.get("initial_balance") or stats.get("cycle_starting_balance")),
        }
        risk = {
            "permitted_loss": _money(overall.get("permitted_loss")),
            "minimum_equity": _money(overall.get("minimum_equity")),
            "remaining_loss_buffer": _money(overall.get("remaining")),
        }
        running = history.get("runningTrades") if isinstance(history, dict) else None
        trades = history.get("trades") if isinstance(history, dict) else {}
        trade_rows = trades.get("data") if isinstance(trades, dict) else []
        try:
            from prop_rules_v1 import load_profile
            recon = reconcile_rules_against_prop_v1(overview, load_profile("FUNDEDNEXT_FLEX_50K").raw)
        except Exception:
            recon = {"rules_match": False, "mismatches": [{"field": "prop_rules_unreadable"}], "survival_critical_ok": False}
        age = max(0.0, time.time() - started)
        live_ok = money.get("equity") is not None and risk.get("remaining_loss_buffer") is not None and age <= self.stale_sec
        if live_ok:
            status = "LIVE"
        elif money.get("equity") is not None:
            status = "STALE"
        else:
            status = "UNAVAILABLE"
        breached = bool(details.get("breached"))
        acct_status = str(details.get("account_status") or ("BREACHED" if breached else "ACTIVE")).upper()
        return {
            "source": "FUNDEDNEXT_MCP",
            "timestamp": _iso(),
            "fresh": live_ok,
            "authenticated": True,
            "connected": True,
            "status": status,
            "account": {
                "name": match.get("fundednext_name"),
                "platform_login": match.get("platform_login"),
                "account_id": account_id,
                "status": acct_status,
                "breached": breached,
                "plan": match.get("plan") or details.get("type"),
            },
            "money": money,
            "risk": risk,
            "rules": _redact(rules),
            "rules_reconciliation": recon,
            "futures": {
                "running_trades": (running.get("data") if isinstance(running, dict) else running) or [],
                "trade_history": trade_rows or [],
            },
            "position": normalize_running_trades(running),
            "match": {
                "matched": True,
                "fundednext_name": match.get("fundednext_name"),
                "platform_login": match.get("platform_login"),
                "account_id": account_id,
                "plan": match.get("plan"),
                "match_method": match.get("match_method"),
            },
            "age_sec": age,
            "stale_sec": self.stale_sec,
            "PROP_EXECUTION": False,
        }
