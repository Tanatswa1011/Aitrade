"""Unit tests for Phase 4 setup-linked FVG detection (no CDP)."""

from __future__ import annotations

import unittest

from fvg_detect import detect_first_fvg, detect_fvg
from models import Bar, FVGConfig, LiquiditySweep, StructureConfirmation


def _bar(ts: int, o: float, h: float, l: float, c: float) -> Bar:
    return Bar(time=ts, open=o, high=h, low=l, close=c)


def _sweep(*, session: str, side: str, level: float, ts: int) -> LiquiditySweep:
    return LiquiditySweep(
        session=session,
        side=side,
        level=level,
        sweep_timestamp=ts,
        sweep_price=level,
        maximum_excursion=1.0,
        reclaim_status=True,
        rule="wick_only",
        sweep_candle=_bar(ts, level, level + 1, level - 1, level),
    )


def _choch(*, direction: str, level: float, ts: int) -> StructureConfirmation:
    return StructureConfirmation(
        kind="CHoCH",
        direction=direction,
        level=level,
        event_timestamp=ts,
        event_bar_index=None,
        source="luxalgo",
        study_id="smUEv2",
        raw_id="1",
        timing_confidence="exact",
    )


class BullishSequenceTests(unittest.TestCase):
    def test_valid_bullish_fvg_after_choch(self):
        # Sweep @ 100, CHoCH @ 200, then bullish FVG on 300/400/500
        sweep = _sweep(session="Asia", side="low", level=90.0, ts=100)
        conf = _choch(direction="bullish", level=95.0, ts=200)
        bars = [
            _bar(100, 90, 92, 88, 91),  # sweep
            _bar(200, 93, 96, 92, 95),  # choch bar
            # After CHoCH: bullish FVG — c1.high < c3.low
            _bar(300, 96, 98, 95, 97),  # c1 high=98
            _bar(400, 97, 105, 96, 104),  # c2 displacement-ish
            _bar(500, 104, 108, 100, 107),  # c3 low=100 > 98 → gap 98-100
        ]
        result = detect_fvg(sweep, conf, bars)
        self.assertTrue(result.found)
        z = result.zones[0]
        self.assertEqual(z.direction, "bullish")
        self.assertEqual(z.low, 98.0)
        self.assertEqual(z.high, 100.0)
        self.assertEqual(z.midpoint, 99.0)
        self.assertEqual(z.created_timestamp, 500)
        self.assertIn("Asia", z.setup_reference["narrative"])
        self.assertIn("low swept", z.setup_reference["narrative"])
        self.assertEqual(z.setup_reference["sweep_side"], "low")
        self.assertEqual(z.setup_reference["confirmation_direction"], "bullish")

    def test_fvg_before_choch_ignored(self):
        sweep = _sweep(session="Asia", side="low", level=90.0, ts=50)
        conf = _choch(direction="bullish", level=95.0, ts=400)
        bars = [
            _bar(100, 96, 98, 95, 97),  # would-be FVG before CHoCH
            _bar(200, 97, 105, 96, 104),
            _bar(300, 104, 108, 100, 107),
            _bar(400, 107, 110, 106, 109),  # choch time
            _bar(500, 109, 111, 108, 110),
            _bar(600, 110, 112, 109, 111),
            _bar(700, 111, 113, 110, 112),  # no gap
        ]
        result = detect_fvg(sweep, conf, bars)
        self.assertFalse(result.found)

    def test_fvg_before_sweep_ignored(self):
        # Even if somehow confirmation is after, FVG candles must be after both.
        sweep = _sweep(session="Asia", side="low", level=90.0, ts=250)
        conf = _choch(direction="bullish", level=95.0, ts=260)
        bars = [
            _bar(100, 96, 98, 95, 97),
            _bar(200, 97, 105, 96, 104),
            _bar(240, 104, 108, 100, 107),  # FVG completes before sweep
            _bar(250, 90, 92, 88, 89),
            _bar(260, 93, 96, 92, 95),
            _bar(300, 95, 96, 94, 95),
            _bar(400, 95, 97, 94, 96),
            _bar(500, 96, 98, 95, 97),
        ]
        result = detect_fvg(sweep, conf, bars)
        self.assertFalse(result.found)


class BearishSequenceTests(unittest.TestCase):
    def test_valid_bearish_fvg_after_choch(self):
        sweep = _sweep(session="London", side="high", level=110.0, ts=100)
        conf = _choch(direction="bearish", level=108.0, ts=200)
        bars = [
            _bar(100, 108, 112, 107, 109),
            _bar(200, 109, 110, 105, 106),
            # Bearish FVG: c1.low > c3.high → zone high=c1.low, low=c3.high
            _bar(300, 106, 107, 104, 105),  # c1 low=104
            _bar(400, 105, 106, 98, 99),  # c2
            _bar(500, 99, 100, 96, 97),  # c3 high=100 < 104 → gap 100-104
        ]
        result = detect_fvg(sweep, conf, bars)
        self.assertTrue(result.found)
        z = result.zones[0]
        self.assertEqual(z.direction, "bearish")
        self.assertEqual(z.low, 100.0)
        self.assertEqual(z.high, 104.0)
        self.assertEqual(z.setup_reference["session"], "London")
        self.assertEqual(z.setup_reference["sweep_side"], "high")


class DirectionAndGapTests(unittest.TestCase):
    def test_wrong_direction_fvg_ignored(self):
        # Bullish setup but only bearish imbalance after CHoCH
        sweep = _sweep(session="Asia", side="low", level=90.0, ts=100)
        conf = _choch(direction="bullish", level=95.0, ts=200)
        bars = [
            _bar(100, 90, 92, 88, 91),
            _bar(200, 93, 96, 92, 95),
            _bar(300, 106, 107, 104, 105),  # bearish-pattern candles
            _bar(400, 105, 106, 98, 99),
            _bar(500, 99, 100, 96, 97),
        ]
        result = detect_fvg(sweep, conf, bars)
        self.assertFalse(result.found)
        self.assertEqual(result.reason, "no_fvg_after_confirmation")

    def test_no_fvg(self):
        sweep = _sweep(session="Asia", side="low", level=90.0, ts=100)
        conf = _choch(direction="bullish", level=95.0, ts=200)
        bars = [
            _bar(100, 90, 92, 88, 91),
            _bar(200, 93, 96, 92, 95),
            _bar(300, 95, 97, 94, 96),
            _bar(400, 96, 98, 95, 97),
            _bar(500, 97, 99, 96, 98),  # overlapping — no gap
        ]
        result = detect_fvg(sweep, conf, bars)
        self.assertFalse(result.found)
        self.assertEqual(result.zones, [])

    def test_min_gap_rejects_small(self):
        sweep = _sweep(session="Asia", side="low", level=90.0, ts=100)
        conf = _choch(direction="bullish", level=95.0, ts=200)
        bars = [
            _bar(100, 90, 92, 88, 91),
            _bar(200, 93, 96, 92, 95),
            _bar(300, 96, 98, 95, 97),  # high 98
            _bar(400, 97, 105, 96, 104),
            _bar(500, 104, 108, 98.5, 107),  # low 98.5 → gap 0.5
        ]
        result = detect_fvg(
            sweep, conf, bars, FVGConfig(first_only=True, min_gap=1.0)
        )
        self.assertFalse(result.found)

        result_ok = detect_fvg(
            sweep, conf, bars, FVGConfig(first_only=True, min_gap=0.4)
        )
        self.assertTrue(result_ok.found)


class MultipleFvgTests(unittest.TestCase):
    def _two_bullish_gaps(self):
        sweep = _sweep(session="Asia", side="low", level=90.0, ts=100)
        conf = _choch(direction="bullish", level=95.0, ts=200)
        bars = [
            _bar(100, 90, 92, 88, 91),
            _bar(200, 93, 96, 92, 95),
            # FVG1: 98 → 100
            _bar(300, 96, 98, 95, 97),
            _bar(400, 97, 105, 96, 104),
            _bar(500, 104, 108, 100, 107),
            # FVG2: 110 → 113
            _bar(600, 107, 110, 106, 109),
            _bar(700, 109, 120, 108, 118),
            _bar(800, 118, 125, 113, 124),
        ]
        return sweep, conf, bars

    def test_first_only_true(self):
        sweep, conf, bars = self._two_bullish_gaps()
        result = detect_fvg(sweep, conf, bars, FVGConfig(first_only=True))
        self.assertTrue(result.found)
        self.assertEqual(len(result.zones), 1)
        self.assertEqual(result.zones[0].created_timestamp, 500)

    def test_first_only_false(self):
        sweep, conf, bars = self._two_bullish_gaps()
        result = detect_fvg(sweep, conf, bars, FVGConfig(first_only=False))
        self.assertTrue(result.found)
        self.assertGreaterEqual(len(result.zones), 2)
        self.assertEqual(result.zones[0].created_timestamp, 500)
        self.assertEqual(result.zones[0].low, 98.0)
        # Second intentional gap (110–113) must appear among all qualifying zones.
        intentional = [z for z in result.zones if z.low == 110.0 and z.high == 113.0]
        self.assertEqual(len(intentional), 1)
        self.assertEqual(intentional[0].created_timestamp, 800)


class MitigationTests(unittest.TestCase):
    def test_partial_mitigation_vs_full_fill(self):
        sweep = _sweep(session="Asia", side="low", level=90.0, ts=100)
        conf = _choch(direction="bullish", level=95.0, ts=200)
        bars = [
            _bar(100, 90, 92, 88, 91),
            _bar(200, 93, 96, 92, 95),
            _bar(300, 96, 98, 95, 97),  # zone 98-100
            _bar(400, 97, 105, 96, 104),
            _bar(500, 104, 108, 100, 107),
            # Partial: trades into zone (low 99) but not through low 98
            _bar(600, 107, 108, 99, 100),
            # Full fill later
            _bar(700, 100, 101, 97, 98),
        ]
        z = detect_first_fvg(sweep, conf, bars)
        self.assertIsNotNone(z)
        assert z is not None
        self.assertTrue(z.mitigated)
        self.assertEqual(z.first_mitigation_timestamp, 600)
        self.assertTrue(z.fully_filled)
        self.assertEqual(z.first_full_fill_timestamp, 700)

    def test_mitigation_without_full_fill(self):
        sweep = _sweep(session="Asia", side="low", level=90.0, ts=100)
        conf = _choch(direction="bullish", level=95.0, ts=200)
        bars = [
            _bar(100, 90, 92, 88, 91),
            _bar(200, 93, 96, 92, 95),
            _bar(300, 96, 98, 95, 97),
            _bar(400, 97, 105, 96, 104),
            _bar(500, 104, 108, 100, 107),
            _bar(600, 107, 108, 99, 100),  # touch only
            _bar(700, 100, 105, 99.5, 104),
        ]
        z = detect_first_fvg(sweep, conf, bars)
        self.assertIsNotNone(z)
        assert z is not None
        self.assertTrue(z.mitigated)
        self.assertFalse(z.fully_filled)
        self.assertIsNone(z.first_full_fill_timestamp)

    def test_bearish_mitigation(self):
        sweep = _sweep(session="London", side="high", level=110.0, ts=100)
        conf = _choch(direction="bearish", level=108.0, ts=200)
        bars = [
            _bar(100, 108, 112, 107, 109),
            _bar(200, 109, 110, 105, 106),
            _bar(300, 106, 107, 104, 105),  # zone 100-104
            _bar(400, 105, 106, 98, 99),
            _bar(500, 99, 100, 96, 97),
            _bar(600, 97, 102, 96, 101),  # into zone (partial)
            _bar(700, 101, 105, 100, 104),  # full fill (>= 104)
        ]
        z = detect_first_fvg(sweep, conf, bars)
        self.assertIsNotNone(z)
        assert z is not None
        self.assertTrue(z.mitigated)
        self.assertEqual(z.first_mitigation_timestamp, 600)
        self.assertTrue(z.fully_filled)
        self.assertEqual(z.first_full_fill_timestamp, 700)


class FailClosedTests(unittest.TestCase):
    def test_unreliable_choch_blocks_fvg(self):
        sweep = _sweep(session="Asia", side="low", level=90.0, ts=100)
        conf = StructureConfirmation(
            kind="CHoCH",
            direction="bullish",
            level=95.0,
            event_timestamp=None,
            event_bar_index=None,
            source="luxalgo",
            study_id="smUEv2",
            raw_id="x",
            timing_confidence="unavailable",
        )
        bars = [
            _bar(100, 90, 92, 88, 91),
            _bar(300, 96, 98, 95, 97),
            _bar(400, 97, 105, 96, 104),
            _bar(500, 104, 108, 100, 107),
        ]
        result = detect_fvg(sweep, conf, bars)
        self.assertFalse(result.found)
        self.assertEqual(result.reason, "confirmation_timing_unreliable")

    def test_direction_mismatch_blocks(self):
        sweep = _sweep(session="Asia", side="low", level=90.0, ts=100)
        conf = _choch(direction="bearish", level=95.0, ts=200)  # wrong for low sweep
        bars = [
            _bar(100, 90, 92, 88, 91),
            _bar(200, 93, 96, 92, 95),
            _bar(300, 96, 98, 95, 97),
            _bar(400, 97, 105, 96, 104),
            _bar(500, 104, 108, 100, 107),
        ]
        result = detect_fvg(sweep, conf, bars)
        self.assertFalse(result.found)
        self.assertEqual(result.reason, "confirmation_direction_mismatch")


if __name__ == "__main__":
    unittest.main()
