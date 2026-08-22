"""AITRADE notification layer — Apprise/Telegram, outbound-only, fail-isolated.

Never part of the execution control surface. A notify failure cannot arm,
flatten, submit, or change PROP_EXECUTION / SIM_ONLY / FundedNext routing.
"""
from __future__ import annotations

import json
import os
import queue
import re
import threading
import time
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Optional

ROOT = Path(__file__).resolve().parent

ENV_URL = "AITRADE_APPRISE_URL"
ENV_ENABLED = "AITRADE_NOTIFICATIONS_ENABLED"
ENV_SHADOW = "AITRADE_NOTIFY_SHADOW"
ENV_REMINDER = "AITRADE_NOTIFY_REMINDER_SEC"
ENV_STATE = "AITRADE_NOTIFY_STATE"
ENV_JOURNAL = "AITRADE_NOTIFY_JOURNAL"
ENV_SYNC = "AITRADE_NOTIFY_SYNC"

LIVE_PROVENANCE = "phase54_live"
QTY_CAP_MNQ = 1
NT_DISCONNECT_SEC = 15.0
TELEMETRY_STALE_SEC = 5.0
DEFAULT_REMINDER_SEC = 6 * 3600.0

_TRUTHY = {"1", "true", "yes", "on"}
_FALSY = {"0", "false", "no", "off"}


class Severity(str, Enum):
    INFO = "INFO"
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"


class EventType(str, Enum):
    ENGINE_START = "ENGINE_START"
    ENGINE_STOP = "ENGINE_STOP"
    ENGINE_FAILURE = "ENGINE_FAILURE"
    ENGINE_UNEXPECTED_EXIT = "ENGINE_UNEXPECTED_EXIT"
    NINJATRADER_DISCONNECTED = "NINJATRADER_DISCONNECTED"
    NINJATRADER_RECONNECTED = "NINJATRADER_RECONNECTED"
    TELEMETRY_STALE = "TELEMETRY_STALE"
    TELEMETRY_RECOVERED = "TELEMETRY_RECOVERED"
    MARKET_DATA_STALE = "MARKET_DATA_STALE"
    MARKET_DATA_RECOVERED = "MARKET_DATA_RECOVERED"
    SAFE_START_FAILED = "SAFE_START_FAILED"
    SAFE_START_RECOVERED = "SAFE_START_RECOVERED"
    RECOVERY_UNSAFE = "RECOVERY_UNSAFE"
    RECOVERY_FLAT_SAFE = "RECOVERY_FLAT_SAFE"
    LIVE_DVP_DETECTED = "LIVE_DVP_DETECTED"
    SHADOW_SIGNAL = "SHADOW_SIGNAL"
    SIM_ONLY_ARMED = "SIM_ONLY_ARMED"
    SIM_ONLY_DISARMED = "SIM_ONLY_DISARMED"
    PROP_CANARY_READY = "PROP_CANARY_READY"
    PROP_CANARY_ARMED = "PROP_CANARY_ARMED"
    PROP_CANARY_DISARMED = "PROP_CANARY_DISARMED"
    PROP_CANARY_BLOCKED = "PROP_CANARY_BLOCKED"
    UNATTENDED_PREFLIGHT_PASS = "UNATTENDED_PREFLIGHT_PASS"
    UNATTENDED_PREFLIGHT_FAIL = "UNATTENDED_PREFLIGHT_FAIL"
    LIVE_BAR_VALIDATION_PASS = "LIVE_BAR_VALIDATION_PASS"
    UNATTENDED_WAITING_DVP = "UNATTENDED_WAITING_DVP"
    UNATTENDED_BLOCKED = "UNATTENDED_BLOCKED"
    UNATTENDED_DVP_DETECTED = "UNATTENDED_DVP_DETECTED"
    UNATTENDED_ORDER_SUBMITTED = "UNATTENDED_ORDER_SUBMITTED"
    UNATTENDED_ORDER_ACCEPTED = "UNATTENDED_ORDER_ACCEPTED"
    UNATTENDED_ORDER_REJECTED = "UNATTENDED_ORDER_REJECTED"
    UNATTENDED_POSITION_OPENED = "UNATTENDED_POSITION_OPENED"
    UNATTENDED_STOP_CONFIRMED = "UNATTENDED_STOP_CONFIRMED"
    UNATTENDED_TARGET_CONFIRMED = "UNATTENDED_TARGET_CONFIRMED"
    UNATTENDED_ENGINE_LOST_POSITION_OPEN = "UNATTENDED_ENGINE_LOST_POSITION_OPEN"
    UNATTENDED_PROTECTION_FAILURE = "UNATTENDED_PROTECTION_FAILURE"
    UNATTENDED_POSITION_CLOSED = "UNATTENDED_POSITION_CLOSED"
    UNATTENDED_COMPLETE = "UNATTENDED_COMPLETE"
    UNATTENDED_COMPLETE_NO_TRADE = "UNATTENDED_COMPLETE_NO_TRADE"
    ORDER_SUBMITTED = "ORDER_SUBMITTED"
    ORDER_ACCEPTED = "ORDER_ACCEPTED"
    ORDER_REJECTED = "ORDER_REJECTED"
    POSITION_OPENED = "POSITION_OPENED"
    STOP_ACTIVE = "STOP_ACTIVE"
    TARGET_ACTIVE = "TARGET_ACTIVE"
    POSITION_CLOSED = "POSITION_CLOSED"
    EXECUTION_FAILURE = "EXECUTION_FAILURE"
    EMERGENCY_FLATTEN = "EMERGENCY_FLATTEN"
    TEST = "TEST"


_SEVERITY = {
    EventType.ENGINE_START: Severity.INFO,
    EventType.ENGINE_STOP: Severity.INFO,
    EventType.ENGINE_FAILURE: Severity.CRITICAL,
    EventType.ENGINE_UNEXPECTED_EXIT: Severity.CRITICAL,
    EventType.NINJATRADER_DISCONNECTED: Severity.CRITICAL,
    EventType.NINJATRADER_RECONNECTED: Severity.INFO,
    EventType.TELEMETRY_STALE: Severity.WARNING,
    EventType.TELEMETRY_RECOVERED: Severity.INFO,
    EventType.MARKET_DATA_STALE: Severity.WARNING,
    EventType.MARKET_DATA_RECOVERED: Severity.INFO,
    EventType.SAFE_START_FAILED: Severity.WARNING,
    EventType.SAFE_START_RECOVERED: Severity.INFO,
    EventType.RECOVERY_UNSAFE: Severity.WARNING,
    EventType.RECOVERY_FLAT_SAFE: Severity.INFO,
    EventType.LIVE_DVP_DETECTED: Severity.INFO,
    EventType.SHADOW_SIGNAL: Severity.INFO,
    EventType.SIM_ONLY_ARMED: Severity.WARNING,
    EventType.SIM_ONLY_DISARMED: Severity.INFO,
    EventType.PROP_CANARY_READY: Severity.INFO,
    EventType.PROP_CANARY_ARMED: Severity.WARNING,
    EventType.PROP_CANARY_DISARMED: Severity.INFO,
    EventType.PROP_CANARY_BLOCKED: Severity.WARNING,
    EventType.UNATTENDED_PREFLIGHT_PASS: Severity.INFO,
    EventType.UNATTENDED_PREFLIGHT_FAIL: Severity.WARNING,
    EventType.LIVE_BAR_VALIDATION_PASS: Severity.INFO,
    EventType.UNATTENDED_WAITING_DVP: Severity.INFO,
    EventType.UNATTENDED_BLOCKED: Severity.WARNING,
    EventType.UNATTENDED_DVP_DETECTED: Severity.INFO,
    EventType.UNATTENDED_ORDER_SUBMITTED: Severity.INFO,
    EventType.UNATTENDED_ORDER_ACCEPTED: Severity.INFO,
    EventType.UNATTENDED_ORDER_REJECTED: Severity.WARNING,
    EventType.UNATTENDED_POSITION_OPENED: Severity.INFO,
    EventType.UNATTENDED_STOP_CONFIRMED: Severity.INFO,
    EventType.UNATTENDED_TARGET_CONFIRMED: Severity.INFO,
    EventType.UNATTENDED_ENGINE_LOST_POSITION_OPEN: Severity.CRITICAL,
    EventType.UNATTENDED_PROTECTION_FAILURE: Severity.CRITICAL,
    EventType.UNATTENDED_POSITION_CLOSED: Severity.INFO,
    EventType.UNATTENDED_COMPLETE: Severity.INFO,
    EventType.UNATTENDED_COMPLETE_NO_TRADE: Severity.INFO,
    EventType.ORDER_SUBMITTED: Severity.INFO,
    EventType.ORDER_ACCEPTED: Severity.INFO,
    EventType.ORDER_REJECTED: Severity.WARNING,
    EventType.POSITION_OPENED: Severity.INFO,
    EventType.STOP_ACTIVE: Severity.INFO,
    EventType.TARGET_ACTIVE: Severity.INFO,
    EventType.POSITION_CLOSED: Severity.INFO,
    EventType.EXECUTION_FAILURE: Severity.CRITICAL,
    EventType.EMERGENCY_FLATTEN: Severity.CRITICAL,
    EventType.TEST: Severity.INFO,
}

_EMOJI = {
    Severity.INFO: "ℹ️",
    Severity.WARNING: "⚠️",
    Severity.CRITICAL: "🚨",
}
_EMOJI_EVENT = {
    EventType.LIVE_DVP_DETECTED: "📈",
    EventType.POSITION_OPENED: "📈",
    EventType.POSITION_CLOSED: "✅",
    EventType.SHADOW_SIGNAL: "👁️",
    EventType.TEST: "🧪",
}

_STATE_FAMILIES = {
    EventType.NINJATRADER_DISCONNECTED: "ninjatrader",
    EventType.NINJATRADER_RECONNECTED: "ninjatrader",
    EventType.TELEMETRY_STALE: "telemetry",
    EventType.TELEMETRY_RECOVERED: "telemetry",
    EventType.MARKET_DATA_STALE: "market_data",
    EventType.MARKET_DATA_RECOVERED: "market_data",
    EventType.SAFE_START_FAILED: "safe_start",
    EventType.SAFE_START_RECOVERED: "safe_start",
    EventType.RECOVERY_UNSAFE: "recovery",
    EventType.RECOVERY_FLAT_SAFE: "recovery",
    EventType.SIM_ONLY_ARMED: "sim_only_arm",
    EventType.SIM_ONLY_DISARMED: "sim_only_arm",
    EventType.PROP_CANARY_READY: "prop_canary",
    EventType.PROP_CANARY_ARMED: "prop_canary",
    EventType.PROP_CANARY_DISARMED: "prop_canary",
    EventType.PROP_CANARY_BLOCKED: "prop_canary",
    EventType.UNATTENDED_PREFLIGHT_PASS: "unattended",
    EventType.UNATTENDED_PREFLIGHT_FAIL: "unattended",
    EventType.LIVE_BAR_VALIDATION_PASS: "unattended",
    EventType.UNATTENDED_WAITING_DVP: "unattended",
    EventType.UNATTENDED_BLOCKED: "unattended",
    EventType.UNATTENDED_COMPLETE: "unattended",
    EventType.UNATTENDED_COMPLETE_NO_TRADE: "unattended",
    EventType.ENGINE_START: "engine",
    EventType.ENGINE_STOP: "engine",
    EventType.ENGINE_FAILURE: "engine",
    EventType.ENGINE_UNEXPECTED_EXIT: "engine",
}

_STALE_EVENTS = {
    EventType.NINJATRADER_DISCONNECTED,
    EventType.TELEMETRY_STALE,
    EventType.MARKET_DATA_STALE,
    EventType.SAFE_START_FAILED,
    EventType.RECOVERY_UNSAFE,
    EventType.SIM_ONLY_ARMED,
    EventType.PROP_CANARY_ARMED,
    EventType.PROP_CANARY_BLOCKED,
    EventType.UNATTENDED_BLOCKED,
}

# Healthy/recovery alerts: record baseline on first observe, do not announce "recovered".
_BOOT_SUPPRESS = {
    EventType.NINJATRADER_RECONNECTED,
    EventType.TELEMETRY_RECOVERED,
    EventType.MARKET_DATA_RECOVERED,
    EventType.SAFE_START_RECOVERED,
    EventType.RECOVERY_FLAT_SAFE,
    EventType.SIM_ONLY_DISARMED,
    EventType.PROP_CANARY_DISARMED,
    EventType.PROP_CANARY_READY,
}


def _load_dotenv() -> None:
    env_path = ROOT / ".env"
    if not env_path.exists():
        return
    try:
        from dotenv import load_dotenv

        load_dotenv(env_path, override=False)
    except Exception:
        return


def _truthy(name: str, default: Optional[bool] = None) -> Optional[bool]:
    raw = os.environ.get(name)
    if raw is None or str(raw).strip() == "":
        return default
    v = str(raw).strip().lower()
    if v in _TRUTHY:
        return True
    if v in _FALSY:
        return False
    return default


def mask_secrets(text: Any) -> str:
    """Mask bot tokens, Apprise URLs, and credential-bearing query params."""
    if text is None:
        return ""
    s = str(text)
    s = re.sub(r"(?i)((?:tgram|telegram|tgrams|mailto|slack|discord):)//[^\s]+", r"\1//***", s)
    s = re.sub(r"(?i)https?://api\.telegram\.org/bot[^\s/]+", "https://api.telegram.org/bot***", s)
    s = re.sub(r"bot\d+:[A-Za-z0-9_-]+", "bot***:***", s)
    s = re.sub(r"(?i)(token|secret|password|apprise_url|chat_id|bot_key)=([^\s&]+)", r"\1=***", s)
    s = re.sub(r"(?i)(access_token|refresh_token|authorization)([\"':=\s]+)[^\s\"']+", r"\1\2***", s)
    return s


def _iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _local_stamp() -> str:
    try:
        from zoneinfo import ZoneInfo

        now = datetime.now(ZoneInfo("Europe/Berlin"))
        return now.strftime("%Y-%m-%d %H:%M %Z")
    except Exception:
        now = datetime.now().astimezone()
        return now.strftime("%Y-%m-%d %H:%M %Z")


def is_genuine_live_dvp(sig: Optional[dict[str, Any]]) -> bool:
    """True only for canonical live provenance. Shadow/warmup/history never qualify."""
    if not isinstance(sig, dict) or not sig:
        return False
    source = str(sig.get("source") or "")
    if source != LIVE_PROVENANCE:
        return False
    if sig.get("live_bar") is False:
        return False
    if sig.get("executable") is False:
        return False
    kind = str(sig.get("kind") or "").upper()
    if kind in {"SHADOW", "HISTORICAL", "WARMUP", "NONE"}:
        return False
    note = str(sig.get("note") or "")
    if "warmup" in note.lower() or "replay" in note.lower() or "not_executable" in note.lower():
        return False
    return bool(sig.get("direction"))


def is_shadow_observation(sig: Optional[dict[str, Any]]) -> bool:
    if not isinstance(sig, dict) or not sig:
        return False
    if is_genuine_live_dvp(sig):
        return False
    source = str(sig.get("source") or "")
    if source in {"phase53_shadow", "HISTORICAL_WARMUP", "HISTORICAL", "SHADOW"}:
        return True
    if sig.get("live_bar") is False:
        return True
    return False


def live_dvp_identity(sig: dict[str, Any]) -> str:
    return str(
        sig.get("signal_id")
        or sig.get("bar_identity")
        or "|".join(
            [
                str(sig.get("source") or ""),
                str(sig.get("direction") or ""),
                str(sig.get("ts") or sig.get("trading_date") or ""),
                str(sig.get("intended_entry") or ""),
            ]
        )
    )


@dataclass
class NotificationEvent:
    event_type: EventType
    severity: Severity
    title: str
    body: str
    timestamp: str = field(default_factory=_iso)
    source: str = "aitrade"
    instrument: Optional[str] = None
    account: Optional[str] = None
    provenance: Optional[str] = None
    event_id: str = field(default_factory=lambda: uuid.uuid4().hex[:16])
    metadata: dict[str, Any] = field(default_factory=dict)
    process: str = "trading_engine"

    def to_public_dict(self) -> dict[str, Any]:
        row = asdict(self)
        row["event_type"] = self.event_type.value
        row["severity"] = self.severity.value
        row["title"] = mask_secrets(self.title)
        row["body"] = mask_secrets(self.body)
        meta = {}
        for k, v in (self.metadata or {}).items():
            lk = str(k).lower()
            if any(s in lk for s in ("token", "secret", "password", "apprise", "chat_id", "bot")):
                continue
            meta[k] = v
        row["metadata"] = meta
        return row


def format_telegram(event: NotificationEvent) -> str:
    emoji = _EMOJI_EVENT.get(event.event_type) or _EMOJI.get(event.severity, "ℹ️")
    lines = [f"{emoji} AITRADE · {event.title}"]
    if event.body:
        lines.append("")
        lines.append(event.body.strip())
    lines.append("")
    lines.append(_local_stamp())
    return mask_secrets("\n".join(lines).strip())


def _engine_stop_body(*, reason: str) -> str:
    return (
        f"INFO · ENGINE STOPPED · {reason}\n"
        "Execution: DISARMED\n"
        "PROP_EXECUTION: FALSE\n"
        "Process: trading_engine (not ops-console, not NinjaTrader)"
    )


def build_event(event_type: EventType, *, title: Optional[str] = None, body: str = "", **fields: Any) -> NotificationEvent:
    sev = _SEVERITY[event_type]
    return NotificationEvent(
        event_type=event_type,
        severity=sev,
        title=title or event_type.value.replace("_", " "),
        body=body,
        source=str(fields.pop("source", "aitrade")),
        instrument=fields.pop("instrument", None),
        account=fields.pop("account", None),
        provenance=fields.pop("provenance", None),
        process=str(fields.pop("process", "trading_engine")),
        metadata=fields.pop("metadata", None) or fields,
    )


class NotificationService:
    """Queue + worker. emit() never raises into the caller."""

    def __init__(
        self,
        *,
        apprise_url: Optional[str] = None,
        enabled: Optional[bool] = None,
        backend: Optional[Callable[[NotificationEvent], bool]] = None,
        state_path: Optional[Path] = None,
        journal_path: Optional[Path] = None,
        worker: Optional[bool] = None,
        reminder_sec: Optional[float] = None,
        now_fn: Callable[[], float] = time.time,
    ) -> None:
        _load_dotenv()
        test_mode = os.environ.get("AITRADE_PHASE54_TEST") == "1" and os.environ.get("AITRADE_NOTIFY_TEST") != "1"
        self._url = apprise_url if apprise_url is not None else (os.environ.get(ENV_URL) or "").strip()
        if test_mode and backend is None:
            self._url = ""
        explicit = _truthy(ENV_ENABLED)
        if enabled is not None:
            self._enabled_flag = bool(enabled)
        elif test_mode and backend is None:
            self._enabled_flag = False
        elif explicit is True:
            self._enabled_flag = True
        else:
            self._enabled_flag = False
        self._backend = backend
        self._now = now_fn
        self._reminder = float(
            reminder_sec
            if reminder_sec is not None
            else (os.environ.get(ENV_REMINDER) or DEFAULT_REMINDER_SEC)
        )
        journal_env = os.environ.get("AITRADE_PHASE54_JOURNAL")
        default_journal_dir = Path(journal_env) if journal_env else (ROOT / "journal" / "phase54_ops")
        self._journal = Path(journal_path) if journal_path else Path(
            os.environ.get(ENV_JOURNAL) or (default_journal_dir / "notifications.jsonl")
        )
        default_state = ROOT / "state" / "aitrade_notifications.json"
        if journal_env:
            default_state = Path(journal_env) / "notify_state.json"
        self._state_path = Path(state_path) if state_path else Path(
            os.environ.get(ENV_STATE) or default_state
        )
        self._state = self._load_state()
        self._lock = threading.Lock()
        self._q: queue.Queue[Optional[NotificationEvent]] = queue.Queue()
        self._health: dict[str, Any] = {
            "enabled": self.notifications_enabled,
            "backend": "APPRISE",
            "configured": self.configured,
            "delivery_status": "NOT_CONFIGURED" if not self.configured else ("DISABLED" if not self.notifications_enabled else "READY"),
            "last_attempt_ts": None,
            "last_success_ts": None,
            "last_failure_ts": None,
            "last_failure_reason": None,
            "last_event_type": None,
            "channel": "TELEGRAM" if self.configured else None,
        }
        sync_env = _truthy(ENV_SYNC, False)
        self._use_worker = (not sync_env) if worker is None else bool(worker)
        self._thread: Optional[threading.Thread] = None
        self._planned_stop: Optional[str] = None
        self._prev_engine: Optional[str] = None
        if self._use_worker:
            self._thread = threading.Thread(target=self._worker, name="aitrade-notify", daemon=True)
            self._thread.start()

    @property
    def configured(self) -> bool:
        return bool(self._url) or self._backend is not None

    @property
    def notifications_enabled(self) -> bool:
        return bool(self._enabled_flag) and self.configured

    def health(self) -> dict[str, Any]:
        with self._lock:
            h = dict(self._health)
        h["enabled"] = self.notifications_enabled
        h["configured"] = self.configured
        h["backend"] = "APPRISE"
        if not self.configured:
            h["delivery_status"] = "NOT_CONFIGURED"
        elif not self._enabled_flag:
            h["delivery_status"] = "DISABLED"
        return h

    def mark_planned_engine_stop(self, reason: str = "OPERATOR REQUEST") -> None:
        self._planned_stop = reason

    def consume_planned_engine_stop(self) -> Optional[str]:
        reason = self._planned_stop
        self._planned_stop = None
        return reason

    def emit(self, event: NotificationEvent, *, force: bool = False) -> bool:
        """Enqueue (or deliver sync). Never raises. Never mutates execution state."""
        try:
            if not self.notifications_enabled:
                return False
            if not force and not self._should_send(event):
                return False
            if self._use_worker:
                self._q.put(event)
                return True
            return self._deliver(event)
        except Exception as exc:
            self._record_failure(event, exc)
            return False

    def notify(self, event_type: EventType, force: bool = False, **kwargs: Any) -> bool:
        try:
            return self.emit(build_event(event_type, **kwargs), force=force)
        except Exception:
            return False

    def _should_send(self, event: NotificationEvent) -> bool:
        family = _STATE_FAMILIES.get(event.event_type)
        if event.event_type == EventType.LIVE_DVP_DETECTED:
            ident = str((event.metadata or {}).get("identity") or event.event_id)
            last = (self._state.get("live_dvp") or {}).get("identity")
            if last == ident:
                return False
            self._state.setdefault("live_dvp", {})["identity"] = ident
            self._save_state()
            return True
        if event.event_type == EventType.SHADOW_SIGNAL:
            ident = str((event.metadata or {}).get("identity") or event.event_id)
            last = (self._state.get("shadow") or {}).get("identity")
            if last == ident:
                return False
            self._state.setdefault("shadow", {})["identity"] = ident
            self._save_state()
            return True
        if not family:
            return True
        value = str((event.metadata or {}).get("state_value") or event.event_type.value)
        slot = self._state.setdefault("families", {}).setdefault(family, {})
        prev = slot.get("value")
        last_ts = float(slot.get("last_emit_ts") or 0)
        now = self._now()
        if prev is None and event.event_type in _BOOT_SUPPRESS:
            slot["value"] = value
            slot["last_event"] = event.event_type.value
            self._save_state()
            return False
        if prev == value:
            if event.event_type in _STALE_EVENTS and last_ts > 0 and self._reminder > 0 and (now - last_ts) >= self._reminder:
                slot["last_emit_ts"] = now
                slot["last_event"] = event.event_type.value
                self._save_state()
                return True
            return False
        slot["value"] = value
        slot["last_emit_ts"] = now
        slot["last_event"] = event.event_type.value
        self._save_state()
        return True

    def _worker(self) -> None:
        while True:
            item = self._q.get()
            if item is None:
                return
            try:
                self._deliver(item)
            except Exception:
                pass

    def _deliver(self, event: NotificationEvent) -> bool:
        self._record_attempt(event)
        try:
            ok = self._backend(event) if self._backend is not None else self._apprise_send(event)
            if ok:
                self._record_success(event)
                self._journal_attempt(event, ok=True, reason=None, delivered=True)
                return True
            self._record_failure(event, "apprise_returned_false")
            self._journal_attempt(event, ok=False, reason="apprise_returned_false", delivered=False)
            return False
        except Exception as exc:
            self._record_failure(event, exc)
            self._journal_attempt(event, ok=False, reason=mask_secrets(exc), delivered=False)
            return False

    def _apprise_send(self, event: NotificationEvent) -> bool:
        if not self._url:
            return False
        try:
            import apprise
        except Exception as exc:
            raise RuntimeError("apprise_import_failed") from exc
        app = apprise.Apprise()
        if not app.add(self._url):
            raise RuntimeError("apprise_url_rejected")
        body = format_telegram(event)
        notify_type = {
            Severity.INFO: apprise.NotifyType.INFO,
            Severity.WARNING: apprise.NotifyType.WARNING,
            Severity.CRITICAL: apprise.NotifyType.FAILURE,
        }.get(event.severity, apprise.NotifyType.INFO)
        return bool(app.notify(body=body, title="AITRADE", notify_type=notify_type))

    def _record_attempt(self, event: NotificationEvent) -> None:
        with self._lock:
            self._health["last_attempt_ts"] = _iso()
            self._health["last_event_type"] = event.event_type.value
            self._health["delivery_status"] = "SENDING"

    def _record_success(self, event: NotificationEvent) -> None:
        with self._lock:
            self._health["last_success_ts"] = _iso()
            self._health["last_event_type"] = event.event_type.value
            self._health["delivery_status"] = "HEALTHY"
            self._health["last_failure_reason"] = None

    def _record_failure(self, event: Optional[NotificationEvent], exc: Any) -> None:
        with self._lock:
            self._health["last_failure_ts"] = _iso()
            self._health["last_failure_reason"] = mask_secrets(exc)
            self._health["delivery_status"] = "FAILED"
            if event is not None:
                self._health["last_event_type"] = event.event_type.value

    def _journal_attempt(self, event: NotificationEvent, *, ok: bool, reason: Optional[str], delivered: bool) -> None:
        try:
            self._journal.parent.mkdir(parents=True, exist_ok=True)
            row = {
                "ts": _iso(),
                "event_id": event.event_id,
                "event_type": event.event_type.value,
                "severity": event.severity.value,
                "destination": "APPRISE/TELEGRAM",
                "success": bool(ok),
                "delivered": bool(delivered),
                "failure_reason": mask_secrets(reason) if reason else None,
                "process": event.process,
            }
            with self._journal.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, default=str) + "\n")
        except Exception:
            return

    def _load_state(self) -> dict[str, Any]:
        try:
            if self._state_path.exists():
                doc = json.loads(self._state_path.read_text(encoding="utf-8"))
                if isinstance(doc, dict):
                    return doc
        except Exception:
            pass
        return {"families": {}}

    def _save_state(self) -> None:
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._state_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._state, indent=2, default=str), encoding="utf-8")
            tmp.replace(self._state_path)
        except Exception:
            return

    def observe_snapshot(self, snap: dict[str, Any]) -> list[EventType]:
        """Transition alerts from a read-only snapshot. Never mutates snap/execution."""
        emitted: list[EventType] = []
        try:
            emitted.extend(self._observe_engine(snap))
            emitted.extend(self._observe_connectivity(snap))
            emitted.extend(self._observe_safe_recovery(snap))
            emitted.extend(self._observe_arm(snap))
            emitted.extend(self._observe_prop_canary(snap))
            emitted.extend(self._observe_unattended(snap))
            emitted.extend(self._observe_signals(snap))
        except Exception:
            return emitted
        return emitted

    def _observe_engine(self, snap: dict[str, Any]) -> list[EventType]:
        out: list[EventType] = []
        cur = str(snap.get("engine") or "STOPPED")
        prev = self._prev_engine
        self._prev_engine = cur
        if prev == "RUNNING" and cur == "STOPPED":
            planned = self.consume_planned_engine_stop()
            if planned:
                return out
            if self.notify(
                EventType.ENGINE_UNEXPECTED_EXIT,
                title="ENGINE UNEXPECTED EXIT",
                body="CRITICAL · trading_engine left RUNNING without operator stop\nProcess: trading_engine",
                metadata={"state_value": "UNEXPECTED_EXIT"},
                process="trading_engine",
            ):
                out.append(EventType.ENGINE_UNEXPECTED_EXIT)
        return out

    def _observe_connectivity(self, snap: dict[str, Any]) -> list[EventType]:
        out: list[EventType] = []
        dump = snap.get("telemetry_dump") if isinstance(snap.get("telemetry_dump"), dict) else {}
        age = dump.get("age_sec")
        try:
            age_f = float(age) if age is not None else None
        except (TypeError, ValueError):
            age_f = None
        dump_alive = bool(dump.get("alive")) or (age_f is not None and age_f <= TELEMETRY_STALE_SEC)
        nt_down = (age_f is None and not dump_alive) or (age_f is not None and age_f > NT_DISCONNECT_SEC)
        tel_stale = (not nt_down) and (age_f is not None and age_f > TELEMETRY_STALE_SEC)

        if nt_down:
            if self.notify(
                EventType.NINJATRADER_DISCONNECTED,
                title="NINJATRADER DISCONNECTED",
                body=self._ctx(snap, extra=f"Dump age: {age_f if age_f is not None else 'unknown'}s"),
                metadata={"state_value": "DISCONNECTED"},
                process="ninjatrader",
            ):
                out.append(EventType.NINJATRADER_DISCONNECTED)
        else:
            if self.notify(
                EventType.NINJATRADER_RECONNECTED,
                title="NINJATRADER RECONNECTED",
                body=self._ctx(snap, extra="Telemetry dump alive"),
                metadata={"state_value": "CONNECTED"},
                process="ninjatrader",
            ):
                out.append(EventType.NINJATRADER_RECONNECTED)

        if tel_stale:
            if self.notify(
                EventType.TELEMETRY_STALE,
                title="TELEMETRY STALE",
                body=self._ctx(snap, extra=f"Dump age: {age_f:.1f}s"),
                metadata={"state_value": "STALE"},
            ):
                out.append(EventType.TELEMETRY_STALE)
        elif dump_alive:
            if self.notify(
                EventType.TELEMETRY_RECOVERED,
                title="TELEMETRY RECOVERED",
                body=self._ctx(snap, extra=f"Dump age: {age_f:.1f}s" if age_f is not None else "Dump alive"),
                metadata={"state_value": "LIVE"},
            ):
                out.append(EventType.TELEMETRY_RECOVERED)

        md = str(snap.get("market_data_status") or "")
        q = str(snap.get("market_data_quality") or "")
        md_live = md == "LIVE" and q.upper() in {"LIVE", ""}
        md_stale = md in {"STALE", "DISCONNECTED", "CONNECTED_STALE"} or (md == "LIVE" and not md_live)
        quote_age = snap.get("market_age_seconds")
        if md_stale:
            extra = f"Quote age: {quote_age:,.0f}s" if isinstance(quote_age, (int, float)) else f"Status: {md}"
            extra += "\nTelemetry dump: " + ("alive" if dump_alive else "stale")
            extra += "\nNot SYSTEM LIVE — quotes/session are not fresh"
            if self.notify(
                EventType.MARKET_DATA_STALE,
                title="MARKET DATA STALE",
                body=self._ctx(snap, extra=extra),
                instrument=snap.get("market_instrument") or "NQ 09-26",
                metadata={"state_value": "STALE"},
            ):
                out.append(EventType.MARKET_DATA_STALE)
        elif md_live:
            if self.notify(
                EventType.MARKET_DATA_RECOVERED,
                title="MARKET DATA RECOVERED",
                body=self._ctx(snap, extra="Quotes LIVE"),
                metadata={"state_value": "LIVE"},
            ):
                out.append(EventType.MARKET_DATA_RECOVERED)
        return out

    def _observe_safe_recovery(self, snap: dict[str, Any]) -> list[EventType]:
        out: list[EventType] = []
        ss = str((snap.get("checks") or {}).get("safe_start_result") or "")
        if ss == "SAFE_START_FAILED":
            if self.notify(
                EventType.SAFE_START_FAILED,
                title="SAFE START FAILED",
                body=self._ctx(snap, extra="Engine may not start. Orders remain blocked."),
                metadata={"state_value": "FAILED"},
            ):
                out.append(EventType.SAFE_START_FAILED)
        elif ss == "ENGINE_MAY_RUN":
            if self.notify(
                EventType.SAFE_START_RECOVERED,
                title="SAFE START RECOVERED",
                body=self._ctx(snap, extra="ENGINE_MAY_RUN · orders still may not"),
                metadata={"state_value": "OK"},
            ):
                out.append(EventType.SAFE_START_RECOVERED)

        rec = str(snap.get("sim101_recovery") or "")
        if rec and rec != "FLAT_SAFE":
            if self.notify(
                EventType.RECOVERY_UNSAFE,
                title="RECOVERY UNSAFE",
                body=self._ctx(snap, extra=f"Recovery: {rec}\nAccount: Sim101"),
                account="Sim101",
                metadata={"state_value": rec},
            ):
                out.append(EventType.RECOVERY_UNSAFE)
        elif rec == "FLAT_SAFE":
            if self.notify(
                EventType.RECOVERY_FLAT_SAFE,
                title="RECOVERY FLAT_SAFE",
                body=self._ctx(snap, extra="Sim101 FLAT_SAFE"),
                account="Sim101",
                metadata={"state_value": "FLAT_SAFE"},
            ):
                out.append(EventType.RECOVERY_FLAT_SAFE)
        return out

    def _observe_arm(self, snap: dict[str, Any]) -> list[EventType]:
        out: list[EventType] = []
        arm = str(snap.get("execution_arm") or "DISARMED")
        armed = "ARMED" in arm and "DISARMED" not in arm
        if armed:
            body = (
                self._ctx(snap)
                + "\nAccount: Sim101"
                + f"\nQuantity cap: {QTY_CAP_MNQ} MNQ"
                + "\nFundedNext remains READ_ONLY"
                + "\nPROP_EXECUTION: FALSE"
            )
            if self.notify(
                EventType.SIM_ONLY_ARMED,
                title="SIM_ONLY ARMED",
                body=body,
                account="Sim101",
                metadata={"state_value": "ARMED"},
            ):
                out.append(EventType.SIM_ONLY_ARMED)
        else:
            if self.notify(
                EventType.SIM_ONLY_DISARMED,
                title="SIM_ONLY DISARMED",
                body=self._ctx(snap, extra="SIM_ONLY DISARMED · PROP_EXECUTION FALSE"),
                account="Sim101",
                metadata={"state_value": "DISARMED"},
            ):
                out.append(EventType.SIM_ONLY_DISARMED)
        return out

    def _observe_prop_canary(self, snap: dict[str, Any]) -> list[EventType]:
        out: list[EventType] = []
        pc = snap.get("prop_canary") if isinstance(snap.get("prop_canary"), dict) else {}
        state = str(pc.get("state") or pc.get("label") or "")
        if not state:
            return out
        extra = (
            f"GENERAL PROP: LOCKED\n"
            f"CANARY: {state}\n"
            f"Account: {pc.get('account') or 'FNFTCHTANATSWAPHILMU92044'}\n"
            f"Qty cap: {QTY_CAP_MNQ} MNQ\n"
            f"PROP_EXECUTION: FALSE"
        )
        mapping = {
            "PROP_CANARY_READY": EventType.PROP_CANARY_READY,
            "PROP_CANARY_ARMED": EventType.PROP_CANARY_ARMED,
            "PROP_CANARY_DISARMED": EventType.PROP_CANARY_DISARMED,
            "PROP_LOCKED": EventType.PROP_CANARY_DISARMED,
            "PROP_CANARY_COMPLETE": EventType.PROP_CANARY_DISARMED,
            "PROP_CANARY_BLOCKED": EventType.PROP_CANARY_BLOCKED,
        }
        et = mapping.get(state)
        if et is None:
            return out
        title = state.replace("_", " ")
        if self.notify(
            et,
            title=title,
            body=self._ctx(snap, extra=extra),
            account=str(pc.get("account") or "FNFTCHTANATSWAPHILMU92044"),
            metadata={"state_value": state, "route": "PROP_CANARY"},
        ):
            out.append(et)
        return out

    def _observe_unattended(self, snap: dict[str, Any]) -> list[EventType]:
        out: list[EventType] = []
        u = snap.get("unattended") if isinstance(snap.get("unattended"), dict) else {}
        state = str(u.get("state") or "")
        if not state:
            return out
        mapping = {
            "UNATTENDED_WAITING_DVP": EventType.UNATTENDED_WAITING_DVP,
            "UNATTENDED_BLOCKED": EventType.UNATTENDED_BLOCKED,
            "UNATTENDED_BLOCKED_RESTART": EventType.UNATTENDED_BLOCKED,
            "UNATTENDED_COMPLETE": EventType.UNATTENDED_COMPLETE,
            "UNATTENDED_COMPLETE_NO_TRADE": EventType.UNATTENDED_COMPLETE_NO_TRADE,
        }
        et = mapping.get(state)
        if et is None:
            return out
        extra = (
            f"FUNDEDNEXT UNATTENDED CANARY\n"
            f"State: {state}\n"
            f"Daily attempt used: {u.get('daily_attempt_used')}\n"
            f"GENERAL PROP: LOCKED\nAccount: {u.get('account') or 'FNFTCHTANATSWAPHILMU92044'}"
        )
        if self.notify(
            et,
            title=state.replace("_", " "),
            body=self._ctx(snap, extra=extra),
            account=str(u.get("account") or "FNFTCHTANATSWAPHILMU92044"),
            metadata={"state_value": state, "route": "UNATTENDED_PROP_CANARY"},
        ):
            out.append(et)
        return out

    def _observe_signals(self, snap: dict[str, Any]) -> list[EventType]:
        out: list[EventType] = []
        d = snap.get("decision") if isinstance(snap.get("decision"), dict) else {}
        live = d.get("last_live_signal") or (snap.get("live_dvp") or {}).get("live_signal")
        if is_genuine_live_dvp(live if isinstance(live, dict) else None):
            ident = live_dvp_identity(live)
            body = (
                f"{live.get('direction')}\n"
                f"Source: {LIVE_PROVENANCE}\n"
                f"{live.get('bar_identity') or live.get('ts') or ''}"
            )
            if self.notify(
                EventType.LIVE_DVP_DETECTED,
                title="LIVE DVP DETECTED",
                body=self._ctx(snap, extra=body),
                provenance=LIVE_PROVENANCE,
                instrument="NQ 09-26",
                metadata={"identity": ident, "state_value": ident},
            ):
                out.append(EventType.LIVE_DVP_DETECTED)
        if _truthy(ENV_SHADOW, False):
            shadow = d.get("last_shadow_signal") or snap.get("last_shadow_signal")
            if is_shadow_observation(shadow if isinstance(shadow, dict) else None):
                ident = live_dvp_identity(shadow)
                if self.notify(
                    EventType.SHADOW_SIGNAL,
                    title="SHADOW · NON-EXECUTABLE",
                    body=f"{shadow.get('direction')} · {shadow.get('source') or 'shadow'}\nNot a live DVP",
                    provenance=str(shadow.get("source") or "shadow"),
                    metadata={"identity": ident},
                ):
                    out.append(EventType.SHADOW_SIGNAL)
        return out

    def _ctx(self, snap: dict[str, Any], extra: str = "") -> str:
        md = snap.get("market_data_status") or "—"
        eng = snap.get("engine") or "—"
        arm = snap.get("execution_arm") or "DISARMED"
        lines = []
        if extra:
            lines.append(extra)
        lines.extend(
            [
                f"Engine: {eng}",
                f"Execution: {arm}",
                "PROP_EXECUTION: FALSE",
                f"Market: {md}",
            ]
        )
        instr = snap.get("market_instrument")
        if instr:
            lines.insert(0, str(instr))
        return "\n".join(lines)


_SERVICE: Optional[NotificationService] = None
_SERVICE_LOCK = threading.Lock()


def get_service() -> NotificationService:
    global _SERVICE
    with _SERVICE_LOCK:
        if _SERVICE is None:
            _SERVICE = NotificationService()
        return _SERVICE


def reset_service_for_tests() -> None:
    global _SERVICE
    with _SERVICE_LOCK:
        _SERVICE = None


def notify_safe(event_type: EventType, **kwargs: Any) -> bool:
    try:
        return get_service().notify(event_type, **kwargs)
    except Exception:
        return False


def observe_snapshot_safe(snap: dict[str, Any]) -> None:
    try:
        get_service().observe_snapshot(snap)
    except Exception:
        return


def notification_health() -> dict[str, Any]:
    try:
        return get_service().health()
    except Exception:
        return {
            "enabled": False,
            "backend": "APPRISE",
            "configured": False,
            "delivery_status": "UNAVAILABLE",
            "last_failure_reason": "health_error",
        }


def notify_engine_start() -> None:
    try:
        svc = get_service()
        svc._prev_engine = "RUNNING"
        svc.notify(
            EventType.ENGINE_START,
            force=True,
            title="ENGINE STARTED",
            body="Engine: RUNNING\nExecution: DISARMED\nPROP_EXECUTION: FALSE\nSIM_ONLY not auto-armed",
            metadata={"state_value": "RUNNING"},
            process="trading_engine",
        )
    except Exception:
        return


def notify_engine_stop_planned(reason: str = "OPERATOR REQUEST") -> None:
    try:
        svc = get_service()
        svc.mark_planned_engine_stop(reason)
        svc.notify(
            EventType.ENGINE_STOP,
            force=True,
            title="ENGINE STOPPED",
            body=_engine_stop_body(reason=reason),
            metadata={"state_value": "STOPPED"},
            process="trading_engine",
        )
    except Exception:
        return


def notify_engine_failure(detail: str) -> None:
    try:
        get_service().notify(
            EventType.ENGINE_FAILURE,
            force=True,
            title="ENGINE FAILURE",
            body=mask_secrets(detail) + "\nPROP_EXECUTION: FALSE",
            metadata={"state_value": "FAILURE"},
            process="trading_engine",
        )
    except Exception:
        return


def notify_emergency_flatten(*, transmitted: bool, detail: str = "") -> None:
    try:
        get_service().mark_planned_engine_stop("EMERGENCY FLATTEN")
    except Exception:
        pass
    try:
        get_service().notify(
            EventType.EMERGENCY_FLATTEN,
            force=True,
            title="EMERGENCY FLATTEN",
            body=(
                f"Transmitted: {transmitted}\n{mask_secrets(detail)}\n"
                "Account: Sim101\nPROP_EXECUTION: FALSE\nFundedNext: READ_ONLY"
            ),
            account="Sim101",
            process="trading_engine",
        )
    except Exception:
        return


def notify_execution_failure(detail: str, **meta: Any) -> None:
    notify_safe(
        EventType.EXECUTION_FAILURE,
        title="EXECUTION FAILURE",
        body=mask_secrets(detail) + "\nPROP_EXECUTION: FALSE\nAccount: Sim101",
        account="Sim101",
        metadata=meta,
    )


def notify_submit_result(result: dict[str, Any], *, intent: Optional[dict[str, Any]] = None) -> None:
    """Map a Sim101 submit result to order/position events. Does not submit orders."""
    try:
        intent = intent or {}
        account = str(result.get("account") or intent.get("account") or "Sim101")
        instrument = str(intent.get("instrument") or "MNQ 09-26")
        side = str(intent.get("direction") or result.get("direction") or "")
        qty = int(intent.get("quantity") or result.get("quantity") or QTY_CAP_MNQ)
        provenance = str(intent.get("source") or intent.get("provenance") or LIVE_PROVENANCE)
        corr = str(result.get("trade_id") or intent.get("trade_id") or "")
        submitted = bool(result.get("submitted"))
        ok = bool(result.get("ok"))
        status = str(result.get("status") or "")
        exec_out = result.get("execution") if isinstance(result.get("execution"), dict) else {}
        if submitted:
            notify_safe(
                EventType.ORDER_SUBMITTED,
                title="ORDER SUBMITTED",
                body=f"{account} · {instrument}\n{side} · {qty} MNQ\nSource: {provenance}\nID: {corr}",
                account=account,
                instrument=instrument,
                provenance=provenance,
                metadata={"correlation_id": corr, "order_type": "BRACKET"},
            )
        if submitted and ok and status in {"BRACKET_ARMED", "SUBMITTED", "ACCEPTED"}:
            notify_safe(
                EventType.ORDER_ACCEPTED,
                title="ORDER ACCEPTED",
                body=f"{account} · {instrument}\n{side} · {qty} MNQ\nStatus: {status}\nID: {corr}",
                account=account,
                instrument=instrument,
                provenance=provenance,
            )
            entry = exec_out.get("entry_fill") or exec_out.get("entry_price")
            notify_safe(
                EventType.POSITION_OPENED,
                title="POSITION OPENED",
                body=(
                    f"{account} · {instrument}\n{side} · {qty} MNQ\n"
                    + (f"Entry: {entry}\n" if entry is not None else "")
                    + f"Source: {provenance}"
                ),
                account=account,
                instrument=instrument,
                provenance=provenance,
            )
            stop_px = exec_out.get("stop_price")
            if stop_px is not None:
                notify_safe(
                    EventType.STOP_ACTIVE,
                    title="STOP ACTIVE",
                    body=f"Stop: {stop_px}\nQuantity protected: {qty}\nStatus: {exec_out.get('stop_status') or 'ARMED'}",
                    account=account,
                    instrument=instrument,
                )
            tgt_px = exec_out.get("target_price")
            if tgt_px is not None:
                notify_safe(
                    EventType.TARGET_ACTIVE,
                    title="TARGET ACTIVE",
                    body=f"Target: {tgt_px}\nQuantity: {qty}\nStatus: {exec_out.get('target_status') or 'ARMED'}",
                    account=account,
                    instrument=instrument,
                )
        elif (result.get("transmit") or submitted) and (not submitted or not ok):
                reason = mask_secrets(result.get("error_code") or status or "REJECTED")
                notify_safe(
                    EventType.ORDER_REJECTED,
                    title="ORDER REJECTED",
                    body=f"{account} · {instrument}\n{side} · {qty} MNQ\nReason: {reason}",
                    account=account,
                    instrument=instrument,
                    provenance=provenance,
                )
    except Exception:
        return


def notify_position_closed(
    *,
    instrument: str = "MNQ 09-26",
    account: str = "Sim101",
    reason: Optional[str] = None,
    exit_price: Any = None,
    realized_pnl: Any = None,
    duration: Any = None,
    recovery: Optional[str] = None,
) -> None:
    lines = [f"{account} · {instrument}"]
    if reason:
        lines.append(f"Exit reason: {reason}")
    if exit_price is not None:
        lines.append(f"Exit: {exit_price}")
    if realized_pnl is not None:
        lines.append(f"Realized: {realized_pnl}")
    if duration is not None:
        lines.append(f"Duration: {duration}")
    if recovery:
        lines.append(f"Recovery after close: {recovery}")
    notify_safe(
        EventType.POSITION_CLOSED,
        title="POSITION CLOSED",
        body="\n".join(lines),
        account=account,
        instrument=instrument,
    )


def send_harmless_test() -> dict[str, Any]:
    """One-shot delivery validation. No broker commands."""
    ev = build_event(
        EventType.TEST,
        title="AITRADE TEST",
        body="Apprise/Telegram delivery validation\nNO EXECUTION\nPROP_EXECUTION=false\nSIM_ONLY=DISARMED",
        process="notification_service",
    )
    ok = get_service().emit(ev, force=True)
    return {"ok": ok, "event_id": ev.event_id, "PROP_EXECUTION": False}


def inbound_execution_controls_present(source_text: str) -> bool:
    """True if text looks like Telegram→AITRADE execution control. Used by tests."""
    lowered = source_text.lower()
    needles = (
        "getupdates",
        "/startengine",
        "/stopengine",
        "/arm",
        "/trade",
        "/flatten",
        "/risk",
        "/prop",
        "telegram command",
        "bot command handler",
        "polling",
    )
    if "command polling" in lowered:
        return True
    return any(n in lowered for n in needles if n != "polling") and (
        "telegram" in lowered or "tgram" in lowered or "getupdates" in lowered
    )
