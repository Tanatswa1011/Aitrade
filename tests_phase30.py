"""Phase 30 tests — freeze immutability, hist/paper equivalence, guardrails, journal."""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from models import Bar
from nq_databento import aggregate_1m_to_ny
from nq_drift_vwap_engine import config_hash, replay_dvp_day, session_anchors
from nq_drift_vwap_models import DVP_ORIGINAL, DVPStrategyConfig, STRATEGY_FAMILY
from nq_dvp_freeze import (
    FROZEN_JSON,
    assert_runtime_matches_frozen,
    assert_source_frozen_match,
    build_frozen_document,
    candidate_to_config,
    frozen_config_hash,
    load_frozen_document,
    load_frozen_strategy_config,
    load_phase29_candidate,
    semantic_payload,
    write_frozen_files,
)
from nq_dvp_paper import (
    NQDVPForwardTrade,
    append_paper_trade,
    existing_ids,
    paper_trade_id,
    recover_daily_state_from_journal,
    refuse_custom_strategy_params,
    run_frozen_dvp_on_bars,
    sample_label,
)

NY = ZoneInfo("America/New_York")
PHASE26_HASH = "0695d7a881b6247033426bceb077b6895b96405b826c21f32a109a02f44b2d43"
PHASE26_FROZEN = Path("strategy_frozen") / "gc_vwap_v2_phase26.json"
PHASE26_PAPER = Path("journal") / "phase26_gc_vwap_v2_paper" / "paper_trades.jsonl"


def _bar(t, o, h, l, c, v=100.0):
    return Bar(time=t, open=o, high=h, low=l, close=c, volume=v)


def _ts(y, m, d, hh, mm):
    return int(datetime(y, m, d, hh, mm, tzinfo=NY).timestamp())


def _rising_session_1m(y=2026, m=7, d=2):
    """Synthetic rising NQ session with enough bars for VWAP + drift + pullback."""
    bars = []
    # 09:30 → 16:00 at 1m
    start = _ts(y, m, d, 9, 30)
    for i in range(0, 390):
        t = start + i * 60
        px = 15000 + i * 1.5
        # inject a red 5m pullback after 11:00 area
        o = px
        c = px + 0.5
        if 150 <= i < 155:  # around 12:00 — small red stretch on 1m that makes red 5m
            o = px + 2
            c = px - 1
        bars.append(_bar(t, o, max(o, c) + 1, min(o, c) - 1, c, v=20))
    return bars


class FreezeImmutabilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        write_frozen_files()

    def test_phase29_source_loads(self):
        raw = load_phase29_candidate()
        cfg = candidate_to_config(raw)
        self.assertEqual(cfg.candidate_id, "DVP_ORIGINAL")
        self.assertEqual(cfg.hour_return_threshold, 0.001)
        self.assertEqual(cfg.long_stop_points, 80.0)
        self.assertEqual(cfg.long_target_points, 40.0)
        self.assertEqual(cfg.short_stop_points, 80.0)
        self.assertEqual(cfg.short_target_points, 50.0)
        self.assertEqual(cfg.max_trades_per_day, 4)
        self.assertEqual(cfg.max_losses_per_day, 2)

    def test_semantic_hash_match(self):
        raw = load_phase29_candidate()
        cfg = candidate_to_config(raw)
        match = assert_source_frozen_match(cfg, raw)
        self.assertTrue(match["ok"], match)
        self.assertEqual(match["source_config_hash"], match["live_config_hash"])
        self.assertEqual(match["live_config_hash"], "e314f828eee7eca5")

    def test_frozen_hash_deterministic(self):
        cfg = candidate_to_config(load_phase29_candidate())
        h1 = frozen_config_hash(semantic_payload(cfg))
        h2 = frozen_config_hash(semantic_payload(cfg))
        self.assertEqual(h1, h2)
        doc = load_frozen_document()
        self.assertEqual(doc["frozen_config_hash"], h1)
        self.assertEqual(doc["strategy_version"], "nq_drift_vwap_pullback_v1.DVP_ORIGINAL.FROZEN_PHASE30")

    def test_runtime_mutation_rejected(self):
        doc = load_frozen_document()
        bad = DVPStrategyConfig(
            candidate_id="DVP_ORIGINAL",
            hour_return_threshold=0.002,
            long_stop_points=80.0,
            long_target_points=40.0,
            short_stop_points=80.0,
            short_target_points=50.0,
            max_trades_per_day=4,
            max_losses_per_day=2,
        )
        check = assert_runtime_matches_frozen(bad, doc)
        self.assertFalse(check["ok"])
        self.assertEqual(check["error_code"], "FROZEN_CONFIG_MISMATCH")

    def test_refuse_custom_params(self):
        with self.assertRaises(ValueError):
            refuse_custom_strategy_params(hour_return_threshold=0.002)

    def test_missing_frozen_fails_closed(self):
        bars = _rising_session_1m()
        backup = FROZEN_JSON.read_text(encoding="utf-8")
        try:
            FROZEN_JSON.unlink()
            out = run_frozen_dvp_on_bars(bars, persist=False)
            self.assertFalse(out["ok"])
            self.assertEqual(out["error_code"], "MISSING_FROZEN_FILE")
        finally:
            FROZEN_JSON.write_text(backup, encoding="utf-8")

    def test_cfg_override_rejected_by_runner(self):
        bad = DVPStrategyConfig(
            candidate_id="DVP_ORIGINAL",
            hour_return_threshold=0.001,
            long_stop_points=100.0,
            long_target_points=40.0,
            short_stop_points=80.0,
            short_target_points=50.0,
            max_trades_per_day=4,
            max_losses_per_day=2,
        )
        out = run_frozen_dvp_on_bars(_rising_session_1m(), persist=False, cfg_override=bad)
        self.assertFalse(out["ok"])
        self.assertEqual(out["error_code"], "FROZEN_CONFIG_MISMATCH")


class TimingTests(unittest.TestCase):
    def test_session_anchors(self):
        a = session_anchors("2026-07-02")
        self.assertEqual(datetime.fromtimestamp(a["vwap_reset"], tz=NY).strftime("%H:%M"), "09:30")
        self.assertEqual(datetime.fromtimestamp(a["trade_start"], tz=NY).strftime("%H:%M"), "10:30")
        self.assertEqual(datetime.fromtimestamp(a["no_new"], tz=NY).strftime("%H:%M"), "15:30")
        self.assertEqual(datetime.fromtimestamp(a["force_close"], tz=NY).strftime("%H:%M"), "15:55")
        b = session_anchors("2026-01-08")
        self.assertNotEqual(
            datetime.fromtimestamp(a["vwap_reset"], tz=ZoneInfo("UTC")).hour,
            datetime.fromtimestamp(b["vwap_reset"], tz=ZoneInfo("UTC")).hour,
        )


class EquivalenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        write_frozen_files()
        cls.bars_1m = _rising_session_1m()
        cls.bars_5m = aggregate_1m_to_ny(cls.bars_1m, 5)
        cls.bars_15m = aggregate_1m_to_ny(cls.bars_1m, 15)

    def test_hist_paper_engine_equivalence(self):
        day = replay_dvp_day(
            trading_date="2026-07-02",
            bars_1m=self.bars_1m,
            bars_5m=self.bars_5m,
            bars_15m=self.bars_15m,
            cfg=DVP_ORIGINAL,
        )
        out = run_frozen_dvp_on_bars(
            self.bars_1m,
            self.bars_5m,
            self.bars_15m,
            persist=False,
        )
        self.assertTrue(out["ok"], out)
        hist = day["trades"]
        paper = out["historical_trades"]
        self.assertEqual(len(hist), len(paper))
        for a, b in zip(hist, paper):
            self.assertEqual(a.entry_timestamp, b.entry_timestamp)
            self.assertEqual(a.entry_price, b.entry_price)
            self.assertEqual(a.stop_price, b.stop_price)
            self.assertEqual(a.target_price, b.target_price)
            self.assertEqual(a.direction, b.direction)
            self.assertEqual(a.outcome, b.outcome)

    def test_risk_points_frozen(self):
        cfg = load_frozen_strategy_config()
        self.assertEqual(cfg.long_stop_points, 80)
        self.assertEqual(cfg.long_target_points, 40)
        self.assertEqual(cfg.short_stop_points, 80)
        self.assertEqual(cfg.short_target_points, 50)


class GuardrailTests(unittest.TestCase):
    def test_max_four_and_two_loss_constants(self):
        cfg = candidate_to_config(load_phase29_candidate())
        self.assertEqual(cfg.max_trades_per_day, 4)
        self.assertEqual(cfg.max_losses_per_day, 2)

    def test_daily_state_recovery(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "paper_trades.jsonl"
            path.write_text("", encoding="utf-8")
            now = datetime.now().isoformat()
            for i, pts in enumerate([40.0, -80.0, -80.0], start=1):
                t = NQDVPForwardTrade(
                    paper_trade_id=f"NQ|DVP|2026-07-02|bullish|{1000 + i}",
                    frozen_config_hash="x",
                    trading_date="2026-07-02",
                    contract="NQ",
                    direction="bullish",
                    drift_timestamp=None,
                    trigger_timestamp=1000 + i,
                    entry_timestamp=1000 + i,
                    entry_price=15000,
                    stop_price=14920,
                    target_price=15040,
                    exit_timestamp=1000 + i + 60,
                    exit_price=15000 + pts,
                    outcome="TARGET_HIT" if pts > 0 else "STOP_HIT",
                    gross_points=pts,
                    net_points=pts,
                    mfe_points=None,
                    mae_points=None,
                    fill_slippage_ticks=1.0,
                    cost_ticks=1.0,
                    daily_trade_number=i,
                    daily_loss_count_before=i - 1 if pts < 0 else 0,
                    status="TARGET_HIT" if pts > 0 else "STOP_HIT",
                    created_at=now,
                    updated_at=now,
                )
                append_paper_trade(t, path=path)
            state = recover_daily_state_from_journal("2026-07-02", path=path)
            self.assertEqual(state["daily_trade_count"], 3)
            self.assertEqual(state["daily_loss_count"], 2)
            self.assertTrue(state["hit_loss_cap"])


class JournalTests(unittest.TestCase):
    def test_append_dedupe(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "paper_trades.jsonl"
            path.write_text("", encoding="utf-8")
            now = datetime.now().isoformat()
            trade = NQDVPForwardTrade(
                paper_trade_id=paper_trade_id("2026-07-02", "bullish", 12345),
                frozen_config_hash="x",
                trading_date="2026-07-02",
                contract="NQ",
                direction="bullish",
                drift_timestamp=None,
                trigger_timestamp=12345,
                entry_timestamp=12345,
                entry_price=15000,
                stop_price=14920,
                target_price=15040,
                exit_timestamp=None,
                exit_price=None,
                outcome=None,
                gross_points=None,
                net_points=None,
                mfe_points=None,
                mae_points=None,
                fill_slippage_ticks=1.0,
                cost_ticks=1.0,
                daily_trade_number=1,
                daily_loss_count_before=0,
                status="ENTRY_TRIGGERED",
                created_at=now,
                updated_at=now,
            )
            self.assertTrue(append_paper_trade(trade, path=path))
            self.assertFalse(append_paper_trade(trade, path=path))
            self.assertEqual(len(existing_ids(path)), 1)

    def test_sample_labels(self):
        self.assertEqual(sample_label(0), "VERY_EARLY")
        self.assertEqual(sample_label(25), "EARLY")
        self.assertEqual(sample_label(35), "MINIMUM_FORWARD_SAMPLE")
        self.assertEqual(sample_label(75), "MEANINGFUL_FORWARD_SAMPLE")
        self.assertEqual(sample_label(150), "STRONG_FORWARD_SAMPLE")
        self.assertEqual(sample_label(250), "LARGE_FORWARD_SAMPLE")


class IsolationTests(unittest.TestCase):
    def test_phase26_hash_unchanged(self):
        doc = json.loads(PHASE26_FROZEN.read_text(encoding="utf-8"))
        self.assertEqual(doc["frozen_config_hash"], PHASE26_HASH)

    def test_phase26_journal_untouched_path(self):
        self.assertTrue(PHASE26_PAPER.exists())
        # Phase 30 journal is separate
        self.assertNotEqual(
            str(PHASE26_PAPER).replace("\\", "/"),
            "journal/phase30_nq_dvp_paper/paper_trades.jsonl",
        )

    def test_family(self):
        self.assertEqual(STRATEGY_FAMILY, "nq_drift_vwap_pullback_v1")
        self.assertEqual(config_hash(DVP_ORIGINAL), "e314f828eee7eca5")


if __name__ == "__main__":
    unittest.main()
