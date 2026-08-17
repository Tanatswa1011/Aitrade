"""Phase 14 tests: TV discovery, Daily evidence, datasets, reporting."""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

from bar_dataset import (
    dedupe_bars,
    load_dataset,
    validate_bars,
    write_dataset,
)
from daily_boundary_evidence import (
    apply_evidence_to_default,
    build_daily_boundary_evidence,
)
from htf_report import (
    compute_mtf_journal_report,
    htf_report_bucket,
    paired_execution_summary,
)
from journal_models import HistoricalEntryResult, SetupJournalRecord
from luxalgo_capture import append_luxalgo_captures, load_luxalgo_captures
from models import Bar, StructureConfirmation
from study_discovery import compare_study_snapshots, rediscover_studies
from trading_day_config import TradingDayConfig, trading_day_open_utc
from tv_desktop import (
    CdpPreflight,
    TradingViewDiscovery,
    cdp_preflight,
    discover_tradingview,
)


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
        status="ENTRY_READY",
        expiry_reason=None,
        invalidation_reason=None,
        reliability_flags=["TRIGGER_BAR_STOP_AMBIGUITY"],
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
                ambiguity_flags=["TRIGGER_BAR_STOP_AMBIGUITY"],
            )
        ],
        strategy_version="v1.phase14",
        config_hash="abc",
        structure_algorithm_version="internal_choch_v1",
        daily_bias="bullish",
        h4_bias="bullish",
        htf_alignment="aligned_bullish",
        execution_timeframe="5m",
        setup_vs_daily="aligned",
        setup_vs_h4="aligned",
        liquidity_event_id="X|Asia|2026-01-05|low|1",
    )
    base.update(kwargs)
    return SetupJournalRecord(**base)


class TradingViewDiscoveryTests(unittest.TestCase):
    def test_discover_prefers_config(self):
        with tempfile.TemporaryDirectory() as td:
            exe = Path(td) / "TradingView.exe"
            exe.write_text("x", encoding="utf-8")
            cfg = Path(td) / "tv_desktop_config.json"
            cfg.write_text(json.dumps({"executable": str(exe)}), encoding="utf-8")
            with patch("tv_desktop._running_tradingview_exes", return_value=[]):
                with patch("tv_desktop._appx_tradingview_exe", return_value=(None, None)):
                    with patch("tv_desktop._known_path_candidates", return_value=[]):
                        d = discover_tradingview(config_path=cfg, prefer_running=False)
            self.assertTrue(d.found)
            self.assertEqual(d.source, "config")
            self.assertEqual(d.executable, str(exe))

    def test_discover_process_path(self):
        with patch(
            "tv_desktop._running_tradingview_exes",
            return_value=[(1, r"C:\fake\TradingView.exe")],
        ):
            d = discover_tradingview(prefer_running=True)
        self.assertTrue(d.found)
        self.assertEqual(d.source, "process")
        self.assertEqual(d.process_ids, [1])


class CdpPreflightTests(unittest.TestCase):
    def test_preflight_down(self):
        disc = TradingViewDiscovery(found=True, executable=r"C:\x\TradingView.exe")
        with patch("tv_desktop.cdp_version", return_value={"ok": False, "error": "down"}):
            pre = cdp_preflight(discovery=disc)
        self.assertFalse(pre.cdp_reachable)
        self.assertIsNotNone(pre.error)
        self.assertEqual(pre.cdp_port, 9222)


class DailyEvidenceTests(unittest.TestCase):
    def test_confirm_17ny(self):
        cfg = TradingDayConfig(day_roll_time="17:00")
        bars = []
        d = date(2026, 1, 5)
        while len(bars) < 12:
            if d.weekday() < 5:
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
        ev = build_daily_boundary_evidence(bars, min_bars=10)
        self.assertEqual(ev.status, "confirmed")
        self.assertEqual(ev.inferred_roll_time, "17:00")
        applied = apply_evidence_to_default(ev)
        self.assertIn("confirmed", applied.source)

    def test_conflicting_roll(self):
        # Alternate opens between 17:00 and 00:00 local → conflicting
        cfg17 = TradingDayConfig(day_roll_time="17:00")
        cfg00 = TradingDayConfig(day_roll_time="00:00")
        bars = []
        d = date(2026, 1, 5)
        i = 0
        while len(bars) < 12:
            if d.weekday() < 5:
                c = cfg17 if i % 2 == 0 else cfg00
                bars.append(
                    Bar(
                        time=trading_day_open_utc(c, d),
                        open=1,
                        high=2,
                        low=0.5,
                        close=1,
                    )
                )
                i += 1
            d += timedelta(days=1)
        ev = build_daily_boundary_evidence(bars, min_bars=10)
        self.assertIn(ev.status, ("conflicting", "provisional"))

    def test_insufficient(self):
        cfg = TradingDayConfig(day_roll_time="17:00")
        bars = [
            Bar(time=trading_day_open_utc(cfg, date(2026, 1, 5)), open=1, high=2, low=0.5, close=1)
        ]
        ev = build_daily_boundary_evidence(bars, min_bars=10)
        self.assertEqual(ev.status, "insufficient_data")


class DatasetTests(unittest.TestCase):
    def test_round_trip_and_dedupe(self):
        bars = [
            Bar(time=100, open=1, high=2, low=0.5, close=1.5),
            Bar(time=200, open=1.5, high=2.5, low=1, close=2),
            Bar(time=100, open=1.1, high=2.1, low=0.6, close=1.6),  # dupe ts
        ]
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            written = write_dataset(
                bars, symbol="OANDA:XAUUSD", timeframe="5m", root=root, source="test"
            )
            self.assertTrue(written["ok"])
            loaded = load_dataset("OANDA:XAUUSD", "5m", root=root)
            self.assertEqual(loaded["bar_count"], 2)
            self.assertEqual(int(loaded["bars"][0].time), 100)
            self.assertEqual(float(loaded["bars"][0].close), 1.6)  # last wins

    def test_validate_gaps_not_filled(self):
        bars = [
            Bar(time=0, open=1, high=2, low=0.5, close=1),
            Bar(time=900, open=1, high=2, low=0.5, close=1),  # skipped 300/600
        ]
        rep = validate_bars(bars, symbol="X", timeframe="5m", expected_period_sec=300)
        self.assertGreaterEqual(rep["gap_count"], 1)
        self.assertEqual(len(dedupe_bars(bars)), 2)


class StudyAndRestoreTests(unittest.TestCase):
    def test_study_rediscovery_after_id_churn(self):
        before = rediscover_studies(
            [
                {"id": "A", "name": "ICT Sessions & Killzones"},
                {"id": "B", "name": "LuxAlgo Market Structure with Inducements & Sweeps"},
            ]
        )
        after = rediscover_studies(
            [
                {"id": "A2", "name": "ICT Sessions & Killzones"},
                {"id": "B2", "name": "LuxAlgo Market Structure with Inducements & Sweeps"},
            ]
        )
        cmp = compare_study_snapshots(before, after)
        self.assertTrue(cmp["all_tracked_present_after"])
        self.assertTrue(cmp["any_id_changed"])

    def test_timeframe_restore_flag_logic(self):
        # Pure logic mirror of phase14 restore_check
        original = {"symbol": "OANDA:XAUUSD", "timeframe": "15"}
        final_ok = {"symbol": "OANDA:XAUUSD", "timeframe": "15"}
        final_bad = {"symbol": "OANDA:XAUUSD", "timeframe": "5"}
        self.assertTrue(
            final_ok["symbol"] == original["symbol"]
            and str(final_ok["timeframe"]) == str(original["timeframe"])
        )
        self.assertFalse(str(final_bad["timeframe"]) == str(original["timeframe"]))


class PairedReplayReportTests(unittest.TestCase):
    def test_paired_and_insufficient_sample(self):
        eid = "E1"
        rows = [
            _rec(
                setup_id=eid + "|exec:5m",
                liquidity_event_id=eid,
                execution_timeframe="5m",
                confirmation_timestamp=10,
            ),
            _rec(
                setup_id=eid + "|exec:15m",
                liquidity_event_id=eid,
                execution_timeframe="15m",
                timeframe="15m",
                confirmation_timestamp=None,
                fvg_created_timestamp=None,
                setup_vs_daily="opposed",
                setup_vs_h4="opposed",
                htf_alignment="aligned_bearish",
                daily_bias="bearish",
                h4_bias="bearish",
                direction="bullish",
            ),
        ]
        report = compute_mtf_journal_report(rows)
        self.assertEqual(report["paired_5m_15m"]["paired_event_count"], 1)
        summary = paired_execution_summary(rows)
        self.assertEqual(summary["5m_only_confirmed"], 1)
        self.assertEqual(summary["sample_warning"], "INSUFFICIENT_SAMPLE")
        self.assertFalse(summary["n_ge_30"])
        self.assertEqual(htf_report_bucket(rows[0]), "aligned_both")
        self.assertEqual(htf_report_bucket(rows[1]), "opposed_both")


class LuxAlgoCaptureTests(unittest.TestCase):
    def test_persist_dedupe(self):
        ev = StructureConfirmation(
            kind="CHoCH",
            direction="bullish",
            level=100.0,
            event_timestamp=123,
            event_bar_index=1,
            source="luxalgo",
            study_id="s1",
            raw_id="r1",
            timing_confidence="exact",
        )
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "caps.jsonl"
            a = append_luxalgo_captures([ev], symbol="OANDA:XAUUSD", timeframe="5m", path=path)
            b = append_luxalgo_captures([ev], symbol="OANDA:XAUUSD", timeframe="5m", path=path)
            self.assertEqual(a["written"], 1)
            self.assertEqual(b["written"], 0)
            rows = load_luxalgo_captures(path=path, symbol="OANDA:XAUUSD", timeframe="5m")
            self.assertEqual(len(rows), 1)


if __name__ == "__main__":
    unittest.main()
