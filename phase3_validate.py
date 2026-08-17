"""Live Phase 3 validation: session sweep → LuxAlgo CHoCH confirmation."""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from bars import fetch_bars
from ict_sessions import fetch_ict_session_ranges
from liquidity_sweep import detect_sweeps
from luxalgo_structure import fetch_luxalgo_choch
from models import SweepRule
from sessions_config import SESSION_DEFINITIONS, SESSION_DST_UNCERTAINTY
from structure_confirm import bars_after_sweep, confirm_after_sweep


def _fmt(ts):
    if ts is None:
        return None
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).isoformat()


def _series_index_for_time(bars_by_series_index: dict, ts: int) -> int | None:
    matches = [idx for idx, t in bars_by_series_index.items() if int(t) == int(ts)]
    if not matches:
        return None
    return int(min(matches))


def _event_brief(e) -> dict:
    return {
        "direction": e.direction,
        "level": e.level,
        "timestamp": _fmt(e.event_timestamp),
        "bar_index": e.event_bar_index,
        "timing_confidence": e.timing_confidence,
        "raw_id": e.raw_id,
    }


async def main():
    try:
        bar_payload = await fetch_bars()
    except Exception as exc:  # noqa: BLE001 - diagnostic script
        report = {
            "ok": False,
            "error": str(exc),
            "note": "TradingView CDP unavailable; start Desktop with --remote-debugging-port=9222",
            "session_dst_uncertainty": SESSION_DST_UNCERTAINTY,
        }
        Path("phase3_validation.json").write_text(
            json.dumps(report, indent=2), encoding="utf-8"
        )
        print(json.dumps(report, indent=2))
        return

    if not bar_payload.get("ok"):
        print(json.dumps(bar_payload, indent=2))
        return

    bars = bar_payload["bars"]
    by_index = bar_payload.get("bars_by_series_index") or {}
    resolution = int(bar_payload.get("resolution") or 15)
    bar_seconds = resolution * 60

    ict = await fetch_ict_session_ranges(bars_by_series_index=by_index)
    lux = await fetch_luxalgo_choch(bars_by_series_index=by_index)
    choch_events = lux.get("events") or []

    examples = []
    for name in ("Asia", "London"):
        session = (ict.get("latest") or {}).get(name)
        if not session:
            examples.append({"session": name, "status": "no_ict_session"})
            continue

        sweeps = detect_sweeps(session, bars, rule=SweepRule.WICK_ONLY)
        if not sweeps:
            examples.append(
                {
                    "session": name,
                    "status": "no_wick_sweep_on_latest_ict_range",
                    "session_high": session.high,
                    "session_low": session.low,
                }
            )
            continue

        # Prefer the earliest sweep on this range for a clean sequence demo.
        for sweep in sweeps:
            sweep_bar = _series_index_for_time(by_index, sweep.sweep_timestamp)
            decision = confirm_after_sweep(
                sweep, choch_events, sweep_bar_index=sweep_bar
            )
            required = decision.required_direction
            candidate_rows = [_event_brief(e) for e in choch_events]

            chosen = None
            if decision.confirmed and decision.confirmation is not None:
                conf = decision.confirmation
                delta = bars_after_sweep(
                    sweep, conf, bar_seconds=bar_seconds
                )
                chosen = {
                    "direction": conf.direction,
                    "level": conf.level,
                    "time": _fmt(conf.event_timestamp),
                    "bar_index": conf.event_bar_index,
                    "timing_confidence": conf.timing_confidence,
                    **delta,
                }

            examples.append(
                {
                    "session": sweep.session,
                    "swept_side": sweep.side,
                    "sweep_level": sweep.level,
                    "sweep_time": _fmt(sweep.sweep_timestamp),
                    "sweep_bar_index": sweep_bar,
                    "required_direction": required,
                    "candidate_choch_events": candidate_rows,
                    "decision": decision.to_dict(),
                    "chosen_confirmation": chosen,
                    "status": "confirmed" if decision.confirmed else decision.reason,
                }
            )

    # Pick the best narrative example: first confirmed, else first with sweeps.
    live_example = None
    for ex in examples:
        if ex.get("status") == "confirmed":
            live_example = ex
            break
    if live_example is None:
        for ex in examples:
            if "swept_side" in ex:
                live_example = ex
                break
    if live_example is None and examples:
        live_example = examples[0]

    report = {
        "ok": bool(ict.get("ok") and lux.get("ok")),
        "symbol": bar_payload.get("symbol"),
        "resolution": bar_payload.get("resolution"),
        "bar_count": bar_payload.get("count"),
        "session_definitions": {k: v.to_dict() for k, v in SESSION_DEFINITIONS.items()},
        "session_dst_uncertainty": SESSION_DST_UNCERTAINTY,
        "ict": {
            "ok": ict.get("ok"),
            "study_id": ict.get("study_id"),
            "counts": ict.get("counts"),
        },
        "luxalgo": {
            "ok": lux.get("ok"),
            "study_id": lux.get("study_id"),
            "study_name": lux.get("study_name"),
            "counts": lux.get("counts"),
            "error": lux.get("error"),
        },
        "live_example": live_example,
        "all_sweep_attempts": examples,
    }

    path = Path("phase3_validation.json")
    path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    print(json.dumps(report, indent=2, default=str))


if __name__ == "__main__":
    asyncio.run(main())
