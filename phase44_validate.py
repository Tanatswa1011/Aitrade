"""Phase 44 — ES/NQ long-only bullish-state research.

DRY_RUN. No broker. No freeze. Primary LONG_STATE_20D_POSITIVE and
LONG20_FIRST_RED_GREEN_5M were declared in phase44_spec.json before P&L.
"""
from __future__ import annotations

import csv
import json
import math
import random
import statistics
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Optional

from long_only_engine import (
    LongTrade,
    build_states,
    find_atr_pullback,
    find_first_red_green,
    last_completed_state,
    simulate_mode2_long,
    simulate_open_long,
    simulate_red_green,
)
from nq_microstructure_models import FROZEN_GC_HASH, FROZEN_NQ_HASH
from nq_pdh_pdl import rth_bars
from orb_index_engine import INSTRUMENTS
from phase34_validate import GC_FILE_SHA, NQ_FILE_SHA, assert_frozen, file_sha256
from phase38_validate import index_days, load_instrument as load_1m, valid_dates
from phase40_validate import block_bootstrap_mean_ci, load_instrument as load_daily, score_trades, tstat
from tsmom_engine import SessionDay

ROOT = Path(__file__).resolve().parent
REPORTS = ROOT / "reports"
VALIDATION = ROOT / "phase44_validation.json"
SPEC_PATH = ROOT / "phase44_spec.json"
CANDIDATE_DIR = ROOT / "strategy_candidates"
DOCS = ROOT / "docs" / "PHASE44_LONG_ONLY_INDEX_DRIFT_RESEARCH.md"

TRAIN_END = "2022-12-30"
HOLDOUT_START = "2023-01-03"
TARGETS = [0.5, 1.0, 1.5, 2.0, 3.0]
TOD_EXITS = ("12:00", "14:00", "15:55")
BEAR_YEARS = {"2022"}


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


def _mean(xs: list[float]) -> Optional[float]:
    return None if not xs else statistics.mean(xs)


def _median(xs: list[float]) -> Optional[float]:
    return None if not xs else statistics.median(xs)


def rate(xs: list[bool]) -> Optional[float]:
    return None if not xs else sum(1 for x in xs if x) / len(xs)


def tertile_cuts(xs: list[float]) -> Optional[tuple[float, float]]:
    ys = sorted(xs)
    if len(ys) < 9:
        return None
    return ys[len(ys) // 3], ys[(2 * len(ys)) // 3]


def tertile_label(x: Optional[float], cuts: Optional[tuple[float, float]], *, positive_only: bool = False) -> Optional[str]:
    if x is None or cuts is None:
        return None
    if positive_only and x <= 0:
        return None
    a, b = cuts
    if x <= a:
        return "weak"
    if x <= b:
        return "medium"
    return "strong"


def gap_bucket(gap: Optional[float], tick: float) -> Optional[str]:
    if gap is None:
        return None
    if gap > tick:
        return "positive"
    if gap < -tick:
        return "negative"
    return "flat"


def close_third(loc: Optional[float]) -> Optional[str]:
    if loc is None:
        return None
    if loc < 1.0 / 3.0:
        return "lower"
    if loc < 2.0 / 3.0:
        return "middle"
    return "upper"


def st_flag(ret: Optional[float]) -> Optional[str]:
    if ret is None:
        return None
    return "negative" if ret < 0 else "positive"


def pct_below_252(days: list[SessionDay], i: int) -> Optional[float]:
    start = max(0, i - 251)
    hh = max(days[j].high for j in range(start, i + 1))
    if hh <= 0:
        return None
    return (hh - days[i].close) / hh


def score(trades: list[LongTrade], *, use_cost: bool = True) -> dict[str, Any]:
    resolved = [t for t in trades if t.outcome in ("TARGET_HIT", "STOP_HIT", "TIME_EXIT") or (t.status == "ENTERED" and t.points is not None and t.outcome != "AMBIGUOUS")]
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
    shorts = [t for t in resolved if t.direction == "SHORT"]
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
    p05 = sum(1 for t in entered if t.risk_points and t.mfe_points is not None and t.mfe_points >= 0.5 * t.risk_points)
    p10 = sum(1 for t in entered if t.reach_1r)
    p15 = sum(1 for t in entered if t.risk_points and t.mfe_points is not None and t.mfe_points >= 1.5 * t.risk_points)
    p20 = sum(1 for t in entered if t.reach_2r)
    p30 = sum(1 for t in entered if t.risk_points and t.mfe_points is not None and t.mfe_points >= 3.0 * t.risk_points)
    n_ent = max(len(entered), 1)
    return {
        "n_entered": len(entered),
        "n_resolved": len(resolved),
        "n_ambiguous": len(amb),
        "n_short": len(shorts),
        "win_rate": None if not pts else sum(1 for x in pts if x > 0) / len(pts),
        "expectancy_points": None if not pts else statistics.mean(pts),
        "expectancy_r": None if not rs else statistics.mean(rs),
        "total_points": None if not pts else sum(pts),
        "profit_factor": None if not loss_pts or sum(loss_pts) == 0 else (sum(win_pts) / sum(loss_pts) if win_pts else 0.0),
        "max_dd_points": abs(max_dd),
        "max_consec_losses": max_streak,
        "avg_stop_points": None if not risks else statistics.mean(risks),
        "median_stop_points": None if not risks else statistics.median(risks),
        "p95_stop_points": None if len(risks) < 8 else sorted(risks)[max(0, int(math.ceil(0.95 * len(risks)) - 1))],
        "avg_mfe": None if not mfes else statistics.mean(mfes),
        "avg_mae": None if not maes else statistics.mean(maes),
        "p_reach_0_5r": None if not entered else p05 / n_ent,
        "p_reach_1r": None if not entered else p10 / n_ent,
        "p_reach_1_5r": None if not entered else p15 / n_ent,
        "p_reach_2r": None if not entered else p20 / n_ent,
        "p_reach_3r": None if not entered else p30 / n_ent,
        "avg_hold_sec": None if not holds else statistics.mean(holds),
        "n_days": len(day_pnl),
        "worst_day_points": None if not daily_loss else min(daily_loss),
        "tstat": tstat(pts),
        "use_cost": use_cost,
    }


def slice_td(trades: list[LongTrade], start: str, end: str) -> list[LongTrade]:
    return [t for t in trades if start <= t.trading_date <= end]


def year_rows(trades: list[LongTrade]) -> list[dict[str, Any]]:
    by: dict[int, list[LongTrade]] = defaultdict(list)
    for t in trades:
        by[int(t.year or t.trading_date[:4])].append(t)
    return [{"year": y, **score(by[y])} for y in sorted(by)]


def calendar_stats(dates: list[str], day_pnl: dict[str, float]) -> dict[str, Any]:
    xs = [float(day_pnl.get(td, 0.0)) for td in dates]
    if not xs:
        return {"n_calendar": 0}
    equity = peak = 0.0
    max_dd = 0.0
    for x in xs:
        equity += x
        peak = max(peak, equity)
        max_dd = min(max_dd, equity - peak)
    active = [x for td, x in ((d, day_pnl.get(d)) for d in dates) if x is not None]
    n_active = sum(1 for td in dates if td in day_pnl)
    downside = [x for x in xs if x < 0]
    dd_dev = None
    if downside:
        dd_dev = (sum(x * x for x in downside) / len(xs)) ** 0.5
    return {
        "n_calendar": len(xs),
        "n_active": n_active,
        "exposure_pct": n_active / len(xs) if xs else None,
        "total_points": sum(xs),
        "mean_per_calendar_day": statistics.mean(xs),
        "mean_per_active_day": None if not active else statistics.mean(active),
        "max_dd_points": abs(max_dd),
        "downside_deviation": dd_dev,
        "hit_rate_calendar": sum(1 for x in xs if x > 0) / len(xs),
        "hit_rate_active": None if not active else sum(1 for x in active if x > 0) / len(active),
    }


def group_mean(rows: list[dict[str, Any]], key: str, val: str) -> list[dict[str, Any]]:
    by: dict[str, list[float]] = defaultdict(list)
    for r in rows:
        k = r.get(key)
        v = r.get(val)
        if k is None or v is None:
            continue
        by[str(k)].append(float(v))
    out = []
    for k, xs in sorted(by.items()):
        out.append({"bucket": k, "n": len(xs), "mean": statistics.mean(xs), "hit": sum(1 for x in xs if x > 0) / len(xs), "tstat": tstat(xs)})
    return out


def day_pnl_map(trades: list[LongTrade], *, use_cost: bool = True) -> dict[str, float]:
    attr = "points_after_cost" if use_cost else "points"
    out: dict[str, float] = defaultdict(float)
    for t in trades:
        if t.status != "ENTERED" or t.outcome == "AMBIGUOUS":
            continue
        p = getattr(t, attr)
        if p is None:
            continue
        out[t.trading_date] += float(p)
    return dict(out)


def dvp_compare(entered: list[LongTrade]) -> dict[str, Any]:
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
    dvp_long_days = set()
    for row in dvp:
        td = str(row.get("trading_date") or "")
        if td:
            dvp_by_day[td].append(row)
        if str(row.get("direction") or "").upper() == "LONG" and td:
            dvp_long_days.add(td)
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
            ddir = str(row.get("direction") or "").upper()
            if ddir:
                dir_n += 1
                if ddir == t.direction:
                    dir_agree += 1
    common = sorted(set(day_s) & set(day_d))
    corr = None
    if len(common) >= 8:
        xs = [day_s[d] for d in common]
        ys = [day_d[d] for d in common]
        mx, my = statistics.mean(xs), statistics.mean(ys)
        den = math.sqrt(sum((a - mx) ** 2 for a in xs) * sum((b - my) ** 2 for b in ys))
        corr = None if den == 0 else sum((a - mx) * (b - my) for a, b in zip(xs, ys)) / den
    strat_days = {t.trading_date for t in entered}
    lose_s = {d for d, v in day_s.items() if v < 0}
    lose_d = {d for d, v in day_d.items() if v < 0}
    overlap_days = len(strat_days & set(dvp_by_day))
    return {
        "n_dvp_trades": len(dvp),
        "n_strategy_days": len(strat_days),
        "overlap_days": overlap_days,
        "overlap_share": None if not strat_days else overlap_days / len(strat_days),
        "dvp_long_day_overlap_share": None if not strat_days else len(strat_days & dvp_long_days) / len(strat_days),
        "same_time_overlap_900s": time_overlap,
        "direction_agree": None if not dir_n else dir_agree / dir_n,
        "daily_pnl_correlation": corr,
        "losing_day_overlap": len(lose_s & lose_d),
        "n_common_pnl_days": len(common),
        "dvp_dependent": bool(strat_days) and overlap_days / max(len(strat_days), 1) >= 0.80 and (corr or 0) >= 0.50,
    }


def gc_compare(day_pnl: dict[str, float]) -> dict[str, Any]:
    path = ROOT / "journal" / "phase26_gc_vwap_v2_paper" / "paper_trades.jsonl"
    gc_day: dict[str, float] = defaultdict(float)
    n = 0
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            n += 1
            td = str(row.get("trading_date") or row.get("date") or "")
            pts = row.get("points") or row.get("points_after_cost")
            if td and pts is not None:
                gc_day[td] += float(pts)
    common = sorted(set(day_pnl) & set(gc_day))
    corr = None
    if len(common) >= 8:
        xs = [day_pnl[d] for d in common]
        ys = [gc_day[d] for d in common]
        mx, my = statistics.mean(xs), statistics.mean(ys)
        den = math.sqrt(sum((a - mx) ** 2 for a in xs) * sum((b - my) ** 2 for b in ys))
        corr = None if den == 0 else sum((a - mx) * (b - my) for a, b in zip(xs, ys)) / den
    return {
        "n_gc_paper": n,
        "overlap_days": len(set(day_pnl) & set(gc_day)),
        "daily_pnl_correlation": corr,
        "note": "Phase 26 paper journal is empty until forward paper trades exist." if n == 0 else None,
    }


def monte_carlo(trades: list[LongTrade], block: int = 5, n: int = 200) -> dict[str, Any]:
    xs = []
    for t in trades:
        if t.status != "ENTERED" or t.outcome == "AMBIGUOUS" or t.points_after_cost is None:
            continue
        xs.append(float(t.points_after_cost))
    if len(xs) < 20:
        return {"n": len(xs)}
    lo, hi = block_bootstrap_mean_ci(xs, block=block, n=n, seed=44)
    rng = random.Random(44)
    dds = []
    streaks = []
    B = max(2, int(block))
    starts = list(range(0, len(xs) - B + 1))
    need = len(xs)
    for _ in range(n):
        sample = []
        while len(sample) < need:
            st = starts[rng.randrange(len(starts))]
            sample.extend(xs[st : st + min(B, need - len(sample))])
        sample = sample[:need]
        eq = peak = 0.0
        dd = 0.0
        streak = mx = 0
        for v in sample:
            eq += v
            peak = max(peak, eq)
            dd = min(dd, eq - peak)
            if v <= 0:
                streak += 1
                mx = max(mx, streak)
            else:
                streak = 0
        dds.append(abs(dd))
        streaks.append(mx)
    dds.sort()
    streaks.sort()
    return {
        "n": len(xs),
        "block": block,
        "expectancy_ci95": [lo, hi],
        "max_dd_p50": dds[len(dds) // 2],
        "max_dd_p95": dds[int(0.95 * (len(dds) - 1))],
        "consec_loss_p50": streaks[len(streaks) // 2],
        "consec_loss_p95": streaks[int(0.95 * (len(streaks) - 1))],
    }


def pack_intraday(trades: list[LongTrade], dates: list[str]) -> dict[str, Any]:
    full = score(trades)
    train = score(slice_td(trades, dates[0] if dates else "1900-01-01", TRAIN_END))
    hold = score(slice_td(trades, HOLDOUT_START, dates[-1] if dates else "2100-01-01"))
    years = year_rows(trades)
    wf = [{"id": f"Y{y['year']}", "year": y["year"], **{k: v for k, v in y.items() if k != "year"}} for y in years]
    pnl = day_pnl_map(trades)
    cal = calendar_stats(dates, pnl)
    return {"full": full, "train": train, "holdout": hold, "years": years, "walkforward": wf, "calendar": cal, "day_pnl": pnl, "trades": trades}


def summarize_fwd(rows: list[dict[str, Any]], flag: str, val: str) -> dict[str, Any]:
    on = [float(r[val]) for r in rows if r.get(flag) is True and r.get(val) is not None]
    off = [float(r[val]) for r in rows if r.get(flag) is False and r.get(val) is not None]
    allv = [float(r[val]) for r in rows if r.get(val) is not None]
    diff = None if not on or not off else statistics.mean(on) - statistics.mean(off)
    return {
        "n_on": len(on),
        "n_off": len(off),
        "mean_on": _mean(on),
        "mean_off": _mean(off),
        "mean_all": _mean(allv),
        "hit_on": rate([x > 0 for x in on]),
        "hit_off": rate([x > 0 for x in off]),
        "hit_all": rate([x > 0 for x in allv]),
        "diff_on_minus_off": diff,
        "tstat_on": tstat(on),
        "tstat_off": tstat(off),
        "tstat_all": tstat(allv),
    }


def decide_one(
    *,
    coverage_ok: bool,
    n_days: int,
    open_full: dict[str, Any],
    open_hold: dict[str, Any],
    open_years: list[dict[str, Any]],
    pb_full: dict[str, Any],
    pb_hold: dict[str, Any],
    pb_years: list[dict[str, Any]],
    open_2tick: dict[str, Any],
    pb_2tick: dict[str, Any],
    always_cal: dict[str, Any],
    bull_cal: dict[str, Any],
    state_distinct: bool,
    filter_improves: bool,
    thresh_stable: bool,
    dvp_dependent: bool,
    n_short: int,
    baseline_negative: bool,
) -> str:
    if not coverage_ok or n_days < 200:
        return "DATA_QUALITY_BLOCKED"
    if n_short:
        return "LONG_ONLY_EDGE_REJECTED"
    e_open = open_full.get("expectancy_points")
    e_pb = pb_full.get("expectancy_points")
    pos_y_open = sum(1 for r in open_years if (r.get("n_resolved") or 0) >= 20 and (r.get("expectancy_points") or 0) > 0)
    y_open = sum(1 for r in open_years if (r.get("n_resolved") or 0) >= 20)
    pos_y_pb = sum(1 for r in pb_years if (r.get("n_resolved") or 0) >= 20 and (r.get("expectancy_points") or 0) > 0)
    y_pb = sum(1 for r in pb_years if (r.get("n_resolved") or 0) >= 20)
    open_ok = (
        (e_open or 0) > 0
        and (open_hold.get("expectancy_points") or 0) > 0
        and (open_full.get("n_resolved") or 0) >= 200
        and (open_2tick.get("expectancy_points") or 0) > 0
        and y_open >= 5
        and pos_y_open >= max(3, y_open // 2)
        and thresh_stable
        and not dvp_dependent
    )
    pb_ok = (
        (e_pb or 0) > 0
        and (pb_hold.get("expectancy_points") or 0) > 0
        and (pb_full.get("n_resolved") or 0) >= 150
        and (pb_2tick.get("expectancy_points") or 0) >= 0
        and y_pb >= 5
        and pos_y_pb >= max(3, y_pb // 2)
        and not dvp_dependent
    )
    if (open_ok or pb_ok) and filter_improves and state_distinct:
        n_hold = (pb_hold.get("n_resolved") or 0) if pb_ok else (open_hold.get("n_resolved") or 0)
        if n_hold < 40:
            return "LONG_ONLY_PROMISING_NEEDS_MORE_DATA"
        return "LONG_ONLY_EDGE_FOUND"
    if (e_open or 0) > 0 and not filter_improves:
        return "LONG_DRIFT_BETA_ONLY"
    if (e_open or 0) > 0 or (e_pb or 0) > 0:
        return "LONG_ONLY_EDGE_WEAK"
    if baseline_negative and (e_pb or 0) <= 0:
        return "LONG_ONLY_EDGE_REJECTED"
    return "LONG_ONLY_EDGE_REJECTED"


def research_instrument(name: str) -> dict[str, Any]:
    print(f"loading daily {name}", flush=True)
    days, dmeta = load_daily(name)
    if days is None:
        return {"instrument": name, "status": "DATA_QUALITY_BLOCKED", "data": dmeta}
    states = build_states(days)
    by_date_state = {s.date: s for s in states}
    bull20 = [bool(s.bull_20) if s.bull_20 is not None else False for s in states]
    always = [s.ret_20 is not None for s in states]
    mode2 = {}
    for hold in (1, 3, 5):
        t_bull = simulate_mode2_long(instrument=name, days=days, bull=bull20, hold=hold, adverse_ticks=1.0)
        t_all = simulate_mode2_long(instrument=name, days=days, bull=always, hold=hold, adverse_ticks=1.0)
        mode2[f"hold_{hold}"] = {
            "bullish": score_trades(t_bull),
            "always_long": score_trades(t_all),
            "bullish_train": score_trades([t for t in t_bull if t.entry_date <= TRAIN_END]),
            "bullish_holdout": score_trades([t for t in t_bull if t.entry_date >= HOLDOUT_START]),
        }
    print(f"  {name} daily n={len(days)} {days[0].date}->{days[-1].date} rolls={dmeta.get('n_roll_flags')}", flush=True)

    print(f"loading 1m {name}", flush=True)
    bars, meta = load_1m(name)
    if bars is None:
        return {"instrument": name, "status": "DATA_QUALITY_BLOCKED", "data": {**dmeta, **meta}, "mode2": mode2}
    by_date = index_days(bars)
    dates = valid_dates(by_date)
    tick = float(INSTRUMENTS[name]["tick"])
    spec = INSTRUMENTS[name]
    print(f"  {name} 1m days={len(dates)} {dates[0]}->{dates[-1]}", flush=True)

    pos20 = [float(s.ret_20) for s in states if s.ret_20 is not None and s.ret_20 > 0]
    cuts20 = tertile_cuts(pos20)
    rvs = [float(s.rv20) for s in states if s.rv20 is not None]
    cuts_rv = tertile_cuts(rvs)

    struct_rows: list[dict[str, Any]] = []
    always_trades: list[LongTrade] = []
    bull_open: list[LongTrade] = []
    bull_open0: list[LongTrade] = []
    bull_open2: list[LongTrade] = []
    neigh10: list[LongTrade] = []
    neigh60: list[LongTrade] = []
    neigh05: list[LongTrade] = []
    neigh10pct: list[LongTrade] = []
    tod_trades: dict[str, list[LongTrade]] = {k: [] for k in TOD_EXITS}
    pb1: list[LongTrade] = []
    pb0: list[LongTrade] = []
    pb2: list[LongTrade] = []
    pb_buf0: list[LongTrade] = []
    pb_buf2: list[LongTrade] = []
    pb_targets: dict[float, list[LongTrade]] = {r: [] for r in TARGETS}
    pb_atr: list[LongTrade] = []
    n_setups = n_tiny = n_news1000 = 0
    prev_rth_close = None
    same_day_daily = 0

    for td in dates:
        rth = rth_bars(by_date[td], td)
        if len(rth) < 350:
            prev_rth_close = float(rth[-1].close) if rth else prev_rth_close
            continue
        st = last_completed_state(states, td)
        if td in by_date_state:
            same_day_daily += 1
            assert st is None or st.date < td
        gap = None if prev_rth_close is None else float(rth[0].open) - prev_rth_close
        bull = bool(st and st.bull_20)
        row = {
            "instrument": name,
            "trading_date": td,
            "year": int(td[:4]),
            "bull_10": None if not st else st.bull_10,
            "bull_20": None if not st else st.bull_20,
            "bull_60": None if not st else st.bull_60,
            "bull_ema": None if not st else st.bull_ema,
            "bull_20_and_5": None if not st else st.bull_20_and_5,
            "ret_20": None if not st else st.ret_20,
            "ret_10": None if not st else st.ret_10,
            "ret_5": None if not st else st.ret_5,
            "prior_1d": None if not st else st.prior_1d,
            "prior_2d": None if not st else st.prior_2d,
            "prior_3d": None if not st else st.prior_3d,
            "st_1d": None if not st else st_flag(st.prior_1d),
            "st_2d": None if not st else st_flag(st.prior_2d),
            "st_3d": None if not st else st_flag(st.prior_3d),
            "dip_bucket": None if not st else st.dip_bucket,
            "close_third": None if not st else close_third(st.close_loc),
            "strength_20": None if not st else tertile_label(st.ret_20, cuts20, positive_only=True),
            "vol_bucket": None if not st else tertile_label(st.rv20, cuts_rv),
            "gap_bucket": gap_bucket(gap, tick),
            "gap_points": gap,
            "bear_year": td[:4] in BEAR_YEARS,
            "rth_open": float(rth[0].open),
            "rth_close": float(rth[-1].close),
            "rth_up": float(rth[-1].close) > float(rth[0].open),
        }
        al = simulate_open_long(instrument=name, td=td, rth=rth, candidate="ALWAYS_LONG_RTH")
        always_trades.append(al)
        row["always_pts"] = al.points_after_cost
        if bull:
            t1 = simulate_open_long(instrument=name, td=td, rth=rth)
            bull_open.append(t1)
            bull_open0.append(simulate_open_long(instrument=name, td=td, rth=rth, adverse_ticks=0.0))
            bull_open2.append(simulate_open_long(instrument=name, td=td, rth=rth, adverse_ticks=2.0))
            row["bull_open_pts"] = t1.points_after_cost
            for hh in TOD_EXITS:
                tod_trades[hh].append(simulate_open_long(instrument=name, td=td, rth=rth, flatten_hhmm=hh, candidate=f"OPEN_FLAT_{hh}"))
            if st and st.bull_10:
                neigh10.append(simulate_open_long(instrument=name, td=td, rth=rth, candidate="LONG_STATE_10D_POSITIVE"))
            if st and st.bull_60:
                neigh60.append(simulate_open_long(instrument=name, td=td, rth=rth, candidate="LONG_STATE_60D_POSITIVE"))
            if st and st.ret_20 is not None and st.ret_20 > 0.005:
                neigh05.append(simulate_open_long(instrument=name, td=td, rth=rth, candidate="LONG_STATE_20D_GT_0_5PCT"))
            if st and st.ret_20 is not None and st.ret_20 > 0.01:
                neigh10pct.append(simulate_open_long(instrument=name, td=td, rth=rth, candidate="LONG_STATE_20D_GT_1PCT"))
            setup = find_first_red_green(rth, td)
            if setup:
                n_setups += 1
                if setup.get("signal_hhmm"):
                    pass
                hhmm = None
                try:
                    from datetime import datetime
                    from zoneinfo import ZoneInfo
                    hhmm = datetime.fromtimestamp(int(setup["entry_ts"]), tz=ZoneInfo("America/New_York")).strftime("%H:%M")
                except Exception:
                    hhmm = None
                if hhmm and "09:55" <= hhmm <= "10:05":
                    n_news1000 += 1
                prim = simulate_red_green(instrument=name, td=td, rth=rth, setup=setup, target_r=1.0, adverse_ticks=1.0)
                if prim.status == "SKIP_TINY_RISK":
                    n_tiny += 1
                else:
                    pb1.append(prim)
                    pb0.append(simulate_red_green(instrument=name, td=td, rth=rth, setup=setup, target_r=1.0, adverse_ticks=0.0))
                    pb2.append(simulate_red_green(instrument=name, td=td, rth=rth, setup=setup, target_r=1.0, adverse_ticks=2.0))
                    pb_buf0.append(simulate_red_green(instrument=name, td=td, rth=rth, setup=setup, target_r=1.0, stop_buffer_ticks=0.0))
                    pb_buf2.append(simulate_red_green(instrument=name, td=td, rth=rth, setup=setup, target_r=1.0, stop_buffer_ticks=2.0))
                    for tr in TARGETS:
                        pb_targets[tr].append(simulate_red_green(instrument=name, td=td, rth=rth, setup=setup, target_r=tr))
            atr_s = find_atr_pullback(rth, td, 0.5)
            if atr_s:
                pb_atr.append(simulate_red_green(instrument=name, td=td, rth=rth, setup=atr_s, candidate="BULL_STATE_ATR_PULLBACK"))
        struct_rows.append(row)
        prev_rth_close = float(rth[-1].close)

    always_pack = pack_intraday(always_trades, dates)
    open_pack = pack_intraday(bull_open, dates)
    pb_pack = pack_intraday(pb1, dates)
    open_ideal = score(bull_open0, use_cost=False)
    open_2t = score(bull_open2)
    pb_ideal = score(pb0, use_cost=False)
    pb_2t = score(pb2)

    e20 = summarize_fwd(struct_rows, "bull_20", "always_pts")
    e10 = summarize_fwd(struct_rows, "bull_10", "always_pts")
    e60 = summarize_fwd(struct_rows, "bull_60", "always_pts")
    eema = summarize_fwd(struct_rows, "bull_ema", "always_pts")
    e25 = summarize_fwd(struct_rows, "bull_20_and_5", "always_pts")

    table_23 = []
    for b20 in (True, False):
        for st1 in ("positive", "negative"):
            chunk = [r for r in struct_rows if r.get("bull_20") is b20 and r.get("st_1d") == st1]
            xs = [float(r["always_pts"]) for r in chunk if r.get("always_pts") is not None]
            table_23.append({
                "bull_20": b20,
                "prior_1d": st1,
                "n": len(xs),
                "mean_rth_pts": _mean(xs),
                "hit": rate([x > 0 for x in xs]),
                "tstat": tstat(xs),
            })
    table_23d = []
    for b20 in (True, False):
        for st3 in ("positive", "negative"):
            chunk = [r for r in struct_rows if r.get("bull_20") is b20 and r.get("st_3d") == st3]
            xs = [float(r["always_pts"]) for r in chunk if r.get("always_pts") is not None]
            table_23d.append({
                "bull_20": b20,
                "prior_3d": st3,
                "n": len(xs),
                "mean_rth_pts": _mean(xs),
                "hit": rate([x > 0 for x in xs]),
                "tstat": tstat(xs),
            })

    cont = [r for r in struct_rows if r.get("bull_20") and r.get("dip_bucket") == "near_high"]
    rec = [r for r in struct_rows if r.get("bull_20") and r.get("dip_bucket") in ("modest_dip", "deep_dip")]

    def _xs(rows):
        return [float(r["always_pts"]) for r in rows if r.get("always_pts") is not None]

    continuation = {"n": len(cont), "mean_rth_pts": _mean(_xs(cont)), "hit": rate([x > 0 for x in _xs(cont)])}
    recovery = {"n": len(rec), "mean_rth_pts": _mean(_xs(rec)), "hit": rate([x > 0 for x in _xs(rec)])}

    always_cal = always_pack["calendar"]
    bull_cal = open_pack["calendar"]
    pb_cal = pb_pack["calendar"]
    always_dd = always_cal.get("max_dd_points") or 0
    bull_dd = bull_cal.get("max_dd_points") or 0
    always_mean = always_cal.get("mean_per_calendar_day") or 0
    bull_mean = bull_cal.get("mean_per_calendar_day") or 0
    always_calmar = None if not always_dd else (always_cal.get("total_points") or 0) / always_dd
    bull_calmar = None if not bull_dd else (bull_cal.get("total_points") or 0) / bull_dd
    state_distinct = bool(
        e20.get("diff_on_minus_off") is not None
        and e20["diff_on_minus_off"] > 0
        and (e20.get("tstat_on") or 0) > 0
        and abs(e20["diff_on_minus_off"]) >= 0.15 * abs(e20.get("mean_all") or 1e-9)
    )
    # Filter improves risk-adjusted calendar path vs always-long.
    filter_improves = False
    if always_calmar and bull_calmar:
        filter_improves = bull_calmar > always_calmar * 1.15 and bull_dd < always_dd * 0.90
    if (e20.get("diff_on_minus_off") or 0) <= 0:
        filter_improves = False

    e_open = open_pack["full"].get("expectancy_points")
    e10m = score(neigh10).get("expectancy_points")
    e60m = score(neigh60).get("expectancy_points")
    e05m = score(neigh05).get("expectancy_points")
    e1m = score(neigh10pct).get("expectancy_points")
    thresh_stable = True
    if e_open is not None and e_open > 0:
        for x in (e10m, e60m):
            if x is not None and x <= 0:
                thresh_stable = False
    baseline_negative = (e_open or 0) < 0

    entered_pb = [t for t in pb1 if t.status == "ENTERED"]
    dvp = dvp_compare(entered_pb) if name == "NQ" else None
    dvp_dep = bool(dvp and dvp.get("dvp_dependent"))
    gc = gc_compare(pb_pack["day_pnl"] or open_pack["day_pnl"])

    n_short = open_pack["full"].get("n_short") or 0
    n_short += pb_pack["full"].get("n_short") or 0
    coverage_ok = bool(dates) and dates[0] <= "2020-06-01" and dates[-1] >= "2025-12-01" and days[0].date <= "2011-01-01"

    status = decide_one(
        coverage_ok=coverage_ok,
        n_days=len(dates),
        open_full=open_pack["full"],
        open_hold=open_pack["holdout"],
        open_years=open_pack["years"],
        pb_full=pb_pack["full"],
        pb_hold=pb_pack["holdout"],
        pb_years=pb_pack["years"],
        open_2tick=open_2t,
        pb_2tick=pb_2t,
        always_cal=always_cal,
        bull_cal=bull_cal,
        state_distinct=state_distinct,
        filter_improves=filter_improves,
        thresh_stable=thresh_stable,
        dvp_dependent=dvp_dep,
        n_short=n_short,
        baseline_negative=baseline_negative,
    )

    bear_rows = [r for r in struct_rows if r.get("bear_year")]
    bear_bull_share = rate([bool(r.get("bull_20")) for r in bear_rows])
    bear_open = score([t for t in bull_open if t.trading_date.startswith("2022")])
    y2020 = score([t for t in bull_open if t.trading_date.startswith("2020")])

    news = {
        "bls_0830_pm5_removed": 0,
        "note": "CPI/NFP 08:30 +/- 5m never overlaps RTH 09:30 or later 5m entries. No complete 10:00 calendar applied.",
        "entries_09_55_to_10_05": n_news1000,
        "share_of_setups": None if not n_setups else n_news1000 / n_setups,
    }

    overlay = {
        "instrument": name,
        "status": status,
        "data": {
            **dmeta,
            **{k: meta.get(k) for k in ("ok", "n_bars", "path", "roll", "source")},
            "n_rth_days": len(dates),
            "rth_start": dates[0] if dates else None,
            "rth_end": dates[-1] if dates else None,
            "daily_start": days[0].date,
            "daily_end": days[-1].date,
            "n_daily": len(days),
            "n_rth_dates_with_same_calendar_daily_bar": same_day_daily,
            "same_day_daily_used": 0,
            "n_roll_flags": dmeta.get("n_roll_flags"),
        },
        "unconditional": {
            "rth_always_long": always_pack["full"],
            "rth_always_calendar": always_cal,
            "rth_always_train": always_pack["train"],
            "rth_always_holdout": always_pack["holdout"],
            "rth_always_years": always_pack["years"],
        },
        "state_forward": {"d10": e10, "d20": e20, "d60": e60, "ema20": eema, "d20_and_d5": e25},
        "primary_state": {
            "id": "LONG_STATE_20D_POSITIVE",
            "bull_share": rate([bool(r.get("bull_20")) for r in struct_rows]),
            "forward_rth": e20,
            "strength": group_mean(struct_rows, "strength_20", "always_pts"),
            "gap": group_mean([r for r in struct_rows if r.get("bull_20")], "gap_bucket", "always_pts"),
            "close_loc": group_mean([r for r in struct_rows if r.get("bull_20")], "close_third", "always_pts"),
            "vol": group_mean([r for r in struct_rows if r.get("bull_20")], "vol_bucket", "always_pts"),
            "dip": group_mean([r for r in struct_rows if r.get("bull_20")], "dip_bucket", "always_pts"),
            "st_1d": group_mean([r for r in struct_rows if r.get("bull_20")], "st_1d", "always_pts"),
            "st_2d": group_mean([r for r in struct_rows if r.get("bull_20")], "st_2d", "always_pts"),
            "st_3d": group_mean([r for r in struct_rows if r.get("bull_20")], "st_3d", "always_pts"),
        },
        "state_table_1d": table_23,
        "state_table_3d": table_23d,
        "continuation": continuation,
        "recovery": recovery,
        "mode2": mode2,
        "open_long": {
            "id": "BULL_STATE_RTH_OPEN_LONG",
            **{k: v for k, v in open_pack.items() if k not in ("trades", "day_pnl")},
            "ideal": open_ideal,
            "stress_2tick": open_2t,
            "neighbors": {
                "10d": score(neigh10),
                "60d": score(neigh60),
                "20d_gt_0.5pct": score(neigh05),
                "20d_gt_1.0pct": score(neigh10pct),
            },
            "tod": {k: score(v) for k, v in tod_trades.items()},
            "threshold_stable": thresh_stable,
            "baseline_negative": baseline_negative,
        },
        "primary": {
            "id": "LONG20_FIRST_RED_GREEN_5M",
            **{k: v for k, v in pb_pack.items() if k not in ("trades", "day_pnl")},
            "n_setups": n_setups,
            "n_tiny_risk": n_tiny,
            "ideal": pb_ideal,
            "stress_2tick": pb_2t,
            "stop_buffer_0": score(pb_buf0),
            "stop_buffer_2": score(pb_buf2),
            "targets": {str(k): score(v) for k, v in pb_targets.items()},
            "atr_pullback": score(pb_atr),
            "mc": monte_carlo(pb1),
            "dvp": dvp,
            "gc": gc,
            "news": news,
            "prop": {
                "avg_stop_points": pb_pack["full"].get("avg_stop_points"),
                "median_stop_points": pb_pack["full"].get("median_stop_points"),
                "p95_stop_points": pb_pack["full"].get("p95_stop_points"),
                "avg_usd_risk": None if not pb_pack["full"].get("avg_stop_points") else pb_pack["full"]["avg_stop_points"] * float(spec["point_usd"]),
                "p95_usd_risk": None if not pb_pack["full"].get("p95_stop_points") else pb_pack["full"]["p95_stop_points"] * float(spec["point_usd"]),
                "max_consec_losses": pb_pack["full"].get("max_consec_losses"),
                "worst_day_points": pb_pack["full"].get("worst_day_points"),
                "avg_hold_sec": pb_pack["full"].get("avg_hold_sec"),
                "max_trades_per_day": 1,
                "flatten": "15:55",
                "overnight": False,
                "exposure_pct": pb_cal.get("exposure_pct"),
            },
        },
        "always_long_compare": {
            "always": always_cal,
            "bull_open": bull_cal,
            "pullback": pb_cal,
            "state_distinct": state_distinct,
            "filter_improves": filter_improves,
            "always_calmar": always_calmar,
            "bull_calmar": bull_calmar,
        },
        "bear": {
            "n_2022_rth_days": len(bear_rows),
            "bull20_share_2022": bear_bull_share,
            "open_long_2022": bear_open,
            "open_long_2020": y2020,
            "note": "Long-only must turn off in deteriorated 20d states. 2022 share is the off-switch test.",
        },
        "dvp_dependent": dvp_dep,
        "flags": {
            "state_distinct": state_distinct,
            "filter_improves": filter_improves,
            "threshold_stable": thresh_stable,
            "baseline_negative": baseline_negative,
            "dvp_dependent": dvp_dep,
        },
    }

    _write_csv(REPORTS / f"phase44_{name.lower()}_structural.csv", struct_rows)
    _write_csv(REPORTS / f"phase44_{name.lower()}_open_long.csv", [t.to_dict() for t in bull_open if t.status == "ENTERED"])
    _write_csv(REPORTS / f"phase44_{name.lower()}_primary.csv", [t.to_dict() for t in pb1 if t.status == "ENTERED"])
    _write_csv(REPORTS / f"phase44_{name.lower()}_years.csv", open_pack["years"])
    _write_csv(REPORTS / f"phase44_{name.lower()}_pb_years.csv", pb_pack["years"])
    _write_csv(REPORTS / f"phase44_{name.lower()}_targets.csv", [{"r": k, **score(v)} for k, v in pb_targets.items()])
    _write_csv(REPORTS / f"phase44_{name.lower()}_tod.csv", [{"flatten": k, **score(v)} for k, v in tod_trades.items()])
    _write_csv(REPORTS / f"phase44_{name.lower()}_state_table.csv", table_23 + table_23d)
    _write_csv(REPORTS / f"phase44_{name.lower()}_fills.csv", [
        {"overlay": "ideal_0tick", **open_ideal},
        {"overlay": "primary_1tick", **open_pack["full"]},
        {"overlay": "stress_2tick", **open_2t},
        {"overlay": "pb_ideal_0tick", **pb_ideal},
        {"overlay": "pb_primary_1tick", **pb_pack["full"]},
        {"overlay": "pb_stress_2tick", **pb_2t},
    ])
    _write_csv(REPORTS / f"phase44_{name.lower()}_mode2.csv", [
        {"hold": h, "side": side, **block}
        for h, pack in mode2.items()
        for side, block in pack.items()
        if isinstance(block, dict)
    ])
    return overlay


def overall_status(statuses: dict[str, str]) -> str:
    vals = [statuses[k] for k in ("ES", "NQ") if k in statuses]
    if not vals:
        return "DATA_QUALITY_BLOCKED"
    if all(s == "DATA_QUALITY_BLOCKED" for s in vals):
        return "DATA_QUALITY_BLOCKED"
    order = [
        "LONG_ONLY_EDGE_FOUND",
        "LONG_ONLY_PROMISING_NEEDS_MORE_DATA",
        "LONG_DRIFT_BETA_ONLY",
        "LONG_ONLY_EDGE_WEAK",
        "LONG_ONLY_EDGE_REJECTED",
        "DATA_QUALITY_BLOCKED",
    ]
    for s in order:
        if s in vals:
            return s
    return "LONG_ONLY_EDGE_REJECTED"


def fmt(x: Any, nd: int = 3) -> str:
    if x is None:
        return "n/a"
    if isinstance(x, float):
        return f"{x:.{nd}f}"
    return str(x)


def recommendation_text(verdict: str, results: dict[str, Any]) -> str:
    if verdict == "LONG_ONLY_EDGE_FOUND":
        picks = []
        for inst, block in results.items():
            if block.get("status") != "LONG_ONLY_EDGE_FOUND":
                continue
            e = ((block.get("primary") or {}).get("holdout") or {}).get("expectancy_r") or ((block.get("open_long") or {}).get("holdout") or {}).get("expectancy_points") or -9
            picks.append((inst, e))
        picks.sort(key=lambda x: x[1], reverse=True)
        inst = picks[0][0] if picks else None
        return (
            f"One clean next-phase candidate: `{inst}` long-only 20d-positive state. "
            "Do not freeze in Phase 44. Do not add shorts or VWAP."
        )
    if verdict == "LONG_DRIFT_BETA_ONLY":
        return (
            "Longs earn because equity indexes drift up. The 20d-positive filter does not create "
            "a standalone Strategy #3 versus simply being long RTH. Do not freeze. "
            "Do not reopen two-sided TSMOM. Do not add shorts."
        )
    if verdict == "LONG_ONLY_PROMISING_NEEDS_MORE_DATA":
        return "Effect looks economically meaningful but sample/coverage is insufficient. Do not freeze."
    if verdict == "LONG_ONLY_EDGE_WEAK":
        return (
            "A simple long-only bullish-state filter does not survive as a robust book: "
            "holdout, costs, years, or always-long comparison fail the FOUND bar. "
            "Do not freeze. Do not add shorts. Do not search indicator soup."
        )
    if verdict == "DATA_QUALITY_BLOCKED":
        return "ES/NQ history did not meet the coverage bar. Do not invent substitutes."
    return (
        "The fresh long-only index-drift hypothesis fails on this implementation. "
        "Do not reopen two-sided TSMOM or HTF pullback. Do not add short trades. "
        "Do not freeze."
    )


def write_markdown(payload: dict[str, Any]) -> None:
    r = payload.get("results") or {}
    lines = [
        "# Phase 44 — Long-only equity-index drift / pullback",
        "",
        "Research only. `DRY_RUN`. No broker. Nothing frozen.",
        "",
        "This is a **new** long-only hypothesis, not Phase 40 minus shorts and not Phase 42 minus shorts. "
        "Locked before P&L: `LONG_STATE_20D_POSITIVE` (prior completed 20-session roll-cleaned return > 0 → next session eligible for longs, else flat) "
        "and `LONG20_FIRST_RED_GREEN_5M` (first red 5m after 09:30, first subsequent green 5m, enter next 5m open ±1 tick, stop = pullback low − 1 tick, flatten 15:55). Never short. No VWAP.",
        "",
        "## 1. Verdict",
        "",
        f"- **Overall:** `{payload.get('verdict')}`",
        f"- **ES_LONG_ONLY_STATUS:** `{payload.get('ES_LONG_ONLY_STATUS')}`",
        f"- **NQ_LONG_ONLY_STATUS:** `{payload.get('NQ_LONG_ONLY_STATUS')}`",
        f"- **Recommendation:** `{payload.get('recommendation')}`",
        "",
        (payload.get("recommendation_text") or ""),
        "",
        "## 2. Frozen integrity",
        "",
        "Verified before and after. Frozen files were not modified. Nothing was written to `strategy_frozen/`.",
        "",
        f"- GC VWAP V2: `{FROZEN_GC_HASH}`",
        f"- NQ DVP: `{FROZEN_NQ_HASH}`",
        f"- File SHA GC: `{payload.get('file_sha', {}).get('gc')}`",
        f"- File SHA NQ: `{payload.get('file_sha', {}).get('nq')}`",
        "",
        "## 3. Data and roll methodology",
        "",
        "- Daily signal series: Databento ohlcv-1d `.v.0` via Phase 40 `load_instrument`. Sunday Globex stubs dropped. "
        "20-session return uses roll-cleaned close-to-close (`mark_rolls`: instrument_id sidecar else 8× trailing-60 median overnight **and** 80 bps).",
        "- State for RTH date `D` uses the last daily bar with `date < D` so Monday 09:30 cannot see Monday's unfinished 1d bar.",
        "- Intraday expression: Phase 38 ES/NQ 1m, NY RTH, holidays, flatten 15:55, 1-tick primary cost. TRAIN through `2022-12-30`, HOLDOUT from `2023-01-03`.",
        "- News: 08:30 T±5m never overlaps RTH entries. No complete 10:00 calendar was invented as a filter.",
        "- Pullback C (percent of morning impulse) was not tested: it was not an objectively locked rule.",
        "",
    ]
    for name in ("ES", "NQ"):
        d = (r.get(name) or {}).get("data") or {}
        lines.append(
            f"- **{name}:** daily n={d.get('n_daily')} {d.get('daily_start')}→{d.get('daily_end')} rolls={d.get('n_roll_flags')}; "
            f"RTH days={d.get('n_rth_days')} {d.get('rth_start')}→{d.get('rth_end')} 1m bars={d.get('n_bars')} roll={d.get('roll')}"
        )
    lines += [
        "",
        "## 4. Unconditional long drift",
        "",
        "Always-long RTH: buy every valid 09:30, flatten 15:55, 1 tick adverse + commissions. This is the beta benchmark.",
        "",
        "| Instrument | N | E[pts] | WR | Total | Cal. mean | Cal. DD | Exposure | Train E | Holdout E |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for name in ("ES", "NQ"):
        u = (r.get(name) or {}).get("unconditional") or {}
        f = u.get("rth_always_long") or {}
        c = u.get("rth_always_calendar") or {}
        lines.append(
            f"| {name} | {f.get('n_resolved')} | {fmt(f.get('expectancy_points'))} | {fmt(f.get('win_rate'))} | {fmt(f.get('total_points'), 1)} | "
            f"{fmt(c.get('mean_per_calendar_day'))} | {fmt(c.get('max_dd_points'), 1)} | {fmt(c.get('exposure_pct'))} | "
            f"{fmt((u.get('rth_always_train') or {}).get('expectancy_points'))} | {fmt((u.get('rth_always_holdout') or {}).get('expectancy_points'))} |"
        )
    lines += ["", "## 5. Bullish-state forward returns", "",
              "Mean cost-adjusted RTH open→15:55 points after a completed prior-session state. Off = not in that state.",
              "",
              "| Instrument | State | N on | Mean on | Hit on | N off | Mean off | Diff |",
              "|---|---|---:|---:|---:|---:|---:|---:|"]
    for name in ("ES", "NQ"):
        sf = (r.get(name) or {}).get("state_forward") or {}
        for key, lab in (("d10", "10d>0"), ("d20", "20d>0 PRIMARY"), ("d60", "60d>0"), ("ema20", "EMA20 rising"), ("d20_and_d5", "20d>0 and 5d>0")):
            b = sf.get(key) or {}
            lines.append(f"| {name} | {lab} | {b.get('n_on')} | {fmt(b.get('mean_on'))} | {fmt(b.get('hit_on'))} | {b.get('n_off')} | {fmt(b.get('mean_off'))} | {fmt(b.get('diff_on_minus_off'))} |")
    lines += ["", "## 6. Primary 20d-positive state", ""]
    for name in ("ES", "NQ"):
        p = (r.get(name) or {}).get("primary_state") or {}
        lines.append(f"- **{name}** bull share={fmt(p.get('bull_share'))} strength={p.get('strength')} vol={p.get('vol')} gap={p.get('gap')} close_loc={p.get('close_loc')}")
    lines += ["", "## 7. Short-term dip inside bullish state", "",
              "2×2: prior completed 20d sign × prior 1d sign. Forward = always-long RTH points (cost-adjusted).",
              "",
              "| Instrument | 20d | Prior 1d | N | Mean RTH pts | Hit | t |",
              "|---|---|---|---:|---:|---:|---:|"]
    for name in ("ES", "NQ"):
        for row in (r.get(name) or {}).get("state_table_1d") or []:
            lines.append(f"| {name} | {row.get('bull_20')} | {row.get('prior_1d')} | {row.get('n')} | {fmt(row.get('mean_rth_pts'))} | {fmt(row.get('hit'))} | {fmt(row.get('tstat'))} |")
    lines += ["", "Same table with prior 3-session cumulative:", "",
              "| Instrument | 20d | Prior 3d | N | Mean RTH pts | Hit | t |",
              "|---|---|---|---:|---:|---:|---:|"]
    for name in ("ES", "NQ"):
        for row in (r.get(name) or {}).get("state_table_3d") or []:
            lines.append(f"| {name} | {row.get('bull_20')} | {row.get('prior_3d')} | {row.get('n')} | {fmt(row.get('mean_rth_pts'))} | {fmt(row.get('hit'))} | {fmt(row.get('tstat'))} |")
    lines += ["", "## 8. Continuation vs recovery", ""]
    for name in ("ES", "NQ"):
        c = (r.get(name) or {}).get("continuation") or {}
        v = (r.get(name) or {}).get("recovery") or {}
        p = (r.get(name) or {}).get("primary_state") or {}
        lines.append(f"- **{name}** continuation (20d>0, near 20d high): n={c.get('n')} mean={fmt(c.get('mean_rth_pts'))} hit={fmt(c.get('hit'))}")
        lines.append(f"  recovery (20d>0, modest/deep dip): n={v.get('n')} mean={fmt(v.get('mean_rth_pts'))} hit={fmt(v.get('hit'))}")
        lines.append(f"  dip buckets={p.get('dip')} st_1d={p.get('st_1d')} st_3d={p.get('st_3d')}")
    lines += ["", "## 9. RTH open-long baseline", "",
              "`BULL_STATE_RTH_OPEN_LONG` on 20d-positive days only. If this baseline is negative, the first-pullback rule is not a rescue.",
              "",
              "| Instrument | N | E[pts] | WR | PF | Train | Holdout | 0-tick | 2-tick | Status flags |",
              "|---|---:|---:|---:|---:|---:|---:|---:|---:|---|"]
    for name in ("ES", "NQ"):
        o = (r.get(name) or {}).get("open_long") or {}
        f = o.get("full") or {}
        flags = (r.get(name) or {}).get("flags") or {}
        lines.append(
            f"| {name} | {f.get('n_resolved')} | {fmt(f.get('expectancy_points'))} | {fmt(f.get('win_rate'))} | {fmt(f.get('profit_factor'), 2)} | "
            f"{fmt((o.get('train') or {}).get('expectancy_points'))} | {fmt((o.get('holdout') or {}).get('expectancy_points'))} | "
            f"{fmt((o.get('ideal') or {}).get('expectancy_points'))} | {fmt((o.get('stress_2tick') or {}).get('expectancy_points'))} | `{flags}` |"
        )
    lines += ["", "Neighbors (open-long, 1 tick):"]
    for name in ("ES", "NQ"):
        nbs = ((r.get(name) or {}).get("open_long") or {}).get("neighbors") or {}
        lines.append(f"- **{name}:** 10d={nbs.get('10d')} 60d={nbs.get('60d')} 20d>0.5%={nbs.get('20d_gt_0.5pct')} 20d>1%={nbs.get('20d_gt_1.0pct')}")
    lines += ["", "## 10. First-pullback candidate", "",
              "`LONG20_FIRST_RED_GREEN_5M`. One trade/day. No VWAP. Stop = pullback low − 1 tick. Primary target 1R, flatten 15:55 if neither stop nor target hits.",
              "",
              "| Instrument | N | E[R] | E[pts] | WR | PF | Train E[R] | Holdout E[R] | P(1R) | P(2R) | Ambiguous |",
              "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for name in ("ES", "NQ"):
        p = (r.get(name) or {}).get("primary") or {}
        f = p.get("full") or {}
        lines.append(
            f"| {name} | {f.get('n_resolved')} | {fmt(f.get('expectancy_r'))} | {fmt(f.get('expectancy_points'))} | {fmt(f.get('win_rate'))} | {fmt(f.get('profit_factor'), 2)} | "
            f"{fmt((p.get('train') or {}).get('expectancy_r'))} | {fmt((p.get('holdout') or {}).get('expectancy_r'))} | "
            f"{fmt(f.get('p_reach_1r'))} | {fmt(f.get('p_reach_2r'))} | {f.get('n_ambiguous')} |"
        )
        lines.append(f"  setups={p.get('n_setups')} tiny-risk skips={p.get('n_tiny_risk')} ATR-0.5 diagnostic={p.get('atr_pullback')}")
    lines += ["", "## 11. Target matrix", "",
              "| Instrument | 0.5R | 1R | 1.5R | 2R | 3R |",
              "|---|---|---|---|---|---|"]
    for name in ("ES", "NQ"):
        tg = ((r.get(name) or {}).get("primary") or {}).get("targets") or {}
        cells = []
        for k in ("0.5", "1.0", "1.5", "2.0", "3.0"):
            b = tg.get(k) or {}
            cells.append(f"N={b.get('n_resolved')} E[R]={fmt(b.get('expectancy_r'))}")
        lines.append(f"| {name} | " + " | ".join(cells) + " |")
    lines += ["", "## 12. MFE / MAE", ""]
    for name in ("ES", "NQ"):
        f = ((r.get(name) or {}).get("primary") or {}).get("full") or {}
        o = ((r.get(name) or {}).get("open_long") or {}).get("full") or {}
        lines.append(
            f"- **{name} pullback:** avg MFE={fmt(f.get('avg_mfe'))} avg MAE={fmt(f.get('avg_mae'))} "
            f"P(0.5R)={fmt(f.get('p_reach_0_5r'))} P(1R)={fmt(f.get('p_reach_1r'))} P(1.5R)={fmt(f.get('p_reach_1_5r'))} "
            f"P(2R)={fmt(f.get('p_reach_2r'))} P(3R)={fmt(f.get('p_reach_3r'))} avg hold={fmt(f.get('avg_hold_sec'), 0)}s "
            f"avg stop={fmt(f.get('avg_stop_points'))}"
        )
        lines.append(f"  open-long MFE={fmt(o.get('avg_mfe'))} MAE={fmt(o.get('avg_mae'))} — pullback should improve RR, not merely shrink N.")
    lines += ["", "## 13. ES results", ""]
    es = r.get("ES") or {}
    lines.append(f"- Status: `{es.get('status')}`")
    lines.append(f"- Open-long calendar vs always-long: `{es.get('always_long_compare')}`")
    lines.append(f"- Mode 2 (overnight diagnostic): `{es.get('mode2')}`")
    lines.append(f"- Bear 2022: `{es.get('bear')}`")
    lines += ["", "## 14. NQ results", ""]
    nq = r.get("NQ") or {}
    lines.append(f"- Status: `{nq.get('status')}`")
    lines.append(f"- Open-long calendar vs always-long: `{nq.get('always_long_compare')}`")
    lines.append(f"- Mode 2 (overnight diagnostic): `{nq.get('mode2')}`")
    lines.append(f"- Bear 2022: `{nq.get('bear')}`")
    lines += ["", "## 15. Cost stress", "",
              "| Instrument | Open 0-tick | Open 1-tick | Open 2-tick | PB 0-tick | PB 1-tick | PB 2-tick |",
              "|---|---:|---:|---:|---:|---:|---:|"]
    for name in ("ES", "NQ"):
        o = (r.get(name) or {}).get("open_long") or {}
        p = (r.get(name) or {}).get("primary") or {}
        lines.append(
            f"| {name} | {fmt((o.get('ideal') or {}).get('expectancy_points'))} | {fmt((o.get('full') or {}).get('expectancy_points'))} | "
            f"{fmt((o.get('stress_2tick') or {}).get('expectancy_points'))} | {fmt((p.get('ideal') or {}).get('expectancy_points'))} | "
            f"{fmt((p.get('full') or {}).get('expectancy_points'))} | {fmt((p.get('stress_2tick') or {}).get('expectancy_points'))} |"
        )
        lines.append(f"  stop buffers 0/2 tick: {p.get('stop_buffer_0')} / {p.get('stop_buffer_2')}")
    lines += ["", "## 16. Train / holdout", "",
              "Predeclared: TRAIN through `2022-12-30`, HOLDOUT from `2023-01-03`. No holdout threshold changes.",
              "",
              "| Instrument | Series | Train N | Train E | Holdout N | Holdout E |",
              "|---|---|---:|---:|---:|---:|"]
    for name in ("ES", "NQ"):
        for key, lab in (("open_long", "open-long"), ("primary", "pullback")):
            b = (r.get(name) or {}).get(key) or {}
            tr, ho = b.get("train") or {}, b.get("holdout") or {}
            emetric = "expectancy_r" if key == "primary" else "expectancy_points"
            lines.append(f"| {name} | {lab} | {tr.get('n_resolved')} | {fmt(tr.get(emetric))} | {ho.get('n_resolved')} | {fmt(ho.get(emetric))} |")
    lines += ["", "## 17. Walk-forward", "", "Year blocks on the locked rules. Multiple positive blocks required for FOUND.", ""]
    for name in ("ES", "NQ"):
        lines.append(f"- **{name} open-long years:** {((r.get(name) or {}).get('open_long') or {}).get('years')}")
        lines.append(f"- **{name} pullback years:** {((r.get(name) or {}).get('primary') or {}).get('years')}")
    lines += ["", "## 18. Year-by-year", "",
              "Inspect 2020, 2021, 2022, 2023, 2024, 2025, 2026 YTD. A long-only book may lose in bears; the filter must keep DD acceptable.",
              ""]
    for name in ("ES", "NQ"):
        lines.append(f"- **{name} always-long years:** {((r.get(name) or {}).get('unconditional') or {}).get('rth_always_years')}")
    lines += ["", "## 19. Bear-regime behavior", ""]
    for name in ("ES", "NQ"):
        lines.append(f"- **{name}:** {(r.get(name) or {}).get('bear')}")
    lines.append("Question: does 20d>0 turn off quickly enough when the regime deteriorates? See 2022 bull-share versus full-sample bull-share.")
    lines += ["", "## 20. Always-long comparison", ""]
    for name in ("ES", "NQ"):
        c = (r.get(name) or {}).get("always_long_compare") or {}
        lines.append(f"- **{name}:** state_distinct={c.get('state_distinct')} filter_improves={c.get('filter_improves')} always_calmar={fmt(c.get('always_calmar'))} bull_calmar={fmt(c.get('bull_calmar'))}")
        lines.append(f"  always={c.get('always')}")
        lines.append(f"  bull-open={c.get('bull_open')}")
        lines.append(f"  pullback={c.get('pullback')}")
    lines += ["", "## 21. Exposure efficiency", ""]
    for name in ("ES", "NQ"):
        c = (r.get(name) or {}).get("always_long_compare") or {}
        a, b = c.get("always") or {}, c.get("bull_open") or {}
        lines.append(
            f"- **{name}:** always mean/calendar={fmt(a.get('mean_per_calendar_day'))} vs bull mean/calendar={fmt(b.get('mean_per_calendar_day'))} "
            f"mean/active={fmt(b.get('mean_per_active_day'))} exposure={fmt(b.get('exposure_pct'))} "
            f"DD always={fmt(a.get('max_dd_points'), 1)} DD bull={fmt(b.get('max_dd_points'), 1)}"
        )
    lines += ["", "## 22. DVP comparison", "",
              "Read-only vs frozen NQ DVP journal. Phase 44 has no VWAP and no 15m drift. If the NQ candidate only works on DVP-active days, flag `DVP_DEPENDENT`.",
              ""]
    dvp = ((r.get("NQ") or {}).get("primary") or {}).get("dvp")
    lines.append(f"- NQ vs DVP: `{dvp}`")
    lines.append(f"- dvp_dependent={(r.get('NQ') or {}).get('dvp_dependent')}")
    lines += ["", "## 23. GC V2 comparison", "", "Read-only. No portfolio optimization.", ""]
    for name in ("ES", "NQ"):
        lines.append(f"- **{name} vs GC paper:** {((r.get(name) or {}).get('primary') or {}).get('gc')}")
    lines += ["", "## 24. Prop geometry", ""]
    for name in ("ES", "NQ"):
        pr = ((r.get(name) or {}).get("primary") or {}).get("prop") or {}
        nw = ((r.get(name) or {}).get("primary") or {}).get("news") or {}
        lines.append(f"- **{name}:** {pr}")
        lines.append(f"  news={nw}")
        lines.append(f"  MC={((r.get(name) or {}).get('primary') or {}).get('mc')}")
        lines.append(f"  TOD exits={((r.get(name) or {}).get('open_long') or {}).get('tod')}")
    lines += [
        "",
        "## 25. Recommendation",
        "",
        f"`{payload.get('recommendation')}`",
        "",
        payload.get("recommendation_text") or "",
        "",
        "Nothing in this phase is frozen. Candidate JSON is written only if `LONG_ONLY_EDGE_FOUND`.",
        "",
        "Closed branches remain closed: two-sided TSMOM, HTF pullback, ORB, NQ sweep, post-news macro, small-cap gap-up.",
        "",
    ]
    DOCS.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> dict[str, Any]:
    frozen_before = assert_frozen()
    if not frozen_before["ok"]:
        payload = {"ok": False, "status": "FROZEN_INTEGRITY_FAILED", "frozen_before": frozen_before}
        VALIDATION.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return payload
    spec = json.loads(SPEC_PATH.read_text(encoding="utf-8"))
    results: dict[str, Any] = {}
    summary = []
    for name in ("ES", "NQ"):
        block = research_instrument(name)
        results[name] = block
        o = (block.get("open_long") or {}).get("full") or {}
        p = (block.get("primary") or {}).get("full") or {}
        summary.append({
            "instrument": name,
            "status": block.get("status"),
            "open_n": o.get("n_resolved"),
            "open_e_pts": o.get("expectancy_points"),
            "open_holdout": ((block.get("open_long") or {}).get("holdout") or {}).get("expectancy_points"),
            "pb_n": p.get("n_resolved"),
            "pb_e_r": p.get("expectancy_r"),
            "pb_holdout_r": ((block.get("primary") or {}).get("holdout") or {}).get("expectancy_r"),
            "filter_improves": ((block.get("always_long_compare") or {}).get("filter_improves")),
            "state_distinct": ((block.get("always_long_compare") or {}).get("state_distinct")),
        })
    statuses = {"ES": (results.get("ES") or {}).get("status") or "DATA_QUALITY_BLOCKED", "NQ": (results.get("NQ") or {}).get("status") or "DATA_QUALITY_BLOCKED"}
    verdict = overall_status(statuses)
    rec_text = recommendation_text(verdict, results)
    if verdict == "LONG_ONLY_EDGE_FOUND":
        rec = "CONTINUE_LONG_ONLY_TO_REFINEMENT_NO_FREEZE"
    elif verdict == "LONG_DRIFT_BETA_ONLY":
        rec = "CLOSE_LONG_ONLY_AS_BETA_NOT_BOOK_3"
    elif verdict == "LONG_ONLY_PROMISING_NEEDS_MORE_DATA":
        rec = "COLLECT_MORE_HISTORY_NO_FREEZE"
    elif verdict == "DATA_QUALITY_BLOCKED":
        rec = "FIX_DATA_BEFORE_DECISION"
    else:
        rec = "CLOSE_LONG_ONLY_INDEX_DRIFT_BRANCH"
    frozen_after = assert_frozen()
    _write_csv(REPORTS / "phase44_primary_summary.csv", summary)

    payload = {
        "ok": frozen_after["ok"],
        "phase": 44,
        "status": "RESEARCH_COMPLETE",
        "execution": "DRY_RUN_NO_BROKER",
        "verdict": verdict,
        "ES_LONG_ONLY_STATUS": statuses["ES"],
        "NQ_LONG_ONLY_STATUS": statuses["NQ"],
        "recommendation": rec,
        "recommendation_text": rec_text,
        "frozen_before": {**frozen_before, "gc": FROZEN_GC_HASH, "nq": FROZEN_NQ_HASH},
        "frozen_after": {**frozen_after, "gc": FROZEN_GC_HASH, "nq": FROZEN_NQ_HASH},
        "file_sha": {
            "gc": file_sha256(ROOT / "strategy_frozen" / "gc_vwap_v2_phase26.json"),
            "nq": file_sha256(ROOT / "strategy_frozen" / "nq_dvp_phase30.json"),
            "gc_expected": GC_FILE_SHA,
            "nq_expected": NQ_FILE_SHA,
        },
        "spec": spec,
        "candidate_written": False,
        "candidate_path": None,
    }
    if verdict == "LONG_ONLY_EDGE_FOUND":
        picks = []
        for inst in ("ES", "NQ"):
            if (results.get(inst) or {}).get("status") == "LONG_ONLY_EDGE_FOUND":
                e = (((results[inst].get("primary") or {}).get("holdout") or {}).get("expectancy_r") or -9)
                picks.append((inst, e))
        if picks:
            picks.sort(key=lambda x: x[1], reverse=True)
            inst = picks[0][0]
            path = CANDIDATE_DIR / f"phase44_{inst}_LONG_ONLY.json"
            path.write_text(json.dumps({
                "status": "RESEARCH_CANDIDATE",
                "phase": 44,
                "instrument": inst,
                "family": "long_only_index_drift_v1",
                "candidate_id": "LONG20_FIRST_RED_GREEN_5M",
                "state_id": "LONG_STATE_20D_POSITIVE",
                "not_frozen": True,
                "rules": spec["primary_intraday_candidate"],
                "metrics": (results[inst].get("primary") or {}),
            }, indent=2, default=str), encoding="utf-8")
            payload["candidate_written"] = True
            payload["candidate_path"] = str(path)

    # drop bulky day_pnl already in CSVs
    slim = {}
    for k, v in results.items():
        if not isinstance(v, dict):
            slim[k] = v
            continue
        slim[k] = v
    payload["results"] = slim
    VALIDATION.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    write_markdown(payload)
    print(json.dumps({"verdict": verdict, **statuses, "rec": rec, "candidate": payload["candidate_path"]}, indent=2), flush=True)
    return payload


if __name__ == "__main__":
    main()
    sys.exit(0)
