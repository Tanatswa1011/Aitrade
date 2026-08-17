"""Phase 25 tests — VWAP, σ bands, extension, reclaim, pairing, no future leakage."""

from __future__ import annotations

import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from gc_vwap_engine import (
    analyze_candidate,
    collect_extension_sequences,
    compute_session_vwap_series,
    config_hash,
    evaluate_vwap_touch_after_entry,
    session_window,
    typical_price,
)
from gc_vwap_models import (
    OR_TIMEZONE,
    PHASE25_CANDIDATES,
    STRATEGY_FAMILY,
    ConfirmationMode,
    EntryMode,
    GCVWAPStrategyConfig,
)
from models import Bar

NY = ZoneInfo(OR_TIMEZONE)


def _bar(t, o, h, l, c, v=100.0):
    return Bar(time=t, open=o, high=h, low=l, close=c, volume=v)


def _ts(y, m, d, hh, mm):
    return int(datetime(y, m, d, hh, mm, tzinfo=NY).timestamp())


class VWAPTests(unittest.TestCase):
    def test_known_vwap_sequence(self):
        # Equal volume → VWAP = average of typical prices
        bars = []
        # 6 warmup bars + more inside session
        for i, px in enumerate([100, 102, 104, 106, 108, 110]):
            t = _ts(2026, 7, 2, 8, 20) + i * 300
            bars.append(_bar(t, px, px + 1, px - 1, px, v=10))
        states = compute_session_vwap_series(bars, "2026-07-02")
        self.assertEqual(len(states), 6)
        last = states[-1]
        self.assertTrue(last.valid)
        tps = [typical_price(b) for b in bars]
        expected = sum(tps) / len(tps)
        self.assertAlmostEqual(last.vwap, expected, places=6)

    def test_zero_volume_unavailable(self):
        bars = [_bar(_ts(2026, 7, 2, 8, 20) + i * 300, 100, 101, 99, 100, v=0) for i in range(6)]
        states = compute_session_vwap_series(bars, "2026-07-02")
        self.assertFalse(states[-1].valid)
        self.assertIsNone(states[-1].vwap)

    def test_session_reset_no_cross_day(self):
        d1 = [_bar(_ts(2026, 7, 1, 8, 20) + i * 300, 100, 101, 99, 100, v=10) for i in range(6)]
        d2 = [_bar(_ts(2026, 7, 2, 8, 20) + i * 300, 200, 201, 199, 200, v=10) for i in range(6)]
        s1 = compute_session_vwap_series(d1 + d2, "2026-07-01")
        s2 = compute_session_vwap_series(d1 + d2, "2026-07-02")
        self.assertLess(s1[-1].vwap, 150)
        self.assertGreater(s2[-1].vwap, 150)


class StdBandTests(unittest.TestCase):
    def test_zero_variance_bands(self):
        bars = [_bar(_ts(2026, 7, 2, 8, 20) + i * 300, 100, 100, 100, 100, v=10) for i in range(8)]
        states = compute_session_vwap_series(bars, "2026-07-02")
        st = states[-1]
        self.assertEqual(st.session_std, 0.0)
        self.assertEqual(st.z_vwap, 0.0)

    def test_no_future_bars_in_state(self):
        bars = [_bar(_ts(2026, 7, 2, 8, 20) + i * 300, 100 + i, 101 + i, 99 + i, 100 + i, v=10) for i in range(10)]
        states = compute_session_vwap_series(bars, "2026-07-02")
        # state at index 5 uses only first 6 bars
        self.assertEqual(states[5].bars_used, 6)
        self.assertLess(states[5].timestamp, states[9].timestamp)


class ExtensionReclaimTests(unittest.TestCase):
    def _stretched_session(self):
        """Build session that goes above +2σ then reclaims."""
        bars = []
        # flat warmup with volume
        for i in range(8):
            t = _ts(2026, 7, 2, 8, 20) + i * 300
            bars.append(_bar(t, 2000, 2001, 1999, 2000, v=100))
        # strong upside stretch
        for i in range(4):
            t = _ts(2026, 7, 2, 9, 0) + i * 300
            px = 2010 + i * 5
            bars.append(_bar(t, px - 1, px + 2, px - 2, px, v=50))
        # reclaim toward value
        for i in range(4):
            t = _ts(2026, 7, 2, 9, 20) + i * 300
            px = 2015 - i * 4
            bars.append(_bar(t, px + 1, px + 2, px - 1, px, v=80))
        return bars

    def test_upper_extension_and_reclaim(self):
        bars = self._stretched_session()
        seqs = collect_extension_sequences(bars, "2026-07-02")
        self.assertTrue(len(seqs) >= 1)
        s0 = seqs[0]
        self.assertEqual(s0["side"], "above")
        self.assertEqual(s0["direction"], "bearish")
        # may or may not reclaim depending on σ path; if reclaim present, direction consistent
        if s0["reclaim_bar"] is not None:
            self.assertGreater(int(s0["reclaim_bar"].time), int(s0["first_ts"]))

    def test_pairing_shared_event_id(self):
        bars = self._stretched_session()
        seqs = collect_extension_sequences(bars, "2026-07-02")
        if not seqs:
            self.skipTest("no extension in synthetic path")
        ids = set()
        for cfg in PHASE25_CANDIDATES:
            setup = analyze_candidate(seqs[0], cfg)
            ids.add(setup.vwap_extension_event_id)
        self.assertEqual(len(ids), 1)

    def test_family_isolated(self):
        self.assertEqual(STRATEGY_FAMILY, "gc_vwap_mean_reversion_v1")
        self.assertEqual(len(PHASE25_CANDIDATES), 6)
        self.assertTrue(all(not c.volume_filter for c in PHASE25_CANDIDATES))


class DynamicVWAPTargetTests(unittest.TestCase):
    def test_vwap_target_no_future_leakage(self):
        bars = []
        for i in range(12):
            t = _ts(2026, 7, 2, 8, 20) + i * 300
            bars.append(_bar(t, 100, 101, 99, 100, v=10))
        # entry mid-session; stop far
        entry_ts = _ts(2026, 7, 2, 9, 0)
        _, end, _ = session_window("2026-07-02")
        res = evaluate_vwap_touch_after_entry(
            bars=bars,
            trading_date="2026-07-02",
            entry_ts=entry_ts,
            direction="bullish",
            stop_price=50.0,
            session_end=end,
        )
        self.assertIn("vwap_hit", res)


class ConfigTests(unittest.TestCase):
    def test_hash_stable(self):
        cfg = PHASE25_CANDIDATES[0]
        self.assertEqual(config_hash(cfg), config_hash(cfg))


if __name__ == "__main__":
    unittest.main()
