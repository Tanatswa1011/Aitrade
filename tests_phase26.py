"""Phase 26 tests — freeze immutability, hist/live equivalence, paper journal, fills."""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from gc_vwap_engine import (
    analyze_candidate,
    collect_extension_sequences,
    compute_session_vwap_series,
    config_hash,
)
from gc_vwap_freeze import (
    FROZEN_JSON,
    assert_runtime_matches_frozen,
    assert_source_frozen_match,
    build_frozen_document,
    candidate_to_config,
    frozen_config_hash,
    load_frozen_document,
    load_frozen_strategy_config,
    load_phase25_v2_candidate,
    semantic_payload,
    write_frozen_files,
)
from gc_vwap_models import (
    OR_TIMEZONE,
    ConfirmationMode,
    EntryMode,
    GCVWAPStrategyConfig,
)
from gc_vwap_paper import (
    GCVWAPPaperTrade,
    append_paper_trade,
    fill_price,
    fill_sensitivity_overlay,
    paper_trade_id,
    run_frozen_v2_on_bars,
)
from models import Bar
from phase18_metrics import theoretical_fixed_target_expectancy

NY = ZoneInfo(OR_TIMEZONE)


def _bar(t, o, h, l, c, v=100.0):
    return Bar(time=t, open=o, high=h, low=l, close=c, volume=v)


def _ts(y, m, d, hh, mm):
    return int(datetime(y, m, d, hh, mm, tzinfo=NY).timestamp())


def _stretched_session():
    bars = []
    for i in range(8):
        t = _ts(2026, 7, 2, 8, 20) + i * 300
        bars.append(_bar(t, 2000, 2001, 1999, 2000, v=100))
    for i in range(4):
        t = _ts(2026, 7, 2, 9, 0) + i * 300
        px = 2010 + i * 5
        bars.append(_bar(t, px - 1, px + 2, px - 2, px, v=50))
    for i in range(6):
        t = _ts(2026, 7, 2, 9, 20) + i * 300
        px = 2015 - i * 3
        bars.append(_bar(t, px + 1, px + 2, px - 2, px, v=80))
    return bars


class FreezeImmutabilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        write_frozen_files()

    def test_canonical_phase25_v2_loads(self):
        raw = load_phase25_v2_candidate()
        cfg = candidate_to_config(raw)
        self.assertEqual(cfg.candidate_id, "V2_BAND_RECLAIM_2SIG_RETEST")
        self.assertEqual(cfg.confirmation_mode, ConfirmationMode.BAND_RECLAIM.value)
        self.assertEqual(cfg.entry_mode, EntryMode.FROZEN_2SIG_RETEST.value)
        self.assertEqual(float(cfg.sigma_threshold), 2.0)
        self.assertEqual(int(cfg.max_entry_bars), 6)

    def test_freeze_hash_deterministic(self):
        cfg = candidate_to_config(load_phase25_v2_candidate())
        h1 = frozen_config_hash(semantic_payload(cfg))
        h2 = frozen_config_hash(semantic_payload(cfg))
        self.assertEqual(h1, h2)
        doc = load_frozen_document()
        self.assertEqual(doc["frozen_config_hash"], h1)

    def test_source_engine_hash_match(self):
        raw = load_phase25_v2_candidate()
        cfg = candidate_to_config(raw)
        match = assert_source_frozen_match(cfg, raw)
        self.assertTrue(match["ok"])
        self.assertEqual(match["source_config_hash"], match["live_config_hash"])
        self.assertEqual(match["live_config_hash"], "da630a519397ec84")

    def test_runtime_mutation_rejected(self):
        doc = load_frozen_document()
        bad = GCVWAPStrategyConfig(
            candidate_id="V2_BAND_RECLAIM_2SIG_RETEST",
            confirmation_mode="BAND_RECLAIM",
            entry_mode="FROZEN_2SIG_RETEST",
            sigma_threshold=2.5,
            max_entry_bars=6,
            min_vwap_bars=6,
            volume_filter=False,
        )
        check = assert_runtime_matches_frozen(bad, doc)
        self.assertFalse(check["ok"])
        self.assertEqual(check["error_code"], "FROZEN_CONFIG_MISMATCH")

    def test_missing_frozen_file_fails(self):
        bars = _stretched_session()
        # Temporarily rename
        backup = FROZEN_JSON.read_text(encoding="utf-8")
        try:
            FROZEN_JSON.unlink()
            out = run_frozen_v2_on_bars(bars, persist=False)
            self.assertFalse(out["ok"])
            self.assertEqual(out["error_code"], "MISSING_FROZEN_FILE")
        finally:
            FROZEN_JSON.write_text(backup, encoding="utf-8")

    def test_mutated_timeout_rejected_by_runner(self):
        doc = load_frozen_document()
        bad = load_frozen_strategy_config(doc)
        # frozen dataclass — rebuild
        bad = GCVWAPStrategyConfig(
            candidate_id=bad.candidate_id,
            confirmation_mode=bad.confirmation_mode,
            entry_mode=bad.entry_mode,
            sigma_threshold=bad.sigma_threshold,
            max_entry_bars=12,
            min_vwap_bars=bad.min_vwap_bars,
            volume_filter=bad.volume_filter,
        )
        out = run_frozen_v2_on_bars(_stretched_session(), persist=False, cfg_override=bad)
        self.assertFalse(out["ok"])
        self.assertEqual(out["error_code"], "FROZEN_CONFIG_MISMATCH")


class EquivalenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        write_frozen_files()

    def test_historical_vs_paper_engine_identical_levels(self):
        bars = _stretched_session()
        doc = load_frozen_document()
        cfg = load_frozen_strategy_config(doc)
        seqs = collect_extension_sequences(bars, "2026-07-02")
        if not seqs:
            self.skipTest("no extension in synthetic path")
        hist = analyze_candidate(seqs[0], cfg)
        paper = run_frozen_v2_on_bars(bars, persist=False)
        self.assertTrue(paper["ok"])
        # Match VWAP/sigma series path
        states = compute_session_vwap_series(bars, "2026-07-02")
        self.assertTrue(states[-1].valid)
        # Paper trades for same day use same engine analyze_candidate
        trades = [t for t in paper["trades"] if t.trading_date == "2026-07-02"]
        self.assertGreaterEqual(len(trades), 1)
        t0 = trades[0]
        self.assertEqual(t0.extension_event_id, hist.vwap_extension_event_id)
        self.assertEqual(t0.entry_band_price, seqs[0].get("frozen_2sig"))
        if hist.entry_triggered:
            self.assertEqual(t0.entry_price, hist.entry_price)
            self.assertEqual(t0.stop_price, hist.stop_price)
            self.assertEqual(t0.theoretical_entry_price, hist.entry_price)

    def test_frozen_band_does_not_move_after_confirmation(self):
        bars = _stretched_session()
        cfg = load_frozen_strategy_config()
        seqs = collect_extension_sequences(bars, "2026-07-02")
        if not seqs:
            self.skipTest("no extension")
        seq = seqs[0]
        band0 = seq.get("frozen_2sig")
        self.assertIsNotNone(band0)
        setup = analyze_candidate(seq, cfg)
        # Band stored on setup extras equals first-extension freeze
        if setup.extras.get("frozen_2sig") is not None:
            self.assertEqual(setup.extras["frozen_2sig"], band0)
        if setup.entry_triggered:
            self.assertEqual(setup.entry_price, band0)

    def test_no_lookahead_vwap_state(self):
        bars = _stretched_session()
        states = compute_session_vwap_series(bars, "2026-07-02")
        for i, st in enumerate(states):
            self.assertEqual(st.bars_used, i + 1)
            self.assertLessEqual(st.timestamp, states[-1].timestamp)


class PaperJournalTests(unittest.TestCase):
    def test_dedupe_append_resume(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "paper_trades.jsonl"
            path.write_text("", encoding="utf-8")
            trade = GCVWAPPaperTrade(
                paper_trade_id=paper_trade_id("2026-07-02", "bearish", 123),
                frozen_config_hash="abc",
                trading_date="2026-07-02",
                contract="GCZ2026",
                direction="bearish",
                extension_event_id="e1",
                first_extension_timestamp=123,
                reclaim_timestamp=456,
                entry_band_price=2000.0,
                entry_trigger_timestamp=789,
                entry_price=2000.0,
                theoretical_entry_price=2000.0,
                paper_fill_price=1999.9,
                fill_delta_points=0.1,
                fill_delta_ticks=1.0,
                stop_price=2010.0,
                risk_points=10.0,
                target_1r=1990.0,
                target_1_5r=1985.0,
                target_2r=1980.0,
                target_3r=1970.0,
                vwap_at_entry=1995.0,
                sigma_at_entry=2.0,
                z_at_extension=2.5,
                status="ENTRY_TRIGGERED",
                created_at="t0",
                updated_at="t0",
            )
            self.assertTrue(append_paper_trade(trade, path=path))
            self.assertFalse(append_paper_trade(trade, path=path))
            lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
            self.assertEqual(len(lines), 1)


class FillSensitivityTests(unittest.TestCase):
    def test_overlays_do_not_change_theoretical(self):
        theoretical = 2000.0
        rows = fill_sensitivity_overlay(theoretical, "bearish", risk=5.0)
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0]["paper_fill_price"], theoretical)
        self.assertLess(rows[1]["paper_fill_price"], theoretical)
        self.assertLess(rows[2]["paper_fill_price"], rows[1]["paper_fill_price"])
        # long adverse is higher
        long_rows = fill_sensitivity_overlay(theoretical, "bullish", risk=5.0)
        self.assertGreater(long_rows[1]["paper_fill_price"], theoretical)

    def test_fill_price_tick(self):
        self.assertAlmostEqual(fill_price(100.0, "bullish", ticks_adverse=1), 100.1)
        self.assertAlmostEqual(fill_price(100.0, "bearish", ticks_adverse=2), 99.8)


class MetricEquivalenceTests(unittest.TestCase):
    def test_phase25_expectancy_formula(self):
        # Match Phase 25 HOLDOUT V2-ish: stop 52/87, r2 41/87
        e2 = theoretical_fixed_target_expectancy(
            target_r=2.0, target_hits=41, stop_hits=52, resolved_n=87
        )
        self.assertAlmostEqual(e2, 41 / 87 * 2 - 52 / 87 * 1, places=10)


class FrozenDocTests(unittest.TestCase):
    def test_build_document_fields(self):
        doc = build_frozen_document(freeze_timestamp="2026-08-16T00:00:00+00:00")
        self.assertEqual(doc["candidate_id"], "V2_BAND_RECLAIM_2SIG_RETEST")
        self.assertEqual(doc["entry"]["mode"], "FROZEN_2SIG_RETEST")
        self.assertEqual(doc["session"]["start"], "08:20")
        self.assertFalse(doc["paper_campaign"]["broker_execution"])
        self.assertIn("gc_vwap_mean_reversion_v1.V2.FROZEN_PHASE26", doc["strategy_version"])


if __name__ == "__main__":
    unittest.main()
