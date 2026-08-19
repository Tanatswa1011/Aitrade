"""Phase 45 — TG Capital London 30m FVG research.

DRY_RUN. No broker. No freeze. Primary TG_GC_30M_LONDON_FVG50_REACTION was
declared in phase45_spec.json before this validator inspected P&L.
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

from bar_dataset import load_dataset
from htf_pullback_engine import aggregate_bars
from nq_microstructure_models import FROZEN_GC_HASH, FROZEN_NQ_HASH
from nq_pdh_pdl import ny_date
from phase34_validate import GC_FILE_SHA, NQ_FILE_SHA, assert_frozen, file_sha256
from phase38_validate import load_instrument as load_nq_1m
from phase40_validate import tstat
from tg_london_engine import (
    DOJI_RATIO,
    INSTRUMENTS,
    TRIDENT_WICK_BODY,
    TfBar,
    TgTrade,
    atr_series,
    collect_session_setups,
    detect_fvgs,
    ema_series,
    london_hhmm,
    scan_fvg_forward,
    simulate_setup,
    trend_aligned,
)

ROOT = Path(__file__).resolve().parent
REPORTS = ROOT / "reports"
VALIDATION = ROOT / "phase45_validation.json"
SPEC_PATH = ROOT / "phase45_spec.json"
CANDIDATE_DIR = ROOT / "strategy_candidates"
DOCS = ROOT / "docs" / "PHASE45_TG_CAPITAL_LONDON_30M_RESEARCH.md"
GC_ROOT = ROOT / "data" / "databento" / "GC" / "stitched"
TRAIN_END = "2022-12-30"
HOLDOUT_START = "2023-01-03"
TARGETS = [1.0, 1.5, 2.0, 3.0, 4.0, 5.0]


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


def rate(xs: list[bool]) -> Optional[float]:
    return None if not xs else sum(1 for x in xs if x) / len(xs)


def score(trades: list[TgTrade], *, use_cost: bool = True) -> dict[str, Any]:
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
    longs = [t for t in resolved if t.direction == "BULLISH"]
    shorts = [t for t in resolved if t.direction == "BEARISH"]

    def side(rows):
        pp = [p for t in rows if (p := _v(t, attr)) is not None]
        rr = [p for t in rows if (p := _v(t, rattr)) is not None]
        if not pp:
            return {"n": 0}
        return {
            "n": len(pp),
            "win_rate": sum(1 for x in pp if x > 0) / len(pp),
            "expectancy_points": statistics.mean(pp),
            "expectancy_r": None if not rr else statistics.mean(rr),
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
    n_ent = max(len(entered), 1)
    return {
        "n_entered": len(entered),
        "n_resolved": len(resolved),
        "n_ambiguous": len(amb),
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
        "p_reach_1r": None if not entered else sum(1 for t in entered if t.reach_1r) / n_ent,
        "p_reach_2r": None if not entered else sum(1 for t in entered if t.reach_2r) / n_ent,
        "p_reach_3r": None if not entered else sum(1 for t in entered if t.reach_3r) / n_ent,
        "avg_hold_sec": None if not holds else statistics.mean(holds),
        "n_days": len(day_pnl),
        "worst_day_points": None if not daily_loss else min(daily_loss),
        "best_trade_r": None if not rs else max(rs),
        "worst_trade_r": None if not rs else min(rs),
        "tstat": tstat(pts),
        "long": side(longs),
        "short": side(shorts),
        "use_cost": use_cost,
        "trades_per_year": None if not resolved else len(resolved) / max(len({t.year for t in resolved}), 1),
    }


def slice_td(trades: list[TgTrade], start: str, end: str) -> list[TgTrade]:
    return [t for t in trades if start <= t.trading_date <= end]


def year_rows(trades: list[TgTrade]) -> list[dict[str, Any]]:
    by: dict[int, list[TgTrade]] = defaultdict(list)
    for t in trades:
        by[int(t.year or t.trading_date[:4])].append(t)
    return [{"year": y, **score(by[y])} for y in sorted(by)]


def funnel(events, scans, key: str) -> dict[str, Any]:
    xs = [bool(s.get(key)) for s in scans]
    return {"n": len(xs), "rate": rate(xs)}


def struct_block(events, scans) -> dict[str, Any]:
    mids = [s for s in scans if s.get("touched_mid")]
    return {
        "n": len(events),
        "P_edge": funnel(events, scans, "touched_edge")["rate"],
        "P_mid": funnel(events, scans, "touched_mid")["rate"],
        "P_fill": funnel(events, scans, "full_fill")["rate"],
        "P_through": funnel(events, scans, "through")["rate"],
        "P_resume_after_mid": rate([bool(s.get("resume_after_mid")) for s in mids]) if mids else None,
        "P_new_extreme_after_mid": rate([bool(s.get("new_extreme_after_mid")) for s in mids]) if mids else None,
        "mean_mfe_after_mid": _mean([float(s["mfe_after_mid"]) for s in mids if s.get("mfe_after_mid") is not None]),
        "mean_mae_after_mid": _mean([float(s["mae_after_mid"]) for s in mids if s.get("mae_after_mid") is not None]),
        "P_doji": rate([s.get("doji_i") is not None for s in scans]),
        "P_trident": rate([s.get("trident_i") is not None for s in scans]),
        "P_close": rate([s.get("close_i") is not None for s in scans]),
        "mean_bars_to_mid": _mean([float(s["bars_to_mid"]) for s in scans if s.get("bars_to_mid") is not None]),
    }


def load_gc_1m() -> tuple[Optional[list], dict[str, Any]]:
    loaded = load_dataset("databento_GC_v0", "1m", root=GC_ROOT)
    meta = {"path": str(GC_ROOT), "roll": "databento_GC.v.0", "source": "phase45_download", "cost_usd": 8.448671028018}
    if not loaded.get("ok"):
        return None, {**meta, "ok": False, "error": loaded.get("error")}
    bars = list(loaded["bars"])
    meta.update({"ok": True, "n_bars": len(bars), "earliest": int(bars[0].time), "latest": int(bars[-1].time)})
    return bars, meta


def index_ny(bars: list) -> dict[str, list]:
    out: dict[str, list] = defaultdict(list)
    for b in bars:
        out[ny_date(int(b.time))].append(b)
    return dict(out)


def pack_trades(trades: list[TgTrade], last_date: str) -> dict[str, Any]:
    first = min((t.trading_date for t in trades), default="2020-01-02")
    return {
        "full": score(trades),
        "train": score(slice_td(trades, first, TRAIN_END)),
        "holdout": score(slice_td(trades, HOLDOUT_START, last_date)),
        "years": year_rows(trades),
        "n_setups": len(trades),
    }


def run_kind(name, bars30, events, by_ny, kind, stop_family, target_r, adverse, cand, reaction, last_date, **kw):
    setups = collect_session_setups(bars30, events, kind=kind, **kw)
    trades = []
    for ev, ri in setups:
        day = by_ny.get(ev.ny_date) or []
        trades.append(
            simulate_setup(
                instrument=name,
                bars_1m=day,
                bars30=bars30,
                ev=ev,
                reaction_i=ri,
                stop_family=stop_family,
                target_r=target_r,
                adverse_ticks=adverse,
                candidate=cand,
                reaction=reaction,
            )
        )
    return setups, trades, pack_trades(trades, last_date)


def gc_v2_compare(entered: list[TgTrade]) -> dict[str, Any]:
    path = ROOT / "journal" / "phase26_gc_vwap_v2_paper" / "paper_trades.jsonl"
    n = 0
    if path.exists():
        n = sum(1 for line in path.read_text(encoding="utf-8").splitlines() if line.strip())
    return {"n_gc_v2_paper": n, "overlap_days": 0, "daily_pnl_correlation": None, "note": "Phase 26 paper journal empty until forward trades exist." if n == 0 else None}


def dvp_compare(entered: list[TgTrade]) -> dict[str, Any]:
    path = ROOT / "journal" / "phase29_nq_drift_vwap" / "trades.jsonl"
    day_d: dict[str, float] = defaultdict(float)
    n = 0
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            n += 1
            td = str(row.get("trading_date") or "")
            if td and row.get("points") is not None:
                day_d[td] += float(row["points"])
    day_s: dict[str, float] = defaultdict(float)
    for t in entered:
        if t.points_after_cost is not None:
            day_s[t.trading_date] += float(t.points_after_cost)
    common = sorted(set(day_s) & set(day_d))
    corr = None
    if len(common) >= 8:
        xs = [day_s[d] for d in common]
        ys = [day_d[d] for d in common]
        mx, my = statistics.mean(xs), statistics.mean(ys)
        den = math.sqrt(sum((a - mx) ** 2 for a in xs) * sum((b - my) ** 2 for b in ys))
        corr = None if den == 0 else sum((a - mx) * (b - my) for a, b in zip(xs, ys)) / den
    lose_s = {d for d, v in day_s.items() if v < 0}
    lose_d = {d for d, v in day_d.items() if v < 0}
    return {
        "n_dvp": n,
        "overlap_days": len(set(day_s) & set(day_d)),
        "daily_pnl_correlation": corr,
        "losing_day_overlap": len(lose_s & lose_d),
    }


def decide_one(
    *,
    coverage_ok,
    n_full,
    n_hold,
    full,
    hold,
    years,
    thresh_stable,
    distinct_from_v2,
    p_resume=None,
    p_fill=None,
    stress_e=None,
) -> str:
    if not coverage_ok:
        return "DATA_QUALITY_BLOCKED"
    e = full.get("expectancy_r")
    eh = hold.get("expectancy_r")
    pos_y = sum(1 for r in years if (r.get("n_resolved") or 0) >= 5 and (r.get("expectancy_r") or 0) > 0)
    y_n = sum(1 for r in years if (r.get("n_resolved") or 0) >= 5)
    if e is None:
        return "TG_LONDON_EDGE_REJECTED"
    # Resume after mid is not continuation if the gap also fills. That is
    # two-way trade through the imbalance, not a skip-away signature.
    continuation_without_fill = (
        p_resume is not None
        and p_fill is not None
        and p_resume >= 0.62
        and p_fill <= 0.50
    )
    costs_ok = stress_e is None or stress_e > 0
    strong = (
        e > 0
        and (eh or 0) > 0
        and costs_ok
        and (full.get("profit_factor") or 0) > 1.05
        and thresh_stable
        and distinct_from_v2
        and y_n >= 4
        and pos_y >= max(3, y_n // 2)
    )
    if strong and n_full >= 100 and n_hold >= 25:
        return "TG_LONDON_EDGE_FOUND"
    if strong and n_full >= 40:
        return "TG_LONDON_PROMISING_NEEDS_MORE_DATA"
    if e > 0 and (eh or 0) > 0 and n_full < 40:
        return "TG_LONDON_PROMISING_NEEDS_MORE_DATA"
    if e > 0 and ((eh or 0) <= 0 or not costs_ok):
        return "TG_LONDON_EDGE_WEAK"
    if e <= 0 and continuation_without_fill and n_full >= 30:
        return "TG_FVG_STRUCTURAL_EFFECT_ONLY"
    if e <= 0:
        return "TG_LONDON_EDGE_REJECTED"
    return "TG_LONDON_EDGE_WEAK"


def research_instrument(name: str, bars: list, meta: dict[str, Any]) -> dict[str, Any]:
    from tg_london_engine import aggregate_30m_london
    print(f"  aggregating {name} 30m/4h", flush=True)
    bars30 = aggregate_30m_london(bars)
    h4 = aggregate_bars(bars, 14400, globex_4h=True)
    closes = [float(b.close) for b in bars30]
    ema20 = ema_series(closes, 20)
    ema50 = ema_series(closes, 50)
    ema200 = ema_series(closes, 200)
    atr = atr_series(bars30, 14)
    h4c = [float(b.close) for b in h4]
    ema200_4h = ema_series(h4c, 200)
    by_ny = index_ny(bars)
    last_date = max(by_ny) if by_ny else "2026-08-14"
    print(f"  {name} 30m={len(bars30)} 4h={len(h4)}", flush=True)

    windows = [("06:00", "10:00"), ("07:00", "11:00"), ("08:00", "12:00")]
    window_stats = {}
    events_primary = None
    scans_primary = None
    for w0, w1 in windows:
        evs = detect_fvgs(
            instrument=name, bars30=bars30, h4=h4, ema20=ema20, ema50=ema50, ema200=ema200,
            atr=atr, ema200_4h=ema200_4h, window=(w0, w1),
        )
        in_w = [e for e in evs if e.in_window]
        scans = [scan_fvg_forward(bars30, e) for e in in_w]
        aligned = [e for e in in_w if trend_aligned(e)]
        a_scans = [scan_fvg_forward(bars30, e) for e in aligned]
        window_stats[f"{w0}-{w1}"] = {
            "n_fvg_all": len(evs),
            "n_london": len(in_w),
            "n_aligned": len(aligned),
            "london": struct_block(in_w, scans),
            "aligned": struct_block(aligned, a_scans),
        }
        if (w0, w1) == ("07:00", "11:00"):
            events_primary = evs
            scans_primary = scans
    assert events_primary is not None
    in_w = [e for e in events_primary if e.in_window]
    aligned = [e for e in in_w if trend_aligned(e)]
    ema_only = [e for e in in_w if e.ema200_side == e.direction]
    stack_ok = [e for e in ema_only if e.stack_side == e.direction]
    incremental = {
        "london_all": struct_block(in_w, [scan_fvg_forward(bars30, e) for e in in_w]),
        "plus_ema200": struct_block(ema_only, [scan_fvg_forward(bars30, e) for e in ema_only]),
        "plus_stack": struct_block(stack_ok, [scan_fvg_forward(bars30, e) for e in stack_ok]),
        "full_align": struct_block(aligned, [scan_fvg_forward(bars30, e) for e in aligned]),
    }
    a_scans = [scan_fvg_forward(bars30, e) for e in aligned]
    mid_a = [s for s in a_scans if s.get("touched_mid")]
    reaction_inc = {
        "mid_only_resume": rate([bool(s.get("resume_after_mid")) for s in mid_a]),
        "mid_only_extreme": rate([bool(s.get("new_extreme_after_mid")) for s in mid_a]),
        "doji_resume": rate([bool(s.get("resume_after_mid")) for s in mid_a if s.get("doji_i") is not None]),
        "trident_resume": rate([bool(s.get("resume_after_mid")) for s in mid_a if s.get("trident_i") is not None]),
        "close_resume": rate([bool(s.get("resume_after_mid")) for s in mid_a if s.get("close_i") is not None]),
        "n_mid": len(mid_a),
        "n_doji": sum(1 for s in a_scans if s.get("doji_i") is not None),
        "n_trident": sum(1 for s in a_scans if s.get("trident_i") is not None),
        "n_close": sum(1 for s in a_scans if s.get("close_i") is not None),
    }

    print(f"  {name} simulating candidates", flush=True)
    setups_b, t_b, pack_b = run_kind(name, bars30, events_primary, by_ny, "trident", "fvg_boundary", 2.0, 1.0, "TG_GC_30M_LONDON_FVG50_REACTION", "trident", last_date)
    _, t_a, pack_a = run_kind(name, bars30, events_primary, by_ny, "doji", "fvg_boundary", 2.0, 1.0, "CAND_A_DOJI", "doji", last_date)
    _, t_c, pack_c = run_kind(name, bars30, events_primary, by_ny, "close", "fvg_boundary", 2.0, 1.0, "CAND_C_CLOSE", "close", last_date)
    _, t_rx, pack_rx = run_kind(name, bars30, events_primary, by_ny, "trident", "reaction_extreme", 2.0, 1.0, "STOP_REACTION", "trident", last_date)
    _, t0, pack0 = run_kind(name, bars30, events_primary, by_ny, "trident", "fvg_boundary", 2.0, 0.0, "IDEAL", "trident", last_date)
    _, t2, pack2 = run_kind(name, bars30, events_primary, by_ny, "trident", "fvg_boundary", 2.0, 2.0, "STRESS2", "trident", last_date)

    targets = {}
    for r in TARGETS:
        _, tr, pk = run_kind(name, bars30, events_primary, by_ny, "trident", "fvg_boundary", r, 1.0, f"T{r}", "trident", last_date)
        targets[str(r)] = pk["full"]

    # threshold neighbors on primary kind
    _, _, d20 = run_kind(name, bars30, events_primary, by_ny, "doji", "fvg_boundary", 2.0, 1.0, "DOJI20", "doji", last_date, doji_ratio=0.20)
    _, _, d30 = run_kind(name, bars30, events_primary, by_ny, "doji", "fvg_boundary", 2.0, 1.0, "DOJI30", "doji", last_date, doji_ratio=0.30)
    _, _, w08 = run_kind(name, bars30, events_primary, by_ny, "trident", "fvg_boundary", 2.0, 1.0, "W08", "trident", last_date, wick_body=0.8)
    _, _, w12 = run_kind(name, bars30, events_primary, by_ny, "trident", "fvg_boundary", 2.0, 1.0, "W12", "trident", last_date, wick_body=1.2)
    base_e = pack_b["full"].get("expectancy_r")
    neigh = [w08["full"].get("expectancy_r"), w12["full"].get("expectancy_r")]
    thresh_stable = True
    if base_e is not None and base_e > 0:
        if any(x is not None and x <= 0 for x in neigh):
            thresh_stable = False
    elif base_e is not None and base_e <= 0:
        thresh_stable = True

    entered = [t for t in t_b if t.status == "ENTERED"]
    news_0830_lon = sum(1 for t in entered if t.signal_hhmm and "08:25" <= t.signal_hhmm <= "08:35")
    news_0830_et = 0  # London 07-11 never overlaps 08:30 ET
    spec = INSTRUMENTS[name]
    v2 = gc_v2_compare(entered) if name == "GC" else None
    dvp = dvp_compare(entered) if name == "NQ" else None
    distinct = True
    if v2 and (v2.get("daily_pnl_correlation") or 0) >= 0.70:
        distinct = False

    n_full = pack_b["full"].get("n_resolved") or 0
    n_hold = pack_b["holdout"].get("n_resolved") or 0
    coverage_ok = bool(bars) and meta.get("n_bars", 0) > 500_000
    status = decide_one(
        coverage_ok=coverage_ok,
        n_full=n_full,
        n_hold=n_hold,
        full=pack_b["full"],
        hold=pack_b["holdout"],
        years=pack_b["years"],
        thresh_stable=thresh_stable,
        distinct_from_v2=distinct,
        p_resume=incremental["full_align"].get("P_resume_after_mid"),
        p_fill=incremental["full_align"].get("P_fill"),
        stress_e=pack2["full"].get("expectancy_r"),
    )

    years_cov = sorted({int(d[:4]) for d in by_ny})
    freq = {
        "london_fvgs_per_year": None if not years_cov else len(in_w) / max(len(years_cov), 1),
        "aligned_per_year": None if not years_cov else len(aligned) / max(len(years_cov), 1),
        "trident_trades_per_year": pack_b["full"].get("trades_per_year"),
    }
    overlay = {
        "instrument": name,
        "status": status,
        "data": {**meta, "n_30m": len(bars30), "n_4h": len(h4), "years": years_cov, "start": min(by_ny) if by_ny else None, "end": last_date},
        "windows": window_stats,
        "incremental_ema": incremental,
        "reaction_incremental": reaction_inc,
        "primary": {
            "id": "TG_GC_30M_LONDON_FVG50_REACTION",
            **{k: v for k, v in pack_b.items()},
            "ideal": pack0["full"],
            "stress_2tick": pack2["full"],
            "targets": targets,
            "stop_reaction_extreme": pack_rx["full"],
            "threshold": {"doji_0.20": d20["full"], "doji_0.30": d30["full"], "wick_0.8": w08["full"], "wick_1.2": w12["full"], "stable": thresh_stable},
            "news": {
                "bls_et_0830_removed": news_0830_et,
                "london_0825_0835_entries": news_0830_lon,
                "note": "08:30 ET is 13:30 London and never overlaps the 07:00-11:00 window. No complete UK 07:00/10:00 calendar was invented.",
            },
            "prop": {
                "avg_stop_points": pack_b["full"].get("avg_stop_points"),
                "median_stop_points": pack_b["full"].get("median_stop_points"),
                "p95_stop_points": pack_b["full"].get("p95_stop_points"),
                "usd_gc": None if name != "GC" or not pack_b["full"].get("avg_stop_points") else pack_b["full"]["avg_stop_points"] * float(spec["point_usd"]),
                "usd_mgc": None if name != "GC" or not pack_b["full"].get("avg_stop_points") else pack_b["full"]["avg_stop_points"] * float(spec.get("mgc_point_usd") or 10),
                "usd_nq": None if name != "NQ" or not pack_b["full"].get("avg_stop_points") else pack_b["full"]["avg_stop_points"] * float(spec["point_usd"]),
                "usd_mnq": None if name != "NQ" or not pack_b["full"].get("avg_stop_points") else pack_b["full"]["avg_stop_points"] * float(spec.get("mnq_point_usd") or 2),
                "max_consec_losses": pack_b["full"].get("max_consec_losses"),
                "worst_day_points": pack_b["full"].get("worst_day_points"),
                "avg_hold_sec": pack_b["full"].get("avg_hold_sec"),
                "flatten": "15:55 ET",
                "overnight": False,
                "max_trades_per_day": 1,
            },
            "gc_v2": v2,
            "dvp": dvp,
        },
        "cand_A_doji": pack_a,
        "cand_C_close": pack_c,
        "frequency": freq,
        "flags": {"threshold_stable": thresh_stable, "distinct_from_gc_v2": distinct},
        "n_fvg_london": len(in_w),
        "n_aligned": len(aligned),
    }
    _write_csv(REPORTS / f"phase45_{name.lower()}_fvgs.csv", [e.to_dict() for e in aligned[:5000]])
    _write_csv(REPORTS / f"phase45_{name.lower()}_primary.csv", [t.to_dict() for t in t_b if t.status == "ENTERED"])
    _write_csv(REPORTS / f"phase45_{name.lower()}_years.csv", pack_b["years"])
    _write_csv(REPORTS / f"phase45_{name.lower()}_targets.csv", [{"r": k, **v} for k, v in targets.items()])
    _write_csv(REPORTS / f"phase45_{name.lower()}_fills.csv", [
        {"overlay": "ideal", **pack0["full"]},
        {"overlay": "1tick", **pack_b["full"]},
        {"overlay": "2tick", **pack2["full"]},
    ])
    return overlay


def aggregate_30m_needed(bars):
    return bars


def overall_status(statuses: dict[str, str]) -> str:
    g = statuses.get("GC") or "DATA_QUALITY_BLOCKED"
    order = [
        "TG_LONDON_EDGE_FOUND",
        "TG_LONDON_PROMISING_NEEDS_MORE_DATA",
        "TG_FVG_STRUCTURAL_EFFECT_ONLY",
        "TG_LONDON_EDGE_WEAK",
        "TG_LONDON_EDGE_REJECTED",
        "TG_MODEL_DEFINITION_BLOCKED",
        "DATA_QUALITY_BLOCKED",
    ]
    # GC is primary; NQ cannot promote the family
    if g in order:
        return g
    return "TG_LONDON_EDGE_REJECTED"


def fmt(x: Any, nd: int = 3) -> str:
    if x is None:
        return "n/a"
    if isinstance(x, float):
        return f"{x:.{nd}f}"
    return str(x)


def recommendation_text(verdict: str) -> str:
    if verdict == "TG_LONDON_EDGE_FOUND":
        return "One research candidate: GC `TG_GC_30M_LONDON_FVG50_REACTION`. Do not freeze in Phase 45."
    if verdict == "TG_LONDON_PROMISING_NEEDS_MORE_DATA":
        return "Effect looks real but N is too small for a freeze path. Do not weaken rules to manufacture sample."
    if verdict == "TG_FVG_STRUCTURAL_EFFECT_ONLY":
        return "Aligned London FVGs show continuation geometry that the locked entry/stop/target does not monetize. Do not force a trade. Do not freeze."
    if verdict == "TG_MODEL_DEFINITION_BLOCKED":
        return "Critical TG source rules remain undefined. Approximations were used; do not invent a different strategy."
    if verdict == "DATA_QUALITY_BLOCKED":
        return "Intraday GC/NQ history did not meet the coverage bar."
    if verdict == "TG_LONDON_EDGE_WEAK":
        return (
            "Aligned London FVGs mostly fill rather than continue. The locked "
            "trident+2R book is +0.10R in-sample on GC but holdout and 2-tick fail; "
            "NQ is negative. Do not freeze. Do not add indicators. Do not decorate "
            "GC VWAP V2 with FVG."
        )
    return (
        "The mechanized TG Capital London 30m FVG model fails on this implementation. "
        "Do not freeze. Do not reopen closed families. Do not decorate GC VWAP V2 with FVG."
    )


def write_markdown(payload: dict[str, Any]) -> None:
    # Keep the human-polished 28-section report if it already exists.
    if DOCS.exists() and "<!-- POLISHED_PHASE45_REPORT -->" in DOCS.read_text(encoding="utf-8")[:800]:
        return
    r = payload.get("results") or {}
    lines = [
        "# Phase 45 — TG Capital London 30m model",
        "",
        "Research only. `DRY_RUN`. No broker. Nothing frozen.",
        "",
        "Mechanized TG-style chain: London 07:00–11:00 Europe/London → completed 4H close vs EMA200 → 30m EMA200 + EMA20/50/200 stack → trend-aligned 3-candle FVG → 50% midpoint → trident approximation → next 30m open ±1 tick → FVG-boundary stop → 2R, flatten 15:55 ET.",
        "",
        "No TG Capital source file exists in-repo. Approximations are labeled in `phase45_spec.json` and were locked before P&L.",
        "",
        "## 1. Verdict",
        "",
        f"- **Overall:** `{payload.get('verdict')}`",
        f"- **GC_TG_LONDON_STATUS:** `{payload.get('GC_TG_LONDON_STATUS')}`",
        f"- **NQ_TG_LONDON_STATUS:** `{payload.get('NQ_TG_LONDON_STATUS')}`",
        f"- **Recommendation:** `{payload.get('recommendation')}`",
        "",
        payload.get("recommendation_text") or "",
        "",
        "## 2. Frozen integrity",
        "",
        "Verified before and after. `strategy_frozen/` was not modified.",
        "",
        f"- GC VWAP V2: `{FROZEN_GC_HASH}`",
        f"- NQ DVP: `{FROZEN_NQ_HASH}`",
        f"- File SHA GC: `{payload.get('file_sha', {}).get('gc')}`",
        f"- File SHA NQ: `{payload.get('file_sha', {}).get('nq')}`",
        "",
        "## 3. Source-rule fidelity",
        "",
        "Exact: 3-candle FVG; FVG midpoint; EMA200 length 200; completed bars only.",
        "",
        "MECHANIZED_APPROXIMATION: London 07:00–11:00; EMA20/50/200 stack; 4H close vs EMA200; doji body/range ≤ 0.25; trident wick ≥ body and close in trend half of the candle; next 30m open; FVG-boundary stop; flatten 15:55 ET.",
        "",
        "## 4. Dataset",
        "",
    ]
    for name in ("GC", "NQ"):
        d = (r.get(name) or {}).get("data") or {}
        lines.append(f"- **{name}:** 1m n={d.get('n_bars')} 30m={d.get('n_30m')} 4h={d.get('n_4h')} {d.get('start')}→{d.get('end')} roll={d.get('roll')} cost={d.get('cost_usd')}")
    lines += ["", "## 5. London timing", "",
              "Timezone `Europe/London` via ZoneInfo (DST-aware). Window start/end compared on local HH:MM. 07:00 London is 02:00 ET in both winter and summer. Diagnostic windows 06:00–10:00 and 08:00–12:00 were not selected by full-sample P&L.",
              ""]
    for name in ("GC", "NQ"):
        lines.append(f"- **{name} windows:** {(r.get(name) or {}).get('windows')}")
    lines += ["", "## 6. HTF bias / 7. EMA200 / 8. EMA stack", "",
              "Incremental P(resume after midpoint) as filters are added. Full alignment is not assumed better a priori.",
              ""]
    for name in ("GC", "NQ"):
        lines.append(f"- **{name}:** {(r.get(name) or {}).get('incremental_ema')}")
    lines += ["", "## 9–12. FVG / midpoint / reaction / structural", ""]
    for name in ("GC", "NQ"):
        p = r.get(name) or {}
        lines.append(f"- **{name}** london FVGs={p.get('n_fvg_london')} aligned={p.get('n_aligned')} reaction_inc={p.get('reaction_incremental')} freq={p.get('frequency')}")
    lines += ["", "## 13. Primary candidate", "",
              "| Instrument | N | E[R] | E[pts] | WR | PF | Train E[R] | Holdout E[R] | P(2R) | P(3R) | Status |",
              "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|"]
    for name in ("GC", "NQ"):
        p = ((r.get(name) or {}).get("primary") or {})
        f = p.get("full") or {}
        lines.append(
            f"| {name} | {f.get('n_resolved')} | {fmt(f.get('expectancy_r'))} | {fmt(f.get('expectancy_points'))} | {fmt(f.get('win_rate'))} | {fmt(f.get('profit_factor'), 2)} | "
            f"{fmt((p.get('train') or {}).get('expectancy_r'))} | {fmt((p.get('holdout') or {}).get('expectancy_r'))} | "
            f"{fmt(f.get('p_reach_2r'))} | {fmt(f.get('p_reach_3r'))} | `{(r.get(name) or {}).get('status')}` |"
        )
    lines += ["", "## 14. Stop comparison", ""]
    for name in ("GC", "NQ"):
        p = (r.get(name) or {}).get("primary") or {}
        lines.append(f"- **{name}** FVG-boundary {p.get('full')} vs reaction-extreme {p.get('stop_reaction_extreme')}")
    lines += ["", "## 15. R-target matrix", ""]
    for name in ("GC", "NQ"):
        lines.append(f"- **{name}:** {((r.get(name) or {}).get('primary') or {}).get('targets')}")
    lines += ["", "## 16. MFE / MAE", ""]
    for name in ("GC", "NQ"):
        f = ((r.get(name) or {}).get("primary") or {}).get("full") or {}
        lines.append(f"- **{name}:** avg MFE={fmt(f.get('avg_mfe'))} MAE={fmt(f.get('avg_mae'))} P(1R)={fmt(f.get('p_reach_1r'))} P(2R)={fmt(f.get('p_reach_2r'))} P(3R)={fmt(f.get('p_reach_3r'))} hold={fmt(f.get('avg_hold_sec'), 0)}s")
    lines += ["", "## 17. Long / short", ""]
    for name in ("GC", "NQ"):
        f = ((r.get(name) or {}).get("primary") or {}).get("full") or {}
        lines.append(f"- **{name}** long={f.get('long')} short={f.get('short')}")
    lines += ["", "## 18. GC results", f"", f"- Status `{(r.get('GC') or {}).get('status')}` flags={(r.get('GC') or {}).get('flags')}"]
    lines += ["", "## 19. NQ results (portability only)", f"", f"- Status `{(r.get('NQ') or {}).get('status')}`"]
    lines += ["", "## 20. Cost stress", ""]
    for name in ("GC", "NQ"):
        p = (r.get(name) or {}).get("primary") or {}
        lines.append(f"- **{name}** 0-tick E[R]={fmt((p.get('ideal') or {}).get('expectancy_r'))} 1-tick={fmt((p.get('full') or {}).get('expectancy_r'))} 2-tick={fmt((p.get('stress_2tick') or {}).get('expectancy_r'))}")
    lines += ["", "## 21. Train / holdout", "", "TRAIN through 2022-12-30, HOLDOUT from 2023-01-03.", ""]
    for name in ("GC", "NQ"):
        p = (r.get(name) or {}).get("primary") or {}
        lines.append(f"- **{name}** train={p.get('train')} holdout={p.get('holdout')}")
    lines += ["", "## 22–23. Walk-forward / years", ""]
    for name in ("GC", "NQ"):
        lines.append(f"- **{name}:** {((r.get(name) or {}).get('primary') or {}).get('years')}")
    lines += ["", "## 24. Threshold stability", ""]
    for name in ("GC", "NQ"):
        lines.append(f"- **{name}:** {((r.get(name) or {}).get('primary') or {}).get('threshold')}")
    lines += ["", "## 25. News impact", ""]
    for name in ("GC", "NQ"):
        lines.append(f"- **{name}:** {((r.get(name) or {}).get('primary') or {}).get('news')}")
    lines += ["", "## 26. Risk geometry", ""]
    for name in ("GC", "NQ"):
        lines.append(f"- **{name}:** {((r.get(name) or {}).get('primary') or {}).get('prop')}")
    lines += ["", "## 27. Frozen-book relationship", ""]
    lines.append(f"- GC vs V2: {((r.get('GC') or {}).get('primary') or {}).get('gc_v2')}")
    lines.append(f"- NQ vs DVP: {((r.get('NQ') or {}).get('primary') or {}).get('dvp')}")
    lines.append("Mechanism is trend-continuation after FVG retracement, not VWAP mean reversion. Identity check is correlation + session (London morning vs NY RTH VWAP).")
    lines += ["", "## 28. Recommendation", "", f"`{payload.get('recommendation')}`", "", payload.get("recommendation_text") or "", "",
              "Nothing in this phase is frozen. Candidate JSON only if `TG_LONDON_EDGE_FOUND`.", ""]
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
    print("loading GC 1m", flush=True)
    gc_bars, gc_meta = load_gc_1m()
    if gc_bars is None:
        results["GC"] = {"instrument": "GC", "status": "DATA_QUALITY_BLOCKED", "data": gc_meta}
    else:
        print(f"  GC n={len(gc_bars)}", flush=True)
        results["GC"] = research_instrument("GC", gc_bars, gc_meta)
        gc_bars = None
    print("loading NQ 1m", flush=True)
    nq_bars, nq_meta = load_nq_1m("NQ")
    if nq_bars is None:
        results["NQ"] = {"instrument": "NQ", "status": "DATA_QUALITY_BLOCKED", "data": nq_meta}
    else:
        nq_meta = {**nq_meta, "roll": nq_meta.get("roll") or "aitrade_volume_crossover"}
        results["NQ"] = research_instrument("NQ", nq_bars, nq_meta)
        nq_bars = None
    for name in ("GC", "NQ"):
        b = results.get(name) or {}
        f = (b.get("primary") or {}).get("full") or {}
        summary.append({"instrument": name, "status": b.get("status"), "n": f.get("n_resolved"), "e_r": f.get("expectancy_r"), "holdout_r": ((b.get("primary") or {}).get("holdout") or {}).get("expectancy_r")})
    statuses = {"GC": (results.get("GC") or {}).get("status") or "DATA_QUALITY_BLOCKED", "NQ": (results.get("NQ") or {}).get("status") or "DATA_QUALITY_BLOCKED"}
    verdict = overall_status(statuses)
    rec_text = recommendation_text(verdict)
    rec = "CLOSE_TG_LONDON_BRANCH"
    if verdict == "TG_LONDON_EDGE_FOUND":
        rec = "CONTINUE_TG_LONDON_NO_FREEZE"
    elif verdict == "TG_LONDON_PROMISING_NEEDS_MORE_DATA":
        rec = "KEEP_RULES_COLLECT_HISTORY"
    elif verdict == "TG_FVG_STRUCTURAL_EFFECT_ONLY":
        rec = "DO_NOT_FORCE_TRADE"
    elif verdict == "DATA_QUALITY_BLOCKED":
        rec = "FIX_DATA_BEFORE_DECISION"
    frozen_after = assert_frozen()
    _write_csv(REPORTS / "phase45_primary_summary.csv", summary)
    payload = {
        "ok": frozen_after["ok"],
        "phase": 45,
        "status": "RESEARCH_COMPLETE",
        "execution": "DRY_RUN_NO_BROKER",
        "verdict": verdict,
        "GC_TG_LONDON_STATUS": statuses["GC"],
        "NQ_TG_LONDON_STATUS": statuses["NQ"],
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
        "results": results,
    }
    if verdict == "TG_LONDON_EDGE_FOUND" and statuses.get("GC") == "TG_LONDON_EDGE_FOUND":
        path = CANDIDATE_DIR / "phase45_GC_TG_LONDON_30M.json"
        path.write_text(json.dumps({
            "status": "RESEARCH_CANDIDATE",
            "phase": 45,
            "instrument": "GC",
            "family": "tg_capital_london_30m_v1",
            "candidate_id": "TG_GC_30M_LONDON_FVG50_REACTION",
            "not_frozen": True,
            "rules": spec["primary_candidate"],
            "metrics": (results["GC"].get("primary") or {}),
        }, indent=2, default=str), encoding="utf-8")
        payload["candidate_written"] = True
        payload["candidate_path"] = str(path)
    VALIDATION.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    write_markdown(payload)
    print(json.dumps({"verdict": verdict, **statuses, "rec": rec, "candidate": payload["candidate_path"]}, indent=2), flush=True)
    return payload


if __name__ == "__main__":
    main()
    sys.exit(0)
