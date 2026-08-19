"""Phase 46 frozen-isolation and portability spec tests."""
from __future__ import annotations

import json
import unittest
from pathlib import Path

from family_port_engine import CLOCKS, dvp_scaled_cfg, in_news_blackout, round_tick
from models import Bar
from nq_drift_vwap_engine import session_anchors
from nq_pdh_pdl import local_ts
from phase34_validate import GC_FILE_SHA, NQ_FILE_SHA, file_sha256

ROOT = Path(__file__).resolve().parent


class FrozenIsolationTests(unittest.TestCase):
    def test_frozen_hashes_unchanged(self):
        gc = json.loads((ROOT / "strategy_frozen" / "gc_vwap_v2_phase26.json").read_text(encoding="utf-8"))
        nq = json.loads((ROOT / "strategy_frozen" / "nq_dvp_phase30.json").read_text(encoding="utf-8"))
        self.assertEqual(gc["frozen_config_hash"], "0695d7a881b6247033426bceb077b6895b96405b826c21f32a109a02f44b2d43")
        self.assertEqual(nq["frozen_config_hash"], "935e3a616351b09dbfa8d2d0e2b5d6850be803fb385ec5e5f8e3593856a1212a")
        self.assertEqual(file_sha256(ROOT / "strategy_frozen" / "gc_vwap_v2_phase26.json"), GC_FILE_SHA)
        self.assertEqual(file_sha256(ROOT / "strategy_frozen" / "nq_dvp_phase30.json"), NQ_FILE_SHA)

    def test_spec_locked_before_pnl(self):
        spec = json.loads((ROOT / "phase46_spec.json").read_text(encoding="utf-8"))
        self.assertEqual(spec["methodology_corrections"], [])
        self.assertEqual(spec["chrono"]["train_end"], "2022-12-30")
        self.assertEqual(spec["vwap_mr"]["sigma_primary"], 2.0)
        self.assertEqual(spec["atr_normalization"]["version_2_normalized"], "PRIMARY for all DVP ports.")
        self.assertTrue(spec["atr_normalization"]["do_not_search_atr_multipliers"])
        self.assertIn("No FVG", spec["forbidden"])

    def test_paper_journals_exist_and_are_not_required_to_grow(self):
        gc = ROOT / "journal" / "phase26_gc_vwap_v2_paper" / "paper_trades.jsonl"
        nq = ROOT / "journal" / "phase30_nq_dvp_paper" / "paper_trades.jsonl"
        self.assertTrue(gc.exists())
        self.assertTrue(nq.exists())


class SessionAndScaleTests(unittest.TestCase):
    def test_es_mr_is_rth_not_gc_gold_window(self):
        self.assertEqual(CLOCKS["ES_MR"]["start"], "09:30")
        self.assertNotEqual(CLOCKS["ES_MR"]["start"], CLOCKS["GC_MR"]["start"])
        self.assertEqual(CLOCKS["CL_MR"]["start"], "09:00")
        self.assertEqual(CLOCKS["CL_DVP"]["force_close"], "14:25")

    def test_dvp_default_anchors_remain_nq(self):
        ts = session_anchors("2024-07-15")
        self.assertEqual(ts["vwap_reset"], int(local_ts("2024-07-15", "09:30")))
        self.assertEqual(ts["trade_start"], int(local_ts("2024-07-15", "10:30")))

    def test_normalized_dvp_does_not_copy_80_es_points_when_scale_differs(self):
        cfg = dvp_scaled_cfg(0.4, 0.25)
        self.assertLess(cfg.long_stop_points, 80.0)
        self.assertAlmostEqual(cfg.long_stop_points / cfg.long_target_points, 2.0, places=0)

    def test_cl_eia_wednesday_blackout_only(self):
        wed = int(local_ts("2024-07-17", "10:30"))  # Wednesday
        thu = int(local_ts("2024-07-18", "10:30"))
        self.assertTrue(in_news_blackout(wed, "CL"))
        self.assertFalse(in_news_blackout(thu, "CL"))
        self.assertFalse(in_news_blackout(wed, "ES"))

    def test_round_tick(self):
        self.assertEqual(round_tick(12.37, 0.25), 12.25)


if __name__ == "__main__":
    unittest.main()
