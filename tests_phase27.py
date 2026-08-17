"""Phase 27 tests — London session DST, VWAP reset, shared sigma, no Phase26 contamination."""

from __future__ import annotations

import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from gc_vwap_engine import (
    analyze_candidate,
    collect_extension_sequences,
    compute_session_vwap_series,
    typical_price,
)
from gc_vwap_london_engine import (
    collect_london_extension_sequences,
    compute_london_vwap_series,
    london_session_window,
)
from gc_vwap_london_models import (
    LONDON_SESSION,
    PHASE27_CANDIDATES,
    STRATEGY_FAMILY,
)
from gc_vwap_models import DEFAULT_NY_SESSION, ConfirmationMode as CM, EntryMode as EM
from models import Bar

LDN = ZoneInfo("Europe/London")
NY = ZoneInfo("America/New_York")
PHASE26_PAPER = Path("journal") / "phase26_gc_vwap_v2_paper" / "paper_trades.jsonl"


def _bar(t, o, h, l, c, v=100.0):
    return Bar(time=t, open=o, high=h, low=l, close=c, volume=v)


def _ts_ldn(y, m, d, hh, mm):
    return int(datetime(y, m, d, hh, mm, tzinfo=LDN).timestamp())


def _ts_ny(y, m, d, hh, mm):
    return int(datetime(y, m, d, hh, mm, tzinfo=NY).timestamp())


class LondonSessionTests(unittest.TestCase):
    def test_dst_aware_window_bst(self):
        # 2026-07-02 is BST (UTC+1)
        start, end, no_new = london_session_window("2026-07-02")
        self.assertEqual(datetime.fromtimestamp(start, tz=LDN).hour, 8)
        self.assertEqual(datetime.fromtimestamp(end, tz=LDN).hour, 12)
        self.assertEqual(datetime.fromtimestamp(no_new, tz=LDN).hour, 11)
        # UTC differs from winter
        start_w, _, _ = london_session_window("2026-01-08")
        # July 08:00 BST = 07:00 UTC; Jan 08:00 GMT = 08:00 UTC → different UTC stamps relative to local
        self.assertNotEqual(
            datetime.fromtimestamp(start, tz=ZoneInfo("UTC")).hour,
            datetime.fromtimestamp(start_w, tz=ZoneInfo("UTC")).hour,
        )

    def test_0800_reset_and_cutoff(self):
        start, end, no_new = london_session_window("2026-07-02")
        self.assertLess(start, no_new)
        self.assertLess(no_new, end)
        self.assertEqual(LONDON_SESSION.start_local, "08:00")
        self.assertEqual(LONDON_SESSION.end_local, "12:00")
        self.assertEqual(LONDON_SESSION.no_new_setups_after, "11:00")

    def test_vwap_resets_london_not_ny(self):
        # Bars only in London morning window
        bars = []
        for i in range(8):
            t = _ts_ldn(2026, 7, 2, 8, 0) + i * 300
            bars.append(_bar(t, 2000 + i, 2001 + i, 1999 + i, 2000 + i, v=10))
        ldn = compute_london_vwap_series(bars, "2026-07-02")
        self.assertTrue(len(ldn) >= 6)
        self.assertTrue(ldn[-1].valid)
        # Same bars are overnight before NY 08:20 — NY session empty / no overlap
        ny = compute_session_vwap_series(bars, "2026-07-02", session=DEFAULT_NY_SESSION)
        self.assertEqual(len(ny), 0)

    def test_sigma_math_matches_phase25_impl(self):
        bars = []
        for i, px in enumerate([100, 102, 104, 106, 108, 110, 112, 114]):
            t = _ts_ldn(2026, 7, 2, 8, 0) + i * 300
            bars.append(_bar(t, px, px + 1, px - 1, px, v=10))
        states = compute_london_vwap_series(bars, "2026-07-02")
        last = states[-1]
        tps = [typical_price(b) for b in bars]
        vols = [10.0] * len(bars)
        vwap = sum(tp * v for tp, v in zip(tps, vols)) / sum(vols)
        var = sum(v * (tp - vwap) ** 2 for tp, v in zip(tps, vols)) / sum(vols)
        import math

        std = math.sqrt(var)
        self.assertAlmostEqual(last.vwap, vwap, places=6)
        self.assertAlmostEqual(last.session_std, std, places=6)


class LondonLogicTests(unittest.TestCase):
    def _session(self):
        bars = []
        for i in range(8):
            t = _ts_ldn(2026, 7, 2, 8, 0) + i * 300
            bars.append(_bar(t, 2000, 2001, 1999, 2000, v=100))
        for i in range(4):
            t = _ts_ldn(2026, 7, 2, 8, 40) + i * 300
            px = 2010 + i * 5
            bars.append(_bar(t, px - 1, px + 2, px - 2, px, v=50))
        for i in range(6):
            t = _ts_ldn(2026, 7, 2, 9, 0) + i * 300
            px = 2015 - i * 3
            bars.append(_bar(t, px + 1, px + 2, px - 2, px, v=80))
        return bars

    def test_extension_reclaim_frozen_retest(self):
        bars = self._session()
        seqs = collect_london_extension_sequences(bars, "2026-07-02")
        if not seqs:
            self.skipTest("no extension in synthetic path")
        cfg = next(c for c in PHASE27_CANDIDATES if c.candidate_id == "L2_BAND_RECLAIM_2SIG_RETEST")
        setup = analyze_candidate(seqs[0], cfg)
        self.assertEqual(cfg.confirmation_mode, CM.BAND_RECLAIM.value)
        self.assertEqual(cfg.entry_mode, EM.FROZEN_2SIG_RETEST.value)
        self.assertEqual(cfg.max_entry_bars, 6)
        if setup.entry_triggered:
            self.assertEqual(setup.entry_price, seqs[0].get("frozen_2sig"))
            self.assertEqual(setup.stop_price, seqs[0]["extreme"])

    def test_event_dedupe_prefix(self):
        bars = self._session()
        seqs = collect_london_extension_sequences(bars, "2026-07-02")
        if not seqs:
            self.skipTest("no extension")
        self.assertEqual(seqs[0].get("event_prefix"), "VWAP2S_LON")
        cfg = PHASE27_CANDIDATES[2]
        setup = analyze_candidate(seqs[0], cfg)
        self.assertIn("VWAP2S_LON", setup.vwap_extension_event_id)
        self.assertEqual(setup.strategy_family, STRATEGY_FAMILY)

    def test_no_lookahead_bars_used(self):
        bars = self._session()
        states = compute_london_vwap_series(bars, "2026-07-02")
        for i, st in enumerate(states):
            self.assertEqual(st.bars_used, i + 1)

    def test_family_isolated(self):
        self.assertEqual(STRATEGY_FAMILY, "gc_vwap_london_mean_reversion_v1")
        self.assertEqual(len(PHASE27_CANDIDATES), 4)
        self.assertTrue(all(c.strategy_family == STRATEGY_FAMILY for c in PHASE27_CANDIDATES))


class TrainHoldoutGuardTests(unittest.TestCase):
    def test_finalist_selection_caps_at_two(self):
        from phase27_validate import select_finalists

        fake = [
            {"candidate_id": "L0", "resolved_n": 40, "theoretical_2r_expectancy": 0.1, "stop_rate": 0.5},
            {"candidate_id": "L1", "resolved_n": 50, "theoretical_2r_expectancy": 0.2, "stop_rate": 0.5},
            {"candidate_id": "L2", "resolved_n": 55, "theoretical_2r_expectancy": 0.3, "stop_rate": 0.5},
            {"candidate_id": "L3", "resolved_n": 45, "theoretical_2r_expectancy": 0.15, "stop_rate": 0.5},
        ]
        out = select_finalists(fake)
        self.assertLessEqual(len(out), 2)

    def test_phase26_paper_not_written_by_london_journal_path(self):
        from phase27_validate import JOURNAL_DIR, PHASE26_PAPER

        self.assertNotEqual(JOURNAL_DIR, PHASE26_PAPER.parent)
        self.assertTrue(str(JOURNAL_DIR).endswith("phase27_gc_vwap_london"))


if __name__ == "__main__":
    unittest.main()
