"""Unit tests for Phase 5 FVG entry candidates (no CDP)."""

from __future__ import annotations

import unittest

from entry_detect import (
    boundary_price,
    ce_price,
    entry_depth_at_price,
    evaluate_entry,
    evaluate_entry_modes,
)
from models import Bar, EntryConfig, FVGZone


def _bar(ts: int, o: float, h: float, l: float, c: float) -> Bar:
    return Bar(time=ts, open=o, high=h, low=l, close=c)


def _bullish_fvg(**kwargs) -> FVGZone:
    base = dict(
        direction="bullish",
        low=100.0,
        high=110.0,
        midpoint=105.0,
        created_timestamp=300,
        candle1_timestamp=100,
        candle2_timestamp=200,
        candle3_timestamp=300,
        gap_size=10.0,
        gap_points=10.0,
        mitigated=False,
        first_mitigation_timestamp=None,
        fully_filled=False,
        first_full_fill_timestamp=None,
        bars_after_sweep=3,
        bars_after_confirmation=2,
        setup_reference={
            "sequence": "sweep→CHoCH→FVG",
            "session": "Asia",
            "sweep_side": "low",
            "sweep_level": 90.0,
            "sweep_timestamp": 50,
            "confirmation_kind": "CHoCH",
            "confirmation_direction": "bullish",
            "confirmation_level": 95.0,
            "confirmation_timestamp": 80,
            "narrative": "Asia low swept → bullish CHoCH → bullish FVG",
        },
    )
    base.update(kwargs)
    return FVGZone(**base)


def _bearish_fvg(**kwargs) -> FVGZone:
    base = dict(
        direction="bearish",
        low=100.0,
        high=110.0,
        midpoint=105.0,
        created_timestamp=300,
        candle1_timestamp=100,
        candle2_timestamp=200,
        candle3_timestamp=300,
        gap_size=10.0,
        gap_points=10.0,
        mitigated=False,
        first_mitigation_timestamp=None,
        fully_filled=False,
        first_full_fill_timestamp=None,
        bars_after_sweep=3,
        bars_after_confirmation=2,
        setup_reference={
            "sequence": "sweep→CHoCH→FVG",
            "session": "London",
            "sweep_side": "high",
            "sweep_level": 120.0,
            "sweep_timestamp": 50,
            "confirmation_kind": "CHoCH",
            "confirmation_direction": "bearish",
            "confirmation_level": 115.0,
            "confirmation_timestamp": 80,
            "narrative": "London high swept → bearish CHoCH → bearish FVG",
        },
    )
    base.update(kwargs)
    return FVGZone(**base)


class FirstTouchTests(unittest.TestCase):
    def test_bullish_first_touch(self):
        fvg = _bullish_fvg()
        bars = [
            _bar(100, 95, 98, 94, 97),
            _bar(200, 97, 112, 96, 111),
            _bar(300, 111, 115, 110, 114),  # creation — must not trigger
            _bar(400, 114, 116, 112, 115),  # still above zone
            _bar(500, 115, 116, 109, 110),  # low enters at 109 → first touch
        ]
        c = evaluate_entry(fvg, bars, EntryConfig(mode="first_touch"))
        self.assertTrue(c.triggered)
        self.assertEqual(c.status, "triggered")
        self.assertEqual(c.trigger_timestamp, 500)
        self.assertEqual(c.bars_after_fvg, 2)
        self.assertAlmostEqual(c.price, 109.0)
        self.assertAlmostEqual(c.entry_depth, 0.1, places=5)  # (110-109)/10
        self.assertAlmostEqual(c.max_retrace_depth, 0.1, places=5)

    def test_bearish_first_touch(self):
        fvg = _bearish_fvg()
        bars = [
            _bar(100, 112, 114, 111, 113),
            _bar(200, 113, 114, 98, 99),
            _bar(300, 99, 100, 96, 97),
            _bar(400, 97, 99, 95, 96),
            _bar(500, 96, 101, 95, 100),  # high enters zone
        ]
        c = evaluate_entry(fvg, bars, EntryConfig(mode="first_touch"))
        self.assertTrue(c.triggered)
        self.assertEqual(c.trigger_timestamp, 500)
        self.assertAlmostEqual(c.price, 101.0)


class CeTests(unittest.TestCase):
    def test_bullish_touch_without_ce_then_ce(self):
        fvg = _bullish_fvg()
        # Touch upper zone only (low=108), CE=105 not reached
        partial = [
            _bar(100, 95, 98, 94, 97),
            _bar(200, 97, 112, 96, 111),
            _bar(300, 111, 115, 110, 114),
            _bar(500, 114, 115, 108, 109),
        ]
        ft = evaluate_entry(fvg, partial, EntryConfig(mode="first_touch"))
        ce = evaluate_entry(fvg, partial, EntryConfig(mode="ce"))
        self.assertTrue(ft.triggered)
        self.assertFalse(ce.triggered)
        self.assertEqual(ce.status, "waiting")

        # Later reaches CE exactly (low=105)
        with_ce = partial + [_bar(600, 109, 110, 105, 106)]
        ce2 = evaluate_entry(fvg, with_ce, EntryConfig(mode="ce"))
        self.assertTrue(ce2.triggered)
        self.assertEqual(ce2.trigger_timestamp, 600)
        self.assertAlmostEqual(ce2.price, 105.0)
        self.assertAlmostEqual(ce2.entry_depth, 0.5, places=5)
        self.assertAlmostEqual(ce2.max_retrace_depth, 0.5, places=5)

    def test_bearish_ce(self):
        fvg = _bearish_fvg()
        bars = [
            _bar(100, 112, 114, 111, 113),
            _bar(200, 113, 114, 98, 99),
            _bar(300, 99, 100, 96, 97),
            _bar(500, 97, 102, 96, 101),  # into zone, not to CE 105
            _bar(600, 101, 106, 100, 105),  # high >= CE
        ]
        ce_early = evaluate_entry(fvg, bars[:-1], EntryConfig(mode="ce"))
        self.assertFalse(ce_early.triggered)
        ce = evaluate_entry(fvg, bars, EntryConfig(mode="ce"))
        self.assertTrue(ce.triggered)
        self.assertEqual(ce.trigger_timestamp, 600)


class BoundaryTests(unittest.TestCase):
    def test_bullish_boundary_price(self):
        fvg = _bullish_fvg()
        self.assertEqual(boundary_price(fvg), 110.0)
        self.assertEqual(ce_price(fvg), 105.0)
        bars = [
            _bar(100, 95, 98, 94, 97),
            _bar(200, 97, 112, 96, 111),
            _bar(300, 111, 115, 110, 114),
            _bar(500, 114, 115, 110, 111),  # touches high exactly
        ]
        c = evaluate_entry(fvg, bars, EntryConfig(mode="boundary"))
        self.assertTrue(c.triggered)
        self.assertAlmostEqual(c.price, 110.0)
        self.assertAlmostEqual(c.entry_depth, 0.0, places=5)
        self.assertAlmostEqual(c.max_retrace_depth, 0.0, places=5)

    def test_bearish_boundary_price(self):
        fvg = _bearish_fvg()
        self.assertEqual(boundary_price(fvg), 100.0)
        bars = [
            _bar(100, 112, 114, 111, 113),
            _bar(200, 113, 114, 98, 99),
            _bar(300, 99, 100, 96, 97),
            _bar(500, 97, 100, 96, 99),
        ]
        c = evaluate_entry(fvg, bars, EntryConfig(mode="boundary"))
        self.assertTrue(c.triggered)
        self.assertAlmostEqual(c.price, 100.0)


class NoRetraceAndCreationTests(unittest.TestCase):
    def test_no_retracement_waiting(self):
        fvg = _bullish_fvg()
        bars = [
            _bar(100, 95, 98, 94, 97),
            _bar(200, 97, 112, 96, 111),
            _bar(300, 111, 115, 110, 114),
            _bar(400, 114, 120, 113, 119),
            _bar(500, 119, 125, 118, 124),
        ]
        c = evaluate_entry(fvg, bars, EntryConfig(mode="first_touch"))
        self.assertFalse(c.triggered)
        self.assertEqual(c.status, "waiting")

    def test_creation_candle_does_not_trigger(self):
        fvg = _bullish_fvg()
        # Creation candle wick deep into zone — still excluded
        bars = [
            _bar(100, 95, 98, 94, 97),
            _bar(200, 97, 112, 96, 111),
            _bar(300, 111, 115, 100, 114),  # low through zone on create bar
        ]
        c = evaluate_entry(fvg, bars, EntryConfig(mode="first_touch"))
        self.assertFalse(c.triggered)
        self.assertEqual(c.status, "waiting")


class FullFillAndModesTests(unittest.TestCase):
    def test_full_fill_tracked_not_auto_invalid(self):
        fvg = _bullish_fvg()
        bars = [
            _bar(100, 95, 98, 94, 97),
            _bar(200, 97, 112, 96, 111),
            _bar(300, 111, 115, 110, 114),
            _bar(500, 114, 115, 99, 100),  # full fill on first touch bar
        ]
        c = evaluate_entry(
            fvg, bars, EntryConfig(mode="first_touch", allow_full_fill=True)
        )
        self.assertTrue(c.triggered)
        self.assertEqual(c.status, "triggered")
        self.assertTrue(c.extras.get("fully_filled_before_or_at_entry"))
        self.assertAlmostEqual(c.entry_depth, 1.0)  # clipped contact at low
        self.assertAlmostEqual(c.max_retrace_depth, 1.0)

    def test_multiple_modes_independent(self):
        fvg = _bullish_fvg()
        bars = [
            _bar(100, 95, 98, 94, 97),
            _bar(200, 97, 112, 96, 111),
            _bar(300, 111, 115, 110, 114),
            _bar(500, 114, 115, 108, 109),  # touch, not CE
        ]
        results = evaluate_entry_modes(fvg, bars)
        self.assertTrue(results["first_touch"].triggered)
        self.assertTrue(results["boundary"].triggered)
        self.assertFalse(results["ce"].triggered)
        self.assertEqual(results["ce"].status, "waiting")

    def test_entry_depth_formula(self):
        fvg = _bullish_fvg()
        self.assertAlmostEqual(entry_depth_at_price(fvg, 110.0), 0.0)
        self.assertAlmostEqual(entry_depth_at_price(fvg, 105.0), 0.5)
        self.assertAlmostEqual(entry_depth_at_price(fvg, 100.0), 1.0)


class DepthSemanticsTests(unittest.TestCase):
    """Explicit 4325–4330 bullish / bearish depth contracts."""

    def _zone_bull(self) -> FVGZone:
        return _bullish_fvg(
            low=4325.0,
            high=4330.0,
            midpoint=4327.5,
            gap_size=5.0,
            gap_points=5.0,
        )

    def _zone_bear(self) -> FVGZone:
        return _bearish_fvg(
            low=4325.0,
            high=4330.0,
            midpoint=4327.5,
            gap_size=5.0,
            gap_points=5.0,
        )

    def test_bullish_entry_depth_by_mode_price(self):
        fvg = self._zone_bull()
        self.assertAlmostEqual(entry_depth_at_price(fvg, 4330.0), 0.0)
        self.assertAlmostEqual(entry_depth_at_price(fvg, 4327.5), 0.5)
        self.assertAlmostEqual(entry_depth_at_price(fvg, 4325.0), 1.0)

        bars = [
            _bar(100, 4320, 4324, 4319, 4323),
            _bar(200, 4323, 4340, 4322, 4338),
            _bar(300, 4338, 4345, 4330, 4342),
            # Penetrates to 4328 — beyond boundary, not to CE
            _bar(500, 4342, 4343, 4328, 4329),
        ]
        boundary = evaluate_entry(fvg, bars, EntryConfig(mode="boundary"))
        self.assertTrue(boundary.triggered)
        self.assertAlmostEqual(boundary.price, 4330.0)
        self.assertAlmostEqual(boundary.entry_depth, 0.0, places=5)
        self.assertAlmostEqual(boundary.max_retrace_depth, 0.4, places=5)

        ft = evaluate_entry(fvg, bars, EntryConfig(mode="first_touch"))
        self.assertTrue(ft.triggered)
        self.assertAlmostEqual(ft.price, 4328.0)
        self.assertAlmostEqual(ft.entry_depth, 0.4, places=5)
        self.assertAlmostEqual(ft.max_retrace_depth, 0.4, places=5)

        ce_wait = evaluate_entry(fvg, bars, EntryConfig(mode="ce"))
        self.assertFalse(ce_wait.triggered)
        self.assertAlmostEqual(ce_wait.price, 4327.5)
        self.assertAlmostEqual(ce_wait.entry_depth, 0.5, places=5)

        # Candle reaches CE but trades to 4326 → entry_depth 0.5, max 0.8
        bars_ce = bars + [_bar(600, 4329, 4330, 4326, 4327)]
        ce = evaluate_entry(fvg, bars_ce, EntryConfig(mode="ce"))
        self.assertTrue(ce.triggered)
        self.assertAlmostEqual(ce.price, 4327.5)
        self.assertAlmostEqual(ce.entry_depth, 0.5, places=5)
        self.assertAlmostEqual(ce.max_retrace_depth, 0.8, places=5)

    def test_bearish_entry_depth_inverses(self):
        fvg = self._zone_bear()
        self.assertAlmostEqual(entry_depth_at_price(fvg, 4325.0), 0.0)
        self.assertAlmostEqual(entry_depth_at_price(fvg, 4327.5), 0.5)
        self.assertAlmostEqual(entry_depth_at_price(fvg, 4330.0), 1.0)

        bars = [
            _bar(100, 4332, 4334, 4331, 4333),
            _bar(200, 4333, 4334, 4320, 4321),
            _bar(300, 4321, 4325, 4318, 4320),
            # High to 4327 — past boundary (4325), short of CE
            _bar(500, 4320, 4327, 4319, 4326),
        ]
        boundary = evaluate_entry(fvg, bars, EntryConfig(mode="boundary"))
        self.assertTrue(boundary.triggered)
        self.assertAlmostEqual(boundary.price, 4325.0)
        self.assertAlmostEqual(boundary.entry_depth, 0.0, places=5)
        self.assertGreater(boundary.max_retrace_depth, 0.0)
        self.assertAlmostEqual(boundary.max_retrace_depth, 0.4, places=5)

        bars_ce = bars + [_bar(600, 4326, 4329, 4325, 4328)]  # high 4329 → depth 0.8
        ce = evaluate_entry(fvg, bars_ce, EntryConfig(mode="ce"))
        self.assertTrue(ce.triggered)
        self.assertAlmostEqual(ce.price, 4327.5)
        self.assertAlmostEqual(ce.entry_depth, 0.5, places=5)
        self.assertAlmostEqual(ce.max_retrace_depth, 0.8, places=5)


class InvalidOrderingTests(unittest.TestCase):
    def test_choch_before_sweep_invalid(self):
        fvg = _bullish_fvg(
            setup_reference={
                "sweep_timestamp": 200,
                "confirmation_timestamp": 100,
                "session": "Asia",
                "sweep_side": "low",
            }
        )
        bars = [
            _bar(100, 95, 98, 94, 97),
            _bar(200, 97, 112, 96, 111),
            _bar(300, 111, 115, 110, 114),
            _bar(500, 114, 115, 108, 109),
        ]
        c = evaluate_entry(fvg, bars, EntryConfig(mode="first_touch"))
        self.assertEqual(c.status, "invalid")
        self.assertFalse(c.triggered)
        self.assertEqual(c.extras.get("reason"), "choch_not_after_sweep")

    def test_missed_when_window_exhausted(self):
        fvg = _bullish_fvg()
        bars = [
            _bar(100, 95, 98, 94, 97),
            _bar(200, 97, 112, 96, 111),
            _bar(300, 111, 115, 110, 114),
            _bar(400, 114, 120, 113, 119),
            _bar(500, 119, 125, 118, 124),
        ]
        c = evaluate_entry(
            fvg,
            bars,
            EntryConfig(mode="first_touch", max_bars_after_fvg=2),
        )
        self.assertEqual(c.status, "missed")
        self.assertFalse(c.triggered)


if __name__ == "__main__":
    unittest.main()
