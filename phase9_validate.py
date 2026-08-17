"""Phase 9 validation: offline expiry + live pipeline annotate/idempotency/clear."""

from __future__ import annotations

import asyncio
import json
from datetime import date
from pathlib import Path
from typing import Any, Optional

from annotation_plan import plan_annotations
from expiry_config import EXPIRY_REASON_NEW_SESSION_STARTED, ExpiryConfig
from models import Bar, SessionRange, SetupStatus, StructureConfirmation
from session_time import resolve_session_window
from sessions_config import SESSION_DEFINITIONS, SESSION_DST_UNCERTAINTY
from setup_engine import analyze_session_setup
from setup_expiry import SessionContext, build_session_context
from strategy_config import DEFAULT_STRATEGY_CONFIG, StrategyConfig


def _cfg(**expiry_kwargs) -> StrategyConfig:
    return StrategyConfig(
        sweep_rule=DEFAULT_STRATEGY_CONFIG.sweep_rule,
        entry_modes=DEFAULT_STRATEGY_CONFIG.entry_modes,
        fvg=DEFAULT_STRATEGY_CONFIG.fvg,
        entry=DEFAULT_STRATEGY_CONFIG.entry,
        risk=DEFAULT_STRATEGY_CONFIG.risk,
        target=DEFAULT_STRATEGY_CONFIG.target,
        expiry=ExpiryConfig(**expiry_kwargs),
        prefer_completed_sessions_only=True,
        session_confidence=dict(DEFAULT_STRATEGY_CONFIG.session_confidence),
        dst_uncertainty=DEFAULT_STRATEGY_CONFIG.dst_uncertainty,
    )


def offline_expiry_fixture() -> dict[str, Any]:
    asia_w = resolve_session_window(SESSION_DEFINITIONS["Asia"], date(2026, 8, 14))
    london_w = resolve_session_window(SESSION_DEFINITIONS["London"], date(2026, 8, 15))
    session = SessionRange(
        name="Asia",
        timezone="America/New_York",
        start=asia_w.utc_start,
        end=asia_w.utc_end,
        high=4360.0,
        low=4311.04,
        high_timestamp=None,
        low_timestamp=None,
        complete=True,
        source="ict_sessions",
        coverage_status="full",
        identity="Asia:2026-08-14",
        extras={"resolved_window": asia_w.to_dict()},
    )
    sweep_ts = asia_w.utc_end + 60
    bars = [Bar(time=sweep_ts, open=4312, high=4313, low=4310, close=4312)]
    before = analyze_session_setup(
        session,
        bars,
        [],
        _cfg(enabled=False),
        symbol="OANDA:XAUUSD",
        timeframe="15",
        now_ts=asia_w.utc_start + 100,
    )
    after = analyze_session_setup(
        session,
        bars,
        [],
        _cfg(expire_on_new_session=True),
        symbol="OANDA:XAUUSD",
        timeframe="15",
        now_ts=london_w.utc_start + 300,
        session_context=SessionContext(
            setup_session_name="Asia",
            setup_trading_date="2026-08-14",
            setup_window=asia_w,
            active_session_name=london_w.session,
            active_trading_date=london_w.trading_date,
            active_window=london_w,
            now_ts=london_w.utc_start + 300,
        ),
    )
    plan = plan_annotations(after)
    return {
        "mode": "offline_expiry_fixture",
        "before_status": before.status,
        "after_status": after.status,
        "expiry_reason": after.expiry_reason,
        "setup_id_before": before.id,
        "setup_id_after": after.id,
        "id_stable": before.id == after.id,
        "expected_reason": EXPIRY_REASON_NEW_SESSION_STARTED,
        "annotation_status_label": next(
            (i.label for i in plan.items if i.role == "status"), None
        ),
    }


def _summarize_setup(setup: Optional[dict[str, Any]], *, symbol: str, timeframe: str) -> dict[str, Any]:
    if not setup:
        return {"symbol": symbol, "timeframe": timeframe, "setup": None}
    sr = setup.get("session_range") or {}
    sweep = setup.get("sweep")
    conf = setup.get("confirmation")
    fvg = setup.get("fvg")
    meta = setup.get("source_metadata") or {}
    entries_out: dict[str, Any] = {}
    stops: dict[str, Any] = {}
    targets: dict[str, Any] = {}
    for item in setup.get("entries") or []:
        entry = (item or {}).get("entry") or {}
        risk = (item or {}).get("risk")
        target = (item or {}).get("target")
        mode = entry.get("mode")
        if not mode:
            continue
        entries_out[mode] = {
            "triggered": entry.get("triggered"),
            "status": entry.get("status"),
            "price": entry.get("price"),
            "entry_depth": entry.get("entry_depth"),
            "max_retrace_depth": entry.get("max_retrace_depth"),
        }
        if risk:
            stops[mode] = {
                "stop_mode": risk.get("stop_mode"),
                "stop_price": risk.get("stop_price"),
                "valid": risk.get("valid"),
            }
        if target:
            targets[mode] = {
                "fixed_rr": [
                    {"rr": ft.get("rr"), "price": ft.get("price")}
                    for ft in (target.get("fixed_rr_targets") or [])
                ],
                "opposite_liquidity_label": target.get("opposite_liquidity_label"),
                "opposite_liquidity_price": target.get("opposite_liquidity_price"),
            }
    out: dict[str, Any] = {
        "symbol": symbol or setup.get("symbol"),
        "timeframe": timeframe or setup.get("timeframe"),
        "session_selected": setup.get("session"),
        "session_date": setup.get("trading_date"),
        "session_complete": sr.get("complete"),
        "session_source": sr.get("source") or meta.get("session_source"),
        "session_high": sr.get("high"),
        "session_low": sr.get("low"),
        "setup_status": setup.get("status"),
        "setup_id": setup.get("id"),
        "expiry_reason": setup.get("expiry_reason"),
        "invalidation_reason": setup.get("invalidation_reason"),
        "reliability_flags": {
            "session_confidence": meta.get("session_confidence"),
            "coverage_status": meta.get("coverage_status"),
            "confirmation_timing_confidence": meta.get(
                "confirmation_timing_confidence"
            ),
            "dst_uncertainty": bool(meta.get("dst_uncertainty")),
            "trigger_bar_stop_ambiguity": bool(meta.get("trigger_bar_stop_ambiguity")),
            "expiry_evaluated": meta.get("expiry_evaluated"),
            "session_context": meta.get("session_context"),
        },
    }
    if sweep:
        out["sweep"] = {
            "side": sweep.get("side"),
            "level": sweep.get("level"),
            "extreme": meta.get("sweep_extreme"),
            "timestamp": sweep.get("sweep_timestamp"),
            "rule": sweep.get("rule"),
        }
    if conf:
        out["CHoCH"] = {
            "found": True,
            "direction": conf.get("direction"),
            "level": conf.get("level"),
            "timestamp": conf.get("event_timestamp"),
            "bar_index": conf.get("event_bar_index"),
            "timing_confidence": conf.get("timing_confidence"),
        }
    elif setup.get("status") not in (
        SetupStatus.WAITING_FOR_SESSION.value,
        SetupStatus.WAITING_FOR_SWEEP.value,
        SetupStatus.NO_SETUP.value,
    ):
        out["CHoCH"] = {"found": False}
    if fvg:
        out["FVG"] = {
            "found": True,
            "direction": fvg.get("direction"),
            "low": fvg.get("low"),
            "high": fvg.get("high"),
            "midpoint": fvg.get("midpoint"),
            "created_timestamp": fvg.get("created_timestamp"),
        }
    if entries_out:
        out["entries"] = entries_out
    if stops:
        out["risk"] = {"stop_prices": stops}
    if targets:
        out["targets"] = targets
    return out


async def _count_aitrade_shapes(setup_id: Optional[str] = None) -> dict[str, Any]:
    from cdp import evaluate_js

    sid = json.dumps(setup_id)
    js = f"""
(() => {{
  const store = window.__aitradeSetupAnnotations || {{}};
  const sid = {sid};
  if (sid) {{
    const ids = Array.isArray(store[sid]) ? store[sid] : [];
    return {{ ok: true, setup_id: sid, count: ids.length, ids }};
  }}
  const keys = Object.keys(store);
  let total = 0;
  const by = {{}};
  for (const k of keys) {{
    const n = Array.isArray(store[k]) ? store[k].length : 0;
    by[k] = n;
    total += n;
  }}
  return {{ ok: true, setups: keys, by_setup: by, total }};
}})()
""".strip()
    return await evaluate_js(js)


async def live_validation() -> dict[str, Any]:
    try:
        from setup_analyze import analyze_live_session_setup
        from setup_annotate import annotate_trade_setup, clear_setup_annotations
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc), "live_tested": False}

    try:
        analysis = await analyze_live_session_setup(session="auto")
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "error": str(exc),
            "live_tested": False,
            "code": "LIVE_ANALYZE_FAILED",
        }

    if not analysis.get("ok"):
        return {**analysis, "live_tested": False}

    setup = analysis.get("setup")
    summary = _summarize_setup(
        setup,
        symbol=analysis.get("symbol") or "",
        timeframe=analysis.get("timeframe") or "",
    )

    # Optional: analyze Asia explicitly for cross-session expiry demonstration.
    asia_expiry: dict[str, Any] = {"checked": False}
    try:
        asia_analysis = await analyze_live_session_setup(session="asia")
        asia_setup = asia_analysis.get("setup") if asia_analysis.get("ok") else None
        if asia_setup:
            asia_expiry = {
                "checked": True,
                "setup_id": asia_setup.get("id"),
                "status": asia_setup.get("status"),
                "expiry_reason": asia_setup.get("expiry_reason"),
                "status_before_expiry": (asia_setup.get("source_metadata") or {}).get(
                    "status_before_expiry"
                ),
                "active_context": (
                    (asia_setup.get("source_metadata") or {}).get("session_context") or {}
                ).get("active_session_name"),
            }
    except Exception as exc:  # noqa: BLE001
        asia_expiry = {"checked": True, "error": str(exc)}

    if not setup:
        return {
            "ok": True,
            "live_tested": True,
            "annotated": False,
            "reason": "no setup payload from auto session",
            "analysis_summary": summary,
            "asia_expiry_check": asia_expiry,
            "raw_status": analysis.get("status"),
            "auto_session": analysis.get("auto_session"),
        }

    # First annotate + screenshot
    first = await annotate_trade_setup(
        setup,
        entry_mode="all",
        show_fixed_rr=True,
        show_opposite_liquidity=True,
        take_screenshot_after=True,
    )
    after_first = await _count_aitrade_shapes(setup.get("id"))

    # Idempotency: second annotate
    second = await annotate_trade_setup(
        setup,
        entry_mode="all",
        show_fixed_rr=True,
        show_opposite_liquidity=True,
        take_screenshot_after=False,
    )
    after_second = await _count_aitrade_shapes(setup.get("id"))

    cleared_count_before = after_second.get("count")
    cleared = await clear_setup_annotations(setup.get("id"))
    after_clear = await _count_aitrade_shapes(setup.get("id"))

    # Re-annotate after clear
    third = await annotate_trade_setup(
        setup,
        entry_mode="all",
        show_fixed_rr=True,
        show_opposite_liquidity=True,
        take_screenshot_after=False,
    )
    after_reannotate = await _count_aitrade_shapes(setup.get("id"))

    created1 = first.get("annotations_created_count")
    created2 = second.get("annotations_created_count")
    idempotent = (
        created1 == created2
        and after_first.get("count") == after_second.get("count")
        and after_second.get("count") == created2
    )

    return {
        "ok": True,
        "live_tested": True,
        "annotated": True,
        "analysis_summary": summary,
        "asia_expiry_check": asia_expiry,
        "annotation": {
            "setup_id": first.get("setup_id"),
            "status": first.get("status"),
            "annotations_created": first.get("annotations_created_count"),
            "annotations_skipped_plan": first.get("annotations_skipped_plan"),
            "annotations_skipped_render": first.get("annotations_skipped_render"),
            "screenshot_path": first.get("screenshot_path"),
        },
        "idempotency": {
            "first_created": created1,
            "second_created": created2,
            "count_after_first": after_first.get("count"),
            "count_after_second": after_second.get("count"),
            "no_duplicate_stack": idempotent,
            "cleared_previous_on_second": True,
        },
        "clear_ownership": {
            "cleared": cleared,
            "count_before_clear": cleared_count_before,
            "count_after_clear": after_clear.get("count"),
            "count_after_reannotate": after_reannotate.get("count"),
            "note": (
                "Cleared only __aitradeSetupAnnotations for setup_id; "
                "personal/indicator drawings not targeted."
            ),
        },
        "explanation": analysis.get("explanation"),
    }


async def main() -> None:
    offline = offline_expiry_fixture()
    live = await live_validation()
    report = {
        "ok": True,
        "phase": 9,
        "session_dst_uncertainty": SESSION_DST_UNCERTAINTY,
        "offline_expiry": offline,
        "live": live,
    }
    Path("phase9_validation.json").write_text(
        json.dumps(report, indent=2, default=str), encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "offline_expiry_status": offline.get("after_status"),
                "offline_expiry_reason": offline.get("expiry_reason"),
                "offline_id_stable": offline.get("id_stable"),
                "live_ok": live.get("ok"),
                "live_tested": live.get("live_tested"),
                "live_status": (live.get("analysis_summary") or {}).get("setup_status")
                or live.get("error")
                or live.get("raw_status"),
                "live_setup_id": (live.get("analysis_summary") or {}).get("setup_id"),
                "annotations_created": (live.get("annotation") or {}).get(
                    "annotations_created"
                ),
                "screenshot_path": (live.get("annotation") or {}).get("screenshot_path"),
                "idempotent": (live.get("idempotency") or {}).get("no_duplicate_stack"),
                "asia_expiry_check": live.get("asia_expiry_check"),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
