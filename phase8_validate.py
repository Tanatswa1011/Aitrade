"""Phase 8 validation: AnnotationPlan offline + live annotate if CDP up."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from annotation_plan import plan_annotations
from models import Bar, SessionRange, StructureConfirmation
from sessions_config import SESSION_DST_UNCERTAINTY
from setup_engine import analyze_session_setup
from strategy_config import DEFAULT_STRATEGY_CONFIG


def offline_plan() -> dict:
    session = SessionRange(
        name="Asia",
        timezone="America/New_York",
        start=0,
        end=900,
        high=4360.0,
        low=4311.04,
        high_timestamp=None,
        low_timestamp=None,
        complete=True,
        source="ict_sessions",
        coverage_status="full",
        identity="Asia:fixture",
        extras={"resolved_window": {"trading_date": "2026-08-14"}},
    )
    bars = [
        Bar(time=1000, open=4312, high=4313, low=4310, close=4312),
        Bar(time=2000, open=4315, high=4322, low=4314, close=4320),
        Bar(time=3000, open=4321, high=4325, low=4320, close=4324),
        Bar(time=4000, open=4324, high=4340, low=4323, close=4338),
        Bar(time=5000, open=4338, high=4345, low=4330, close=4342),
        Bar(time=6000, open=4342, high=4343, low=4328, close=4329),
        Bar(time=7000, open=4329, high=4330, low=4326, close=4327),
    ]
    choch = StructureConfirmation(
        kind="CHoCH",
        direction="bullish",
        level=4320.0,
        event_timestamp=2000,
        event_bar_index=None,
        source="luxalgo",
        study_id="smUEv2",
        raw_id="fixture",
        timing_confidence="exact",
    )
    setup = analyze_session_setup(
        session, bars, [choch], DEFAULT_STRATEGY_CONFIG, symbol="OANDA:XAUUSD", timeframe="15"
    )
    plan = plan_annotations(setup, entry_mode="all")
    return {
        "mode": "offline_annotation_plan",
        "setup_id": setup.id,
        "status": setup.status,
        "plan": plan.to_dict(),
    }


async def live_annotate() -> dict:
    try:
        from setup_analyze import analyze_live_session_setup
        from setup_annotate import annotate_trade_setup
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}

    try:
        analysis = await analyze_live_session_setup(session="auto")
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc), "live_tested": False}

    if not analysis.get("ok"):
        return {**analysis, "live_tested": False, "annotated": False}

    setup = analysis.get("setup")
    if not setup:
        return {
            "ok": True,
            "live_tested": True,
            "annotated": False,
            "status": analysis.get("status"),
            "reason": "no setup payload (auto session ambiguous or unavailable)",
            "analysis": analysis,
        }

    try:
        drawn = await annotate_trade_setup(
            setup,
            entry_mode="all",
            show_fixed_rr=True,
            show_opposite_liquidity=True,
            take_screenshot_after=True,
        )
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "live_tested": True,
            "annotated": False,
            "setup_id": setup.get("id"),
            "status": setup.get("status"),
            "error": str(exc),
            "plan_only": plan_annotations(setup).to_dict(),
        }

    return {
        "ok": True,
        "live_tested": True,
        "annotated": True,
        "setup_id": drawn.get("setup_id"),
        "status": drawn.get("status"),
        "annotations_created_count": drawn.get("annotations_created_count"),
        "annotations_skipped_plan": drawn.get("annotations_skipped_plan"),
        "annotations_skipped_render": drawn.get("annotations_skipped_render"),
        "screenshot_path": drawn.get("screenshot_path"),
        "explanation": analysis.get("explanation"),
    }


async def main():
    offline = offline_plan()
    live = await live_annotate()
    report = {
        "ok": True,
        "phase": 8,
        "session_dst_uncertainty": SESSION_DST_UNCERTAINTY,
        "offline": offline,
        "live": live,
    }
    Path("phase8_validation.json").write_text(
        json.dumps(report, indent=2, default=str), encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "offline_setup_id": offline["setup_id"],
                "offline_status": offline["status"],
                "offline_item_count": offline["plan"]["item_count"],
                "live_ok": live.get("ok"),
                "live_tested": live.get("live_tested"),
                "live_status": live.get("status") or live.get("error"),
                "live_created": live.get("annotations_created_count"),
                "screenshot_path": live.get("screenshot_path"),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
