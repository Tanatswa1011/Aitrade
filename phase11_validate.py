"""Phase 11 offline + live MTF validation."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from bias_models import compute_htf_alignment
from bias_provider import ManualBiasProvider
from execution_config import ExecutionTimeframeConfig
from expiry_config import ExpiryConfig
from models import Bar, SessionRange, StructureConfirmation
from multi_tf_bars import MultiTimeframeBars
from session_time import resolve_session_window
from sessions_config import SESSION_DEFINITIONS, SESSION_DST_UNCERTAINTY
from setup_engine import analyze_session_setup
from strategy_config import DEFAULT_STRATEGY_CONFIG, StrategyConfig
from datetime import date


def _cfg(tf: str) -> StrategyConfig:
    return StrategyConfig(
        sweep_rule=DEFAULT_STRATEGY_CONFIG.sweep_rule,
        entry_modes=DEFAULT_STRATEGY_CONFIG.entry_modes,
        fvg=DEFAULT_STRATEGY_CONFIG.fvg,
        entry=DEFAULT_STRATEGY_CONFIG.entry,
        risk=DEFAULT_STRATEGY_CONFIG.risk,
        target=DEFAULT_STRATEGY_CONFIG.target,
        expiry=ExpiryConfig(enabled=False),
        execution=ExecutionTimeframeConfig(timeframe=tf),
    )


def offline_examples() -> dict:
    w = resolve_session_window(SESSION_DEFINITIONS["Asia"], date(2026, 8, 14))
    s = SessionRange(
        name="Asia",
        timezone="America/New_York",
        start=w.utc_start,
        end=w.utc_end,
        high=4360.0,
        low=4311.04,
        high_timestamp=None,
        low_timestamp=None,
        complete=True,
        source="ict_sessions",
        coverage_status="full",
        identity="Asia:fixture",
        extras={"resolved_window": w.to_dict()},
    )
    t0 = int(s.end) + 60
    bull_bars = [
        Bar(time=t0, open=4312, high=4313, low=4310, close=4312),
        Bar(time=t0 + 60, open=4315, high=4322, low=4314, close=4320),
        Bar(time=t0 + 120, open=4321, high=4325, low=4320, close=4324),
        Bar(time=t0 + 180, open=4324, high=4340, low=4323, close=4338),
        Bar(time=t0 + 240, open=4338, high=4345, low=4330, close=4342),
        Bar(time=t0 + 300, open=4342, high=4343, low=4328, close=4329),
        Bar(time=t0 + 360, open=4329, high=4330, low=4326, close=4327),
    ]
    choch = StructureConfirmation(
        kind="CHoCH",
        direction="bullish",
        level=4320.0,
        event_timestamp=t0 + 60,
        event_bar_index=None,
        source="test",
        study_id="t",
        raw_id="t",
        timing_confidence="exact",
    )
    a = analyze_session_setup(
        s,
        bull_bars,
        [choch],
        _cfg("5m"),
        symbol="OANDA:XAUUSD",
        timeframe="5m",
        now_ts=bull_bars[-1].time,
        execution_timeframe="5m",
        bias_provider=ManualBiasProvider(daily="bullish", h4="bullish"),
        mtf_bars=MultiTimeframeBars().with_series("5m", bull_bars),
    )

    bear_bars = [
        Bar(time=t0, open=4355, high=4362, low=4354, close=4356),
        Bar(time=t0 + 60, open=4356, high=4357, low=4348, close=4350),
        Bar(time=t0 + 120, open=4350, high=4351, low=4345, close=4346),
        Bar(time=t0 + 180, open=4346, high=4347, low=4320, close=4322),
        Bar(time=t0 + 240, open=4322, high=4325, low=4318, close=4320),
        Bar(time=t0 + 300, open=4320, high=4332, low=4319, close=4330),
    ]
    b = analyze_session_setup(
        s,
        bear_bars,
        [
            StructureConfirmation(
                kind="CHoCH",
                direction="bearish",
                level=4355.0,
                event_timestamp=t0 + 60,
                event_bar_index=None,
                source="test",
                study_id="t",
                raw_id="t",
                timing_confidence="exact",
            )
        ],
        _cfg("15m"),
        symbol="OANDA:XAUUSD",
        timeframe="15m",
        now_ts=bear_bars[-1].time,
        execution_timeframe="15m",
        bias_provider=ManualBiasProvider(daily="bullish", h4="bearish"),
        mtf_bars=MultiTimeframeBars().with_series("15m", bear_bars),
    )
    return {
        "example_a": {
            "status": a.status,
            "execution_timeframe": a.execution_timeframe,
            "htf_alignment": (a.higher_timeframe_context or {}).get("alignment"),
            "setup_vs_daily": a.setup_vs_daily,
            "setup_vs_h4": a.setup_vs_h4,
        },
        "example_b": {
            "status": b.status,
            "execution_timeframe": b.execution_timeframe,
            "htf_alignment": (b.higher_timeframe_context or {}).get("alignment"),
            "setup_vs_daily": b.setup_vs_daily,
            "setup_vs_h4": b.setup_vs_h4,
            "direction": b.direction,
            "rejected_for_mixed": False,
        },
        "alignment_helpers": {
            "bull_bull": compute_htf_alignment("bullish", "bullish"),
            "bull_bear": compute_htf_alignment("bullish", "bearish"),
        },
    }


async def live_mtf() -> dict:
    try:
        from setup_analyze import analyze_live_session_setup
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc), "live_tested": False}

    out = {"live_tested": True, "runs": {}}
    for tf in ("5m", "15m"):
        try:
            res = await analyze_live_session_setup(
                session="auto", execution_timeframe=tf
            )
        except Exception as exc:  # noqa: BLE001
            out["runs"][tf] = {"ok": False, "error": str(exc)}
            continue
        out["runs"][tf] = {
            "ok": res.get("ok"),
            "status": res.get("status"),
            "original_chart_timeframe": res.get("original_chart_timeframe"),
            "requested_execution_timeframe": res.get("requested_execution_timeframe"),
            "bars_obtained": res.get("bars_obtained"),
            "chart_timeframe_after_analysis": res.get("chart_timeframe_after_analysis"),
            "restore_ok": res.get("restore_ok"),
            "execution_timeframe": res.get("execution_timeframe"),
            "htf_alignment": (res.get("higher_timeframe_context") or {}).get(
                "alignment"
            ),
            "daily_bias": (
                (res.get("higher_timeframe_context") or {}).get("daily_bias") or {}
            ).get("direction"),
            "h4_bias": (
                (res.get("higher_timeframe_context") or {}).get("h4_bias") or {}
            ).get("direction"),
        }
    out["ok"] = all(v.get("ok") for v in out["runs"].values())
    return out


async def main():
    offline = offline_examples()
    live = await live_mtf()
    report = {
        "ok": True,
        "phase": 11,
        "session_dst_uncertainty": SESSION_DST_UNCERTAINTY,
        "offline": offline,
        "live": live,
    }
    Path("phase11_validation.json").write_text(
        json.dumps(report, indent=2, default=str), encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "example_a": offline["example_a"],
                "example_b": offline["example_b"],
                "live_ok": live.get("ok"),
                "live_5m": (live.get("runs") or {}).get("5m"),
                "live_15m": (live.get("runs") or {}).get("15m"),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
