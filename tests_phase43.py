"""Phase 43 frozen-isolation and data-gate tests."""
from __future__ import annotations

import json
import unittest
from pathlib import Path

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

    def test_spec_primary_locked(self):
        spec = json.loads((ROOT / "phase43_spec.json").read_text(encoding="utf-8"))
        self.assertEqual(spec["methodology_corrections"], [])
        self.assertEqual(spec["primary_candidate"]["id"], "SMALLCAP_GAP50_OR5_BREAKDOWN")
        self.assertEqual(spec["status"], "DEFINITIONS_FROZEN_BEFORE_ENTRIES")
        self.assertTrue(spec.get("not_futures"))
        self.assertIn("No yfinance", spec["forbidden"])


class DataGateTests(unittest.TestCase):
    def test_no_local_equity_bars(self):
        data = ROOT / "data"
        hits = []
        for p in data.rglob("*.jsonl"):
            name = p.name.lower()
            if any(x in name for x in ("equity", "stock", "nasdaq_listed", "nyse_listed")):
                hits.append(str(p))
        self.assertEqual(hits, [])

    def test_validation_is_blocked_not_a_fake_edge(self):
        path = ROOT / "phase43_validation.json"
        if not path.exists():
            self.skipTest("run phase43_validate.py first")
        payload = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(payload.get("verdict"), "SMALLCAP_DATA_QUALITY_BLOCKED")
        self.assertTrue(payload.get("FLOAT_DATA_UNAVAILABLE"))
        self.assertFalse(payload.get("primary_tested"))
        self.assertEqual(payload.get("n_entered"), 0)
        self.assertFalse(payload.get("candidate_written"))


if __name__ == "__main__":
    unittest.main()
