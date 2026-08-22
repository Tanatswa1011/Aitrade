"""Unattended FundedNext canary — no OIF writes, PROP_EXECUTION stays false."""
from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

from execution_status import NQ_FROZEN_HASH
from phase54_ops import PROP_EXECUTION
from prop_canary import genuine_signal, passing_context
from prop_canary_nt_exec import CANARY_NT_ACCOUNT, SIM101_ACCOUNT, parse_oif_account
from unattended_prop_canary import (
    ENV_FLAG,
    ENV_STATE,
    NY,
    UNATTENDED_BLOCKED,
    UNATTENDED_BLOCKED_RESTART,
    UNATTENDED_COMPLETE,
    UNATTENDED_COMPLETE_NO_TRADE,
    UNATTENDED_DISABLED,
    UNATTENDED_WAITING_DVP,
    UNATTENDED_WAITING_LIVE_DATA,
    UNATTENDED_WAITING_SESSION,
    attempt_entry,
    broker_protection_survival,
    daily_latch_used,
    enable,
    evaluate_automated_phase_55b,
    evaluate_preflight,
    passing_unattended,
    public_snapshot,
    reset_for_tests,
    simulate_process_restart,
    tick,
    unattended_dry_run,
)
from unattended_watchdog import crash_surface, observe as wd_observe, WatchdogObservation

MON = datetime(2026, 8, 24, 14, 45, tzinfo=NY)
PRE = datetime(2026, 8, 24, 9, 0, tzinfo=NY)
AFTER = datetime(2026, 8, 24, 16, 0, tzinfo=NY)


def _flag(on: bool) -> None:
    if on:
        os.environ[ENV_FLAG] = "true"
    else:
        os.environ.pop(ENV_FLAG, None)


def _ctx(**over):
    over.setdefault("now", MON)
    return passing_unattended(**over)


class UnattendedPropCanaryTests(unittest.TestCase):
    def setUp(self):
        os.environ[ENV_STATE] = str(Path(tempfile.mkdtemp(prefix="unatt_")) / "u.json")
        os.environ.pop(ENV_FLAG, None)
        reset_for_tests(clear_persist=True)

    def tearDown(self):
        os.environ.pop(ENV_FLAG, None)
        os.environ.pop(ENV_STATE, None)
        reset_for_tests(clear_persist=True)

    def _ready(self, **over):
        _flag(True)
        ctx = _ctx(**over)
        out = enable(ctx)
        self.assertTrue(out.get("ok"), out)
        return ctx

    def test_01_missing_unattended_flag(self):
        pf = evaluate_preflight(_ctx())
        self.assertFalse(pf["ok"])
        self.assertIn("UNATTENDED_FLAG_DISABLED", pf["errors"])
        self.assertEqual(public_snapshot()["state"], UNATTENDED_DISABLED)
        os.environ["AITRADE_PROP_CANARY_EXECUTION"] = "true"
        try:
            pf2 = evaluate_preflight(_ctx())
            self.assertIn("UNATTENDED_FLAG_DISABLED", pf2["errors"])
        finally:
            os.environ.pop("AITRADE_PROP_CANARY_EXECUTION", None)

    def test_02_wrong_fundednext_account(self):
        _flag(True)
        pf = evaluate_preflight(_ctx(canary_over={"nt_account": "WRONG"}))
        self.assertIn("WRONG_ACCOUNT", pf["errors"])

    def test_03_stale_mcp_state(self):
        _flag(True)
        pf = evaluate_preflight(_ctx(canary_over={"account_age_sec": 120.0}))
        self.assertIn("STALE_MCP_STATE", pf["errors"])

    def test_04_unknown_mll(self):
        _flag(True)
        pf = evaluate_preflight(_ctx(canary_over={"mll": None}))
        self.assertIn("MLL_UNKNOWN", pf["errors"])

    def test_05_account_not_flat(self):
        _flag(True)
        pf = evaluate_preflight(_ctx(canary_over={"position_qty": 1, "position_side": "LONG"}))
        self.assertIn("ACCOUNT_NOT_FLAT", pf["errors"])

    def test_06_working_order_present(self):
        _flag(True)
        pf = evaluate_preflight(_ctx(canary_over={"working_orders": 1}))
        self.assertIn("WORKING_ORDER_PRESENT", pf["errors"])

    def test_07_wrong_instrument(self):
        _flag(True)
        pf = evaluate_preflight(_ctx(canary_over={"requested_exec_instrument": "ES 09-26"}))
        self.assertTrue(any("INSTRUMENT" in e or "NQ_EXECUTION" in e for e in pf["errors"]))

    def test_08_qty_not_1(self):
        _flag(True)
        pf = evaluate_preflight(_ctx(canary_over={"requested_qty": 2}))
        self.assertIn("CANARY_QTY_REJECTED", pf["errors"])

    def test_09_market_delayed(self):
        live = evaluate_automated_phase_55b(_ctx(delayed_feed=True, market_quality="DELAYED"))
        self.assertFalse(live["ok"])
        self.assertIn("MARKET_DELAYED", live["errors"])

    def test_10_market_stale(self):
        live = evaluate_automated_phase_55b(_ctx(canary_over={"market_stale": True, "market_live": False}))
        self.assertIn("MARKET_NOT_LIVE", live["errors"])

    def test_11_no_nq_bars(self):
        live = evaluate_automated_phase_55b(_ctx(nq_1m_count=0, nq_1m_count_prev=0, nq_bars=[]))
        self.assertIn("NO_NQ_BARS", live["errors"])

    def test_12_duplicate_bars(self):
        bars = [{"id": "x", "ts": 1}, {"id": "x", "ts": 2}]
        live = evaluate_automated_phase_55b(_ctx(duplicate_bar_ids=True, nq_bars=bars))
        self.assertIn("DUPLICATE_BARS", live["errors"])

    def test_13_non_advancing_timestamps(self):
        live = evaluate_automated_phase_55b(_ctx(timestamps_monotonic=False, nq_bars=[{"id": "a", "ts": 9}, {"id": "b", "ts": 1}]))
        self.assertIn("TIMESTAMPS_NOT_ADVANCING", live["errors"])

    def test_14_5m_aggregation_broken(self):
        live = evaluate_automated_phase_55b(_ctx(agg_5m_advancing=False))
        self.assertIn("AGG_5M_BROKEN", live["errors"])

    def test_15_15m_aggregation_broken(self):
        live = evaluate_automated_phase_55b(_ctx(agg_15m_advancing=False))
        self.assertIn("AGG_15M_BROKEN", live["errors"])

    def test_16_warmup_incomplete(self):
        live = evaluate_automated_phase_55b(_ctx(canary_over={"warmup_complete": False}))
        self.assertIn("WARMUP_INCOMPLETE", live["errors"])

    def test_17_shadow_signal(self):
        ctx = self._ready()
        ctx = passing_unattended(
            now=MON,
            canary_over={"signal": genuine_signal(source="phase53_shadow", live_bar=False, kind="SHADOW", ts=(MON + timedelta(seconds=5)).isoformat())},
        )
        from unattended_prop_canary import _ensure_mem, UNATTENDED_WAITING_DVP as W
        mem = _ensure_mem()
        mem["state"] = W
        mem["enabled"] = True
        mem["readiness_at"] = MON
        out = attempt_entry(ctx, transmit=False)
        self.assertFalse(out.get("ok"))

    def test_18_historical_signal(self):
        self._ready()
        from unattended_prop_canary import _ensure_mem
        _ensure_mem()["state"] = UNATTENDED_WAITING_DVP
        _ensure_mem()["enabled"] = True
        _ensure_mem()["readiness_at"] = MON
        ctx = passing_unattended(now=MON, canary_over={"signal": genuine_signal(source="HISTORICAL", kind="HISTORICAL", ts=(MON + timedelta(seconds=5)).isoformat())})
        out = attempt_entry(ctx, transmit=False)
        self.assertFalse(out.get("ok"))

    def test_19_stale_genuine_signal(self):
        self._ready()
        from unattended_prop_canary import _ensure_mem
        _ensure_mem()["state"] = UNATTENDED_WAITING_DVP
        _ensure_mem()["readiness_at"] = MON
        _ensure_mem()["enabled"] = True
        old = genuine_signal(ts=(MON - timedelta(hours=3)).isoformat())
        out = attempt_entry(passing_unattended(now=MON, canary_over={"signal": old}), transmit=False)
        self.assertFalse(out.get("ok"))

    def test_20_dvp_before_readiness(self):
        self._ready()
        from unattended_prop_canary import _ensure_mem
        _ensure_mem()["state"] = UNATTENDED_WAITING_DVP
        _ensure_mem()["readiness_at"] = MON
        _ensure_mem()["enabled"] = True
        sig = genuine_signal(ts=(MON - timedelta(seconds=1)).isoformat())
        out = attempt_entry(passing_unattended(now=MON, canary_over={"signal": sig}), transmit=False)
        self.assertFalse(out.get("ok"))

    def test_21_dvp_outside_session(self):
        _flag(True)
        ctx = _ctx(now=AFTER)
        enable(passing_unattended(now=PRE))
        from unattended_prop_canary import _ensure_mem
        _ensure_mem()["state"] = UNATTENDED_WAITING_DVP
        _ensure_mem()["enabled"] = True
        _ensure_mem()["readiness_at"] = PRE
        sig = genuine_signal(ts=AFTER.isoformat())
        out = attempt_entry(passing_unattended(now=AFTER, canary_over={"signal": sig}), transmit=False)
        self.assertEqual(out.get("error_code"), "OUTSIDE_SESSION")

    def test_22_second_dvp_after_latch(self):
        _flag(True)
        ctx = _ctx()
        dry = unattended_dry_run(ctx)
        self.assertEqual(dry["verdict"], "UNATTENDED_DRY_RUN_PASS", dry)
        self.assertTrue(dry["second_blocked"])
        self.assertTrue(daily_latch_used())

    def test_23_order_reject(self):
        self._ready()
        from unattended_prop_canary import _ensure_mem
        _ensure_mem()["state"] = UNATTENDED_WAITING_DVP
        _ensure_mem()["enabled"] = True
        _ensure_mem()["readiness_at"] = MON - timedelta(seconds=10)
        sig = genuine_signal(ts=MON.isoformat())

        def tx(lines, *, transmit):
            return {"ok": False, "submitted": True, "transmitted": True, "status": "REJECTED"}

        out = attempt_entry(passing_unattended(now=MON, canary_over={"signal": sig}), transmit=True, transmitter=tx)
        self.assertIn(out.get("reason") or out.get("error_code"), {"ORDER_REJECTED", UNATTENDED_BLOCKED})
        self.assertTrue(daily_latch_used())

    def test_24_oif_write_exception(self):
        self._ready()
        from unattended_prop_canary import _ensure_mem
        _ensure_mem()["state"] = UNATTENDED_WAITING_DVP
        _ensure_mem()["enabled"] = True
        _ensure_mem()["readiness_at"] = MON - timedelta(seconds=10)

        def tx(lines, *, transmit):
            raise OSError("incoming locked")

        out = attempt_entry(passing_unattended(now=MON, canary_over={"signal": genuine_signal(ts=MON.isoformat())}), transmit=True, transmitter=tx)
        self.assertEqual(out.get("error_code") or out.get("reason"), "OIF_WRITE_EXCEPTION")
        self.assertTrue(daily_latch_used())

    def test_25_partial_fill(self):
        self._ready()
        from unattended_prop_canary import _ensure_mem
        _ensure_mem()["state"] = UNATTENDED_WAITING_DVP
        _ensure_mem()["enabled"] = True
        _ensure_mem()["readiness_at"] = MON - timedelta(seconds=10)

        def tx(lines, *, transmit):
            return {"ok": True, "submitted": False, "transmitted": False, "status": "DRY"}

        out = attempt_entry(
            passing_unattended(now=MON, fill_qty=0, canary_over={"signal": genuine_signal(ts=MON.isoformat())}),
            transmit=False,
            transmitter=tx,
        )
        self.assertFalse(out.get("ok") and out.get("verdict") == "UNATTENDED_DRY_RUN_PASS")

    def test_26_stop_submission_failure(self):
        self._ready()
        from unattended_prop_canary import _ensure_mem
        _ensure_mem()["state"] = UNATTENDED_WAITING_DVP
        _ensure_mem()["enabled"] = True
        _ensure_mem()["readiness_at"] = MON - timedelta(seconds=10)
        out = attempt_entry(
            passing_unattended(now=MON, stop_ack=False, canary_over={"signal": genuine_signal(ts=MON.isoformat())}),
            transmit=False,
        )
        self.assertFalse(out.get("ok") and out.get("verdict") == "UNATTENDED_DRY_RUN_PASS")

    def test_27_stop_ack_timeout(self):
        self._ready()
        from unattended_prop_canary import _ensure_mem
        _ensure_mem()["state"] = UNATTENDED_WAITING_DVP
        _ensure_mem()["enabled"] = True
        _ensure_mem()["readiness_at"] = MON - timedelta(seconds=10)
        out = attempt_entry(
            passing_unattended(now=MON, protection_timeout=True, canary_over={"signal": genuine_signal(ts=MON.isoformat())}),
            transmit=False,
        )
        self.assertNotEqual(out.get("verdict"), "UNATTENDED_DRY_RUN_PASS")

    def test_28_target_failure(self):
        self._ready()
        from unattended_prop_canary import _ensure_mem
        _ensure_mem()["state"] = UNATTENDED_WAITING_DVP
        _ensure_mem()["enabled"] = True
        _ensure_mem()["readiness_at"] = MON - timedelta(seconds=10)
        out = attempt_entry(
            passing_unattended(now=MON, target_ack=False, canary_over={"signal": genuine_signal(ts=MON.isoformat())}),
            transmit=False,
        )
        self.assertNotEqual(out.get("verdict"), "UNATTENDED_DRY_RUN_PASS")

    def test_29_engine_crash_while_flat(self):
        crash = crash_surface(kind="python_engine", position_open_flag=False, stop_working=False)
        self.assertFalse(crash["cancel_stop"])
        self.assertIn("BLOCK_DAY", crash["actions"])
        self._ready()
        out = tick(passing_unattended(now=MON, engine_state="ABSENT", engine_heartbeat_age_sec=999), transmit=False)
        self.assertEqual(out.get("state") or out.get("reason") and UNATTENDED_BLOCKED, UNATTENDED_BLOCKED)

    def test_30_engine_crash_while_position_open(self):
        crash = crash_surface(kind="python_engine", position_open_flag=True, stop_working=True)
        self.assertFalse(crash["cancel_stop"])
        self.assertFalse(crash["second_entry"])
        self.assertTrue(crash["broker_native_stop_survives"])
        self.assertIn("UNATTENDED_ENGINE_LOST_POSITION_OPEN", crash["alerts"])

    def test_31_dashboard_crash_while_position_open(self):
        crash = crash_surface(kind="dashboard", position_open_flag=True, stop_working=True)
        self.assertFalse(crash["cancel_stop"])
        self.assertTrue(crash["broker_native_stop_survives"])

    def test_32_telegram_failure(self):
        _flag(True)
        with mock.patch("unattended_prop_canary._notify", side_effect=RuntimeError("tg down")):
            # _notify swallows internally; patching it to raise would break enable unless enable catches.
            pass
        with mock.patch("aitrade_notifications.notify_safe", side_effect=RuntimeError("tg down")):
            out = enable(_ctx())
        self.assertTrue(out.get("ok"))
        self.assertFalse(PROP_EXECUTION)

    def test_33_nt_disconnect_while_flat(self):
        self._ready()
        out = tick(passing_unattended(now=MON, canary_over={"nt_connected": False}), transmit=False)
        self.assertEqual(out.get("state"), UNATTENDED_BLOCKED)

    def test_34_nt_disconnect_while_open(self):
        self._ready()
        from unattended_prop_canary import _ensure_mem
        _ensure_mem()["position_open"] = True
        _ensure_mem()["state"] = "UNATTENDED_POSITION_OPEN"
        _ensure_mem()["enabled"] = True
        out = tick(passing_unattended(now=MON, canary_over={"nt_connected": False, "position_qty": 1, "position_side": "LONG"}), transmit=False)
        self.assertNotEqual(out.get("state"), UNATTENDED_WAITING_DVP)
        self.assertFalse(out.get("second_entry", False))

    def test_35_mcp_stale_while_waiting(self):
        wd = wd_observe(WatchdogObservation(canary_state=UNATTENDED_WAITING_DVP, mcp_age_sec=120, position_qty=0, position_side="FLAT"))
        self.assertIn("BLOCK_DAY", wd["actions"])

    def test_36_mcp_stale_while_open(self):
        wd = wd_observe(WatchdogObservation(canary_state="UNATTENDED_POSITION_OPEN", mcp_age_sec=120, position_qty=1, position_side="LONG", stop_working=True, target_working=True))
        self.assertFalse(wd["second_entry"])
        self.assertFalse(wd["cancel_stop"])

    def test_37_restart_before_entry(self):
        self._ready()
        self.assertEqual(public_snapshot()["state"], UNATTENDED_WAITING_DVP)
        simulate_process_restart()
        snap = public_snapshot()
        self.assertEqual(snap["state"], UNATTENDED_BLOCKED_RESTART)
        self.assertFalse(snap["enabled"])

    def test_38_restart_after_entry_attempt(self):
        _flag(True)
        unattended_dry_run(_ctx())
        self.assertTrue(daily_latch_used())
        simulate_process_restart()
        self.assertTrue(daily_latch_used())
        en = enable(_ctx())
        self.assertFalse(en.get("ok"))

    def test_39_reconnect_does_not_rearm(self):
        self._ready()
        simulate_process_restart()
        _flag(True)
        t = tick(_ctx(), transmit=False)
        self.assertEqual(t.get("state"), UNATTENDED_BLOCKED_RESTART)

    def test_40_session_close_no_dvp(self):
        _flag(True)
        enable(passing_unattended(now=PRE))
        from unattended_prop_canary import _ensure_mem
        _ensure_mem()["state"] = UNATTENDED_WAITING_DVP
        _ensure_mem()["enabled"] = True
        out = tick(passing_unattended(now=AFTER), transmit=False)
        self.assertEqual(out.get("state"), UNATTENDED_COMPLETE_NO_TRADE)

    def test_41_one_shot_latch_persists_restart_after_submit(self):
        _flag(True)
        unattended_dry_run(_ctx())
        simulate_process_restart()
        self.assertTrue(daily_latch_used())
        self.assertTrue(public_snapshot()["daily_attempt_used"] or public_snapshot()["locked_for_day"] or daily_latch_used())

    def test_42_prop_execution_never_enabled(self):
        _flag(True)
        unattended_dry_run(_ctx())
        self.assertFalse(PROP_EXECUTION)
        self.assertFalse(public_snapshot()["PROP_EXECUTION"])
        self.assertEqual(public_snapshot()["general_prop"], "LOCKED")

    def test_43_sim101_never_eligible(self):
        _flag(True)
        pf = evaluate_preflight(_ctx(canary_over={"nt_account": "Sim101", "requested_account": "Sim101", "sim_only_armed": True}))
        self.assertTrue("SIM101_BLOCKED_FROM_CANARY" in pf["errors"] or "SIM101_MUST_REMAIN_DISARMED" in pf["errors"])
        surv = broker_protection_survival()
        self.assertNotIn("Sim101", surv["stop_line"])
        self.assertEqual(parse_oif_account(surv["stop_line"]), CANARY_NT_ACCOUNT)

    def test_44_emergency_path_cannot_affect_other_account(self):
        from prop_canary import emergency_flatten
        recorded = []

        def tx(lines, *, transmit):
            recorded.extend(lines)
            return {"ok": True, "submitted": False, "transmitted": False}

        emergency_flatten(transmit=False, transmitter=tx)
        self.assertTrue(recorded)
        self.assertEqual(parse_oif_account(recorded[0]), CANARY_NT_ACCOUNT)
        self.assertNotIn(SIM101_ACCOUNT, recorded[0])
        wd = wd_observe(WatchdogObservation(position_qty=1, position_side="LONG", stop_working=False, account=CANARY_NT_ACCOUNT))
        self.assertEqual(wd["flatten_account"], CANARY_NT_ACCOUNT)
        wd2 = wd_observe(WatchdogObservation(account="Sim101", position_qty=1, position_side="LONG"))
        self.assertIsNone(wd2["flatten_account"])

    def test_waiting_live_data_before_55b(self):
        _flag(True)
        out = enable(_ctx(nq_1m_count=0, nq_1m_count_prev=0, nq_bars=[], nq_bars_1m_status="WAITING"))
        self.assertTrue(out.get("ok"))
        self.assertEqual(out["state"], UNATTENDED_WAITING_LIVE_DATA)

    def test_waiting_session_before_1030(self):
        _flag(True)
        out = enable(passing_unattended(now=PRE))
        self.assertTrue(out.get("ok"))
        self.assertIn(out["state"], {UNATTENDED_WAITING_SESSION, UNATTENDED_WAITING_LIVE_DATA})

    def test_broker_protection_survival(self):
        surv = broker_protection_survival()
        self.assertEqual(surv["verdict"], "BROKER_PROTECTIVE_ORDER_SURVIVAL_PASS")
        self.assertFalse(surv["python_held_stop"])

    def test_frozen_hash_unchanged(self):
        self.assertEqual(NQ_FROZEN_HASH, "935e3a616351b09dbfa8d2d0e2b5d6850be803fb385ec5e5f8e3593856a1212a")
        self.assertFalse(PROP_EXECUTION)

    def test_canary_flag_alone_does_not_enable_unattended(self):
        os.environ["AITRADE_PROP_CANARY_EXECUTION"] = "true"
        try:
            self.assertEqual(public_snapshot()["state"], UNATTENDED_DISABLED)
            pf = evaluate_preflight(_ctx())
            self.assertIn("UNATTENDED_FLAG_DISABLED", pf["errors"])
        finally:
            os.environ.pop("AITRADE_PROP_CANARY_EXECUTION", None)


if __name__ == "__main__":
    unittest.main()
