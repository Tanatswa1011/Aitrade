"""Phase 17 — credentialed spot XAUUSD validation + gated deep replay."""

from __future__ import annotations

import argparse
import csv
import json
import sys
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from bar_dataset import load_dataset
from bias_provider import StructureBiasProvider
from feed_equivalence import (
    CLASS_RESEARCH,
    evaluate_feed_equivalence,
    replay_gate,
)
from historical_data_provider import integrity_report
from historical_structure import detect_internal_choch
from htf_report import compute_mtf_journal_report
from intrabar_resolver import resolve_15m_ambiguities_from_journal
from invalid_stop_diagnostics import diagnose_invalid_stops
from luxalgo_capture import captures_to_confirmations, load_luxalgo_captures
from luxalgo_overlap import compare_choch_overlap
from ohlc_resample import resample_ohlc
from openbb_history import (
    OpenBBHistoricalDataProvider,
    load_dotenv_credentials,
    openbb_version,
    provider_preflight,
)
from replay_engine import replay_historical_mtf_setups
from sample_quality import sample_quality_label
from setup_journal import append_journal_records
from strategy_config import DEFAULT_STRATEGY_CONFIG
from strategy_version import STRATEGY_VERSION
from trading_day_config import load_confirmed_trading_day_from_evidence


SYMBOL_TV = "OANDA:XAUUSD"
REPORTS = Path("reports")
JOURNAL = Path("journal") / "phase17_deep"


def _scrub(obj: Any) -> Any:
    """Recursively drop any accidental secret-looking fields."""
    secret_keys = {
        "token",
        "api_key",
        "apikey",
        "password",
        "secret",
        "tiingo_token",
        "fmp_api_key",
        "authorization",
    }
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            lk = str(k).lower()
            if any(s in lk for s in secret_keys) and lk not in {
                "credential_key",
                "credential_required",
                "credential_present",
                "credentials_required",
                "environment_variable_names",
            }:
                if isinstance(v, bool) or v is None:
                    out[k] = v
                else:
                    out[k] = "<redacted>"
                continue
            out[k] = _scrub(v)
        return out
    if isinstance(obj, list):
        return [_scrub(x) for x in obj]
    return obj


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return str(path)
    keys = sorted({k for r in rows for k in r.keys()})
    with path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=keys, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            flat = {
                k: (json.dumps(v, default=str) if isinstance(v, (dict, list)) else v)
                for k, v in r.items()
            }
            w.writerow(flat)
    return str(path)


def load_tv_benchmark(tf: str = "5m") -> dict[str, Any]:
    loaded = load_dataset(SYMBOL_TV, tf)
    if not loaded.get("ok"):
        return {"ok": False, "error": loaded.get("error")}
    bars = loaded["bars"]
    return {
        "ok": True,
        "bars": bars,
        "meta": loaded.get("meta"),
        "earliest": int(bars[0].time) if bars else None,
        "latest": int(bars[-1].time) if bars else None,
        "bar_count": len(bars),
    }


def validate_spot_candidate(
    *,
    underlying: str,
    symbol: str = "XAUUSD",
    start_ts: int,
    end_ts: int,
) -> dict[str, Any]:
    """Fetch overlap window and run equivalence gate. Never accepts futures."""
    pre = provider_preflight(underlying, route="currency")
    if not pre.get("ok"):
        return {
            "provider": underlying,
            "symbol": symbol,
            "preflight": pre,
            "accepted": False,
            "reason": "preflight_failed",
            "deep_replay_allowed": False,
        }

    prov = OpenBBHistoricalDataProvider(underlying_provider=underlying, route="currency")
    res = prov.fetch_result(
        symbol,
        "5m",
        start_ts=start_ts,
        end_ts=end_ts,
        underlying_provider=underlying,
        route="currency",
    )
    instrument = res.instrument_type
    if instrument == "futures":
        return {
            "provider": underlying,
            "symbol": symbol,
            "preflight": pre,
            "fetch": res.to_dict(),
            "accepted": False,
            "reason": "futures_rejected_as_canonical",
            "deep_replay_allowed": False,
        }

    if not res.bars:
        return {
            "provider": underlying,
            "symbol": symbol,
            "preflight": pre,
            "fetch": res.to_dict(),
            "accepted": False,
            "reason": "empty_or_error:" + ";".join(res.errors),
            "deep_replay_allowed": False,
        }

    tv = load_tv_benchmark("5m")
    tv_daily = load_dataset(SYMBOL_TV, "1D")
    tv_h4 = load_dataset(SYMBOL_TV, "4H")
    report = evaluate_feed_equivalence(
        tv["bars"],
        list(res.bars),
        benchmark_provider="tradingview_oanda",
        candidate_provider=f"openbb:{underlying}",
        benchmark_symbol=SYMBOL_TV,
        candidate_symbol=symbol,
        instrument_type=instrument,
        tv_daily=tv_daily.get("bars") if tv_daily.get("ok") else None,
        tv_h4=tv_h4.get("bars") if tv_h4.get("ok") else None,
        extra_warnings=res.warnings,
    )
    ds = res.to_dataset()
    integ = integrity_report(ds, trading_day=load_confirmed_trading_day_from_evidence())
    gate = replay_gate(
        report,
        integrity_ok=bool(integ.get("integrity_ok")),
        require_session_events=True,
    )
    return {
        "provider": underlying,
        "symbol": symbol,
        "preflight": pre,
        "fetch": res.to_dict(),
        "instrument_type": instrument,
        "returned_symbol": res.source_symbol,
        "equivalence": report.to_dict(),
        "integrity": integ,
        "gate": gate,
        "accepted": bool(gate.get("deep_replay_allowed")),
        "deep_replay_allowed": bool(gate.get("deep_replay_allowed")),
        "bars": list(res.bars) if gate.get("deep_replay_allowed") else None,
    }


def deep_download_if_allowed(
    validation: dict[str, Any],
    *,
    months: int = 3,
) -> dict[str, Any]:
    if not validation.get("deep_replay_allowed"):
        return {
            "downloaded": False,
            "reason": "equivalence_gate_blocked",
            "provider": validation.get("provider"),
        }

    underlying = validation["provider"]
    symbol = validation["symbol"]
    end = datetime.now(tz=timezone.utc)
    start = end - timedelta(days=30 * max(1, months))
    start_ts = int(start.timestamp())
    end_ts = int(end.timestamp())

    prov = OpenBBHistoricalDataProvider(underlying_provider=underlying, route="currency")
    result = prov.fetch_chunked(
        symbol,
        "5m",
        start_ts=start_ts,
        end_ts=end_ts,
        chunk_days=30,
        underlying_provider=underlying,
        route="currency",
        persist=True,
    )
    if not result.bars:
        return {
            "downloaded": False,
            "reason": "empty_deep_fetch",
            "errors": list(result.errors),
            "provider": underlying,
        }

    trading_day = load_confirmed_trading_day_from_evidence()
    bars5 = list(result.bars)
    res15 = resample_ohlc(bars5, "15m", source_timeframe="5m", trading_day=trading_day)
    res4h = resample_ohlc(bars5, "4H", source_timeframe="5m", trading_day=trading_day)
    res1d = resample_ohlc(bars5, "1D", source_timeframe="5m", trading_day=trading_day)

    # Persist derived with tagged source (separate paths under openbb provider)
    for series, tf, tag in (
        (res15, "15m", "resampled_from_openbb_5m"),
        (res4h, "4H", "resampled_from_openbb_5m"),
        (res1d, "1D", "resampled_from_openbb_5m"),
    ):
        from bar_dataset import write_dataset
        from timeframe import timeframe_seconds

        write_dataset(
            list(series.bars),
            symbol=f"openbb_{underlying}_{symbol}",
            timeframe=tf,
            source=tag,
            root=Path("data") / "openbb" / underlying,
            expected_period_sec=timeframe_seconds(tf) if tf in ("15m",) else None,
        )

    integ = integrity_report(
        result.to_dataset(), trading_day=trading_day
    )
    return {
        "downloaded": True,
        "provider": underlying,
        "source_symbol": symbol,
        "period_months": months,
        "requested_start": start_ts,
        "requested_end": end_ts,
        "actual_start": result.actual_start,
        "actual_end": result.actual_end,
        "bars_5m": len(bars5),
        "bars_15m_resampled": len(res15.bars),
        "bars_4H_resampled": len(res4h.bars),
        "bars_1D_resampled": len(res1d.bars),
        "integrity": integ,
        "h4_anchor": (res4h.extras or {}).get("h4_anchor"),
        "daily_boundary": (res1d.extras or {}).get("daily_boundary"),
        "bars_by_tf": {
            "5m": bars5,
            "15m": list(res15.bars),
            "4H": list(res4h.bars),
            "1D": list(res1d.bars),
        },
        "feed_equivalence_class": (validation.get("equivalence") or {}).get(
            "classification"
        ),
    }


def run_deep_replay(deep: dict[str, Any], validation: dict[str, Any]) -> dict[str, Any]:
    if not deep.get("downloaded"):
        return {"executed": False, "reason": deep.get("reason"), "journal_size": 0}

    bars_by_tf = deep["bars_by_tf"]
    result = replay_historical_mtf_setups(
        bars_by_tf,
        symbol=SYMBOL_TV,
        strategy_config=DEFAULT_STRATEGY_CONFIG,
        execution_timeframes=("5m", "15m"),
        bias_provider=StructureBiasProvider(),
    )
    eq_class = deep.get("feed_equivalence_class")
    underlying = deep.get("provider")
    enriched = []
    for rec in result.journal_records:
        extras = dict(rec.extras or {})
        extras.update(
            {
                "data_provider": "openbb",
                "underlying_provider": underlying,
                "source_symbol": deep.get("source_symbol"),
                "feed_equivalence_class": eq_class,
                "phase": "phase17",
            }
        )
        enriched.append(
            replace(
                rec,
                extras=extras,
                strategy_version=STRATEGY_VERSION,
            )
        )

    JOURNAL.mkdir(parents=True, exist_ok=True)
    path = append_journal_records(enriched, root=JOURNAL)
    report = compute_mtf_journal_report(enriched)
    invalid = diagnose_invalid_stops(
        enriched, default_stop_mode=str(DEFAULT_STRATEGY_CONFIG.risk.stop_mode)
    )
    amb15 = resolve_15m_ambiguities_from_journal(enriched, bars_by_tf.get("5m") or [])
    amb5 = [
        r.setup_id
        for r in enriched
        for e in r.entry_results
        if (r.execution_timeframe or r.timeframe) == "5m"
        and e.triggered
        and (
            "TRIGGER_BAR_STOP_AMBIGUITY" in (e.ambiguity_flags or [])
            or e.outcome == "AMBIGUOUS_INTRABAR"
        )
    ]

    # complete sessions from coverage
    complete = result.coverage.complete_sessions if result.coverage else 0
    return {
        "executed": True,
        "journal_path": str(path),
        "journal_size": len(enriched),
        "complete_sessions": complete,
        "complete_sessions_sample_quality": sample_quality_label(complete),
        "mtf_report": report,
        "invalid_directional_stop": invalid,
        "intrabar_15m_resolved_by_5m": amb15,
        "remaining_5m_ambiguities": {"count": len(amb5), "requires_1m": True},
        "funnel": report.get("session_funnel"),
        "htf_alignment": report.get("htf_alignment_distribution"),
        "metrics_by_tf": report.get("metrics_by_execution_timeframe"),
        "timing": report.get("timing_distributions"),
        "trigger_bar_ambiguity": report.get("trigger_bar_ambiguity"),
        "paired": report.get("paired_5m_15m_summary"),
    }


def luxalgo_status() -> dict[str, Any]:
    out = {}
    for tf in ("5m", "15m"):
        loaded = load_dataset(SYMBOL_TV, tf)
        bars = loaded.get("bars") or []
        internal = detect_internal_choch(bars) if bars else []
        caps = load_luxalgo_captures(symbol=SYMBOL_TV, timeframe=tf)
        lux = captures_to_confirmations(caps)
        ov = compare_choch_overlap(internal, lux)
        out[tf] = {
            "reliable_luxalgo_events": ov.get("luxalgo_reliable_count"),
            "internal_events": ov.get("internal_count"),
            "matched_count": ov.get("matched_count"),
            "equivalence_status": ov.get("equivalence_status")
            or "unvalidated_against_luxalgo",
        }
    return {
        "by_timeframe": out,
        "equivalence_status": "unvalidated_against_luxalgo",
        "note": "Deep OpenBB history does not validate LuxAlgo equivalence",
    }


def run_phase17(*, write_artifacts: bool = True, deep_months: int = 3) -> dict[str, Any]:
    load_dotenv_credentials()
    tv = load_tv_benchmark("5m")
    if not tv.get("ok"):
        return {"ok": False, "error": "missing_tv_benchmark"}

    start, end = int(tv["earliest"]), int(tv["latest"])
    evaluations: list[dict[str, Any]] = []
    accepted: Optional[dict[str, Any]] = None

    # --- Tiingo first ---
    tiingo_pre = provider_preflight("tiingo")
    if tiingo_pre.get("credential_present"):
        for sym in ("XAUUSD",):
            row = validate_spot_candidate(
                underlying="tiingo", symbol=sym, start_ts=start, end_ts=end
            )
            # Only try XAU/USD if first form fails empty (not if credential missing)
            evaluations.append(row)
            if row.get("accepted"):
                accepted = row
                break
        if not accepted and evaluations and not (evaluations[-1].get("fetch") or {}).get(
            "bar_count"
        ):
            row2 = validate_spot_candidate(
                underlying="tiingo", symbol="XAU/USD", start_ts=start, end_ts=end
            )
            evaluations.append(row2)
            if row2.get("accepted"):
                accepted = row2
    else:
        evaluations.append(
            {
                "provider": "tiingo",
                "symbol": "XAUUSD",
                "preflight": tiingo_pre,
                "accepted": False,
                "reason": "credential_absent",
                "deep_replay_allowed": False,
                "history_depth": None,
                "classification": None,
            }
        )

    # --- FMP fallback ---
    fmp_evaluated = False
    if accepted is None:
        fmp_pre = provider_preflight("fmp")
        fmp_evaluated = True
        if fmp_pre.get("credential_present"):
            row = validate_spot_candidate(
                underlying="fmp", symbol="XAUUSD", start_ts=start, end_ts=end
            )
            evaluations.append(row)
            if row.get("accepted"):
                accepted = row
        else:
            evaluations.append(
                {
                    "provider": "fmp",
                    "symbol": "XAUUSD",
                    "preflight": fmp_pre,
                    "accepted": False,
                    "reason": "credential_absent",
                    "deep_replay_allowed": False,
                }
            )

    other_candidates = []
    if accepted is None:
        other_candidates = [
            {
                "provider": "yfinance_currency",
                "note": "Phase 16: no spot XAUUSD via currency route",
                "canonical": False,
            },
            {
                "provider": "yfinance_futures_GC",
                "note": "Futures remain RESEARCH_ONLY — not evaluated as canonical in Phase 17",
                "classification": CLASS_RESEARCH,
                "canonical": False,
            },
        ]

    deep = {"downloaded": False, "reason": "no_accepted_provider"}
    replay = {"executed": False, "reason": "no_accepted_provider", "journal_size": 0}
    if accepted is not None:
        deep = deep_download_if_allowed(accepted, months=deep_months)
        # Progressive expand if first chunk ok and sessions still low — keep simple for now
        if deep.get("downloaded"):
            # Estimate sessions roughly via replay coverage after first download
            replay = run_deep_replay(deep, accepted)
            # If under 100 sessions and provider working, try 6 then 12 months
            complete = int(replay.get("complete_sessions") or 0)
            for months in (6, 12, 24):
                if complete >= 100 or not deep.get("downloaded"):
                    break
                if months <= deep_months:
                    continue
                deep2 = deep_download_if_allowed(accepted, months=months)
                if deep2.get("downloaded"):
                    deep = deep2
                    replay = run_deep_replay(deep, accepted)
                    complete = int(replay.get("complete_sessions") or 0)

    lux = luxalgo_status()
    csv_paths = {}
    if write_artifacts:
        REPORTS.mkdir(parents=True, exist_ok=True)
        prov_rows = []
        for e in evaluations:
            pre = e.get("preflight") or {}
            eq = e.get("equivalence") or {}
            prov_rows.append(
                {
                    "provider": e.get("provider"),
                    "symbol": e.get("symbol"),
                    "credential_present": pre.get("credential_present"),
                    "credential_required": pre.get("credential_required"),
                    "route_available": pre.get("route_available"),
                    "accepted": e.get("accepted"),
                    "reason": e.get("reason"),
                    "instrument_type": e.get("instrument_type"),
                    "classification": eq.get("classification"),
                    "deep_replay_allowed": e.get("deep_replay_allowed"),
                    "overlap_bars": eq.get("overlap_bars"),
                }
            )
        csv_paths["provider_validation"] = _write_csv(
            REPORTS / "phase17_provider_validation.csv", prov_rows
        )
        eq_rows = []
        for e in evaluations:
            eq = e.get("equivalence")
            if not eq:
                continue
            eq_rows.append(
                {
                    "provider": e.get("provider"),
                    "symbol": e.get("symbol"),
                    "classification": eq.get("classification"),
                    "overlap_bars": eq.get("overlap_bars"),
                    "session_hl_match_rate": (eq.get("session_match_metrics") or {}).get(
                        "session_hl_match_rate"
                    ),
                    "sweep_match_rate": (eq.get("sweep_match_metrics") or {}).get(
                        "sweep_side_match_rate"
                    ),
                    "exact_ohlc_match_pct": (eq.get("ohlc_metrics") or {}).get(
                        "exact_ohlc_match_pct"
                    ),
                    "deep_replay_allowed": e.get("deep_replay_allowed"),
                }
            )
        csv_paths["feed_equivalence"] = _write_csv(
            REPORTS / "phase17_feed_equivalence.csv", eq_rows
        )
        if replay.get("executed"):
            # setup summary from journal file if present
            from setup_journal import load_journal_records

            rows = load_journal_records(root=JOURNAL)
            summary = []
            for r in rows:
                for e in r.get("entry_results") or []:
                    summary.append(
                        {
                            "setup_id": r.get("setup_id"),
                            "session": r.get("session"),
                            "execution_timeframe": r.get("execution_timeframe"),
                            "status": r.get("status"),
                            "entry_mode": e.get("mode"),
                            "outcome": e.get("outcome"),
                            "underlying_provider": (r.get("extras") or {}).get(
                                "underlying_provider"
                            ),
                            "feed_equivalence_class": (r.get("extras") or {}).get(
                                "feed_equivalence_class"
                            ),
                        }
                    )
            csv_paths["setup_summary"] = _write_csv(
                REPORTS / "phase17_setup_summary.csv", summary
            )

    out = {
        "ok": True,
        "phase": 17,
        "strategy_version": STRATEGY_VERSION,
        "generated_at": datetime.now(tz=timezone.utc).isoformat(),
        "openbb_version": openbb_version(),
        "tv_benchmark_window": {
            "earliest": start,
            "latest": end,
            "bar_count": tv.get("bar_count"),
        },
        "provider_validation": {
            "evaluations": evaluations,
            "accepted_provider": None
            if accepted is None
            else {
                "provider": accepted.get("provider"),
                "symbol": accepted.get("symbol"),
                "instrument_type": accepted.get("instrument_type"),
                "classification": (accepted.get("equivalence") or {}).get(
                    "classification"
                ),
            },
            "fmp_evaluated": fmp_evaluated,
            "other_candidates": other_candidates,
        },
        "feed_comparison": None
        if accepted is None
        else {
            "equivalence": accepted.get("equivalence"),
            "gate": accepted.get("gate"),
            "integrity": accepted.get("integrity"),
        },
        "deep_history": {
            k: v
            for k, v in deep.items()
            if k != "bars_by_tf"
        },
        "historical_replay": {
            k: v
            for k, v in replay.items()
            if k != "mtf_report"
        },
        "replay_details": replay.get("mtf_report") if replay.get("executed") else None,
        "luxalgo": lux,
        "csv_reports": csv_paths,
        "limitations": [
            "No Tiingo/FMP credentials present in environment or OpenBB user settings"
            if accepted is None
            else "Deep history limited by provider/API constraints",
            "Futures (GC) intentionally excluded from canonical gate",
            "TradingView OANDA:XAUUSD remains the live benchmark",
            "Strategy rules unchanged — evidence gathering only",
        ],
        "recommended_next_phase": [
            "Set TIINGO_TOKEN (preferred) or FMP_API_KEY in .env / OpenBB credentials",
            "Re-run phase17_validate.py to complete overlap → gate → deep replay",
            "Only after LARGER_SAMPLE evidence: consider strategy locking discussions",
            "Keep LuxAlgo live capture campaign separate from OpenBB history",
        ]
        if accepted is None
        else [
            "Review descriptive Phase 17 journal evidence without optimizing yet",
            "Optionally extend history toward 1–2 years if session N still moderate",
            "Continue LuxAlgo live overlap accumulation",
        ],
        "conclusion": (
            "Phase 17 blocked: no credentialed spot-XAUUSD provider available to test "
            "against TradingView OANDA:XAUUSD. Equivalence gate correctly idle; "
            "deep replay not executed."
            if accepted is None
            and all(
                (e.get("reason") == "credential_absent")
                for e in evaluations
                if e.get("provider") in ("tiingo", "fmp")
            )
            else (
                "No spot-XAUUSD provider passed the feed-equivalence gate; "
                "deep replay not executed."
                if accepted is None
                else (
                    "Accepted spot provider passed equivalence gate; deep replay executed."
                    if replay.get("executed")
                    else "Provider accepted but deep download/replay did not complete."
                )
            )
        ),
    }

    if write_artifacts:
        Path("phase17_validation.json").write_text(
            json.dumps(_scrub(out), indent=2, default=str), encoding="utf-8"
        )
    return out


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(description="Phase 17 credentialed spot XAUUSD validation")
    p.add_argument("--preflight", action="store_true")
    p.add_argument("--overlap-only", action="store_true")
    p.add_argument("--download-deep", action="store_true")
    p.add_argument("--replay", action="store_true")
    p.add_argument("--months", type=int, default=3)
    p.add_argument("--full", action="store_true")
    args = p.parse_args(argv)

    if args.preflight:
        load_dotenv_credentials()
        print(
            json.dumps(
                {
                    "tiingo": provider_preflight("tiingo"),
                    "fmp": provider_preflight("fmp"),
                },
                indent=2,
            )
        )
        return 0

    out = run_phase17(deep_months=args.months)
    print(
        json.dumps(
            {
                "ok": out.get("ok"),
                "conclusion": out.get("conclusion"),
                "accepted": out.get("provider_validation", {}).get("accepted_provider"),
                "deep_downloaded": (out.get("deep_history") or {}).get("downloaded"),
                "replay_executed": (out.get("historical_replay") or {}).get("executed"),
                "journal_size": (out.get("historical_replay") or {}).get("journal_size"),
            },
            indent=2,
        )
    )
    return 0 if out.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
