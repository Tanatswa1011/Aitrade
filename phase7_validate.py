"""Phase 7 validation: orchestration live + offline ENTRY_READY fixture."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from models import Bar, SessionRange, StructureConfirmation
from setup_engine import analyze_session_setup
from sessions_config import SESSION_DST_UNCERTAINTY
from strategy_config import DEFAULT_STRATEGY_CONFIG


def offline_entry_ready() -> dict:
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
    choch = [
        StructureConfirmation(
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
    ]
    setup = analyze_session_setup(
        session,
        bars,
        choch,
        DEFAULT_STRATEGY_CONFIG,
        symbol="OANDA:XAUUSD",
        timeframe="15",
    )
    return {
        "mode": "offline_fixture",
        "status": setup.status,
        "setup_id": setup.id,
        "explanation": setup.explanation,
        "setup": setup.to_dict(),
    }


async def live_attempt() -> dict:
    try:
        from setup_analyze import analyze_live_session_setup

        return await analyze_live_session_setup(session="auto")
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


async def main():
    fixture = offline_entry_ready()
    live = await live_attempt()
    report = {
        "ok": True,
        "phase": 7,
        "session_dst_uncertainty": SESSION_DST_UNCERTAINTY,
        "offline_complete_setup": fixture,
        "live_analysis": live,
    }
    Path("phase7_validation.json").write_text(
        json.dumps(report, indent=2, default=str), encoding="utf-8"
    )
    # Concise stdout
    print(
        json.dumps(
            {
                "offline_status": fixture["status"],
                "offline_setup_id": fixture["setup_id"],
                "live_ok": live.get("ok"),
                "live_status": live.get("status") or live.get("error"),
                "live_partial": live.get("partial"),
            },
            indent=2,
        )
    )
    print("\n--- Offline explanation ---\n")
    print(fixture["explanation"])


if __name__ == "__main__":
    asyncio.run(main())
