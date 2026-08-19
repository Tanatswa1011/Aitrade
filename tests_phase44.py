"""Phase 44 frozen-isolation and leak-safety tests."""
from __future__ import annotations

import json
import unittest
from datetime import date, timedelta
from pathlib import Path

from models import Bar
from nq_pdh_pdl import local_ts
from orb_index_engine import resolve_path, flatten_ts
from phase34_validate import GC_FILE_SHA, NQ_FILE_SHA, file_sha256
from long_only_engine import (
    DayState,
    build_states,
    find_first_red_green,
    last_completed_state,
    simulate_open_long,
    simulate_red_green,
)
from tsmom_engine import bars_to_days, mark_rolls, signal_at

ROOT = Path(__file__).resolve().parent
TD = "2024-03-12"


def _st(**kwargs) -> DayState:
    base = dict(
        date="2024-03-11",
        bull_10=True,
        bull_20=True,
        bull_60=True,
        bull_ema=True,
        bull_20_and_5=True,
        ret_5=0.01,
        ret_10=0.02,
        ret_20=0.03,
        ret_60=0.05,
        prior_1d=0.001,
        prior_2d=0.002,
        prior_3d=0.003,
        pct_below_20h=0.01,
        close_loc=0.8,
        rv20=0.01,
        dip_bucket="near_high",
    )
    base.update(kwargs)
    return DayState(**base)


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


def _fill_5m(td: str, hhmm: str, o: float, h: float, l: float, c: float) -> list[Bar]:
    t0 = local_ts(td, hhmm)
    out = []
    for k in range(5):
        px = o if k == 0 else (c if k == 4 else (o + c) / 2)
        out.append(Bar(time=t0 + k * 60, open=o if k == 0 else px, high=h, low=l, close=c if k == 4 else px, volume=5))
    return out


def _rth_until(td: str, last_hhmm: str, px: float = 100.0) -> list[Bar]:
    t0 = local_ts(td, "09:30")
    end = local_ts(td, last_hhmm)
    bars = []
    t = t0
    while t < end:
        bars.append(Bar(time=t, open=px, high=px + 0.5, low=px - 0.5, close=px, volume=1))
        t += 60
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
        spec = json.loads((ROOT / "phase44_spec.json").read_text(encoding="utf-8"))
        self.assertEqual(spec["methodology_corrections"], [])
        self.assertEqual(spec["primary_state"]["id"], "LONG_STATE_20D_POSITIVE")
        self.assertEqual(spec["primary_intraday_candidate"]["id"], "LONG20_FIRST_RED_GREEN_5M")
        self.assertEqual(spec["primary_intraday_baseline"]["id"], "BULL_STATE_RTH_OPEN_LONG")
        self.assertEqual(spec["chrono"]["train_end"], "2022-12-30")
        self.assertEqual(spec["chrono"]["holdout_start"], "2023-01-03")
        self.assertIn("No shorts", spec["forbidden"])
        self.assertIn("no VWAP", spec["forbidden"])
        self.assertEqual(spec["status"], "DEFINITIONS_FROZEN_BEFORE_ENTRIES")
        self.assertEqual(spec["thesis"], "BULLISH STATE -> LONG; NON-BULLISH STATE -> FLAT. Never BEARISH -> SHORT.")


class LeakAndEntryTests(unittest.TestCase):
    def test_same_day_daily_bar_excluded_at_rth(self):
        states = [
            _st(date="2024-03-08", bull_20=True, ret_20=0.04),
            _st(date="2024-03-11", bull_20=True, ret_20=0.05),
            _st(date="2024-03-12", bull_20=False, ret_20=-0.01),
        ]
        st = last_completed_state(states, "2024-03-12")
        self.assertIsNotNone(st)
        self.assertEqual(st.date, "2024-03-11")
        self.assertTrue(st.bull_20)
        self.assertIsNone(last_completed_state(states, "2024-03-08"))

    def test_20d_signal_uses_completed_close_only(self):
        bars = _weekday_seq("2026-01-05", 25, drift=1.0)
        days = bars_to_days(bars)
        mark_rolls(days, 0.25)
        states = build_states(days)
        i = 20
        self.assertAlmostEqual(states[i].ret_20, signal_at(days, i, 20))
        self.assertNotAlmostEqual(states[i].ret_20, signal_at(days, i + 1, 20))
        monday = days[i + 1].date
        used = last_completed_state(states, monday)
        self.assertEqual(used.date, days[i].date)

    def test_entry_is_next_5m_open_not_green_close(self):
        rth = []
        rth += _fill_5m(TD, "09:30", 100.0, 100.2, 99.0, 99.2)  # red
        rth += _fill_5m(TD, "09:35", 99.2, 100.4, 99.1, 100.3)  # green confirm
        rth += _fill_5m(TD, "09:40", 100.25, 101.0, 100.1, 100.8)  # entry
        setup = find_first_red_green(rth, TD)
        self.assertIsNotNone(setup)
        self.assertEqual(setup["entry_ts"], local_ts(TD, "09:40"))
        self.assertAlmostEqual(setup["entry_theo"], 100.25)
        self.assertNotAlmostEqual(setup["entry_theo"], 100.3)
        self.assertLess(setup["pullback_low"], 100.25)
        trade = simulate_red_green(instrument="ES", td=TD, rth=rth, setup=setup)
        self.assertEqual(trade.direction, "LONG")
        self.assertNotEqual(trade.direction, "SHORT")
        self.assertAlmostEqual(trade.entry_fill, 100.25 + 0.25)

    def test_never_shorts(self):
        rth = _rth_until(TD, "16:00", 100.0)
        t = simulate_open_long(instrument="ES", td=TD, rth=rth)
        self.assertEqual(t.direction, "LONG")
        self.assertEqual(t.status, "ENTERED")

    def test_same_bar_stop_target_ambiguous(self):
        rth = []
        rth += _fill_5m(TD, "09:30", 100.0, 100.2, 99.0, 99.2)
        rth += _fill_5m(TD, "09:35", 99.2, 100.4, 99.1, 100.3)
        rth += _fill_5m(TD, "09:40", 100.00, 104.0, 96.0, 100.0)
        setup = find_first_red_green(rth, TD)
        self.assertIsNotNone(setup)
        trade = simulate_red_green(instrument="ES", td=TD, rth=rth, setup=setup, target_r=1.0)
        self.assertEqual(trade.outcome, "AMBIGUOUS")
        self.assertIsNone(trade.points)

    def test_ambiguous_helper_matches_engine(self):
        flatten = flatten_ts(TD)
        t0 = local_ts(TD, "09:40")
        path = [Bar(time=t0, open=100.25, high=102.0, low=98.5, close=100.0, volume=1)]
        outcome, _, px, _, _ = resolve_path(path, is_long=True, sl=98.75, tp=101.75, flatten=flatten)
        self.assertEqual(outcome, "AMBIGUOUS")
        self.assertIsNone(px)


if __name__ == "__main__":
    unittest.main()
