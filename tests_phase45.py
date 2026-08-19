"""Phase 45 frozen-isolation and leak-safety tests."""
from __future__ import annotations

import json
import unittest
from pathlib import Path

from models import Bar
from nq_pdh_pdl import local_ts
from phase34_validate import GC_FILE_SHA, NQ_FILE_SHA, file_sha256
from phase45_validate import decide_one
from tg_london_engine import (
    TfBar,
    detect_fvgs,
    find_reaction_index,
    in_window,
    is_doji,
    is_trident,
    london_date,
    london_hhmm,
    simulate_setup,
)

ROOT = Path(__file__).resolve().parent
TD = "2024-03-12"  # after US DST; London still GMT+0 until last Sunday March... 2024-03-12 is before UK DST (Mar 31). US DST started Mar 10.


def _tb(ts: int, o, h, l, c) -> TfBar:
    return TfBar(time=ts, close_ts=ts + 1800, open=o, high=h, low=l, close=c, volume=1)


class FrozenIsolationTests(unittest.TestCase):
    def test_frozen_hashes_unchanged(self):
        gc = json.loads((ROOT / "strategy_frozen" / "gc_vwap_v2_phase26.json").read_text(encoding="utf-8"))
        nq = json.loads((ROOT / "strategy_frozen" / "nq_dvp_phase30.json").read_text(encoding="utf-8"))
        self.assertEqual(gc["frozen_config_hash"], "0695d7a881b6247033426bceb077b6895b96405b826c21f32a109a02f44b2d43")
        self.assertEqual(nq["frozen_config_hash"], "935e3a616351b09dbfa8d2d0e2b5d6850be803fb385ec5e5f8e3593856a1212a")
        self.assertEqual(file_sha256(ROOT / "strategy_frozen" / "gc_vwap_v2_phase26.json"), GC_FILE_SHA)
        self.assertEqual(file_sha256(ROOT / "strategy_frozen" / "nq_dvp_phase30.json"), NQ_FILE_SHA)

    def test_spec_primary_locked(self):
        spec = json.loads((ROOT / "phase45_spec.json").read_text(encoding="utf-8"))
        self.assertEqual(spec["methodology_corrections"], [])
        self.assertEqual(spec["primary_candidate"]["id"], "TG_GC_30M_LONDON_FVG50_REACTION")
        self.assertEqual(spec["primary_candidate"]["reaction"], "B_trident")
        self.assertEqual(spec["london"]["timezone"], "Europe/London")
        self.assertEqual(spec["london"]["primary_window"], ["07:00", "11:00"])
        self.assertEqual(spec["source_fidelity"]["trident_wick_ge_body_close_half"], "TRIDENT_MECHANIZED_APPROXIMATION")
        self.assertEqual(spec["chrono"]["train_end"], "2022-12-30")
        self.assertIn("No RSI", spec["forbidden"])


class LondonAndPatternTests(unittest.TestCase):
    def test_london_window_dst_aware_not_fixed_utc(self):
        # 2024-01-15 07:00 London = 07:00 GMT = 07:00 UTC
        winter = int(local_ts("2024-01-15", "02:00"))  # 07:00 London in winter (EST=UTC-5, London GMT)
        # 2024-07-15 07:00 London = 06:00 UTC = 02:00 EDT
        summer = int(local_ts("2024-07-15", "02:00"))
        self.assertEqual(london_hhmm(winter), "07:00")
        self.assertEqual(london_hhmm(summer), "07:00")
        self.assertTrue(in_window(winter))
        self.assertTrue(in_window(summer))
        self.assertFalse(in_window(int(local_ts("2024-07-15", "06:00"))))  # 11:00 London, end exclusive

    def test_fvg_uses_completed_three_candles(self):
        t0 = 1_000_000_000
        bars = [
            _tb(t0, 100, 101, 99, 100.5),
            _tb(t0 + 1800, 100.5, 103, 100.4, 102.8),
            _tb(t0 + 3600, 102.8, 104, 103.2, 103.5),  # bullish FVG: c1 high 101 < c3 low 103.2
        ]
        n = len(bars)
        none = [None] * n
        evs = detect_fvgs(
            instrument="GC",
            bars30=bars,
            h4=[],
            ema20=none,
            ema50=none,
            ema200=none,
            atr=none,
            ema200_4h=[],
        )
        self.assertEqual(len(evs), 1)
        self.assertEqual(evs[0].direction, "BULLISH")
        self.assertAlmostEqual(evs[0].zone_low, 101.0)
        self.assertAlmostEqual(evs[0].zone_high, 103.2)
        self.assertAlmostEqual(evs[0].mid, 102.1)

    def test_doji_and_trident_definitions(self):
        t0 = 1_000_000_000
        doji = _tb(t0, 100, 102, 98, 100.2)  # body 0.2 / range 4 = 0.05
        trident = _tb(t0, 100, 101, 96, 100.8)  # lower wick 4, body 0.8, close loc (100.8-96)/5=0.96
        self.assertTrue(is_doji(doji))
        self.assertTrue(is_trident(trident, "BULLISH"))
        self.assertFalse(is_trident(trident, "BEARISH"))

    def test_entry_is_next_30m_open_not_reaction_close(self):
        # Build a tiny aligned path: FVG then mid-touch trident then entry open 200
        t0 = int(local_ts("2024-07-15", "02:00"))  # 07:00 London
        bars30 = [
            _tb(t0, 100, 101, 99, 100.5),
            _tb(t0 + 1800, 100.5, 110, 100.4, 109),
            _tb(t0 + 3600, 109, 111, 108.5, 110),  # FVG bullish 101 -> 108.5, mid 104.75
            _tb(t0 + 5400, 110, 110.5, 104.0, 109.2),  # trident-ish at mid
            _tb(t0 + 7200, 200.0, 201, 199, 200.5),  # entry
        ]
        none = [None] * 5
        evs = detect_fvgs(instrument="GC", bars30=bars30, h4=[], ema20=none, ema50=none, ema200=none, atr=none, ema200_4h=[], window=("07:00", "11:00"))
        self.assertTrue(evs)
        ev = evs[0]
        ri = find_reaction_index(bars30, ev, kind="trident")
        self.assertIsNotNone(ri)
        rth = []
        for b in bars30:
            for k in range(30):
                rth.append(Bar(time=b.time + k * 60, open=b.open, high=b.high, low=b.low, close=b.close, volume=1))
        trade = simulate_setup(
            instrument="GC", bars_1m=rth, bars30=bars30, ev=ev, reaction_i=ri,
            stop_family="fvg_boundary", target_r=2.0, adverse_ticks=1.0,
            candidate="TG_GC_30M_LONDON_FVG50_REACTION", reaction="trident",
        )
        self.assertEqual(trade.direction, "BULLISH")
        self.assertAlmostEqual(trade.entry_theo, 200.0)
        self.assertNotAlmostEqual(trade.entry_theo, bars30[ri].close)

    def test_same_bar_stop_target_ambiguous(self):
        t0 = int(local_ts("2024-07-15", "02:00"))
        bars30 = [
            _tb(t0, 100, 101, 99, 100.5),
            _tb(t0 + 1800, 100.5, 110, 100.4, 109),
            _tb(t0 + 3600, 109, 111, 108.5, 110),
            _tb(t0 + 5400, 110, 110.5, 104.0, 109.2),
            _tb(t0 + 7200, 108.0, 200.0, 50.0, 108.0),
        ]
        none = [None] * 5
        evs = detect_fvgs(instrument="GC", bars30=bars30, h4=[], ema20=none, ema50=none, ema200=none, atr=none, ema200_4h=[], window=("07:00", "11:00"))
        ev = evs[0]
        ri = find_reaction_index(bars30, ev, kind="trident")
        path = [Bar(time=bars30[4].time, open=108.0, high=200.0, low=50.0, close=108.0, volume=1)]
        trade = simulate_setup(
            instrument="GC", bars_1m=path, bars30=bars30, ev=ev, reaction_i=ri,
            stop_family="fvg_boundary", target_r=2.0, adverse_ticks=1.0,
            candidate="X", reaction="trident",
        )
        self.assertEqual(trade.outcome, "AMBIGUOUS")
        self.assertIsNone(trade.points)


class GateTests(unittest.TestCase):
    def test_fill_heavy_resume_is_not_structural_continuation(self):
        years = [{"n_resolved": 15, "expectancy_r": 0.2} for _ in range(4)]
        years.append({"n_resolved": 13, "expectancy_r": -0.8})
        gc = decide_one(
            coverage_ok=True,
            n_full=101,
            n_hold=53,
            full={"expectancy_r": 0.101, "profit_factor": 1.37, "p_reach_2r": 0.38},
            hold={"expectancy_r": -0.058},
            years=years,
            thresh_stable=True,
            distinct_from_v2=True,
            p_resume=0.92,
            p_fill=0.81,
            stress_e=-0.039,
        )
        self.assertEqual(gc, "TG_LONDON_EDGE_WEAK")
        nq = decide_one(
            coverage_ok=True,
            n_full=106,
            n_hold=56,
            full={"expectancy_r": -0.179, "profit_factor": 0.85, "p_reach_2r": 0.30},
            hold={"expectancy_r": -0.338},
            years=[{"n_resolved": 15, "expectancy_r": -0.2} for _ in range(6)],
            thresh_stable=True,
            distinct_from_v2=True,
            p_resume=0.91,
            p_fill=0.90,
            stress_e=-0.203,
        )
        self.assertEqual(nq, "TG_LONDON_EDGE_REJECTED")

    def test_resume_without_fill_can_be_structural_only(self):
        status = decide_one(
            coverage_ok=True,
            n_full=80,
            n_hold=40,
            full={"expectancy_r": -0.10, "profit_factor": 0.9, "p_reach_2r": 0.20},
            hold={"expectancy_r": -0.12},
            years=[{"n_resolved": 12, "expectancy_r": -0.1} for _ in range(5)],
            thresh_stable=True,
            distinct_from_v2=True,
            p_resume=0.70,
            p_fill=0.35,
            stress_e=-0.15,
        )
        self.assertEqual(status, "TG_FVG_STRUCTURAL_EFFECT_ONLY")


if __name__ == "__main__":
    unittest.main()
