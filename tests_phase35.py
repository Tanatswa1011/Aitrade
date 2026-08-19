"""Phase 35 frozen-isolation and spec tests."""

from __future__ import annotations

import hashlib
import json
import unittest
from datetime import date
from pathlib import Path

from nq_front_month import contract_on_rth_date, load_rolls

ROOT = Path(__file__).resolve().parent


class FrozenIsolationTests(unittest.TestCase):
    def test_frozen_hashes_unchanged(self):
        gc = json.loads((ROOT / "strategy_frozen" / "gc_vwap_v2_phase26.json").read_text(encoding="utf-8"))
        nq = json.loads((ROOT / "strategy_frozen" / "nq_dvp_phase30.json").read_text(encoding="utf-8"))
        self.assertEqual(gc["frozen_config_hash"], "0695d7a881b6247033426bceb077b6895b96405b826c21f32a109a02f44b2d43")
        self.assertEqual(nq["frozen_config_hash"], "935e3a616351b09dbfa8d2d0e2b5d6850be803fb385ec5e5f8e3593856a1212a")
        self.assertEqual(
            hashlib.sha256((ROOT / "strategy_frozen" / "gc_vwap_v2_phase26.json").read_bytes()).hexdigest(),
            "12615d19ef3deed36b7929c161ef7c377975cd2317c0ae6d06f1949bd327046f",
        )
        self.assertEqual(
            hashlib.sha256((ROOT / "strategy_frozen" / "nq_dvp_phase30.json").read_bytes()).hexdigest(),
            "34927fba0f268e56cf166cf8939bdfec6aa031510f7cee9acfab3909dd36b541",
        )


class SpecTests(unittest.TestCase):
    def test_phase35_spec_does_not_retune_outcomes(self):
        spec = json.loads((ROOT / "phase35_spec.json").read_text(encoding="utf-8"))
        c = spec["carried_forward_from_phase_34"]
        self.assertEqual(c["primary_horizon_sec"], 300)
        self.assertEqual(c["reversal_target_points"], 8.0)
        self.assertEqual(c["continuation_extension_points"], 12.0)
        self.assertEqual(c["feature_lookback_sec"], 60)
        self.assertEqual(spec["methodology_corrections"], [])

    def test_roll_day_rth_stays_on_old_contract(self):
        rolls = load_rolls()
        # NQH6 -> NQM6 decision 2026-03-16; RTH that day is still NQH6.
        self.assertEqual(contract_on_rth_date(date(2026, 3, 16), rolls), "NQH6")
        self.assertEqual(contract_on_rth_date(date(2026, 3, 17), rolls), "NQM6")
        self.assertEqual(contract_on_rth_date(date(2026, 6, 14), rolls), "NQM6")
        self.assertEqual(contract_on_rth_date(date(2026, 6, 15), rolls), "NQU6")


if __name__ == "__main__":
    unittest.main()
