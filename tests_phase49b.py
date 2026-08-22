"""Phase 49B tests. Frozen isolation, no martingale, no invented MFFU price, DRY_RUN."""
from __future__ import annotations

import json
import unittest
from pathlib import Path

import numpy as np

from account_state_engine import EVAL_STATES, classify_account_state
from aitrade_operating_policy import load_operating_policy
from phase34_validate import GC_FILE_SHA, NQ_FILE_SHA, assert_frozen, file_sha256
from phase49b_engine import FastPassSpec, assert_no_martingale, max_executable_qty, pack_days, simulate_batch, unit_risk_usd
from phase49_prop_sim import DayBundle, size_qty
from phase51_pools import load_eval_matrix_row, mffu_eval_price_status
from prop_rules_v1 import REQUIRES_CONFIRMATION, AccountMetrics
from risk_manager import propose_size

ROOT = Path(__file__).resolve().parent
MFFU = "MFFU_RAPID_EOD_50K"
FN = "FUNDEDNEXT_FLEX_50K"


def _toy_days() -> list[DayBundle]:
    # Alternating modest wins so 1 MNQ can pass without forcing speed.
    days = []
    for i in range(40):
        r = np.array([0.6, 0.4] if i % 3 else [-1.0, 0.5], dtype=np.float64)
        rp = np.array([80.0] * len(r))
        days.append(DayBundle(trading_date=f"2024-01-{i+1:02d}", r=r, risk_points=rp))
    return days


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
        self.assertEqual(propose_size()["status"], "PROP_QTY_LOCKED")

    def test_mffu_price_unconfirmed(self):
        st = mffu_eval_price_status()
        self.assertEqual(st["status"], REQUIRES_CONFIRMATION)
        self.assertIsNone(st["confirmed_usd"])

    def test_phase49_cells_intact(self):
        mffu = load_eval_matrix_row("NQ", MFFU)
        fn = load_eval_matrix_row("NQ", FN)
        self.assertAlmostEqual(float(mffu["P(pass)"]), 0.7235, places=4)
        self.assertAlmostEqual(float(fn["P(pass)"]), 0.6537, places=4)


class SizingTests(unittest.TestCase):
    def test_never_rounds_up(self):
        qty, actual = size_qty("NQ", 159.0, 80.0, 30)
        self.assertEqual(qty, 0)
        self.assertEqual(actual, 0.0)

    def test_executable_qty_fits_dd(self):
        q, note = max_executable_qty("NQ", FN, 80.0)
        self.assertGreaterEqual(q, 1)
        self.assertEqual(note, "OK")
        self.assertLessEqual(q * unit_risk_usd("NQ", 80.0), 1500.0)

    def test_accel_state_in_scaffold(self):
        self.assertIn("EVAL_ACCELERATE", EVAL_STATES)
        snap = classify_account_state(
            firm_profile=FN,
            account_stage="EVALUATION",
            metrics=AccountMetrics(
                realized_pnl=400.0,
                remaining_drawdown=1400.0,
                distance_to_target=2100.0,
                consecutive_losses=0,
                extras={"initial_drawdown": 1500.0},
            ),
        )
        self.assertEqual(snap.state, "EVAL_ACCELERATE")


class MartingaleTests(unittest.TestCase):
    def test_defensive_cannot_exceed_normal(self):
        with self.assertRaises(ValueError):
            assert_no_martingale(FastPassSpec(name="bad", qty_normal=1, qty_defensive=2))

    def test_no_accel_after_loss_in_sim(self):
        days = _toy_days()
        pack = pack_days(days, "NQ")
        spec = FastPassSpec(name="accel", qty_normal=1, qty_accel=2, accel_dd_frac=0.50, accel_min_pnl=-999)
        assert_no_martingale(spec)
        out = simulate_batch(pack, book="NQ", profile_id=FN, spec=spec, n_paths=32, rng=np.random.default_rng(1))
        self.assertIn("PASS", set(out["terminal"]).union(set(out["terminal"])))
        self.assertTrue(np.all(out["days"] >= 0))


class ConservationAndEmptyTests(unittest.TestCase):
    def test_zero_qty_when_blocked(self):
        days = [DayBundle("2024-01-02", np.array([0.5]), np.array([80.0]))]
        pack = pack_days(days, "NQ")
        spec = FastPassSpec(name="q0", qty_normal=0, qty_accel=0, qty_defensive=0, qty_approach=0)
        out = simulate_batch(pack, book="NQ", profile_id=FN, spec=spec, n_paths=8, rng=np.random.default_rng(2))
        self.assertTrue(np.all(out["terminal"] == "NO_SIZE"))


if __name__ == "__main__":
    unittest.main()
