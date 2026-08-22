"""Phase 55A — SIM_ONLY bridge from Phase 54 approved intent to Phase 31 ATI.

Reuses ``nq_dvp_nt_exec.submit_dvp_bracket`` / ``flatten_dvp_owned`` and
``nt_ati.flatten_sim``. Does not implement PLACE/CANCEL/CLOSEPOSITION itself.
Never enables PROP_EXECUTION. Never targets FundedNext.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

import nt_ati as nt
from execution_instrument import (
    EXEC_INSTRUMENT_DISPLAY,
    EXEC_INSTRUMENT_NT,
    InstrumentError,
    parse_execution_instrument,
)
from execution_status import (
    BLOCKED_MODES,
    NQ_FROZEN_HASH,
    assert_execution_allowed,
    sim_only_execution_armed,
)
from nq_dvp_live_signal import STALE_5M_SECONDS
from nq_dvp_nt_exec import (
    EXEC_ACCOUNT,
    flatten_dvp_owned,
    frozen_risk_for_direction,
    submit_dvp_bracket,
)

ROOT = Path(__file__).resolve().parent
DEFAULT_STATE_PATH = ROOT / "state" / "phase55_sim_only.json"
FN_EVAL_ACCOUNT = "FNFTCHTANATSWAPHILMU92044"
ALLOWED_STRATEGY_IDS = frozenset({
    "NQ_DRIFT_VWAP_PULLBACK",
    "NQ DVP",
    "nq_drift_vwap_pullback_v1.DVP_ORIGINAL.FROZEN_PHASE30",
})
PHASE_55A_MAX_QTY = 1
RECOVERY_FLAT_SAFE = "FLAT_SAFE"
RECOVERY_ACTIVE = "ACTIVE_TRADE_RECOVERED"
RECOVERY_ORPHAN_POSITION = "ORPHAN_POSITION"
RECOVERY_ORPHAN_ORDER = "ORPHAN_ORDER"
RECOVERY_UNPROTECTED = "UNPROTECTED_POSITION"
RECOVERY_UNKNOWN = "UNKNOWN_STATE"
RECOVERY_CORRUPT = "CORRUPT_STATE"
ALLOW_NEW_ENTRIES = frozenset({RECOVERY_FLAT_SAFE})


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _state_path() -> Path:
    override = os.environ.get("AITRADE_PHASE55_STATE")
    if override:
        return Path(override)
    return DEFAULT_STATE_PATH


def _default_state() -> dict[str, Any]:
    return {
        "schema": "AITRADE_PHASE55A_SIM_ONLY_V1",
        "PROP_EXECUTION": False,
        "mode": "SIM_ONLY",
        "recovery": RECOVERY_UNKNOWN,
        "entries_blocked": True,
        "disconnect": False,
        "halt_reason": None,
        "seen_triggers": [],
        "attempted_triggers": [],
        "open_trade": None,
        "last_submit": None,
        "last_flatten": None,
        "updated_at": _utc_iso(),
    }


def default_parse_sim101_position(*, account: str = EXEC_ACCOUNT, instrument: Any = None, **_k: Any) -> dict[str, Any]:
    """NT Sim101 account position first. ATI log is diagnostic fallback. Never FundedNext."""
    from sim101_telemetry import (
        fundednext_must_not_substitute,
        merge_ati_fallback,
        parse_sim101_position,
    )

    rt = None
    try:
        from nt_readonly import NTReadOnly

        rt = NTReadOnly().runtime_snapshot()
    except Exception:
        rt = None
    mtime = None
    if isinstance(rt, dict):
        mtime = rt.get("_mtime")
    primary = parse_sim101_position(rt, dump_mtime=mtime)
    primary = fundednext_must_not_substitute(rt, primary)
    ati = None
    try:
        ati = nt.parse_mnq_sim_position(account=account, instrument=instrument or EXEC_INSTRUMENT_NT)
    except Exception as exc:
        ati = {"flat": None, "known": False, "error": str(exc), "source": "ati_error"}
    return merge_ati_fallback(primary, ati)


class NinjaTraderExecutionBridge:
    """Single submit boundary: policy-approved intent → existing Sim101 ATI stack."""

    executable_accounts = frozenset({EXEC_ACCOUNT})
    blocked_accounts = frozenset({FN_EVAL_ACCOUNT})

    def __init__(
        self,
        *,
        state_path: Optional[Path] = None,
        parse_position: Optional[Callable[..., dict[str, Any]]] = None,
        detect_orphans: Optional[Callable[..., dict[str, Any]]] = None,
        submit_bracket: Optional[Callable[..., dict[str, Any]]] = None,
        flatten_owned: Optional[Callable[..., dict[str, Any]]] = None,
        flatten_sim_fn: Optional[Callable[..., dict[str, Any]]] = None,
        journal: Optional[Callable[..., None]] = None,
        prop_execution: bool = False,
    ) -> None:
        self.state_path = Path(state_path) if state_path else _state_path()
        self._parse_position = parse_position or default_parse_sim101_position
        self._detect_orphans = detect_orphans or nt.detect_orphan_aitrade_orders
        self._submit_bracket = submit_bracket or submit_dvp_bracket
        self._flatten_owned = flatten_owned or flatten_dvp_owned
        self._flatten_sim = flatten_sim_fn or nt.flatten_sim
        self._journal = journal
        self._prop_execution = bool(prop_execution)

    def _load(self) -> dict[str, Any]:
        if not self.state_path.exists():
            return _default_state()
        try:
            doc = json.loads(self.state_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            doc = _default_state()
            doc["recovery"] = RECOVERY_CORRUPT
            doc["entries_blocked"] = True
            doc["halt_reason"] = "CORRUPT_STATE"
            return doc
        if not isinstance(doc, dict):
            doc = _default_state()
            doc["recovery"] = RECOVERY_CORRUPT
            doc["entries_blocked"] = True
            doc["halt_reason"] = "CORRUPT_STATE"
            return doc
        doc["PROP_EXECUTION"] = False
        doc.setdefault("seen_triggers", [])
        doc.setdefault("attempted_triggers", [])
        return doc

    def _save(self, doc: dict[str, Any]) -> dict[str, Any]:
        doc["PROP_EXECUTION"] = False
        doc["updated_at"] = _utc_iso()
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps(doc, indent=2, default=str), encoding="utf-8")
        return doc

    def _log(self, event: str, **extra: Any) -> None:
        if self._journal:
            self._journal(event=event, **extra)

    def _position(self) -> dict[str, Any]:
        pos = self._parse_position(account=EXEC_ACCOUNT, instrument=EXEC_INSTRUMENT_NT)
        if pos.get("known") is False or pos.get("stale") is True:
            pos = dict(pos)
            pos["flat"] = None
        return pos

    def preflight(self, intent: dict[str, Any]) -> dict[str, Any]:
        """All gates. Must pass before ``drop_oif`` may be reached."""
        st = self._load()
        if (
            str(st.get("recovery") or RECOVERY_UNKNOWN) in {RECOVERY_UNKNOWN, ""}
            and not st.get("disconnect")
            and st.get("halt_reason") != "CORRUPT_STATE"
            and st.get("recovery") != RECOVERY_CORRUPT
        ):
            self.reconcile()

        gates: dict[str, str] = {}
        errors: list[str] = []

        def fail(code: str, gate: str) -> None:
            gates[gate] = "FAIL"
            errors.append(code)

        def pass_gate(gate: str) -> None:
            gates[gate] = "PASS"

        if self._prop_execution:
            fail("PROP_EXECUTION_FORBIDDEN_PHASE55A", "prop_execution")
        else:
            pass_gate("prop_execution")

        mode = str(intent.get("mode") or "SIM_ONLY").upper()
        if mode in BLOCKED_MODES:
            fail(f"EXECUTION_MODE_BLOCKED:{mode}", "mode")
        elif mode != "SIM_ONLY":
            fail("SIM_ONLY_REQUIRED", "mode")
        else:
            pass_gate("mode")

        verdict = str(intent.get("policy_verdict") or "").upper()
        if verdict not in ("ALLOW", "APPROVED"):
            fail("POLICY_NOT_APPROVED", "policy")
        else:
            pass_gate("policy")

        if intent.get("news_blocked") or "NEWS" in str(intent.get("policy_code") or "").upper():
            fail("NEWS_BLACKOUT_VIOLATION_RISK", "news")
        else:
            pass_gate("news")

        cal = str(intent.get("calendar_status") or "OK").upper()
        if cal in ("FAIL_SAFE", "LOCK", "BLACKOUT", "MISSING"):
            fail("NEWS_BLACKOUT_VIOLATION_RISK", "news")

        if intent.get("prop_blocked") or str(intent.get("policy_code") or "").upper() in {
            "PROP_RULE_DATA_MISSING",
            "BLOCK_TRADING_HOURS",
            "ACCOUNT_BREACH_IMMINENT",
        }:
            fail(str(intent.get("policy_code") or "PROP_RULE_BLOCK"), "prop_rule")
        else:
            pass_gate("prop_rule")

        account = str(intent.get("account") or "").strip()
        if account in self.blocked_accounts or account != EXEC_ACCOUNT:
            fail(f"LIVE_ACCOUNT_BLOCKED:{account or 'missing'}", "account")
        else:
            pass_gate("account")

        strategy = str(intent.get("strategy_id") or "").strip()
        if strategy not in ALLOWED_STRATEGY_IDS:
            fail("STRATEGY_NOT_ALLOWLISTED", "strategy")
        else:
            pass_gate("strategy")

        strategy_hash = str(intent.get("strategy_hash") or "")
        if strategy_hash != NQ_FROZEN_HASH:
            fail("STRATEGY_HASH_MISMATCH", "strategy_hash")
        else:
            pass_gate("strategy_hash")

        try:
            inst = parse_execution_instrument(str(intent.get("instrument") or EXEC_INSTRUMENT_NT))
            if inst.ninjatrader_oif() != EXEC_INSTRUMENT_NT:
                fail("REFUSED_UNSUPPORTED_INSTRUMENT", "instrument")
            else:
                pass_gate("instrument")
        except (InstrumentError, PermissionError) as exc:
            fail(str(exc), "instrument")
            inst = None

        try:
            qty = int(intent.get("quantity") or 0)
        except (TypeError, ValueError):
            qty = 0
        if qty != PHASE_55A_MAX_QTY:
            fail("PHASE_55A_QTY_CAP", "quantity")
        else:
            pass_gate("quantity")
        pass_gate("risk_lane")  # 55A executes 1 MNQ only; FAST 2 is not transmitted

        age = intent.get("data_age_sec")
        try:
            age_f = float(age) if age is not None else None
        except (TypeError, ValueError):
            age_f = None
        if intent.get("stale") or (age_f is not None and age_f > STALE_5M_SECONDS):
            fail("STALE_DATA_BLOCK", "stale")
        else:
            pass_gate("stale")

        trigger = str(intent.get("trigger_key") or intent.get("trade_id") or "")
        st = self._load()
        seen = set(st.get("seen_triggers") or []) | set(st.get("attempted_triggers") or [])
        if intent.get("duplicate") or (trigger and trigger in seen):
            fail("DUPLICATE_ORDER_DETECTED", "duplicate")
        else:
            pass_gate("duplicate")

        if not intent.get("nt_connected", True):
            fail("CONNECTION_STATE_BLOCK", "ninjatrader")
        else:
            pass_gate("ninjatrader")

        if st.get("disconnect"):
            fail("CONNECTION_STATE_BLOCK", "ninjatrader")

        recovery = str(st.get("recovery") or RECOVERY_UNKNOWN)
        if recovery not in ALLOW_NEW_ENTRIES or st.get("disconnect"):
            fail(str(st.get("halt_reason") or "RECOVERY_BLOCKS_ENTRIES"), "recovery")
        else:
            pass_gate("recovery")

        direction = str(intent.get("direction") or "").upper()
        if direction not in ("LONG", "SHORT"):
            fail("DIRECTION_REQUIRED", "direction")
        else:
            pass_gate("direction")

        pos = intent.get("position")
        if pos is None:
            try:
                pos = self._position()
            except Exception:
                pos = {"flat": None, "error": "POSITION_UNKNOWN"}
        if pos.get("flat") is not True:
            fail("POSITION_STATE_UNSAFE", "position")
        else:
            pass_gate("position")

        armed_ok = True
        if intent.get("require_armed", True):
            try:
                assert_execution_allowed(requested_mode="SIM_ONLY", sim_enable=True)
                pass_gate("sim_only_armed")
            except PermissionError as exc:
                fail(str(exc), "sim_only_armed")
                armed_ok = False
        else:
            pass_gate("sim_only_armed")

        ok = not errors
        trade_id = str(intent.get("trade_id") or "").strip()
        if ok and not trade_id:
            trade_id = f"AITRADE_DVP_{direction}_{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}"

        return {
            "ok": ok,
            "error_code": errors[0] if errors else None,
            "errors": errors,
            "gates": gates,
            "account": EXEC_ACCOUNT,
            "instrument_nt": EXEC_INSTRUMENT_NT,
            "instrument_display": EXEC_INSTRUMENT_DISPLAY,
            "quantity": PHASE_55A_MAX_QTY,
            "direction": direction,
            "trade_id": trade_id,
            "trigger_key": trigger,
            "position": pos,
            "recovery": recovery,
            "sim_only_armed": armed_ok and sim_only_execution_armed(),
            "PROP_EXECUTION": False,
        }

    def submit(self, intent: dict[str, Any], *, transmit: bool = False) -> dict[str, Any]:
        """Convert approved intent into ``submit_dvp_bracket``. ``transmit=False`` never writes OIF."""
        payload = dict(intent)
        payload["require_armed"] = bool(transmit)
        pre = self.preflight(payload)
        out: dict[str, Any] = {
            "ok": False,
            "submitted": False,
            "transmit": bool(transmit),
            "preflight": pre,
            "PROP_EXECUTION": False,
            "account": EXEC_ACCOUNT,
            "status": "BLOCKED",
        }
        if not pre["ok"]:
            out["error_code"] = pre["error_code"]
            out["status"] = pre["error_code"]
            self._log("SUBMIT_BLOCKED", error_code=pre["error_code"], gates=pre["gates"])
            try:
                from aitrade_notifications import notify_submit_result

                notify_submit_result(out, intent=payload)
            except Exception:
                pass
            return out

        direction = pre["direction"]
        trade_id = pre["trade_id"]
        stop_pts, tgt_pts = frozen_risk_for_direction(direction)
        if not transmit:
            plan = self._submit_bracket(
                direction=direction,
                trade_id=trade_id,
                stop_points=stop_pts,
                target_points=tgt_pts,
                submit=False,
            )
            out.update({"ok": True, "status": "SIM_ONLY_PLAN", "execution": plan, "stop_points": stop_pts, "target_points": tgt_pts})
            return out

        st = self._load()
        trigger = pre.get("trigger_key") or trade_id
        attempted = list(st.get("attempted_triggers") or [])
        if trigger and trigger not in attempted:
            attempted.append(trigger)
        st["attempted_triggers"] = attempted[-500:]
        self._save(st)

        exec_out = self._submit_bracket(
            direction=direction,
            trade_id=trade_id,
            stop_points=stop_pts,
            target_points=tgt_pts,
            submit=True,
        )
        submitted = bool(exec_out.get("submitted"))
        st = self._load()
        if submitted:
            seen = list(st.get("seen_triggers") or [])
            if trigger and trigger not in seen:
                seen.append(trigger)
            st["seen_triggers"] = seen[-500:]
        if exec_out.get("ok") and exec_out.get("status") == "BRACKET_ARMED":
            st["open_trade"] = {
                "trade_id": trade_id,
                "direction": direction,
                "account": EXEC_ACCOUNT,
                "instrument": EXEC_INSTRUMENT_NT,
                "quantity": 1,
                "entry_fill": exec_out.get("entry_fill"),
                "stop_price": exec_out.get("stop_price"),
                "target_price": exec_out.get("target_price"),
                "entry_order_id": exec_out.get("entry_order_id"),
                "stop_order_id": exec_out.get("stop_order_id"),
                "target_order_id": exec_out.get("target_order_id"),
                "oco_id": exec_out.get("oco_id"),
                "nt_entry_order_id": exec_out.get("nt_entry_order_id"),
            }
            st["recovery"] = RECOVERY_ACTIVE
            st["entries_blocked"] = True
        st["last_submit"] = {
            "ts": _utc_iso(),
            "status": exec_out.get("status"),
            "submitted": submitted,
            "trade_id": trade_id,
        }
        self._save(st)
        self._log(
            "SUBMIT_RESULT",
            status=exec_out.get("status"),
            submitted=submitted,
            account=EXEC_ACCOUNT,
            trade_id=trade_id,
        )
        out.update(
            {
                "ok": bool(exec_out.get("ok")),
                "submitted": submitted,
                "status": exec_out.get("status") or "SUBMITTED",
                "execution": exec_out,
                "stop_points": stop_pts,
                "target_points": tgt_pts,
            }
        )
        if not exec_out.get("ok"):
            out["error_code"] = exec_out.get("error_code") or exec_out.get("status")
        try:
            from aitrade_notifications import notify_execution_failure, notify_submit_result

            notify_submit_result(out, intent=payload)
            if transmit and not out.get("ok"):
                notify_execution_failure(str(out.get("error_code") or out.get("status") or "submit_failed"))
        except Exception:
            pass
        return out

    def reconcile(self, *, flatten_unprotected: bool = False) -> dict[str, Any]:
        """Startup / reconnect recovery. Blocks new entries until FLAT_SAFE."""
        st = self._load()
        if st.get("recovery") == RECOVERY_CORRUPT or st.get("halt_reason") == "CORRUPT_STATE":
            st["entries_blocked"] = True
            st["recovery"] = RECOVERY_CORRUPT
            self._save(st)
            return {
                "ok": False,
                "status": RECOVERY_CORRUPT,
                "entries_blocked": True,
                "halt": True,
                "PROP_EXECUTION": False,
            }

        try:
            pos = self._position()
        except Exception as exc:
            st["recovery"] = RECOVERY_UNKNOWN
            st["entries_blocked"] = True
            st["halt_reason"] = "POSITION_UNKNOWN"
            self._save(st)
            return {
                "ok": False,
                "status": RECOVERY_UNKNOWN,
                "entries_blocked": True,
                "halt": True,
                "error": str(exc),
                "PROP_EXECUTION": False,
            }

        open_trade = st.get("open_trade") or {}
        order_ids = [
            x
            for x in (
                open_trade.get("nt_entry_order_id"),
                open_trade.get("entry_order_id"),
                open_trade.get("stop_order_id"),
                open_trade.get("target_order_id"),
            )
            if x
        ]
        orphans = self._detect_orphans(order_ids, oco_id=open_trade.get("oco_id"))
        live_orders = int(orphans.get("orphan_count") or 0) > 0 or int(orphans.get("oco_live_count") or 0) > 0
        protected = int(orphans.get("oco_live_count") or 0) >= 1
        flat = pos.get("flat") is True
        unknown = pos.get("flat") is None
        halt = False
        status = RECOVERY_UNKNOWN

        if unknown:
            status = RECOVERY_UNKNOWN
            halt = True
        elif flat and not live_orders and not open_trade:
            status = RECOVERY_FLAT_SAFE
        elif flat and live_orders:
            status = RECOVERY_ORPHAN_ORDER
            halt = True
        elif not flat and protected and open_trade:
            status = RECOVERY_ACTIVE
        elif not flat and open_trade and not protected:
            status = RECOVERY_UNPROTECTED
            halt = True
            if flatten_unprotected and sim_only_execution_armed():
                flat_out = self.emergency_flatten(account=EXEC_ACCOUNT, transmit=True)
                pos = self._position()
                return {
                    "ok": bool(pos.get("flat")),
                    "status": status,
                    "flatten": flat_out,
                    "position": pos,
                    "entries_blocked": True,
                    "halt": True,
                    "PROP_EXECUTION": False,
                }
        elif not flat and not open_trade:
            status = RECOVERY_ORPHAN_POSITION
            halt = True
        else:
            status = RECOVERY_UNKNOWN
            halt = True

        st["recovery"] = status
        st["entries_blocked"] = status != RECOVERY_FLAT_SAFE or bool(st.get("disconnect"))
        if halt:
            st["halt_reason"] = st.get("halt_reason") or status
        elif status == RECOVERY_FLAT_SAFE:
            st["halt_reason"] = None
            st["open_trade"] = None
        self._save(st)
        return {
            "ok": status in (RECOVERY_FLAT_SAFE, RECOVERY_ACTIVE) and not unknown,
            "status": status,
            "position": pos,
            "orphans": orphans,
            "entries_blocked": st["entries_blocked"],
            "halt": halt,
            "open_trade": st.get("open_trade"),
            "PROP_EXECUTION": False,
        }

    def notify_disconnect(self) -> dict[str, Any]:
        st = self._load()
        st["disconnect"] = True
        st["entries_blocked"] = True
        self._save(st)
        self._log("NT_DISCONNECT", entries_blocked=True)
        return {"ok": True, "disconnect": True, "entries_blocked": True, "status": "CONNECTION_STATE_BLOCK"}

    def notify_reconnect(self) -> dict[str, Any]:
        st = self._load()
        st["disconnect"] = False
        self._save(st)
        rec = self.reconcile()
        if rec.get("status") not in (RECOVERY_FLAT_SAFE, RECOVERY_ACTIVE):
            st = self._load()
            st["halt_reason"] = "RECONNECT_UNSAFE"
            st["entries_blocked"] = True
            self._save(st)
            rec["halt"] = True
            rec["ok"] = False
            rec["error_code"] = "RECONNECT_UNSAFE"
        self._log("NT_RECONNECT", recovery=rec.get("status"), halt=rec.get("halt"))
        return rec

    def emergency_flatten(
        self,
        *,
        account: str = EXEC_ACCOUNT,
        transmit: bool = False,
        active: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        """Sim101-only flatten via existing ATI CLOSEPOSITION. FundedNext remains blocked."""
        requested = {
            "ts": _utc_iso(),
            "account": account,
            "transmit": bool(transmit),
            "PROP_EXECUTION": False,
        }
        if account in self.blocked_accounts or account != EXEC_ACCOUNT:
            out = {
                "ok": False,
                "submitted": False,
                "flatten": "NOT_TRANSMITTED",
                "error_code": f"LIVE_ACCOUNT_BLOCKED:{account}",
                "orders_transmitted": 0,
                **requested,
            }
            self._log("FLATTEN_BLOCKED", **out)
            return out

        owned = active if active is not None else (self._load().get("open_trade") or {})
        if not transmit:
            planned = self._flatten_owned(owned, submit=False) if owned else self._flatten_sim(submit=False)
            return {
                "ok": True,
                "submitted": False,
                "flatten": "PLANNED",
                "orders_transmitted": 0,
                "plan": planned,
                **requested,
            }

        if not sim_only_execution_armed():
            out = {
                "ok": True,
                "submitted": False,
                "flatten": "REQUESTED_NOT_TRANSMITTED",
                "orders_transmitted": 0,
                **requested,
            }
            self._log("FLATTEN_REQUESTED_NOT_TRANSMITTED", **out)
            return out

        if owned:
            result = self._flatten_owned(owned, submit=True)
        else:
            result = self._flatten_sim(submit=True)
        pos = self._position()
        confirmed = bool(pos.get("flat")) and result.get("status") in ("FLATTENED",) or (
            bool(pos.get("flat")) and bool(result.get("submitted"))
        )
        status = "FLATTENED" if pos.get("flat") else "MANUAL_FLATTEN_REQUIRED"
        st = self._load()
        st["last_flatten"] = {
            "ts": _utc_iso(),
            "requested": True,
            "transmitted": bool(result.get("submitted")),
            "ack": result.get("wait") or result.get("drop"),
            "position": pos,
            "status": status,
        }
        if pos.get("flat"):
            st["open_trade"] = None
            st["recovery"] = RECOVERY_FLAT_SAFE
            st["entries_blocked"] = bool(st.get("disconnect"))
            st["halt_reason"] = None
        else:
            st["halt_reason"] = "FLATTEN_UNCONFIRMED"
            st["entries_blocked"] = True
        self._save(st)
        self._log(
            "FLATTEN_RESULT",
            flatten=status,
            transmitted=bool(result.get("submitted")),
            confirmed=bool(pos.get("flat")),
        )
        try:
            from aitrade_notifications import notify_execution_failure, notify_position_closed

            if pos.get("flat"):
                notify_position_closed(
                    account=EXEC_ACCOUNT,
                    reason="flatten",
                    recovery=RECOVERY_FLAT_SAFE if pos.get("flat") else None,
                )
            else:
                notify_execution_failure("flatten_unconfirmed")
        except Exception:
            pass
        return {
            "ok": bool(pos.get("flat")),
            "submitted": bool(result.get("submitted")),
            "flatten": status if pos.get("flat") else "TRANSMITTED_UNCONFIRMED",
            "orders_transmitted": 1 if result.get("submitted") else 0,
            "broker_ack": result.get("wait") or result.get("drop"),
            "position_after": pos,
            "result": result,
            "confirmed": bool(pos.get("flat")),
            **requested,
        }


def sim_only_bridge(*, state_path: Optional[Path] = None) -> NinjaTraderExecutionBridge:
    return NinjaTraderExecutionBridge(state_path=state_path)
