"""Phase 39 frozen-isolation and leak-safety tests."""
from __future__ import annotations

import json
import unittest
from pathlib import Path

from models import Bar
from nq_pdh_pdl import local_ts
from orb_index_engine import build_opening_range, simulate
from orb_retest_engine import scan_retest, simulate_retest
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
        spec = json.loads((ROOT / "phase39_spec.json").read_text(encoding="utf-8"))
        self.assertEqual(spec["methodology_corrections"], [])
        p = spec["primary_candidate"]
        self.assertEqual(p["id"], "OR15_1M_BREAK_1M_RETEST_HOLD")
        self.assertEqual(p["or_minutes"], 15)
        self.assertEqual(p["breakout"], "close_1m")
        self.assertEqual(p["retest_trigger"], "T0_exact")
        self.assertEqual(p["fail_frac"], 0.10)
        self.assertEqual(p["confirm"], "B_close_1m")
        self.assertEqual(p["stop"], "A_retest_extreme")
        self.assertEqual(p["target_R"], 1.0)
        self.assertTrue(spec["final_orb_branch"])


class LeakSafetyTests(unittest.TestCase):
    def _or5_breakout_setup(self):
        td = "2026-01-05"
        or_bars = _bars(td, "09:30", [(100, 101, 99, 100)] * 5)
        rest = _bars(td, "09:35", [
            (100.0, 101.4, 100.0, 101.2),  # close break
            (101.3, 101.6, 101.1, 101.4),  # extension, no retest
            (101.4, 101.5, 100.9, 101.1),  # retest touch OR_HIGH=101
            (101.15, 101.8, 101.05, 101.6),  # close hold above
            (101.7, 103.5, 101.6, 103.0),  # entry bar
            (103.0, 104.0, 102.9, 103.8),
        ])
        rth = or_bars + rest
        orng = build_opening_range(or_bars, td, 5)
        return td, rth, orng

    def test_breakout_does_not_enter(self):
        td, rth, orng = self._or5_breakout_setup()
        self.assertTrue(orng.complete)
        ev = scan_retest(rth, orng, trigger="T0_exact", fail_frac=0.10, confirm="B_close_1m", tick=0.25)
        self.assertEqual(ev["status"], "RETEST_CONFIRMED")
        # Phase 38 would enter on the bar after the 09:35 close, not wait
        p38 = simulate(instrument="NQ", rth=rth, orng=orng, family="close_1m", stop_mode="A_opposite", target_r=1.0, adverse_ticks=1.0)
        self.assertEqual(p38.status, "ENTERED")
        self.assertNotEqual(p38.entry_ts, None)
        trade = simulate_retest(
            instrument="NQ", rth=rth, orng=orng, trigger="T0_exact", fail_frac=0.10,
            confirm="B_close_1m", stop_mode="A_retest_extreme", target_r=1.0, adverse_ticks=1.0,
        )
        self.assertEqual(trade.status, "ENTERED")
        self.assertGreater(trade.entry_ts, p38.entry_ts)
        self.assertNotAlmostEqual(trade.entry_fill, orng.high)

    def test_deep_retest_fails(self):
        td = "2026-01-05"
        or_bars = _bars(td, "09:30", [(100, 101, 99, 100)] * 5)
        rest = _bars(td, "09:35", [
            (100.0, 101.4, 100.0, 101.2),
            (101.3, 101.5, 99.5, 100.8),  # penetration 1.5 > 10% of width 2.0
            (100.9, 101.8, 100.8, 101.5),
        ])
        rth = or_bars + rest
        orng = build_opening_range(or_bars, td, 5)
        ev = scan_retest(rth, orng, trigger="T0_exact", fail_frac=0.10, confirm="B_close_1m", tick=0.25)
        self.assertEqual(ev["status"], "RETEST_FAILED")

    def test_opposite_invalidates(self):
        td = "2026-01-05"
        or_bars = _bars(td, "09:30", [(100, 101, 99, 100)] * 5)
        rest = _bars(td, "09:35", [
            (100.0, 101.4, 100.0, 101.2),
            (101.1, 101.2, 98.5, 99.0),  # through OR_LOW
        ])
        rth = or_bars + rest
        orng = build_opening_range(or_bars, td, 5)
        ev = scan_retest(rth, orng, trigger="T0_exact", fail_frac=0.10, confirm="B_close_1m", tick=0.25)
        self.assertEqual(ev["status"], "BREAKOUT_INVALIDATED")

    def test_same_bar_stop_target_ambiguous(self):
        td = "2026-01-05"
        or_bars = _bars(td, "09:30", [(100, 101, 99, 100)] * 5)
        rest = _bars(td, "09:35", [
            (100.0, 101.4, 100.0, 101.2),
            (101.2, 101.3, 100.95, 101.0),
            (101.05, 101.4, 101.0, 101.3),
            (101.4, 120.0, 80.0, 100.0),
        ])
        rth = or_bars + rest
        orng = build_opening_range(or_bars, td, 5)
        trade = simulate_retest(
            instrument="NQ", rth=rth, orng=orng, trigger="T0_exact", fail_frac=0.25,
            confirm="B_close_1m", stop_mode="A_retest_extreme", target_r=1.0, adverse_ticks=1.0,
        )
        self.assertEqual(trade.outcome, "AMBIGUOUS")


if __name__ == "__main__":
    unittest.main()
