"""Unit tests for Phase 6 risk / target planning (no CDP)."""

from __future__ import annotations

import unittest

from models import (
    Bar,
    EntryCandidate,
    FVGZone,
    LiquiditySweep,
    RiskConfig,
    SessionRange,
    TargetConfig,
)
from risk_plan import build_risk_plan, sweep_extreme
from target_plan import build_target_plan, opposite_liquidity_target


def _bar(ts: int, o: float, h: float, l: float, c: float) -> Bar:
    return Bar(time=ts, open=o, high=h, low=l, close=c)


def _sweep_low(*, extreme: float = 4300.0, level: float = 4311.0, ts: int = 100) -> LiquiditySweep:
    return LiquiditySweep(
        session="Asia",
        side="low",
        level=level,
        sweep_timestamp=ts,
        sweep_price=extreme,
        maximum_excursion=level - extreme,
        reclaim_status=True,
        rule="wick_only",
        sweep_candle=_bar(ts, level, level + 1, extreme, level + 0.5),
    )


def _sweep_high(*, extreme: float = 4400.0, level: float = 4380.0, ts: int = 100) -> LiquiditySweep:
    return LiquiditySweep(
        session="London",
        side="high",
        level=level,
        sweep_timestamp=ts,
        sweep_price=extreme,
        maximum_excursion=extreme - level,
        reclaim_status=True,
        rule="wick_only",
        sweep_candle=_bar(ts, level, extreme, level - 1, level - 0.5),
    )


def _fvg_bull(**kwargs) -> FVGZone:
    base = dict(
        direction="bullish",
        low=4325.0,
        high=4330.0,
        midpoint=4327.5,
        created_timestamp=300,
        candle1_timestamp=100,
        candle2_timestamp=200,
        candle3_timestamp=300,
        gap_size=5.0,
        gap_points=5.0,
        mitigated=False,
        first_mitigation_timestamp=None,
        fully_filled=False,
        first_full_fill_timestamp=None,
        bars_after_sweep=3,
        bars_after_confirmation=2,
        setup_reference={
            "session": "Asia",
            "sweep_side": "low",
            "sweep_level": 4311.0,
            "sweep_timestamp": 50,
            "confirmation_timestamp": 80,
            "confirmation_direction": "bullish",
        },
    )
    base.update(kwargs)
    return FVGZone(**base)


def _fvg_bear(**kwargs) -> FVGZone:
    f = _fvg_bull(direction="bearish", **kwargs)
    return f


def _entry(
    *,
    mode: str,
    direction: str,
    price: float,
    ts: int,
    depth: float = 0.5,
) -> EntryCandidate:
    return EntryCandidate(
        mode=mode,
        direction=direction,
        price=price,
        triggered=True,
        trigger_timestamp=ts,
        trigger_bar_index=10,
        fvg_reference={},
        setup_reference={
            "session": "Asia" if direction == "bullish" else "London",
            "sweep_side": "low" if direction == "bullish" else "high",
            "sweep_timestamp": 50,
            "confirmation_timestamp": 80,
        },
        entry_depth=depth,
        max_retrace_depth=depth,
        bars_after_fvg=2,
        status="triggered",
        extras={"fully_filled_before_or_at_entry": False},
    )


def _session_asia(*, high: float = 4360.0, low: float = 4311.0) -> SessionRange:
    return SessionRange(
        name="Asia",
        timezone="America/New_York",
        start=0,
        end=100,
        high=high,
        low=low,
        high_timestamp=None,
        low_timestamp=None,
        complete=True,
        source="ict_sessions",
        coverage_status="full",
    )


class BeyondSweepTests(unittest.TestCase):
    def test_bullish_beyond_sweep(self):
        sweep = _sweep_low(extreme=4300.0, level=4311.0)
        fvg = _fvg_bull()
        entry = _entry(mode="ce", direction="bullish", price=4327.5, ts=500)
        bars = [
            _bar(300, 4330, 4340, 4325, 4335),
            _bar(500, 4330, 4331, 4327.5, 4328),
        ]
        plan = build_risk_plan(sweep, fvg, entry, bars, RiskConfig(stop_mode="beyond_sweep"))
        self.assertTrue(plan.valid)
        self.assertEqual(plan.stop_price, 4300.0)
        self.assertEqual(sweep_extreme(sweep), 4300.0)
        self.assertAlmostEqual(plan.risk_distance, 27.5)
        self.assertLess(plan.stop_price, plan.entry_price)

    def test_bearish_beyond_sweep(self):
        sweep = _sweep_high(extreme=4400.0, level=4380.0)
        fvg = _fvg_bear()
        entry = _entry(mode="ce", direction="bearish", price=4327.5, ts=500)
        bars = [_bar(300, 4330, 4340, 4325, 4335), _bar(500, 4326, 4328, 4325, 4327)]
        plan = build_risk_plan(sweep, fvg, entry, bars, RiskConfig(stop_mode="beyond_sweep"))
        self.assertTrue(plan.valid)
        self.assertEqual(plan.stop_price, 4400.0)
        self.assertGreater(plan.stop_price, plan.entry_price)
        self.assertAlmostEqual(plan.risk_distance, 72.5)

    def test_buffer_applied(self):
        sweep = _sweep_low(extreme=4300.0)
        fvg = _fvg_bull()
        entry = _entry(mode="ce", direction="bullish", price=4327.5, ts=500)
        bars = [_bar(500, 4330, 4331, 4327.5, 4328)]
        plan = build_risk_plan(
            sweep,
            fvg,
            entry,
            bars,
            RiskConfig(stop_mode="beyond_sweep", stop_buffer_price=2.0),
        )
        self.assertTrue(plan.valid)
        self.assertEqual(plan.stop_price, 4298.0)
        self.assertEqual(plan.buffer, 2.0)


class BeyondFvgTests(unittest.TestCase):
    def test_bullish_beyond_fvg(self):
        sweep = _sweep_low(extreme=4300.0)
        fvg = _fvg_bull(low=4325.0, high=4330.0)
        entry = _entry(mode="boundary", direction="bullish", price=4330.0, ts=500)
        bars = [_bar(500, 4331, 4332, 4330, 4331)]
        plan = build_risk_plan(sweep, fvg, entry, bars, RiskConfig(stop_mode="beyond_fvg"))
        self.assertTrue(plan.valid)
        self.assertEqual(plan.stop_price, 4325.0)
        self.assertAlmostEqual(plan.risk_distance, 5.0)

    def test_bearish_beyond_fvg(self):
        sweep = _sweep_high(extreme=4400.0)
        fvg = _fvg_bear(low=4325.0, high=4330.0)
        entry = _entry(mode="boundary", direction="bearish", price=4325.0, ts=500)
        bars = [_bar(500, 4324, 4325, 4323, 4324)]
        plan = build_risk_plan(sweep, fvg, entry, bars, RiskConfig(stop_mode="beyond_fvg"))
        self.assertTrue(plan.valid)
        self.assertEqual(plan.stop_price, 4330.0)


class InvalidStopTests(unittest.TestCase):
    def test_stop_not_directional(self):
        # Entry below sweep extreme with beyond_sweep → stop would be above entry for bullish? 
        # Bullish stop = extreme - buffer. If entry is below extreme, stop_not_directional.
        sweep = _sweep_low(extreme=4300.0)
        fvg = _fvg_bull()
        entry = _entry(mode="ce", direction="bullish", price=4290.0, ts=500)
        bars = [_bar(500, 4291, 4292, 4289, 4290)]
        plan = build_risk_plan(sweep, fvg, entry, bars, RiskConfig(stop_mode="beyond_sweep"))
        self.assertFalse(plan.valid)
        self.assertEqual(plan.invalidation_reason, "stop_not_directional")


class PreEntryInvalidationTests(unittest.TestCase):
    def test_invalidated_before_entry(self):
        sweep = _sweep_low(extreme=4300.0)
        fvg = _fvg_bull()
        entry = _entry(mode="ce", direction="bullish", price=4327.5, ts=600)
        bars = [
            _bar(300, 4330, 4340, 4325, 4335),
            _bar(400, 4330, 4331, 4299, 4305),  # violates stop 4300 before entry
            _bar(600, 4328, 4329, 4327.5, 4328),
        ]
        plan = build_risk_plan(
            sweep,
            fvg,
            entry,
            bars,
            RiskConfig(stop_mode="beyond_sweep", invalidate_before_entry=True),
        )
        self.assertFalse(plan.valid)
        self.assertEqual(plan.invalidation_reason, "invalidated_before_entry")
        self.assertEqual(plan.extras.get("invalidation_timestamp"), 400)

    def test_full_fill_does_not_auto_invalidate(self):
        sweep = _sweep_low(extreme=4300.0)
        fvg = _fvg_bull()
        entry = _entry(mode="ce", direction="bullish", price=4327.5, ts=500)
        entry = EntryCandidate(
            **{
                **entry.to_dict(),
                "extras": {"fully_filled_before_or_at_entry": True},
            }
        )
        # Fill FVG (trade to 4325) but do not breach stop at 4300
        bars = [
            _bar(300, 4330, 4340, 4325, 4335),
            _bar(400, 4335, 4336, 4325, 4326),  # full fill of FVG only
            _bar(500, 4328, 4329, 4327.5, 4328),
        ]
        plan = build_risk_plan(sweep, fvg, entry, bars, RiskConfig(stop_mode="beyond_sweep"))
        self.assertTrue(plan.valid)
        self.assertTrue(plan.extras.get("fully_filled_before_or_at_entry"))


class TargetTests(unittest.TestCase):
    def test_fixed_rr_bullish(self):
        sweep = _sweep_low(extreme=4300.0)
        fvg = _fvg_bull()
        entry = _entry(mode="ce", direction="bullish", price=4327.5, ts=500)
        bars = [_bar(500, 4328, 4329, 4327.5, 4328)]
        risk = build_risk_plan(sweep, fvg, entry, bars)
        session = _session_asia(high=4360.0, low=4311.0)
        targets = build_target_plan(session, sweep, entry, risk)
        self.assertTrue(targets.valid)
        by_rr = {t.rr: t for t in targets.fixed_rr_targets}
        self.assertAlmostEqual(by_rr[1.0].price, 4327.5 + 27.5)
        self.assertAlmostEqual(by_rr[2.0].price, 4327.5 + 55.0)
        self.assertAlmostEqual(by_rr[3.0].price, 4327.5 + 82.5)

    def test_opposite_liquidity_bullish(self):
        sweep = _sweep_low(extreme=4300.0)
        session = _session_asia(high=4360.0, low=4311.0)
        label, price, err = opposite_liquidity_target(session, sweep)
        self.assertIsNone(err)
        self.assertEqual(label, "Asia High")
        self.assertEqual(price, 4360.0)

        fvg = _fvg_bull()
        entry = _entry(mode="ce", direction="bullish", price=4327.5, ts=500)
        risk = build_risk_plan(sweep, fvg, entry, [_bar(500, 4328, 4329, 4327.5, 4328)])
        targets = build_target_plan(session, sweep, entry, risk)
        self.assertTrue(targets.opposite_target_valid)
        self.assertEqual(targets.opposite_liquidity_label, "Asia High")
        self.assertEqual(targets.opposite_liquidity_price, 4360.0)
        # reward=32.5, risk=27.5 → RR ≈ 1.1818
        self.assertAlmostEqual(targets.rr_to_opposite, 32.5 / 27.5, places=5)

    def test_opposite_liquidity_bearish(self):
        sweep = _sweep_high(extreme=4400.0)
        session = SessionRange(
            name="London",
            timezone="America/New_York",
            start=0,
            end=100,
            high=4380.0,
            low=4320.0,
            high_timestamp=None,
            low_timestamp=None,
            complete=True,
            source="ict_sessions",
            coverage_status="full",
        )
        label, price, err = opposite_liquidity_target(session, sweep)
        self.assertEqual(label, "London Low")
        self.assertEqual(price, 4320.0)

        fvg = _fvg_bear()
        entry = _entry(mode="ce", direction="bearish", price=4350.0, ts=500)
        risk = build_risk_plan(sweep, fvg, entry, [_bar(500, 4349, 4351, 4348, 4350)])
        targets = build_target_plan(session, sweep, entry, risk)
        self.assertTrue(targets.opposite_target_valid)
        self.assertEqual(targets.opposite_liquidity_price, 4320.0)
        # reward=30, risk=50 → 0.6
        self.assertAlmostEqual(targets.rr_to_opposite, 30.0 / 50.0, places=5)

    def test_invalid_opposing_behind_entry(self):
        sweep = _sweep_low(extreme=4300.0)
        # Session high already below entry → no reward
        session = _session_asia(high=4320.0, low=4311.0)
        fvg = _fvg_bull()
        entry = _entry(mode="ce", direction="bullish", price=4327.5, ts=500)
        risk = build_risk_plan(sweep, fvg, entry, [_bar(500, 4328, 4329, 4327.5, 4328)])
        targets = build_target_plan(session, sweep, entry, risk)
        self.assertTrue(targets.valid)  # fixed RR still ok
        self.assertFalse(targets.opposite_target_valid)
        self.assertIsNone(targets.rr_to_opposite)


class MultiModeTests(unittest.TestCase):
    def test_boundary_vs_ce_different_risk(self):
        sweep = _sweep_low(extreme=4300.0)
        fvg = _fvg_bull()
        bars = [_bar(500, 4330, 4331, 4327.5, 4328)]
        boundary = _entry(mode="boundary", direction="bullish", price=4330.0, ts=500, depth=0.0)
        ce = _entry(mode="ce", direction="bullish", price=4327.5, ts=500, depth=0.5)
        rb = build_risk_plan(sweep, fvg, boundary, bars)
        rc = build_risk_plan(sweep, fvg, ce, bars)
        self.assertTrue(rb.valid and rc.valid)
        self.assertNotEqual(rb.entry_price, rc.entry_price)
        self.assertNotEqual(rb.risk_distance, rc.risk_distance)
        session = _session_asia(high=4360.0)
        tb = build_target_plan(session, sweep, boundary, rb)
        tc = build_target_plan(session, sweep, ce, rc)
        self.assertNotEqual(tb.fixed_rr_targets[0].price, tc.fixed_rr_targets[0].price)
        self.assertNotEqual(tb.rr_to_opposite, tc.rr_to_opposite)


if __name__ == "__main__":
    unittest.main()
