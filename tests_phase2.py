"""Unit tests for Phase 2 / 2.5 session + sweep logic (no CDP required)."""

from __future__ import annotations

import unittest
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from liquidity_sweep import detect_first_sweep, detect_sweeps
from models import Bar, SessionRange, SweepRule
from ohlc_sessions import compute_session_ranges
from session_time import SessionDefinition, resolve_session_window
from sessions_config import SESSION_DEFINITIONS


def _ts(y, m, d, hh, mm, tz_name="UTC") -> int:
    tz = timezone.utc if tz_name == "UTC" else ZoneInfo(tz_name)
    return int(datetime(y, m, d, hh, mm, tzinfo=tz).timestamp())


def _bar(ts: int, o: float, h: float, l: float, c: float) -> Bar:
    return Bar(time=ts, open=o, high=h, low=l, close=c)


class SessionDefinitionTests(unittest.TestCase):
    def test_asia_crosses_midnight(self):
        d = SESSION_DEFINITIONS["Asia"]
        self.assertTrue(d.crosses_midnight)
        self.assertEqual(d.reference_timezone, "America/New_York")
        self.assertEqual(d.local_start, "20:00")
        self.assertEqual(d.local_end, "03:00")

    def test_london_no_midnight_cross(self):
        d = SESSION_DEFINITIONS["London"]
        self.assertFalse(d.crosses_midnight)
        self.assertEqual(d.local_start, "03:00")
        self.assertEqual(d.local_end, "08:30")

    def test_summer_utc_conversion_us_dst(self):
        # 2026-08-13 20:00 America/New_York (EDT, UTC-4) → 2026-08-14 00:00 UTC
        asia = resolve_session_window(SESSION_DEFINITIONS["Asia"], date(2026, 8, 13))
        self.assertEqual(asia.utc_offset_start, "-04:00")
        self.assertTrue(asia.dst_active)
        self.assertEqual(
            datetime.fromtimestamp(asia.utc_start, tz=timezone.utc).isoformat(),
            "2026-08-14T00:00:00+00:00",
        )
        self.assertEqual(
            datetime.fromtimestamp(asia.utc_end, tz=timezone.utc).isoformat(),
            "2026-08-14T07:00:00+00:00",
        )

        london = resolve_session_window(SESSION_DEFINITIONS["London"], date(2026, 8, 14))
        self.assertEqual(london.utc_offset_start, "-04:00")
        self.assertEqual(
            datetime.fromtimestamp(london.utc_start, tz=timezone.utc).isoformat(),
            "2026-08-14T07:00:00+00:00",
        )
        self.assertEqual(
            datetime.fromtimestamp(london.utc_end, tz=timezone.utc).isoformat(),
            "2026-08-14T12:30:00+00:00",
        )

    def test_winter_utc_conversion_us_standard(self):
        # After US DST ends (2026-11-01): EST UTC-5
        asia = resolve_session_window(SESSION_DEFINITIONS["Asia"], date(2026, 11, 10))
        self.assertEqual(asia.utc_offset_start, "-05:00")
        self.assertFalse(asia.dst_active)
        self.assertEqual(
            datetime.fromtimestamp(asia.utc_start, tz=timezone.utc).isoformat(),
            "2026-11-11T01:00:00+00:00",
        )
        london = resolve_session_window(SESSION_DEFINITIONS["London"], date(2026, 11, 11))
        self.assertEqual(london.utc_offset_start, "-05:00")
        self.assertEqual(
            datetime.fromtimestamp(london.utc_start, tz=timezone.utc).isoformat(),
            "2026-11-11T08:00:00+00:00",
        )
        self.assertEqual(
            datetime.fromtimestamp(london.utc_end, tz=timezone.utc).isoformat(),
            "2026-11-11T13:30:00+00:00",
        )

    def test_us_dst_transition_changes_utc(self):
        before = resolve_session_window(SESSION_DEFINITIONS["London"], date(2026, 3, 6))
        after = resolve_session_window(SESSION_DEFINITIONS["London"], date(2026, 3, 9))
        self.assertEqual(before.utc_offset_start, "-05:00")
        self.assertEqual(after.utc_offset_start, "-04:00")
        # Same local 03:00, different UTC
        self.assertNotEqual(before.utc_start % 86400, after.utc_start % 86400)

    def test_eu_dst_does_not_change_ny_definition(self):
        # Between US and EU spring transitions, NY already on DST.
        pre_eu = resolve_session_window(SESSION_DEFINITIONS["London"], date(2026, 3, 20))
        post_eu = resolve_session_window(SESSION_DEFINITIONS["London"], date(2026, 3, 30))
        self.assertEqual(pre_eu.utc_offset_start, post_eu.utc_offset_start)
        self.assertEqual(pre_eu.utc_offset_start, "-04:00")

    def test_chart_timezone_independence(self):
        # Resolution uses definition timezone only; Berlin vs NY chart irrelevant.
        w = resolve_session_window(SESSION_DEFINITIONS["Asia"], date(2026, 8, 13))
        self.assertEqual(w.reference_timezone, "America/New_York")
        self.assertNotIn("Berlin", w.reference_timezone)


class OhlcSessionTests(unittest.TestCase):
    def test_asia_london_levels_summer(self):
        # Build UTC bars covering Asia 00:00-07:00 and London 07:00-12:30 on 2026-08-14.
        bars = []
        base = _ts(2026, 8, 14, 0, 0)
        for i in range(0, 84):  # Asia
            t = base + i * 300
            o = c = 4000.0
            h, l = 4001.0, 3999.0
            if i == 10:
                h = 4100.0
            if i == 20:
                l = 3900.0
            bars.append(_bar(t, o, h, l, c))
        for i in range(84, 150):  # London 07:00-12:30
            t = base + i * 300
            o = c = 4050.0
            h, l = 4051.0, 4049.0
            if i == 90:
                h = 4200.0
            if i == 120:
                l = 3950.0
            bars.append(_bar(t, o, h, l, c))

        now = base + 24 * 3600
        ranges = compute_session_ranges(bars, resolution_minutes=5, now_ts=now)
        asia = [r for r in ranges if r.name == "Asia" and r.complete]
        london = [r for r in ranges if r.name == "London" and r.complete]
        self.assertTrue(asia)
        self.assertTrue(london)
        self.assertEqual(asia[-1].high, 4100.0)
        self.assertEqual(asia[-1].low, 3900.0)
        self.assertEqual(london[-1].high, 4200.0)
        self.assertEqual(london[-1].low, 3950.0)
        self.assertEqual(asia[-1].coverage_status, "full")
        self.assertEqual(asia[-1].timezone, "America/New_York")

    def test_incomplete_session_not_marked_complete(self):
        base = _ts(2026, 8, 14, 0, 0)
        bars = [_bar(base + i * 300, 4000, 4001, 3999, 4000) for i in range(12)]
        now = base + 3600
        ranges = compute_session_ranges(bars, resolution_minutes=5, now_ts=now)
        asia = [r for r in ranges if r.name == "Asia"]
        self.assertTrue(asia)
        self.assertFalse(asia[-1].complete)


class SweepTests(unittest.TestCase):
    def _session(self, high=4100.0, low=3900.0, end=None) -> SessionRange:
        return SessionRange(
            name="Asia",
            timezone="America/New_York",
            start=_ts(2026, 8, 14, 0, 0),
            end=end or _ts(2026, 8, 14, 7, 0),
            high=high,
            low=low,
            high_timestamp=None,
            low_timestamp=None,
            complete=True,
            source="internal_ohlc",
            coverage_status="full",
            identity="Asia:test",
        )

    def test_wick_only_low_sweep(self):
        session = self._session()
        bars = [
            _bar(_ts(2026, 8, 14, 8, 0), 3950, 3960, 3940, 3955),
            _bar(_ts(2026, 8, 14, 8, 5), 3955, 3960, 3890, 3910),
        ]
        sweep = detect_first_sweep(session, bars, rule=SweepRule.WICK_ONLY, side="low")
        self.assertIsNotNone(sweep)
        assert sweep is not None
        self.assertEqual(sweep.side, "low")
        self.assertEqual(sweep.level, 3900.0)
        self.assertTrue(sweep.reclaim_status)

    def test_no_sweep(self):
        session = self._session()
        bars = [_bar(_ts(2026, 8, 14, 8, 0), 4000, 4010, 3990, 4005)]
        self.assertEqual(detect_sweeps(session, bars, rule=SweepRule.WICK_ONLY), [])

    def test_touch_vs_wick(self):
        session = self._session(high=4100.0)
        bars = [_bar(_ts(2026, 8, 14, 8, 0), 4090, 4110, 4085, 4105)]
        self.assertTrue(detect_sweeps(session, bars, rule=SweepRule.TOUCH, sides=["high"]))
        self.assertEqual(
            detect_sweeps(session, bars, rule=SweepRule.WICK_ONLY, sides=["high"]), []
        )

    def test_rejects_non_primary_session(self):
        session = SessionRange(
            name="New York",
            timezone="America/New_York",
            start=1,
            end=2,
            high=1.0,
            low=0.5,
            high_timestamp=None,
            low_timestamp=None,
            complete=True,
            source="internal_ohlc",
            coverage_status="full",
        )
        bars = [_bar(3, 1, 2, 0, 1)]
        self.assertEqual(detect_sweeps(session, bars), [])


if __name__ == "__main__":
    unittest.main()
