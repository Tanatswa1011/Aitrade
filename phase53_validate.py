"""Phase 53 — pre-purchase FundedNext Flex 50K shadow rehearsal. DRY_RUN. No account purchase."""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from datetime import date as dcls
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from macro_calendar import EVENTS_PATH, load_events
from nq_drift_vwap_engine import index_bars_by_ny_date, replay_dvp_day, trading_dates_ny
from nq_dvp_freeze import load_frozen_strategy_config
from nq_dvp_live_signal import extract_signal_entries_for_day
from phase34_validate import file_sha256
from phase49_trade_audit import write_csv
from phase52_policy import CHICAGO, NY, chicago_session_id, in_fundednext_flat_window, news_blackout_window
from phase53_engine import (
    AUDIT_PATH,
    PROP_POLICY_PATH,
    ShadowAccount,
    calendar_status_for,
    classify_health,
    freeze_verdict,
    integrity_snapshot,
    load_recent_stitched_nq,
    policy_verdict,
    process_signal,
    reset_audit,
)

ROOT = Path(__file__).resolve().parent
VALIDATION = ROOT / "phase53_validation.json"
DOCS = ROOT / "docs" / "PHASE53_PRE_EVALUATION_SHADOW_VALIDATION.md"
REGISTRY = ROOT / "docs" / "STRATEGY_REGISTRY.md"

OUT = {
    "shadow": ROOT / "reports" / "phase53_shadow",
    "signals": ROOT / "reports" / "phase53_signal_reproduction",
    "exec": ROOT / "reports" / "phase53_execution",
    "health": ROOT / "reports" / "phase53_distribution_health",
    "replay": ROOT / "reports" / "phase53_account_replay",
    "fail": ROOT / "reports" / "phase53_failure_injection",
    "news": ROOT / "reports" / "phase53_news",
    "tz": ROOT / "reports" / "phase53_timezone",
    "gate": ROOT / "reports" / "phase53_purchase_gate",
}

PRIOR = (
    ROOT / "phase52_validation.json",
    ROOT / "reports" / "phase52_pareto" / "selection.json",
    ROOT / "reports" / "phase49_eval_simulation" / "eval_matrix.json",
    ROOT / "phase51_validation.json",
    ROOT / "phase50_validation.json",
    ROOT / "phase49_validation.json",
    ROOT / "phase49b_validation.json",
    PROP_POLICY_PATH,
)

SHADOW_DAYS = 40
UTC = timezone.utc
META_1M = ROOT / "data" / "databento" / "NQ" / "stitched" / "databento_NQ_stitched_1m.meta.json"


def _write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, default=str), encoding="utf-8")


def _csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    flat = []
    for r in rows:
        nr = {}
        for k, v in r.items():
            nr[k] = json.dumps(v) if isinstance(v, (dict, list, set)) else v
        flat.append(nr)
    write_csv(path, flat)


def _fp(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"path": str(path.as_posix()), "exists": False, "sha256": None}
    return {"path": str(path.as_posix()), "exists": True, "sha256": file_sha256(path), "bytes": path.stat().st_size}


def _mark(ok: bool, *, warn: bool = False, insuff: bool = False) -> str:
    if insuff:
        return "INSUFFICIENT_EVIDENCE"
    if ok:
        return "WARNING" if warn else "PASS"
    return "FAIL"


def trade_to_signal(t) -> dict[str, Any]:
    d = t.direction.upper()
    direction = "LONG" if d in ("LONG", "BULLISH") else "SHORT"
    return {
        "trading_date": t.trading_date,
        "direction": direction,
        "entry_timestamp": int(t.entry_timestamp),
        "entry_price": float(t.entry_price),
        "stop_price": float(t.stop_price),
        "target_price": float(t.target_price),
        "exit_timestamp": t.exit_timestamp,
        "exit_price": t.exit_price,
        "outcome": t.outcome,
        "r_multiple": t.r_multiple,
        "points": t.points,
        "trade_id": t.trade_id,
        "frozen_direction_raw": t.direction,
    }


def _entry_key(ts, entry, stop, target, direction) -> tuple:
    d = str(direction).upper()
    mapped = "LONG" if d in ("LONG", "BULLISH") else "SHORT"
    return (int(ts), round(float(entry), 8), round(float(stop), 8), round(float(target), 8), mapped)


def run_timezone_matrix() -> dict[str, Any]:
    rows = []
    samples = [
        datetime(2026, 8, 12, 16, 59, tzinfo=CHICAGO),
        datetime(2026, 8, 12, 17, 0, tzinfo=CHICAGO),
        datetime(2026, 8, 12, 15, 10, tzinfo=CHICAGO),
        datetime(2026, 8, 12, 15, 9, tzinfo=CHICAGO),
        datetime(2026, 8, 12, 15, 55, tzinfo=NY),
        datetime(2026, 8, 15, 12, 0, tzinfo=CHICAGO),
        datetime(2026, 8, 16, 16, 59, tzinfo=CHICAGO),
        datetime(2026, 8, 16, 17, 0, tzinfo=CHICAGO),
        datetime(2026, 3, 8, 17, 0, tzinfo=CHICAGO),
        datetime(2026, 11, 1, 17, 0, tzinfo=CHICAGO),
    ]
    ok = True
    for ts in samples:
        et = ts.astimezone(NY)
        utc = ts.astimezone(UTC)
        sid_a = chicago_session_id(ts)
        sid_b = chicago_session_id(et)
        sid_c = chicago_session_id(utc)
        match = sid_a == sid_b == sid_c
        ok = ok and match
        rows.append(
            {
                "input": ts.isoformat(),
                "ny": et.isoformat(),
                "utc": utc.isoformat(),
                "session_id": sid_a,
                "session_ids_agree": match,
                "fn_flat": in_fundednext_flat_window(ts),
            }
        )
    a = chicago_session_id(datetime(2026, 8, 12, 16, 59, tzinfo=CHICAGO))
    b = chicago_session_id(datetime(2026, 8, 12, 17, 0, tzinfo=CHICAGO))
    ok = ok and (a != b)
    return {"ok": ok, "session_resets_at_1700_ct": a != b, "rows": rows}


def news_boundary_matrix(events: list) -> dict[str, Any]:
    from phase53_engine import event_datetime

    rows = []
    if not events:
        return {"ok": False, "reason": "NO_EVENTS", "rows": []}
    ev = None
    for e in reversed(events):
        if e.publication_date >= "2026-08-01":
            ev = e
            break
    if ev is None:
        ev = events[-1]
    ts = event_datetime(ev.publication_date, ev.release_local or "08:30")
    start, end = news_blackout_window(ts)
    cases = [
        ("1s_before", start - timedelta(seconds=1), True),
        ("inside", ts, False),
        ("immediately_after_clock", datetime(ts.year, ts.month, ts.day, 8, 36, tzinfo=NY), True),
        ("clock_0825", datetime(ts.year, ts.month, ts.day, 8, 25, tzinfo=NY), False),
    ]
    ok = True
    prevented = []
    for name, when, expect_allow in cases:
        acct = ShadowAccount()
        sig = {
            "trading_date": when.astimezone(NY).date().isoformat(),
            "direction": "LONG",
            "entry_timestamp": int(when.timestamp()),
            "entry_price": 20000.0,
            "stop_price": 19920.0,
            "target_price": 20040.0,
            "exit_price": 20040.0,
            "outcome": "TARGET_HIT",
            "r_multiple": 0.5,
        }
        out = process_signal(acct, signal=sig, now=when.astimezone(CHICAGO), calendar_status="OK", event_ts=ts)
        allowed = bool(out.get("accepted"))
        pass_case = allowed == expect_allow
        ok = ok and pass_case
        rows.append(
            {
                "case": name,
                "when": when.isoformat(),
                "expected_allow": expect_allow,
                "accepted": allowed,
                "reason": out.get("rejection_reason"),
                "pass": pass_case,
            }
        )
        if not allowed:
            prevented.append(rows[-1])
    missing = process_signal(
        ShadowAccount(),
        signal={
            "trading_date": "2026-08-12",
            "direction": "LONG",
            "entry_timestamp": 1786552200,
            "entry_price": 20000.0,
            "stop_price": 19920.0,
            "target_price": 20040.0,
            "exit_price": 20040.0,
            "outcome": "TARGET_HIT",
            "r_multiple": 0.5,
        },
        now=datetime(2026, 8, 12, 10, 30, tzinfo=CHICAGO),
        calendar_status="MISSING",
        event_ts=None,
    )
    stale = process_signal(
        ShadowAccount(),
        signal={
            "trading_date": "2026-08-12",
            "direction": "LONG",
            "entry_timestamp": 1786552200,
            "entry_price": 20000.0,
            "stop_price": 19920.0,
            "target_price": 20040.0,
            "exit_price": 20040.0,
            "outcome": "TARGET_HIT",
            "r_multiple": 0.5,
        },
        now=datetime(2026, 8, 12, 10, 30, tzinfo=CHICAGO),
        calendar_status="STALE",
        event_ts=None,
    )
    fail_close = missing.get("rejection_reason") == "NEWS_BLACKOUT_VIOLATION_RISK" and stale.get(
        "rejection_reason"
    ) == "NEWS_BLACKOUT_VIOLATION_RISK"
    ok = ok and fail_close
    return {
        "ok": ok,
        "event": ev.publication_date,
        "event_ts": ts.isoformat(),
        "blackout": {"start": start.isoformat(), "end": end.isoformat()},
        "rows": rows,
        "prevented": prevented,
        "missing_fail_closed": missing.get("rejection_reason"),
        "stale_fail_closed": stale.get("rejection_reason"),
        "fail_close_ok": fail_close,
        "calendar_path": str(EVENTS_PATH.as_posix()),
        "calendar_exists": EVENTS_PATH.exists(),
    }


def injection_matrix() -> dict[str, Any]:
    now = datetime(2026, 8, 12, 10, 30, tzinfo=CHICAGO)
    sig = {
        "trading_date": "2026-08-12",
        "direction": "LONG",
        "entry_timestamp": 1786552200,
        "entry_price": 20000.0,
        "stop_price": 19920.0,
        "target_price": 20040.0,
        "exit_price": 20040.0,
        "outcome": "TARGET_HIT",
        "r_multiple": 0.5,
    }
    cases = [
        ("stale_quote", {"data_age_sec": 99}, "LIVE_DATA_STALE"),
        ("missing_candle", {"data_fault": "MISSING_CANDLE"}, "LIVE_DATA_STALE"),
        ("duplicate_candle", {"data_fault": "DUPLICATE_CANDLE"}, "LIVE_DATA_STALE"),
        ("ooo_timestamp", {"data_fault": "OOO_TIMESTAMP"}, "LIVE_DATA_STALE"),
        ("calendar_missing", {"calendar_status": "MISSING"}, "NEWS_BLACKOUT_VIOLATION_RISK"),
        ("equity_unavailable", {"equity_override": None}, "ACCOUNT_EQUITY_UNKNOWN"),
        ("wrong_hash", {"strategy_hash": "deadbeef"}, "STRATEGY_HASH_MISMATCH"),
        ("wrong_strategy_id", {"strategy_id": "GC_VWAP_V2"}, "STRATEGY_HASH_MISMATCH"),
        ("corrupt_signal", {"signal_corrupt": True}, "SIGNAL_PAYLOAD_CORRUPT"),
        ("delayed_ack", {"ack_delay_sec": 12}, "ORDER_STATE_MISMATCH"),
        ("order_rejected", {"ack_fault": "ORDER_REJECTED"}, "BROKER_CONNECTION_UNSTABLE"),
        ("unexpected_open", {"position_fault": "UNEXPECTED_OPEN"}, "POSITION_STATE_MISMATCH"),
        ("position_missing", {"position_fault": "EXPECTED_MISSING"}, "POSITION_STATE_MISMATCH"),
        ("impossible_mll", {"impossible_mll": True}, "DRAW_DOWN_CALCULATION_INVALID"),
        ("balance_jump", {"balance_jump": True}, "ACCOUNT_EQUITY_UNKNOWN"),
        ("pnl_mismatch", {"pnl_mismatch": True}, "DRAW_DOWN_CALCULATION_INVALID"),
        ("qty_3mnq", {"requested_qty": 3}, ("BLOCK_QTY_3MNQ_REJECTED", "MAX_POSITION_EXCEEDED")),
    ]
    rows = []
    ok = True
    for name, kw, expect in cases:
        kwargs = {"calendar_status": "OK", "event_ts": None, **kw}
        out = process_signal(ShadowAccount(), signal=sig, now=now, **kwargs)
        got = out.get("kill_switch") or out.get("rejection_reason")
        exp = expect if isinstance(expect, tuple) else (expect,)
        passed = got in exp
        ok = ok and passed
        rows.append({"case": name, "expected": list(exp), "got": got, "pass": passed, "transmitted": 0})
    return {"ok": ok, "rows": rows}


def decide_verdict(payload: dict[str, Any]) -> str:
    if payload.get("freeze_fail"):
        return payload["freeze_fail"]
    if payload.get("policy_fail"):
        return payload["policy_fail"]
    if payload["snap_after"]["gc"] != payload["snap_before"]["gc"] or payload["snap_after"]["nq"] != payload["snap_before"]["nq"]:
        return "STOP_PHASE53_FREEZE_INTEGRITY_FAILURE"
    if payload["snap_after"]["policy_sha256"] != payload["snap_before"]["policy_sha256"]:
        return "STOP_PHASE53_POLICY_INTEGRITY_FAILURE"
    if not payload["prior_ok"]:
        return "STOP_PHASE53_POLICY_INTEGRITY_FAILURE"
    if payload.get("orders_transmitted"):
        return "SHADOW_EXECUTION_PIPELINE_FAILED"
    if payload["snap_after"]["execution_default"] != "DRY_RUN" or payload["snap_after"]["broker_execution"]:
        return "SHADOW_EXECUTION_PIPELINE_FAILED"
    if not payload["tests"]["ok"]:
        return "SHADOW_PROP_RULE_ENGINE_FAILED"
    if not payload["intent_ok"] or not payload["failinj"]["ok"]:
        return "SHADOW_PROP_RULE_ENGINE_FAILED"
    if not payload["tz"]["ok"]:
        return "SHADOW_EXECUTION_PIPELINE_FAILED"
    if not payload["news"]["ok"]:
        return "SHADOW_DATA_INTEGRITY_FAILED"
    if payload.get("data_ok") is False:
        return "SHADOW_DATA_INTEGRITY_FAILED"
    health = payload["health"]["class"]
    if health == "DEGRADED":
        return "SHADOW_DISTRIBUTION_DEGRADED"
    flip = payload["health"].get("flip_pct")
    flip_n = int(payload["health"].get("flip_n") or 0)
    if flip is not None and flip_n >= 20 and float(flip) >= 0.10:
        return "SHADOW_DISTRIBUTION_DEGRADED"
    if health == "INSUFFICIENT_SAMPLE":
        return "SHADOW_VALIDATION_INCOMPLETE"
    return "READY_TO_PURCHASE_EVALUATION"


def write_docs(payload: dict[str, Any]) -> None:
    h = payload["health"]
    r = payload["replay"]
    v = payload["verdict"]
    blockers = payload.get("blockers") or []
    DOCS.write_text(
        f"""# Phase 53 — Pre-evaluation shadow validation

## Executive summary

Phase 53 is a **DRY_RUN dress rehearsal** of frozen NQ Drift VWAP Pullback through the locked Phase 52 FundedNext Flex 50K policy, on the most recently available Databento stitched NQ sequencing. No evaluation account was purchased. No broker order was transmitted. Frozen strategy logic was not altered.

**Verdict: `{v}`**

| Item | Value |
| --- | --- |
| Shadow window | {payload.get("period")} |
| NQ signals | {payload.get("n_signals")} |
| Accepted | {payload.get("n_accepted")} |
| Rejected | {payload.get("n_rejected")} |
| Simulated fills | {payload.get("n_fills")} |
| Shadow P&L | {r.get("pnl")} |
| Ending equity | {r.get("ending_equity")} |
| Realized R (mean) | {r.get("mean_realized_R")} |
| Max drawdown | {r.get("max_dd")} |
| Lowest remaining DD | {r.get("lowest_remaining_dd")} |
| Daily-stop events | {r.get("daily_stop_count")} |
| FAST→PROTECTED | {r.get("demote_count")} |
| Near-target | {r.get("near_count")} |
| Distribution health | {h.get("class")} |
| Win rate vs frozen | {h.get("wr")} vs {h.get("frozen_wr")} |
| Expectancy vs frozen | {h.get("er")} vs {h.get("frozen_er")} |
| Winner→loser flip | {h.get("flip_pct")} (n={h.get("flip_n")}) |
| Mean entry slip (ticks) | {r.get("mean_entry_slip_ticks")} |
| Mean exit slip (ticks) | {r.get("mean_exit_slip_ticks")} |
| DRY_RUN | `{payload["snap_after"]["execution_default"]}` |
| Orders transmitted | {payload.get("orders_transmitted")} |

## 1. Freeze and policy integrity

- GC hash: `{payload["snap_after"]["gc"]}`
- NQ hash: `{payload["snap_after"]["nq"]}`
- GC file SHA: `{payload["snap_after"]["gc_file_sha"]}`
- NQ file SHA: `{payload["snap_after"]["nq_file_sha"]}`
- Phase 52 policy SHA: `{payload["snap_after"]["policy_sha256"]}`
- Policy fields match lock: `{payload["snap_after"]["policy_fields_match_phase52_lock"]}`
- execution_default: `{payload["snap_after"]["execution_default"]}`
- broker_execution: `{payload["snap_after"]["broker_execution"]}`
- Prior Phase 49–52 fingerprints unchanged: `{payload["prior_ok"]}`
- FundedNext automation confirmation: `{payload["snap_after"]["automation_confirmation"]}`

Freeze fail: `{payload.get("freeze_fail")}` · Policy fail: `{payload.get("policy_fail")}`

## 2. Shadow account

Synthetic FundedNext Flex 50K: start $50,000, profit target $2,500, EOD trailing MLL lock at $50,100, 35% remaining-DD daily governor frozen at 17:00 CT, FAST 2 MNQ / SAFE·PROTECTED·NEAR 1 MNQ, reject 3 MNQ, PCT_95 near-target, FAST never auto-restored after demotion. Orders are never transmitted.

## 3. Pipeline

```
Databento stitched NQ (most recent {SHADOW_DAYS} NY sessions)
→ frozen replay_dvp_day / extract_signal_entries_for_day
→ Phase 52 evaluate_intent (PCT_95)
→ conservative 1-tick adverse entry/exit + $0.40 RT/MNQ commission
→ shadow FundedNext Flex 50K state machine
→ journal/phase53_fn_flex_shadow/audit.jsonl
```

Signals are not rewritten. Execution may reject. `nt_ati` is not called. `submit=True` is never used.

Signal reproduction keys match: `{payload.get("repro_ok")}`.

## 4–8. Execution, news, timezone, fills

- Intent / kill-switch matrix: `{payload["failinj"]["ok"]}`
- News boundary + fail-closed calendar: `{payload["news"]["ok"]}`
- Chicago/NY/UTC session IDs agree; 17:00 CT reset: `{payload["tz"]["ok"]}`
- Conservative fills: 1 tick entry + 1 tick exit + commission. Not perfect fills.

## 9–10. Distribution health and winner→loser watch

Classification `{h.get("class")}` on n={h.get("n")} filled trades. Destructive flip threshold is 10% of theoretical winners with n≥20. Phase 52 showed that a 10% winner→loser flip collapsed P(pass) from 55.9% to ~15%. This window does not retune the strategy; it only asks whether current sequencing resembles that regime.

## 11. Account-survival replay

State `{r.get("state")}` · passed `{r.get("passed")}` · stall `{r.get("stall")}` · breach attempts prevented `{r.get("breach_attempts_prevented")}`. This is not a proof of expected profitability.

## 12–13. Failure injection and audit

See `reports/phase53_failure_injection/` and `{payload.get("journal")}`. Every intent writes market timestamp, ingestion timestamp, strategy hash, state, qty request/allow, news, kill switch, simulated fill, equity, remaining DD.

## 14–15. Purchase checklist and gate

See `reports/phase53_purchase_gate/checklist.json`. `READY_TO_PURCHASE_EVALUATION` requires freeze/policy integrity, survival-critical tests, working data and calendar, timezone handling, no duplicate-order path, working governor/DD/sizing, distribution not `DEGRADED`, no supported destructive flip, and DRY_RUN with zero transmissions. `INSUFFICIENT_SAMPLE` is `SHADOW_VALIDATION_INCOMPLETE`, not ready.

Unresolved blockers:

{chr(10).join("- " + b for b in blockers) if blockers else "- None."}

## What this phase did not do

No live orders. No evaluation purchase. No frozen-strategy edit. No overwrite of Phase 49/49B/50/51/52 research reports. No pass-time optimization.
""",
        encoding="utf-8",
    )


def patch_registry(verdict: str) -> None:
    text = REGISTRY.read_text(encoding="utf-8")
    block = f"""### Pre-evaluation shadow validation (Phase 53)

| Field | Value |
|-------|--------|
| Phase | 53 |
| Status | See `phase53_validation.json` verdict (`{verdict}`) |
| Question | Does the frozen NQ → Phase 52 policy → simulated-order pipeline behave correctly on the most recent NQ sequencing, enough to justify buying one FundedNext Flex 50K evaluation? |
| Forbidden | Retune GC/NQ; enable broker; purchase/connect eval; overwrite Phase 49–52 reports |
| Evidence | `docs/PHASE53_PRE_EVALUATION_SHADOW_VALIDATION.md`, `reports/phase53_*`, `phase53_validation.json`, `tests_phase53.py` |
| Frozen impact | None. DRY_RUN only. |

"""
    marker = "### Prop execution policy layer (Phase 52)"
    if "### Pre-evaluation shadow validation (Phase 53)" in text:
        import re

        text = re.sub(
            r"### Pre-evaluation shadow validation \(Phase 53\).*?(?=\n### |\n## |\Z)",
            block,
            text,
            flags=re.S,
        )
    else:
        text = text.replace(marker, block + "\n" + marker, 1)
    REGISTRY.write_text(text, encoding="utf-8")


def run() -> dict[str, Any]:
    snap_before = integrity_snapshot()
    freeze_fail = freeze_verdict(snap_before)
    policy_fail = policy_verdict(snap_before)
    prior_before = [_fp(p) for p in PRIOR]
    for d in OUT.values():
        d.mkdir(parents=True, exist_ok=True)
    reset_audit()

    latest = 1786741140
    if META_1M.exists():
        latest = int(json.loads(META_1M.read_text(encoding="utf-8")).get("latest_bar") or latest)
    min_ts = int(latest - 80 * 86400)
    data = load_recent_stitched_nq(min_ts=min_ts)
    data_ok = bool(data.get("ok"))
    integ = data.get("integrity") or {}
    if data_ok and (int(integ.get("duplicate_count") or 0) > 0 or int(integ.get("ohlc_invalid_count") or 0) > 0):
        data_ok = False

    period = None
    n_signals = n_accepted = n_rejected = n_fills = 0
    repro_rows: list[dict[str, Any]] = []
    exec_rows: list[dict[str, Any]] = []
    health: dict[str, Any] = {"class": "INSUFFICIENT_SAMPLE", "n": 0}
    replay: dict[str, Any] = {}
    acct = ShadowAccount()
    events = load_events() if EVENTS_PATH.exists() and EVENTS_PATH.stat().st_size > 0 else []
    calendar_ok = bool(events)
    prevented_news: list[dict[str, Any]] = []
    slips_e: list[float] = []
    slips_x: list[float] = []
    rs: list[float] = []
    flips = 0
    theo_winners = 0
    no_session_days: list[str] = []

    if data_ok and not freeze_fail and not policy_fail:
        cfg = load_frozen_strategy_config()
        b1 = data["bars_1m"]
        b5 = data["bars_5m"]
        b15 = data["bars_15m"]
        dates = trading_dates_ny(b5)
        use = dates[-SHADOW_DAYS:] if len(dates) > SHADOW_DAYS else dates
        period = f"{use[0]} → {use[-1]}" if use else None
        by1 = index_bars_by_ny_date(b1)
        by5 = index_bars_by_ny_date(b5)
        by15 = index_bars_by_ny_date(b15)
        weekday_no_bars: list[str] = []
        if use:
            d0 = dcls.fromisoformat(use[0])
            d1 = dcls.fromisoformat(use[-1])
            have = set(use)
            cur = d0
            while cur <= d1:
                if cur.weekday() < 5 and cur.isoformat() not in have:
                    weekday_no_bars.append(cur.isoformat())
                cur += timedelta(days=1)
        no_session_days = weekday_no_bars

        for td in use:
            day = replay_dvp_day(
                trading_date=td,
                bars_1m=by1.get(td, []),
                bars_5m=by5.get(td, []),
                bars_15m=by15.get(td, []),
                cfg=cfg,
            )
            extracted = extract_signal_entries_for_day(
                bars_1m=by1.get(td, []),
                bars_5m=by5.get(td, []),
                bars_15m=by15.get(td, []),
                trading_date=td,
                cfg=cfg,
            )
            replay_keys = [
                _entry_key(t.entry_timestamp, t.entry_price, t.stop_price, t.target_price, t.direction)
                for t in day["trades"]
            ]
            ext_keys = [
                _entry_key(e["entry_timestamp"], e["entry_price"], e["stop_price"], e["target_price"], e["direction"])
                for e in extracted
            ]
            mutated = replay_keys != ext_keys
            for t in day["trades"]:
                n_signals += 1
                sig = trade_to_signal(t)
                now = datetime.fromtimestamp(int(t.entry_timestamp), tz=UTC).astimezone(CHICAGO)
                cal, ev = calendar_status_for(now, events)
                out = process_signal(acct, signal=sig, now=now, calendar_status=cal, event_ts=ev)
                if out.get("rejection_reason") == "NEWS_BLACKOUT_VIOLATION_RISK":
                    prevented_news.append({"trading_date": td, "ts": now.isoformat(), "code": out["rejection_reason"]})
                if out.get("accepted"):
                    n_accepted += 1
                else:
                    n_rejected += 1
                fill = out.get("fill") or {}
                if fill.get("filled"):
                    n_fills += 1
                    slips_e.append(float(fill.get("entry_slippage_ticks") or 0))
                    slips_x.append(float(fill.get("exit_slippage_ticks") or 0))
                    r = fill.get("realized_R")
                    if r is not None:
                        rs.append(float(r))
                    theo = sig.get("r_multiple")
                    if theo is not None and float(theo) > 0:
                        theo_winners += 1
                        if r is not None and float(r) <= 0:
                            flips += 1
                exec_rows.append(
                    {
                        "trading_date": td,
                        "entry_ts": t.entry_timestamp,
                        "direction": sig["direction"],
                        "intended_entry": sig["entry_price"],
                        "stop": sig["stop_price"],
                        "target": sig["target_price"],
                        "expected_R": sig["r_multiple"],
                        "state": out.get("account_state"),
                        "requested_qty": out.get("quantity_request"),
                        "allowed_qty": out.get("quantity_allowed"),
                        "accepted": out.get("accepted"),
                        "reason": out.get("rejection_reason"),
                        "realized_R": fill.get("realized_R"),
                        "pnl": fill.get("pnl_usd"),
                        "entry_slip_ticks": fill.get("entry_slippage_ticks"),
                        "exit_slip_ticks": fill.get("exit_slippage_ticks"),
                        "signal_mutated": mutated,
                        "kill_switch": out.get("kill_switch"),
                    }
                )
            repro_rows.append(
                {
                    "trading_date": td,
                    "replay_n": len(day["trades"]),
                    "extract_n": len(extracted),
                    "keys_match": not mutated,
                }
            )

        flip_pct = (flips / theo_winners) if theo_winners else 0.0
        health = classify_health(rs, flip_pct=flip_pct, flip_n=theo_winners)
        acct._eod_trail()
        stall = acct.state not in ("EVAL_PASSED", "EVAL_BREACHED") and acct.remaining_dd() < 160
        replay = {
            "starting_equity": 50000.0,
            "ending_equity": acct.equity,
            "pnl": acct.realized_pnl,
            "target_progress": (acct.equity - 50000.0) / 2500.0,
            "max_dd": acct.max_dd,
            "lowest_remaining_dd": acct.lowest_remaining_dd,
            "daily_stop_count": acct.daily_stop_count,
            "demote_count": acct.demote_count,
            "near_count": acct.near_count,
            "accepted": acct.accepted,
            "rejected": acct.rejected,
            "fills": acct.trades_filled,
            "breach_attempts_prevented": acct.breach_attempts_prevented,
            "state": acct.state,
            "stall": stall,
            "passed": acct.state == "EVAL_PASSED",
            "breached": acct.state == "EVAL_BREACHED",
            "orders_transmitted": acct.orders_transmitted,
            "mean_entry_slip_ticks": (sum(slips_e) / len(slips_e)) if slips_e else 0.0,
            "mean_exit_slip_ticks": (sum(slips_x) / len(slips_x)) if slips_x else 0.0,
            "mean_realized_R": (sum(rs) / len(rs)) if rs else None,
            "no_session_weekdays": no_session_days,
            "kill_log": acct.kill_log,
        }

    if AUDIT_PATH.exists():
        shutil.copy2(AUDIT_PATH, OUT["shadow"] / "audit.jsonl")

    tz = run_timezone_matrix()
    news = news_boundary_matrix(events)
    failinj = injection_matrix()

    proc = subprocess.run(
        [sys.executable, "-m", "unittest", "tests_phase53", "-v"],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
    )
    tests_ok = proc.returncode == 0
    snap_after = integrity_snapshot()
    prior_after = [_fp(p) for p in PRIOR]
    prior_ok = [a["sha256"] for a in prior_before] == [a["sha256"] for a in prior_after]
    repro_ok = bool(repro_rows) and all(r.get("keys_match") for r in repro_rows)
    intent_ok = tests_ok and failinj["ok"]

    health_cls = health.get("class")
    flip_ok = not (
        health_cls == "DEGRADED"
        or ((health.get("flip_pct") or 0) >= 0.10 and int(health.get("flip_n") or 0) >= 20)
    )
    checklist = {
        "frozen_hashes_verified": _mark(freeze_fail is None),
        "phase52_policy_verified": _mark(policy_fail is None and snap_before["policy_sha256"] == snap_after["policy_sha256"]),
        "fundednext_automation_rule_source_stored": _mark(
            bool(snap_after["automation_confirmation"].get("automation_allowed"))
        ),
        "fundednext_rule_snapshot_date_stored": _mark(bool(snap_after["automation_confirmation"].get("snapshot_date"))),
        "nq_data_feed_functioning": _mark(data_ok),
        "economic_calendar_functioning": _mark(calendar_ok),
        "timezone_handling_verified": _mark(tz["ok"]),
        "news_locks_verified": _mark(news["ok"] and tests_ok),
        "position_sizing_verified": _mark(intent_ok),
        "duplicate_order_prevention_verified": _mark(intent_ok),
        "daily_governor_verified": _mark(intent_ok),
        "remaining_dd_calculation_verified": _mark(intent_ok),
        "fast_to_protected_verified": _mark(intent_ok),
        "near_target_logic_verified": _mark(intent_ok),
        "kill_switches_verified": _mark(failinj["ok"] and tests_ok),
        "shadow_fills_reasonable": _mark(bool(slips_e) and min(slips_e) >= 1.0 - 1e-9, insuff=not slips_e),
        "strategy_distribution_health_acceptable": _mark(
            health_cls in ("HEALTHY", "WATCH"),
            insuff=health_cls == "INSUFFICIENT_SAMPLE",
        ),
        "no_destructive_winner_loser_shift": _mark(flip_ok, insuff=int(health.get("flip_n") or 0) < 15),
        "no_real_orders_transmitted": _mark(acct.orders_transmitted == 0),
        "signal_reproduction_unmutated": _mark(repro_ok, insuff=not repro_rows),
    }

    payload: dict[str, Any] = {
        "phase": 53,
        "snap_before": snap_before,
        "snap_after": snap_after,
        "freeze_fail": freeze_fail,
        "policy_fail": policy_fail,
        "prior_before": prior_before,
        "prior_after": prior_after,
        "prior_ok": prior_ok,
        "data_ok": data_ok,
        "data_source": data.get("source") if data.get("ok") else data.get("error_code"),
        "data_integrity": integ,
        "period": period,
        "n_signals": n_signals,
        "n_accepted": n_accepted,
        "n_rejected": n_rejected,
        "n_fills": n_fills,
        "health": health,
        "replay": replay,
        "tz": tz,
        "news": news,
        "news_prevented_during_shadow": prevented_news,
        "intent_ok": intent_ok,
        "failinj": failinj,
        "repro_ok": repro_ok,
        "orders_transmitted": acct.orders_transmitted,
        "checklist": checklist,
        "tests": {
            "ok": tests_ok,
            "returncode": proc.returncode,
            "stdout_tail": (proc.stdout or "")[-4000:],
            "stderr_tail": (proc.stderr or "")[-1500:],
        },
        "journal": str(AUDIT_PATH.as_posix()),
    }
    payload["verdict"] = decide_verdict(payload)
    blockers = []
    if payload["verdict"] != "READY_TO_PURCHASE_EVALUATION":
        blockers.append(f"Purchase gate blocked by verdict {payload['verdict']}")
    if health_cls == "INSUFFICIENT_SAMPLE":
        blockers.append("Shadow fill sample too small for a HEALTHY/WATCH distribution call")
    if health_cls == "DEGRADED":
        blockers.append("Distribution classified DEGRADED — do not purchase")
    if not tests_ok:
        blockers.append("tests_phase53 failed")
    if not data_ok:
        blockers.append("NQ stitched feed unavailable or integrity failed")
    payload["blockers"] = blockers

    _csv(OUT["signals"] / "signals.csv", exec_rows)
    _write_json(OUT["signals"] / "reproduction.json", {"ok": repro_ok, "days": repro_rows})
    _csv(OUT["exec"] / "intents.csv", exec_rows)
    _write_json(OUT["health"] / "health.json", health)
    _write_json(OUT["replay"] / "account.json", replay)
    _write_json(OUT["news"] / "prevented.json", {"shadow": prevented_news, "boundary": news})
    _write_json(OUT["tz"] / "matrix.json", tz)
    _write_json(OUT["gate"] / "checklist.json", checklist)
    _write_json(
        OUT["gate"] / "verdict.json",
        {"verdict": payload["verdict"], "blockers": blockers, "DRY_RUN": True, "orders_transmitted": 0},
    )
    _write_json(
        OUT["shadow"] / "executive.json",
        {
            "verdict": payload["verdict"],
            "period": period,
            "n_signals": n_signals,
            "n_accepted": n_accepted,
            "n_rejected": n_rejected,
            "n_fills": n_fills,
            "health": health.get("class"),
            "equity": replay.get("ending_equity"),
            "remaining_dd": replay.get("lowest_remaining_dd"),
            "state": replay.get("state"),
            "target_progress": replay.get("target_progress"),
            "daily_stop_count": replay.get("daily_stop_count"),
            "DRY_RUN": True,
            "orders_transmitted": 0,
        },
    )
    _write_json(OUT["fail"] / "injection.json", failinj)
    _write_json(VALIDATION, payload)
    write_docs(payload)
    patch_registry(payload["verdict"])
    return payload


if __name__ == "__main__":
    out = run()
    print(
        json.dumps(
            {
                "verdict": out["verdict"],
                "period": out.get("period"),
                "n_signals": out.get("n_signals"),
                "n_accepted": out.get("n_accepted"),
                "n_rejected": out.get("n_rejected"),
                "health": (out.get("health") or {}).get("class"),
                "tests_ok": out["tests"]["ok"],
            },
            indent=2,
        )
    )
    if str(out["verdict"]).startswith("STOP_"):
        sys.exit(2)
    if not out["tests"]["ok"]:
        sys.exit(1)
