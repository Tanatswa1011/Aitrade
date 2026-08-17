"""Phase 11 tests: MTF bias architecture, execution TF, closed candles."""

from __future__ import annotations

import unittest
from datetime import date

from bias_models import (
    BiasDirection,
    HtfAlignment,
    compute_htf_alignment,
    setup_vs_bias,
)
from bias_provider import ManualBiasProvider, UnknownBiasProvider
from closed_candles import filter_closed_bars, latest_closed_bar
from confirmation_provider import LuxAlgoLiveProvider
from execution_config import ExecutionTimeframeConfig, ExecutionTimeframeConfigError
from expiry_config import ExpiryConfig
from models import Bar, SessionRange, SetupStatus, StructureConfirmation
from multi_tf_bars import MultiTimeframeBars
from ohlc_resample import resample_ohlc
from session_time import resolve_session_window
from sessions_config import SESSION_DEFINITIONS
from setup_engine import analyze_session_setup
from strategy_config import DEFAULT_STRATEGY_CONFIG, StrategyConfig
from timeframe import normalize_timeframe


def _choch(direction: str, ts: int, level: float = 4320.0):
    return StructureConfirmation(
        kind="CHoCH",
        direction=direction,
        level=level,
        event_timestamp=ts,
        event_bar_index=None,
        source="test",
        study_id="t",
        raw_id="t",
        timing_confidence="exact",
    )


def _asia() -> SessionRange:
    w = resolve_session_window(SESSION_DEFINITIONS["Asia"], date(2026, 8, 14))
    return SessionRange(
        name="Asia",
        timezone="America/New_York",
        start=w.utc_start,
        end=w.utc_end,
        high=4360.0,
        low=4311.04,
        high_timestamp=None,
        low_timestamp=None,
        complete=True,
        source="ict_sessions",
        coverage_status="full",
        identity="Asia:2026-08-14",
        extras={"resolved_window": w.to_dict()},
    )


def _cfg(execution_tf: str = "5m") -> StrategyConfig:
    return StrategyConfig(
        sweep_rule=DEFAULT_STRATEGY_CONFIG.sweep_rule,
        entry_modes=DEFAULT_STRATEGY_CONFIG.entry_modes,
        fvg=DEFAULT_STRATEGY_CONFIG.fvg,
        entry=DEFAULT_STRATEGY_CONFIG.entry,
        risk=DEFAULT_STRATEGY_CONFIG.risk,
        target=DEFAULT_STRATEGY_CONFIG.target,
        expiry=ExpiryConfig(enabled=False),
        execution=ExecutionTimeframeConfig(timeframe=execution_tf),
    )


class TimeframeNormalizeTests(unittest.TestCase):
    def test_aliases(self):
        self.assertEqual(normalize_timeframe("5"), "5m")
        self.assertEqual(normalize_timeframe("15"), "15m")
        self.assertEqual(normalize_timeframe("240"), "4H")
        self.assertEqual(normalize_timeframe("1D"), "1D")
        self.assertEqual(normalize_timeframe("D"), "1D")


class BiasAlignmentTests(unittest.TestCase):
    def test_aligned_bullish(self):
        self.assertEqual(
            compute_htf_alignment("bullish", "bullish"),
            HtfAlignment.ALIGNED_BULLISH.value,
        )

    def test_aligned_bearish(self):
        self.assertEqual(
            compute_htf_alignment("bearish", "bearish"),
            HtfAlignment.ALIGNED_BEARISH.value,
        )

    def test_mixed(self):
        self.assertEqual(
            compute_htf_alignment("bullish", "bearish"),
            HtfAlignment.MIXED.value,
        )

    def test_setup_vs_htf(self):
        self.assertEqual(setup_vs_bias("bullish", "bullish"), "aligned")
        self.assertEqual(setup_vs_bias("bearish", "bullish"), "opposed")
        self.assertEqual(setup_vs_bias("bullish", "unknown"), "unknown")


class ExecutionConfigTests(unittest.TestCase):
    def test_valid_equal(self):
        cfg = ExecutionTimeframeConfig(timeframe="15m")
        self.assertEqual(cfg.confirmation_timeframe, "15m")
        self.assertEqual(cfg.entry_timeframe, "15m")

    def test_reject_mixed(self):
        with self.assertRaises(ExecutionTimeframeConfigError):
            ExecutionTimeframeConfig(
                timeframe="15m",
                confirmation_timeframe="15m",
                entry_timeframe="5m",
            )


class ClosedCandleTests(unittest.TestCase):
    def test_no_lookahead(self):
        # 4H bars: open at t, close at t+14400
        bars = [
            Bar(time=1000, open=1, high=2, low=0.5, close=1.5),
            Bar(time=15400, open=1.5, high=3, low=1, close=2.5),  # closes 29800
        ]
        as_of = 15400 + 100  # during second bar — only first closed
        closed = filter_closed_bars(bars, as_of_ts=as_of, timeframe="4H")
        self.assertEqual(len(closed), 1)
        self.assertEqual(closed[0].time, 1000)
        latest = latest_closed_bar(bars, as_of_ts=as_of, timeframe="4H")
        self.assertEqual(latest.time, 1000)
        # After second closes
        closed2 = filter_closed_bars(bars, as_of_ts=15400 + 14400, timeframe="4H")
        self.assertEqual(len(closed2), 2)


class ManualBiasSetupTests(unittest.TestCase):
    def test_example_a_aligned_bullish_5m(self):
        s = _asia()
        t0 = int(s.end) + 60
        bars = [
            Bar(time=t0, open=4312, high=4313, low=4310, close=4312),
            Bar(time=t0 + 60, open=4315, high=4322, low=4314, close=4320),
            Bar(time=t0 + 120, open=4321, high=4325, low=4320, close=4324),
            Bar(time=t0 + 180, open=4324, high=4340, low=4323, close=4338),
            Bar(time=t0 + 240, open=4338, high=4345, low=4330, close=4342),
            Bar(time=t0 + 300, open=4342, high=4343, low=4328, close=4329),
            Bar(time=t0 + 360, open=4329, high=4330, low=4326, close=4327),
        ]
        # Closed HTF bars before sweep
        daily = [Bar(time=t0 - 200000, open=1, high=2, low=0.5, close=1.5)]
        h4 = [Bar(time=t0 - 20000, open=1, high=2, low=0.5, close=1.5)]
        mtf = (
            MultiTimeframeBars()
            .with_series("5m", bars, source="native")
            .with_series("1D", daily, source="native")
            .with_series("4H", h4, source="native")
        )
        provider = ManualBiasProvider(daily="bullish", h4="bullish")
        setup = analyze_session_setup(
            s,
            bars,
            [_choch("bullish", t0 + 60)],
            _cfg("5m"),
            symbol="OANDA:XAUUSD",
            timeframe="5m",
            now_ts=bars[-1].time,
            execution_timeframe="5m",
            bias_provider=provider,
            mtf_bars=mtf,
        )
        self.assertEqual(setup.status, SetupStatus.ENTRY_READY.value)
        self.assertEqual(setup.execution_timeframe, "5m")
        self.assertEqual(
            (setup.higher_timeframe_context or {}).get("alignment"),
            "aligned_bullish",
        )
        self.assertEqual(setup.setup_vs_daily, "aligned")
        self.assertEqual(setup.setup_vs_h4, "aligned")
        self.assertIn("Higher Timeframe Context", setup.explanation)
        self.assertIn("Bias: Bullish", setup.explanation)

    def test_example_b_mixed_not_rejected_15m(self):
        s = _asia()
        t0 = int(s.end) + 60
        # High sweep bearish path
        bars = [
            Bar(time=t0, open=4355, high=4362, low=4354, close=4356),
            Bar(time=t0 + 60, open=4356, high=4357, low=4348, close=4350),
            Bar(time=t0 + 120, open=4350, high=4351, low=4345, close=4346),
            Bar(time=t0 + 180, open=4346, high=4347, low=4320, close=4322),
            Bar(time=t0 + 240, open=4322, high=4325, low=4318, close=4320),
            Bar(time=t0 + 300, open=4320, high=4332, low=4319, close=4330),
        ]
        mtf = MultiTimeframeBars().with_series("15m", bars, source="native")
        provider = ManualBiasProvider(daily="bullish", h4="bearish")
        setup = analyze_session_setup(
            s,
            bars,
            [_choch("bearish", t0 + 60, level=4355.0)],
            _cfg("15m"),
            symbol="XAU",
            timeframe="15m",
            now_ts=bars[-1].time,
            execution_timeframe="15m",
            bias_provider=provider,
            mtf_bars=mtf,
        )
        self.assertEqual(setup.execution_timeframe, "15m")
        self.assertEqual(
            (setup.higher_timeframe_context or {}).get("alignment"), "mixed"
        )
        self.assertNotEqual(setup.status, SetupStatus.INVALIDATED.value)
        # Should not be rejected solely for mixed HTF
        self.assertFalse((setup.source_metadata or {}).get("htf_hard_filter"))
        if setup.direction == "bearish":
            self.assertEqual(setup.setup_vs_daily, "opposed")
            self.assertEqual(setup.setup_vs_h4, "aligned")

    def test_unknown_bias_still_works(self):
        s = _asia()
        bars = [
            Bar(time=int(s.end) + 60, open=4312, high=4313, low=4310, close=4312)
        ]
        setup = analyze_session_setup(
            s,
            bars,
            [],
            _cfg("5m"),
            symbol="XAU",
            timeframe="5m",
            now_ts=bars[-1].time,
            bias_provider=UnknownBiasProvider(),
        )
        self.assertEqual(setup.status, SetupStatus.WAITING_FOR_CONFIRMATION.value)
        self.assertEqual(
            (setup.higher_timeframe_context or {}).get("alignment"), "unknown"
        )
        self.assertIn("Bias: Unknown", setup.explanation)

    def test_htf_snapshot_uses_closed_only_in_manual_evidence(self):
        s = _asia()
        sweep_ts = int(s.end) + 60
        bars = [Bar(time=sweep_ts, open=4312, high=4313, low=4310, close=4312)]
        # Future 4H bar that has NOT closed at sweep time
        future_open = sweep_ts - 1000
        # close would be future_open+14400 > sweep_ts if future_open > sweep_ts-14400
        open_forming = sweep_ts - 100  # closes at sweep_ts - 100 + 14400 >> sweep
        h4 = [
            Bar(time=sweep_ts - 50000, open=1, high=2, low=0.5, close=1),  # closed
            Bar(time=open_forming, open=2, high=3, low=1.5, close=2.5),  # open
        ]
        mtf = MultiTimeframeBars().with_series("4H", h4, source="native")
        provider = ManualBiasProvider(daily="bullish", h4="bullish")
        setup = analyze_session_setup(
            s,
            bars,
            [],
            _cfg("5m"),
            symbol="XAU",
            timeframe="5m",
            now_ts=sweep_ts,
            bias_provider=provider,
            mtf_bars=mtf,
        )
        h4_bias = (setup.higher_timeframe_context or {}).get("h4_bias") or {}
        evidence = h4_bias.get("evidence") or {}
        self.assertEqual(evidence.get("closed_h4_bars_available"), 1)
        self.assertEqual(evidence.get("latest_closed_h4_ts"), sweep_ts - 50000)


class ResampleTests(unittest.TestCase):
    def test_resample_tagged(self):
        bars = [
            Bar(time=1_000_000 + i * 300, open=1, high=2, low=0.5, close=1.2)
            for i in range(12)
        ]
        series = resample_ohlc(bars, "15m", source_timeframe="5m", as_of_ts=1_000_000 + 3600)
        self.assertEqual(series.source, "resampled")
        self.assertEqual(series.timeframe, "15m")
        self.assertGreater(len(series.bars), 0)


if __name__ == "__main__":
    unittest.main()
