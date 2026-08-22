"""Phase 49 tests. Does not modify frozen strategies or paper journals."""
from __future__ import annotations

import json
import unittest
from pathlib import Path

import numpy as np

from aitrade_operating_policy import load_operating_policy
from phase34_validate import GC_FILE_SHA, NQ_FILE_SHA, assert_frozen, file_sha256
from phase49_distributions import distribution_report
from phase49_prop_sim import (
    DayBundle,
    PolicySpec,
    assert_no_martingale,
    initial_max_loss,
    size_qty,
    simulate_eval_path,
    trades_to_days,
)
from prop_rules_v1 import load_profile, load_rules_document
from risk_manager import propose_size

ROOT = Path(__file__).resolve().parent
MFFU = "MFFU_RAPID_EOD_50K"
FN = "FUNDEDNEXT_FLEX_50K"


class FrozenIsolationTests(unittest.TestCase):
    def test_frozen_hashes_unchanged(self):
        gc = json.loads((ROOT / "strategy_frozen" / "gc_vwap_v2_phase26.json").read_text(encoding="utf-8"))
        nq = json.loads((ROOT / "strategy_frozen" / "nq_dvp_phase30.json").read_text(encoding="utf-8"))
        self.assertEqual(gc["frozen_config_hash"], "0695d7a881b6247033426bceb077b6895b96405b826c21f32a109a02f44b2d43")
        self.assertEqual(nq["frozen_config_hash"], "935e3a616351b09dbfa8d2d0e2b5d6850be803fb385ec5e5f8e3593856a1212a")
        self.assertEqual(file_sha256(ROOT / "strategy_frozen" / "gc_vwap_v2_phase26.json"), GC_FILE_SHA)
        self.assertEqual(file_sha256(ROOT / "strategy_frozen" / "nq_dvp_phase30.json"), NQ_FILE_SHA)
        self.assertFalse((ROOT / "strategy_frozen" / "es_dvp_phase47.json").exists())
        frozen = assert_frozen()
        self.assertTrue(frozen.get("ok"), frozen)

    def test_paper_journals_empty(self):
        for rel in (
            "journal/phase26_gc_vwap_v2_paper/paper_trades.jsonl",
            "journal/phase30_nq_dvp_paper/paper_trades.jsonl",
            "journal/phase47_es_dvp_paper/paper_trades.jsonl",
        ):
            p = ROOT / rel
            self.assertTrue(p.exists())
            self.assertEqual(p.stat().st_size, 0)


class PolicyLockTests(unittest.TestCase):
    def test_dry_run_and_null_risk(self):
        pol = load_operating_policy()
        self.assertEqual(pol.execution_default, "DRY_RUN")
        self.assertFalse(pol.broker_execution)
        self.assertIsInstance(pol.numerics.get("risk_per_trade"), dict)
        self.assertEqual(propose_size()["status"], "PROP_QTY_LOCKED")

    def test_firm_numbers_from_prop_rules_v1(self):
        self.assertEqual(initial_max_loss(MFFU, "EVALUATION"), 2000)
        self.assertEqual(initial_max_loss(FN, "EVALUATION"), 1500)
        self.assertEqual(load_profile(MFFU).payout()["first_payout_required_buffer"], 2100)
        self.assertEqual(load_profile(FN).stage("FUNDED").get("benchmark_days_required"), 5)
        doc = load_rules_document()
        self.assertEqual(doc["execution_default"], "DRY_RUN")


class SizingAndMartingaleTests(unittest.TestCase):
    def test_mnq_cannot_size_at_100(self):
        qty, actual = size_qty("NQ", 100.0, 80.0, 30)
        self.assertEqual(qty, 0)
        self.assertEqual(actual, 0.0)

    def test_mnq_one_micro_at_160(self):
        qty, actual = size_qty("NQ", 160.0, 80.0, 30)
        self.assertEqual(qty, 1)
        self.assertEqual(actual, 160.0)

    def test_mes_one_micro_at_90(self):
        qty, actual = size_qty("ES", 90.0, 18.0, 30)
        self.assertEqual(qty, 1)
        self.assertEqual(actual, 90.0)

    def test_martingale_rejected(self):
        with self.assertRaises(ValueError):
            assert_no_martingale(PolicySpec(name="BAD", defensive_scale=2.0))


class DistributionAndSimTests(unittest.TestCase):
    def test_distribution_fields(self):
        trades = [
            {"trading_date": "2024-01-02", "r_multiple": 1.0},
            {"trading_date": "2024-01-02", "r_multiple": -1.0},
            {"trading_date": "2024-01-03", "r_multiple": 2.0},
        ]
        rep = distribution_report(trades, book="TEST")
        self.assertEqual(rep["number_of_trades"], 3)
        self.assertAlmostEqual(rep["win_rate"], 2 / 3)
        self.assertIn("max_historical_drawdown_R", rep)
        self.assertIn("90th_percentile_losing_streak", rep)

    def test_eval_breach_on_all_losses(self):
        days = [DayBundle("2024-01-02", np.array([-1.0]), np.array([18.0]))]
        rng = np.random.default_rng(0)
        out = simulate_eval_path(
            days,
            book="ES",
            profile_id=FN,
            dd_frac=0.20,
            policy=PolicySpec(name="FIXED"),
            rng=rng,
            mode="bootstrap",
            horizon=40,
        )
        self.assertIn(out["terminal"], ("BREACH", "TIMEOUT"))

    def test_eval_can_pass_on_wins(self):
        days = [DayBundle("2024-01-02", np.array([2.0]), np.array([18.0]))]
        rng = np.random.default_rng(1)
        out = simulate_eval_path(
            days,
            book="ES",
            profile_id=FN,
            dd_frac=0.20,
            policy=PolicySpec(name="FIXED"),
            rng=rng,
            mode="bootstrap",
            horizon=80,
        )
        self.assertEqual(out["terminal"], "PASS")

    def test_day_clustering_preserved(self):
        trades = [
            {"trading_date": "2020-01-02", "r_multiple": 0.5, "risk_points": 80},
            {"trading_date": "2020-01-02", "r_multiple": -0.2, "risk_points": 80},
            {"trading_date": "2020-01-03", "r_multiple": 0.1, "risk_points": 80},
        ]
        days = trades_to_days(trades, "NQ")
        self.assertEqual(len(days), 2)
        self.assertEqual(len(days[0].r), 2)


class OriginalsUntouchedTests(unittest.TestCase):
    def test_phase46_sources_exist(self):
        self.assertTrue((ROOT / "reports" / "phase46_nq_dvp_frozen_proxy.csv").exists())
        self.assertTrue((ROOT / "reports" / "phase46_es_dvp.csv").exists())


if __name__ == "__main__":
    unittest.main()
