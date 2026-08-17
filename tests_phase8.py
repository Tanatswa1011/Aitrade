"""Unit tests for Phase 8 AnnotationPlan (no CDP / TradingView required)."""

from __future__ import annotations

import unittest

from annotation_plan import plan_annotations
from models import (
    Bar,
    SessionRange,
    SetupStatus,
    StructureConfirmation,
)
from setup_engine import analyze_session_setup


def _bar(ts, o, h, l, c):
    return Bar(time=ts, open=o, high=h, low=l, close=c)


def _session(**kwargs):
    base = dict(
        name="Asia",
        timezone="America/New_York",
        start=0,
        end=900,
        high=4360.0,
        low=4311.04,
        high_timestamp=None,
        low_timestamp=None,
        complete=True,
        source="ict_sessions",
        coverage_status="full",
        identity="Asia:0",
        extras={"resolved_window": {"trading_date": "2026-08-14"}},
    )
    base.update(kwargs)
    return SessionRange(**base)


def _choch(ts=2000):
    return StructureConfirmation(
        kind="CHoCH",
        direction="bullish",
        level=4320.0,
        event_timestamp=ts,
        event_bar_index=None,
        source="luxalgo",
        study_id="smUEv2",
        raw_id="t",
        timing_confidence="exact",
    )


def _full_bars():
    return [
        _bar(1000, 4312, 4313, 4310, 4312),
        _bar(2000, 4315, 4322, 4314, 4320),
        _bar(3000, 4321, 4325, 4320, 4324),
        _bar(4000, 4324, 4340, 4323, 4338),
        _bar(5000, 4338, 4345, 4330, 4342),
        _bar(6000, 4342, 4343, 4328, 4329),
        _bar(7000, 4329, 4330, 4326, 4327),
    ]


def _roles(plan):
    return [i.role for i in plan.items]


class PartialStateAnnotationTests(unittest.TestCase):
    def test_waiting_for_sweep(self):
        s = _session()
        setup = analyze_session_setup(
            s, [_bar(1000, 4320, 4330, 4320, 4325)], [], symbol="XAU", timeframe="15"
        )
        self.assertEqual(setup.status, SetupStatus.WAITING_FOR_SWEEP.value)
        plan = plan_annotations(setup)
        roles = _roles(plan)
        self.assertIn("session_high", roles)
        self.assertIn("session_low", roles)
        self.assertIn("status", roles)
        self.assertNotIn("sweep", roles)
        self.assertTrue(any("WAITING_FOR_SWEEP" in i.label for i in plan.items))

    def test_waiting_for_confirmation(self):
        s = _session()
        setup = analyze_session_setup(
            s, [_bar(1000, 4312, 4313, 4310, 4312)], [], symbol="XAU", timeframe="15"
        )
        self.assertEqual(setup.status, SetupStatus.WAITING_FOR_CONFIRMATION.value)
        plan = plan_annotations(setup)
        self.assertIn("sweep", _roles(plan))
        self.assertTrue(any("SWEPT" in i.label for i in plan.items))
        self.assertNotIn("choch", _roles(plan))

    def test_waiting_for_fvg(self):
        s = _session()
        bars = [
            _bar(1000, 4312, 4313, 4310, 4312),
            _bar(2000, 4315, 4322, 4314, 4320),
            _bar(3000, 4320, 4322, 4319, 4321),
            _bar(4000, 4321, 4323, 4320, 4322),
            _bar(5000, 4322, 4324, 4321, 4323),
        ]
        setup = analyze_session_setup(s, bars, [_choch()], symbol="XAU", timeframe="15")
        self.assertEqual(setup.status, SetupStatus.WAITING_FOR_FVG.value)
        plan = plan_annotations(setup)
        self.assertIn("choch", _roles(plan))
        self.assertNotIn("fvg_zone", _roles(plan))
        self.assertNotIn("fvg_high", _roles(plan))

    def test_waiting_for_retrace(self):
        s = _session()
        bars = [
            _bar(1000, 4312, 4313, 4310, 4312),
            _bar(2000, 4315, 4322, 4314, 4320),
            _bar(3000, 4321, 4325, 4320, 4324),
            _bar(4000, 4324, 4340, 4323, 4338),
            _bar(5000, 4338, 4345, 4330, 4342),
            _bar(6000, 4342, 4350, 4341, 4348),
        ]
        setup = analyze_session_setup(s, bars, [_choch()], symbol="XAU", timeframe="15")
        self.assertEqual(setup.status, SetupStatus.WAITING_FOR_RETRACE.value)
        plan = plan_annotations(setup)
        roles = _roles(plan)
        self.assertTrue("fvg_zone" in roles or "fvg_high" in roles)
        self.assertIn("fvg_ce", roles)
        self.assertFalse(any(r.startswith("entry:") for r in roles))


class EntryReadyAnnotationTests(unittest.TestCase):
    def test_entry_ready_all_modes(self):
        setup = analyze_session_setup(
            _session(), _full_bars(), [_choch()], symbol="XAU", timeframe="15"
        )
        self.assertEqual(setup.status, SetupStatus.ENTRY_READY.value)
        plan = plan_annotations(setup, entry_mode="all")
        roles = _roles(plan)
        self.assertIn("entry:boundary", roles)
        self.assertIn("entry:ce", roles)
        self.assertIn("entry:first_touch", roles)
        self.assertIn("stop", roles)
        self.assertTrue(any(r.startswith("target_rr:") for r in roles))
        self.assertIn("opposite_liquidity", roles)
        # Shared stop should be a single annotation
        self.assertEqual(sum(1 for r in roles if r == "stop"), 1)

    def test_boundary_only(self):
        setup = analyze_session_setup(
            _session(), _full_bars(), [_choch()], symbol="XAU", timeframe="15"
        )
        plan = plan_annotations(setup, entry_mode="boundary")
        roles = _roles(plan)
        self.assertIn("entry:boundary", roles)
        self.assertNotIn("entry:ce", roles)
        self.assertNotIn("entry:first_touch", roles)

    def test_ce_only(self):
        setup = analyze_session_setup(
            _session(), _full_bars(), [_choch()], symbol="XAU", timeframe="15"
        )
        plan = plan_annotations(setup, entry_mode="ce")
        self.assertIn("entry:ce", _roles(plan))
        self.assertNotIn("entry:boundary", _roles(plan))

    def test_hide_fixed_rr(self):
        setup = analyze_session_setup(
            _session(), _full_bars(), [_choch()], symbol="XAU", timeframe="15"
        )
        plan = plan_annotations(setup, show_fixed_rr=False)
        self.assertFalse(any(r.startswith("target_rr:") for r in _roles(plan)))

    def test_idempotent_plan_same_setup_id(self):
        setup = analyze_session_setup(
            _session(), _full_bars(), [_choch()], symbol="XAU", timeframe="15"
        )
        a = plan_annotations(setup)
        b = plan_annotations(setup)
        self.assertEqual(a.setup_id, b.setup_id)
        self.assertEqual(len(a.items), len(b.items))
        self.assertEqual([i.label for i in a.items], [i.label for i in b.items])

    def test_bullish_labels(self):
        setup = analyze_session_setup(
            _session(), _full_bars(), [_choch()], symbol="XAU", timeframe="15"
        )
        plan = plan_annotations(setup)
        self.assertEqual(plan.direction, "bullish")
        self.assertTrue(any("Bullish CHoCH" in i.label for i in plan.items))

    def test_bearish_setup_plan(self):
        s = _session(name="London", high=4380.0, low=4320.0)
        bars = [
            _bar(1000, 4378, 4384, 4375, 4379),
            _bar(2000, 4370, 4375, 4360, 4365),
            _bar(3000, 4364, 4365, 4360, 4361),
            _bar(4000, 4361, 4362, 4350, 4352),
            _bar(5000, 4352, 4355, 4348, 4350),
            _bar(6000, 4350, 4358, 4349, 4356),
        ]
        choch = StructureConfirmation(
            kind="CHoCH",
            direction="bearish",
            level=4365.0,
            event_timestamp=2000,
            event_bar_index=None,
            source="luxalgo",
            study_id="smUEv2",
            raw_id="b",
            timing_confidence="exact",
        )
        setup = analyze_session_setup(s, bars, [choch], symbol="XAU", timeframe="15")
        self.assertEqual(setup.status, SetupStatus.ENTRY_READY.value)
        plan = plan_annotations(setup)
        self.assertEqual(plan.direction, "bearish")
        self.assertTrue(any("Bearish CHoCH" in i.label for i in plan.items))
        self.assertTrue(any("London Low · Opposing Liquidity" in i.label for i in plan.items))

    def test_missing_optional_fields_skip(self):
        # Minimal dict setup — no inventing FVG
        setup = {
            "id": "x|Asia|d|low|1",
            "status": "WAITING_FOR_SWEEP",
            "direction": None,
            "session": "Asia",
            "session_range": {"high": 100.0, "low": 90.0},
            "sweep": None,
            "confirmation": None,
            "fvg": None,
            "entries": [],
            "source_metadata": {},
        }
        plan = plan_annotations(setup)
        self.assertIn("session_high", _roles(plan))
        self.assertNotIn("fvg_zone", _roles(plan))
        self.assertNotIn("choch", _roles(plan))


if __name__ == "__main__":
    unittest.main()
