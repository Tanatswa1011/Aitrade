"""Phase 42 frozen-isolation and leak-safety tests."""
from __future__ import annotations

import json
import unittest
from pathlib import Path

from models import Bar
from nq_pdh_pdl import local_ts
from phase34_validate import GC_FILE_SHA, NQ_FILE_SHA, file_sha256
from htf_pullback_engine import (
    CONFIRM_A,
    HtfBar,
    find_first_pullback_setup,
    htf_return_at,
    last_completed_index,
    simulate_setup,
    trend_side,
)

ROOT = Path(__file__).resolve().parent
TD = "2024-03-12"


def _hb(hhmm: str, close: float, period: int = 3600) -> HtfBar:
    t = local_ts(TD, hhmm)
    return HtfBar(time=t, close_ts=t + period, open=close - 1, high=close + 1, low=close - 1, close=close)


def _m1(hhmm: str, o: float, h: float, l: float, c: float, n: int = 1) -> list[Bar]:
    t0 = local_ts(TD, hhmm)
    return [Bar(time=t0 + i * 60, open=o, high=h, low=l, close=c, volume=10) for i in range(n)]


class FrozenIsolationTests(unittest.TestCase):
    def test_frozen_hashes_unchanged(self):
        gc = json.loads((ROOT / "strategy_frozen" / "gc_vwap_v2_phase26.json").read_text(encoding="utf-8"))
        nq = json.loads((ROOT / "strategy_frozen" / "nq_dvp_phase30.json").read_text(encoding="utf-8"))
        self.assertEqual(gc["frozen_config_hash"], "0695d7a881b6247033426bceb077b6895b96405b826c21f32a109a02f44b2d43")
        self.assertEqual(nq["frozen_config_hash"], "935e3a616351b09dbfa8d2d0e2b5d6850be803fb385ec5e5f8e3593856a1212a")
        self.assertEqual(file_sha256(ROOT / "strategy_frozen" / "gc_vwap_v2_phase26.json"), GC_FILE_SHA)
        self.assertEqual(file_sha256(ROOT / "strategy_frozen" / "nq_dvp_phase30.json"), NQ_FILE_SHA)

    def test_spec_primary_locked(self):
        spec = json.loads((ROOT / "phase42_spec.json").read_text(encoding="utf-8"))
        self.assertEqual(spec["methodology_corrections"], [])
        self.assertEqual(spec["primary_candidate"]["id"], "HTF_1H_TREND_FIRST_PULLBACK_5M_CONFIRM")
        self.assertEqual(spec["htf"]["1h"]["threshold"], 0.002)
        self.assertIn("No VWAP as primary", spec["forbidden"])
        self.assertEqual(spec["status"], "DEFINITIONS_FROZEN_BEFORE_ENTRIES")


class LeakAndEntryTests(unittest.TestCase):
    def test_unfinished_1h_bar_excluded(self):
        series = [
            _hb("04:00", 100),
            _hb("05:00", 100),
            _hb("06:00", 100),
            _hb("07:00", 100),
            _hb("08:00", 100),
            _hb("09:00", 200),
        ]
        as_of = local_ts(TD, "09:30")
        i = last_completed_index(series, as_of)
        self.assertEqual(series[i].time, local_ts(TD, "08:00"))
        ret = htf_return_at(series, as_of, 4)
        self.assertAlmostEqual(ret, 0.0)
        after = htf_return_at(series, local_ts(TD, "10:00"), 4)
        self.assertAlmostEqual(after, 1.0)
        self.assertEqual(trend_side(after), "BULLISH")

    def test_entry_is_next_5m_open_not_confirm_close(self):
        # Overnight 1h return large enough to be bullish at RTH.
        h1 = [
            _hb("04:00", 100.00),
            _hb("05:00", 100.10),
            _hb("06:00", 100.20),
            _hb("07:00", 100.30),
            _hb("08:00", 100.50),
        ]
        h4 = [_hb("18:00", 100.00, 14400)]
        # 5m RTH: impulse 100 -> 110, pullback to 105, then green confirm, then entry open 105.25
        rth5 = []
        t0 = local_ts(TD, "09:30")
        # 09:30-09:45 impulse up
        rth5.append(HtfBar(time=t0, close_ts=t0 + 300, open=100, high=106, low=100, close=106))
        rth5.append(HtfBar(time=t0 + 300, close_ts=t0 + 600, open=106, high=110, low=105.5, close=110))
        # 09:40 pullback tags 50%
        rth5.append(HtfBar(time=t0 + 600, close_ts=t0 + 900, open=110, high=110, low=105, close=105.2))
        # 09:45 red continuation of pullback (not confirm)
        rth5.append(HtfBar(time=t0 + 900, close_ts=t0 + 1200, open=105.2, high=105.4, low=104.8, close=105.0))
        # 09:50 green confirm
        rth5.append(HtfBar(time=t0 + 1200, close_ts=t0 + 1500, open=105.0, high=106.0, low=104.9, close=105.8))
        # 09:55 entry bar
        rth5.append(HtfBar(time=t0 + 1500, close_ts=t0 + 1800, open=105.25, high=107, low=105, close=106.5))
        for i in range(6, 78):
            ts = t0 + i * 300
            rth5.append(HtfBar(time=ts, close_ts=ts + 300, open=106.5, high=107, low=106, close=106.5))
        rth_1m = []
        for b in rth5:
            rth_1m.extend(
                [
                    Bar(time=b.time + k, open=b.open, high=b.high, low=b.low, close=b.close, volume=5)
                    for k in range(0, 300, 60)
                ]
            )
        setup = find_first_pullback_setup(
            instrument="ES",
            td=TD,
            rth_1m=rth_1m,
            bars5=rth5,
            h1=h1,
            h4=h4,
            horizon="1h",
            confirm_kind=CONFIRM_A,
        )
        self.assertIsNotNone(setup)
        self.assertEqual(setup.direction, "LONG")
        self.assertEqual(setup.entry_ts, t0 + 1500)
        self.assertAlmostEqual(setup.entry_theo, 105.25)
        self.assertNotAlmostEqual(setup.entry_theo, 105.8)
        self.assertLess(setup.pullback_extreme, 105.25)

    def test_neutral_htf_no_trade(self):
        h1 = [_hb(f"{h:02d}:00", 100.0) for h in range(4, 9)]
        t0 = local_ts(TD, "09:30")
        rth5 = [
            HtfBar(time=t0 + i * 300, close_ts=t0 + (i + 1) * 300, open=100, high=110, low=100, close=100)
            for i in range(20)
        ]
        rth_1m = [Bar(time=t0 + i * 60, open=100, high=101, low=99, close=100, volume=1) for i in range(390)]
        setup = find_first_pullback_setup(
            instrument="ES",
            td=TD,
            rth_1m=rth_1m,
            bars5=rth5,
            h1=h1,
            h4=h1,
            horizon="1h",
            confirm_kind=CONFIRM_A,
        )
        self.assertIsNone(setup)

    def test_same_bar_stop_target_ambiguous(self):
        h1 = [
            _hb("04:00", 100.00),
            _hb("05:00", 100.10),
            _hb("06:00", 100.20),
            _hb("07:00", 100.30),
            _hb("08:00", 100.50),
        ]
        h4 = [_hb("18:00", 100.00, 14400)]
        t0 = local_ts(TD, "09:30")
        rth5 = [
            HtfBar(time=t0, close_ts=t0 + 300, open=100, high=110, low=100, close=110),
            HtfBar(time=t0 + 300, close_ts=t0 + 600, open=110, high=110, low=105, close=105),
            HtfBar(time=t0 + 600, close_ts=t0 + 900, open=105, high=106, low=104.9, close=105.8),
            HtfBar(time=t0 + 900, close_ts=t0 + 1200, open=105.25, high=200, low=1, close=105.25),
        ]
        for i in range(4, 78):
            ts = t0 + i * 300
            rth5.append(HtfBar(time=ts, close_ts=ts + 300, open=105.25, high=106, low=105, close=105.5))
        rth_1m = []
        for b in rth5:
            if b.time == t0 + 900:
                rth_1m.append(Bar(time=b.time, open=105.25, high=200, low=1, close=105.25, volume=1))
                for k in range(1, 5):
                    rth_1m.append(Bar(time=b.time + k * 60, open=105.25, high=106, low=105, close=105.5, volume=1))
            else:
                for k in range(5):
                    rth_1m.append(
                        Bar(time=b.time + k * 60, open=b.open, high=b.high, low=b.low, close=b.close, volume=1)
                    )
        setup = find_first_pullback_setup(
            instrument="ES",
            td=TD,
            rth_1m=rth_1m,
            bars5=rth5,
            h1=h1,
            h4=h4,
            horizon="1h",
            confirm_kind=CONFIRM_A,
        )
        self.assertIsNotNone(setup)
        trade = simulate_setup(setup, rth_1m, target_r=1.0, adverse_ticks=0.0, stop_buffer_ticks=1.0)
        if trade.status == "ENTERED":
            self.assertEqual(trade.outcome, "AMBIGUOUS")
            self.assertIsNone(trade.points)


if __name__ == "__main__":
    unittest.main()
