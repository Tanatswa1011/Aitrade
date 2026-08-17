"""Phase 21 tests — liquidity reclaim sweep/confirm/entry/risk/split isolation."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from chrono_split import assert_no_split_leakage, chronological_split
from liquidity_reclaim_engine import (
    build_reclaim_risk,
    detect_first_penetration_sweep,
    find_confirmation,
    find_entry,
    find_reclaim,
    is_bearish_reclaim,
    is_bearish_sweep,
    is_bullish_reclaim,
    is_bullish_sweep,
    analyze_session_liquidity_reclaim,
    config_hash,
)
from liquidity_reclaim_models import (
    ConfirmationMode,
    EntryMode,
    PHASE21_CANDIDATES,
    ReclaimStrategyConfig,
    STRATEGY_FAMILY,
)
from liquidity_reclaim_replay import replay_liquidity_reclaim
from models import Bar, SessionRange


def _bar(t, o, h, l, c):
    return Bar(time=t, open=o, high=h, low=l, close=c)


def _session(high=110.0, low=100.0, start=0, end=1000):
    return SessionRange(
        name="Asia",
        timezone="America/New_York",
        start=start,
        end=end,
        high=high,
        low=low,
        high_timestamp=start,
        low_timestamp=start,
        complete=True,
        source="internal_ohlc",
        coverage_status="full",
        identity="Asia:0",
        extras={"resolved_window": {"trading_date": "2026-01-02"}},
    )


class SweepReclaimTests(unittest.TestCase):
    def test_bullish_same_candle_reclaim(self):
        level = 100.0
        bar = _bar(2000, 101, 102, 99, 101)  # low < L, close > L
        self.assertTrue(is_bullish_sweep(bar, level))
        self.assertTrue(is_bullish_reclaim(bar, level))
        rec = find_reclaim(
            side="low",
            level=level,
            sweep_bar=bar,
            bars_after_including_sweep=[bar],
            max_reclaim_bars=3,
        )
        self.assertIsNotNone(rec)
        self.assertEqual(rec["reclaim_bars_after_sweep"], 0)

    def test_bullish_sweep_without_reclaim(self):
        level = 100.0
        bar = _bar(2000, 101, 102, 99, 99.5)  # close still below L
        self.assertTrue(is_bullish_sweep(bar, level))
        self.assertFalse(is_bullish_reclaim(bar, level))
        rec = find_reclaim(
            side="low",
            level=level,
            sweep_bar=bar,
            bars_after_including_sweep=[bar, _bar(2300, 99, 99.8, 98, 99.2)],
            max_reclaim_bars=1,
        )
        self.assertIsNone(rec)

    def test_bearish_high_sweep_reclaim(self):
        level = 110.0
        bar = _bar(2000, 109, 111, 108, 109)  # high > H, close < H
        self.assertTrue(is_bearish_sweep(bar, level))
        self.assertTrue(is_bearish_reclaim(bar, level))

    def test_touch_without_penetration(self):
        self.assertFalse(is_bullish_sweep(_bar(1, 100, 101, 100, 100.5), 100.0))
        self.assertFalse(is_bearish_sweep(_bar(1, 110, 110, 109, 109.5), 110.0))

    def test_close_exactly_at_level_not_reclaim(self):
        self.assertFalse(is_bullish_reclaim(_bar(1, 101, 102, 99, 100.0), 100.0))
        self.assertFalse(is_bearish_reclaim(_bar(1, 109, 111, 108, 110.0), 110.0))

    def test_multiple_sweep_candles_first_only(self):
        session = _session()
        bars = [
            _bar(1500, 101, 102, 99.5, 101),
            _bar(1800, 101, 102, 98.0, 101),  # deeper later
        ]
        sw = detect_first_penetration_sweep(session, bars, side="low", search_from_ts=1000)
        self.assertEqual(sw["sweep_timestamp"], 1500)
        self.assertAlmostEqual(sw["sweep_extreme"], 99.5)


class ConfirmationTests(unittest.TestCase):
    def test_immediate_reclaim(self):
        cfg = ReclaimStrategyConfig(confirmation_mode=ConfirmationMode.IMMEDIATE_RECLAIM.value)
        sweep = _bar(2000, 101, 103, 99, 101.5)
        conf = find_confirmation(
            cfg=cfg, side="low", level=100.0, sweep_bar=sweep, reclaim_bar=sweep, bars=[sweep]
        )
        self.assertEqual(conf["confirmation_timestamp"], 2000)

    def test_confirmation_candle(self):
        cfg = ReclaimStrategyConfig(confirmation_mode=ConfirmationMode.CONFIRMATION_CANDLE.value)
        reclaim = _bar(2000, 101, 102, 99, 101)
        nxt = _bar(2300, 101, 104, 100.5, 103)  # bullish close > reclaim close
        conf = find_confirmation(
            cfg=cfg, side="low", level=100.0, sweep_bar=reclaim, reclaim_bar=reclaim, bars=[reclaim, nxt]
        )
        self.assertEqual(conf["confirmation_timestamp"], 2300)

    def test_sweep_candle_close_break(self):
        cfg = ReclaimStrategyConfig(
            confirmation_mode=ConfirmationMode.SWEEP_CANDLE_BREAK.value,
            break_mode="close_break",
        )
        sweep = _bar(2000, 101, 103, 99, 101)  # conf level = 103
        reclaim = sweep
        later = _bar(2600, 102, 104, 101, 103.5)
        conf = find_confirmation(
            cfg=cfg, side="low", level=100.0, sweep_bar=sweep, reclaim_bar=reclaim, bars=[sweep, later]
        )
        self.assertEqual(conf["confirmation_timestamp"], 2600)
        self.assertEqual(conf["confirmation_level"], 103.0)

    def test_wick_break_vs_close(self):
        cfg_w = ReclaimStrategyConfig(
            confirmation_mode=ConfirmationMode.SWEEP_CANDLE_BREAK.value,
            break_mode="wick_break",
        )
        cfg_c = ReclaimStrategyConfig(
            confirmation_mode=ConfirmationMode.SWEEP_CANDLE_BREAK.value,
            break_mode="close_break",
        )
        sweep = _bar(2000, 101, 103, 99, 101)
        later = _bar(2600, 102, 104, 101, 102.5)  # wick above 103, close below
        self.assertIsNotNone(
            find_confirmation(cfg=cfg_w, side="low", level=100, sweep_bar=sweep, reclaim_bar=sweep, bars=[sweep, later])
        )
        self.assertIsNone(
            find_confirmation(cfg=cfg_c, side="low", level=100, sweep_bar=sweep, reclaim_bar=sweep, bars=[sweep, later])
        )

    def test_confirmation_timeout(self):
        cfg = ReclaimStrategyConfig(
            confirmation_mode=ConfirmationMode.SWEEP_CANDLE_BREAK.value,
            max_confirmation_bars=1,
        )
        sweep = _bar(2000, 101, 103, 99, 101)
        # only one bar after reclaim and it does not break
        later = _bar(2300, 101, 102, 100, 101.5)
        self.assertIsNone(
            find_confirmation(cfg=cfg, side="low", level=100, sweep_bar=sweep, reclaim_bar=sweep, bars=[sweep, later])
        )


class EntryTests(unittest.TestCase):
    def test_confirmation_close(self):
        cfg = ReclaimStrategyConfig(entry_mode=EntryMode.CONFIRMATION_CLOSE.value)
        conf = _bar(3000, 102, 104, 101, 103)
        e = find_entry(cfg=cfg, side="low", level=100, sweep_bar=conf, confirmation_bar=conf, bars=[conf])
        self.assertTrue(e["triggered"])
        self.assertEqual(e["price"], 103)

    def test_liquidity_retest(self):
        cfg = ReclaimStrategyConfig(entry_mode=EntryMode.LIQUIDITY_RETEST.value, max_entry_bars=5)
        conf = _bar(3000, 102, 104, 101, 103)
        retest = _bar(3600, 101, 102, 99.5, 100.5)
        e = find_entry(
            cfg=cfg, side="low", level=100, sweep_bar=_bar(2000, 101, 103, 99, 101), confirmation_bar=conf, bars=[conf, retest]
        )
        self.assertTrue(e["triggered"])
        self.assertEqual(e["price"], 100.0)

    def test_sweep_midpoint(self):
        cfg = ReclaimStrategyConfig(entry_mode=EntryMode.SWEEP_MIDPOINT.value)
        sweep = _bar(2000, 101, 104, 98, 101)  # mid=101
        conf = _bar(3000, 102, 105, 101, 104)
        touch = _bar(3600, 102, 103, 100.5, 101.2)
        e = find_entry(cfg=cfg, side="low", level=100, sweep_bar=sweep, confirmation_bar=conf, bars=[conf, touch])
        self.assertTrue(e["triggered"])
        self.assertEqual(e["price"], 101.0)

    def test_not_triggered(self):
        cfg = ReclaimStrategyConfig(entry_mode=EntryMode.LIQUIDITY_RETEST.value, max_entry_bars=1)
        conf = _bar(3000, 102, 104, 101, 103)
        far = _bar(3300, 103, 105, 102, 104)
        e = find_entry(cfg=cfg, side="low", level=100, sweep_bar=conf, confirmation_bar=conf, bars=[conf, far])
        self.assertFalse(e["triggered"])


class RiskTests(unittest.TestCase):
    def test_beyond_sweep_valid(self):
        r = build_reclaim_risk(direction="bullish", entry_price=105, sweep_extreme=99)
        self.assertTrue(r.valid)
        self.assertEqual(r.stop_price, 99)

    def test_entry_already_beyond_sweep(self):
        r = build_reclaim_risk(direction="bullish", entry_price=98, sweep_extreme=99)
        self.assertFalse(r.valid)
        self.assertEqual(r.invalidation_reason, "entry_already_beyond_sweep_extreme")


class ReplayDeterminismTests(unittest.TestCase):
    def test_same_config_same_ids(self):
        session = _session()
        # Build a tiny synthetic series around session
        bars = [_bar(t, 105, 106, 104, 105) for t in range(0, 1000, 300)]
        bars += [
            _bar(1300, 101, 103, 99, 101.5),  # sweep+reclaim low
            _bar(1600, 102, 104, 101, 103.5),  # break high of sweep (103)
        ]
        cfg = ReclaimStrategyConfig(
            candidate_id="TEST",
            confirmation_mode=ConfirmationMode.IMMEDIATE_RECLAIM.value,
            entry_mode=EntryMode.CONFIRMATION_CLOSE.value,
        )
        a = analyze_session_liquidity_reclaim(session, bars, symbol="OANDA:XAUUSD", cfg=cfg, side="low")
        b = analyze_session_liquidity_reclaim(session, bars, symbol="OANDA:XAUUSD", cfg=cfg, side="low")
        self.assertEqual(a.setup_id, b.setup_id)
        self.assertEqual(config_hash(cfg), config_hash(cfg))
        self.assertEqual(a.event.liquidity_event_id, b.event.liquidity_event_id)


class SplitIsolationTests(unittest.TestCase):
    def test_chronological_no_leakage(self):
        rows = []
        for i, d in enumerate(["2025-09-01", "2025-10-01", "2026-01-01", "2026-06-01", "2026-07-01", "2026-08-01"]):
            rows.append(
                {
                    "trading_date": d,
                    "liquidity_event_id": f"id-{i}",
                    "sweep_timestamp": 1700000000 + i * 86400,
                }
            )
        train, hold, split = chronological_split(rows, train_fraction=0.7)
        assert_no_split_leakage(split)
        self.assertTrue(max(r["trading_date"] for r in train) < min(r["trading_date"] for r in hold))

    def test_candidates_frozen_count(self):
        self.assertLessEqual(len(PHASE21_CANDIDATES), 10)
        self.assertEqual(STRATEGY_FAMILY, "liquidity_reclaim_v1")
        ids = {c.candidate_id for c in PHASE21_CANDIDATES}
        self.assertEqual(len(ids), len(PHASE21_CANDIDATES))


if __name__ == "__main__":
    unittest.main()
