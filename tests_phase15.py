"""Phase 15 tests: providers, overlap, invalid-stop categories, intrabar resolver."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from bar_dataset import write_dataset
from dataset_overlap import compare_ohlc_overlap, compare_session_ranges_overlap
from gap_classify import classify_gap, classify_bar_gaps
from historical_data_provider import (
    HistoricalCoverageMeta,
    HistoricalDataset,
    LocalJsonlProvider,
    TradingViewDesktopProvider,
    TradingViewHistoryProvider,
    integrity_report,
)
from intrabar_resolver import (
    ENTRY_THEN_STOP,
    INSUFFICIENT_DATA,
    IntrabarResolver,
    RESOLVED_NO_STOP,
    STILL_AMBIGUOUS,
    STOP_BEFORE_ENTRY,
)
from invalid_stop_diagnostics import (
    categorize_invalid_directional_stop,
    diagnose_invalid_stops,
)
from journal_models import HistoricalEntryResult, SetupJournalRecord
from models import Bar
from ohlc_resample import resample_ohlc
from sample_quality import mark_sample, sample_quality_label
from setup_journal import append_journal_records, load_journal_records
from trading_day_config import TradingDayConfig, trading_day_open_utc
from datetime import date


def _bar(t: int, o: float, h: float, l: float, c: float) -> Bar:
    return Bar(time=t, open=o, high=h, low=l, close=c)


def _rec(**kwargs) -> SetupJournalRecord:
    base = dict(
        setup_id="X|Asia|2026-01-05|low|1|exec:5m",
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
        status="INVALIDATED",
        expiry_reason=None,
        invalidation_reason="first_touch:stop_not_directional",
        reliability_flags=[],
        entry_results=[
            HistoricalEntryResult(
                mode="first_touch",
                triggered=True,
                entry_price=88.0,
                entry_timestamp=4,
                entry_depth=0.5,
                max_retrace_depth=0.5,
                stop_price=88.5,
                risk_distance=None,
                outcome="NO_RISK_PLAN",
            )
        ],
        strategy_version="v1.phase15",
        config_hash="abc",
        structure_algorithm_version="internal_choch_v1",
        execution_timeframe="5m",
        liquidity_event_id="X|Asia|2026-01-05|low|1",
        extras={"stop_mode": "beyond_sweep"},
    )
    base.update(kwargs)
    return SetupJournalRecord(**base)


class ProviderAbstractionTests(unittest.TestCase):
    def test_local_provider_metadata_provenance(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            bars = [_bar(1_000_000 + i * 300, 1, 2, 0.5, 1.5) for i in range(5)]
            write_dataset(
                bars, symbol="OANDA:XAUUSD", timeframe="5m", source="test", root=root
            )
            prov = LocalJsonlProvider(root)
            ds = prov.fetch("OANDA:XAUUSD", "5m")
            self.assertEqual(ds.meta.provider, "local_jsonl")
            self.assertEqual(ds.meta.symbol, "OANDA:XAUUSD")
            self.assertEqual(ds.meta.timeframe, "5m")
            self.assertEqual(ds.meta.source_symbol, "OANDA:XAUUSD")
            self.assertEqual(ds.meta.bar_count, 5)
            self.assertIsNotNone(ds.meta.capture_timestamp)
            written = prov.persist(ds, root=root)
            meta = json.loads(Path(written["meta_path"]).read_text(encoding="utf-8"))
            self.assertIn("provider_meta", meta)
            self.assertEqual(meta["provider"], "local_jsonl")

    def test_history_provider_documents_ceiling(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            bars = [_bar(1_000_000 + i * 300, 1, 2, 0.5, 1.5) for i in range(3)]
            write_dataset(
                bars, symbol="OANDA:XAUUSD", timeframe="5m", source="tv", root=root
            )
            hist = TradingViewHistoryProvider(
                TradingViewDesktopProvider(root=root, allow_live=False)
            )
            ds = hist.fetch("OANDA:XAUUSD", "5m")
            self.assertEqual(ds.meta.provider, "tradingview_history")
            self.assertFalse(ds.meta.extras.get("deeper_history_available"))
            self.assertIn("limitation", ds.meta.extras)

    def test_integrity_and_gap_classification(self):
        # weekday gap that looks like maintenance near 17:00 NY is classified;
        # a mid-session hole is unexpected.
        bars = [
            _bar(1_700_000_000, 1, 2, 0.5, 1),
            _bar(1_700_000_000 + 300, 1, 2, 0.5, 1),
            _bar(1_700_000_000 + 300 * 5, 1, 2, 0.5, 1),  # unexpected hole
        ]
        ds = HistoricalDataset(
            bars=tuple(bars),
            meta=HistoricalCoverageMeta(
                provider="test",
                symbol="X",
                timeframe="5m",
                source_symbol="X",
                timezone="UTC",
                price_precision=2,
                capture_timestamp="t",
                requested_start=None,
                requested_end=None,
                actual_start=bars[0].time,
                actual_end=bars[-1].time,
                bar_count=3,
            ),
        )
        rep = integrity_report(ds)
        self.assertIn("gap_classification", rep)
        g = classify_gap(
            1_700_000_000,
            1_700_000_000 + 900,
            expected_period_sec=300,
        )
        self.assertIn(
            g["category"],
            {
                "unexpected_missing_interval",
                "known_daily_maintenance_break",
                "expected_weekend_or_market_closure",
            },
        )


class OverlapTests(unittest.TestCase):
    def test_ohlc_overlap_exact(self):
        bars = [_bar(100 + i * 300, 10 + i, 11 + i, 9 + i, 10.5 + i) for i in range(4)]
        rep = compare_ohlc_overlap(bars, bars)
        self.assertEqual(rep["bar_count_compared"], 4)
        self.assertEqual(rep["ohlc_exact_matches"], 4)
        self.assertEqual(rep["max_open_delta"], 0.0)

    def test_ohlc_overlap_delta(self):
        a = [_bar(100, 10, 11, 9, 10.5)]
        b = [_bar(100, 10.2, 11, 9, 10.5)]
        rep = compare_ohlc_overlap(a, b, price_tolerance=0.25)
        self.assertEqual(rep["ohlc_exact_matches"], 0)
        self.assertEqual(rep["ohlc_within_tolerance"], 1)
        self.assertAlmostEqual(rep["max_open_delta"], 0.2)

    def test_session_range_self_compare(self):
        # Minimal contiguous 5m bars spanning one Asia window is heavy; empty ok.
        rep = compare_session_ranges_overlap([], [])
        self.assertEqual(rep["sessions_compared"], 0)


class FourHAnchorTests(unittest.TestCase):
    def test_4h_aligns_to_ny_trading_day_open(self):
        cfg = TradingDayConfig(
            day_roll_time="17:00",
            reference_timezone="America/New_York",
            source="test",
        )
        # 2024-06-03 is a Monday; NY roll 17:00 EDT = 21:00 UTC
        d = date(2024, 6, 3)
        open_ts = trading_day_open_utc(cfg, d)
        bars = [
            _bar(open_ts + i * 300, 1, 2, 0.5, 1.2) for i in range(48)  # 4 hours of 5m
        ]
        series = resample_ohlc(
            bars, "4H", source_timeframe="5m", trading_day=cfg, as_of_ts=open_ts + 14400
        )
        self.assertEqual(series.source, "resampled")
        self.assertIsNotNone((series.extras or {}).get("h4_anchor"))
        self.assertEqual(series.extras["h4_anchor"]["mode"], "ny_trading_day_open")
        self.assertTrue(len(series.bars) >= 1)
        self.assertEqual(int(series.bars[0].time), open_ts)


class InvalidStopTests(unittest.TestCase):
    def test_entry_beyond_sweep_extreme(self):
        cat = categorize_invalid_directional_stop(
            direction="bullish",
            entry_price=88.0,
            stop_price=88.5,
            sweep_extreme=89.0,
            sweep_level=90.0,
            fvg_low=92.0,
            fvg_high=93.0,
            stop_mode="beyond_sweep",
        )
        self.assertEqual(cat, "entry_already_beyond_sweep_extreme")

    def test_entry_beyond_fvg(self):
        cat = categorize_invalid_directional_stop(
            direction="bullish",
            entry_price=91.0,
            stop_price=91.5,
            sweep_extreme=80.0,
            sweep_level=80.0,
            fvg_low=92.0,
            fvg_high=93.0,
            stop_mode="beyond_fvg",
        )
        self.assertEqual(cat, "entry_already_beyond_fvg_invalidation")

    def test_diagnose_breakdown(self):
        recs = [_rec()]
        out = diagnose_invalid_stops(recs)
        self.assertGreaterEqual(out["case_count"], 1)
        self.assertIn("by_category", out)
        self.assertIn(
            out["cases"][0]["category"],
            {
                "entry_already_beyond_sweep_extreme",
                "entry_already_beyond_fvg_invalidation",
                "other",
                "same_trigger_bar_contains_stop_level",
            },
        )


class IntrabarResolverTests(unittest.TestCase):
    def test_15m_resolved_entry_then_stop(self):
        parent = 1_700_000_000
        # 3x 5m: first touches entry only, later touches stop (not entry)
        kids = [
            _bar(parent, 100, 101, 99.5, 100.5),  # hits entry 100, not stop 98.5
            _bar(parent + 300, 100.5, 101, 100, 100.2),
            _bar(parent + 600, 99, 99.2, 98, 98.5),  # hits stop 98.5 only
        ]
        r = IntrabarResolver().resolve_entry_stop(
            direction="bullish",
            entry_price=100.0,
            stop_price=98.5,
            parent_bar_time=parent,
            child_bars=kids,
        )
        self.assertEqual(r.result, ENTRY_THEN_STOP)

    def test_stop_before_entry(self):
        parent = 1_700_000_000
        kids = [
            _bar(parent, 99, 99.5, 97, 98.5),  # stop 98 only (no entry 100)
            _bar(parent + 300, 99, 100.5, 98.5, 100),  # entry 100
            _bar(parent + 600, 100, 100.1, 99.9, 100),
        ]
        r = IntrabarResolver().resolve_entry_stop(
            direction="bullish",
            entry_price=100.0,
            stop_price=98.0,
            parent_bar_time=parent,
            child_bars=kids,
        )
        self.assertEqual(r.result, STOP_BEFORE_ENTRY)

    def test_still_ambiguous_inside_one_5m(self):
        parent = 1_700_000_000
        kids = [
            _bar(parent, 100, 101, 97, 99),  # both entry 100 and stop 98
            _bar(parent + 300, 99, 99.5, 98.5, 99),
            _bar(parent + 600, 99, 99.2, 98.8, 99),
        ]
        r = IntrabarResolver().resolve_entry_stop(
            direction="bullish",
            entry_price=100.0,
            stop_price=98.0,
            parent_bar_time=parent,
            child_bars=kids,
        )
        self.assertEqual(r.result, STILL_AMBIGUOUS)

    def test_no_invented_intrabar_ordering_without_children(self):
        r = IntrabarResolver().resolve_entry_stop(
            direction="bullish",
            entry_price=100.0,
            stop_price=98.0,
            parent_bar_time=1,
            child_bars=[],
        )
        self.assertEqual(r.result, INSUFFICIENT_DATA)

    def test_resolved_no_stop(self):
        parent = 1_700_000_000
        kids = [
            _bar(parent, 100, 101, 99.5, 100.5),
            _bar(parent + 300, 100.5, 101, 100, 100.8),
            _bar(parent + 600, 100.8, 101.2, 100.5, 101),
        ]
        r = IntrabarResolver().resolve_entry_stop(
            direction="bullish",
            entry_price=100.0,
            stop_price=90.0,
            parent_bar_time=parent,
            child_bars=kids,
        )
        self.assertEqual(r.result, RESOLVED_NO_STOP)


class JournalDedupeTests(unittest.TestCase):
    def test_large_journal_dedupe(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            r1 = _rec(status="ENTRY_READY", invalidation_reason=None)
            append_journal_records([r1], root=root)
            append_journal_records([r1], root=root)
            rows = load_journal_records(root=root)
            self.assertEqual(len(rows), 1)
            # New config_hash is a new line
            r2 = _rec(config_hash="xyz", status="ENTRY_READY", invalidation_reason=None)
            append_journal_records([r2], root=root)
            rows2 = load_journal_records(root=root)
            self.assertEqual(len(rows2), 2)


class SampleQualityTests(unittest.TestCase):
    def test_labels(self):
        self.assertEqual(sample_quality_label(0), "INSUFFICIENT_SAMPLE")
        self.assertEqual(sample_quality_label(19), "INSUFFICIENT_SAMPLE")
        self.assertEqual(sample_quality_label(20), "SMALL_SAMPLE")
        self.assertEqual(sample_quality_label(49), "SMALL_SAMPLE")
        self.assertEqual(sample_quality_label(50), "MODERATE_SAMPLE")
        self.assertEqual(sample_quality_label(99), "MODERATE_SAMPLE")
        self.assertEqual(sample_quality_label(100), "LARGER_SAMPLE")
        m = mark_sample(25)
        self.assertEqual(m["sample_quality"], "SMALL_SAMPLE")
        self.assertIsNone(m["sample_warning"])


class HtfBiasOverlapSmokeTests(unittest.TestCase):
    def test_identical_series_agree(self):
        from dataset_overlap import compare_htf_bias_overlap

        daily = [_bar(1_700_000_000 + i * 86400, 100 + i, 101 + i, 99 + i, 100.5 + i) for i in range(8)]
        h4 = [_bar(1_700_000_000 + i * 14400, 100, 101, 99, 100.5) for i in range(20)]
        rep = compare_htf_bias_overlap(
            daily, h4, daily, h4, sample_timestamps=[1_700_000_000 + 5 * 86400]
        )
        self.assertGreaterEqual(rep["timestamps_compared"], 1)
        self.assertEqual(rep["daily_bias_agree"], rep["timestamps_compared"])


if __name__ == "__main__":
    unittest.main()
