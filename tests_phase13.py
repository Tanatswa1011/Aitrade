"""Phase 13 tests: trading-day Daily, study rediscovery, MTF journal."""

from __future__ import annotations

import unittest
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from closed_candles import bar_close_ts, filter_closed_bars
from htf_report import (
    compute_mtf_journal_report,
    htf_report_bucket,
    paired_execution_comparison,
)
from journal_models import HistoricalEntryResult, SetupJournalRecord
from models import Bar
from ohlc_resample import resample_ohlc
from replay_engine import replay_historical_mtf_setups
from replay_fixtures import build_multi_day_fixture_bars
from setup_engine import make_liquidity_event_id, make_setup_id
from study_discovery import compare_study_snapshots, rediscover_studies
from trading_day_config import (
    DEFAULT_TRADING_DAY_CONFIG,
    TradingDayConfig,
    daily_bar_close_ts,
    describe_daily_boundaries,
    infer_trading_day_config_from_native_bars,
    is_weekend,
    iter_trading_dates,
    trading_day_close_utc,
    trading_day_open_utc,
)


def _rec(**kwargs) -> SetupJournalRecord:
    base = dict(
        setup_id="X|Asia|2026-01-05|low|1",
        symbol="X",
        timeframe="5m",
        trading_date="2026-01-05",
        session="Asia",
        direction="bullish",
        swept_side="low",
        session_high=100.0,
        session_low=90.0,
        sweep_level=90.0,
        sweep_extreme=89.0,
        sweep_timestamp=1,
        confirmation_source="test",
        confirmation_algorithm="t",
        confirmation_timestamp=2,
        confirmation_level=91.0,
        confirmation_equivalence_status="unvalidated_against_luxalgo",
        fvg_low=92.0,
        fvg_high=93.0,
        fvg_midpoint=92.5,
        fvg_created_timestamp=3,
        status="ENTRY_READY",
        expiry_reason=None,
        invalidation_reason=None,
        reliability_flags=[],
        entry_results=[
            HistoricalEntryResult(
                mode="first_touch",
                triggered=True,
                entry_price=92.5,
                entry_timestamp=4,
                entry_depth=0.5,
                max_retrace_depth=0.5,
                stop_price=89.0,
                risk_distance=3.5,
                outcome="1R_HIT",
                mfe_r=1.2,
                mae_r=0.3,
            )
        ],
        strategy_version="v1.phase13",
        config_hash="abc",
        structure_algorithm_version="internal_choch_v1",
        daily_bias="bullish",
        h4_bias="bullish",
        htf_alignment="aligned_bullish",
        execution_timeframe="5m",
        setup_vs_daily="aligned",
        setup_vs_h4="aligned",
        daily_bias_confidence="high",
        h4_bias_confidence="medium",
        liquidity_event_id="X|Asia|2026-01-05|low|1",
    )
    base.update(kwargs)
    return SetupJournalRecord(**base)


class TradingDayBoundaryTests(unittest.TestCase):
    def test_native_boundary_resolution_17ny(self):
        cfg = TradingDayConfig(day_roll_time="17:00", source="test")
        # 2026-01-05 is Monday, EST (UTC-5) → 17:00 local
        open_ts = trading_day_open_utc(cfg, date(2026, 1, 5))
        close_ts = trading_day_close_utc(cfg, date(2026, 1, 5))
        local = datetime.fromtimestamp(open_ts, tz=timezone.utc).astimezone(
            ZoneInfo("America/New_York")
        )
        self.assertEqual(local.hour, 17)
        self.assertEqual(local.minute, 0)
        self.assertEqual(close_ts, trading_day_open_utc(cfg, date(2026, 1, 6)))
        # Winter: duration happens to be 86400; DST cases cover non-86400
        self.assertEqual(close_ts - open_ts, 86400)

    def test_dst_spring_forward_23h(self):
        cfg = TradingDayConfig(day_roll_time="17:00")
        # US spring 2026: 2026-03-08
        # Day opening Fri 2026-03-06 17:00 EST → Sat 03-07 17:00 EST (still EST)
        # Day opening Sat 03-07 → Sun 03-08 17:00 EDT spans spring transition night
        open_before = trading_day_open_utc(cfg, date(2026, 3, 7))  # Sat 17:00 EST
        close_after = trading_day_close_utc(cfg, date(2026, 3, 7))  # Sun 17:00 EDT
        duration = close_after - open_before
        self.assertEqual(duration, 23 * 3600)
        self.assertNotEqual(duration, 86400)

    def test_dst_fall_back_25h(self):
        cfg = TradingDayConfig(day_roll_time="17:00")
        # US fall 2026: 2026-11-01
        open_ts = trading_day_open_utc(cfg, date(2026, 10, 31))  # Sat 17:00 EDT
        close_ts = trading_day_close_utc(cfg, date(2026, 10, 31))  # Sun 17:00 EST
        self.assertEqual(close_ts - open_ts, 25 * 3600)

    def test_weekend_gaps_no_fabricate(self):
        dates = iter_trading_dates(date(2026, 1, 2), date(2026, 1, 5))
        self.assertTrue(all(not is_weekend(d) for d in dates))
        self.assertNotIn(date(2026, 1, 3), dates)  # Saturday
        self.assertNotIn(date(2026, 1, 4), dates)  # Sunday

    def test_daily_closed_bar_at_sweep(self):
        cfg = DEFAULT_TRADING_DAY_CONFIG
        d0, d1, d2 = date(2026, 1, 5), date(2026, 1, 6), date(2026, 1, 7)
        bars = [
            Bar(time=trading_day_open_utc(cfg, d0), open=1, high=2, low=0.5, close=1.5),
            Bar(time=trading_day_open_utc(cfg, d1), open=1.5, high=2.5, low=1, close=2),
            Bar(time=trading_day_open_utc(cfg, d2), open=2, high=3, low=1.5, close=2.5),
        ]
        # Sweep during d1 bar (before d1 close) → only d0 closed
        sweep = trading_day_open_utc(cfg, d1) + 3600
        closed = filter_closed_bars(bars, as_of_ts=sweep, timeframe="1D")
        self.assertEqual([b.time for b in closed], [bars[0].time])
        # After d1 close, d1 included
        after = trading_day_close_utc(cfg, d1)
        closed2 = filter_closed_bars(bars, as_of_ts=after, timeframe="1D")
        self.assertEqual([b.time for b in closed2], [bars[0].time, bars[1].time])

    def test_infer_from_native_opens(self):
        from datetime import timedelta

        cfg = TradingDayConfig(day_roll_time="17:00")
        bars = []
        d = date(2026, 1, 5)
        while len(bars) < 8:
            if not is_weekend(d):
                bars.append(
                    Bar(
                        time=trading_day_open_utc(cfg, d),
                        open=1,
                        high=2,
                        low=0.5,
                        close=1,
                    )
                )
            d += timedelta(days=1)
        inferred = infer_trading_day_config_from_native_bars(bars)
        self.assertEqual(inferred.day_roll_time, "17:00")
        self.assertEqual(inferred.source, "native_tv_daily_opens")

    def test_bar_close_not_naive_86400_across_dst(self):
        cfg = TradingDayConfig(day_roll_time="17:00")
        open_ts = trading_day_open_utc(cfg, date(2026, 3, 7))
        close_ts = bar_close_ts(
            Bar(time=open_ts, open=1, high=2, low=0.5, close=1),
            "1D",
            trading_day=cfg,
        )
        self.assertEqual(close_ts - open_ts, 23 * 3600)

    def test_resample_daily_uses_trading_day(self):
        cfg = DEFAULT_TRADING_DAY_CONFIG
        # 5m bars across one trading day
        start = trading_day_open_utc(cfg, date(2026, 1, 5))
        end = trading_day_close_utc(cfg, date(2026, 1, 5))
        bars = []
        t = start
        px = 100.0
        while t < end:
            bars.append(Bar(time=t, open=px, high=px + 1, low=px - 1, close=px))
            t += 300
            px += 0.01
        series = resample_ohlc(bars, "1D", as_of_ts=end, trading_day=cfg)
        self.assertEqual(series.source, "resampled")
        self.assertEqual(len(series.bars), 1)
        self.assertEqual(int(series.bars[0].time), start)
        self.assertEqual(series.extras.get("daily_boundary"), "ny_trading_day")


class StudyRediscoveryTests(unittest.TestCase):
    def test_semantic_stable_when_id_changes(self):
        before_studies = [
            {"id": "AAA", "name": "ICT Sessions & Killzones"},
            {"id": "BBB", "name": "LuxAlgo Market Structure with Inducements & Sweeps"},
        ]
        after_studies = [
            {"id": "AAA2", "name": "ICT Sessions & Killzones"},
            {"id": "BBB2", "name": "LuxAlgo Market Structure with Inducements & Sweeps"},
        ]
        b = rediscover_studies(before_studies)
        a = rediscover_studies(after_studies, previous=[
            __import__("study_discovery").StudyIdentity(
                semantic_key="ict_sessions",
                name="ICT Sessions & Killzones",
                study_id="AAA",
                name_pattern=r"ICT\s*Sessions",
            ),
            __import__("study_discovery").StudyIdentity(
                semantic_key="luxalgo_structure",
                name="LuxAlgo Market Structure with Inducements & Sweeps",
                study_id="BBB",
                name_pattern=r"LuxAlgo|Market Structure with Inducements",
            ),
        ])
        cmp = compare_study_snapshots(b, a)
        self.assertTrue(cmp["all_tracked_present_after"])
        self.assertTrue(cmp["any_id_changed"])
        self.assertTrue(a["id_changes"])


class PairedJournalIdentityTests(unittest.TestCase):
    def test_liquidity_event_shared_exec_distinct(self):
        eid = make_liquidity_event_id(
            symbol="X",
            session="Asia",
            trading_date="2026-01-05",
            sweep_side="low",
            sweep_timestamp=100,
        )
        a = make_setup_id(
            symbol="X",
            session="Asia",
            trading_date="2026-01-05",
            sweep_side="low",
            sweep_timestamp=100,
            execution_timeframe="5m",
        )
        b = make_setup_id(
            symbol="X",
            session="Asia",
            trading_date="2026-01-05",
            sweep_side="low",
            sweep_timestamp=100,
            execution_timeframe="15m",
        )
        self.assertEqual(a.split("|exec:")[0], eid)
        self.assertEqual(b.split("|exec:")[0], eid)
        self.assertNotEqual(a, b)

    def test_paired_rows(self):
        eid = "X|Asia|2026-01-05|low|1"
        rows = [
            _rec(
                setup_id=eid + "|exec:5m",
                execution_timeframe="5m",
                liquidity_event_id=eid,
                confirmation_timestamp=10,
                fvg_created_timestamp=20,
            ),
            _rec(
                setup_id=eid + "|exec:15m",
                execution_timeframe="15m",
                liquidity_event_id=eid,
                confirmation_timestamp=15,
                fvg_created_timestamp=25,
                timeframe="15m",
            ),
        ]
        paired = paired_execution_comparison(rows)
        self.assertEqual(paired["paired_event_count"], 1)
        self.assertEqual(paired["pairs"][0]["which_confirmed_first"], "5m")
        self.assertEqual(paired["pairs"][0]["which_fvg_first"], "5m")


class HtfBucketTests(unittest.TestCase):
    def test_buckets(self):
        self.assertEqual(
            htf_report_bucket(_rec(setup_vs_daily="aligned", setup_vs_h4="aligned")),
            "aligned_both",
        )
        self.assertEqual(
            htf_report_bucket(_rec(setup_vs_daily="aligned", setup_vs_h4="opposed")),
            "aligned_daily_only",
        )
        self.assertEqual(
            htf_report_bucket(_rec(setup_vs_daily="opposed", setup_vs_h4="aligned")),
            "aligned_h4_only",
        )
        self.assertEqual(
            htf_report_bucket(_rec(setup_vs_daily="opposed", setup_vs_h4="opposed")),
            "opposed_both",
        )
        self.assertEqual(
            htf_report_bucket(
                _rec(
                    setup_vs_daily="unknown",
                    setup_vs_h4="unknown",
                    htf_alignment="unknown",
                    daily_bias="unknown",
                    h4_bias="unknown",
                )
            ),
            "neutral_or_unknown",
        )

    def test_report_marks_insufficient_sample(self):
        report = compute_mtf_journal_report([_rec()])
        self.assertEqual(report["journal_size"], 1)
        both = report["funnel_by_htf_bucket"]["aligned_both"]
        self.assertEqual(both["sample_warning"], "INSUFFICIENT_SAMPLE")

    def test_unknown_bias_aliases(self):
        r = _rec(daily_bias="unknown", h4_bias="unknown", daily_bias_confidence=None)
        d = r.to_dict()
        self.assertEqual(d["daily_confidence"], "unknown")
        self.assertIsNone(d["daily_break_timestamp"])


class MtfReplaySmokeTests(unittest.TestCase):
    def test_dual_execution_replay(self):
        bars5 = build_multi_day_fixture_bars()
        result = replay_historical_mtf_setups(
            {"5m": bars5},
            symbol="OANDA:XAUUSD",
            execution_timeframes=("5m", "15m"),
        )
        self.assertGreaterEqual(result.total_setups, 1)
        tfs = {r.execution_timeframe for r in result.journal_records}
        self.assertIn("5m", tfs)
        self.assertIn("15m", tfs)
        # paired identity
        eids = {}
        for r in result.journal_records:
            eids.setdefault(r.liquidity_event_id, set()).add(r.execution_timeframe)
        self.assertTrue(any(len(v) == 2 for v in eids.values()) or result.total_setups >= 2)


class RestoreStateHandlingTests(unittest.TestCase):
    def test_describe_boundaries_shape(self):
        cfg = DEFAULT_TRADING_DAY_CONFIG
        bars = [
            Bar(time=trading_day_open_utc(cfg, date(2026, 1, 5)), open=1, high=2, low=0.5, close=1),
            Bar(time=trading_day_open_utc(cfg, date(2026, 1, 6)), open=1, high=2, low=0.5, close=1),
        ]
        desc = describe_daily_boundaries(bars, cfg=cfg)
        self.assertEqual(desc["bar_count"], 2)
        self.assertIn("config", desc)


if __name__ == "__main__":
    unittest.main()
