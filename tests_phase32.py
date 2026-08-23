"""Phase 32 tests — pause gate, prop sim, sizing, account risk, restart/failure fixtures."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ["AITRADE_PHASE54_TEST"] = "1"
os.environ.setdefault("AITRADE_TEST_ROOT", tempfile.mkdtemp(prefix="phase32_authoritative_root_"))

from account_risk import AccountRiskLimits, RiskCheckContext, run_account_risk_checks
from execution_status import (
    GC_FROZEN_HASH,
    NQ_FROZEN_HASH,
    ensure_project_paused,
    is_execution_paused,
    load_pause,
    PAUSE_PATH,
)
from nq_dvp_live_runner import load_state, run_once, save_state, set_halt, STATE_PATH, JOURNAL_DIR
from position_sizing import mnq_dvp_frozen_risk, size_from_metadata, size_position, SizingInput
from prop_firm_simulator import PropConfig, PropTrade, simulate_prop_account, trades_from_r_multiples


class TestPhase32FreezeIntegrity(unittest.TestCase):
    def test_gc_hash(self):
        doc = json.loads(Path("strategy_frozen/gc_vwap_v2_phase26.json").read_text(encoding="utf-8"))
        self.assertEqual(doc["frozen_config_hash"], GC_FROZEN_HASH)

    def test_nq_hash(self):
        doc = json.loads(Path("strategy_frozen/nq_dvp_phase30.json").read_text(encoding="utf-8"))
        self.assertEqual(doc["frozen_config_hash"], NQ_FROZEN_HASH)

    def test_pause_marker(self):
        self.assertTrue(PAUSE_PATH.exists())
        doc = load_pause()
        self.assertTrue(doc.get("paused"))
        self.assertEqual(doc.get("execution_status"), "PAUSED")


class TestPhase32PauseGate(unittest.TestCase):
    def test_sim_blocked_when_paused(self):
        out = run_once(enable_sim=True)
        self.assertFalse(out.get("ok", True))
        self.assertEqual(out.get("error_code"), "PROJECT_PAUSED")

    def test_dry_run_allowed_when_paused(self):
        out = run_once(enable_sim=False)
        self.assertTrue(out.get("ok"))
        self.assertEqual(out.get("mode"), "DRY_RUN")


class TestPropSimulator(unittest.TestCase):
    def test_pass_on_profit_target(self):
        trades = [PropTrade("2026-01-02", 500), PropTrade("2026-01-03", 600), PropTrade("2026-01-06", 400)]
        cfg = PropConfig(profit_target=1000, max_drawdown=2000)
        res = simulate_prop_account(trades, cfg)
        self.assertEqual(res.outcome, "PASS")

    def test_fail_daily_loss(self):
        trades = [PropTrade("2026-01-02", -600)]
        cfg = PropConfig(profit_target=3000, max_drawdown=2000, daily_loss_limit=500)
        res = simulate_prop_account(trades, cfg)
        self.assertEqual(res.outcome, "FAIL")
        self.assertTrue(any("daily_loss" in r for r in res.fail_reasons))

    def test_fail_drawdown_eod(self):
        trades = [
            PropTrade("2026-01-02", 500),
            PropTrade("2026-01-03", -2500),
        ]
        cfg = PropConfig(profit_target=5000, max_drawdown=2000, drawdown_type="EOD")
        res = simulate_prop_account(trades, cfg)
        self.assertEqual(res.outcome, "FAIL")

    def test_r_multiple_trades(self):
        rows = trades_from_r_multiples(
            trade_rs=[1.0, -1.0, 2.0],
            dates=["2026-01-02", "2026-01-03", "2026-01-06"],
            risk_per_trade_usd=250,
            strategy="NQ_DVP",
        )
        self.assertEqual(len(rows), 3)
        self.assertAlmostEqual(rows[0].pnl_usd, 250)


class TestPositionSizing(unittest.TestCase):
    def test_mnq_frozen_stop_risk(self):
        risk = mnq_dvp_frozen_risk(1)
        self.assertAlmostEqual(risk["stop_dollar_risk"], 160.0)
        self.assertAlmostEqual(risk["long_target_dollars"], 80.0)
        self.assertAlmostEqual(risk["short_target_dollars"], 100.0)

    def test_reject_when_min_contract_exceeds_risk(self):
        res = size_position(
            SizingInput(
                strategy="GC_V2",
                signal_instrument="GC",
                execution_instrument="MGC",
                entry=2400.0,
                stop=2388.0,
                max_dollar_risk=100.0,
                contract_point_value=10.0,
                contract_tick_size=0.1,
            )
        )
        self.assertTrue(res.rejected)

    def test_mgc_metadata_sizing(self):
        res = size_from_metadata(
            execution_root="MGC",
            entry=2400.0,
            stop=2395.0,
            max_dollar_risk=100.0,
            strategy="GC_V2",
            signal_instrument="GC",
        )
        self.assertFalse(res.rejected)
        self.assertEqual(res.permitted_quantity, 2)


class TestAccountRisk(unittest.TestCase):
    def test_blocks_wrong_account(self):
        ctx = RiskCheckContext(
            account="Live",
            instrument="MNQ SEP26",
            quantity=1,
            strategy="NQ_DVP",
            strategy_hash=NQ_FROZEN_HASH,
        )
        res = run_account_risk_checks(ctx)
        self.assertFalse(res.ok)
        self.assertTrue(any("LIVE_ACCOUNT_BLOCKED" in b for b in res.blocks))

    def test_blocks_stale_data(self):
        ctx = RiskCheckContext(
            account="Sim101",
            instrument="MNQ SEP26",
            quantity=1,
            strategy="NQ_DVP",
            strategy_hash=NQ_FROZEN_HASH,
            data_stale=True,
        )
        res = run_account_risk_checks(ctx)
        self.assertIn("STALE_DATA_BLOCK", res.blocks)

    def test_blocks_hash_mismatch(self):
        ctx = RiskCheckContext(
            account="Sim101",
            instrument="MNQ SEP26",
            quantity=1,
            strategy="NQ_DVP",
            strategy_hash="deadbeef",
        )
        res = run_account_risk_checks(ctx)
        self.assertIn("STRATEGY_HASH_MISMATCH", res.blocks)

    def test_nq_daily_limits(self):
        ctx = RiskCheckContext(
            account="Sim101",
            instrument="MNQ SEP26",
            quantity=1,
            strategy="NQ_DVP",
            strategy_hash=NQ_FROZEN_HASH,
            daily_trades=4,
        )
        res = run_account_risk_checks(ctx)
        self.assertIn("NQ_MAX_TRADES_PER_DAY", res.blocks)


class TestRestartRecovery(unittest.TestCase):
    _backup: str | None = None

    def setUp(self):
        JOURNAL_DIR.mkdir(parents=True, exist_ok=True)
        if STATE_PATH.exists():
            self._backup = STATE_PATH.read_text(encoding="utf-8")

    def tearDown(self):
        if self._backup is not None:
            STATE_PATH.write_text(self._backup, encoding="utf-8")
        elif STATE_PATH.exists():
            STATE_PATH.unlink()

    def test_state_roundtrip_and_dedupe(self):
        st = {
            "mode": "DRY_RUN",
            "state": "WAITING",
            "seen_triggers": ["abc123"],
            "daily": {"2026-08-14": {"trades": 1, "losses": 0}},
            "open_trade": None,
            "halted": False,
        }
        save_state(st)
        loaded = load_state()
        self.assertEqual(loaded["seen_triggers"], ["abc123"])
        self.assertEqual(loaded["daily"]["2026-08-14"]["trades"], 1)

    def test_corrupt_state_fails_closed(self):
        STATE_PATH.write_text("{not-json", encoding="utf-8")
        with self.assertRaises(json.JSONDecodeError):
            load_state()

    def test_halt_persists(self):
        set_halt(True)
        st = load_state()
        self.assertTrue(st.get("halted"))


class TestEmergencyControls(unittest.TestCase):
    _backup: str | None = None

    def setUp(self):
        JOURNAL_DIR.mkdir(parents=True, exist_ok=True)
        if STATE_PATH.exists():
            self._backup = STATE_PATH.read_text(encoding="utf-8")

    def tearDown(self):
        if self._backup is not None:
            STATE_PATH.write_text(self._backup, encoding="utf-8")

    def test_halt_resume_cycle(self):
        set_halt(True)
        st = load_state()
        self.assertTrue(st.get("halted"))
        set_halt(False)
        st2 = load_state()
        self.assertFalse(st2.get("halted"))


if __name__ == "__main__":
    unittest.main()
