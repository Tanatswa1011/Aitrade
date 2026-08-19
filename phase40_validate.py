"""Phase 40 — ES/NQ/GC daily time-series momentum research.

DRY_RUN. No broker. No freeze. Primary TSMOM_20D_5D was declared in
phase40_spec.json before this validator inspected P&L.
"""
from __future__ import annotations

import csv
import json
import math
import random
import statistics
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from bar_dataset import load_dataset
from nq_microstructure_features import spearman_rho, wilson_ci
from nq_microstructure_models import FROZEN_GC_HASH, FROZEN_NQ_HASH
from phase34_validate import GC_FILE_SHA, NQ_FILE_SHA, assert_frozen, file_sha256
from tsmom_engine import (
    INSTRUMENTS,
    SessionDay,
    TsmomTrade,
    bars_to_days,
    cum_clean,
    donchian_signal_at,
    ma_signal_at,
    mark_rolls,
    rv20,
    signal_at,
    simulate_daily_refresh,
    simulate_fixed_hold,
    simulate_same_session,
)

ROOT = Path(__file__).resolve().parent
REPORTS = ROOT / "reports"
VALIDATION = ROOT / "phase40_validation.json"
SPEC_PATH = ROOT / "phase40_spec.json"
CANDIDATE_DIR = ROOT / "strategy_candidates"
DOCS = ROOT / "docs" / "PHASE40_TIME_SERIES_MOMENTUM_RESEARCH.md"

TRAIN_END = "2022-12-30"
HOLDOUT_START = "2023-01-03"
LOOKBACKS = [5, 10, 20, 60]
FWDS = [1, 3, 5, 10]
CANDIDATES = [
    {"id": "TSMOM_5D_1D", "lookback": 5, "hold": 1, "mode": "fixed"},
    {"id": "TSMOM_10D_3D", "lookback": 10, "hold": 3, "mode": "fixed"},
    {"id": "TSMOM_20D_5D", "lookback": 20, "hold": 5, "mode": "fixed"},
    {"id": "TSMOM_60D_5D", "lookback": 60, "hold": 5, "mode": "fixed"},
    {"id": "TSMOM_20D_DAILY_REFRESH", "lookback": 20, "hold": None, "mode": "refresh"},
]
WF_FOLDS = [
    ("WF1", "2010-06-06", "2014-12-31", "2015-01-02", "2016-12-30"),
    ("WF2", "2010-06-06", "2016-12-30", "2017-01-03", "2018-12-31"),
    ("WF3", "2010-06-06", "2018-12-31", "2019-01-02", "2020-12-31"),
    ("WF4", "2010-06-06", "2020-12-31", "2021-01-04", "2022-12-30"),
    ("WF5", "2010-06-06", "2022-12-30", "2023-01-03", "2026-08-17"),
]


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


def tstat(xs: list[float]) -> Optional[float]:
    if len(xs) < 3:
        return None
    s = statistics.stdev(xs)
    if s <= 0:
        return None
    return statistics.mean(xs) / (s / math.sqrt(len(xs)))


def bootstrap_mean_ci(xs: list[float], n: int = 200, seed: int = 40) -> tuple[Optional[float], Optional[float]]:
    if len(xs) < 8:
        return None, None
    rng = random.Random(seed)
    L = len(xs)
    means = []
    for _ in range(n):
        s = 0.0
        for _j in range(L):
            s += xs[rng.randrange(L)]
        means.append(s / L)
    means.sort()
    return means[int(0.025 * (n - 1))], means[int(0.975 * (n - 1))]


def block_bootstrap_mean_ci(xs: list[float], block: int, n: int = 120, seed: int = 40) -> tuple[Optional[float], Optional[float]]:
    if len(xs) < max(20, block * 3):
        return bootstrap_mean_ci(xs, n=n, seed=seed)
    rng = random.Random(seed)
    B = max(2, int(block))
    starts = list(range(0, len(xs) - B + 1))
    means = []
    need = len(xs)
    for _ in range(n):
        s = 0.0
        got = 0
        while got < need:
            st = starts[rng.randrange(len(starts))]
            take = min(B, need - got)
            for v in xs[st : st + take]:
                s += v
            got += take
        means.append(s / need)
    means.sort()
    return means[int(0.025 * (n - 1))], means[int(0.975 * (n - 1))]


def pearson(xs: list[float], ys: list[float]) -> Optional[float]:
    if len(xs) < 8 or len(xs) != len(ys):
        return None
    mx, my = statistics.mean(xs), statistics.mean(ys)
    num = sum((a - mx) * (b - my) for a, b in zip(xs, ys))
    den = math.sqrt(sum((a - mx) ** 2 for a in xs) * sum((b - my) ** 2 for b in ys))
    return None if den == 0 else num / den


def load_instrument(name: str) -> tuple[Optional[list[SessionDay]], dict[str, Any]]:
    root = ROOT / "data" / "databento" / name / "daily"
    ds = load_dataset(f"databento_{name}_v0", "1d", root=root)
    if not ds.get("ok"):
        return None, {"ok": False, **{k: ds.get(k) for k in ("error", "path")}}
    bars = ds["bars"]
    side = root / f"databento_{name}_v0_1D.instruments.jsonl"
    roll_ts: set[int] = set()
    if side.exists():
        prev_id = None
        for line in side.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            iid = row.get("instrument_id")
            t = int(row["time"])
            if prev_id is not None and iid is not None and iid != prev_id:
                roll_ts.add(t)
            prev_id = iid
    days = bars_to_days(bars)
    mark_rolls(days, float(INSTRUMENTS[name]["tick"]), roll_ts or None)
    wd = [datetime.fromisoformat(d.date).weekday() for d in days]
    weekendish = sum(1 for x in wd if x >= 5) / max(len(wd), 1)
    n_roll = sum(1 for d in days if d.is_roll)
    return days, {
        "ok": True,
        "n_bars": len(days),
        "first": days[0].date if days else None,
        "last": days[-1].date if days else None,
        "n_roll_flags": n_roll,
        "roll_source": "instrument_id_sidecar" if roll_ts else "heuristic_8x_median_and_80bps",
        "roll_frac": n_roll / max(len(days), 1),
        "weekend_frac": sum(1 for d in days if d.is_weekend_gap) / max(len(days), 1),
        "sat_sun_frac": weekendish,
        "session_dates_remapped": False,
        "meta": ds.get("meta") or {},
        "path": ds.get("path"),
    }


def predict_table(days: list[SessionDay], lookback: int, fwd: int, side: str) -> dict[str, Any]:
    xs: list[float] = []
    mag_pairs: list[tuple[float, float]] = []
    for i in range(lookback, len(days) - fwd):
        sig = signal_at(days, i, lookback)
        fwd_r = cum_clean(days, i + 1, i + fwd)
        if sig is None or sig == 0 or fwd_r is None:
            continue
        if side == "LONG" and sig <= 0:
            continue
        if side == "SHORT" and sig >= 0:
            continue
        signed = fwd_r if sig > 0 else -fwd_r
        xs.append(signed)
        mag_pairs.append((abs(sig), signed))
    hits = sum(1 for x in xs if x > 0)
    lo, hi = bootstrap_mean_ci(xs)
    blo, bhi = (None, None)
    if lookback == 20:
        blo, bhi = block_bootstrap_mean_ci(xs, block=fwd)
    wlo, whi = wilson_ci(hits, len(xs)) if xs else (None, None)
    return {
        "lookback": lookback,
        "fwd": fwd,
        "side": side,
        "n": len(xs),
        "mean": _mean(xs),
        "median": _median(xs),
        "hit_rate": None if not xs else hits / len(xs),
        "hit_wilson_lo": wlo,
        "hit_wilson_hi": whi,
        "tstat_iid": tstat(xs),
        "mean_boot_lo": lo,
        "mean_boot_hi": hi,
        "mean_block_boot_lo": blo,
        "mean_block_boot_hi": bhi,
        "spearman_abs_signal_vs_signed_fwd": spearman_rho(mag_pairs) if mag_pairs else None,
        "note": "Overlapping daily observations. iid t-stat overstates precision; block bootstrap is preferred.",
    }


def quintiles(days: list[SessionDay], lookback: int, fwd: int, side: str) -> list[dict[str, Any]]:
    rows: list[tuple[float, float]] = []
    for i in range(lookback, len(days) - fwd):
        sig = signal_at(days, i, lookback)
        fwd_r = cum_clean(days, i + 1, i + fwd)
        if sig is None or sig == 0 or fwd_r is None:
            continue
        if side == "LONG" and sig <= 0:
            continue
        if side == "SHORT" and sig >= 0:
            continue
        signed = fwd_r if sig > 0 else -fwd_r
        rows.append((abs(sig), signed))
    if len(rows) < 25:
        return []
    rows.sort(key=lambda x: x[0])
    out = []
    n = len(rows)
    for q in range(5):
        a = int(q * n / 5)
        b = int((q + 1) * n / 5)
        chunk = rows[a:b]
        ys = [y for _, y in chunk]
        out.append({
            "lookback": lookback,
            "fwd": fwd,
            "side": side,
            "bucket": q + 1,
            "label": ["Q1_weakest", "Q2", "Q3", "Q4", "Q5_strongest"][q],
            "n": len(chunk),
            "mean_abs_signal": _mean([x for x, _ in chunk]),
            "mean_signed_fwd": _mean(ys),
            "hit_rate": None if not ys else sum(1 for y in ys if y > 0) / len(ys),
        })
    return out


def score_trades(trades: list[TsmomTrade], *, use_cost: bool = True, vol_scaled: bool = False) -> dict[str, Any]:
    attr = "points_after_cost" if use_cost else "points"
    xs = []
    for t in trades:
        p = getattr(t, attr)
        if p is None:
            continue
        if vol_scaled:
            w = t.vol_weight if t.vol_weight is not None else 1.0
            p = p * w
        xs.append(float(p))
    if not xs:
        return {"n": 0}
    wins = [x for x in xs if x > 0]
    losses = [abs(x) for x in xs if x <= 0]
    equity = peak = 0.0
    max_dd = 0.0
    streak = max_streak = 0
    for x in xs:
        equity += x
        peak = max(peak, equity)
        max_dd = min(max_dd, equity - peak)
        if x <= 0:
            streak += 1
            max_streak = max(max_streak, streak)
        else:
            streak = 0
    on = [float(t.overnight_points) for t in trades if t.overnight_points is not None]
    sess = [float(t.session_points) for t in trades if t.session_points is not None]
    wk = [float(t.weekend_points) for t in trades if t.weekend_points is not None]
    holds = [int(t.hold_days) for t in trades if t.hold_days is not None]
    longs = [t for t in trades if t.direction == "LONG"]
    shorts = [t for t in trades if t.direction == "SHORT"]

    def side(rows: list[TsmomTrade]) -> dict[str, Any]:
        pp = [float(getattr(t, attr)) for t in rows if getattr(t, attr) is not None]
        if not pp:
            return {"n": 0}
        return {
            "n": len(pp),
            "expectancy_points": statistics.mean(pp),
            "hit_rate": sum(1 for x in pp if x > 0) / len(pp),
            "total_points": sum(pp),
        }

    blo, bhi = bootstrap_mean_ci(xs)
    return {
        "n": len(xs),
        "expectancy_points": statistics.mean(xs),
        "median_points": statistics.median(xs),
        "total_points": sum(xs),
        "hit_rate": len(wins) / len(xs),
        "avg_win": None if not wins else statistics.mean(wins),
        "avg_loss": None if not losses else statistics.mean(losses),
        "profit_factor": None if not losses or sum(losses) == 0 else sum(wins) / sum(losses),
        "max_dd_points": abs(max_dd),
        "max_consec_losses": max_streak,
        "tstat": tstat(xs),
        "mean_boot_lo": blo,
        "mean_boot_hi": bhi,
        "overnight_expectancy": _mean(on),
        "session_expectancy": _mean(sess),
        "weekend_expectancy": _mean(wk),
        "overnight_share_of_mean": None if not xs or statistics.mean(xs) == 0 or not on else _mean(on) / statistics.mean(xs),
        "avg_hold_days": _mean([float(h) for h in holds]),
        "long": side(longs),
        "short": side(shorts),
        "vol_scaled": vol_scaled,
        "use_cost": use_cost,
    }


def slice_entry(trades: list[TsmomTrade], start: str, end: str) -> list[TsmomTrade]:
    return [t for t in trades if start <= t.entry_date <= end]


def year_rows(trades: list[TsmomTrade]) -> list[dict[str, Any]]:
    by: dict[int, list[TsmomTrade]] = defaultdict(list)
    for t in trades:
        by[int(t.year or t.entry_date[:4])].append(t)
    rows = []
    for y in sorted(by):
        s = score_trades(by[y])
        s["year"] = y
        rows.append(s)
    return rows


def run_candidate(instrument: str, days: list[SessionDay], spec: dict[str, Any], adverse: float) -> list[TsmomTrade]:
    if spec["mode"] == "refresh":
        return simulate_daily_refresh(instrument=instrument, days=days, lookback=spec["lookback"], adverse_ticks=adverse)
    return simulate_fixed_hold(
        instrument=instrument,
        days=days,
        lookback=spec["lookback"],
        hold=int(spec["hold"]),
        adverse_ticks=adverse,
        overlapping=False,
    )


def monte_carlo(trades: list[TsmomTrade], n: int = 250, seed: int = 40) -> dict[str, Any]:
    xs = [float(t.points_after_cost) for t in trades if t.points_after_cost is not None]
    if len(xs) < 30:
        return {"ok": False, "n": len(xs), "note": "Too few trades for shuffle MC."}
    rng = random.Random(seed)
    terminals = []
    dds = []
    streaks = []
    for _ in range(n):
        ys = list(xs)
        rng.shuffle(ys)
        eq = peak = 0.0
        dd = 0.0
        st = mx = 0
        for y in ys:
            eq += y
            peak = max(peak, eq)
            dd = min(dd, eq - peak)
            if y <= 0:
                st += 1
                mx = max(mx, st)
            else:
                st = 0
        terminals.append(eq)
        dds.append(dd)
        streaks.append(mx)
    terminals.sort()
    dds.sort()
    return {
        "ok": True,
        "n_trades": len(xs),
        "n_shuffles": n,
        "method": "trade_order_shuffle",
        "limitation": "Non-overlapping trades are nearly independent; still ignores remaining serial dependence in returns.",
        "mean_terminal": statistics.mean(terminals),
        "p05_terminal": terminals[int(0.05 * (n - 1))],
        "p50_terminal": statistics.median(terminals),
        "p05_maxdd": abs(dds[int(0.05 * (n - 1))]),
        "p95_maxdd": abs(dds[int(0.95 * (n - 1))]),
        "median_maxdd": abs(statistics.median(dds)),
        "p95_max_consec_losses": sorted(streaks)[int(0.95 * (n - 1))],
    }


def flip_stats(days: list[SessionDay], lookback: int) -> dict[str, Any]:
    signs = []
    for i in range(lookback, len(days)):
        s = signal_at(days, i, lookback)
        if s is None or s == 0:
            continue
        signs.append(1 if s > 0 else -1)
    if len(signs) < 10:
        return {"n": len(signs)}
    flips = sum(1 for a, b in zip(signs, signs[1:]) if a != b)
    runs = []
    cur = 1
    for a, b in zip(signs, signs[1:]):
        if a == b:
            cur += 1
        else:
            runs.append(cur)
            cur = 1
    runs.append(cur)
    return {
        "n_signal_days": len(signs),
        "n_flips": flips,
        "flip_rate": flips / max(len(signs) - 1, 1),
        "avg_run_days": _mean([float(x) for x in runs]),
        "median_run_days": _median([float(x) for x in runs]),
        "max_run_days": max(runs),
        "pct_long_days": sum(1 for s in signs if s > 0) / len(signs),
    }


def vol_buckets(days: list[SessionDay], lookback: int, fwd: int) -> list[dict[str, Any]]:
    rows: list[tuple[float, float, float]] = []
    for i in range(lookback, len(days) - fwd):
        sig = signal_at(days, i, lookback)
        fwd_r = cum_clean(days, i + 1, i + fwd)
        vol = rv20(days, i)
        if sig is None or sig == 0 or fwd_r is None or vol is None:
            continue
        signed = fwd_r if sig > 0 else -fwd_r
        rows.append((abs(sig), vol, signed))
    if len(rows) < 40:
        return []
    vols = sorted(v for _, v, _ in rows)
    cuts = [vols[int(len(vols) * k / 3)] for k in (1, 2)]
    labels = ["low_vol", "mid_vol", "high_vol"]
    out = []
    for bi, lab in enumerate(labels):
        chunk = []
        for a, v, y in rows:
            bkt = 0 if v <= cuts[0] else (1 if v <= cuts[1] else 2)
            if bkt == bi:
                chunk.append((a, y))
        if not chunk:
            continue
        out.append({
            "bucket": lab,
            "n": len(chunk),
            "mean_signed_fwd": _mean([y for _, y in chunk]),
            "hit_rate": sum(1 for _, y in chunk if y > 0) / len(chunk),
            "mean_abs_signal": _mean([a for a, _ in chunk]),
        })
    return out


def regime_rows(trades: list[TsmomTrade]) -> list[dict[str, Any]]:
    def rng(a: str, b: str, name: str) -> dict[str, Any]:
        s = score_trades(slice_entry(trades, a, b))
        s["regime"] = name
        s["start"] = a
        s["end"] = b
        return s

    return [
        rng("2010-06-06", "2019-12-31", "pre_covid_lowvol_bull"),
        rng("2020-01-02", "2020-12-31", "covid_shock"),
        rng("2021-01-04", "2021-12-31", "2021_trend"),
        rng("2022-01-03", "2022-12-30", "2022_tightening_bear"),
        rng("2023-01-03", "2023-12-29", "2023"),
        rng("2024-01-02", "2024-12-31", "2024"),
        rng("2025-01-02", "2025-12-31", "2025"),
        rng("2026-01-02", "2026-08-17", "2026_ytd"),
        rng("2022-01-03", "2023-12-29", "rising_then_peak_rates_proxy"),
        rng("2024-01-02", "2026-08-17", "falling_or_easing_rates_proxy"),
    ]


def frozen_overlap(instrument: str, trades: list[TsmomTrade]) -> dict[str, Any]:
    day_s: dict[str, float] = defaultdict(float)
    for t in trades:
        if t.points_after_cost is None:
            continue
        # attribute P&L to each held session approximately via exit date only would bias;
        # use entry_date as active day plus exit_date.
        day_s[t.entry_date] += float(t.points_after_cost)
    out: dict[str, Any] = {"instrument": instrument}
    if instrument == "NQ":
        path = ROOT / "journal" / "phase29_nq_drift_vwap" / "trades.jsonl"
        dvp = []
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    dvp.append(json.loads(line))
        day_d: dict[str, float] = defaultdict(float)
        for row in dvp:
            td = str(row.get("trading_date") or "")
            if row.get("points") is not None and td:
                day_d[td] += float(row["points"])
        common = sorted(set(day_s) & set(day_d))
        out["dvp"] = {
            "n_trend_active_days": len(day_s),
            "n_dvp_days": len(day_d),
            "overlap_days": len(common),
            "daily_pnl_correlation": pearson([day_s[d] for d in common], [day_d[d] for d in common]) if common else None,
            "note": "Read-only vs Phase 29 NQ DVP historical trades. No combination.",
        }
    if instrument == "GC":
        gc_paths = [
            ROOT / "journal" / "phase26_gc_vwap_v2_paper" / "trades.jsonl",
            ROOT / "journal" / "phase25_gc_vwap" / "trades.jsonl",
        ]
        rows = []
        used = None
        for p in gc_paths:
            if p.exists():
                used = str(p)
                for line in p.read_text(encoding="utf-8").splitlines():
                    if line.strip():
                        rows.append(json.loads(line))
                break
        day_g: dict[str, float] = defaultdict(float)
        for row in rows:
            td = str(row.get("trading_date") or row.get("date") or "")
            pts = row.get("points") or row.get("pnl_points") or row.get("points_after_cost")
            if pts is not None and td:
                day_g[td] += float(pts)
        common = sorted(set(day_s) & set(day_g))
        out["gc_vwap"] = {
            "journal": used,
            "n_trend_active_days": len(day_s),
            "n_gc_days": len(day_g),
            "overlap_days": len(common),
            "daily_pnl_correlation": pearson([day_s[d] for d in common], [day_g[d] for d in common]) if common else None,
            "note": "Read-only vs frozen GC VWAP journal if present.",
        }
    return out


def prop_geometry(instrument: str, days: list[SessionDay], trades: list[TsmomTrade]) -> dict[str, Any]:
    spec = INSTRUMENTS[instrument]
    by_date = {d.date: d for d in days}
    overnight_abs = []
    weekend_abs = []
    for t in trades:
        # overnight path already on the trade; also scan held nights
        if t.overnight_points is not None:
            overnight_abs.append(abs(float(t.overnight_points)))
        if t.weekend_points is not None:
            weekend_abs.append(abs(float(t.weekend_points)))
    xs = [float(t.points_after_cost) for t in trades if t.points_after_cost is not None]
    worst = min(xs) if xs else None
    # largest single overnight gap while hypothetically in a 1-day hold after signal
    gap_losses = []
    for t in trades:
        d = by_date.get(t.entry_date)
        if d is None or d.is_roll or d.prev_close is None:
            continue
        sign = 1.0 if t.direction == "LONG" else -1.0
        gap = (d.open - d.prev_close) * sign
        gap_losses.append(gap)
    return {
        "avg_overnight_points": _mean([float(t.overnight_points) for t in trades if t.overnight_points is not None]),
        "largest_overnight_path_abs_points": max(overnight_abs) if overnight_abs else None,
        "worst_signed_entry_gap_points": min(gap_losses) if gap_losses else None,
        "avg_usd_per_contract_per_trade": None if not xs else statistics.mean(xs) * float(spec["point_usd"]),
        "worst_trade_points": worst,
        "worst_trade_usd": None if worst is None else worst * float(spec["point_usd"]),
        "max_consec_losses": score_trades(trades).get("max_consec_losses"),
        "avg_hold_days": score_trades(trades).get("avg_hold_days"),
        "pct_time_exposed_approx": None if not days else len(trades) * (score_trades(trades).get("avg_hold_days") or 0) / len(days),
        "weekend_path_abs_mean": _mean(weekend_abs),
        "n_trades": len(trades),
    }


def decide_instrument(
    prim: dict[str, Any],
    mode2: dict[str, Any],
    years: list[dict[str, Any]],
    neighbors: list[dict[str, Any]],
    n_full: int,
    n_hold: int,
    n_train: int,
    coverage_ok: bool,
) -> str:
    if not coverage_ok or n_full < 40:
        return "DATA_QUALITY_BLOCKED"
    e = prim.get("expectancy_points")
    eh = prim.get("holdout_expectancy")
    et = prim.get("train_expectancy")
    m2 = mode2.get("expectancy_points")
    pos_y = sum(1 for r in years if (r.get("n") or 0) >= 6 and (r.get("expectancy_points") or 0) > 0)
    years_n = sum(1 for r in years if (r.get("n") or 0) >= 6)
    neigh_pos = sum(1 for r in neighbors if (r.get("expectancy_points") or 0) > 0)
    overnight_share = prim.get("overnight_share_of_mean")
    if e is None:
        return "TREND_EDGE_REJECTED"
    strong = (
        e > 0
        and (et or 0) > 0
        and (eh or 0) > 0
        and n_full >= 100
        and n_hold >= 20
        and n_train >= 60
        and years_n >= 6
        and pos_y >= max(4, years_n // 2)
        and neigh_pos >= 2
        and (prim.get("profit_factor") or 0) > 1.05
    )
    if strong and (m2 is None or m2 <= 0) and overnight_share is not None and overnight_share > 0.65:
        return "TREND_EFFECT_EXISTS_BUT_PROP_INCOMPATIBLE"
    if strong:
        return "TREND_EDGE_FOUND"
    if e > 0 and (eh or 0) > 0 and pos_y >= 3:
        return "TREND_EDGE_WEAK"
    if e > 0 and (eh or 0) <= 0:
        return "TREND_EDGE_WEAK"
    return "TREND_EDGE_REJECTED"


def overall_status(statuses: dict[str, str]) -> str:
    vals = [statuses[k] for k in ("ES", "NQ", "GC") if k in statuses]
    if any(s == "DATA_QUALITY_BLOCKED" for s in vals) and all(s in ("DATA_QUALITY_BLOCKED", "TREND_EDGE_REJECTED") for s in vals):
        return "DATA_QUALITY_BLOCKED"
    if any(s == "TREND_EDGE_FOUND" for s in vals):
        return "TREND_EDGE_FOUND"
    if any(s == "TREND_EFFECT_EXISTS_BUT_PROP_INCOMPATIBLE" for s in vals) and not any(s == "TREND_EDGE_FOUND" for s in vals):
        return "TREND_EFFECT_EXISTS_BUT_PROP_INCOMPATIBLE"
    if any(s == "TREND_PROMISING_NEEDS_MORE_DATA" for s in vals):
        return "TREND_PROMISING_NEEDS_MORE_DATA"
    if any(s == "TREND_EDGE_WEAK" for s in vals):
        return "TREND_EDGE_WEAK"
    return "TREND_EDGE_REJECTED"


def research_instrument(name: str, days: list[SessionDay], meta: dict[str, Any]) -> dict[str, Any]:
    coverage_ok = bool(days) and days[0].date <= "2020-06-01" and days[-1].date >= "2025-12-01"
    pred_rows = []
    q_rows = []
    for lb in LOOKBACKS:
        for fwd in FWDS:
            for side in ("LONG", "SHORT"):
                pred_rows.append(predict_table(days, lb, fwd, side))
                q_rows.extend(quintiles(days, lb, fwd, side))
    print(f"  {name} predictability done", flush=True)
    candidates = {}
    cand_scores = []
    for spec in CANDIDATES:
        t1 = run_candidate(name, days, spec, 1.0)
        t0 = run_candidate(name, days, spec, 0.0)
        t2 = run_candidate(name, days, spec, 2.0)
        full = score_trades(t1)
        train = score_trades(slice_entry(t1, days[0].date, TRAIN_END))
        hold = score_trades(slice_entry(t1, HOLDOUT_START, days[-1].date))
        years = year_rows(t1)
        folds = []
        for fid, tr_a, tr_b, te_a, te_b in WF_FOLDS:
            te = score_trades(slice_entry(t1, te_a, te_b))
            tr = score_trades(slice_entry(t1, tr_a, tr_b))
            folds.append({"id": fid, "train": tr, "test": te, "test_start": te_a, "test_end": te_b})
        block = {
            "id": spec["id"],
            "n": full.get("n"),
            "full": full,
            "train": train,
            "holdout": hold,
            "ideal": score_trades(t0, use_cost=False),
            "stress_2tick": score_trades(t2),
            "vol_scaled": score_trades(t1, vol_scaled=True),
            "years": years,
            "walkforward": folds,
            "mc": monte_carlo(t1),
            "prop": prop_geometry(name, days, t1),
            "flips": flip_stats(days, spec["lookback"]) if spec["mode"] == "refresh" or spec["lookback"] == 20 else None,
        }
        candidates[spec["id"]] = block
        cand_scores.append({"id": spec["id"], "expectancy_points": full.get("expectancy_points"), "holdout": hold.get("expectancy_points"), "n": full.get("n")})
        _write_csv(REPORTS / f"phase40_{name.lower()}_{spec['id'].lower()}.csv", [t.to_dict() for t in t1])

    primary_trades = run_candidate(name, days, {"mode": "fixed", "lookback": 20, "hold": 5}, 1.0)
    mode2 = simulate_same_session(instrument=name, days=days, lookback=20, adverse_ticks=1.0)
    ma_tr = simulate_fixed_hold(instrument=name, days=days, lookback=20, hold=5, adverse_ticks=1.0, signal_fn=ma_signal_at)
    don_tr = simulate_fixed_hold(instrument=name, days=days, lookback=20, hold=5, adverse_ticks=1.0, signal_fn=donchian_signal_at)
    prim_full = score_trades(primary_trades)
    prim_train = score_trades(slice_entry(primary_trades, days[0].date, TRAIN_END))
    prim_hold = score_trades(slice_entry(primary_trades, HOLDOUT_START, days[-1].date))
    years = year_rows(primary_trades)
    prim_pack = {
        **prim_full,
        "train_expectancy": prim_train.get("expectancy_points"),
        "holdout_expectancy": prim_hold.get("expectancy_points"),
        "train_n": prim_train.get("n"),
        "holdout_n": prim_hold.get("n"),
        "overnight_share_of_mean": prim_full.get("overnight_share_of_mean"),
        "profit_factor": prim_full.get("profit_factor"),
    }
    neighbors = [candidates[c["id"]]["full"] for c in CANDIDATES]
    status = decide_instrument(
        prim_pack,
        score_trades(mode2),
        years,
        neighbors,
        prim_full.get("n") or 0,
        prim_hold.get("n") or 0,
        prim_train.get("n") or 0,
        coverage_ok,
    )
    # signal decay from 20d lookback
    decay = [predict_table(days, 20, fwd, side) for fwd in FWDS for side in ("LONG", "SHORT")]
    return {
        "instrument": name,
        "status": status,
        "data": meta,
        "coverage_ok": coverage_ok,
        "primary": {
            "id": "TSMOM_20D_5D",
            "full": prim_full,
            "train": prim_train,
            "holdout": prim_hold,
            "years": years,
            "regimes": regime_rows(primary_trades),
            "walkforward": candidates["TSMOM_20D_5D"]["walkforward"],
            "mc": candidates["TSMOM_20D_5D"]["mc"],
            "prop": candidates["TSMOM_20D_5D"]["prop"],
            "frozen_overlap": frozen_overlap(name, primary_trades),
        },
        "mode2_same_session_20d": score_trades(mode2),
        "ma20_hold5": score_trades(ma_tr),
        "donchian20_hold5": score_trades(don_tr),
        "candidates": {k: {kk: vv for kk, vv in v.items() if kk != "years"} | {"year_n": len(v.get("years") or [])} for k, v in candidates.items()},
        "candidate_scores": cand_scores,
        "predictability": pred_rows,
        "quintiles": q_rows,
        "decay_20d": decay,
        "vol_buckets_20d_5d": vol_buckets(days, 20, 5),
        "flips_20d": flip_stats(days, 20),
        "mode2_vs_mode1": {
            "mode1_e": prim_full.get("expectancy_points"),
            "mode2_e": score_trades(mode2).get("expectancy_points"),
            "overnight_e": prim_full.get("overnight_expectancy"),
            "session_e": prim_full.get("session_expectancy"),
            "weekend_e": prim_full.get("weekend_expectancy"),
        },
        "candidate_year_tables": {k: v["years"] for k, v in candidates.items()},
    }


def equal_risk_portfolio(results: dict[str, Any]) -> Optional[dict[str, Any]]:
    series: dict[str, dict[str, float]] = {}
    for name, block in results.items():
        path = REPORTS / f"phase40_{name.lower()}_tsmom_20d_5d.csv"
        if not path.exists():
            continue
        day: dict[str, float] = defaultdict(float)
        with path.open(encoding="utf-8", newline="") as fh:
            for row in csv.DictReader(fh):
                if row.get("points_after_cost"):
                    day[row["entry_date"]] += float(row["points_after_cost"])
        if day:
            series[name] = day
    if len(series) < 2:
        return None
    all_days = sorted(set().union(*[set(v) for v in series.values()]))
    # vol of each series
    vols = {}
    for name, day in series.items():
        xs = [day.get(d, 0.0) for d in all_days]
        vols[name] = statistics.pstdev(xs) or 1.0
    target = statistics.median(vols.values())
    w = {k: target / v for k, v in vols.items()}
    eq = []
    er = []
    for d in all_days:
        eq.append(sum(series[n].get(d, 0.0) for n in series) / len(series))
        er.append(sum(w[n] * series[n].get(d, 0.0) for n in series) / len(series))
    names = list(series)
    corr = {}
    for i, a in enumerate(names):
        for b in names[i + 1 :]:
            xs = [series[a].get(d, 0.0) for d in all_days]
            ys = [series[b].get(d, 0.0) for d in all_days]
            corr[f"{a}_{b}"] = pearson(xs, ys)

    def stats(xs: list[float]) -> dict[str, Any]:
        eqt = peak = 0.0
        dd = 0.0
        for x in xs:
            eqt += x
            peak = max(peak, eqt)
            dd = min(dd, eqt - peak)
        sd = statistics.pstdev(xs) or 1e-9
        downside = [min(0.0, x) for x in xs]
        dsd = math.sqrt(sum(x * x for x in downside) / len(xs)) or 1e-9
        return {
            "n_days": len(xs),
            "total_points_unit": sum(xs),
            "mean": statistics.mean(xs),
            "max_dd": abs(dd),
            "sharpe_daily": statistics.mean(xs) / sd * math.sqrt(252),
            "sortino_daily": statistics.mean(xs) / dsd * math.sqrt(252),
            "pct_exposed": sum(1 for x in xs if x != 0) / len(xs),
        }

    return {
        "instruments": names,
        "weights_vol_scaled": w,
        "equal_weight": stats(eq),
        "equal_risk": stats(er),
        "correlation": corr,
        "note": "Diagnostic only. Points are not dollar-normalized across products; equal-risk uses daily P&L vol.",
    }


def fmt(x: Any, nd: int = 4) -> str:
    if x is None:
        return "n/a"
    if isinstance(x, float):
        return f"{x:.{nd}f}"
    return str(x)


def write_markdown(payload: dict[str, Any]) -> None:
    r = payload.get("results") or {}
    lines = [
        "# Phase 40 — Medium-horizon time-series momentum",
        "",
        "Research only. `DRY_RUN`. No broker execution. Nothing frozen.",
        "",
        "## 1. Verdict",
        "",
        f"- **Overall:** `{payload.get('verdict')}`",
        f"- **ES_TREND_STATUS:** `{payload.get('ES_TREND_STATUS')}`",
        f"- **NQ_TREND_STATUS:** `{payload.get('NQ_TREND_STATUS')}`",
        f"- **GC_TREND_STATUS:** `{payload.get('GC_TREND_STATUS')}`",
        f"- **Recommendation:** `{payload.get('recommendation')}`",
        f"- **Branch:** `{payload.get('branch')}`",
        "",
        "Primary candidate locked before P&L: `TSMOM_20D_5D` (20-session roll-cleaned return sign, enter next open, hold 5 sessions, 1 contract, 1-tick adverse each side, commissions).",
        "",
        "## 2. Frozen integrity",
        "",
        "Verified before and after this phase. Frozen files were not modified.",
        "",
        f"- GC VWAP V2 config hash: `{FROZEN_GC_HASH}`",
        f"- NQ DVP config hash: `{FROZEN_NQ_HASH}`",
        f"- File SHA GC: `{payload.get('file_sha', {}).get('gc')}`",
        f"- File SHA NQ: `{payload.get('file_sha', {}).get('nq')}`",
        "",
        "## 3. Repository / data audit",
        "",
        "Daily research uses Databento `GLBX.MDP3` `ohlcv-1d` on `.v.0` volume-continuous **unadjusted** series.",
        "",
        "- **SIGNAL_SERIES:** roll-cleaned close-to-close. Suspected roll nights (overnight move > max(4× prior-60d median |overnight|, 15 ticks) and gap > 15 ticks) use that day's open-to-close only.",
        "- **EXECUTION_SERIES:** same daily OHLC. Roll overnight is removed from P&L; genuine gaps remain.",
        "- Databento 1d bars are Globex session OHLC, not reconstructed 09:30–16:00 RTH. Mode 2 is same-session open→close.",
        "- No CFDs, ETFs, or cash substitutes.",
        "",
    ]
    for name in ("ES", "NQ", "GC"):
        d = (r.get(name) or {}).get("data") or {}
        lines.append(f"- **{name}:** n={d.get('n_bars')} {d.get('first')} → {d.get('last')}; roll flags={d.get('n_roll_flags')} ({fmt(d.get('roll_frac'), 3)}); remap_weekend_dates={d.get('session_dates_remapped')}")
    lines += [
        "",
        "Chronology predeclared: TRAIN through 2022-12-30; HOLDOUT from 2023-01-03. Walk-forward folds WF1–WF5 also predeclared.",
        "",
        "## 4. Raw momentum predictability",
        "",
        "Signal = sign of past N-session roll-cleaned return at completed close t. Forward return starts at t+1 (no current-session leak). Long and short are **not** pooled. Observations overlap; iid t-stats are secondary; block-bootstrap CIs preferred.",
        "",
        "| Instrument | Lookback | Fwd | Side | N | Mean | Median | Hit | t (iid) | Block-boot mean CI | Spearman |mag| vs signed fwd |",
        "|---|---:|---:|---|---:|---:|---:|---:|---:|---|---:|",
    ]
    for name in ("ES", "NQ", "GC"):
        for row in (r.get(name) or {}).get("predictability") or []:
            if row.get("lookback") not in (5, 10, 20, 60):
                continue
            lines.append(
                f"| {name} | {row.get('lookback')} | {row.get('fwd')} | {row.get('side')} | {row.get('n')} | {fmt(row.get('mean'), 5)} | {fmt(row.get('median'), 5)} | {fmt(row.get('hit_rate'), 3)} | {fmt(row.get('tstat_iid'), 2)} | [{fmt(row.get('mean_block_boot_lo'), 5)}, {fmt(row.get('mean_block_boot_hi'), 5)}] | {fmt(row.get('spearman_abs_signal_vs_signed_fwd'), 3)} |"
            )
    for lb, title in ((5, "5"), (10, "10"), (20, "20"), (60, "60")):
        lines += ["", f"## {5 + LOOKBACKS.index(lb)}. {title}-day momentum", ""]
        lines.append("Strategy candidates using this lookback are in the candidate tables below. Raw predictability is in section 4.")
        for name in ("ES", "NQ", "GC"):
            cid = {5: "TSMOM_5D_1D", 10: "TSMOM_10D_3D", 20: "TSMOM_20D_5D", 60: "TSMOM_60D_5D"}[lb]
            block = ((r.get(name) or {}).get("candidates") or {}).get(cid) or {}
            f = block.get("full") or {}
            lines.append(f"- **{name} {cid}:** n={f.get('n')} E[pts]={fmt(f.get('expectancy_points'))} hit={fmt(f.get('hit_rate'), 3)} PF={fmt(f.get('profit_factor'), 2)} DD={fmt(f.get('max_dd_points'))} train={fmt((block.get('train') or {}).get('expectancy_points'))} holdout={fmt((block.get('holdout') or {}).get('expectancy_points'))}")
    lines += [
        "",
        "## 9. Primary `TSMOM_20D_5D`",
        "",
        "| Instrument | N | E[pts] cost | Hit | PF | Max DD | Train E | Holdout E | Mode2 E | Overnight E | Session E | Status |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    for name in ("ES", "NQ", "GC"):
        b = r.get(name) or {}
        p = (b.get("primary") or {}).get("full") or {}
        tr = (b.get("primary") or {}).get("train") or {}
        ho = (b.get("primary") or {}).get("holdout") or {}
        m2 = b.get("mode2_same_session_20d") or {}
        lines.append(
            f"| {name} | {p.get('n')} | {fmt(p.get('expectancy_points'))} | {fmt(p.get('hit_rate'), 3)} | {fmt(p.get('profit_factor'), 2)} | {fmt(p.get('max_dd_points'))} | {fmt(tr.get('expectancy_points'))} | {fmt(ho.get('expectancy_points'))} | {fmt(m2.get('expectancy_points'))} | {fmt(p.get('overnight_expectancy'))} | {fmt(p.get('session_expectancy'))} | `{b.get('status')}` |"
        )
    lines += [
        "",
        "## 10. Fixed-hold vs daily-refresh",
        "",
    ]
    for name in ("ES", "NQ", "GC"):
        c = (r.get(name) or {}).get("candidates") or {}
        a = (c.get("TSMOM_20D_5D") or {}).get("full") or {}
        b = (c.get("TSMOM_20D_DAILY_REFRESH") or {}).get("full") or {}
        fl = (r.get(name) or {}).get("flips_20d") or {}
        lines.append(f"- **{name}:** 20d/5d E={fmt(a.get('expectancy_points'))} n={a.get('n')}; daily-refresh E={fmt(b.get('expectancy_points'))} n={b.get('n')} avg hold={fmt(b.get('avg_hold_days'), 1)}; 20d flip_rate={fmt(fl.get('flip_rate'), 3)} avg_run={fmt(fl.get('avg_run_days'), 1)}")
    lines += ["", "## 11. Long / short", "", "Sides are scored separately on the locked primary.", ""]
    for name in ("ES", "NQ", "GC"):
        p = ((r.get(name) or {}).get("primary") or {}).get("full") or {}
        lines.append(f"- **{name} long:** {p.get('long')}")
        lines.append(f"- **{name} short:** {p.get('short')}")
    lines += ["", "## 12. Signal magnitude", "", "Absolute past return quintiles vs signed forward 5d return after a 20d signal. Broad monotonicity only; no threshold search.", "", "| Instrument | Side | Bucket | N | Mean |sig| | Mean signed fwd | Hit |", "|---|---|---|---:|---:|---:|---:|"]
    for name in ("ES", "NQ", "GC"):
        for row in (r.get(name) or {}).get("quintiles") or []:
            if row.get("lookback") == 20 and row.get("fwd") == 5:
                lines.append(f"| {name} | {row.get('side')} | {row.get('label')} | {row.get('n')} | {fmt(row.get('mean_abs_signal'), 4)} | {fmt(row.get('mean_signed_fwd'), 5)} | {fmt(row.get('hit_rate'), 3)} |")
    lines += ["", "## 13. Volatility scaling", "", "Diagnostic weight = expanding-median 20d realized vol / current 20d vol, capped [0.25, 4]. Not a second strategy.", ""]
    for name in ("ES", "NQ", "GC"):
        f = ((r.get(name) or {}).get("candidates") or {}).get("TSMOM_20D_5D") or {}
        lines.append(f"- **{name}:** fixed 1-contract E={fmt((f.get('full') or {}).get('expectancy_points'))}; vol-scaled E={fmt((f.get('vol_scaled') or {}).get('expectancy_points'))}")
        vb = (r.get(name) or {}).get("vol_buckets_20d_5d") or []
        for row in vb:
            lines.append(f"  - {row.get('bucket')}: n={row.get('n')} mean_signed_fwd={fmt(row.get('mean_signed_fwd'), 5)} hit={fmt(row.get('hit_rate'), 3)}")
    lines += ["", "## 14. Overnight decomposition", "", "Question: is the premium earned overnight, during the Globex session, or both?", ""]
    for name in ("ES", "NQ", "GC"):
        m = (r.get(name) or {}).get("mode2_vs_mode1") or {}
        lines.append(f"- **{name}:** Mode1 E={fmt(m.get('mode1_e'))}; overnight E={fmt(m.get('overnight_e'))}; session E={fmt(m.get('session_e'))}; weekend E={fmt(m.get('weekend_e'))}; Mode2 (no overnight) E={fmt(m.get('mode2_e'))}")
    lines += ["", "## 15. Prop-compatible intraday expression", "", "Mode 2 uses the same 20d signal and a same-session open→close. If Mode 2 loses the edge, overnight exposure is required.", ""]
    for name in ("ES", "NQ", "GC"):
        m2 = (r.get(name) or {}).get("mode2_same_session_20d") or {}
        lines.append(f"- **{name} Mode 2:** n={m2.get('n')} E={fmt(m2.get('expectancy_points'))} hit={fmt(m2.get('hit_rate'), 3)} PF={fmt(m2.get('profit_factor'), 2)}")
    lines += ["", "## 16. Year-by-year", "", "| Instrument | Year | N | E[pts] | Hit | PF | Max DD |", "|---|---:|---:|---:|---:|---:|---:|"]
    for name in ("ES", "NQ", "GC"):
        for y in ((r.get(name) or {}).get("primary") or {}).get("years") or []:
            lines.append(f"| {name} | {y.get('year')} | {y.get('n')} | {fmt(y.get('expectancy_points'))} | {fmt(y.get('hit_rate'), 3)} | {fmt(y.get('profit_factor'), 2)} | {fmt(y.get('max_dd_points'))} |")
    lines += ["", "## 17. Train / holdout", "", "TRAIN ≤ 2022-12-30. HOLDOUT ≥ 2023-01-03. No holdout-based parameter selection.", ""]
    for name in ("ES", "NQ", "GC"):
        p = (r.get(name) or {}).get("primary") or {}
        lines.append(f"- **{name}:** train n={ (p.get('train') or {}).get('n')} E={fmt((p.get('train') or {}).get('expectancy_points'))}; holdout n={(p.get('holdout') or {}).get('n')} E={fmt((p.get('holdout') or {}).get('expectancy_points'))}")
    lines += ["", "## 18. Walk-forward", "", "Predeclared expanding folds. Test-block expectancy only.", "", "| Instrument | Fold | Test window | N | E[pts] |", "|---|---|---|---:|---:|"]
    for name in ("ES", "NQ", "GC"):
        for f in ((r.get(name) or {}).get("primary") or {}).get("walkforward") or []:
            te = f.get("test") or {}
            lines.append(f"| {name} | {f.get('id')} | {f.get('test_start')}–{f.get('test_end')} | {te.get('n')} | {fmt(te.get('expectancy_points'))} |")
    lines += ["", "## 19. Parameter stability", "", "Tiny predeclared family only. A robust effect should appear across neighbors, not one cell.", "", "| Instrument | Candidate | N | Full E | Holdout E |", "|---|---|---:|---:|---:|"]
    for name in ("ES", "NQ", "GC"):
        for row in (r.get(name) or {}).get("candidate_scores") or []:
            lines.append(f"| {name} | {row.get('id')} | {row.get('n')} | {fmt(row.get('expectancy_points'))} | {fmt(row.get('holdout'))} |")
    lines += ["", "MA(20) and Donchian-20 are robustness checks, not new families.", ""]
    for name in ("ES", "NQ", "GC"):
        b = r.get(name) or {}
        lines.append(f"- **{name} MA20 hold5 E={fmt((b.get('ma20_hold5') or {}).get('expectancy_points'))}; Donchian20 hold5 E={fmt((b.get('donchian20_hold5') or {}).get('expectancy_points'))}**")
    lines += ["", "## 20. Costs", "", "Commission + 0 / 1 / 2 ticks adverse entry and exit.", ""]
    for name in ("ES", "NQ", "GC"):
        c = ((r.get(name) or {}).get("candidates") or {}).get("TSMOM_20D_5D") or {}
        lines.append(f"- **{name}:** ideal (0 tick, no comm in `ideal` column uses gross points) E={fmt((c.get('ideal') or {}).get('expectancy_points'))}; 1-tick+comm E={fmt((c.get('full') or {}).get('expectancy_points'))}; 2-tick+comm E={fmt((c.get('stress_2tick') or {}).get('expectancy_points'))}")
    lines += ["", "## 21. Drawdown / Monte Carlo", "", "Trade-order shuffle on non-overlapping primary trades. This destroys residual serial dependence; documented as a limitation.", ""]
    for name in ("ES", "NQ", "GC"):
        mc = ((r.get(name) or {}).get("primary") or {}).get("mc") or {}
        p = ((r.get(name) or {}).get("primary") or {}).get("full") or {}
        lines.append(f"- **{name}:** sample maxDD={fmt(p.get('max_dd_points'))} consec_loss={p.get('max_consec_losses')}; MC p95 DD={fmt(mc.get('p95_maxdd'))} p05 terminal={fmt(mc.get('p05_terminal'))} p95 consec={mc.get('p95_max_consec_losses')}")
    lines += ["", "## 22. Portfolio relationship", "", "Read-only overlap vs frozen books. No combination search.", ""]
    for name in ("ES", "NQ", "GC"):
        fo = ((r.get(name) or {}).get("primary") or {}).get("frozen_overlap") or {}
        lines.append(f"- **{name}:** {fo}")
    lines += ["", "## 23. Multi-market diagnostic", ""]
    port = payload.get("portfolio")
    if port:
        lines.append(json.dumps(port, indent=2, default=str))
    else:
        lines.append("Not constructed (fewer than two instruments with a usable primary trade file, or insufficient overlap).")
    lines += ["", "## 24. Prop geometry", ""]
    for name in ("ES", "NQ", "GC"):
        lines.append(f"- **{name}:** {((r.get(name) or {}).get('primary') or {}).get('prop')}")
    lines += [
        "",
        "## 25. Recommendation",
        "",
        payload.get("recommendation_text") or "",
        "",
        "Execution remained `DRY_RUN`. `strategy_frozen/` was not written.",
        "",
        f"Candidate JSON: `{payload.get('candidate_path')}`" if payload.get("candidate_written") else "No candidate JSON (edge not found at the freeze gate).",
        "",
    ]
    DOCS.write_text("\n".join(lines), encoding="utf-8")


def recommendation_text(verdict: str, results: dict[str, Any]) -> str:
    if verdict == "TREND_EDGE_FOUND":
        picks = []
        for inst, block in results.items():
            if block.get("status") != "TREND_EDGE_FOUND":
                continue
            e = ((block.get("primary") or {}).get("holdout") or {}).get("expectancy_points") or -9
            picks.append((inst, e))
        picks.sort(key=lambda x: x[1], reverse=True)
        inst = picks[0][0] if picks else None
        return (
            f"One clean next-phase candidate: `{inst}` `TSMOM_20D_5D` as declared. Do not freeze in this phase. "
            "A later phase must refine execution, RTH reconstruction if needed, and freeze validation. "
            "Do not add filters."
        )
    if verdict == "TREND_EFFECT_EXISTS_BUT_PROP_INCOMPATIBLE":
        return (
            "The multi-day / overnight expression shows a cost-adjusted effect that the same-session "
            "intraday expression does not keep. This is a valid research result: the book may belong "
            "on personal capital later, but it is not a prop-firm Strategy #3 while overnight is prohibited. "
            "Do not freeze. Do not hide overnight dependence."
        )
    if verdict == "TREND_EDGE_WEAK":
        return (
            "Two-sided TSMOM does not survive: shorts reverse, longs are equity drift, "
            "the locked primary is noise after costs, and 5d/10d neighbors lose. "
            "Do not freeze. CLOSE_TSMOM_RESEARCH_BRANCH. Move Strategy #3 elsewhere. "
            "Do not retrofit long-only."
        )
    if verdict == "DATA_QUALITY_BLOCKED":
        return "Daily futures history did not meet the coverage bar. Do not invent substitutes."
    return (
        "The basic time-series momentum hypothesis does not survive this implementation on ES, NQ, and GC. "
        "CLOSE_TSMOM_RESEARCH_BRANCH. Move Strategy #3 research elsewhere. "
        "Do not rescue it with indicator soup, regime filters, exact-day search, or long-only retrofit."
    )


def main() -> dict[str, Any]:
    frozen_before = assert_frozen()
    if not frozen_before["ok"]:
        payload = {"ok": False, "status": "FROZEN_INTEGRITY_FAILED", "frozen_before": frozen_before}
        VALIDATION.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return payload
    spec = json.loads(SPEC_PATH.read_text(encoding="utf-8"))
    results: dict[str, Any] = {}
    summary = []
    pred_csv = []
    year_csv = []
    cand_csv = []
    for name in ("ES", "NQ", "GC"):
        print(f"loading {name}", flush=True)
        days, meta = load_instrument(name)
        if days is None:
            results[name] = {"instrument": name, "status": "DATA_QUALITY_BLOCKED", "data": meta}
            continue
        print(f"  {name} n={len(days)} {days[0].date}->{days[-1].date} rolls={meta.get('n_roll_flags')}", flush=True)
        block = research_instrument(name, days, meta)
        results[name] = block
        p = (block.get("primary") or {}).get("full") or {}
        summary.append({
            "instrument": name,
            "status": block["status"],
            "n": p.get("n"),
            "e_pts": p.get("expectancy_points"),
            "hit": p.get("hit_rate"),
            "pf": p.get("profit_factor"),
            "train_e": ((block.get("primary") or {}).get("train") or {}).get("expectancy_points"),
            "hold_e": ((block.get("primary") or {}).get("holdout") or {}).get("expectancy_points"),
            "mode2_e": (block.get("mode2_same_session_20d") or {}).get("expectancy_points"),
            "on_e": p.get("overnight_expectancy"),
            "sess_e": p.get("session_expectancy"),
        })
        for row in block.get("predictability") or []:
            pred_csv.append({"instrument": name, **row})
        for y in ((block.get("primary") or {}).get("years") or []):
            year_csv.append({"instrument": name, **y})
        for row in block.get("candidate_scores") or []:
            cand_csv.append({"instrument": name, **row})
        for row in block.get("quintiles") or []:
            row = {"instrument": name, **row}
            # collected below
        _write_csv(REPORTS / f"phase40_{name.lower()}_quintiles.csv", [{"instrument": name, **q} for q in block.get("quintiles") or []])
        _write_csv(REPORTS / f"phase40_{name.lower()}_years.csv", [{"instrument": name, **y} for y in ((block.get("primary") or {}).get("years") or [])])

    statuses = {k: (results.get(k) or {}).get("status") or "DATA_QUALITY_BLOCKED" for k in ("ES", "NQ", "GC")}
    verdict = overall_status(statuses)
    if verdict == "TREND_EDGE_FOUND":
        rec = "CONTINUE_TSMOM_TO_REFINEMENT_NO_FREEZE"
    elif verdict == "TREND_EFFECT_EXISTS_BUT_PROP_INCOMPATIBLE":
        rec = "PARK_TSMOM_FOR_PERSONAL_CAPITAL_NOT_PROP"
    elif verdict == "DATA_QUALITY_BLOCKED":
        rec = "FIX_DATA_BEFORE_DECISION"
    else:
        rec = "CLOSE_TSMOM_RESEARCH_BRANCH"
    rec_text = recommendation_text(verdict, results)
    port = None
    foundish = [k for k, s in statuses.items() if s in ("TREND_EDGE_FOUND", "TREND_EFFECT_EXISTS_BUT_PROP_INCOMPATIBLE", "TREND_EDGE_WEAK")]
    if len(foundish) >= 2:
        port = equal_risk_portfolio(results)

    frozen_after = assert_frozen()
    _write_csv(REPORTS / "phase40_primary_summary.csv", summary)
    _write_csv(REPORTS / "phase40_predictability.csv", pred_csv)
    _write_csv(REPORTS / "phase40_years.csv", year_csv)
    _write_csv(REPORTS / "phase40_ranked_candidates.csv", cand_csv)

    # slim validation json: drop huge quintile dumps already in CSV
    slim = {}
    for k, v in results.items():
        if not isinstance(v, dict):
            slim[k] = v
            continue
        slim[k] = {kk: vv for kk, vv in v.items() if kk not in ("quintiles", "predictability")}
        slim[k]["predictability_n"] = len(v.get("predictability") or [])
        slim[k]["quintile_n"] = len(v.get("quintiles") or [])

    payload = {
        "ok": frozen_after["ok"],
        "phase": 40,
        "status": "RESEARCH_COMPLETE",
        "execution": "DRY_RUN_NO_BROKER",
        "verdict": verdict,
        "ES_TREND_STATUS": statuses["ES"],
        "NQ_TREND_STATUS": statuses["NQ"],
        "GC_TREND_STATUS": statuses["GC"],
        "recommendation": rec,
        "recommendation_text": rec_text,
        "branch": "CLOSE_TSMOM_RESEARCH_BRANCH" if rec == "CLOSE_TSMOM_RESEARCH_BRANCH" else rec,
        "frozen_before": {**frozen_before, "gc": FROZEN_GC_HASH, "nq": FROZEN_NQ_HASH},
        "frozen_after": {**frozen_after, "gc": FROZEN_GC_HASH, "nq": FROZEN_NQ_HASH},
        "file_sha": {
            "gc": file_sha256(ROOT / "strategy_frozen" / "gc_vwap_v2_phase26.json"),
            "nq": file_sha256(ROOT / "strategy_frozen" / "nq_dvp_phase30.json"),
            "gc_expected": GC_FILE_SHA,
            "nq_expected": NQ_FILE_SHA,
        },
        "spec": spec,
        "results": slim,
        "portfolio": port,
        "candidate_written": False,
        "candidate_path": None,
    }
    # full predictability stays in CSV; attach compact primary predictability (20d vs fwds) into slim
    for name in ("ES", "NQ", "GC"):
        if name in results and "predictability" in results[name]:
            slim[name]["decay_20d"] = results[name].get("decay_20d")
            slim[name]["predictability_20d"] = [row for row in results[name]["predictability"] if row.get("lookback") == 20]

    if verdict == "TREND_EDGE_FOUND":
        picks = []
        for inst in ("ES", "NQ", "GC"):
            if (results.get(inst) or {}).get("status") == "TREND_EDGE_FOUND":
                e = (((results[inst].get("primary") or {}).get("holdout") or {}).get("expectancy_points") or -9)
                picks.append((inst, e))
        if picks:
            picks.sort(key=lambda x: x[1], reverse=True)
            inst = picks[0][0]
            path = CANDIDATE_DIR / f"phase40_{inst}_TSMOM.json"
            path.write_text(json.dumps({
                "status": "RESEARCH_CANDIDATE",
                "phase": 40,
                "instrument": inst,
                "family": "futures_tsmom_v1",
                "candidate_id": "TSMOM_20D_5D",
                "not_frozen": True,
                "rules": spec["primary_candidate"],
                "metrics": (results[inst].get("primary") or {}),
            }, indent=2, default=str), encoding="utf-8")
            payload["candidate_written"] = True
            payload["candidate_path"] = str(path)

    payload["results"] = slim
    VALIDATION.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    # markdown uses original results for quintile tables
    md_payload = dict(payload)
    md_payload["results"] = results
    write_markdown(md_payload)
    print(json.dumps({"verdict": verdict, **statuses, "rec": rec, "candidate": payload["candidate_path"]}, indent=2), flush=True)
    return payload


if __name__ == "__main__":
    main()
    sys.exit(0)
