"""Phase 5 validation: FVG entry candidates (live or offline fixture)."""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from entry_config import COMPARE_ENTRY_MODES, DEFAULT_ENTRY_CONFIG
from entry_detect import evaluate_entry_modes
from fvg_config import DEFAULT_FVG_CONFIG
from fvg_detect import detect_fvg
from models import Bar, FVGZone, LiquiditySweep, StructureConfirmation
from sessions_config import SESSION_DST_UNCERTAINTY


def _fmt(ts):
    if ts is None:
        return None
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).isoformat()


def _candidate_brief(c) -> dict:
    return {
        "price": c.price,
        "triggered": c.triggered,
        "status": c.status,
        "time": _fmt(c.trigger_timestamp),
        "bars_after_fvg": c.bars_after_fvg,
        "entry_depth": c.entry_depth,
        "max_retrace_depth": c.max_retrace_depth,
        "fully_filled_before_or_at_entry": (c.extras or {}).get(
            "fully_filled_before_or_at_entry"
        ),
    }


def _offline_fixture() -> dict:
    """
    Deterministic bullish setup where first_touch/boundary trigger before CE.

    Labeled offline — not live CDP.
    """
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
        Bar(time=3_000, open=4321, high=4325, low=4320, close=4324),  # c1
        Bar(time=4_000, open=4324, high=4340, low=4323, close=4338),  # c2
        Bar(time=5_000, open=4338, high=4345, low=4330, close=4342),  # c3 → FVG 4325-4330
        # Retrace into upper zone only (low=4328), CE=4327.5 not reached yet
        Bar(time=6_000, open=4342, high=4343, low=4328, close=4329),
        # Later reaches CE
        Bar(time=7_000, open=4329, high=4330, low=4326, close=4327),
    ]
    fvg_result = detect_fvg(sweep, conf, bars, DEFAULT_FVG_CONFIG)
    assert fvg_result.found, fvg_result.reason
    fvg = fvg_result.zones[0]

    # Snapshot after partial retrace only
    bars_partial = [b for b in bars if b.time <= 6_000]
    partial_modes = evaluate_entry_modes(fvg, bars_partial, COMPARE_ENTRY_MODES)
    full_modes = evaluate_entry_modes(fvg, bars, COMPARE_ENTRY_MODES)

    return {
        "mode": "offline_fixture",
        "note": (
            "Deterministic fixture (not live CDP). Shows first_touch/boundary "
            "triggering while CE still waiting on partial bars, then CE triggers later."
        ),
        "session": sweep.session,
        "sweep": {"side": sweep.side, "level": sweep.level, "time": _fmt(sweep.sweep_timestamp)},
        "choch": {
            "direction": conf.direction,
            "level": conf.level,
            "time": _fmt(conf.event_timestamp),
        },
        "fvg": {
            "direction": fvg.direction,
            "low": fvg.low,
            "high": fvg.high,
            "ce": fvg.midpoint,
            "created": _fmt(fvg.created_timestamp),
            "setup_reference": fvg.setup_reference,
        },
        "entry_candidates_after_partial_retrace": {
            k: _candidate_brief(v) for k, v in partial_modes.items()
        },
        "entry_candidates_after_ce_reach": {
            k: _candidate_brief(v) for k, v in full_modes.items()
        },
        "default_entry_config": DEFAULT_ENTRY_CONFIG.to_dict(),
    }


async def _try_live() -> dict:
    from bars import fetch_bars
    from ict_sessions import fetch_ict_session_ranges
    from liquidity_sweep import detect_sweeps
    from luxalgo_structure import fetch_luxalgo_choch
    from models import SweepRule
    from structure_confirm import confirm_after_sweep

    try:
        bar_payload = await fetch_bars()
    except Exception as exc:  # noqa: BLE001
        return {"mode": "live_unavailable", "error": str(exc)}

    if not bar_payload.get("ok"):
        return {"mode": "live_unavailable", "error": bar_payload.get("error")}

    bars = bar_payload["bars"]
    by_index = bar_payload.get("bars_by_series_index") or {}
    ict = await fetch_ict_session_ranges(bars_by_series_index=by_index)
    lux = await fetch_luxalgo_choch(bars_by_series_index=by_index)
    events = lux.get("events") or []

    def series_index_for_time(ts: int):
        hits = [i for i, t in by_index.items() if int(t) == int(ts)]
        return int(min(hits)) if hits else None

    for name in ("Asia", "London"):
        session = (ict.get("latest") or {}).get(name)
        if not session:
            continue
        for sweep in detect_sweeps(session, bars, rule=SweepRule.WICK_ONLY):
            decision = confirm_after_sweep(
                sweep,
                events,
                sweep_bar_index=series_index_for_time(sweep.sweep_timestamp),
            )
            if not decision.confirmed or decision.confirmation is None:
                continue
            if decision.confirmation.event_timestamp is None:
                continue
            fvg_result = detect_fvg(
                sweep, decision.confirmation, bars, DEFAULT_FVG_CONFIG
            )
            if not fvg_result.found:
                continue
            fvg = fvg_result.zones[0]
            modes = evaluate_entry_modes(fvg, bars, COMPARE_ENTRY_MODES)
            return {
                "mode": "live",
                "symbol": bar_payload.get("symbol"),
                "resolution": bar_payload.get("resolution"),
                "session": sweep.session,
                "sweep": {
                    "side": sweep.side,
                    "level": sweep.level,
                    "time": _fmt(sweep.sweep_timestamp),
                },
                "choch": {
                    "direction": decision.confirmation.direction,
                    "level": decision.confirmation.level,
                    "time": _fmt(decision.confirmation.event_timestamp),
                },
                "fvg": {
                    "direction": fvg.direction,
                    "low": fvg.low,
                    "high": fvg.high,
                    "ce": fvg.midpoint,
                    "created": _fmt(fvg.created_timestamp),
                },
                "entry_candidates": {k: _candidate_brief(v) for k, v in modes.items()},
            }

    return {
        "mode": "live_no_sequence",
        "note": "No reliable live sweep→CHoCH→FVG chain on latest sessions.",
    }


async def main():
    live = await _try_live()
    fixture = _offline_fixture()
    example = live if live.get("mode") == "live" else {**fixture, "live_status": live}

    report = {
        "ok": True,
        "phase": 5,
        "session_dst_uncertainty": SESSION_DST_UNCERTAINTY,
        "example": example,
        "offline_fixture": fixture,
    }
    Path("phase5_validation.json").write_text(
        json.dumps(report, indent=2, default=str), encoding="utf-8"
    )
    print(json.dumps(report, indent=2, default=str))


if __name__ == "__main__":
    asyncio.run(main())
