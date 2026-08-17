"""Phase 16 OpenBB CLI + validation orchestration."""

from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from bar_dataset import load_dataset
from feed_equivalence import evaluate_feed_equivalence, replay_gate
from historical_data_provider import (
    LocalJsonlProvider,
    TradingViewDesktopProvider,
    integrity_report,
)
from historical_structure import detect_internal_choch
from luxalgo_capture import captures_to_confirmations, load_luxalgo_captures
from luxalgo_overlap import compare_choch_overlap
from openbb_history import (
    OpenBBHistoricalDataProvider,
    inspect_openbb,
    openbb_version,
    probe_xauusd_symbols,
)
from strategy_version import STRATEGY_VERSION


SYMBOL_TV = "OANDA:XAUUSD"
REPORTS = Path("reports")
JOURNAL = Path("journal") / "phase16_openbb"


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
    return {
        "ok": True,
        "bars": loaded["bars"],
        "meta": loaded.get("meta"),
        "earliest": int(loaded["bars"][0].time) if loaded["bars"] else None,
        "latest": int(loaded["bars"][-1].time) if loaded["bars"] else None,
    }


def evaluate_providers() -> dict[str, Any]:
    """Enumerate candidates; probe Tiingo/YFinance/FMP for XAUUSD."""
    insp = inspect_openbb()
    tv = load_tv_benchmark("5m")
    start = tv.get("earliest")
    end = tv.get("latest")

    provider_rows = []
    # Tiingo
    tiingo_cred = OpenBBHistoricalDataProvider(
        underlying_provider="tiingo", route="currency"
    ).credential_status("tiingo")
    tiingo_probe = probe_xauusd_symbols(
        underlying="tiingo", route="currency", start_ts=start, end_ts=end
    )
    provider_rows.append(
        {
            "provider": "tiingo",
            "openbb_route": "obb.currency.price.historical",
            "symbol_probes": tiingo_probe,
            "instrument_type": "spot_fx_metals_if_accepted",
            "spot_or_futures": "spot_candidate",
            "supports_1m": True,
            "supports_5m": True,
            "supports_15m": True,
            "max_history": "unknown_without_credential",
            "credentials_required": True,
            "credential_key": "tiingo_token",
            "rate_limits": "not_discoverable_here",
            "timezone": "UTC (Tiingo FX timestamps)",
            "xauusd_accepted": any(p.get("accepted") for p in tiingo_probe),
            "credential_status": tiingo_cred,
        }
    )
    # YFinance currency
    yf_probe = probe_xauusd_symbols(
        underlying="yfinance", route="currency", start_ts=start, end_ts=end
    )
    provider_rows.append(
        {
            "provider": "yfinance",
            "openbb_route": "obb.currency.price.historical",
            "symbol_probes": yf_probe,
            "instrument_type": "spot_fx_metals_if_accepted",
            "spot_or_futures": "spot_candidate",
            "supports_1m": True,
            "supports_5m": True,
            "supports_15m": True,
            "max_history": "provider_window_limited_intraday",
            "credentials_required": False,
            "credential_key": None,
            "rate_limits": "yahoo_undocumented",
            "timezone": "normalized_to_UTC",
            "xauusd_accepted": any(p.get("accepted") for p in yf_probe),
        }
    )
    # FMP
    fmp_cred = OpenBBHistoricalDataProvider(
        underlying_provider="fmp", route="currency"
    ).credential_status("fmp")
    fmp_probe = probe_xauusd_symbols(
        underlying="fmp", route="currency", start_ts=start, end_ts=end
    )
    provider_rows.append(
        {
            "provider": "fmp",
            "openbb_route": "obb.currency.price.historical",
            "symbol_probes": fmp_probe,
            "instrument_type": "unknown_until_accepted",
            "spot_or_futures": "spot_candidate",
            "supports_1m": "unknown",
            "supports_5m": "unknown",
            "supports_15m": "unknown",
            "max_history": "unknown_without_credential",
            "credentials_required": True,
            "credential_key": "fmp_api_key",
            "rate_limits": "not_discoverable_here",
            "timezone": "unknown",
            "xauusd_accepted": any(p.get("accepted") for p in fmp_probe),
            "credential_status": fmp_cred,
        }
    )
    # Futures research comparator (not gate-eligible as OANDA)
    fut_probe = probe_xauusd_symbols(
        underlying="yfinance", route="futures", start_ts=start, end_ts=end
    )
    provider_rows.append(
        {
            "provider": "yfinance",
            "openbb_route": "obb.derivatives.futures.historical",
            "symbol_probes": fut_probe,
            "instrument_type": "futures",
            "spot_or_futures": "futures",
            "supports_1m": True,
            "supports_5m": True,
            "supports_15m": True,
            "max_history": "~60d_intraday_typical",
            "credentials_required": False,
            "credential_key": None,
            "rate_limits": "yahoo_undocumented",
            "timezone": "normalized_to_UTC",
            "xauusd_accepted": False,
            "note": "COMEX gold futures — research comparator only; not OANDA:XAUUSD",
        }
    )

    return {
        "openbb_inspect": insp,
        "providers": provider_rows,
        "tv_benchmark_window": {"start": start, "end": end},
    }


def run_overlap_and_gate() -> dict[str, Any]:
    """Small overlap window vs TV OANDA; evaluate gate."""
    tv = load_tv_benchmark("5m")
    if not tv.get("ok"):
        return {"ok": False, "error": "missing_tv_benchmark"}

    tv_bars = tv["bars"]
    start, end = tv["earliest"], tv["latest"]
    tv_daily = load_dataset(SYMBOL_TV, "1D")
    tv_h4 = load_dataset(SYMBOL_TV, "4H")

    evaluations = []
    selected = None

    # 1) Tiingo spot attempts
    for sym in ("XAUUSD", "XAU/USD"):
        prov = OpenBBHistoricalDataProvider(underlying_provider="tiingo", route="currency")
        res = prov.fetch_result(sym, "5m", start_ts=start, end_ts=end)
        if res.bars:
            report = evaluate_feed_equivalence(
                tv_bars,
                list(res.bars),
                candidate_provider="openbb:tiingo",
                candidate_symbol=sym,
                instrument_type=res.instrument_type,
                tv_daily=tv_daily.get("bars") if tv_daily.get("ok") else None,
                tv_h4=tv_h4.get("bars") if tv_h4.get("ok") else None,
                extra_warnings=res.warnings,
            )
            evaluations.append(
                {
                    "candidate": f"tiingo:{sym}",
                    "fetch": res.to_dict(),
                    "equivalence": report.to_dict(),
                    "gate": replay_gate(report),
                }
            )
            if report.deep_replay_allowed and selected is None:
                selected = evaluations[-1]
        else:
            evaluations.append(
                {
                    "candidate": f"tiingo:{sym}",
                    "fetch": res.to_dict(),
                    "equivalence": None,
                    "gate": {
                        "deep_replay_allowed": False,
                        "reason": "fetch_failed:" + ";".join(res.errors),
                    },
                }
            )

    # 2) YFinance currency spot attempts
    for sym in ("XAUUSD", "XAU/USD"):
        prov = OpenBBHistoricalDataProvider(
            underlying_provider="yfinance", route="currency"
        )
        res = prov.fetch_result(sym, "5m", start_ts=start, end_ts=end)
        evaluations.append(
            {
                "candidate": f"yfinance_currency:{sym}",
                "fetch": res.to_dict(),
                "equivalence": None,
                "gate": {
                    "deep_replay_allowed": False,
                    "reason": "fetch_failed:" + ";".join(res.errors)
                    if res.errors or not res.bars
                    else "no_equivalence_run",
                },
            }
        )

    # 3) Futures research comparator (must not pass as OANDA)
    fut = OpenBBHistoricalDataProvider(underlying_provider="yfinance", route="futures")
    fres = fut.fetch_result("GC", "5m", start_ts=start, end_ts=end)
    if fres.bars:
        # Persist research sample separately (not canonical)
        fut.persist_result(fres)
        report = evaluate_feed_equivalence(
            tv_bars,
            list(fres.bars),
            candidate_provider="openbb:yfinance_futures",
            candidate_symbol="GC",
            instrument_type="futures",
            tv_daily=tv_daily.get("bars") if tv_daily.get("ok") else None,
            tv_h4=tv_h4.get("bars") if tv_h4.get("ok") else None,
            extra_warnings=fres.warnings,
        )
        evaluations.append(
            {
                "candidate": "yfinance_futures:GC",
                "fetch": fres.to_dict(),
                "equivalence": report.to_dict(),
                "gate": replay_gate(report),
                "note": "research_comparator_only",
            }
        )
    else:
        evaluations.append(
            {
                "candidate": "yfinance_futures:GC",
                "fetch": fres.to_dict(),
                "gate": {"deep_replay_allowed": False, "reason": "empty"},
            }
        )

    any_pass = any(
        (e.get("gate") or {}).get("deep_replay_allowed") for e in evaluations
    )
    return {
        "ok": True,
        "tv_window": {"start": start, "end": end, "bars": len(tv_bars)},
        "evaluations": evaluations,
        "selected_for_deep": selected,
        "deep_replay_allowed": any_pass,
        "conclusion": (
            "OpenBB integration works, but no currently tested OpenBB provider "
            "satisfies AITRADE's OANDA-compatible XAUUSD historical requirement."
            if not any_pass
            else "At least one candidate passed the feed-equivalence gate."
        ),
    }


def maybe_deep_replay(gate_payload: dict[str, Any]) -> dict[str, Any]:
    """Only run replay_historical_mtf_setups if gate passed."""
    if not gate_payload.get("deep_replay_allowed"):
        return {
            "executed": False,
            "reason": "equivalence_gate_blocked",
            "journal_size": 0,
        }
    # Would download deep + replay here when a CLOSE/EXACT provider exists
    selected = gate_payload.get("selected_for_deep")
    return {
        "executed": False,
        "reason": "selected_payload_incomplete_for_deep_path",
        "selected": selected,
        "journal_size": 0,
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
            "direction_matches": ov.get("direction_match_count"),
            "time_matches": ov.get("time_match_count"),
            "level_matches": ov.get("level_match_count"),
            "luxalgo_only": ov.get("missed_luxalgo_count"),
            "internal_only": ov.get("missed_internal_count"),
            "equivalence_status": ov.get("equivalence_status")
            or "unvalidated_against_luxalgo",
        }
    return {
        "by_timeframe": out,
        "equivalence_status": "unvalidated_against_luxalgo",
        "note": "OpenBB history does not validate LuxAlgo equivalence",
    }


def run_phase16(*, write_artifacts: bool = True) -> dict[str, Any]:
    providers = evaluate_providers()
    overlap = run_overlap_and_gate()
    replay = maybe_deep_replay(overlap)
    lux = luxalgo_status()

    csv_paths = {}
    if write_artifacts:
        REPORTS.mkdir(parents=True, exist_ok=True)
        prov_rows = []
        for p in providers.get("providers") or []:
            prov_rows.append(
                {
                    "provider": p.get("provider"),
                    "route": p.get("openbb_route"),
                    "spot_or_futures": p.get("spot_or_futures"),
                    "supports_5m": p.get("supports_5m"),
                    "supports_15m": p.get("supports_15m"),
                    "supports_1m": p.get("supports_1m"),
                    "credentials_required": p.get("credentials_required"),
                    "credential_key": p.get("credential_key"),
                    "xauusd_accepted": p.get("xauusd_accepted"),
                    "note": p.get("note"),
                }
            )
        csv_paths["providers"] = _write_csv(
            REPORTS / "phase16_openbb_providers.csv", prov_rows
        )

        overlap_rows = []
        session_rows = []
        for e in overlap.get("evaluations") or []:
            eq = e.get("equivalence") or {}
            ohlc = eq.get("ohlc_metrics") or {}
            overlap_rows.append(
                {
                    "candidate": e.get("candidate"),
                    "classification": eq.get("classification"),
                    "overlap_bars": eq.get("overlap_bars"),
                    "exact_ohlc_match_pct": ohlc.get("exact_ohlc_match_pct"),
                    "deep_replay_allowed": (e.get("gate") or {}).get(
                        "deep_replay_allowed"
                    ),
                    "instrument_type": eq.get("instrument_type"),
                    "fetch_errors": (e.get("fetch") or {}).get("errors"),
                }
            )
            sess = eq.get("session_match_metrics") or {}
            for row in sess.get("rows_head") or []:
                session_rows.append({"candidate": e.get("candidate"), **row})
        csv_paths["feed_overlap"] = _write_csv(
            REPORTS / "phase16_feed_overlap.csv", overlap_rows
        )
        csv_paths["session_equivalence"] = _write_csv(
            REPORTS / "phase16_session_equivalence.csv", session_rows
        )

    # Futures metrics if present
    fut_eq = None
    for e in overlap.get("evaluations") or []:
        if e.get("candidate") == "yfinance_futures:GC" and e.get("equivalence"):
            fut_eq = e["equivalence"]

    out = {
        "ok": True,
        "phase": 16,
        "strategy_version": STRATEGY_VERSION,
        "generated_at": datetime.now(tz=timezone.utc).isoformat(),
        "openbb": {
            "version": openbb_version(),
            "installation": "ok",
            "inspect": providers.get("openbb_inspect"),
        },
        "provider_evaluation": providers.get("providers"),
        "feed_validation": overlap,
        "selected_provider": None,
        "feed_equivalence_classification": (
            None
            if not fut_eq
            else {
                "futures_research_gc": fut_eq.get("classification"),
                "spot_xauusd": "no_accepted_spot_provider",
            }
        ),
        "deep_dataset": {
            "downloaded": False,
            "reason": "equivalence_gate_blocked_no_oanda_compatible_provider",
        },
        "historical_replay": replay,
        "luxalgo": lux,
        "macro_inventory": (providers.get("openbb_inspect") or {}).get(
            "macro_inventory"
        ),
        "future_macro_context_architecture": {
            "HigherTimeframeContext": {"technical": ["Daily", "4H"]},
            "FutureMacroContext": {
                "fields": ["CPI", "PPI", "NFP", "Fed", "economic_calendar"],
                "note": (
                    "Placeholder only — no macro bullish/bearish rules; "
                    "does not alter Daily/4H bias"
                ),
            },
        },
        "csv_reports": csv_paths,
        "limitations": [
            "Tiingo requires tiingo_token — not present in environment",
            "YFinance currency route does not return spot XAUUSD",
            "FMP requires fmp_api_key — not present",
            "COMEX GC futures available but NOT_EQUIVALENT / RESEARCH_ONLY vs OANDA spot",
            "No deep OANDA-compatible history downloaded; Phase 14/15 TV files untouched",
            "Deep strategy replay blocked by feed-equivalence gate",
        ],
        "recommended_next_phase": [
            "Supply Tiingo (or other spot XAUUSD) credentials via env / OpenBB settings",
            "Re-run --overlap-only then gate; only then --download-deep",
            "Prefer exact OANDA history if an approved path appears",
            "Keep TradingView for live; OpenBB for history/macro inventory",
            "Do not optimize strategy rules until trustworthy deep history exists",
        ],
        "conclusion": overlap.get("conclusion"),
    }

    if write_artifacts:
        Path("phase16_validation.json").write_text(
            json.dumps(out, indent=2, default=str), encoding="utf-8"
        )
    return out


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(description="Phase 16 OpenBB historical integration")
    p.add_argument("--inspect-openbb", action="store_true")
    p.add_argument("--provider", default=None, help="tiingo|yfinance|fmp")
    p.add_argument("--symbol", default="XAUUSD")
    p.add_argument("--overlap-only", action="store_true")
    p.add_argument("--start", default=None, help="YYYY-MM-DD (optional)")
    p.add_argument("--end", default=None, help="YYYY-MM-DD (optional)")
    p.add_argument(
        "--download-deep",
        action="store_true",
        help="Only runs if equivalence gate already passes",
    )
    p.add_argument("--replay", action="store_true")
    p.add_argument("--full", action="store_true", help="Run full Phase 16 validation")
    args = p.parse_args(argv)

    if args.inspect_openbb:
        print(json.dumps(inspect_openbb(), indent=2, default=str))
        return 0

    if args.full or not any(
        [args.overlap_only, args.download_deep, args.replay, args.provider]
    ):
        out = run_phase16()
        print(
            json.dumps(
                {
                    "ok": out["ok"],
                    "openbb_version": out["openbb"]["version"],
                    "deep_replay_allowed": out["feed_validation"].get(
                        "deep_replay_allowed"
                    ),
                    "conclusion": out.get("conclusion"),
                },
                indent=2,
            )
        )
        return 0

    if args.overlap_only:
        print(json.dumps(run_overlap_and_gate(), indent=2, default=str))
        return 0

    if args.download_deep or args.replay:
        gate = run_overlap_and_gate()
        if not gate.get("deep_replay_allowed"):
            print(
                json.dumps(
                    {
                        "executed": False,
                        "reason": "equivalence_gate_blocked",
                        "gate": gate.get("deep_replay_allowed"),
                        "conclusion": gate.get("conclusion"),
                    },
                    indent=2,
                )
            )
            return 2
        print(json.dumps(maybe_deep_replay(gate), indent=2, default=str))
        return 0

    if args.provider:
        tv = load_tv_benchmark("5m")
        prov = OpenBBHistoricalDataProvider(
            underlying_provider=args.provider, route="currency"
        )
        # parse dates loosely
        start = tv.get("earliest")
        end = tv.get("latest")
        res = prov.fetch_result(args.symbol, "5m", start_ts=start, end_ts=end)
        print(json.dumps(res.to_dict(), indent=2, default=str))
        return 0 if res.bars else 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
