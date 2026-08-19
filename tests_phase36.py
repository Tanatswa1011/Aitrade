"""Phase 36 frozen-isolation and leak-safety tests."""
from __future__ import annotations

import hashlib
import json
import unittest
from pathlib import Path

from models import Bar
from nq_microstructure_models import SweepEvent
from nq_pdh_pdl import local_ts
from nq_shallow_sweep_engine import find_reclaim, resolve_path, simulate
from phase34_validate import GC_FILE_SHA, NQ_FILE_SHA, file_sha256

ROOT = Path(__file__).resolve().parent


def _bars(td: str, start: int, rows: list[tuple[float, float, float, float]]) -> list[Bar]:
    out = []
    t = start
    for o, h, l, c in rows:
        out.append(Bar(time=t, open=o, high=h, low=l, close=c, volume=100))
        t += 60
    return out


class FrozenIsolationTests(unittest.TestCase):
    def test_frozen_hashes_unchanged(self):
        gc = json.loads((ROOT / "strategy_frozen" / "gc_vwap_v2_phase26.json").read_text(encoding="utf-8"))
        nq = json.loads((ROOT / "strategy_frozen" / "nq_dvp_phase30.json").read_text(encoding="utf-8"))
        self.assertEqual(gc["frozen_config_hash"], "0695d7a881b6247033426bceb077b6895b96405b826c21f32a109a02f44b2d43")
        self.assertEqual(nq["frozen_config_hash"], "935e3a616351b09dbfa8d2d0e2b5d6850be803fb385ec5e5f8e3593856a1212a")
        self.assertEqual(file_sha256(ROOT / "strategy_frozen" / "gc_vwap_v2_phase26.json"), GC_FILE_SHA)
        self.assertEqual(file_sha256(ROOT / "strategy_frozen" / "nq_dvp_phase30.json"), NQ_FILE_SHA)

    def test_spec_reuses_phase35_train_median(self):
        spec = json.loads((ROOT / "phase36_spec.json").read_text(encoding="utf-8"))
        self.assertEqual(spec["methodology_corrections"], [])
        self.assertEqual(spec["shallow"]["threshold_points"], 18.25)
        self.assertEqual(spec["primary_candidate"]["reclaim"], "B_close_1m")
        self.assertEqual(spec["primary_candidate"]["target_R"], 1.5)
        self.assertIn("NOT IN THE LIVE FEATURE SET", spec["dom"])


class EngineLeakTests(unittest.TestCase):
    def setUp(self):
        self.rth_open = 1_700_000_000  # arbitrary unix; bars are relative
        # Use a real-looking NY RTH open via event fields only.

    def _event(self, *, side="pdl_sweep", t0=None, extreme=99.0, level=100.0, pen=1.0):
        t0 = int(t0 if t0 is not None else local_ts("2026-01-02", "09:30"))
        rth_open = local_ts("2026-01-02", "09:30")
        return SweepEvent(
            event_id="t",
            trading_date="2026-01-02",
            side=side,
            level=level,
            sweep_bar_time=t0,
            sweep_ts=t0,
            extreme=extreme,
            penetration_points=pen,
            rth_open_ts=rth_open,
            seconds_from_rth_open=t0 - rth_open,
            atr_1m_14=2.0,
            volume_sweep_bar=1000,
            prior_rth_high=101.0,
            prior_rth_low=level,
            extras={"contract": "NQM6"},
        )

    def test_same_bar_stop_and_target_is_ambiguous(self):
        outcome, _, _ = resolve_path(
            [Bar(time=10, open=100, high=110, low=90, close=100, volume=1)],
            is_long=True,
            sl=95,
            tp=105,
            flatten_ts=10_000,
        )
        self.assertEqual(outcome, "AMBIGUOUS")

    def test_wick_reclaim_on_sweep_bar_is_not_taken(self):
        t0 = local_ts("2026-01-02", "09:30")
        e = self._event(t0=t0, extreme=99.0, level=100.0)
        # Sweep bar: low 99, high 100.25 (wicks through) but close 99.5 still below PDL.
        bars = [
            Bar(time=t0, open=100.0, high=100.25, low=99.0, close=99.5, volume=1),
            Bar(time=t0 + 60, open=99.5, high=99.6, low=99.4, close=99.5, volume=1),
        ]
        found = find_reclaim(e, bars, mode="range_1m", expiry_sec=300)
        self.assertIsNone(found)

    def test_close_reclaim_enters_next_open_not_at_level(self):
        t0 = local_ts("2026-01-02", "09:30")
        e = self._event(t0=t0, extreme=99.0, level=100.0)
        bars = [
            Bar(time=t0, open=100.2, high=100.2, low=99.0, close=100.1, volume=1),  # close back above PDL
            Bar(time=t0 + 60, open=100.3, high=101.0, low=100.2, close=100.8, volume=1),
            Bar(time=t0 + 120, open=100.8, high=103.0, low=100.7, close=102.9, volume=1),
        ]
        trade = simulate(
            e, bars, candidate="B", reclaim_mode="close_1m",
            expiry_sec=300, sl_buffer_ticks=1, target_r=1.5, entry_adverse_ticks=1.0,
        )
        self.assertEqual(trade.status, "ENTERED")
        self.assertNotAlmostEqual(trade.entry_fill, 100.0)
        self.assertGreater(trade.entry_fill, trade.entry_theo)  # long, 1 tick adverse
        self.assertEqual(trade.entry_ts, t0 + 60)


if __name__ == "__main__":
    unittest.main()
