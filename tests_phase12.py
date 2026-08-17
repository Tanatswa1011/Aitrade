"""Phase 12 tests: HTF structure bias, no look-ahead, soft filter."""

from __future__ import annotations

import unittest
from datetime import date, timedelta

from bias_models import compute_htf_alignment
from bias_provider import ManualBiasProvider, StructureBiasProvider
from closed_candles import filter_closed_bars
from execution_config import ExecutionTimeframeConfig
from expiry_config import ExpiryConfig
from htf_bias_config import HTFBiasConfig
from htf_structure import compute_timeframe_structure_bias, detect_confirmed_swings
from models import Bar, SessionRange, SetupStatus, StructureConfirmation
from multi_tf_bars import MultiTimeframeBars
from session_time import resolve_session_window
from sessions_config import SESSION_DEFINITIONS
from setup_engine import analyze_session_setup
from strategy_config import DEFAULT_STRATEGY_CONFIG, StrategyConfig
from trading_day_config import (
    DEFAULT_TRADING_DAY_CONFIG,
    trading_day_close_utc,
    trading_day_open_utc,
)


def _weekdays_from(start: date, n: int) -> list[date]:
    out: list[date] = []
    d = start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def _bars_up_then_break() -> list[Bar]:
    """Build closed-style sequence with swing high then close break above."""
    seq = [90, 92, 94, 96, 98, 100, 98, 96, 94, 92, 95, 97, 99, 101, 103]
    out = []
    days = _weekdays_from(date(2026, 1, 5), len(seq))
    for i, px in enumerate(seq):
        out.append(
            Bar(
                time=trading_day_open_utc(DEFAULT_TRADING_DAY_CONFIG, days[i]),
                open=px - 0.5,
                high=px + 0.5,
                low=px - 1.0,
                close=px,
            )
        )
    return out


def _bars_down_then_break() -> list[Bar]:
    seq = [110, 108, 106, 104, 102, 100, 102, 104, 106, 108, 105, 103, 101, 99, 97]
    out = []
    t = 2_000_000
    for i, px in enumerate(seq):
        out.append(
            Bar(
                time=t + i * 14400,  # 4H spacing
                open=px + 0.5,
                high=px + 1.0,
                low=px - 0.5,
                close=px,
            )
        )
    return out


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


def _cfg(tf: str = "5m") -> StrategyConfig:
    return StrategyConfig(
        sweep_rule=DEFAULT_STRATEGY_CONFIG.sweep_rule,
        entry_modes=DEFAULT_STRATEGY_CONFIG.entry_modes,
        fvg=DEFAULT_STRATEGY_CONFIG.fvg,
        entry=DEFAULT_STRATEGY_CONFIG.entry,
        risk=DEFAULT_STRATEGY_CONFIG.risk,
        target=DEFAULT_STRATEGY_CONFIG.target,
        expiry=ExpiryConfig(enabled=False),
        execution=ExecutionTimeframeConfig(timeframe=tf),
        htf_bias=HTFBiasConfig(),
    )


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


class StructureBiasUnitTests(unittest.TestCase):
    def test_bullish_daily_close_break(self):
        bars = _bars_up_then_break()
        last_day = _weekdays_from(date(2026, 1, 5), len(bars))[-1]
        as_of = trading_day_close_utc(DEFAULT_TRADING_DAY_CONFIG, last_day)
        bias = compute_timeframe_structure_bias(
            bars, timeframe="1D", as_of_ts=as_of, config=HTFBiasConfig()
        )
        self.assertEqual(bias.direction, "bullish")
        self.assertIn("closed above", bias.evidence.get("reason", ""))
        self.assertIsNotNone(bias.evidence.get("last_break_timestamp"))
        self.assertIn(bias.confidence, ("high", "medium", "low"))

    def test_bearish_h4_close_break(self):
        bars = _bars_down_then_break()
        as_of = int(bars[-1].time) + 14400
        bias = compute_timeframe_structure_bias(
            bars, timeframe="4H", as_of_ts=as_of, config=HTFBiasConfig()
        )
        self.assertEqual(bias.direction, "bearish")
        self.assertIn("closed below", bias.evidence.get("reason", ""))

    def test_wick_only_does_not_count(self):
        # Swing high at 100, then wick above but close below
        seq = [90, 92, 94, 96, 98, 100, 98, 96, 94, 92]
        days = _weekdays_from(date(2026, 2, 2), len(seq) + 1)
        bars = []
        for i, px in enumerate(seq):
            bars.append(
                Bar(
                    time=trading_day_open_utc(DEFAULT_TRADING_DAY_CONFIG, days[i]),
                    open=px,
                    high=px + 0.3,
                    low=px - 0.3,
                    close=px,
                )
            )
        # wick-only break bar
        bars.append(
            Bar(
                time=trading_day_open_utc(DEFAULT_TRADING_DAY_CONFIG, days[len(seq)]),
                open=99,
                high=101.5,  # wick above 100.3-ish swing
                low=98,
                close=99.5,  # close NOT above swing high ~100.3
            )
        )
        as_of = trading_day_close_utc(DEFAULT_TRADING_DAY_CONFIG, days[len(seq)])
        bias = compute_timeframe_structure_bias(
            bars,
            timeframe="1D",
            as_of_ts=as_of,
            config=HTFBiasConfig(require_close_break=True, neutral_when_unclear=True),
        )
        self.assertIn(bias.direction, ("neutral", "unknown"))

    def test_insufficient_history_unknown(self):
        days = _weekdays_from(date(2026, 3, 2), 3)
        bars = [
            Bar(
                time=trading_day_open_utc(DEFAULT_TRADING_DAY_CONFIG, d),
                open=1,
                high=2,
                low=0.5,
                close=1,
            )
            for d in days
        ]
        as_of = trading_day_close_utc(DEFAULT_TRADING_DAY_CONFIG, days[-1]) + 86400 * 5
        bias = compute_timeframe_structure_bias(
            bars, timeframe="1D", as_of_ts=as_of
        )
        self.assertEqual(bias.direction, "unknown")

    def test_no_lookahead_forming_bar(self):
        bars = _bars_up_then_break()
        # as_of during the last bar's life — last bar must be excluded
        last = bars[-1]
        as_of = int(last.time) + 100  # not yet closed (needs next roll)
        closed = filter_closed_bars(bars, as_of_ts=as_of, timeframe="1D")
        self.assertNotIn(last.time, [b.time for b in closed])
        bias = compute_timeframe_structure_bias(
            bars, timeframe="1D", as_of_ts=as_of, config=HTFBiasConfig()
        )
        # Must not use last bar's close which is the bullish break close
        if bias.evidence.get("last_break_timestamp") is not None:
            self.assertLess(int(bias.evidence["last_break_timestamp"]), int(last.time))


class AlignmentTests(unittest.TestCase):
    def test_matrix(self):
        self.assertEqual(compute_htf_alignment("bullish", "bullish"), "aligned_bullish")
        self.assertEqual(compute_htf_alignment("bearish", "bearish"), "aligned_bearish")
        self.assertEqual(compute_htf_alignment("bullish", "bearish"), "mixed")
        self.assertEqual(compute_htf_alignment("bullish", "neutral"), "partial")
        self.assertEqual(compute_htf_alignment("unknown", "bullish"), "partial")


class SoftFilterSetupTests(unittest.TestCase):
    def test_aligned_and_opposed_progress(self):
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
        mtf = MultiTimeframeBars().with_series("5m", bars)
        aligned = analyze_session_setup(
            s,
            bars,
            [_choch("bullish", t0 + 60)],
            _cfg("5m"),
            symbol="XAU",
            timeframe="5m",
            now_ts=bars[-1].time,
            bias_provider=ManualBiasProvider(daily="bullish", h4="bullish"),
            mtf_bars=mtf,
        )
        opposed = analyze_session_setup(
            s,
            bars,
            [_choch("bullish", t0 + 60)],
            _cfg("5m"),
            symbol="XAU",
            timeframe="5m",
            now_ts=bars[-1].time,
            bias_provider=ManualBiasProvider(daily="bearish", h4="bearish"),
            mtf_bars=mtf,
        )
        self.assertEqual(aligned.status, SetupStatus.ENTRY_READY.value)
        self.assertEqual(opposed.status, SetupStatus.ENTRY_READY.value)
        self.assertEqual(opposed.setup_vs_daily, "opposed")
        self.assertEqual(opposed.setup_vs_h4, "opposed")

    def test_5m_vs_15m_consume_own_bars(self):
        s = _asia()
        t0 = int(s.end) + 60
        bars5 = [
            Bar(time=t0 + i * 300, open=4312, high=4313, low=4310, close=4312)
            for i in range(3)
        ]
        bars5[0] = Bar(time=t0, open=4312, high=4313, low=4310, close=4312)
        bars15 = [
            Bar(time=t0 + i * 900, open=4312, high=4313, low=4310, close=4312)
            for i in range(3)
        ]
        bars15[0] = Bar(time=t0, open=4312, high=4313, low=4310, close=4312)
        provider = ManualBiasProvider(daily="bullish", h4="bullish")
        a = analyze_session_setup(
            s,
            bars5,
            [],
            _cfg("5m"),
            symbol="XAU",
            timeframe="5m",
            now_ts=bars5[-1].time,
            execution_timeframe="5m",
            bias_provider=provider,
            mtf_bars=MultiTimeframeBars().with_series("5m", bars5),
        )
        b = analyze_session_setup(
            s,
            bars15,
            [],
            _cfg("15m"),
            symbol="XAU",
            timeframe="15m",
            now_ts=bars15[-1].time,
            execution_timeframe="15m",
            bias_provider=provider,
            mtf_bars=MultiTimeframeBars().with_series("15m", bars15),
        )
        self.assertEqual(a.execution_timeframe, "5m")
        self.assertEqual(b.execution_timeframe, "15m")
        self.assertEqual(a.source_metadata.get("bar_count"), len(bars5))
        self.assertEqual(b.source_metadata.get("bar_count"), len(bars15))


class StructureProviderTests(unittest.TestCase):
    def test_provider_builds_context(self):
        daily = _bars_up_then_break()
        h4 = _bars_down_then_break()
        last_day = _weekdays_from(date(2026, 1, 5), len(daily))[-1]
        as_of = max(
            trading_day_close_utc(DEFAULT_TRADING_DAY_CONFIG, last_day),
            int(h4[-1].time) + 14400,
        )
        ctx = StructureBiasProvider().get_context(
            as_of_ts=as_of, daily_bars=daily, h4_bars=h4
        )
        self.assertEqual(ctx.source_metadata.get("provider"), "structure_bias")
        self.assertEqual(ctx.daily_bias.direction, "bullish")
        self.assertEqual(ctx.h4_bias.direction, "bearish")
        self.assertEqual(ctx.alignment, "mixed")


if __name__ == "__main__":
    unittest.main()
