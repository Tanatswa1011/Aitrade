"""Phase 4 validation: setup-linked FVG after sweep → CHoCH."""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fvg_config import DEFAULT_FVG_CONFIG
from fvg_detect import detect_fvg
from models import Bar, FVGConfig, LiquiditySweep, StructureConfirmation
from sessions_config import SESSION_DST_UNCERTAINTY


def _fmt(ts):
    if ts is None:
        return None
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).isoformat()


def _fixture_example() -> dict:
    """Deterministic offline fixture — clearly labeled, not live."""
    sweep = LiquiditySweep(
        session="Asia",
        side="low",
        level=4311.04,
        sweep_timestamp=1_000,
        sweep_price=4310.0,
        maximum_excursion=1.04,
        reclaim_status=True,
        rule="wick_only",
        sweep_candle=Bar(time=1_000, open=4312, high=4313, low=4310, close=4312),
    )
    conf = StructureConfirmation(
        kind="CHoCH",
        direction="bullish",
        level=4320.0,
        event_timestamp=2_000,
        event_bar_index=10,
        source="luxalgo",
        study_id="smUEv2",
        raw_id="fixture",
        timing_confidence="exact",
    )
    bars = [
        Bar(time=1_000, open=4312, high=4313, low=4310, close=4312),
        Bar(time=2_000, open=4315, high=4322, low=4314, close=4320),
        Bar(time=3_000, open=4321, high=4325, low=4320, close=4324),  # c1 h=4325
        Bar(time=4_000, open=4324, high=4340, low=4323, close=4338),
        Bar(time=5_000, open=4338, high=4345, low=4330, close=4342),  # c3 l=4330 → gap
        Bar(time=6_000, open=4342, high=4344, low=4328, close=4329),  # mitigate
        Bar(time=7_000, open=4329, high=4331, low=4324, close=4326),  # full fill
    ]
    result = detect_fvg(sweep, conf, bars, DEFAULT_FVG_CONFIG)
    z = result.zones[0] if result.found else None
    return {
        "mode": "offline_fixture",
        "note": (
            "Deterministic fixture (not live CDP). Used when no reliable live "
            "sweep→CHoCH sequence is available."
        ),
        "session": sweep.session,
        "sweep_side": sweep.side,
        "sweep_level": sweep.level,
        "sweep_time": _fmt(sweep.sweep_timestamp),
        "choch": {
            "direction": conf.direction,
            "level": conf.level,
            "time": _fmt(conf.event_timestamp),
        },
        "fvg": None
        if z is None
        else {
            "direction": z.direction,
            "low": z.low,
            "high": z.high,
            "midpoint": z.midpoint,
            "created_time": _fmt(z.created_timestamp),
            "gap_size": z.gap_size,
            "bars_after_sweep": z.bars_after_sweep,
            "bars_after_choch": z.bars_after_confirmation,
            "mitigated": z.mitigated,
            "first_mitigation_time": _fmt(z.first_mitigation_timestamp),
            "fully_filled": z.fully_filled,
            "first_full_fill_time": _fmt(z.first_full_fill_timestamp),
            "setup_reference": z.setup_reference,
        },
        "detection_reason": result.reason,
    }


async def _live_example() -> dict | None:
    from bars import fetch_bars
    from ict_sessions import fetch_ict_session_ranges
    from liquidity_sweep import detect_sweeps
    from luxalgo_structure import fetch_luxalgo_choch
    from models import SweepRule
    from structure_confirm import confirm_after_sweep

    try:
        bar_payload = await fetch_bars()
    except Exception as exc:  # noqa: BLE001
        return {
            "mode": "live_unavailable",
            "error": str(exc),
            "fallback": "offline_fixture",
        }

    if not bar_payload.get("ok"):
        return {
            "mode": "live_unavailable",
            "error": bar_payload.get("error"),
            "fallback": "offline_fixture",
        }

    bars = bar_payload["bars"]
    by_index = bar_payload.get("bars_by_series_index") or {}
    ict = await fetch_ict_session_ranges(bars_by_series_index=by_index)
    lux = await fetch_luxalgo_choch(bars_by_series_index=by_index)
    choch_events = lux.get("events") or []

    def series_index_for_time(ts: int) -> int | None:
        hits = [i for i, t in by_index.items() if int(t) == int(ts)]
        return int(min(hits)) if hits else None

    attempts = []
    for name in ("Asia", "London"):
        session = (ict.get("latest") or {}).get(name)
        if not session:
            continue
        for sweep in detect_sweeps(session, bars, rule=SweepRule.WICK_ONLY):
            decision = confirm_after_sweep(
                sweep,
                choch_events,
                sweep_bar_index=series_index_for_time(sweep.sweep_timestamp),
            )
            if not decision.confirmed or decision.confirmation is None:
                attempts.append(
                    {
                        "session": name,
                        "sweep_side": sweep.side,
                        "status": decision.reason,
                    }
                )
                continue
            # Need timestamp on confirmation for FVG search.
            if decision.confirmation.event_timestamp is None:
                attempts.append(
                    {
                        "session": name,
                        "sweep_side": sweep.side,
                        "status": "choch_confirmed_but_no_timestamp_for_fvg",
                    }
                )
                continue

            fvg_result = detect_fvg(
                sweep, decision.confirmation, bars, DEFAULT_FVG_CONFIG
            )
            z = fvg_result.zones[0] if fvg_result.found else None
            example = {
                "mode": "live",
                "symbol": bar_payload.get("symbol"),
                "resolution": bar_payload.get("resolution"),
                "session": sweep.session,
                "sweep_side": sweep.side,
                "sweep_level": sweep.level,
                "sweep_time": _fmt(sweep.sweep_timestamp),
                "choch": {
                    "direction": decision.confirmation.direction,
                    "level": decision.confirmation.level,
                    "time": _fmt(decision.confirmation.event_timestamp),
                    "timing_confidence": decision.confirmation.timing_confidence,
                },
                "fvg": None
                if z is None
                else {
                    "direction": z.direction,
                    "low": z.low,
                    "high": z.high,
                    "midpoint": z.midpoint,
                    "created_time": _fmt(z.created_timestamp),
                    "gap_size": z.gap_size,
                    "bars_after_sweep": z.bars_after_sweep,
                    "bars_after_choch": z.bars_after_confirmation,
                    "mitigated": z.mitigated,
                    "first_mitigation_time": _fmt(z.first_mitigation_timestamp),
                    "fully_filled": z.fully_filled,
                    "first_full_fill_time": _fmt(z.first_full_fill_timestamp),
                    "setup_reference": z.setup_reference,
                },
                "fvg_reason": fvg_result.reason,
                "status": "fvg_found" if fvg_result.found else fvg_result.reason,
            }
            return example

    return {
        "mode": "live_no_sequence",
        "note": (
            "CDP reachable but no reliable wick-sweep → CHoCH → FVG sequence "
            "on latest ICT Asia/London ranges."
        ),
        "attempts": attempts,
        "fallback": "offline_fixture",
    }


async def main():
    live = await _live_example()
    fixture = _fixture_example()

    if live and live.get("mode") == "live":
        primary = live
    else:
        primary = {**fixture, "live_status": live}

    report = {
        "ok": True,
        "phase": 4,
        "session_dst_uncertainty": SESSION_DST_UNCERTAINTY,
        "fvg_config": DEFAULT_FVG_CONFIG.to_dict(),
        "example": primary,
        "offline_fixture": fixture,
    }
    path = Path("phase4_validation.json")
    path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    print(json.dumps(report, indent=2, default=str))


if __name__ == "__main__":
    asyncio.run(main())
