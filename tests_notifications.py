"""Notification layer — fail-isolated, outbound-only. Never arms or submits."""
from __future__ import annotations

import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

os.environ["AITRADE_NOTIFY_TEST"] = "1"
os.environ.setdefault("AITRADE_PHASE54_TEST", "1")
os.environ.setdefault(
    "AITRADE_PHASE54_JOURNAL",
    str(Path(tempfile.mkdtemp(prefix="phase54_notify_journal_"))),
)

from aitrade_notifications import (
    ENV_ENABLED,
    ENV_URL,
    EventType,
    NotificationEvent,
    NotificationService,
    inbound_execution_controls_present,
    is_genuine_live_dvp,
    is_shadow_observation,
    mask_secrets,
    notify_submit_result,
    reset_service_for_tests,
)
from execution_status import BLOCKED_MODES, NQ_FROZEN_HASH, sim_only_execution_armed
from phase54_ops import PROP_EXECUTION


def _svc(backend, **kw):
    td = Path(tempfile.mkdtemp(prefix="aitrade_notify_"))
    return NotificationService(
        enabled=kw.pop("enabled", True),
        backend=backend,
        worker=kw.pop("worker", False),
        state_path=td / "state.json",
        journal_path=td / "notifications.jsonl",
        reminder_sec=kw.pop("reminder_sec", 3600),
        **kw,
    )


def _snap(**over):
    doc = {
        "engine": "STOPPED",
        "execution_arm": "DISARMED",
        "PROP_EXECUTION": False,
        "market_data_status": "DISCONNECTED",
        "market_data_quality": "UNKNOWN",
        "market_age_seconds": 126000.0,
        "market_instrument": "NQ 09-26",
        "sim101_recovery": "FLAT_SAFE",
        "telemetry_dump": {"alive": True, "age_sec": 0.2, "timestamp": "2026-08-22T12:00:00Z"},
        "checks": {"safe_start_result": "SAFE_START_FAILED"},
        "decision": {"last_live_signal": None, "signal_source": "NONE"},
        "live_dvp": {"live_signal": None},
        "last_shadow_signal": {
            "direction": "SHORT",
            "source": "phase53_shadow",
            "ts": "2026-08-14T10:45:00-05:00",
            "live_bar": False,
        },
    }
    doc.update(over)
    return doc


class NotificationTests(unittest.TestCase):
    def setUp(self):
        reset_service_for_tests()
        self.sent: list[NotificationEvent] = []

        def backend(ev: NotificationEvent) -> bool:
            self.sent.append(ev)
            return True

        self.backend = backend

    def test_disabled_does_not_send(self):
        svc = _svc(self.backend, enabled=False)
        ok = svc.notify(EventType.TEST, force=True, title="x")
        self.assertFalse(ok)
        self.assertEqual(self.sent, [])
        self.assertEqual(svc.health()["delivery_status"], "DISABLED")

    def test_missing_apprise_url(self):
        with mock.patch.dict(os.environ, {ENV_URL: "", ENV_ENABLED: "true"}, clear=False):
            td = Path(tempfile.mkdtemp())
            svc = NotificationService(
                apprise_url="",
                enabled=True,
                backend=None,
                worker=False,
                state_path=td / "s.json",
                journal_path=td / "n.jsonl",
            )
            self.assertFalse(svc.configured)
            self.assertFalse(svc.notifications_enabled)
            self.assertEqual(svc.health()["delivery_status"], "NOT_CONFIGURED")
            self.assertFalse(svc.notify(EventType.TEST, force=True, title="x"))

    def test_valid_notifier_mock(self):
        svc = _svc(self.backend)
        ok = svc.notify(EventType.TEST, force=True, title="AITRADE TEST", body="NO EXECUTION")
        self.assertTrue(ok)
        self.assertEqual(self.sent[0].event_type, EventType.TEST)

    def test_failed_notifier_does_not_crash(self):
        def boom(_ev):
            raise RuntimeError("telegram timeout")

        svc = _svc(boom)
        self.assertFalse(svc.notify(EventType.TEST, force=True, title="x"))
        self.assertEqual(svc.health()["delivery_status"], "FAILED")
        self.assertIn("timeout", svc.health()["last_failure_reason"])

    def test_secrets_are_masked(self):
        url = "tgram://123456789:AAThisIsAFakeTokenValueForUnitTests/987654321"
        text = mask_secrets(f"url={url} bot123456789:AAThisIsAFakeTokenValueForUnitTests")
        self.assertNotIn("AAThisIsAFakeTokenValueForUnitTests", text)
        self.assertNotIn("987654321", text)
        self.assertIn("tgram://***", text)
        svc = _svc(self.backend)
        h = svc.health()
        blob = str(h)
        self.assertNotIn("tgram://", blob)
        self.assertNotIn("APPRISE_URL", blob)

    def test_stale_does_not_spam(self):
        svc = _svc(self.backend)
        snap = _snap()
        a = svc.observe_snapshot(snap)
        b = svc.observe_snapshot(snap)
        c = svc.observe_snapshot(snap)
        stale = [e for e in (a + b + c) if e == EventType.MARKET_DATA_STALE]
        self.assertEqual(len(stale), 1)

    def test_stale_then_recovered_sends_recovery(self):
        svc = _svc(self.backend)
        svc.observe_snapshot(_snap())
        rec = svc.observe_snapshot(
            _snap(market_data_status="LIVE", market_data_quality="LIVE", market_age_seconds=0.4)
        )
        self.assertIn(EventType.MARKET_DATA_RECOVERED, rec)
        types = [e.event_type for e in self.sent]
        self.assertIn(EventType.MARKET_DATA_STALE, types)
        self.assertIn(EventType.MARKET_DATA_RECOVERED, types)

    def test_dedup_telemetry_vs_market(self):
        svc = _svc(self.backend)
        emitted = svc.observe_snapshot(_snap())
        self.assertIn(EventType.MARKET_DATA_STALE, emitted)
        self.assertNotIn(EventType.TELEMETRY_STALE, emitted)
        self.assertNotIn(EventType.NINJATRADER_DISCONNECTED, emitted)

    def test_planned_engine_stop_not_failure(self):
        svc = _svc(self.backend)
        svc._prev_engine = "RUNNING"
        svc.mark_planned_engine_stop("OPERATOR REQUEST")
        svc.notify(
            EventType.ENGINE_STOP,
            force=True,
            title="ENGINE STOPPED",
            body="INFO · ENGINE STOPPED · OPERATOR REQUEST",
            metadata={"state_value": "STOPPED"},
        )
        out = svc.observe_snapshot(_snap(engine="STOPPED"))
        self.assertNotIn(EventType.ENGINE_UNEXPECTED_EXIT, out)
        self.assertNotIn(EventType.ENGINE_FAILURE, [e.event_type for e in self.sent])
        self.assertEqual(self.sent[0].event_type, EventType.ENGINE_STOP)
        self.assertIn("OPERATOR REQUEST", self.sent[0].body)

    def test_unexpected_engine_exit_critical(self):
        svc = _svc(self.backend)
        svc.observe_snapshot(_snap(engine="RUNNING", checks={"safe_start_result": "ENGINE_MAY_RUN"}))
        out = svc.observe_snapshot(_snap(engine="STOPPED"))
        self.assertIn(EventType.ENGINE_UNEXPECTED_EXIT, out)
        ev = [e for e in self.sent if e.event_type == EventType.ENGINE_UNEXPECTED_EXIT][0]
        self.assertEqual(ev.severity.value, "CRITICAL")

    def test_shadow_cannot_create_live_dvp(self):
        shadow = {
            "direction": "SHORT",
            "source": "phase53_shadow",
            "ts": "2026-08-14T10:45:00-05:00",
            "live_bar": False,
        }
        self.assertFalse(is_genuine_live_dvp(shadow))
        self.assertTrue(is_shadow_observation(shadow))
        svc = _svc(self.backend)
        svc.observe_snapshot(_snap(decision={"last_live_signal": shadow, "last_shadow_signal": shadow}))
        self.assertNotIn(EventType.LIVE_DVP_DETECTED, [e.event_type for e in self.sent])

    def test_historical_cannot_create_live_dvp(self):
        hist = {"direction": "LONG", "source": "HISTORICAL", "live_bar": False, "kind": "HISTORICAL"}
        self.assertFalse(is_genuine_live_dvp(hist))
        svc = _svc(self.backend)
        svc.observe_snapshot(_snap(live_dvp={"live_signal": hist}))
        self.assertNotIn(EventType.LIVE_DVP_DETECTED, [e.event_type for e in self.sent])

    def test_warmup_cannot_create_live_dvp(self):
        warm = {
            "direction": "LONG",
            "source": "HISTORICAL_WARMUP",
            "live_bar": False,
            "executable": False,
            "note": "warmup_or_replay_not_executable",
        }
        self.assertFalse(is_genuine_live_dvp(warm))
        svc = _svc(self.backend)
        svc.observe_snapshot(_snap(decision={"last_live_signal": warm}))
        self.assertNotIn(EventType.LIVE_DVP_DETECTED, [e.event_type for e in self.sent])

    def test_phase54_live_creates_live_dvp(self):
        live = {
            "direction": "LONG",
            "source": "phase54_live",
            "live_bar": True,
            "executable": True,
            "ts": "2026-08-24T14:35:00-04:00",
            "bar_identity": "nq-5m-20260824-1430",
            "signal_id": "dvp-1",
        }
        self.assertTrue(is_genuine_live_dvp(live))
        svc = _svc(self.backend)
        out = svc.observe_snapshot(_snap(decision={"last_live_signal": live, "signal_source": "LIVE"}))
        self.assertIn(EventType.LIVE_DVP_DETECTED, out)
        self.assertEqual(self.sent[-1].provenance, "phase54_live")
        svc.observe_snapshot(_snap(decision={"last_live_signal": live, "signal_source": "LIVE"}))
        self.assertEqual(sum(1 for e in self.sent if e.event_type == EventType.LIVE_DVP_DETECTED), 1)

    def test_sim_only_armed_event_does_not_arm(self):
        self.assertFalse(sim_only_execution_armed())
        svc = _svc(self.backend)
        os.environ.pop("AITRADE_SIM_ONLY_EXECUTION", None)
        svc.observe_snapshot(_snap(execution_arm="SIM_ONLY ARMED"))
        self.assertFalse(sim_only_execution_armed())
        self.assertNotEqual(os.environ.get("AITRADE_SIM_ONLY_EXECUTION"), "1")
        self.assertIn(EventType.SIM_ONLY_ARMED, [e.event_type for e in self.sent])
        self.assertIn("1 MNQ", self.sent[-1].body)
        self.assertIn("PROP_EXECUTION: FALSE", self.sent[-1].body)

    def test_order_event_does_not_submit(self):
        calls = []

        def fake_submit(*_a, **_k):
            calls.append(1)
            raise AssertionError("must not submit")

        with mock.patch("phase55_execution_bridge.NinjaTraderExecutionBridge.submit", fake_submit):
            notify_submit_result(
                {
                    "ok": True,
                    "submitted": True,
                    "transmit": True,
                    "status": "BRACKET_ARMED",
                    "account": "Sim101",
                    "trade_id": "T1",
                    "execution": {"entry_fill": 23450.25, "stop_price": 23420.0, "target_price": 23490.0},
                },
                intent={"direction": "LONG", "instrument": "MNQ 09-26", "quantity": 1, "source": "phase54_live"},
            )
        self.assertEqual(calls, [])

    def test_notification_exception_leaves_flags_unchanged(self):
        def boom(_ev):
            raise RuntimeError("dns")

        self.assertFalse(PROP_EXECUTION)
        self.assertFalse(sim_only_execution_armed())
        svc = _svc(boom)
        svc.notify(EventType.SIM_ONLY_ARMED, force=True, title="x")
        self.assertFalse(PROP_EXECUTION)
        self.assertFalse(sim_only_execution_armed())

    def test_engine_failure_hook(self):
        svc = _svc(self.backend)
        with mock.patch("aitrade_notifications.get_service", return_value=svc):
            from aitrade_notifications import notify_engine_failure

            notify_engine_failure("uncaught boom")
        self.assertEqual(self.sent[0].event_type, EventType.ENGINE_FAILURE)
        self.assertEqual(self.sent[0].severity.value, "CRITICAL")

    def test_engine_start_hook(self):
        svc = _svc(self.backend)
        with mock.patch("aitrade_notifications.get_service", return_value=svc):
            from aitrade_notifications import notify_engine_start

            notify_engine_start()
        self.assertEqual(self.sent[0].event_type, EventType.ENGINE_START)
        self.assertEqual(svc._prev_engine, "RUNNING")

    def test_emergency_flatten_and_execution_failure_hooks(self):
        svc = _svc(self.backend)
        with mock.patch("aitrade_notifications.get_service", return_value=svc):
            from aitrade_notifications import notify_emergency_flatten, notify_execution_failure, notify_position_closed

            notify_emergency_flatten(transmitted=False, detail="REQUESTED_NOT_TRANSMITTED")
            notify_execution_failure("stop_unconfirmed")
            notify_position_closed(reason="flatten", recovery="FLAT_SAFE")
        types = [e.event_type for e in self.sent]
        self.assertIn(EventType.EMERGENCY_FLATTEN, types)
        self.assertIn(EventType.EXECUTION_FAILURE, types)
        self.assertIn(EventType.POSITION_CLOSED, types)
        self.assertEqual(self.sent[0].severity.value, "CRITICAL")

    def test_repo_has_no_inbound_telegram_execution(self):
        src = Path("aitrade_notifications.py").read_text(encoding="utf-8")
        self.assertNotIn("getUpdates", src)
        self.assertIn("outbound-only", src.lower())
        self.assertFalse(inbound_execution_controls_present("AITRADE → Apprise → Telegram → operator"))
        hits = []
        for path in Path(".").glob("*.py"):
            if path.name.startswith("test"):
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            if "getUpdates" in text or "telegram.ext" in text or "Updater(" in text:
                hits.append(path.name)
        self.assertEqual(hits, [])

    def test_safe_start_and_recovery_fixtures(self):
        svc = _svc(self.backend)
        out = svc.observe_snapshot(_snap())
        self.assertIn(EventType.SAFE_START_FAILED, out)
        svc.observe_snapshot(_snap(sim101_recovery="ORPHAN_POSITION"))
        self.assertIn(EventType.RECOVERY_UNSAFE, [e.event_type for e in self.sent])
        rec = svc.observe_snapshot(_snap(sim101_recovery="FLAT_SAFE", checks={"safe_start_result": "ENGINE_MAY_RUN"}))
        self.assertIn(EventType.RECOVERY_FLAT_SAFE, rec)
        self.assertIn(EventType.SAFE_START_RECOVERED, rec)

    def test_nt_disconnect_reconnect(self):
        svc = _svc(self.backend)
        dead = _snap(telemetry_dump={"alive": False, "age_sec": 40.0})
        self.assertIn(EventType.NINJATRADER_DISCONNECTED, svc.observe_snapshot(dead))
        live = _snap(telemetry_dump={"alive": True, "age_sec": 0.2})
        self.assertIn(EventType.NINJATRADER_RECONNECTED, svc.observe_snapshot(live))

    def test_telemetry_stale_recovered(self):
        svc = _svc(self.backend)
        stale = _snap(telemetry_dump={"alive": False, "age_sec": 8.0})
        self.assertIn(EventType.TELEMETRY_STALE, svc.observe_snapshot(stale))
        live = _snap(telemetry_dump={"alive": True, "age_sec": 0.3})
        self.assertIn(EventType.TELEMETRY_RECOVERED, svc.observe_snapshot(live))

    def test_order_rejected_sanitized(self):
        svc = _svc(self.backend)
        reset_service_for_tests()
        with mock.patch("aitrade_notifications.get_service", return_value=svc):
            notify_submit_result(
                {
                    "ok": False,
                    "submitted": False,
                    "transmit": True,
                    "error_code": "LIVE_ACCOUNT_BLOCKED:tgram://111:SECRET/222",
                    "account": "Sim101",
                },
                intent={"direction": "LONG", "quantity": 1},
            )
        ev = [e for e in self.sent if e.event_type == EventType.ORDER_REJECTED][0]
        self.assertNotIn("SECRET", ev.body)

    def test_position_lifecycle_from_submit(self):
        svc = _svc(self.backend)
        with mock.patch("aitrade_notifications.get_service", return_value=svc):
            notify_submit_result(
                {
                    "ok": True,
                    "submitted": True,
                    "transmit": True,
                    "status": "BRACKET_ARMED",
                    "account": "Sim101",
                    "trade_id": "T2",
                    "execution": {"entry_fill": 23450.25, "stop_price": 23420.0, "target_price": 23490.0},
                },
                intent={"direction": "LONG", "instrument": "MNQ 09-26", "quantity": 1, "source": "phase54_live"},
            )
        types = [e.event_type for e in self.sent]
        self.assertIn(EventType.ORDER_SUBMITTED, types)
        self.assertIn(EventType.ORDER_ACCEPTED, types)
        self.assertIn(EventType.POSITION_OPENED, types)
        self.assertIn(EventType.STOP_ACTIVE, types)
        self.assertIn(EventType.TARGET_ACTIVE, types)

    def test_nonblocking_worker(self):
        gate = threading.Event()

        def slow(ev):
            time.sleep(0.4)
            self.sent.append(ev)
            gate.set()
            return True

        svc = _svc(slow, worker=True)
        t0 = time.time()
        ok = svc.notify(EventType.TEST, force=True, title="async")
        elapsed = time.time() - t0
        self.assertTrue(ok)
        self.assertLess(elapsed, 0.25)
        self.assertTrue(gate.wait(2.0))

    def test_frozen_hash_and_flags_untouched(self):
        self.assertEqual(
            NQ_FROZEN_HASH,
            "935e3a616351b09dbfa8d2d0e2b5d6850be803fb385ec5e5f8e3593856a1212a",
        )
        self.assertFalse(PROP_EXECUTION)
        self.assertFalse(sim_only_execution_armed())
        self.assertIn("PROP_EVALUATION", BLOCKED_MODES)


if __name__ == "__main__":
    unittest.main()
