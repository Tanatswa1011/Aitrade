"""Fixture-only tests: morning claim lease vs frozen Monday session permission."""
from __future__ import annotations

import hashlib
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from dataclasses import replace

import phase55d_session_authorization as auth
from prop_canary import genuine_signal
from unattended_prop_canary import (
    ENV_STATE,
    UNATTENDED_BLOCKED,
    UNATTENDED_BLOCKED_RESTART,
    UNATTENDED_COMPLETE_NO_TRADE,
    UNATTENDED_WAITING_DVP,
    UNATTENDED_WAITING_SESSION,
    _build_payload,
    attempt_entry,
    daily_latch_used,
    passing_unattended,
    process_pending_session_authorization,
    public_snapshot,
    reset_for_tests,
    simulate_process_restart,
    structured_fixture_acknowledgements,
    tick,
)

BERLIN = ZoneInfo("Europe/Berlin")
NY = ZoneInfo("America/New_York")
ROOT = Path(__file__).resolve().parent
PROD_PATHS = [
    ROOT / "state" / "phase54_ops.json",
    ROOT / "state" / "phase55_live_dvp.json",
    ROOT / "state" / "phase55d_session_authorization.json",
    ROOT / "journal" / "phase54_ops" / "events.jsonl",
    ROOT / "journal" / "phase54_ops" / "signals.jsonl",
    ROOT / "journal" / "phase54_ops" / "notifications.jsonl",
    ROOT / "journal" / "phase54_ops" / "telemetry.jsonl",
    ROOT / "journal" / "phase54_ops" / "soak.json",
]


def berlin(hour: int, minute: int = 0, second: int = 0) -> datetime:
    return datetime(2026, 8, 24, hour, minute, second, tzinfo=BERLIN)


def _sha(path: Path) -> str:
    if not path.exists():
        return "missing"
    return hashlib.sha256(path.read_bytes()).hexdigest()


class Phase55DSessionLeaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._prod = {str(p): _sha(p) for p in PROD_PATHS}

    def setUp(self) -> None:
        self.td = Path(tempfile.mkdtemp(prefix="phase55d_lease_"))
        self.old = {k: os.environ.get(k) for k in (
            "AITRADE_PHASE54_TEST", "AITRADE_TEST_ROOT", "AITRADE_PHASE55D_AUTH_STATE", ENV_STATE,
            "AITRADE_UNATTENDED_PROP_CANARY")}
        os.environ["AITRADE_PHASE54_TEST"] = "1"
        os.environ["AITRADE_TEST_ROOT"] = str(self.td)
        os.environ["AITRADE_PHASE55D_AUTH_STATE"] = str(self.td / "auth.json")
        os.environ[ENV_STATE] = str(self.td / "unattended.json")
        os.environ.pop("AITRADE_UNATTENDED_PROP_CANARY", None)
        reset_for_tests(clear_persist=True)
        self.tx_calls: list[object] = []

    def tearDown(self) -> None:
        reset_for_tests(clear_persist=True)
        for key, value in self.old.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def _until(self, now: datetime, minutes: int = 45) -> str:
        return (now + timedelta(minutes=minutes)).astimezone(timezone.utc).isoformat()

    def _issue(self, now: datetime, minutes: int = 45, authorization_id: str = "B" * 32, **over):
        payload = auth.expected_payload(claim_valid_until=self._until(now, minutes), now=now)
        payload.update(over)
        return auth.issue_request(payload, now=now, authorization_id=authorization_id)

    def _claim(self, now: datetime, *, authorization_id: str = "B" * 32, **ctx_over):
        issued = self._issue(now, authorization_id=authorization_id)
        self.assertTrue(issued["ok"], issued)
        out = process_pending_session_authorization(passing_unattended(now=now, **ctx_over))
        self.assertTrue(out["ok"], out)
        self.assertEqual(auth.status(now=now)["status"], "CONSUMED")
        return out

    def _transmitter(self, lines, *, transmit):
        self.tx_calls.append((list(lines), transmit))
        raise AssertionError("tests must not transmit")

    def test_01_morning_claim_waits_until_session(self):
        out = self._claim(berlin(8, 0))
        self.assertEqual(out["state"], UNATTENDED_WAITING_SESSION)
        mid = tick(passing_unattended(now=berlin(12, 0)))
        self.assertEqual(mid["state"], UNATTENDED_WAITING_SESSION)
        self.assertFalse(mid.get("submitted"))
        self.assertTrue(auth.runtime_permission_active(now=berlin(12, 0)))
        self.assertFalse(out["PROP_EXECUTION"])

    def test_02_no_entry_before_1630_berlin(self):
        self._claim(berlin(8, 0))
        sig = genuine_signal(ts=berlin(8, 5).isoformat())
        early = attempt_entry(
            passing_unattended(now=berlin(8, 5), canary_over={"signal": sig}),
            transmit=False,
            transmitter=self._transmitter,
        )
        self.assertFalse(early.get("ok"))
        self.assertFalse(early.get("submitted"))
        self.assertIn(early.get("error_code"), {"NOT_WAITING_DVP", "BEFORE_SESSION_ENTRY_START", "OUTSIDE_SESSION"})
        self.assertEqual(self.tx_calls, [])
        just_before = tick(passing_unattended(
            now=berlin(16, 29, 59),
            canary_over={"signal": genuine_signal(ts=berlin(16, 29, 59).isoformat())},
        ))
        self.assertEqual(just_before["state"], UNATTENDED_WAITING_SESSION)
        self.assertFalse(just_before.get("submitted"))

    def test_03_session_open_requires_fresh_gates(self):
        self._claim(berlin(8, 0))
        opened = tick(passing_unattended(now=berlin(16, 30)))
        self.assertEqual(opened["state"], UNATTENDED_WAITING_DVP)
        self.assertFalse(opened.get("submitted"))

    def test_04_unclaimed_lease_expires(self):
        self.assertTrue(self._issue(berlin(8, 0))["ok"])
        claim = auth.claim_for_runtime(now=berlin(8, 46))
        self.assertFalse(claim.get("ok"))
        self.assertIn(auth.status(now=berlin(8, 46))["status"], {"EXPIRED", "REJECTED"})
        self.assertFalse(auth.runtime_permission_active(now=berlin(8, 46)))

    def test_05_claim_expiry_after_consume_does_not_end_session(self):
        self._claim(berlin(8, 0))
        later = berlin(8, 50)
        self.assertTrue(auth.runtime_permission_active(now=later))
        self.assertEqual(auth.status(now=later)["status"], "CONSUMED")
        waited = tick(passing_unattended(now=later))
        self.assertEqual(waited["state"], UNATTENDED_WAITING_SESSION)
        self.assertNotEqual(waited["state"], UNATTENDED_COMPLETE_NO_TRADE)

    def test_06_session_end_disarms_automatically(self):
        self._claim(berlin(8, 0))
        tick(passing_unattended(now=berlin(16, 30)))
        done = tick(passing_unattended(now=berlin(21, 30)))
        self.assertEqual(done["state"], UNATTENDED_COMPLETE_NO_TRADE)
        self.assertFalse(auth.runtime_permission_active(now=berlin(21, 30)))
        self.assertEqual(auth.status(now=berlin(21, 30))["status"], "EXPIRED")

    def test_07_authorization_does_not_survive_next_day(self):
        self._claim(berlin(8, 0))
        nxt = datetime(2026, 8, 25, 8, 0, tzinfo=BERLIN)
        self.assertFalse(auth.runtime_permission_active(now=nxt))
        payload = auth.expected_payload(claim_valid_until=self._until(nxt), now=nxt)
        payload["authorized_session_date"] = "2026-08-25"
        payload["session_date"] = "2026-08-25"
        self.assertFalse(auth.issue_request(payload, now=nxt, authorization_id="N" * 32)["ok"])

    def test_08_restart_invalidates_consumed_permission(self):
        self._claim(berlin(8, 0))
        simulate_process_restart()
        self.assertFalse(auth.runtime_permission_active(now=berlin(12, 0)))
        out = tick(passing_unattended(now=berlin(12, 0)))
        self.assertNotEqual(out["state"], UNATTENDED_WAITING_DVP)
        self.assertNotEqual(out["state"], UNATTENDED_WAITING_SESSION)
        self.assertIn(out["state"], {UNATTENDED_BLOCKED_RESTART, UNATTENDED_BLOCKED, "UNATTENDED_DISABLED"})

    def test_09_restart_preserves_attempt_lock(self):
        self._claim(berlin(8, 0))
        tick(passing_unattended(now=berlin(16, 30)))
        first = attempt_entry(
            passing_unattended(now=berlin(16, 35), canary_over={"signal": genuine_signal(ts=berlin(16, 35).isoformat())}),
            transmit=False,
        )
        self.assertTrue(first.get("ok") or daily_latch_used(), first)
        self.assertTrue(daily_latch_used())
        simulate_process_restart()
        self.assertTrue(daily_latch_used())
        second = attempt_entry(
            passing_unattended(now=berlin(16, 40), canary_over={"signal": genuine_signal(ts=berlin(16, 40).isoformat())}),
            transmit=False,
        )
        self.assertFalse(second.get("ok"))
        self.assertFalse(second.get("submitted"))

    def test_10_serious_failure_blocks_the_day(self):
        self._claim(berlin(8, 0))
        blocked = tick(passing_unattended(now=berlin(9, 0), canary_over={"nt_connected": False}))
        self.assertEqual(blocked["state"], UNATTENDED_BLOCKED)
        self.assertFalse(blocked.get("submitted"))

    def test_11_dependency_recovery_does_not_resume(self):
        self._claim(berlin(8, 0))
        tick(passing_unattended(now=berlin(9, 0), canary_over={"nt_connected": False}))
        recovered = tick(passing_unattended(now=berlin(9, 5), canary_over={"nt_connected": True}))
        self.assertEqual(recovered["state"], UNATTENDED_BLOCKED)
        self.assertNotEqual(recovered["state"], UNATTENDED_WAITING_DVP)

    def test_12_stale_gate_immediately_before_entry_blocks(self):
        self._claim(berlin(8, 0))
        blocked = tick(passing_unattended(now=berlin(16, 30), canary_over={"market_stale": True, "market_live": False}))
        self.assertEqual(blocked["state"], UNATTENDED_BLOCKED)
        self.assertNotEqual(blocked["state"], UNATTENDED_WAITING_DVP)

    def test_13_genuine_in_window_dvp_one_mnq_no_transmit(self):
        self._claim(berlin(8, 0))
        tick(passing_unattended(now=berlin(16, 30)))
        now = berlin(16, 35)
        sig = genuine_signal(ts=now.isoformat(), signal_id="lease-window")
        ctx = passing_unattended(now=now, canary_over={"signal": sig})
        trade_id = "AITRADE_UNATTENDED_lease-window"
        plan = _build_payload(ctx.canary, direction="LONG", trade_id=trade_id)["plan"]
        entry_ack, stop_ack, target_ack = structured_fixture_acknowledgements(plan, fill_qty=1, now=now)
        ctx = replace(ctx, entry_ack=entry_ack, stop_ack=stop_ack, target_ack=target_ack)
        out = attempt_entry(ctx, transmit=False)
        self.assertTrue(out.get("ok"), out)
        self.assertFalse(out.get("submitted") or out.get("transmitted"))
        payload = out.get("payload") or {}
        self.assertEqual(payload.get("quantity") or payload.get("qty"), 1)
        self.assertEqual(self.tx_calls, [])
        self.assertFalse(out["PROP_EXECUTION"])

    def test_14_synthetic_replayed_early_and_late_signals_rejected(self):
        cases = [
            genuine_signal(ts=berlin(16, 35).isoformat(), source="SYNTHETIC", live_bar=False),
            genuine_signal(ts=berlin(16, 35).isoformat(), kind="REPLAY", note="replay"),
            genuine_signal(ts=berlin(9, 0).isoformat()),
            genuine_signal(ts=berlin(21, 31).isoformat()),
        ]
        for i, sig in enumerate(cases):
            with self.subTest(i=i):
                reset_for_tests(clear_persist=True)
                Path(os.environ["AITRADE_PHASE55D_AUTH_STATE"]).unlink(missing_ok=True)
                self._claim(berlin(8, 0), authorization_id=chr(65 + i) * 32)
                tick(passing_unattended(now=berlin(16, 30)))
                out = attempt_entry(passing_unattended(now=berlin(16, 35), canary_over={"signal": sig}), transmit=False)
                self.assertFalse(out.get("ok"), out)
                self.assertFalse(out.get("submitted"))

    def test_15_second_attempt_blocked(self):
        self._claim(berlin(8, 0))
        tick(passing_unattended(now=berlin(16, 30)))
        first = attempt_entry(
            passing_unattended(now=berlin(16, 35), canary_over={"signal": genuine_signal(ts=berlin(16, 35).isoformat())}),
            transmit=False,
        )
        self.assertTrue(first.get("ok") or daily_latch_used(), first)
        second = attempt_entry(
            passing_unattended(now=berlin(16, 40), canary_over={"signal": genuine_signal(ts=berlin(16, 40).isoformat())}),
            transmit=False,
        )
        self.assertFalse(second.get("ok"))
        self.assertFalse(second.get("submitted"))

    def test_16_no_dvp_completes_without_trade(self):
        self._claim(berlin(8, 0))
        tick(passing_unattended(now=berlin(16, 30)))
        done = tick(passing_unattended(now=berlin(21, 30)))
        self.assertEqual(done["state"], UNATTENDED_COMPLETE_NO_TRADE)
        self.assertFalse(done.get("submitted"))

    def test_17_dst_timezone_conversion_matches_frozen_window(self):
        start, end = auth.frozen_session_window("2026-08-24")
        self.assertEqual(start, datetime(2026, 8, 24, 10, 30, tzinfo=NY).astimezone(timezone.utc))
        self.assertEqual(end, datetime(2026, 8, 24, 15, 30, tzinfo=NY).astimezone(timezone.utc))
        self.assertEqual(start, berlin(16, 30).astimezone(timezone.utc))
        self.assertEqual(end, berlin(21, 30).astimezone(timezone.utc))
        self.assertEqual(start.isoformat(), "2026-08-24T14:30:00+00:00")
        self.assertEqual(end.isoformat(), "2026-08-24T19:30:00+00:00")

    def test_18_session_configuration_mismatch_fails_closed(self):
        bad = auth.expected_payload(claim_valid_until=self._until(berlin(8, 0)), now=berlin(8, 0))
        bad["session_valid_until"] = "2026-08-24T23:59:59+00:00"
        result = auth.issue_request(bad, now=berlin(8, 0), authorization_id="C" * 32)
        self.assertFalse(result.get("ok"))
        self.assertTrue(any("SESSION" in e or "MISMATCH" in e or "CONFLICT" in e for e in (result.get("errors") or [])))
        orig = auth.TRADE_START_LOCAL
        try:
            auth.TRADE_START_LOCAL = "09:00"
            with self.assertRaises(Exception):
                auth.frozen_session_window("2026-08-24")
        finally:
            auth.TRADE_START_LOCAL = orig

    def test_19_production_state_and_journals_byte_identical(self):
        for path, digest in self._prod.items():
            self.assertEqual(_sha(Path(path)), digest, path)

    def test_20_no_oif_or_broker_operation_in_tests(self):
        self._claim(berlin(8, 0))
        tick(passing_unattended(now=berlin(16, 30)))
        out = attempt_entry(
            passing_unattended(now=berlin(16, 35), canary_over={"signal": genuine_signal(ts=berlin(16, 35).isoformat())}),
            transmit=False,
        )
        self.assertFalse(out.get("submitted") or out.get("transmitted"))
        self.assertEqual(self.tx_calls, [])
        self.assertFalse(any(self.td.rglob("*.oif")))
        self.assertFalse(public_snapshot().get("PROP_EXECUTION"))

    @classmethod
    def tearDownClass(cls) -> None:
        for path, digest in cls._prod.items():
            if _sha(Path(path)) != digest:
                raise AssertionError("production artifact mutated: " + path)


if __name__ == "__main__":
    unittest.main()
