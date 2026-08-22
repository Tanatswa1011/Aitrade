"""Phase 55B.0 — Sim101 telemetry + live NQ DVP input. No OIF. Frozen hash unchanged."""

from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

from models import Bar
from nq_databento import aggregate_1m_to_ny
from nq_dvp_freeze import frozen_config_hash, load_frozen_document, load_frozen_strategy_config, semantic_payload
from nq_dvp_live_feed import (
    bar_identity,
    evaluate_live_dvp,
    load_nt_1m_bars,
    merge_warmup_and_live,
)
from nq_dvp_live_signal import evaluate_completed_bars
from execution_status import NQ_FROZEN_HASH
from phase55_execution_bridge import (
    FN_EVAL_ACCOUNT,
    RECOVERY_FLAT_SAFE,
    RECOVERY_ORPHAN_POSITION,
    RECOVERY_UNKNOWN,
    NinjaTraderExecutionBridge,
)
from sim101_telemetry import (
    fundednext_must_not_substitute,
    parse_sim101_position,
    recovery_from_sim101,
)

os.environ.setdefault("AITRADE_PHASE54_TEST", "1")
NY = ZoneInfo("America/New_York")
EXPECTED_HASH = "935e3a616351b09dbfa8d2d0e2b5d6850be803fb385ec5e5f8e3593856a1212a"


def _now_iso() -> str:
    return datetime.now(tz=NY).astimezone().isoformat()


def _dump_sim101(*, side="FLAT", qty=0, present=True, ts=None, excluded=False):
    ts = ts or datetime.now().astimezone().isoformat()
    pos = None
    if present:
        pos = {
            "instrument": "MNQ 09-26",
            "quantity": qty,
            "side": side,
            "average_price": 24800.0 if side != "FLAT" else None,
            "timestamp": ts,
            "source": "NINJATRADER_ACCOUNT_POSITION",
        }
    return {
        "timestamp": ts,
        "ts": ts,
        "position": {"instrument": "MNQ 09-26", "side": "FLAT", "quantity": 0, "average_price": None},
        "sim101_excluded": excluded,
        "sim101": {
            "present": present,
            "excluded": excluded,
            "account": "Sim101" if present else None,
            "read_only": True,
            "position": pos,
        },
        "nq_bars_1m": [],
    }


def _bridge(tmp, **kw):
    return NinjaTraderExecutionBridge(state_path=Path(tmp) / "st.json", detect_orphans=lambda *a, **k: {"orphan_count": 0, "oco_live_count": 0}, **kw)


def _1m(trading_date: str, hour: int, minute: int, o, h, l, c, vol=100.0) -> Bar:
    from datetime import timedelta

    dt = datetime.fromisoformat(f"{trading_date}T00:00:00").replace(tzinfo=NY) + timedelta(hours=hour, minutes=minute)
    return Bar(time=int(dt.timestamp()), open=o, high=h, low=l, close=c, volume=vol)


class Sim101TelemetryTests(unittest.TestCase):
    def test_known_flat_flat_safe(self):
        p = parse_sim101_position(_dump_sim101(side="FLAT", qty=0))
        self.assertTrue(p["known"])
        self.assertTrue(p["flat"])
        self.assertEqual(p["source"], "NINJATRADER_ACCOUNT_POSITION")
        self.assertEqual(recovery_from_sim101(p), RECOVERY_FLAT_SAFE)
        with tempfile.TemporaryDirectory() as tmp:
            b = _bridge(tmp, parse_position=lambda **k: p)
            out = b.reconcile()
        self.assertEqual(out["status"], RECOVERY_FLAT_SAFE)

    def test_long_expected_flat_orphan(self):
        p = parse_sim101_position(_dump_sim101(side="LONG", qty=1))
        self.assertFalse(p["flat"])
        self.assertEqual(recovery_from_sim101(p), RECOVERY_ORPHAN_POSITION)
        with tempfile.TemporaryDirectory() as tmp:
            b = _bridge(tmp, parse_position=lambda **k: p)
            out = b.reconcile()
        self.assertEqual(out["status"], RECOVERY_ORPHAN_POSITION)

    def test_short_expected_flat_orphan(self):
        p = parse_sim101_position(_dump_sim101(side="SHORT", qty=1))
        self.assertEqual(recovery_from_sim101(p), RECOVERY_ORPHAN_POSITION)

    def test_missing_unknown(self):
        p = parse_sim101_position(_dump_sim101(present=False))
        p = fundednext_must_not_substitute(_dump_sim101(present=False), p)
        self.assertFalse(p["known"])
        self.assertIsNone(p["flat"])
        self.assertEqual(recovery_from_sim101(p), RECOVERY_UNKNOWN)
        with tempfile.TemporaryDirectory() as tmp:
            b = _bridge(tmp, parse_position=lambda **k: p)
            out = b.reconcile()
        self.assertEqual(out["status"], RECOVERY_UNKNOWN)

    def test_stale_unknown(self):
        old = "2026-08-20T10:00:00+00:00"
        p = parse_sim101_position(_dump_sim101(side="FLAT", qty=0, ts=old), now=datetime.fromisoformat(old).timestamp() + 120.0, stale_sec=30)
        self.assertTrue(p["stale"])
        self.assertFalse(p["known"])
        self.assertEqual(recovery_from_sim101(p), RECOVERY_UNKNOWN)

    def test_fundednext_flat_does_not_substitute(self):
        dump = {
            "timestamp": datetime.now().astimezone().isoformat(),
            "position": {"side": "FLAT", "quantity": 0, "instrument": "MNQ 09-26"},
            "sim101": {"present": False, "position": None},
        }
        p = fundednext_must_not_substitute(dump, parse_sim101_position(dump))
        self.assertFalse(p["known"])
        self.assertIsNone(p["flat"])
        self.assertTrue(p["fundednext_position_ignored"])
        self.assertEqual(recovery_from_sim101(p), RECOVERY_UNKNOWN)

    def test_old_addon_excluded_without_position_is_unknown(self):
        dump = {
            "timestamp": datetime.now().astimezone().isoformat(),
            "sim101_excluded": True,
            "sim101": {"present": True, "excluded": True, "id": "Sim101"},
            "position": {"side": "FLAT", "quantity": 0},
        }
        p = fundednext_must_not_substitute(dump, parse_sim101_position(dump))
        self.assertFalse(p["known"])
        self.assertEqual(recovery_from_sim101(p), RECOVERY_UNKNOWN)

    def test_addon_read_only(self):
        src = Path("ninjascript/AITRADEReadOnlySnapshot.cs").read_text(encoding="utf-8")
        self.assertNotIn("PLACE", src)
        self.assertNotIn("CLOSEPOSITION", src)
        self.assertIn("Never writes incoming", src)
        self.assertIn("NINJATRADER_ACCOUNT_POSITION", src)
        self.assertIn("nq_bars_1m", src)
        self.assertIn("read_only", src.lower())


class LiveBarTests(unittest.TestCase):
    def test_5m_and_15m_finalization(self):
        td = "2026-08-21"
        bars = [_1m(td, 10, m, 100, 101, 99, 100.5) for m in range(0, 15)]
        b5 = aggregate_1m_to_ny(bars, 5)
        b15 = aggregate_1m_to_ny(bars, 15)
        self.assertEqual(len(b5), 3)
        self.assertEqual(len(b15), 1)
        self.assertEqual(bar_identity(b5[0].time, "5m").count("5m"), 1)
        self.assertIn(":5m", bar_identity(b5[-1].time, "5m"))
        self.assertIn(":15m", bar_identity(b15[0].time, "15m"))

    def test_timezone_and_dst(self):
        winter = datetime(2026, 1, 15, 10, 30, tzinfo=NY)
        summer = datetime(2026, 7, 15, 10, 30, tzinfo=NY)
        self.assertEqual(winter.utcoffset().total_seconds(), -5 * 3600)
        self.assertEqual(summer.utcoffset().total_seconds(), -4 * 3600)
        self.assertTrue(bar_identity(int(winter.timestamp()), "5m").startswith("NQ:2026-01-15T10:30:00-05:00"))
        self.assertTrue(bar_identity(int(summer.timestamp()), "5m").startswith("NQ:2026-07-15T10:30:00-04:00"))

    def test_session_closed_and_open(self):
        td = "2026-08-21"
        live = [_1m(td, 10, m, 100, 101, 99, 100) for m in range(0, 90)]
        dump = {"nq_bars_1m": [{"time": b.time, "open": b.open, "high": b.high, "low": b.low, "close": b.close, "volume": b.volume, "finalized": True} for b in live]}
        closed = int(datetime(2026, 8, 21, 22, 0, tzinfo=NY).timestamp())
        opened = int(datetime(2026, 8, 21, 11, 0, tzinfo=NY).timestamp())
        a = evaluate_live_dvp(dump=dump, now_ts=closed, warmup_bars=[], persist=False)
        b = evaluate_live_dvp(dump=dump, now_ts=opened, warmup_bars=[], persist=False)
        self.assertEqual(a["strategy_status"], "SESSION_CLOSED")
        self.assertEqual(a["pipeline"], "LIVE_DVP_PIPELINE_READY_SESSION_CLOSED")
        self.assertFalse(a["executable"])
        self.assertIn(b["strategy_status"], ("WARMING_UP", "READY", "LIVE"))
        self.assertNotEqual(b["pipeline"], "LIVE_DVP_PIPELINE_READY_SESSION_CLOSED")

    def test_warmup_boundary_not_executable(self):
        td = "2026-08-21"
        warm = [_1m(td, 9, m, 100, 101, 99, 100) for m in range(30, 60)]
        live = [_1m(td, 11, m, 100, 101, 99, 100) for m in range(0, 10)]
        merged = merge_warmup_and_live(warm, live)
        self.assertLess(merged["last_historical_bar_ts"], merged["first_live_bar_ts"])
        dump = {"nq_bars_1m": [{"time": b.time, "open": b.open, "high": b.high, "low": b.low, "close": b.close, "volume": b.volume, "finalized": True} for b in live]}
        now = int(datetime(2026, 8, 21, 11, 20, tzinfo=NY).timestamp())
        out = evaluate_live_dvp(dump=dump, now_ts=now, warmup_bars=warm, persist=False)
        sig = out.get("live_signal")
        if sig:
            self.assertFalse(sig.get("executable"))
            if sig.get("source") == "HISTORICAL_WARMUP":
                self.assertFalse(sig.get("live_bar"))

    def test_partial_bar_excluded(self):
        dump = {
            "nq_bars_1m": [
                {"time": 1, "open": 1, "high": 2, "low": 1, "close": 1.5, "volume": 10, "finalized": True},
                {"time": 61, "open": 1.5, "high": 2, "low": 1, "close": 1.6, "volume": 10, "finalized": False},
            ]
        }
        bars = load_nt_1m_bars(dump)
        self.assertEqual(len(bars), 1)
        self.assertEqual(bars[0].time, 1)

    def test_unique_signal_ids(self):
        a = bar_identity(int(datetime(2026, 8, 21, 10, 45, tzinfo=NY).timestamp()), "5m")
        b = bar_identity(int(datetime(2026, 8, 21, 10, 50, tzinfo=NY).timestamp()), "5m")
        self.assertNotEqual(a, b)
        self.assertEqual(a, "NQ:2026-08-21T10:45:00-04:00:5m")


class StrategyIntegrityTests(unittest.TestCase):
    def test_frozen_hash_unchanged(self):
        doc = load_frozen_document()
        cfg = load_frozen_strategy_config(doc)
        h = frozen_config_hash(semantic_payload(cfg))
        self.assertEqual(h, EXPECTED_HASH)
        self.assertEqual(h, NQ_FROZEN_HASH)
        self.assertEqual(doc.get("frozen_config_hash"), EXPECTED_HASH)

    def test_same_bars_same_output(self):
        td = "2026-08-14"
        bars_1m = [_1m(td, 9, 30 + i, 20000 + i, 20010 + i, 19990 + i, 20005 + i, 50) for i in range(0, 180)]
        b5 = aggregate_1m_to_ny(bars_1m, 5)
        b15 = aggregate_1m_to_ny(bars_1m, 15)
        now = int(datetime(2026, 8, 14, 12, 0, tzinfo=NY).timestamp())
        a = evaluate_completed_bars(bars_1m=bars_1m, bars_5m=b5, bars_15m=b15, trading_date=td, now_ts=now)
        b = evaluate_completed_bars(bars_1m=bars_1m, bars_5m=b5, bars_15m=b15, trading_date=td, now_ts=now)
        self.assertEqual(a.to_dict()["state"], b.to_dict()["state"])
        self.assertEqual(a.to_dict()["intended_order"], b.to_dict()["intended_order"])
        self.assertEqual(a.to_dict()["pending_entry"], b.to_dict()["pending_entry"])


class ExecutionBoundaryTests(unittest.TestCase):
    def test_phase54_live_approved_reaches_bridge(self):
        from phase54_ops import try_execute_approved_sim_only

        live = {
            "direction": "LONG",
            "intended_entry": 24800.0,
            "trading_date": "2026-08-21",
            "source": "phase54_live",
            "live_bar": True,
            "accepted": True,
            "ts": datetime.now().astimezone().isoformat(),
        }
        drop = mock.Mock()
        submit = mock.Mock(return_value={"ok": True, "submitted": False, "status": "GATES_PASSED", "error_code": "SIM_ONLY_NOT_ARMED"})
        with mock.patch.dict(os.environ, {"AITRADE_SIM_ONLY_EXECUTION": "1"}):
            with mock.patch("phase54_ops.EngineSupervisor._load", return_value={"engine": "RUNNING", "entries_paused": False}):
                with mock.patch("phase54_ops.last_operator_signal", return_value=live):
                    with mock.patch("phase54_ops.PolicyEngine.evaluate", return_value={"verdict": "ALLOW", "code": "ALLOW", "calendar_status": "OK", "lane": "FAST", "allowed_qty": 1, "reasons": []}):
                        with mock.patch("phase55_execution_bridge.NinjaTraderExecutionBridge") as br:
                            inst = mock.Mock()
                            inst.submit.return_value = {"ok": True, "submitted": False, "status": "PREFLIGHT", "error_code": None, "PROP_EXECUTION": False}
                            br.return_value = inst
                            with mock.patch("nt_ati.drop_oif", drop), mock.patch("nq_dvp_nt_exec.nt.drop_oif", drop):
                                out = try_execute_approved_sim_only()
        inst.submit.assert_called_once()
        drop.assert_not_called()
        self.assertFalse(out.get("submitted"))

    def test_shadow_cannot_execute(self):
        from phase54_ops import try_execute_approved_sim_only

        drop = mock.Mock()
        shadow = {"direction": "SHORT", "source": "phase53_shadow", "live_bar": False, "accepted": True, "intended_entry": 1}
        with mock.patch.dict(os.environ, {"AITRADE_SIM_ONLY_EXECUTION": "1"}):
            with mock.patch("phase54_ops.EngineSupervisor._load", return_value={"engine": "RUNNING", "entries_paused": False}):
                with mock.patch("phase54_ops.last_operator_signal", return_value=shadow):
                    with mock.patch("nt_ati.drop_oif", drop):
                        out = try_execute_approved_sim_only()
        self.assertEqual(out["error_code"], "LIVE_DVP_REQUIRED")
        drop.assert_not_called()

    def test_warmup_cannot_execute(self):
        from phase54_ops import try_execute_approved_sim_only

        drop = mock.Mock()
        warm = {"direction": "LONG", "source": "HISTORICAL_WARMUP", "live_bar": False, "accepted": True}
        with mock.patch.dict(os.environ, {"AITRADE_SIM_ONLY_EXECUTION": "1"}):
            with mock.patch("phase54_ops.EngineSupervisor._load", return_value={"engine": "RUNNING", "entries_paused": False}):
                with mock.patch("phase54_ops.last_operator_signal", return_value=warm):
                    with mock.patch("nt_ati.drop_oif", drop):
                        out = try_execute_approved_sim_only()
        self.assertEqual(out["error_code"], "LIVE_DVP_REQUIRED")
        drop.assert_not_called()

    def test_stale_live_event_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            b = _bridge(
                tmp,
                parse_position=lambda **k: {"flat": True, "known": True, "quantity": 0, "side": "FLAT"},
            )
            b.reconcile()
            intent = {
                "direction": "LONG",
                "account": "Sim101",
                "instrument": "MNQ 09-26",
                "quantity": 1,
                "strategy_id": "NQ_DRIFT_VWAP_PULLBACK",
                "strategy_hash": NQ_FROZEN_HASH,
                "policy_verdict": "ALLOW",
                "calendar_status": "OK",
                "data_age_sec": 9_999,
                "trigger_key": "stale-1",
                "nt_connected": True,
                "mode": "SIM_ONLY",
            }
            with mock.patch("phase55_execution_bridge.sim_only_execution_armed", return_value=True):
                with mock.patch("phase55_execution_bridge.assert_execution_allowed"):
                    out = b.submit(intent, transmit=True)
            self.assertFalse(out.get("submitted"))
            self.assertNotEqual(out.get("status"), "SUBMITTED")

    def test_duplicate_live_event_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            submit_fn = mock.Mock(return_value={"ok": True, "status": "DRY_RUN"})
            b = _bridge(
                tmp,
                parse_position=lambda **k: {"flat": True, "known": True, "quantity": 0, "side": "FLAT"},
                submit_bracket=submit_fn,
            )
            b.reconcile()
            intent = {
                "direction": "LONG",
                "account": "Sim101",
                "instrument": "MNQ 09-26",
                "quantity": 1,
                "strategy_id": "NQ_DRIFT_VWAP_PULLBACK",
                "strategy_hash": NQ_FROZEN_HASH,
                "policy_verdict": "ALLOW",
                "calendar_status": "OK",
                "data_age_sec": 1,
                "trigger_key": "dup-1",
                "nt_connected": True,
                "mode": "SIM_ONLY",
            }
            with mock.patch("phase55_execution_bridge.sim_only_execution_armed", return_value=True):
                with mock.patch("phase55_execution_bridge.assert_execution_allowed"):
                    first = b.submit(intent, transmit=True)
                    second = b.submit(intent, transmit=True)
            self.assertEqual(second.get("error_code"), "DUPLICATE_ORDER_DETECTED")
            self.assertFalse(second.get("submitted"))

    def test_fn_account_still_blocked(self):
        self.assertEqual(FN_EVAL_ACCOUNT, "FNFTCHTANATSWAPHILMU92044")
        from phase54_ops import PROP_EXECUTION, prop_execution_allowed

        self.assertFalse(PROP_EXECUTION)
        self.assertFalse(prop_execution_allowed())


if __name__ == "__main__":
    unittest.main()
