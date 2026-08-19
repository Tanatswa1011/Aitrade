"""Phase 40 frozen-isolation and leak-safety tests."""
from __future__ import annotations

import json
import unittest
from datetime import date, timedelta
from pathlib import Path

from models import Bar
from nq_pdh_pdl import local_ts
from phase34_validate import GC_FILE_SHA, NQ_FILE_SHA, file_sha256
from tsmom_engine import (
    bars_to_days,
    mark_rolls,
    signal_at,
    simulate_fixed_hold,
    simulate_same_session,
)

ROOT = Path(__file__).resolve().parent


def _weekday_seq(start: str, n: int, o=100.0, drift=0.5, gap=0.4) -> list[Bar]:
    d = date.fromisoformat(start)
    bars = []
    px = o
    while len(bars) < n:
        if d.weekday() < 5:
            o_ = px + gap
            c = o_ + drift
            h = max(o_, c) + 0.25
            l = min(o_, c) - 0.25
            bars.append(Bar(time=local_ts(d.isoformat(), "10:00"), open=o_, high=h, low=l, close=c, volume=1000))
            px = c
        d += timedelta(days=1)
    return bars


class FrozenIsolationTests(unittest.TestCase):
    def test_frozen_hashes_unchanged(self):
        gc = json.loads((ROOT / "strategy_frozen" / "gc_vwap_v2_phase26.json").read_text(encoding="utf-8"))
        nq = json.loads((ROOT / "strategy_frozen" / "nq_dvp_phase30.json").read_text(encoding="utf-8"))
        self.assertEqual(gc["frozen_config_hash"], "0695d7a881b6247033426bceb077b6895b96405b826c21f32a109a02f44b2d43")
        self.assertEqual(nq["frozen_config_hash"], "935e3a616351b09dbfa8d2d0e2b5d6850be803fb385ec5e5f8e3593856a1212a")
        self.assertEqual(file_sha256(ROOT / "strategy_frozen" / "gc_vwap_v2_phase26.json"), GC_FILE_SHA)
        self.assertEqual(file_sha256(ROOT / "strategy_frozen" / "nq_dvp_phase30.json"), NQ_FILE_SHA)

    def test_spec_primary_locked(self):
        spec = json.loads((ROOT / "phase40_spec.json").read_text(encoding="utf-8"))
        self.assertEqual(len(spec["methodology_corrections"]), 3)
        self.assertTrue(all(isinstance(x, str) for x in spec["methodology_corrections"]))
        self.assertEqual(spec["primary_candidate"]["id"], "TSMOM_20D_5D")
        self.assertEqual(spec["primary_candidate"]["lookback"], 20)
        self.assertEqual(spec["primary_candidate"]["hold"], 5)
        self.assertEqual(spec["chrono"]["train_end"], "2022-12-30")
        self.assertEqual(spec["chrono"]["holdout_start"], "2023-01-03")
        self.assertIn("No ORB", spec["forbidden"])


class LeakSafetyTests(unittest.TestCase):
    def test_signal_uses_completed_close_only(self):
        bars = _weekday_seq("2026-01-05", 25, drift=1.0)
        days = bars_to_days(bars)
        mark_rolls(days, 0.25)
        i = 20
        sig = signal_at(days, i, 20)
        self.assertIsNotNone(sig)
        leaked = signal_at(days, i + 1, 20)
        self.assertNotAlmostEqual(sig, leaked)
        # 20 completed returns ending at i: close[i] / close[i-20] - 1
        self.assertAlmostEqual(days[i].close / days[i - 19].prev_close - 1.0, sig, places=8)

    def test_entry_is_next_open_not_signal_close(self):
        bars = _weekday_seq("2026-01-05", 30, drift=1.0)
        days = bars_to_days(bars)
        mark_rolls(days, 0.25)
        trades = simulate_fixed_hold(instrument="ES", days=days, lookback=5, hold=3, adverse_ticks=1.0)
        self.assertTrue(trades)
        t = trades[0]
        sig_i = next(i for i, d in enumerate(days) if d.date == t.signal_date)
        self.assertEqual(t.entry_date, days[sig_i + 1].date)
        self.assertAlmostEqual(t.entry_theo, days[sig_i + 1].open)
        self.assertNotAlmostEqual(t.entry_theo, days[sig_i].close)
        self.assertGreater(t.entry_fill, t.entry_theo)

    def test_hold5_exits_fifth_session_close(self):
        bars = _weekday_seq("2026-01-05", 40, drift=0.4)
        days = bars_to_days(bars)
        mark_rolls(days, 0.25)
        trades = simulate_fixed_hold(instrument="ES", days=days, lookback=5, hold=5, adverse_ticks=0.0)
        t = trades[0]
        sig_i = next(i for i, d in enumerate(days) if d.date == t.signal_date)
        self.assertEqual(t.exit_date, days[sig_i + 5].date)
        self.assertAlmostEqual(t.exit_theo, days[sig_i + 5].close)
        self.assertEqual(t.hold_days, 5)

    def test_roll_gap_neutralized_in_signal(self):
        bars = _weekday_seq("2026-01-05", 80, drift=0.1)
        # inject a contract-switch gap on bar 50
        b = bars[50]
        gap = 80.0
        bars[50] = Bar(time=b.time, open=b.open + gap, high=b.high + gap, low=b.low + gap, close=b.close + gap, volume=b.volume)
        for i in range(51, len(bars)):
            x = bars[i]
            bars[i] = Bar(time=x.time, open=x.open + gap, high=x.high + gap, low=x.low + gap, close=x.close + gap, volume=x.volume)
        days = bars_to_days(bars)
        mark_rolls(days, 0.25)
        self.assertTrue(days[50].is_roll)
        self.assertAlmostEqual(days[50].cc_clean, days[50].session_ret)
        self.assertNotAlmostEqual(days[50].cc_clean, days[50].cc_raw)
        raw = days[50].close / days[49].close - 1.0
        self.assertGreater(abs(raw), abs(days[50].cc_clean or 0) + 0.01)

    def test_globex_2000_timestamp_maps_to_next_weekday(self):
        # Databento 1d: Sunday 20:00 ET -> Monday session date
        ts = local_ts("2010-06-06", "20:00")
        from tsmom_engine import ny_date
        self.assertEqual(ny_date(ts), "2010-06-07")
        ts_thu = local_ts("2010-06-10", "20:00")
        self.assertEqual(ny_date(ts_thu), "2010-06-11")
        bars = [
            Bar(time=local_ts("2026-01-09", "10:00"), open=100, high=101, low=99, close=100.5, volume=1),  # Fri
            Bar(time=local_ts("2026-01-12", "10:00"), open=101, high=102, low=100, close=101.2, volume=1),  # Mon
        ]
        days = bars_to_days(bars)
        self.assertTrue(days[1].is_weekend_gap)

    def test_same_session_has_zero_overnight(self):
        bars = _weekday_seq("2026-01-05", 20, drift=0.3)
        days = bars_to_days(bars)
        mark_rolls(days, 0.25)
        trades = simulate_same_session(instrument="ES", days=days, lookback=5, adverse_ticks=1.0)
        self.assertTrue(trades)
        self.assertTrue(all(t.overnight_points == 0 for t in trades))
        self.assertTrue(all(t.entry_date == t.exit_date for t in trades))

    def test_nonoverlapping_next_signal_at_exit_close(self):
        bars = _weekday_seq("2026-01-05", 30, drift=0.5)
        days = bars_to_days(bars)
        mark_rolls(days, 0.25)
        trades = simulate_fixed_hold(instrument="ES", days=days, lookback=5, hold=3, adverse_ticks=0.0)
        self.assertGreaterEqual(len(trades), 2)
        first_exit = trades[0].exit_date
        self.assertEqual(trades[1].signal_date, first_exit)


if __name__ == "__main__":
    unittest.main()
