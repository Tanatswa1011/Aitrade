"""Phase 41 — ES/NQ prior-RTH volume-profile auction research.

DRY_RUN. No broker. No freeze. Primary VP_OUTSIDE_ACCEPT_POC was declared
in phase41_spec.json before this validator inspected P&L.

Profile source is DEGRADED 1m volume-at-price, not a trade tape.
"""
from __future__ import annotations

import csv
import json
import math
import random
import statistics
import sys
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path
from typing import Any, Optional

from nq_microstructure_models import FROZEN_GC_HASH, FROZEN_NQ_HASH
from nq_pdh_pdl import local_ts, ny_date, rth_bars
from orb_index_engine import US_RTH_HOLIDAYS
from phase34_validate import GC_FILE_SHA, NQ_FILE_SHA, assert_frozen, file_sha256
from phase38_validate import HOLDOUT_START, MIN_RTH, REPORTS, TRAIN_END, index_days, load_instrument, valid_dates
from volume_profile_engine import (
    VpTrade,
    VolumeProfile,
    build_profile,
    open_class,
    simulate_accept_poc,
    simulate_inside_poc,
    simulate_reject_1r,
    structural_day,
)

ROOT = Path(__file__).resolve().parent
VALIDATION = ROOT / "phase41_validation.json"
SPEC_PATH = ROOT / "phase41_spec.json"
CANDIDATE_DIR = ROOT / "strategy_candidates"
DOCS = ROOT / "docs" / "PHASE41_VOLUME_PROFILE_AUCTION_RESEARCH.md"


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


def score(trades: list[VpTrade], *, use_cost: bool = True) -> dict[str, Any]:
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
        return {
            "n": len(pp),
            "win_rate": sum(1 for x in pp if x > 0) / len(pp),
            "expectancy_points": statistics.mean(pp),
            "expectancy_r": None if not [p for t in rows if _v(t, rattr) is not None] else statistics.mean([_v(t, rattr) for t in rows if _v(t, rattr) is not None]),
        }

    statuses = dict(Counter(t.status for t in trades))
    risks = [float(t.risk_points) for t in entered if t.risk_points]
    return {
        "n_days": len(trades),
        "n_entered": len(entered),
        "n_resolved": len(resolved),
        "n_ambiguous": len(amb),
        "status_counts": statuses,
        "win_rate": None if not resolved else len(wins) / len(resolved),
        "expectancy_points": None if not pts else statistics.mean(pts),
        "expectancy_r": None if not rs else statistics.mean(rs),
        "profit_factor": None if not loss_pts or sum(loss_pts) == 0 else (sum(win_pts) / sum(loss_pts) if win_pts else 0.0),
        "max_dd_points": abs(max_dd),
        "max_consec_losses": max_streak,
        "avg_stop_points": None if not risks else statistics.mean(risks),
        "p95_stop_points": None if len(risks) < 8 else sorted(risks)[max(0, int(math.ceil(0.95 * len(risks)) - 1))],
        "long": side(longs),
        "short": side(shorts),
        "use_cost": use_cost,
    }


def slice_dates(trades: list[VpTrade], start: str, end: str) -> list[VpTrade]:
    return [t for t in trades if start <= t.trading_date <= end]


def year_rows(trades: list[VpTrade]) -> list[dict[str, Any]]:
    by: dict[int, list[VpTrade]] = defaultdict(list)
    for t in trades:
        by[int(t.year or t.trading_date[:4])].append(t)
    return [{"year": y, **score(by[y])} for y in sorted(by)]


def monte_carlo(trades: list[VpTrade], n: int = 250, seed: int = 41) -> dict[str, Any]:
    xs = [float(t.r_after_cost) for t in trades if t.r_after_cost is not None and t.outcome in ("TARGET_HIT", "STOP_HIT", "TIME_EXIT")]
    if len(xs) < 30:
        return {"ok": False, "n": len(xs)}
    rng = random.Random(seed)
    dds = []
    terminals = []
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
        "p05_terminal_r": terminals[int(0.05 * (n - 1))],
        "p50_terminal_r": statistics.median(terminals),
        "p95_maxdd_r": abs(dds[int(0.95 * (n - 1))]),
        "median_maxdd_r": abs(statistics.median(dds)),
    }


def rate(xs: list[bool]) -> Optional[float]:
    return None if not xs else sum(1 for x in xs if x) / len(xs)


def structural_table(rows: list[dict[str, Any]], cls: str) -> dict[str, Any]:
    chunk = [r for r in rows if r.get("open_class") == cls]
    if not chunk:
        return {"n": 0, "open_class": cls}
    return {
        "open_class": cls,
        "n": len(chunk),
        "share": None,
        "P_return_vah": rate([bool(r["return_vah"]) for r in chunk]),
        "P_return_val": rate([bool(r["return_val"]) for r in chunk]),
        "P_enter_value": rate([bool(r["enter_value"]) for r in chunk]),
        "P_touch_poc": rate([bool(r["touch_poc"]) for r in chunk]),
        "P_traverse_full": rate([bool(r["traverse_full_value"]) for r in chunk]),
        "P_continue_away": rate([bool(r["continue_away"]) for r in chunk]),
        "P_reject_1m": rate([bool(r["reject_1m"]) for r in chunk]),
        "P_accept_1m": rate([bool(r["accept_1m"]) for r in chunk]),
        "P_accept_two_1m": rate([bool(r["accept_two_1m"]) for r in chunk]),
        "P_accept_5m": rate([bool(r["accept_5m"]) for r in chunk]),
        "mean_mfe_away": statistics.mean(float(r["mfe_away"]) for r in chunk),
        "mean_mae_into": statistics.mean(float(r["mae_into"]) for r in chunk),
        "exit_side": dict(Counter(r.get("first_exit_side") or "NONE" for r in chunk)),
    }


def decide_one(full: dict[str, Any], hold: dict[str, Any], years: list[dict[str, Any]], n_full: int, n_hold: int, struct_signal: bool) -> str:
    e = full.get("expectancy_r")
    eh = hold.get("expectancy_r")
    pos_y = sum(1 for r in years if (r.get("n_resolved") or 0) >= 8 and (r.get("expectancy_r") or 0) > 0)
    if n_full < 40:
        return "VOLUME_PROFILE_PROMISING_NEEDS_MORE_DATA" if e and e > 0 else "DATA_QUALITY_BLOCKED"
    if e is not None and e > 0 and eh is not None and eh > 0 and n_full >= 150 and n_hold >= 40 and pos_y >= 4 and (full.get("profit_factor") or 0) > 1.1:
        return "VOLUME_PROFILE_EDGE_FOUND"
    if struct_signal and (e is None or e <= 0):
        return "VOLUME_PROFILE_STRUCTURAL_EFFECT_ONLY"
    if e is not None and e > 0 and (eh or 0) > 0:
        return "VOLUME_PROFILE_EDGE_WEAK"
    if e is not None and e > 0:
        return "VOLUME_PROFILE_EDGE_WEAK"
    if struct_signal:
        return "VOLUME_PROFILE_STRUCTURAL_EFFECT_ONLY"
    return "VOLUME_PROFILE_EDGE_REJECTED"


def overall_status(es_s: str, nq_s: str) -> str:
    rank = {
        "VOLUME_PROFILE_EDGE_FOUND": 5,
        "VOLUME_PROFILE_PROMISING_NEEDS_MORE_DATA": 4,
        "VOLUME_PROFILE_STRUCTURAL_EFFECT_ONLY": 3,
        "VOLUME_PROFILE_EDGE_WEAK": 2,
        "VOLUME_PROFILE_EDGE_REJECTED": 1,
        "DATA_QUALITY_BLOCKED": 0,
    }
    m = max(rank.get(es_s, 0), rank.get(nq_s, 0))
    inv = {v: k for k, v in rank.items()}
    if m >= 5:
        return "VOLUME_PROFILE_EDGE_FOUND"
    if m == 4:
        return "VOLUME_PROFILE_PROMISING_NEEDS_MORE_DATA"
    if m == 3:
        return "VOLUME_PROFILE_STRUCTURAL_EFFECT_ONLY"
    if m == 2:
        return "VOLUME_PROFILE_EDGE_WEAK"
    if m == 1:
        return "VOLUME_PROFILE_EDGE_REJECTED"
    return "DATA_QUALITY_BLOCKED"


def dvp_compare(entered: list[VpTrade]) -> dict[str, Any]:
    path = ROOT / "journal" / "phase29_nq_drift_vwap" / "trades.jsonl"
    dvp = []
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                dvp.append(json.loads(line))
    day_s: dict[str, float] = defaultdict(float)
    day_d: dict[str, float] = defaultdict(float)
    for t in entered:
        if t.points_after_cost is not None:
            day_s[t.trading_date] += float(t.points_after_cost)
    for row in dvp:
        td = str(row.get("trading_date") or "")
        if row.get("points") is not None and td:
            day_d[td] += float(row["points"])
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
        "overlap_days": len(set(day_s) & set(day_d)),
        "n_days_for_corr": len(common),
        "daily_pnl_correlation": corr,
        "note": "Read-only vs Phase 29 NQ DVP. No combination.",
    }


def research_instrument(name: str) -> dict[str, Any]:
    print(f"=== {name} ===", flush=True)
    bars, meta = load_instrument(name)
    if bars is None:
        return {"instrument": name, "status": "DATA_QUALITY_BLOCKED", "data": meta}
    print(f"loaded {len(bars)}", flush=True)
    by_date = index_days(bars)
    dates = [d for d in valid_dates(by_date) if "2020-01-02" <= d <= "2026-08-14"]
    print(f"valid days {len(dates)} {dates[0]}->{dates[-1]}", flush=True)
    profiles: dict[str, VolumeProfile] = {}
    struct_rows = []
    trs: list[float] = []
    cand_a = []
    cand_b = []
    cand_b_1r = []
    cand_b_opp = []
    cand_c = []
    cand_b0 = []
    cand_b2 = []
    for i, td in enumerate(dates):
        rth = rth_bars(by_date[td], td)
        prof = build_profile(rth, td)
        if prof:
            profiles[td] = prof
        if i == 0:
            continue
        prev = dates[i - 1]
        prior = profiles.get(prev)
        if prior is None or prior.width <= 0:
            continue
        op = float(rth[0].open)
        cls = open_class(op, prior)
        st = structural_day(rth, prior, td)
        gap = op - float(rth_bars(by_date[prev], prev)[-1].close)
        atr = None if len(trs) < 5 else statistics.mean(trs[-14:])
        prior_range = prior.high - prior.low
        on_inside = None
        prev_rows = by_date[prev]
        cur_pre = [b for b in by_date[td] if int(b.time) < local_ts(td, "09:30")]
        post = [b for b in prev_rows if int(b.time) >= local_ts(prev, "16:00")]
        on_win = post + cur_pre
        if len(on_win) >= 10:
            oh = max(float(b.high) for b in on_win)
            ol = min(float(b.low) for b in on_win)
            on_inside = not (oh < prior.val or ol > prior.vah)
        row = {
            "instrument": name,
            "trading_date": td,
            "prior_date": prev,
            "poc": prior.poc,
            "vah": prior.vah,
            "val": prior.val,
            "width": prior.width,
            "width_over_atr": None if not atr else prior.width / atr,
            "width_over_range": None if prior_range <= 0 else prior.width / prior_range,
            "gap": gap,
            "gap_over_atr": None if not atr else gap / atr,
            "poc_in_range": prior.poc_in_range,
            "vol_above_poc": prior.vol_above_poc,
            "close_vs_value": prior.close_vs_value,
            "overnight_inside_value": on_inside,
            "year": int(td[:4]),
            **st,
        }
        struct_rows.append(row)
        cand_a.append(simulate_reject_1r(instrument=name, td=td, rth=rth, prof=prior, adverse_ticks=1.0))
        cand_b.append(simulate_accept_poc(instrument=name, td=td, rth=rth, prof=prior, adverse_ticks=1.0, target_name="POC"))
        cand_b_1r.append(simulate_accept_poc(instrument=name, td=td, rth=rth, prof=prior, adverse_ticks=1.0, target_name="1R"))
        cand_b_opp.append(simulate_accept_poc(instrument=name, td=td, rth=rth, prof=prior, adverse_ticks=1.0, target_name="opposite_value"))
        cand_c.append(simulate_inside_poc(instrument=name, td=td, rth=rth, prof=prior, adverse_ticks=1.0))
        cand_b0.append(simulate_accept_poc(instrument=name, td=td, rth=rth, prof=prior, adverse_ticks=0.0, target_name="POC"))
        cand_b2.append(simulate_accept_poc(instrument=name, td=td, rth=rth, prof=prior, adverse_ticks=2.0, target_name="POC"))
        trs.append(max(float(b.high) for b in rth) - min(float(b.low) for b in rth))
        if i % 250 == 0:
            print(f"  {name} {td} {i}/{len(dates)}", flush=True)

    n = len(struct_rows)
    loc = dict(Counter(r["open_class"] for r in struct_rows))
    loc_share = {k: v / max(n, 1) for k, v in loc.items()}
    struct = {cls: structural_table(struct_rows, cls) for cls in ("OPEN_ABOVE_VAH", "OPEN_INSIDE_VALUE", "OPEN_BELOW_VAL")}
    for cls, block in struct.items():
        block["share"] = loc_share.get(cls)
    # structural effect: outside opens usually return to boundary more than 50% and accept vs continue is not 50/50
    above = struct["OPEN_ABOVE_VAH"]
    below = struct["OPEN_BELOW_VAL"]
    struct_signal = False
    if (above.get("n") or 0) >= 80 and (below.get("n") or 0) >= 80:
        # both sides: P(enter) clearly != 0.5 or P(continue_away) clearly low/high
        pe = [above.get("P_enter_value"), below.get("P_enter_value")]
        if all(x is not None for x in pe) and (min(pe) >= 0.65 or max(pe) <= 0.35):
            struct_signal = True
        if all(x is not None for x in pe) and abs(pe[0] - pe[1]) < 0.08 and min(pe) >= 0.60:
            struct_signal = True

    primary = cand_b
    sc = score(primary)
    sc_tr = score(slice_dates(primary, dates[0], TRAIN_END))
    sc_ho = score(slice_dates(primary, HOLDOUT_START, dates[-1]))
    years = year_rows(primary)
    status = decide_one(sc, sc_ho, years, sc.get("n_resolved") or 0, sc_ho.get("n_resolved") or 0, struct_signal)

    # width quintiles vs P(enter) for outside opens
    outside = [r for r in struct_rows if r["open_class"] != "OPEN_INSIDE_VALUE" and r.get("width_over_atr")]
    width_q = []
    if len(outside) >= 25:
        outside.sort(key=lambda r: float(r["width_over_atr"]))
        nn = len(outside)
        for q in range(5):
            a, b = int(q * nn / 5), int((q + 1) * nn / 5)
            chunk = outside[a:b]
            width_q.append({
                "bucket": q + 1,
                "n": len(chunk),
                "mean_width_atr": statistics.mean(float(r["width_over_atr"]) for r in chunk),
                "P_enter": rate([bool(r["enter_value"]) for r in chunk]),
                "P_continue_away": rate([bool(r["continue_away"]) for r in chunk]),
                "P_touch_poc": rate([bool(r["touch_poc"]) for r in chunk]),
            })

    tod = []
    for bucket in ("0930_1000", "1000_1130", "1130_1330", "1330_1530"):
        chunk = [t for t in primary if t.status == "ENTERED" and t.signal_hhmm and (
            (bucket == "0930_1000" and t.signal_hhmm < "10:00")
            or (bucket == "1000_1130" and "10:00" <= t.signal_hhmm < "11:30")
            or (bucket == "1130_1330" and "11:30" <= t.signal_hhmm < "13:30")
            or (bucket == "1330_1530" and t.signal_hhmm >= "13:30")
        )]
        tod.append({"bucket": bucket, **score(chunk)})

    wf = []
    for y in range(2020, 2027):
        wf.append({"year": y, **score([t for t in primary if t.year == y])})

    entered = [t for t in primary if t.status == "ENTERED"]
    overlay = {
        "instrument": name,
        "status": status,
        "data": meta,
        "n_profile_days": len(profiles),
        "n_next_days": n,
        "open_location": loc,
        "open_share": loc_share,
        "structural": struct,
        "structural_effect_flag": struct_signal,
        "width_quintiles": width_q,
        "primary": {
            "id": "VP_OUTSIDE_ACCEPT_POC",
            "full": sc,
            "train": sc_tr,
            "holdout": sc_ho,
            "years": years,
            "walkforward": wf,
            "ideal": score(cand_b0, use_cost=False),
            "stress_2tick": score(cand_b2),
            "target_1r": score(cand_b_1r),
            "target_opposite": score(cand_b_opp),
            "tod": tod,
            "mc": monte_carlo(primary),
            "prop": {
                "avg_stop_points": sc.get("avg_stop_points"),
                "p95_stop_points": sc.get("p95_stop_points"),
                "avg_usd_risk": None if not sc.get("avg_stop_points") else sc["avg_stop_points"] * (50.0 if name == "ES" else 20.0),
                "max_consec_losses": sc.get("max_consec_losses"),
                "flatten": "15:55",
                "overnight": False,
            },
            "dvp": dvp_compare(entered) if name == "NQ" else None,
        },
        "cand_A_reject": score(cand_a),
        "cand_A_train": score(slice_dates(cand_a, dates[0], TRAIN_END)),
        "cand_A_holdout": score(slice_dates(cand_a, HOLDOUT_START, dates[-1])),
        "cand_C_inside": score(cand_c),
        "cand_C_train": score(slice_dates(cand_c, dates[0], TRAIN_END)),
        "cand_C_holdout": score(slice_dates(cand_c, HOLDOUT_START, dates[-1])),
        "poc_magnet": {
            "P_touch_poc_open_above": (struct["OPEN_ABOVE_VAH"] or {}).get("P_touch_poc"),
            "P_touch_poc_open_below": (struct["OPEN_BELOW_VAL"] or {}).get("P_touch_poc"),
            "P_touch_poc_open_inside": (struct["OPEN_INSIDE_VALUE"] or {}).get("P_touch_poc"),
        },
    }
    _write_csv(REPORTS / f"phase41_{name.lower()}_structural.csv", struct_rows)
    _write_csv(REPORTS / f"phase41_{name.lower()}_accept_poc.csv", [t.to_dict() for t in primary if t.status == "ENTERED"])
    _write_csv(REPORTS / f"phase41_{name.lower()}_reject.csv", [t.to_dict() for t in cand_a if t.status == "ENTERED"])
    _write_csv(REPORTS / f"phase41_{name.lower()}_inside.csv", [t.to_dict() for t in cand_c if t.status == "ENTERED"])
    _write_csv(REPORTS / f"phase41_{name.lower()}_years.csv", years)
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
        "# Phase 41 — Volume profile / auction market structure",
        "",
        "Research only. `DRY_RUN`. No broker. Nothing frozen.",
        "",
        "**Profile source: `DEGRADED_1M_VOLUME_PROFILE`.** 1m bar volume is spread uniformly across each bar's tick range. This is not a trade-print volume profile. Databento `trades` is ~$0.30 per NQ RTH day; 2020–2026 ES+NQ is not economically feasible here.",
        "",
        "## 1. Verdict",
        "",
        f"- **Overall:** `{payload.get('verdict')}`",
        f"- **ES_VOLUME_PROFILE_STATUS:** `{payload.get('ES_VOLUME_PROFILE_STATUS')}`",
        f"- **NQ_VOLUME_PROFILE_STATUS:** `{payload.get('NQ_VOLUME_PROFILE_STATUS')}`",
        f"- **Recommendation:** `{payload.get('recommendation')}`",
        "",
        "Primary locked before P&L: `VP_OUTSIDE_ACCEPT_POC` (open outside prior 70% value, 1m close inside, next open ±1 tick toward POC, stop = outside excursion extreme ±1 tick, flatten 15:55).",
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
        "- Reused Phase 38 ES/NQ 1m RTH session handling, holidays, flatten 15:55, cost ticks, chronological split, walk-forward years, frozen-hash checks.",
        "- No MBO/MBP/DOM. No ORB/sweep/TSMOM logic.",
        "- Trade-tape reconstruction skipped (cost). Result is labeled degraded.",
        "",
        "## 4. Dataset",
        "",
    ]
    for name in ("ES", "NQ"):
        d = (r.get(name) or {}).get("data") or {}
        lines.append(f"- **{name}:** n_bars={d.get('n_bars')} roll={d.get('roll')} next-days={(r.get(name) or {}).get('n_next_days')} profiles={(r.get(name) or {}).get('n_profile_days')}")
    lines += ["", "Coverage: valid RTH days 2020-01-02 → 2026-08-14. TRAIN ≤ 2024-12-31. HOLDOUT ≥ 2025-01-02.", "", "## 5. POC / VAH / VAL construction", "",
              "- Tick bucket: 0.25.",
              "- Volume: each 1m bar's volume split equally across ticks from low to high.",
              "- POC: max-volume tick; ties → closest to session VWAP of typical price; remaining ties → lower tick.",
              "- Value area: 70%. Single-row expansion from POC; equal adjacent volume expands the lower-price side first.",
              "- Profile for day T uses only the prior completed RTH session.",
              "", "## 6. Open-location frequencies", ""]
    for name in ("ES", "NQ"):
        lines.append(f"- **{name}:** {(r.get(name) or {}).get('open_share')} counts={(r.get(name) or {}).get('open_location')}")
    lines += ["", "## 7. Structural transition probabilities", "",
              "| Instrument | Open class | N | P(enter value) | P(touch POC) | P(traverse) | P(continue away) | P(return VAH) | P(return VAL) |",
              "|---|---|---:|---:|---:|---:|---:|---:|---:|"]
    for name in ("ES", "NQ"):
        for cls in ("OPEN_ABOVE_VAH", "OPEN_INSIDE_VALUE", "OPEN_BELOW_VAL"):
            s = ((r.get(name) or {}).get("structural") or {}).get(cls) or {}
            lines.append(f"| {name} | {cls} | {s.get('n')} | {fmt(s.get('P_enter_value'))} | {fmt(s.get('P_touch_poc'))} | {fmt(s.get('P_traverse_full'))} | {fmt(s.get('P_continue_away'))} | {fmt(s.get('P_return_vah'))} | {fmt(s.get('P_return_val'))} |")
    lines += ["", "## 8. Acceptance", "", "| Instrument | Class | P(1m close inside) | P(two 1m) | P(5m) |", "|---|---|---:|---:|---:|"]
    for name in ("ES", "NQ"):
        for cls in ("OPEN_ABOVE_VAH", "OPEN_BELOW_VAL", "OPEN_INSIDE_VALUE"):
            s = ((r.get(name) or {}).get("structural") or {}).get(cls) or {}
            lines.append(f"| {name} | {cls} | {fmt(s.get('P_accept_1m'))} | {fmt(s.get('P_accept_two_1m'))} | {fmt(s.get('P_accept_5m'))} |")
    lines += ["", "## 9. Rejection", "", "| Instrument | Class | P(1m reject) |", "|---|---|---:|"]
    for name in ("ES", "NQ"):
        for cls in ("OPEN_ABOVE_VAH", "OPEN_BELOW_VAL"):
            s = ((r.get(name) or {}).get("structural") or {}).get(cls) or {}
            lines.append(f"| {name} | {cls} | {fmt(s.get('P_reject_1m'))} |")
    lines += ["", "## 10. Outside-value rejection candidate (`VP_OUTSIDE_REJECT_1R`)", ""]
    for name in ("ES", "NQ"):
        a = (r.get(name) or {}).get("cand_A_reject") or {}
        lines.append(f"- **{name}:** entered={a.get('n_entered')} resolved={a.get('n_resolved')} E[R]={fmt(a.get('expectancy_r'))} E[pts]={fmt(a.get('expectancy_points'))} hit={fmt(a.get('win_rate'))} PF={fmt(a.get('profit_factor'), 2)} train={fmt(((r.get(name) or {}).get('cand_A_train') or {}).get('expectancy_r'))} holdout={fmt(((r.get(name) or {}).get('cand_A_holdout') or {}).get('expectancy_r'))} long={a.get('long')} short={a.get('short')}")
    lines += ["", "## 11. Outside-value acceptance candidate (`VP_OUTSIDE_ACCEPT_POC`) — PRIMARY", "",
              "| Instrument | N | E[R] | E[pts] | Hit | PF | Train E[R] | Holdout E[R] | 1R E[R] | Opposite E[R] | Status |",
              "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|"]
    for name in ("ES", "NQ"):
        p = ((r.get(name) or {}).get("primary") or {})
        f = p.get("full") or {}
        lines.append(f"| {name} | {f.get('n_resolved')} | {fmt(f.get('expectancy_r'))} | {fmt(f.get('expectancy_points'))} | {fmt(f.get('win_rate'))} | {fmt(f.get('profit_factor'), 2)} | {fmt((p.get('train') or {}).get('expectancy_r'))} | {fmt((p.get('holdout') or {}).get('expectancy_r'))} | {fmt((p.get('target_1r') or {}).get('expectancy_r'))} | {fmt((p.get('target_opposite') or {}).get('expectancy_r'))} | `{(r.get(name) or {}).get('status')}` |")
    lines += ["", "## 12. Inside-value rotation candidate (`VP_INSIDE_ROTATE_POC`)", ""]
    for name in ("ES", "NQ"):
        c = (r.get(name) or {}).get("cand_C_inside") or {}
        lines.append(f"- **{name}:** entered={c.get('n_entered')} resolved={c.get('n_resolved')} E[R]={fmt(c.get('expectancy_r'))} hit={fmt(c.get('win_rate'))} PF={fmt(c.get('profit_factor'), 2)} train={fmt(((r.get(name) or {}).get('cand_C_train') or {}).get('expectancy_r'))} holdout={fmt(((r.get(name) or {}).get('cand_C_holdout') or {}).get('expectancy_r'))} long={c.get('long')} short={c.get('short')}")
    lines += ["", "## 13. POC analysis", ""]
    for name in ("ES", "NQ"):
        lines.append(f"- **{name}:** {(r.get(name) or {}).get('poc_magnet')}")
    lines += ["", "## 14. Value-width analysis", "", "Outside-value days, width/ATR quintiles vs P(enter) / P(continue away).", ""]
    for name in ("ES", "NQ"):
        for row in (r.get(name) or {}).get("width_quintiles") or []:
            lines.append(f"- **{name} Q{row.get('bucket')}:** n={row.get('n')} mean_w/ATR={fmt(row.get('mean_width_atr'))} P(enter)={fmt(row.get('P_enter'))} P(away)={fmt(row.get('P_continue_away'))} P(POC)={fmt(row.get('P_touch_poc'))}")
    lines += ["", "## 15. Gap / overnight context", "", "Gap and overnight-inside-value are recorded on `reports/phase41_*_structural.csv`. They are diagnostics, not rules.", "",
              "## 16. Long / short", "", "See candidate sections. Sides are not pooled.", "",
              "## 17. ES / NQ", "", "Not pooled. Statuses in section 1.", "",
              "## 18. Cost stress", ""]
    for name in ("ES", "NQ"):
        p = (r.get(name) or {}).get("primary") or {}
        lines.append(f"- **{name} primary:** ideal E[R]={fmt((p.get('ideal') or {}).get('expectancy_r'))} 1-tick={fmt((p.get('full') or {}).get('expectancy_r'))} 2-tick={fmt((p.get('stress_2tick') or {}).get('expectancy_r'))}")
    lines += ["", "## 19. Train / holdout", ""]
    for name in ("ES", "NQ"):
        p = (r.get(name) or {}).get("primary") or {}
        lines.append(f"- **{name}:** train n={(p.get('train') or {}).get('n_resolved')} E[R]={fmt((p.get('train') or {}).get('expectancy_r'))}; holdout n={(p.get('holdout') or {}).get('n_resolved')} E[R]={fmt((p.get('holdout') or {}).get('expectancy_r'))}")
    lines += ["", "## 20. Walk-forward", "", "| Instrument | Year | N | E[R] | Hit |", "|---|---:|---:|---:|---:|"]
    for name in ("ES", "NQ"):
        for y in ((r.get(name) or {}).get("primary") or {}).get("years") or []:
            lines.append(f"| {name} | {y.get('year')} | {y.get('n_resolved')} | {fmt(y.get('expectancy_r'))} | {fmt(y.get('win_rate'))} |")
    lines += ["", "## 21. Portfolio relationship", ""]
    nq_dvp = ((r.get("NQ") or {}).get("primary") or {}).get("dvp")
    lines.append(f"- NQ vs frozen DVP: {nq_dvp}")
    lines.append("- GC VWAP V2 journal has no comparable historical daily series in this repo.")
    lines += ["", "## 22. Prop geometry", ""]
    for name in ("ES", "NQ"):
        lines.append(f"- **{name}:** {((r.get(name) or {}).get('primary') or {}).get('prop')}")
    lines += ["", "## 23. Recommendation", "", payload.get("recommendation_text") or "", "",
              "Execution remained `DRY_RUN`. `strategy_frozen/` was not written.",
              "" if payload.get("candidate_written") else "No candidate JSON.",
              ""]
    DOCS.write_text("\n".join(lines), encoding="utf-8")


def rec_text(verdict: str) -> str:
    if verdict == "VOLUME_PROFILE_EDGE_FOUND":
        return "One clean next-phase candidate is the locked `VP_OUTSIDE_ACCEPT_POC` on the stronger instrument. Do not freeze. A later phase must rebuild the profile from actual trades before any freeze."
    if verdict == "VOLUME_PROFILE_STRUCTURAL_EFFECT_ONLY":
        return "Prior value location changes next-session transition probabilities, but the simple acceptance/rejection geometries do not monetize it after costs. Do not force a trade. Do not add Phase 42 filter soup."
    if verdict == "VOLUME_PROFILE_EDGE_WEAK":
        return "Mildly positive or unstable after costs. Do not freeze. Do not expand HVN/LVN or value-area percentages."
    if verdict == "DATA_QUALITY_BLOCKED":
        return "1m history missing. Do not substitute CFDs."
    return "No stable value-profile edge. **CLOSE_VOLUME_PROFILE_RESEARCH_BRANCH.** Move Strategy #3 elsewhere. Do not rescue with shapes, nodes, or news filters."


def main() -> dict[str, Any]:
    frozen_before = assert_frozen()
    if not frozen_before["ok"]:
        payload = {"ok": False, "status": "FROZEN_INTEGRITY_FAILED", "frozen_before": frozen_before}
        VALIDATION.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return payload
    spec = json.loads(SPEC_PATH.read_text(encoding="utf-8"))
    results = {}
    summary = []
    for name in ("ES", "NQ"):
        block = research_instrument(name)
        results[name] = block
        p = (block.get("primary") or {}).get("full") or {}
        summary.append({
            "instrument": name,
            "status": block.get("status"),
            "open_share": block.get("open_share"),
            "n": p.get("n_resolved"),
            "e_r": p.get("expectancy_r"),
            "hit": p.get("win_rate"),
            "pf": p.get("profit_factor"),
            "train_e_r": ((block.get("primary") or {}).get("train") or {}).get("expectancy_r"),
            "hold_e_r": ((block.get("primary") or {}).get("holdout") or {}).get("expectancy_r"),
            "reject_e_r": (block.get("cand_A_reject") or {}).get("expectancy_r"),
            "inside_e_r": (block.get("cand_C_inside") or {}).get("expectancy_r"),
        })
    es_s = results["ES"].get("status") or "DATA_QUALITY_BLOCKED"
    nq_s = results["NQ"].get("status") or "DATA_QUALITY_BLOCKED"
    verdict = overall_status(es_s, nq_s)
    rec = "CLOSE_VOLUME_PROFILE_RESEARCH_BRANCH"
    if verdict == "VOLUME_PROFILE_EDGE_FOUND":
        rec = "CONTINUE_VP_TO_TRADE_TAPE_THEN_REFINEMENT"
    elif verdict == "VOLUME_PROFILE_STRUCTURAL_EFFECT_ONLY":
        rec = "DO_NOT_FORCE_TRADE_STRUCTURAL_ONLY"
    elif verdict == "VOLUME_PROFILE_EDGE_WEAK":
        rec = "CLOSE_VOLUME_PROFILE_RESEARCH_BRANCH"
    rtxt = rec_text(verdict)
    frozen_after = assert_frozen()
    _write_csv(REPORTS / "phase41_primary_summary.csv", summary)
    slim = {}
    for k, v in results.items():
        slim[k] = {kk: vv for kk, vv in v.items() if kk not in ("")}
    payload = {
        "ok": frozen_after["ok"],
        "phase": 41,
        "status": "RESEARCH_COMPLETE",
        "execution": "DRY_RUN_NO_BROKER",
        "profile_source": "DEGRADED_1M_VOLUME_PROFILE",
        "verdict": verdict,
        "ES_VOLUME_PROFILE_STATUS": es_s,
        "NQ_VOLUME_PROFILE_STATUS": nq_s,
        "recommendation": rec,
        "recommendation_text": rtxt,
        "branch": rec if rec.startswith("CLOSE") else rec,
        "frozen_before": {**frozen_before, "gc": FROZEN_GC_HASH, "nq": FROZEN_NQ_HASH},
        "frozen_after": {**frozen_after, "gc": FROZEN_GC_HASH, "nq": FROZEN_NQ_HASH},
        "file_sha": {
            "gc": file_sha256(ROOT / "strategy_frozen" / "gc_vwap_v2_phase26.json"),
            "nq": file_sha256(ROOT / "strategy_frozen" / "nq_dvp_phase30.json"),
            "gc_expected": GC_FILE_SHA,
            "nq_expected": NQ_FILE_SHA,
        },
        "spec": spec,
        "results": results,
        "candidate_written": False,
        "candidate_path": None,
    }
    if verdict == "VOLUME_PROFILE_EDGE_FOUND":
        picks = []
        for inst in ("ES", "NQ"):
            if results[inst].get("status") == "VOLUME_PROFILE_EDGE_FOUND":
                e = (((results[inst].get("primary") or {}).get("holdout") or {}).get("expectancy_r") or -9)
                picks.append((inst, e))
        if picks:
            picks.sort(key=lambda x: x[1], reverse=True)
            inst = picks[0][0]
            path = CANDIDATE_DIR / f"phase41_{inst}_VOLUME_PROFILE.json"
            path.write_text(json.dumps({
                "status": "RESEARCH_CANDIDATE",
                "phase": 41,
                "instrument": inst,
                "family": "index_rth_volume_profile_v1",
                "candidate_id": "VP_OUTSIDE_ACCEPT_POC",
                "not_frozen": True,
                "profile_source": "DEGRADED_1M_VOLUME_PROFILE",
                "rules": spec["primary_candidate"],
                "metrics": results[inst].get("primary"),
            }, indent=2, default=str), encoding="utf-8")
            payload["candidate_written"] = True
            payload["candidate_path"] = str(path)
    VALIDATION.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    write_markdown(payload)
    print(json.dumps({"verdict": verdict, "ES": es_s, "NQ": nq_s, "rec": rec, "candidate": payload["candidate_path"]}, indent=2), flush=True)
    return payload


if __name__ == "__main__":
    main()
    sys.exit(0)
