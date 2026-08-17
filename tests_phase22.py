"""Phase 22 tests — GC ORB, volume RVOL leakage, roll artifacts, split isolation."""

from __future__ import annotations

import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from gc_orb_engine import (
    build_opening_range,
    compute_rvol,
    detect_roll_gap_timestamps,
    find_first_breakouts,
    find_retest,
    rolling_median_volume,
)
from gc_orb_models import (
    DISPLACEMENT_BODY_OR_RATIO,
    OR_TIMEZONE,
    PHASE22_CANDIDATES,
    STRATEGY_FAMILY,
    VOLUME_RVOL_THRESHOLD,
)
from models import Bar


def _bar(t, o, h, l, c, v=100.0):
    return Bar(time=t, open=o, high=h, low=l, close=c, volume=v)


def _ts(y, m, d, hh, mm):
    return int(datetime(y, m, d, hh, mm, tzinfo=ZoneInfo(OR_TIMEZONE)).timestamp())


class FuturesVolumeTests(unittest.TestCase):
    def test_volume_preserved_and_non_negative(self):
        b = _bar(1, 1, 2, 0.5, 1.5, 12.0)
        self.assertEqual(b.volume, 12.0)
        self.assertGreaterEqual(b.volume, 0)

    def test_rvol_excludes_breakout_bar(self):
        bars = [_bar(i * 300, 1, 2, 0.5, 1, v=float(i + 1)) for i in range(25)]
        # breakout at index 20
        ref = rolling_median_volume(bars, 20, lookback=20)
        # median of volumes 1..20 (indices 0..19) = 10.5
        self.assertAlmostEqual(ref, 10.5)
        rvol = compute_rvol(bars[20].volume, ref)
        self.assertAlmostEqual(rvol, 21.0 / 10.5)

    def test_zero_reference_volume(self):
        self.assertIsNone(compute_rvol(10.0, 0.0))
        self.assertIsNone(compute_rvol(10.0, None))


class OpeningRangeTests(unittest.TestCase):
    def test_complete_or_high_low_mid(self):
        # 2026-07-02 08:20-08:50 NY → 6 five-minute bars for OR30
        bars = []
        for i in range(6):
            t = _ts(2026, 7, 2, 8, 20) + i * 300
            bars.append(_bar(t, 2000 + i, 2010 + i, 1990 + i, 2005 + i, 50 + i))
        orng = build_opening_range(bars, "2026-07-02", or_minutes=30)
        self.assertTrue(orng.complete)
        self.assertEqual(orng.bar_count, 6)
        self.assertAlmostEqual(orng.high, max(b.high for b in bars))
        self.assertAlmostEqual(orng.low, min(b.low for b in bars))
        self.assertAlmostEqual(orng.midpoint, (orng.high + orng.low) / 2)

    def test_missing_bars_incomplete(self):
        bars = [_bar(_ts(2026, 7, 2, 8, 20), 1, 2, 0.5, 1.5)]
        orng = build_opening_range(bars, "2026-07-02", or_minutes=30)
        self.assertFalse(orng.complete)

    def test_zero_range_incomplete(self):
        bars = [_bar(_ts(2026, 7, 2, 8, 20) + i * 300, 100, 100, 100, 100) for i in range(6)]
        orng = build_opening_range(bars, "2026-07-02", or_minutes=30)
        self.assertFalse(orng.complete)


class BreakoutTests(unittest.TestCase):
    def test_wick_outside_close_inside_no_breakout(self):
        or_bars = [_bar(_ts(2026, 7, 2, 8, 20) + i * 300, 100, 102, 98, 100) for i in range(6)]
        orng = build_opening_range(or_bars, "2026-07-02", or_minutes=30)
        # wick above high but close inside
        later = [_bar(orng.end_timestamp + 300, 101, 105, 100, 101.5)]
        events = find_first_breakouts(or_bars + later, orng)
        self.assertEqual(events, [])

    def test_close_above_bullish(self):
        or_bars = [_bar(_ts(2026, 7, 2, 8, 20) + i * 300, 100, 102, 98, 100, v=10) for i in range(6)]
        orng = build_opening_range(or_bars, "2026-07-02", or_minutes=30)
        later = [_bar(orng.end_timestamp + 300, 101, 106, 100, 104, v=50)]
        # pad history for rvol
        hist = [_bar(orng.start_timestamp - (20 - i) * 300, 100, 101, 99, 100, v=10) for i in range(20)]
        events = find_first_breakouts(hist + or_bars + later, orng)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].side, "bullish")
        self.assertGreater(events[0].body_or_ratio, 0)

    def test_close_below_bearish(self):
        or_bars = [_bar(_ts(2026, 7, 2, 8, 20) + i * 300, 100, 102, 98, 100, v=10) for i in range(6)]
        orng = build_opening_range(or_bars, "2026-07-02", or_minutes=30)
        later = [_bar(orng.end_timestamp + 300, 99, 100, 90, 95, v=50)]
        hist = [_bar(orng.start_timestamp - (20 - i) * 300, 100, 101, 99, 100, v=10) for i in range(20)]
        events = find_first_breakouts(hist + or_bars + later, orng)
        self.assertEqual(events[0].side, "bearish")


class DisplacementTests(unittest.TestCase):
    def test_threshold_equality(self):
        self.assertEqual(DISPLACEMENT_BODY_OR_RATIO, 0.50)
        self.assertEqual(VOLUME_RVOL_THRESHOLD, 1.5)

    def test_zero_range_guard(self):
        or_bars = [_bar(_ts(2026, 7, 2, 8, 20) + i * 300, 100, 100, 100, 100) for i in range(6)]
        orng = build_opening_range(or_bars, "2026-07-02", or_minutes=30)
        self.assertFalse(orng.complete)


class RetestTests(unittest.TestCase):
    def test_valid_bullish_retest(self):
        or_bars = [_bar(_ts(2026, 7, 2, 8, 20) + i * 300, 100, 102, 98, 100, v=10) for i in range(6)]
        orng = build_opening_range(or_bars, "2026-07-02", or_minutes=30)
        hist = [_bar(orng.start_timestamp - (20 - i) * 300, 100, 101, 99, 100, v=10) for i in range(20)]
        bo = _bar(orng.end_timestamp + 300, 101, 106, 100, 104, v=50)
        events = find_first_breakouts(hist + or_bars + [bo], orng)
        rt_bar = _bar(bo.time + 300, 103, 104, 101.5, 102.5)  # low <= OR high, close >= OR high
        rt = find_retest(hist + or_bars + [bo, rt_bar], events[0], max_retest_bars=6)
        self.assertIsNotNone(rt)

    def test_touch_but_close_fails(self):
        or_bars = [_bar(_ts(2026, 7, 2, 8, 20) + i * 300, 100, 102, 98, 100, v=10) for i in range(6)]
        orng = build_opening_range(or_bars, "2026-07-02", or_minutes=30)
        hist = [_bar(orng.start_timestamp - (20 - i) * 300, 100, 101, 99, 100, v=10) for i in range(20)]
        bo = _bar(orng.end_timestamp + 300, 101, 106, 100, 104, v=50)
        events = find_first_breakouts(hist + or_bars + [bo], orng)
        fail = _bar(bo.time + 300, 103, 104, 101, 101)  # close < OR high (102)
        rt = find_retest([bo, fail], events[0], max_retest_bars=6)
        self.assertIsNone(rt)

    def test_timeout(self):
        or_bars = [_bar(_ts(2026, 7, 2, 8, 20) + i * 300, 100, 102, 98, 100, v=10) for i in range(6)]
        orng = build_opening_range(or_bars, "2026-07-02", or_minutes=30)
        hist = [_bar(orng.start_timestamp - (20 - i) * 300, 100, 101, 99, 100, v=10) for i in range(20)]
        bo = _bar(orng.end_timestamp + 300, 101, 106, 100, 104, v=50)
        events = find_first_breakouts(hist + or_bars + [bo], orng)
        far = [_bar(bo.time + (i + 1) * 300, 105, 107, 104, 106) for i in range(6)]
        self.assertIsNone(find_retest([bo] + far, events[0], max_retest_bars=6))


class RollArtifactTests(unittest.TestCase):
    def test_roll_gap_flagged(self):
        a = _bar(1_000_000, 2000, 2001, 1999, 2000)
        b = _bar(1_000_000 + 4 * 3600, 2020, 2021, 2019, 2020)  # 20pt jump after 4h
        flags = detect_roll_gap_timestamps([a, b])
        self.assertIn(b.time, flags)

    def test_fake_breakout_marked_roll_artifact(self):
        or_bars = [_bar(_ts(2026, 7, 2, 8, 20) + i * 300, 100, 102, 98, 100, v=10) for i in range(6)]
        orng = build_opening_range(or_bars, "2026-07-02", or_minutes=30)
        # inject roll flag at breakout timestamp
        bo = _bar(orng.end_timestamp + 300, 101, 106, 100, 104, v=50)
        hist = [_bar(orng.start_timestamp - (20 - i) * 300, 100, 101, 99, 100, v=10) for i in range(20)]
        events = find_first_breakouts(hist + or_bars + [bo], orng, roll_flags={bo.time})
        self.assertTrue(events[0].roll_artifact)


class MatrixIsolationTests(unittest.TestCase):
    def test_candidate_count_and_family(self):
        self.assertLessEqual(len(PHASE22_CANDIDATES), 10)
        self.assertEqual(STRATEGY_FAMILY, "gc_orb_volume_v1")
        ids = {c.candidate_id for c in PHASE22_CANDIDATES}
        self.assertEqual(len(ids), len(PHASE22_CANDIDATES))
        # volume threshold frozen
        for c in PHASE22_CANDIDATES:
            self.assertEqual(c.rvol_threshold, 1.5)


if __name__ == "__main__":
    unittest.main()
