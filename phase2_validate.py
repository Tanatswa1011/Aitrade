"""Live Phase 2 validation with DST-aware session definitions."""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from bars import fetch_bars
from ict_sessions import fetch_ict_session_ranges
from liquidity_sweep import detect_first_sweep, detect_sweeps
from models import SweepRule
from ohlc_sessions import compute_session_ranges, latest_any, latest_completed
from sessions_config import SESSION_DEFINITIONS


def _fmt_ts(ts):
    if ts is None:
        return None
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).isoformat()


def _level_diff(a, b):
    if a is None or b is None:
        return None
    return round(float(a) - float(b), 6)


async def main():
    bar_payload = await fetch_bars()
    if not bar_payload.get("ok"):
        print(json.dumps(bar_payload, indent=2))
        return

    bars = bar_payload["bars"]
    by_index = bar_payload.get("bars_by_series_index") or {}
    resolution = int(bar_payload.get("resolution") or 5)

    ict = await fetch_ict_session_ranges(bars_by_series_index=by_index)
    ohlc_ranges = compute_session_ranges(bars, resolution_minutes=resolution)

    ict_latest = {k: v.to_dict() for k, v in (ict.get("latest") or {}).items()}
    ohlc_latest_completed = {
        name: (
            latest_completed(ohlc_ranges, name).to_dict()
            if latest_completed(ohlc_ranges, name)
            else None
        )
        for name in ("Asia", "London")
    }

    discrepancies = []
    for name in ("Asia", "London"):
        ict_r = (ict.get("latest") or {}).get(name)
        ohlc_r = latest_completed(ohlc_ranges, name) or latest_any(ohlc_ranges, name)
        if not ict_r or not ohlc_r:
            discrepancies.append(
                {
                    "session": name,
                    "issue": "missing_side",
                    "ict_present": bool(ict_r),
                    "ohlc_present": bool(ohlc_r),
                }
            )
            continue
        discrepancies.append(
            {
                "session": name,
                "ict_high": ict_r.high,
                "ohlc_high": ohlc_r.high,
                "high_diff": _level_diff(ict_r.high, ohlc_r.high),
                "ict_low": ict_r.low,
                "ohlc_low": ohlc_r.low,
                "low_diff": _level_diff(ict_r.low, ohlc_r.low),
                "ict_start": _fmt_ts(ict_r.start),
                "ohlc_start": _fmt_ts(ohlc_r.start),
                "ict_end": _fmt_ts(ict_r.end),
                "ohlc_end": _fmt_ts(ohlc_r.end),
                "match_high": _level_diff(ict_r.high, ohlc_r.high) == 0,
                "match_low": _level_diff(ict_r.low, ohlc_r.low) == 0,
                "resolved_window": (ohlc_r.extras or {}).get("resolved_window"),
            }
        )

    sweep_results = {}
    for name in ("Asia", "London"):
        session = (ict.get("latest") or {}).get(name)
        if not session:
            sweep_results[name] = {"error": "no ICT latest session"}
            continue
        sweeps_wick = detect_sweeps(session, bars, rule=SweepRule.WICK_ONLY)
        first_low = detect_first_sweep(session, bars, rule=SweepRule.WICK_ONLY, side="low")
        first_high = detect_first_sweep(session, bars, rule=SweepRule.WICK_ONLY, side="high")
        touch_any = detect_sweeps(session, bars, rule=SweepRule.TOUCH, first_only=True)
        sweep_results[name] = {
            "wick_only_count": len(sweeps_wick),
            "first_low": None if not first_low else first_low.to_dict(),
            "first_high": None if not first_high else first_high.to_dict(),
            "first_touch": None if not touch_any else touch_any[0].to_dict(),
        }

    report = {
        "ok": bool(ict.get("ok")),
        "symbol": bar_payload.get("symbol"),
        "resolution": bar_payload.get("resolution"),
        "bar_count": bar_payload.get("count"),
        "definitions": {k: v.to_dict() for k, v in SESSION_DEFINITIONS.items()},
        "ict": {
            "ok": ict.get("ok"),
            "study_id": ict.get("study_id"),
            "timezone_input": ict.get("timezone"),
            "counts": ict.get("counts"),
            "latest": ict_latest,
        },
        "ohlc_latest_completed": ohlc_latest_completed,
        "discrepancies_ict_vs_ohlc": discrepancies,
        "sweeps": sweep_results,
    }

    out = Path(__file__).resolve().parent / "phase2_validation.json"
    out.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    print(json.dumps(report, indent=2, default=str))
    print(f"\nWrote {out}")


if __name__ == "__main__":
    asyncio.run(main())
