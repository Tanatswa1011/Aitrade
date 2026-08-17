"""Phase 14 validation: live MTF proof + historical evidence expansion."""

from __future__ import annotations

import asyncio
import csv
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from bar_dataset import load_dataset, merge_and_write, write_dataset
from bias_provider import StructureBiasProvider
from chart_symbol import get_chart_symbol, set_chart_symbol
from chart_timeframe import get_chart_resolution
from closed_candles import filter_closed_bars, latest_closed_bar
from daily_boundary_evidence import (
    apply_evidence_to_default,
    build_daily_boundary_evidence,
)
from htf_report import compute_mtf_journal_report
from htf_structure import compute_timeframe_structure_bias
from luxalgo_capture import append_luxalgo_captures, load_luxalgo_captures
from luxalgo_overlap import compare_choch_overlap
from models import Bar
from mtf_fetch import fetch_mtf_bar_bundle
from replay_engine import replay_historical_mtf_setups
from replay_fixtures import build_multi_day_fixture_bars
from setup_journal import append_journal_records
from strategy_version import STRATEGY_VERSION
from timeframe import timeframe_seconds
from trading_day_config import DEFAULT_TRADING_DAY_CONFIG
from tv_desktop import cdp_preflight, discover_tradingview, launch_tradingview_with_cdp


SYMBOL = "OANDA:XAUUSD"
REPORTS_DIR = Path("reports")


def _bar_summary(bars: list[Bar], timeframe: str, as_of: Optional[int] = None) -> dict[str, Any]:
    if not bars:
        return {
            "bars_returned": 0,
            "earliest_timestamp": None,
            "latest_timestamp": None,
            "latest_closed_bar": None,
        }
    as_of_ts = as_of if as_of is not None else int(bars[-1].time) + 7 * 86400
    closed = latest_closed_bar(bars, as_of_ts=as_of_ts, timeframe=timeframe)
    return {
        "bars_returned": len(bars),
        "earliest_timestamp": int(bars[0].time),
        "latest_timestamp": int(bars[-1].time),
        "latest_closed_bar": None
        if closed is None
        else {
            "time": int(closed.time),
            "open": closed.open,
            "high": closed.high,
            "low": closed.low,
            "close": closed.close,
        },
    }


def _setup_snapshot(setup: Optional[dict[str, Any]]) -> dict[str, Any]:
    if not setup:
        return {"status": None}
    conf = setup.get("confirmation") or {}
    fvg = setup.get("fvg") or {}
    entries = setup.get("entries") or []
    entry_modes = {}
    for e in entries:
        # EntryAnalysis may be dict-serialized
        if hasattr(e, "to_dict"):
            e = e.to_dict()
        mode = (e.get("entry") or {}).get("mode") if isinstance(e.get("entry"), dict) else e.get("mode")
        if mode is None and isinstance(e.get("entry"), dict):
            mode = e["entry"].get("mode")
        # TradeSetup.entries are EntryAnalysis objects in live path often already dicts
        if isinstance(e, dict) and "entry" in e:
            ent = e.get("entry") or {}
            risk = e.get("risk") or {}
            tgt = e.get("target") or {}
            entry_modes[ent.get("mode") or "unknown"] = {
                "triggered": ent.get("triggered"),
                "entry_price": ent.get("entry_price"),
                "entry_timestamp": ent.get("entry_timestamp"),
                "risk": risk,
                "targets": tgt,
            }
    return {
        "session": setup.get("session"),
        "trading_date": setup.get("trading_date"),
        "direction": setup.get("direction"),
        "status": setup.get("status"),
        "sweep": setup.get("sweep"),
        "choch": {
            "found": bool(conf),
            "direction": conf.get("direction"),
            "time": conf.get("event_timestamp"),
            "level": conf.get("level"),
        },
        "fvg": {
            "found": bool(fvg),
            "time": fvg.get("created_timestamp"),
            "zone": {"low": fvg.get("low"), "high": fvg.get("high"), "mid": fvg.get("midpoint")},
        },
        "entry_modes": entry_modes,
        "setup_vs_daily": setup.get("setup_vs_daily"),
        "setup_vs_h4": setup.get("setup_vs_h4"),
        "execution_timeframe": setup.get("execution_timeframe"),
        "id": setup.get("id"),
        "liquidity_event_id": (setup.get("source_metadata") or {}).get("liquidity_event_id"),
    }


async def ensure_cdp(*, may_launch: bool = True) -> dict[str, Any]:
    discovery = discover_tradingview()
    pre = cdp_preflight(discovery=discovery)
    launch_info = {"reused": True, "launched": False}
    if not pre.cdp_reachable and may_launch:
        launch_info = launch_tradingview_with_cdp(
            executable=discovery.executable,
            kill_existing=True,
            wait_seconds=30,
        )
        # wait for chart page
        for _ in range(20):
            pre = cdp_preflight()
            if pre.cdp_reachable and pre.chart_targets:
                break
            await asyncio.sleep(1.5)
    return {
        "discovery": discovery.to_dict(),
        "preflight": pre.to_dict(),
        "launch": launch_info,
        "ok": bool(pre.cdp_reachable and pre.chart_targets),
    }


async def live_mtf_proof() -> dict[str, Any]:
    from setup_analyze import analyze_live_session_setup
    from setup_annotate import annotate_trade_setup

    boot = await ensure_cdp(may_launch=True)
    if not boot.get("ok"):
        return {
            "ok": False,
            "live_tested": False,
            "cdp_status": "CDP_DOWN",
            "boot": boot,
            "note": "Live portion failed explicitly — fixtures not labeled live",
        }

    out: dict[str, Any] = {
        "ok": True,
        "live_tested": True,
        "cdp_status": "UP",
        "boot": boot,
    }

    original_sym = await get_chart_symbol()
    original_tf = await get_chart_resolution()
    out["original"] = {
        "symbol": original_sym.get("symbol"),
        "timeframe": original_tf.get("resolution"),
    }

    # Prefer OANDA:XAUUSD for Phase 14 evidence; restore after.
    switch = {"requested": SYMBOL, "switched": False}
    if original_sym.get("symbol") != SYMBOL:
        sw = await set_chart_symbol(SYMBOL)
        await asyncio.sleep(4.0)
        switch["switched"] = bool(sw.get("ok"))
        switch["result"] = sw
        # wait for studies to bind to new symbol
        for _ in range(6):
            from mtf_fetch import _study_snapshot
            from study_discovery import rediscover_studies

            snap = await _study_snapshot()
            red = rediscover_studies(snap.get("studies") or [])
            if red.get("ok"):
                switch["studies_ready"] = red
                break
            await asyncio.sleep(1.0)
    out["symbol_switch"] = switch

    try:
        bundle = await fetch_mtf_bar_bundle(("1D", "4H", "15m", "5m"), settle_ms=1800)
    except Exception as exc:  # noqa: BLE001
        out["ok"] = False
        out["mtf_error"] = str(exc)
        # attempt restore symbol/tf best-effort
        if original_sym.get("symbol"):
            await set_chart_symbol(str(original_sym["symbol"]))
        return out

    out["mtf"] = {
        "original_chart_timeframe": bundle.get("original_chart_timeframe"),
        "final_chart_timeframe": bundle.get("chart_timeframe_after"),
        "restore_ok": bundle.get("restore_ok"),
        "switch_log": bundle.get("switch_log"),
        "study_trail": bundle.get("study_trail"),
        "study_ids_changed": bundle.get("study_ids_changed"),
        "study_semantic_stable": bundle.get("study_semantic_stable"),
        "errors": bundle.get("errors"),
    }

    mtf = bundle.get("mtf")
    series = {}
    persisted = {}
    from chart_history import fetch_expanded_bars
    from chart_timeframe import set_chart_resolution
    from multi_tf_bars import MultiTimeframeBars

    expanded_meta = {}
    for tf in ("1D", "4H", "15m", "5m"):
        await set_chart_resolution(tf)
        await asyncio.sleep(1.5)
        # Expand loaded history (TV keeps a finite window unless scrolled).
        scrolls = 12 if tf in ("5m", "15m") else 6
        expanded = await fetch_expanded_bars(scrolls=scrolls, scroll_bars=300, max_bars=8000)
        bars = list(expanded.get("bars") or [])
        if not bars and mtf:
            bars = list(mtf.bars_for(tf) or [])
        series[tf] = bars
        expanded_meta[tf] = {
            "bar_count": len(bars),
            "scrolls_ok": expanded.get("scrolls_ok"),
            "earliest": expanded.get("earliest"),
            "latest": expanded.get("latest"),
            "errors": expanded.get("errors"),
        }
        summary = _bar_summary(bars, tf)
        actual = None
        for row in bundle.get("switch_log") or []:
            if row.get("requested_timeframe") == tf:
                actual = row.get("actual_timeframe_after_switch")
                row.update(summary)
        out.setdefault("native_bars", {})[tf] = {
            "requested_timeframe": tf,
            "actual_tradingview_resolution": actual or expanded.get("resolution"),
            **summary,
            "expanded": expanded_meta[tf],
        }
        if bars:
            period = timeframe_seconds(tf)
            persisted[tf] = write_dataset(
                bars,
                symbol=SYMBOL,
                timeframe=tf,
                source="tradingview_native_expanded",
                expected_period_sec=period if tf in ("5m", "15m") else None,
            )

    out["history_limit_note"] = (
        "TradingView Desktop CDP series.data() is capped at ~300 bars on this client; "
        "scroll/goto did not expose additional older 5m/15m history. "
        f"Actual maximum persisted: "
        + ", ".join(f"{tf}={len(series.get(tf) or [])}" for tf in ("5m", "15m", "4H", "1D"))
        + ". Session objective 100+ cannot be met without a deeper history source."
    )

    # Restore chart TF to the bundle's original before further live analysis hops.
    if bundle.get("original_chart_timeframe") is not None:
        await set_chart_resolution(str(bundle["original_chart_timeframe"]))
        await asyncio.sleep(1.0)

    # Daily boundary evidence
    daily_bars = series.get("1D") or []
    evidence = build_daily_boundary_evidence(daily_bars, symbol=SYMBOL, min_bars=10)
    trading_day = apply_evidence_to_default(evidence)
    Path("data").mkdir(parents=True, exist_ok=True)
    Path("data/daily_boundary_evidence.json").write_text(
        json.dumps(evidence.to_dict(), indent=2, default=str), encoding="utf-8"
    )
    out["daily_boundary_evidence"] = evidence.to_dict()
    out["final_daily_roll"] = trading_day.to_dict()

    # Bias live
    as_of = None
    if daily_bars:
        as_of = int(daily_bars[-1].time) + 3 * 86400
    if series.get("4H"):
        as_of = max(as_of or 0, int(series["4H"][-1].time) + 14400)
    as_of = as_of or int(datetime.now(tz=timezone.utc).timestamp())

    d_bias = compute_timeframe_structure_bias(daily_bars, timeframe="1D", as_of_ts=as_of)
    h_bias = compute_timeframe_structure_bias(
        series.get("4H") or [], timeframe="4H", as_of_ts=as_of
    )
    from closed_candles import filter_closed_bars as _fc

    closed_d = _fc(daily_bars, as_of_ts=as_of, timeframe="1D", trading_day=trading_day)
    closed_h = _fc(series.get("4H") or [], as_of_ts=as_of, timeframe="4H")
    provider = StructureBiasProvider()
    ctx = provider.get_context(
        as_of_ts=as_of,
        daily_bars=daily_bars,
        h4_bars=series.get("4H") or [],
    )
    out["live_daily_bias"] = {
        "bars_loaded": len(daily_bars),
        "closed_bars": len(closed_d),
        **d_bias.to_dict(),
    }
    out["live_h4_bias"] = {
        "bars_loaded": len(series.get("4H") or []),
        "closed_bars": len(closed_h),
        **h_bias.to_dict(),
    }
    out["higher_timeframe_context"] = ctx.to_dict()

    # No-lookahead example: use latest 5m bar mid as synthetic sweep if no setup yet
    sweep_example = None
    bars5 = series.get("5m") or []
    if bars5 and daily_bars:
        # pick a timestamp near 70% of history for a historical sweep example
        idx = max(0, int(len(bars5) * 0.7))
        sweep_ts = int(bars5[idx].time)
        d_closed = filter_closed_bars(
            daily_bars, as_of_ts=sweep_ts, timeframe="1D", trading_day=trading_day
        )
        h_closed = filter_closed_bars(
            series.get("4H") or [], as_of_ts=sweep_ts, timeframe="4H"
        )
        sweep_example = {
            "sweep_timestamp": sweep_ts,
            "daily_latest_usable": None
            if not d_closed
            else {
                "open": int(d_closed[-1].time),
                "close_boundary": next(
                    (
                        int(b.time)
                        for b in daily_bars
                        if int(b.time) > int(d_closed[-1].time)
                    ),
                    None,
                ),
                "close": d_closed[-1].close,
            },
            "h4_latest_usable": None
            if not h_closed
            else {
                "open": int(h_closed[-1].time),
                "close_boundary": int(h_closed[-1].time) + 14400,
                "close": h_closed[-1].close,
            },
            "excluded_future_daily_count": sum(
                1 for b in daily_bars if int(b.time) > sweep_ts
            ),
            "excluded_future_h4_count": sum(
                1 for b in (series.get("4H") or []) if int(b.time) > sweep_ts
            ),
        }
    out["no_lookahead_example"] = sweep_example

    # Live 5m / 15m analyses
    runs = {}
    for tf in ("5m", "15m"):
        try:
            res = await analyze_live_session_setup(
                session="auto",
                execution_timeframe=tf,
                bias_provider="structure",
            )
            if res.get("status") == "NO_SETUP":
                # Fall back to explicit sessions — do not invent ENTRY_READY.
                for sess in ("london", "asia"):
                    alt = await analyze_live_session_setup(
                        session=sess,
                        execution_timeframe=tf,
                        bias_provider="structure",
                    )
                    if alt.get("setup") or (
                        alt.get("status") not in (None, "NO_SETUP", "WAITING_FOR_SESSION")
                    ):
                        res = alt
                        res["session_fallback"] = sess
                        break
            setup = res.get("setup") or {}
            if hasattr(setup, "to_dict"):
                setup = setup.to_dict()
            runs[tf] = {
                "ok": res.get("ok"),
                "status": res.get("status") or setup.get("status"),
                "setup": _setup_snapshot(setup) if setup else {"status": res.get("status")},
                "auto_session": res.get("auto_session"),
                "explanation": res.get("explanation"),
                "session_fallback": res.get("session_fallback"),
                "htf": res.get("higher_timeframe_context"),
                "daily_bias_summary": res.get("daily_bias_summary"),
                "h4_bias_summary": res.get("h4_bias_summary"),
                "original_chart_timeframe": res.get("original_chart_timeframe"),
                "chart_timeframe_after_analysis": res.get(
                    "chart_timeframe_after_analysis"
                ),
                "restore_ok": res.get("restore_ok"),
                "error": res.get("error"),
            }
        except Exception as exc:  # noqa: BLE001
            runs[tf] = {"ok": False, "error": str(exc)}
    out["runs"] = runs

    # LuxAlgo capture on current (restored) chart — prefer 5m
    lux_stats = {}
    try:
        from bars import fetch_bars
        from luxalgo_structure import fetch_luxalgo_choch
        from chart_timeframe import set_chart_resolution

        cur = await get_chart_resolution()
        for tf in ("5m", "15m"):
            await set_chart_resolution(tf)
            await asyncio.sleep(1.5)
            bars_payload = await fetch_bars()
            lux = await fetch_luxalgo_choch(
                bars_by_series_index=bars_payload.get("bars_by_series_index")
            )
            lux_stats[f"{tf}_fetch"] = {
                "ok": lux.get("ok"),
                "count": (lux.get("counts") or {}).get("CHoCH"),
                "timing": (lux.get("counts") or {}).get("timing"),
                "study_id": lux.get("study_id"),
                "study_name": lux.get("study_name"),
            }
            if lux.get("ok"):
                lux_stats[f"{tf}_persist"] = append_luxalgo_captures(
                    lux.get("events") or [],
                    symbol=SYMBOL,
                    timeframe=tf,
                    bars_by_series_index=bars_payload.get("bars_by_series_index"),
                )
        if cur.get("resolution") is not None:
            await set_chart_resolution(str(cur["resolution"]))
            await asyncio.sleep(1.0)
    except Exception as exc:  # noqa: BLE001
        lux_stats["error"] = str(exc)
    out["luxalgo_capture"] = lux_stats

    r5 = runs.get("5m") or {}
    r15 = runs.get("15m") or {}
    s5 = r5.get("setup") or {}
    s15 = r15.get("setup") or {}
    out["live_paired"] = {
        "liquidity_event_id": s5.get("liquidity_event_id") or s15.get("liquidity_event_id"),
        "htf": {
            "daily": (out.get("live_daily_bias") or {}).get("direction"),
            "h4": (out.get("live_h4_bias") or {}).get("direction"),
            "alignment": (out.get("higher_timeframe_context") or {}).get("alignment"),
        },
        "5m": {
            "status": s5.get("status"),
            "confirmation_timestamp": (s5.get("choch") or {}).get("time"),
            "fvg_timestamp": (s5.get("fvg") or {}).get("time"),
            "entry_state": s5.get("entry_modes"),
        },
        "15m": {
            "status": s15.get("status"),
            "confirmation_timestamp": (s15.get("choch") or {}).get("time"),
            "fvg_timestamp": (s15.get("fvg") or {}).get("time"),
            "entry_state": s15.get("entry_modes"),
        },
        "same_liquidity_event": s5.get("liquidity_event_id") == s15.get("liquidity_event_id"),
    }

    # Annotate prefer any partial/valid live analysis state (not NO_SETUP).
    annotation = {"ok": False, "note": "no annotatable setup"}
    annotatable = {
        "WAITING_FOR_SESSION",
        "WAITING_FOR_SWEEP",
        "WAITING_FOR_CONFIRMATION",
        "WAITING_FOR_FVG",
        "WAITING_FOR_RETRACE",
        "ENTRY_READY",
        "INVALIDATED",
        "EXPIRED",
    }
    for tf in ("5m", "15m"):
        run = runs.get(tf) or {}
        if not run.get("ok"):
            continue
        st = (run.get("setup") or {}).get("status") or run.get("status")
        if st not in annotatable:
            continue
        try:
            full = await analyze_live_session_setup(
                session=run.get("session_fallback") or "auto",
                execution_timeframe=tf,
                bias_provider="structure",
            )
            setup_obj = full.get("setup")
            if setup_obj is None:
                continue
            ann1 = await annotate_trade_setup(setup_obj, take_screenshot_after=False)
            ann2 = await annotate_trade_setup(setup_obj, take_screenshot_after=True)
            annotation = {
                "ok": bool(ann2.get("ok")),
                "execution_timeframe": tf,
                "setup_id": ann2.get("setup_id"),
                "status": full.get("status") or (setup_obj.get("status") if isinstance(setup_obj, dict) else getattr(setup_obj, "status", None)),
                "annotations_created_count": ann2.get("annotations_created_count"),
                "idempotent_second_pass_count": ann2.get("annotations_created_count"),
                "first_pass_count": ann1.get("annotations_created_count"),
                "screenshot_path": ann2.get("screenshot_path"),
                "cleared_previous": ann2.get("cleared_previous"),
            }
            break
        except Exception as exc:  # noqa: BLE001
            annotation = {"ok": False, "error": str(exc), "execution_timeframe": tf}
    out["annotation"] = annotation

    # Restore original symbol + verify TF
    final_tf = await get_chart_resolution()
    if original_sym.get("symbol") and original_sym.get("symbol") != SYMBOL:
        await set_chart_symbol(str(original_sym["symbol"]))
        await asyncio.sleep(1.5)
    elif original_sym.get("symbol") == SYMBOL:
        pass
    # If we switched away from original non-OANDA, restore
    if out["original"]["symbol"] and out["original"]["symbol"] != (
        await get_chart_symbol()
    ).get("symbol"):
        await set_chart_symbol(str(out["original"]["symbol"]))
        await asyncio.sleep(1.2)

    # Ensure timeframe restore to original
    if out["original"]["timeframe"] is not None:
        from chart_timeframe import set_chart_resolution

        await set_chart_resolution(str(out["original"]["timeframe"]))
        await asyncio.sleep(1.0)

    final_sym = await get_chart_symbol()
    final_tf = await get_chart_resolution()
    out["final"] = {
        "symbol": final_sym.get("symbol"),
        "timeframe": final_tf.get("resolution"),
    }
    out["restore_check"] = {
        "symbol_ok": out["final"]["symbol"] == out["original"]["symbol"],
        "timeframe_ok": str(out["final"]["timeframe"]) == str(out["original"]["timeframe"]),
    }
    if not out["restore_check"]["symbol_ok"] or not out["restore_check"]["timeframe_ok"]:
        out["ok"] = False
        out["restore_error"] = "Chart restore failed — original symbol/timeframe not restored"

    out["ok"] = bool(out.get("ok")) and bool(bundle.get("restore_ok")) and not out.get(
        "restore_error"
    )
    return out


def historical_expansion(live_datasets: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """Replay from persisted native datasets; fall back to fixtures without labeling live."""
    from confirmation_provider import HistoricalStructureProvider
    from historical_structure import detect_internal_choch
    from luxalgo_capture import captures_to_confirmations

    bars_by_tf: dict[str, list[Bar]] = {}
    meta = {}
    for tf in ("5m", "15m", "4H", "1D"):
        loaded = load_dataset(SYMBOL, tf)
        if loaded.get("ok") and loaded.get("bars"):
            bars_by_tf[tf] = loaded["bars"]
            meta[tf] = loaded.get("meta")
        elif live_datasets and (live_datasets.get(tf) or {}).get("meta"):
            # already written in live path
            loaded2 = load_dataset(SYMBOL, tf)
            if loaded2.get("ok"):
                bars_by_tf[tf] = loaded2["bars"]
                meta[tf] = loaded2.get("meta")

    source = "native_datasets"
    if "5m" not in bars_by_tf:
        # Offline fallback — explicit
        bars5 = build_multi_day_fixture_bars(days=12)
        from ohlc_resample import resample_ohlc

        bars_by_tf = {
            "5m": bars5,
            "15m": list(resample_ohlc(bars5, "15m").bars),
            "4H": list(resample_ohlc(bars5, "4H").bars),
            "1D": list(resample_ohlc(bars5, "1D").bars),
        }
        source = "fixture_offline_not_live"
        for tf, bars in bars_by_tf.items():
            write_dataset(
                bars,
                symbol=SYMBOL,
                timeframe=tf,
                source=source,
                expected_period_sec=timeframe_seconds(tf) if tf in ("5m", "15m") else None,
            )
            meta[tf] = load_dataset(SYMBOL, tf).get("meta")

    result = replay_historical_mtf_setups(
        bars_by_tf,
        symbol=SYMBOL,
        execution_timeframes=("5m", "15m"),
        bias_provider=StructureBiasProvider(),
    )
    journal_path = append_journal_records(
        result.journal_records, root=Path("journal") / "phase14_mtf"
    )
    report = compute_mtf_journal_report(result.journal_records)

    # Session coverage estimate
    completed = result.coverage.complete_sessions if result.coverage else 0

    # LuxAlgo overlap vs internal on each TF
    overlap = {}
    for tf in ("5m", "15m"):
        bars = bars_by_tf.get(tf) or []
        internal = detect_internal_choch(bars) if bars else []
        caps = load_luxalgo_captures(symbol=SYMBOL, timeframe=tf)
        lux_events = captures_to_confirmations(caps)
        ov = compare_choch_overlap(internal, lux_events)
        overlap[tf] = {
            "reliable_luxalgo_events": ov.get("luxalgo_reliable_count"),
            "internal_events": ov.get("internal_count"),
            "matched_count": ov.get("matched_count"),
            "luxalgo_only": ov.get("missed_luxalgo_count"),
            "internal_only": ov.get("missed_internal_count"),
            "equivalence_status": ov.get("equivalence_status"),
            "full_match_rate": (
                None
                if not ov.get("luxalgo_reliable_count")
                else ov["matched_count"] / max(1, ov["luxalgo_reliable_count"])
            ),
        }

    # CSV reports
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    summary_csv = REPORTS_DIR / "phase14_setup_summary.csv"
    with summary_csv.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(
            fh,
            fieldnames=[
                "setup_id",
                "liquidity_event_id",
                "execution_timeframe",
                "session",
                "direction",
                "status",
                "daily_bias",
                "h4_bias",
                "htf_alignment",
                "setup_vs_daily",
                "setup_vs_h4",
                "sweep_timestamp",
                "confirmation_timestamp",
                "fvg_created_timestamp",
            ],
        )
        w.writeheader()
        for r in result.journal_records:
            w.writerow(
                {
                    "setup_id": r.setup_id,
                    "liquidity_event_id": r.liquidity_event_id,
                    "execution_timeframe": r.execution_timeframe,
                    "session": r.session,
                    "direction": r.direction,
                    "status": r.status,
                    "daily_bias": r.daily_bias,
                    "h4_bias": r.h4_bias,
                    "htf_alignment": r.htf_alignment,
                    "setup_vs_daily": r.setup_vs_daily,
                    "setup_vs_h4": r.setup_vs_h4,
                    "sweep_timestamp": r.sweep_timestamp,
                    "confirmation_timestamp": r.confirmation_timestamp,
                    "fvg_created_timestamp": r.fvg_created_timestamp,
                }
            )

    paired_csv = REPORTS_DIR / "phase14_paired_5m_15m.csv"
    paired = report.get("paired_5m_15m") or {}
    with paired_csv.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(
            fh,
            fieldnames=[
                "liquidity_event_id",
                "5m_status",
                "15m_status",
                "5m_confirmation",
                "15m_confirmation",
                "5m_fvg",
                "15m_fvg",
                "5m_triggered",
                "15m_triggered",
                "which_confirmed_first",
                "which_fvg_first",
            ],
        )
        w.writeheader()
        for p in paired.get("pairs") or []:
            w.writerow(
                {
                    "liquidity_event_id": p.get("liquidity_event_id"),
                    "5m_status": (p.get("5m") or {}).get("status"),
                    "15m_status": (p.get("15m") or {}).get("status"),
                    "5m_confirmation": (p.get("5m") or {}).get("confirmation_timestamp"),
                    "15m_confirmation": (p.get("15m") or {}).get("confirmation_timestamp"),
                    "5m_fvg": (p.get("5m") or {}).get("fvg_created_timestamp"),
                    "15m_fvg": (p.get("15m") or {}).get("fvg_created_timestamp"),
                    "5m_triggered": (p.get("5m") or {}).get("triggered"),
                    "15m_triggered": (p.get("15m") or {}).get("triggered"),
                    "which_confirmed_first": p.get("which_confirmed_first"),
                    "which_fvg_first": p.get("which_fvg_first"),
                }
            )

    return {
        "source": source,
        "bar_counts": {tf: len(bars_by_tf.get(tf) or []) for tf in ("5m", "15m", "4H", "1D")},
        "meta": meta,
        "earliest_latest": {
            tf: {
                "earliest": meta.get(tf, {}).get("earliest_bar"),
                "latest": meta.get(tf, {}).get("latest_bar"),
            }
            for tf in ("5m", "15m", "4H", "1D")
        },
        "replay": {
            "total_sessions": result.total_sessions,
            "total_sweeps": result.total_sweeps,
            "total_setups": result.total_setups,
            "complete_sessions": completed,
            "coverage": result.coverage.to_dict() if result.coverage else None,
            "warnings": result.warnings,
            "metadata": result.metadata,
        },
        "journal_path": str(journal_path),
        "journal_size": len(result.journal_records),
        "report": report,
        "luxalgo_overlap": overlap,
        "equivalence_status": "unvalidated_against_luxalgo",
        "csv": {
            "setup_summary": str(summary_csv),
            "paired_5m_15m": str(paired_csv),
        },
        "strategy_version": STRATEGY_VERSION,
    }


async def main() -> None:
    live = await live_mtf_proof()
    hist = historical_expansion(live.get("datasets") if live.get("live_tested") else None)
    report = {
        "ok": True,
        "phase": 14,
        "strategy_version": STRATEGY_VERSION,
        "live": live,
        "historical": {
            k: hist[k]
            for k in hist
            if k != "report"
        },
        "historical_report": hist.get("report"),
    }
    Path("phase14_validation.json").write_text(
        json.dumps(report, indent=2, default=str), encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "phase": 14,
                "cdp": (live.get("boot") or {}).get("preflight", {}).get("cdp_reachable")
                if live.get("boot")
                else live.get("cdp_status"),
                "live_ok": live.get("ok"),
                "symbol_original": (live.get("original") or {}).get("symbol"),
                "symbol_final": (live.get("final") or {}).get("symbol"),
                "tf_original": (live.get("original") or {}).get("timeframe"),
                "tf_final": (live.get("final") or {}).get("timeframe"),
                "daily_roll_status": (live.get("daily_boundary_evidence") or {}).get(
                    "status"
                ),
                "daily_bias": (live.get("live_daily_bias") or {}).get("direction"),
                "h4_bias": (live.get("live_h4_bias") or {}).get("direction"),
                "journal_size": hist.get("journal_size"),
                "complete_sessions": (hist.get("replay") or {}).get("complete_sessions"),
                "bar_counts": hist.get("bar_counts"),
                "screenshot": (live.get("annotation") or {}).get("screenshot_path"),
            },
            indent=2,
            default=str,
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
