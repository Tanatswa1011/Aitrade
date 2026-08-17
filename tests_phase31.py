"""Phase 31 tests — frozen DVP → Sim101 integration (no live OIF submission)."""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

from models import Bar
from nq_dvp_freeze import (
    FROZEN_STRATEGY_VERSION,
    load_frozen_document,
    load_frozen_strategy_config,
    semantic_payload,
    frozen_config_hash,
)
from nq_dvp_live_runner import assert_frozen_immutable, historical_live_equivalence, run_once
from nq_dvp_live_signal import extract_signal_entries_for_day
from nq_dvp_nt_exec import assert_execution_locks, plan_dvp_entry, submit_dvp_bracket
from nq_databento import aggregate_1m_to_ny
from nt_ati import asymmetric_bracket_prices, build_bracket_child_oifs

NY = ZoneInfo("America/New_York")
PHASE26_HASH = "0695d7a881b6247033426bceb077b6895b96405b826c21f32a109a02f44b2d43"
PHASE26_FROZEN = Path("strategy_frozen") / "gc_vwap_v2_phase26.json"
PHASE30_HASH = "935e3a616351b09dbfa8d2d0e2b5d6850be803fb385ec5e5f8e3593856a1212a"


def _ts(y, m, d, hh, mm):
    return int(datetime(y, m, d, hh, mm, tzinfo=NY).timestamp())


def _bar(t, o, h, l, c, v=100.0):
    return Bar(time=t, open=o, high=h, low=l, close=c, volume=v)


class FreezeTests(unittest.TestCase):
    def test_frozen_loads(self):
        doc = load_frozen_document()
        self.assertEqual(doc["strategy_version"], FROZEN_STRATEGY_VERSION)
        self.assertEqual(doc["frozen_config_hash"], PHASE30_HASH)
        cfg = load_frozen_strategy_config(doc)
        self.assertEqual(cfg.hour_return_threshold, 0.001)
        self.assertEqual(cfg.long_stop_points, 80)
        self.assertEqual(cfg.long_target_points, 40)
        self.assertEqual(cfg.short_stop_points, 80)
        self.assertEqual(cfg.short_target_points, 50)
        self.assertEqual(cfg.max_trades_per_day, 4)
        self.assertEqual(cfg.max_losses_per_day, 2)

    def test_no_semantic_mutation(self):
        info = assert_frozen_immutable()
        self.assertTrue(info["ok"])
        self.assertFalse(info["semantic_mutation"])
        cfg = load_frozen_strategy_config()
        self.assertEqual(frozen_config_hash(semantic_payload(cfg)), PHASE30_HASH)


class SafetyLockTests(unittest.TestCase):
    def test_sim101_only(self):
        with self.assertRaises(PermissionError) as ctx:
            assert_execution_locks(account="Live1", quantity=1)
        self.assertIn("LIVE_ACCOUNT_BLOCKED", str(ctx.exception))

    def test_qty_one(self):
        with self.assertRaises(PermissionError) as ctx:
            assert_execution_locks(account="Sim101", quantity=2)
        self.assertIn("QUANTITY_BLOCKED", str(ctx.exception))

    def test_mnq_only(self):
        with self.assertRaises(PermissionError):
            assert_execution_locks(account="Sim101", quantity=1, instrument="NQ SEP26")


class BracketDistanceTests(unittest.TestCase):
    def test_long_80_40(self):
        px = asymmetric_bracket_prices("LONG", 9000.0, stop_points=80, target_points=40)
        self.assertEqual(px["stop"], 8920.0)
        self.assertEqual(px["target"], 9040.0)

    def test_short_80_50(self):
        px = asymmetric_bracket_prices("SHORT", 9000.0, stop_points=80, target_points=50)
        self.assertEqual(px["stop"], 9080.0)
        self.assertEqual(px["target"], 8950.0)

    def test_oco_children_use_dvp_distances(self):
        kids = build_bracket_child_oifs(
            direction="LONG",
            entry_fill=9000.0,
            oco_id="AITRADE_DVP_OCO_x",
            stop_order_id="AITRADE_DVP_x_STOP",
            target_order_id="AITRADE_DVP_x_TGT",
            stop_points=80,
            target_points=40,
        )
        self.assertIn("8920.0", kids["stop_line"])
        self.assertIn("9040.0", kids["target_line"])
        self.assertIn("AITRADE_DVP_OCO_x", kids["stop_line"])


class DryRunExecTests(unittest.TestCase):
    def test_plan_no_submit(self):
        plan = plan_dvp_entry(direction="LONG", trade_id="AITRADE_DVP_2026-07-02_LONG_1", stop_points=80, target_points=40)
        self.assertFalse(plan["submitted"])
        self.assertEqual(plan["account"], "Sim101")
        self.assertEqual(plan["instrument"], "MNQ SEP26")

    def test_submit_false_no_oif(self):
        with mock.patch("nq_dvp_nt_exec.nt.drop_oif") as drop:
            out = submit_dvp_bracket(
                direction="LONG",
                trade_id="AITRADE_DVP_t",
                stop_points=80,
                target_points=40,
                submit=False,
            )
            self.assertEqual(out["status"], "DRY_RUN_PLAN")
            drop.assert_not_called()

    def test_runner_default_no_execution(self):
        with mock.patch("nq_dvp_live_runner.submit_dvp_bracket") as sub:
            out = run_once(enable_sim=False)
            self.assertTrue(out.get("ok"))
            self.assertEqual(out.get("mode"), "DRY_RUN")
            sub.assert_not_called()


class SessionRuleConstantTests(unittest.TestCase):
    def test_frozen_session_and_risk(self):
        doc = load_frozen_document()
        self.assertEqual(doc["session"]["vwap_reset"], "09:30")
        self.assertEqual(doc["session"]["trade_start"], "10:30")
        self.assertEqual(doc["session"]["no_new_trades_after"], "15:30")
        self.assertEqual(doc["session"]["force_close"], "15:55")
        self.assertEqual(doc["risk"]["long_stop_points"], 80.0)
        self.assertEqual(doc["risk"]["long_target_points"], 40.0)
        self.assertEqual(doc["risk"]["short_target_points"], 50.0)


class IsolationTests(unittest.TestCase):
    def test_phase26_hash(self):
        doc = json.loads(PHASE26_FROZEN.read_text(encoding="utf-8"))
        self.assertEqual(doc["frozen_config_hash"], PHASE26_HASH)

    def test_phase30_hash(self):
        doc = load_frozen_document()
        self.assertEqual(doc["frozen_config_hash"], PHASE30_HASH)


class EquivalenceSmokeTests(unittest.TestCase):
    def test_equivalence_or_skip_if_no_data(self):
        out = historical_live_equivalence(max_days=3)
        if out.get("error_code") == "SIGNAL_DATA_UNAVAILABLE":
            self.skipTest("NQ stitched data not present")
        self.assertEqual(out.get("verdict"), "LIVE_EQUIVALENCE_OK", out)


if __name__ == "__main__":
    unittest.main()
