"""Isolated Task 7 alert and Task 8 recovery validation."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from aitrade_notifications import NotificationService, format_telegram
from phase55d_tasks78 import (
    ACCOUNT, ALERT_SCENARIOS, DRY_RUN_PREFIX, INSTRUMENT, QUANTITY,
    RecoveryHarness, build_dry_run_alert,
)


class Task7AlertTests(unittest.TestCase):
    def setUp(self):
        self.root=Path(tempfile.mkdtemp(prefix="task7_capture_")); self.captured=[]
        self.service=NotificationService(
            enabled=True, backend=lambda event: self.captured.append(event) is None,
            state_path=self.root/"state.json", journal_path=self.root/"journal.jsonl", worker=False,
        )

    def test_01_all_scenarios_have_required_envelope(self):
        for index,name in enumerate(ALERT_SCENARIOS):
            event=build_dry_run_alert(name,correlation_id=f"FIXTURE-{index:02d}")
            public=event.to_public_dict(); rendered=format_telegram(event)
            self.assertTrue(public["title"].startswith(DRY_RUN_PREFIX)); self.assertIn(DRY_RUN_PREFIX,rendered)
            self.assertEqual(ACCOUNT,public["account"]); self.assertEqual(INSTRUMENT,public["instrument"])
            self.assertEqual(QUANTITY,public["metadata"]["quantity"]); self.assertEqual("DISARMED",public["metadata"]["engine_state"])
            self.assertTrue(public["timestamp"].endswith("+00:00")); self.assertTrue(public["event_type"]); self.assertTrue(public["severity"])
            self.assertIn("FIXTURE-",public["metadata"]["correlation_id"])
            self.assertNotRegex(rendered.lower(),r"access_token|refresh_token|authorization:|bearer |password=")

    def test_02_capture_acceptance_is_not_external_confirmation(self):
        event=build_dry_run_alert("runtime_started",correlation_id="FIXTURE-ACCEPT")
        self.assertTrue(self.service.emit(event,force=True)); self.assertEqual(1,len(self.captured))
        self.assertEqual("CONSTRUCTED_ONLY",self.captured[0].metadata["delivery_claim"])

    def test_03_delivery_failure_visible_and_non_crashing(self):
        failing=NotificationService(enabled=True,backend=lambda event: (_ for _ in ()).throw(RuntimeError("token=secret-value")),state_path=self.root/"fstate.json",journal_path=self.root/"failure.jsonl",worker=False)
        event=build_dry_run_alert("unexpected_engine_failure",correlation_id="FIXTURE-FAIL")
        self.assertFalse(failing.emit(event,force=True)); health=failing.health()
        self.assertEqual("FAILED",health["delivery_status"]); self.assertNotIn("secret-value",json.dumps(health))
        journal=(self.root/"failure.jsonl").read_text(encoding="utf-8"); self.assertIn('"delivered": false',journal); self.assertNotIn("secret-value",journal)

    def test_04_duplicate_suppression_bounded(self):
        event=build_dry_run_alert("valid_fixture_dvp",correlation_id="FIXTURE-DUP")
        event.metadata["identity"]="FIXTURE-DUP"
        self.assertTrue(self.service.emit(event)); self.assertFalse(self.service.emit(event)); self.assertEqual(1,len(self.captured))

    def test_05_alerts_never_arm(self):
        for name in ALERT_SCENARIOS: self.service.emit(build_dry_run_alert(name,correlation_id="FIXTURE-SAFE"),force=True)
        self.assertEqual(16,len(self.captured)); self.assertTrue(all(e.metadata["engine_state"]=="DISARMED" for e in self.captured))


class Task8RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.root=Path(tempfile.mkdtemp(prefix="task8_recovery_")); self.h=RecoveryHarness(self.root/"state.json")

    def test_01_cold_start_disarmed(self): self.assertEqual("DISARMED",self.h.snapshot()["state"]); self.assertFalse(self.h.prop_execution)
    def test_02_dashboard_restart_disarmed(self): self.h.restart("dashboard"); self.assertEqual("DISARMED",self.h.state)
    def test_03_runtime_restart_disarmed(self): self.h.restart("runtime"); self.assertEqual("DISARMED",self.h.state)
    def test_04_nt_disconnect_and_restore_do_not_arm(self): self.h.interrupt("ninjatrader",serious=True); self.h.restore("ninjatrader"); self.assertEqual("MANUAL_REVALIDATION_REQUIRED",self.h.state); self.assertFalse(self.h.prop_execution)
    def test_05_market_stale_and_restore_blocked(self): self.h.interrupt("market"); self.h.restore("market"); self.assertEqual("BLOCKED",self.h.state)
    def test_06_risk_interruption_and_restore_blocked(self): self.h.interrupt("risk",serious=True); self.h.restore("risk"); self.assertEqual("MANUAL_REVALIDATION_REQUIRED",self.h.state)
    def test_07_auth_failure_visible(self): self.h.interrupt("auth",serious=True); self.assertIn("AUTH_UNAVAILABLE",self.h.reasons)
    def test_08_serious_failure_requires_manual_revalidation(self): self.h.serious_failure("PROTECTION_UNKNOWN"); self.h.restore("market"); self.assertTrue(self.h.serious_failure_latch); self.assertEqual("MANUAL_REVALIDATION_REQUIRED",self.h.state)
    def test_09_one_shot_survives_restarts(self): self.h.one_shot_used=True; self.h.save(); other=RecoveryHarness(self.h.state_path); other.load(); other.restart("runtime"); self.assertTrue(other.one_shot_used)
    def test_10_complete_or_blocked_not_reset(self): self.h.one_shot_used=True; self.h.state="COMPLETE"; self.h.save(); self.h.restart("dashboard"); self.assertTrue(self.h.one_shot_used); self.assertNotEqual("ARMED",self.h.state)
    def test_11_unknown_state_fails_closed(self): self.h.state_path.write_text('{"schema":"PHASE55D_RECOVERY_FIXTURE_V1","state":"ARMED","PROP_EXECUTION":true}',encoding="utf-8"); self.h.load(); self.assertEqual("BLOCKED",self.h.state); self.assertFalse(self.h.prop_execution)
    def test_12_corrupt_state_fails_closed(self): self.h.state_path.write_text('{partial',encoding="utf-8"); self.h.load(); self.assertEqual("BLOCKED",self.h.state)
    def test_13_restart_clears_unproven_protection_and_flat(self): self.h.protected_confirmed=True; self.h.flat_confirmed=True; self.h.restart("runtime"); self.assertFalse(self.h.protected_confirmed); self.assertFalse(self.h.flat_confirmed)
    def test_14_no_missed_signal_replay(self): self.h.restart("runtime"); self.assertNotIn("signal",self.h.snapshot())
    def test_15_no_fixture_oif(self): self.assertFalse(any(self.root.rglob("*.oif")))


if __name__=="__main__": unittest.main()
