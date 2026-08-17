"""Phase 24 tests — OR15, breakout, boundary retest, FVG, pairing, 1m resolver."""

from __future__ import annotations

import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from gc_orb15_engine import (
    analyze_candidate,
    collect_or15_events,
    config_hash,
    find_boundary_retest,
    find_first_breakout_fvg,
    find_first_or15_breakout,
    find_fvg_retrace_entry,
    resolve_intrabar_with_1m,
)
from gc_orb15_models import (
    OR_MINUTES,
    OR_TIMEZONE,
    PHASE24_CANDIDATES,
    STRATEGY_FAMILY,
    EntryMode,
    ORB15StrategyConfig,
)
from gc_orb_engine import build_opening_range
from models import Bar


def _bar(t, o, h, l, c, v=100.0):
    return Bar(time=t, open=o, high=h, low=l, close=c, volume=v)


def _ts(y, m, d, hh, mm):
    return int(datetime(y, m, d, hh, mm, tzinfo=ZoneInfo(OR_TIMEZONE)).timestamp())


class OR15Tests(unittest.TestCase):
    def test_three_complete_5m_bars(self):
        bars = [
            _bar(_ts(2026, 7, 2, 8, 20), 100, 102, 99, 101),
            _bar(_ts(2026, 7, 2, 8, 25), 101, 103, 100, 102),
            _bar(_ts(2026, 7, 2, 8, 30), 102, 104, 101, 103),
        ]
        orng = build_opening_range(bars, "2026-07-02", or_minutes=OR_MINUTES)
        self.assertTrue(orng.complete)
        self.assertEqual(orng.bar_count, 3)
        self.assertAlmostEqual(orng.high, 104)
        self.assertAlmostEqual(orng.low, 99)
        self.assertEqual(orng.end_timestamp, _ts(2026, 7, 2, 8, 35))

    def test_missing_bar_incomplete(self):
        bars = [_bar(_ts(2026, 7, 2, 8, 20), 100, 102, 99, 101)]
        orng = build_opening_range(bars, "2026-07-02", or_minutes=15)
        self.assertFalse(orng.complete)

    def test_dst_aware_anchor(self):
        # EDT July vs EST January — both 08:20 local
        j = build_opening_range([], "2026-01-08", or_minutes=15)
        u = build_opening_range([], "2026-07-02", or_minutes=15)
        self.assertNotEqual(j.start_timestamp, u.start_timestamp)


class BreakoutTests(unittest.TestCase):
    def _or_and_hist(self):
        or_bars = [
            _bar(_ts(2026, 7, 2, 8, 20) + i * 300, 100, 102, 98, 100) for i in range(3)
        ]
        orng = build_opening_range(or_bars, "2026-07-02", or_minutes=15)
        hist = [_bar(orng.start_timestamp - (10 - i) * 300, 100, 101, 99, 100) for i in range(10)]
        return or_bars, orng, hist

    def test_wick_outside_no_breakout(self):
        or_bars, orng, hist = self._or_and_hist()
        later = [_bar(orng.end_timestamp, 101, 110, 100, 101.5)]  # wick above, close inside
        ev = find_first_or15_breakout(hist + or_bars + later, orng)
        self.assertIsNone(ev)

    def test_close_outside_breakout(self):
        or_bars, orng, hist = self._or_and_hist()
        later = [_bar(orng.end_timestamp, 101, 106, 100, 105)]
        ev = find_first_or15_breakout(hist + or_bars + later, orng)
        self.assertIsNotNone(ev)
        self.assertEqual(ev.direction, "bullish")

    def test_first_only_no_second_canonical(self):
        or_bars, orng, hist = self._or_and_hist()
        later = [
            _bar(orng.end_timestamp, 101, 106, 100, 105),  # bull first
            _bar(orng.end_timestamp + 300, 104, 105, 90, 92),  # later bear
        ]
        ev = find_first_or15_breakout(hist + or_bars + later, orng)
        self.assertEqual(ev.direction, "bullish")
        self.assertTrue(ev.opposite_break_after_first)


class BoundaryTests(unittest.TestCase):
    def test_bullish_touch_hold_and_reject(self):
        or_bars = [_bar(_ts(2026, 7, 2, 8, 20) + i * 300, 100, 102, 98, 100) for i in range(3)]
        orng = build_opening_range(or_bars, "2026-07-02", or_minutes=15)
        bo = _bar(orng.end_timestamp, 101, 106, 100, 105)
        ev = find_first_or15_breakout(or_bars + [bo], orng)
        # touch but close back inside → fail hold
        bad = _bar(bo.time + 300, 104, 104.5, 101.5, 101.8)
        self.assertIsNone(find_boundary_retest(or_bars + [bo, bad], ev, require_hold=True))
        # touch + hold
        good = _bar(bo.time + 300, 104, 104.5, 101.5, 102.5)
        rt = find_boundary_retest(or_bars + [bo, good], ev, require_hold=True)
        self.assertIsNotNone(rt)
        # touch-only accepts without hold close
        rt2 = find_boundary_retest(or_bars + [bo, bad], ev, require_hold=False)
        self.assertIsNotNone(rt2)

    def test_retest_timeout(self):
        or_bars = [_bar(_ts(2026, 7, 2, 8, 20) + i * 300, 100, 102, 98, 100) for i in range(3)]
        orng = build_opening_range(or_bars, "2026-07-02", or_minutes=15)
        bo = _bar(orng.end_timestamp, 101, 106, 100, 105)
        ev = find_first_or15_breakout(or_bars + [bo], orng)
        later = [_bar(bo.time + (i + 1) * 300, 110, 111, 109, 110) for i in range(12)]
        self.assertIsNone(find_boundary_retest(or_bars + [bo] + later, ev, require_hold=True))


class TouchLookaheadTests(unittest.TestCase):
    def test_b1_uses_or_mid_stop_not_future_extreme(self):
        or_bars = [_bar(_ts(2026, 7, 2, 8, 20) + i * 300, 100, 102, 98, 100) for i in range(3)]
        orng = build_opening_range(or_bars, "2026-07-02", or_minutes=15)
        bo = _bar(orng.end_timestamp, 101, 106, 100, 105)
        ev = find_first_or15_breakout(or_bars + [bo], orng)
        rt_bar = _bar(bo.time + 300, 104, 104.5, 99.0, 103.0)  # deep low
        cfg = next(c for c in PHASE24_CANDIDATES if c.candidate_id.startswith("B1_"))
        setup = analyze_candidate(ev, or_bars + [bo, rt_bar], cfg)
        self.assertTrue(setup.entry_triggered)
        self.assertAlmostEqual(setup.entry_price, ev.or_high)
        self.assertAlmostEqual(setup.stop_price, ev.or_mid)
        self.assertNotAlmostEqual(setup.stop_price, 99.0)


class FVGTests(unittest.TestCase):
    def test_first_bullish_fvg_and_pre_breakout_rejected(self):
        # Build OR + breakout, then bullish FVG after
        or_bars = [_bar(_ts(2026, 7, 2, 8, 20) + i * 300, 100, 102, 98, 100) for i in range(3)]
        orng = build_opening_range(or_bars, "2026-07-02", or_minutes=15)
        t0 = orng.end_timestamp
        # pre-breakout fake gap bars (before BO) should not count once we require c3>=BO
        pre = [
            _bar(t0 - 900, 90, 91, 89, 90),
            _bar(t0 - 600, 90, 95, 90, 94),
            _bar(t0 - 300, 94, 96, 93, 95),  # would be bullish FVG vs c1 but before BO
        ]
        bo = _bar(t0, 101, 108, 100, 107)
        # After BO: c1 high 107, c2, c3 low 110 → bullish FVG 107-110
        post = [
            _bar(t0 + 300, 107, 109, 106, 108),
            _bar(t0 + 600, 108, 112, 107, 111),
            _bar(t0 + 900, 111, 113, 110, 112),
        ]
        bars = or_bars + pre + [bo] + post
        ev = find_first_or15_breakout(bars, orng)
        self.assertEqual(ev.direction, "bullish")
        fvg = find_first_breakout_fvg(bars, ev)
        self.assertIsNotNone(fvg)
        self.assertGreaterEqual(fvg.created_timestamp, ev.breakout_timestamp)
        self.assertEqual(fvg.direction, "bullish")
        # CE retrace
        ce_bar = _bar(fvg.created_timestamp + 300, fvg.ce + 1, fvg.ce + 2, fvg.ce - 0.1, fvg.ce + 0.5)
        hit = find_fvg_retrace_entry(bars + [ce_bar], fvg, mode=EntryMode.FVG_CE.value)
        self.assertIsNotNone(hit)
        self.assertAlmostEqual(hit["entry_price"], fvg.ce)

    def test_wrong_direction_rejected(self):
        or_bars = [_bar(_ts(2026, 7, 2, 8, 20) + i * 300, 100, 102, 98, 100) for i in range(3)]
        orng = build_opening_range(or_bars, "2026-07-02", or_minutes=15)
        bo = _bar(orng.end_timestamp, 101, 106, 100, 105)  # bull
        # bearish FVG after bullish BO
        post = [
            _bar(bo.time + 300, 105, 106, 104, 104.5),
            _bar(bo.time + 600, 104, 104.5, 100, 101),
            _bar(bo.time + 900, 101, 101.5, 99, 100),  # c1.low > c3.high? 104 > 101.5 → bearish
        ]
        bars = or_bars + [bo] + post
        ev = find_first_or15_breakout(bars, orng)
        fvg = find_first_breakout_fvg(bars, ev)
        # may be None if no bullish FVG; must not be bearish
        if fvg is not None:
            self.assertEqual(fvg.direction, "bullish")


class IntrabarResolverTests(unittest.TestCase):
    def test_1m_resolves_and_same_1m_ambiguous(self):
        # stop 100, target 102, bullish
        bars_ok = [
            _bar(1000, 101, 101.5, 100.5, 101),
            _bar(1060, 101, 101.2, 99.5, 100),  # stop first
        ]
        r = resolve_intrabar_with_1m(
            entry_ts=1000,
            entry_price=101,
            stop_price=100,
            direction="bullish",
            target_prices=[102],
            bars_1m=bars_ok,
        )
        self.assertEqual(r["status"], "STOP_FIRST")
        bars_amb = [_bar(1000, 101, 103, 99, 102)]  # both in same 1m
        r2 = resolve_intrabar_with_1m(
            entry_ts=1000,
            entry_price=101,
            stop_price=100,
            direction="bullish",
            target_prices=[102],
            bars_1m=bars_amb,
        )
        self.assertEqual(r2["status"], "STILL_AMBIGUOUS")


class PairingTests(unittest.TestCase):
    def test_shared_event_id_across_candidates(self):
        or_bars = [_bar(_ts(2026, 7, 2, 8, 20) + i * 300, 100, 102, 98, 100) for i in range(3)]
        orng = build_opening_range(or_bars, "2026-07-02", or_minutes=15)
        bo = _bar(orng.end_timestamp, 101, 106, 100, 105)
        ev = find_first_or15_breakout(or_bars + [bo], orng)
        ids = set()
        for cfg in PHASE24_CANDIDATES:
            setup = analyze_candidate(ev, or_bars + [bo], cfg)
            ids.add(setup.orb_breakout_event_id)
        self.assertEqual(len(ids), 1)
        self.assertEqual(next(iter(ids)), ev.event_id)

    def test_family_isolated(self):
        self.assertEqual(STRATEGY_FAMILY, "gc_orb15_retest_fvg_v1")
        self.assertEqual(len(PHASE24_CANDIDATES), 5)
        self.assertTrue(all(not c.volume_filter for c in PHASE24_CANDIDATES))
        self.assertTrue(all(not c.displacement_filter for c in PHASE24_CANDIDATES))


class SplitFreezeTests(unittest.TestCase):
    def test_config_hash_stable(self):
        cfg = PHASE24_CANDIDATES[0]
        self.assertEqual(config_hash(cfg), config_hash(cfg))


if __name__ == "__main__":
    unittest.main()
