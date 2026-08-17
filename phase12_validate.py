"""Phase 12 validation: structure bias offline + live MTF if CDP up."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from bias_provider import StructureBiasProvider
from htf_structure import compute_timeframe_structure_bias
from models import Bar
from tests_phase12 import _bars_down_then_break, _bars_up_then_break


def offline_structure() -> dict:
    daily = _bars_up_then_break()
    h4 = _bars_down_then_break()
    from datetime import date
    from trading_day_config import DEFAULT_TRADING_DAY_CONFIG, trading_day_close_utc

    # Match Phase 13 Daily close semantics for last weekday in fixture
    as_of = max(int(daily[-1].time) + 86400, int(h4[-1].time) + 14400)
    d = compute_timeframe_structure_bias(daily, timeframe="1D", as_of_ts=as_of)
    h = compute_timeframe_structure_bias(h4, timeframe="4H", as_of_ts=as_of)
    ctx = StructureBiasProvider().get_context(
        as_of_ts=as_of, daily_bars=daily, h4_bars=h4
    )
    return {
        "daily": d.to_dict(),
        "h4": h.to_dict(),
        "alignment": ctx.alignment,
        "provider": ctx.source_metadata.get("provider"),
        "algorithm_version": ctx.source_metadata.get("algorithm_version"),
    }


async def live_validation() -> dict:
    try:
        from setup_analyze import analyze_live_session_setup
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc), "live_tested": False}

    out = {"live_tested": True, "runs": {}}
    for tf in ("5m", "15m"):
        try:
            res = await analyze_live_session_setup(
                session="auto",
                execution_timeframe=tf,
                bias_provider="structure",
            )
        except Exception as exc:  # noqa: BLE001
            out["runs"][tf] = {"ok": False, "error": str(exc)}
            continue
        out["runs"][tf] = {
            "ok": res.get("ok"),
            "status": res.get("status"),
            "bias_source": res.get("bias_source"),
            "daily": res.get("daily_bias_summary"),
            "h4": res.get("h4_bias_summary"),
            "execution_timeframe": res.get("execution_timeframe"),
            "bars_obtained": res.get("bars_obtained"),
            "original_chart_timeframe": res.get("original_chart_timeframe"),
            "chart_timeframe_after_analysis": res.get("chart_timeframe_after_analysis"),
            "restore_ok": res.get("restore_ok"),
            "study_ids_changed": res.get("study_ids_changed"),
            "mtf_fetched": res.get("mtf_fetched"),
            "choch_found": bool((res.get("setup") or {}).get("confirmation")),
            "fvg_found": bool((res.get("setup") or {}).get("fvg")),
            "setup_status": (res.get("setup") or {}).get("status"),
            "htf_alignment": (res.get("higher_timeframe_context") or {}).get(
                "alignment"
            ),
        }
    out["ok"] = any(v.get("ok") for v in out["runs"].values())
    return out


async def main():
    offline = offline_structure()
    live = await live_validation()
    report = {"ok": True, "phase": 12, "offline": offline, "live": live}
    Path("phase12_validation.json").write_text(
        json.dumps(report, indent=2, default=str), encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "offline_daily": offline["daily"]["direction"],
                "offline_h4": offline["h4"]["direction"],
                "offline_alignment": offline["alignment"],
                "live_ok": live.get("ok"),
                "live_5m": (live.get("runs") or {}).get("5m"),
                "live_15m": (live.get("runs") or {}).get("15m"),
            },
            indent=2,
            default=str,
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
