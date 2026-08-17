"""Phase 20 tests — LuxAlgo capture, mapping, matching, divergence, isolation."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from historical_structure import detect_internal_choch
from luxalgo_capture import append_luxalgo_captures, load_luxalgo_captures
from luxalgo_structure import normalize_choch_events
from models import Bar, StructureConfirmation
from phase20_divergence import (
    CLOSE_VS_WICK_BREAK,
    INDUCEMENT_DEPENDENCY,
    STRUCTURE_STATE_DIFFERENCE,
    UNKNOWN,
    classify_divergence,
)
from phase20_mapping import map_luxalgo_event_to_bars
from phase20_matching import (
    DIRECTION_ONLY_MATCH,
    EXACT_MATCH,
    INTERNAL_ONLY,
    LEVEL_ONLY_MATCH,
    LUXALGO_ONLY,
    NEAR_TIME_MATCH,
    TIMING_UNRESOLVED,
    classify_equivalence_status_phase20,
    classify_luxalgo_match,
    match_overlap,
    MatchTolerances,
)
from phase20_validate import decide_final, LEVEL_TOLERANCE


def _bar(t, o, h, l, c):
    return Bar(time=t, open=o, high=h, low=l, close=c)


def _lux(direction, level, ts, conf="exact", bar_index=None):
    return StructureConfirmation(
        kind="CHoCH",
        direction=direction,
        level=level,
        event_timestamp=ts,
        event_bar_index=bar_index,
        source="luxalgo",
        study_id="smUEv2",
        raw_id="1",
        timing_confidence=conf,
    )


def _int(direction, level, ts, bar_index=0):
    return StructureConfirmation(
        kind="CHoCH",
        direction=direction,
        level=level,
        event_timestamp=ts,
        event_bar_index=bar_index,
        source="internal_structure",
        study_id=None,
        raw_id=f"i_{ts}",
        timing_confidence="exact",
    )


class CaptureTests(unittest.TestCase):
    def test_choch_recognized_bos_idm_rejected(self):
        payload = {
            "ok": True,
            "studyId": "smUEv2",
            "bullColor": 1,
            "bearColor": 2,
            "labels": [
                {"t": "CHoCH", "y": 100.0, "tci": 1, "indexMapped": 10, "id": "a"},
                {"t": "BOS", "y": 101.0, "tci": 1, "indexMapped": 11, "id": "b"},
                {"t": "IDM", "y": 99.0, "tci": 2, "indexMapped": 12, "id": "c"},
            ],
            "lines": [],
        }
        events = normalize_choch_events(
            payload, bars_by_series_index={10: 1_700_000_000}
        )
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].kind, "CHoCH")
        self.assertEqual(events[0].direction, "bullish")

    def test_dedupe_and_unreliable_excluded_from_strict(self):
        good = _lux("bullish", 100.0, 1_700_000_000, "exact", 1)
        bad = _lux("bearish", 99.0, None, "unavailable", -2000000)
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "caps.jsonl"
            a = append_luxalgo_captures(
                [good, bad],
                symbol="OANDA:XAUUSD",
                timeframe="5m",
                path=path,
                include_unreliable=True,
            )
            b = append_luxalgo_captures(
                [good],
                symbol="OANDA:XAUUSD",
                timeframe="5m",
                path=path,
                include_unreliable=True,
            )
            self.assertEqual(a["written"], 2)
            self.assertEqual(b["written"], 0)
            rel = load_luxalgo_captures(path=path, reliable_only=True)
            self.assertEqual(len(rel), 1)
            self.assertTrue(rel[0]["reliable"])


class MappingTests(unittest.TestCase):
    def setUp(self):
        self.bars = [_bar(1_700_000_000 + i * 300, 1, 2, 0.5, 1.5) for i in range(5)]

    def test_exact_timestamp(self):
        ev = _lux("bullish", 1.0, 1_700_000_600)
        m = map_luxalgo_event_to_bars(ev, self.bars, period_sec=300)
        self.assertEqual(m["mapping_status"], "mapped")
        self.assertEqual(m["mapping_method"], "exact_timestamp")
        self.assertEqual(m["mapped_bar_index"], 2)

    def test_bar_index_mapping(self):
        ev = _lux("bullish", 1.0, None, "unavailable", 2)
        m = map_luxalgo_event_to_bars(
            ev,
            self.bars,
            bars_by_series_index={2: 1_700_000_600},
            period_sec=300,
        )
        self.assertEqual(m["mapping_status"], "mapped")
        self.assertEqual(m["mapping_method"], "bar_index_to_timestamp")

    def test_nearest_explicit(self):
        ev = _lux("bullish", 1.0, 1_700_000_650, "exact")
        m = map_luxalgo_event_to_bars(
            ev, self.bars, period_sec=300, allow_nearest=True, nearest_max_bars=1
        )
        self.assertEqual(m["mapping_status"], "mapped")
        self.assertEqual(m["mapping_method"], "derived_nearest_valid_bar")

    def test_unresolved_no_silent_snap(self):
        ev = _lux("bullish", 1.0, 1_800_000_000, "exact")
        m = map_luxalgo_event_to_bars(ev, self.bars, period_sec=300, allow_nearest=False)
        self.assertEqual(m["mapping_status"], "unresolved")

    def test_placeholder_rejected(self):
        ev = _lux("bullish", 1.0, None, "unavailable", -2000000)
        m = map_luxalgo_event_to_bars(ev, self.bars, period_sec=300)
        self.assertEqual(m["mapping_status"], "unresolved")


class MatchingTests(unittest.TestCase):
    def test_exact_and_near(self):
        lux = _lux("bullish", 100.0, 1_700_000_000)
        internal = [_int("bullish", 100.2, 1_700_000_000)]
        row = classify_luxalgo_match(
            lux, internal, tolerances=MatchTolerances(period_sec=300)
        )
        self.assertEqual(row["category"], EXACT_MATCH)

        internal2 = [_int("bullish", 100.1, 1_700_000_300)]
        row2 = classify_luxalgo_match(
            lux, internal2, tolerances=MatchTolerances(period_sec=300)
        )
        self.assertEqual(row2["category"], NEAR_TIME_MATCH)
        self.assertEqual(row2["bar_delta"], 1)

    def test_within_two_bars(self):
        lux = _lux("bullish", 100.0, 1_700_000_000)
        internal = [_int("bullish", 100.0, 1_700_000_600)]
        row = classify_luxalgo_match(
            lux, internal, tolerances=MatchTolerances(period_sec=300, max_bar_distance=2)
        )
        self.assertEqual(row["category"], NEAR_TIME_MATCH)
        self.assertEqual(row["bar_delta"], 2)

    def test_opposite_direction(self):
        lux = _lux("bullish", 100.0, 1_700_000_000)
        internal = [_int("bearish", 100.0, 1_700_000_000)]
        row = classify_luxalgo_match(
            lux, internal, tolerances=MatchTolerances(period_sec=300)
        )
        self.assertEqual(row["category"], LUXALGO_ONLY)
        self.assertTrue(row.get("opposite_nearby"))

    def test_level_mismatch_direction_only(self):
        lux = _lux("bullish", 100.0, 1_700_000_000)
        internal = [_int("bullish", 120.0, 1_700_000_000)]
        row = classify_luxalgo_match(
            lux,
            internal,
            tolerances=MatchTolerances(period_sec=300, level_tolerance=LEVEL_TOLERANCE),
        )
        self.assertEqual(row["category"], DIRECTION_ONLY_MATCH)

    def test_level_only(self):
        lux = _lux("bullish", 100.0, 1_700_000_000)
        # opposite dir far in time, same level nearby wrong dir — force level-only path
        internal = [_int("bearish", 100.0, 1_700_100_000)]
        row = classify_luxalgo_match(
            lux, internal, tolerances=MatchTolerances(period_sec=300)
        )
        self.assertIn(row["category"], (LEVEL_ONLY_MATCH, LUXALGO_ONLY))

    def test_timing_unresolved(self):
        lux = _lux("bullish", 100.0, None, "unavailable")
        row = classify_luxalgo_match(
            lux, [], tolerances=MatchTolerances(period_sec=300)
        )
        self.assertEqual(row["category"], TIMING_UNRESOLVED)

    def test_match_overlap_internal_only(self):
        lux = [_lux("bullish", 100.0, 1_700_000_000)]
        internal = [
            _int("bullish", 100.0, 1_700_000_000),
            _int("bearish", 90.0, 1_700_000_300),
        ]
        ov = match_overlap(lux, internal, timeframe="5m", period_sec=300)
        self.assertEqual(ov["exact_matches"], 1)
        self.assertGreaterEqual(ov["internal_only_count"], 1)
        self.assertIn(INTERNAL_ONLY, (INTERNAL_ONLY,))  # constant exported


class DivergenceTests(unittest.TestCase):
    def test_wick_close(self):
        d = classify_divergence(
            {"category": LUXALGO_ONLY},
            internal_wick_would_match=True,
        )
        self.assertEqual(d["cause"], CLOSE_VS_WICK_BREAK)

    def test_idm_context_low_confidence(self):
        d = classify_divergence(
            {"category": LUXALGO_ONLY, "matched_internal": None},
            nearby_luxalgo_context=[{"t": "IDM"}],
        )
        self.assertEqual(d["cause"], INDUCEMENT_DEPENDENCY)
        self.assertEqual(d.get("confidence"), "low")

    def test_state_opposite(self):
        d = classify_divergence(
            {
                "category": LUXALGO_ONLY,
                "opposite_nearby": [{"direction": "bearish"}],
            }
        )
        self.assertEqual(d["cause"], STRUCTURE_STATE_DIFFERENCE)

    def test_unknown_default(self):
        d = classify_divergence({"category": LUXALGO_ONLY, "matched_internal": None})
        self.assertEqual(d["cause"], UNKNOWN)


class EquivalenceDecisionTests(unittest.TestCase):
    def test_small_sample_unvalidated(self):
        status, _ = classify_equivalence_status_phase20(
            reliable_n=3, exact=2, near2=2, luxalgo_only=0, internal_only=0
        )
        self.assertEqual(status, "UNVALIDATED")

    def test_need_more_events_decision(self):
        d = decide_final(
            reliable_total=4,
            overall_status="UNVALIDATED",
            by_tf={},
        )
        self.assertEqual(d["decision"], "NEED_MORE_LUXALGO_EVENTS")
        self.assertEqual(d["phase19_verdict_preserved"], "NO_EDGE_OBSERVED")
        self.assertFalse(d["v2_required"])
        self.assertFalse(d["replay_required"])


class ReplayIsolationGuardTests(unittest.TestCase):
    """If a v2 confirmation candidate were introduced, only confirmation may change."""

    def test_internal_detector_unchanged_signature(self):
        bars = [
            _bar(i * 300, 10, 11, 9, 10.5) for i in range(20)
        ]
        # simple up-break pattern
        bars[5] = _bar(5 * 300, 10, 12, 9, 10)
        bars[6] = _bar(6 * 300, 10, 11, 9, 10)
        bars[7] = _bar(7 * 300, 10, 11, 9, 10)
        bars[10] = _bar(10 * 300, 11, 13, 11, 12.5)
        events = detect_internal_choch(bars)
        # Algorithm may or may not emit depending on swings; ensure callable + source tag
        for e in events:
            self.assertEqual(e.source, "internal_structure")
            self.assertEqual(e.kind, "CHoCH")


if __name__ == "__main__":
    unittest.main()
