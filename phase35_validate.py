"""Phase 35 — larger-sample PDH/PDL sweep replication + incremental MBP-10 test.

Definitions are frozen in phase35_spec.json (Phase 34 carried forward).
Default: reproduce Phase 34, detect multi-contract sweeps, structural analysis.
Pass --download-mbp to cost then fetch windowed MBP-10+trades if under $50.
"""

from __future__ import annotations

import csv
import json
import math
import statistics
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

from bar_dataset import load_dataset
from nq_front_month import contract_on_rth_date, dates_by_contract, load_rolls
from nq_microstructure_features import (
    FEATURE_CUTOFF_OFFSET_SEC,
    features_from_records,
    median_split,
    merge_spans,
    neighboring_threshold_stable,
    quantile_rows,
    spearman_rho,
    wilson_ci,
)
from nq_microstructure_models import (
    FROZEN_GC_HASH,
    FROZEN_NQ_HASH,
    PRIMARY_HORIZON_SEC,
    SweepEvent,
)
from nq_pdh_pdl import detect_pdh_pdl_sweeps, label_outcome_1m, local_ts, rth_bars
from phase34_validate import (
    HOLIDAYS as P34_HOLIDAYS,
    NEWS_BLACKOUT_DATES as P34_NEWS,
    PILOT_END,
    PILOT_START,
    assert_frozen,
    load_nqm6,
    pilot_dates,
)

ROOT = Path(__file__).resolve().parent
NY = ZoneInfo("America/New_York")
REPORTS = ROOT / "reports"
JOURNAL = ROOT / "journal" / "phase35_nq_structural_sweep"
VALIDATION = ROOT / "phase35_validation.json"
CONTRACT_ROOT = ROOT / "data" / "databento" / "NQ" / "contracts"
SPEC_PATH = ROOT / "phase35_spec.json"
CANDIDATE = ROOT / "strategy_candidates" / "phase35_NQ_STRUCTURAL_SWEEP.json"

WINDOW_BEFORE_SEC = 60
WINDOW_AFTER_SEC = 120
COST_CAP_USD = 50.0
# NQU5 front after 2025-06-16 roll through NQU6 (still front as of 2026-08-14)
EXPAND_START = date(2025, 6, 17)
EXPAND_END = date(2026, 8, 14)

US_RTH_HOLIDAYS = {
    "2022-01-17", "2022-02-21", "2022-04-15", "2022-05-30", "2022-06-20",
    "2022-07-04", "2022-09-05", "2022-11-24", "2022-12-26",
    "2023-01-02", "2023-01-16", "2023-02-20", "2023-04-07", "2023-05-29",
    "2023-06-19", "2023-07-04", "2023-09-04", "2023-11-23", "2023-12-25",
    "2024-01-01", "2024-01-15", "2024-02-19", "2024-03-29", "2024-05-27",
    "2024-06-19", "2024-07-04", "2024-09-02", "2024-11-28", "2024-12-25",
    "2025-01-01", "2025-01-20", "2025-02-17", "2025-04-18", "2025-05-26",
    "2025-06-19", "2025-07-04", "2025-09-01", "2025-11-27", "2025-12-25",
    "2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03", "2026-05-25",
    "2026-06-19", "2026-07-03",
}

MBP_FEATS = (
    "imb_for_reversal_top1",
    "imb_for_reversal_top3",
    "imb_for_reversal_top5",
    "imb_for_reversal_top10",
    "signed_flow_for_reversal",
    "ofi_for_reversal",
    "absorption_proxy",
    "executed_to_displayed",
    "slope_for_reversal",
    "persistence_top1_swept_side",
    "withdrawal_proxy",
    "price_impact_per_lot",
)


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


def news_dates() -> set[str]:
    path = ROOT / "data" / "macro" / "bls_events.jsonl"
    out = set()
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                out.add(json.loads(line)["publication_date"])
    return out


def reproduce_phase34() -> dict[str, Any]:
    bars = load_nqm6()
    dates = pilot_dates()
    events = detect_pdh_pdl_sweeps(bars, dates, skip_source_dates=P34_HOLIDAYS)
    outcomes = [label_outcome_1m(e, bars, horizon_sec=PRIMARY_HORIZON_SEC) for e in events]
    n_rev = sum(1 for o in outcomes if o.label == "REVERSAL")
    n = len(outcomes)
    p = n_rev / n if n else None
    expected_n = 20
    expected_p = 0.45
    ok = n == expected_n and p is not None and abs(p - expected_p) < 1e-9
    # Allow tiny float noise
    if n == expected_n and p is not None and abs(p - expected_p) < 1e-6:
        ok = True
    return {
        "ok": ok,
        "n_sweeps": n,
        "expected_n": expected_n,
        "p_reversal": p,
        "expected_p_reversal": expected_p,
        "n_pdh": sum(1 for e in events if e.side == "pdh_sweep"),
        "n_pdl": sum(1 for e in events if e.side == "pdl_sweep"),
        "discrepancy": None if ok else "Phase 34 N/P(rev) did not match 20 / 0.45",
    }


def load_contract_bars(symbol: str) -> list:
    loaded = load_dataset(f"databento_NQ_{symbol}", "1m", root=CONTRACT_ROOT)
    if not loaded.get("ok"):
        raise RuntimeError(loaded.get("error") or f"missing {symbol} 1m")
    return list(loaded["bars"])


def session_bucket(seconds_from_open: int) -> str:
    if seconds_from_open < 1800:
        return "open_30m"
    if seconds_from_open < 3600:
        return "open_60m"
    if seconds_from_open < 18000:
        return "midday"
    return "afternoon"


def enrich(event: SweepEvent, bars: list) -> dict[str, Any]:
    td = event.trading_date
    rth = rth_bars(bars, td)
    closed_before = [b for b in rth if int(b.time) < int(event.sweep_bar_time)]
    ranges = [float(b.high) - float(b.low) for b in closed_before[-14:]]
    med_rng = statistics.median(ranges) if ranges else None
    atr = event.atr_1m_14
    pen = float(event.penetration_points)
    open_px = float(rth[0].open) if rth else None
    t30 = int(event.rth_open_ts) + 1800
    bar30 = next((b for b in rth if int(b.time) == t30), None)
    ret30 = None
    if bar30 is not None and open_px is not None and int(event.sweep_bar_time) >= t30 + 60:
        ret30 = float(bar30.close) - open_px
    aligned = None
    if ret30 is not None:
        if event.side == "pdh_sweep":
            aligned = ret30 > 0
        else:
            aligned = ret30 < 0
    return {
        "contract": (event.extras or {}).get("contract"),
        "session_bucket": session_bucket(int(event.seconds_from_rth_open)),
        "opening_drive_30m": int(event.seconds_from_rth_open) < 1800,
        "opening_drive_60m": int(event.seconds_from_rth_open) < 3600,
        "median_1m_range_14": med_rng,
        "penetration_ticks": pen / 0.25,
        "penetration_over_atr": None if not atr else pen / atr,
        "penetration_over_median_range": None if not med_rng else pen / med_rng,
        "open_30m_return": ret30,
        "sweep_aligned_open_30m": aligned,
    }


def rate(k: int, n: int) -> Optional[float]:
    return None if n <= 0 else k / n


def label_counts(labels: list[str]) -> dict[str, Any]:
    n = len(labels)
    n_rev = labels.count("REVERSAL")
    n_cont = labels.count("CONTINUATION")
    n_nei = labels.count("NEITHER")
    n_amb = labels.count("AMBIGUOUS")
    decided = n_rev + n_cont
    lo, hi = wilson_ci(n_rev, n)
    return {
        "n": n,
        "reversal": n_rev,
        "continuation": n_cont,
        "neither": n_nei,
        "ambiguous": n_amb,
        "unresolved": n_nei + n_amb,
        "p_reversal": rate(n_rev, n),
        "p_reversal_wilson_lo": lo,
        "p_reversal_wilson_hi": hi,
        "p_reversal_among_decided": rate(n_rev, decided),
        "p_reversal_unresolved_as_fail": rate(n_rev, n),
    }


def logit_fit(X: list[list[float]], y: list[int], steps: int = 120, lr: float = 0.15, l2: float = 0.5) -> list[float]:
    n, p = len(X), len(X[0])
    w = [0.0] * p
    for _ in range(steps):
        grad = [0.0] * p
        for i in range(n):
            z = sum(w[j] * X[i][j] for j in range(p))
            z = max(-20.0, min(20.0, z))
            pi = 1.0 / (1.0 + math.exp(-z))
            err = pi - y[i]
            for j in range(p):
                grad[j] += err * X[i][j]
        for j in range(p):
            pen = 0.0 if j == 0 else l2 * w[j]
            w[j] -= lr * ((grad[j] / n) + pen)
    return w


def logit_predict(X: list[list[float]], w: list[float]) -> list[float]:
    out = []
    for row in X:
        z = sum(w[j] * row[j] for j in range(len(w)))
        z = max(-20.0, min(20.0, z))
        out.append(1.0 / (1.0 + math.exp(-z)))
    return out


def brier(ps: list[float], y: list[int]) -> Optional[float]:
    if not ps:
        return None
    return sum((p - yi) ** 2 for p, yi in zip(ps, y)) / len(ps)


def logloss(ps: list[float], y: list[int]) -> Optional[float]:
    if not ps:
        return None
    s = 0.0
    for p, yi in zip(ps, y):
        p = min(1 - 1e-9, max(1e-9, p))
        s += -yi * math.log(p) - (1 - yi) * math.log(1 - p)
    return s / len(ps)


def zscore_fit(cols: list[list[float]]) -> tuple[list[float], list[float]]:
    means, sds = [], []
    for col in zip(*cols):
        mu = statistics.mean(col)
        sd = statistics.pstdev(col) or 1.0
        means.append(mu)
        sds.append(sd)
    return means, sds


def zscore_apply(row: list[float], means: list[float], sds: list[float]) -> list[float]:
    return [1.0] + [(row[j] - means[j]) / sds[j] for j in range(len(row))]


def event_span(event: SweepEvent) -> tuple[int, int]:
    a = int(event.sweep_bar_time) - WINDOW_BEFORE_SEC
    b = int(event.sweep_bar_time) + WINDOW_AFTER_SEC
    return a, b


def build_windows(events: list[SweepEvent]) -> list[dict[str, Any]]:
    """Construct leak-safe download windows without per-window Databento get_cost calls.

    Phase 34 scaled cost is the pre-download gate. Exact get_cost on ~250 windows is
    hundreds of serial HTTPS round-trips and was aborted as blocking.
    """
    groups: dict[tuple[str, str], list[tuple[int, int]]] = {}
    for e in events:
        sym = str((e.extras or {}).get("contract") or "NQM6")
        groups.setdefault((sym, e.trading_date), []).append(event_span(e))
    parts = []
    for (sym, td), spans in sorted(groups.items()):
        for lo, hi in merge_spans(spans):
            start = datetime.fromtimestamp(lo, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
            end = datetime.fromtimestamp(hi, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
            parts.append({
                "symbol": sym, "date": td, "start": start, "end": end,
                "start_unix": lo, "end_unix": hi,
            })
    return parts


def cost_windows(events: list[SweepEvent], schema: str) -> dict[str, Any]:
    """Window list for download. Cost is the Phase 34 scaled estimate, not per-window API."""
    parts = build_windows(events)
    return {
        "ok": True,
        "schema": schema,
        "n_windows": len(parts),
        "cost_method": "scaled_from_phase34_not_per_window_get_cost",
        "windows": parts,
    }


def download_windows(costed: dict[str, Any], schema: str) -> dict[str, Any]:
    from databento_history import load_databento_credential
    import databento as db

    load_databento_credential()
    dest = ROOT / "data" / "databento" / "NQ" / "microstructure" / schema
    dest.mkdir(parents=True, exist_ok=True)
    client = db.Historical()
    saved = []
    n_cached = n_new = 0
    windows = [w for w in (costed.get("windows") or []) if "error" not in w]
    print(f"{schema}: {len(windows)} merged windows", flush=True)
    for w in windows:
        sym = w["symbol"]
        path = dest / f"{sym}_{schema}_{w['date']}_{int(w.get('start_unix') or 0)}.dbn.zst"
        if path.exists() and path.stat().st_size > 1000:
            n_cached += 1
            saved.append({**{k: w[k] for k in ("symbol", "date", "start_unix", "end_unix") if k in w}, "path": str(path), "cached": True, "bytes": path.stat().st_size})
            continue
        try:
            data = client.timeseries.get_range(
                dataset="GLBX.MDP3", symbols=sym, schema=schema,
                start=w["start"], end=w["end"], stype_in="raw_symbol",
            )
            data.to_file(path)
        except Exception as exc:  # noqa: BLE001
            print(f"FAILED {path.name}: {type(exc).__name__}:{exc}", flush=True)
            saved.append({
                **{k: w[k] for k in ("symbol", "date", "start_unix", "end_unix") if k in w},
                "path": str(path), "cached": False, "error": f"{type(exc).__name__}:{exc}",
            })
            continue
        n_new += 1
        saved.append({**{k: w[k] for k in ("symbol", "date", "start_unix", "end_unix") if k in w}, "path": str(path), "cached": False, "bytes": path.stat().st_size})
        print(f"saved {path.name} ({path.stat().st_size} bytes)  cached={n_cached} new={n_new}", flush=True)
    return {"ok": True, "files": saved, "n_cached": n_cached, "n_new": n_new, "dir": str(dest)}


def covering_file(files: list[dict[str, Any]], event: SweepEvent) -> Optional[Path]:
    a, _b = event_span(event)
    t_cut = int(event.sweep_bar_time) + FEATURE_CUTOFF_OFFSET_SEC
    sym = str((event.extras or {}).get("contract") or "")
    best = None
    for f in files:
        p = Path(f["path"])
        if not p.exists():
            continue
        if f.get("symbol") and f.get("symbol") != sym:
            continue
        lo = int(f.get("start_unix") or 0)
        hi = int(f.get("end_unix") or 0)
        if lo and hi and lo <= a and hi >= t_cut:
            return p
        if f.get("date") == event.trading_date:
            best = p
    return best


def dvp_relationship(events: list[SweepEvent]) -> dict[str, Any]:
    dvp_path = ROOT / "journal" / "phase29_nq_drift_vwap" / "trades.jsonl"
    trades = []
    if dvp_path.exists():
        for line in dvp_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                trades.append(json.loads(line))
    sweep_days = {e.trading_date for e in events}
    dvp_in = [t for t in trades if t.get("trading_date") in sweep_days]
    dvp_days = {t.get("trading_date") for t in dvp_in}
    same_hour = oppose = agree = 0
    for e in events:
        day = [t for t in dvp_in if t.get("trading_date") == e.trading_date]
        if any(abs(int(t.get("entry_timestamp") or 0) - int(e.sweep_ts)) <= 3600 for t in day):
            same_hour += 1
        drifts = {(t.get("extras") or {}).get("drift") for t in day}
        if e.side == "pdl_sweep" and "NEGATIVE_DRIFT" in drifts:
            oppose += 1
        elif e.side == "pdh_sweep" and "POSITIVE_DRIFT" in drifts:
            oppose += 1
        elif e.side == "pdl_sweep" and "POSITIVE_DRIFT" in drifts:
            agree += 1
        elif e.side == "pdh_sweep" and "NEGATIVE_DRIFT" in drifts:
            agree += 1
    return {
        "dvp_same_day_overlap_n": len(sweep_days & dvp_days),
        "n_sweep_days": len(sweep_days),
        "n_sweeps_with_dvp_same_hour": same_hour,
        "n_sweeps_opposing_dvp_drift": oppose,
        "n_sweeps_agreeing_dvp_drift": agree,
        "note": "Event-level only. No Phase 35 entries, so P&L correlation is undefined.",
    }


def detect_expanded(rolls, news: set[str]) -> tuple[list[SweepEvent], dict[str, list], dict[tuple[str, int], Any], list[SweepEvent]]:
    by_c = dates_by_contract(EXPAND_START, EXPAND_END, rolls, US_RTH_HOLIDAYS)
    events: list[SweepEvent] = []
    bars_by_contract: dict[str, list] = {}
    outcomes: dict[tuple[str, int], Any] = {}
    print(f"contracts in window: {sorted(by_c)}", flush=True)
    for sym, dates in sorted(by_c.items()):
        print(f"loading {sym} ({len(dates)} RTH dates)...", flush=True)
        bars = load_contract_bars(sym)
        bars_by_contract[sym] = bars
        ev = detect_pdh_pdl_sweeps(bars, dates, skip_source_dates=US_RTH_HOLIDAYS, contract=sym)
        for e in ev:
            outcomes[(e.event_id, PRIMARY_HORIZON_SEC)] = label_outcome_1m(e, bars, horizon_sec=PRIMARY_HORIZON_SEC)
        events.extend(ev)
        print(f"  {sym}: {len(ev)} sweeps", flush=True)
    events.sort(key=lambda e: (e.trading_date, e.sweep_bar_time, e.side))
    raw = list(events)
    eligible = [e for e in events if e.trading_date not in news]
    return eligible, bars_by_contract, outcomes, raw


def main() -> dict[str, Any]:
    frozen = assert_frozen()
    if not frozen["ok"]:
        payload = {"ok": False, "status": "FROZEN_INTEGRITY_FAILED", "frozen": frozen}
        VALIDATION.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return payload

    spec = json.loads(SPEC_PATH.read_text(encoding="utf-8"))
    print("Reproducing Phase 34...", flush=True)
    repro = reproduce_phase34()
    print(f"Phase 34 repro: {repro}", flush=True)

    rolls = load_rolls()
    news = news_dates()
    eligible, bars_by_c, outcomes, raw = detect_expanded(rolls, news)
    removed_news = [e for e in raw if e.trading_date in news]
    # Strict ±5m around 08:30 never overlaps RTH 09:30+
    removed_pm55 = []

    JOURNAL.mkdir(parents=True, exist_ok=True)
    detail_rows = []
    y_map = {}
    for e in eligible:
        o = outcomes[(e.event_id, PRIMARY_HORIZON_SEC)]
        extra = enrich(e, bars_by_c[e.extras["contract"]])
        y_map[e.event_id] = o.label == "REVERSAL"
        detail_rows.append({**e.to_dict(), **extra, "label_300s": o.label, "mfe": o.mfe_points, "mae": o.mae_points})
    _write_csv(REPORTS / "phase35_sweep_events.csv", detail_rows)
    with (JOURNAL / "sweeps.jsonl").open("w", encoding="utf-8") as fh:
        for r in detail_rows:
            fh.write(json.dumps(r, default=str) + "\n")

    def _labs(subset):
        return [outcomes[(e.event_id, PRIMARY_HORIZON_SEC)].label for e in subset]

    base_all = label_counts(_labs(eligible))
    base_pdh = label_counts(_labs([e for e in eligible if e.side == "pdh_sweep"]))
    base_pdl = label_counts(_labs([e for e in eligible if e.side == "pdl_sweep"]))
    _write_csv(REPORTS / "phase35_sweep_baseline.csv", [
        {"slice": "ALL", **base_all},
        {"slice": "PDH", **base_pdh},
        {"slice": "PDL", **base_pdl},
    ])

    # Structural features vs reversal
    struct_rows = []
    getters = {
        "penetration_points": lambda r: r["penetration_points"],
        "penetration_over_atr": lambda r: r.get("penetration_over_atr"),
        "penetration_over_median_range": lambda r: r.get("penetration_over_median_range"),
        "volume_sweep_bar": lambda r: r.get("volume_sweep_bar"),
        "seconds_from_rth_open": lambda r: r.get("seconds_from_rth_open"),
        "atr_1m_14": lambda r: r.get("atr_1m_14"),
    }
    for name, getter in getters.items():
        pairs = []
        for r in detail_rows:
            v = getter(r)
            if v is None:
                continue
            pairs.append((float(v), r["label_300s"] == "REVERSAL"))
        split = median_split(pairs)
        rho = spearman_rho([(x, 1.0 if y else 0.0) for x, y in pairs])
        stab = neighboring_threshold_stable(pairs, 0.08, (0.5, 0.6, 0.7, 0.8))
        struct_rows.append({"feature": name, "kind": "median_split", "spearman": rho, "stability": stab, **split})
        for qrow in quantile_rows(pairs, 5):
            struct_rows.append({"feature": name, "kind": "quintile", **qrow})
    _write_csv(REPORTS / "phase35_structure_quantiles.csv", struct_rows)

    # Time of day / opening drive
    tod_rows = []
    for bucket in ("open_30m", "open_60m", "midday", "afternoon"):
        sub = [r for r in detail_rows if r.get("session_bucket") == bucket]
        tod_rows.append({"slice": bucket, **label_counts([r["label_300s"] for r in sub])})
    for flag, label in (("opening_drive_30m", "in_open_30m"), ("opening_drive_60m", "in_open_60m")):
        sub_yes = [r for r in detail_rows if r.get(flag)]
        sub_no = [r for r in detail_rows if not r.get(flag)]
        tod_rows.append({"slice": label, **label_counts([r["label_300s"] for r in sub_yes])})
        tod_rows.append({"slice": f"not_{label}", **label_counts([r["label_300s"] for r in sub_no])})
    aligned = [r for r in detail_rows if r.get("sweep_aligned_open_30m") is True]
    against = [r for r in detail_rows if r.get("sweep_aligned_open_30m") is False]
    tod_rows.append({"slice": "aligned_open_30m", **label_counts([r["label_300s"] for r in aligned])})
    tod_rows.append({"slice": "against_open_30m", **label_counts([r["label_300s"] for r in against])})
    _write_csv(REPORTS / "phase35_time_of_day.csv", tod_rows)

    # Chrono split 70/30 by unique dates
    dates_sorted = sorted({e.trading_date for e in eligible})
    cut = max(1, int(round(len(dates_sorted) * 0.7)))
    train_d, hold_d = set(dates_sorted[:cut]), set(dates_sorted[cut:])
    train_ev = [e for e in eligible if e.trading_date in train_d]
    hold_ev = [e for e in eligible if e.trading_date in hold_d]

    def _pen_pairs(subset):
        return [(float(e.penetration_points), outcomes[(e.event_id, PRIMARY_HORIZON_SEC)].label == "REVERSAL") for e in subset]

    train_pen = _pen_pairs(train_ev)
    hold_pen = _pen_pairs(hold_ev)
    train_med_pen = statistics.median(x for x, _ in train_pen) if train_pen else None

    def _shallow_rate(subset, med):
        if med is None:
            return {}
        sh = [e for e in subset if e.penetration_points <= med]
        dp = [e for e in subset if e.penetration_points > med]
        return {
            "median_from_train": med,
            "shallow": label_counts(_labs(sh)),
            "deep": label_counts(_labs(dp)),
        }

    chrono = {
        "train_dates": len(train_d),
        "holdout_dates": len(hold_d),
        "train": label_counts(_labs(train_ev)),
        "holdout": label_counts(_labs(hold_ev)),
        "train_penetration": _shallow_rate(train_ev, train_med_pen),
        "holdout_penetration": _shallow_rate(hold_ev, train_med_pen),
        "train_spearman_penetration": spearman_rho([(x, 1.0 if y else 0.0) for x, y in train_pen]),
        "holdout_spearman_penetration": spearman_rho([(x, 1.0 if y else 0.0) for x, y in hold_pen]),
    }

    # Walk-forward 4 blocks
    wf = []
    n_blocks = 4 if len(dates_sorted) >= 40 else 2
    block_len = max(1, len(dates_sorted) // n_blocks)
    for i in range(n_blocks):
        a = i * block_len
        b = len(dates_sorted) if i == n_blocks - 1 else (i + 1) * block_len
        ds = set(dates_sorted[a:b])
        sub = [e for e in eligible if e.trading_date in ds]
        pairs = _pen_pairs(sub)
        wf.append({
            "block": i + 1,
            "start": dates_sorted[a],
            "end": dates_sorted[b - 1],
            **label_counts(_labs(sub)),
            "spearman_penetration": spearman_rho([(x, 1.0 if y else 0.0) for x, y in pairs]),
            "quintiles": quantile_rows(pairs, 5),
        })
    _write_csv(REPORTS / "phase35_walkforward.csv", [{k: v for k, v in r.items() if k != "quintiles"} | {"quintiles": r["quintiles"]} for r in wf])

    # MBP
    do_dl = "--download-mbp" in sys.argv
    cache_only = "--cache-only" in sys.argv
    n_per = 20.0
    est_mbp = (2.9228472188109995 / n_per) * len(eligible)
    est_trd = (0.549571573734 / n_per) * len(eligible)
    estimate = {
        "n_eligible_sweeps": len(eligible),
        "method": "scale Phase 34 windowed cost ($2.92 MBP-10 + $0.55 trades / 20 events)",
        "est_mbp10_usd": est_mbp,
        "est_trades_usd": est_trd,
        "est_total_usd": est_mbp + est_trd,
        "est_mb_from_phase34": (136.77 / n_per) * len(eligible),
        "window": "T-60s to T+120s (Phase 34 frozen; not T-120/T+300)",
        "cost_cap_usd": COST_CAP_USD,
    }
    (REPORTS / "phase35_cost_estimate.json").write_text(json.dumps(estimate, indent=2), encoding="utf-8")
    print(json.dumps({"cost_estimate": estimate}, indent=2), flush=True)

    cost_mbp = cost_trd = None
    dl_mbp = dl_trd = None
    micro_rows: list[dict[str, Any]] = []
    micro_splits: list[dict[str, Any]] = []
    incremental: dict[str, Any] = {}
    n_book = 0
    if (do_dl or cache_only) and eligible:
        print("Building download windows (no per-window get_cost)...", flush=True)
        cost_mbp = cost_windows(eligible, "mbp-10")
        cost_trd = cost_windows(eligible, "trades")
        (REPORTS / "phase35_window_cost.json").write_text(
            json.dumps({
                "gate": "scaled_phase34_estimate",
                "scaled_estimate_usd": estimate["est_total_usd"],
                "cost_cap_usd": COST_CAP_USD,
                "n_windows_mbp10": cost_mbp.get("n_windows"),
                "n_windows_trades": cost_trd.get("n_windows"),
                "mbp-10": {k: v for k, v in cost_mbp.items() if k != "windows"},
                "trades": {k: v for k, v in (cost_trd or {}).items() if k != "windows"},
            }, indent=2),
            encoding="utf-8",
        )
        total_est = float(estimate["est_total_usd"])
        print(
            f"scaled estimate ${total_est:.2f} for {cost_mbp.get('n_windows')} merged windows "
            f"(cap ${COST_CAP_USD:.0f})",
            flush=True,
        )
        if cache_only:
            print("Cache-only: indexing existing MBP-10/trades files, no new Databento requests.", flush=True)
            dest_m = ROOT / "data" / "databento" / "NQ" / "microstructure" / "mbp-10"
            dest_t = ROOT / "data" / "databento" / "NQ" / "microstructure" / "trades"
            def _index(dest, schema, windows):
                files = []
                n_cached = 0
                for w in windows:
                    path = dest / f"{w['symbol']}_{schema}_{w['date']}_{int(w['start_unix'])}.dbn.zst"
                    if path.exists() and path.stat().st_size > 1000:
                        n_cached += 1
                        files.append({**{k: w[k] for k in ("symbol", "date", "start_unix", "end_unix") if k in w}, "path": str(path), "cached": True, "bytes": path.stat().st_size})
                return {"ok": True, "files": files, "n_cached": n_cached, "n_new": 0, "dir": str(dest), "cache_only": True}
            dl_mbp = _index(dest_m, "mbp-10", cost_mbp.get("windows") or [])
            dl_trd = _index(dest_t, "trades", cost_trd.get("windows") or [])
            print(f"cache mbp-10={dl_mbp['n_cached']} trades={dl_trd['n_cached']}", flush=True)
        elif total_est > COST_CAP_USD:
            dl_mbp = {"ok": False, "skipped": True, "reason": f"scaled_cost_{total_est:.2f}_exceeds_{COST_CAP_USD}"}
        else:
            print("Downloading mbp-10...", flush=True)
            dl_mbp = download_windows(cost_mbp, "mbp-10")
            print("Downloading trades...", flush=True)
            dl_trd = download_windows(cost_trd, "trades")
        if dl_mbp and not dl_mbp.get("skipped"):
            mbp_files = list(dl_mbp.get("files") or [])
            trd_files = list((dl_trd or {}).get("files") or [])
            rec_cache: dict[str, list] = {}
            import databento as db
            from collections import defaultdict

            grouped: dict[str, list[SweepEvent]] = defaultdict(list)
            for e in eligible:
                mbp_path = covering_file(mbp_files, e)
                grouped[str(mbp_path) if mbp_path else ""].append(e)
            done = 0
            for key, evs in grouped.items():
                recs: list = []
                if key:
                    try:
                        recs = list(db.DBNStore.from_file(key))
                        trd_path = covering_file(trd_files, evs[0]) if trd_files else None
                        if trd_path is not None and str(trd_path) != key:
                            recs.extend(list(db.DBNStore.from_file(trd_path)))
                    except Exception as exc:  # noqa: BLE001
                        print(f"DBN read failed {key}: {type(exc).__name__}:{exc}", flush=True)
                        recs = []
                for e in evs:
                    feats = {"has_book": False}
                    try:
                        if recs:
                            feats = features_from_records(recs, e)
                    except Exception as exc:  # noqa: BLE001
                        feats = {"has_book": False, "error": f"{type(exc).__name__}:{exc}"}
                    lab = outcomes[(e.event_id, PRIMARY_HORIZON_SEC)].label
                    micro_rows.append({"event_id": e.event_id, "side": e.side, "trading_date": e.trading_date, "label_300s": lab, **feats})
                    done += 1
                    if done % 20 == 0:
                        print(f"features {done}/{len(eligible)}", flush=True)
                recs = []
            _write_csv(REPORTS / "phase35_micro_features.csv", micro_rows)
            n_book = sum(1 for r in micro_rows if r.get("has_book"))
            rev = [r["label_300s"] == "REVERSAL" for r in micro_rows]
            for feat in MBP_FEATS:
                pairs = []
                for r, yi in zip(micro_rows, rev):
                    v = r.get(feat)
                    if v is None or (isinstance(v, float) and (math.isnan(v) or math.isinf(v))):
                        continue
                    pairs.append((float(v), bool(yi)))
                split = median_split(pairs)
                stab = neighboring_threshold_stable(pairs, 0.08, (0.5, 0.6, 0.7, 0.8))
                rho = spearman_rho([(x, 1.0 if y else 0.0) for x, y in pairs])
                status = "THRESHOLD_STABLE" if stab.get("stable") else "THRESHOLD_UNSTABLE"
                micro_splits.append({"feature": feat, "status": status, "spearman": rho, "stability": stab, **split})
            _write_csv(REPORTS / "phase35_micro_median_splits.csv", micro_splits)

            # Incremental logistic: train/holdout on events with book
            by_id = {r["event_id"]: r for r in micro_rows if r.get("has_book")}
            train_ids = [e.event_id for e in train_ev if e.event_id in by_id]
            hold_ids = [e.event_id for e in hold_ev if e.event_id in by_id]
            det = {r["event_id"]: r for r in detail_rows}

            def _row_struct(eid: str) -> list[float]:
                r = det[eid]
                side = 1.0 if r["side"] == "pdh_sweep" else 0.0
                return [
                    side,
                    float(r["penetration_points"] or 0),
                    float(r.get("seconds_from_rth_open") or 0),
                    float(r.get("volume_sweep_bar") or 0),
                ]

            def _row_mbp(eid: str) -> list[float]:
                m = by_id[eid]
                return _row_struct(eid) + [
                    float(m.get("imb_for_reversal_top10") or 0.5),
                    float(m.get("ofi_for_reversal") or 0),
                    float(m.get("absorption_proxy") or 0),
                    float(m.get("signed_flow_for_reversal") or 0),
                ]

            def _eval(ids, make_row):
                rawX = [make_row(i) for i in ids]
                y = [1 if by_id[i]["label_300s"] == "REVERSAL" else 0 for i in ids]
                return rawX, y

            def _run(make_row, name):
                if len(train_ids) < 30 or len(hold_ids) < 15:
                    return {"name": name, "skipped": True, "reason": "n_too_small"}
                Xtr, ytr = _eval(train_ids, make_row)
                means, sds = zscore_fit(Xtr)
                Ztr = [zscore_apply(r, means, sds) for r in Xtr]
                w = logit_fit(Ztr, ytr)
                Xh, yh = _eval(hold_ids, make_row)
                Zh = [zscore_apply(r, means, sds) for r in Xh]
                ph = logit_predict(Zh, w)
                ptr = logit_predict(Ztr, w)
                return {
                    "name": name,
                    "n_train": len(ytr),
                    "n_holdout": len(yh),
                    "train_brier": brier(ptr, ytr),
                    "holdout_brier": brier(ph, yh),
                    "holdout_logloss": logloss(ph, yh),
                    "holdout_base_rate": sum(yh) / len(yh),
                    "holdout_mean_p": statistics.mean(ph) if ph else None,
                }

            incremental = {
                "model0_side": _run(lambda i: [1.0 if det[i]["side"] == "pdh_sweep" else 0.0], "side"),
                "model1_side_pen": _run(lambda i: [1.0 if det[i]["side"] == "pdh_sweep" else 0.0, float(det[i]["penetration_points"] or 0)], "side+penetration"),
                "model2_struct": _run(_row_struct, "side+pen+tod+vol"),
                "model3_top10": _run(lambda i: _row_struct(i) + [float(by_id[i].get("imb_for_reversal_top10") or 0.5)], "struct+top10"),
                "model4_ofi": _run(lambda i: _row_struct(i) + [float(by_id[i].get("ofi_for_reversal") or 0)], "struct+ofi"),
                "model5_abs": _run(lambda i: _row_struct(i) + [float(by_id[i].get("absorption_proxy") or 0)], "struct+absorption"),
                "model6_mbp_set": _run(_row_mbp, "struct+mbp_set"),
            }
            (REPORTS / "phase35_incremental.json").write_text(json.dumps(incremental, indent=2), encoding="utf-8")

            # Conditional: shallow + high reversal-aligned top10
            if train_med_pen is not None:
                sh = [r for r in micro_rows if det.get(r["event_id"], {}).get("penetration_points", 9e9) <= train_med_pen]
                imb_vals = [float(r["imb_for_reversal_top10"]) for r in sh if r.get("imb_for_reversal_top10") is not None]
                if imb_vals:
                    med_imb = statistics.median(imb_vals)
                    hi = [r for r in sh if r.get("imb_for_reversal_top10") is not None and r["imb_for_reversal_top10"] > med_imb]
                    lo = [r for r in sh if r.get("imb_for_reversal_top10") is not None and r["imb_for_reversal_top10"] <= med_imb]
                    incremental["p_rev_shallow"] = label_counts([r["label_300s"] for r in sh])
                    incremental["p_rev_shallow_high_top10imb"] = label_counts([r["label_300s"] for r in hi])
                    incremental["p_rev_shallow_low_top10imb"] = label_counts([r["label_300s"] for r in lo])

    dvp = dvp_relationship(eligible)
    gc_paper = ROOT / "journal" / "phase26_gc_vwap_v2_paper" / "paper_trades.jsonl"
    gc = {
        "gc_paper_empty": gc_paper.exists() and gc_paper.stat().st_size == 0,
        "note": "GC is a different market. No Phase 35 entries, so active-day P&L overlap is undefined.",
    }

    # Verdict (predeclared in this function, not tuned to maximize a number)
    pen_pairs_all = [(float(e.penetration_points), y_map[e.event_id]) for e in eligible]
    rho_all = spearman_rho([(x, 1.0 if y else 0.0) for x, y in pen_pairs_all])
    q_all = quantile_rows(pen_pairs_all, 5)
    q_mono = False
    if len(q_all) == 5:
        q_mono = q_all[0]["p_reversal"] - q_all[-1]["p_reversal"] >= 0.10
    rho_hold = chrono.get("holdout_spearman_penetration")
    struct_ok_sample = len(eligible) >= 100 and rho_all is not None and rho_all <= -0.20 and q_mono
    struct_hold = (
        rho_hold is not None
        and rho_hold <= -0.15
        and (chrono.get("holdout_penetration") or {}).get("shallow", {}).get("p_reversal")
        is not None
        and (chrono.get("holdout_penetration") or {}).get("deep", {}).get("p_reversal") is not None
        and chrono["holdout_penetration"]["shallow"]["p_reversal"] - chrono["holdout_penetration"]["deep"]["p_reversal"] >= 0.08
    )
    mbp_incr = False
    m2 = (incremental.get("model2_struct") or {})
    m6 = (incremental.get("model6_mbp_set") or {})
    if m2.get("holdout_brier") is not None and m6.get("holdout_brier") is not None:
        mbp_incr = (m2["holdout_brier"] - m6["holdout_brier"]) >= 0.01
    stable_mbp = [r["feature"] for r in micro_splits if r.get("status") == "THRESHOLD_STABLE"]

    if not repro["ok"]:
        verdict = "DATA_QUALITY_BLOCKED"
    elif mbp_incr and stable_mbp and n_book >= 80:
        verdict = "MBP_INCREMENTAL_EDGE_FOUND"
    elif struct_ok_sample and struct_hold and not mbp_incr:
        verdict = "STRUCTURAL_ONLY_EDGE_FOUND"
    elif struct_ok_sample and not struct_hold:
        verdict = "STRUCTURAL_SWEEP_PROMISING_NEEDS_MORE_DATA"
    elif rho_all is not None and rho_all <= -0.15 and len(eligible) >= 80:
        verdict = "STRUCTURAL_SWEEP_PROMISING_NEEDS_MORE_DATA"
    elif n_book >= 80 and not mbp_incr and not struct_ok_sample:
        verdict = "MICROSTRUCTURE_EDGE_REJECTED"
    else:
        verdict = "MICROSTRUCTURE_EDGE_WEAK"

    mbo = "DO_NOT_ESCALATE_TO_MBO"
    if any(r["feature"] == "executed_to_displayed" and r.get("status") == "THRESHOLD_STABLE" for r in micro_splits):
        mbo = "MBO_TARGETED_STUDY_JUSTIFIED"
    if any(r["feature"] == "persistence_top1_swept_side" and r.get("status") == "THRESHOLD_STABLE" for r in micro_splits):
        mbo = "MBO_TARGETED_STUDY_JUSTIFIED"

    frozen_after = assert_frozen()
    payload = {
        "ok": frozen["ok"] and frozen_after["ok"] and repro["ok"],
        "phase": 35,
        "status": "RESEARCH_COMPLETE",
        "verdict": verdict,
        "mbo_recommendation": mbo,
        "execution": "DRY_RUN_NO_BROKER",
        "frozen_before": frozen,
        "frozen_after": frozen_after,
        "spec": spec,
        "phase34_reproduction": repro,
        "expansion": {
            "start": EXPAND_START.isoformat(),
            "end": EXPAND_END.isoformat(),
            "contracts": sorted({e.extras.get("contract") for e in eligible}),
            "n_raw_sweeps": len(raw),
            "n_removed_full_cpi_nfp_day": len(removed_news),
            "n_removed_pm_5_5": len(removed_pm55),
            "n_eligible": len(eligible),
            "n_pdh": sum(1 for e in eligible if e.side == "pdh_sweep"),
            "n_pdl": sum(1 for e in eligible if e.side == "pdl_sweep"),
            "roll": "volume-crossover, 18:00 NY activation; PDH/PDL from same raw contract as sweep RTH day",
        },
        "baseline_all": base_all,
        "baseline_pdh": base_pdh,
        "baseline_pdl": base_pdl,
        "penetration_spearman": rho_all,
        "penetration_quintiles": q_all,
        "chrono": chrono,
        "walkforward_blocks": wf,
        "cost_estimate": estimate,
        "window_cost_mbp10": None if not cost_mbp else {k: v for k, v in cost_mbp.items() if k != "windows"},
        "window_cost_trades": None if not cost_trd else {k: v for k, v in cost_trd.items() if k != "windows"},
        "download_mbp": None if not dl_mbp else {k: v for k, v in dl_mbp.items() if k != "files"},
        "n_events_with_book": n_book,
        "microstructure_median_splits": micro_splits,
        "incremental": incremental,
        "dvp": dvp,
        "gc": gc,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    VALIDATION.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    CANDIDATE.write_text(
        json.dumps(
            {
                "phase": "phase35",
                "strategy_family": "nq_liquidity_microstructure_reversal_v1",
                "strategy_version": "v1.phase35",
                "status": "RESEARCH_ONLY_NOT_FROZEN",
                "verdict": verdict,
                "mbo_recommendation": mbo,
                "n_eligible": len(eligible),
                "p_reversal": base_all.get("p_reversal"),
                "note": "RESEARCH ONLY. No freeze. No broker execution. No entries unless a later phase authorizes strategy construction.",
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return payload


if __name__ == "__main__":
    out = main()
    print(
        json.dumps(
            {
                "ok": out.get("ok"),
                "verdict": out.get("verdict"),
                "mbo": out.get("mbo_recommendation"),
                "repro": out.get("phase34_reproduction"),
                "n_eligible": (out.get("expansion") or {}).get("n_eligible"),
                "baseline": out.get("baseline_all"),
                "spearman_pen": out.get("penetration_spearman"),
                "n_book": out.get("n_events_with_book"),
                "frozen": (out.get("frozen_after") or {}).get("ok"),
            },
            indent=2,
            default=str,
        )
    )
