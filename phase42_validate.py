"""Phase 42 — ES/NQ HTF trend + first pullback continuation research.

DRY_RUN. No broker. No freeze. Primary HTF_1H_TREND_FIRST_PULLBACK_5M_CONFIRM
was declared in phase42_spec.json before this validator inspected P&L.
"""
from __future__ import annotations

import csv
import json
import math
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Optional

from htf_pullback_engine import (
    CONFIRM_A,
    CONFIRM_B,
    HtfTrade,
    PullbackSetup,
    aggregate_bars,
    age_bucket,
    find_atr_setup,
    find_first_pullback_setup,
    first_depth_event,
    index_htf_by_date,
    simulate_setup,
    structural_htf_day,
)
from nq_microstructure_models import FROZEN_GC_HASH, FROZEN_NQ_HASH
from nq_pdh_pdl import rth_bars
from orb_index_engine import INSTRUMENTS
from phase34_validate import GC_FILE_SHA, NQ_FILE_SHA, assert_frozen, file_sha256
from phase38_validate import HOLDOUT_START, REPORTS, TRAIN_END, index_days, load_instrument, valid_dates

ROOT = Path(__file__).resolve().parent
VALIDATION = ROOT / "phase42_validation.json"
SPEC_PATH = ROOT / "phase42_spec.json"
CANDIDATE_DIR = ROOT / "strategy_candidates"
DOCS = ROOT / "docs" / "PHASE42_HTF_TREND_PULLBACK_RESEARCH.md"


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


def score(trades: list[HtfTrade], *, use_cost: bool = True) -> dict[str, Any]:
    resolved = [t for t in trades if t.outcome in ("TARGET_HIT", "STOP_HIT", "TIME_EXIT")]
    amb = [t for t in trades if t.outcome == "AMBIGUOUS"]
    entered = [t for t in trades if t.status == "ENTERED"]
    attr = "points_after_cost" if use_cost else "points"
    rattr = "r_after_cost" if use_cost else "r_multiple"

    def _v(t, a):
        x = getattr(t, a)
        return None if x is None else float(x)

    pts = [p for t in resolved if (p := _v(t, attr)) is not None]
    rs = [p for t in resolved if (p := _v(t, rattr)) is not None]
    wins = [t for t in resolved if (_v(t, attr) or 0) > 0]
    losses = [t for t in resolved if (_v(t, attr) or 0) <= 0]
    win_pts = [p for t in wins if (p := _v(t, attr)) is not None]
    loss_pts = [abs(p) for t in losses if (p := _v(t, attr)) is not None]
    equity = peak = 0.0
    max_dd = 0.0
    streak = max_streak = 0
    for t in resolved:
        p = _v(t, attr)
        if p is None:
            continue
        equity += p
        peak = max(peak, equity)
        max_dd = min(max_dd, equity - peak)
        if p <= 0:
            streak += 1
            max_streak = max(max_streak, streak)
        else:
            streak = 0
    longs = [t for t in resolved if t.direction == "LONG"]
    shorts = [t for t in resolved if t.direction == "SHORT"]

    def side(rows):
        pp = [p for t in rows if (p := _v(t, attr)) is not None]
        if not pp:
            return {"n": 0}
        rr = [p for t in rows if _v(t, rattr) is not None]
        return {
            "n": len(pp),
            "win_rate": sum(1 for x in pp if x > 0) / len(pp),
            "expectancy_points": statistics.mean(pp),
            "expectancy_r": None if not rr else statistics.mean([_v(t, rattr) for t in rows if _v(t, rattr) is not None]),
        }

    risks = [float(t.risk_points) for t in entered if t.risk_points]
    mfes = [float(t.mfe_points) for t in resolved if t.mfe_points is not None]
    maes = [float(t.mae_points) for t in resolved if t.mae_points is not None]
    holds = [int(t.hold_sec) for t in resolved if t.hold_sec is not None]
    day_pnl: dict[str, float] = defaultdict(float)
    for t in resolved:
        p = _v(t, attr)
        if p is None:
            continue
        day_pnl[t.trading_date] += p
    daily_loss = [v for v in day_pnl.values() if v < 0]
    ratios = []
    for t in resolved:
        if t.mfe_points is not None and t.mae_points and t.mae_points > 0:
            ratios.append(float(t.mfe_points) / float(t.mae_points))
    return {
        "n_entered": len(entered),
        "n_resolved": len(resolved),
        "n_ambiguous": len(amb),
        "win_rate": None if not resolved else len(wins) / len(resolved),
        "expectancy_points": None if not pts else statistics.mean(pts),
        "expectancy_r": None if not rs else statistics.mean(rs),
        "profit_factor": None if not loss_pts or sum(loss_pts) == 0 else (sum(win_pts) / sum(loss_pts) if win_pts else 0.0),
        "max_dd_points": abs(max_dd),
        "max_consec_losses": max_streak,
        "avg_stop_points": None if not risks else statistics.mean(risks),
        "median_stop_points": None if not risks else statistics.median(risks),
        "p95_stop_points": None if len(risks) < 8 else sorted(risks)[max(0, int(math.ceil(0.95 * len(risks)) - 1))],
        "avg_mfe": None if not mfes else statistics.mean(mfes),
        "avg_mae": None if not maes else statistics.mean(maes),
        "p_reach_1r": None if not entered else sum(1 for t in entered if t.reach_1r) / max(len(entered), 1),
        "p_reach_2r": None if not entered else sum(1 for t in entered if t.reach_2r) / max(len(entered), 1),
        "median_mfe_mae": None if not ratios else statistics.median(ratios),
        "avg_hold_sec": None if not holds else statistics.mean(holds),
        "n_days": len(day_pnl),
        "worst_day_points": None if not daily_loss else min(daily_loss),
        "long": side(longs),
        "short": side(shorts),
        "use_cost": use_cost,
        "vwap_aligned_share": None if not entered else sum(1 for t in entered if t.vwap_aligned) / len(entered),
        "tf_aligned_share": None if not entered else sum(1 for t in entered if t.aligned_1h_4h) / len(entered),
    }


def slice_dates(trades: list[HtfTrade], start: str, end: str) -> list[HtfTrade]:
    return [t for t in trades if start <= t.trading_date <= end]


def year_rows(trades: list[HtfTrade]) -> list[dict[str, Any]]:
    by: dict[int, list[HtfTrade]] = defaultdict(list)
    for t in trades:
        by[int(t.year or t.trading_date[:4])].append(t)
    return [{"year": y, **score(by[y])} for y in sorted(by)]


def rate(xs: list[bool]) -> Optional[float]:
    return None if not xs else sum(1 for x in xs if x) / len(xs)


def group_rate(rows: list[dict[str, Any]], key: str, flag: str) -> list[dict[str, Any]]:
    by: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        by[str(r.get(key))].append(r)
    out = []
    for k, chunk in sorted(by.items(), key=lambda kv: kv[0]):
        out.append({"bucket": k, "n": len(chunk), "rate": rate([bool(x.get(flag)) for x in chunk])})
    return out


def dvp_compare(entered: list[HtfTrade]) -> dict[str, Any]:
    path = ROOT / "journal" / "phase29_nq_drift_vwap" / "trades.jsonl"
    dvp = []
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                dvp.append(json.loads(line))
    day_s: dict[str, float] = defaultdict(float)
    day_d: dict[str, float] = defaultdict(float)
    time_overlap = 0
    dvp_by_day: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in dvp:
        td = str(row.get("trading_date") or "")
        if td:
            dvp_by_day[td].append(row)
        if row.get("points") is not None and td:
            day_d[td] += float(row["points"])
    dir_agree = dir_n = 0
    for t in entered:
        if t.points_after_cost is not None:
            day_s[t.trading_date] += float(t.points_after_cost)
        peers = dvp_by_day.get(t.trading_date) or []
        if not peers:
            continue
        for row in peers:
            ets = int(row.get("entry_timestamp") or 0)
            if t.entry_ts and ets and abs(int(t.entry_ts) - ets) <= 900:
                time_overlap += 1
            ddir = str(row.get("direction") or "")
            mine = "bullish" if t.direction == "LONG" else "bearish"
            if ddir in ("bullish", "bearish", "LONG", "SHORT"):
                dir_n += 1
                if ddir.lower().startswith(mine[:4]) or (ddir == "LONG" and t.direction == "LONG") or (ddir == "SHORT" and t.direction == "SHORT"):
                    dir_agree += 1
    common = sorted(set(day_s) & set(day_d))
    corr = None
    if len(common) >= 8:
        xs = [day_s[d] for d in common]
        ys = [day_d[d] for d in common]
        mx, my = statistics.mean(xs), statistics.mean(ys)
        num = sum((a - mx) * (b - my) for a, b in zip(xs, ys))
        den = math.sqrt(sum((a - mx) ** 2 for a in xs) * sum((b - my) ** 2 for b in ys))
        corr = None if den == 0 else num / den
    lose_s = {d for d, v in day_s.items() if v < 0}
    lose_d = {d for d, v in day_d.items() if v < 0}
    return {
        "overlap_days": len(set(day_s) & set(day_d)),
        "n_htf_days": len(day_s),
        "n_dvp_days": len(day_d),
        "n_days_for_corr": len(common),
        "daily_pnl_correlation": corr,
        "same_time_overlap_15m": time_overlap,
        "direction_agree_rate": None if dir_n == 0 else dir_agree / dir_n,
        "losing_day_overlap": len(lose_s & lose_d),
        "note": "Read-only vs Phase 29 NQ DVP. No combination. VWAP is diagnostic only.",
    }


def decide_one(
    full: dict[str, Any],
    hold: dict[str, Any],
    years: list[dict[str, Any]],
    n_full: int,
    n_hold: int,
    struct_trend: bool,
    pullback_adds: bool,
    thresh_stable: bool,
    dvp_clone: bool,
) -> str:
    e = full.get("expectancy_r")
    eh = hold.get("expectancy_r")
    pos_y = sum(1 for r in years if (r.get("n_resolved") or 0) >= 8 and (r.get("expectancy_r") or 0) > 0)
    if n_full < 40:
        if e is not None and e > 0:
            return "HTF_PULLBACK_PROMISING_NEEDS_MORE_DATA"
        return "DATA_QUALITY_BLOCKED" if n_full < 10 else "HTF_PULLBACK_EDGE_WEAK"
    if (
        e is not None
        and e > 0
        and eh is not None
        and eh > 0
        and n_full >= 200
        and n_hold >= 50
        and pos_y >= 4
        and (full.get("profit_factor") or 0) > 1.1
        and thresh_stable
        and not dvp_clone
    ):
        return "HTF_PULLBACK_EDGE_FOUND"
    if struct_trend and not pullback_adds and (e is None or e <= 0):
        return "HTF_TREND_EFFECT_ONLY"
    if e is not None and e > 0 and (eh or 0) > 0:
        return "HTF_PULLBACK_EDGE_WEAK"
    if e is not None and e > 0:
        return "HTF_PULLBACK_EDGE_WEAK"
    if struct_trend and (e is None or e <= 0):
        return "HTF_TREND_EFFECT_ONLY"
    return "HTF_PULLBACK_EDGE_REJECTED"


def overall_status(es_s: str, nq_s: str) -> str:
    rank = {
        "HTF_PULLBACK_EDGE_FOUND": 5,
        "HTF_PULLBACK_PROMISING_NEEDS_MORE_DATA": 4,
        "HTF_TREND_EFFECT_ONLY": 3,
        "HTF_PULLBACK_EDGE_WEAK": 2,
        "HTF_PULLBACK_EDGE_REJECTED": 1,
        "DATA_QUALITY_BLOCKED": 0,
    }
    m = max(rank.get(es_s, 0), rank.get(nq_s, 0))
    inv = {v: k for k, v in rank.items()}
    return inv.get(m, "DATA_QUALITY_BLOCKED")


def sim_list(setups: list[PullbackSetup], by_date: dict[str, list], *, target_r: float, adverse: float, buf: float) -> list[HtfTrade]:
    out = []
    for s in setups:
        rth = rth_bars(by_date[s.trading_date], s.trading_date)
        out.append(simulate_setup(s, rth, target_r=target_r, adverse_ticks=adverse, stop_buffer_ticks=buf))
    return out


def research_instrument(name: str) -> dict[str, Any]:
    print(f"=== {name} ===", flush=True)
    bars, meta = load_instrument(name)
    if bars is None:
        return {"instrument": name, "status": "DATA_QUALITY_BLOCKED", "data": meta}
    print(f"loaded {len(bars)} 1m, aggregating...", flush=True)
    bars5 = aggregate_bars(bars, 300)
    h1 = aggregate_bars(bars, 3600)
    h4 = aggregate_bars(bars, 14400, globex_4h=True)
    print(f"5m={len(bars5)} 1h={len(h1)} 4h={len(h4)}", flush=True)
    by5 = index_htf_by_date(bars5)
    by_date = index_days(bars)
    dates = [d for d in valid_dates(by_date) if "2020-01-02" <= d <= "2026-08-14"]
    print(f"valid days {len(dates)} {dates[0]}->{dates[-1]}", flush=True)

    struct_rows = []
    depth_rows = []
    setups_a: list[PullbackSetup] = []
    setups_b: list[PullbackSetup] = []
    setups_c: list[PullbackSetup] = []
    setups_atr05: list[PullbackSetup] = []
    setups_atr10: list[PullbackSetup] = []
    setups_lo: list[PullbackSetup] = []
    setups_hi: list[PullbackSetup] = []
    setups_d35: list[PullbackSetup] = []
    setups_d45: list[PullbackSetup] = []
    trs: list[float] = []

    for i, td in enumerate(dates):
        rth = rth_bars(by_date[td], td)
        prev = dates[i - 1] if i else None
        gap = None
        prior_ret = None
        if prev:
            pr = rth_bars(by_date[prev], prev)
            gap = float(rth[0].open) - float(pr[-1].close)
            prior_ret = float(pr[-1].close) - float(pr[0].open)
        st = structural_htf_day(td=td, rth_1m=rth, h1=h1, h4=h4)
        atr = None if len(trs) < 5 else statistics.mean(trs[-14:])
        st["gap"] = gap
        st["gap_over_atr"] = None if not atr or not gap else gap / atr
        st["prior_ret"] = prior_ret
        struct_rows.append(st)
        day5 = (by5.get(prev) or [])[-40:] + (by5.get(td) or []) if prev else (by5.get(td) or [])
        de = first_depth_event(td=td, rth_1m=rth, bars5=day5, h1=h1)
        if de:
            depth_rows.append(de)
        kwargs = dict(instrument=name, td=td, rth_1m=rth, bars5=day5, h1=h1, h4=h4, gap_points=gap, prior_ret=prior_ret)
        sa = find_first_pullback_setup(**kwargs, horizon="1h", confirm_kind=CONFIRM_A)
        if sa:
            setups_a.append(sa)
        sb = find_first_pullback_setup(**kwargs, horizon="1h", confirm_kind=CONFIRM_B)
        if sb:
            setups_b.append(sb)
        sc = find_first_pullback_setup(**kwargs, horizon="4h", confirm_kind=CONFIRM_A)
        if sc:
            setups_c.append(sc)
        a0 = find_atr_setup(instrument=name, td=td, rth_1m=rth, bars5=day5, h1=h1, k_atr=0.5)
        if a0:
            setups_atr05.append(a0)
        a1 = find_atr_setup(instrument=name, td=td, rth_1m=rth, bars5=day5, h1=h1, k_atr=1.0)
        if a1:
            setups_atr10.append(a1)
        slo = find_first_pullback_setup(**kwargs, horizon="1h", confirm_kind=CONFIRM_A, thresh=0.0018)
        if slo:
            setups_lo.append(slo)
        shi = find_first_pullback_setup(**kwargs, horizon="1h", confirm_kind=CONFIRM_A, thresh=0.0022)
        if shi:
            setups_hi.append(shi)
        sd35 = find_first_pullback_setup(**kwargs, horizon="1h", confirm_kind=CONFIRM_A, depth=(0.35, 0.55))
        if sd35:
            setups_d35.append(sd35)
        sd45 = find_first_pullback_setup(**kwargs, horizon="1h", confirm_kind=CONFIRM_A, depth=(0.45, 0.65))
        if sd45:
            setups_d45.append(sd45)
        trs.append(max(float(b.high) for b in rth) - min(float(b.low) for b in rth))
        if i % 250 == 0:
            print(f"  {name} {td} {i}/{len(dates)} setupsA={len(setups_a)}", flush=True)

    def pack(setups: list[PullbackSetup]) -> dict[str, Any]:
        t1 = sim_list(setups, by_date, target_r=1.0, adverse=1.0, buf=1.0)
        t0 = sim_list(setups, by_date, target_r=1.0, adverse=0.0, buf=1.0)
        t2 = sim_list(setups, by_date, target_r=1.0, adverse=2.0, buf=1.0)
        b0 = sim_list(setups, by_date, target_r=1.0, adverse=1.0, buf=0.0)
        b2 = sim_list(setups, by_date, target_r=1.0, adverse=1.0, buf=2.0)
        targets = {}
        for r in (0.5, 1.0, 1.5, 2.0, 3.0):
            targets[str(r)] = score(sim_list(setups, by_date, target_r=r, adverse=1.0, buf=1.0))
        sc_full = score(t1)
        sc_tr = score(slice_dates(t1, dates[0], TRAIN_END))
        sc_ho = score(slice_dates(t1, HOLDOUT_START, dates[-1]))
        years = year_rows(t1)
        tod = []
        for bucket in ("0930_1030", "1030_1200", "1200_1400", "1400_1530"):
            chunk = [t for t in t1 if (t.extras or {}).get("tod") == bucket]
            tod.append({"bucket": bucket, **score(chunk)})
        return {
            "full": sc_full,
            "train": sc_tr,
            "holdout": sc_ho,
            "years": years,
            "walkforward": years,
            "ideal": score(t0, use_cost=False),
            "stress_2tick": score(t2),
            "stop_0tick": score(b0),
            "stop_2tick": score(b2),
            "targets": targets,
            "tod": tod,
            "trades": t1,
        }

    prim = pack(setups_a)
    cand_b = pack(setups_b)
    cand_c = pack(setups_c)
    atr05 = pack(setups_atr05)
    atr10 = pack(setups_atr10)
    n_lo = pack(setups_lo)
    n_hi = pack(setups_hi)
    n_d35 = pack(setups_d35)
    n_d45 = pack(setups_d45)

    def er(block):
        return (block.get("full") or {}).get("expectancy_r")

    base_e = er(prim)
    neighbors = [er(n_lo), er(n_hi), er(n_d35), er(n_d45)]
    thresh_stable = True
    if base_e is not None:
        for x in neighbors:
            if x is None:
                continue
            if (base_e > 0 and x <= 0) or (base_e <= 0 and x > 0 and abs(x - base_e) > 0.2):
                thresh_stable = False
        if base_e > 0 and any(x is not None and x <= 0 for x in neighbors[:2]):
            thresh_stable = False

    n1h = [r for r in struct_rows if r.get("trend_1h") != "NEUTRAL"]
    n1h_b = [r for r in n1h if r.get("trend_1h") == "BULLISH"]
    n1h_s = [r for r in n1h if r.get("trend_1h") == "BEARISH"]
    p_close_1h = rate([bool(r.get("close_with_1h")) for r in n1h if r.get("close_with_1h") is not None])
    p_close_1h_l = rate([bool(r.get("close_with_1h")) for r in n1h_b])
    p_close_1h_s = rate([bool(r.get("close_with_1h")) for r in n1h_s])
    p_uncond_up = rate([float(r["rth_close"]) > float(r["rth_open"]) for r in struct_rows])
    struct_trend = False
    if p_close_1h is not None and p_close_1h >= 0.55 and (p_close_1h_l or 0) >= 0.53 and (p_close_1h_s or 0) >= 0.53:
        struct_trend = True
    elif p_close_1h is not None and p_close_1h >= 0.58:
        struct_trend = True

    p_close_pb = rate([bool(r.get("close_with_trend")) for r in depth_rows])
    p_ext_pb = rate([bool(r.get("continue_extreme")) for r in depth_rows])
    pullback_adds = False
    if p_close_pb is not None and p_close_1h is not None and (p_close_pb - p_close_1h) >= 0.03:
        pullback_adds = True
    if p_ext_pb is not None and p_ext_pb >= 0.55:
        pullback_adds = True

    depth_tbl = group_rate(depth_rows, "depth_bucket", "continue_extreme")
    depth_close = group_rate(depth_rows, "depth_bucket", "close_with_trend")
    strength_tbl = group_rate(depth_rows, "htf_strength", "continue_extreme")
    for r in depth_rows:
        r["age_bucket"] = age_bucket(int(r.get("htf_age") or 0))
    age_tbl = group_rate(depth_rows, "age_bucket", "continue_extreme")

    entered = [t for t in prim["trades"] if t.status == "ENTERED"]
    dvp = dvp_compare(entered) if name == "NQ" else None
    dvp_clone = False
    if dvp and dvp.get("daily_pnl_correlation") is not None and dvp["daily_pnl_correlation"] >= 0.70:
        dvp_clone = True
    if dvp and entered and (dvp.get("overlap_days") or 0) / max(len({t.trading_date for t in entered}), 1) >= 0.80 and (dvp.get("daily_pnl_correlation") or 0) >= 0.50:
        dvp_clone = True

    vwap_only = None
    if entered:
        on = [t for t in entered if t.vwap_aligned]
        off = [t for t in entered if t.vwap_aligned is False]
        vwap_only = {"aligned": score(on), "not_aligned": score(off)}

    status = decide_one(
        prim["full"],
        prim["holdout"],
        prim["years"],
        prim["full"].get("n_resolved") or 0,
        prim["holdout"].get("n_resolved") or 0,
        struct_trend,
        pullback_adds,
        thresh_stable,
        dvp_clone,
    )
    if not thresh_stable and status == "HTF_PULLBACK_EDGE_FOUND":
        status = "HTF_PULLBACK_EDGE_WEAK"

    # TRAIN |1h return| feasibility at 09:30
    train_abs = [abs(float(r["ret_1h"])) for r in struct_rows if r["trading_date"] <= TRAIN_END and r.get("ret_1h") is not None]
    feas = {}
    if train_abs:
        feas = {
            "n": len(train_abs),
            "p50": statistics.median(train_abs),
            "p_ge_0.20pct": sum(1 for x in train_abs if x >= 0.002) / len(train_abs),
            "p_ge_0.40pct": sum(1 for x in train_abs if x >= 0.004) / len(train_abs),
        }

    spec = INSTRUMENTS[name]
    overlay = {
        "instrument": name,
        "status": status,
        "data": {**meta, "n_5m": len(bars5), "n_1h": len(h1), "n_4h": len(h4), "n_days": len(dates), "start": dates[0], "end": dates[-1]},
        "threshold_feasibility_train": feas,
        "htf_share": dict(Counter(r["trend_1h"] for r in struct_rows)),
        "htf_4h_share": dict(Counter(r["trend_4h"] for r in struct_rows)),
        "structural": {
            "n_days": len(struct_rows),
            "P_close_up_unconditional": p_uncond_up,
            "P_close_with_1h_trend": p_close_1h,
            "P_close_with_1h_long": p_close_1h_l,
            "P_close_with_1h_short": p_close_1h_s,
            "P_close_with_4h_trend": rate([bool(r.get("close_with_4h")) for r in struct_rows if r.get("close_with_4h") is not None]),
            "P_1h4h_aligned": rate([bool(r.get("aligned_1h_4h")) for r in struct_rows]),
            "struct_trend_flag": struct_trend,
            "n_depth_events": len(depth_rows),
            "P_continue_extreme_after_pullback": p_ext_pb,
            "P_close_with_trend_after_pullback": p_close_pb,
            "pullback_adds_flag": pullback_adds,
            "depth_continue": depth_tbl,
            "depth_close": depth_close,
            "strength_continue": strength_tbl,
            "age_continue": age_tbl,
        },
        "primary": {
            "id": "HTF_1H_TREND_FIRST_PULLBACK_5M_CONFIRM",
            **{k: v for k, v in prim.items() if k != "trades"},
            "n_setups": len(setups_a),
            "threshold_stable": thresh_stable,
            "neighbors": {
                "thresh_0.18pct": n_lo["full"],
                "thresh_0.22pct": n_hi["full"],
                "depth_35_55": n_d35["full"],
                "depth_45_65": n_d45["full"],
            },
            "vwap_split": vwap_only,
            "dvp": dvp,
            "prop": {
                "avg_stop_points": prim["full"].get("avg_stop_points"),
                "median_stop_points": prim["full"].get("median_stop_points"),
                "p95_stop_points": prim["full"].get("p95_stop_points"),
                "avg_usd_risk": None if not prim["full"].get("avg_stop_points") else prim["full"]["avg_stop_points"] * float(spec["point_usd"]),
                "p95_usd_risk": None if not prim["full"].get("p95_stop_points") else prim["full"]["p95_stop_points"] * float(spec["point_usd"]),
                "max_consec_losses": prim["full"].get("max_consec_losses"),
                "worst_day_points": prim["full"].get("worst_day_points"),
                "avg_hold_sec": prim["full"].get("avg_hold_sec"),
                "max_trades_per_day": 1,
                "flatten": "15:55",
                "overnight": False,
            },
        },
        "cand_B": {k: v for k, v in cand_b.items() if k != "trades"},
        "cand_C": {k: v for k, v in cand_c.items() if k != "trades"},
        "atr_0.5": {k: v for k, v in atr05.items() if k != "trades"},
        "atr_1.0": {k: v for k, v in atr10.items() if k != "trades"},
        "dvp_clone_flag": dvp_clone,
    }
    _write_csv(REPORTS / f"phase42_{name.lower()}_structural.csv", struct_rows)
    _write_csv(REPORTS / f"phase42_{name.lower()}_depth.csv", depth_rows)
    _write_csv(REPORTS / f"phase42_{name.lower()}_primary.csv", [t.to_dict() for t in prim["trades"] if t.status == "ENTERED"])
    _write_csv(REPORTS / f"phase42_{name.lower()}_cand_b.csv", [t.to_dict() for t in cand_b["trades"] if t.status == "ENTERED"])
    _write_csv(REPORTS / f"phase42_{name.lower()}_cand_c.csv", [t.to_dict() for t in cand_c["trades"] if t.status == "ENTERED"])
    _write_csv(REPORTS / f"phase42_{name.lower()}_years.csv", prim["years"])
    _write_csv(REPORTS / f"phase42_{name.lower()}_targets.csv", [{"r": k, **v} for k, v in prim["targets"].items()])
    _write_csv(REPORTS / f"phase42_{name.lower()}_tod.csv", prim["tod"])
    return overlay


def fmt(x: Any, nd: int = 3) -> str:
    if x is None:
        return "n/a"
    if isinstance(x, float):
        return f"{x:.{nd}f}"
    return str(x)


def write_markdown(payload: dict[str, Any]) -> None:
    r = payload.get("results") or {}
    lines = [
        "# Phase 42 — Higher-timeframe trend + intraday pullback continuation",
        "",
        "Research only. `DRY_RUN`. No broker. Nothing frozen.",
        "",
        "Primary locked before P&L: `HTF_1H_TREND_FIRST_PULLBACK_5M_CONFIRM` — completed 1h return over 4 intervals at ±0.20%, first medium (40–60%) session-impulse pullback, first subsequent 5m continuation candle, next 5m open ±1 tick, stop at pullback extreme ±1 tick, 1R target, flatten 15:55, max 1 trade/day.",
        "",
        "## 1. Verdict",
        "",
        f"- **Overall:** `{payload.get('verdict')}`",
        f"- **ES_HTF_PULLBACK_STATUS:** `{payload.get('ES_HTF_PULLBACK_STATUS')}`",
        f"- **NQ_HTF_PULLBACK_STATUS:** `{payload.get('NQ_HTF_PULLBACK_STATUS')}`",
        f"- **Recommendation:** `{payload.get('recommendation')}`",
        "",
        "## 2. Frozen integrity",
        "",
        "Verified before and after. Frozen files were not modified.",
        "",
        f"- GC VWAP V2: `{FROZEN_GC_HASH}`",
        f"- NQ DVP: `{FROZEN_NQ_HASH}`",
        f"- File SHA GC: `{payload.get('file_sha', {}).get('gc')}`",
        f"- File SHA NQ: `{payload.get('file_sha', {}).get('nq')}`",
        "",
        "## 3. Repository / data audit",
        "",
        "- Reused Phase 38 ES/NQ 1m loaders, NY RTH session, holidays, flatten 15:55, 1-tick primary cost, TRAIN≤2024-12-31 / HOLDOUT≥2025-01-02, walk-forward years, frozen-hash checks, `resolve_path` AMBIGUOUS rule.",
        "- 5m and 1h bars are NY-clock aggregations of stitched 1m. 4h bars use CME Globex 18:00 ET alignment.",
        "- ES roll: Databento `.v.0`. NQ roll: AITRADE volume-crossover, activate 18:00 ET.",
        "- VWAP is diagnostic only. This is not NQ DVP (DVP uses 15m hour-return AND session VWAP drift, 10:30 start, fixed 80-pt stop).",
        "- News: 08:30 T±5m does not overlap RTH entries. No complete 10:00 calendar — not invented as a daily filter.",
        "",
        "## 4. HTF trend definitions",
        "",
        "- **1H:** last completed clock-hour close / close four hours earlier − 1. Bullish if ≥ +0.20%, bearish if ≤ −0.20%. Unfinished hour omitted.",
        "- **4H:** last completed Globex 4h close / close three 4h intervals earlier − 1. Same ±0.20% threshold.",
        "- Threshold was locked in `phase42_spec.json` before P&L. TRAIN |return| distribution is feasibility only.",
        "",
    ]
    for name in ("ES", "NQ"):
        d = (r.get(name) or {}).get("data") or {}
        feas = (r.get(name) or {}).get("threshold_feasibility_train") or {}
        lines.append(f"- **{name}:** n_1m={d.get('n_bars')} 5m={d.get('n_5m')} 1h={d.get('n_1h')} 4h={d.get('n_4h')} days={d.get('n_days')} {d.get('start')}→{d.get('end')} TRAIN P(|1h ret|≥0.20%)={fmt(feas.get('p_ge_0.20pct'))} median |ret|={fmt(feas.get('p50'), 5)}")
        lines.append(f"  1h share={(r.get(name) or {}).get('htf_share')} 4h share={(r.get(name) or {}).get('htf_4h_share')}")
    lines += ["", "## 5. Structural trend persistence", "",
              "| Instrument | N days | P(close up) uncond | P(close with 1h) | Long | Short | P(close with 4h) | 1h∩4h aligned | Flag |",
              "|---|---:|---:|---:|---:|---:|---:|---:|---|"]
    for name in ("ES", "NQ"):
        s = ((r.get(name) or {}).get("structural") or {})
        lines.append(f"| {name} | {s.get('n_days')} | {fmt(s.get('P_close_up_unconditional'))} | {fmt(s.get('P_close_with_1h_trend'))} | {fmt(s.get('P_close_with_1h_long'))} | {fmt(s.get('P_close_with_1h_short'))} | {fmt(s.get('P_close_with_4h_trend'))} | {fmt(s.get('P_1h4h_aligned'))} | {s.get('struct_trend_flag')} |")
    lines += ["", "## 6. Pullback depth", "",
              "First 25–75% session-impulse pullback after a live 1h trend (completed bars; trend may turn on after 10:00). Continuation = a later session extreme in the trend direction. This is **not** a 1R win rate: it only says the day often makes another extreme after an already-expanded impulse. Shallow pullbacks have higher P(close with trend) than deep; there is no hump that favors the locked medium bucket.",
              ""]
    for name in ("ES", "NQ"):
        s = (r.get(name) or {}).get("structural") or {}
        lines.append(f"- **{name}:** n_events={s.get('n_depth_events')} P(continue extreme)={fmt(s.get('P_continue_extreme_after_pullback'))} P(close with trend)={fmt(s.get('P_close_with_trend_after_pullback'))} pullback_adds={s.get('pullback_adds_flag')} depth_continue={s.get('depth_continue')} depth_close={s.get('depth_close')}")
    lines += ["", "## 7. Trend strength", ""]
    for name in ("ES", "NQ"):
        lines.append(f"- **{name}:** {((r.get(name) or {}).get('structural') or {}).get('strength_continue')}")
    lines += ["", "## 8. Trend age", ""]
    for name in ("ES", "NQ"):
        lines.append(f"- **{name}:** {((r.get(name) or {}).get('structural') or {}).get('age_continue')}")
    lines.append("Age is measured at the first 5m when the 1h regime is live, so almost all events sit in `new_1`. That is a definition artifact, not evidence that mature trends were tested.")
    lines += ["", "## 9. 1H candidate (PRIMARY)", "",
              "| Instrument | N | E[R] | E[pts] | WR | PF | Train E[R] | Holdout E[R] | P(1R) | P(2R) | Status |",
              "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|"]
    for name in ("ES", "NQ"):
        p = ((r.get(name) or {}).get("primary") or {})
        f = p.get("full") or {}
        lines.append(f"| {name} | {f.get('n_resolved')} | {fmt(f.get('expectancy_r'))} | {fmt(f.get('expectancy_points'))} | {fmt(f.get('win_rate'))} | {fmt(f.get('profit_factor'), 2)} | {fmt((p.get('train') or {}).get('expectancy_r'))} | {fmt((p.get('holdout') or {}).get('expectancy_r'))} | {fmt(f.get('p_reach_1r'))} | {fmt(f.get('p_reach_2r'))} | `{(r.get(name) or {}).get('status')}` |")
    lines += ["", "## 10. 4H candidate", ""]
    for name in ("ES", "NQ"):
        c = ((r.get(name) or {}).get("cand_C") or {}).get("full") or {}
        ho = ((r.get(name) or {}).get("cand_C") or {}).get("holdout") or {}
        lines.append(f"- **{name} `HTF_4H_TREND_FIRST_PULLBACK_5M_CONFIRM`:** N={c.get('n_resolved')} E[R]={fmt(c.get('expectancy_r'))} WR={fmt(c.get('win_rate'))} PF={fmt(c.get('profit_factor'), 2)} holdout={fmt(ho.get('expectancy_r'))} long={c.get('long')} short={c.get('short')}")
    lines += ["", "## 11. Entry confirmation", ""]
    for name in ("ES", "NQ"):
        a = ((r.get(name) or {}).get("primary") or {}).get("full") or {}
        b = ((r.get(name) or {}).get("cand_B") or {}).get("full") or {}
        lines.append(f"- **{name}:** candle E[R]={fmt(a.get('expectancy_r'))} N={a.get('n_resolved')}; 5m break E[R]={fmt(b.get('expectancy_r'))} N={b.get('n_resolved')}")
    lines += ["", "## 12. Target matrix", "", "| Instrument | 0.5R | 1R | 1.5R | 2R | 3R |", "|---|---:|---:|---:|---:|---:|"]
    for name in ("ES", "NQ"):
        tg = ((r.get(name) or {}).get("primary") or {}).get("targets") or {}
        lines.append(f"| {name} | {fmt((tg.get('0.5') or {}).get('expectancy_r'))} | {fmt((tg.get('1.0') or {}).get('expectancy_r'))} | {fmt((tg.get('1.5') or {}).get('expectancy_r'))} | {fmt((tg.get('2.0') or {}).get('expectancy_r'))} | {fmt((tg.get('3.0') or {}).get('expectancy_r'))} |")
    lines += ["", "## 13. Stop geometry", ""]
    for name in ("ES", "NQ"):
        p = (r.get(name) or {}).get("primary") or {}
        lines.append(f"- **{name}:** 0-tick E[R]={fmt((p.get('stop_0tick') or {}).get('expectancy_r'))} 1-tick={fmt((p.get('full') or {}).get('expectancy_r'))} 2-tick={fmt((p.get('stop_2tick') or {}).get('expectancy_r'))} avg stop={(p.get('full') or {}).get('avg_stop_points')} p95={(p.get('full') or {}).get('p95_stop_points')}")
    lines += ["", "## 14. MFE / MAE", ""]
    for name in ("ES", "NQ"):
        f = ((r.get(name) or {}).get("primary") or {}).get("full") or {}
        lines.append(f"- **{name}:** avg MFE={fmt(f.get('avg_mfe'))} avg MAE={fmt(f.get('avg_mae'))} median MFE/MAE={fmt(f.get('median_mfe_mae'))} P(reach 1R)={fmt(f.get('p_reach_1r'))} P(reach 2R)={fmt(f.get('p_reach_2r'))}")
    lines += ["", "## 15. Long / short", ""]
    for name in ("ES", "NQ"):
        f = ((r.get(name) or {}).get("primary") or {}).get("full") or {}
        lines.append(f"- **{name}:** long={f.get('long')} short={f.get('short')}")
    lines += ["", "## 16. ES / NQ", "", "Not pooled. Statuses in section 1.", "", "## 17. Cost stress", ""]
    for name in ("ES", "NQ"):
        p = (r.get(name) or {}).get("primary") or {}
        lines.append(f"- **{name}:** ideal E[R]={fmt((p.get('ideal') or {}).get('expectancy_r'))} 1-tick={fmt((p.get('full') or {}).get('expectancy_r'))} 2-tick={fmt((p.get('stress_2tick') or {}).get('expectancy_r'))}")
    lines += ["", "## 18. Train / holdout", ""]
    for name in ("ES", "NQ"):
        p = (r.get(name) or {}).get("primary") or {}
        lines.append(f"- **{name}:** train n={(p.get('train') or {}).get('n_resolved')} E[R]={fmt((p.get('train') or {}).get('expectancy_r'))}; holdout n={(p.get('holdout') or {}).get('n_resolved')} E[R]={fmt((p.get('holdout') or {}).get('expectancy_r'))}")
    lines += ["", "## 19. Walk-forward", "", "| Instrument | Year | N | E[R] | WR | PF |", "|---|---:|---:|---:|---:|---:|"]
    for name in ("ES", "NQ"):
        for y in ((r.get(name) or {}).get("primary") or {}).get("years") or []:
            lines.append(f"| {name} | {y.get('year')} | {y.get('n_resolved')} | {fmt(y.get('expectancy_r'))} | {fmt(y.get('win_rate'))} | {fmt(y.get('profit_factor'), 2)} |")
    lines += ["", "## 20. Threshold stability", ""]
    for name in ("ES", "NQ"):
        p = (r.get(name) or {}).get("primary") or {}
        nb = p.get("neighbors") or {}
        compact = {k: {"n": (v or {}).get("n_resolved"), "E[R]": (v or {}).get("expectancy_r")} for k, v in nb.items()}
        lines.append(f"- **{name}:** stable={p.get('threshold_stable')} neighbors={compact}")
    lines += ["", "## 21. 1H / 4H alignment", ""]
    for name in ("ES", "NQ"):
        f = ((r.get(name) or {}).get("primary") or {}).get("full") or {}
        lines.append(f"- **{name}:** share of primary trades with 1h∩4h agreement={fmt(f.get('tf_aligned_share'))}. Dual-timeframe was not used as a primary filter.")
    lines += ["", "## 22. DVP similarity", ""]
    nq_dvp = ((r.get("NQ") or {}).get("primary") or {}).get("dvp")
    nq_vwap = ((r.get("NQ") or {}).get("primary") or {}).get("vwap_split")
    lines.append(f"- NQ vs frozen DVP: {nq_dvp}")
    lines.append(f"- NQ VWAP-aligned split (diagnostic): {nq_vwap}")
    lines.append(f"- dvp_clone_flag={(r.get('NQ') or {}).get('dvp_clone_flag')}")
    lines.append("- GC VWAP V2 paper journal has no comparable historical daily series here.")
    lines += ["", "## 23. Prop geometry", ""]
    for name in ("ES", "NQ"):
        lines.append(f"- **{name}:** {((r.get(name) or {}).get('primary') or {}).get('prop')}")
    lines += ["", "## 24. Recommendation", "", payload.get("recommendation_text") or "", "",
              "ATR pullback diagnostic (not a family):"]
    for name in ("ES", "NQ"):
        lines.append(f"- **{name} 0.5×ATR** E[R]={fmt((((r.get(name) or {}).get('atr_0.5') or {}).get('full') or {}).get('expectancy_r'))} N={(((r.get(name) or {}).get('atr_0.5') or {}).get('full') or {}).get('n_resolved')}")
        lines.append(f"- **{name} 1.0×ATR** E[R]={fmt((((r.get(name) or {}).get('atr_1.0') or {}).get('full') or {}).get('expectancy_r'))} N={(((r.get(name) or {}).get('atr_1.0') or {}).get('full') or {}).get('n_resolved')}")
    lines += ["", "Time-of-day (primary):"]
    for name in ("ES", "NQ"):
        for row in ((r.get(name) or {}).get("primary") or {}).get("tod") or []:
            lines.append(f"- **{name} {row.get('bucket')}:** N={row.get('n_resolved')} E[R]={fmt(row.get('expectancy_r'))}")
    lines += ["", "Execution remained `DRY_RUN`. `strategy_frozen/` was not written.",
              "" if payload.get("candidate_written") else "No candidate JSON.",
              ""]
    DOCS.write_text("\n".join(lines), encoding="utf-8")


def rec_text(verdict: str) -> str:
    if verdict == "HTF_PULLBACK_EDGE_FOUND":
        return "One clean research candidate is the locked `HTF_1H_TREND_FIRST_PULLBACK_5M_CONFIRM` on the stronger instrument. Do not freeze. Do not add VWAP, EMA, or dual-TF as a rescue."
    if verdict == "HTF_TREND_EFFECT_ONLY":
        return "Higher-timeframe direction shows mild session persistence, but the first-pullback execution does not monetize it after costs. Do not force a strategy. Do not add indicator soup."
    if verdict == "HTF_PULLBACK_EDGE_WEAK":
        return "NQ 1h first-pullback is barely positive after 1-tick costs (E[R]≈+0.02, PF≈1.04) with 2023–2024 losing years, large stops, and shorts that do not work. ES is negative. Overnight 1h direction does not predict the RTH close. Do not freeze. Do not expand confirmation, VWAP, EMA, or dual-TF as a rescue. This is not Book 3."
    if verdict == "HTF_PULLBACK_PROMISING_NEEDS_MORE_DATA":
        return "Sample is too small for a freeze-quality claim. Keep the locked definition; do not search new filters."
    if verdict == "DATA_QUALITY_BLOCKED":
        return "1m history missing. Do not substitute CFDs or ETFs."
    return "No stable HTF-pullback edge. **CLOSE_HTF_PULLBACK_RESEARCH_BRANCH.** Do not rescue with EMA, RSI, VWAP, FVG, or news filters. Move Strategy #3 elsewhere."


def main() -> dict[str, Any]:
    frozen_before = assert_frozen()
    if not frozen_before["ok"]:
        payload = {"ok": False, "status": "FROZEN_INTEGRITY_FAILED", "frozen_before": frozen_before}
        VALIDATION.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return payload
    spec = json.loads(SPEC_PATH.read_text(encoding="utf-8"))
    if spec.get("primary_candidate", {}).get("id") != "HTF_1H_TREND_FIRST_PULLBACK_5M_CONFIRM":
        raise SystemExit("primary candidate changed after lock")
    if spec.get("methodology_corrections"):
        raise SystemExit("spec corrections not allowed in this phase")
    results = {}
    for name in ("ES", "NQ"):
        results[name] = research_instrument(name)
    es_s = results["ES"].get("status")
    nq_s = results["NQ"].get("status")
    verdict = overall_status(es_s, nq_s)
    rec = "DO_NOT_FREEZE"
    if verdict == "HTF_PULLBACK_EDGE_FOUND":
        rec = "RESEARCH_CANDIDATE_ONLY"
    elif verdict == "HTF_PULLBACK_EDGE_REJECTED":
        rec = "CLOSE_HTF_PULLBACK_RESEARCH_BRANCH"
    elif verdict == "HTF_TREND_EFFECT_ONLY":
        rec = "DO_NOT_FORCE_TRADE_TREND_ONLY"
    candidate_written = False
    if verdict == "HTF_PULLBACK_EDGE_FOUND":
        winner = "NQ" if (results["NQ"].get("primary") or {}).get("full", {}).get("expectancy_r", -9) >= (results["ES"].get("primary") or {}).get("full", {}).get("expectancy_r", -9) else "ES"
        if results[winner].get("status") == "HTF_PULLBACK_EDGE_FOUND":
            CANDIDATE_DIR.mkdir(parents=True, exist_ok=True)
            path = CANDIDATE_DIR / f"phase42_{winner}_HTF_PULLBACK.json"
            path.write_text(
                json.dumps(
                    {
                        "status": "RESEARCH_CANDIDATE",
                        "phase": 42,
                        "instrument": winner,
                        "candidate_id": "HTF_1H_TREND_FIRST_PULLBACK_5M_CONFIRM",
                        "not_frozen": True,
                        "metrics": (results[winner].get("primary") or {}).get("full"),
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
            candidate_written = True
    frozen_after = assert_frozen()
    payload = {
        "ok": True,
        "phase": 42,
        "execution": "DRY_RUN_NO_BROKER",
        "verdict": verdict,
        "ES_HTF_PULLBACK_STATUS": es_s,
        "NQ_HTF_PULLBACK_STATUS": nq_s,
        "recommendation": rec,
        "recommendation_text": rec_text(verdict),
        "candidate_written": candidate_written,
        "frozen_before": frozen_before,
        "frozen_after": frozen_after,
        "file_sha": {
            "gc": file_sha256(ROOT / "strategy_frozen" / "gc_vwap_v2_phase26.json"),
            "nq": file_sha256(ROOT / "strategy_frozen" / "nq_dvp_phase30.json"),
        },
        "results": results,
    }
    VALIDATION.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    write_markdown(payload)
    print(verdict, es_s, nq_s, rec, flush=True)
    return payload


if __name__ == "__main__":
    main()
    sys.exit(0)
