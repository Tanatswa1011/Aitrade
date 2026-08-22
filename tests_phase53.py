"""Phase 53 — pre-purchase shadow tests. DRY_RUN. Frozen strategies untouched."""
from __future__ import annotations

import json
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from nq_microstructure_models import FROZEN_GC_HASH, FROZEN_NQ_HASH
from phase34_validate import GC_FILE_SHA, NQ_FILE_SHA, assert_frozen, file_sha256
from phase52_policy import CHICAGO, NY, START_EQUITY, chicago_session_id, in_fundednext_flat_window, news_blackout_window
from phase53_engine import (
    ShadowAccount,
    calendar_status_for,
    classify_health,
    event_datetime,
    freeze_verdict,
    integrity_snapshot,
    policy_verdict,
    process_signal,
    reset_audit,
    simulate_fill,
)
from prop_rules_v1 import in_fundednext_flat_window as fn_flat
from aitrade_operating_policy import load_operating_policy

ROOT = Path(__file__).resolve().parent
UTC = ZoneInfo("UTC")


def _sig(**over):
    base = {
        "trading_date": "2026-08-12",
        "direction": "LONG",
        "entry_timestamp": 1786552200,
        "entry_price": 20000.0,
        "stop_price": 19920.0,
        "target_price": 20040.0,
        "exit_price": 20040.0,
        "outcome": "TARGET_HIT",
        "r_multiple": 0.5,
    }
    base.update(over)
    return base


def _ct(y, m, d, hh, mm=0, ss=0):
    return datetime(y, m, d, hh, mm, ss, tzinfo=CHICAGO)


def _et(y, m, d, hh, mm=0, ss=0):
    return datetime(y, m, d, hh, mm, ss, tzinfo=NY)


class FreezeAndPolicyTests(unittest.TestCase):
    def test_hashes(self):
        snap = integrity_snapshot()
        self.assertIsNone(freeze_verdict(snap))
        self.assertIsNone(policy_verdict(snap))
        self.assertEqual(snap["gc"], FROZEN_GC_HASH)
        self.assertEqual(snap["nq"], FROZEN_NQ_HASH)
        self.assertEqual(file_sha256(ROOT / "strategy_frozen" / "gc_vwap_v2_phase26.json"), GC_FILE_SHA)
        self.assertEqual(file_sha256(ROOT / "strategy_frozen" / "nq_dvp_phase30.json"), NQ_FILE_SHA)
        self.assertTrue(assert_frozen().get("ok"))

    def test_dry_run(self):
        pol = load_operating_policy()
        self.assertEqual(pol.execution_default, "DRY_RUN")
        self.assertFalse(pol.broker_execution)


class IntentSafetyTests(unittest.TestCase):
    def setUp(self):
        reset_audit()
        self.acct = ShadowAccount()
        self.now = _ct(2026, 8, 12, 10, 30)

    def test_valid_2mnq_fast(self):
        out = process_signal(self.acct, signal=_sig(), now=self.now, calendar_status="OK", event_ts=None)
        self.assertTrue(out["accepted"])
        self.assertEqual(out["quantity_allowed"], 2)
        self.assertEqual(self.acct.orders_transmitted, 0)

    def test_protected_1mnq(self):
        self.acct.demoted = True
        self.acct.state = "EVAL_PROTECTED"
        out = process_signal(self.acct, signal=_sig(), now=self.now, calendar_status="OK", event_ts=None)
        self.assertTrue(out["accepted"])
        self.assertEqual(out["quantity_allowed"], 1)

    def test_reject_3mnq(self):
        out = process_signal(
            self.acct, signal=_sig(), now=self.now, calendar_status="OK", event_ts=None, requested_qty=3
        )
        self.assertFalse(out["accepted"])
        self.assertIn(out["rejection_reason"], ("BLOCK_QTY_3MNQ_REJECTED", "MAX_POSITION_EXCEEDED"))

    def test_after_daily_stop(self):
        self.acct.session_id = chicago_session_id(self.now)
        self.acct.daily_stopped = True
        self.acct.state = "EVAL_DAILY_STOPPED"
        out = process_signal(self.acct, signal=_sig(), now=self.now, calendar_status="OK", event_ts=None)
        self.assertFalse(out["accepted"])
        self.assertEqual(out["rejection_reason"], "DAILY_STOP_TRIGGERED")

    def test_duplicate(self):
        process_signal(self.acct, signal=_sig(), now=self.now, calendar_status="OK", event_ts=None)
        out = process_signal(self.acct, signal=_sig(), now=self.now, calendar_status="OK", event_ts=None)
        self.assertFalse(out["accepted"])
        self.assertEqual(out["rejection_reason"], "DUPLICATE_ORDER_DETECTED")

    def test_stale(self):
        out = process_signal(
            self.acct, signal=_sig(), now=self.now, calendar_status="OK", event_ts=None, data_age_sec=31
        )
        self.assertEqual(out["rejection_reason"], "LIVE_DATA_STALE")

    def test_unknown_equity(self):
        out = process_signal(
            self.acct, signal=_sig(), now=self.now, calendar_status="OK", event_ts=None, equity_override=None
        )
        self.assertEqual(out["rejection_reason"], "ACCOUNT_EQUITY_UNKNOWN")

    def test_wrong_hash(self):
        out = process_signal(
            self.acct, signal=_sig(), now=self.now, calendar_status="OK", event_ts=None, strategy_hash="deadbeef"
        )
        self.assertEqual(out["rejection_reason"], "STRATEGY_HASH_MISMATCH")
        self.assertEqual(self.acct.state, "PAUSED")

    def test_missing_calendar(self):
        out = process_signal(self.acct, signal=_sig(), now=self.now, calendar_status="MISSING", event_ts=None)
        self.assertEqual(out["rejection_reason"], "NEWS_BLACKOUT_VIOLATION_RISK")

    def test_never_increase_after_loss(self):
        self.acct.consecutive_losses = 2
        self.acct.last_qty = 1
        out = process_signal(self.acct, signal=_sig(), now=self.now, calendar_status="OK", event_ts=None, requested_qty=2)
        self.assertLessEqual(out["quantity_allowed"], 1)

    def test_never_increase_because_behind(self):
        # remaining profit still large; requested 3 still rejected
        out = process_signal(
            self.acct, signal=_sig(), now=self.now, calendar_status="OK", event_ts=None, requested_qty=3
        )
        self.assertFalse(out["accepted"])


class NewsCalendarTests(unittest.TestCase):
    def setUp(self):
        reset_audit()
        self.acct = ShadowAccount()
        self.event = _et(2026, 8, 12, 8, 30, 0)

    def test_1s_before_lock_allow(self):
        start, _ = news_blackout_window(self.event)
        now = start - timedelta(seconds=1)
        out = process_signal(self.acct, signal=_sig(), now=now.astimezone(CHICAGO), calendar_status="OK", event_ts=self.event)
        self.assertTrue(out["accepted"])

    def test_inside_blackout_reject(self):
        out = process_signal(
            ShadowAccount(), signal=_sig(), now=self.event.astimezone(CHICAGO), calendar_status="OK", event_ts=self.event
        )
        self.assertEqual(out["rejection_reason"], "NEWS_BLACKOUT_VIOLATION_RISK")

    def test_after_blackout_allow(self):
        now = _et(2026, 8, 12, 8, 36, 0)
        out = process_signal(self.acct, signal=_sig(), now=now.astimezone(CHICAGO), calendar_status="OK", event_ts=self.event)
        self.assertTrue(out["accepted"])

    def test_event_datetime_ny(self):
        ts = event_datetime("2026-08-12", "08:30")
        self.assertEqual(ts.tzinfo, NY)
        self.assertEqual(ts.hour, 8)
        self.assertEqual(ts.minute, 30)


class TimezoneSessionTests(unittest.TestCase):
    def test_1700_ct_resets_session(self):
        a = chicago_session_id(_ct(2026, 8, 12, 16, 59))
        b = chicago_session_id(_ct(2026, 8, 12, 17, 0))
        self.assertNotEqual(a, b)
        utc = datetime(2026, 8, 12, 22, 0, tzinfo=UTC)  # 17:00 CT in CDT
        self.assertEqual(chicago_session_id(utc), chicago_session_id(_ct(2026, 8, 12, 17, 0)))

    def test_ny_utc_chicago_agree(self):
        et = _et(2026, 8, 12, 18, 0)
        ct = et.astimezone(CHICAGO)
        utc = et.astimezone(UTC)
        self.assertEqual(chicago_session_id(et), chicago_session_id(ct))
        self.assertEqual(chicago_session_id(ct), chicago_session_id(utc))

    def test_flat_1510_ct(self):
        self.assertTrue(fn_flat(_ct(2026, 8, 12, 15, 10)))
        self.assertFalse(fn_flat(_ct(2026, 8, 12, 15, 9)))
        self.assertFalse(fn_flat(_ct(2026, 8, 12, 17, 0)))

    def test_weekend(self):
        self.assertTrue(fn_flat(_ct(2026, 8, 14, 15, 10)))  # Friday 15:10 CT
        self.assertFalse(fn_flat(_ct(2026, 8, 14, 12, 0)))
        self.assertTrue(in_fundednext_flat_window(_ct(2026, 8, 15, 10, 0)))  # Saturday
        self.assertTrue(in_fundednext_flat_window(_ct(2026, 8, 16, 16, 59)))  # Sunday before 17:00
        self.assertFalse(in_fundednext_flat_window(_ct(2026, 8, 16, 17, 0)))

    def test_daily_stop_then_reset_allows(self):
        reset_audit()
        acct = ShadowAccount()
        acct.daily_stopped = True
        acct.state = "EVAL_DAILY_STOPPED"
        acct.session_id = chicago_session_id(_ct(2026, 8, 12, 16, 0))
        out = process_signal(acct, signal=_sig(), now=_ct(2026, 8, 12, 17, 1), calendar_status="OK", event_ts=None)
        self.assertTrue(out["accepted"])
        self.assertNotEqual(acct.state, "EVAL_DAILY_STOPPED")

    def test_open_position_not_required_at_reset_dvp_flat_before_fn(self):
        # DVP force-close 15:55 ET = 14:55 CT, before FN 15:10 CT.
        et_force = _et(2026, 8, 12, 15, 55)
        self.assertFalse(fn_flat(et_force.astimezone(CHICAGO)))
        self.assertTrue(fn_flat(_ct(2026, 8, 12, 15, 10)))


class FillModelTests(unittest.TestCase):
    def test_adverse_not_perfect(self):
        fill = simulate_fill(
            direction="LONG",
            theoretical_entry=20000.0,
            theoretical_exit=20040.0,
            outcome="TARGET_HIT",
            qty=2,
        )
        self.assertTrue(fill["filled"])
        self.assertGreater(fill["entry_fill"], 20000.0)
        self.assertLess(fill["exit_fill"], 20040.0)
        self.assertLess(fill["realized_R"], 0.5)

    def test_missed_entry(self):
        fill = simulate_fill(
            direction="LONG",
            theoretical_entry=20000.0,
            theoretical_exit=20040.0,
            outcome="TARGET_HIT",
            qty=2,
            miss_entry=True,
        )
        self.assertFalse(fill["filled"])

    def test_partial(self):
        fill = simulate_fill(
            direction="LONG",
            theoretical_entry=20000.0,
            theoretical_exit=20040.0,
            outcome="TARGET_HIT",
            qty=2,
            partial_frac=0.5,
        )
        self.assertEqual(fill["qty_filled"], 1)


class FailureInjectionTests(unittest.TestCase):
    def setUp(self):
        reset_audit()
        self.acct = ShadowAccount()
        self.now = _ct(2026, 8, 12, 10, 30)

    def test_stale_quote(self):
        out = process_signal(self.acct, signal=_sig(), now=self.now, calendar_status="OK", event_ts=None, data_age_sec=99)
        self.assertEqual(out["kill_switch"], "LIVE_DATA_STALE")

    def test_calendar_unavailable(self):
        out = process_signal(self.acct, signal=_sig(), now=self.now, calendar_status="STALE", event_ts=None)
        self.assertEqual(out["kill_switch"], "NEWS_BLACKOUT_VIOLATION_RISK")

    def test_equity_unavailable(self):
        out = process_signal(self.acct, signal=_sig(), now=self.now, calendar_status="OK", event_ts=None, equity_override=None)
        self.assertEqual(out["kill_switch"], "ACCOUNT_EQUITY_UNKNOWN")

    def test_duplicate_ack(self):
        process_signal(self.acct, signal=_sig(), now=self.now, calendar_status="OK", event_ts=None)
        out = process_signal(self.acct, signal=_sig(), now=self.now, calendar_status="OK", event_ts=None, duplicate=True)
        self.assertEqual(out["kill_switch"], "DUPLICATE_ORDER_DETECTED")

    def test_wrong_strategy_id_hash(self):
        out = process_signal(
            self.acct, signal=_sig(), now=self.now, calendar_status="OK", event_ts=None, strategy_hash="wrong"
        )
        self.assertEqual(out["kill_switch"], "STRATEGY_HASH_MISMATCH")

    def test_position_unknown(self):
        out = process_signal(
            self.acct, signal=_sig(), now=self.now, calendar_status="OK", event_ts=None, position_known=False
        )
        self.assertEqual(out["kill_switch"], "POSITION_STATE_MISMATCH")

    def test_negative_remaining_dd(self):
        out = process_signal(
            self.acct,
            signal=_sig(),
            now=self.now,
            calendar_status="OK",
            event_ts=None,
            equity_override=48000.0,
            mll_override=48500.0,
        )
        self.assertEqual(out["kill_switch"], "ACCOUNT_BREACH_IMMINENT")

    def test_broker_unstable(self):
        out = process_signal(self.acct, signal=_sig(), now=self.now, calendar_status="OK", event_ts=None, broker_ok=False)
        self.assertEqual(out["kill_switch"], "BROKER_CONNECTION_UNSTABLE")

    def test_no_orders_transmitted(self):
        process_signal(self.acct, signal=_sig(), now=self.now, calendar_status="OK", event_ts=None)
        self.assertEqual(self.acct.orders_transmitted, 0)


class HealthClassifierTests(unittest.TestCase):
    def test_insufficient(self):
        h = classify_health([0.5, -1.0], flip_pct=0.0, flip_n=2)
        self.assertEqual(h["class"], "INSUFFICIENT_SAMPLE")

    def test_healthy_frozen_like(self):
        rs = ([0.5, 0.5, -0.8] * 10)
        h = classify_health(rs, flip_pct=0.0, flip_n=20)
        self.assertIn(h["class"], ("HEALTHY", "WATCH"))

    def test_degraded_flip(self):
        rs = [-1.0] * 25
        h = classify_health(rs, flip_pct=0.12, flip_n=25)
        self.assertEqual(h["class"], "DEGRADED")


class SignalNotMutatedTests(unittest.TestCase):
    def test_rejected_keeps_frozen_prices(self):
        reset_audit()
        acct = ShadowAccount()
        sig = _sig()
        out = process_signal(acct, signal=sig, now=_ct(2026, 8, 12, 10, 30), calendar_status="OK", event_ts=None, requested_qty=3)
        self.assertEqual(out["signal"]["intended_entry"], 20000.0)
        self.assertEqual(out["signal"]["stop"], 19920.0)
        self.assertEqual(out["signal"]["target"], 20040.0)
        self.assertFalse(out["signal"]["signal_mutated"])


class AdditionalFailureInjectionTests(unittest.TestCase):
    def setUp(self):
        reset_audit()
        self.acct = ShadowAccount()
        self.now = _ct(2026, 8, 12, 10, 30)

    def test_missing_candle(self):
        out = process_signal(
            self.acct, signal=_sig(), now=self.now, calendar_status="OK", event_ts=None, data_fault="MISSING_CANDLE"
        )
        self.assertEqual(out["kill_switch"], "LIVE_DATA_STALE")
        self.assertFalse(out["accepted"])

    def test_duplicate_candle(self):
        out = process_signal(
            ShadowAccount(), signal=_sig(), now=self.now, calendar_status="OK", event_ts=None, data_fault="DUPLICATE_CANDLE"
        )
        self.assertEqual(out["kill_switch"], "LIVE_DATA_STALE")

    def test_out_of_order_timestamp(self):
        out = process_signal(
            ShadowAccount(), signal=_sig(), now=self.now, calendar_status="OK", event_ts=None, data_fault="OOO_TIMESTAMP"
        )
        self.assertEqual(out["kill_switch"], "LIVE_DATA_STALE")

    def test_delayed_ack(self):
        out = process_signal(
            self.acct, signal=_sig(), now=self.now, calendar_status="OK", event_ts=None, ack_delay_sec=12
        )
        self.assertEqual(out["kill_switch"], "ORDER_STATE_MISMATCH")

    def test_order_rejected(self):
        out = process_signal(
            self.acct, signal=_sig(), now=self.now, calendar_status="OK", event_ts=None, ack_fault="ORDER_REJECTED"
        )
        self.assertEqual(out["kill_switch"], "BROKER_CONNECTION_UNSTABLE")

    def test_partial_fill_path(self):
        out = process_signal(
            self.acct, signal=_sig(), now=self.now, calendar_status="OK", event_ts=None, partial_frac=0.5
        )
        self.assertTrue(out["accepted"])
        self.assertEqual(out["fill"]["qty_filled"], 1)

    def test_unexpected_open_position(self):
        out = process_signal(
            self.acct, signal=_sig(), now=self.now, calendar_status="OK", event_ts=None, position_fault="UNEXPECTED_OPEN"
        )
        self.assertEqual(out["kill_switch"], "POSITION_STATE_MISMATCH")

    def test_position_missing(self):
        out = process_signal(
            ShadowAccount(),
            signal=_sig(),
            now=self.now,
            calendar_status="OK",
            event_ts=None,
            position_fault="EXPECTED_MISSING",
        )
        self.assertEqual(out["kill_switch"], "POSITION_STATE_MISMATCH")

    def test_wrong_strategy_id(self):
        out = process_signal(
            self.acct,
            signal=_sig(),
            now=self.now,
            calendar_status="OK",
            event_ts=None,
            strategy_id="GC_VWAP_V2",
        )
        self.assertEqual(out["kill_switch"], "STRATEGY_HASH_MISMATCH")

    def test_corrupted_payload(self):
        out = process_signal(
            ShadowAccount(),
            signal=_sig(entry_price="not-a-price"),
            now=self.now,
            calendar_status="OK",
            event_ts=None,
        )
        self.assertEqual(out["kill_switch"], "SIGNAL_PAYLOAD_CORRUPT")

    def test_impossible_mll(self):
        out = process_signal(
            self.acct, signal=_sig(), now=self.now, calendar_status="OK", event_ts=None, impossible_mll=True
        )
        self.assertEqual(out["kill_switch"], "DRAW_DOWN_CALCULATION_INVALID")

    def test_balance_jump(self):
        out = process_signal(
            self.acct, signal=_sig(), now=self.now, calendar_status="OK", event_ts=None, balance_jump=True
        )
        self.assertEqual(out["kill_switch"], "ACCOUNT_EQUITY_UNKNOWN")

    def test_pnl_mismatch(self):
        out = process_signal(
            self.acct, signal=_sig(), now=self.now, calendar_status="OK", event_ts=None, pnl_mismatch=True
        )
        self.assertEqual(out["kill_switch"], "DRAW_DOWN_CALCULATION_INVALID")

    def test_position_size_mismatch_open_qty(self):
        acct = ShadowAccount()
        acct.session_id = chicago_session_id(self.now)
        acct.open_qty = 1
        out = process_signal(acct, signal=_sig(), now=self.now, calendar_status="OK", event_ts=None, requested_qty=2)
        self.assertFalse(out["accepted"])
        self.assertEqual(out["rejection_reason"], "MAX_POSITION_EXCEEDED")

    def test_open_position_crossing_reset_fail_closed(self):
        acct = ShadowAccount()
        acct.session_id = chicago_session_id(_ct(2026, 8, 12, 16, 0))
        acct.open_qty = 2
        out = process_signal(acct, signal=_sig(), now=_ct(2026, 8, 12, 17, 1), calendar_status="OK", event_ts=None)
        self.assertFalse(out["accepted"])
        self.assertEqual(out["kill_switch"], "OVERNIGHT_POSITION_AT_SESSION_RESET")
        self.assertEqual(acct.state, "PAUSED")
        self.assertEqual(acct.orders_transmitted, 0)


if __name__ == "__main__":
    unittest.main()
