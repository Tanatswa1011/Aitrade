"""Phase 41 frozen-isolation and leak-safety tests."""
from __future__ import annotations

import json
import unittest
from pathlib import Path

from models import Bar
from nq_pdh_pdl import local_ts
from phase34_validate import GC_FILE_SHA, NQ_FILE_SHA, file_sha256
from volume_profile_engine import (
    build_profile,
    open_class,
    simulate_accept_poc,
    simulate_inside_poc,
    simulate_reject_1r,
)

ROOT = Path(__file__).resolve().parent


def _rth(td: str, rows: list[tuple[float, float, float, float, float]]) -> list[Bar]:
    t0 = local_ts(td, "09:30")
    out = []
    for i, (o, h, l, c, v) in enumerate(rows):
        out.append(Bar(time=t0 + i * 60, open=o, high=h, low=l, close=c, volume=v))
    return out


class FrozenIsolationTests(unittest.TestCase):
    def test_frozen_hashes_unchanged(self):
        gc = json.loads((ROOT / "strategy_frozen" / "gc_vwap_v2_phase26.json").read_text(encoding="utf-8"))
        nq = json.loads((ROOT / "strategy_frozen" / "nq_dvp_phase30.json").read_text(encoding="utf-8"))
        self.assertEqual(gc["frozen_config_hash"], "0695d7a881b6247033426bceb077b6895b96405b826c21f32a109a02f44b2d43")
        self.assertEqual(nq["frozen_config_hash"], "935e3a616351b09dbfa8d2d0e2b5d6850be803fb385ec5e5f8e3593856a1212a")
        self.assertEqual(file_sha256(ROOT / "strategy_frozen" / "gc_vwap_v2_phase26.json"), GC_FILE_SHA)
        self.assertEqual(file_sha256(ROOT / "strategy_frozen" / "nq_dvp_phase30.json"), NQ_FILE_SHA)

    def test_spec_primary_locked(self):
        spec = json.loads((ROOT / "phase41_spec.json").read_text(encoding="utf-8"))
        self.assertEqual(spec["methodology_corrections"], [])
        self.assertEqual(spec["primary_candidate"]["id"], "VP_OUTSIDE_ACCEPT_POC")
        self.assertEqual(spec["profile_source"]["status"], "DEGRADED_1M_VOLUME_PROFILE")
        self.assertIn("70", spec["profile_source"]["value_area"])
        self.assertIn("No ORB", spec["forbidden"])


class ProfileAndLeakTests(unittest.TestCase):
    def test_poc_is_max_volume_tick(self):
        td = "2026-01-05"
        bars = _rth(td, [
            (100.00, 100.00, 100.00, 100.00, 10),
            (100.25, 100.25, 100.25, 100.25, 50),
            (100.50, 100.50, 100.50, 100.50, 10),
        ] + [(100.25, 100.50, 100.00, 100.25, 1)] * 387)
        prof = build_profile(bars, td)
        self.assertIsNotNone(prof)
        self.assertAlmostEqual(prof.poc, 100.25)
        self.assertGreaterEqual(prof.vah, prof.poc)
        self.assertLessEqual(prof.val, prof.poc)

    def test_next_day_cannot_use_current_volume(self):
        d1 = _rth("2026-01-05", [(100, 101, 99, 100, 100)] * 390)
        d2 = _rth("2026-01-06", [(110, 111, 109, 110, 100)] * 390)
        p1 = build_profile(d1, "2026-01-05")
        p2 = build_profile(d2, "2026-01-06")
        self.assertNotAlmostEqual(p1.poc, p2.poc)
        self.assertLess(p1.poc, 105)
        self.assertGreater(p2.poc, 105)

    def test_open_class_uses_prior_profile_only(self):
        prof_dummy = build_profile(_rth("2026-01-05", [(100, 100.5, 99.5, 100, 10)] * 390), "2026-01-05")
        self.assertEqual(open_class(prof_dummy.vah + 1, prof_dummy), "OPEN_ABOVE_VAH")
        self.assertEqual(open_class(prof_dummy.val - 1, prof_dummy), "OPEN_BELOW_VAL")
        self.assertEqual(open_class((prof_dummy.vah + prof_dummy.val) / 2, prof_dummy), "OPEN_INSIDE_VALUE")

    def test_accept_enters_next_open_not_close(self):
        prior = _rth("2026-01-05", [(100.0, 100.50, 99.50, 100.0, 10)] * 390)
        prof = build_profile(prior, "2026-01-05")
        self.assertGreater(prof.vah, prof.val)
        rows = [(prof.vah + 2, prof.vah + 2.5, prof.vah + 1.5, prof.vah + 2, 10)] * 5
        rows += [(prof.poc, max(prof.poc + 0.25, prof.val + 0.25), min(prof.poc, prof.vah), prof.poc, 10)]
        rows += [(prof.poc, prof.poc + 0.5, prof.poc - 0.25, prof.poc, 10)] * 384
        rth = _rth("2026-01-06", rows)
        t = simulate_accept_poc(instrument="ES", td="2026-01-06", rth=rth, prof=prof, adverse_ticks=1.0)
        if t.status == "ENTERED":
            self.assertEqual(t.direction, "SHORT")
            self.assertLess(t.entry_fill, t.entry_theo)
            self.assertNotAlmostEqual(t.entry_theo, float(rth[5].close))

    def test_inside_rotates_toward_poc(self):
        prior = _rth("2026-01-05", [(100.0, 101.0, 99.0, 100.0, 10)] * 390)
        prof = build_profile(prior, "2026-01-05")
        open_px = prof.poc + 0.50
        if not (prof.val < open_px < prof.vah):
            open_px = (prof.vah + prof.poc) / 2
        self.assertTrue(prof.val < open_px < prof.vah)
        rows = [(open_px, open_px + 0.25, open_px - 0.25, open_px, 10)] * 390
        rth = _rth("2026-01-06", rows)
        t = simulate_inside_poc(instrument="ES", td="2026-01-06", rth=rth, prof=prof, adverse_ticks=1.0)
        if t.status == "ENTERED":
            self.assertEqual(t.direction, "SHORT" if open_px > prof.poc else "LONG")
            self.assertAlmostEqual(t.target, prof.poc)

    def test_same_bar_stop_target_ambiguous(self):
        prior = _rth("2026-01-05", [(100.0, 100.0, 100.0, 100.0, 50)] * 390)
        prof = build_profile(prior, "2026-01-05")
        # Force a reject setup with huge range bar after entry
        rows = [(prof.vah + 2, prof.vah + 2, prof.vah + 2, prof.vah + 2, 1)] * 3
        rows.append((prof.vah + 0.25, prof.vah + 0.25, prof.vah - 0.25, prof.vah + 0.5, 1))  # test + close back above
        # next bar spans stop and target
        rows.append((prof.vah + 1, prof.vah + 50, prof.val - 50, prof.vah + 1, 1))
        rows += [(prof.vah + 1, prof.vah + 1.25, prof.vah + 0.75, prof.vah + 1, 1)] * 382
        rth = _rth("2026-01-06", rows)
        t = simulate_reject_1r(instrument="ES", td="2026-01-06", rth=rth, prof=prof, adverse_ticks=0.0)
        if t.status == "ENTERED" and t.outcome:
            if t.outcome == "AMBIGUOUS":
                self.assertIsNone(t.points)


if __name__ == "__main__":
    unittest.main()
