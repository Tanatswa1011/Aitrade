"""Phase 34 validation — PDH/PDL sweep baseline + optional windowed MBP-10.

Default path uses local NQM6 1m OHLCV only (no extra download).
Pass --download-mbp to fetch MBP-10+trades around sweeps after printing cost.
"""

from __future__ import annotations

import csv
import hashlib
import json
import math
import statistics
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

from bar_dataset import load_dataset
from nq_microstructure_features import (
    FEATURE_CUTOFF_OFFSET_SEC,
    FEATURE_LOOKBACK_SEC,
    features_from_records,
    median_split,
    merge_spans,
    neighboring_threshold_stable,
    quartile_rows,
    spearman_rho,
)
from nq_microstructure_models import (
    AUX_HORIZONS_SEC,
    CONTINUATION_EXT_POINTS,
    FROZEN_GC_HASH,
    FROZEN_NQ_HASH,
    PRIMARY_HORIZON_SEC,
    REVERSAL_TARGET_POINTS,
    SweepEvent,
)
from nq_pdh_pdl import detect_pdh_pdl_sweeps, label_outcome_1m, local_ts

ROOT = Path(__file__).resolve().parent
NY = ZoneInfo("America/New_York")
REPORTS = ROOT / "reports"
JOURNAL = ROOT / "journal" / "phase34_nq_microstructure"
VALIDATION = ROOT / "phase34_validation.json"
CONTRACT_ROOT = ROOT / "data" / "databento" / "NQ" / "contracts"

GC_FROZEN = ROOT / "strategy_frozen" / "gc_vwap_v2_phase26.json"
NQ_FROZEN = ROOT / "strategy_frozen" / "nq_dvp_phase30.json"
GC_FILE_SHA = "12615d19ef3deed36b7929c161ef7c377975cd2317c0ae6d06f1949bd327046f"
NQ_FILE_SHA = "34927fba0f268e56cf166cf8939bdfec6aa031510f7cee9acfab3909dd36b541"
GC_PAPER = ROOT / "journal" / "phase26_gc_vwap_v2_paper" / "paper_trades.jsonl"
NQ_PAPER = ROOT / "journal" / "phase30_nq_dvp_paper" / "paper_trades.jsonl"

# NQM6 front-month window (roll NQH6→NQM6 2026-03-16; NQM6→NQU6 2026-06-14)
PILOT_START = date(2026, 5, 13)
PILOT_END = date(2026, 6, 12)
NEWS_BLACKOUT_DATES = {"2026-05-08", "2026-06-05", "2026-06-10"}  # NFP / CPI
# Memorial Day 2026-05-25 — no RTH
HOLIDAYS = {"2026-05-25"}
SYMBOL = "NQM6"
WINDOW_BEFORE_SEC = 60
WINDOW_AFTER_SEC = 120


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def assert_frozen() -> dict[str, Any]:
    reasons = []
    gc = json.loads(GC_FROZEN.read_text(encoding="utf-8"))
    nq = json.loads(NQ_FROZEN.read_text(encoding="utf-8"))
    if gc.get("frozen_config_hash") != FROZEN_GC_HASH:
        reasons.append("gc_hash")
    if nq.get("frozen_config_hash") != FROZEN_NQ_HASH:
        reasons.append("nq_hash")
    if file_sha256(GC_FROZEN) != GC_FILE_SHA:
        reasons.append("gc_bytes")
    if file_sha256(NQ_FROZEN) != NQ_FILE_SHA:
        reasons.append("nq_bytes")
    if file_sha256(GC_PAPER) != "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855":
        reasons.append("gc_paper")
    if file_sha256(NQ_PAPER) != "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855":
        reasons.append("nq_paper")
    return {"ok": not reasons, "reasons": reasons, "gc": gc.get("frozen_config_hash"), "nq": nq.get("frozen_config_hash")}


def pilot_dates() -> list[str]:
    out = []
    d = PILOT_START
    while d <= PILOT_END:
        iso = d.isoformat()
        if d.weekday() < 5 and iso not in HOLIDAYS and iso not in NEWS_BLACKOUT_DATES:
            out.append(iso)
        d += timedelta(days=1)
    return out


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    keys = sorted({k for r in rows for k in r.keys()})
    with path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=keys, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: json.dumps(v, default=str) if isinstance(v, (dict, list)) else v for k, v in r.items()})


def rate(n: int, d: int) -> Optional[float]:
    return None if d <= 0 else n / d


def baseline_table(events: list[SweepEvent], outcomes: dict[tuple[str, int], Any]) -> list[dict[str, Any]]:
    rows = []
    for h in AUX_HORIZONS_SEC:
        for side in ("pdl_sweep", "pdh_sweep", "ALL"):
            sub = [e for e in events if side == "ALL" or e.side == side]
            labels = [outcomes[(e.event_id, h)].label for e in sub]
            n = len(labels)
            rows.append(
                {
                    "horizon_sec": h,
                    "side": side,
                    "n": n,
                    "reversal": labels.count("REVERSAL"),
                    "continuation": labels.count("CONTINUATION"),
                    "neither": labels.count("NEITHER"),
                    "ambiguous": labels.count("AMBIGUOUS"),
                    "p_reversal": rate(labels.count("REVERSAL"), n),
                    "p_continuation": rate(labels.count("CONTINUATION"), n),
                    "p_reversal_among_decided": rate(
                        labels.count("REVERSAL"),
                        labels.count("REVERSAL") + labels.count("CONTINUATION"),
                    ),
                }
            )
    return rows


def load_nqm6() -> list:
    loaded = load_dataset("databento_NQ_NQM6", "1m", root=CONTRACT_ROOT)
    if not loaded.get("ok"):
        raise RuntimeError(loaded.get("error") or "missing NQM6 1m")
    return list(loaded["bars"])


def event_span(event: SweepEvent) -> tuple[int, int]:
    a = int(event.sweep_bar_time) - WINDOW_BEFORE_SEC
    b = int(event.sweep_bar_time) + WINDOW_AFTER_SEC
    return a, b


def cost_windows(events: list[SweepEvent], schema: str) -> dict[str, Any]:
    from databento_history import load_databento_credential

    load_databento_credential()
    import os
    import databento as db

    if not os.environ.get("DATABENTO_API_KEY"):
        return {"ok": False, "error": "no_key"}
    client = db.Historical()
    by_day: dict[str, list[tuple[int, int]]] = {}
    for e in events:
        by_day.setdefault(e.trading_date, []).append(event_span(e))
    total = 0.0
    recs = 0.0
    parts = []
    for td, spans in sorted(by_day.items()):
        for lo, hi in merge_spans(spans):
            start = datetime.fromtimestamp(lo, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
            end = datetime.fromtimestamp(hi, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
            try:
                c = float(
                    client.metadata.get_cost(
                        dataset="GLBX.MDP3",
                        symbols=SYMBOL,
                        schema=schema,
                        start=start,
                        end=end,
                        stype_in="raw_symbol",
                    )
                )
                n = float(
                    client.metadata.get_record_count(
                        dataset="GLBX.MDP3",
                        symbols=SYMBOL,
                        schema=schema,
                        start=start,
                        end=end,
                        stype_in="raw_symbol",
                    )
                )
            except Exception as exc:  # noqa: BLE001
                parts.append({"date": td, "start": start, "end": end, "error": f"{type(exc).__name__}:{exc}"})
                continue
            total += c
            recs += n
            parts.append(
                {
                    "date": td,
                    "start": start,
                    "end": end,
                    "start_unix": lo,
                    "end_unix": hi,
                    "cost_usd": c,
                    "records": n,
                }
            )
    return {"ok": True, "schema": schema, "total_cost_usd": total, "total_records": recs, "windows": parts}


def download_mbp_and_trades(costed: dict[str, Any], schema: str) -> dict[str, Any]:
    import databento as db

    dest = ROOT / "data" / "databento" / "NQ" / "microstructure" / schema
    dest.mkdir(parents=True, exist_ok=True)
    client = db.Historical()
    saved = []
    for w in costed.get("windows") or []:
        if "error" in w:
            continue
        path = dest / f"{SYMBOL}_{schema}_{w['date']}_{int(w.get('start_unix') or 0)}.dbn.zst"
        if path.exists() and path.stat().st_size > 1000:
            saved.append(
                {
                    "date": w["date"],
                    "path": str(path),
                    "cached": True,
                    "bytes": path.stat().st_size,
                    "start_unix": w.get("start_unix"),
                    "end_unix": w.get("end_unix"),
                }
            )
            continue
        data = client.timeseries.get_range(
            dataset="GLBX.MDP3",
            symbols=SYMBOL,
            schema=schema,
            start=w["start"],
            end=w["end"],
            stype_in="raw_symbol",
        )
        data.to_file(path)
        saved.append(
            {
                "date": w["date"],
                "path": str(path),
                "cached": False,
                "bytes": path.stat().st_size,
                "start_unix": w.get("start_unix"),
                "end_unix": w.get("end_unix"),
            }
        )
    return {"ok": True, "files": saved, "dir": str(dest)}


def covering_file(files: list[dict[str, Any]], event: SweepEvent) -> Optional[Path]:
    a, b = event_span(event)
    t_cut = int(event.sweep_bar_time) + FEATURE_CUTOFF_OFFSET_SEC
    best = None
    for f in files:
        p = Path(f["path"])
        if not p.exists():
            continue
        lo = int(f.get("start_unix") or 0)
        hi = int(f.get("end_unix") or 0)
        if lo and hi and lo <= a and hi >= t_cut:
            return p
        if f.get("date") == event.trading_date:
            best = p
    return best


def mbp10_features_at_sweep(dbn_path: Path, event: SweepEvent) -> dict[str, Any]:
    """Features from MBP-10 with cutoff at sweep-bar close. No future book/trade updates."""
    import databento as db

    store = db.DBNStore.from_file(dbn_path)
    return features_from_records(store, event)


def dvp_relationship(events: list[SweepEvent]) -> dict[str, Any]:
    dvp_path = ROOT / "journal" / "phase29_nq_drift_vwap" / "trades.jsonl"
    trades = []
    if dvp_path.exists():
        for line in dvp_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                trades.append(json.loads(line))
    sweep_days = {e.trading_date for e in events}
    dvp_in_window = [t for t in trades if t.get("trading_date") in sweep_days]
    dvp_days = {t.get("trading_date") for t in dvp_in_window}
    same_hour = 0
    oppose = 0
    agree = 0
    flags = []
    for e in events:
        day_trades = [t for t in dvp_in_window if t.get("trading_date") == e.trading_date]
        hour_hits = [
            t
            for t in day_trades
            if abs(int(t.get("entry_timestamp") or 0) - int(e.sweep_ts)) <= 3600
        ]
        if hour_hits:
            same_hour += 1
        drifts = {(t.get("extras") or {}).get("drift") for t in day_trades}
        # Reversal-long after PDL opposes DVP negative drift; reversal-short after PDH opposes positive drift.
        if e.side == "pdl_sweep" and "NEGATIVE_DRIFT" in drifts:
            oppose += 1
            flags.append({"event_id": e.event_id, "flag": "PDL_SWEEP+DVP_NEGATIVE_DRIFT"})
        elif e.side == "pdh_sweep" and "POSITIVE_DRIFT" in drifts:
            oppose += 1
            flags.append({"event_id": e.event_id, "flag": "PDH_SWEEP+DVP_POSITIVE_DRIFT"})
        elif e.side == "pdl_sweep" and "POSITIVE_DRIFT" in drifts:
            agree += 1
        elif e.side == "pdh_sweep" and "NEGATIVE_DRIFT" in drifts:
            agree += 1
    dvp_pnl_days = {}
    for t in dvp_in_window:
        dvp_pnl_days[t["trading_date"]] = dvp_pnl_days.get(t["trading_date"], 0.0) + float(t.get("points") or 0)
    return {
        "dvp_same_day_overlap_n": len(sweep_days & dvp_days),
        "dvp_overlap_days": sorted(sweep_days & dvp_days),
        "n_sweeps_with_dvp_same_hour": same_hour,
        "n_sweeps_opposing_dvp_drift": oppose,
        "n_sweeps_agreeing_dvp_drift": agree,
        "dvp_mean_points_on_sweep_days": None
        if not dvp_pnl_days
        else statistics.mean(dvp_pnl_days.values()),
        "ensemble_flags_n": len(flags),
        "ensemble_flags_head": flags[:20],
        "note": "No microstructure P&L exists yet; overlap is event-level only.",
    }


def gc_relationship(events: list[SweepEvent]) -> dict[str, Any]:
    # Frozen GC V2 paper journal is empty by design; Phase 25 research journal may exist.
    p25 = ROOT / "journal" / "phase25_gc_vwap" / "trades.jsonl"
    if not p25.exists():
        p25 = ROOT / "journal" / "phase26_gc_vwap_v2_paper" / "paper_trades.jsonl"
    gc_days: set[str] = set()
    if p25.exists() and p25.stat().st_size > 0:
        for line in p25.read_text(encoding="utf-8").splitlines():
            if line.strip():
                row = json.loads(line)
                gc_days.add(str(row.get("trading_date") or row.get("date") or ""))
    sweep_days = {e.trading_date for e in events}
    overlap = sorted(d for d in (sweep_days & gc_days) if d)
    return {
        "gc_paper_empty": file_sha256(GC_PAPER) == "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        "gc_active_day_overlap_n": len(overlap),
        "gc_overlap_days": overlap,
        "note": "GC is a different market. No microstructure entries, so P&L correlation is not defined.",
    }


def decile_lift(pairs: list[tuple[float, bool]]) -> list[dict[str, Any]]:
    if len(pairs) < 10:
        return []
    pairs = sorted(pairs, key=lambda x: x[0])
    n = len(pairs)
    rows = []
    for d in range(10):
        a = int(d * n / 10)
        b = int((d + 1) * n / 10)
        chunk = pairs[a:b]
        if not chunk:
            continue
        ys = [int(y) for _, y in chunk]
        rows.append(
            {
                "decile": d + 1,
                "n": len(chunk),
                "mean_feature": statistics.mean(x for x, _ in chunk),
                "p_reversal": sum(ys) / len(ys),
            }
        )
    return rows


def main() -> dict[str, Any]:
    frozen = assert_frozen()
    if not frozen["ok"]:
        payload = {"ok": False, "status": "FROZEN_INTEGRITY_FAILED", "frozen": frozen}
        VALIDATION.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return payload

    dates = pilot_dates()
    bars = load_nqm6()
    events = detect_pdh_pdl_sweeps(bars, dates, skip_source_dates=HOLIDAYS)
    outcomes: dict[tuple[str, int], Any] = {}
    for e in events:
        for h in AUX_HORIZONS_SEC:
            outcomes[(e.event_id, h)] = label_outcome_1m(e, bars, horizon_sec=h)

    JOURNAL.mkdir(parents=True, exist_ok=True)
    with (JOURNAL / "sweeps.jsonl").open("w", encoding="utf-8") as fh:
        for e in events:
            row = e.to_dict()
            row["outcomes"] = {str(h): outcomes[(e.event_id, h)].to_dict() for h in AUX_HORIZONS_SEC}
            fh.write(json.dumps(row) + "\n")

    base = baseline_table(events, outcomes)
    _write_csv(REPORTS / "phase34_sweep_baseline.csv", base)
    detail = []
    for e in events:
        o = outcomes[(e.event_id, PRIMARY_HORIZON_SEC)]
        detail.append(
            {
                **e.to_dict(),
                "label_300s": o.label,
                "mfe": o.mfe_points,
                "mae": o.mae_points,
                "further_ext": o.further_extension_points,
                "seconds_to_reclaim": o.seconds_to_reclaim,
            }
        )
    _write_csv(REPORTS / "phase34_sweep_events.csv", detail)

    # Univariate 1m structure features vs 300s reversal (no book yet)
    y = [outcomes[(e.event_id, PRIMARY_HORIZON_SEC)].label == "REVERSAL" for e in events]
    struct_rows = []
    for name, getter in (
        ("penetration_points", lambda e: e.penetration_points),
        ("seconds_from_rth_open", lambda e: float(e.seconds_from_rth_open)),
        ("volume_sweep_bar", lambda e: e.volume_sweep_bar),
        ("atr_1m_14", lambda e: e.atr_1m_14),
        ("penetration_over_atr", lambda e: None if not e.atr_1m_14 else e.penetration_points / e.atr_1m_14),
    ):
        pairs = []
        for e, yi in zip(events, y):
            v = getter(e)
            if v is None:
                continue
            pairs.append((float(v), bool(yi)))
        lifts = decile_lift(pairs)
        for r in lifts:
            struct_rows.append({"feature": name, **r})
        split = median_split(pairs)
        struct_rows.append({"feature": name, "decile": "median_split", **split})
        rho = spearman_rho([(x, 1.0 if y else 0.0) for x, y in pairs])
        struct_rows.append({"feature": name, "decile": "spearman", "rho": rho, "n": len(pairs)})
    _write_csv(REPORTS / "phase34_structure_deciles.csv", struct_rows)

    do_dl = "--download-mbp" in sys.argv
    cost_mbp = cost_trd = None
    dl_mbp = dl_trd = None
    micro_rows: list[dict[str, Any]] = []
    feature_lifts: list[dict[str, Any]] = []
    feature_splits: list[dict[str, Any]] = []
    stable_features: list[str] = []
    if do_dl and events:
        cost_mbp = cost_windows(events, "mbp-10")
        cost_trd = cost_windows(events, "trades")
        (REPORTS / "phase34_window_cost.json").write_text(
            json.dumps({"mbp-10": cost_mbp, "trades": cost_trd}, indent=2), encoding="utf-8"
        )
        mbp_cost = float(cost_mbp.get("total_cost_usd") or 0)
        trd_cost = float(cost_trd.get("total_cost_usd") or 0)
        total = mbp_cost + trd_cost
        if mbp_cost > 25:
            dl_mbp = {"ok": False, "skipped": True, "reason": f"mbp10_cost_{mbp_cost:.2f}_exceeds_25_usd_cap"}
        else:
            dl_mbp = download_mbp_and_trades(cost_mbp, "mbp-10")
            if total <= 25:
                dl_trd = download_mbp_and_trades(cost_trd, "trades")
            else:
                dl_trd = {"ok": False, "skipped": True, "reason": f"trades_skipped_combined_{total:.2f}"}
            mbp_files = list(dl_mbp.get("files") or [])
            trd_files = list((dl_trd or {}).get("files") or [])
            for e in events:
                mbp_path = covering_file(mbp_files, e)
                trd_path = covering_file(trd_files, e) if trd_files else None
                feats = {"has_book": False}
                try:
                    recs = []
                    if mbp_path is not None:
                        import databento as db

                        recs.extend(list(db.DBNStore.from_file(mbp_path)))
                        if trd_path is not None and trd_path != mbp_path:
                            recs.extend(list(db.DBNStore.from_file(trd_path)))
                        feats = features_from_records(recs, e)
                except Exception as exc:  # noqa: BLE001
                    feats = {"has_book": False, "error": f"{type(exc).__name__}:{exc}"}
                lab = outcomes[(e.event_id, PRIMARY_HORIZON_SEC)].label
                micro_rows.append(
                    {
                        "event_id": e.event_id,
                        "side": e.side,
                        "label_300s": lab,
                        **feats,
                    }
                )
            _write_csv(REPORTS / "phase34_micro_features.csv", micro_rows)
            rev = [r["label_300s"] == "REVERSAL" for r in micro_rows]
            for feat in (
                "signed_flow_for_reversal",
                "absorption_proxy",
                "imb_for_reversal_top1",
                "book_imbalance_top10",
                "ofi_for_reversal",
                "executed_to_displayed",
                "price_impact_per_lot",
                "bid_ask_slope_ratio",
            ):
                pairs = []
                for r, yi in zip(micro_rows, rev):
                    v = r.get(feat)
                    if v is None or (isinstance(v, float) and (math.isnan(v) or math.isinf(v))):
                        continue
                    pairs.append((float(v), bool(yi)))
                split = median_split(pairs)
                stab = neighboring_threshold_stable(pairs, 0.08)
                rho = spearman_rho([(x, 1.0 if y else 0.0) for x, y in pairs])
                feature_splits.append({"feature": feat, "spearman": rho, "stability": stab, **split})
                if stab.get("stable"):
                    stable_features.append(feat)
                for row in quartile_rows(pairs):
                    feature_lifts.append({"feature": feat, **row})
                for row in decile_lift(pairs):
                    feature_lifts.append({"feature": feat, "kind": "decile", **row})
            _write_csv(REPORTS / "phase34_micro_deciles.csv", feature_lifts)
            _write_csv(REPORTS / "phase34_micro_median_splits.csv", feature_splits)

    primary = [r for r in base if r["horizon_sec"] == PRIMARY_HORIZON_SEC]
    p_all = next((r for r in primary if r["side"] == "ALL"), {})
    # Chrono 70/30 on 300s labels
    ev_sorted = sorted(events, key=lambda e: e.trading_date)
    cut = max(1, int(round(len(set(e.trading_date for e in ev_sorted)) * 0.7)))
    dates_sorted = sorted({e.trading_date for e in ev_sorted})
    train_d, hold_d = set(dates_sorted[:cut]), set(dates_sorted[cut:])
    def _pr(ds):
        sub = [e for e in events if e.trading_date in ds]
        labs = [outcomes[(e.event_id, PRIMARY_HORIZON_SEC)].label for e in sub]
        return {"n": len(labs), "p_reversal": rate(labs.count("REVERSAL"), len(labs))}

    # News blackout: all pilot dates already exclude CPI/NFP days; report count removed from a wider set
    wide = detect_pdh_pdl_sweeps(
        bars,
        [
            (PILOT_START + timedelta(days=i)).isoformat()
            for i in range((PILOT_END - PILOT_START).days + 1)
            if (PILOT_START + timedelta(days=i)).weekday() < 5
            and (PILOT_START + timedelta(days=i)).isoformat() not in HOLIDAYS
        ],
        skip_source_dates=HOLIDAYS,
    )
    removed = [e for e in wide if e.trading_date in NEWS_BLACKOUT_DATES]

    dvp = dvp_relationship(events)
    gc = gc_relationship(events)
    (JOURNAL / "ensemble_flags.jsonl").write_text(
        "".join(json.dumps(f) + "\n" for f in dvp.get("ensemble_flags_head") or []),
        encoding="utf-8",
    )

    n_book = sum(1 for r in micro_rows if r.get("has_book"))
    micro_improves = bool(stable_features)
    n = int(p_all.get("n") or 0)
    if do_dl and (dl_mbp or {}).get("skipped") and float((cost_mbp or {}).get("total_cost_usd") or 0) > 100:
        verdict = "MICROSTRUCTURE_DATA_NOT_FEASIBLE"
    elif not do_dl or n_book == 0:
        # OHLCV cannot answer the order-book question.
        verdict = "MICROSTRUCTURE_PROMISING_NEEDS_MORE_DATA" if not do_dl else "MICROSTRUCTURE_DATA_NOT_FEASIBLE"
    elif n < 40:
        verdict = (
            "MICROSTRUCTURE_PROMISING_NEEDS_MORE_DATA"
            if micro_improves
            else "MICROSTRUCTURE_PROMISING_NEEDS_MORE_DATA"
        )
    elif micro_improves:
        verdict = "MICROSTRUCTURE_EDGE_FOUND"
    else:
        verdict = "MICROSTRUCTURE_EDGE_WEAK"

    frozen_after = assert_frozen()
    payload = {
        "ok": frozen["ok"] and frozen_after["ok"],
        "phase": 34,
        "status": "RESEARCH_COMPLETE",
        "verdict": verdict,
        "execution": "DRY_RUN_NO_BROKER",
        "frozen_before": frozen,
        "frozen_after": frozen_after,
        "pilot": {
            "contract": SYMBOL,
            "start": PILOT_START.isoformat(),
            "end": PILOT_END.isoformat(),
            "trading_days": dates,
            "n_days": len(dates),
            "excluded_news_dates": sorted(NEWS_BLACKOUT_DATES),
            "holidays_skipped_as_pdh_pdl_source": sorted(HOLIDAYS),
            "roll": "NQM6 is front-month (volume-crossover 2026-03-16 until 2026-06-14). No month mix.",
            "pdh_pdl": "Prior RTH 09:30-16:00 America/New_York high/low from completed 1m bars. Holidays skipped as source.",
            "schema_plan": "Windowed MBP-10 (self-contained 10-level snapshots). Full-day MBO not downloaded: mid-session slices lack the UTC-midnight snapshot.",
            "feature_window": {
                "lookback_sec": FEATURE_LOOKBACK_SEC,
                "cutoff": "sweep_bar_close = sweep_bar_time + 60s",
                "download_after_sec": WINDOW_AFTER_SEC,
                "note": "Predictive features ignore records after bar close.",
            },
            "outcome": {
                "horizons_sec": list(AUX_HORIZONS_SEC),
                "primary_horizon_sec": PRIMARY_HORIZON_SEC,
                "reversal_target_points": REVERSAL_TARGET_POINTS,
                "continuation_extension_points": CONTINUATION_EXT_POINTS,
                "subminute_note": "30s/60s labels on 1m OHLC are empty by construction (path starts at bar close).",
            },
        },
        "n_sweeps": len(events),
        "baseline_primary_300s": p_all,
        "baseline_all_horizons": base,
        "chrono_split": {
            "train": _pr(train_d),
            "holdout": _pr(hold_d),
            "train_dates": len(train_d),
            "holdout_dates": len(hold_d),
        },
        "news_blackout_removed_sweeps": len(removed),
        "dvp": dvp,
        "gc": gc,
        "window_cost_mbp10": {
            k: v for k, v in (cost_mbp or {}).items() if k != "windows"
        }
        if cost_mbp
        else None,
        "window_cost_trades": {k: v for k, v in (cost_trd or {}).items() if k != "windows"} if cost_trd else None,
        "download_mbp": {k: v for k, v in (dl_mbp or {}).items() if k != "files"} if dl_mbp else None,
        "n_events_with_book": n_book,
        "microstructure_median_splits": feature_splits,
        "microstructure_stable_features": stable_features,
        "microstructure_improves_baseline": micro_improves,
        "cost_stress_note": (
            "No entries tested. A reclaim-after-confirmation market order is the only realistic first fill model. "
            "Passive fills at the sweep extreme are rejected as unmodelable. NQ tick=0.25 pts ($5); 1-tick adverse=$5; "
            "2-tick=$10; round-turn CME/clearing typically a few dollars on NQ / under $1 on MNQ."
        ),
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    VALIDATION.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    return payload


if __name__ == "__main__":
    out = main()
    print(json.dumps({
        "ok": out.get("ok"),
        "verdict": out.get("verdict"),
        "n_sweeps": out.get("n_sweeps"),
        "baseline": out.get("baseline_primary_300s"),
        "frozen": (out.get("frozen_after") or {}).get("ok"),
        "cost_mbp": (out.get("window_cost_mbp10") or {}).get("total_cost_usd")
        if isinstance(out.get("window_cost_mbp10"), dict)
        else None,
        "n_book": out.get("n_events_with_book"),
        "stable": out.get("microstructure_stable_features"),
        "dvp_overlap": (out.get("dvp") or {}).get("dvp_same_day_overlap_n"),
    }, indent=2, default=str))
