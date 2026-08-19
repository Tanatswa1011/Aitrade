"""Phase 38 frozen-isolation and leak-safety tests."""
from __future__ import annotations

import json
import unittest
from pathlib import Path

from models import Bar
from nq_pdh_pdl import local_ts
from orb_index_engine import build_opening_range, detect_first_break, simulate
from phase34_validate import GC_FILE_SHA, NQ_FILE_SHA, file_sha256

ROOT = Path(__file__).resolve().parent


def _bars(td: str, start_hhmm: str, rows: list[tuple[float, float, float, float]]) -> list[Bar]:
    t0 = local_ts(td, start_hhmm)
    out = []
    t = t0
    for o, h, l, c in rows:
        out.append(Bar(time=t, open=o, high=h, low=l, close=c, volume=100))
        t += 60
    return out


class FrozenIsolationTests(unittest.TestCase):
    def test_frozen_hashes_unchanged(self):
        gc = json.loads((ROOT / "strategy_frozen" / "gc_vwap_v2_phase26.json").read_text(encoding="utf-8"))
        nq = json.loads((ROOT / "strategy_frozen" / "nq_dvp_phase30.json").read_text(encoding="utf-8"))
        self.assertEqual(gc["frozen_config_hash"], "0695d7a881b6247033426bceb077b6895b96405b826c21f32a109a02f44b2d43")
        self.assertEqual(nq["frozen_config_hash"], "935e3a616351b09dbfa8d2d0e2b5d6850be803fb385ec5e5f8e3593856a1212a")
        self.assertEqual(file_sha256(ROOT / "strategy_frozen" / "gc_vwap_v2_phase26.json"), GC_FILE_SHA)
        self.assertEqual(file_sha256(ROOT / "strategy_frozen" / "nq_dvp_phase30.json"), NQ_FILE_SHA)

    def test_spec_primary_declared(self):
        spec = json.loads((ROOT / "phase38_spec.json").read_text(encoding="utf-8"))
        self.assertEqual(spec["methodology_corrections"], [])
        self.assertEqual(spec["primary_candidate"]["or_minutes"], 15)
        self.assertEqual(spec["primary_candidate"]["entry"], "B_close_1m")
        self.assertEqual(spec["primary_candidate"]["target_R"], 1.0)
        self.assertIn("No VWAP", spec["forbidden"])


class LeakSafetyTests(unittest.TestCase):
    def test_or_not_valid_before_window_close(self):
        td = "2026-01-05"
        # 4 minutes of bars is incomplete for OR5
        bars = _bars(td, "09:30", [(100, 101, 99, 100)] * 4)
        orng = build_opening_range(bars, td, 5)
        self.assertFalse(orng.complete)

    def test_close_break_enters_next_open_not_or_high(self):
        td = "2026-01-05"
        # OR5: 09:30-09:35, high 101 low 99
        or_bars = _bars(td, "09:30", [
            (100, 101, 99.5, 100.5),
            (100.5, 100.8, 99.0, 100.2),
            (100.2, 100.4, 99.8, 100.1),
            (100.1, 100.3, 99.9, 100.0),
            (100.0, 100.2, 99.7, 100.0),
        ])
        rest = _bars(td, "09:35", [
            (100.0, 101.4, 100.0, 101.2),  # close above 101
            (101.3, 103.0, 101.2, 102.5),  # entry bar
            (102.5, 104.0, 102.4, 103.8),
        ])
        rth = or_bars + rest
        orng = build_opening_range(or_bars, td, 5)
        self.assertTrue(orng.complete)
        self.assertAlmostEqual(orng.high, 101.0)
        self.assertAlmostEqual(orng.low, 99.0)
        br = detect_first_break(rth, orng, family="close_1m")
        self.assertIsNotNone(br)
        self.assertEqual(br["direction"], "LONG")
        trade = simulate(instrument="NQ", rth=rth, orng=orng, family="close_1m", stop_mode="A_opposite", target_r=1.0, adverse_ticks=1.0)
        self.assertEqual(trade.status, "ENTERED")
        self.assertGreater(trade.entry_fill, trade.entry_theo)
        self.assertNotAlmostEqual(trade.entry_fill, orng.high)

    def test_same_bar_stop_and_target_ambiguous(self):
        td = "2026-01-05"
        or_bars = _bars(td, "09:30", [(100, 101, 99, 100)] * 5)
        rest = _bars(td, "09:35", [
            (100.0, 101.5, 100.0, 101.2),
            (101.3, 110.0, 90.0, 100.0),  # both stop and target possible
        ])
        rth = or_bars + rest
        orng = build_opening_range(or_bars, td, 5)
        trade = simulate(instrument="NQ", rth=rth, orng=orng, family="close_1m", stop_mode="A_opposite", target_r=1.0, adverse_ticks=1.0)
        self.assertEqual(trade.outcome, "AMBIGUOUS")


if __name__ == "__main__":
    unittest.main()
