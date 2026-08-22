"""Phase 55C FundedNext prop canary — no OIF writes, PROP_EXECUTION stays false."""
from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from execution_instrument import EXEC_INSTRUMENT_DISPLAY, EXEC_INSTRUMENT_NT
from execution_status import NQ_FROZEN_HASH
from nq_dvp_nt_exec import EXEC_ACCOUNT, assert_execution_locks, plan_dvp_entry
from phase52_policy import FAST_QTY
from phase54_ops import PROP_EXECUTION
from prop_canary import (
    CANARY_ACCOUNT_ID,
    CANARY_LOGIN,
    CANARY_NT_ACCOUNT,
    ENV_FLAG,
    ENV_STATE,
    PROP_CANARY_ARMED,
    PROP_CANARY_BLOCKED,
    PROP_CANARY_COMPLETE,
    PROP_CANARY_DISARMED,
    PROP_CANARY_READY,
    PROP_FLAT_SAFE,
    PROP_LOCKED,
    arm,
    current_mode,
    disarm,
    dry_run,
    emergency_flatten,
    evaluate_preflight,
    genuine_signal,
    mark_round_trip_complete,
    mark_stop_rejected,
    observe_runtime,
    passing_context,
    public_snapshot,
    reset_for_tests,
    simulate_process_restart,
    submit_once,
)
from prop_canary_nt_exec import (
    SIM101_ACCOUNT,
    build_canary_place_oif,
    parse_oif_account,
    parse_oif_qty,
    plan_canary_bracket,
    validate_canary_oif_line,
)
import nt_ati as nt


def _flag(on: bool) -> None:
    if on:
        os.environ[ENV_FLAG] = "true"
    else:
        os.environ.pop(ENV_FLAG, None)


class PropCanaryTests(unittest.TestCase):
    def setUp(self):
        self._td = Path(tempfile.mkdtemp(prefix="prop_canary_"))
        os.environ[ENV_STATE] = str(self._td / "canary.json")
        os.environ.pop(ENV_FLAG, None)
        reset_for_tests(clear_persist=True)

    def tearDown(self):
        os.environ.pop(ENV_FLAG, None)
        os.environ.pop(ENV_STATE, None)
        reset_for_tests(clear_persist=True)

    def test_01_canary_default_disarmed(self):
        snap = public_snapshot(passing_context())
        self.assertEqual(snap["state"], PROP_LOCKED)
        self.assertFalse(snap["armed"])
        self.assertFalse(snap["flag"])
        self.assertEqual(snap["general_prop"], "LOCKED")
        self.assertFalse(PROP_EXECUTION)

    def test_02_restart_resets_disarmed(self):
        _flag(True)
        ctx = passing_context()
        out = arm(ctx)
        self.assertTrue(out["armed"])
        self.assertEqual(current_mode(ctx), PROP_CANARY_ARMED)
        simulate_process_restart()
        self.assertNotEqual(current_mode(ctx), PROP_CANARY_ARMED)
        self.assertFalse(public_snapshot(ctx)["armed"])

    def test_03_missing_canary_flag_blocks(self):
        pf = evaluate_preflight(passing_context())
        self.assertFalse(pf["ok"])
        self.assertIn("CANARY_FLAG_DISARMED", pf["errors"])
        self.assertEqual(current_mode(passing_context()), PROP_LOCKED)

    def test_04_prop_execution_false_does_not_block_narrow_canary(self):
        _flag(True)
        self.assertFalse(PROP_EXECUTION)
        ctx = passing_context(prop_execution=False)
        pf = evaluate_preflight(ctx)
        self.assertTrue(pf["ok"], pf["errors"])
        self.assertEqual(current_mode(ctx), PROP_CANARY_READY)
        blocked = evaluate_preflight(passing_context(prop_execution=True))
        self.assertFalse(blocked["ok"])
        self.assertIn("GENERAL_PROP_MUST_REMAIN_LOCKED", blocked["errors"])

    def test_05_wrong_account_blocked(self):
        _flag(True)
        pf = evaluate_preflight(passing_context(nt_account="SOMEOTHERACCT"))
        self.assertFalse(pf["ok"])
        self.assertIn("WRONG_ACCOUNT", pf["errors"])

    def test_06_missing_account_identity_blocked(self):
        _flag(True)
        pf = evaluate_preflight(passing_context(nt_account="", platform_login=None, fundednext_account_id=None))
        self.assertFalse(pf["ok"])
        self.assertIn("ACCOUNT_IDENTITY_MISSING", pf["errors"])

    def test_07_second_fundednext_account_blocked(self):
        _flag(True)
        pf = evaluate_preflight(passing_context(extra_nt_fn_accounts=("FNOTHERACCOUNT",)))
        self.assertFalse(pf["ok"])
        self.assertIn("SECOND_FUNDEDNEXT_ACCOUNT_BLOCKED", pf["errors"])
        pf2 = evaluate_preflight(passing_context(extra_mcp_ids=(3999999,)))
        self.assertIn("SECOND_FUNDEDNEXT_ACCOUNT_BLOCKED", pf2["errors"])

    def test_08_sim101_blocked_from_canary(self):
        _flag(True)
        with self.assertRaises(PermissionError) as cm:
            build_canary_place_oif(account=SIM101_ACCOUNT, action="BUY")
        self.assertIn("SIM101", str(cm.exception))
        pf = evaluate_preflight(passing_context(nt_account="Sim101", requested_account="Sim101"))
        self.assertIn("SIM101_BLOCKED_FROM_CANARY", pf["errors"])

    def test_08b_canary_and_sim101_routes_isolated(self):
        with self.assertRaises(PermissionError):
            assert_execution_locks(account=CANARY_NT_ACCOUNT, quantity=1)
        with self.assertRaises(PermissionError):
            nt.build_place_oif(account=CANARY_NT_ACCOUNT, instrument=EXEC_INSTRUMENT_NT, action="BUY", quantity=1)
        sim_plan = plan_dvp_entry(direction="LONG", trade_id="T_SIM", stop_points=80, target_points=40)
        self.assertEqual(sim_plan["account"], EXEC_ACCOUNT)
        self.assertNotEqual(sim_plan["account"], CANARY_NT_ACCOUNT)
        fn_plan = plan_canary_bracket(direction="LONG", trade_id="T_FN", stop_points=80, target_points=40)
        self.assertEqual(fn_plan["account"], CANARY_NT_ACCOUNT)
        self.assertNotEqual(fn_plan["account"], SIM101_ACCOUNT)
        self.assertEqual(parse_oif_account(fn_plan["entry_line"]), CANARY_NT_ACCOUNT)
        self.assertNotIn("Sim101", fn_plan["entry_line"])
        self.assertNotIn(CANARY_NT_ACCOUNT, sim_plan["entry_line"])

    def test_09_wrong_instrument_blocked(self):
        _flag(True)
        pf = evaluate_preflight(passing_context(requested_exec_instrument="ES 09-26"))
        self.assertFalse(pf["ok"])
        self.assertTrue(any(e in pf["errors"] for e in ("WRONG_INSTRUMENT", "NQ_EXECUTION_BLOCKED")))

    def test_10_nq_execution_blocked(self):
        _flag(True)
        pf = evaluate_preflight(passing_context(requested_exec_instrument="NQ 09-26"))
        self.assertIn("NQ_EXECUTION_BLOCKED", pf["errors"])

    def test_11_qty_0_blocked(self):
        _flag(True)
        pf = evaluate_preflight(passing_context(requested_qty=0))
        self.assertIn("CANARY_QTY_REJECTED", pf["errors"])

    def test_12_qty_2_blocked(self):
        _flag(True)
        self.assertEqual(FAST_QTY, 2)
        pf = evaluate_preflight(passing_context(requested_qty=2))
        self.assertIn("CANARY_QTY_REJECTED", pf["errors"])
        pf3 = evaluate_preflight(passing_context(requested_qty=3))
        self.assertIn("CANARY_QTY_REJECTED", pf3["errors"])

    def test_13_qty_1_allowed_structurally(self):
        _flag(True)
        ctx = passing_context(requested_qty=1)
        pf = evaluate_preflight(ctx)
        self.assertTrue(pf["ok"], pf["errors"])
        plan = plan_canary_bracket(direction="LONG", trade_id="Q1", stop_points=80, target_points=40)
        self.assertEqual(plan["quantity"], 1)
        self.assertEqual(parse_oif_qty(plan["entry_line"]), 1)
        validate_canary_oif_line(plan["entry_line"])

    def test_14_stale_account_state_blocked(self):
        _flag(True)
        pf = evaluate_preflight(passing_context(account_age_sec=120.0))
        self.assertIn("STALE_ACCOUNT_STATE", pf["errors"])
        pf2 = evaluate_preflight(passing_context(account_age_sec=None))
        self.assertIn("STALE_ACCOUNT_STATE", pf2["errors"])

    def test_15_unknown_mll_blocked(self):
        _flag(True)
        pf = evaluate_preflight(passing_context(mll=None))
        self.assertIn("MLL_UNKNOWN", pf["errors"])

    def test_16_open_fundednext_position_blocked(self):
        _flag(True)
        pf = evaluate_preflight(passing_context(position_qty=1, position_side="LONG"))
        self.assertIn("OPEN_POSITION_BLOCKED", pf["errors"])

    def test_17_working_order_blocked(self):
        _flag(True)
        pf = evaluate_preflight(passing_context(working_orders=1))
        self.assertIn("WORKING_ORDER_BLOCKED", pf["errors"])

    def test_18_unsafe_reconciliation_blocked(self):
        _flag(True)
        pf = evaluate_preflight(passing_context(recon_status="FLAT_SAFE"))
        self.assertIn("UNSAFE_RECONCILIATION", pf["errors"])
        self.assertNotEqual("FLAT_SAFE", PROP_FLAT_SAFE)

    def test_19_stale_market_blocked(self):
        _flag(True)
        pf = evaluate_preflight(passing_context(market_stale=True, market_live=False))
        self.assertTrue("STALE_MARKET" in pf["errors"] or "MARKET_NOT_LIVE" in pf["errors"])

    def test_20_warmup_blocked(self):
        _flag(True)
        pf = evaluate_preflight(passing_context(warmup_complete=False))
        self.assertIn("WARMUP_BLOCKED", pf["errors"])

    def test_21_shadow_blocked(self):
        _flag(True)
        from prop_canary import evaluate_signal

        t0 = datetime(2026, 8, 24, 14, 30, tzinfo=timezone.utc)
        arm(passing_context(), now=t0)
        ctx = passing_context(signal=genuine_signal(source="phase53_shadow", live_bar=False, kind="SHADOW"))
        ev = evaluate_signal(ctx, require_newer_than_arm=True)
        self.assertIn("SHADOW_BLOCKED", ev["errors"])

    def test_22_historical_blocked(self):
        _flag(True)
        from prop_canary import evaluate_signal

        t0 = datetime(2026, 8, 24, 14, 30, tzinfo=timezone.utc)
        arm(passing_context(), now=t0)
        ev = evaluate_signal(
            passing_context(signal=genuine_signal(source="HISTORICAL", kind="HISTORICAL", ts=(t0 + timedelta(seconds=2)).isoformat())),
            require_newer_than_arm=True,
        )
        self.assertIn("HISTORICAL_BLOCKED", ev["errors"])

    def test_23_replay_blocked(self):
        _flag(True)
        from prop_canary import evaluate_signal

        t0 = datetime(2026, 8, 24, 14, 30, tzinfo=timezone.utc)
        arm(passing_context(), now=t0)
        ev = evaluate_signal(
            passing_context(signal=genuine_signal(note="replay_not_executable", ts=(t0 + timedelta(seconds=2)).isoformat())),
            require_newer_than_arm=True,
        )
        self.assertIn("REPLAY_BLOCKED", ev["errors"])

    def test_24_non_phase54_live_blocked(self):
        _flag(True)
        from prop_canary import evaluate_signal

        t0 = datetime(2026, 8, 24, 14, 30, tzinfo=timezone.utc)
        arm(passing_context(), now=t0)
        ev = evaluate_signal(
            passing_context(signal=genuine_signal(source="manual_inject", ts=(t0 + timedelta(seconds=2)).isoformat())),
            require_newer_than_arm=True,
        )
        self.assertIn("NON_PHASE54_LIVE_BLOCKED", ev["errors"])

    def test_25_pre_arm_signal_blocked(self):
        _flag(True)
        t0 = datetime(2026, 8, 24, 14, 30, tzinfo=timezone.utc)
        old = genuine_signal(ts=(t0 - timedelta(minutes=10)).isoformat(), signal_id="old-aug14")
        arm(passing_context(), now=t0)
        from prop_canary import evaluate_signal

        ev = evaluate_signal(passing_context(signal=old), require_newer_than_arm=True)
        self.assertIn("PRE_ARM_SIGNAL_BLOCKED", ev["errors"])
        res = submit_once(passing_context(signal=old), transmit=False)
        self.assertFalse(res.get("submitted"))

    def test_26_post_arm_genuine_reaches_dry_run_boundary(self):
        _flag(True)
        t0 = datetime(2026, 8, 24, 14, 30, tzinfo=timezone.utc)
        arm(passing_context(), now=t0)
        sig = genuine_signal(ts=(t0 + timedelta(seconds=5)).isoformat(), signal_id="live-1")
        ctx = passing_context(signal=sig)
        out = dry_run(ctx)
        self.assertEqual(out["verdict"], "PROP_CANARY_DRY_RUN_PASS", out.get("errors"))
        self.assertFalse(out["submitted"])
        self.assertFalse(out["transmitted"])
        self.assertIsNone(out["broker_ack"])
        self.assertEqual(out["payload"]["account"], CANARY_NT_ACCOUNT)
        self.assertEqual(out["payload"]["quantity"], 1)
        self.assertEqual(out["payload"]["execution_instrument"], EXEC_INSTRUMENT_DISPLAY)
        self.assertIn("MNQ SEP26", out["payload"]["entry_line"])
        self.assertNotIn("Sim101", out["payload"]["entry_line"])
        self.assertEqual(out["payload"]["protective"]["stop_qty"], 1)
        self.assertEqual(out["payload"]["protective"]["stop_account"], CANARY_NT_ACCOUNT)
        self.assertFalse(PROP_EXECUTION)

    def test_27_second_signal_blocked_after_one_shot(self):
        _flag(True)
        t0 = datetime(2026, 8, 24, 14, 30, tzinfo=timezone.utc)
        arm(passing_context(), now=t0)
        sig1 = genuine_signal(ts=(t0 + timedelta(seconds=5)).isoformat(), signal_id="live-a")
        recorded = []

        def tx(lines, *, transmit):
            recorded.append(lines)
            return {"ok": True, "submitted": True, "transmitted": True, "status": "SUBMITTED"}

        first = submit_once(passing_context(signal=sig1), transmit=True, transmitter=tx)
        self.assertTrue(first.get("submitted"))
        sig2 = genuine_signal(ts=(t0 + timedelta(seconds=30)).isoformat(), signal_id="live-b")
        second = submit_once(passing_context(signal=sig2), transmit=True, transmitter=tx)
        self.assertFalse(second.get("submitted"))
        self.assertEqual(second.get("error_code"), "ONE_SHOT_LATCH")
        self.assertEqual(len(recorded), 1)

    def test_28_rejection_disarms(self):
        _flag(True)
        t0 = datetime(2026, 8, 24, 14, 30, tzinfo=timezone.utc)
        arm(passing_context(), now=t0)
        sig = genuine_signal(ts=(t0 + timedelta(seconds=5)).isoformat())

        def tx(lines, *, transmit):
            return {"ok": False, "submitted": True, "transmitted": True, "status": "REJECTED"}

        out = submit_once(passing_context(signal=sig), transmit=True, transmitter=tx)
        self.assertEqual(out.get("error_code"), "ORDER_REJECTED")
        self.assertFalse(public_snapshot(passing_context())["armed"])
        self.assertIn(current_mode(passing_context()), {PROP_CANARY_BLOCKED, PROP_CANARY_DISARMED})

    def test_29_execution_exception_disarms(self):
        _flag(True)
        t0 = datetime(2026, 8, 24, 14, 30, tzinfo=timezone.utc)
        arm(passing_context(), now=t0)
        sig = genuine_signal(ts=(t0 + timedelta(seconds=5)).isoformat())

        def tx(lines, *, transmit):
            raise RuntimeError("broker exploded")

        out = submit_once(passing_context(signal=sig), transmit=True, transmitter=tx)
        self.assertEqual(out.get("error_code"), "EXECUTION_EXCEPTION")
        self.assertFalse(public_snapshot(passing_context())["armed"])

    def test_30_disconnect_disarms(self):
        _flag(True)
        arm(passing_context())
        obs = observe_runtime(passing_context(nt_connected=False))
        self.assertTrue(obs.get("changed"))
        self.assertFalse(public_snapshot(passing_context())["armed"])

    def test_31_stale_market_while_armed_disarms(self):
        _flag(True)
        arm(passing_context())
        obs = observe_runtime(passing_context(market_stale=True, market_live=False))
        self.assertTrue(obs.get("changed"))
        self.assertFalse(public_snapshot(passing_context())["armed"])

    def test_32_stop_rejection_becomes_critical(self):
        _flag(True)
        arm(passing_context())
        out = mark_stop_rejected()
        snap = public_snapshot(passing_context())
        self.assertTrue(snap["critical"])
        self.assertEqual(snap["state"], PROP_CANARY_BLOCKED)
        self.assertIn("STOP_REJECTED", str(out.get("reason")))

    def test_33_telegram_failure_cannot_alter_execution_state(self):
        _flag(True)
        ctx = passing_context()
        with mock.patch("aitrade_notifications.notify_safe", side_effect=RuntimeError("telegram down")):
            out = arm(ctx)
            self.assertTrue(out.get("armed"))
            self.assertEqual(current_mode(ctx), PROP_CANARY_ARMED)
            self.assertFalse(PROP_EXECUTION)
            disarm("OPERATOR")
        self.assertFalse(public_snapshot(ctx)["armed"])
        self.assertFalse(PROP_EXECUTION)

    def test_34_no_automatic_transition_to_general_prop(self):
        _flag(True)
        t0 = datetime(2026, 8, 24, 14, 30, tzinfo=timezone.utc)
        arm(passing_context(), now=t0)
        mark_round_trip_complete()
        snap = public_snapshot(passing_context())
        self.assertEqual(snap["general_prop"], "LOCKED")
        self.assertFalse(snap["PROP_EXECUTION"])
        self.assertFalse(PROP_EXECUTION)
        self.assertEqual(snap["state"], PROP_CANARY_COMPLETE)
        pf = evaluate_preflight(passing_context())
        self.assertFalse(pf["ok"])
        self.assertIn("ONE_SHOT_LATCH", pf["errors"])

    def test_wildcard_and_auto_account_fail_closed(self):
        _flag(True)
        pf = evaluate_preflight(passing_context(config_expected_account="AUTO"))
        self.assertIn("ACCOUNT_IDENTITY_AMBIGUOUS", pf["errors"])
        pf2 = evaluate_preflight(passing_context(config_expected_account="AUTO_FUNDEDNEXT"))
        self.assertIn("ACCOUNT_IDENTITY_AMBIGUOUS", pf2["errors"])

    def test_login_and_internal_id_must_match(self):
        _flag(True)
        self.assertEqual(CANARY_LOGIN, "962841277")
        self.assertEqual(CANARY_ACCOUNT_ID, 3969349)
        pf = evaluate_preflight(passing_context(platform_login="000000000"))
        self.assertIn("WRONG_ACCOUNT", pf["errors"])
        pf2 = evaluate_preflight(passing_context(fundednext_account_id=1))
        self.assertIn("WRONG_ACCOUNT", pf2["errors"])

    def test_emergency_flatten_targets_fundednext_not_sim101(self):
        recorded = []

        def tx(lines, *, transmit):
            recorded.extend(lines)
            return {"ok": True, "submitted": False, "transmitted": False}

        emergency_flatten(transmit=False, transmitter=tx)
        self.assertTrue(recorded)
        self.assertEqual(parse_oif_account(recorded[0]), CANARY_NT_ACCOUNT)
        self.assertNotIn("Sim101", recorded[0])

    def test_dry_run_without_live_market_fails_closed(self):
        _flag(True)
        out = dry_run(passing_context(phase_55b_0_pass=False, market_live=False, market_stale=True))
        self.assertEqual(out["verdict"], "PROP_CANARY_DRY_RUN_FAIL")
        self.assertFalse(out["transmitted"])

    def test_frozen_hash_unchanged(self):
        self.assertEqual(
            NQ_FROZEN_HASH,
            "935e3a616351b09dbfa8d2d0e2b5d6850be803fb385ec5e5f8e3593856a1212a",
        )
        self.assertFalse(PROP_EXECUTION)


if __name__ == "__main__":
    unittest.main()
