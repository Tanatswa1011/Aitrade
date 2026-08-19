"""Phase 47 frozen-isolation, lock hash, and paper-journal tests."""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from es_dvp_lock import (
    LOCKED_CFG,
    LOCKED_VERSION,
    assert_matches_phase46,
    load_phase46_candidate,
    locked_config_hash,
    semantic_payload,
)
from es_dvp_paper import (
    ESDVPForwardTrade,
    append_paper_trade,
    counts_toward_forward,
    existing_ids,
    paper_trade_id,
    refuse_custom_strategy_params,
    setup_key,
)
from phase34_validate import GC_FILE_SHA, NQ_FILE_SHA, file_sha256

ROOT = Path(__file__).resolve().parent


class FrozenIsolationTests(unittest.TestCase):
    def test_frozen_hashes_unchanged(self):
        gc = json.loads((ROOT / "strategy_frozen" / "gc_vwap_v2_phase26.json").read_text(encoding="utf-8"))
        nq = json.loads((ROOT / "strategy_frozen" / "nq_dvp_phase30.json").read_text(encoding="utf-8"))
        self.assertEqual(gc["frozen_config_hash"], "0695d7a881b6247033426bceb077b6895b96405b826c21f32a109a02f44b2d43")
        self.assertEqual(nq["frozen_config_hash"], "935e3a616351b09dbfa8d2d0e2b5d6850be803fb385ec5e5f8e3593856a1212a")
        self.assertEqual(file_sha256(ROOT / "strategy_frozen" / "gc_vwap_v2_phase26.json"), GC_FILE_SHA)
        self.assertEqual(file_sha256(ROOT / "strategy_frozen" / "nq_dvp_phase30.json"), NQ_FILE_SHA)

    def test_phase46_candidate_is_authority(self):
        src = load_phase46_candidate()
        assert_matches_phase46(src)
        self.assertEqual(src["instrument"], "ES")
        self.assertEqual(src["family"], "DVP")
        self.assertEqual(src["metrics"]["status"], "PORTABLE_EDGE_FOUND")
        cfg = src["metrics"]["cfg"]
        self.assertEqual(cfg["long_stop_points"], 18.0)
        self.assertEqual(cfg["long_target_points"], 9.0)
        self.assertEqual(cfg["short_stop_points"], 18.0)
        self.assertEqual(cfg["short_target_points"], 11.25)
        self.assertNotEqual(cfg["long_stop_points"], 80.0)

    def test_locked_cfg_matches_phase46(self):
        self.assertEqual(LOCKED_CFG.long_stop_points, 18.0)
        self.assertEqual(LOCKED_CFG.long_target_points, 9.0)
        self.assertEqual(LOCKED_CFG.short_stop_points, 18.0)
        self.assertEqual(LOCKED_CFG.short_target_points, 11.25)
        self.assertEqual(LOCKED_CFG.hour_return_threshold, 0.001)
        self.assertEqual(LOCKED_CFG.max_trades_per_day, 4)
        self.assertEqual(LOCKED_CFG.max_losses_per_day, 2)
        self.assertEqual(LOCKED_VERSION, "es_dvp_v1.PORT.LOCKED_PHASE47")


class LockHashTests(unittest.TestCase):
    def test_hash_is_deterministic(self):
        a = locked_config_hash()
        b = locked_config_hash(semantic_payload())
        self.assertEqual(a, b)
        self.assertEqual(len(a), 64)

    def test_hash_changes_if_stop_changes(self):
        sem = semantic_payload()
        base = locked_config_hash(sem)
        sem["long_stop_points"] = 80.0
        self.assertNotEqual(base, locked_config_hash(sem))


class JournalTests(unittest.TestCase):
    def test_gc_nq_journals_exist(self):
        gc = ROOT / "journal" / "phase26_gc_vwap_v2_paper" / "paper_trades.jsonl"
        nq = ROOT / "journal" / "phase30_nq_dvp_paper" / "paper_trades.jsonl"
        self.assertTrue(gc.exists())
        self.assertTrue(nq.exists())

    def test_es_journal_empty_no_backfill(self):
        path = ROOT / "journal" / "phase47_es_dvp_paper" / "paper_trades.jsonl"
        self.assertTrue(path.exists())
        self.assertEqual(path.read_text(encoding="utf-8").strip(), "")
        self.assertEqual(len(existing_ids(path)), 0)

    def test_duplicate_append_is_noop(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "paper_trades.jsonl"
            path.write_text("", encoding="utf-8")
            trade = ESDVPForwardTrade(
                paper_trade_id=paper_trade_id("2026-08-19", "LONG", 1),
                strategy_family="nq_drift_vwap_pullback_v1",
                strategy_version=LOCKED_VERSION,
                config_hash=locked_config_hash(),
                instrument="ES",
                contract="ES",
                session_date="2026-08-19",
                timezone="America/New_York",
                direction="LONG",
                setup_timestamp=1,
                signal_timestamp=1,
                entry_timestamp=301,
                entry_price=5000.0,
                theoretical_entry_price=5000.0,
                stop_price=4982.0,
                target_price=5009.0,
                exit_timestamp=None,
                exit_price=None,
                exit_reason=None,
                raw_pnl_points=None,
                net_pnl_points=None,
                pnl_dollars_es=None,
                pnl_dollars_mes=None,
                r_result=None,
                slippage_assumption="1_TICK_ADVERSE",
                news_blackout=False,
                daily_trade_number=1,
                daily_prior_losses=0,
                mfe_points=None,
                mae_points=None,
                state="OPEN_POSITION",
            )
            self.assertTrue(append_paper_trade(trade, path=path))
            self.assertFalse(append_paper_trade(trade, path=path))
            lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
            self.assertEqual(len(lines), 1)

    def test_setup_before_lock_does_not_count(self):
        self.assertFalse(counts_toward_forward(100, "2026-08-19T00:00:00+00:00"))
        self.assertTrue(counts_toward_forward(2_000_000_000, "2020-01-01T00:00:00+00:00"))

    def test_setup_key_is_deterministic(self):
        self.assertEqual(
            setup_key("2026-08-19", 123, "LONG"),
            "nq_drift_vwap_pullback_v1|ES|2026-08-19|123|LONG",
        )

    def test_refuse_locked_params(self):
        with self.assertRaises(ValueError):
            refuse_custom_strategy_params(long_stop_points=80)


class SafetyTests(unittest.TestCase):
    def test_runner_rejects_sim_execution(self):
        proc = subprocess.run(
            [sys.executable, str(ROOT / "es_dvp_paper_runner.py"), "--enable-sim-execution"],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("BROKER_EXECUTION_FORBIDDEN", proc.stderr + proc.stdout)

    def test_locked_file_not_in_strategy_frozen(self):
        frozen_dir = ROOT / "strategy_frozen"
        self.assertFalse((frozen_dir / "es_dvp_phase47.json").exists())
        names = {p.name for p in frozen_dir.glob("*")}
        self.assertNotIn("es_dvp_phase47.json", names)


if __name__ == "__main__":
    unittest.main()
