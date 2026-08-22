"""Phase 55A — SIM_ONLY execution bridge. Mocks never write OIF. PROP_EXECUTION stays false."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from execution_instrument import (
    EXEC_INSTRUMENT_DISPLAY,
    EXEC_INSTRUMENT_NT,
    MNQ_SEP26,
    parse_execution_instrument,
)
from execution_status import NQ_FROZEN_HASH, sim_only_execution_armed
from phase55_execution_bridge import (
    FN_EVAL_ACCOUNT,
    PHASE_55A_MAX_QTY,
    RECOVERY_ACTIVE,
    RECOVERY_CORRUPT,
    RECOVERY_FLAT_SAFE,
    RECOVERY_ORPHAN_ORDER,
    RECOVERY_ORPHAN_POSITION,
    RECOVERY_UNKNOWN,
    RECOVERY_UNPROTECTED,
    NinjaTraderExecutionBridge,
)

FN = "FNFTCHTANATSWAPHILMU92044"


def _flat(**extra):
    row = {
        "flat": True,
        "market_position": "Flat",
        "quantity": 0,
        "account": "Sim101",
        "instrument": EXEC_INSTRUMENT_NT,
    }
    row.update(extra)
    return row


def _long(**extra):
    row = {
        "flat": False,
        "market_position": "Long",
        "quantity": 1,
        "account": "Sim101",
        "instrument": EXEC_INSTRUMENT_NT,
        "average_price": 24800.0,
    }
    row.update(extra)
    return row


def _no_orders(*_a, **_k):
    return {"orphan_count": 0, "oco_live_count": 0, "orphan_order_ids": []}


def _live_oco(*_a, **_k):
    return {"orphan_count": 2, "oco_live_count": 2, "orphan_order_ids": ["s", "t"]}


def _intent(**over):
    base = {
        "direction": "LONG",
        "account": "Sim101",
        "instrument": "MNQ 09-26",
        "quantity": 1,
        "strategy_id": "NQ_DRIFT_VWAP_PULLBACK",
        "strategy_hash": NQ_FROZEN_HASH,
        "policy_verdict": "ALLOW",
        "policy_code": "ALLOW",
        "calendar_status": "OK",
        "data_age_sec": 1.0,
        "trigger_key": "uniq-trigger-1",
        "trade_id": "AITRADE_DVP_uniq1",
        "nt_connected": True,
        "mode": "SIM_ONLY",
        "news_blocked": False,
        "prop_blocked": False,
        "duplicate": False,
    }
    base.update(over)
    return base


def _bridge(tmp: str, pos=None, orphans=None, **kwargs) -> NinjaTraderExecutionBridge:
    position = pos if pos is not None else _flat()

    def parse_pos(**_k):
        return dict(position) if isinstance(position, dict) else position()

    return NinjaTraderExecutionBridge(
        state_path=Path(tmp) / "phase55.json",
        parse_position=parse_pos,
        detect_orphans=orphans or _no_orders,
        **kwargs,
    )


class InstrumentTests(unittest.TestCase):
    def test_canonical_maps_to_ninjatrader(self):
        inst = parse_execution_instrument("MNQ 09-26")
        self.assertEqual(inst, MNQ_SEP26)
        self.assertEqual(inst.ninjatrader_oif(), "MNQ SEP26")
        self.assertEqual(inst.display(), "MNQ 09-26")
        self.assertEqual(EXEC_INSTRUMENT_NT, "MNQ SEP26")
        self.assertEqual(EXEC_INSTRUMENT_DISPLAY, "MNQ 09-26")

    def test_nt_form_accepted(self):
        self.assertEqual(parse_execution_instrument("MNQ SEP26").ninjatrader_oif(), "MNQ SEP26")

    def test_wrong_contract_rejected(self):
        with self.assertRaises(Exception):
            parse_execution_instrument("MNQ DEC26")
        with self.assertRaises(PermissionError):
            parse_execution_instrument("NQ SEP26")


class GateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def _submit_blocked(self, intent, **bridge_kw):
        drop = mock.Mock()
        bracket = mock.Mock()
        b = _bridge(self.tmp.name, submit_bracket=bracket, **bridge_kw)
        with mock.patch("nt_ati.drop_oif", drop), mock.patch("nq_dvp_nt_exec.nt.drop_oif", drop):
            out = b.submit(intent, transmit=True)
        drop.assert_not_called()
        bracket.assert_not_called()
        self.assertFalse(out["submitted"])
        return out

    def test_rejected_policy_cannot_drop_oif(self):
        out = self._submit_blocked(_intent(policy_verdict="BLOCK", policy_code="NO"))
        self.assertEqual(out["error_code"], "POLICY_NOT_APPROVED")

    def test_stale_cannot_drop_oif(self):
        out = self._submit_blocked(_intent(data_age_sec=9999))
        self.assertEqual(out["error_code"], "STALE_DATA_BLOCK")

    def test_duplicate_cannot_drop_oif(self):
        out = self._submit_blocked(_intent(duplicate=True))
        self.assertEqual(out["error_code"], "DUPLICATE_ORDER_DETECTED")

    def test_news_blocked_cannot_drop_oif(self):
        out = self._submit_blocked(_intent(news_blocked=True, policy_code="NEWS_BLACKOUT_VIOLATION_RISK"))
        self.assertIn(out["error_code"], ("NEWS_BLACKOUT_VIOLATION_RISK", "POLICY_NOT_APPROVED"))

    def test_news_calendar_lock_cannot_drop_oif(self):
        out = self._submit_blocked(_intent(calendar_status="LOCK"))
        self.assertEqual(out["error_code"], "NEWS_BLACKOUT_VIOLATION_RISK")

    def test_prop_rule_blocked_cannot_drop_oif(self):
        out = self._submit_blocked(_intent(prop_blocked=True, policy_code="PROP_RULE_DATA_MISSING"))
        self.assertTrue(str(out["error_code"]).startswith("PROP") or "PROP" in str(out["error_code"]))

    def test_unsafe_position_cannot_drop_oif(self):
        out = self._submit_blocked(_intent(), pos=_long())
        self.assertIn(
            out["error_code"],
            ("POSITION_STATE_UNSAFE", "ORPHAN_POSITION", "RECOVERY_BLOCKS_ENTRIES"),
        )

    def test_disconnected_cannot_drop_oif(self):
        out = self._submit_blocked(_intent(nt_connected=False))
        self.assertEqual(out["error_code"], "CONNECTION_STATE_BLOCK")

    def test_qty_over_one_rejected(self):
        out = self._submit_blocked(_intent(quantity=2))
        self.assertEqual(out["error_code"], "PHASE_55A_QTY_CAP")
        self.assertEqual(PHASE_55A_MAX_QTY, 1)

    def test_qty_one_gate_passes(self):
        b = _bridge(self.tmp.name, submit_bracket=mock.Mock(return_value={"status": "DRY_RUN_PLAN", "submitted": False}))
        pre = b.preflight(_intent(quantity=1, require_armed=False))
        self.assertEqual(pre["gates"].get("quantity"), "PASS")

    def test_fundednext_account_rejected(self):
        out = self._submit_blocked(_intent(account=FN))
        self.assertIn("LIVE_ACCOUNT_BLOCKED", out["error_code"])
        self.assertIn(FN, out["error_code"])

    def test_arbitrary_account_rejected(self):
        out = self._submit_blocked(_intent(account="Live1"))
        self.assertIn("LIVE_ACCOUNT_BLOCKED", out["error_code"])

    def test_sim101_account_gate_passes(self):
        b = _bridge(self.tmp.name, submit_bracket=mock.Mock(return_value={"status": "DRY_RUN_PLAN", "submitted": False}))
        pre = b.preflight(_intent(account="Sim101", require_armed=False))
        self.assertEqual(pre["gates"].get("account"), "PASS")

    def test_wrong_instrument_rejected(self):
        out = self._submit_blocked(_intent(instrument="MNQ DEC26"))
        self.assertIn("REFUSED_UNSUPPORTED_INSTRUMENT", out["error_code"])

    def test_prop_evaluation_mode_blocked(self):
        out = self._submit_blocked(_intent(mode="PROP_EVALUATION"))
        self.assertIn("EXECUTION_MODE_BLOCKED", out["error_code"])

    def test_unarmed_cannot_drop_oif(self):
        self.assertFalse(sim_only_execution_armed())
        out = self._submit_blocked(_intent())
        self.assertEqual(out["error_code"], "SIM_ONLY_NOT_ARMED")


class ApprovedSubmitTests(unittest.TestCase):
    def test_approved_sim_only_calls_existing_bracket(self):
        with tempfile.TemporaryDirectory() as tmp:
            bracket = mock.Mock(
                return_value={
                    "ok": True,
                    "submitted": True,
                    "status": "BRACKET_ARMED",
                    "entry_fill": 24800.0,
                    "stop_price": 24720.0,
                    "target_price": 24840.0,
                    "entry_order_id": "AITRADE_DVP_uniq1_ENTRY",
                    "stop_order_id": "AITRADE_DVP_uniq1_STOP",
                    "target_order_id": "AITRADE_DVP_uniq1_TGT",
                    "oco_id": "AITRADE_DVP_OCO_uniq1",
                    "nt_entry_order_id": "a" * 32,
                }
            )
            drop = mock.Mock()
            b = _bridge(tmp, submit_bracket=bracket)
            with mock.patch.dict(os.environ, {"AITRADE_SIM_ONLY_EXECUTION": "1"}):
                with mock.patch("nt_ati.drop_oif", drop), mock.patch("nq_dvp_nt_exec.nt.drop_oif", drop):
                    out = b.submit(_intent(), transmit=True)
            self.assertTrue(out["ok"])
            self.assertTrue(out["submitted"])
            self.assertEqual(out["status"], "BRACKET_ARMED")
            bracket.assert_called_once()
            kwargs = bracket.call_args.kwargs
            self.assertTrue(kwargs["submit"])
            self.assertEqual(kwargs["direction"], "LONG")
            self.assertEqual(kwargs["stop_points"], 80.0)
            self.assertEqual(kwargs["target_points"], 40.0)
            drop.assert_not_called()  # transport is inside mocked submit_dvp_bracket
            self.assertFalse(out["PROP_EXECUTION"])

    def test_approved_reaches_drop_oif_only_when_all_gates_pass(self):
        with tempfile.TemporaryDirectory() as tmp:
            drop = mock.Mock(return_value={"ok": True, "path": str(Path(tmp) / "oif.txt")})
            drop_lines = mock.Mock(return_value={"ok": True, "path": str(Path(tmp) / "oif2.txt")})
            b = _bridge(tmp)
            with mock.patch.dict(os.environ, {"AITRADE_SIM_ONLY_EXECUTION": "1"}):
                with mock.patch("nq_dvp_nt_exec.nt.parse_mnq_sim_position", return_value=_flat()):
                    with mock.patch("nq_dvp_nt_exec.nt.drop_oif", drop):
                        with mock.patch("nq_dvp_nt_exec.nt.drop_oif_lines", drop_lines):
                            with mock.patch("nq_dvp_nt_exec.nt.wait_for_oif_consumed", return_value={"consumed": True}):
                                with mock.patch(
                                    "nq_dvp_nt_exec.nt.wait_for_entry_fill",
                                    return_value={"ok": True, "fill_price": 24800.0, "nt_order_id": "b" * 32},
                                ):
                                    with mock.patch("nq_dvp_nt_exec.nt.detect_orphan_aitrade_orders", _live_oco):
                                        with mock.patch("nq_dvp_nt_exec.time.sleep"):
                                            out = b.submit(_intent(), transmit=True)
            self.assertTrue(drop.called)
            self.assertTrue(out.get("submitted"))
            self.assertEqual(out.get("execution", {}).get("account"), "Sim101")
            self.assertFalse(out["PROP_EXECUTION"])

    def test_second_submit_same_trigger_is_duplicate(self):
        with tempfile.TemporaryDirectory() as tmp:
            bracket = mock.Mock(
                return_value={"ok": True, "submitted": True, "status": "BRACKET_ARMED", "entry_fill": 1}
            )
            b = _bridge(tmp, submit_bracket=bracket)
            drop = mock.Mock()
            with mock.patch.dict(os.environ, {"AITRADE_SIM_ONLY_EXECUTION": "1"}):
                with mock.patch("nt_ati.drop_oif", drop), mock.patch("nq_dvp_nt_exec.nt.drop_oif", drop):
                    first = b.submit(_intent(), transmit=True)
                    second = b.submit(_intent(), transmit=True)
            self.assertTrue(first["submitted"])
            self.assertEqual(second["error_code"], "DUPLICATE_ORDER_DETECTED")
            self.assertEqual(bracket.call_count, 1)
            drop.assert_not_called()


class RecoveryTests(unittest.TestCase):
    def test_flat_startup(self):
        with tempfile.TemporaryDirectory() as tmp:
            b = _bridge(tmp)
            rec = b.reconcile()
            self.assertEqual(rec["status"], RECOVERY_FLAT_SAFE)
            self.assertFalse(rec["entries_blocked"])
            self.assertFalse(rec["halt"])

    def test_active_trade_recovered(self):
        with tempfile.TemporaryDirectory() as tmp:
            b = _bridge(tmp, pos=_long(), orphans=_live_oco)
            st = b._load()
            st["open_trade"] = {
                "trade_id": "AITRADE_DVP_x",
                "oco_id": "AITRADE_DVP_OCO_x",
                "stop_order_id": "s",
                "target_order_id": "t",
            }
            b._save(st)
            rec = b.reconcile()
            self.assertEqual(rec["status"], RECOVERY_ACTIVE)
            self.assertTrue(rec["entries_blocked"])

    def test_orphan_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            b = _bridge(tmp, pos=_flat(), orphans=_live_oco)
            rec = b.reconcile()
            self.assertEqual(rec["status"], RECOVERY_ORPHAN_ORDER)
            self.assertTrue(rec["halt"])

    def test_orphan_position(self):
        with tempfile.TemporaryDirectory() as tmp:
            b = _bridge(tmp, pos=_long(), orphans=_no_orders)
            rec = b.reconcile()
            self.assertEqual(rec["status"], RECOVERY_ORPHAN_POSITION)
            self.assertTrue(rec["halt"])

    def test_missing_protective_stop(self):
        with tempfile.TemporaryDirectory() as tmp:
            b = _bridge(tmp, pos=_long(), orphans=_no_orders)
            st = b._load()
            st["open_trade"] = {"trade_id": "AITRADE_DVP_x", "oco_id": "oco"}
            b._save(st)
            rec = b.reconcile()
            self.assertEqual(rec["status"], RECOVERY_UNPROTECTED)
            self.assertTrue(rec["halt"])

    def test_corrupt_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "phase55.json"
            path.write_text("{not-json", encoding="utf-8")
            b = NinjaTraderExecutionBridge(
                state_path=path,
                parse_position=lambda **k: _flat(),
                detect_orphans=_no_orders,
                submit_bracket=mock.Mock(),
            )
            rec = b.reconcile()
            self.assertEqual(rec["status"], RECOVERY_CORRUPT)
            self.assertTrue(rec["halt"])
            drop = mock.Mock()
            with mock.patch("nt_ati.drop_oif", drop):
                out = b.submit(_intent(), transmit=True)
            drop.assert_not_called()
            self.assertFalse(out["submitted"])

    def test_disconnect_blocks_then_reconnect_reconciles(self):
        with tempfile.TemporaryDirectory() as tmp:
            b = _bridge(tmp)
            b.reconcile()
            disc = b.notify_disconnect()
            self.assertTrue(disc["entries_blocked"])
            drop = mock.Mock()
            with mock.patch.dict(os.environ, {"AITRADE_SIM_ONLY_EXECUTION": "1"}):
                with mock.patch("nt_ati.drop_oif", drop), mock.patch("nq_dvp_nt_exec.nt.drop_oif", drop):
                    blocked = b.submit(_intent(), transmit=True)
            drop.assert_not_called()
            self.assertEqual(blocked["error_code"], "CONNECTION_STATE_BLOCK")
            rec = b.notify_reconnect()
            self.assertEqual(rec["status"], RECOVERY_FLAT_SAFE)
            self.assertFalse(rec.get("halt"))


class FlattenTests(unittest.TestCase):
    def test_sim101_flatten_transmits_when_armed(self):
        with tempfile.TemporaryDirectory() as tmp:
            flatten = mock.Mock(
                return_value={"ok": True, "submitted": True, "status": "FLATTENED", "wait": {"consumed": True}}
            )
            b = _bridge(tmp, flatten_sim_fn=flatten, flatten_owned=flatten)
            with mock.patch.dict(os.environ, {"AITRADE_SIM_ONLY_EXECUTION": "1"}):
                out = b.emergency_flatten(account="Sim101", transmit=True)
            flatten.assert_called()
            self.assertTrue(out["submitted"])
            self.assertIn(out["flatten"], ("FLATTENED", "TRANSMITTED_UNCONFIRMED"))
            self.assertFalse(out["PROP_EXECUTION"])

    def test_fundednext_flatten_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            flatten = mock.Mock()
            drop = mock.Mock()
            b = _bridge(tmp, flatten_sim_fn=flatten, flatten_owned=flatten)
            with mock.patch.dict(os.environ, {"AITRADE_SIM_ONLY_EXECUTION": "1"}):
                with mock.patch("nt_ati.drop_oif", drop), mock.patch("nt_ati.drop_oif_lines", drop):
                    out = b.emergency_flatten(account=FN, transmit=True)
            flatten.assert_not_called()
            drop.assert_not_called()
            self.assertEqual(out["flatten"], "NOT_TRANSMITTED")
            self.assertIn("LIVE_ACCOUNT_BLOCKED", out["error_code"])

    def test_unarmed_flatten_not_transmitted(self):
        with tempfile.TemporaryDirectory() as tmp:
            flatten = mock.Mock()
            b = _bridge(tmp, flatten_sim_fn=flatten, flatten_owned=flatten)
            out = b.emergency_flatten(account="Sim101", transmit=True)
            flatten.assert_not_called()
            self.assertEqual(out["flatten"], "REQUESTED_NOT_TRANSMITTED")


class Phase54IntegrationTests(unittest.TestCase):
    def test_try_execute_unarmed_does_not_submit(self):
        from phase54_ops import try_execute_approved_sim_only

        drop = mock.Mock()
        with mock.patch("nt_ati.drop_oif", drop), mock.patch("nq_dvp_nt_exec.nt.drop_oif", drop):
            out = try_execute_approved_sim_only()
        self.assertEqual(out["error_code"], "SIM_ONLY_NOT_ARMED")
        drop.assert_not_called()
        self.assertFalse(out["submitted"])

    def test_try_execute_rejects_shadow_even_when_armed(self):
        from phase54_ops import try_execute_approved_sim_only

        drop = mock.Mock()
        shadow = {
            "direction": "SHORT",
            "intended_entry": 24800.0,
            "trading_date": "2026-08-14",
            "source": "phase53_shadow",
            "accepted": True,
        }
        with mock.patch.dict(os.environ, {"AITRADE_SIM_ONLY_EXECUTION": "1"}):
            with mock.patch("phase54_ops.EngineSupervisor._load", return_value={"engine": "RUNNING", "entries_paused": False}):
                with mock.patch("phase54_ops.last_operator_signal", return_value=shadow):
                    with mock.patch("nt_ati.drop_oif", drop), mock.patch("nq_dvp_nt_exec.nt.drop_oif", drop):
                        out = try_execute_approved_sim_only()
        self.assertEqual(out["error_code"], "LIVE_DVP_REQUIRED")
        drop.assert_not_called()
        self.assertFalse(out["submitted"])

    def test_emergency_flatten_default_still_not_transmitted(self):
        from phase54_ops import EngineSupervisor

        with mock.patch("nt_ati.drop_oif") as drop, mock.patch("nt_ati.drop_oif_lines") as drop_lines:
            out = EngineSupervisor.emergency_flatten_stop()
        drop.assert_not_called()
        drop_lines.assert_not_called()
        self.assertEqual(out["flatten"], "REQUESTED_NOT_TRANSMITTED")

    def test_prop_execution_remains_false(self):
        from phase54_ops import PROP_EXECUTION, prop_execution_allowed

        self.assertFalse(PROP_EXECUTION)
        self.assertFalse(prop_execution_allowed())
        self.assertEqual(FN_EVAL_ACCOUNT, FN)


if __name__ == "__main__":
    unittest.main()
