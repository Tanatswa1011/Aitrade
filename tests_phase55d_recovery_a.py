"""Phase 55D Recovery A: state isolation and structured broker acknowledgements."""
from __future__ import annotations

import hashlib
import os
import tempfile
import unittest
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path

import prop_canary
import unattended_prop_canary
from broker_acknowledgements import (
    ExpectedLifecycle, ProtectionLifecycle, PROTECTED_CONFIRMED,
    PROTECTION_REJECTED, PROTECTION_UNKNOWN, FLATTEN_REQUESTED,
    FLATTEN_ACKNOWLEDGED, FLAT_CONFIRMED,
)
from prop_canary_nt_exec import CANARY_NT_ACCOUNT
from unattended_prop_canary import structured_fixture_acknowledgements

ROOT = Path(__file__).resolve().parent
PRODUCTION_STATE = ROOT / "state" / "prop_canary.json"
NOW = datetime(2026, 8, 24, 14, 45, tzinfo=timezone.utc)


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else "MISSING"


class CanaryPathIsolationTests(unittest.TestCase):
    def setUp(self):
        self.old = {key: os.environ.get(key) for key in (
            "AITRADE_PHASE54_TEST", "AITRADE_TEST_ROOT",
            prop_canary.ENV_STATE, unattended_prop_canary.ENV_STATE,
        )}
        self.prod_before = digest(PRODUCTION_STATE)
        self.root = Path(tempfile.mkdtemp(prefix="recovery_a_"))

    def tearDown(self):
        # Clear only isolated persistence before restoring the environment.
        os.environ.pop(prop_canary.ENV_STATE, None)
        os.environ.pop(unattended_prop_canary.ENV_STATE, None)
        if os.environ.get("AITRADE_PHASE54_TEST") == "1" and os.environ.get("AITRADE_TEST_ROOT"):
            prop_canary.reset_for_tests(clear_persist=True)
            unattended_prop_canary.reset_for_tests(clear_persist=True)
        for key, value in self.old.items():
            if value is None: os.environ.pop(key, None)
            else: os.environ[key] = value
        self.assertEqual(self.prod_before, digest(PRODUCTION_STATE))

    def activate(self, root: Path | None = None) -> Path:
        chosen = (root or self.root).resolve()
        os.environ["AITRADE_PHASE54_TEST"] = "1"
        os.environ["AITRADE_TEST_ROOT"] = str(chosen)
        os.environ.pop(prop_canary.ENV_STATE, None)
        os.environ.pop(unattended_prop_canary.ENV_STATE, None)
        return chosen

    def test_01_import_before_test_mode_resolves_at_use_time(self):
        root = self.activate()
        self.assertEqual(root / "state" / "prop_canary.json", prop_canary._state_path())

    def test_02_import_after_test_mode_uses_root(self):
        root = self.activate()
        self.assertTrue(str(prop_canary._state_path()).startswith(str(root)))

    def test_03_default_replacement_is_dynamic(self):
        a = self.activate(self.root / "a")
        self.assertEqual(a / "state" / "prop_canary.json", prop_canary._state_path())
        b = self.activate(self.root / "b")
        self.assertEqual(b / "state" / "prop_canary.json", prop_canary._state_path())

    def test_04_lifecycle_write_leaves_production_identical(self):
        root = self.activate()
        prop_canary._save_persist(last_persisted_mode=prop_canary.PROP_CANARY_BLOCKED)
        self.assertTrue((root / "state" / "prop_canary.json").exists())

    def test_05_restart_persistence_is_temporary(self):
        root = self.activate()
        prop_canary._save_persist(one_shot_consumed=True)
        prop_canary.simulate_process_restart()
        self.assertTrue((root / "state" / "prop_canary.json").exists())

    def test_06_parallel_roots_do_not_share_lock(self):
        a = self.activate(self.root / "one")
        prop_canary._save_persist(one_shot_consumed=True)
        b = self.activate(self.root / "two")
        self.assertFalse(prop_canary._load_persist().get("one_shot_consumed"))
        self.assertNotEqual(a, b)

    def test_07_missing_test_root_fails_closed(self):
        os.environ["AITRADE_PHASE54_TEST"] = "1"
        os.environ.pop("AITRADE_TEST_ROOT", None)
        with self.assertRaisesRegex(RuntimeError, "authoritative_test_root_required"):
            prop_canary._state_path()

    def test_08_path_traversal_rejected(self):
        self.activate()
        os.environ[prop_canary.ENV_STATE] = str(self.root.parent / "escaped.json")
        with self.assertRaisesRegex(RuntimeError, "test_path_escaped_workspace"):
            prop_canary._state_path()

    def test_09_unattended_path_is_isolated(self):
        root = self.activate()
        self.assertEqual(root / "state" / "unattended_prop_canary.json", unattended_prop_canary._state_path())

    def test_10_production_ignores_untrusted_override(self):
        os.environ.pop("AITRADE_PHASE54_TEST", None)
        os.environ[prop_canary.ENV_STATE] = str(self.root / "untrusted.json")
        self.assertEqual(prop_canary.DEFAULT_STATE_PATH, prop_canary._state_path())

    def test_11_test_cannot_clear_production_lock(self):
        self.activate()
        prop_canary.reset_for_tests(clear_persist=True)
        self.assertEqual(self.prod_before, digest(PRODUCTION_STATE))

    def test_12_no_production_journal_or_oif_path(self):
        root = self.activate()
        for path in (prop_canary._state_path(), unattended_prop_canary._state_path()):
            self.assertTrue(path == root or root in path.parents)
        self.assertNotIn("NinjaTrader 8", str(prop_canary._state_path()))


def expected() -> ExpectedLifecycle:
    return ExpectedLifecycle(
        account_id=CANARY_NT_ACCOUNT, instrument="MNQ SEP26", contract_month="09-26",
        entry_action="BUY", protective_action="SELL", quantity=1,
        entry_order_id="LIFE_ENTRY", oco_id="AITRADE_OCO_LIFE",
        correlation_id="LIFE", source="NINJATRADER_FUNDEDNEXT",
    )


def ack(kind: str, **over):
    protective = kind.startswith("STOP") or kind.startswith("TARGET")
    out = {
        "ack_type": kind, "account_id": CANARY_NT_ACCOUNT,
        "instrument": "MNQ SEP26", "contract_month": "09-26",
        "action": "SELL" if protective else "BUY", "quantity": 1,
        "filled_quantity": 0 if protective or kind == "ENTRY_ACKNOWLEDGED" else 1,
        "broker_order_id": "STOP1" if kind.startswith("STOP") else "TGT1" if kind.startswith("TARGET") else "LIFE_ENTRY",
        "parent_entry_id": "LIFE_ENTRY", "oco_id": "AITRADE_OCO_LIFE" if protective else None,
        "correlation_id": "LIFE",
        "order_state": "WORKING" if protective else "ACCEPTED" if kind == "ENTRY_ACKNOWLEDGED" else "FILLED",
        "broker_event_timestamp": NOW.isoformat(), "local_receipt_timestamp": NOW.isoformat(),
        "source": "NINJATRADER_FUNDEDNEXT",
    }
    out.update(over)
    return out


def filled_lifecycle() -> ProtectionLifecycle:
    life = ProtectionLifecycle(expected())
    life.apply(ack("ENTRY_FILL"), now=NOW)
    return life


class BrokerAcknowledgementTests(unittest.TestCase):
    def test_01_entry_ack_alone_not_protected(self):
        life=ProtectionLifecycle(expected()); life.apply(ack("ENTRY_ACKNOWLEDGED"),now=NOW); self.assertFalse(life.protected)
    def test_02_stop_alone_not_protected(self):
        life=filled_lifecycle(); life.apply(ack("STOP_ACKNOWLEDGED"),now=NOW); self.assertFalse(life.protected)
    def test_03_target_alone_not_protected(self):
        life=filled_lifecycle(); life.apply(ack("TARGET_ACKNOWLEDGED"),now=NOW); self.assertFalse(life.protected)
    def test_04_both_legs_confirm_protection(self):
        life=filled_lifecycle(); life.apply(ack("STOP_ACKNOWLEDGED"),now=NOW); life.apply(ack("TARGET_ACKNOWLEDGED"),now=NOW); self.assertEqual(PROTECTED_CONFIRMED,life.state); self.assertTrue(life.protected)
    def mismatch(self, **over):
        life=filled_lifecycle(); out=life.apply(ack("STOP_ACKNOWLEDGED",**over),now=NOW); self.assertFalse(out["ok"]); self.assertFalse(life.protected)
    def test_05_wrong_account(self): self.mismatch(account_id="WRONG")
    def test_06_wrong_instrument(self): self.mismatch(instrument="NQ SEP26")
    def test_07_wrong_contract(self): self.mismatch(contract_month="12-26")
    def test_08_wrong_quantity(self): self.mismatch(quantity=0)
    def test_09_wrong_parent(self): self.mismatch(parent_entry_id="OTHER")
    def test_10_wrong_correlation(self): self.mismatch(correlation_id="OTHER")
    def test_11_missing_oco(self): self.mismatch(oco_id=None)
    def test_12_mismatched_oco(self): self.mismatch(oco_id="OTHER")
    def test_13_missing_broker_id(self): self.mismatch(broker_order_id="")
    def test_14_missing_timestamp(self): self.mismatch(broker_event_timestamp=None)
    def test_15_stale_timestamp(self): self.mismatch(broker_event_timestamp=(NOW-timedelta(seconds=16)).isoformat())
    def test_16_future_timestamp(self): self.mismatch(broker_event_timestamp=(NOW+timedelta(seconds=3)).isoformat())
    def test_17_duplicate_cannot_advance_twice(self):
        life=filled_lifecycle(); item=ack("STOP_ACKNOWLEDGED"); life.apply(item,now=NOW); out=life.apply(item,now=NOW); self.assertFalse(out["ok"]); self.assertEqual(1,life.stop_quantity)
    def test_18_out_of_order_fails_closed(self):
        life=ProtectionLifecycle(expected()); out=life.apply(ack("STOP_ACKNOWLEDGED"),now=NOW); self.assertFalse(out["ok"]); self.assertEqual(PROTECTION_UNKNOWN,life.state)
    def test_19_stop_rejected_escalates(self):
        life=filled_lifecycle(); out=life.apply(ack("STOP_REJECTED",order_state="REJECTED"),now=NOW); self.assertTrue(out["escalation_required"]); self.assertEqual(PROTECTION_REJECTED,life.state)
    def test_20_target_rejected_escalates(self):
        life=filled_lifecycle(); out=life.apply(ack("TARGET_REJECTED",order_state="REJECTED"),now=NOW); self.assertTrue(out["escalation_required"])
    def test_21_cancel_removes_protection(self):
        life=filled_lifecycle(); life.apply(ack("STOP_ACKNOWLEDGED"),now=NOW); life.apply(ack("TARGET_ACKNOWLEDGED"),now=NOW); life.apply(ack("STOP_CANCELLED",order_state="CANCELLED",broker_event_timestamp=(NOW+timedelta(seconds=1)).isoformat(),local_receipt_timestamp=(NOW+timedelta(seconds=1)).isoformat()),now=NOW+timedelta(seconds=1)); self.assertFalse(life.protected)
    def test_22_zero_fill_not_protected(self):
        life=ProtectionLifecycle(expected()); out=life.apply(ack("ENTRY_PARTIAL_FILL",filled_quantity=0,order_state="PARTFILLED"),now=NOW); self.assertFalse(out["ok"]); self.assertFalse(life.protected)
    def test_23_unprotected_exposure_escalates(self):
        life=filled_lifecycle(); self.assertTrue(life.escalation_required); self.assertFalse(life.protected)
    def test_24_connection_loss_marks_unknown(self):
        life=filled_lifecycle(); life.apply(ack("STOP_ACKNOWLEDGED"),now=NOW); life.apply(ack("TARGET_ACKNOWLEDGED"),now=NOW); life.connection_lost(); self.assertEqual(PROTECTION_UNKNOWN,life.state); self.assertFalse(life.protected)
    def test_25_flatten_request_not_completion(self):
        life=filled_lifecycle(); out=life.request_flatten(); self.assertEqual(FLATTEN_REQUESTED,out["state"]); self.assertFalse(out["flat_confirmed"])
    def test_26_flatten_ack_not_flat(self):
        life=filled_lifecycle(); life.request_flatten(); out=life.apply(ack("FLATTEN_ACKNOWLEDGED",action="SELL",order_state="ACCEPTED",broker_order_id="FLAT1"),now=NOW); self.assertEqual(FLATTEN_ACKNOWLEDGED,out["state"]); self.assertFalse(out["flat_confirmed"])
    def test_27_only_authoritative_flat_confirms(self):
        life=filled_lifecycle(); life.request_flatten(); life.apply(ack("FLATTEN_ACKNOWLEDGED",action="SELL",order_state="ACCEPTED",broker_order_id="FLAT1"),now=NOW); out=life.apply(ack("POSITION_FLAT_CONFIRMED",action="SELL",order_state="FLAT",broker_order_id="POS1"),now=NOW); self.assertEqual(FLAT_CONFIRMED,out["state"]); self.assertTrue(out["flat_confirmed"])
    def test_28_no_mutation_api(self):
        src=Path("broker_acknowledgements.py").read_text(encoding="utf-8"); self.assertFalse(any(x in src for x in ("SubmitOrder", "CancelOrder(", "ChangeOrder(", "Flatten(")))
    def test_29_no_real_oif_created(self): self.assertFalse(any(Path(".").glob("*.oif")))
    def test_30_fixture_ack_schema_complete(self):
        plan={"action":"BUY","exit_action":"SELL","entry_order_id":"LIFE_ENTRY","stop_order_id":"STOP1","target_order_id":"TGT1","oco_id":"AITRADE_OCO_LIFE","trade_id":"LIFE"}
        for item in structured_fixture_acknowledgements(plan,now=NOW):
            self.assertIn("broker_order_id",item); self.assertIn("source",item); self.assertNotIn("token",str(item).lower())


if __name__ == "__main__": unittest.main()
