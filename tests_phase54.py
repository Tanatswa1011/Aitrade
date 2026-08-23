"""Phase 54 ops console — NinjaTrader read-only. PROP_EXECUTION remains false."""
from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock
from uuid import uuid4

os.environ["AITRADE_PHASE54_TEST"] = "1"
os.environ.setdefault(
    "AITRADE_PHASE54_JOURNAL",
    str(Path(tempfile.mkdtemp(prefix="phase54f_test_journal_"))),
)
from test_workspace import mutable_path

from execution_status import BLOCKED_MODES
from nq_microstructure_models import FROZEN_GC_HASH, FROZEN_NQ_HASH
from nt_readonly import (
    NTReadOnly,
    PROP_EXECUTION,
    _normalize_provider,
    classify_account_name,
    classify_market_provider,
    resolve_nq_mnq_contracts,
)
from phase54_ops import (
    BrokerAdapter,
    EngineSupervisor,
    MarketDataMonitor,
    PolicyEngine,
    StrategyRegistry,
    journal_blocked_live_signal,
    prop_execution_allowed,
    safe_start_checks,
    snapshot,
    soak_metrics,
    update_soak,
    _mll_for_equity,
)
from tradovate_readonly import (
    FORBIDDEN_METHODS,
    TradovateReadOnlyAccountAdapter,
    TradovateReadOnlyViolation,
    match_fundednext_account,
    normalize_money,
)
from fundednext_mcp import (
    FundedNextMCPReadOnlyAdapter,
    READ_ALLOWLIST,
    WRITE_DENYLIST,
    match_active_futures_account,
    normalize_running_trades,
    reconcile_rules_against_prop_v1,
)

FN_NAME = "FNFTCHTANATSWAPHILMU92044"
FN_LOGIN = "962841277"
FN_ID = 3969349
FN_PLAN = "Futures Flex Challenge | 50K"


def _mcp_fail(**overrides) -> dict:
    doc = {
        "source": "FUNDEDNEXT_MCP",
        "timestamp": "2026-08-20T20:00:00+00:00",
        "fresh": False,
        "connected": False,
        "authenticated": False,
        "status": "AUTH_FAILED",
        "reason": "credentials_missing",
        "account": {
            "name": FN_NAME,
            "platform_login": FN_LOGIN,
            "account_id": FN_ID,
            "status": None,
            "breached": None,
            "plan": FN_PLAN,
        },
        "money": {"balance": None, "equity": None, "profit": None, "initial_balance": None},
        "risk": {"permitted_loss": None, "minimum_equity": None, "remaining_loss_buffer": None},
        "rules": {},
        "rules_reconciliation": {"rules_match": False, "mismatches": [{"field": "mcp_unavailable"}], "survival_critical_ok": False},
        "futures": {"running_trades": [], "trade_history": []},
        "position": {"side": "FLAT", "quantity": 0, "known": False, "source": "FUNDEDNEXT_MCP"},
        "match": {
            "matched": False,
            "fundednext_name": FN_NAME,
            "platform_login": FN_LOGIN,
            "account_id": FN_ID,
            "plan": FN_PLAN,
            "match_method": "auth_failed",
        },
        "age_sec": 0.0,
        "PROP_EXECUTION": False,
    }
    doc.update(overrides)
    return doc


def _mcp_live(**overrides) -> dict:
    doc = {
        "source": "FUNDEDNEXT_MCP",
        "timestamp": "2026-08-20T20:00:00+00:00",
        "fresh": True,
        "connected": True,
        "authenticated": True,
        "status": "LIVE",
        "account": {
            "name": FN_NAME,
            "platform_login": FN_LOGIN,
            "account_id": FN_ID,
            "status": "ACTIVE",
            "breached": False,
            "plan": FN_PLAN,
        },
        "money": {"balance": 50000.0, "equity": 50000.0, "profit": 0.0, "initial_balance": 50000.0},
        "risk": {"permitted_loss": 1500.0, "minimum_equity": 48500.0, "remaining_loss_buffer": 1500.0},
        "rules": {},
        "rules_reconciliation": {"rules_match": True, "mismatches": [], "survival_critical_ok": True},
        "futures": {"running_trades": [], "trade_history": []},
        "position": {"side": "FLAT", "quantity": 0, "known": True, "source": "FUNDEDNEXT_MCP", "flat": True},
        "match": {
            "matched": True,
            "fundednext_name": FN_NAME,
            "platform_login": FN_LOGIN,
            "account_id": FN_ID,
            "plan": FN_PLAN,
            "match_method": "name+login+id+plan+active",
        },
        "age_sec": 0.2,
        "PROP_EXECUTION": False,
    }
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(doc.get(key), dict):
            merged = dict(doc[key])
            merged.update(value)
            doc[key] = merged
        else:
            doc[key] = value
    return doc


def _tv_fail(**overrides) -> dict:
    doc = {
        "source": "TRADOVATE_READ_ONLY",
        "timestamp": "2026-08-20T20:00:00+00:00",
        "fresh": False,
        "connected": False,
        "authenticated": False,
        "status": "AUTH_FAILED",
        "reason": "credentials_missing",
        "account": {"name": FN_NAME, "id": None, "status": None},
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
            "fundednext_account_name": FN_NAME,
            "tradovate_account_id": None,
            "matched": False,
            "match_method": "auth_failed",
        },
        "age_sec": 0.0,
        "PROP_EXECUTION": False,
        "raw_source_fields": {},
    }
    doc.update(overrides)
    return doc


def _tv_live(**overrides) -> dict:
    doc = {
        "source": "TRADOVATE_READ_ONLY",
        "timestamp": "2026-08-20T20:00:00+00:00",
        "fresh": True,
        "connected": True,
        "authenticated": True,
        "status": "LIVE",
        "account": {"name": FN_NAME, "id": 4242, "status": "active"},
        "money": {
            "equity": 50100.0,
            "net_liquidation": 50100.0,
            "cash_balance": 50080.0,
            "realized_pnl": 100.0,
            "unrealized_pnl": 0.0,
            "total_pnl": 100.0,
        },
        "positions": [],
        "position": {"side": "FLAT", "quantity": 0, "known": True, "source": "TRADOVATE_READ_ONLY"},
        "match": {
            "fundednext_account_name": FN_NAME,
            "tradovate_account_id": 4242,
            "matched": True,
            "match_method": "name_exact",
        },
        "age_sec": 0.2,
        "PROP_EXECUTION": False,
        "raw_source_fields": {
            "netLiq": 50100.0,
            "totalCashValue": 50080.0,
            "realizedPnL": 100.0,
            "openPnL": 0.0,
            "totalPnL": 100.0,
        },
    }
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(doc.get(key), dict):
            merged = dict(doc[key])
            merged.update(value)
            doc[key] = merged
        else:
            doc[key] = value
    return doc


def _write_readonly_market(
    root: Path,
    *,
    last: float | None = 24800.25,
    bid: float | None = 24800.0,
    ask: float | None = 24800.5,
    last_update: str | None = None,
    quality: str = "LIVE",
    instrument: str = "MNQ 09-26",
    market_connected: str = "CONNECTED",
    connections: str | None = None,
    providers: list | None = None,
) -> None:
    now = last_update or datetime.now(timezone.utc).isoformat()
    write_ts = datetime.now(timezone.utc).isoformat()
    q = (quality or "UNKNOWN").upper()
    if connections is None:
        if market_connected != "CONNECTED":
            connections = ""
        elif q == "SIMULATED":
            connections = "Simulated Data Feed=Connected"
        elif q == "DELAYED":
            connections = "Kinetick – End Of Day (Free)=Connected"
        elif q == "LIVE":
            connections = "NinjaTrader Continuum=Connected"
        else:
            connections = ""
    doc = {
        "schema": "AITRADE_NT_READONLY_V1",
        "source": "NINJATRADER_READ_ONLY",
        "PROP_EXECUTION": False,
        "ts": write_ts,
        "timestamp": write_ts,
        "market_data_quality": quality,
        "connection": {
            "status": "CONNECTED",
            "account": "CONNECTED",
            "market": market_connected,
            "quality": quality,
            "feed": quality,
        },
        "market_data": {
            "source": "NINJATRADER_READ_ONLY",
            "instrument": instrument,
            "last": last,
            "bid": bid,
            "ask": ask,
            "last_update": now if last is not None else None,
            "timestamp": now if last is not None else None,
            "quality": quality,
            "connected": market_connected == "CONNECTED",
            "fresh": last is not None,
        },
        "market": {
            "instrument": instrument,
            "last": last,
            "bid": bid,
            "ask": ask,
            "last_time": now if last is not None else None,
            "last_update": now if last is not None else None,
            "quality": quality,
        },
        "position": {"instrument": instrument, "side": "FLAT", "quantity": 0, "average_price": None},
        "account": {"id": FN_NAME, "kind": "FUNDEDNEXT"},
        "diagnostics": {
            "connections": connections,
            "mnq_found": True,
            "nq_found": True,
            "market_data_subscribed": True,
        },
        "nq": {
            "instrument": "NQ 09-26",
            "last": last,
            "bid": bid,
            "ask": ask,
            "timestamp": now if last is not None else None,
            "last_update": now if last is not None else None,
        },
        "mnq": {
            "instrument": "MNQ 09-26",
            "last": last,
            "bid": bid,
            "ask": ask,
            "timestamp": now if last is not None else None,
            "last_update": now if last is not None else None,
        },
        "account_environment": "SIMULATION" if q != "SIMULATED" and connections and "NinjaTrader" in connections else None,
    }
    if providers:
        doc["diagnostics"]["providers"] = providers
        env0 = providers[0].get("account_environment") if providers else None
        if env0:
            doc["account_environment"] = env0
    (root / "outgoing").mkdir(parents=True, exist_ok=True)
    (root / "outgoing" / "AITRADE_READONLY.json").write_text(json.dumps(doc), encoding="utf-8")


def _ticks(dt: datetime) -> int:
    return int((dt - datetime(1, 1, 1, tzinfo=timezone.utc)).total_seconds() * 10_000_000)


def _make_nt_root(
    tmp: str,
    *,
    fn_equity: float | None = 50000.0,
    sim_equity: float = 100003.0,
    fn_name: str = FN_NAME,
    feed_connected: bool = True,
    live_market: bool = False,
    market_quality: str = "LIVE",
) -> Path:
    root = Path(tmp)
    for name in ("incoming", "outgoing", "log", "trace"):
        (root / name).mkdir()
    (root / "db" / "minute" / "MNQ 09-26").mkdir(parents=True)
    (root / "outgoing" / "Simulation.txt").write_text(
        ("CONNECTED\n" if feed_connected else "DISCONNECTED\n"),
        encoding="utf-8",
    )
    (root / "db" / "minute" / "MNQ 09-26" / "20260820.Last.ncd").write_bytes(b"\x01\x00\x00\x00")
    feed = "Connected" if feed_connected else "Disconnected"
    (root / "log" / "log.20260820.00000.en.txt").write_text(
        f"2026-08-20 21:11:59:836|1|2|Simulation: Primary connection={feed}, Price feed={feed}\n"
        f"2026-08-20 21:12:00:000|1|64|Instrument='MNQ SEP26' Account='{fn_name}' "
        "Average price=0 Quantity=0 Market position=Flat Operation=Remove\n"
        "2026-08-20 21:12:00:001|1|64|Instrument='MNQ SEP26' Account='Sim101' "
        "Average price=9000 Quantity=1 Market position=Long Operation=Add\n",
        encoding="utf-8",
    )
    fn_val = "*****" if fn_equity is None else str(fn_equity)
    (root / "trace" / "trace.20260820.00000.txt").write_text(
        "(Simulation) Cbi.Connection.CreateAccount: account='Sim101' displayName='Sim101' fcm='' denomination=UsDollar forexLotSize=10000\n"
        "(Simulation) Cbi.Account.OnConnectionStatus: account='Sim101' fcm='' status=Connected previousStatus=Connecting message=''\n"
        f"(Simulation) Cbi.Connection.CreateAccount: account='{fn_name}' displayName='{fn_name}' fcm='' denomination=UsDollar forexLotSize=1\n"
        f"(Simulation) Cbi.Account.OnConnectionStatus: account='{fn_name}' fcm='' status=Connected previousStatus=Connecting message=''\n"
        f"(Simulation) Cbi.Account.AccountItemUpdateCallback: account='{fn_name}' accountItem=CashValue currency=UsDollar value={fn_val}\n",
        encoding="utf-8",
    )
    now = datetime(2026, 8, 20, 19, 12, tzinfo=timezone.utc)
    con = sqlite3.connect(root / "db" / "NinjaTrader.sqlite")
    cur = con.cursor()
    cur.execute(
        "CREATE TABLE Accounts (Id INTEGER, Name TEXT, DisplayName TEXT, Provider INTEGER, "
        "SimulatorInitialCash REAL, LossLimit REAL, AccountStatus INTEGER, LiquidationState INTEGER)"
    )
    cur.execute(
        "CREATE TABLE AccountItems (Account INTEGER, Currency INTEGER, ItemType INTEGER, Value REAL, TimeUtc INTEGER)"
    )
    cur.execute(
        "CREATE TABLE Positions (Account INTEGER, Instrument INTEGER, AvgPrice REAL, MarketPosition INTEGER, Quantity INTEGER, StatementDate INTEGER)"
    )
    cur.execute(
        "CREATE TABLE Orders (Id INTEGER, Account INTEGER, OrderId TEXT, Name TEXT, OrderState INTEGER, Filled INTEGER, Quantity INTEGER)"
    )
    cur.executemany(
        "INSERT INTO Accounts VALUES (?,?,?,?,?,?,?,?)",
        [
            (2, "Sim101", "Sim101", 15, 100000.0, 0.0, 1, 2),
            (3, fn_name, fn_name, 50, 100000.0, 0.0, 1, 4),
        ],
    )
    cur.execute(
        "INSERT INTO AccountItems VALUES (2,7,1,?,?)",
        (sim_equity, _ticks(now)),
    )
    if fn_equity is not None:
        cur.execute(
            "INSERT INTO AccountItems VALUES (3,7,1,?,?)",
            (fn_equity, _ticks(now)),
        )
    con.commit()
    con.close()
    if live_market:
        _write_readonly_market(root, quality=market_quality)
    return root


class Phase54SafetyTests(unittest.TestCase):
    def setUp(self):
        p = mock.patch("phase54_ops._fn_mcp_snapshot", return_value=_mcp_fail())
        p.start()
        self.addCleanup(p.stop)

    def test_prop_execution_never_allowed(self):
        self.assertFalse(prop_execution_allowed())
        snap = snapshot()
        self.assertFalse(snap["PROP_EXECUTION"])
        self.assertEqual(snap["order_execution"], "DISABLED")
        self.assertIn("PROP_EVALUATION", BLOCKED_MODES)

    def test_nq_only_evaluation_enabled(self):
        books = StrategyRegistry.active_books()
        by = {b["id"]: b for b in books}
        self.assertEqual(by["NQ_DRIFT_VWAP_PULLBACK"]["assignment"], "EVALUATION_ENABLED")
        self.assertEqual(by["GC_VWAP_V2"]["assignment"], "NOT_ASSIGNED")
        self.assertIn("Not assigned", by["GC_VWAP_V2"]["label"])

    def test_hashes(self):
        h = StrategyRegistry.verify_hashes()
        self.assertTrue(h["nq_match"])
        self.assertTrue(h["gc_match"])
        self.assertEqual(h["nq"], FROZEN_NQ_HASH)
        self.assertEqual(h["gc"], FROZEN_GC_HASH)

    def test_mode_does_not_enable_execution(self):
        out = EngineSupervisor.set_mode("EVALUATION")
        self.assertEqual(out["order_execution"], "DISABLED")
        self.assertFalse(out["PROP_EXECUTION"])
        EngineSupervisor.set_mode("DRY_RUN")

    def test_emergency_kill_does_not_transmit(self):
        with mock.patch("nt_ati.drop_oif") as drop, mock.patch("nt_ati.drop_oif_lines") as drop_lines:
            out = EngineSupervisor.emergency_flatten_stop()
            drop.assert_not_called()
            drop_lines.assert_not_called()
        self.assertEqual(out["flatten"], "REQUESTED_NOT_TRANSMITTED")
        self.assertEqual(out["orders_transmitted"], 0)
        self.assertEqual(out["order_execution"], "DISABLED")

    def test_execution_permission_check_is_disabled(self):
        c = safe_start_checks()
        self.assertEqual(c["display"]["execution_permission_checked"], "PASS")
        self.assertTrue(c["checks"]["execution_permission_checked"])
        self.assertEqual(c["order_execution"], "DISABLED")
        self.assertEqual(c["execution_permission_value"], "DISABLED")
        self.assertFalse(prop_execution_allowed())

    def test_broker_adapter_read_only(self):
        pos = BrokerAdapter.positions()
        self.assertIn(pos.get("side"), ("FLAT", "LONG", "SHORT"))
        conn = BrokerAdapter.connection_status()
        self.assertEqual(conn["permission"], "READ_ONLY")
        self.assertFalse(conn["PROP_EXECUTION"])
        if conn.get("account_id"):
            self.assertNotEqual(conn["account_id"], "Sim101")

    def test_snapshot_execution_blocked(self):
        snap = snapshot()
        self.assertEqual(snap["decision"]["execution"]["verdict"], "BLOCKED")
        self.assertEqual(snap["decision"]["execution"]["detail"], "PROP_EXECUTION=false")
        self.assertEqual(snap["fundednext_permission"], "READ_ONLY")
        self.assertIn(snap["market"].get("status"), ("LIVE", "STALE", "DISCONNECTED", "CONNECTED_STALE", "SIMULATED", "DELAYED", "PLAYBACK"))


class NTReadOnlyTests(unittest.TestCase):
    def test_classify_accounts(self):
        self.assertEqual(classify_account_name("Sim101"), "SIM")
        self.assertEqual(classify_account_name("FNFTCHTANATSWAPHILMU92044"), "FUNDEDNEXT")
        self.assertEqual(classify_account_name("Playback101"), "SIM")

    def test_fn_equity_not_sim101(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _make_nt_root(tmp)
            nt = NTReadOnly(root)
            fn = nt.fundednext_account()
            sim = nt.sim101_account()
            self.assertIsNotNone(fn)
            self.assertIsNotNone(sim)
            self.assertEqual(fn["name"], FN_NAME)
            self.assertEqual(sim["name"], "Sim101")
            self.assertEqual(nt.snapshot_account(sim["name"], sim["id"])["equity"], 100003.0)
            self.assertEqual(nt.snapshot_account(fn["name"], fn["id"])["equity"], 50000.0)
            self.assertEqual(nt.position_for(fn["name"], fn["id"])["side"], "FLAT")
            self.assertEqual(nt.position_for(sim["name"], sim["id"])["side"], "LONG")
            self.assertEqual(nt.position_for(sim["name"], sim["id"])["quantity"], 1)
            self.assertEqual(nt.incoming_oif_files(), [])
            hb = nt.market_heartbeat(stale_sec=3600)
            self.assertNotEqual(hb["status"], "LIVE")
            self.assertIn(hb["status"], ("STALE", "DISCONNECTED", "CONNECTED_STALE", "SIMULATED"))

    def test_missing_fn_items_do_not_use_sim_cash(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _make_nt_root(tmp, fn_equity=None)
            nt = NTReadOnly(root)
            fn = nt.fundednext_account()
            snap = nt.snapshot_account(fn["name"], fn["id"])
            self.assertIsNone(snap["equity"])
            self.assertIsNone(snap["realized_pnl"])
            self.assertIsNone(snap["today_pnl"])
            sim = nt.sim101_account()
            self.assertEqual(nt.snapshot_account(sim["name"], sim["id"])["equity"], 100003.0)

    def test_latest_feed_event_wins(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _make_nt_root(tmp, feed_connected=False)
            (root / "log" / "log.20260820.00000.en.txt").write_text(
                "2026-08-20 20:00:00:000|1|2|Simulation: Primary connection=Disconnected, Price feed=Disconnected\n",
                encoding="utf-8",
            )
            newer = root / "log" / "log.20260820.00003.en.txt"
            newer.write_text(
                "2026-08-20 21:11:59:836|1|2|Simulation: Primary connection=Connected, Price feed=Connected\n",
                encoding="utf-8",
            )
            (root / "outgoing" / "Simulation.txt").write_text("DISCONNECTED\n", encoding="utf-8")
            # outgoing mtime older than the connect log timestamp is hard; delete it so logs decide
            (root / "outgoing" / "Simulation.txt").unlink()
            nt = NTReadOnly(root)
            feed = nt.price_feed()
            self.assertEqual(feed["price_feed"], "Connected")
            self.assertTrue(feed["ok"])

    def test_phase54_uses_fn_not_sim(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _make_nt_root(tmp, fn_equity=50100.0)
            nt = NTReadOnly(root)
            with mock.patch("phase54_ops._nt", return_value=nt):
                with mock.patch("phase54_ops._fn_mcp_snapshot", return_value=_mcp_fail()):
                    acct_nt_only = BrokerAdapter.account_snapshot()
                with mock.patch("phase54_ops._fn_mcp_snapshot", return_value=_mcp_live()):
                    acct = BrokerAdapter.account_snapshot()
                    pos = BrokerAdapter.positions()
                    md = MarketDataMonitor.last_heartbeat()
                    conn = BrokerAdapter.connection_status()
            self.assertIsNone(acct_nt_only["equity"])
            self.assertNotEqual(acct_nt_only.get("equity"), 100003.0)
            self.assertEqual(acct["account_id"], FN_NAME)
            self.assertEqual(acct["equity"], 50000.0)
            self.assertEqual(acct["equity_source"], "FUNDEDNEXT_MCP")
            self.assertEqual(acct["risk_source"], "FUNDEDNEXT_MCP")
            self.assertNotEqual(acct["equity"], 50100.0)
            self.assertNotEqual(acct["equity"], 100003.0)
            self.assertEqual(pos["account"], FN_NAME)
            self.assertEqual(pos["side"], "FLAT")
            self.assertTrue(md.get("instrument", "").startswith("MNQ") or "MNQ" in str(md.get("source")))
            self.assertEqual(conn["permission"], "READ_ONLY")
            self.assertFalse(conn["PROP_EXECUTION"])
            self.assertEqual(conn["incoming_oif"], [])

    def test_stale_when_bars_old_even_if_connected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _make_nt_root(tmp)
            ncd = root / "db" / "minute" / "MNQ 09-26" / "20260820.Last.ncd"
            os.utime(ncd, (time.time() - 1000, time.time() - 1000))
            nt = NTReadOnly(root)
            hb = nt.market_heartbeat(stale_sec=120)
            self.assertIn(hb["status"], ("STALE", "CONNECTED_STALE", "SIMULATED", "DISCONNECTED"))
            self.assertNotEqual(hb["status"], "LIVE")
            self.assertIsNone(hb["last_price"])

    def test_runtime_json_makes_live_and_fn_equity(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _make_nt_root(tmp, fn_equity=None)
            now = datetime.now(timezone.utc).isoformat()
            doc = {
                "schema": "AITRADE_NT_READONLY_V1",
                "PROP_EXECUTION": False,
                "ts": now,
                "account": {
                    "id": FN_NAME,
                    "kind": "FUNDEDNEXT",
                    "cash_value": 50125.5,
                    "net_liquidation": 50125.5,
                    "unrealized": 0,
                    "realized": 125.5,
                },
                "market": {"instrument": "MNQ 09-26", "last": 24800.25, "bid": 24800.0, "ask": 24800.5, "last_time": now},
                "diagnostics": {"connections": "NinjaTrader=Connected"},
                "position": {"instrument": "MNQ 09-26", "market_position": "Flat", "quantity": 0, "average_price": 0},
            }
            (root / "outgoing" / "AITRADE_READONLY.json").write_text(json.dumps(doc), encoding="utf-8")
            nt = NTReadOnly(root)
            hb = nt.market_heartbeat(stale_sec=120)
            self.assertEqual(hb["status"], "LIVE")
            self.assertEqual(hb["last_price"], 24800.25)
            fn = nt.fundednext_account()
            snap = nt.snapshot_account(fn["name"], fn["id"])
            self.assertEqual(snap["equity"], 50125.5)
            self.assertNotEqual(snap["equity"], 100003.0)
            self.assertEqual(snap["source"], "ninjatrader_runtime_json")
            with mock.patch("phase54_ops._nt", return_value=nt):
                with mock.patch("phase54_ops._fn_mcp_snapshot", return_value=_mcp_fail()):
                    acct = BrokerAdapter.account_snapshot()
            self.assertIsNone(acct["equity"])
            self.assertNotEqual(acct.get("equity"), 50125.5)

    def test_runtime_json_sim101_cannot_fill_fn(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _make_nt_root(tmp, fn_equity=None)
            now = datetime.now(timezone.utc).isoformat()
            doc = {
                "ts": now,
                "account": {"id": "Sim101", "cash_value": 100003.0, "net_liquidation": 100003.0},
                "market": {"instrument": "MNQ 09-26", "last": 1, "last_time": now},
            }
            (root / "outgoing" / "AITRADE_READONLY.json").write_text(json.dumps(doc), encoding="utf-8")
            nt = NTReadOnly(root)
            fn = nt.fundednext_account()
            snap = nt.snapshot_account(fn["name"], fn["id"])
            self.assertIsNone(snap["equity"])
            self.assertNotEqual(snap.get("equity"), 100003.0)
            self.assertNotEqual(snap.get("equity"), 50000.0)

    def test_unavailable_equity_not_nominal(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _make_nt_root(tmp, fn_equity=None)
            nt = NTReadOnly(root)
            with mock.patch("phase54_ops._nt", return_value=nt):
                with mock.patch("phase54_ops._fn_mcp_snapshot", return_value=_mcp_fail()):
                    acct = BrokerAdapter.account_snapshot()
                    checks = safe_start_checks()
            self.assertIsNone(acct["equity"])
            self.assertIsNone(acct["mll"])
            self.assertNotEqual(acct["equity"], 50000.0)
            self.assertFalse(checks["checks"]["equity_mll_available"])
            self.assertEqual(checks["display"]["equity_mll_available"], "FAIL")
            self.assertFalse(checks["ok_to_run_engine"])

    def test_recon_mismatch_fails_safe_start(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _make_nt_root(tmp, fn_equity=50100.0)
            nt = NTReadOnly(root)
            mismatch = {
                "source": "nt_log",
                "account": FN_NAME,
                "instrument": "MNQ",
                "side": "LONG",
                "quantity": 1,
                "entry": 24800,
                "flat": False,
                "error": None,
                "known": True,
            }
            with mock.patch("phase54_ops._nt", return_value=nt):
                with mock.patch("phase54_ops._fn_mcp_snapshot", return_value=_mcp_live()):
                    with mock.patch.object(BrokerAdapter, "positions", return_value=mismatch):
                        c = safe_start_checks()
            self.assertFalse(c["checks"]["broker_positions_reconciled"])
            self.assertFalse(c["ok_to_run_engine"])
            self.assertEqual(c["order_execution"], "DISABLED")

    def test_blocked_live_signal_is_journaled(self):
        with tempfile.TemporaryDirectory() as tmp:
            sig_path = Path(tmp) / "signals.jsonl"
            ev_path = Path(tmp) / "events.jsonl"
            sig = {"direction": "LONG", "intended_entry": 24800.25, "trading_date": "2026-08-20", "source": "live"}
            policy = {"verdict": "ALLOW", "code": "ALLOW", "allowed_qty": 1, "lane": "PROTECTED"}
            with mock.patch("phase54_ops.SIGNALS_LOG", sig_path), mock.patch("phase54_ops.EVENTS_LOG", ev_path):
                journal_blocked_live_signal(sig, policy)
                from phase54_ops import last_live_signal
                row = last_live_signal()
            self.assertIsNotNone(row)
            self.assertEqual(row["execution"], "BLOCKED")
            self.assertEqual(row["detail"], "PROP_EXECUTION=false")
            self.assertFalse(row["PROP_EXECUTION"])
            self.assertTrue(sig_path.exists())
            text = ev_path.read_text(encoding="utf-8")
            self.assertIn("LIVE SIGNAL", text)
            self.assertIn("PROP_EXECUTION=false", text)
            self.assertIn('"execution": "BLOCKED"', text)

    def test_addon_write_ts_is_not_live_without_last_update(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _make_nt_root(tmp, fn_equity=None)
            ncd = root / "db" / "minute" / "MNQ 09-26" / "20260820.Last.ncd"
            os.utime(ncd, (time.time() - 1000, time.time() - 1000))
            now = datetime.now(timezone.utc).isoformat()
            old = datetime.fromtimestamp(time.time() - 1000, tz=timezone.utc).isoformat()
            doc = {
                "source": "NINJATRADER_RUNTIME",
                "timestamp": now,
                "ts": now,
                "account": {"id": FN_NAME, "cash_value": 50100.0, "net_liquidation": 50100.0},
                "market": {"instrument": "MNQ 09-26", "last": 24800.0, "last_time": old, "last_update": old},
                "market_data": {"instrument": "MNQ 09-26", "last": 24800.0, "last_update": old, "age_sec": 1000},
                "diagnostics": {"connections": "NinjaTrader Continuum=Connected"},
            }
            (root / "outgoing" / "AITRADE_READONLY.json").write_text(json.dumps(doc), encoding="utf-8")
            nt = NTReadOnly(root)
            hb = nt.market_heartbeat(stale_sec=120)
            self.assertIn(hb["status"], ("STALE", "CONNECTED_STALE"))
            self.assertNotEqual(hb["status"], "LIVE")

    def test_addon_fresh_last_update_beats_old_ncd(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _make_nt_root(tmp, fn_equity=None)
            ncd = root / "db" / "minute" / "MNQ 09-26" / "20260820.Last.ncd"
            os.utime(ncd, (time.time() - 1000, time.time() - 1000))
            now = datetime.now(timezone.utc).isoformat()
            doc = {
                "source": "NINJATRADER_RUNTIME",
                "fundednext": {"account_id": FN_NAME, "cash_value": 50125.5, "net_liquidation": 50125.5, "value_source": "NINJATRADER_RUNTIME"},
                "account": {"id": FN_NAME, "cash_value": 50125.5, "net_liquidation": 50125.5},
                "market_data": {"instrument": "MNQ 09-26", "last": 24801.5, "bid": 24801.25, "ask": 24801.75, "last_update": now, "age_sec": 1},
                "diagnostics": {"connections": "NinjaTrader Continuum=Connected"},
                "position": {"instrument": "MNQ 09-26", "side": "FLAT", "quantity": 0, "average_price": None},
            }
            (root / "outgoing" / "AITRADE_READONLY.json").write_text(json.dumps(doc), encoding="utf-8")
            nt = NTReadOnly(root)
            hb = nt.market_heartbeat(stale_sec=120)
            self.assertEqual(hb["status"], "LIVE")
            self.assertEqual(hb["last_price"], 24801.5)
            self.assertEqual(hb["source"], "NINJATRADER_READ_ONLY")
            snap = nt.snapshot_account(FN_NAME, nt.fundednext_account()["id"])
            self.assertEqual(snap["equity"], 50125.5)
            self.assertEqual(snap["source"], "ninjatrader_runtime_json")

    def test_wrong_account_id_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _make_nt_root(tmp, fn_equity=None)
            now = datetime.now(timezone.utc).isoformat()
            doc = {
                "timestamp": now,
                "account": {"id": "FNOTHERACCOUNT", "cash_value": 51000.0, "net_liquidation": 51000.0},
                "market": {"instrument": "MNQ 09-26", "last": 1, "last_time": now},
            }
            (root / "outgoing" / "AITRADE_READONLY.json").write_text(json.dumps(doc), encoding="utf-8")
            nt = NTReadOnly(root)
            fn = nt.fundednext_account()
            snap = nt.snapshot_account(fn["name"], fn["id"])
            self.assertNotEqual(snap.get("equity"), 51000.0)
            self.assertIsNone(snap.get("equity"))

    def test_null_fn_values_stay_unavailable(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _make_nt_root(tmp, fn_equity=None)
            now = datetime.now(timezone.utc).isoformat()
            doc = {
                "fundednext": {
                    "account_id": FN_NAME,
                    "cash_value": None,
                    "net_liquidation": None,
                    "value_source": "UNAVAILABLE_FROM_NINJATRADER_RUNTIME",
                },
                "account": {"id": FN_NAME, "cash_value": None, "net_liquidation": None},
                "market_data": {"instrument": "MNQ 09-26", "last": 24800.0, "last_update": now},
            }
            (root / "outgoing" / "AITRADE_READONLY.json").write_text(json.dumps(doc), encoding="utf-8")
            nt = NTReadOnly(root)
            with mock.patch("phase54_ops._nt", return_value=nt):
                with mock.patch("phase54_ops._fn_mcp_snapshot", return_value=_mcp_fail()):
                    acct = BrokerAdapter.account_snapshot()
            self.assertIsNone(acct["equity"])
            self.assertIsNone(acct["mll"])
            self.assertEqual(acct["equity_source"], "AUTH_FAILED")
            self.assertIsNone(acct["mll_source"])

    def test_runtime_json_preferred_over_sqlite(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _make_nt_root(tmp, fn_equity=50000.0)
            now = datetime.now(timezone.utc).isoformat()
            doc = {
                "account": {"id": FN_NAME, "cash_value": 50125.5, "net_liquidation": 50125.5},
                "market": {"instrument": "MNQ 09-26", "last": 24800.0, "last_time": now},
            }
            (root / "outgoing" / "AITRADE_READONLY.json").write_text(json.dumps(doc), encoding="utf-8")
            nt = NTReadOnly(root)
            snap = nt.snapshot_account(FN_NAME, nt.fundednext_account()["id"])
            self.assertEqual(snap["equity"], 50125.5)
            self.assertNotEqual(snap["equity"], 50000.0)

    def test_safe_start_pass_keeps_execution_disabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _make_nt_root(tmp, fn_equity=50100.0, live_market=True)
            nt = NTReadOnly(root)
            with mock.patch("phase54_ops._nt", return_value=nt):
                with mock.patch("phase54_ops._fn_mcp_snapshot", return_value=_mcp_live()):
                    with mock.patch("phase54_ops.calendar_status_for", return_value=("OK", None)):
                        c = safe_start_checks()
                        out = EngineSupervisor.start()
            self.assertTrue(c["checks"]["fresh_market_data"])
            self.assertTrue(c["checks"]["equity_mll_available"])
            self.assertTrue(c["ok_to_run_engine"])
            self.assertEqual(c["execution_permission_value"], "DISABLED")
            self.assertEqual(out["order_execution"], "DISABLED")
            self.assertFalse(out.get("PROP_EXECUTION", False) if "PROP_EXECUTION" in out else prop_execution_allowed())
            self.assertEqual(out["engine"], "RUNNING")
            EngineSupervisor.stop_gracefully()
            self.assertEqual(EngineSupervisor.status()["order_execution"], "DISABLED")


class FakeTradovateHttp:
    def __init__(self):
        self.auth = {
            "accessToken": "tok_test_not_for_journal",
            "expirationTime": "2099-01-01T00:00:00Z",
            "userStatus": "Active",
        }
        self.accounts = [
            {"id": 1, "name": "Sim101", "active": True, "accountType": "Customer"},
            {"id": 4242, "name": FN_NAME, "active": True, "accountType": "Customer"},
        ]
        self.cash = {
            "netLiq": 50125.5,
            "totalCashValue": 50080.0,
            "realizedPnL": 80.0,
            "openPnL": 45.5,
            "totalPnL": 125.5,
        }
        self.positions = []
        self.fail_auth = False

    def post(self, path, body, auth):
        self.last_post = (path, body, auth)
        if path == "/auth/accesstokenrequest":
            if self.fail_auth:
                return {"errorText": "Invalid credentials"}
            return dict(self.auth)
        if path == "/cashBalance/getcashbalancesnapshot":
            return dict(self.cash)
        raise AssertionError(path)

    def get(self, path, auth):
        if path == "/account/list":
            return list(self.accounts)
        if path == "/position/list":
            return list(self.positions)
        raise AssertionError(path)


class Phase54DTradovateTests(unittest.TestCase):
    def setUp(self):
        os.environ["TRADOVATE_DEVICE_ID"] = "phase54d-test-device"

    def test_no_trading_methods_exposed(self):
        adapter = TradovateReadOnlyAccountAdapter(http_post=lambda *a, **k: {}, http_get=lambda *a, **k: [])
        self.assertFalse(adapter.has_trading_methods())
        for name in FORBIDDEN_METHODS:
            self.assertFalse(hasattr(adapter, name) and callable(getattr(adapter, name)))
        self.assertNotIn("place_order", adapter.exposed_methods())
        self.assertFalse(adapter.PROP_EXECUTION)

    def test_order_paths_blocked(self):
        adapter = TradovateReadOnlyAccountAdapter(http_post=lambda *a, **k: {}, http_get=lambda *a, **k: [])
        with self.assertRaises(TradovateReadOnlyViolation):
            adapter._post("/order/placeorder", {}, auth=True)
        with self.assertRaises(TradovateReadOnlyViolation):
            adapter._get("/order/list", auth=True)

    def test_positive_fundednext_match(self):
        accounts = [
            {"id": 1, "name": "Sim101"},
            {"id": 4242, "name": FN_NAME},
            {"id": 9, "name": "Playback101"},
        ]
        match = match_fundednext_account(accounts, FN_NAME)
        self.assertTrue(match["matched"])
        self.assertEqual(match["tradovate_account_id"], 4242)
        self.assertEqual(match["match_method"], "name_exact")
        self.assertNotEqual(match["account"]["name"], "Sim101")

    def test_does_not_select_first_or_sim101(self):
        accounts = [{"id": 1, "name": "Sim101"}, {"id": 2, "name": "Other"}]
        match = match_fundednext_account(accounts, FN_NAME)
        self.assertFalse(match["matched"])
        auto = match_fundednext_account(accounts, "AUTO_FUNDEDNEXT")
        self.assertFalse(auto["matched"])

    def test_two_fn_accounts_are_unmatched_in_auto(self):
        accounts = [
            {"id": 1, "name": "FNAAAA"},
            {"id": 2, "name": "FNBBBB"},
        ]
        match = match_fundednext_account(accounts, "AUTO")
        self.assertFalse(match["matched"])

    def test_normalize_money_uses_netliq(self):
        money = normalize_money({
            "netLiq": 50125.5,
            "totalCashValue": 50080.0,
            "realizedPnL": 80.0,
            "openPnL": 45.5,
            "totalPnL": 125.5,
        })
        self.assertEqual(money["equity"], 50125.5)
        self.assertEqual(money["net_liquidation"], 50125.5)
        self.assertEqual(money["cash_balance"], 50080.0)
        self.assertEqual(money["realized_pnl"], 80.0)
        self.assertEqual(money["unrealized_pnl"], 45.5)

    def test_zero_null_equity_unavailable(self):
        self.assertIsNone(normalize_money({"netLiq": 0, "totalCashValue": 0})["equity"])
        self.assertIsNone(normalize_money({})["equity"])
        self.assertIsNone(normalize_money({"netLiq": None, "totalCashValue": None})["equity"])

    def test_valid_tradovate_snapshot(self):
        http = FakeTradovateHttp()
        adapter = TradovateReadOnlyAccountAdapter(
            expected_account_name=FN_NAME,
            http_post=http.post,
            http_get=http.get,
        )
        snap = adapter.fundednext_snapshot()
        self.assertEqual(snap["status"], "LIVE")
        self.assertTrue(snap["authenticated"])
        self.assertTrue(snap["match"]["matched"])
        self.assertEqual(snap["match"]["tradovate_account_id"], 4242)
        self.assertEqual(snap["money"]["equity"], 50125.5)
        self.assertEqual(snap["raw_source_fields"]["netLiq"], 50125.5)
        self.assertNotIn("accessToken", json.dumps(snap))
        self.assertNotIn("tok_test", json.dumps(snap))

    def test_auth_failure(self):
        http = FakeTradovateHttp()
        http.fail_auth = True
        adapter = TradovateReadOnlyAccountAdapter(
            expected_account_name=FN_NAME,
            http_post=http.post,
            http_get=http.get,
        )
        snap = adapter.fundednext_snapshot()
        self.assertEqual(snap["status"], "AUTH_FAILED")
        self.assertFalse(snap["authenticated"])
        self.assertIsNone(snap["money"]["equity"])
        self.assertFalse(snap["match"]["matched"])

    def test_wrong_account_returned(self):
        http = FakeTradovateHttp()
        http.accounts = [{"id": 99, "name": "FNOTHERACCOUNT", "active": True}]
        adapter = TradovateReadOnlyAccountAdapter(
            expected_account_name=FN_NAME,
            http_post=http.post,
            http_get=http.get,
        )
        snap = adapter.fundednext_snapshot()
        self.assertFalse(snap["match"]["matched"])
        self.assertEqual(snap["status"], "UNAVAILABLE")
        self.assertIsNone(snap["money"]["equity"])

    def test_stale_tradovate_snapshot_fails_safe_start(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _make_nt_root(tmp, fn_equity=50100.0)
            nt = NTReadOnly(root)
            stale = _mcp_live(status="STALE", fresh=False, age_sec=120.0)
            with mock.patch("phase54_ops._nt", return_value=nt):
                with mock.patch("phase54_ops._fn_mcp_snapshot", return_value=stale):
                    with mock.patch("phase54_ops.calendar_status_for", return_value=("OK", None)):
                        c = safe_start_checks()
                        acct = BrokerAdapter.account_snapshot()
            self.assertEqual(acct["equity"], 50000.0)
            self.assertFalse(c["checks"]["fundednext_authenticated"])
            self.assertFalse(c["ok_to_run_engine"])
            self.assertEqual(c["order_execution"], "DISABLED")

    def test_tradovate_equity_authoritative(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _make_nt_root(tmp, fn_equity=50000.0, sim_equity=100003.0)
            nt = NTReadOnly(root)
            live = _mcp_live(money={"equity": 50125.5, "balance": 50125.5, "profit": 125.5, "initial_balance": 50000.0})
            with mock.patch("phase54_ops._nt", return_value=nt):
                with mock.patch("phase54_ops._fn_mcp_snapshot", return_value=live):
                    acct = BrokerAdapter.account_snapshot()
                    snap = snapshot()
            self.assertEqual(acct["equity"], 50125.5)
            self.assertEqual(acct["equity_source"], "FUNDEDNEXT_MCP")
            self.assertEqual(acct["risk_source"], "FUNDEDNEXT_MCP")
            self.assertNotEqual(acct["equity"], 50000.0)
            self.assertNotEqual(acct["equity"], 100003.0)
            self.assertEqual(snap["fundednext"]["source"], "FUNDEDNEXT_MCP")
            self.assertNotEqual((snap.get("market") or {}).get("source"), "FUNDEDNEXT_MCP")

    def test_sim101_cannot_influence_fundednext_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _make_nt_root(tmp, fn_equity=None, sim_equity=100003.0)
            nt = NTReadOnly(root)
            with mock.patch("phase54_ops._nt", return_value=nt):
                with mock.patch("phase54_ops._fn_mcp_snapshot", return_value=_mcp_fail()):
                    acct = BrokerAdapter.account_snapshot()
            self.assertIsNone(acct["equity"])
            self.assertNotEqual(acct.get("equity"), 100003.0)

    def test_mll_only_from_lock_state(self):
        self.assertEqual(_mll_for_equity(None), (None, None))
        self.assertEqual(_mll_for_equity(50000.0), (None, None))
        mll, src = _mll_for_equity(50100.0)
        self.assertEqual(mll, 50100.0)
        self.assertEqual(src, "FUNDEDNEXT_FLEX_50K.MLL_LOCK_AT")
        with tempfile.TemporaryDirectory() as tmp:
            root = _make_nt_root(tmp)
            nt = NTReadOnly(root)
            below = _mcp_live(
                money={"equity": 50050.0, "balance": 50050.0, "profit": 50.0, "initial_balance": 50000.0},
                risk={"permitted_loss": None, "minimum_equity": None, "remaining_loss_buffer": None},
            )
            with mock.patch("phase54_ops._nt", return_value=nt):
                with mock.patch("phase54_ops._fn_mcp_snapshot", return_value=below):
                    acct = BrokerAdapter.account_snapshot()
                    c = safe_start_checks()
            self.assertEqual(acct["equity"], 50050.0)
            self.assertIsNone(acct["mll"])
            self.assertFalse(c["checks"]["equity_mll_available"])

    def test_tv_nt_position_mismatch_fails_safe_start(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _make_nt_root(tmp, fn_equity=50100.0)
            nt = NTReadOnly(root)
            mcp = _mcp_live(position={"side": "LONG", "quantity": 1, "known": True, "source": "FUNDEDNEXT_MCP", "flat": False})
            with mock.patch("phase54_ops._nt", return_value=nt):
                with mock.patch("phase54_ops._fn_mcp_snapshot", return_value=mcp):
                    with mock.patch("phase54_ops.calendar_status_for", return_value=("OK", None)):
                        c = safe_start_checks()
                        recon = snapshot()["position_reconciliation"]
            self.assertFalse(c["checks"]["broker_positions_reconciled"])
            self.assertFalse(c["ok_to_run_engine"])
            self.assertFalse(recon["reconciled"])
            self.assertEqual(recon["mcp"]["side"], "LONG")
            self.assertEqual(recon["ninjatrader"]["side"], "FLAT")
            self.assertEqual(recon["expected"]["side"], "FLAT")

    def test_safe_start_pass_does_not_enable_execution(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _make_nt_root(tmp, fn_equity=50100.0, live_market=True)
            nt = NTReadOnly(root)
            with mock.patch("phase54_ops._nt", return_value=nt):
                with mock.patch("phase54_ops._fn_mcp_snapshot", return_value=_mcp_live()):
                    with mock.patch("phase54_ops.calendar_status_for", return_value=("OK", None)):
                        c = safe_start_checks()
                        out = EngineSupervisor.start()
                        snap = snapshot()
            self.assertTrue(c["ok_to_run_engine"])
            self.assertTrue(c["checks"]["fundednext_authenticated"])
            self.assertTrue(c["checks"]["correct_account_id"])
            self.assertTrue(c["checks"]["equity_mll_available"])
            self.assertEqual(out["engine"], "RUNNING")
            self.assertEqual(out["order_execution"], "DISABLED")
            self.assertFalse(snap["PROP_EXECUTION"])
            self.assertFalse(snap["prop_execution"])
            self.assertEqual(snap["order_execution"], "DISABLED")
            EngineSupervisor.stop_gracefully()

    def test_mode_switch_and_prop_execution_false(self):
        out = EngineSupervisor.set_mode("EVALUATION")
        self.assertFalse(out["PROP_EXECUTION"])
        self.assertEqual(out["order_execution"], "DISABLED")
        self.assertFalse(prop_execution_allowed())
        EngineSupervisor.set_mode("DRY_RUN")


def _fn_account_row(**over):
    row = {
        "id": FN_ID,
        "login": FN_LOGIN,
        "breached": 0,
        "tradovate_account_name": {"tradovate_account_name": FN_NAME},
        "plan": {"title": FN_PLAN},
        "type": FN_PLAN,
    }
    row.update(over)
    return row


def _fn_overview(**over):
    doc = {
        "account_details": {
            "initial_balance": 50000,
            "breached": 0,
            "account_status": "Active",
            "type": FN_PLAN,
        },
        "stats": {"balance": 50000, "equity": 50000, "profit": 0, "cycle_starting_balance": 50000},
        "objectives": {
            "overall_loss": {"permitted_loss": 1500, "minimum_equity": 48500, "remaining": 1500},
            "profit": {"profit_target": 2500},
            "consistency": {"consistency_rate": 40},
            "daily_loss": [],
            "trading_days": {"target_value": 0},
        },
    }
    doc.update(over)
    return doc


class FakeFundedNextMCP:
    def __init__(self):
        self.accounts = [_fn_account_row()]
        self.overview = _fn_overview()
        self.rules = {"addons": {"ANT": 0}}
        self.history = {"runningTrades": {"total": 0, "data": []}, "trades": {"total": 0, "data": []}}
        self.calls = []

    def __call__(self, name, args):
        self.calls.append((name, dict(args)))
        if name == "get_accounts_v2":
            return {"data": list(self.accounts)}
        if name == "get_account_overview":
            return dict(self.overview)
        if name == "get_account_applicable_rules":
            return dict(self.rules)
        if name == "get_futures_trade_history":
            return dict(self.history)
        raise AssertionError(name)


class Phase54EFundedNextMCPTests(unittest.TestCase):
    def test_allowlist_rejects_write_tools(self):
        fake = FakeFundedNextMCP()
        adapter = FundedNextMCPReadOnlyAdapter(tool_caller=fake)
        for tool in ("create_free_trial_account", "register_competition", "record_ai_feedback", "initiate_payout_withdrawal"):
            with self.assertRaises(PermissionError):
                adapter.call_tool(tool, {"account_id": FN_ID})
        self.assertEqual(fake.calls, [])
        self.assertTrue(WRITE_DENYLIST <= {"create_free_trial_account", "register_competition", "record_ai_feedback"} | WRITE_DENYLIST)

    def test_allowlist_permits_only_read_tools(self):
        fake = FakeFundedNextMCP()
        adapter = FundedNextMCPReadOnlyAdapter(tool_caller=fake)
        adapter.call_tool("get_accounts_v2", {"type": "active", "tab": "futures"})
        self.assertEqual(fake.calls[0][0], "get_accounts_v2")
        self.assertTrue("get_accounts_v2" in READ_ALLOWLIST)
        self.assertFalse(adapter.has_trading_methods())
        self.assertFalse(adapter.PROP_EXECUTION)
        self.assertNotIn("place_order", adapter.exposed_methods())
        self.assertNotIn("drop_oif", adapter.exposed_methods())

    def test_valid_mcp_snapshot(self):
        fake = FakeFundedNextMCP()
        adapter = FundedNextMCPReadOnlyAdapter(tool_caller=fake)
        snap = adapter.normalized_snapshot()
        self.assertEqual(snap["source"], "FUNDEDNEXT_MCP")
        self.assertEqual(snap["status"], "LIVE")
        self.assertTrue(snap["match"]["matched"])
        self.assertEqual(snap["account"]["name"], FN_NAME)
        self.assertEqual(snap["account"]["platform_login"], FN_LOGIN)
        self.assertEqual(snap["account"]["account_id"], FN_ID)
        self.assertEqual(snap["money"]["balance"], 50000.0)
        self.assertEqual(snap["money"]["equity"], 50000.0)
        self.assertEqual(snap["risk"]["permitted_loss"], 1500.0)
        self.assertEqual(snap["risk"]["minimum_equity"], 48500.0)
        self.assertEqual(snap["risk"]["remaining_loss_buffer"], 1500.0)
        self.assertTrue(snap["position"]["known"])
        self.assertEqual(snap["position"]["side"], "FLAT")
        self.assertTrue(snap["rules_reconciliation"]["rules_match"])
        blob = json.dumps(snap)
        self.assertNotIn("access_token", blob)
        self.assertNotIn("refresh_token", blob)

    def test_wrong_account_rejected(self):
        fake = FakeFundedNextMCP()
        fake.accounts = [_fn_account_row(id=1, login="000", tradovate_account_name={"tradovate_account_name": "FNOTHER"})]
        adapter = FundedNextMCPReadOnlyAdapter(tool_caller=fake)
        snap = adapter.normalized_snapshot()
        self.assertFalse(snap["match"]["matched"])
        self.assertIsNone(snap["money"]["equity"])
        self.assertNotEqual(snap["status"], "LIVE")

    def test_inactive_and_breached_rejected(self):
        fake = FakeFundedNextMCP()
        fake.accounts = [_fn_account_row(breached=1)]
        adapter = FundedNextMCPReadOnlyAdapter(tool_caller=fake)
        snap = adapter.normalized_snapshot()
        self.assertFalse(snap["match"]["matched"])
        match = match_active_futures_account(
            [_fn_account_row(), _fn_account_row(id=1, login="1", tradovate_account_name={"tradovate_account_name": "FNFIRST"})],
            expected_name=FN_NAME,
            expected_login=FN_LOGIN,
            expected_account_id=FN_ID,
            expected_plan=FN_PLAN,
        )
        self.assertTrue(match["matched"])
        self.assertEqual(match["account_id"], FN_ID)

    def test_empty_running_trades_is_known_flat(self):
        pos = normalize_running_trades({"total": 0, "data": []})
        self.assertTrue(pos["known"])
        self.assertTrue(pos["flat"])
        self.assertEqual(pos["side"], "FLAT")
        self.assertFalse(normalize_running_trades(None)["known"])

    def test_rules_reconciliation_match_and_critical_mismatch(self):
        from prop_rules_v1 import load_profile
        raw = load_profile("FUNDEDNEXT_FLEX_50K").raw
        ok = reconcile_rules_against_prop_v1(_fn_overview(), raw)
        self.assertTrue(ok["rules_match"])
        self.assertTrue(ok["survival_critical_ok"])
        bad = _fn_overview()
        bad["objectives"]["overall_loss"]["permitted_loss"] = 9999
        mismatch = reconcile_rules_against_prop_v1(bad, raw)
        self.assertFalse(mismatch["rules_match"])
        self.assertTrue(any(m["field"] == "max_loss" and m.get("critical") for m in mismatch["mismatches"]))

    def test_mcp_equity_authoritative_not_nominal_or_sim(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _make_nt_root(tmp, fn_equity=None, sim_equity=100003.0)
            nt = NTReadOnly(root)
            live = _mcp_live()
            tel = Path(tmp) / "telemetry.jsonl"
            with mock.patch("phase54_ops._nt", return_value=nt), mock.patch("phase54_ops._fn_mcp_snapshot", return_value=live), mock.patch("phase54_ops.TELEMETRY_PATH", tel):
                acct = BrokerAdapter.account_snapshot()
                snap = snapshot()
            self.assertEqual(acct["equity"], 50000.0)
            self.assertEqual(acct["equity_source"], "FUNDEDNEXT_MCP")
            self.assertEqual(acct["mll"], 48500.0)
            self.assertEqual(acct["remaining_dd"], 1500.0)
            self.assertNotEqual(acct["equity"], 100003.0)
            self.assertEqual(snap["fundednext"]["source"], "FUNDEDNEXT_MCP")
            self.assertNotEqual(acct["equity"], 100003.0)

    def test_critical_rules_mismatch_degrades_policy(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _make_nt_root(tmp)
            nt = NTReadOnly(root)
            live = _mcp_live(rules_reconciliation={"rules_match": False, "mismatches": [{"field": "max_loss", "critical": True}], "survival_critical_ok": False})
            with mock.patch("phase54_ops._nt", return_value=nt), mock.patch("phase54_ops._fn_mcp_snapshot", return_value=live), mock.patch("phase54_ops.calendar_status_for", return_value=("OK", None)):
                c = safe_start_checks()
                snap = snapshot()
            self.assertFalse(c["checks"]["prop_rules_loaded"])
            self.assertFalse(c["ok_to_run_engine"])
            self.assertEqual(snap["policy_engine"], "DEGRADED")

    def test_safe_start_mcp_live_stale_market_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _make_nt_root(tmp)
            ncd = root / "db" / "minute" / "MNQ 09-26" / "20260820.Last.ncd"
            os.utime(ncd, (time.time() - 1000, time.time() - 1000))
            nt = NTReadOnly(root)
            with mock.patch("phase54_ops._nt", return_value=nt), mock.patch("phase54_ops._fn_mcp_snapshot", return_value=_mcp_live()), mock.patch("phase54_ops.calendar_status_for", return_value=("OK", None)):
                c = safe_start_checks()
                snap = snapshot()
            self.assertFalse(c["checks"]["fresh_market_data"])
            self.assertTrue(c["checks"]["fundednext_authenticated"])
            self.assertTrue(c["checks"]["correct_account_id"])
            self.assertTrue(c["checks"]["equity_mll_available"])
            self.assertTrue(c["checks"]["broker_positions_reconciled"])
            self.assertFalse(c["ok_to_run_engine"])
            self.assertEqual(c["order_execution"], "DISABLED")
            self.assertEqual(snap["policy_engine"], "DEGRADED")
            self.assertFalse(snap["PROP_EXECUTION"])

    def test_safe_start_all_pass_execution_still_disabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _make_nt_root(tmp, live_market=True)
            nt = NTReadOnly(root)
            tel = Path(tmp) / "telemetry.jsonl"
            with mock.patch("phase54_ops._nt", return_value=nt), mock.patch("phase54_ops._fn_mcp_snapshot", return_value=_mcp_live()), mock.patch("phase54_ops.calendar_status_for", return_value=("OK", None)), mock.patch("phase54_ops.TELEMETRY_PATH", tel):
                c = safe_start_checks()
                out = EngineSupervisor.start()
                snap = snapshot()
            self.assertTrue(c["ok_to_run_engine"])
            self.assertEqual(out["engine"], "RUNNING")
            self.assertEqual(out["order_execution"], "DISABLED")
            self.assertEqual(snap["order_execution"], "DISABLED")
            self.assertFalse(snap["PROP_EXECUTION"])
            self.assertFalse(prop_execution_allowed())
            EngineSupervisor.stop_gracefully()

    def test_three_way_recon_flat(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _make_nt_root(tmp)
            nt = NTReadOnly(root)
            with mock.patch("phase54_ops._nt", return_value=nt), mock.patch("phase54_ops._fn_mcp_snapshot", return_value=_mcp_live()):
                from phase54_ops import reconcile_position
                recon = reconcile_position()
            self.assertTrue(recon["reconciled"])
            self.assertEqual(recon["mcp"]["side"], "FLAT")
            self.assertEqual(recon["ninjatrader"]["side"], "FLAT")
            self.assertEqual(recon["expected"]["side"], "FLAT")

    def test_mcp_stale_unavailable(self):
        stale = _mcp_live(status="STALE", fresh=False)
        with mock.patch("phase54_ops._fn_mcp_snapshot", return_value=stale):
            conn = BrokerAdapter.connection_status()
            c = safe_start_checks()
        self.assertEqual(conn["fundednext"], "CONNECTED")
        self.assertFalse(conn["authenticated"])
        self.assertFalse(c["checks"]["fundednext_authenticated"])
        with mock.patch("phase54_ops._fn_mcp_snapshot", return_value=_mcp_fail()):
            acct = BrokerAdapter.account_snapshot()
        self.assertIsNone(acct["equity"])
        self.assertNotEqual(acct.get("equity"), 50000.0)
        self.assertEqual(acct["equity_source"], "AUTH_FAILED")

    def test_no_oif_or_submit(self):
        import phase54_ops
        import fundednext_mcp
        import fundednext_mcp_oauth
        for mod in (phase54_ops, fundednext_mcp, fundednext_mcp_oauth):
            src = Path(mod.__file__).read_text(encoding="utf-8")
            self.assertNotIn("PLACE ", src)
            self.assertNotIn("CLOSEPOSITION", src)
            self.assertNotIn("drop_oif(", src)
        self.assertFalse(prop_execution_allowed())
        self.assertFalse(phase54_ops.PROP_EXECUTION)
        self.assertFalse(fundednext_mcp.PROP_EXECUTION)
        self.assertFalse(fundednext_mcp_oauth.PROP_EXECUTION)


class Phase54E1OAuthTests(unittest.TestCase):
    def test_pkce_verifier_and_challenge(self):
        from fundednext_mcp_oauth import generate_pkce_verifier, pkce_challenge_s256
        verifier = generate_pkce_verifier()
        challenge = pkce_challenge_s256(verifier)
        self.assertGreaterEqual(len(verifier), 43)
        self.assertNotEqual(verifier, challenge)
        self.assertEqual(challenge, pkce_challenge_s256(verifier))
        self.assertNotIn("=", challenge)
        self.assertNotIn("=", verifier)

    def test_state_mismatch_rejected(self):
        from fundednext_mcp_oauth import CallbackRejected, bind_localhost_callback, wait_for_callback
        server, redirect = bind_localhost_callback()
        ready = threading.Event()
        err = []

        def run():
            try:
                wait_for_callback(server, expected_state="expected-state", timeout_sec=5, ready=ready)
            except Exception as exc:
                err.append(exc)

        t = threading.Thread(target=run, daemon=True)
        t.start()
        ready.wait(2)
        try:
            urllib.request.urlopen(redirect + "?code=abc&state=wrong-state", timeout=3).read()
        except urllib.error.HTTPError:
            pass
        t.join(5)
        server.server_close()
        self.assertTrue(err)
        self.assertIsInstance(err[0], CallbackRejected)
        self.assertEqual(str(err[0]), "state_mismatch")

    def test_callback_success(self):
        from fundednext_mcp_oauth import bind_localhost_callback, wait_for_callback
        server, redirect = bind_localhost_callback()
        ready = threading.Event()
        box = {}

        def run():
            box["code"] = wait_for_callback(server, expected_state="st", timeout_sec=5, ready=ready)

        t = threading.Thread(target=run, daemon=True)
        t.start()
        ready.wait(2)
        urllib.request.urlopen(redirect + "?code=auth-code-1&state=st", timeout=3).read()
        t.join(5)
        server.server_close()
        self.assertEqual(box.get("code"), "auth-code-1")

    def test_callback_timeout(self):
        from fundednext_mcp_oauth import CallbackTimeout, bind_localhost_callback, wait_for_callback
        server, _redirect = bind_localhost_callback()
        try:
            with self.assertRaises(CallbackTimeout):
                wait_for_callback(server, expected_state="st", timeout_sec=0.6)
        finally:
            server.server_close()

    def test_token_exchange_success_and_failure(self):
        from fundednext_mcp_oauth import OAuthError, exchange_authorization_code

        def ok_form(url, body, headers):
            self.assertEqual(body["grant_type"], "authorization_code")
            self.assertEqual(body["code"], "good")
            return {
                "access_token": "tok_access_test",
                "refresh_token": "tok_refresh_test",
                "expires_in": 3600,
                "token_type": "Bearer",
                "scope": "mcp:read",
            }

        doc = exchange_authorization_code(
            code="good",
            redirect_uri="http://127.0.0.1:9/callback",
            code_verifier="v",
            client_id="cid",
            metadata={"token_endpoint": "https://mcp.fundednext.com/oauth/token", "resource": "https://mcp.fundednext.com"},
            http_form=ok_form,
        )
        self.assertEqual(doc["token_type"], "Bearer")
        self.assertTrue(doc["access_token"])

        def bad_form(url, body, headers):
            return {"error": "invalid_grant"}

        with self.assertRaises(OAuthError):
            exchange_authorization_code(
                code="bad",
                redirect_uri="http://127.0.0.1:9/callback",
                code_verifier="v",
                client_id="cid",
                metadata={"token_endpoint": "https://mcp.fundednext.com/oauth/token"},
                http_form=bad_form,
            )

    def test_session_persist_reload_and_refresh(self):
        from fundednext_mcp_oauth import (
            invalidate_oauth_session,
            load_oauth_session,
            resolve_access_token,
            save_oauth_session,
            session_from_token_response,
        )
        path = mutable_path("oauth_fixtures", uuid4().hex, "fundednext_mcp_oauth.json")
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.subTest(path=path.name):
            session = session_from_token_response(
                {"access_token": "tok_a1", "refresh_token": "tok_r1", "expires_in": 3600, "token_type": "Bearer", "scope": "mcp:read"},
                client_id="cid-1",
            )
            save_oauth_session(session, path)
            loaded = load_oauth_session(path)
            self.assertEqual(loaded["token_type"], "Bearer")
            self.assertEqual(loaded["scope"], "mcp:read")
            token = resolve_access_token(path=path, now=time.time())
            self.assertEqual(token, "tok_a1")
            calls = []

            def refresh_form(url, body, headers):
                calls.append(body["grant_type"])
                if body["refresh_token"] != "tok_r1":
                    raise AssertionError("unexpected refresh")
                return {
                    "access_token": "tok_a2",
                    "refresh_token": "tok_r2",
                    "expires_in": 3600,
                    "token_type": "Bearer",
                    "scope": "mcp:read",
                }

            expired = dict(loaded)
            expired["expires_at"] = "2000-01-01T00:00:00+00:00"
            save_oauth_session(expired, path)
            rotated = resolve_access_token(path=path, now=time.time(), http_form=refresh_form)
            self.assertEqual(rotated, "tok_a2")
            self.assertEqual(load_oauth_session(path)["refresh_token"], "tok_r2")
            self.assertEqual(calls, ["refresh_token"])

            def fail_form(url, body, headers):
                raise __import__("fundednext_mcp_oauth", fromlist=["OAuthError"]).OAuthError("refresh_rejected")

            self.assertIsNone(resolve_access_token(path=path, now=time.time(), http_form=fail_form, force_refresh=True))
            dead = load_oauth_session(path)
            self.assertFalse(dead.get("access_token"))
            self.assertTrue(dead.get("invalid"))
            invalidate_oauth_session(path)

    def test_env_credentials_precede_file(self):
        from fundednext_mcp_oauth import resolve_access_token, save_oauth_session
        path = mutable_path("oauth_fixtures", uuid4().hex, "oauth.json")
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.subTest(path=path.name):
            save_oauth_session({"access_token": "tok_file", "refresh_token": "tok_r", "expires_at": time.time() + 3600, "scope": "mcp:read", "token_type": "Bearer"}, path)
            with mock.patch.dict(os.environ, {"FUNDEDNEXT_MCP_ACCESS_TOKEN": "tok_env"}, clear=False):
                self.assertEqual(resolve_access_token(path=path), "tok_env")

    def test_stale_mcp_invalidated_after_auth_failure(self):
        from fundednext_mcp_oauth import bump_auth_generation
        import phase54_ops
        phase54_ops._MCP_SNAP["doc"] = _mcp_live()
        phase54_ops._MCP_SNAP["ts"] = time.time()
        phase54_ops._MCP_SNAP["gen"] = 0
        bump_auth_generation()
        with mock.patch("phase54_ops._fn_mcp") as fn:
            adapter = mock.Mock()
            adapter.normalized_snapshot.return_value = _mcp_fail()
            fn.return_value = adapter
            acct = BrokerAdapter.account_snapshot()
        self.assertIsNone(acct["equity"])
        self.assertEqual(acct["equity_source"], "AUTH_FAILED")
        self.assertNotEqual(acct.get("equity"), 50000.0)

    def test_tokens_never_appear_in_snapshot_or_journal(self):
        secret = "tok_must_never_leak_54e1"
        snap = snapshot()
        blob = json.dumps(snap)
        self.assertNotIn(secret, blob)
        self.assertNotIn("FUNDEDNEXT_MCP_ACCESS_TOKEN", blob)
        from phase54_ops import _journal_safe, append_event
        safe = _journal_safe({"access_token": secret, "refresh_token": secret, "Authorization": "Bearer " + secret, "equity": 1})
        self.assertNotIn("access_token", safe)
        self.assertNotIn("refresh_token", safe)
        self.assertEqual(safe.get("equity"), 1)
        with tempfile.TemporaryDirectory() as tmp:
            ev = Path(tmp) / "events.jsonl"
            with mock.patch("phase54_ops.EVENTS_LOG", ev):
                append_event("INFO", "oauth test", access_token=secret, refresh_token=secret, equity=2)
            text = ev.read_text(encoding="utf-8")
            self.assertNotIn(secret, text)

    def test_write_tools_still_blocked_and_no_execution(self):
        from fundednext_mcp import FundedNextMCPReadOnlyAdapter
        from fundednext_mcp_oauth import PROP_EXECUTION as oauth_prop
        adapter = FundedNextMCPReadOnlyAdapter(tool_caller=lambda *a, **k: (_ for _ in ()).throw(AssertionError("no")))
        for tool in ("create_free_trial_account", "register_competition", "record_ai_feedback"):
            with self.assertRaises(PermissionError):
                adapter.call_tool(tool, {})
        self.assertFalse(adapter.has_trading_methods())
        self.assertFalse(prop_execution_allowed())
        self.assertFalse(oauth_prop)
        src = Path("fundednext_mcp_oauth.py").read_text(encoding="utf-8")
        self.assertNotIn("drop_oif(", src)
        self.assertNotIn("PLACE ", src)
        self.assertNotIn("Flatten", src)


class Phase54FMarketDataTests(unittest.TestCase):
    def test_fresh_runtime_print_is_live(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _make_nt_root(tmp)
            _write_readonly_market(root, last=24777.25, bid=24777.0, ask=24777.5)
            nt = NTReadOnly(root)
            hb = nt.market_heartbeat(stale_sec=120)
            self.assertEqual(hb["status"], "LIVE")
            self.assertEqual(hb["quality"], "LIVE")
            self.assertEqual(hb["source"], "NINJATRADER_READ_ONLY")
            self.assertEqual(hb["last_price"], 24777.25)
            self.assertEqual(hb["bid"], 24777.0)
            self.assertEqual(hb["ask"], 24777.5)
            self.assertLessEqual(hb["age_sec"], 120)
            self.assertFalse(hb["PROP_EXECUTION"])

    def test_stale_timestamp_is_stale(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _make_nt_root(tmp)
            old = datetime.fromtimestamp(time.time() - 1000, tz=timezone.utc).isoformat()
            _write_readonly_market(root, last_update=old)
            nt = NTReadOnly(root)
            hb = nt.market_heartbeat(stale_sec=120)
            self.assertIn(hb["status"], ("STALE", "CONNECTED_STALE"))
            self.assertEqual(hb["freshness"], "STALE")
            self.assertGreater(hb["age_sec"], 120)

    def test_disconnected_nt_is_disconnected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _make_nt_root(tmp, feed_connected=False)
            nt = NTReadOnly(root)
            hb = nt.market_heartbeat(stale_sec=120)
            self.assertEqual(hb["status"], "DISCONNECTED")
            self.assertEqual(hb["ninjatrader_market_connection"], "DISCONNECTED")

    def test_simulated_feed_rejected_for_live_readiness(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _make_nt_root(tmp)
            _write_readonly_market(root, quality="SIMULATED")
            nt = NTReadOnly(root)
            hb = nt.market_heartbeat(stale_sec=120)
            self.assertNotEqual(hb["status"], "LIVE")
            self.assertEqual(hb["quality"], "SIMULATED")
            self.assertEqual(hb["status"], "SIMULATED")
            with mock.patch("phase54_ops._nt", return_value=nt), mock.patch("phase54_ops._fn_mcp_snapshot", return_value=_mcp_live()), mock.patch("phase54_ops.calendar_status_for", return_value=("OK", None)):
                c = safe_start_checks()
            self.assertFalse(c["checks"]["fresh_market_data"])
            self.assertFalse(c["ok_to_run_engine"])

    def test_delayed_feed_labeled(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _make_nt_root(tmp)
            _write_readonly_market(root, quality="DELAYED")
            nt = NTReadOnly(root)
            hb = nt.market_heartbeat(stale_sec=120)
            self.assertEqual(hb["quality"], "DELAYED")
            self.assertEqual(hb["status"], "DELAYED")
            with mock.patch("phase54_ops._nt", return_value=nt), mock.patch("phase54_ops._fn_mcp_snapshot", return_value=_mcp_live()), mock.patch("phase54_ops.calendar_status_for", return_value=("OK", None)):
                c = safe_start_checks()
                snap = snapshot()
            self.assertFalse(c["checks"]["fresh_market_data"])
            self.assertEqual(snap["market_data"]["quality"], "DELAYED")

    def test_missing_price_timestamp_cannot_be_live(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _make_nt_root(tmp)
            doc = {
                "source": "NINJATRADER_READ_ONLY",
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "market_data": {"instrument": "MNQ 09-26", "last": 24800.0, "last_update": None, "quality": "LIVE"},
                "connection": {"market": "CONNECTED", "quality": "LIVE"},
            }
            (root / "outgoing" / "AITRADE_READONLY.json").write_text(json.dumps(doc), encoding="utf-8")
            nt = NTReadOnly(root)
            hb = nt.market_heartbeat(stale_sec=120)
            self.assertNotEqual(hb["status"], "LIVE")

    def test_file_mtime_cannot_fake_live(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _make_nt_root(tmp)
            ncd = root / "db" / "minute" / "MNQ 09-26" / "20260820.Last.ncd"
            os.utime(ncd, None)
            cache = root / "db" / "cache" / "CME" / "MINUTE" / "MNQ 09-26"
            cache.mkdir(parents=True)
            ntb = cache / "20260820.Last.ntb"
            ntb.write_bytes(b"\x01\x00")
            os.utime(ntb, None)
            nt = NTReadOnly(root)
            hb = nt.market_heartbeat(stale_sec=120)
            self.assertNotEqual(hb["status"], "LIVE")
            self.assertNotEqual(hb.get("source"), "NINJATRADER_READ_ONLY")

    def test_contract_rollover_resolution(self):
        aug = resolve_nq_mnq_contracts(datetime(2026, 8, 21, tzinfo=timezone.utc))
        self.assertEqual(aug["nq"], "NQ 09-26")
        self.assertEqual(aug["mnq"], "MNQ 09-26")
        self.assertEqual(aug["expiry"], "2026-09-18")
        self.assertEqual(aug["signal_instrument"], "NQ")
        self.assertEqual(aug["position_instrument"], "MNQ")
        after = resolve_nq_mnq_contracts(datetime(2026, 9, 19, tzinfo=timezone.utc))
        self.assertEqual(after["nq"], "NQ 12-26")
        self.assertEqual(after["mnq"], "MNQ 12-26")

    def test_nq_signal_mnq_position_mapping(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _make_nt_root(tmp, live_market=True)
            nt = NTReadOnly(root)
            hb = nt.market_heartbeat(stale_sec=120)
            self.assertEqual(hb["contracts"]["signal_instrument"], "NQ")
            self.assertEqual(hb["contracts"]["position_instrument"], "MNQ")
            self.assertIn("NQ", hb["signal_instrument"])
            self.assertIn("MNQ", hb["position_instrument"])

    def test_mcp_healthy_market_stale_safe_start_fail(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _make_nt_root(tmp)
            old = datetime.fromtimestamp(time.time() - 1000, tz=timezone.utc).isoformat()
            _write_readonly_market(root, last_update=old)
            nt = NTReadOnly(root)
            with mock.patch("phase54_ops._nt", return_value=nt), mock.patch("phase54_ops._fn_mcp_snapshot", return_value=_mcp_live()), mock.patch("phase54_ops.calendar_status_for", return_value=("OK", None)):
                c = safe_start_checks()
                snap = snapshot()
            self.assertTrue(c["checks"]["fundednext_authenticated"])
            self.assertFalse(c["checks"]["fresh_market_data"])
            self.assertFalse(c["ok_to_run_engine"])
            self.assertEqual(snap["fundednext_account_status"], "CONNECTED")
            self.assertEqual(snap["market_data"]["freshness"], "STALE")
            self.assertEqual(snap["order_execution"], "DISABLED")

    def test_all_gates_pass_execution_still_disabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _make_nt_root(tmp, live_market=True)
            nt = NTReadOnly(root)
            with mock.patch("phase54_ops._nt", return_value=nt), mock.patch("phase54_ops._fn_mcp_snapshot", return_value=_mcp_live()), mock.patch("phase54_ops.calendar_status_for", return_value=("OK", None)):
                c = safe_start_checks()
                out = EngineSupervisor.start()
                snap = snapshot()
            self.assertTrue(c["ok_to_run_engine"])
            self.assertEqual(c["display"]["fresh_market_data"], "PASS")
            self.assertEqual(c["execution_permission_value"], "DISABLED")
            self.assertEqual(out["engine"], "RUNNING")
            self.assertEqual(out["order_execution"], "DISABLED")
            self.assertFalse(snap["PROP_EXECUTION"])
            self.assertEqual(snap["market_data"]["freshness"], "LIVE")
            self.assertEqual(snap["market_data"]["quality"], "LIVE")
            soak = snap["soak"]
            self.assertIn("market_heartbeat_count", soak)
            self.assertFalse(soak["pnl_fabricated"])
            EngineSupervisor.stop_gracefully()

    def test_policy_approval_produces_blocked_shadow_decision(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _make_nt_root(tmp, live_market=True)
            nt = NTReadOnly(root)
            sig = {"direction": "LONG", "intended_entry": 24800.25, "trading_date": "2026-08-20", "source": "live"}
            fake = mock.Mock(verdict="ALLOW", code="ALLOW", allowed_qty=1, state="EVAL_PROTECTED", reasons=["ok"])
            with mock.patch("phase54_ops._nt", return_value=nt), mock.patch("phase54_ops._fn_mcp_snapshot", return_value=_mcp_live()), mock.patch("phase54_ops.calendar_status_for", return_value=("OK", None)), mock.patch("phase54_ops.evaluate_intent", return_value=fake):
                pol = PolicyEngine.evaluate(sig)
            self.assertEqual(pol["verdict"], "ALLOW")
            self.assertEqual(pol["allowed_qty"], 1)
            from phase54_ops import SIGNALS_LOG, EVENTS_LOG
            journal_blocked_live_signal(sig, pol)
            row = json.loads(SIGNALS_LOG.read_text(encoding="utf-8").strip().splitlines()[-1])
            self.assertEqual(row["execution"], "BLOCKED")
            self.assertEqual(row["detail"], "PROP_EXECUTION=false")
            self.assertFalse(row["PROP_EXECUTION"])
            self.assertIn("PROP_EXECUTION=false", EVENTS_LOG.read_text(encoding="utf-8"))

    def test_news_gate_blocks(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _make_nt_root(tmp, live_market=True)
            nt = NTReadOnly(root)
            sig = {"direction": "SHORT", "intended_entry": 24810.0, "trading_date": "2026-08-20"}
            with mock.patch("phase54_ops._nt", return_value=nt), mock.patch("phase54_ops._fn_mcp_snapshot", return_value=_mcp_live()), mock.patch("phase54_ops.calendar_status_for", return_value=("MISSING", None)):
                pol = PolicyEngine.evaluate(sig)
            self.assertEqual(pol["verdict"], "BLOCK")
            self.assertIn("NEWS", str(pol.get("code") or "").upper())

    def test_market_disconnect_after_engine_running(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _make_nt_root(tmp, live_market=True)
            nt = NTReadOnly(root)
            with mock.patch("phase54_ops._nt", return_value=nt), mock.patch("phase54_ops._fn_mcp_snapshot", return_value=_mcp_live()), mock.patch("phase54_ops.calendar_status_for", return_value=("OK", None)):
                EngineSupervisor.start()
                _write_readonly_market(root, market_connected="DISCONNECTED", last=None)
                st = EngineSupervisor.status()
                snap = snapshot()
                pol = PolicyEngine.evaluate({"direction": "LONG", "intended_entry": 1, "trading_date": "2026-08-20"})
            self.assertTrue(st["entries_paused"])
            self.assertEqual(st["order_execution"], "DISABLED")
            self.assertNotEqual(snap["market_data"]["freshness"], "LIVE")
            self.assertEqual(pol["code"], "MARKET_DATA_NOT_LIVE")
            EngineSupervisor.stop_gracefully()

    def test_mcp_disconnect_after_engine_running(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _make_nt_root(tmp, live_market=True)
            nt = NTReadOnly(root)
            with mock.patch("phase54_ops._nt", return_value=nt), mock.patch("phase54_ops.calendar_status_for", return_value=("OK", None)):
                with mock.patch("phase54_ops._fn_mcp_snapshot", return_value=_mcp_live()):
                    EngineSupervisor.start()
                with mock.patch("phase54_ops._fn_mcp_snapshot", return_value=_mcp_fail()):
                    st = EngineSupervisor.status()
                    snap = snapshot()
                    pol = PolicyEngine.evaluate({"direction": "LONG", "intended_entry": 1, "trading_date": "2026-08-20"})
                    acct = BrokerAdapter.account_snapshot()
            self.assertTrue(st["entries_paused"])
            self.assertEqual(snap["fundednext_connection"], "DISCONNECTED")
            self.assertIsNone(acct["equity"])
            self.assertEqual(pol["code"], "MCP_UNAVAILABLE")
            self.assertEqual(snap["policy_engine"], "DEGRADED")
            EngineSupervisor.stop_gracefully()

    def test_position_mismatch_and_no_stale_equity_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _make_nt_root(tmp, live_market=True)
            nt = NTReadOnly(root)
            mcp = _mcp_live(position={"side": "LONG", "quantity": 2, "known": True, "source": "FUNDEDNEXT_MCP", "flat": False})
            with mock.patch("phase54_ops._nt", return_value=nt), mock.patch("phase54_ops._fn_mcp_snapshot", return_value=mcp), mock.patch("phase54_ops.calendar_status_for", return_value=("OK", None)):
                c = safe_start_checks()
                recon = snapshot()["position_reconciliation"]
            self.assertFalse(c["checks"]["broker_positions_reconciled"])
            self.assertFalse(c["ok_to_run_engine"])
            self.assertFalse(recon["reconciled"])
            self.assertEqual(recon["mcp"]["side"], "LONG")
            self.assertEqual(recon["expected"]["side"], "FLAT")

    def test_no_synthetic_journal_contamination(self):
        from phase54_ops import JOURNAL_DIR, EVENTS_LOG, TELEMETRY_PATH
        self.assertTrue(os.environ.get("AITRADE_PHASE54_TEST") == "1")
        self.assertNotEqual(JOURNAL_DIR, Path("journal") / "phase54_ops")
        self.assertTrue(str(JOURNAL_DIR).find("phase54f_test_journal_") >= 0 or str(JOURNAL_DIR).find("phase54_ops_test") >= 0)
        soak = soak_metrics()
        self.assertTrue(soak.get("test"))
        self.assertFalse(soak.get("pnl_fabricated"))
        if EVENTS_LOG.exists():
            text = EVENTS_LOG.read_text(encoding="utf-8")
            self.assertNotIn("temporary market LIVE fixtures", text)

    def test_prop_execution_false_and_no_oif(self):
        import phase54_ops
        import nt_readonly
        for mod in (phase54_ops, nt_readonly):
            src = Path(mod.__file__).read_text(encoding="utf-8")
            self.assertNotIn("PLACE ", src)
            self.assertNotIn("CLOSEPOSITION", src)
            self.assertNotIn("drop_oif(", src)
            self.assertNotIn("submit_order", src)
        self.assertFalse(prop_execution_allowed())
        self.assertFalse(phase54_ops.PROP_EXECUTION)
        self.assertFalse(nt_readonly.PROP_EXECUTION)

    def test_nested_market_data_on_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _make_nt_root(tmp, live_market=True)
            nt = NTReadOnly(root)
            with mock.patch("phase54_ops._nt", return_value=nt), mock.patch("phase54_ops._fn_mcp_snapshot", return_value=_mcp_live()):
                snap = snapshot()
            md = snap["market_data"]
            for key in ("source", "instrument", "last", "bid", "ask", "timestamp", "age_seconds", "freshness", "quality", "connection"):
                self.assertIn(key, md)
            self.assertEqual(md["source"], "NINJATRADER_READ_ONLY")
            self.assertIn("soak", snap)

    def test_addon_heartbeat_without_quote_is_stale_not_disconnected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _make_nt_root(tmp, feed_connected=False)
            _write_readonly_market(root, last=None, quality="UNKNOWN", market_connected="DISCONNECTED")
            nt = NTReadOnly(root)
            hb = nt.market_heartbeat(stale_sec=120)
            self.assertEqual(hb["status"], "DISCONNECTED")
            self.assertEqual(hb["freshness"], "DISCONNECTED")
            self.assertIsNone(hb["last_price"])
            self.assertEqual(hb["reason"], "NO_MARKET_DATA_CONNECTION")
            self.assertFalse(hb["market_provider_connected"])
            self.assertNotEqual(hb["status"], "LIVE")
            self.assertTrue(hb["addon_heartbeat_alive"])

    def test_global_simulation_log_is_simulated(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _make_nt_root(tmp)
            (root / "log" / "log.20260821.00000.txt").write_text(
                "2026-08-21 00:00:00:000|1|4|Global simulation mode enabled\n",
                encoding="utf-8",
            )
            _write_readonly_market(root, last=None, quality="UNKNOWN")
            nt = NTReadOnly(root)
            hb = nt.market_heartbeat(stale_sec=120)
            self.assertEqual(hb["quality"], "SIMULATED")
            self.assertEqual(hb["status"], "SIMULATED")
            self.assertNotEqual(hb["status"], "LIVE")

    def test_file_mtime_fresh_quote_stale_is_stale(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _make_nt_root(tmp)
            old = datetime.fromtimestamp(time.time() - 1000, tz=timezone.utc).isoformat()
            _write_readonly_market(root, last=24800.0, last_update=old, quality="LIVE")
            ncd = root / "db" / "minute" / "MNQ 09-26" / "20260820.Last.ncd"
            os.utime(ncd, None)
            nt = NTReadOnly(root)
            hb = nt.market_heartbeat(stale_sec=120)
            self.assertIn(hb["status"], ("STALE", "CONNECTED_STALE"))
            self.assertGreater(hb["age_sec"], 120)
            self.assertNotEqual(hb["status"], "LIVE")

    def test_wrong_contract_fails_safely(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _make_nt_root(tmp)
            _write_readonly_market(root, last=5000.0, instrument="CL 09-26", quality="LIVE")
            nt = NTReadOnly(root)
            hb = nt.market_heartbeat(stale_sec=120)
            self.assertNotEqual(hb["status"], "LIVE")
            self.assertEqual(hb["reason"], "WRONG_CONTRACT")

    def test_no_stale_price_reuse_without_quote(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _make_nt_root(tmp, live_market=True)
            nt = NTReadOnly(root)
            hb1 = nt.market_heartbeat(stale_sec=120)
            self.assertEqual(hb1["status"], "LIVE")
            _write_readonly_market(root, last=None, quality="LIVE")
            hb2 = nt.market_heartbeat(stale_sec=120)
            self.assertNotEqual(hb2["status"], "LIVE")
            self.assertIsNone(hb2["last_price"])

    def test_market_reconnect_to_live(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _make_nt_root(tmp, feed_connected=False)
            nt = NTReadOnly(root)
            self.assertEqual(nt.market_heartbeat(stale_sec=120)["status"], "DISCONNECTED")
            _write_readonly_market(root, last=24750.25, quality="LIVE", market_connected="CONNECTED")
            hb = nt.market_heartbeat(stale_sec=120)
            self.assertEqual(hb["status"], "LIVE")
            self.assertEqual(hb["last_price"], 24750.25)

    def test_snapshot_exposes_market_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _make_nt_root(tmp, live_market=True)
            nt = NTReadOnly(root)
            with mock.patch("phase54_ops._nt", return_value=nt), mock.patch("phase54_ops._fn_mcp_snapshot", return_value=_mcp_fail()):
                snap = snapshot()
            self.assertEqual(snap["market_data_status"], "LIVE")
            self.assertEqual(snap["market_data_quality"], "LIVE")
            self.assertEqual(snap["market_data_connection"], "CONNECTED")
            self.assertIsNotNone(snap["market_last"])
            self.assertEqual(snap["order_execution"], "DISABLED")
            self.assertFalse(snap["PROP_EXECUTION"])
            self.assertEqual(snap["checks"]["display"]["fundednext_authenticated"], "FAIL")
            self.assertFalse(snap["checks"]["ok_to_run_engine"])


class Phase54F2EvidenceTests(unittest.TestCase):
    def test_global_simulation_true(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _make_nt_root(tmp)
            (root / "log" / "log.20260821.00000.txt").write_text(
                "2026-08-21 00:00:00:000|1|4|Global simulation mode enabled\n",
                encoding="utf-8",
            )
            nt = NTReadOnly(root)
            st = nt.global_simulation_state()
            self.assertTrue(st["global_simulation"])
            hb = nt.market_heartbeat(stale_sec=120)
            self.assertEqual(hb["quality"], "SIMULATED")
            self.assertNotEqual(hb["status"], "LIVE")

    def test_global_simulation_false_overrides_old_enabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _make_nt_root(tmp, feed_connected=False)
            (root / "log" / "log.20260820.00001.txt").write_text(
                "2026-08-20 23:00:00:000|1|4|Global simulation mode enabled\n",
                encoding="utf-8",
            )
            (root / "log" / "log.20260821.00000.txt").write_text(
                "2026-08-21 01:18:51:202|1|4|Global simulation mode disabled\n",
                encoding="utf-8",
            )
            nt = NTReadOnly(root)
            st = nt.global_simulation_state()
            self.assertFalse(st["global_simulation"])
            hb = nt.market_heartbeat(stale_sec=120)
            self.assertNotEqual(hb["quality"], "SIMULATED")
            self.assertEqual(hb["status"], "DISCONNECTED")
            self.assertEqual(hb["freshness"], "DISCONNECTED")

    def test_empty_provider_connection_list(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _make_nt_root(tmp, feed_connected=False)
            _write_readonly_market(root, last=None, quality="UNKNOWN", market_connected="DISCONNECTED", connections="")
            nt = NTReadOnly(root)
            hb = nt.market_heartbeat(stale_sec=120)
            self.assertEqual(hb["providers"], [])
            self.assertFalse(hb["market_provider_connected"])
            self.assertEqual(hb["ninjatrader_market_connection"], "DISCONNECTED")
            self.assertEqual(hb["quality"], "UNKNOWN")
            self.assertEqual(hb["freshness"], "DISCONNECTED")
            self.assertNotEqual(hb["status"], "LIVE")

    def test_real_provider_connected_without_quote_is_stale(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _make_nt_root(tmp, feed_connected=False)
            _write_readonly_market(
                root,
                last=None,
                quality="UNKNOWN",
                market_connected="CONNECTED",
                connections="NinjaTrader Continuum=Connected",
            )
            nt = NTReadOnly(root)
            hb = nt.market_heartbeat(stale_sec=120)
            self.assertTrue(hb["market_provider_connected"])
            self.assertEqual(hb["ninjatrader_market_connection"], "CONNECTED")
            self.assertEqual(hb["status"], "CONNECTED_STALE")
            self.assertEqual(hb["freshness"], "STALE")
            self.assertNotEqual(hb["status"], "LIVE")

    def test_simulated_provider_is_not_live(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _make_nt_root(tmp)
            _write_readonly_market(root, last=24800.25, quality="SIMULATED", connections="Simulated Data Feed=Connected")
            nt = NTReadOnly(root)
            hb = nt.market_heartbeat(stale_sec=120)
            self.assertEqual(hb["quality"], "SIMULATED")
            self.assertEqual(hb["status"], "SIMULATED")
            self.assertNotEqual(hb["freshness"], "LIVE")
            self.assertFalse(hb["market_provider_connected"])

    def test_delayed_provider_is_not_live(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _make_nt_root(tmp)
            _write_readonly_market(
                root,
                last=24800.25,
                quality="DELAYED",
                connections="Kinetick – End Of Day (Free)=Connected",
            )
            nt = NTReadOnly(root)
            hb = nt.market_heartbeat(stale_sec=120)
            self.assertEqual(hb["quality"], "DELAYED")
            self.assertEqual(hb["status"], "DELAYED")
            self.assertNotEqual(hb["freshness"], "LIVE")
            self.assertTrue(hb["market_provider_connected"])

    def test_real_quote_advancing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _make_nt_root(tmp)
            t1 = datetime.fromtimestamp(time.time() - 8, tz=timezone.utc).isoformat()
            t2 = datetime.fromtimestamp(time.time() - 2, tz=timezone.utc).isoformat()
            _write_readonly_market(root, last=24700.0, last_update=t1, quality="LIVE")
            nt = NTReadOnly(root)
            a = nt.market_heartbeat(stale_sec=120)
            _write_readonly_market(root, last=24701.25, last_update=t2, quality="LIVE")
            b = nt.market_heartbeat(stale_sec=120)
            self.assertEqual(a["status"], "LIVE")
            self.assertEqual(b["status"], "LIVE")
            self.assertEqual(b["last_price"], 24701.25)
            self.assertNotEqual(a["quote_timestamp"], b["quote_timestamp"])

    def test_heartbeat_advancing_but_quote_null(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _make_nt_root(tmp, feed_connected=False)
            _write_readonly_market(root, last=None, quality="UNKNOWN", market_connected="DISCONNECTED", connections="")
            nt = NTReadOnly(root)
            hb = nt.market_heartbeat(stale_sec=120)
            self.assertTrue(hb["addon_heartbeat_alive"])
            self.assertIsNone(hb["last_price"])
            self.assertIsNone(hb["quote_timestamp"])
            self.assertEqual(hb["status"], "DISCONNECTED")
            self.assertNotEqual(hb["status"], "LIVE")

    def test_mcp_credentials_missing(self):
        from fundednext_mcp import FundedNextMCPReadOnlyAdapter
        adapter = FundedNextMCPReadOnlyAdapter()
        with mock.patch("fundednext_mcp.resolve_access_token", return_value=None):
            doc = adapter.normalized_snapshot()
        self.assertEqual(doc["status"], "AUTH_FAILED")
        self.assertEqual(doc["reason"], "credentials_missing")
        self.assertFalse(doc["authenticated"])

    def test_oauth_session_present_metadata_has_no_tokens(self):
        from fundednext_mcp_oauth import oauth_session_metadata, save_oauth_session
        path = mutable_path("oauth_fixtures", uuid4().hex, "fundednext_mcp_oauth.json")
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.subTest(path=path.name):
            save_oauth_session(
                {
                    "access_token": "tok_secret",
                    "refresh_token": "tok_refresh",
                    "expires_at": "2026-08-21T04:00:00+00:00",
                    "scope": "mcp:read",
                    "token_type": "Bearer",
                },
                path,
            )
            meta = oauth_session_metadata(path)
            self.assertTrue(meta["session_present"])
            self.assertEqual(meta["scope"], "mcp:read")
            self.assertTrue(meta["has_refresh_token"])
            self.assertEqual(meta["expires_at"], "2026-08-21T04:00:00+00:00")
            self.assertNotIn("access_token", meta)
            self.assertNotIn("refresh_token", meta)
            gi = Path(".gitignore").read_text(encoding="utf-8")
            self.assertIn("state/fundednext_mcp_oauth.json", gi)

    def test_mcp_auth_transition_counted_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            j = Path(tmp)
            with mock.patch.multiple(
                "phase54_ops",
                JOURNAL_DIR=j,
                SOAK_PATH=j / "soak.json",
                EVENTS_LOG=j / "events.jsonl",
            ):
                snap = {
                    "market_data": {"freshness": "DISCONNECTED"},
                    "fundednext_mcp": {"status": "AUTH_FAILED", "reason": "credentials_missing"},
                    "position": {"reconciled": False},
                    "decision": {},
                }
                a = update_soak(snap)
                b = update_soak(snap)
                c = update_soak(snap)
                self.assertEqual(a["mcp_auth_failures"], 1)
                self.assertEqual(b["mcp_auth_failures"], 1)
                self.assertEqual(c["mcp_auth_failures"], 1)
                text = (j / "events.jsonl").read_text(encoding="utf-8")
                self.assertEqual(text.count("FundedNext MCP auth failed"), 1)
                self.assertNotIn("still unauthenticated", text)

    def test_repeated_auth_failed_rate_limited_warning(self):
        with tempfile.TemporaryDirectory() as tmp:
            j = Path(tmp)
            with mock.patch.multiple(
                "phase54_ops",
                JOURNAL_DIR=j,
                SOAK_PATH=j / "soak.json",
                EVENTS_LOG=j / "events.jsonl",
                AUTH_FAIL_REMINDER_SEC=0.05,
            ):
                snap = {
                    "market_data": {"freshness": "DISCONNECTED"},
                    "fundednext_mcp": {"status": "AUTH_FAILED", "reason": "credentials_missing"},
                    "position": {"reconciled": False},
                    "decision": {},
                }
                update_soak(snap)
                time.sleep(0.06)
                second = update_soak(snap)
                self.assertEqual(second["mcp_auth_failures"], 1)
                self.assertEqual(second["mcp_auth_reminders"], 1)
                text = (j / "events.jsonl").read_text(encoding="utf-8")
                self.assertIn("still unauthenticated (rate-limited)", text)

    def test_market_heartbeat_count_is_quote_events_not_api_reads(self):
        with tempfile.TemporaryDirectory() as tmp:
            j = Path(tmp)
            with mock.patch.multiple(
                "phase54_ops",
                JOURNAL_DIR=j,
                SOAK_PATH=j / "soak.json",
                EVENTS_LOG=j / "events.jsonl",
            ):
                snap = {
                    "market_data": {
                        "freshness": "LIVE",
                        "quote_timestamp": "2026-08-21T00:00:01Z",
                        "last": 24700.0,
                        "bid": 24699.75,
                        "ask": 24700.25,
                    },
                    "fundednext_mcp": {"status": "LIVE", "timestamp": "m1"},
                    "position": {"reconciled": True},
                    "decision": {},
                }
                a = update_soak(snap)
                b = update_soak(snap)
                self.assertEqual(a["market_heartbeat_count"], 1)
                self.assertEqual(b["market_heartbeat_count"], 1)
                snap["market_data"]["quote_timestamp"] = "2026-08-21T00:00:04Z"
                snap["market_data"]["last"] = 24701.0
                c = update_soak(snap)
                self.assertEqual(c["market_heartbeat_count"], 2)
                self.assertEqual(c["mcp_successful_reads"], 1)

    def test_rules_reconciliation_survival_critical(self):
        from prop_rules_v1 import load_profile
        raw = load_profile("FUNDEDNEXT_FLEX_50K").raw
        ok = reconcile_rules_against_prop_v1(_fn_overview(), raw)
        self.assertTrue(ok["rules_match"])
        self.assertTrue(ok["survival_critical_ok"])
        bad = _fn_overview()
        bad["objectives"]["overall_loss"]["permitted_loss"] = 1
        mismatch = reconcile_rules_against_prop_v1(bad, raw)
        self.assertFalse(mismatch["survival_critical_ok"])

    def test_three_way_position_reconciliation_all_flat(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _make_nt_root(tmp, live_market=True)
            nt = NTReadOnly(root)
            with mock.patch("phase54_ops._nt", return_value=nt), mock.patch("phase54_ops._fn_mcp_snapshot", return_value=_mcp_live()), mock.patch("phase54_ops.calendar_status_for", return_value=("OK", None)):
                snap = snapshot()
            recon = snap["position_reconciliation"]
            self.assertEqual(recon["ninjatrader"]["side"], "FLAT")
            self.assertEqual(recon["mcp"]["side"], "FLAT")
            self.assertEqual(recon["expected"]["side"], "FLAT")
            self.assertTrue(recon["reconciled"])

    def test_safe_start_requires_both_real_market_and_real_mcp(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _make_nt_root(tmp, live_market=True)
            nt = NTReadOnly(root)
            with mock.patch("phase54_ops._nt", return_value=nt), mock.patch("phase54_ops._fn_mcp_snapshot", return_value=_mcp_fail()), mock.patch("phase54_ops.calendar_status_for", return_value=("OK", None)):
                c = safe_start_checks()
            self.assertTrue(c["checks"]["fresh_market_data"])
            self.assertFalse(c["checks"]["fundednext_authenticated"])
            self.assertFalse(c["ok_to_run_engine"])
        with tempfile.TemporaryDirectory() as tmp:
            root = _make_nt_root(tmp, feed_connected=False)
            nt = NTReadOnly(root)
            with mock.patch("phase54_ops._nt", return_value=nt), mock.patch("phase54_ops._fn_mcp_snapshot", return_value=_mcp_live()), mock.patch("phase54_ops.calendar_status_for", return_value=("OK", None)):
                c = safe_start_checks()
            self.assertFalse(c["checks"]["fresh_market_data"])
            self.assertTrue(c["checks"]["fundednext_authenticated"])
            self.assertFalse(c["ok_to_run_engine"])

    def test_prop_execution_false_order_execution_disabled(self):
        self.assertFalse(prop_execution_allowed())
        snap = snapshot()
        self.assertFalse(snap["PROP_EXECUTION"])
        self.assertEqual(snap["order_execution"], "DISABLED")


class Phase54F6TradovateSimulationTests(unittest.TestCase):
    def test_classify_simulated_data_feed_is_artificial(self):
        sim = classify_market_provider("Simulated Data Feed")
        self.assertEqual(sim["kind"], "SIMULATED")
        self.assertEqual(sim["provider_kind"], "ARTIFICIAL")
        self.assertEqual(classify_market_provider("Simulation")["kind"], "SIMULATED")
        self.assertEqual(classify_market_provider("Playback Connection")["kind"], "PLAYBACK")
        self.assertEqual(classify_market_provider("Kinetick – End Of Day (Free)")["kind"], "DELAYED")

    def test_classify_ninjatrader_simulation_account_is_not_artificial(self):
        nt = classify_market_provider("NinjaTrader")
        self.assertEqual(nt["kind"], "LIVE")
        self.assertEqual(nt["provider_kind"], "TRADOVATE")
        named = classify_market_provider("My NinjaTrader")
        self.assertEqual(named["kind"], "LIVE")
        self.assertEqual(named["provider_kind"], "TRADOVATE")
        self.assertNotEqual(classify_market_provider("NinjaTrader")["kind"], "SIMULATED")

    def test_simulated_data_feed_is_simulated(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _make_nt_root(tmp)
            _write_readonly_market(root, last=24800.25, quality="SIMULATED", connections="Simulated Data Feed=Connected")
            nt = NTReadOnly(root)
            hb = nt.market_heartbeat(stale_sec=120)
            self.assertEqual(hb["quality"], "SIMULATED")
            self.assertEqual(hb["status"], "SIMULATED")
            self.assertNotEqual(hb["freshness"], "LIVE")
            self.assertFalse(hb["PROP_EXECUTION"])
            with mock.patch("phase54_ops._nt", return_value=nt), mock.patch("phase54_ops._fn_mcp_snapshot", return_value=_mcp_live()), mock.patch("phase54_ops.calendar_status_for", return_value=("OK", None)):
                c = safe_start_checks()
            self.assertFalse(c["checks"]["fresh_market_data"])

    def test_ninjatrader_simulation_account_without_quotes_is_stale(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _make_nt_root(tmp, feed_connected=False)
            _write_readonly_market(
                root,
                last=None,
                bid=None,
                ask=None,
                quality="SIMULATED",
                market_connected="CONNECTED",
                connections="NinjaTrader=Connected",
            )
            nt = NTReadOnly(root)
            hb = nt.market_heartbeat(stale_sec=120)
            self.assertEqual(hb["provider_kind"], "TRADOVATE")
            self.assertEqual(hb["account_environment"], "SIMULATION")
            self.assertEqual(hb["status"], "CONNECTED_STALE")
            self.assertEqual(hb["freshness"], "STALE")
            self.assertNotEqual(hb["status"], "LIVE")
            self.assertNotEqual(hb["quality"], "LIVE")

    def test_ninjatrader_simulation_account_with_advancing_quotes_is_live(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _make_nt_root(tmp)
            t1 = datetime.fromtimestamp(time.time() - 6, tz=timezone.utc).isoformat()
            t2 = datetime.fromtimestamp(time.time() - 1, tz=timezone.utc).isoformat()
            _write_readonly_market(
                root,
                last=24710.25,
                last_update=t1,
                quality="SIMULATED",
                connections="NinjaTrader=Connected",
            )
            nt = NTReadOnly(root)
            a = nt.market_heartbeat(stale_sec=120)
            _write_readonly_market(
                root,
                last=24711.00,
                last_update=t2,
                quality="SIMULATED",
                connections="NinjaTrader=Connected",
            )
            b = nt.market_heartbeat(stale_sec=120)
            self.assertEqual(a["status"], "LIVE")
            self.assertEqual(b["status"], "LIVE")
            self.assertEqual(b["quality"], "LIVE")
            self.assertEqual(b["provider_kind"], "TRADOVATE")
            self.assertEqual(b["account_environment"], "SIMULATION")
            self.assertEqual(b["provider_status"], "CONNECTED")
            self.assertIsNotNone(b["nq"].get("last"))
            self.assertIsNotNone(b["mnq"].get("last"))
            self.assertIsNotNone(b["nq"].get("bid"))
            self.assertIsNotNone(b["mnq"].get("ask"))
            self.assertNotEqual(a["quote_timestamp"], b["quote_timestamp"])
            self.assertFalse(b["PROP_EXECUTION"])
            with mock.patch("phase54_ops._nt", return_value=nt), mock.patch("phase54_ops._fn_mcp_snapshot", return_value=_mcp_live()), mock.patch("phase54_ops.calendar_status_for", return_value=("OK", None)):
                c = safe_start_checks()
                snap = snapshot()
            self.assertTrue(c["ok_to_run_engine"])
            self.assertEqual(c["display"]["fresh_market_data"], "PASS")
            self.assertEqual(snap["order_execution"], "DISABLED")
            self.assertFalse(snap["PROP_EXECUTION"])
            self.assertEqual(snap["provider_kind"], "TRADOVATE")
            self.assertEqual(snap["account_environment"], "SIMULATION")
            self.assertEqual(snap["market_data_quality"], "LIVE")

    def test_playback_is_not_live(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _make_nt_root(tmp)
            _write_readonly_market(root, last=24800.25, quality="PLAYBACK", connections="Playback Connection=Connected")
            nt = NTReadOnly(root)
            hb = nt.market_heartbeat(stale_sec=120)
            self.assertIn(hb["status"], ("PLAYBACK", "SIMULATED"))
            self.assertIn(hb["quality"], ("PLAYBACK", "SIMULATED"))
            self.assertNotEqual(hb["freshness"], "LIVE")

    def test_kinetick_eod_is_delayed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _make_nt_root(tmp)
            _write_readonly_market(
                root,
                last=24800.25,
                quality="DELAYED",
                connections="Kinetick – End Of Day (Free)=Connected",
            )
            nt = NTReadOnly(root)
            hb = nt.market_heartbeat(stale_sec=120)
            self.assertEqual(hb["quality"], "DELAYED")
            self.assertEqual(hb["status"], "DELAYED")
            self.assertNotEqual(hb["freshness"], "LIVE")

    def test_provider_name_alone_cannot_make_feed_live(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _make_nt_root(tmp, feed_connected=False)
            _write_readonly_market(
                root,
                last=None,
                bid=None,
                ask=None,
                quality="LIVE",
                market_connected="CONNECTED",
                connections="NinjaTrader=Connected",
            )
            nt = NTReadOnly(root)
            hb = nt.market_heartbeat(stale_sec=120)
            self.assertTrue(hb["market_provider_connected"])
            self.assertNotEqual(hb["status"], "LIVE")
            self.assertNotEqual(hb["quality"], "LIVE")

    def test_fresh_timestamps_required(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _make_nt_root(tmp)
            old = datetime.fromtimestamp(time.time() - 1000, tz=timezone.utc).isoformat()
            _write_readonly_market(
                root,
                last=24777.25,
                last_update=old,
                quality="LIVE",
                connections="NinjaTrader=Connected",
            )
            nt = NTReadOnly(root)
            hb = nt.market_heartbeat(stale_sec=120)
            self.assertIn(hb["status"], ("STALE", "CONNECTED_STALE"))
            self.assertGreater(hb["age_sec"], 120)
            self.assertNotEqual(hb["status"], "LIVE")

    def test_execution_flags_remain_disabled(self):
        self.assertFalse(prop_execution_allowed())
        import phase54_ops
        import nt_readonly
        self.assertFalse(phase54_ops.PROP_EXECUTION)
        self.assertFalse(nt_readonly.PROP_EXECUTION)


class Phase54F10SimulationDisplayNameTests(unittest.TestCase):
    def test_display_name_simulation_with_simulator_backend_is_simulated(self):
        art = classify_market_provider("Simulation", "SimulatorOptions")
        self.assertEqual(art["kind"], "SIMULATED")
        self.assertEqual(art["provider_kind"], "ARTIFICIAL")
        self.assertEqual(classify_market_provider("Simulation")["kind"], "SIMULATED")

    def test_display_name_simulation_with_tradovate_backend_is_live_capable(self):
        tv = classify_market_provider("Simulation", "TradovateOptions")
        self.assertEqual(tv["kind"], "LIVE")
        self.assertEqual(tv["provider_kind"], "TRADOVATE")
        self.assertNotEqual(tv["kind"], "SIMULATED")

    def test_simulated_data_feed_exact_is_simulated(self):
        sim = classify_market_provider("Simulated Data Feed")
        self.assertEqual(sim["kind"], "SIMULATED")
        self.assertEqual(sim["provider_kind"], "ARTIFICIAL")

    def test_simulation_tradovate_fresh_quotes_are_live(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _make_nt_root(tmp)
            t1 = datetime.fromtimestamp(time.time() - 6, tz=timezone.utc).isoformat()
            t2 = datetime.fromtimestamp(time.time() - 1, tz=timezone.utc).isoformat()
            prov = [{
                "name": "Simulation",
                "status": "Connected",
                "provider_type": "TradovateOptions",
                "provider_backend": "TradovateOptions",
                "provider_kind": "ARTIFICIAL",
                "provider_id": 50,
                "account_environment": "SIMULATION",
                "connected": True,
            }]
            _write_readonly_market(
                root,
                last=24710.25,
                last_update=t1,
                quality="SIMULATED",
                connections="Simulation=Connected",
                providers=prov,
            )
            nt = NTReadOnly(root)
            a = nt.market_heartbeat(stale_sec=120)
            _write_readonly_market(
                root,
                last=24711.00,
                last_update=t2,
                quality="SIMULATED",
                connections="Simulation=Connected",
                providers=prov,
            )
            b = nt.market_heartbeat(stale_sec=120)
            self.assertEqual(b["provider_kind"], "TRADOVATE")
            self.assertEqual(b["account_environment"], "SIMULATION")
            self.assertEqual(b["provider_backend"], "TradovateOptions")
            self.assertEqual(b["provider_id"], 50)
            self.assertEqual(b["status"], "LIVE")
            self.assertEqual(b["quality"], "LIVE")
            self.assertNotEqual(a["quote_timestamp"], b["quote_timestamp"])
            with mock.patch("phase54_ops._nt", return_value=nt), mock.patch("phase54_ops._fn_mcp_snapshot", return_value=_mcp_live()), mock.patch("phase54_ops.calendar_status_for", return_value=("OK", None)):
                c = safe_start_checks()
                snap = snapshot()
            self.assertTrue(c["ok_to_run_engine"])
            self.assertEqual(c["display"]["fresh_market_data"], "PASS")
            self.assertEqual(snap["order_execution"], "DISABLED")
            self.assertFalse(snap["PROP_EXECUTION"])

    def test_tradovate_stale_or_null_quotes_are_stale(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _make_nt_root(tmp, feed_connected=False)
            _write_readonly_market(
                root,
                last=None,
                bid=None,
                ask=None,
                quality="UNKNOWN",
                market_connected="CONNECTED",
                connections="Simulation=Connected",
                providers=[{
                    "name": "Simulation",
                    "status": "Connected",
                    "provider_type": "TradovateOptions",
                    "provider_kind": "TRADOVATE",
                    "provider_id": 50,
                    "account_environment": "SIMULATION",
                    "connected": True,
                }],
            )
            nt = NTReadOnly(root)
            hb = nt.market_heartbeat(stale_sec=120)
            self.assertEqual(hb["provider_kind"], "TRADOVATE")
            self.assertNotEqual(hb["status"], "LIVE")
            self.assertNotEqual(hb["quality"], "LIVE")

    def test_display_name_alone_cannot_create_live(self):
        self.assertEqual(classify_market_provider("Simulation")["kind"], "SIMULATED")
        with tempfile.TemporaryDirectory() as tmp:
            root = _make_nt_root(tmp)
            _write_readonly_market(
                root,
                last=24800.25,
                quality="UNKNOWN",
                connections="Simulation=Connected",
            )
            nt = NTReadOnly(root)
            hb = nt.market_heartbeat(stale_sec=120)
            self.assertEqual(hb["quality"], "SIMULATED")
            self.assertNotEqual(hb["status"], "LIVE")

    def test_account_environment_simulation_alone_is_not_simulated(self):
        rec = _normalize_provider({
            "name": "NinjaTrader",
            "status": "Connected",
            "provider_type": "TradovateOptions",
            "provider_kind": "TRADOVATE",
            "account_environment": "SIMULATION",
            "connected": True,
        })
        self.assertEqual(rec["kind"], "LIVE")
        self.assertEqual(rec["provider_kind"], "TRADOVATE")
        self.assertEqual(rec["account_environment"], "SIMULATION")
        self.assertNotEqual(rec["kind"], "SIMULATED")

    def test_fn_bound_simulation_display_name_is_tradovate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _make_nt_root(tmp)
            now = datetime.now(timezone.utc).isoformat()
            _write_readonly_market(
                root,
                last=24722.25,
                last_update=now,
                quality="SIMULATED",
                connections="Simulation=Connected",
            )
            path = root / "outgoing" / "AITRADE_READONLY.json"
            doc = json.loads(path.read_text(encoding="utf-8"))
            doc["fundednext"] = {
                "account_id": FN_NAME,
                "connected": True,
                "connection_name": "Simulation",
                "connection_backend": "TradovateOptions",
            }
            path.write_text(json.dumps(doc), encoding="utf-8")
            hb = NTReadOnly(root).market_heartbeat(stale_sec=120)
            self.assertEqual(hb["provider_kind"], "TRADOVATE")
            self.assertEqual(hb["provider_display_name"], "Simulation")
            self.assertEqual(hb["account_environment"], "SIMULATION")
            self.assertEqual(hb["status"], "LIVE")
            self.assertEqual(hb["quality"], "LIVE")
            self.assertNotEqual(hb["provider_name"], "Simulated Data Feed")

    def test_safe_start_pass_keeps_execution_disabled(self):
        self.assertFalse(prop_execution_allowed())
        self.assertFalse(PROP_EXECUTION)


if __name__ == "__main__":
    unittest.main()
