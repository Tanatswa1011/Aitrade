"""Phase 38 — ES/NQ RTH opening-range breakout research.

DRY_RUN. No broker. No freeze. Primary OR15 + 1m close + opposite-OR stop + 1R
was declared in phase38_spec.json before this validator inspected P&L.
"""
from __future__ import annotations

import csv
import json
import math
import random
import statistics
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

from bar_dataset import load_dataset
from nq_microstructure_features import quantile_rows, spearman_rho, wilson_ci
from nq_microstructure_models import FROZEN_GC_HASH, FROZEN_NQ_HASH
from nq_pdh_pdl import local_ts, ny_date, rth_bars
from orb_index_engine import (
    INSTRUMENTS,
    US_RTH_HOLIDAYS,
    OrbTrade,
    build_opening_range,
    flatten_ts,
    overnight_hl,
    simulate,
)
from phase34_validate import GC_FILE_SHA, NQ_FILE_SHA, assert_frozen, file_sha256

ROOT = Path(__file__).resolve().parent
NY = ZoneInfo("America/New_York")
REPORTS = ROOT / "reports"
VALIDATION = ROOT / "phase38_validation.json"
SPEC_PATH = ROOT / "phase38_spec.json"
NQ_ROOT = ROOT / "data" / "databento" / "NQ" / "stitched"
ES_ROOT = ROOT / "data" / "databento" / "ES" / "stitched"
CANDIDATE_DIR = ROOT / "strategy_candidates"

TRAIN_END = "2024-12-31"
HOLDOUT_START = "2025-01-02"
PRIMARY = {"or_minutes": 15, "family": "close_1m", "stop": "A_opposite", "target_r": 1.0, "adverse": 1.0}
MIN_RTH = 350


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


def score(trades: list[OrbTrade], *, use_cost: bool = True) -> dict[str, Any]:
    resolved = [t for t in trades if t.outcome in ("TARGET_HIT", "STOP_HIT", "TIME_EXIT")]
    amb = [t for t in trades if t.outcome == "AMBIGUOUS"]
    entered = [t for t in trades if t.status == "ENTERED"]
    pts_attr = "points_after_cost" if use_cost else "points"
    r_attr = "r_after_cost" if use_cost else "r_multiple"

    def _v(t, a):
        x = getattr(t, a)
        return None if x is None else float(x)

    pts = [p for t in resolved if (p := _v(t, pts_attr)) is not None]
    rs = [p for t in resolved if (p := _v(t, r_attr)) is not None]
    wins = [t for t in resolved if (_v(t, pts_attr) or 0) > 0]
    losses = [t for t in resolved if (_v(t, pts_attr) or 0) <= 0]
    win_pts = [p for t in wins if (p := _v(t, pts_attr)) is not None]
    loss_pts = [abs(p) for t in losses if (p := _v(t, pts_attr)) is not None]
    equity = peak = 0.0
    max_dd = 0.0
    streak = max_streak = 0
    for t in resolved:
        p = _v(t, pts_attr)
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
    day_pnl: dict[str, float] = defaultdict(float)
    for t in resolved:
        p = _v(t, pts_attr)
        if p is None:
            continue
        day_pnl[t.trading_date] += p
    longs = [t for t in resolved if t.direction == "LONG"]
    shorts = [t for t in resolved if t.direction == "SHORT"]

    def _side(rows):
        pp = [p for t in rows if (p := _v(t, pts_attr)) is not None]
        if not pp:
            return {"n": 0, "win_rate": None, "expectancy_points": None, "expectancy_r": None}
        w = sum(1 for x in pp if x > 0)
        rr = [p for t in rows if (p := _v(t, r_attr)) is not None]
        return {
            "n": len(rows),
            "win_rate": w / len(rows),
            "expectancy_points": statistics.mean(pp),
            "expectancy_r": None if not rr else statistics.mean(rr),
        }

    risks = [float(t.risk_points) for t in entered if t.risk_points]
    holds = [int(t.hold_sec) for t in resolved if t.hold_sec is not None]
    mfes = [float(t.mfe_points) for t in resolved if t.mfe_points is not None]
    maes = [float(t.mae_points) for t in resolved if t.mae_points is not None]
    daily_loss = [v for v in day_pnl.values() if v < 0]
    return {
        "n_days_or": None,
        "n_entered": len(entered),
        "n_resolved": len(resolved),
        "n_ambiguous": len(amb),
        "n_no_break": sum(1 for t in trades if t.status == "NO_BREAK"),
        "n_target": sum(1 for t in resolved if t.outcome == "TARGET_HIT"),
        "n_stop": sum(1 for t in resolved if t.outcome == "STOP_HIT"),
        "n_time": sum(1 for t in resolved if t.outcome == "TIME_EXIT"),
        "win_rate": None if not resolved else len(wins) / len(resolved),
        "expectancy_points": None if not pts else statistics.mean(pts),
        "expectancy_r": None if not rs else statistics.mean(rs),
        "median_r": None if not rs else statistics.median(rs),
        "avg_win_points": None if not win_pts else statistics.mean(win_pts),
        "avg_loss_points": None if not loss_pts else statistics.mean(loss_pts),
        "profit_factor": None if not loss_pts else (sum(win_pts) / sum(loss_pts) if sum(loss_pts) else None),
        "max_dd_points": abs(max_dd),
        "max_consec_losses": max_streak,
        "n_days": len(day_pnl),
        "worst_day_points": None if not daily_loss else min(daily_loss),
        "avg_stop_points": None if not risks else statistics.mean(risks),
        "p95_stop_points": None if len(risks) < 8 else sorted(risks)[max(0, int(math.ceil(0.95 * len(risks)) - 1))],
        "avg_hold_sec": None if not holds else statistics.mean(holds),
        "avg_mfe": None if not mfes else statistics.mean(mfes),
        "avg_mae": None if not maes else statistics.mean(maes),
        "false_break_rate": None if not entered else sum(1 for t in entered if t.false_break) / len(entered),
        "crossed_opposite_rate": None if not entered else sum(1 for t in entered if t.crossed_opposite) / len(entered),
        "ambiguity_rate": None if not entered else len(amb) / len(entered),
        "long": _side(longs),
        "short": _side(shorts),
        "cost_adjusted": use_cost,
    }


def load_instrument(name: str) -> tuple[Optional[list], dict[str, Any]]:
    if name == "NQ":
        loaded = load_dataset("databento_NQ_stitched", "1m", root=NQ_ROOT)
        meta = {"path": str(NQ_ROOT), "roll": "aitrade_volume_crossover", "source": "local_cache"}
    else:
        loaded = load_dataset("databento_ES_v0", "1m", root=ES_ROOT)
        meta = {"path": str(ES_ROOT), "roll": "databento_ES.v.0", "source": "phase38_download"}
    if not loaded.get("ok"):
        return None, {**meta, "ok": False, "error": loaded.get("error")}
    bars = list(loaded["bars"])
    meta.update({"ok": True, "n_bars": len(bars), "earliest": int(bars[0].time), "latest": int(bars[-1].time)})
    return bars, meta


def index_days(bars: list) -> dict[str, list]:
    out: dict[str, list] = defaultdict(list)
    for b in bars:
        out[ny_date(int(b.time))].append(b)
    for k in out:
        out[k].sort(key=lambda x: int(x.time))
    return dict(out)


def valid_dates(by_date: dict[str, list]) -> list[str]:
    out = []
    for td, rows in sorted(by_date.items()):
        if td in US_RTH_HOLIDAYS:
            continue
        d = date.fromisoformat(td)
        if d.weekday() >= 5:
            continue
        rth = rth_bars(rows, td)
        if len(rth) < MIN_RTH:
            continue
        out.append(td)
    return out


def daily_context(by_date: dict[str, list], dates: list[str]) -> dict[str, dict[str, Any]]:
    ctx = {}
    prev_rth = None
    trs: list[float] = []
    vol_hist: list[dict[int, float]] = []
    for td in dates:
        rows = by_date[td]
        rth = rth_bars(rows, td)
        hi = max(float(b.high) for b in rth)
        lo = min(float(b.low) for b in rth)
        cl = float(rth[-1].close)
        op = float(rth[0].open)
        tr = hi - lo
        gap = None if prev_rth is None else op - float(prev_rth[-1].close)
        prior_ret = None if prev_rth is None else float(prev_rth[-1].close) - float(prev_rth[0].open)
        atr = None if len(trs) < 5 else statistics.mean(trs[-14:])
        rth0 = local_ts(td, "09:30")
        vmap = {int(b.time) - rth0: float(b.volume or 0) for b in rth}
        rel_lookup = {}
        if vol_hist:
            recent = vol_hist[-20:]
            offsets = set(vmap)
            for off in offsets:
                xs = [h[off] for h in recent if off in h]
                if xs:
                    rel_lookup[off] = statistics.median(xs)
        on = overnight_hl({td: rows, **({dates[dates.index(td) - 1]: by_date[dates[dates.index(td) - 1]]} if dates.index(td) else {})}, td)
        ctx[td] = {
            "rth": rth,
            "tr": tr,
            "atr": atr,
            "gap": gap,
            "prior_ret": prior_ret,
            "vol_map": vmap,
            "rel_med": rel_lookup,
            "overnight": on,
        }
        trs.append(tr)
        vol_hist.append(vmap)
        prev_rth = rth
    return ctx


def run_config(instrument: str, ctx: dict[str, dict[str, Any]], dates: list[str], *, or_minutes: int, family: str, stop: str, target_r: float, adverse: float) -> list[OrbTrade]:
    trades = []
    for td in dates:
        c = ctx[td]
        orng = build_opening_range(c["rth"], td, or_minutes)
        if not orng.complete:
            continue
        br_tmp = None
        rel = None
        # rel volume filled after simulate from trade break offset
        on = c.get("overnight")
        overnight_broke = None
        trade = simulate(
            instrument=instrument,
            rth=c["rth"],
            orng=orng,
            family=family,
            stop_mode=stop,
            target_r=target_r,
            adverse_ticks=adverse,
            atr_daily=c.get("atr"),
            gap_points=c.get("gap"),
            overnight_broke=None,
            prior_day_return_pts=c.get("prior_ret"),
        )
        if trade.break_ts is not None:
            rth0 = local_ts(td, "09:30")
            if family == "range_1m":
                off = int(trade.break_ts) - rth0
            elif family == "close_5m":
                off = int(trade.break_ts) - 300 - rth0
            else:
                off = int(trade.break_ts) - 60 - rth0
            med = (c.get("rel_med") or {}).get(off)
            if med and trade.breakout_bar_volume and family != "close_5m":
                trade.rel_volume = float(trade.breakout_bar_volume) / med
            if on is not None:
                oh, ol = on
                if trade.direction == "LONG":
                    trade.overnight_broke = orng.high >= oh or (trade.entry_fill or 0) >= oh
                elif trade.direction == "SHORT":
                    trade.overnight_broke = orng.low <= ol or (trade.entry_fill or 0) <= ol
        trades.append(trade)
    return trades


def slice_dates(trades: list[OrbTrade], start: str, end: str) -> list[OrbTrade]:
    return [t for t in trades if start <= t.trading_date <= end]


def year_rows(trades: list[OrbTrade]) -> list[dict[str, Any]]:
    by: dict[int, list[OrbTrade]] = defaultdict(list)
    for t in trades:
        if t.year:
            by[int(t.year)].append(t)
    return [{"year": y, **score(by[y])} for y in sorted(by)]


def width_buckets(trades: list[OrbTrade]) -> list[dict[str, Any]]:
    resolved = [t for t in trades if t.outcome in ("TARGET_HIT", "STOP_HIT", "TIME_EXIT") and t.or_width]
    pairs = [(float(t.or_width), (t.r_after_cost or 0) > 0) for t in resolved]
    # quantile on width vs win; also mean R per quintile
    q = quantile_rows(pairs, 5) if len(pairs) >= 10 else []
    rows = []
    if not resolved:
        return rows
    ordered = sorted(resolved, key=lambda t: float(t.or_width))
    n = len(ordered)
    for i in range(5):
        a = int(i * n / 5)
        b = int((i + 1) * n / 5)
        chunk = ordered[a:b]
        if not chunk:
            continue
        rs = [float(t.r_after_cost) for t in chunk if t.r_after_cost is not None]
        rows.append({
            "bucket": i + 1,
            "n": len(chunk),
            "mean_width": statistics.mean(float(t.or_width) for t in chunk),
            "win_rate": sum(1 for t in chunk if (t.points_after_cost or 0) > 0) / len(chunk),
            "expectancy_r": None if not rs else statistics.mean(rs),
        })
    return rows


def _signed_buckets(trades: list[OrbTrade], key_fn, labels: list[tuple[str, Any]]) -> list[dict[str, Any]]:
    resolved = [t for t in trades if t.outcome in ("TARGET_HIT", "STOP_HIT", "TIME_EXIT")]
    rows = []
    for name, pred in labels:
        chunk = [t for t in resolved if pred(t)]
        if not chunk:
            rows.append({"bucket": name, "n": 0})
            continue
        rs = [float(t.r_after_cost) for t in chunk if t.r_after_cost is not None]
        rows.append({
            "bucket": name,
            "n": len(chunk),
            "win_rate": sum(1 for t in chunk if (t.points_after_cost or 0) > 0) / len(chunk),
            "expectancy_r": None if not rs else statistics.mean(rs),
        })
    return rows


def gap_buckets(trades: list[OrbTrade]) -> list[dict[str, Any]]:
    def _gap(t: OrbTrade) -> Optional[str]:
        g = t.gap_points
        if g is None:
            return None
        if g > 2.0:
            return "gap_up"
        if g < -2.0:
            return "gap_down"
        return "flat"
    return _signed_buckets(
        trades,
        lambda t: t.gap_points,
        [
            ("gap_up", lambda t: _gap(t) == "gap_up"),
            ("flat", lambda t: _gap(t) == "flat"),
            ("gap_down", lambda t: _gap(t) == "gap_down"),
        ],
    )


def overnight_buckets(trades: list[OrbTrade]) -> list[dict[str, Any]]:
    return _signed_buckets(
        trades,
        lambda t: t.overnight_broke,
        [
            ("broke_overnight", lambda t: t.overnight_broke is True),
            ("inside_overnight", lambda t: t.overnight_broke is False),
            ("unknown", lambda t: t.overnight_broke is None),
        ],
    )


def prior_day_buckets(trades: list[OrbTrade]) -> list[dict[str, Any]]:
    def aligned(t: OrbTrade) -> Optional[bool]:
        r = t.prior_day_return_pts
        if r is None or not t.direction:
            return None
        if t.direction == "LONG":
            return r > 0
        return r < 0
    return _signed_buckets(
        trades,
        lambda t: t.prior_day_return_pts,
        [
            ("aligned", lambda t: aligned(t) is True),
            ("opposed", lambda t: aligned(t) is False),
            ("unknown", lambda t: aligned(t) is None),
        ],
    )


def quintile_feature(trades: list[OrbTrade], attr: str, name: str) -> list[dict[str, Any]]:
    resolved = [t for t in trades if t.outcome in ("TARGET_HIT", "STOP_HIT", "TIME_EXIT") and getattr(t, attr) is not None]
    if len(resolved) < 10:
        return []
    ordered = sorted(resolved, key=lambda t: float(getattr(t, attr)))
    n = len(ordered)
    rows = []
    for i in range(5):
        a = int(i * n / 5)
        b = int((i + 1) * n / 5)
        chunk = ordered[a:b]
        if not chunk:
            continue
        rs = [float(t.r_after_cost) for t in chunk if t.r_after_cost is not None]
        rows.append({
            "feature": name,
            "bucket": i + 1,
            "n": len(chunk),
            "mean_x": statistics.mean(float(getattr(t, attr)) for t in chunk),
            "win_rate": sum(1 for t in chunk if (t.points_after_cost or 0) > 0) / len(chunk),
            "expectancy_r": None if not rs else statistics.mean(rs),
        })
    return rows


def long_short_rows(trades: list[OrbTrade]) -> list[dict[str, Any]]:
    rows = []
    for side in ("LONG", "SHORT"):
        chunk = [t for t in trades if t.direction == side]
        sc = score(chunk)
        rows.append({"side": side, **sc})
    return rows


def structural_only(width: list[dict[str, Any]], timing: list[dict[str, Any]], full_e: Optional[float]) -> bool:
    if full_e is not None and full_e > 0:
        return False
    ers = [r.get("expectancy_r") for r in width if r.get("expectancy_r") is not None]
    if len(ers) >= 4 and max(ers) > 0.05 and (max(ers) - min(ers)) > 0.15:
        return True
    early = next((r for r in timing if r.get("bucket") == "0_15m"), None)
    late = next((r for r in timing if r.get("bucket") == "gt_60m"), None)
    if (
        early and late
        and (early.get("n") or 0) >= 50
        and (late.get("n") or 0) >= 50
        and early.get("expectancy_r") is not None
        and late.get("expectancy_r") is not None
        and abs(early["expectancy_r"] - late["expectancy_r"]) > 0.12
        and max(early["expectancy_r"], late["expectancy_r"]) > 0.02
    ):
        return True
    return False


def timing_buckets(trades: list[OrbTrade]) -> list[dict[str, Any]]:
    resolved = [t for t in trades if t.outcome in ("TARGET_HIT", "STOP_HIT", "TIME_EXIT") and t.break_lag_sec is not None]
    bins = [(0, 900, "0_15m"), (900, 1800, "15_30m"), (1800, 3600, "30_60m"), (3600, 10**9, "gt_60m")]
    rows = []
    for lo, hi, name in bins:
        chunk = [t for t in resolved if lo <= int(t.break_lag_sec) < hi]
        if not chunk:
            rows.append({"bucket": name, "n": 0})
            continue
        rs = [float(t.r_after_cost) for t in chunk if t.r_after_cost is not None]
        rows.append({
            "bucket": name,
            "n": len(chunk),
            "win_rate": sum(1 for t in chunk if (t.points_after_cost or 0) > 0) / len(chunk),
            "expectancy_r": None if not rs else statistics.mean(rs),
        })
    return rows


def monte_carlo(trades: list[OrbTrade], n: int = 300) -> dict[str, Any]:
    resolved = [t for t in trades if t.r_after_cost is not None and t.outcome in ("TARGET_HIT", "STOP_HIT", "TIME_EXIT")]
    xs = [float(t.r_after_cost) for t in resolved]
    if len(xs) < 30:
        return {"ok": False, "n": len(xs)}
    rng = random.Random(38)
    terminals = []
    dds = []
    for _ in range(n):
        ys = list(xs)
        rng.shuffle(ys)
        eq = peak = 0.0
        dd = 0.0
        for y in ys:
            eq += y
            peak = max(peak, eq)
            dd = min(dd, eq - peak)
        terminals.append(eq)
        dds.append(dd)
    terminals.sort()
    dds.sort()
    return {
        "ok": True,
        "n_trades": len(xs),
        "n_shuffles": n,
        "mean_terminal_r": statistics.mean(terminals),
        "p05_terminal_r": terminals[int(0.05 * (n - 1))],
        "p50_terminal_r": statistics.median(terminals),
        "p05_maxdd_r": abs(dds[int(0.05 * (n - 1))]),
        "median_maxdd_r": abs(statistics.median(dds)),
    }


def dvp_compare(entered: list[OrbTrade]) -> dict[str, Any]:
    path = ROOT / "journal" / "phase29_nq_drift_vwap" / "trades.jsonl"
    dvp = []
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                dvp.append(json.loads(line))
    days_s = {t.trading_date for t in entered}
    days_d = {t.get("trading_date") for t in dvp}
    day_s: dict[str, float] = defaultdict(float)
    day_d: dict[str, float] = defaultdict(float)
    for t in entered:
        if t.points_after_cost is not None:
            day_s[t.trading_date] += float(t.points_after_cost)
    for row in dvp:
        if row.get("points") is not None:
            day_d[str(row.get("trading_date"))] += float(row["points"])
    common = sorted(set(day_s) & set(day_d))
    corr = None
    if len(common) >= 8:
        xs = [day_s[d] for d in common]
        ys = [day_d[d] for d in common]
        mx, my = statistics.mean(xs), statistics.mean(ys)
        num = sum((a - mx) * (b - my) for a, b in zip(xs, ys))
        den = math.sqrt(sum((a - mx) ** 2 for a in xs) * sum((b - my) ** 2 for b in ys))
        corr = None if den == 0 else num / den
    return {
        "n_entered_days": len(days_s),
        "same_day_overlap": len(days_s & days_d),
        "daily_pnl_correlation": corr,
        "n_days_for_corr": len(common),
        "note": "Read-only vs frozen NQ DVP historical trades. No combination.",
    }


def decide_one(full: dict[str, Any], hold: dict[str, Any], years: list[dict[str, Any]], n_full: int, n_hold: int) -> str:
    e = full.get("expectancy_r")
    eh = hold.get("expectancy_r")
    pos_y = sum(1 for r in years if (r.get("expectancy_r") or 0) > 0)
    if n_full < 80 or n_hold < 20:
        return "BREAKOUT_PROMISING_NEEDS_MORE_DATA" if e is not None and e > 0 else "DATA_QUALITY_BLOCKED"
    if e is not None and e > 0 and eh is not None and eh > 0 and n_full >= 200 and n_hold >= 50 and pos_y >= 4:
        return "BREAKOUT_EDGE_FOUND"
    if e is not None and e > 0 and (eh or 0) > 0 and pos_y >= 3:
        return "BREAKOUT_PROMISING_NEEDS_MORE_DATA"
    if e is not None and e > 0 and (eh is None or eh <= 0):
        return "BREAKOUT_EDGE_WEAK"
    return "BREAKOUT_EDGE_REJECTED"


def overall_status(es_s: str, nq_s: str) -> str:
    rank = {
        "BREAKOUT_EDGE_FOUND": 5,
        "BREAKOUT_PROMISING_NEEDS_MORE_DATA": 4,
        "BREAKOUT_STRUCTURAL_EFFECT_ONLY": 3,
        "BREAKOUT_EDGE_WEAK": 2,
        "BREAKOUT_EDGE_REJECTED": 1,
        "DATA_QUALITY_BLOCKED": 0,
    }
    a, b = rank.get(es_s, 0), rank.get(nq_s, 0)
    # if both rejected -> rejected; if one found -> found only if not the other blocked-only
    if max(a, b) >= 5:
        return "BREAKOUT_EDGE_FOUND"
    if max(a, b) >= 4:
        return "BREAKOUT_PROMISING_NEEDS_MORE_DATA"
    if max(a, b) >= 2:
        return "BREAKOUT_EDGE_WEAK" if max(a, b) == 2 else "BREAKOUT_STRUCTURAL_EFFECT_ONLY"
    if max(a, b) == 1:
        return "BREAKOUT_EDGE_REJECTED"
    return "DATA_QUALITY_BLOCKED"


def research_instrument(name: str) -> dict[str, Any]:
    print(f"=== {name} ===", flush=True)
    bars, meta = load_instrument(name)
    if bars is None:
        return {"instrument": name, "status": "DATA_QUALITY_BLOCKED", "data": meta}
    print(f"loaded {len(bars)} bars", flush=True)
    by_date = index_days(bars)
    dates = valid_dates(by_date)
    dates = [d for d in dates if "2020-01-02" <= d <= "2026-08-14"]
    print(f"valid RTH days {len(dates)} {dates[0] if dates else None} -> {dates[-1] if dates else None}", flush=True)
    ctx = daily_context(by_date, dates)
    train_dates = [d for d in dates if d <= TRAIN_END]
    hold_dates = [d for d in dates if d >= HOLDOUT_START]
    matrix = []
    families = ["range_1m", "close_1m"]
    for or_m in (5, 15, 30):
        fams = list(families) + (["close_5m"] if or_m == 15 else [])
        for fam in fams:
            for tgt in (0.5, 1.0, 1.5, 2.0, 3.0):
                print(f"  {name} OR{or_m} {fam} {tgt}R ...", flush=True)
                trades = run_config(name, ctx, dates, or_minutes=or_m, family=fam, stop="A_opposite", target_r=tgt, adverse=1.0)
                sc = score(trades)
                sc_tr = score(slice_dates(trades, "2020-01-02", TRAIN_END))
                sc_ho = score(slice_dates(trades, HOLDOUT_START, "2026-08-14"))
                matrix.append({
                    "instrument": name,
                    "or_minutes": or_m,
                    "entry_family": fam,
                    "stop": "A_opposite",
                    "target_r": tgt,
                    "adverse_ticks": 1,
                    "full": sc,
                    "train": sc_tr,
                    "holdout": sc_ho,
                    "n_or_days": sum(1 for t in trades if t.status != "INIT"),
                })
    # primary
    print(f"  {name} primary overlays ...", flush=True)
    prim = run_config(name, ctx, dates, or_minutes=15, family="close_1m", stop="A_opposite", target_r=1.0, adverse=1.0)
    fills = {}
    for adv in (0.0, 1.0, 2.0):
        tr = run_config(name, ctx, dates, or_minutes=15, family="close_1m", stop="A_opposite", target_r=1.0, adverse=adv)
        fills[f"{int(adv)}_tick"] = {"full": score(tr), "holdout": score(slice_dates(tr, HOLDOUT_START, "2026-08-14"))}
    stop_diag = {}
    for sm in ("B_mid", "C_atr"):
        tr = run_config(name, ctx, dates, or_minutes=15, family="close_1m", stop=sm, target_r=1.0, adverse=1.0)
        stop_diag[sm] = {"full": score(tr), "holdout": score(slice_dates(tr, HOLDOUT_START, "2026-08-14"))}
    years = year_rows(prim)
    wf = []
    for y in range(2020, 2027):
        chunk = [t for t in prim if t.year == y]
        wf.append({"block": y, "start": f"{y}-01-01", "end": f"{y}-12-31", **score(chunk)})
    sc_full = score(prim)
    sc_tr = score(slice_dates(prim, "2020-01-02", TRAIN_END))
    sc_ho = score(slice_dates(prim, HOLDOUT_START, "2026-08-14"))
    status = decide_one(sc_full, sc_ho, years, sc_full.get("n_resolved") or 0, sc_ho.get("n_resolved") or 0)
    tick1 = (fills.get("1_tick") or {}).get("full") or {}
    if status == "BREAKOUT_EDGE_FOUND":
        if (tick1.get("expectancy_r") or 0) <= 0 or (sc_ho.get("expectancy_r") or 0) <= 0:
            status = "BREAKOUT_EDGE_WEAK"
        ideal = (fills.get("0_tick") or {}).get("full") or {}
        if (tick1.get("expectancy_r") or 0) <= 0 and (ideal.get("expectancy_r") or 0) > 0:
            status = "BREAKOUT_EDGE_REJECTED"
    wrows = width_buckets(prim)
    trows = timing_buckets(prim)
    if status in ("BREAKOUT_EDGE_REJECTED", "BREAKOUT_EDGE_WEAK") and structural_only(wrows, trows, sc_full.get("expectancy_r")):
        status = "BREAKOUT_STRUCTURAL_EFFECT_ONLY"
    rel_pairs = [(float(t.rel_volume), (t.r_after_cost or 0) > 0) for t in prim if t.rel_volume and t.outcome in ("TARGET_HIT", "STOP_HIT", "TIME_EXIT")]
    rel_split = None
    if len(rel_pairs) >= 40:
        med = statistics.median(x for x, _ in rel_pairs)
        lo = [t for t in prim if t.rel_volume is not None and t.rel_volume <= med and t.r_after_cost is not None]
        hi = [t for t in prim if t.rel_volume is not None and t.rel_volume > med and t.r_after_cost is not None]
        rel_split = {
            "median": med,
            "n_lo": len(lo),
            "n_hi": len(hi),
            "e_r_lo": None if not lo else statistics.mean(float(t.r_after_cost) for t in lo),
            "e_r_hi": None if not hi else statistics.mean(float(t.r_after_cost) for t in hi),
        }
    mc = monte_carlo(prim)
    n_or = sum(1 for td in dates if build_opening_range(ctx[td]["rth"], td, 15).complete)
    g_rows = gap_buckets(prim)
    on_rows = overnight_buckets(prim)
    pd_rows = prior_day_buckets(prim)
    atr_rows = quintile_feature(prim, "or_width_over_atr", "or_width_over_atr")
    dist_rows = quintile_feature(prim, "break_distance_points", "break_distance")
    rng_rows = quintile_feature(prim, "breakout_bar_range", "breakout_bar_range")
    ls_rows = long_short_rows(prim)
    out = {
        "instrument": name,
        "status": status,
        "data": meta,
        "n_valid_rth_days": len(dates),
        "n_or15_complete": n_or,
        "train_days": len(train_dates),
        "holdout_days": len(hold_dates),
        "date_start": dates[0] if dates else None,
        "date_end": dates[-1] if dates else None,
        "primary": {
            "config": PRIMARY,
            "full": sc_full,
            "train": sc_tr,
            "holdout": sc_ho,
            "years": years,
            "walkforward": wf,
        },
        "fill_stress": fills,
        "stop_diagnostics": stop_diag,
        "width_buckets": wrows,
        "timing_buckets": trows,
        "gap_buckets": g_rows,
        "overnight_buckets": on_rows,
        "prior_day_buckets": pd_rows,
        "width_over_atr_buckets": atr_rows,
        "break_distance_buckets": dist_rows,
        "breakout_range_buckets": rng_rows,
        "long_short": ls_rows,
        "rel_volume_split": rel_split,
        "monte_carlo": mc,
        "matrix": matrix,
        "portfolio_dvp": dvp_compare([t for t in prim if t.status == "ENTERED"]) if name == "NQ" else None,
        "gc_v2": {
            "gc_paper_empty": (ROOT / "journal" / "phase26_gc_vwap_v2_paper" / "paper_trades.jsonl").stat().st_size == 0
            if (ROOT / "journal" / "phase26_gc_vwap_v2_paper" / "paper_trades.jsonl").exists()
            else True
        },
    }
    _write_csv(REPORTS / f"phase38_{name.lower()}_target_matrix.csv", [
        {"or_minutes": r["or_minutes"], "entry": r["entry_family"], "target_r": r["target_r"],
         "n": r["full"].get("n_resolved"), "wr": r["full"].get("win_rate"),
         "e_r": r["full"].get("expectancy_r"), "e_pts": r["full"].get("expectancy_points"),
         "pf": r["full"].get("profit_factor"), "hold_n": r["holdout"].get("n_resolved"),
         "hold_e_r": r["holdout"].get("expectancy_r"), "train_e_r": r["train"].get("expectancy_r")}
        for r in matrix
    ])
    _write_csv(REPORTS / f"phase38_{name.lower()}_years.csv", years)
    _write_csv(REPORTS / f"phase38_{name.lower()}_width.csv", wrows)
    _write_csv(REPORTS / f"phase38_{name.lower()}_timing.csv", trows)
    _write_csv(REPORTS / f"phase38_{name.lower()}_gap.csv", g_rows)
    _write_csv(REPORTS / f"phase38_{name.lower()}_overnight.csv", on_rows)
    _write_csv(REPORTS / f"phase38_{name.lower()}_prior_day.csv", pd_rows)
    _write_csv(REPORTS / f"phase38_{name.lower()}_width_atr.csv", atr_rows)
    _write_csv(REPORTS / f"phase38_{name.lower()}_break_distance.csv", dist_rows)
    _write_csv(REPORTS / f"phase38_{name.lower()}_long_short.csv", [
        {"side": r["side"], "n": r.get("n_resolved"), "wr": r.get("win_rate"),
         "e_r": r.get("expectancy_r"), "e_pts": r.get("expectancy_points"), "pf": r.get("profit_factor")}
        for r in ls_rows
    ])
    _write_csv(REPORTS / f"phase38_{name.lower()}_fills.csv", [
        {"adverse_ticks": k, "n": v["full"].get("n_resolved"), "e_r": v["full"].get("expectancy_r"),
         "hold_e_r": v["holdout"].get("expectancy_r"), "wr": v["full"].get("win_rate")}
        for k, v in fills.items()
    ])
    _write_csv(REPORTS / f"phase38_{name.lower()}_stops.csv", [
        {"stop": k, "n": v["full"].get("n_resolved"), "e_r": v["full"].get("expectancy_r"),
         "hold_e_r": v["holdout"].get("expectancy_r"), "wr": v["full"].get("win_rate")}
        for k, v in stop_diag.items()
    ])
    return out


def maybe_write_candidate(payload: dict[str, Any]) -> Optional[str]:
    if payload.get("verdict") not in ("BREAKOUT_EDGE_FOUND", "BREAKOUT_PROMISING_NEEDS_MORE_DATA"):
        return None
    # pick one: prefer FOUND instrument, else best holdout E[R] among promising
    picks = []
    for inst in ("ES", "NQ"):
        block = payload.get("results", {}).get(inst) or {}
        if block.get("status") in ("BREAKOUT_EDGE_FOUND", "BREAKOUT_PROMISING_NEEDS_MORE_DATA"):
            prim = (block.get("primary") or {}).get("holdout") or {}
            picks.append((inst, block["status"], prim.get("expectancy_r") or -9e9))
    if not picks:
        return None
    picks.sort(key=lambda x: (x[1] == "BREAKOUT_EDGE_FOUND", x[2]), reverse=True)
    inst = picks[0][0]
    path = CANDIDATE_DIR / f"phase38_{inst}_OPENING_RANGE_BREAKOUT.json"
    doc = {
        "status": "RESEARCH_CANDIDATE",
        "phase": 38,
        "instrument": inst,
        "family": "index_rth_opening_range_breakout_v1",
        "candidate_id": "OR15_B_STOPA_1R",
        "not_frozen": True,
        "rules": json.loads(SPEC_PATH.read_text(encoding="utf-8"))["primary_candidate"],
        "metrics": (payload["results"][inst].get("primary") or {}),
    }
    path.write_text(json.dumps(doc, indent=2, default=str), encoding="utf-8")
    return str(path)


def main() -> dict[str, Any]:
    frozen_before = assert_frozen()
    if not frozen_before["ok"]:
        payload = {"ok": False, "status": "FROZEN_INTEGRITY_FAILED", "frozen_before": frozen_before}
        VALIDATION.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return payload
    spec = json.loads(SPEC_PATH.read_text(encoding="utf-8"))
    results = {}
    for name in ("ES", "NQ"):
        results[name] = research_instrument(name)
    es_s = results["ES"].get("status") or "DATA_QUALITY_BLOCKED"
    nq_s = results["NQ"].get("status") or "DATA_QUALITY_BLOCKED"
    verdict = overall_status(es_s, nq_s)
    summary = []
    ranked = []
    for inst in ("ES", "NQ"):
        block = results[inst]
        prim = block.get("primary") or {}
        summary.append({
            "instrument": inst,
            "status": block.get("status"),
            "n_days": block.get("n_valid_rth_days"),
            "date_start": block.get("date_start"),
            "date_end": block.get("date_end"),
            "full_n": (prim.get("full") or {}).get("n_resolved"),
            "full_wr": (prim.get("full") or {}).get("win_rate"),
            "full_e_r": (prim.get("full") or {}).get("expectancy_r"),
            "full_e_pts": (prim.get("full") or {}).get("expectancy_points"),
            "full_pf": (prim.get("full") or {}).get("profit_factor"),
            "train_e_r": (prim.get("train") or {}).get("expectancy_r"),
            "hold_n": (prim.get("holdout") or {}).get("n_resolved"),
            "hold_e_r": (prim.get("holdout") or {}).get("expectancy_r"),
            "ambiguity_rate": (prim.get("full") or {}).get("ambiguity_rate"),
            "false_break_rate": (prim.get("full") or {}).get("false_break_rate"),
        })
        for r in block.get("matrix") or []:
            ranked.append({
                "instrument": inst,
                "or_minutes": r.get("or_minutes"),
                "entry": r.get("entry_family"),
                "target_r": r.get("target_r"),
                "n": (r.get("full") or {}).get("n_resolved"),
                "wr": (r.get("full") or {}).get("win_rate"),
                "e_r": (r.get("full") or {}).get("expectancy_r"),
                "e_pts": (r.get("full") or {}).get("expectancy_points"),
                "pf": (r.get("full") or {}).get("profit_factor"),
                "train_e_r": (r.get("train") or {}).get("expectancy_r"),
                "hold_n": (r.get("holdout") or {}).get("n_resolved"),
                "hold_e_r": (r.get("holdout") or {}).get("expectancy_r"),
            })
    _write_csv(REPORTS / "phase38_primary_summary.csv", summary)
    _write_csv(REPORTS / "phase38_ranked_matrix.csv", ranked)
    frozen_after = assert_frozen()
    payload = {
        "ok": frozen_after["ok"],
        "phase": 38,
        "status": "RESEARCH_COMPLETE",
        "execution": "DRY_RUN_NO_BROKER",
        "verdict": verdict,
        "ES_ORB_STATUS": es_s,
        "NQ_ORB_STATUS": nq_s,
        "frozen_before": {**frozen_before, "gc": FROZEN_GC_HASH, "nq": FROZEN_NQ_HASH},
        "frozen_after": {**frozen_after, "gc": FROZEN_GC_HASH, "nq": FROZEN_NQ_HASH},
        "file_sha": {
            "gc": file_sha256(ROOT / "strategy_frozen" / "gc_vwap_v2_phase26.json"),
            "nq": file_sha256(ROOT / "strategy_frozen" / "nq_dvp_phase30.json"),
            "gc_expected": GC_FILE_SHA,
            "nq_expected": NQ_FILE_SHA,
        },
        "spec": spec,
        "news": {
            "bls_0830_pm5_rth_entries_removed": 0,
            "note": "CPI/NFP 08:30 +/- 5m never overlaps RTH 09:30 entries. No complete 10:00 or FOMC calendar was applied as a filter.",
        },
        "results": results,
        "candidate_written": False,
    }
    cand = maybe_write_candidate(payload)
    payload["candidate_written"] = bool(cand)
    payload["candidate_path"] = cand
    VALIDATION.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    print(json.dumps({"verdict": verdict, "ES": es_s, "NQ": nq_s, "candidate": cand}, indent=2), flush=True)
    return payload


if __name__ == "__main__":
    main()
    sys.exit(0)
