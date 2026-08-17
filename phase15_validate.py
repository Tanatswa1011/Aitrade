"""Phase 15 validation: deeper-history audit + ambiguity / invalid-stop diagnostics."""

from __future__ import annotations

import csv
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from bias_provider import StructureBiasProvider
from dataset_overlap import (
    compare_htf_bias_overlap,
    compare_ohlc_overlap,
    compare_session_ranges_overlap,
)
from historical_data_provider import (
    EXTERNAL_HISTORY_OPTIONS,
    LocalJsonlProvider,
    TradingViewDesktopProvider,
    TradingViewHistoryProvider,
    integrity_report,
)
from htf_report import compute_mtf_journal_report, trigger_bar_ambiguity_report
from intrabar_resolver import resolve_15m_ambiguities_from_journal
from invalid_stop_diagnostics import diagnose_invalid_stops
from luxalgo_capture import load_luxalgo_captures
from luxalgo_overlap import compare_choch_overlap
from luxalgo_capture import captures_to_confirmations
from historical_structure import detect_internal_choch
from ohlc_resample import resample_ohlc
from replay_engine import replay_historical_mtf_setups
from sample_quality import sample_quality_label
from setup_journal import append_journal_records, load_journal_records
from strategy_config import DEFAULT_STRATEGY_CONFIG
from strategy_version import STRATEGY_VERSION
from timeframe import timeframe_seconds
from trading_day_config import load_confirmed_trading_day_from_evidence


SYMBOL = "OANDA:XAUUSD"
REPORTS_DIR = Path("reports")
JOURNAL_ROOT = Path("journal") / "phase15_mtf"
AUDIT_PATH = Path("data") / "phase15_history_source_audit.json"


def _write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: Optional[list[str]] = None) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return str(path)
    keys = fieldnames or sorted({k for r in rows for k in r.keys()})
    with path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=keys, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            flat = {}
            for k, v in r.items():
                if isinstance(v, (dict, list)):
                    flat[k] = json.dumps(v, default=str)
                else:
                    flat[k] = v
            w.writerow(flat)
    return str(path)


def build_history_source_audit() -> dict[str, Any]:
    """Document investigated paths; do not integrate unapproved external feeds."""
    audit = {
        "generated_at": datetime.now(tz=timezone.utc).isoformat(),
        "priority_order": [
            "tradingview_authenticated_session",
            "oanda_compatible_approved",
            "explicitly_approved_external",
        ],
        "investigated": [
            {
                "provider": "TradingViewDesktopProvider",
                "result": "usable ~300 bars via series.data(); ceiling confirmed Phase 14/15",
                "selected_for_deep_history": False,
            },
            {
                "provider": "TradingViewHistoryProvider",
                "result": (
                    "requestMoreData / loadDataTo / setVisibleRange / "
                    "setInitialRequestOptions({count:5000}) did not expand usable history"
                ),
                "selected_for_deep_history": False,
            },
            {
                "provider": "LocalJsonlProvider",
                "result": "loads Phase 14 OANDA:XAUUSD native captures from data/",
                "selected_for_deep_history": False,
                "selected_for_phase15_pipeline": True,
            },
            {
                "provider": "OANDA API / local export",
                "result": "no credentials or approved export present in project",
                "selected_for_deep_history": False,
            },
        ],
        "external_options_pending_approval": EXTERNAL_HISTORY_OPTIONS,
        "selected_deep_provider": None,
        "selected_pipeline_provider": "TradingViewDesktopProvider+LocalJsonlProvider",
        "blocking_note": (
            "Phase 15 does not integrate Yahoo GC=F, Dukascopy, or other non-OANDA "
            "feeds without explicit approval. Exact OANDA history preferred."
        ),
        "session_objective": {
            "target_complete_sessions": 100,
            "preferred": 250,
            "better": 500,
            "met": False,
            "reason": "no approved deeper OANDA-compatible history source integrated",
        },
    }
    AUDIT_PATH.parent.mkdir(parents=True, exist_ok=True)
    AUDIT_PATH.write_text(json.dumps(audit, indent=2), encoding="utf-8")
    return audit


def load_provider_datasets() -> dict[str, Any]:
    desktop = TradingViewDesktopProvider(allow_live=False)
    history = TradingViewHistoryProvider(desktop)
    local = LocalJsonlProvider()
    trading_day = load_confirmed_trading_day_from_evidence()
    datasets = {}
    integrity = {}
    for tf in ("5m", "15m", "4H", "1D"):
        ds = history.fetch(SYMBOL, tf)
        datasets[tf] = ds
        integrity[tf] = integrity_report(ds, trading_day=trading_day)
    return {
        "datasets": datasets,
        "integrity": integrity,
        "providers": {
            "desktop": desktop.name,
            "history": history.name,
            "local": local.name,
        },
        "trading_day": trading_day.to_dict(),
    }


def native_vs_resampled_overlap(datasets: dict[str, Any]) -> dict[str, Any]:
    """
    Compare native TV HTF series against deterministic resample from native 5m.

    Deep-vs-native overlap is blocked until an approved deeper source exists;
    this validates resampling / anchors on the overlapping native window.
    """
    trading_day = load_confirmed_trading_day_from_evidence()
    bars5 = list(datasets["5m"].bars)
    out: dict[str, Any] = {
        "deep_source_status": "not_integrated",
        "comparison_mode": "native_vs_resampled_from_5m",
        "note": (
            "No approved deeper dataset; comparing Phase 14 native 15m/4H/1D "
            "against resampled-from-5m on the overlapping window."
        ),
    }
    if not bars5:
        out["error"] = "no_5m_bars"
        return out

    res15 = resample_ohlc(bars5, "15m", source_timeframe="5m", trading_day=trading_day)
    res4h = resample_ohlc(bars5, "4H", source_timeframe="5m", trading_day=trading_day)
    res1d = resample_ohlc(bars5, "1D", source_timeframe="5m", trading_day=trading_day)

    out["h4_anchor"] = (res4h.extras or {}).get("h4_anchor")
    out["daily_boundary"] = (res1d.extras or {}).get("daily_boundary")
    out["sources"] = {
        "resampled_15m": res15.source,
        "resampled_4H": res4h.source,
        "resampled_1D": res1d.source,
    }

    native15 = list(datasets["15m"].bars)
    native4h = list(datasets["4H"].bars)
    native1d = list(datasets["1D"].bars)

    out["ohlc"] = {
        "5m": compare_ohlc_overlap(
            bars5, bars5, left_label="native_5m", right_label="native_5m"
        ),
        "15m": compare_ohlc_overlap(
            native15, list(res15.bars), left_label="native_15m", right_label="resampled_15m"
        ),
        "4H": compare_ohlc_overlap(
            native4h, list(res4h.bars), left_label="native_4H", right_label="resampled_4H_ny_anchor"
        ),
        "1D": compare_ohlc_overlap(
            native1d, list(res1d.bars), left_label="native_1D", right_label="resampled_1D_ny_roll"
        ),
    }
    out["session_hl"] = compare_session_ranges_overlap(
        bars5,
        bars5,
        left_label="native_5m",
        right_label="native_5m",
    )
    # Session compare using resampled 15m as proxy alternate series (same feed)
    out["session_hl_native5_vs_self"] = out["session_hl"]
    out["htf_bias"] = compare_htf_bias_overlap(
        native1d,
        native4h,
        list(res1d.bars),
        list(res4h.bars),
        left_label="native_htf",
        right_label="resampled_htf",
    )
    out["deep_vs_native_ohlc"] = {
        "status": "skipped",
        "reason": "no_approved_deeper_provider",
    }
    return out


def ambiguity_breakdown(records, stop_mode: str = "beyond_sweep") -> dict[str, Any]:
    base = trigger_bar_ambiguity_report(records)
    by_entry: dict[str, dict[str, Any]] = {}
    by_stop: dict[str, dict[str, Any]] = {}
    by_tf_mode: dict[str, dict[str, Any]] = {}

    for tf in ("5m", "15m"):
        for mode in ("first_touch", "boundary", "CE"):
            trig = [
                (r, e)
                for r in records
                for e in r.entry_results
                if e.triggered
                and (r.execution_timeframe or r.timeframe) == tf
                and e.mode == mode
            ]
            flagged = [
                (r, e)
                for r, e in trig
                if "TRIGGER_BAR_STOP_AMBIGUITY" in (r.reliability_flags or [])
                or "TRIGGER_BAR_STOP_AMBIGUITY" in (e.ambiguity_flags or [])
            ]
            key = f"{tf}|{mode}"
            by_tf_mode[key] = {
                "triggered_candidates": len(trig),
                "trigger_bar_stop_ambiguities": len(flagged),
                "percentage": (len(flagged) / len(trig) * 100.0) if trig else None,
            }
            by_entry.setdefault(mode, {"triggered": 0, "ambiguous": 0})
            by_entry[mode]["triggered"] += len(trig)
            by_entry[mode]["ambiguous"] += len(flagged)

    # Stop mode is config-level for this strategy version (descriptive).
    trig_all = [(r, e) for r in records for e in r.entry_results if e.triggered]
    flagged_all = [
        (r, e)
        for r, e in trig_all
        if "TRIGGER_BAR_STOP_AMBIGUITY" in (r.reliability_flags or [])
        or "TRIGGER_BAR_STOP_AMBIGUITY" in (e.ambiguity_flags or [])
    ]
    by_stop[stop_mode] = {
        "triggered_candidates": len(trig_all),
        "trigger_bar_stop_ambiguities": len(flagged_all),
        "percentage": (len(flagged_all) / len(trig_all) * 100.0) if trig_all else None,
    }
    # Also report beyond_fvg as not-run in this config (descriptive placeholder).
    if stop_mode != "beyond_fvg":
        by_stop["beyond_fvg"] = {
            "triggered_candidates": 0,
            "trigger_bar_stop_ambiguities": 0,
            "percentage": None,
            "note": "not_replayed_this_phase_config",
        }

    for mode, stats in by_entry.items():
        stats["percentage"] = (
            (stats["ambiguous"] / stats["triggered"] * 100.0)
            if stats["triggered"]
            else None
        )

    return {
        **base,
        "by_entry_mode_detail": by_entry,
        "by_stop_mode": by_stop,
        "by_execution_timeframe_and_entry_mode": by_tf_mode,
    }


def run_phase15(*, write_artifacts: bool = True) -> dict[str, Any]:
    audit = build_history_source_audit()
    loaded = load_provider_datasets()
    datasets = loaded["datasets"]
    trading_day = load_confirmed_trading_day_from_evidence()

    bar_counts = {tf: ds.meta.bar_count for tf, ds in datasets.items()}
    date_span = {
        tf: {
            "earliest": ds.meta.actual_start,
            "latest": ds.meta.actual_end,
            "provider": ds.meta.provider,
            "source": ds.meta.source,
            "source_symbol": ds.meta.source_symbol,
        }
        for tf, ds in datasets.items()
    }

    overlap = native_vs_resampled_overlap(datasets)

    bars_by_tf = {tf: list(ds.bars) for tf, ds in datasets.items()}
    # Prefer native 5m → resample HTF for replay continuity when native HTF thin
    if bars_by_tf.get("5m"):
        if not bars_by_tf.get("15m"):
            bars_by_tf["15m"] = list(
                resample_ohlc(
                    bars_by_tf["5m"], "15m", source_timeframe="5m", trading_day=trading_day
                ).bars
            )

    result = replay_historical_mtf_setups(
        bars_by_tf,
        symbol=SYMBOL,
        strategy_config=DEFAULT_STRATEGY_CONFIG,
        execution_timeframes=("5m", "15m"),
        bias_provider=StructureBiasProvider(),
    )

    # Enrich extras with stop_mode for diagnostics
    stop_mode = str(DEFAULT_STRATEGY_CONFIG.risk.stop_mode)
    enriched = []
    for rec in result.journal_records:
        extras = dict(rec.extras or {})
        extras["stop_mode"] = stop_mode
        extras["phase"] = "phase15"
        from dataclasses import replace

        enriched.append(replace(rec, extras=extras, strategy_version=STRATEGY_VERSION))

    journal_path = None
    if write_artifacts:
        JOURNAL_ROOT.mkdir(parents=True, exist_ok=True)
        journal_path = append_journal_records(enriched, root=JOURNAL_ROOT)

    report = compute_mtf_journal_report(enriched)
    invalid = diagnose_invalid_stops(enriched, default_stop_mode=stop_mode)
    amb = ambiguity_breakdown(enriched, stop_mode=stop_mode)
    resolved_15 = resolve_15m_ambiguities_from_journal(enriched, bars_by_tf.get("5m") or [])

    # 5m remaining ambiguities (need 1m — not invented)
    amb_5m = [
        {
            "setup_id": r.setup_id,
            "entry_mode": e.mode,
            "entry_timestamp": e.entry_timestamp,
            "requires_1m_dataset": True,
        }
        for r in enriched
        for e in r.entry_results
        if (r.execution_timeframe or r.timeframe) == "5m"
        and e.triggered
        and (
            "TRIGGER_BAR_STOP_AMBIGUITY" in (e.ambiguity_flags or [])
            or "TRIGGER_BAR_STOP_AMBIGUITY" in (r.reliability_flags or [])
            or e.outcome == "AMBIGUOUS_INTRABAR"
        )
    ]

    # LuxAlgo overlap status
    lux = {}
    for tf in ("5m", "15m"):
        bars = bars_by_tf.get(tf) or []
        internal = detect_internal_choch(bars) if bars else []
        caps = load_luxalgo_captures(symbol=SYMBOL, timeframe=tf)
        lux_events = captures_to_confirmations(caps)
        ov = compare_choch_overlap(internal, lux_events)
        lux[tf] = {
            "reliable_luxalgo_events": ov.get("luxalgo_reliable_count"),
            "internal_events": ov.get("internal_count"),
            "matched_count": ov.get("matched_count"),
            "equivalence_status": ov.get("equivalence_status")
            or "unvalidated_against_luxalgo",
        }
    eq_statuses = {v.get("equivalence_status") for v in lux.values()}
    overall_eq = "unvalidated_against_luxalgo"
    if any(s == "partially_validated" for s in eq_statuses):
        overall_eq = "partially_validated"

    complete_sessions = result.coverage.complete_sessions if result.coverage else 0

    csv_paths = {}
    if write_artifacts:
        REPORTS_DIR.mkdir(parents=True, exist_ok=True)
        summary_rows = []
        for r in enriched:
            for e in r.entry_results:
                summary_rows.append(
                    {
                        "setup_id": r.setup_id,
                        "liquidity_event_id": r.liquidity_event_id,
                        "session": r.session,
                        "direction": r.direction,
                        "execution_timeframe": r.execution_timeframe,
                        "status": r.status,
                        "invalidation_reason": r.invalidation_reason,
                        "entry_mode": e.mode,
                        "triggered": e.triggered,
                        "outcome": e.outcome,
                        "htf_bucket": None,
                        "daily_bias": r.daily_bias,
                        "h4_bias": r.h4_bias,
                        "setup_vs_daily": r.setup_vs_daily,
                        "setup_vs_h4": r.setup_vs_h4,
                    }
                )
        csv_paths["setup_summary"] = _write_csv(
            REPORTS_DIR / "phase15_setup_summary.csv", summary_rows
        )
        paired = (report.get("paired_5m_15m") or {}).get("pairs") or []
        csv_paths["paired"] = _write_csv(
            REPORTS_DIR / "phase15_paired_5m_15m.csv",
            paired if isinstance(paired, list) else [],
        )
        csv_paths["invalid_stops"] = _write_csv(
            REPORTS_DIR / "phase15_invalid_stop_diagnostics.csv",
            invalid.get("cases") or [],
        )
        amb_rows = []
        for key, stats in (amb.get("by_execution_timeframe_and_entry_mode") or {}).items():
            tf, mode = key.split("|", 1)
            amb_rows.append(
                {
                    "execution_timeframe": tf,
                    "entry_mode": mode,
                    **stats,
                }
            )
        for row in resolved_15.get("rows") or []:
            amb_rows.append({"kind": "15m_resolved", **row})
        for row in amb_5m:
            amb_rows.append({"kind": "5m_unresolved", **row})
        csv_paths["ambiguities"] = _write_csv(
            REPORTS_DIR / "phase15_ambiguities.csv", amb_rows
        )

    out = {
        "ok": True,
        "phase": 15,
        "strategy_version": STRATEGY_VERSION,
        "generated_at": datetime.now(tz=timezone.utc).isoformat(),
        "history_source_audit": audit,
        "selected_deep_provider": None,
        "pipeline_provider": loaded["providers"],
        "symbol": SYMBOL,
        "symbol_feed_equivalence": {
            "strategy_symbol": SYMBOL,
            "data_source_symbol": SYMBOL,
            "feed": "TradingView OANDA:XAUUSD native (Phase 14 capture)",
            "note": (
                "No alternate XAUUSD feed integrated. External options differ in "
                "broker/feed, session, daily roll, and quotes — see audit."
            ),
        },
        "bar_counts": bar_counts,
        "date_span": date_span,
        "data_integrity": loaded["integrity"],
        "native_vs_resampled_overlap": overlap,
        "complete_sessions": complete_sessions,
        "complete_sessions_sample_quality": sample_quality_label(complete_sessions),
        "journal_size": len(enriched),
        "journal_path": str(journal_path) if journal_path else None,
        "journal_dedupe_keys": "liquidity_event_id|execution_timeframe|config_hash",
        "funnel": report.get("session_funnel"),
        "mtf_report": report,
        "htf_alignment_stats": report.get("htf_alignment_distribution"),
        "metrics_by_execution_timeframe": report.get("metrics_by_execution_timeframe"),
        "invalid_directional_stop": invalid,
        "implementation_bug_fixes": [],
        "trigger_bar_ambiguity": amb,
        "intrabar_15m_resolved_by_5m": resolved_15,
        "remaining_5m_ambiguities": {
            "count": len(amb_5m),
            "requires_1m_dataset": True,
            "rows_head": amb_5m[:40],
        },
        "luxalgo": {
            "by_timeframe": lux,
            "equivalence_status": overall_eq,
            "note": "Conservative; no jump to validated.",
        },
        "csv_reports": csv_paths,
        "limitations": [
            "TradingView Desktop history ceiling ~300 bars remains",
            "No approved deeper OANDA-compatible history integrated",
            "Session objective 100+ complete Asia/London observations not met",
            "5m same-bar ambiguities cannot be ordered without 1m data",
            "Native vs deep-source OHLC overlap skipped pending approved deep source",
        ],
        "recommended_phase16_scope": [
            "Approve and integrate exact OANDA (or validated equivalent) deep history",
            "Re-run Phase 15 overlap + large-sample replay once depth available",
            "Optionally add 1m only if same approved feed covers it",
            "Keep stop rules unchanged until invalid-stop categories stabilize on large N",
            "Do not hard-filter HTF or optimize strategy yet",
        ],
    }

    if write_artifacts:
        Path("phase15_validation.json").write_text(
            json.dumps(out, indent=2, default=str), encoding="utf-8"
        )
    return out


if __name__ == "__main__":
    payload = run_phase15()
    print(json.dumps({k: payload[k] for k in (
        "ok",
        "selected_deep_provider",
        "bar_counts",
        "complete_sessions",
        "journal_size",
        "luxalgo",
    )}, indent=2, default=str))
