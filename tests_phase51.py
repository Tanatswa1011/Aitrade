"""Phase 51 tests. Frozen isolation, no invented MFFU price, dollar conservation, DRY_RUN."""
from __future__ import annotations

import json
import unittest
from pathlib import Path

import numpy as np

from aitrade_operating_policy import load_operating_policy
from phase34_validate import GC_FILE_SHA, NQ_FILE_SHA, assert_frozen, file_sha256
from phase51_flywheel import FlywheelSpec, classify_replication, simulate_paths
from phase51_pools import FN, MFFU, fn_eval_prices, funded_caps, load_eval_matrix_row, mffu_eval_price_status
from prop_rules_v1 import REQUIRES_CONFIRMATION
from risk_manager import propose_size

ROOT = Path(__file__).resolve().parent


def _toy_pools() -> dict:
    n = 8
    passed = np.array([True, True, True, True, True, False, False, False])
    days = np.array([12, 18, 22, 30, 40, 8, 15, 20], dtype=np.int32)
    po_day = np.full((n, 80), -1, dtype=np.int32)
    po_amt = np.zeros((n, 80))
    n_po = np.zeros(n, dtype=np.int32)
    for i in range(n):
        n_po[i] = 3
        po_day[i, :3] = [20, 45, 70]
        po_amt[i, :3] = [500.0, 500.0, 500.0]
    breach = np.full(n, -1, dtype=np.int32)
    ev = {
        "book": "NQ",
        "profile": FN,
        "empirical_P(pass)": float(np.mean(passed)),
        "phase49_median_days_to_pass": 18.0,
        "passed": passed,
        "days": days,
    }
    fu = {
        "n": n,
        "po_day": po_day,
        "po_amt": po_amt,
        "n_po": n_po,
        "breach_day": breach,
        "phase50_expected_payout": 1500.0,
        "profile": FN,
    }
    ev_m = dict(ev)
    ev_m["profile"] = MFFU
    fu_m = dict(fu)
    return {
        "eval": {f"NQ->{FN}": ev, f"NQ->{MFFU}": ev_m},
        "funded": {f"NQ->{FN}": fu, f"NQ->{MFFU}": fu_m},
        "fn_price": fn_eval_prices(),
        "mffu_price": mffu_eval_price_status(),
    }


class FrozenIsolationTests(unittest.TestCase):
    def test_frozen_hashes_unchanged(self):
        gc = json.loads((ROOT / "strategy_frozen" / "gc_vwap_v2_phase26.json").read_text(encoding="utf-8"))
        nq = json.loads((ROOT / "strategy_frozen" / "nq_dvp_phase30.json").read_text(encoding="utf-8"))
        self.assertEqual(gc["frozen_config_hash"], "0695d7a881b6247033426bceb077b6895b96405b826c21f32a109a02f44b2d43")
        self.assertEqual(nq["frozen_config_hash"], "935e3a616351b09dbfa8d2d0e2b5d6850be803fb385ec5e5f8e3593856a1212a")
        self.assertEqual(file_sha256(ROOT / "strategy_frozen" / "gc_vwap_v2_phase26.json"), GC_FILE_SHA)
        self.assertEqual(file_sha256(ROOT / "strategy_frozen" / "nq_dvp_phase30.json"), NQ_FILE_SHA)
        self.assertFalse((ROOT / "strategy_frozen" / "es_dvp_phase47.json").exists())
        self.assertTrue(assert_frozen().get("ok"))

    def test_paper_journals_empty(self):
        for rel in (
            "journal/phase26_gc_vwap_v2_paper/paper_trades.jsonl",
            "journal/phase30_nq_dvp_paper/paper_trades.jsonl",
            "journal/phase47_es_dvp_paper/paper_trades.jsonl",
        ):
            self.assertEqual((ROOT / rel).stat().st_size, 0)


class PolicyLockTests(unittest.TestCase):
    def test_dry_run_null_risk(self):
        pol = load_operating_policy()
        self.assertEqual(pol.execution_default, "DRY_RUN")
        self.assertFalse(pol.broker_execution)
        self.assertIsInstance(pol.numerics.get("risk_per_trade"), dict)
        self.assertEqual(pol.numerics["risk_per_trade"].get("mode"), "PROP_CONTRACT_QTY")
        self.assertEqual(propose_size()["status"], "PROP_QTY_LOCKED")
        self.assertEqual(propose_size()["quantity"], 2)
        self.assertFalse(propose_size()["broker_execution"])

    def test_mffu_price_unconfirmed(self):
        st = mffu_eval_price_status()
        self.assertEqual(st["status"], REQUIRES_CONFIRMATION)
        self.assertIsNone(st["confirmed_usd"])

    def test_fn_prices_confirmed(self):
        px = fn_eval_prices()
        self.assertEqual(px["first_5_purchase_price"], 69.99)
        self.assertEqual(px["purchase_6_plus_price"], 79.99)
        self.assertEqual(px["reset_fee"], 77.99)

    def test_fn_cap_unconfirmed(self):
        caps = funded_caps()
        self.assertEqual(caps["FUNDEDNEXT_FLEX_50K"]["status"], REQUIRES_CONFIRMATION)
        self.assertEqual(caps["MFFU_RAPID_EOD_50K"]["value"], 3)

    def test_phase49_cells_not_fabricated(self):
        mffu = load_eval_matrix_row("NQ", MFFU)
        fn = load_eval_matrix_row("NQ", FN)
        self.assertAlmostEqual(float(mffu["P(pass)"]), 0.7235, places=4)
        self.assertEqual(float(mffu["median_days_to_pass"]), 55.0)
        self.assertAlmostEqual(float(fn["P(pass)"]), 0.6537, places=4)
        self.assertEqual(float(fn["median_days_to_pass"]), 40.0)


class FlywheelUnitTests(unittest.TestCase):
    def test_cannot_buy_with_empty_bankroll(self):
        pools = _toy_pools()
        spec = FlywheelSpec(name="broke", start_cash=10.0, expansion="FUNDEDNEXT_ONLY")
        out = simulate_paths(spec, pools, n_paths=20, rng=np.random.default_rng(1))
        self.assertEqual(out["total_evaluation_attempts"], 0.0)
        self.assertGreater(out["probability_bankroll_exhausted_before_self_funding"], 0.9)

    def test_conservation(self):
        pools = _toy_pools()
        spec = FlywheelSpec(name="cons", start_cash=500.0, expansion="FUNDEDNEXT_ONLY", fn_cap=3)
        out = simulate_paths(spec, pools, n_paths=40, rng=np.random.default_rng(2))
        self.assertLess(out["conservation_gap"], 0.05)

    def test_classify_unsupported_zero(self):
        self.assertEqual(
            classify_replication(
                {
                    "P(self_funding_by_1y)": 0.0,
                    "probability_bankroll_exhausted_before_self_funding": 1.0,
                    "h365_expected_active_funded": 0.0,
                    "payout_dollars_per_eval_dollar": 0.0,
                }
            ),
            "REPLICATION_UNSUPPORTED",
        )


if __name__ == "__main__":
    unittest.main()
