"""Unit tests for Phase 7 setup orchestration (no CDP)."""

from __future__ import annotations

import unittest

from models import (
    Bar,
    SessionRange,
    SetupStatus,
    StructureConfirmation,
)
from setup_engine import analyze_session_setup, make_setup_id
from strategy_config import DEFAULT_STRATEGY_CONFIG


def _bar(ts: int, o: float, h: float, l: float, c: float) -> Bar:
    return Bar(time=ts, open=o, high=h, low=l, close=c)


def _session(
    *,
    name: str = "Asia",
    high: float = 4360.0,
    low: float = 4311.04,
    complete: bool = True,
    coverage: str = "full",
    start: int = 0,
    end: int = 900,
    source: str = "ict_sessions",
) -> SessionRange:
    return SessionRange(
        name=name,
        timezone="America/New_York",
        start=start,
        end=end,
        high=high,
        low=low,
        high_timestamp=None,
        low_timestamp=None,
        complete=complete,
        source=source,  # type: ignore[arg-type]
        coverage_status=coverage,
        identity=f"{name}:{start}",
        extras={"resolved_window": {"trading_date": "2026-08-14"}},
    )


def _choch(direction: str, ts: int, level: float = 4320.0, timing: str = "exact"):
    return StructureConfirmation(
        kind="CHoCH",
        direction=direction,
        level=level,
        event_timestamp=ts,
        event_bar_index=None,
        source="luxalgo",
        study_id="smUEv2",
        raw_id="t",
        timing_confidence=timing,
    )


def _full_bullish_bars():
    # Session low 4311.04; wick sweep at t=1000 to 4310 then reclaim
    return [
        _bar(500, 4320, 4325, 4315, 4322),
        _bar(1000, 4312, 4313, 4310, 4312),  # low sweep wick
        _bar(2000, 4315, 4322, 4314, 4320),  # CHoCH time
        _bar(3000, 4321, 4325, 4320, 4324),  # FVG c1
        _bar(4000, 4324, 4340, 4323, 4338),
        _bar(5000, 4338, 4345, 4330, 4342),  # FVG create 4325-4330
        _bar(6000, 4342, 4343, 4328, 4329),  # retrace
        _bar(7000, 4329, 4330, 4326, 4327),  # CE
    ]


class OrchestrationProgressTests(unittest.TestCase):
    def test_incomplete_session(self):
        s = _session(complete=False, coverage="partial_start", source="internal_ohlc")
        setup = analyze_session_setup(s, [], [], symbol="XAUUSD", timeframe="15")
        self.assertEqual(setup.status, SetupStatus.WAITING_FOR_SESSION.value)

    def test_no_sweep(self):
        s = _session()
        bars = [_bar(1000, 4320, 4330, 4320, 4325)]  # never takes low/high
        setup = analyze_session_setup(s, bars, [], symbol="XAUUSD", timeframe="15")
        self.assertEqual(setup.status, SetupStatus.WAITING_FOR_SWEEP.value)
        self.assertIn("neither", setup.source_metadata.get("reason", "").lower())

    def test_sweep_no_choch(self):
        s = _session()
        bars = [_bar(1000, 4312, 4313, 4310, 4312)]
        setup = analyze_session_setup(s, bars, [], symbol="XAUUSD", timeframe="15")
        self.assertEqual(setup.status, SetupStatus.WAITING_FOR_CONFIRMATION.value)
        self.assertEqual(setup.direction, "bullish")
        self.assertIsNotNone(setup.sweep)

    def test_sweep_wrong_choch_direction(self):
        s = _session()
        bars = [_bar(1000, 4312, 4313, 4310, 4312), _bar(2000, 4315, 4320, 4314, 4318)]
        # Bearish CHoCH after low sweep is ignored by confirm_after_sweep
        setup = analyze_session_setup(
            s, bars, [_choch("bearish", 2000)], symbol="XAUUSD", timeframe="15"
        )
        self.assertEqual(setup.status, SetupStatus.WAITING_FOR_CONFIRMATION.value)

    def test_unreliable_choch_fail_closed(self):
        s = _session()
        bars = [_bar(1000, 4312, 4313, 4310, 4312)]
        # Unavailable CHoCH cannot confirm after sweep
        setup = analyze_session_setup(
            s,
            bars,
            [_choch("bullish", 2000, timing="unavailable")],
            symbol="XAUUSD",
            timeframe="15",
        )
        # Without usable timestamp, confirm_after_sweep rejects → waiting
        self.assertEqual(setup.status, SetupStatus.WAITING_FOR_CONFIRMATION.value)

    def test_waiting_for_fvg(self):
        s = _session()
        bars = [
            _bar(1000, 4312, 4313, 4310, 4312),
            _bar(2000, 4315, 4322, 4314, 4320),
            _bar(3000, 4320, 4322, 4319, 4321),
            _bar(4000, 4321, 4323, 4320, 4322),
            _bar(5000, 4322, 4324, 4321, 4323),  # no gap
        ]
        setup = analyze_session_setup(
            s, bars, [_choch("bullish", 2000)], symbol="XAUUSD", timeframe="15"
        )
        self.assertEqual(setup.status, SetupStatus.WAITING_FOR_FVG.value)
        self.assertIsNotNone(setup.confirmation)

    def test_waiting_for_retrace(self):
        s = _session()
        bars = [
            _bar(1000, 4312, 4313, 4310, 4312),
            _bar(2000, 4315, 4322, 4314, 4320),
            _bar(3000, 4321, 4325, 4320, 4324),
            _bar(4000, 4324, 4340, 4323, 4338),
            _bar(5000, 4338, 4345, 4330, 4342),
            _bar(6000, 4342, 4350, 4341, 4348),  # never retraces into FVG
        ]
        setup = analyze_session_setup(
            s, bars, [_choch("bullish", 2000)], symbol="XAUUSD", timeframe="15"
        )
        self.assertEqual(setup.status, SetupStatus.WAITING_FOR_RETRACE.value)
        self.assertIsNotNone(setup.fvg)


class FullSequenceTests(unittest.TestCase):
    def test_full_bullish_entry_ready(self):
        s = _session()
        bars = _full_bullish_bars()
        setup = analyze_session_setup(
            s, bars, [_choch("bullish", 2000)], symbol="OANDA:XAUUSD", timeframe="15"
        )
        self.assertEqual(setup.status, SetupStatus.ENTRY_READY.value)
        self.assertEqual(setup.direction, "bullish")
        self.assertIsNotNone(setup.fvg)
        triggered = [e for e in setup.entries if e.entry.triggered and e.risk and e.risk.valid]
        self.assertGreaterEqual(len(triggered), 1)
        # Independent risk per mode
        modes = {e.entry.mode: e for e in triggered}
        if "boundary" in modes and "ce" in modes:
            self.assertNotEqual(
                modes["boundary"].risk.risk_distance, modes["ce"].risk.risk_distance
            )
        self.assertIn("ENTRY_READY", setup.explanation)
        self.assertIn("Asia", setup.explanation)

    def test_full_bearish_entry_ready(self):
        s = _session(name="London", high=4380.0, low=4320.0)
        bars = [
            _bar(1000, 4375, 4385, 4370, 4378),  # high sweep wick above 4380
            _bar(2000, 4370, 4375, 4360, 4365),  # bearish CHoCH
            _bar(3000, 4364, 4365, 4360, 4361),  # c1 low=4360
            _bar(4000, 4361, 4362, 4350, 4352),
            _bar(5000, 4352, 4355, 4348, 4350),  # c3 high=4355 < 4360 → bearish FVG
            _bar(6000, 4350, 4358, 4349, 4356),  # retrace into zone
        ]
        # Session high 4380; need wick high > 4380 and close <= 4380
        bars[0] = _bar(1000, 4378, 4384, 4375, 4379)
        setup = analyze_session_setup(
            s, bars, [_choch("bearish", 2000, level=4365)], symbol="XAUUSD", timeframe="15"
        )
        self.assertEqual(setup.status, SetupStatus.ENTRY_READY.value)
        self.assertEqual(setup.direction, "bearish")

    def test_deterministic_setup_id(self):
        s = _session()
        bars = _full_bullish_bars()
        choch = [_choch("bullish", 2000)]
        a = analyze_session_setup(s, bars, choch, symbol="XAUUSD", timeframe="15")
        b = analyze_session_setup(s, bars, choch, symbol="XAUUSD", timeframe="15")
        self.assertEqual(a.id, b.id)
        self.assertEqual(
            a.id,
            make_setup_id(
                symbol="XAUUSD",
                session="Asia",
                trading_date="2026-08-14",
                sweep_side="low",
                sweep_timestamp=a.sweep["sweep_timestamp"],
                execution_timeframe=a.execution_timeframe or "5m",
            ),
        )

    def test_invalid_risk_blocks_entry_ready(self):
        # Negative buffer pushes bullish stop above entry → all risks invalid.
        from models import RiskConfig
        from strategy_config import StrategyConfig

        s = _session()
        bars = _full_bullish_bars()
        bad = StrategyConfig(
            sweep_rule=DEFAULT_STRATEGY_CONFIG.sweep_rule,
            entry_modes=DEFAULT_STRATEGY_CONFIG.entry_modes,
            fvg=DEFAULT_STRATEGY_CONFIG.fvg,
            entry=DEFAULT_STRATEGY_CONFIG.entry,
            risk=RiskConfig(
                stop_mode="beyond_sweep",
                stop_buffer_price=-500.0,  # stop = extreme - (-500) = extreme+500
                invalidate_before_entry=True,
            ),
            target=DEFAULT_STRATEGY_CONFIG.target,
            prefer_completed_sessions_only=True,
            session_confidence=dict(DEFAULT_STRATEGY_CONFIG.session_confidence),
            dst_uncertainty=DEFAULT_STRATEGY_CONFIG.dst_uncertainty,
        )
        setup = analyze_session_setup(
            s, bars, [_choch("bullish", 2000)], bad, symbol="XAUUSD", timeframe="15"
        )
        self.assertEqual(setup.status, SetupStatus.INVALIDATED.value)
        self.assertIsNotNone(setup.invalidation_reason)


if __name__ == "__main__":
    unittest.main()
