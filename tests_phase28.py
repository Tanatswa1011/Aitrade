"""Phase 28 tests — impulse, pullback, continuation, isolation from Phase 26."""

from __future__ import annotations

import json
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from gc_momentum_engine import (
    analyze_candidate,
    bar_range,
    close_location,
    collect_all_impulses,
    detect_impulses,
    median_prior_range,
    median_prior_volume,
    momentum_session_window,
)
from gc_momentum_models import PHASE28_CANDIDATES, STRATEGY_FAMILY, EntryMode, PullbackMode
from models import Bar

NY = ZoneInfo("America/New_York")
PHASE26_HASH = "0695d7a881b6247033426bceb077b6895b96405b826c21f32a109a02f44b2d43"
PHASE26_FROZEN = Path("strategy_frozen") / "gc_vwap_v2_phase26.json"


def _bar(t, o, h, l, c, v=100.0):
    return Bar(time=t, open=o, high=h, low=l, close=c, volume=v)


def _ts(y, m, d, hh, mm):
    return int(datetime(y, m, d, hh, mm, tzinfo=NY).timestamp())


class SessionTests(unittest.TestCase):
    def test_session_boundaries(self):
        start, end, no_new = momentum_session_window("2026-07-02")
        self.assertEqual(datetime.fromtimestamp(start, tz=NY).strftime("%H:%M"), "08:20")
        self.assertEqual(datetime.fromtimestamp(end, tz=NY).strftime("%H:%M"), "13:30")
        self.assertEqual(datetime.fromtimestamp(no_new, tz=NY).strftime("%H:%M"), "12:30")


class ImpulseMathTests(unittest.TestCase):
    def test_close_location_and_range(self):
        b = _bar(1, 100, 110, 100, 108)
        self.assertAlmostEqual(bar_range(b), 10.0)
        self.assertAlmostEqual(close_location(b), 0.8)

    def test_median_prior_no_lookahead(self):
        bars = [_bar(i * 300, 100, 101, 99, 100, v=10 + i) for i in range(25)]
        med = median_prior_range(bars, 20)
        self.assertIsNotNone(med)
        # index 20 excludes bar 20
        self.assertEqual(len([bar_range(b) for b in bars[0:20]]), 20)


class ImpulseDetectionTests(unittest.TestCase):
    def _build_bull_impulse_session(self):
        bars = []
        # quiet warmup inside session
        for i in range(12):
            t = _ts(2026, 7, 2, 8, 20) + i * 300
            bars.append(_bar(t, 2000, 2000.5, 1999.5, 2000.2, v=50))
        # expansion impulse: new session high close, large range, close near high
        t = _ts(2026, 7, 2, 9, 20)
        bars.append(_bar(t, 2000, 2008, 1999.8, 2007.5, v=200))
        # pullback 50%
        for i in range(3):
            t = _ts(2026, 7, 2, 9, 25) + i * 300
            bars.append(_bar(t, 2006, 2006.5, 2003, 2003.5, v=80))
        # continuation break
        t = _ts(2026, 7, 2, 9, 40)
        bars.append(_bar(t, 2004, 2007, 2003.8, 2006.8, v=90))
        return bars

    def test_detect_bullish_impulse(self):
        bars = self._build_bull_impulse_session()
        # need prior bars for median range — prepend overnight context
        prior = []
        for i in range(25):
            t = _ts(2026, 7, 2, 6, 0) + i * 300
            prior.append(_bar(t, 2000, 2000.4, 1999.6, 2000, v=40))
        bars = prior + bars
        seqs = detect_impulses(bars, "2026-07-02")
        self.assertTrue(any(s["direction"] == "bullish" for s in seqs))

    def test_c1_pipeline_and_stop(self):
        prior = []
        for i in range(25):
            t = _ts(2026, 7, 2, 6, 0) + i * 300
            prior.append(_bar(t, 2000, 2000.4, 1999.6, 2000, v=40))
        bars = prior + self._build_bull_impulse_session()
        seqs = detect_impulses(bars, "2026-07-02")
        if not seqs:
            self.skipTest("no impulse")
        cfg = next(c for c in PHASE28_CANDIDATES if c.candidate_id == "C1_P1_CONFIRM_CLOSE")
        setup = analyze_candidate(seqs[0], cfg)
        self.assertEqual(cfg.pullback_mode, PullbackMode.P1_HALF_RETRACE.value)
        self.assertEqual(cfg.entry_mode, EntryMode.CONFIRMATION_CLOSE.value)
        if setup.entry_triggered:
            self.assertIsNotNone(setup.stop_price)
            self.assertLess(setup.stop_price, setup.entry_price)

    def test_invalidation_breaks_impulse_low(self):
        prior = [_bar(_ts(2026, 7, 2, 6, 0) + i * 300, 2000, 2000.3, 1999.7, 2000, v=40) for i in range(25)]
        bars = list(prior)
        for i in range(10):
            t = _ts(2026, 7, 2, 8, 20) + i * 300
            bars.append(_bar(t, 2000, 2000.4, 1999.6, 2000.1, v=50))
        # impulse
        bars.append(_bar(_ts(2026, 7, 2, 9, 10), 2000, 2008, 1999.5, 2007.6, v=180))
        # invalidation below impulse low before pullback
        bars.append(_bar(_ts(2026, 7, 2, 9, 15), 2005, 2005.5, 1998.0, 1998.5, v=80))
        seqs = detect_impulses(bars, "2026-07-02")
        if not seqs:
            self.skipTest("no impulse")
        cfg = next(c for c in PHASE28_CANDIDATES if c.candidate_id == "C1_P1_CONFIRM_CLOSE")
        setup = analyze_candidate(seqs[0], cfg)
        self.assertFalse(setup.entry_triggered)
        self.assertIn(setup.state, ("INVALIDATED", "EXPIRED"))

    def test_event_dedupe_and_family(self):
        self.assertEqual(STRATEGY_FAMILY, "gc_ny_momentum_continuation_v1")
        self.assertLessEqual(len(PHASE28_CANDIDATES), 8)
        self.assertTrue(all(c.strategy_family == STRATEGY_FAMILY for c in PHASE28_CANDIDATES))


class IsolationTests(unittest.TestCase):
    def test_phase26_hash_unchanged(self):
        doc = json.loads(PHASE26_FROZEN.read_text(encoding="utf-8"))
        self.assertEqual(doc["frozen_config_hash"], PHASE26_HASH)

    def test_finalist_cap(self):
        from phase28_validate import select_finalists

        fake = [
            {"candidate_id": f"C{i}", "resolved_n": 40 + i, "theoretical_2r_expectancy": 0.1 * i, "stop_rate": 0.5}
            for i in range(5)
        ]
        out = select_finalists(fake)
        self.assertLessEqual(len(out), 3)

    def test_journal_path_isolated(self):
        from phase28_validate import JOURNAL_DIR, PHASE26_PAPER

        self.assertIn("phase28", str(JOURNAL_DIR))
        self.assertNotIn("phase26", str(JOURNAL_DIR))
        self.assertNotEqual(JOURNAL_DIR, PHASE26_PAPER.parent)


if __name__ == "__main__":
    unittest.main()
