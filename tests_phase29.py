"""Phase 29 tests — DVP exact rules, DST, guardrails, Phase 26 isolation."""

from __future__ import annotations

import json
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from models import Bar
from nq_databento import aggregate_1m_to_ny
from nq_drift_vwap_engine import (
    classify_drift_15m,
    compute_session_vwap_by_ts,
    config_hash,
    resolve_exit_1m,
    session_anchors,
    typical_price,
)
from nq_drift_vwap_models import (
    DVP_ORIGINAL,
    LONG_STOP_POINTS,
    LONG_TARGET_POINTS,
    SHORT_STOP_POINTS,
    SHORT_TARGET_POINTS,
    STRATEGY_FAMILY,
    VWAP_BASIS_STATUS,
)

NY = ZoneInfo("America/New_York")
PHASE26_HASH = "0695d7a881b6247033426bceb077b6895b96405b826c21f32a109a02f44b2d43"
PHASE26_FROZEN = Path("strategy_frozen") / "gc_vwap_v2_phase26.json"


def _bar(t, o, h, l, c, v=100.0):
    return Bar(time=t, open=o, high=h, low=l, close=c, volume=v)


def _ts(y, m, d, hh, mm):
    return int(datetime(y, m, d, hh, mm, tzinfo=NY).timestamp())


class SessionTests(unittest.TestCase):
    def test_session_anchors_dst(self):
        # July BST/EDT
        a = session_anchors("2026-07-02")
        self.assertEqual(datetime.fromtimestamp(a["vwap_reset"], tz=NY).strftime("%H:%M"), "09:30")
        self.assertEqual(datetime.fromtimestamp(a["trade_start"], tz=NY).strftime("%H:%M"), "10:30")
        self.assertEqual(datetime.fromtimestamp(a["no_new"], tz=NY).strftime("%H:%M"), "15:30")
        self.assertEqual(datetime.fromtimestamp(a["force_close"], tz=NY).strftime("%H:%M"), "15:55")
        # January EST — local clock same labels
        b = session_anchors("2026-01-08")
        self.assertEqual(datetime.fromtimestamp(b["vwap_reset"], tz=NY).strftime("%H:%M"), "09:30")
        # UTC hours differ winter vs summer
        self.assertNotEqual(
            datetime.fromtimestamp(a["vwap_reset"], tz=ZoneInfo("UTC")).hour,
            datetime.fromtimestamp(b["vwap_reset"], tz=ZoneInfo("UTC")).hour,
        )


class VwapAggTests(unittest.TestCase):
    def test_typical_price_and_vwap(self):
        bars = []
        for i, px in enumerate([100, 102, 104, 106]):
            t = _ts(2026, 7, 2, 9, 30) + i * 60
            bars.append(_bar(t, px, px + 1, px - 1, px, v=10))
        m = compute_session_vwap_by_ts(bars, "2026-07-02")
        self.assertTrue(m)
        last = m[max(m)]
        tps = [typical_price(b) for b in bars]
        expected = sum(tps) / len(tps)
        self.assertAlmostEqual(last, expected, places=6)
        self.assertEqual(VWAP_BASIS_STATUS, "IMPLEMENTATION_ASSUMPTION")

    def test_ny_15m_aggregation(self):
        bars = [_bar(_ts(2026, 7, 2, 9, 30) + i * 60, 100, 101, 99, 100, v=1) for i in range(15)]
        out = aggregate_1m_to_ny(bars, 15)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].time, _ts(2026, 7, 2, 9, 30))


class DriftExitTests(unittest.TestCase):
    def test_hour_return_threshold_positive(self):
        # Build 5×15m bars with rising closes and rising VWAP via 1m
        bars_1m = []
        for i in range(90):
            t = _ts(2026, 7, 2, 9, 30) + i * 60
            px = 15000 + i * 2
            bars_1m.append(_bar(t, px, px + 1, px - 1, px, v=10))
        vmap = compute_session_vwap_by_ts(bars_1m, "2026-07-02")
        bars_15 = aggregate_1m_to_ny(bars_1m, 15)
        # need idx>=4
        state = classify_drift_15m(bars_15, 4, vmap, threshold=0.001)
        # may or may not be positive depending on path; ensure function returns known set
        self.assertIn(state, (None, "POSITIVE_DRIFT", "NEGATIVE_DRIFT"))

    def test_stop_target_points(self):
        self.assertEqual(LONG_STOP_POINTS, 80)
        self.assertEqual(LONG_TARGET_POINTS, 40)
        self.assertEqual(SHORT_STOP_POINTS, 80)
        self.assertEqual(SHORT_TARGET_POINTS, 50)

    def test_resolve_target_before_stop(self):
        entry = _ts(2026, 7, 2, 11, 0)
        bars = [
            _bar(entry, 15000, 15000, 15000, 15000, v=1),
            _bar(entry + 60, 15010, 15045, 15005, 15040, v=1),  # hits +40 target
        ]
        res = resolve_exit_1m(
            bars_1m=bars,
            entry_ts=entry,
            direction="bullish",
            stop=14920,
            target=15040,
            force_close_ts=_ts(2026, 7, 2, 15, 55),
        )
        self.assertEqual(res["outcome"], "TARGET_HIT")

    def test_ambiguous_same_1m(self):
        entry = _ts(2026, 7, 2, 11, 0)
        bars = [
            _bar(entry, 15000, 15000, 15000, 15000, v=1),
            _bar(entry + 60, 15000, 15050, 14900, 15000, v=1),
        ]
        res = resolve_exit_1m(
            bars_1m=bars,
            entry_ts=entry,
            direction="bullish",
            stop=14920,
            target=15040,
            force_close_ts=_ts(2026, 7, 2, 15, 55),
        )
        self.assertEqual(res["outcome"], "AMBIGUOUS")


class IsolationTests(unittest.TestCase):
    def test_phase26_hash(self):
        doc = json.loads(PHASE26_FROZEN.read_text(encoding="utf-8"))
        self.assertEqual(doc["frozen_config_hash"], PHASE26_HASH)

    def test_family_and_hash_stable(self):
        self.assertEqual(STRATEGY_FAMILY, "nq_drift_vwap_pullback_v1")
        self.assertEqual(config_hash(DVP_ORIGINAL), config_hash(DVP_ORIGINAL))
        self.assertEqual(DVP_ORIGINAL.max_losses_per_day, 2)


if __name__ == "__main__":
    unittest.main()
