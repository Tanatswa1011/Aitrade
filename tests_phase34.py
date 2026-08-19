"""Phase 34 look-ahead / PDH-PDL / frozen-isolation tests."""

from __future__ import annotations

import hashlib
import json
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from models import Bar
from nq_microstructure_features import features_from_records, merge_spans
from nq_microstructure_models import SweepEvent
from nq_pdh_pdl import detect_pdh_pdl_sweeps, label_outcome_1m, local_ts

NY = ZoneInfo("America/New_York")
ROOT = Path(__file__).resolve().parent


def _bar(t, o, h, l, c, v=100.0):
    return Bar(time=t, open=o, high=h, low=l, close=c, volume=v)


def _rth_day(trading_date: str, high: float, low: float, close: float) -> list[Bar]:
    start = local_ts(trading_date, "09:30")
    bars = []
    px = close
    for i in range(390):  # 09:30-16:00
        t = start + i * 60
        bars.append(_bar(t, px, high, low, close, 50.0))
    return bars


class PdhPdlTests(unittest.TestCase):
    def test_pdh_pdl_uses_only_prior_rth(self):
        d1 = _rth_day("2026-05-13", high=100.0, low=90.0, close=95.0)
        d2_start = local_ts("2026-05-14", "09:30")
        d2 = [_bar(d2_start, 96.0, 101.0, 94.0, 96.0, 80.0)]
        # Rest of day 2 stays inside.
        for i in range(1, 390):
            t = d2_start + i * 60
            d2.append(_bar(t, 96.0, 97.0, 95.0, 96.0, 20.0))
        events = detect_pdh_pdl_sweeps(d1 + d2, ["2026-05-14"])
        pdh = [e for e in events if e.side == "pdh_sweep"]
        self.assertEqual(len(pdh), 1)
        self.assertAlmostEqual(pdh[0].level, 100.0)
        self.assertEqual(pdh[0].extras["pdh_pdl_source_date"], "2026-05-13")
        self.assertEqual(pdh[0].sweep_bar_time, d2_start)

    def test_holiday_skipped_as_pdh_source(self):
        # Friday 05-22 has the real RTH range; holiday 05-25 has a fake wide range.
        fri = _rth_day("2026-05-22", high=200.0, low=180.0, close=190.0)
        hol = _rth_day("2026-05-25", high=999.0, low=1.0, close=500.0)
        tue_start = local_ts("2026-05-26", "09:30")
        tue = [_bar(tue_start, 190.0, 201.0, 189.0, 195.0)]
        for i in range(1, 390):
            tue.append(_bar(tue_start + i * 60, 195.0, 196.0, 194.0, 195.0))
        events = detect_pdh_pdl_sweeps(
            fri + hol + tue, ["2026-05-26"], skip_source_dates={"2026-05-25"}
        )
        pdh = [e for e in events if e.side == "pdh_sweep"]
        self.assertEqual(len(pdh), 1)
        self.assertAlmostEqual(pdh[0].level, 200.0)
        self.assertEqual(pdh[0].extras["pdh_pdl_source_date"], "2026-05-22")

    def test_second_touch_is_not_a_new_sweep(self):
        d1 = _rth_day("2026-05-13", high=100.0, low=90.0, close=95.0)
        d2_start = local_ts("2026-05-14", "09:30")
        d2 = [_bar(d2_start, 96.0, 101.0, 94.0, 96.0)]
        d2.append(_bar(d2_start + 60, 96.0, 110.0, 95.0, 96.0))
        for i in range(2, 390):
            d2.append(_bar(d2_start + i * 60, 96.0, 97.0, 95.0, 96.0))
        events = detect_pdh_pdl_sweeps(d1 + d2, ["2026-05-14"])
        self.assertEqual(sum(1 for e in events if e.side == "pdh_sweep"), 1)


class OutcomeLookAheadTests(unittest.TestCase):
    def _event(self, side, level, extreme, t):
        return SweepEvent(
            event_id="t",
            trading_date="2026-05-14",
            side=side,
            level=level,
            sweep_bar_time=t,
            sweep_ts=t,
            extreme=extreme,
            penetration_points=abs(extreme - level),
            rth_open_ts=t,
            seconds_from_rth_open=0,
            atr_1m_14=10.0,
            volume_sweep_bar=100.0,
            prior_rth_high=level if side == "pdh_sweep" else level + 50,
            prior_rth_low=level if side == "pdl_sweep" else level - 50,
        )

    def test_same_bar_stop_and_target_is_ambiguous(self):
        t = local_ts("2026-05-14", "10:00")
        event = self._event("pdl_sweep", 100.0, 99.0, t)
        # First bar after sweep both reclaims (+8) and continues (-12 from extreme 99)
        path = [_bar(t + 60, 99.0, 108.0, 87.0, 100.0)]
        out = label_outcome_1m(event, path, horizon_sec=300)
        self.assertEqual(out.label, "AMBIGUOUS")

    def test_path_starts_after_sweep_bar(self):
        t = local_ts("2026-05-14", "10:00")
        event = self._event("pdh_sweep", 100.0, 101.0, t)
        # Sweep bar itself would look like a reversal if used; it must be ignored.
        bars = [
            _bar(t, 100.0, 101.0, 90.0, 90.0),  # would reclaim 8+ pts if counted
            _bar(t + 60, 101.0, 114.0, 100.5, 113.0),  # continuation +12 from extreme
        ]
        out = label_outcome_1m(event, bars, horizon_sec=300)
        self.assertEqual(out.label, "CONTINUATION")

    def test_future_spike_after_horizon_ignored(self):
        t = local_ts("2026-05-14", "10:00")
        event = self._event("pdl_sweep", 100.0, 99.0, t)
        bars = [
            _bar(t + 60, 99.0, 99.5, 98.5, 99.0),
            _bar(t + 120, 99.0, 99.2, 98.8, 99.0),
            _bar(t + 400, 99.0, 120.0, 99.0, 120.0),  # after 300s horizon
        ]
        out = label_outcome_1m(event, bars, horizon_sec=300)
        self.assertEqual(out.label, "NEITHER")


class FeatureCutoffTests(unittest.TestCase):
    def test_records_after_bar_close_are_ignored(self):
        t = local_ts("2026-05-14", "10:00")
        event = SweepEvent(
            event_id="t",
            trading_date="2026-05-14",
            side="pdl_sweep",
            level=100.0,
            sweep_bar_time=t,
            sweep_ts=t,
            extreme=99.0,
            penetration_points=1.0,
            rth_open_ts=t,
            seconds_from_rth_open=0,
            atr_1m_14=10.0,
            volume_sweep_bar=100.0,
            prior_rth_high=110.0,
            prior_rth_low=100.0,
        )
        recs = [
            {
                "ts_event": t + 30,
                "bid_sz_00": 10,
                "ask_sz_00": 10,
                "bid_px_00": 99.0,
                "ask_px_00": 99.25,
                "action": "A",
                "side": "B",
            },
            {
                "ts_event": t + 30,
                "price": 99.0,
                "size": 5,
                "action": "T",
                "side": "A",
            },
            # After cutoff (t+60): huge sell that must not enter features.
            {
                "ts_event": t + 90,
                "price": 90.0,
                "size": 9999,
                "action": "T",
                "side": "A",
            },
        ]
        feats = features_from_records(recs, event)
        self.assertEqual(feats["aggressive_sell"], 5.0)
        self.assertGreaterEqual(feats["n_future_records_ignored"], 1)

    def test_merge_spans_does_not_minmax_the_whole_day(self):
        merged = merge_spans([(100, 280), (10000, 10180)])
        self.assertEqual(merged, [(100, 280), (10000, 10180)])
        overlapping = merge_spans([(100, 280), (200, 400)])
        self.assertEqual(overlapping, [(100, 400)])


class FrozenIsolationTests(unittest.TestCase):
    def test_frozen_hashes_unchanged(self):
        gc = json.loads((ROOT / "strategy_frozen" / "gc_vwap_v2_phase26.json").read_text(encoding="utf-8"))
        nq = json.loads((ROOT / "strategy_frozen" / "nq_dvp_phase30.json").read_text(encoding="utf-8"))
        self.assertEqual(gc["frozen_config_hash"], "0695d7a881b6247033426bceb077b6895b96405b826c21f32a109a02f44b2d43")
        self.assertEqual(nq["frozen_config_hash"], "935e3a616351b09dbfa8d2d0e2b5d6850be803fb385ec5e5f8e3593856a1212a")
        self.assertEqual(
            hashlib.sha256((ROOT / "strategy_frozen" / "gc_vwap_v2_phase26.json").read_bytes()).hexdigest(),
            "12615d19ef3deed36b7929c161ef7c377975cd2317c0ae6d06f1949bd327046f",
        )
        self.assertEqual(
            hashlib.sha256((ROOT / "strategy_frozen" / "nq_dvp_phase30.json").read_bytes()).hexdigest(),
            "34927fba0f268e56cf166cf8939bdfec6aa031510f7cee9acfab3909dd36b541",
        )


if __name__ == "__main__":
    unittest.main()
