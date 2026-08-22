"""Phase 54 — NinjaTrader-backed operations adapters. Never transmits prop orders."""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from aitrade_operating_policy import load_operating_policy
from execution_status import (
    BLOCKED_MODES,
    execution_summary,
    is_execution_paused,
    sim_only_execution_armed,
)
from macro_calendar import EVENTS_PATH, load_events
from nq_microstructure_models import FROZEN_GC_HASH, FROZEN_NQ_HASH
from phase34_validate import assert_frozen
from phase52_policy import (
    FAST_QTY,
    MAX_LOSS,
    MLL_LOCK_AT,
    PROFIT_TARGET,
    SAFE_QTY,
    START_EQUITY,
    STOP_POINTS,
    UNIT_RISK_USD,
    evaluate_intent,
    near_target,
    remaining_drawdown,
)
from phase53_engine import calendar_status_for
from prop_rules_v1 import load_profile
from nt_readonly import NTReadOnly
from fundednext_mcp import FundedNextMCPReadOnlyAdapter
from fundednext_mcp_oauth import auth_generation, oauth_session_metadata
from tradovate_readonly import TradovateReadOnlyAccountAdapter  # deprecated for FN money; kept unused by Phase 54E

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config" / "aitrade_phase54_ops.json"
STATE_PATH = ROOT / "state" / "phase54_ops.json"
_LIVE_DVP_CACHE: dict[str, Any] = {}


def live_dvp_status(*, force: bool = False) -> Optional[dict[str, Any]]:
    if os.environ.get("AITRADE_PHASE54_TEST") == "1":
        return None
    now = time.time()
    if not force and _LIVE_DVP_CACHE.get("t") and now - float(_LIVE_DVP_CACHE["t"]) < 1.0:
        doc = _LIVE_DVP_CACHE.get("doc")
        return doc if isinstance(doc, dict) else None
    try:
        from nq_dvp_live_feed import evaluate_live_dvp

        # Ops snapshot must not load the Databento archive (that blocks the desk).
        doc = evaluate_live_dvp(persist=True, consume=False, warmup_bars=[])
    except Exception as exc:
        doc = {"ok": False, "error": str(exc), "pipeline": "LIVE_DVP_ERROR"}
    _LIVE_DVP_CACHE["t"] = now
    _LIVE_DVP_CACHE["doc"] = doc
    return doc


def _journal_dir() -> Path:
    override = os.environ.get("AITRADE_PHASE54_JOURNAL")
    if override:
        return Path(override)
    if os.environ.get("AITRADE_PHASE54_TEST") == "1":
        import tempfile
        return Path(tempfile.mkdtemp(prefix="phase54_ops_test_"))
    return ROOT / "journal" / "phase54_ops"


JOURNAL_DIR = _journal_dir()
EVENTS_LOG = JOURNAL_DIR / "events.jsonl"
TELEMETRY_PATH = JOURNAL_DIR / "telemetry.jsonl"
SIGNALS_LOG = JOURNAL_DIR / "signals.jsonl"
SOAK_PATH = JOURNAL_DIR / "soak.json"
HEALTH_PATH = ROOT / "reports" / "phase53_distribution_health" / "health.json"
AUDIT_PATH = ROOT / "reports" / "phase53_shadow" / "audit.jsonl"
AUDIT_PATH_LIVE = ROOT / "journal" / "phase53_fn_flex_shadow" / "audit.jsonl"
PROP_POLICY_PATH = ROOT / "config" / "aitrade_prop_execution_policy_v1.json"

UTC = timezone.utc
PROP_EXECUTION = False
SOAK_SCHEMA = "AITRADE_PHASE54F2_SOAK_V1"
AUTH_FAIL_REMINDER_SEC = 60.0
EXPECTED_STRATEGY_ID = "NQ_DRIFT_VWAP_PULLBACK"
_EVENTS_CACHE: Optional[list] = None


def _nt() -> NTReadOnly:
    return NTReadOnly()


_TV: Optional[TradovateReadOnlyAccountAdapter] = None  # Phase 54D path; disabled for FN money
_MCP: Optional[FundedNextMCPReadOnlyAdapter] = None
_MCP_SNAP: dict[str, Any] = {"ts": 0.0, "doc": None}


def _expected_fn_name() -> str:
    cfg = load_config()
    exp = cfg.get("expected_account_id")
    if exp and exp not in ("AUTO", "AUTO_FUNDEDNEXT"):
        return str(exp)
    fn = cfg.get("fundednext_mcp") or {}
    return str(fn.get("expected_account_name") or "FNFTCHTANATSWAPHILMU92044")


def _fn_mcp() -> FundedNextMCPReadOnlyAdapter:
    global _MCP
    if _MCP is None:
        cfg = load_config()
        fn = cfg.get("fundednext_mcp") or {}
        stale = float(fn.get("stale_account_sec") or cfg.get("stale_account_sec") or 60)
        _MCP = FundedNextMCPReadOnlyAdapter(
            expected_name=str(fn.get("expected_account_name") or _expected_fn_name()),
            expected_login=str(fn.get("expected_login") or "962841277"),
            expected_account_id=int(fn.get("expected_account_id") or 3969349),
            expected_plan=str(fn.get("expected_plan") or "Futures Flex Challenge | 50K"),
            stale_sec=stale,
        )
    return _MCP


def _mcp_stale_sec() -> float:
    cfg = load_config()
    fn = cfg.get("fundednext_mcp") or {}
    return float(fn.get("stale_account_sec") or cfg.get("stale_account_sec") or 60)


def _with_mcp_age(doc: dict[str, Any], age_sec: float) -> dict[str, Any]:
    out = dict(doc)
    out["age_sec"] = age_sec
    if out.get("status") == "LIVE" and age_sec > _mcp_stale_sec():
        out["status"] = "STALE"
        out["fresh"] = False
    return out


def _fn_mcp_snapshot() -> dict[str, Any]:
    global _MCP
    now = time.time()
    cached = _MCP_SNAP.get("doc")
    fetched = float(_MCP_SNAP.get("ts") or 0)
    gen = auth_generation()
    if cached and now - fetched < 4.0 and int(_MCP_SNAP.get("gen") or 0) == gen:
        if cached.get("status") != "AUTH_FAILED":
            return _with_mcp_age(cached, now - fetched + float(cached.get("age_sec") or 0))
    doc = _fn_mcp().normalized_snapshot()
    _MCP_SNAP["ts"] = now
    _MCP_SNAP["doc"] = doc
    _MCP_SNAP["gen"] = gen
    if doc.get("status") == "AUTH_FAILED":
        _MCP = None
    return _with_mcp_age(doc, float(doc.get("age_sec") or 0))


def _mll_for_equity(equity: Optional[float]) -> tuple[Optional[float], Optional[str]]:
    """Policy interpretation. EOD trailing MLL needs a high-water mark we do not have yet.

    Lock at $50,100 is reconstructable from PROP_RULES_V1. Below lock, MLL stays unavailable.
    """
    if equity is None:
        return None, None
    if equity + 1e-9 >= MLL_LOCK_AT:
        return float(MLL_LOCK_AT), "FUNDEDNEXT_FLEX_50K.MLL_LOCK_AT"
    return None, None


def live_eval_state(equity: Optional[float], *, demoted: bool, mll: Optional[float] = None) -> str:
    if equity is None:
        return "PAUSED"
    if mll is None:
        mll, _src = _mll_for_equity(equity)
    if mll is None:
        return "PAUSED"
    rem = remaining_drawdown(equity, mll)
    realized = float(equity) - START_EQUITY
    if rem <= 0:
        return "EVAL_BREACHED"
    if realized >= PROFIT_TARGET - 1e-9:
        return "EVAL_PASSED"
    if near_target(PROFIT_TARGET - realized, rule="PCT_95"):
        return "EVAL_NEAR_TARGET"
    if demoted:
        return "EVAL_PROTECTED"
    return "EVAL_FAST"


def _events() -> list:
    global _EVENTS_CACHE
    if _EVENTS_CACHE is None:
        _EVENTS_CACHE = load_events() if EVENTS_PATH.exists() else []
    return _EVENTS_CACHE


def _now() -> datetime:
    return datetime.now(tz=UTC)


def _iso(ts: Optional[datetime] = None) -> str:
    return (ts or _now()).isoformat()


def load_config() -> dict[str, Any]:
    doc = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    doc["PROP_EXECUTION"] = False
    return doc


def _read_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, default=str), encoding="utf-8")


_JOURNAL_SECRET_KEYS = frozenset({
    "password", "token", "accesstoken", "refreshtoken", "sec", "secret",
    "authorization", "cid", "access_token", "refresh_token",
})


def _journal_safe(extra: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in extra.items():
        lk = str(k).lower().replace("_", "")
        if lk in _JOURNAL_SECRET_KEYS or any(s in lk for s in ("password", "accesstoken", "refreshtoken")):
            continue
        out[k] = v
    return out


def append_event(level: str, message: str, **extra: Any) -> dict[str, Any]:
    JOURNAL_DIR.mkdir(parents=True, exist_ok=True)
    row = {"ts": _iso(), "level": level, "message": message, **_journal_safe(extra)}
    with EVENTS_LOG.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, default=str) + "\n")
    return row


def _tail_jsonl(path: Path, n: int = 40) -> list[dict[str, Any]]:
    if not path.exists() or path.stat().st_size == 0:
        return []
    lines = path.read_text(encoding="utf-8").splitlines()
    out = []
    for line in lines[-n:]:
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def _last_jsonl_obj(path: Path) -> Optional[dict[str, Any]]:
    rows = _tail_jsonl(path, 1)
    return rows[-1] if rows else None


def _last_jsonl_file_obj(path: Path) -> Optional[dict[str, Any]]:
    if not path.exists() or path.stat().st_size == 0:
        return None
    with path.open("rb") as fh:
        fh.seek(0, 2)
        size = fh.tell()
        fh.seek(max(0, size - 8192))
        chunk = fh.read().decode("utf-8", errors="replace")
    lines = [ln.strip() for ln in chunk.splitlines() if ln.strip()]
    if not lines:
        return None
    try:
        return json.loads(lines[-1])
    except json.JSONDecodeError:
        return None


def prop_execution_allowed() -> bool:
    """Authoritative Phase 54 lock. Always false."""
    pol = load_operating_policy()
    cfg = load_config()
    return bool(
        PROP_EXECUTION
        and cfg.get("PROP_EXECUTION")
        and pol.broker_execution
        and pol.execution_default not in ("DRY_RUN", "SIM_ONLY")
    )


def assert_prop_execution_disabled() -> None:
    if prop_execution_allowed():
        raise PermissionError("PROP_EXECUTION_FORBIDDEN_PHASE54")
    pol = load_operating_policy()
    if pol.broker_execution:
        raise PermissionError("BROKER_EXECUTION_MUST_REMAIN_FALSE")


def _md_detail(md: dict[str, Any]) -> dict[str, Any]:
    return {
        "source": md.get("source") or "NINJATRADER_READ_ONLY",
        "instrument": md.get("instrument"),
        "last": md.get("last_price"),
        "bid": md.get("bid"),
        "ask": md.get("ask"),
        "timestamp": md.get("timestamp") or md.get("last_update"),
        "age_seconds": md.get("age_sec"),
        "freshness": md.get("freshness") or md.get("status"),
        "quality": md.get("quality") or "UNKNOWN",
        "market_data_quality": md.get("market_data_quality") or md.get("quality") or "UNKNOWN",
        "connection": md.get("connection") or md.get("ninjatrader_market_connection"),
        "quote_timestamp": md.get("quote_timestamp") or md.get("timestamp") or md.get("last_update"),
        "snapshot_timestamp": md.get("snapshot_timestamp"),
        "fresh": bool(md.get("fresh")),
        "reason": md.get("reason"),
        "signal_instrument": md.get("signal_instrument"),
        "position_instrument": md.get("position_instrument") or (md.get("contracts") or {}).get("mnq"),
        "contracts": md.get("contracts") or {},
        "nq": md.get("nq") if isinstance(md.get("nq"), dict) else {},
        "mnq": md.get("mnq") if isinstance(md.get("mnq"), dict) else {},
        "global_simulation": md.get("global_simulation"),
        "market_provider_connected": md.get("market_provider_connected"),
        "providers": md.get("providers") if isinstance(md.get("providers"), list) else [],
        "provider_name": md.get("provider_name"),
        "provider_kind": md.get("provider_kind"),
        "provider_status": md.get("provider_status"),
        "provider_display_name": md.get("provider_display_name"),
        "provider_backend": md.get("provider_backend"),
        "provider_id": md.get("provider_id"),
        "account_environment": md.get("account_environment"),
        "PROP_EXECUTION": False,
    }


def _md_ready_for_entries(md: dict[str, Any]) -> bool:
    quality = str(md.get("quality") or "").upper()
    return md.get("status") == "LIVE" and quality == "LIVE"


def _default_soak() -> dict[str, Any]:
    return {
        "schema": SOAK_SCHEMA,
        "started_at": _iso(),
        "started_epoch": time.time(),
        "uptime_sec": 0.0,
        "market_heartbeat_count": 0,
        "market_stale_transitions": 0,
        "mcp_successful_reads": 0,
        "mcp_auth_failures": 0,
        "mcp_auth_reminders": 0,
        "mcp_refresh_events": 0,
        "signal_count": 0,
        "policy_approvals": 0,
        "policy_rejections": 0,
        "blocked_execution_decisions": 0,
        "position_mismatches": 0,
        "news_gate_blocks": 0,
        "exceptions": 0,
        "last_market_freshness": None,
        "last_mcp_status": None,
        "last_signal_key": None,
        "last_recon": None,
        "last_quote_fp": None,
        "last_mcp_read_ts": None,
        "last_auth_fail_event_ts": None,
        "PROP_EXECUTION": False,
        "order_execution": "DISABLED",
        "test": os.environ.get("AITRADE_PHASE54_TEST") == "1",
        "pnl_fabricated": False,
    }


def soak_metrics() -> dict[str, Any]:
    JOURNAL_DIR.mkdir(parents=True, exist_ok=True)
    doc = None
    raw_text = None
    if SOAK_PATH.exists():
        raw_text = SOAK_PATH.read_text(encoding="utf-8")
        try:
            parsed = json.loads(raw_text)
            doc = parsed if isinstance(parsed, dict) else None
        except json.JSONDecodeError:
            try:
                parsed, _ = json.JSONDecoder().raw_decode(raw_text)
                doc = parsed if isinstance(parsed, dict) else None
            except json.JSONDecodeError:
                doc = None
            if os.environ.get("AITRADE_PHASE54_TEST") != "1":
                closed = JOURNAL_DIR / ("soak_closed_%s.json" % datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ"))
                closed.write_text(raw_text, encoding="utf-8")
            doc = _default_soak()
            _write_json(SOAK_PATH, doc)
            started = float(doc.get("started_epoch") or time.time())
            doc["uptime_sec"] = max(0.0, time.time() - started)
            doc["PROP_EXECUTION"] = False
            doc["order_execution"] = "DISABLED"
            return doc
    if isinstance(doc, dict) and doc.get("schema") != SOAK_SCHEMA:
        if os.environ.get("AITRADE_PHASE54_TEST") != "1":
            closed = JOURNAL_DIR / ("soak_closed_%s.json" % datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ"))
            archived = dict(doc)
            archived["closed"] = True
            archived["closed_reason"] = "phase54f2_semantics_reset"
            archived["closed_at"] = _iso()
            _write_json(closed, archived)
        doc = _default_soak()
        if os.environ.get("AITRADE_PHASE54_TEST") != "1":
            _write_json(SOAK_PATH, doc)
    elif not isinstance(doc, dict):
        doc = _default_soak()
    started = float(doc.get("started_epoch") or time.time())
    doc["uptime_sec"] = max(0.0, time.time() - started)
    doc["PROP_EXECUTION"] = False
    doc["order_execution"] = "DISABLED"
    return doc


def update_soak(snap: dict[str, Any], *, exception: bool = False) -> dict[str, Any]:
    JOURNAL_DIR.mkdir(parents=True, exist_ok=True)
    doc = soak_metrics()
    if exception:
        doc["exceptions"] = int(doc.get("exceptions") or 0) + 1
    md = snap.get("market_data")
    if isinstance(md, dict):
        freshness = md.get("freshness")
    else:
        freshness = snap.get("market_data_status") or md
    prev_md = doc.get("last_market_freshness")
    quote_ts = None
    last_px = None
    if isinstance(md, dict):
        quote_ts = md.get("quote_timestamp") or md.get("timestamp")
        last_px = md.get("last")
    quote_fp = "|".join([str(quote_ts or ""), str(last_px or ""), str((md or {}).get("bid") if isinstance(md, dict) else ""), str((md or {}).get("ask") if isinstance(md, dict) else "")])
    if quote_ts and quote_fp != doc.get("last_quote_fp"):
        doc["market_heartbeat_count"] = int(doc.get("market_heartbeat_count") or 0) + 1
        doc["last_quote_fp"] = quote_fp
    if prev_md == "LIVE" and freshness in ("STALE", "DISCONNECTED", "CONNECTED_STALE", "SIMULATED", "DELAYED"):
        doc["market_stale_transitions"] = int(doc.get("market_stale_transitions") or 0) + 1
    doc["last_market_freshness"] = freshness
    mcp = snap.get("fundednext_mcp") or {}
    st = mcp.get("status")
    mcp_ts = mcp.get("timestamp")
    if st == "LIVE" and mcp_ts and mcp_ts != doc.get("last_mcp_read_ts"):
        doc["mcp_successful_reads"] = int(doc.get("mcp_successful_reads") or 0) + 1
        doc["last_mcp_read_ts"] = mcp_ts
    prev_mcp = doc.get("last_mcp_status")
    now = time.time()
    if st == "AUTH_FAILED":
        if prev_mcp != "AUTH_FAILED":
            doc["mcp_auth_failures"] = int(doc.get("mcp_auth_failures") or 0) + 1
            append_event("WARN", "FundedNext MCP auth failed", reason=mcp.get("reason") or "AUTH_FAILED")
            doc["last_auth_fail_event_ts"] = now
        else:
            last_ev = float(doc.get("last_auth_fail_event_ts") or 0)
            if now - last_ev >= AUTH_FAIL_REMINDER_SEC:
                doc["mcp_auth_reminders"] = int(doc.get("mcp_auth_reminders") or 0) + 1
                append_event("WARN", "FundedNext MCP still unauthenticated (rate-limited)", reason=mcp.get("reason") or "AUTH_FAILED")
                doc["last_auth_fail_event_ts"] = now
    if st and prev_mcp and prev_mcp != st and st == "LIVE":
        doc["mcp_refresh_events"] = int(doc.get("mcp_refresh_events") or 0) + 1
    if st:
        doc["last_mcp_status"] = st
    recon = (snap.get("position") or {}).get("reconciled")
    if recon is False and doc.get("last_recon") is not False:
        doc["position_mismatches"] = int(doc.get("position_mismatches") or 0) + 1
    if recon is not None:
        doc["last_recon"] = recon
    dec = snap.get("decision") or {}
    pol = dec.get("policy") or {}
    sig = dec.get("signal") or {}
    key = "|".join(
        [
            str(sig.get("direction") or ""),
            str(pol.get("code") or ""),
            str(pol.get("verdict") or ""),
            str((snap.get("decision") or {}).get("signal", {}).get("detail") or ""),
        ]
    )
    if sig.get("direction") and sig.get("direction") not in ("NONE", "—") and key != doc.get("last_signal_key"):
        doc["signal_count"] = int(doc.get("signal_count") or 0) + 1
        verdict = str(pol.get("verdict") or "").upper()
        code = str(pol.get("code") or "")
        if verdict in ("ALLOW", "APPROVED"):
            doc["policy_approvals"] = int(doc.get("policy_approvals") or 0) + 1
        elif verdict in ("BLOCK", "REJECT", "REJECTED"):
            doc["policy_rejections"] = int(doc.get("policy_rejections") or 0) + 1
        if "NEWS" in code.upper():
            doc["news_gate_blocks"] = int(doc.get("news_gate_blocks") or 0) + 1
        doc["blocked_execution_decisions"] = int(doc.get("blocked_execution_decisions") or 0) + 1
        doc["last_signal_key"] = key
    started = float(doc.get("started_epoch") or time.time())
    doc["uptime_sec"] = max(0.0, time.time() - started)
    doc["PROP_EXECUTION"] = False
    doc["order_execution"] = "DISABLED"
    _write_json(SOAK_PATH, doc)
    return doc


def policy_lane(state: str) -> str:
    s = (state or "").upper()
    if "NEAR" in s:
        return "NEAR"
    if "PROTECTED" in s:
        return "PROTECTED"
    if "FAST" in s:
        return "FAST"
    if "SAFE" in s:
        return "SAFE"
    if "DAILY" in s:
        return "SAFE"
    return "SAFE"


class StrategyRegistry:
    @staticmethod
    def verify_hashes() -> dict[str, Any]:
        frozen = assert_frozen()
        return {
            "ok": bool(frozen.get("ok")),
            "nq": frozen.get("nq") or FROZEN_NQ_HASH,
            "gc": frozen.get("gc") or FROZEN_GC_HASH,
            "nq_expected": FROZEN_NQ_HASH,
            "gc_expected": FROZEN_GC_HASH,
            "nq_match": (frozen.get("nq") or FROZEN_NQ_HASH) == FROZEN_NQ_HASH,
            "gc_match": (frozen.get("gc") or FROZEN_GC_HASH) == FROZEN_GC_HASH,
        }

    @staticmethod
    def active_books() -> list[dict[str, Any]]:
        h = StrategyRegistry.verify_hashes()
        return [
            {
                "id": EXPECTED_STRATEGY_ID,
                "name": "NQ Drift VWAP Pullback",
                "family": "Drift / Continuation · Phase 30",
                "hash": h["nq"],
                "hash_short": (h["nq"] or "")[:8] + "…",
                "assignment": "EVALUATION_ENABLED",
                "label": "EVALUATION ENABLED",
            },
            {
                "id": "GC_VWAP_V2",
                "name": "GC VWAP V2",
                "family": "Mean Reversion · Phase 26",
                "hash": h["gc"],
                "hash_short": (h["gc"] or "")[:8] + "…",
                "assignment": "NOT_ASSIGNED",
                "label": "Frozen · Not assigned to this account",
            },
        ]


class MarketDataMonitor:
    @staticmethod
    def last_heartbeat() -> dict[str, Any]:
        cfg = load_config()
        stale_sec = float(cfg.get("stale_market_sec") or 120)
        hb = _nt().market_heartbeat(stale_sec=stale_sec)
        contracts = hb.get("contracts") or {}
        return {
            "status": hb.get("status"),
            "freshness": hb.get("freshness") or hb.get("status"),
            "quality": hb.get("quality") or "UNKNOWN",
            "latest_bar_ts": hb.get("last_update") or hb.get("nq_mnq", {}).get("mtime"),
            "age_sec": hb.get("age_sec"),
            "last_price": hb.get("last_price"),
            "bid": hb.get("bid"),
            "ask": hb.get("ask"),
            "source": hb.get("source") or "NINJATRADER_READ_ONLY",
            "instrument": hb.get("instrument"),
            "price_feed": hb.get("price_feed"),
            "nq_mnq": hb.get("nq_mnq"),
            "ok": bool(hb.get("ok")),
            "last_update": hb.get("last_update"),
            "timestamp": hb.get("timestamp") or hb.get("last_update"),
            "connection": hb.get("ninjatrader_market_connection"),
            "ninjatrader_market_connection": hb.get("ninjatrader_market_connection"),
            "contracts": contracts,
            "signal_instrument": contracts.get("nq"),
            "position_instrument": contracts.get("mnq"),
            "reason": hb.get("reason"),
            "quote_timestamp": hb.get("quote_timestamp") or hb.get("timestamp"),
            "snapshot_timestamp": hb.get("snapshot_timestamp"),
            "fresh": bool(hb.get("fresh")),
            "addon_heartbeat_age_sec": hb.get("addon_heartbeat_age_sec"),
            "addon_heartbeat_alive": hb.get("addon_heartbeat_alive"),
            "global_simulation": hb.get("global_simulation"),
            "global_simulation_source": hb.get("global_simulation_source"),
            "providers": hb.get("providers") or [],
            "market_provider_connected": bool(hb.get("market_provider_connected")),
            "connection_dump": hb.get("connection_dump") or "",
            "nq": hb.get("nq") if isinstance(hb.get("nq"), dict) else {},
            "mnq": hb.get("mnq") if isinstance(hb.get("mnq"), dict) else {},
            "provider_name": hb.get("provider_name"),
            "provider_kind": hb.get("provider_kind"),
            "provider_status": hb.get("provider_status"),
            "provider_display_name": hb.get("provider_display_name"),
            "provider_backend": hb.get("provider_backend"),
            "provider_id": hb.get("provider_id"),
            "account_environment": hb.get("account_environment"),
            "market_data_quality": hb.get("market_data_quality") or hb.get("quality"),
        }

    @staticmethod
    def status() -> str:
        return str(MarketDataMonitor.last_heartbeat()["status"])


class BrokerAdapter:
    """FundedNext money via MCP; NT for market/position. Never submits OIF."""

    @staticmethod
    def _fn() -> tuple[NTReadOnly, Optional[dict[str, Any]]]:
        nt = _nt()
        cfg = load_config()
        acct = nt.fundednext_account(cfg.get("expected_account_id"))
        return nt, acct

    @staticmethod
    def connection_status() -> dict[str, Any]:
        nt, fn = BrokerAdapter._fn()
        sim = nt.sim101_account()
        statuses = nt._trace_account_status()
        fn_name = fn["name"] if fn else _expected_fn_name()
        mcp = _fn_mcp_snapshot()
        mcp_auth = bool(mcp.get("authenticated")) and bool((mcp.get("match") or {}).get("matched"))
        if mcp_auth and mcp.get("status") == "LIVE":
            fn_ui = "CONNECTED"
            authenticated = True
        elif mcp_auth and mcp.get("status") == "STALE":
            fn_ui = "CONNECTED"
            authenticated = False
        else:
            fn_ui = "DISCONNECTED"
            authenticated = False
        sim_conn = statuses.get("Sim101", "Disconnected")
        return {
            "fundednext": fn_ui,
            "permission": "READ_ONLY",
            "telemetry_source": "FUNDEDNEXT_MCP",
            "broker": "CONNECTED" if nt.price_feed().get("ok") else "DISCONNECTED",
            "account_id": (mcp.get("account") or {}).get("name") or fn_name,
            "platform_login": (mcp.get("account") or {}).get("platform_login"),
            "fundednext_account_id": (mcp.get("match") or {}).get("account_id"),
            "mcp_status": mcp.get("status"),
            "mcp_authenticated": bool(mcp.get("authenticated")),
            "sim101_account": sim["name"] if sim else "Sim101",
            "sim101_connection": sim_conn,
            "broker_account": fn_name,
            "authenticated": authenticated,
            "fn_detected": bool(fn) or bool((mcp.get("match") or {}).get("matched")),
            "fn_kind": fn["kind"] if fn else ("FUNDEDNEXT" if (mcp.get("match") or {}).get("matched") else None),
            "PROP_EXECUTION": False,
            "incoming_oif": nt.incoming_oif_files(),
            "tradovate_deprecated": True,
        }

    @staticmethod
    def account_snapshot() -> dict[str, Any]:
        nt, fn = BrokerAdapter._fn()
        st = EngineSupervisor._load()
        demoted = bool(st.get("demoted"))
        mcp = _fn_mcp_snapshot()
        money = mcp.get("money") or {}
        risk = mcp.get("risk") or {}
        match = mcp.get("match") or {}
        fn_name = (mcp.get("account") or {}).get("name") or (fn["name"] if fn else _expected_fn_name())
        if match.get("matched") and mcp.get("source") == "FUNDEDNEXT_MCP":
            equity = money.get("equity")
            equity_source = "FUNDEDNEXT_MCP" if equity is not None else (mcp.get("status") or "UNAVAILABLE")
        else:
            equity = None
            equity_source = mcp.get("status") or "UNAVAILABLE_FROM_FUNDEDNEXT_MCP"
        mll = risk.get("minimum_equity")
        mll_source = "FUNDEDNEXT_MCP" if mll is not None else None
        rem = risk.get("remaining_loss_buffer")
        if rem is None and equity is not None and mll is not None:
            rem = remaining_drawdown(equity, mll)
        realized = money.get("profit")
        state = live_eval_state(equity, demoted=demoted, mll=mll)
        progress = None
        if equity is not None:
            start = money.get("initial_balance") if money.get("initial_balance") is not None else START_EQUITY
            progress = (float(equity) - float(start)) / PROFIT_TARGET
        return {
            "account_id": fn_name,
            "source": mcp.get("source") or "FUNDEDNEXT_MCP",
            "equity_source": equity_source,
            "mll_source": mll_source,
            "risk_source": "FUNDEDNEXT_MCP" if mll is not None else equity_source,
            "equity": equity,
            "balance": money.get("balance"),
            "mll": mll,
            "permitted_loss": risk.get("permitted_loss"),
            "minimum_equity": mll,
            "remaining_dd": rem,
            "realized_pnl": realized,
            "unrealized_pnl": None,
            "today_pnl": realized,
            "max_dd": None if equity is None else max(0.0, START_EQUITY - float(equity)) if equity < START_EQUITY else 0.0,
            "target_progress": progress,
            "state": state,
            "demoted": demoted or state == "EVAL_PROTECTED",
            "start_equity": START_EQUITY,
            "profit_target": PROFIT_TARGET,
            "max_loss": MAX_LOSS,
            "sim101_excluded": True,
            "asof": mcp.get("timestamp"),
            "mcp_status": mcp.get("status"),
            "platform_login": (mcp.get("account") or {}).get("platform_login"),
            "fundednext_account_id": match.get("account_id"),
            "breached": (mcp.get("account") or {}).get("breached"),
            "account_status": (mcp.get("account") or {}).get("status"),
            "fresh": bool(mcp.get("fresh")),
            "rules_reconciliation": mcp.get("rules_reconciliation") or {},
        }

    @staticmethod
    def positions() -> dict[str, Any]:
        nt, fn = BrokerAdapter._fn()
        if not fn:
            return {
                "source": "ninjatrader_missing",
                "account": None,
                "instrument": "MNQ",
                "side": "FLAT",
                "quantity": 0,
                "entry": None,
                "flat": True,
                "error": "FUNDEDNEXT_ACCOUNT_NOT_DETECTED",
                "known": False,
            }
        return nt.position_for(fn["name"], fn.get("id"))

    @staticmethod
    def sim101_positions() -> dict[str, Any]:
        from sim101_telemetry import fundednext_must_not_substitute, parse_sim101_position

        nt = _nt()
        rt = nt.runtime_snapshot()
        mtime = rt.get("_mtime") if isinstance(rt, dict) else None
        primary = parse_sim101_position(rt, dump_mtime=mtime)
        return fundednext_must_not_substitute(rt, primary)

    @staticmethod
    def fundednext_account_state() -> dict[str, Any]:
        mcp = _fn_mcp_snapshot()
        acct = BrokerAdapter.account_snapshot()
        match = mcp.get("match") or {}
        money = mcp.get("money") or {}
        return {
            "account_id": acct.get("account_id"),
            "platform_login": match.get("platform_login"),
            "fundednext_account_id": match.get("account_id"),
            "equity": acct.get("equity"),
            "balance": money.get("balance"),
            "mll": acct.get("mll"),
            "remaining_loss_buffer": acct.get("remaining_dd"),
            "source": acct.get("equity_source"),
            "timestamp": mcp.get("timestamp"),
            "fresh": bool(mcp.get("fresh")),
            "mll_source": acct.get("mll_source"),
            "equity_source": acct.get("equity_source"),
            "risk_source": acct.get("risk_source"),
            "sim101_excluded": True,
            "status": mcp.get("status"),
            "match": {
                "matched": bool(match.get("matched")),
                "fundednext_name": match.get("fundednext_name"),
                "platform_login": match.get("platform_login"),
                "account_id": match.get("account_id"),
                "plan": match.get("plan"),
                "match_method": match.get("match_method"),
            },
        }


class PolicyEngine:
    @staticmethod
    def current_risk_state() -> dict[str, Any]:
        acct = BrokerAdapter.account_snapshot()
        lane = policy_lane(acct["state"])
        qty = FAST_QTY if lane == "FAST" else SAFE_QTY
        return {
            "lane": lane,
            "raw_state": acct["state"],
            "permitted_qty": qty,
            "demoted": bool(acct.get("demoted")),
            "remaining_dd": acct["remaining_dd"],
            "open_risk": 0.0,
            "today_pnl": acct.get("today_pnl"),
            "current_dd": float(acct.get("max_dd") or 0.0),
        }

    @staticmethod
    def evaluate(signal: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        acct = BrokerAdapter.account_snapshot()
        now = _now()
        events = _events()
        cal, ev = calendar_status_for(now, events)
        sig = signal or last_operator_signal()
        md = MarketDataMonitor.last_heartbeat()
        mcp = _fn_mcp_snapshot()
        if not sig:
            return {
                "verdict": "NO_SIGNAL",
                "code": "NO_SIGNAL",
                "allowed_qty": 0,
                "lane": policy_lane(acct["state"]),
                "calendar_status": cal,
                "reasons": ["no_signal"],
            }
        if str(md.get("quality") or "").upper() == "SIMULATED":
            return {
                "verdict": "BLOCK",
                "code": "MARKET_DATA_SIMULATED",
                "allowed_qty": 0,
                "lane": policy_lane(acct["state"]),
                "calendar_status": cal,
                "reasons": ["simulated_data_feed_rejected"],
                "signal": {"direction": sig.get("direction"), "strategy": "NQ DVP"},
            }
        if str(md.get("quality") or "").upper() == "DELAYED":
            return {
                "verdict": "BLOCK",
                "code": "MARKET_DATA_DELAYED",
                "allowed_qty": 0,
                "lane": policy_lane(acct["state"]),
                "calendar_status": cal,
                "reasons": ["delayed_feed"],
                "signal": {"direction": sig.get("direction"), "strategy": "NQ DVP"},
            }
        if md.get("status") != "LIVE":
            return {
                "verdict": "BLOCK",
                "code": "MARKET_DATA_NOT_LIVE",
                "allowed_qty": 0,
                "lane": policy_lane(acct["state"]),
                "calendar_status": cal,
                "reasons": [f"market_{str(md.get('status') or 'DISCONNECTED').lower()}"],
                "signal": {"direction": sig.get("direction"), "strategy": "NQ DVP"},
            }
        if mcp.get("status") != "LIVE" or not mcp.get("authenticated"):
            return {
                "verdict": "BLOCK",
                "code": "MCP_UNAVAILABLE",
                "allowed_qty": 0,
                "lane": policy_lane(acct["state"]),
                "calendar_status": cal,
                "reasons": [f"mcp_{str(mcp.get('status') or 'DISCONNECTED').lower()}"],
                "signal": {"direction": sig.get("direction"), "strategy": "NQ DVP"},
            }
        decision = evaluate_intent(
            state=acct["state"] if acct["state"] in (
                "EVAL_FAST",
                "EVAL_SAFE",
                "EVAL_PROTECTED",
                "EVAL_NEAR_TARGET",
                "EVAL_DAILY_STOPPED",
                "EVAL_BREACHED",
                "EVAL_PASSED",
                "PAUSED",
            ) else "EVAL_PROTECTED",
            intent_qty=SAFE_QTY if policy_lane(acct["state"]) != "FAST" else FAST_QTY,
            action="NEW_ENTRY",
            now=now,
            equity=acct["equity"],
            mll=acct["mll"],
            session_open_equity=acct["equity"],
            remaining_dd_open=acct["remaining_dd"],
            realized_pnl=acct["realized_pnl"],
            open_pnl=0.0,
            open_qty=0,
            last_qty=SAFE_QTY,
            consecutive_losses=0,
            demoted=bool(acct.get("demoted")),
            strategy_hash=FROZEN_NQ_HASH,
            calendar_status=cal,
            event_ts=ev,
            data_age_sec=MarketDataMonitor.last_heartbeat().get("age_sec"),
            broker_ok=BrokerAdapter.connection_status().get("authenticated", False),
            position_known=bool(BrokerAdapter.positions().get("known")),
            order_known=True,
            duplicate=False,
            daily_already_stopped=acct["state"] == "EVAL_DAILY_STOPPED",
            near_rule="PCT_95",
        )
        return {
            "verdict": decision.verdict,
            "code": decision.code,
            "allowed_qty": decision.allowed_qty,
            "lane": policy_lane(decision.state),
            "state": decision.state,
            "calendar_status": cal,
            "reasons": list(decision.reasons),
            "signal": {
                "direction": sig.get("direction"),
                "strategy": "NQ DVP",
                "entry": sig.get("intended_entry") or sig.get("entry_price"),
            },
        }


class EngineSupervisor:
    @staticmethod
    def _default_state() -> dict[str, Any]:
        return {
            "engine": "STOPPED",
            "entries_paused": True,
            "order_execution": "DISABLED",
            "mode": "DRY_RUN",
            "PROP_EXECUTION": False,
            "started_at": None,
            "heartbeat_ts": None,
            "last_safe_start": None,
            "demoted": False,
        }

    @staticmethod
    def _load() -> dict[str, Any]:
        doc = _read_json(STATE_PATH, None)
        if not isinstance(doc, dict):
            return EngineSupervisor._default_state()
        doc["order_execution"] = "DISABLED"
        doc["PROP_EXECUTION"] = False
        return doc

    @staticmethod
    def _save(doc: dict[str, Any]) -> dict[str, Any]:
        doc["order_execution"] = "DISABLED"
        doc["PROP_EXECUTION"] = False
        _write_json(STATE_PATH, doc)
        return doc

    @staticmethod
    def status() -> dict[str, Any]:
        st = EngineSupervisor._load()
        if st.get("engine") == "RUNNING":
            st["heartbeat_ts"] = _iso()
            md = MarketDataMonitor.last_heartbeat()
            mcp = _fn_mcp_snapshot()
            halt = (not _md_ready_for_entries(md)) or mcp.get("status") != "LIVE" or not mcp.get("authenticated")
            if halt:
                st["entries_paused"] = True
            EngineSupervisor._save(st)
            if sim_only_execution_armed() and os.environ.get("AITRADE_PHASE54_TEST") != "1":
                from phase55_execution_bridge import NinjaTraderExecutionBridge
                bridge = NinjaTraderExecutionBridge()
                if halt:
                    bridge.notify_disconnect()
                else:
                    rec = bridge.notify_reconnect()
                    if rec.get("status") == "FLAT_SAFE" and not st.get("entries_paused"):
                        try_execute_approved_sim_only(bridge=bridge)
            try:
                from prop_canary import canary_flag_enabled, canary_is_armed

                if canary_flag_enabled() and canary_is_armed() and os.environ.get("AITRADE_PHASE54_TEST") != "1":
                    try_execute_prop_canary(halt=halt)
            except Exception as exc:
                append_event("WARN", "PROP_CANARY loop error", error=str(exc)[:240])
            try:
                from unattended_prop_canary import (
                    context_from_ops_snapshot as unattended_ctx,
                    tick as unattended_tick,
                    unattended_flag_enabled,
                )

                if unattended_flag_enabled() and os.environ.get("AITRADE_PHASE54_TEST") != "1":
                    unattended_tick(unattended_ctx(snapshot()), transmit=not halt, allow_entry=not halt)
            except Exception as exc:
                append_event("WARN", "UNATTENDED loop error", error=str(exc)[:240])
        return st

    @staticmethod
    def start() -> dict[str, Any]:
        assert_prop_execution_disabled()
        checks = safe_start_checks()
        if not checks["ok_to_run_engine"]:
            return {"ok": False, "checks": checks, "engine": "STOPPED", "order_execution": "DISABLED"}
        st = EngineSupervisor._load()
        st["engine"] = "RUNNING"
        st["entries_paused"] = False
        st["started_at"] = _iso()
        st["heartbeat_ts"] = _iso()
        st["last_safe_start"] = checks
        st["mode"] = st.get("mode") or "DRY_RUN"
        EngineSupervisor._save(st)
        record_telemetry()
        startup: dict[str, Any] = {
            "market_connection": "verified_by_safe_start",
            "sim_only_armed": False,
            "PROP_EXECUTION": False,
        }
        if os.environ.get("AITRADE_PHASE54_TEST") != "1":
            try:
                from phase55_execution_bridge import NinjaTraderExecutionBridge

                rec = NinjaTraderExecutionBridge().reconcile()
                startup["sim101_recovery"] = rec.get("status")
                startup["sim101_position"] = rec.get("position")
            except Exception as exc:
                startup["sim101_recovery"] = "UNKNOWN_STATE"
                startup["sim101_error"] = str(exc)
            try:
                from nq_dvp_live_feed import evaluate_live_dvp

                live = evaluate_live_dvp(persist=True, consume=False)
                startup["live_dvp_pipeline"] = live.get("pipeline")
                startup["live_strategy_status"] = live.get("strategy_status")
            except Exception as exc:
                startup["live_dvp_error"] = str(exc)
        append_event("INFO", "Safe Start passed — engine running", result="ENGINE_RUNNING", **startup)
        append_event("BLOCK", "Order execution remains disabled · PROP_EXECUTION=false · SIM_ONLY not auto-armed")
        try:
            from aitrade_notifications import notify_engine_start

            notify_engine_start()
        except Exception:
            pass
        return {
            "ok": True,
            "engine": "RUNNING",
            "order_execution": "DISABLED",
            "fundednext_permission": "READ_ONLY",
            "sim_only_armed": False,
            "startup": startup,
        }

    @staticmethod
    def stop_gracefully() -> dict[str, Any]:
        st = EngineSupervisor._load()
        st["engine"] = "STOPPED"
        st["entries_paused"] = True
        EngineSupervisor._save(st)
        append_event("WARN", "Execution engine stopped by operator")
        try:
            from aitrade_notifications import notify_engine_stop_planned

            notify_engine_stop_planned("OPERATOR REQUEST")
        except Exception:
            pass
        return {"ok": True, "engine": "STOPPED", "order_execution": "DISABLED"}

    @staticmethod
    def pause_entries(paused: bool) -> dict[str, Any]:
        st = EngineSupervisor._load()
        st["entries_paused"] = bool(paused)
        EngineSupervisor._save(st)
        append_event("INFO", "New entries paused" if paused else "New entries resumed; execution still blocked")
        return {"ok": True, "entries_paused": bool(paused), "order_execution": "DISABLED"}

    @staticmethod
    def set_mode(mode: str) -> dict[str, Any]:
        mode_u = (mode or "DRY_RUN").upper().replace(" ", "_")
        st = EngineSupervisor._load()
        st["mode"] = mode_u
        st["order_execution"] = "DISABLED"
        EngineSupervisor._save(st)
        append_event("INFO", f"Execution mode set to {mode_u}. Execution permission remains disabled.")
        return {"ok": True, "mode": mode_u, "order_execution": "DISABLED", "PROP_EXECUTION": False}

    @staticmethod
    def emergency_flatten_stop() -> dict[str, Any]:
        """Stop engine. Transmit flatten only for the explicitly armed route.

        Sim101 flatten: SIM_ONLY armed. FundedNext flatten: prop canary in-flight.
        Default path does not write OIF. PROP_EXECUTION stays false.
        """
        assert_prop_execution_disabled()
        incoming = _nt().incoming_oif_files()
        st = EngineSupervisor._load()
        st["engine"] = "STOPPED"
        st["entries_paused"] = True
        EngineSupervisor._save(st)
        try:
            from aitrade_notifications import notify_emergency_flatten
        except Exception:
            notify_emergency_flatten = None  # type: ignore[assignment]
        try:
            from prop_canary import canary_in_flight, emergency_flatten as fn_canary_flatten
        except Exception:
            canary_in_flight = lambda: False  # type: ignore[assignment]
            fn_canary_flatten = None  # type: ignore[assignment]
        if canary_in_flight() and fn_canary_flatten and os.environ.get("AITRADE_PHASE54_TEST") != "1":
            flat = fn_canary_flatten(transmit=True)
            append_event(
                "WARN",
                "Emergency flatten FundedNext canary",
                transmitted=bool(flat.get("submitted")),
                account="FNFTCHTANATSWAPHILMU92044",
                PROP_EXECUTION=False,
            )
            return {
                "ok": bool(flat.get("ok")),
                "engine": "STOPPED",
                "order_execution": "DISABLED",
                "flatten": flat.get("result"),
                "account": "FNFTCHTANATSWAPHILMU92044",
                "PROP_EXECUTION": False,
                "incoming_oif": incoming,
                "prop_canary": True,
            }
        if sim_only_execution_armed() and os.environ.get("AITRADE_PHASE54_TEST") != "1":
            from phase55_execution_bridge import NinjaTraderExecutionBridge
            flat = NinjaTraderExecutionBridge().emergency_flatten(account="Sim101", transmit=True)
            append_event(
                "WARN",
                "Emergency flatten Sim101 · " + str(flat.get("flatten")),
                flatten=flat.get("flatten"),
                transmitted=bool(flat.get("submitted")),
                confirmed=bool(flat.get("confirmed")),
                PROP_EXECUTION=False,
            )
            if notify_emergency_flatten:
                try:
                    notify_emergency_flatten(
                        transmitted=bool(flat.get("submitted")),
                        detail=str(flat.get("flatten") or ""),
                    )
                except Exception:
                    pass
            return {
                "ok": bool(flat.get("ok")),
                "engine": "STOPPED",
                "order_execution": "DISABLED",
                "flatten": flat.get("flatten"),
                "orders_transmitted": int(flat.get("orders_transmitted") or 0),
                "incoming_oif": incoming,
                "sim_only": True,
                "PROP_EXECUTION": False,
                "broker_ack": flat.get("broker_ack"),
                "position_after": flat.get("position_after"),
            }
        append_event(
            "WARN",
            "Emergency flatten requested — not transmitted · PROP_EXECUTION=false",
            flatten="REQUESTED_NOT_TRANSMITTED",
            incoming_oif=incoming,
        )
        if notify_emergency_flatten:
            try:
                notify_emergency_flatten(transmitted=False, detail="REQUESTED_NOT_TRANSMITTED")
            except Exception:
                pass
        return {
            "ok": True,
            "engine": "STOPPED",
            "order_execution": "DISABLED",
            "flatten": "REQUESTED_NOT_TRANSMITTED",
            "orders_transmitted": 0,
            "incoming_oif": incoming,
        }


def last_live_signal() -> Optional[dict[str, Any]]:
    row = _last_jsonl_obj(SIGNALS_LOG)
    if not row:
        return None
    return row


def journal_blocked_live_signal(sig: dict[str, Any], policy: dict[str, Any]) -> None:
    JOURNAL_DIR.mkdir(parents=True, exist_ok=True)
    row = {
        "ts": _iso(),
        "direction": sig.get("direction"),
        "intended_entry": sig.get("intended_entry"),
        "trading_date": sig.get("trading_date"),
        "policy_verdict": policy.get("verdict"),
        "policy_code": policy.get("code"),
        "qty": policy.get("allowed_qty"),
        "lane": policy.get("lane"),
        "execution": "BLOCKED",
        "detail": "PROP_EXECUTION=false",
        "PROP_EXECUTION": False,
        "source": sig.get("source") or "live",
    }
    last = _last_jsonl_obj(SIGNALS_LOG)
    key = f"{row.get('direction')}|{row.get('intended_entry')}|{row.get('trading_date')}|{row.get('policy_code')}"
    last_key = ""
    if last:
        last_key = f"{last.get('direction')}|{last.get('intended_entry')}|{last.get('trading_date')}|{last.get('policy_code')}"
    if key != last_key:
        with SIGNALS_LOG.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, default=str) + "\n")
        append_event(
            "BLOCK",
            (
                f"LIVE SIGNAL {row['direction']} → POLICY {row['policy_verdict']} "
                f"qty={row['qty']} {row['lane']} {row['policy_code']} → BLOCKED · PROP_EXECUTION=false"
            ),
            **row,
        )


def last_operator_signal() -> Optional[dict[str, Any]]:
    """Executable operator signal is live DVP only. Shadow is never returned here."""
    ev = live_dvp_status()
    sig = ev.get("live_signal") if isinstance(ev, dict) else None
    if sig and sig.get("source") == "phase54_live" and sig.get("live_bar"):
        return {
            "direction": sig.get("direction"),
            "intended_entry": sig.get("intended_entry"),
            "trading_date": sig.get("trading_date"),
            "ts": sig.get("ts") or sig.get("signal_timestamp"),
            "accepted": True,
            "quantity_allowed": 1,
            "rejection_reason": None,
            "account_state": None,
            "source": "phase54_live",
            "live_bar": True,
            "signal_id": sig.get("signal_id"),
            "bar_identity": sig.get("bar_identity"),
            "strategy_hash": sig.get("strategy_hash"),
            "executable": sig.get("executable"),
        }
    live = last_live_signal()
    if live and live.get("source") == "phase54_live" and live.get("live_bar") is True:
        live = dict(live)
        live["source"] = "phase54_live"
        return live
    return None


def try_execute_approved_sim_only(*, bridge: Any = None) -> dict[str, Any]:
    """Phase 54 APPROVED → existing Sim101 ATI. No-op unless SIM_ONLY is armed.

    Quantity is forced to 1 MNQ. FundedNext is never the destination account.
    """
    from phase55_execution_bridge import NinjaTraderExecutionBridge

    if not sim_only_execution_armed():
        return {"ok": False, "submitted": False, "error_code": "SIM_ONLY_NOT_ARMED", "PROP_EXECUTION": False}
    if PROP_EXECUTION or prop_execution_allowed():
        return {"ok": False, "submitted": False, "error_code": "PROP_EXECUTION_FORBIDDEN_PHASE55A"}
    eng = EngineSupervisor._load()
    if eng.get("engine") != "RUNNING" or eng.get("entries_paused"):
        return {"ok": False, "submitted": False, "error_code": "ENGINE_NOT_READY", "PROP_EXECUTION": False}
    sig = last_operator_signal()
    source = str((sig or {}).get("source") or "")
    if source != "phase54_live" or (sig or {}).get("live_bar") is False or (sig or {}).get("source") == "HISTORICAL_WARMUP":
        return {
            "ok": False,
            "submitted": False,
            "error_code": "LIVE_DVP_REQUIRED",
            "source": source or "none",
            "PROP_EXECUTION": False,
            "note": "Phase 55B.1 refuses shadow/historical operator signals",
        }
    pol = PolicyEngine.evaluate(sig)
    if str(pol.get("verdict") or "").upper() not in ("ALLOW", "APPROVED"):
        return {
            "ok": False,
            "submitted": False,
            "error_code": "POLICY_NOT_APPROVED",
            "policy": pol,
            "PROP_EXECUTION": False,
        }
    md = MarketDataMonitor.last_heartbeat()
    adapter = bridge or NinjaTraderExecutionBridge()
    intent = {
        "direction": (sig or {}).get("direction"),
        "account": "Sim101",
        "instrument": md.get("instrument") or "MNQ 09-26",
        "quantity": 1,
        "strategy_id": EXPECTED_STRATEGY_ID,
        "strategy_hash": FROZEN_NQ_HASH,
        "policy_verdict": pol.get("verdict"),
        "policy_code": pol.get("code"),
        "calendar_status": pol.get("calendar_status"),
        "reasons": pol.get("reasons"),
        "data_age_sec": md.get("age_sec"),
        "signal_ts": (sig or {}).get("ts"),
        "trigger_key": "|".join(
            [
                str((sig or {}).get("direction") or ""),
                str((sig or {}).get("intended_entry") or ""),
                str((sig or {}).get("trading_date") or ""),
            ]
        ),
        "trade_id": "AITRADE_DVP_"
        + str((sig or {}).get("trading_date") or "na")
        + "_"
        + str((sig or {}).get("direction") or "X"),
        "nt_connected": _md_ready_for_entries(md),
        "mode": "SIM_ONLY",
        "lane": pol.get("lane"),
        "news_blocked": "NEWS" in str(pol.get("code") or "").upper(),
        "prop_blocked": False,
        "duplicate": False,
    }
    result = adapter.submit(intent, transmit=True)
    append_event(
        "INFO" if result.get("submitted") else "BLOCK",
        "SIM_ONLY submit " + str(result.get("status") or result.get("error_code")),
        submitted=bool(result.get("submitted")),
        account="Sim101",
        error_code=result.get("error_code"),
        PROP_EXECUTION=False,
    )
    try:
        from aitrade_notifications import notify_submit_result

        intent["source"] = source
        notify_submit_result(result, intent=intent)
    except Exception:
        pass
    return result


def try_execute_prop_canary(*, halt: bool = False) -> dict[str, Any]:
    """One-shot FundedNext canary. Separate from Sim101. Never enables PROP_EXECUTION."""
    from prop_canary import (
        canary_flag_enabled,
        canary_is_armed,
        context_from_ops_snapshot,
        observe_runtime,
        submit_once,
    )

    if not canary_flag_enabled() or not canary_is_armed():
        return {"ok": False, "submitted": False, "error_code": "NOT_ARMED", "PROP_EXECUTION": False}
    snap = snapshot()
    ctx = context_from_ops_snapshot(snap)
    observe_runtime(ctx)
    if halt or not canary_is_armed():
        return {"ok": False, "submitted": False, "error_code": "DISARMED_OR_HALT", "PROP_EXECUTION": False}
    result = submit_once(ctx, transmit=True)
    append_event(
        "INFO" if result.get("submitted") else "BLOCK",
        "PROP_CANARY submit " + str(result.get("error_code") or result.get("state") or result.get("status")),
        submitted=bool(result.get("submitted")),
        account="FNFTCHTANATSWAPHILMU92044",
        error_code=result.get("error_code"),
        PROP_EXECUTION=False,
    )
    return result


def last_shadow_signal() -> Optional[dict[str, Any]]:
    row = _last_jsonl_obj(AUDIT_PATH) or _last_jsonl_obj(AUDIT_PATH_LIVE)
    if not row:
        return None
    sig = row.get("signal") if isinstance(row.get("signal"), dict) else row
    return {
        "direction": sig.get("direction"),
        "intended_entry": sig.get("intended_entry") or sig.get("entry_price"),
        "stop": sig.get("stop") or sig.get("stop_price"),
        "target": sig.get("target") or sig.get("target_price"),
        "trading_date": sig.get("trading_date"),
        "ts": row.get("market_timestamp"),
        "accepted": row.get("accepted"),
        "quantity_allowed": row.get("quantity_allowed"),
        "rejection_reason": row.get("rejection_reason"),
        "account_state": row.get("account_state") or row.get("policy_state"),
        "source": "phase53_shadow",
    }


def decision_trace() -> dict[str, Any]:
    sig = last_operator_signal()
    live = PolicyEngine.evaluate(sig)
    direction = (sig or {}).get("direction") or "NONE"
    source = (sig or {}).get("source") or "none"
    last_px_note = (sig or {}).get("ts") or "no live DVP signal"
    lane = str(live.get("lane") or policy_lane((sig or {}).get("account_state") or "EVAL_FAST"))
    if live.get("verdict") == "ALLOW":
        qty = int(live.get("allowed_qty") or 0)
        policy_txt = f"APPROVED · {qty} MNQ · {lane}"
        verdict = "ALLOW"
        detail = live.get("code") or "Risk + prop policy accepted"
        code = live.get("code")
        qty_out = qty
    elif sig and live.get("verdict"):
        policy_txt = f"REJECTED · {live.get('code')} · {lane}"
        verdict = "BLOCK"
        detail = str(live.get("code") or "blocked")
        code = live.get("code")
        qty_out = 0
    elif sig and sig.get("accepted"):
        qty = int(sig.get("quantity_allowed") or SAFE_QTY)
        policy_txt = f"APPROVED · {qty} MNQ · {lane}"
        verdict = "ALLOW"
        detail = "Risk + prop policy accepted"
        code = "ALLOW"
        qty_out = qty
    elif sig:
        policy_txt = f"REJECTED · {sig.get('rejection_reason') or live.get('code')} · {lane}"
        verdict = "BLOCK"
        detail = str(sig.get("rejection_reason") or live.get("code") or "blocked")
        code = sig.get("rejection_reason") or live.get("code")
        qty_out = 0
    else:
        policy_txt = f"NO SIGNAL · {lane}"
        verdict = "NO_SIGNAL"
        detail = "idle"
        code = "NO_SIGNAL"
        qty_out = 0
    if sig and source == "phase54_live":
        journal_blocked_live_signal(sig, live)
    elif sig and source != "phase53_shadow" and live:
        journal_blocked_live_signal(sig, live)
    armed = sim_only_execution_armed()
    shadow = last_shadow_signal()
    live_sig = sig if source == "phase54_live" else None
    md = MarketDataMonitor.last_heartbeat()
    age = md.get("age_sec")
    if age is not None:
        try:
            age = float(age)
        except (TypeError, ValueError):
            age = None
    return {
        "signal": {
            "label": f"NQ DVP · {direction}" if direction and direction != "NONE" else "NQ DVP · NONE",
            "direction": direction,
            "detail": f"{last_px_note} · {source}",
            "source": source,
            "kind": "LIVE" if source == "phase54_live" else "NONE",
        },
        "last_live_signal": live_sig,
        "last_shadow_signal": shadow,
        "signal_source": "LIVE" if source == "phase54_live" else "NONE",
        "policy": {
            "label": policy_txt,
            "verdict": verdict,
            "qty": qty_out,
            "lane": lane,
            "code": code,
            "detail": detail,
            "live_recheck": live,
        },
        "execution": {
            "label": "SIM_ONLY ARMED" if armed else "DISARMED",
            "verdict": "BLOCKED",
            "detail": "PROP_EXECUTION=false",
            "arm": "SIM_ONLY ARMED" if armed else "DISARMED",
        },
        "heartbeat": {
            "label": f"{age:.1f}s · {md.get('status')}" if age is not None else str(md.get("status") or "—"),
            "detail": f"{md.get('instrument') or 'MNQ/NQ'} · {md.get('source') or 'ninjatrader'}",
            "age_sec": age,
            "status": md.get("status"),
            "source": md.get("source"),
            "instrument": md.get("instrument"),
            "last_update": md.get("last_update"),
        },
    }


def record_telemetry() -> None:
    acct = BrokerAdapter.account_snapshot()
    if acct.get("equity") is None or acct.get("equity_source") != "FUNDEDNEXT_MCP":
        return
    JOURNAL_DIR.mkdir(parents=True, exist_ok=True)
    row = {
        "ts": _iso(),
        "timestamp": _iso(),
        "account_id": acct.get("account_id"),
        "balance": acct.get("balance"),
        "equity": acct["equity"],
        "profit": acct.get("realized_pnl"),
        "pnl": acct.get("realized_pnl"),
        "remaining_loss_buffer": acct.get("remaining_dd"),
        "remaining_dd": acct.get("remaining_dd"),
        "source": "FUNDEDNEXT_MCP",
        "unrealized": acct.get("unrealized_pnl"),
    }
    last = _last_jsonl_obj(TELEMETRY_PATH)
    if last and last.get("source") == "FUNDEDNEXT_MCP" and abs(float(last.get("equity") or 0) - acct["equity"]) < 1e-9 and last.get("pnl") == row["pnl"]:
        ts = str(last.get("ts") or last.get("timestamp") or "")
        if ts:
            try:
                if (_now() - datetime.fromisoformat(ts)).total_seconds() < 50:
                    return
            except ValueError:
                pass
    with TELEMETRY_PATH.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row) + "\n")


def telemetry_series(range_key: str = "ALL") -> dict[str, Any]:
    rows = _tail_jsonl(TELEMETRY_PATH, 2000)
    if not rows:
        acct = BrokerAdapter.account_snapshot()
        if acct.get("equity") is None:
            return {
                "points": [],
                "labels": [],
                "net": None,
                "high": None,
                "low": None,
                "source": "journal/phase54_ops/telemetry.jsonl — no FundedNext MCP equity snapshots yet",
                "range": range_key,
                "n": 0,
            }
        rows = [{"ts": _iso(), "equity": acct["equity"], "pnl": acct["realized_pnl"]}]
    pnls = [float(r.get("pnl") or 0) for r in rows]
    labels = []
    for r in rows:
        ts = str(r.get("ts") or "")
        labels.append(ts[11:16] if len(ts) >= 16 else ts[-8:-3])
    net = pnls[-1] if pnls else 0.0
    return {
        "points": pnls,
        "labels": labels[:6] if len(labels) > 6 else labels,
        "net": net,
        "high": max(pnls) if pnls else 0.0,
        "low": min(pnls) if pnls else 0.0,
        "source": "journal/phase54_ops/telemetry.jsonl from FundedNext MCP snapshots",
        "range": range_key,
        "n": len(pnls),
    }


def reconcile_position() -> dict[str, Any]:
    broker = BrokerAdapter.positions()
    md = MarketDataMonitor.last_heartbeat()
    engine_qty = 0
    engine_side = "FLAT"
    last_sig = last_operator_signal()
    stop = None
    if last_sig and last_sig.get("accepted") and last_sig.get("stop"):
        stop = last_sig.get("stop")
    broker_qty = int(broker.get("quantity") or 0)
    broker_side = broker.get("side") or "FLAT"
    mcp = _fn_mcp_snapshot()
    mcp_pos = mcp.get("position") or {}
    mcp_side = mcp_pos.get("side") or "FLAT"
    mcp_qty = int(mcp_pos.get("quantity") or 0)
    mcp_known = bool(mcp_pos.get("known"))
    mcp_usable = mcp.get("status") in ("LIVE", "STALE") and mcp_known
    if mcp_usable:
        if broker.get("known") is False:
            matched = False
            note = "NinjaTrader FundedNext position unread; cannot complete three-way recon"
        else:
            matched = (
                broker_side == engine_side == mcp_side
                and broker_qty == engine_qty == mcp_qty
            )
            note = "Three-way matched" if matched else "NinjaTrader / FundedNext MCP / engine position mismatch"
    else:
        matched = False
        note = "FundedNext MCP position unavailable; cannot complete three-way recon"
    last_px = md.get("last_price")
    entry = broker.get("entry")
    unreal = None
    if last_px is not None and entry is not None and broker_qty:
        sign = 1 if broker_side == "LONG" else -1
        unreal = (last_px - float(entry)) * sign * 2.0 * broker_qty
    open_risk = abs(broker_qty) * UNIT_RISK_USD if broker_qty else 0.0
    return {
        "instrument": broker.get("instrument") or "MNQ",
        "side": broker_side if broker.get("known") else engine_side,
        "quantity": broker_qty if broker.get("known") else engine_qty,
        "entry": entry,
        "last": last_px,
        "unrealized": unreal if unreal is not None else 0.0,
        "stop": stop,
        "open_risk": open_risk,
        "reconciled": matched,
        "note": note,
        "broker_error": broker.get("error"),
        "broker_side": broker_side,
        "broker_qty": broker_qty,
        "expected_side": engine_side,
        "expected_qty": engine_qty,
        "mcp_side": mcp_side,
        "mcp_qty": mcp_qty,
        "ninjatrader": {"side": broker_side, "quantity": broker_qty, "known": bool(broker.get("known"))},
        "mcp": {
            "side": mcp_side,
            "quantity": mcp_qty,
            "known": mcp_known,
            "status": mcp.get("status"),
            "source": "FUNDEDNEXT_MCP",
        },
        "expected": {"side": engine_side, "quantity": engine_qty},
        "tradovate_deprecated": True,
    }


def safe_start_checks() -> dict[str, Any]:
    md = MarketDataMonitor.last_heartbeat()
    conn = BrokerAdapter.connection_status()
    acct = BrokerAdapter.account_snapshot()
    hashes = StrategyRegistry.verify_hashes()
    recon = reconcile_position()
    rules_ok = PROP_POLICY_PATH.exists()
    try:
        prof = load_profile("FUNDEDNEXT_FLEX_50K")
        rules_ok = rules_ok and prof is not None
    except Exception:
        rules_ok = False
    cfg = load_config()
    expected = cfg.get("expected_account_id")
    mcp = _fn_mcp_snapshot()
    match = mcp.get("match") or {}
    fn_name = str(match.get("fundednext_name") or conn.get("account_id") or "")
    account_ok = (
        bool(match.get("matched"))
        and fn_name == _expected_fn_name()
        and str(match.get("platform_login") or "") == str((cfg.get("fundednext_mcp") or {}).get("expected_login") or "962841277")
        and str(match.get("account_id") or "") == str((cfg.get("fundednext_mcp") or {}).get("expected_account_id") or "3969349")
        and fn_name != "Sim101"
        and not bool((mcp.get("account") or {}).get("breached"))
        and str((mcp.get("account") or {}).get("status") or "ACTIVE").upper() == "ACTIVE"
    )
    if expected not in (None, "AUTO", "AUTO_FUNDEDNEXT"):
        account_ok = account_ok and fn_name == expected
    recon_rules = mcp.get("rules_reconciliation") or acct.get("rules_reconciliation") or {}
    if match.get("matched") and mcp.get("status") in ("LIVE", "STALE"):
        rules_ok = rules_ok and bool(recon_rules.get("survival_critical_ok", recon_rules.get("rules_match")))
    equity_ok = (
        acct.get("equity") is not None
        and acct.get("mll") is not None
        and acct.get("equity_source") == "FUNDEDNEXT_MCP"
        and acct.get("risk_source") == "FUNDEDNEXT_MCP"
    )
    pol = load_operating_policy()
    exec_checked = (not pol.broker_execution) and (not prop_execution_allowed()) and ("PROP_EVALUATION" in BLOCKED_MODES)
    events = _events()
    cal, _ = calendar_status_for(_now(), events)
    news_ok = cal == "OK"
    nt, fn = BrokerAdapter._fn()
    stale_orders = bool(fn and nt.working_orders(fn.get("id")))
    md_ok = _md_ready_for_entries(md)
    checks = {
        "fresh_market_data": bool(md_ok),
        "fundednext_authenticated": bool(conn.get("authenticated")),
        "correct_account_id": bool(account_ok),
        "equity_mll_available": bool(equity_ok),
        "broker_positions_reconciled": bool(recon.get("reconciled")),
        "prop_rules_loaded": bool(rules_ok),
        "frozen_nq_hash_verified": bool(hashes.get("nq_match")),
        "no_stale_orders": not stale_orders,
        "risk_limits_valid": UNIT_RISK_USD == 160.0 and STOP_POINTS == 80.0,
        "news_gate_valid": bool(news_ok),
        "execution_permission_checked": bool(exec_checked),
    }
    display = {k: ("PASS" if v else "FAIL") for k, v in checks.items()}
    if exec_checked:
        display["execution_permission_checked"] = "PASS"
    engine_ok = all(v for k, v in checks.items() if k != "execution_permission_checked") and exec_checked
    return {
        "checks": checks,
        "display": display,
        "ok_to_run_engine": engine_ok,
        "order_execution": "DISABLED",
        "execution_permission_value": "DISABLED",
        "safe_start_result": "ENGINE_MAY_RUN" if engine_ok else "SAFE_START_FAILED",
        "market_data_status": md.get("status"),
        "PROP_EXECUTION": False,
        "pause": is_execution_paused(),
    }


def recent_events(n: int = 25) -> list[dict[str, Any]]:
    rows = _tail_jsonl(EVENTS_LOG, n)
    if len(rows) < 8:
        extra = _tail_jsonl(AUDIT_PATH, 8) or _tail_jsonl(AUDIT_PATH_LIVE, 8)
        for a in extra:
            rows.append(
                {
                    "ts": a.get("market_timestamp") or a.get("ingestion_timestamp"),
                    "level": "BLOCK" if not a.get("accepted") else "INFO",
                    "message": (
                        f"Shadow {'accepted' if a.get('accepted') else 'rejected'} "
                        f"qty={a.get('quantity_allowed')} {a.get('rejection_reason') or 'ALLOW'}"
                    ),
                }
            )
    rows.sort(key=lambda r: str(r.get("ts") or ""), reverse=True)
    return rows[:n]


_WATCH: dict[str, Any] = {}


def _journal_watch(snap: dict[str, Any]) -> None:
    recon = (snap.get("position") or {}).get("reconciled")
    md = snap.get("market_data")
    md_status = md.get("freshness") if isinstance(md, dict) else snap.get("market_data_status") or md
    cur = {
        "market_data": md_status,
        "fundednext": snap.get("fundednext_connection"),
        "equity": (snap.get("account") or {}).get("equity"),
        "mll": (snap.get("account") or {}).get("mll"),
        "reconciled": recon,
        "account_id": (snap.get("account") or {}).get("account_id"),
    }
    prev = dict(_WATCH)
    _WATCH.update(cur)
    if prev.get("market_data") != cur["market_data"]:
        lvl = "WARN" if cur["market_data"] in ("STALE", "DISCONNECTED", "CONNECTED_STALE", "SIMULATED", "DELAYED") else "INFO"
        append_event(lvl, f"Market-data freshness → {cur['market_data']}", **(snap.get("market") or {}))
    if prev.get("fundednext") != cur["fundednext"]:
        append_event("INFO", f"FundedNext MCP connection → {cur['fundednext']}", account_id=cur["account_id"])
    if prev.get("equity") != cur["equity"] or prev.get("mll") != cur["mll"]:
        append_event(
            "INFO",
            "FundedNext MCP equity/risk snapshot",
            equity=cur["equity"],
            mll=cur["mll"],
            remaining_loss_buffer=(snap.get("account") or {}).get("remaining_dd"),
            source=(snap.get("account") or {}).get("equity_source"),
        )
    mcp_pub = snap.get("fundednext_mcp") or {}
    mcp_status = mcp_pub.get("status")
    if prev.get("mcp_status") != mcp_status:
        append_event(
            "INFO",
            f"FundedNext MCP telemetry → {mcp_status}",
            matched=mcp_pub.get("matched"),
            match_method=(mcp_pub.get("match") or {}).get("match_method"),
        )
    _WATCH["mcp_status"] = mcp_status
    rules = (snap.get("account") or {}).get("rules_reconciliation") or {}
    match_flag = bool(rules.get("rules_match"))
    if prev.get("rules_match") != match_flag:
        lvl = "WARN" if not match_flag else "INFO"
        append_event(lvl, "FundedNext MCP vs PROP_RULES_V1 reconciliation", **{k: rules[k] for k in ("rules_match", "mismatches", "survival_critical_ok") if k in rules})
    _WATCH["rules_match"] = match_flag
    if recon is False and prev.get("reconciled") is not False:
        append_event("WARN", "Three-way FundedNext position reconciliation NO — system degraded", **(snap.get("position") or {}))
    if recon is True and prev.get("reconciled") is False:
        append_event("INFO", "Three-way FundedNext position reconciliation YES")
    ss = (snap.get("checks") or {}).get("safe_start_result")
    if prev.get("safe_start") != ss and ss:
        append_event("INFO" if ss == "ENGINE_MAY_RUN" else "WARN", f"Safe Start → {ss}")
    _WATCH["safe_start"] = ss
    try:
        from aitrade_notifications import observe_snapshot_safe

        observe_snapshot_safe(snap)
    except Exception:
        pass


def snapshot() -> dict[str, Any]:
    record_telemetry()
    md = MarketDataMonitor.last_heartbeat()
    conn = BrokerAdapter.connection_status()
    acct = BrokerAdapter.account_snapshot()
    risk = PolicyEngine.current_risk_state()
    eng = EngineSupervisor.status()
    recon = reconcile_position()
    health = _read_json(HEALTH_PATH, {}) or {}
    checks = safe_start_checks()
    mcp = _fn_mcp_snapshot()
    mcp_pub = {
        "status": mcp.get("status"),
        "authenticated": mcp.get("authenticated"),
        "matched": bool((mcp.get("match") or {}).get("matched")),
        "match": mcp.get("match") or {},
        "age_sec": mcp.get("age_sec"),
        "timestamp": mcp.get("timestamp"),
        "source": "FUNDEDNEXT_MCP",
        "permission": "READ_ONLY",
        "account": mcp.get("account") or {},
        "money": mcp.get("money") or {},
        "risk": mcp.get("risk") or {},
        "rules_reconciliation": mcp.get("rules_reconciliation") or {},
        "position": mcp.get("position") or {},
        "reason": mcp.get("reason"),
        "oauth": oauth_session_metadata(),
    }
    rem = acct.get("remaining_dd")
    buffer_pct = (float(rem) / MAX_LOSS * 100.0) if rem is not None and MAX_LOSS else 0.0
    used_pct = max(0.0, 100.0 - buffer_pct)
    rules_ok = bool((mcp.get("rules_reconciliation") or {}).get("survival_critical_ok", False))
    policy_ok = (
        checks["checks"].get("prop_rules_loaded")
        and checks["checks"].get("frozen_nq_hash_verified")
        and acct.get("equity") is not None
        and acct.get("mll") is not None
        and acct.get("equity_source") == "FUNDEDNEXT_MCP"
        and recon.get("reconciled") is True
        and mcp.get("status") == "LIVE"
        and _md_ready_for_entries(md)
        and rules_ok
    )
    md_detail = _md_detail(md)
    sim101 = BrokerAdapter.sim101_positions()
    live_dvp = live_dvp_status()
    from sim101_telemetry import recovery_from_sim101

    sim_recovery = recovery_from_sim101(sim101, expected_flat=True, aittrade_orders=0)
    rt_dump = _nt().runtime_snapshot() or {}
    dump_mtime = rt_dump.get("_mtime")
    try:
        dump_age = max(0.0, time.time() - float(dump_mtime)) if dump_mtime is not None else None
    except (TypeError, ValueError):
        dump_age = None
    nq_bars = rt_dump.get("nq_bars_1m") if isinstance(rt_dump.get("nq_bars_1m"), list) else []
    last_nq_bar = nq_bars[-1] if nq_bars else None
    last_nq_bar_ts = None
    if isinstance(last_nq_bar, dict):
        last_nq_bar_ts = last_nq_bar.get("iso_et") or last_nq_bar.get("time")
    elif isinstance(live_dvp, dict) and (live_dvp.get("last_finalized_5m") or {}).get("iso_et"):
        last_nq_bar_ts = live_dvp["last_finalized_5m"]["iso_et"]
    out = {
        "phase": 54,
        "phase_id": "54F",
        "PROP_EXECUTION": False,
        "order_execution": "DISABLED",
        "engine": eng.get("engine"),
        "entries_paused": eng.get("entries_paused"),
        "mode": eng.get("mode") or "DRY_RUN",
        "market_data": md_detail,
        "market_data_status": md.get("status"),
        "market_data_source": md_detail.get("source") or "NINJATRADER_READ_ONLY",
        "market_data_quality": md.get("quality"),
        "market_data_connection": md.get("ninjatrader_market_connection") or md_detail.get("connection"),
        "provider_name": md.get("provider_name") or md_detail.get("provider_name"),
        "provider_kind": md.get("provider_kind") or md_detail.get("provider_kind"),
        "provider_status": md.get("provider_status") or md_detail.get("provider_status"),
        "provider_display_name": md.get("provider_display_name") or md_detail.get("provider_display_name"),
        "provider_backend": md.get("provider_backend") or md_detail.get("provider_backend"),
        "provider_id": md.get("provider_id") or md_detail.get("provider_id"),
        "account_environment": md.get("account_environment") or md_detail.get("account_environment"),
        "market_instrument": md.get("instrument"),
        "market_last": md.get("last_price"),
        "market_bid": md.get("bid"),
        "market_ask": md.get("ask"),
        "market_timestamp": md.get("quote_timestamp") or md.get("timestamp"),
        "market_age_seconds": md.get("age_sec"),
        "market_data_reason": md.get("reason"),
        "fundednext_account_status": conn.get("fundednext"),
        "ninjatrader_market_connection": md.get("ninjatrader_market_connection") or md_detail.get("connection"),
        "market_data_freshness": md.get("freshness") or md.get("status"),
        "fundednext_connection": conn.get("fundednext"),
        "fundednext_permission": conn.get("permission"),
        "policy_engine": "ACTIVE" if policy_ok else "DEGRADED",
        "heartbeat_ts": eng.get("heartbeat_ts"),
        "paused_project": execution_summary(),
        "account": acct,
        "fundednext_account_state": BrokerAdapter.fundednext_account_state(),
        "connection": conn,
        "position": recon,
        "risk": {
            **risk,
            "today_pnl": acct.get("today_pnl") if acct.get("today_pnl") is not None else risk.get("today_pnl"),
            "open_risk": recon.get("open_risk") or 0.0,
            "buffer_remaining_pct": buffer_pct,
            "buffer_used_pct": used_pct,
            "permitted_label": f"{risk['permitted_qty']} MNQ permitted",
        },
        "books": StrategyRegistry.active_books(),
        "hashes": StrategyRegistry.verify_hashes(),
        "decision": decision_trace(),
        "telemetry": telemetry_series("ALL"),
        "health": {
            "wr": health.get("wr"),
            "class": health.get("class"),
            "n": health.get("n"),
        },
        "checks": checks,
        "events": recent_events(),
        "market": md,
        "sim101": sim101,
        "sim101_recovery": sim_recovery,
        "telemetry_dump": {
            "timestamp": rt_dump.get("timestamp") or rt_dump.get("ts") or md.get("snapshot_timestamp"),
            "age_sec": dump_age if dump_age is not None else md.get("addon_heartbeat_age_sec"),
            "alive": (dump_age is not None and dump_age <= 5.0) if dump_age is not None else bool(md.get("addon_heartbeat_alive")),
            "nq_bars_1m_status": rt_dump.get("nq_bars_1m_status"),
            "nq_bars_1m_count": rt_dump.get("nq_bars_1m_count") if rt_dump.get("nq_bars_1m_count") is not None else len(nq_bars),
            "last_nq_bar_ts": last_nq_bar_ts,
        },
        "live_dvp": live_dvp,
        "live_strategy_status": (live_dvp or {}).get("strategy_status") if isinstance(live_dvp, dict) else None,
        "execution_arm": "SIM_ONLY ARMED" if sim_only_execution_armed() else "DISARMED",
        "last_shadow_signal": last_shadow_signal(),
        "fundednext_mcp": mcp_pub,
        "tradovate": {
            "status": "DEPRECATED_FALLBACK_DISABLED",
            "source": "TRADOVATE_READ_ONLY",
            "used_for_money": False,
            "note": "Phase 54E: FundedNext MCP is authoritative for FundedNext account/risk telemetry.",
        },
        "fundednext": {
            "source": "FUNDEDNEXT_MCP",
            "account_id": acct.get("account_id"),
            "platform_login": acct.get("platform_login"),
            "fundednext_account_id": acct.get("fundednext_account_id"),
            "connected": conn.get("fundednext") == "CONNECTED",
            "permission": "READ_ONLY",
            "balance": acct.get("balance"),
            "equity": acct.get("equity"),
            "mll": acct.get("mll"),
            "permitted_loss": acct.get("permitted_loss"),
            "minimum_equity": acct.get("minimum_equity"),
            "remaining_loss_buffer": acct.get("remaining_dd"),
            "profit": acct.get("realized_pnl"),
            "breached": acct.get("breached"),
            "account_status": acct.get("account_status"),
            "equity_source": acct.get("equity_source"),
            "mll_source": acct.get("mll_source"),
            "risk_source": acct.get("risk_source"),
            "status": mcp.get("status"),
            "rules_reconciliation": mcp.get("rules_reconciliation") or {},
        },
        "position_reconciliation": {
            "ninjatrader": recon.get("ninjatrader"),
            "mcp": recon.get("mcp"),
            "expected": recon.get("expected"),
            "reconciled": recon.get("reconciled"),
        },
        "prop_execution": False,
        "market_data_source": md_detail.get("source") or "NINJATRADER_READ_ONLY",
    }
    try:
        from aitrade_notifications import notification_health

        out["notifications"] = notification_health()
    except Exception:
        out["notifications"] = {
            "enabled": False,
            "backend": "APPRISE",
            "configured": False,
            "delivery_status": "UNAVAILABLE",
        }
    try:
        from prop_canary import context_from_ops_snapshot, observe_runtime, public_snapshot as canary_public

        ctx = context_from_ops_snapshot(out)
        observe_runtime(ctx)
        out["prop_canary"] = canary_public(ctx)
        fn_pub = out.get("fundednext")
        if isinstance(fn_pub, dict):
            fn_pub["general_prop"] = "LOCKED"
            fn_pub["canary_state"] = out["prop_canary"].get("state")
            fn_pub["permission"] = "READ_ONLY"
    except Exception as exc:
        out["prop_canary"] = {
            "state": "PROP_CANARY_BLOCKED",
            "label": "PROP_CANARY_BLOCKED",
            "general_prop": "LOCKED",
            "PROP_EXECUTION": False,
            "error": str(exc)[:240],
        }
    try:
        from unattended_prop_canary import (
            context_from_ops_snapshot as unattended_from_snap,
            public_snapshot as unattended_public,
            tick as unattended_tick,
            unattended_flag_enabled,
        )

        uctx = unattended_from_snap(out)
        if unattended_flag_enabled():
            unattended_tick(uctx, transmit=False, allow_entry=False)
        out["unattended"] = unattended_public(uctx)
    except Exception as exc:
        out["unattended"] = {
            "state": "UNATTENDED_DISABLED",
            "mode": "UNATTENDED_PROP_CANARY",
            "PROP_EXECUTION": False,
            "general_prop": "LOCKED",
            "error": str(exc)[:240],
        }
    out["soak"] = update_soak(out)
    _journal_watch(out)
    return out
