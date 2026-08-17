"""Phase 19 tests — history resume, 1m resolver, frozen candidates, holdout, CHoCH."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from chrono_split import assert_no_split_leakage, chronological_split
from history_extend import current_5m_span
from intrabar_resolver import (
    ENTRY_THEN_STOP,
    INSUFFICIENT_DATA,
    STILL_AMBIGUOUS,
    STOP_BEFORE_ENTRY,
    TARGET_THEN_STOP,
    STOP_THEN_TARGET,
    resolver_5m_from_1m,
)
from luxalgo_overlap import classify_equivalence_status, compare_choch_overlap
from models import Bar, StructureConfirmation
from phase19_1m import identify_5m_ambiguous_windows, resolve_5m_with_1m
from phase19_validate import (
    assert_candidate_unchanged,
    choose_split_fraction,
    load_frozen_finalists,
)


def _bar(t, o, h, l, c):
    return Bar(time=t, open=o, high=h, low=l, close=c)


class TestHistoryResume(unittest.TestCase):
    def test_current_span_provenance_path(self):
        span = current_5m_span()
        self.assertTrue(span.get("ok"))
        self.assertGreater(span.get("bar_count") or 0, 0)
        self.assertIsNotNone(span.get("earliest"))
        self.assertLess(span["earliest"], span["latest"])

    def test_choose_split_not_performance_based(self):
        self.assertEqual(choose_split_fraction(250), 0.70)
        self.assertEqual(choose_split_fraction(150), 0.65)
        self.assertEqual(choose_split_fraction(80), 0.60)


class TestOneMResolver(unittest.TestCase):
    def test_sequential_1m_entry_then_stop(self):
        parent = 1_000_000
        # bullish: entry 100, stop 90 — distinct 1m bars
        kids = [
            _bar(parent, 101, 102, 100.5, 101),  # no entry/stop
            _bar(parent + 60, 100.2, 100.5, 99.5, 100),  # entry only
            _bar(parent + 120, 95, 95, 88, 90),  # stop only
            _bar(parent + 180, 90, 91, 90, 91),
            _bar(parent + 240, 91, 92, 91, 92),
        ]
        r = resolver_5m_from_1m().resolve_entry_stop(
            direction="bullish",
            entry_price=100.0,
            stop_price=90.0,
            parent_bar_time=parent,
            child_bars=kids,
        )
        self.assertEqual(r.result, ENTRY_THEN_STOP)

    def test_same_1m_bar_still_ambiguous(self):
        parent = 2_000_000
        kids = [
            _bar(parent, 100, 101, 89, 95),  # both entry 100 and stop 90 in same 1m
            _bar(parent + 60, 95, 96, 95, 96),
        ]
        r = resolver_5m_from_1m().resolve_entry_stop(
            direction="bullish",
            entry_price=100.0,
            stop_price=90.0,
            parent_bar_time=parent,
            child_bars=kids,
        )
        self.assertEqual(r.result, STILL_AMBIGUOUS)

    def test_missing_1m_insufficient(self):
        r = resolver_5m_from_1m().resolve_entry_stop(
            direction="bullish",
            entry_price=100.0,
            stop_price=90.0,
            parent_bar_time=3_000_000,
            child_bars=[],
        )
        self.assertEqual(r.result, INSUFFICIENT_DATA)

    def test_target_then_stop_labels(self):
        parent = 4_000_000
        kids = [
            _bar(parent, 100, 110, 100, 109),  # target 105 only
            _bar(parent + 60, 95, 95, 88, 90),  # stop 90 only
        ]
        r = resolver_5m_from_1m().resolve_target_stop(
            direction="bullish",
            target_price=105.0,
            stop_price=90.0,
            parent_bar_time=parent,
            child_bars=kids,
        )
        self.assertEqual(r.result, TARGET_THEN_STOP)

    def test_identify_and_resolve_batch(self):
        recs = [
            {
                "setup_id": "s1|exec:5m",
                "execution_timeframe": "5m",
                "direction": "bullish",
                "entry_results": [
                    {
                        "mode": "first_touch",
                        "triggered": True,
                        "outcome": "AMBIGUOUS_INTRABAR",
                        "ambiguity_flags": ["TRIGGER_BAR_STOP_AMBIGUITY"],
                        "entry_timestamp": 1_000_050,
                        "entry_price": 100.0,
                        "stop_price": 90.0,
                    }
                ],
            }
        ]
        wins = identify_5m_ambiguous_windows(recs)
        self.assertEqual(len(wins), 1)
        parent = wins[0]["parent_bar_time"]
        kids = [
            _bar(parent, 101, 102, 100.5, 101),
            _bar(parent + 60, 100.2, 100.5, 99.5, 100),
            _bar(parent + 120, 95, 95, 88, 90),
            _bar(parent + 180, 90, 91, 90, 91),
            _bar(parent + 240, 91, 92, 91, 92),
        ]
        rep = resolve_5m_with_1m(wins, kids)
        self.assertEqual(rep["ambiguous_before_1m"], 1)
        self.assertGreaterEqual(rep["resolved_with_1m"] + rep["still_ambiguous"] + rep["insufficient_data"], 1)


class TestFrozenCandidates(unittest.TestCase):
    def test_load_exact_finalists(self):
        bundle = load_frozen_finalists()
        ids = [c.candidate_id for _, c, _ in bundle]
        self.assertEqual(ids, ["C4_5m_first_touch", "C3_5m_ce", "C12_5m_htf_C"])
        for raw, cand, path in bundle:
            self.assertEqual(raw["candidate"]["candidate_id"], cand.candidate_id)
            assert_candidate_unchanged(raw, path)

    def test_assert_detects_mutation(self):
        bundle = load_frozen_finalists()
        raw, _, path = bundle[0]
        mutated = json.loads(json.dumps(raw))
        mutated["candidate"]["entry_mode"] = "boundary"
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "x.json"
            p.write_text(json.dumps(mutated), encoding="utf-8")
            with self.assertRaises(AssertionError):
                assert_candidate_unchanged(raw, str(p))


class TestHoldoutSplit(unittest.TestCase):
    def test_no_leakage(self):
        rows = []
        for i in range(1, 41):
            d = f"2026-01-{i:02d}" if i <= 31 else f"2026-02-{i-31:02d}"
            rows.append(
                {
                    "trading_date": d,
                    "liquidity_event_id": f"L{i}",
                    "session": "Asia",
                    "setup_id": f"L{i}|exec:5m",
                }
            )
        train, hold, split = chronological_split(rows, train_fraction=0.70)
        assert_no_split_leakage(split)
        self.assertEqual(len(set(split.train_liquidity_event_ids) & set(split.holdout_liquidity_event_ids)), 0)
        self.assertTrue(split.train_end < split.holdout_start)


class TestChochOverlap(unittest.TestCase):
    def _ev(self, direction, ts, level):
        return StructureConfirmation(
            kind="CHoCH",
            direction=direction,
            level=level,
            event_timestamp=ts,
            event_bar_index=None,
            source="test",
            study_id=None,
            raw_id=None,
            timing_confidence="exact",
        )

    def test_exact_match(self):
        internal = [self._ev("bullish", 1000, 2000.0)]
        lux = [self._ev("bullish", 1000, 2000.0)]
        rep = compare_choch_overlap(
            internal, lux, time_tolerance_sec=300, level_tolerance=5.0, max_bar_distance=1, period_sec=300
        )
        self.assertEqual(rep["matched_count"], 1)
        self.assertEqual(rep["luxalgo_only_count"], 0)
        self.assertEqual(rep["internal_only_count"], 0)

    def test_direction_mismatch(self):
        internal = [self._ev("bullish", 1000, 2000.0)]
        lux = [self._ev("bearish", 1000, 2000.0)]
        rep = compare_choch_overlap(internal, lux, time_tolerance_sec=300, level_tolerance=5.0)
        self.assertEqual(rep["matched_count"], 0)
        self.assertEqual(rep["luxalgo_only_count"], 1)
        self.assertEqual(rep["internal_only_count"], 1)

    def test_time_tolerance(self):
        internal = [self._ev("bullish", 1000, 2000.0)]
        lux = [self._ev("bullish", 1000 + 301, 2000.0)]
        rep = compare_choch_overlap(internal, lux, time_tolerance_sec=300, level_tolerance=5.0)
        self.assertEqual(rep["matched_count"], 0)

    def test_level_tolerance(self):
        internal = [self._ev("bullish", 1000, 2000.0)]
        lux = [self._ev("bullish", 1000, 2010.0)]
        rep = compare_choch_overlap(internal, lux, time_tolerance_sec=300, level_tolerance=5.0)
        self.assertEqual(rep["matched_count"], 0)

    def test_lux_only_internal_only(self):
        internal = [self._ev("bullish", 1000, 2000.0)]
        lux = [self._ev("bearish", 5000, 2100.0)]
        rep = compare_choch_overlap(internal, lux)
        self.assertEqual(rep["luxalgo_only_count"], 1)
        self.assertEqual(rep["internal_only_count"], 1)

    def test_status_conservative(self):
        self.assertEqual(
            classify_equivalence_status(luxalgo_reliable_count=2, matched_count=2),
            "unvalidated_against_luxalgo",
        )
        self.assertEqual(
            classify_equivalence_status(luxalgo_reliable_count=20, matched_count=10),
            "partially_validated",
        )


if __name__ == "__main__":
    unittest.main()
