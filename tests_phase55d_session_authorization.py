"""Fixture-only tests for the Monday session authorization boundary."""
from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import phase55d_session_authorization as auth
from prop_canary import _save_persist as save_canary_persist, manual_revalidation_required
from unattended_prop_canary import (
    ENV_STATE, NY, UNATTENDED_BLOCKED, UNATTENDED_WAITING_DVP,
    ordered_monday_gates, passing_unattended, process_pending_session_authorization,
    public_snapshot, reset_for_tests, simulate_process_restart, tick,
)

MON = datetime(2026, 8, 24, 14, 45, tzinfo=NY)


class Phase55DSessionAuthorizationTests(unittest.TestCase):
    def setUp(self):
        self.td = Path(tempfile.mkdtemp(prefix="phase55d_auth_"))
        self.old = {k: os.environ.get(k) for k in (
            "AITRADE_PHASE54_TEST", "AITRADE_TEST_ROOT", "AITRADE_PHASE55D_AUTH_STATE", ENV_STATE,
            "AITRADE_UNATTENDED_PROP_CANARY")}
        os.environ["AITRADE_PHASE54_TEST"] = "1"
        os.environ["AITRADE_TEST_ROOT"] = str(self.td)
        os.environ["AITRADE_PHASE55D_AUTH_STATE"] = str(self.td / "auth.json")
        os.environ[ENV_STATE] = str(self.td / "unattended.json")
        os.environ.pop("AITRADE_UNATTENDED_PROP_CANARY", None)
        reset_for_tests(clear_persist=True)

    def tearDown(self):
        reset_for_tests(clear_persist=True)
        for key, value in self.old.items():
            if value is None: os.environ.pop(key, None)
            else: os.environ[key] = value

    def payload(self, **over):
        until = (MON.astimezone(timezone.utc) + timedelta(minutes=20)).isoformat()
        p = auth.expected_payload(valid_until=until)
        p.update(over)
        return p

    def issue(self, **over):
        return auth.issue_request(self.payload(**over), now=MON, authorization_id="A" * 32)

    def test_valid_authorization_consumes_and_enters_waiting(self):
        self.assertTrue(self.issue()["ok"])
        out = process_pending_session_authorization(passing_unattended(now=MON))
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["state"], UNATTENDED_WAITING_DVP)
        self.assertEqual(auth.status()["status"], "CONSUMED")
        self.assertFalse(out["PROP_EXECUTION"])

    def test_payload_mismatches_rejected(self):
        for field, value in (("expected_account", "Sim101"), ("signal_contract", "NQ 12-26"),
                             ("execution_contract", "MNQ 12-26"), ("maximum_quantity", 2),
                             ("session_date", "2026-08-25"), ("require_all_live_gates", False)):
            with self.subTest(field=field):
                result = auth.validate_payload(self.payload(**{field: value}), now=MON)
                self.assertTrue(any(field in e for e in result), result)

    def test_expired_and_long_lived_rejected(self):
        expired = auth.expected_payload(valid_until=(MON.astimezone(timezone.utc)-timedelta(seconds=1)).isoformat())
        self.assertIn("AUTH_EXPIRED", auth.validate_payload(expired, now=MON))
        long = auth.expected_payload(valid_until=(MON.astimezone(timezone.utc)+timedelta(hours=2)).isoformat())
        self.assertIn("AUTH_NOT_SHORT_LIVED", auth.validate_payload(long, now=MON))

    def test_duplicate_and_reuse_rejected(self):
        self.assertTrue(self.issue()["ok"])
        self.assertIn("AUTH_DUPLICATE_OR_REUSED", self.issue()["errors"])

    def test_each_ordered_gate_fails_closed(self):
        cases = [
            {"canary_over": {"nt_connected": False}}, {"canary_over": {"market_live": False}},
            {"canary_over": {"agg_5m_healthy": False}}, {"canary_over": {"agg_15m_healthy": False}},
            {"canary_over": {"connected": False}}, {"canary_over": {"account_age_sec": 99}},
            {"canary_over": {"account_status": "LOCKED"}}, {"canary_over": {"position_qty": 1}},
            {"canary_over": {"working_orders": 1}}, {"orphan_protective": True},
            {"canary_over": {"requested_qty": 2}}, {"canary_over": {"phase_55b_0_pass": False}},
            {"bars_fresh": False}, {"nq_1m_count_prev": 12},
            {"market_provenance": "PLAYBACK"}, {"independent_5m_match": False},
            {"independent_15m_match": False}, {"native_orders_fresh": False},
            {"pending_orders": 1}, {"partial_orders": 1}, {"orphan_orders": 1},
            {"unknown_orders": 1},
        ]
        for over in cases:
            with self.subTest(over=over):
                gates = ordered_monday_gates(passing_unattended(now=MON, **over))
                self.assertFalse(all(g["ok"] for g in gates))

    def test_failed_gate_rejects_without_waiting(self):
        self.issue()
        out = process_pending_session_authorization(passing_unattended(now=MON, canary_over={"working_orders": 1}))
        self.assertFalse(out["ok"])
        self.assertNotEqual(public_snapshot()["state"], UNATTENDED_WAITING_DVP)
        self.assertEqual(auth.status()["status"], "REJECTED")

    def test_restart_invalidates_runtime_binding(self):
        self.issue()
        self.assertTrue(process_pending_session_authorization(passing_unattended(now=MON))["ok"])
        simulate_process_restart()
        self.assertNotEqual(tick(passing_unattended(now=MON))["state"], UNATTENDED_WAITING_DVP)

    def test_pending_authorization_is_invalidated_on_runtime_restart(self):
        self.assertTrue(self.issue()["ok"])
        self.assertEqual(auth.status()["status"], "PENDING")
        auth.invalidate_on_restart(now=MON)
        self.assertEqual(auth.status()["status"], "INVALIDATED_RESTART")
        self.assertFalse(auth.runtime_permission_active(now=MON))

    def test_serious_latch_only_resolves_after_authorized_current_gates(self):
        save_canary_persist(last_persisted_mode="PROP_CANARY_BLOCKED", last_error="EMERGENCY_FLATTEN")
        self.assertTrue(manual_revalidation_required())
        self.issue()
        out = process_pending_session_authorization(passing_unattended(now=MON))
        self.assertTrue(out["ok"], out)
        self.assertFalse(manual_revalidation_required())

    def test_auth_file_contains_no_token_or_credential(self):
        self.issue()
        raw = (self.td / "auth.json").read_text(encoding="utf-8").lower()
        for forbidden in ("access_token", "refresh_token", "client_secret", "password"):
            self.assertNotIn(forbidden, raw)

    def test_no_pending_request_does_nothing(self):
        out = tick(passing_unattended(now=MON))
        self.assertNotEqual(out["state"], UNATTENDED_WAITING_DVP)
        self.assertFalse(out["PROP_EXECUTION"])


if __name__ == "__main__":
    unittest.main()
