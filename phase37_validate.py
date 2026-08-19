"""Phase 37 — executed volume / delta confirmation on Phase 35 shallow sweeps.

DRY_RUN. No broker. No freeze. No MBP/MBO/DOM.
Specification is frozen in phase37_spec.json before this validator inspects P&L.
"""
from __future__ import annotations

import csv
import json
import math
import statistics
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Optional

from nq_executed_flow_features import expected_sign, flow_from_trades
from nq_microstructure_features import median_split, neighboring_threshold_stable, spearman_rho, wilson_ci
from nq_microstructure_models import FROZEN_GC_HASH, FROZEN_NQ_HASH, SweepEvent
from nq_pdh_pdl import rth_bars
from nq_shallow_sweep_engine import find_reclaim
from phase34_validate import assert_frozen, file_sha256, GC_FILE_SHA, NQ_FILE_SHA
from phase35_validate import (
    brier,
    load_contract_bars,
    logit_fit,
    logit_predict,
    logloss,
    zscore_apply,
    zscore_fit,
)
from phase36_validate import dvp_compare, first_of_day_ids, load_events, run_set, score

ROOT = Path(__file__).resolve().parent
REPORTS = ROOT / "reports"
JOURNAL = ROOT / "journal" / "phase37_nq_executed_flow"
VALIDATION = ROOT / "phase37_validation.json"
SPEC_PATH = ROOT / "phase37_spec.json"
TRADES_DIR = ROOT / "data" / "databento" / "NQ" / "microstructure" / "trades"
EVENTS_CSV = REPORTS / "phase35_sweep_events.csv"
CANDIDATE = ROOT / "strategy_candidates" / "phase37_NQ_SHALLOW_SWEEP_FLOW.json"

SHALLOW = 18.25
TRAIN_END = "2026-04-09"
HOLDOUT_START = "2026-04-13"
WF_BLOCKS = [
    ("2025-06-17", "2025-09-29"),
    ("2025-09-30", "2026-01-16"),
    ("2026-01-20", "2026-04-24"),
    ("2026-04-28", "2026-08-14"),
]
BRIER_LIFT = 0.01
COND_LIFT = 0.08
FLOW_FEATS = (
    "ndelta_rev_sweep60",
    "volume_burst",
    "flow_efficiency",
    "exhaustion_score",
    "delta_divergence",
    "price_impact",
)
LEAK_FEATS = ("ndelta_rev_post60", "flow_flip_diagnostic", "ndelta_rev_reclaim_diagnostic")


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


def label_counts(labels: list[str]) -> dict[str, Any]:
    n = len(labels)
    n_rev = labels.count("REVERSAL")
    lo, hi = wilson_ci(n_rev, n)
    return {
        "n": n,
        "reversal": n_rev,
        "continuation": labels.count("CONTINUATION"),
        "neither": labels.count("NEITHER"),
        "ambiguous": labels.count("AMBIGUOUS"),
        "p_reversal": None if n == 0 else n_rev / n,
        "p_reversal_wilson_lo": lo,
        "p_reversal_wilson_hi": hi,
    }


def index_trade_files() -> list[dict[str, Any]]:
    files = []
    if not TRADES_DIR.exists():
        return files
    for p in TRADES_DIR.glob("*_trades_*.dbn.zst"):
        stem = p.name.replace(".dbn.zst", "")
        parts = stem.split("_")
        if len(parts) < 4:
            continue
        files.append({
            "symbol": parts[0],
            "date": parts[2],
            "start_unix": int(parts[3]),
            "path": str(p),
            "bytes": p.stat().st_size,
        })
    return files


def covering_trades(files: list[dict[str, Any]], event: SweepEvent) -> Optional[Path]:
    t0 = int(event.sweep_bar_time) - 60
    sym = str((event.extras or {}).get("contract") or "")
    cands: list[tuple[int, dict[str, Any]]] = []
    for f in files:
        if f["symbol"] != sym or f["date"] != event.trading_date:
            continue
        lo = int(f["start_unix"])
        if lo <= t0:
            cands.append((t0 - lo, f))
    if not cands:
        return None
    cands.sort()
    return Path(cands[0][1]["path"])


def load_labels(events: list[SweepEvent]) -> dict[str, str]:
    rows = list(csv.DictReader(EVENTS_CSV.open(encoding="utf-8")))
    by_id = {r["event_id"]: r.get("label_300s") or "" for r in rows}
    return {e.event_id: by_id.get(e.event_id, "") for e in events}


def split_chrono(events: list[SweepEvent]) -> tuple[list[SweepEvent], list[SweepEvent]]:
    train = [e for e in events if e.trading_date <= TRAIN_END]
    hold = [e for e in events if e.trading_date >= HOLDOUT_START]
    return train, hold


def _num(v: Any) -> Optional[float]:
    if v is None or v == "":
        return None
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    if math.isnan(x) or math.isinf(x):
        return None
    return x


def train_threshold(train_rows: list[dict[str, Any]], feat: str, pct: float) -> Optional[float]:
    xs = sorted(x for r in train_rows if (x := _num(r.get(feat))) is not None)
    if not xs:
        return None
    i = min(len(xs) - 1, max(0, int(round((len(xs) - 1) * pct))))
    return xs[i]


def apply_split(rows: list[dict[str, Any]], feat: str, thr: float, *, high: bool) -> list[dict[str, Any]]:
    out = []
    for r in rows:
        v = _num(r.get(feat))
        if v is None:
            continue
        if high and v > thr:
            out.append(r)
        if not high and v <= thr:
            out.append(r)
    return out


def lift_vs_base(subset: list[dict[str, Any]], base_p: Optional[float]) -> Optional[float]:
    if not subset or base_p is None:
        return None
    p = sum(1 for r in subset if r["label_300s"] == "REVERSAL") / len(subset)
    return p - base_p


def eval_feature(rows: list[dict[str, Any]], feat: str) -> dict[str, Any]:
    pairs = []
    y = []
    for r in rows:
        v = _num(r.get(feat))
        if v is None:
            continue
        yi = r["label_300s"] == "REVERSAL"
        pairs.append((v, yi))
        y.append(yi)
    rho = spearman_rho([(x, 1.0 if yi else 0.0) for x, yi in pairs])
    split = median_split(pairs)
    stab = neighboring_threshold_stable(pairs, COND_LIFT, (0.5, 0.6, 0.7, 0.8))
    exp = expected_sign(feat)
    hold_sign_ok = rho is not None and ((rho > 0 and exp > 0) or (rho < 0 and exp < 0))
    return {
        "feature": feat,
        "n": len(pairs),
        "spearman": rho,
        "expected_sign": exp,
        "spearman_matches_expected": hold_sign_ok,
        "median_split": split,
        "threshold_stability": stab,
        "status": "THRESHOLD_STABLE" if stab.get("stable") else "THRESHOLD_UNSTABLE",
    }


def logit_run(train_rows: list[dict[str, Any]], hold_rows: list[dict[str, Any]], cols: list[str], name: str) -> dict[str, Any]:
    def _x(r):
        return [float(_num(r.get(c)) or 0.0) for c in cols]

    if len(train_rows) < 20 or len(hold_rows) < 10:
        return {"name": name, "skipped": True, "reason": "n_too_small", "cols": cols}
    Xtr = [_x(r) for r in train_rows]
    ytr = [1 if r["label_300s"] == "REVERSAL" else 0 for r in train_rows]
    if not cols:
        p_tr = sum(ytr) / len(ytr)
        yh = [1 if r["label_300s"] == "REVERSAL" else 0 for r in hold_rows]
        ph = [p_tr] * len(yh)
        ptr = [p_tr] * len(ytr)
        return {
            "name": name,
            "cols": cols,
            "n_train": len(ytr),
            "n_holdout": len(yh),
            "train_brier": brier(ptr, ytr),
            "holdout_brier": brier(ph, yh),
            "holdout_logloss": logloss(ph, yh),
            "holdout_base_rate": sum(yh) / len(yh),
            "holdout_mean_p": p_tr,
            "intercept_only": True,
        }
    means, sds = zscore_fit(Xtr)
    Ztr = [zscore_apply(r, means, sds) for r in Xtr]
    w = logit_fit(Ztr, ytr)
    Xh = [_x(r) for r in hold_rows]
    yh = [1 if r["label_300s"] == "REVERSAL" else 0 for r in hold_rows]
    Zh = [zscore_apply(r, means, sds) for r in Xh]
    ph = logit_predict(Zh, w)
    ptr = logit_predict(Ztr, w)
    return {
        "name": name,
        "cols": cols,
        "n_train": len(ytr),
        "n_holdout": len(yh),
        "train_brier": brier(ptr, ytr),
        "holdout_brier": brier(ph, yh),
        "holdout_logloss": logloss(ph, yh),
        "holdout_base_rate": sum(yh) / len(yh),
        "holdout_mean_p": statistics.mean(ph) if ph else None,
        "weights": w,
    }


def session_slice(rows: list[dict[str, Any]], kind: str) -> list[dict[str, Any]]:
    out = []
    for r in rows:
        s = int(r.get("seconds_from_rth_open") or 0)
        if kind == "first_bar" and s < 60:
            out.append(r)
        elif kind == "first_30m" and s < 1800:
            out.append(r)
        elif kind == "first_60m" and s < 3600:
            out.append(r)
        elif kind == "later" and s >= 3600:
            out.append(r)
    return out


def decide(payload: dict[str, Any]) -> tuple[str, str]:
    gate = payload.get("classification_gate") or {}
    n = int((payload.get("funnel") or {}).get("n_first_shallow_with_flow") or 0)
    n_hold = int((payload.get("funnel") or {}).get("n_holdout_first_shallow_flow") or 0)
    if n < 40 or n_hold < 15:
        return "INSUFFICIENT_FLOW_CONFIRMED_SAMPLE", "CLOSE_NQ_SWEEP_RESEARCH_BRANCH"
    brier_ok = bool(gate.get("brier_ok"))
    cond_ok = bool(gate.get("conditional_ok"))
    if brier_ok and cond_ok:
        strat = payload.get("strategy") or {}
        if strat.get("built"):
            prim = (strat.get("primary") or {})
            er = prim.get("holdout_expectancy_r")
            full_er = prim.get("full_expectancy_r")
            if er is not None and full_er is not None and er > 0 and full_er > 0:
                return "EXECUTED_FLOW_EDGE_FOUND", "CONTINUE_TO_STRATEGY_FREEZE_VALIDATION"
            return "FLOW_CONFIRMED_STRATEGY_NOT_TRADABLE", "CLOSE_NQ_SWEEP_RESEARCH_BRANCH"
        return "EXECUTED_FLOW_PROMISING_NEEDS_MORE_DATA", "CLOSE_NQ_SWEEP_RESEARCH_BRANCH"
    return "NO_INCREMENTAL_FLOW_VALUE", "CLOSE_NQ_SWEEP_RESEARCH_BRANCH"


def main() -> dict[str, Any]:
    frozen_before = assert_frozen()
    if not frozen_before["ok"]:
        payload = {"ok": False, "status": "FROZEN_INTEGRITY_FAILED", "frozen_before": frozen_before}
        VALIDATION.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return payload

    spec = json.loads(SPEC_PATH.read_text(encoding="utf-8"))
    events = load_events()
    labels = load_labels(events)
    first_ids = first_of_day_ids(events)
    first = [e for e in events if e.event_id in first_ids]
    first_shallow = [e for e in first if float(e.penetration_points) <= SHALLOW]
    all_shallow = [e for e in events if float(e.penetration_points) <= SHALLOW]

    files = index_trade_files()
    print(f"events={len(events)} first={len(first)} first_shallow={len(first_shallow)} trade_files={len(files)}", flush=True)

    bars_by_c: dict[str, list] = {}
    for e in events:
        c = str((e.extras or {}).get("contract") or "")
        if c and c not in bars_by_c:
            print(f"loading 1m {c}...", flush=True)
            bars_by_c[c] = load_contract_bars(c)

    grouped: dict[str, list[SweepEvent]] = defaultdict(list)
    missing = []
    for e in events:
        p = covering_trades(files, e)
        if p is None:
            missing.append(e.event_id)
            grouped[""].append(e)
        else:
            grouped[str(p)].append(e)

    import databento as db

    rows: list[dict[str, Any]] = []
    done = 0
    sample_signs: list[dict[str, Any]] = []
    for key, evs in grouped.items():
        recs: list = []
        if key:
            try:
                recs = list(db.DBNStore.from_file(key))
            except Exception as exc:  # noqa: BLE001
                print(f"DBN read failed {key}: {type(exc).__name__}:{exc}", flush=True)
                recs = []
        for e in evs:
            contract = str((e.extras or {}).get("contract") or "")
            rth = rth_bars(bars_by_c[contract], e.trading_date) if contract in bars_by_c else []
            rec_found = find_reclaim(e, rth, mode="close_1m", expiry_sec=300)
            reclaim_ts = None if rec_found is None else rec_found[1]
            feats = flow_from_trades(recs, e, reclaim_ts=reclaim_ts) if recs else {
                "has_trades": False,
                "n_trades_le_cutoff": 0,
                "opening_bar": 1.0 if int(e.seconds_from_rth_open) < 60 else 0.0,
            }
            lab = labels.get(e.event_id, "")
            row = {
                "event_id": e.event_id,
                "trading_date": e.trading_date,
                "side": e.side,
                "contract": contract,
                "penetration_points": float(e.penetration_points),
                "seconds_from_rth_open": int(e.seconds_from_rth_open),
                "first_of_day": e.event_id in first_ids,
                "shallow": float(e.penetration_points) <= SHALLOW,
                "label_300s": lab,
                "reclaim_ts": reclaim_ts,
                "trades_path": key,
                **feats,
            }
            rows.append(row)
            if len(sample_signs) < 8 and feats.get("has_trades") and e.event_id in first_ids:
                sample_signs.append({
                    "event_id": e.event_id,
                    "side": e.side,
                    "sweep60_abv": feats.get("sweep_60_abv"),
                    "sweep60_asv": feats.get("sweep_60_asv"),
                    "ndelta_rev_sweep60": feats.get("ndelta_rev_sweep60"),
                    "n_unsigned": feats.get("sweep_60_n_unsigned"),
                    "n_signed": int(feats.get("sweep_60_n_buy") or 0) + int(feats.get("sweep_60_n_sell") or 0),
                })
            done += 1
            if done % 25 == 0:
                print(f"flow features {done}/{len(events)}", flush=True)
        recs = []

    _write_csv(REPORTS / "phase37_flow_features.csv", rows)

    def _sel(pred) -> list[dict[str, Any]]:
        return [r for r in rows if pred(r)]

    usable = _sel(lambda r: r.get("has_trades"))
    first_rows = _sel(lambda r: r["first_of_day"])
    fs = _sel(lambda r: r["first_of_day"] and r["shallow"] and r.get("has_trades"))
    train_fs, hold_fs = [], []
    for r in fs:
        if r["trading_date"] <= TRAIN_END:
            train_fs.append(r)
        elif r["trading_date"] >= HOLDOUT_START:
            hold_fs.append(r)

    funnel = {
        "n_phase35_eligible": len(events),
        "n_first_sweeps": len(first),
        "n_any_shallow": len(all_shallow),
        "n_first_shallow": len(first_shallow),
        "n_trade_files": len(files),
        "n_missing_trade_file": len(missing),
        "missing_event_ids": missing,
        "n_usable_flow_all": len(usable),
        "n_first_shallow_with_flow": len(fs),
        "n_train_first_shallow_flow": len(train_fs),
        "n_holdout_first_shallow_flow": len(hold_fs),
        "dropped_silently": 0,
    }
    print(json.dumps(funnel, indent=2), flush=True)

    baseline_all = label_counts([labels[e.event_id] for e in events])
    baseline_first = label_counts([labels[e.event_id] for e in first])
    baseline_fs = label_counts([r["label_300s"] for r in fs])
    baseline_train = label_counts([r["label_300s"] for r in train_fs])
    baseline_hold = label_counts([r["label_300s"] for r in hold_fs])

    feat_eval_full = [eval_feature(fs, f) for f in FLOW_FEATS]
    feat_eval_train = [eval_feature(train_fs, f) for f in FLOW_FEATS]
    feat_eval_hold = [eval_feature(hold_fs, f) for f in FLOW_FEATS]
    _write_csv(REPORTS / "phase37_feature_splits.csv", feat_eval_full)

    # Conditional lift using TRAIN thresholds only
    cond_rows = []
    cond_ok_features = []
    base_hold_p = baseline_hold.get("p_reversal")
    base_train_p = baseline_train.get("p_reversal")
    for feat in FLOW_FEATS:
        high = expected_sign(feat) > 0
        pcts = (0.5, 0.6, 0.7, 0.8)
        if feat == "volume_burst":
            primary_pct = 0.7
        elif feat == "delta_divergence":
            primary_pct = None
        else:
            primary_pct = 0.5
        block_signs = []
        for i, (a, b) in enumerate(WF_BLOCKS):
            block = [r for r in fs if a <= r["trading_date"] <= b]
            if feat == "delta_divergence":
                hi = [r for r in block if (r.get(feat) or 0) > 0]
                lo = [r for r in block if (r.get(feat) or 0) <= 0]
            else:
                thr_b = train_threshold(train_fs, feat, primary_pct or 0.5)
                if thr_b is None:
                    continue
                hi = apply_split(block, feat, thr_b, high=high)
                lo = apply_split(block, feat, thr_b, high=not high)
            p_hi = None if not hi else sum(x["label_300s"] == "REVERSAL" for x in hi) / len(hi)
            p_lo = None if not lo else sum(x["label_300s"] == "REVERSAL" for x in lo) / len(lo)
            lift = None if p_hi is None or p_lo is None else p_hi - p_lo
            block_signs.append({"block": i + 1, "start": a, "end": b, "n_hi": len(hi), "n_lo": len(lo), "p_hi": p_hi, "p_lo": p_lo, "lift": lift})
        if feat == "delta_divergence":
            hi_tr = [r for r in train_fs if (r.get(feat) or 0) > 0]
            lo_tr = [r for r in train_fs if (r.get(feat) or 0) <= 0]
            hi_ho = [r for r in hold_fs if (r.get(feat) or 0) > 0]
            lo_ho = [r for r in hold_fs if (r.get(feat) or 0) <= 0]
            thr = 0.0
        else:
            thr = train_threshold(train_fs, feat, primary_pct or 0.5)
            hi_tr = apply_split(train_fs, feat, thr, high=high) if thr is not None else []
            lo_tr = apply_split(train_fs, feat, thr, high=not high) if thr is not None else []
            hi_ho = apply_split(hold_fs, feat, thr, high=high) if thr is not None else []
            lo_ho = apply_split(hold_fs, feat, thr, high=not high) if thr is not None else []
        def _p(xs):
            return None if not xs else sum(x["label_300s"] == "REVERSAL" for x in xs) / len(xs)
        train_lift = None if _p(hi_tr) is None or _p(lo_tr) is None else _p(hi_tr) - _p(lo_tr)
        hold_lift = None if _p(hi_ho) is None or _p(lo_ho) is None else _p(hi_ho) - _p(lo_ho)
        wf_same = 0
        for b in block_signs:
            if train_lift is None or b.get("lift") is None:
                continue
            if (train_lift > 0 and b["lift"] > 0) or (train_lift < 0 and b["lift"] < 0):
                wf_same += 1
        hold_vs_base = None if _p(hi_ho) is None or base_hold_p is None else _p(hi_ho) - base_hold_p
        ok = (
            train_lift is not None
            and hold_lift is not None
            and ((train_lift > 0 and hold_lift > 0) or (train_lift < 0 and hold_lift < 0))
            and abs(hold_lift) >= COND_LIFT
            and wf_same >= 3
        )
        if ok:
            cond_ok_features.append(feat)
        rec = {
            "feature": feat,
            "threshold_source": "train",
            "threshold": thr,
            "high_means_more_reversal": high,
            "n_train_hi": len(hi_tr),
            "n_train_lo": len(lo_tr),
            "p_train_hi": _p(hi_tr),
            "p_train_lo": _p(lo_tr),
            "train_lift": train_lift,
            "n_hold_hi": len(hi_ho),
            "n_hold_lo": len(lo_ho),
            "p_hold_hi": _p(hi_ho),
            "p_hold_lo": _p(lo_ho),
            "hold_lift": hold_lift,
            "hold_hi_minus_shallow_baseline": hold_vs_base,
            "wf_same_sign": wf_same,
            "walkforward": block_signs,
            "conditional_ok": ok,
        }
        cond_rows.append(rec)
        neigh = []
        if feat != "delta_divergence":
            for p in pcts:
                t2 = train_threshold(train_fs, feat, p)
                if t2 is None:
                    continue
                hi2 = apply_split(hold_fs, feat, t2, high=high)
                lo2 = apply_split(hold_fs, feat, t2, high=not high)
                neigh.append({
                    "percentile": p,
                    "threshold": t2,
                    "n_hi": len(hi2),
                    "n_lo": len(lo2),
                    "p_hi": _p(hi2),
                    "p_lo": _p(lo2),
                    "lift": None if _p(hi2) is None or _p(lo2) is None else _p(hi2) - _p(lo2),
                })
        rec["neighbor_holdout"] = neigh
    _write_csv(REPORTS / "phase37_conditional_lift.csv", [{k: v for k, v in r.items() if k != "walkforward"} for r in cond_rows])

    leak_eval = [eval_feature(fs, f) for f in LEAK_FEATS]

    models = {
        "model0_intercept": logit_run(train_fs, hold_fs, [], "intercept"),
        "model1_pen": logit_run(train_fs, hold_fs, ["penetration_points"], "penetration"),
        "model2_struct": logit_run(train_fs, hold_fs, ["penetration_points", "seconds_from_rth_open", "opening_bar"], "pen+tod+openbar"),
        "model3_ndelta": logit_run(train_fs, hold_fs, ["penetration_points", "seconds_from_rth_open", "opening_bar", "ndelta_rev_sweep60"], "struct+ndelta"),
        "model4_burst": logit_run(train_fs, hold_fs, ["penetration_points", "seconds_from_rth_open", "opening_bar", "volume_burst"], "struct+burst"),
        "model5_eff": logit_run(train_fs, hold_fs, ["penetration_points", "seconds_from_rth_open", "opening_bar", "flow_efficiency"], "struct+efficiency"),
        "model6_flow_set": logit_run(
            train_fs, hold_fs,
            ["penetration_points", "seconds_from_rth_open", "opening_bar", "ndelta_rev_sweep60", "volume_burst", "exhaustion_score"],
            "struct+ndelta+burst+exhaustion",
        ),
    }
    (REPORTS / "phase37_incremental.json").write_text(json.dumps(models, indent=2), encoding="utf-8")
    m0 = models["model0_intercept"]
    m2 = models["model2_struct"]
    m6 = models["model6_flow_set"]
    brier_delta = None
    brier_vs_intercept = None
    if m2.get("holdout_brier") is not None and m6.get("holdout_brier") is not None:
        brier_delta = m2["holdout_brier"] - m6["holdout_brier"]
    if m0.get("holdout_brier") is not None and m6.get("holdout_brier") is not None:
        brier_vs_intercept = m0["holdout_brier"] - m6["holdout_brier"]
    brier_ok = brier_delta is not None and brier_delta >= BRIER_LIFT

    # Side / session
    pdl = [r for r in fs if r["side"] == "pdl_sweep"]
    pdh = [r for r in fs if r["side"] == "pdh_sweep"]
    side_rows = [
        {"slice": "pdl_long_reversal", **label_counts([r["label_300s"] for r in pdl]), **eval_feature(pdl, "ndelta_rev_sweep60")},
        {"slice": "pdh_short_reversal", **label_counts([r["label_300s"] for r in pdh]), **eval_feature(pdh, "ndelta_rev_sweep60")},
    ]
    _write_csv(REPORTS / "phase37_side.csv", side_rows)
    sess_rows = []
    for kind in ("first_bar", "first_30m", "first_60m", "later"):
        sub = session_slice(fs, kind)
        sess_rows.append({"slice": kind, **label_counts([r["label_300s"] for r in sub]), "mean_volume_burst": None if not sub else statistics.mean(_num(r.get("volume_burst")) or 0 for r in sub)})
    _write_csv(REPORTS / "phase37_session.csv", sess_rows)

    wf_base = []
    for i, (a, b) in enumerate(WF_BLOCKS):
        block = [r for r in fs if a <= r["trading_date"] <= b]
        wf_base.append({"block": i + 1, "start": a, "end": b, **label_counts([r["label_300s"] for r in block])})
    _write_csv(REPORTS / "phase37_walkforward.csv", wf_base)

    classification_gate = {
        "brier_ok": brier_ok,
        "brier_delta_model2_minus_model6": brier_delta,
        "brier_delta_intercept_minus_model6": brier_vs_intercept,
        "required_brier_lift": BRIER_LIFT,
        "conditional_ok": bool(cond_ok_features),
        "conditional_ok_features": cond_ok_features,
        "required_conditional_lift": COND_LIFT,
        "note": "Predeclared Brier gate is Model 6 vs Model 2. Intercept-only is also reported; beating a worse structural model is not by itself an edge.",
    }

    strategy: dict[str, Any] = {"built": False, "reason": "classification_gate_failed"}
    if classification_gate["brier_ok"] and classification_gate["conditional_ok"]:
        # Confirming feature: first passing leak-safe feature, TRAIN threshold, high side
        feat = cond_ok_features[0]
        high = expected_sign(feat) > 0
        if feat == "delta_divergence":
            confirmed_ids = {r["event_id"] for r in fs if (r.get(feat) or 0) > 0}
            thr = 0.0
        else:
            pct = 0.7 if feat == "volume_burst" else 0.5
            thr = train_threshold(train_fs, feat, pct)
            confirmed_ids = {r["event_id"] for r in apply_split(fs, feat, thr, high=high)}
        conf_events = [e for e in first_shallow if e.event_id in confirmed_ids]
        print(f"classification gate passed; building trades on {feat} n={len(conf_events)}", flush=True)
        hold_ids = {r["event_id"] for r in hold_fs}
        fills = {}
        trades_primary = None
        for ticks in (0.0, 1.0, 2.0):
            tb = run_set(
                conf_events, bars_by_c, first_ids,
                candidate="B", reclaim_mode="close_1m", expiry_sec=300,
                sl_buffer_ticks=1, target_r=1.5, entry_adverse_ticks=ticks,
                first_only=True, shallow_max=SHALLOW, require_shallow=True,
            )
            fills[f"{int(ticks)}_tick"] = {
                "full": score(tb),
                "holdout": score([t for t in tb if t.event_id in hold_ids]),
            }
            if ticks == 1.0:
                trades_primary = tb
        trades_b = trades_primary or []
        sc_full = fills["1_tick"]["full"]
        sc_hold = fills["1_tick"]["holdout"]
        trades_a = run_set(
            conf_events, bars_by_c, first_ids,
            candidate="A", reclaim_mode="range_1m", expiry_sec=300,
            sl_buffer_ticks=1, target_r=1.5, entry_adverse_ticks=1.0,
            first_only=True, shallow_max=SHALLOW, require_shallow=True,
        )
        portfolio = dvp_compare([t for t in trades_b if t.status == "ENTERED"])
        gc_paper = ROOT / "journal" / "phase26_gc_vwap_v2_paper" / "paper_trades.jsonl"
        strategy = {
            "built": True,
            "confirm_feature": feat,
            "threshold": thr,
            "n_confirmed_setups": len(conf_events),
            "primary": {
                "full": sc_full,
                "holdout": sc_hold,
                "full_expectancy_r": sc_full.get("expectancy_r"),
                "holdout_expectancy_r": sc_hold.get("expectancy_r"),
            },
            "fill_stress": fills,
            "candidate_A_1tick": score(trades_a),
            "portfolio_dvp": portfolio,
            "gc_v2": {
                "gc_paper_empty": gc_paper.exists() and gc_paper.stat().st_size == 0,
                "note": "GC paper journal empty. Loss-day overlap undefined.",
            },
        }
        JOURNAL.mkdir(parents=True, exist_ok=True)
        with (JOURNAL / "trades_flow.jsonl").open("w", encoding="utf-8") as fh:
            for t in trades_b:
                fh.write(json.dumps(t.to_dict(), default=str) + "\n")
        _write_csv(REPORTS / "phase37_trades_primary.csv", [t.to_dict() for t in trades_b])
        _write_csv(REPORTS / "phase37_fill_stress.csv", [
            {"overlay": k, "split": sk, **sv}
            for k, splits in fills.items()
            for sk, sv in splits.items()
        ])

    frozen_after = assert_frozen()
    payload: dict[str, Any] = {
        "ok": frozen_after["ok"] and frozen_before["ok"],
        "phase": 37,
        "status": "RESEARCH_COMPLETE",
        "execution": "DRY_RUN_NO_BROKER",
        "frozen_before": {**frozen_before, "gc": FROZEN_GC_HASH, "nq": FROZEN_NQ_HASH},
        "frozen_after": {**frozen_after, "gc": FROZEN_GC_HASH, "nq": FROZEN_NQ_HASH},
        "file_sha": {
            "gc": file_sha256(ROOT / "strategy_frozen" / "gc_vwap_v2_phase26.json"),
            "nq": file_sha256(ROOT / "strategy_frozen" / "nq_dvp_phase30.json"),
            "gc_expected": GC_FILE_SHA,
            "nq_expected": NQ_FILE_SHA,
        },
        "spec": spec,
        "funnel": funnel,
        "signing_sample": sample_signs,
        "baseline": {
            "all_phase35": baseline_all,
            "first_sweeps": baseline_first,
            "first_shallow_with_flow": baseline_fs,
            "train": baseline_train,
            "holdout": baseline_hold,
        },
        "feature_eval_full": feat_eval_full,
        "feature_eval_train": feat_eval_train,
        "feature_eval_holdout": feat_eval_hold,
        "leak_diagnostic_eval": leak_eval,
        "conditional_lift": cond_rows,
        "models": models,
        "classification_gate": classification_gate,
        "side": side_rows,
        "session": sess_rows,
        "walkforward_baseline": wf_base,
        "strategy": strategy,
        "candidate_written": False,
    }
    verdict, branch = decide(payload)
    payload["verdict"] = verdict
    payload["branch_recommendation"] = branch
    VALIDATION.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    print(json.dumps({"verdict": verdict, "branch": branch, "gate": classification_gate, "funnel": funnel}, indent=2), flush=True)
    return payload


if __name__ == "__main__":
    main()
    sys.exit(0)
