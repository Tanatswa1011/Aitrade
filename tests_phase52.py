"""Phase 52 — prop execution policy. Deliberately tries to violate the policy."""
from __future__ import annotations

import json
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from aitrade_operating_policy import load_operating_policy
from nq_microstructure_models import FROZEN_GC_HASH, FROZEN_NQ_HASH
from phase34_validate import GC_FILE_SHA, NQ_FILE_SHA, assert_frozen, file_sha256
from phase52_degradation import MIN_SAMPLE, DegradationMonitor
from phase52_policy import (
    CHICAGO,
    DAILY_STOP_FRAC,
    FAST_QTY,
    MAX_LOSS,
    NY,
    PROFIT_TARGET,
    REJECT_QTY,
    SAFE_QTY,
    START_EQUITY,
    UNIT_RISK_USD,
    allowed_qty,
    chicago_session_id,
    daily_governor_triggered,
    daily_loss_usd,
    evaluate_intent,
    fn_eval_rules_catalog,
    in_internal_news_lock,
    near_target,
    news_blackout_window,
    next_state,
    remaining_drawdown,
    session_daily_stop_threshold,
)
from prop_rules_v1 import REQUIRES_CONFIRMATION
from risk_manager import propose_size

ROOT = Path(__file__).resolve().parent


def _ct(y, m, d, hh, mm=0, ss=0) -> datetime:
    return datetime(y, m, d, hh, mm, ss, tzinfo=CHICAGO)


def _et(y, m, d, hh, mm=0, ss=0) -> datetime:
    return datetime(y, m, d, hh, mm, ss, tzinfo=NY)


def _ok_kwargs(**over):
    now = _ct(2026, 8, 19, 10, 30)
    base = dict(
        state="EVAL_FAST",
        intent_qty=2,
        action="NEW_ENTRY",
        now=now,
        equity=START_EQUITY,
        mll=START_EQUITY - MAX_LOSS,
        session_open_equity=START_EQUITY,
        remaining_dd_open=MAX_LOSS,
        realized_pnl=0.0,
        open_pnl=0.0,
        open_qty=0,
        last_qty=2,
        consecutive_losses=0,
        demoted=False,
        strategy_hash=FROZEN_NQ_HASH,
        calendar_status="OK",
        event_ts=None,
        data_age_sec=1.0,
        broker_ok=True,
        position_known=True,
        order_known=True,
        duplicate=False,
        in_price_limit=False,
        prop_rules_ok=True,
    )
    base.update(over)
    return base


class FreezeHashTests(unittest.TestCase):
    def test_frozen_hashes_match_phase52_lock(self):
        gc = json.loads((ROOT / "strategy_frozen" / "gc_vwap_v2_phase26.json").read_text(encoding="utf-8"))
        nq = json.loads((ROOT / "strategy_frozen" / "nq_dvp_phase30.json").read_text(encoding="utf-8"))
        self.assertEqual(gc["frozen_config_hash"], FROZEN_GC_HASH)
        self.assertEqual(nq["frozen_config_hash"], FROZEN_NQ_HASH)
        self.assertEqual(gc["frozen_config_hash"], "0695d7a881b6247033426bceb077b6895b96405b826c21f32a109a02f44b2d43")
        self.assertEqual(nq["frozen_config_hash"], "935e3a616351b09dbfa8d2d0e2b5d6850be803fb385ec5e5f8e3593856a1212a")
        self.assertEqual(file_sha256(ROOT / "strategy_frozen" / "gc_vwap_v2_phase26.json"), GC_FILE_SHA)
        self.assertEqual(file_sha256(ROOT / "strategy_frozen" / "nq_dvp_phase30.json"), NQ_FILE_SHA)
        self.assertFalse((ROOT / "strategy_frozen" / "es_dvp_phase47.json").exists())
        self.assertTrue(assert_frozen().get("ok"))

    def test_paper_journals_empty(self):
        for rel in (
            "journal/phase26_gc_vwap_v2_paper/paper_trades.jsonl",
            "journal/phase30_nq_dvp_paper/paper_trades.jsonl",
            "journal/phase47_es_dvp_paper/paper_trades.jsonl",
        ):
            self.assertEqual((ROOT / rel).stat().st_size, 0)

    def test_dry_run_locked_qty(self):
        pol = load_operating_policy()
        self.assertEqual(pol.execution_default, "DRY_RUN")
        self.assertFalse(pol.broker_execution)
        rpt = pol.numerics.get("risk_per_trade")
        self.assertIsInstance(rpt, dict)
        self.assertEqual(rpt.get("mode"), "PROP_CONTRACT_QTY")
        self.assertIsNotNone(rpt)
        out = propose_size()
        self.assertEqual(out["status"], "PROP_QTY_LOCKED")
        self.assertEqual(out["quantity"], 2)
        self.assertFalse(out["broker_execution"])


class RuleEngineTests(unittest.TestCase):
    def test_catalog_material_survival_confirmed(self):
        rows = fn_eval_rules_catalog()
        self.assertGreaterEqual(len(rows), 15)
        material_rc = [r for r in rows if r["material_eval_survival"] and r["status"] == REQUIRES_CONFIRMATION]
        self.assertEqual(material_rc, [])

    def test_catalog_marks_automation_unconfirmed(self):
        rows = {r["canonical_rule"]: r for r in fn_eval_rules_catalog()}
        self.assertEqual(rows["AUTOMATION_ALLOWED"]["status"], REQUIRES_CONFIRMATION)
        self.assertFalse(rows["AUTOMATION_ALLOWED"]["material_eval_survival"])


class StateTransitionTests(unittest.TestCase):
    def test_fast_is_privilege(self):
        self.assertEqual(next_state("EVAL_FAST", demoted=True), "EVAL_PROTECTED")
        self.assertEqual(next_state("EVAL_FAST", near=True), "EVAL_NEAR_TARGET")
        self.assertEqual(next_state("EVAL_FAST", daily_stopped=True), "EVAL_DAILY_STOPPED")
        self.assertEqual(next_state("EVAL_FAST", integrity_fail=True), "PAUSED")
        self.assertEqual(next_state("EVAL_SAFE", breached=True), "EVAL_BREACHED")
        self.assertEqual(next_state("EVAL_FAST", passed=True), "EVAL_PASSED")

    def test_daily_stop_resets_next_session(self):
        st = next_state("EVAL_DAILY_STOPPED", new_session=True, demoted=False)
        self.assertEqual(st, "EVAL_FAST")
        st = next_state("EVAL_DAILY_STOPPED", new_session=True, demoted=True)
        self.assertEqual(st, "EVAL_PROTECTED")

    def test_paused_and_breach_absorbing(self):
        self.assertEqual(next_state("PAUSED", near=True, new_session=True), "PAUSED")
        self.assertEqual(next_state("EVAL_BREACHED", new_session=True), "EVAL_BREACHED")


class DrawdownAndGovernorTests(unittest.TestCase):
    def test_remaining_dd_equation(self):
        self.assertEqual(remaining_drawdown(50000.0, 48500.0), 1500.0)

    def test_threshold_is_35pct_of_session_open_remaining(self):
        thr = session_daily_stop_threshold(1500.0)
        self.assertAlmostEqual(thr, 525.0, places=9)
        self.assertAlmostEqual(DAILY_STOP_FRAC, 0.35)

    def test_governor_includes_unrealized_via_marked_equity(self):
        # session open 50000; marked 49474.99 is 525.01 loss → trigger
        self.assertTrue(
            daily_governor_triggered(
                session_open_equity=50000.0,
                current_equity=49474.99,
                remaining_dd_at_session_open=1500.0,
            )
        )
        self.assertFalse(
            daily_governor_triggered(
                session_open_equity=50000.0,
                current_equity=49475.01,
                remaining_dd_at_session_open=1500.0,
            )
        )

    def test_boundary_equals_threshold_triggers(self):
        loss = daily_loss_usd(session_open_equity=50000.0, current_equity=49475.0)
        self.assertAlmostEqual(loss, 525.0, places=9)
        self.assertTrue(
            daily_governor_triggered(
                session_open_equity=50000.0,
                current_equity=49475.0,
                remaining_dd_at_session_open=1500.0,
            )
        )

    def test_gap_through_blocks_new_entries_does_not_force_flatten(self):
        d = evaluate_intent(**_ok_kwargs(equity=49300.0, remaining_dd_open=1500.0, session_open_equity=50000.0))
        self.assertEqual(d.verdict, "BLOCK")
        self.assertEqual(d.code, "DAILY_STOP_TRIGGERED")
        self.assertEqual(d.state, "EVAL_DAILY_STOPPED")
        self.assertFalse(d.flatten)
        self.assertIn("cancel_entries", d.actions)

    def test_session_id_resets_at_1700_ct(self):
        a = chicago_session_id(_ct(2026, 8, 19, 16, 59))
        b = chicago_session_id(_ct(2026, 8, 19, 17, 0))
        self.assertNotEqual(a, b)

    def test_hold_stop_allowed_after_daily_stop(self):
        d = evaluate_intent(
            **_ok_kwargs(
                action="HOLD_STOP",
                equity=49400.0,
                remaining_dd_open=1500.0,
                session_open_equity=50000.0,
                daily_already_stopped=True,
            )
        )
        self.assertEqual(d.verdict, "ALLOW")


class PositionSizingTests(unittest.TestCase):
    def test_reject_3_mnq(self):
        q, why = allowed_qty(
            state="EVAL_FAST",
            requested=REJECT_QTY,
            remaining_dd=1500.0,
            daily_capacity=525.0,
            demoted=False,
            consecutive_losses=0,
            last_qty=2,
        )
        self.assertEqual(q, 0)
        self.assertEqual(why, "BLOCK_QTY_3MNQ_REJECTED")
        out = propose_size(requested_qty=3)
        self.assertEqual(out["quantity"], 0)
        self.assertEqual(out["status"], "BLOCK_QTY_3MNQ_REJECTED")

    def test_fast_2_safe_1(self):
        q, _ = allowed_qty(
            state="EVAL_FAST", requested=2, remaining_dd=1500.0, daily_capacity=525.0,
            demoted=False, consecutive_losses=0, last_qty=2,
        )
        self.assertEqual(q, FAST_QTY)
        q, _ = allowed_qty(
            state="EVAL_SAFE", requested=2, remaining_dd=1500.0, daily_capacity=525.0,
            demoted=False, consecutive_losses=0, last_qty=2,
        )
        self.assertEqual(q, SAFE_QTY)

    def test_never_increase_after_loss(self):
        q, _ = allowed_qty(
            state="EVAL_FAST", requested=2, remaining_dd=1500.0, daily_capacity=525.0,
            demoted=False, consecutive_losses=2, last_qty=1,
        )
        self.assertEqual(q, 1)

    def test_dd_capacity_blocks_when_one_stop_exceeds_remaining(self):
        q, why = allowed_qty(
            state="EVAL_FAST", requested=2, remaining_dd=100.0, daily_capacity=525.0,
            demoted=False, consecutive_losses=0, last_qty=2,
        )
        self.assertEqual(q, 0)
        self.assertEqual(why, "BLOCK_INSUFFICIENT_RISK_CAPACITY")


class NearTargetTests(unittest.TestCase):
    def test_one_fast_r(self):
        self.assertTrue(near_target(320.0, rule="ONE_FAST_R"))
        self.assertFalse(near_target(321.0, rule="ONE_FAST_R"))

    def test_evaluate_intent_sizes_down_near_target(self):
        equity = START_EQUITY + PROFIT_TARGET - 100.0  # remaining $100 ≤ PCT_95 $125
        d = evaluate_intent(**_ok_kwargs(equity=equity, remaining_dd_open=1500.0, mll=START_EQUITY - MAX_LOSS))
        self.assertEqual(d.verdict, "ALLOW")
        self.assertEqual(d.allowed_qty, 1)
        self.assertEqual(d.state, "EVAL_NEAR_TARGET")


class DegradationMonitorTests(unittest.TestCase):
    def test_min_sample_no_demote(self):
        m = DegradationMonitor()
        for _ in range(MIN_SAMPLE - 1):
            info = m.observe(-1.0)
        self.assertFalse(info["demoted"])

    def test_hard_demote_on_negative_expectancy(self):
        m = DegradationMonitor()
        info = {}
        for _ in range(MIN_SAMPLE):
            info = m.observe(-1.0)
        self.assertTrue(info["demoted"])

    def test_hysteresis_one_winner_does_not_restore_fast(self):
        m = DegradationMonitor()
        for _ in range(MIN_SAMPLE):
            m.observe(-1.0)
        self.assertTrue(m.demoted)
        info = m.observe(1.0)
        self.assertTrue(info["demoted"])

    def test_winner_loser_collapse_demotes(self):
        m = DegradationMonitor()
        # ~18pp WR collapse vs frozen 0.663 → ~0.48
        seq = [1.0] * 10 + [-1.0] * 14
        info = {}
        for r in seq:
            info = m.observe(r)
        self.assertTrue(info["demoted"])


class NewsBlackoutTests(unittest.TestCase):
    def test_one_second_before_lock_allowed(self):
        event = _et(2026, 8, 19, 8, 30, 0)
        start, _ = news_blackout_window(event)
        now = start - timedelta(seconds=1)
        lock, code = in_internal_news_lock(now=now, event_ts=event, calendar_status="OK")
        self.assertFalse(lock)
        d = evaluate_intent(**_ok_kwargs(now=now.astimezone(CHICAGO), event_ts=event, calendar_status="OK"))
        self.assertEqual(d.verdict, "ALLOW")

    def test_pending_into_blackout_blocked(self):
        event = _et(2026, 8, 19, 8, 30, 0)
        now = event
        d = evaluate_intent(**_ok_kwargs(now=now.astimezone(CHICAGO), event_ts=event))
        self.assertEqual(d.verdict, "BLOCK")
        self.assertEqual(d.code, "NEWS_BLACKOUT_VIOLATION_RISK")

    def test_position_opened_before_blackout_hold_stop_allowed(self):
        event = _et(2026, 8, 19, 8, 30, 0)
        d = evaluate_intent(**_ok_kwargs(action="HOLD_STOP", now=event.astimezone(CHICAGO), event_ts=event, open_qty=2))
        self.assertEqual(d.verdict, "ALLOW")

    def test_blackout_end_signal_still_valid_then_allow(self):
        event = _et(2026, 8, 19, 8, 30, 0)
        # Clock lock is minute-precision through 08:35 ET; both locks are clear at 08:36.
        now = _et(2026, 8, 19, 8, 36, 0)
        lock, _ = in_internal_news_lock(now=now, event_ts=event, calendar_status="OK")
        self.assertFalse(lock)
        d = evaluate_intent(**_ok_kwargs(now=now.astimezone(CHICAGO), event_ts=event))
        self.assertEqual(d.verdict, "ALLOW")

    def test_missing_calendar_fail_closed(self):
        d = evaluate_intent(**_ok_kwargs(calendar_status="MISSING"))
        self.assertEqual(d.verdict, "BLOCK")
        self.assertEqual(d.code, "NEWS_BLACKOUT_VIOLATION_RISK")
        d2 = evaluate_intent(**_ok_kwargs(calendar_status="STALE"))
        self.assertEqual(d2.verdict, "BLOCK")

    def test_clock_window_0825_et(self):
        now = _et(2026, 8, 19, 8, 27)
        d = evaluate_intent(**_ok_kwargs(now=now.astimezone(CHICAGO), event_ts=None, calendar_status="OK"))
        self.assertEqual(d.verdict, "BLOCK")
        self.assertEqual(d.code, "NEWS_BLACKOUT_VIOLATION_RISK")


class KillSwitchTests(unittest.TestCase):
    def test_hash_mismatch_pauses(self):
        d = evaluate_intent(**_ok_kwargs(strategy_hash="deadbeef"))
        self.assertEqual(d.code, "STRATEGY_HASH_MISMATCH")
        self.assertEqual(d.state, "PAUSED")
        self.assertTrue(d.flatten)

    def test_equity_unknown(self):
        d = evaluate_intent(**_ok_kwargs(equity=None))
        self.assertEqual(d.code, "ACCOUNT_EQUITY_UNKNOWN")

    def test_stale_data(self):
        d = evaluate_intent(**_ok_kwargs(data_age_sec=31))
        self.assertEqual(d.code, "LIVE_DATA_STALE")

    def test_duplicate_order(self):
        d = evaluate_intent(**_ok_kwargs(duplicate=True))
        self.assertEqual(d.code, "DUPLICATE_ORDER_DETECTED")
        self.assertFalse(d.flatten)

    def test_max_position(self):
        d = evaluate_intent(**_ok_kwargs(open_qty=2, intent_qty=1))
        self.assertEqual(d.code, "MAX_POSITION_EXCEEDED")

    def test_broker_unstable(self):
        d = evaluate_intent(**_ok_kwargs(broker_ok=False))
        self.assertEqual(d.code, "BROKER_CONNECTION_UNSTABLE")

    def test_prop_rules_missing(self):
        d = evaluate_intent(**_ok_kwargs(prop_rules_ok=False))
        self.assertEqual(d.code, "PROP_RULE_DATA_MISSING")

    def test_position_mismatch(self):
        d = evaluate_intent(**_ok_kwargs(position_known=False))
        self.assertEqual(d.code, "POSITION_STATE_MISMATCH")

    def test_breach_imminent_flattens(self):
        d = evaluate_intent(**_ok_kwargs(equity=48499.0, mll=48500.0, remaining_dd_open=1.0))
        self.assertEqual(d.code, "ACCOUNT_BREACH_IMMINENT")
        self.assertTrue(d.flatten)

    def test_rejects_invalid_rather_than_warn(self):
        d = evaluate_intent(**_ok_kwargs(intent_qty=3))
        self.assertEqual(d.verdict, "BLOCK")
        self.assertEqual(d.allowed_qty, 0)


class IntentHappyPathTests(unittest.TestCase):
    def test_allow_fast_2mnq(self):
        d = evaluate_intent(**_ok_kwargs())
        self.assertEqual(d.verdict, "ALLOW")
        self.assertEqual(d.allowed_qty, 2)
        self.assertEqual(d.state, "EVAL_FAST")


if __name__ == "__main__":
    unittest.main()
