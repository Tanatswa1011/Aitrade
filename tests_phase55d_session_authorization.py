"""Fixture-only tests for the Monday session authorization boundary."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

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

    def test_issued_at_comes_from_issuance_clock_not_wall_clock_or_payload(self):
        until = (MON.astimezone(timezone.utc) + timedelta(minutes=20)).isoformat()
        wall = datetime(1999, 12, 31, 23, 59, 59, tzinfo=timezone.utc)
        real_now = auth._now

        def fake_now(now=None):
            if now is None:
                return wall
            return real_now(now)

        with patch.object(auth, "_now", fake_now):
            payload = auth.expected_payload(valid_until=until)
            wall_issued = payload["issued_at"]
            self.assertEqual(auth._parse_utc(wall_issued), wall)
            ancient = dict(payload)
            ancient["issued_at"] = "2020-01-01T00:00:00+00:00"
            result = auth.issue_request(ancient, now=MON, authorization_id="A" * 32)
            self.assertTrue(result["ok"], result)
            bound = json.loads((self.td / "auth.json").read_text(encoding="utf-8"))["payload"]
            issued = auth._parse_utc(bound["issued_at"])
            claim = auth._parse_utc(bound["claim_valid_until"])
            self.assertEqual(issued, MON.astimezone(timezone.utc))
            self.assertNotEqual(bound["issued_at"], "2020-01-01T00:00:00+00:00")
            self.assertNotEqual(bound["issued_at"], wall_issued)
            self.assertEqual(result["issued_at"], bound["issued_at"])
            self.assertEqual((claim - issued).total_seconds(), 20 * 60)
            self.assertLessEqual((claim - issued).total_seconds(), 45 * 60)
            self.assertGreater(claim, issued)
            self.assertFalse(result["PROP_EXECUTION"])
            (self.td / "auth.json").unlink()
            far_future = dict(payload)
            far_future["issued_at"] = "2099-01-01T00:00:00+00:00"
            second = auth.issue_request(far_future, now=MON, authorization_id="B" * 32)
            self.assertTrue(second["ok"], second)
            bound2 = json.loads((self.td / "auth.json").read_text(encoding="utf-8"))["payload"]
            self.assertEqual(bound2["issued_at"], bound["issued_at"])
            self.assertEqual(
                (auth._parse_utc(bound2["claim_valid_until"]) - auth._parse_utc(bound2["issued_at"])).total_seconds(),
                20 * 60,
            )

    def test_naive_and_malformed_claim_timestamps_rejected(self):
        naive = self.payload()
        naive["claim_valid_until"] = "2026-08-24T18:55:00"
        naive["valid_until"] = naive["claim_valid_until"]
        self.assertIn("AUTH_EXPIRY_MALFORMED", auth.validate_payload(naive, now=MON))
        self.assertIn("AUTH_EXPIRY_MALFORMED", auth.issue_request(naive, now=MON, authorization_id="C" * 32)["errors"])
        naive_now = datetime(2026, 8, 24, 14, 45)
        self.assertEqual(auth.issue_request(self.payload(), now=naive_now, authorization_id="D" * 32)["errors"], ["AUTH_TIMESTAMP_NAIVE"])
        malformed = self.payload()
        malformed["claim_valid_until"] = "not-a-timestamp"
        malformed["valid_until"] = malformed["claim_valid_until"]
        self.assertIn("AUTH_EXPIRY_MALFORMED", auth.validate_payload(malformed, now=MON))
        future = self.payload()
        future["issued_at"] = (MON.astimezone(timezone.utc) + timedelta(hours=1)).isoformat()
        self.assertIn("AUTH_ISSUED_AT_FUTURE", auth.validate_payload(future, now=MON))
        inconsistent = self.payload()
        inconsistent["issued_at"] = (MON.astimezone(timezone.utc) + timedelta(minutes=30)).isoformat()
        self.assertTrue(
            {"AUTH_EXPIRED", "AUTH_ISSUED_AT_FUTURE"} & set(auth.validate_payload(inconsistent, now=MON))
        )

    def test_consumed_permission_uses_session_deadline_not_claim_lease(self):
        self.assertTrue(self.issue()["ok"])
        out = process_pending_session_authorization(passing_unattended(now=MON))
        self.assertTrue(out["ok"], out)
        after_claim = MON + timedelta(minutes=30)
        self.assertGreater(after_claim, MON + timedelta(minutes=20))
        self.assertLess(after_claim, datetime(2026, 8, 24, 15, 30, tzinfo=NY))
        self.assertTrue(auth.runtime_permission_active(now=after_claim))
        self.assertGreater(
            auth._parse_utc(auth.status(now=after_claim)["session_valid_until"]),
            after_claim.astimezone(timezone.utc),
        )


if __name__ == "__main__":
    unittest.main()
