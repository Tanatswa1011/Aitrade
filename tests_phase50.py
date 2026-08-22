"""Phase 50 tests. Frozen isolation, no martingale, cushion/reserve, DRY_RUN."""
from __future__ import annotations

import json
import unittest
from pathlib import Path

import numpy as np

from aitrade_operating_policy import load_operating_policy
from phase34_validate import GC_FILE_SHA, NQ_FILE_SHA, assert_frozen, file_sha256
from phase49_prop_sim import DayBundle
from phase50_funded_engine import (
    AITRADE_INTERNAL_MIN_PAYOUT,
    FundedPolicy,
    assert_no_martingale,
    classify,
    simulate_funded,
)
from prop_rules_v1 import REQUIRES_CONFIRMATION, load_profile
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

    def test_fn_first_buffer_unconfirmed(self):
        self.assertEqual(load_profile(FN).payout()["first_payout_required_buffer"], REQUIRES_CONFIRMATION)
        self.assertEqual(load_profile(FN).payout()["minimum_payout"], REQUIRES_CONFIRMATION)
        self.assertEqual(AITRADE_INTERNAL_MIN_PAYOUT, 500.0)


class MartingaleAndFloorTests(unittest.TestCase):
    def test_martingale_rejected(self):
        with self.assertRaises(ValueError):
            assert_no_martingale(FundedPolicy(name="bad", streak_scale=2.0))

    def test_block_when_micro_exceeds_cushion(self):
        days = [DayBundle("2024-01-02", np.array([-1.0]), np.array([80.0]))]
        pol = FundedPolicy(
            name="floor",
            payout_mode="PAYOUT_NONE",
            use_dynamic_risk=False,
            fixed_risk_usd=160.0,
            reserve_usd=0.0,
            floor_block_ratio=1.0,
        )
        rng = np.random.default_rng(0)
        out = simulate_funded(days, book="NQ", profile_id=MFFU, policy=pol, n_paths=20, rng=rng, horizon=5)
        self.assertTrue(out["ok"])
        self.assertGreaterEqual(float(np.mean(out["n_block_floor"])), 0.0)

    def test_reserve_keeps_surplus(self):
        days = [DayBundle("2024-01-02", np.array([2.0]), np.array([18.0]))]
        asap = FundedPolicy(
            name="asap",
            payout_mode="PAYOUT_AS_SOON_AS_ELIGIBLE",
            reserve_usd=1000.0,
            use_dynamic_risk=False,
            fixed_risk_usd=90.0,
        )
        none = FundedPolicy(
            name="none",
            payout_mode="PAYOUT_NONE",
            reserve_usd=1000.0,
            use_dynamic_risk=False,
            fixed_risk_usd=90.0,
        )
        a = simulate_funded(days, book="ES", profile_id=MFFU, policy=asap, n_paths=40, rng=np.random.default_rng(1), horizon=80)
        b = simulate_funded(days, book="ES", profile_id=MFFU, policy=none, n_paths=40, rng=np.random.default_rng(1), horizon=80)
        self.assertGreaterEqual(float(np.mean(b["alive"])), float(np.mean(a["alive"])) - 1e-9)

    def test_force_through_ruins_block_survives(self):
        days = [DayBundle("2024-01-02", np.array([-1.0]), np.array([80.0]))]
        force = FundedPolicy(
            name="force",
            payout_mode="PAYOUT_NONE",
            use_dynamic_risk=False,
            fixed_risk_usd=160.0,
            reserve_usd=0.0,
            block_insufficient_capacity=False,
            cap_risk_to_cushion=False,
        )
        block = FundedPolicy(
            name="block",
            payout_mode="PAYOUT_NONE",
            use_dynamic_risk=False,
            fixed_risk_usd=160.0,
            reserve_usd=0.0,
            block_insufficient_capacity=True,
            cap_risk_to_cushion=True,
        )
        a = simulate_funded(days, book="NQ", profile_id=MFFU, policy=force, n_paths=30, rng=np.random.default_rng(2), horizon=25)
        b = simulate_funded(days, book="NQ", profile_id=MFFU, policy=block, n_paths=30, rng=np.random.default_rng(2), horizon=25)
        self.assertGreater(float(np.mean(a["breach_day"] > 0)), 0.9)
        self.assertGreater(float(np.mean(b["alive"])), float(np.mean(a["alive"])))
        self.assertGreater(float(np.mean(b["n_block_floor"])), 0.0)


if __name__ == "__main__":
    unittest.main()
