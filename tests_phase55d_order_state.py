"""Phase 55D authoritative NT working-order fail-closed tests. No OIF writes."""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from order_state import evaluate_order_snapshot
from prop_canary import context_from_ops_snapshot, evaluate_account_gates

ACCOUNT = "FNFTCHTANATSWAPHILMU92044"
CONTRACT = "MNQ 09-26"
NOW = datetime(2026, 8, 23, 16, 0, tzinfo=timezone.utc)


def snap(**over):
    out = {"timestamp": NOW.isoformat(), "source": "NINJATRADER_ACCOUNT_ORDERS", "connection_status": "CONNECTED", "account_id": ACCOUNT, "available": True, "collection_available": True, "total_observed": 0, "active_count": 0, "pending_count": 0, "partial_active_count": 0, "orphan_candidate_count": 0, "unknown_count": 0, "active_orders": []}
    out.update(over)
    return out


def order(state="Working", **over):
    out = {"correlation_id": "NT-1", "account_id": ACCOUNT, "instrument": CONTRACT, "action": "Buy", "order_type": "Limit", "quantity": 1, "filled_quantity": 0, "remaining_quantity": 1, "state": state, "oco_id": None, "recognized": False, "protective": False, "potential_orphan": True}
    out.update(over)
    return out


def active(o, **over):
    pending = o["state"].upper() not in {"WORKING", "PARTFILLED"}
    return snap(total_observed=1, active_count=1, pending_count=int(pending), partial_active_count=int(o["state"].upper() == "PARTFILLED"), orphan_candidate_count=int(o.get("potential_orphan") is not False), active_orders=[o], **over)


def gate(doc, **over):
    args = {"expected_account": ACCOUNT, "expected_contract": CONTRACT, "position_flat": True, "local_oif_count": 0, "now": NOW}
    args.update(over)
    return evaluate_order_snapshot(doc, **args)


class OrderStateGateTests(unittest.TestCase):
    def test_01_zero_orders_passes(self): self.assertTrue(gate(snap())["ok"])
    def test_02_missing_blocks(self): self.assertIn("ORDER_STATE_MISSING", gate(None)["errors"])
    def test_03_stale_blocks(self): self.assertIn("ORDER_STATE_STALE", gate(snap(timestamp=(NOW-timedelta(seconds=6)).isoformat()))["errors"])
    def test_04_account_mismatch_blocks(self): self.assertIn("ORDER_ACCOUNT_MISMATCH", gate(snap(account_id="WRONG"))["errors"])
    def test_05_market_blocks(self): self.assertIn("WORKING_ORDER_PRESENT", gate(active(order(order_type="Market")))["errors"])
    def test_06_limit_blocks(self): self.assertIn("WORKING_ORDER_PRESENT", gate(active(order(order_type="Limit")))["errors"])
    def test_07_stop_blocks(self): self.assertIn("ORPHAN_ORDER_CANDIDATE", gate(active(order(order_type="StopMarket", protective=True)))["errors"])
    def test_08_accepted_blocks(self): self.assertIn("WORKING_ORDER_PRESENT", gate(active(order("Accepted")))["errors"])
    def test_09_submitted_blocks(self): self.assertIn("WORKING_ORDER_PRESENT", gate(active(order("Submitted")))["errors"])
    def test_10_partial_blocks(self): self.assertIn("PARTIAL_ORDER_REMAINS", gate(active(order("PartFilled", quantity=2, filled_quantity=1, remaining_quantity=1)))["errors"])
    def test_11_flat_protective_is_orphan(self): self.assertIn("ORPHAN_ORDER_CANDIDATE", gate(active(order(protective=True)))["errors"])
    def test_12_oco_without_position_is_orphan(self): self.assertIn("ORPHAN_ORDER_CANDIDATE", gate(active(order(oco_id="OCO")))["errors"])
    def test_13_wrong_contract_blocks(self): self.assertIn("ORDER_POSITION_RECONCILIATION_FAILED", gate(active(order(instrument="MNQ 12-26")))["errors"])
    def test_14_wrong_instrument_blocks(self): self.assertIn("ORDER_POSITION_RECONCILIATION_FAILED", gate(active(order(instrument="NQ 09-26")))["errors"])
    def test_15_unknown_blocks(self): self.assertIn("UNKNOWN_ORDER_STATE", gate(active(order("Unknown"), unknown_count=1))["errors"])
    def test_16_terminal_only_passes(self): self.assertTrue(gate(snap(total_observed=3))["ok"])
    def test_17_sim101_cannot_match(self): self.assertIn("ORDER_ACCOUNT_MISMATCH", gate(snap(account_id="Sim101"))["errors"])
    def test_18_local_oif_blocks(self): self.assertIn("LOCAL_OIF_PENDING", gate(snap(), local_oif_count=1)["errors"])
    def test_19_no_oif_written(self): self.assertFalse(any(Path(".").glob("*.oif")))
    def test_20_no_mutation_api_in_addon(self):
        src=Path("ninjascript/AITRADEReadOnlySnapshot.cs").read_text(encoding="utf-8")
        for token in ("SubmitOrderUnmanaged", "CancelOrder(", "ChangeOrder(", "Flatten(", "File.WriteAllText(Path.Combine(Globals.UserDataDir, \"incoming\")"):
            self.assertNotIn(token, src)
    def test_21_missing_ops_order_gate_propagates_fail_closed(self):
        ctx = context_from_ops_snapshot({})
        self.assertIn("ORDER_STATE_MISSING", ctx.order_state_errors)
        self.assertIn("ORDER_STATE_MISSING", evaluate_account_gates(ctx)["errors"])
    def test_22_false_empty_ops_order_gate_propagates_fail_closed(self):
        ctx = context_from_ops_snapshot({"order_state_gate": {"ok": False, "errors": []}, "orders": {"active_count": 0}})
        self.assertEqual(("ORDER_STATE_MISSING",), ctx.order_state_errors)
    def test_23_valid_ops_order_gate_has_no_order_error(self):
        ctx = context_from_ops_snapshot({"order_state_gate": {"ok": True, "errors": []}, "orders": {"active_count": 0}})
        self.assertEqual((), ctx.order_state_errors)


if __name__ == "__main__": unittest.main()
