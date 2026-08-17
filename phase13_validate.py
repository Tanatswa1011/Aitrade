"""Phase 13 validation: trading-day Daily + live MTF if CDP up + MTF journal."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from bias_provider import StructureBiasProvider
from closed_candles import filter_closed_bars
from htf_report import compute_mtf_journal_report
from htf_structure import compute_timeframe_structure_bias
from ohlc_resample import resample_ohlc
from replay_engine import replay_historical_mtf_setups
from replay_fixtures import build_multi_day_fixture_bars
from setup_journal import append_journal_records
from trading_day_config import (
    DEFAULT_TRADING_DAY_CONFIG,
    describe_daily_boundaries,
    infer_trading_day_config_from_native_bars,
    trading_day_close_utc,
    trading_day_open_utc,
)
from datetime import date


def offline_trading_day() -> dict[str, Any]:
    from models import Bar

    cfg = DEFAULT_TRADING_DAY_CONFIG
    spring_open = trading_day_open_utc(cfg, date(2026, 3, 7))
    spring_close = trading_day_close_utc(cfg, date(2026, 3, 7))
    fall_open = trading_day_open_utc(cfg, date(2026, 10, 31))
    fall_close = trading_day_close_utc(cfg, date(2026, 10, 31))
    bars = [
        Bar(time=trading_day_open_utc(cfg, d), open=1, high=2, low=0.5, close=1)
        for d in (
            date(2026, 1, 5),
            date(2026, 1, 6),
            date(2026, 1, 7),
            date(2026, 1, 8),
            date(2026, 1, 9),
        )
    ]
    inferred = infer_trading_day_config_from_native_bars(bars)
    return {
        "default_config": cfg.to_dict(),
        "inferred_from_synthetic_native": inferred.to_dict(),
        "dst_spring_duration_sec": spring_close - spring_open,
        "dst_fall_duration_sec": fall_close - fall_open,
        "closed_bar_rule": "canonical_close <= sweep_ts (NY roll or next native open)",
        "boundaries": describe_daily_boundaries(bars, cfg=cfg),
    }


def offline_mtf_journal() -> dict[str, Any]:
    from tests_phase12 import _bars_down_then_break, _bars_up_then_break

    bars5 = build_multi_day_fixture_bars(days=5)
    # Prefer structure-rich HTF series for descriptive alignment stats;
    # also keep resampled Daily from 5m available in metadata.
    daily_native = _bars_up_then_break()
    h4_native = _bars_down_then_break()
    daily_res = resample_ohlc(bars5, "1D", source_timeframe="5m")
    h4_res = resample_ohlc(bars5, "4H", source_timeframe="5m")
    result = replay_historical_mtf_setups(
        {
            "5m": bars5,
            "1D": daily_native,
            "4H": h4_native,
        },
        symbol="OANDA:XAUUSD",
        execution_timeframes=("5m", "15m"),
        bias_provider=StructureBiasProvider(),
    )
    path = append_journal_records(
        result.journal_records, root=Path("journal") / "phase13_mtf"
    )
    report = compute_mtf_journal_report(result.journal_records)
    return {
        "bars_available": {
            "5m": len(bars5),
            "15m": "resampled_from_5m",
            "4H_structure": len(h4_native),
            "1D_structure": len(daily_native),
            "4H_resampled_from_5m": len(h4_res.bars),
            "1D_resampled_from_5m": len(daily_res.bars),
            "1D_resampled_source": daily_res.source,
        },
        "replay": {
            "total_sessions": result.total_sessions,
            "total_sweeps": result.total_sweeps,
            "total_setups": result.total_setups,
            "warnings": result.warnings,
            "metadata": result.metadata,
        },
        "journal_path": str(path),
        "journal_size": len(result.journal_records),
        "mtf_report": report,
    }


async def live_mtf_validation() -> dict[str, Any]:
    try:
        from cdp import health_check
        from mtf_fetch import fetch_mtf_bar_bundle
        from setup_analyze import analyze_live_session_setup
        from setup_annotate import annotate_trade_setup
        from trading_day_config import infer_trading_day_config_from_native_bars
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "live_tested": False, "error": str(exc)}

    try:
        health = health_check()
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "live_tested": False,
            "cdp_status": "CDP_DOWN",
            "error": str(exc),
            "note": "Live validation blocked — offline tests used",
        }

    if not health.get("cdp_connected"):
        return {
            "ok": False,
            "live_tested": False,
            "cdp_status": "CDP_DOWN",
            "health": health,
            "note": "Live validation blocked — offline tests used",
        }

    out: dict[str, Any] = {"live_tested": True, "cdp_status": "UP", "health": health}
    try:
        bundle = await fetch_mtf_bar_bundle(("1D", "4H", "5m", "15m"), settle_ms=1500)
    except Exception as exc:  # noqa: BLE001
        out["ok"] = False
        out["mtf_error"] = str(exc)
        return out

    out["original_chart_timeframe"] = bundle.get("original_chart_timeframe")
    out["final_chart_timeframe"] = bundle.get("chart_timeframe_after")
    out["restore_ok"] = bundle.get("restore_ok")
    out["switch_log"] = bundle.get("switch_log")
    out["study_trail"] = bundle.get("study_trail")
    out["study_ids_changed"] = bundle.get("study_ids_changed")
    out["study_semantic_stable"] = bundle.get("study_semantic_stable")

    mtf = bundle.get("mtf")
    daily_bars = list(mtf.bars_for("1D")) if mtf else []
    h4_bars = list(mtf.bars_for("4H")) if mtf else []
    if daily_bars:
        inferred = infer_trading_day_config_from_native_bars(daily_bars)
        out["confirmed_daily_boundary"] = inferred.to_dict()
        out["daily_boundary_description"] = describe_daily_boundaries(
            daily_bars, cfg=inferred
        )
    else:
        out["confirmed_daily_boundary"] = {
            "source": "unavailable",
            "fallback": DEFAULT_TRADING_DAY_CONFIG.to_dict(),
        }

    # Bias at latest closed
    if daily_bars and h4_bars:
        as_of = max(int(daily_bars[-1].time), int(h4_bars[-1].time)) + 7 * 86400
        # Prefer last bar's native close via filter
        closed_d = filter_closed_bars(daily_bars, as_of_ts=as_of, timeframe="1D")
        closed_h = filter_closed_bars(h4_bars, as_of_ts=as_of, timeframe="4H")
        d_bias = compute_timeframe_structure_bias(
            daily_bars, timeframe="1D", as_of_ts=as_of
        )
        h_bias = compute_timeframe_structure_bias(
            h4_bars, timeframe="4H", as_of_ts=as_of
        )
        out["live_daily_bias"] = {
            "bars_loaded": len(daily_bars),
            "closed_bars": len(closed_d),
            "latest_closed": None
            if not closed_d
            else {
                "time": closed_d[-1].time,
                "close": closed_d[-1].close,
            },
            **d_bias.to_dict(),
        }
        out["live_h4_bias"] = {
            "bars_loaded": len(h4_bars),
            "closed_bars": len(closed_h),
            "latest_closed": None
            if not closed_h
            else {"time": closed_h[-1].time, "close": closed_h[-1].close},
            **h_bias.to_dict(),
        }

    # Live setup analysis 5m / 15m
    runs = {}
    for tf in ("5m", "15m"):
        try:
            res = await analyze_live_session_setup(
                session="auto",
                execution_timeframe=tf,
                bias_provider="structure",
            )
            setup = res.get("setup") or {}
            htf = res.get("higher_timeframe_context") or {}
            runs[tf] = {
                "ok": res.get("ok"),
                "status": setup.get("status") or res.get("status"),
                "session": setup.get("session"),
                "sweep": setup.get("sweep"),
                "confirmation": bool(setup.get("confirmation")),
                "fvg": bool(setup.get("fvg")),
                "entry": bool(setup.get("entries")),
                "htf": {
                    "alignment": htf.get("alignment"),
                    "daily": (htf.get("daily_bias") or {}).get("direction"),
                    "h4": (htf.get("h4_bias") or {}).get("direction"),
                },
                "setup_vs_daily": setup.get("setup_vs_daily"),
                "setup_vs_h4": setup.get("setup_vs_h4"),
                "execution_timeframe": res.get("execution_timeframe"),
                "original_chart_timeframe": res.get("original_chart_timeframe"),
                "chart_timeframe_after_analysis": res.get(
                    "chart_timeframe_after_analysis"
                ),
                "restore_ok": res.get("restore_ok"),
            }
        except Exception as exc:  # noqa: BLE001
            runs[tf] = {"ok": False, "error": str(exc)}
    out["runs"] = runs

    # Descriptive 5m vs 15m
    r5, r15 = runs.get("5m") or {}, runs.get("15m") or {}
    out["descriptive_5m_vs_15m"] = {
        "same_liquidity_event": (r5.get("sweep") or {}).get("sweep_timestamp")
        == (r15.get("sweep") or {}).get("sweep_timestamp"),
        "same_htf_context": r5.get("htf") == r15.get("htf"),
        "5m": {
            "status": r5.get("status"),
            "confirmation": r5.get("confirmation"),
            "fvg": r5.get("fvg"),
            "entry": r5.get("entry"),
        },
        "15m": {
            "status": r15.get("status"),
            "confirmation": r15.get("confirmation"),
            "fvg": r15.get("fvg"),
            "entry": r15.get("entry"),
        },
    }

    # Optional annotate 5m only
    if r5.get("ok") and (r5.get("status") not in (None, "NO_SETUP", "EXPIRED")):
        try:
            full = await analyze_live_session_setup(
                session="auto",
                execution_timeframe="5m",
                bias_provider="structure",
            )
            if full.get("setup"):
                ann = await annotate_trade_setup(
                    full["setup"], take_screenshot_after=True
                )
                out["annotation_5m"] = {
                    "ok": ann.get("ok"),
                    "setup_id": ann.get("setup_id"),
                    "annotations_created_count": ann.get("annotations_created_count"),
                    "screenshot_path": ann.get("screenshot_path"),
                    "error": ann.get("error"),
                }
        except Exception as exc:  # noqa: BLE001
            out["annotation_5m"] = {"ok": False, "error": str(exc)}

    out["ok"] = bool(bundle.get("ok")) and bool(out.get("restore_ok"))
    return out


async def main() -> None:
    offline_td = offline_trading_day()
    offline_j = offline_mtf_journal()
    live = await live_mtf_validation()
    report = {
        "ok": True,
        "phase": 13,
        "offline_trading_day": offline_td,
        "offline_mtf_journal": {
            k: v
            for k, v in offline_j.items()
            if k != "mtf_report"
        },
        "mtf_report_summary": {
            "journal_size": offline_j["journal_size"],
            "htf_alignment_distribution": offline_j["mtf_report"][
                "htf_alignment_distribution"
            ],
            "paired_5m_15m_n": offline_j["mtf_report"]["paired_5m_15m"][
                "paired_event_count"
            ],
            "metrics_by_execution_timeframe": {
                tf: {
                    "setup_count": block.get("setup_count"),
                    "sample_warning": block.get("sample_warning"),
                }
                for tf, block in offline_j["mtf_report"][
                    "metrics_by_execution_timeframe"
                ].items()
            },
            "daily_vs_h4": {
                k: {"n": v.get("n"), "sample_warning": v.get("sample_warning")}
                for k, v in offline_j["mtf_report"]["daily_vs_h4_contribution"].items()
            },
        },
        "mtf_report_full": offline_j["mtf_report"],
        "live": live,
    }
    Path("phase13_validation.json").write_text(
        json.dumps(report, indent=2, default=str), encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "phase": 13,
                "cdp": live.get("cdp_status"),
                "restore_ok": live.get("restore_ok"),
                "daily_roll": (
                    live.get("confirmed_daily_boundary")
                    or offline_td["default_config"]
                ).get("day_roll_time")
                if isinstance(
                    live.get("confirmed_daily_boundary") or offline_td["default_config"],
                    dict,
                )
                else offline_td["default_config"]["day_roll_time"],
                "dst_spring_h": offline_td["dst_spring_duration_sec"] / 3600,
                "dst_fall_h": offline_td["dst_fall_duration_sec"] / 3600,
                "journal_size": offline_j["journal_size"],
                "live_ok": live.get("ok"),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
