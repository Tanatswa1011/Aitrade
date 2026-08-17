"""Unit tests for Phase 3 LuxAlgo CHoCH confirmation (no CDP required)."""

from __future__ import annotations

import unittest

from models import Bar, LiquiditySweep, StructureConfirmation
from luxalgo_structure import normalize_choch_events
from structure_confirm import confirm_after_sweep, required_direction_for_sweep


def _bar(ts: int, o: float = 100.0, h: float = 101.0, l: float = 99.0, c: float = 100.5) -> Bar:
    return Bar(time=ts, open=o, high=h, low=l, close=c)


def _sweep(*, session: str, side: str, level: float, ts: int) -> LiquiditySweep:
    return LiquiditySweep(
        session=session,
        side=side,
        level=level,
        sweep_timestamp=ts,
        sweep_price=level + (1 if side == "high" else -1),
        maximum_excursion=1.0,
        reclaim_status=True,
        rule="wick_only",
        sweep_candle=_bar(ts),
    )


def _choch(
    *,
    direction: str,
    level: float,
    ts: int | None,
    bar_index: int | None = None,
    timing: str = "exact",
    raw_id: str = "1",
    kind: str = "CHoCH",
) -> StructureConfirmation:
    return StructureConfirmation(
        kind=kind,
        direction=direction,
        level=level,
        event_timestamp=ts,
        event_bar_index=bar_index,
        source="luxalgo",
        study_id="smUEv2",
        raw_id=raw_id,
        timing_confidence=timing,
    )


class RequiredDirectionTests(unittest.TestCase):
    def test_low_requires_bullish(self):
        s = _sweep(session="Asia", side="low", level=4300.0, ts=1000)
        self.assertEqual(required_direction_for_sweep(s), "bullish")

    def test_high_requires_bearish(self):
        s = _sweep(session="London", side="high", level=4400.0, ts=1000)
        self.assertEqual(required_direction_for_sweep(s), "bearish")


class ConfirmAfterSweepTests(unittest.TestCase):
    def test_valid_bullish_after_asia_low(self):
        sweep = _sweep(session="Asia", side="low", level=4300.0, ts=1_000)
        events = [
            _choch(direction="bullish", level=4310.0, ts=1_500, raw_id="a"),
        ]
        decision = confirm_after_sweep(sweep, events)
        self.assertTrue(decision.confirmed)
        self.assertEqual(decision.reason, "confirmed")
        self.assertEqual(decision.confirmation.direction, "bullish")
        self.assertEqual(decision.confirmation.level, 4310.0)
        self.assertEqual(decision.required_direction, "bullish")

    def test_valid_bearish_after_london_high(self):
        sweep = _sweep(session="London", side="high", level=4400.0, ts=2_000)
        events = [
            _choch(direction="bearish", level=4380.0, ts=2_800, raw_id="b"),
        ]
        decision = confirm_after_sweep(sweep, events)
        self.assertTrue(decision.confirmed)
        self.assertEqual(decision.confirmation.direction, "bearish")
        self.assertEqual(decision.confirmation.level, 4380.0)

    def test_wrong_direction_ignored(self):
        sweep = _sweep(session="Asia", side="low", level=4300.0, ts=1_000)
        events = [
            _choch(direction="bearish", level=4290.0, ts=1_500, raw_id="wrong"),
        ]
        decision = confirm_after_sweep(sweep, events)
        self.assertFalse(decision.confirmed)
        self.assertEqual(decision.reason, "no_direction_aligned_choch")
        self.assertIsNone(decision.confirmation)

    def test_choch_before_sweep_ignored(self):
        sweep = _sweep(session="Asia", side="low", level=4300.0, ts=2_000)
        events = [
            _choch(direction="bullish", level=4310.0, ts=1_000, raw_id="old"),
        ]
        decision = confirm_after_sweep(sweep, events)
        self.assertFalse(decision.confirmed)
        self.assertEqual(decision.reason, "no_choch_after_sweep")

    def test_first_valid_of_multiple(self):
        sweep = _sweep(session="Asia", side="low", level=4300.0, ts=1_000)
        events = [
            _choch(direction="bullish", level=4305.0, ts=1_200, raw_id="first"),
            _choch(direction="bullish", level=4315.0, ts=1_800, raw_id="second"),
            _choch(direction="bearish", level=4320.0, ts=1_100, raw_id="noise"),
        ]
        decision = confirm_after_sweep(sweep, events)
        self.assertTrue(decision.confirmed)
        self.assertEqual(decision.confirmation.raw_id, "first")
        self.assertEqual(decision.confirmation.level, 4305.0)

    def test_no_choch(self):
        sweep = _sweep(session="London", side="high", level=4400.0, ts=1_000)
        decision = confirm_after_sweep(sweep, [])
        self.assertFalse(decision.confirmed)
        self.assertEqual(decision.reason, "no_choch_events")
        self.assertIsNone(decision.confirmation)

    def test_unreliable_ordering_fail_closed(self):
        sweep = _sweep(session="Asia", side="low", level=4300.0, ts=1_000)
        events = [
            _choch(
                direction="bullish",
                level=4310.0,
                ts=None,
                bar_index=None,
                timing="unavailable",
                raw_id="ghost",
            ),
        ]
        decision = confirm_after_sweep(sweep, events)
        self.assertFalse(decision.confirmed)
        self.assertEqual(decision.reason, "no_reliable_ordering")
        self.assertTrue(
            any(r.get("reason") == "unreliable_ordering" for r in decision.rejected)
        )

    def test_bos_never_confirms(self):
        sweep = _sweep(session="Asia", side="low", level=4300.0, ts=1_000)
        events = [
            _choch(
                direction="bullish",
                level=4310.0,
                ts=1_500,
                kind="BOS",
                raw_id="bos",
            ),
        ]
        decision = confirm_after_sweep(sweep, events)
        self.assertFalse(decision.confirmed)
        self.assertEqual(decision.reason, "no_choch_events")
        self.assertTrue(any(r.get("reason") == "not_choch" for r in decision.rejected))

    def test_bar_index_ordering_when_timestamp_missing(self):
        sweep = _sweep(session="London", side="high", level=4400.0, ts=5_000)
        events = [
            _choch(
                direction="bearish",
                level=4380.0,
                ts=None,
                bar_index=120,
                timing="derived",
                raw_id="bi",
            ),
        ]
        decision = confirm_after_sweep(sweep, events, sweep_bar_index=100)
        self.assertTrue(decision.confirmed)
        self.assertEqual(decision.confirmation.raw_id, "bi")

        # Same event but sweep bar is later → fail.
        decision2 = confirm_after_sweep(sweep, events, sweep_bar_index=200)
        self.assertFalse(decision2.confirmed)
        self.assertEqual(decision2.reason, "no_choch_after_sweep")

    def test_same_timestamp_not_after(self):
        sweep = _sweep(session="Asia", side="low", level=4300.0, ts=1_000)
        events = [
            _choch(direction="bullish", level=4310.0, ts=1_000, raw_id="same"),
        ]
        decision = confirm_after_sweep(sweep, events)
        self.assertFalse(decision.confirmed)
        self.assertEqual(decision.reason, "no_choch_after_sweep")


class NormalizeChochTests(unittest.TestCase):
    def test_normalize_pairs_dashed_line_and_direction(self):
        payload = {
            "ok": True,
            "studyId": "smUEv2",
            "bullColor": 4286683400,
            "bearColor": 4283585279,
            "labels": [
                {
                    "id": 10,
                    "t": "CHoCH",
                    "y": 100.5,
                    "x": 50,
                    "st": "ldn",
                    "tci": 4286683400,
                    "indexMapped": 20,
                },
                {
                    "id": 11,
                    "t": "BOS",
                    "y": 101.0,
                    "x": 51,
                    "st": "lup",
                    "tci": 4283585279,
                    "indexMapped": 21,
                },
            ],
            "lines": [
                {
                    "id": 1,
                    "y1": 100.5,
                    "st": "dsh",
                    "ci": 4286683400,
                    "x1": 40,
                    "x2": 50,
                    "indexMapped1": 10,
                    "indexMapped2": 20,
                }
            ],
        }
        bars = {20: 9_000}
        events = normalize_choch_events(payload, bars_by_series_index=bars)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].kind, "CHoCH")
        self.assertEqual(events[0].direction, "bullish")
        self.assertEqual(events[0].level, 100.5)
        self.assertEqual(events[0].event_timestamp, 9_000)
        self.assertEqual(events[0].timing_confidence, "exact")

    def test_placeholder_index_unavailable(self):
        payload = {
            "ok": True,
            "studyId": "smUEv2",
            "bullColor": 4286683400,
            "bearColor": 4283585279,
            "labels": [
                {
                    "id": 99,
                    "t": "CHoCH",
                    "y": 50.0,
                    "x": 1,
                    "st": "lup",
                    "tci": 4283585279,
                    "indexMapped": -2000000,
                }
            ],
            "lines": [],
        }
        events = normalize_choch_events(payload, bars_by_series_index={})
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].timing_confidence, "unavailable")
        self.assertIsNone(events[0].event_timestamp)


if __name__ == "__main__":
    unittest.main()
