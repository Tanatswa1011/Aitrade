"""Phase 23 — Databento GC depth + frozen Phase 22 ORB revalidation."""

from __future__ import annotations

import json
import statistics
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from bar_dataset import load_dataset, write_dataset
from databento_history import (
    DATA_ROOT,
    VOLUME_SEMANTICS,
    VOLUME_STATUS,
    DatabentoHistoricalDataProvider,
    databento_preflight,
    persist_contract_bars,
    validate_bars_quality,
)
from gc_contract_stitch import (
    ContractSeries,
    decide_rolls,
    detect_roll_price_artifacts,
    persist_stitched,
    stitch_contracts,
)
from gc_orb_engine import build_opening_range, config_hash, trading_dates_in_bars
from gc_orb_models import (
    DISPLACEMENT_BODY_OR_RATIO,
    PHASE22_CANDIDATES,
    STRATEGY_FAMILY,
    STRATEGY_VERSION,
    VOLUME_RVOL_THRESHOLD,
    GCORBStrategyConfig,
)
from gc_orb_replay import collect_or30_events, replay_all_candidates
from phase18_metrics import iter_entry_pairs, median_or_none, scorecard_from_pairs
from phase22_validate import (
    _write_csv,
    body_bucket,
    bucket_outcomes,
    classify_stability,
    decide_verdict,
    evaluate_rows,
    rvol_bucket,
    volume_conclusion,
)
from setup_journal import append_journal_records, load_journal_records

REPORTS = Path("reports")
JOURNAL_DIR = Path("journal") / "phase23_gc_orb"
VALIDATION_JSON = Path("phase23_validation.json")
CANDIDATES_DIR = Path("strategy_candidates")
YAHOO_ROOT = Path("data") / "openbb" / "yfinance"
PHASE22_G1 = CANDIDATES_DIR / "phase22_gc_G1_OR30_bo_volOFF_dispOFF.json"
GC_TICK_SIZE_DOCUMENTED = 0.1  # COMEX GC; verify via definition when credential available


def load_frozen_phase22_candidates() -> list[GCORBStrategyConfig]:
    configs: list[GCORBStrategyConfig] = []
    paths = sorted(CANDIDATES_DIR.glob("phase22_gc_*.json"))
    for p in paths:
        raw = json.loads(p.read_text(encoding="utf-8"))
        c = raw.get("candidate") or {}
        configs.append(
            GCORBStrategyConfig(
                strategy_family=c.get("strategy_family", STRATEGY_FAMILY),
                candidate_id=c["candidate_id"],
                or_minutes=int(c.get("or_minutes", 30)),
                volume_filter=bool(c.get("volume_filter", False)),
                displacement_filter=bool(c.get("displacement_filter", False)),
                rvol_threshold=float(c.get("rvol_threshold", 1.5)),
                displacement_body_or_ratio=float(c.get("displacement_body_or_ratio", 0.5)),
                entry_mode=str(c.get("entry_mode")),
                stop_mode=str(c.get("stop_mode")),
                max_retest_bars=int(c.get("max_retest_bars", 6)),
                rvol_lookback=int(c.get("rvol_lookback", 20)),
                execution_timeframe=str(c.get("execution_timeframe", "5m")),
                extras=dict(c.get("extras") or {}),
            )
        )
    have = {c.candidate_id for c in configs}
    for c in PHASE22_CANDIDATES:
        if c.candidate_id not in have:
            configs.append(c)
    order = {c.candidate_id: i for i, c in enumerate(PHASE22_CANDIDATES)}
    configs.sort(key=lambda c: order.get(c.candidate_id, 999))
    return configs


def assert_g1_frozen(configs: list[GCORBStrategyConfig]) -> dict[str, Any]:
    g1 = next(c for c in configs if c.candidate_id.startswith("G1_"))
    frozen = json.loads(PHASE22_G1.read_text(encoding="utf-8"))
    fc = frozen["candidate"]
    frozen_cfg = GCORBStrategyConfig(
        strategy_family=fc.get("strategy_family", STRATEGY_FAMILY),
        candidate_id=fc["candidate_id"],
        or_minutes=int(fc["or_minutes"]),
        volume_filter=bool(fc["volume_filter"]),
        displacement_filter=bool(fc["displacement_filter"]),
        rvol_threshold=float(fc["rvol_threshold"]),
        displacement_body_or_ratio=float(fc["displacement_body_or_ratio"]),
        entry_mode=str(fc["entry_mode"]),
        stop_mode=str(fc["stop_mode"]),
        max_retest_bars=int(fc["max_retest_bars"]),
        rvol_lookback=int(fc["rvol_lookback"]),
        execution_timeframe=str(fc["execution_timeframe"]),
    )
    h_live = config_hash(g1)
    h_frozen = config_hash(frozen_cfg)
    fields = [
        "or_minutes",
        "volume_filter",
        "displacement_filter",
        "rvol_threshold",
        "displacement_body_or_ratio",
        "entry_mode",
        "stop_mode",
        "max_retest_bars",
    ]
    mismatches = [f for f in fields if getattr(g1, f) != getattr(frozen_cfg, f)]
    return {
        "ok": h_live == h_frozen and g1.candidate_id == frozen_cfg.candidate_id and not mismatches,
        "live_hash": h_live,
        "frozen_hash": h_frozen,
        "mismatches": mismatches,
        "or_anchor": (frozen.get("predeclared") or {}).get("or_anchor"),
    }


def ensure_frozen_candidate_jsons() -> list[str]:
    """Persist missing Phase 22 G4–G8 definitions from in-code frozen matrix (no rule changes)."""
    written: list[str] = []
    CANDIDATES_DIR.mkdir(parents=True, exist_ok=True)
    for cfg in PHASE22_CANDIDATES:
        path = CANDIDATES_DIR / f"phase22_gc_{cfg.candidate_id}.json"
        if path.exists():
            continue
        payload = {
            "phase": "phase22",
            "strategy_family": STRATEGY_FAMILY,
            "strategy_version": STRATEGY_VERSION,
            "instrument": "GC",
            "provider": "openbb:yfinance",
            "candidate": cfg.to_dict(),
            "predeclared": {
                "rvol_threshold": VOLUME_RVOL_THRESHOLD,
                "displacement_body_or_ratio": DISPLACEMENT_BODY_OR_RATIO,
                "or_anchor": "08:20 America/New_York",
            },
            "note": "Frozen Phase 22 candidate definition for Phase 23 revalidation (no strategy mutation)",
        }
        path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
        written.append(str(path))
    return written


def chronological_split(rows: list[dict], train_fraction: float = 0.70) -> tuple[list, list, dict]:
    dates = sorted({str(r.get("trading_date"))[:10] for r in rows if r.get("trading_date")})
    if len(dates) < 4:
        cut = max(1, len(dates) // 2)
    else:
        cut = max(1, min(len(dates) - 1, int(round(len(dates) * train_fraction))))
    train_d, hold_d = set(dates[:cut]), set(dates[cut:])
    train = [r for r in rows if str(r.get("trading_date"))[:10] in train_d]
    hold = [r for r in rows if str(r.get("trading_date"))[:10] in hold_d]
    return train, hold, {
        "train_start": dates[0] if dates else None,
        "train_end": dates[cut - 1] if dates else None,
        "holdout_start": dates[cut] if cut < len(dates) else None,
        "holdout_end": dates[-1] if dates else None,
        "train_dates": len(train_d),
        "holdout_dates": len(hold_d),
        "train_fraction": train_fraction,
        "method": "chronological_trading_date_70_30_databento",
    }


def yahoo_overlap_report(db_bars: list) -> dict[str, Any]:
    y = load_dataset("openbb_yfinance_GC", "5m", root=YAHOO_ROOT)
    ybars = y.get("bars") or []
    if not db_bars or not ybars:
        return {"ok": False, "reason": "missing_one_or_both_series"}
    ymap = {int(b.time): b for b in ybars}
    dmap = {int(b.time): b for b in db_bars}
    overlap = sorted(set(ymap) & set(dmap))
    if not overlap:
        return {"ok": True, "overlap_bars": 0, "note": "no_timestamp_overlap"}
    close_deltas = [abs(float(dmap[t].close) - float(ymap[t].close)) for t in overlap]
    vol_pairs = [
        (float(dmap[t].volume or 0), float(ymap[t].volume or 0))
        for t in overlap
        if dmap[t].volume is not None and ymap[t].volume is not None
    ]
    return {
        "ok": True,
        "overlap_bars": len(overlap),
        "median_abs_close_delta": sorted(close_deltas)[len(close_deltas) // 2],
        "max_abs_close_delta": max(close_deltas),
        "mean_abs_close_delta": sum(close_deltas) / len(close_deltas),
        "volume_pairs": len(vol_pairs),
        "note": "Diagnostic only; Yahoo GC=F continuous vs Databento contracts may differ",
    }


def attempt_databento_fetch() -> dict[str, Any]:
    """Probe then fetch deeper history. Prefer raw contracts + AITRADE stitch; continuous fallback."""
    pf = databento_preflight()
    out: dict[str, Any] = {"preflight": pf, "fetched": False}
    if not pf.get("ok"):
        out["error_code"] = pf.get("error_code")
        return out

    provider = DatabentoHistoricalDataProvider()
    deep_start = "2025-08-01"
    deep_end = "2026-08-15"
    probe_start = "2026-08-01"
    probe_end = "2026-08-05"

    # Discover raw contracts via parent symbology
    discovered = provider.list_gc_raw_symbols(start=deep_start, end=deep_end, parent="GC.FUT")
    out["symbol_discovery"] = {
        "ok": discovered.get("ok"),
        "error": discovered.get("error"),
        "n_symbols": len(discovered.get("raw_symbols") or []),
        "raw_symbols": (discovered.get("raw_symbols") or [])[:40],
    }
    raw_symbols = list(discovered.get("raw_symbols") or [])

    if raw_symbols:
        # Probe with volume continuous (always maps to active front), then raw contracts
        probe_cost = provider.estimate_cost(
            symbols=["GC.v.0"],
            start=probe_start,
            end=probe_end,
            schema="ohlcv-1m",
            stype_in="continuous",
        )
        out["probe_cost_estimate"] = probe_cost
        probe = provider.fetch_5m(
            ["GC.v.0"], start=probe_start, end=probe_end, stype_in="continuous", limit=5000
        )
        out["probe"] = {
            "ok": not bool(probe.errors) and len(probe.bars) > 0,
            "errors": list(probe.errors),
            "bars": len(probe.bars),
            "symbol": "GC.v.0",
            "qa": validate_bars_quality(probe.bars) if probe.bars else None,
        }
        if probe.errors or not probe.bars:
            out["error_code"] = "DATABENTO_PROBE_FAILED"
            return out

        deep_cost = provider.estimate_cost(
            symbols=raw_symbols,
            start=deep_start,
            end=deep_end,
            schema="ohlcv-1m",
            stype_in="raw_symbol",
        )
        out["deep_cost_estimate"] = deep_cost

        series_list: list[ContractSeries] = []
        contracts_meta: list[dict[str, Any]] = []
        for sym in raw_symbols:
            # Full-window fetch per contract (provider-safe at this symbol count / depth)
            res = provider.fetch_5m(
                [sym], start=deep_start, end=deep_end, stype_in="raw_symbol"
            )
            if res.errors or not res.bars:
                continue
            all_bars = list(res.bars)
            persist_contract_bars(
                all_bars,
                contract=sym,
                root=DATA_ROOT,
                extras={
                    "contract_symbol": sym,
                    "root": "GC",
                    "exchange": "GLBX",
                    "provider": "databento",
                    "dataset": "GLBX.MDP3",
                    "first_seen": int(all_bars[0].time),
                    "last_seen": int(all_bars[-1].time),
                    "tick_size": 0.1,
                },
            )
            series_list.append(
                ContractSeries(
                    contract_symbol=sym,
                    bars=tuple(all_bars),
                    first_seen=int(all_bars[0].time),
                    last_seen=int(all_bars[-1].time),
                    exchange="GLBX",
                    root="GC",
                )
            )
            contracts_meta.append(
                {
                    "contract_symbol": sym,
                    "first_seen": int(all_bars[0].time),
                    "last_seen": int(all_bars[-1].time),
                    "bars_5m": len(all_bars),
                    "exchange": "GLBX",
                    "root": "GC",
                    "tick_size": 0.1,
                }
            )

        if len(series_list) < 1:
            out["error_code"] = "DATABENTO_NO_CONTRACT_BARS"
            # fall through to continuous fallback below
        else:
            # Use discovered calendar order (already sorted), intersect with fetched
            have = {s.contract_symbol for s in series_list}
            order = [s for s in raw_symbols if s in have]
            rolls = decide_rolls(series_list, calendar_order=order)
            stitched, _prov = stitch_contracts(series_list, rolls)
            artifacts = detect_roll_price_artifacts(stitched, rolls)
            written = persist_stitched(
                stitched,
                rolls=rolls,
                root=DATA_ROOT,
                meta_extras={
                    "volume_semantics": VOLUME_SEMANTICS,
                    "volume_status": VOLUME_STATUS,
                    "schema": "ohlcv-5m(agg-from-1m)",
                    "contracts": contracts_meta,
                    "continuous_choice": "aitrade_volume_crossover_unadjusted",
                    "phase": 23,
                    "tick_size": 0.1,
                    "compared_to": "databento_GC.v.0_available_as_continuous_alternative",
                },
            )
            out.update(
                {
                    "fetched": True,
                    "bars_5m": len(stitched),
                    "path": written["bars_path"],
                    "rolls_path": written["rolls_path"],
                    "meta_path": written["meta_path"],
                    "contracts": contracts_meta,
                    "rolls": [r.to_dict() for r in rolls],
                    "roll_artifacts": artifacts,
                    "continuous_choice": "aitrade_volume_crossover_unadjusted",
                    "volume_semantics": VOLUME_SEMANTICS,
                    "volume_status": VOLUME_STATUS,
                    "qa": validate_bars_quality(stitched),
                }
            )
            return out

    # Fallback: Databento continuous volume-front GC.v.0 (NOT calendar GC.c.0 —
    # calendar front can map to illiquid/near-expiry instruments with sparse OHLCV).
    cont_sym = "GC.v.0"
    cost = provider.estimate_cost(
        symbols=[cont_sym],
        start=probe_start,
        end=probe_end,
        schema="ohlcv-1m",
        stype_in="continuous",
    )
    out["probe_cost_estimate"] = cost
    probe = provider.fetch_5m(
        [cont_sym], start=probe_start, end=probe_end, stype_in="continuous", limit=5000
    )
    out["probe"] = {
        "ok": not bool(probe.errors),
        "errors": list(probe.errors),
        "bars": len(probe.bars),
        "schema": probe.schema,
        "symbol": cont_sym,
    }
    if probe.errors:
        out["error_code"] = "DATABENTO_PROBE_FAILED"
        out["probe_errors"] = list(probe.errors)
        return out

    deep_cost = provider.estimate_cost(
        symbols=[cont_sym],
        start=deep_start,
        end=deep_end,
        schema="ohlcv-1m",
        stype_in="continuous",
    )
    out["deep_cost_estimate"] = deep_cost
    deep = provider.fetch_5m([cont_sym], start=deep_start, end=deep_end, stype_in="continuous")
    if deep.errors:
        out["error_code"] = "DATABENTO_DEEP_FETCH_FAILED"
        out["deep_errors"] = list(deep.errors)
        return out

    stitched_dir = DATA_ROOT / "stitched"
    stitched_dir.mkdir(parents=True, exist_ok=True)
    written = write_dataset(
        list(deep.bars),
        symbol="databento_GC_stitched",
        timeframe="5m",
        source="databento:GLBX.MDP3:continuous_GC.v.0",
        root=stitched_dir,
        expected_period_sec=300,
    )
    meta_path = Path(written["path"]).with_suffix(".meta.json")
    if meta_path.exists():
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        meta.update(
            {
                "provider": "databento",
                "dataset": "GLBX.MDP3",
                "symbology": cont_sym,
                "stype_in": "continuous",
                "continuous": True,
                "continuous_method": "databento_native_volume_continuous_GC.v.0",
                "back_adjusted": False,
                "volume_semantics": VOLUME_SEMANTICS,
                "volume_status": VOLUME_STATUS,
                "schema": deep.schema,
                "phase": 23,
                "tick_size": 0.1,
                "note": (
                    "Preferred path is raw-contract AITRADE stitch; this fallback uses "
                    "Databento volume-ranked continuous (unadjusted traded prices)."
                ),
            }
        )
        meta_path.write_text(json.dumps(meta, indent=2, default=str), encoding="utf-8")

    out.update(
        {
            "fetched": True,
            "bars_5m": len(deep.bars),
            "path": written["path"],
            "meta_path": str(meta_path),
            "actual_start": deep.actual_start,
            "actual_end": deep.actual_end,
            "volume_semantics": VOLUME_SEMANTICS,
            "volume_status": VOLUME_STATUS,
            "continuous_choice": "databento_native_volume_continuous_GC.v.0",
            "qa": validate_bars_quality(deep.bars),
        }
    )
    return out


def _block_stability(wf_rows: list[dict]) -> str:
    usable = [b for b in wf_rows if (b.get("resolved_n") or 0) >= 8]
    if len(usable) < 2:
        return "INSUFFICIENT_SAMPLE"
    pos = [b for b in usable if (b.get("e2r") or -1) > 0]
    neg = [b for b in usable if (b.get("e2r") or 1) <= 0]
    if len(pos) == len(usable):
        return "STABLE_POSITIVE"
    if len(neg) == len(usable):
        return "STABLE_NEGATIVE"
    if len(pos) == 1 and len(usable) >= 3:
        return "REGIME_SENSITIVE"
    if len(pos) >= 1:
        return "WEAK_POSITIVE"
    return "STABLE_NEGATIVE"


def run_phase23(*, force_fetch: bool = False) -> dict[str, Any]:
    REPORTS.mkdir(parents=True, exist_ok=True)
    JOURNAL_DIR.mkdir(parents=True, exist_ok=True)
    for sub in ("raw", "contracts", "stitched"):
        (DATA_ROOT / sub).mkdir(parents=True, exist_ok=True)

    written_candidates = ensure_frozen_candidate_jsons()
    configs = load_frozen_phase22_candidates()
    freeze_check = assert_g1_frozen(configs)
    if not freeze_check.get("ok"):
        payload = {
            "ok": False,
            "phase": 23,
            "verdict": "DATA_SOURCE_UNSUITABLE",
            "error": "FROZEN_CANDIDATE_MISMATCH",
            "freeze_check": freeze_check,
        }
        VALIDATION_JSON.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return payload

    pf = databento_preflight()
    _write_csv(
        REPORTS / "phase23_databento_provider.csv",
        [
            {
                "provider": "databento",
                "dataset_intended": "GLBX.MDP3",
                "schema_intended": "ohlcv-1m→5m",
                "volume_status": VOLUME_STATUS,
                **{k: pf.get(k) for k in (
                    "databento_package_available",
                    "databento_version",
                    "credential_present",
                    "historical_client_available",
                    "error_code",
                    "ok",
                )},
            }
        ],
    )

    stitched_path = DATA_ROOT / "stitched" / "databento_GC_stitched_5m.jsonl"
    fetch_info: dict[str, Any] = {"skipped": False}
    if force_fetch or not stitched_path.exists():
        fetch_info = attempt_databento_fetch()
    else:
        fetch_info = {"skipped": True, "reused_path": str(stitched_path), "preflight": pf}

    if not pf.get("credential_present"):
        payload = {
            "ok": False,
            "phase": 23,
            "strategy_family": STRATEGY_FAMILY,
            "strategy_version": STRATEGY_VERSION,
            "generated_at": datetime.now(tz=timezone.utc).isoformat(),
            "phase22_preserved_verdict": "DATA_SOURCE_UNSUITABLE",
            "freeze_check": freeze_check,
            "frozen_candidates_written": written_candidates,
            "databento": {
                **pf,
                "dataset_intended": "GLBX.MDP3",
                "publisher_exchange": "CME Globex / COMEX",
                "root_symbol": "GC",
                "schema_intended": "ohlcv-1m aggregated to 5m",
                "symbology_intended": "raw GC contracts via GC.FUT parent; fallback GC.c.0 continuous",
                "volume_semantics": VOLUME_SEMANTICS,
                "volume_status": VOLUME_STATUS,
                "native_timezone": "UTC",
            },
            "fetch": fetch_info,
            "verdict": "DATA_SOURCE_UNSUITABLE",
            "error_code": "DATABENTO_CREDENTIAL_REQUIRED",
            "g1_still_promising": "unknown_pending_databento_data",
            "paper_validation_justified": False,
            "paper_candidate_json": None,
            "limitations": [
                "DATABENTO_API_KEY missing from .env",
                "Cannot download COMEX GC depth without credential",
                "Phase 22 Yahoo ~47-day preliminary G1 result remains unvalidated",
                "No silent Yahoo fallback for canonical Phase 23",
            ],
            "recommended_next_action": (
                "Add DATABENTO_API_KEY to local .env (never commit), then re-run: "
                "python phase23_validate.py --force-fetch"
            ),
            "artifacts": {
                "validation_json": str(VALIDATION_JSON),
                "provider_csv": str(REPORTS / "phase23_databento_provider.csv"),
            },
        }
        VALIDATION_JSON.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
        return payload

    if not stitched_path.exists() and not fetch_info.get("fetched"):
        payload = {
            "ok": False,
            "phase": 23,
            "verdict": "DATA_SOURCE_UNSUITABLE",
            "error_code": fetch_info.get("error_code") or "DATABENTO_FETCH_FAILED",
            "databento": pf,
            "fetch": fetch_info,
            "freeze_check": freeze_check,
            "paper_validation_justified": False,
            "recommended_next_action": (
                "Inspect Databento probe errors; verify GLBX.MDP3 subscription and GC symbology."
            ),
        }
        VALIDATION_JSON.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
        return payload

    loaded = load_dataset("databento_GC_stitched", "5m", root=DATA_ROOT / "stitched")
    bars = loaded.get("bars") or []
    meta_path = DATA_ROOT / "stitched" / "databento_GC_stitched_5m.meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}

    if len(bars) < 1000:
        payload = {
            "ok": False,
            "phase": 23,
            "verdict": "DATA_SOURCE_UNSUITABLE",
            "error_code": "INSUFFICIENT_BARS_AFTER_FETCH",
            "bars": len(bars),
            "fetch": fetch_info,
            "freeze_check": freeze_check,
        }
        VALIDATION_JSON.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
        return payload

    qa = validate_bars_quality(bars)
    _write_csv(REPORTS / "phase23_data_quality.csv", [qa])

    opening_ranges, events, roll_flags = collect_or30_events(bars)
    complete_ors = [o for o in opening_ranges if o.complete]
    dates = trading_dates_in_bars(bars)

    journal_path = JOURNAL_DIR / "setups.jsonl"
    by_cand = replay_all_candidates(bars, candidates=configs)
    all_recs = []
    for recs in by_cand.values():
        all_recs.extend(recs)
    if journal_path.exists():
        journal_path.unlink()
    append_journal_records(all_recs, path=journal_path)
    rows = load_journal_records(path=journal_path)
    train, hold, split = chronological_split(rows)

    train_metrics = [evaluate_rows(train, c) for c in configs]
    hold_metrics = [evaluate_rows(hold, c) for c in configs]
    stability = {
        t["candidate_id"]: classify_stability(t, h) for t, h in zip(train_metrics, hold_metrics)
    }

    g1_id = "G1_OR30_bo_volOFF_dispOFF"
    g1_train = next(m for m in train_metrics if m["candidate_id"] == g1_id)
    g1_hold = next(m for m in hold_metrics if m["candidate_id"] == g1_id)
    g2_train = next(m for m in train_metrics if m["candidate_id"].startswith("G2_"))
    g2_hold = next(m for m in hold_metrics if m["candidate_id"].startswith("G2_"))
    g3_hold = next(m for m in hold_metrics if m["candidate_id"].startswith("G3_"))
    g4_hold = next(m for m in hold_metrics if m["candidate_id"].startswith("G4_"))

    g1_rows = [r for r in rows if (r.get("extras") or {}).get("candidate_id") == g1_id]
    g2_rows = [r for r in rows if (r.get("extras") or {}).get("candidate_id") == "G2_OR30_bo_volON_dispOFF"]
    vol_buckets = bucket_outcomes(g1_rows, lambda ex: rvol_bucket(ex.get("rvol")))
    disp_buckets = bucket_outcomes(g1_rows, lambda ex: body_bucket(ex.get("body_or_ratio")))
    g1_sc = scorecard_from_pairs(
        iter_entry_pairs([r for r in g1_rows if "ROLL_ARTIFACT" not in (r.get("reliability_flags") or [])])
    )
    g2_sc = scorecard_from_pairs(
        iter_entry_pairs([r for r in g2_rows if "ROLL_ARTIFACT" not in (r.get("reliability_flags") or [])])
    )
    lift = {
        "g1_resolved": g1_sc.get("resolved_n"),
        "g2_resolved": g2_sc.get("resolved_n"),
        "opportunity_reduction": None
        if not g1_sc.get("triggered_n")
        else 1 - (g2_sc.get("triggered_n") or 0) / max(g1_sc.get("triggered_n"), 1),
        "stop_rate_delta": None
        if g1_sc.get("stop_rate") is None or g2_sc.get("stop_rate") is None
        else (g2_sc["stop_rate"] - g1_sc["stop_rate"]),
        "r1_delta": None
        if g1_sc.get("r1_rate") is None or g2_sc.get("r1_rate") is None
        else (g2_sc["r1_rate"] - g1_sc["r1_rate"]),
        "r2_delta": None
        if g1_sc.get("r2_rate") is None or g2_sc.get("r2_rate") is None
        else (g2_sc["r2_rate"] - g1_sc["r2_rate"]),
        "r3_delta": None
        if g1_sc.get("r3_rate") is None or g2_sc.get("r3_rate") is None
        else (g2_sc["r3_rate"] - g1_sc["r3_rate"]),
        "e2r_delta": None
        if g1_sc.get("theoretical_2r_expectancy") is None or g2_sc.get("theoretical_2r_expectancy") is None
        else (g2_sc["theoretical_2r_expectancy"] - g1_sc["theoretical_2r_expectancy"]),
        "mfe_delta": None
        if g1_sc.get("median_mfe_r") is None or g2_sc.get("median_mfe_r") is None
        else (g2_sc["median_mfe_r"] - g1_sc["median_mfe_r"]),
        "mae_delta": None
        if g1_sc.get("median_mae_r") is None or g2_sc.get("median_mae_r") is None
        else (g2_sc["median_mae_r"] - g1_sc["median_mae_r"]),
    }
    vol_answer = volume_conclusion(vol_buckets, lift)

    # Retest G5–G8
    retest_ids = [c.candidate_id for c in configs if c.candidate_id.startswith(("G5_", "G6_", "G7_", "G8_"))]
    retest_report = {
        cid: next((m for m in hold_metrics if m["candidate_id"] == cid), {})
        for cid in retest_ids
    }

    # Walk-forward 4 blocks on G1
    dates_all = sorted({str(r.get("trading_date"))[:10] for r in rows if r.get("trading_date")})
    wf: list[dict[str, Any]] = []
    n_blocks = 4
    size = max(1, len(dates_all) // n_blocks) if dates_all else 1
    g1_cfg = next(c for c in configs if c.candidate_id == g1_id)
    cand_rows = [r for r in rows if (r.get("extras") or {}).get("candidate_id") == g1_id]
    for i in range(n_blocks):
        s = i * size
        e = (i + 1) * size if i < n_blocks - 1 else len(dates_all)
        dset = set(dates_all[s:e])
        br = [r for r in cand_rows if str(r.get("trading_date"))[:10] in dset]
        m = evaluate_rows(br, g1_cfg)
        wf.append(
            {
                "candidate_id": g1_id,
                "block": i + 1,
                "date_start": dates_all[s] if dates_all else None,
                "date_end": dates_all[e - 1] if dates_all and e else None,
                "resolved_n": m.get("resolved_n"),
                "stop_rate": m.get("stop_rate"),
                "r1_rate": m.get("r1_rate"),
                "r2_rate": m.get("r2_rate"),
                "r3_rate": m.get("r3_rate"),
                "e1r": m.get("theoretical_1r_expectancy"),
                "e2r": m.get("theoretical_2r_expectancy"),
                "e3r": m.get("theoretical_3r_expectancy"),
                "median_mfe_r": m.get("median_mfe_r"),
                "median_mae_r": m.get("median_mae_r"),
            }
        )
    wf_stability = _block_stability(wf)

    # Cost sensitivity (illustrative)
    cost_sens = []
    e2 = g1_hold.get("theoretical_2r_expectancy")
    rd = g1_hold.get("median_risk_distance") or 5.0
    tick = float(meta.get("tick_size") or GC_TICK_SIZE_DOCUMENTED)
    for ticks in (0, 1, 2):
        friction_r = (2 * ticks * tick) / max(float(rd), tick)
        adj = None if e2 is None else float(e2) - friction_r
        cost_sens.append(
            {
                "candidate_id": g1_id,
                "ticks_per_side": ticks,
                "tick_size": tick,
                "interpretation": f"{ticks} tick(s) per side round-turn ~ {2 * ticks} ticks total",
                "friction_r": friction_r,
                "e2r_raw": e2,
                "e2r_after_friction": adj,
                "survives": None if adj is None else adj > 0,
            }
        )

    # Roll-near sensitivity (±1 trading day)
    rolls_path = DATA_ROOT / "stitched" / "rolls.jsonl"
    roll_dates: set[str] = set()
    if rolls_path.exists():
        for line in rolls_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            rd_obj = json.loads(line)
            roll_dates.add(str(rd_obj.get("decision_date"))[:10])
    near_rows = []
    far_rows = []
    for r in g1_rows:
        td = str(r.get("trading_date") or "")[:10]
        near = any(
            abs(
                (
                    datetime.fromisoformat(td) - datetime.fromisoformat(rd)
                ).days
            )
            <= 1
            for rd in roll_dates
            if td and rd
        ) if roll_dates else False
        (near_rows if near else far_rows).append(r)
    roll_sens = {
        "roll_dates": sorted(roll_dates),
        "all": scorecard_from_pairs(
            iter_entry_pairs([r for r in g1_rows if "ROLL_ARTIFACT" not in (r.get("reliability_flags") or [])])
        ),
        "excluding_roll_near": scorecard_from_pairs(
            iter_entry_pairs([r for r in far_rows if "ROLL_ARTIFACT" not in (r.get("reliability_flags") or [])])
        ),
        "roll_near_only": scorecard_from_pairs(
            iter_entry_pairs([r for r in near_rows if "ROLL_ARTIFACT" not in (r.get("reliability_flags") or [])])
        ),
    }

    overlap = yahoo_overlap_report(bars)
    or_sizes = [o.range_size for o in complete_ors if o.range_size > 0]
    or_size_dist = {
        "n": len(or_sizes),
        "median": median_or_none(or_sizes),
        "p25": None if not or_sizes else sorted(or_sizes)[len(or_sizes) // 4],
        "p75": None if not or_sizes else sorted(or_sizes)[(3 * len(or_sizes)) // 4],
        "p90": None if not or_sizes else sorted(or_sizes)[int(0.9 * (len(or_sizes) - 1))],
        "min": min(or_sizes) if or_sizes else None,
        "max": max(or_sizes) if or_sizes else None,
    }

    days = len(dates)
    # Prefer walk-forward regime flag over naive verdict when one block dominates
    base_verdict = decide_verdict(
        [g1_hold],
        {g1_id: stability.get(g1_id, "INSUFFICIENT_SAMPLE")},
        bar_count=len(bars),
        days=days,
    )
    if wf_stability == "REGIME_SENSITIVE" and base_verdict in ("EDGE_OBSERVED", "WEAK_EDGE_OBSERVED"):
        verdict = "NO_EDGE_OBSERVED"
        stability_note = "downgraded_due_to_REGIME_SENSITIVE_walkforward"
    elif days < 90:
        verdict = "DATA_SOURCE_UNSUITABLE"
        stability_note = None
    elif (g1_hold.get("resolved_n") or 0) < 30:
        verdict = "INSUFFICIENT_SAMPLE"
        stability_note = None
    else:
        verdict = base_verdict
        stability_note = None

    # Cost survival gate for EDGE
    survives_1tick = next((c for c in cost_sens if c["ticks_per_side"] == 1), {})
    if verdict == "EDGE_OBSERVED" and survives_1tick.get("survives") is False:
        verdict = "WEAK_EDGE_OBSERVED"

    paper_path = None
    if verdict in ("EDGE_OBSERVED", "WEAK_EDGE_OBSERVED"):
        paper_path = str(CANDIDATES_DIR / "phase23_gc_paper_candidate.json")
        Path(paper_path).write_text(
            json.dumps(
                {
                    "phase": "phase23",
                    "status": "READY_FOR_PAPER_VALIDATION",
                    "strategy_family": STRATEGY_FAMILY,
                    "candidate": g1_cfg.to_dict(),
                    "config_hash": config_hash(g1_cfg),
                    "verdict": verdict,
                    "note": "NOT production default — paper validation only; no broker connection",
                },
                indent=2,
                default=str,
            ),
            encoding="utf-8",
        )

    _write_csv(REPORTS / "phase23_train.csv", train_metrics)
    _write_csv(REPORTS / "phase23_holdout.csv", hold_metrics)
    _write_csv(REPORTS / "phase23_yahoo_overlap.csv", [overlap])
    _write_csv(REPORTS / "phase23_volume.csv", vol_buckets)
    _write_csv(REPORTS / "phase23_displacement.csv", disp_buckets)
    _write_csv(REPORTS / "phase23_retests.csv", [{"candidate_id": k, **v} for k, v in retest_report.items()])
    _write_csv(REPORTS / "phase23_walkforward.csv", wf)
    _write_csv(REPORTS / "phase23_cost_sensitivity.csv", cost_sens)
    _write_csv(
        REPORTS / "phase23_roll_sensitivity.csv",
        [
            {"scope": "all", **{k: v for k, v in (roll_sens["all"] or {}).items() if k != "label"}},
            {
                "scope": "excluding_roll_near",
                **{k: v for k, v in (roll_sens["excluding_roll_near"] or {}).items() if k != "label"},
            },
            {
                "scope": "roll_near_only",
                **{k: v for k, v in (roll_sens["roll_near_only"] or {}).items() if k != "label"},
            },
        ],
    )
    _write_csv(
        REPORTS / "phase23_funnel.csv",
        [
            {
                "candidate_id": m["candidate_id"],
                **(m.get("funnel") or {}),
                "resolved_n": m.get("resolved_n"),
            }
            for m in train_metrics
        ],
    )
    if fetch_info.get("contracts"):
        _write_csv(REPORTS / "phase23_contracts.csv", fetch_info["contracts"])
    if fetch_info.get("rolls"):
        _write_csv(REPORTS / "phase23_rolls.csv", fetch_info["rolls"])

    te2 = g1_train.get("theoretical_2r_expectancy")
    he2 = g1_hold.get("theoretical_2r_expectancy")
    payload = {
        "ok": True,
        "phase": 23,
        "strategy_family": STRATEGY_FAMILY,
        "strategy_version": STRATEGY_VERSION,
        "generated_at": datetime.now(tz=timezone.utc).isoformat(),
        "freeze_check": freeze_check,
        "databento": {
            **pf,
            "dataset": meta.get("dataset") or "GLBX.MDP3",
            "schema": meta.get("schema") or "ohlcv-5m(agg-from-1m)",
            "volume_semantics": VOLUME_SEMANTICS,
            "volume_status": VOLUME_STATUS,
            "continuous_choice": meta.get("continuous_method") or fetch_info.get("continuous_choice"),
            "bars_5m": len(bars),
        },
        "fetch": {k: v for k, v in fetch_info.items() if k != "preflight"},
        "dataset": {
            "bars_5m": len(bars),
            "trading_days": days,
            "historical_start": meta.get("actual_start") or (int(bars[0].time) if bars else None),
            "historical_end": meta.get("actual_end") or (int(bars[-1].time) if bars else None),
            "qa": qa,
            "split": split,
            "path": str(stitched_path),
            "trading_days_flag": None if days >= 200 else "BELOW_PREFERRED_200_TRADING_DAYS",
        },
        "opening_range": {
            "or30_complete": len(complete_ors),
            "or30_missing": max(0, days - len(complete_ors)),
            "breakouts": len([e for e in events if not e.roll_artifact]),
            "or_size_distribution": or_size_dist,
            "anchor": "08:20 America/New_York",
        },
        "yahoo_overlap": overlap,
        "g1": {
            "train": g1_train,
            "holdout": g1_hold,
            "train_holdout_e2r_delta": None
            if te2 is None or he2 is None
            else float(he2) - float(te2),
        },
        "volume": {
            "threshold": VOLUME_RVOL_THRESHOLD,
            "buckets": vol_buckets,
            "lift_g1_vs_g2": lift,
            "g1_vs_g2_holdout": {
                "g1": g1_hold,
                "g2": g2_hold,
            },
            "conclusion": vol_answer,
        },
        "displacement": {
            "threshold": DISPLACEMENT_BODY_OR_RATIO,
            "buckets": disp_buckets,
            "g1_vs_g3_holdout": {"g1": g1_hold, "g3": g3_hold},
            "g4_combined_holdout": g4_hold,
            "conclusion": (
                "INSUFFICIENT_SAMPLE"
                if (g3_hold.get("resolved_n") or 0) < 15
                else (
                    "YES_PRELIMINARY"
                    if (g3_hold.get("theoretical_2r_expectancy") or -1)
                    > (g1_hold.get("theoretical_2r_expectancy") or 0) + 0.05
                    else "NO"
                    if (g3_hold.get("theoretical_2r_expectancy") or 0)
                    < (g1_hold.get("theoretical_2r_expectancy") or 0) - 0.05
                    else "MIXED"
                )
            ),
        },
        "retest": retest_report,
        "walkforward": wf,
        "stability_classification": wf_stability,
        "train_holdout_stability": stability,
        "stability_note": stability_note,
        "roll_sensitivity": roll_sens,
        "cost_sensitivity": cost_sens,
        "train_metrics": train_metrics,
        "holdout_metrics": hold_metrics,
        "verdict": verdict,
        "g1_still_promising": verdict in ("EDGE_OBSERVED", "WEAK_EDGE_OBSERVED"),
        "paper_validation_justified": verdict in ("EDGE_OBSERVED", "WEAK_EDGE_OBSERVED"),
        "paper_candidate_json": paper_path,
        "roll_gap_flags": len(roll_flags),
        "recommended_next_action": (
            "READY_FOR_PAPER_VALIDATION"
            if verdict in ("EDGE_OBSERVED", "WEAK_EDGE_OBSERVED")
            else "Retire gc_orb_volume_v1 / G1 OR30 breakout hypothesis; do not optimize thresholds to rescue HOLDOUT"
            if verdict == "NO_EDGE_OBSERVED"
            else "Add DATABENTO_API_KEY and re-run with --force-fetch"
            if verdict == "DATA_SOURCE_UNSUITABLE"
            else "Extend Databento history depth and re-run"
        ),
        "limitations": [
            "Phase 23 requires authentic Databento GC depth; Yahoo not used as canonical",
            "1m ambiguity resolver for retests not auto-fetched (optional evidence layer)",
            f"Tick size used for friction illustrative={tick} (verify via instrument definition when available)",
        ],
    }
    VALIDATION_JSON.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    return payload


def main() -> None:
    import sys

    force = "--force-fetch" in sys.argv
    p = run_phase23(force_fetch=force)
    print(
        json.dumps(
            {
                "ok": p.get("ok"),
                "verdict": p.get("verdict"),
                "error_code": p.get("error_code"),
                "credential_present": (p.get("databento") or {}).get("credential_present"),
                "freeze_ok": (p.get("freeze_check") or {}).get("ok"),
                "g1_still_promising": p.get("g1_still_promising"),
                "paper": p.get("paper_validation_justified"),
                "recommended_next_action": p.get("recommended_next_action"),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
