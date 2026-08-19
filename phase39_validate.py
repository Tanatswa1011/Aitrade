"""Phase 39 — OR breakout retest continuation research.

DRY_RUN. No broker. No freeze. Primary OR15_1M_BREAK_1M_RETEST_HOLD was
declared in phase39_spec.json before this validator inspected P&L.
"""
from __future__ import annotations

import json
import math
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Optional

from nq_microstructure_models import FROZEN_GC_HASH, FROZEN_NQ_HASH
from nq_pdh_pdl import local_ts
from orb_index_engine import build_opening_range, simulate as simulate_p38
from orb_retest_engine import RetestTrade, simulate_retest
from phase34_validate import GC_FILE_SHA, NQ_FILE_SHA, assert_frozen, file_sha256
from phase38_validate import (
    HOLDOUT_START,
    REPORTS,
    TRAIN_END,
    _write_csv,
    daily_context,
    dvp_compare,
    index_days,
    load_instrument,
    long_short_rows,
    monte_carlo,
    quintile_feature,
    score,
    slice_dates,
    valid_dates,
    year_rows,
)

ROOT = Path(__file__).resolve().parent
VALIDATION = ROOT / "phase39_validation.json"
SPEC_PATH = ROOT / "phase39_spec.json"
CANDIDATE_DIR = ROOT / "strategy_candidates"

PRIMARY = {
    "or_minutes": 15,
    "trigger": "T0_exact",
    "fail_frac": 0.10,
    "confirm": "B_close_1m",
    "stop": "A_retest_extreme",
    "target_r": 1.0,
    "adverse": 1.0,
}


def run_config(instrument: str, ctx: dict, dates: list[str], **kw) -> list[RetestTrade]:
    trades = []
    for td in dates:
        c = ctx[td]
        orng = build_opening_range(c["rth"], td, kw.get("or_minutes", 15))
        if not orng or not orng.complete:
            continue
        trades.append(
            simulate_retest(
                instrument=instrument,
                rth=c["rth"],
                orng=orng,
                trigger=kw["trigger"],
                fail_frac=kw["fail_frac"],
                confirm=kw["confirm"],
                stop_mode=kw["stop"],
                target_r=kw["target_r"],
                adverse_ticks=kw["adverse"],
                atr_daily=c.get("atr"),
                gap_points=c.get("gap"),
                prior_day_return_pts=c.get("prior_ret"),
            )
        )
    return trades


def funnel(trades: list[RetestTrade]) -> dict[str, int]:
    c = Counter(t.status for t in trades)
    entered = [t for t in trades if t.status == "ENTERED"]
    return {
        "n_days_with_or": len(trades),
        "no_break": c.get("NO_BREAK", 0),
        "expired": c.get("EXPIRED", 0),
        "retest_failed": c.get("RETEST_FAILED", 0),
        "invalidated": c.get("BREAKOUT_INVALIDATED", 0),
        "confirmed": c.get("RETEST_CONFIRMED", 0) + len(entered) + c.get("NO_ENTRY_BAR", 0) + c.get("REJECT_TIGHT_STOP", 0) + c.get("REJECT_WIDE_STOP", 0) + c.get("NO_PATH", 0),
        "reject_tight": c.get("REJECT_TIGHT_STOP", 0),
        "reject_wide": c.get("REJECT_WIDE_STOP", 0),
        "no_entry_bar": c.get("NO_ENTRY_BAR", 0),
        "entered": len(entered),
        "resolved": sum(1 for t in entered if t.outcome in ("TARGET_HIT", "STOP_HIT", "TIME_EXIT")),
        "ambiguous": sum(1 for t in entered if t.outcome == "AMBIGUOUS"),
        "status_counts": dict(c),
    }


def depth_buckets(trades: list[RetestTrade]) -> list[dict[str, Any]]:
    resolved = [t for t in trades if t.outcome in ("TARGET_HIT", "STOP_HIT", "TIME_EXIT") and t.penetration_frac is not None]
    bins = [(0, 0.05, "0_5pct"), (0.05, 0.10, "5_10pct"), (0.10, 0.25, "10_25pct"), (0.25, 0.50, "25_50pct"), (0.50, 9, "gt_50pct")]
    rows = []
    for lo, hi, name in bins:
        chunk = [t for t in resolved if lo <= float(t.penetration_frac) < hi]
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


def retest_timing(trades: list[RetestTrade]) -> list[dict[str, Any]]:
    resolved = [t for t in trades if t.outcome in ("TARGET_HIT", "STOP_HIT", "TIME_EXIT") and t.retest_lag_sec is not None]
    bins = [(0, 300, "le_5m"), (300, 900, "5_15m"), (900, 1800, "15_30m"), (1800, 10**9, "gt_30m")]
    rows = []
    for lo, hi, name in bins:
        chunk = [t for t in resolved if lo <= int(t.retest_lag_sec) < hi]
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


def stop_stats(trades: list[RetestTrade], point_usd: float) -> dict[str, Any]:
    xs = [float(t.risk_points) for t in trades if t.risk_points]
    if not xs:
        return {"n": 0}
    s = sorted(xs)
    def pct(p):
        return s[max(0, min(len(s) - 1, int(math.ceil(p * len(s)) - 1)))]
    return {
        "n": len(xs),
        "mean": statistics.mean(xs),
        "median": statistics.median(xs),
        "p90": pct(0.90),
        "p95": pct(0.95),
        "mean_usd": statistics.mean(xs) * point_usd,
        "p95_usd": pct(0.95) * point_usd,
        "n_gt_30pts": sum(1 for x in xs if x > 30),
    }


def slot_1000(trades: list[RetestTrade]) -> int:
    n = 0
    for t in trades:
        if t.entry_ts is None:
            continue
        hhmm = local_ts(t.trading_date, "09:55")
        hhmm2 = local_ts(t.trading_date, "10:05")
        if hhmm <= int(t.entry_ts) < hhmm2:
            n += 1
    return n


def reproduce_p38(instrument: str, ctx: dict, dates: list[str]) -> dict[str, Any]:
    trades = []
    for td in dates:
        c = ctx[td]
        orng = build_opening_range(c["rth"], td, 15)
        if not orng or not orng.complete:
            continue
        trades.append(simulate_p38(
            instrument=instrument, rth=c["rth"], orng=orng, family="close_1m",
            stop_mode="A_opposite", target_r=1.0, adverse_ticks=1.0, atr_daily=c.get("atr"),
        ))
    sc = score(trades)
    return {"n_entered": sc["n_entered"], "n_resolved": sc["n_resolved"], "expectancy_r": sc["expectancy_r"], "win_rate": sc["win_rate"]}


def matched_p38(instrument: str, ctx: dict, p39: list[RetestTrade]) -> dict[str, Any]:
    days = [t for t in p39 if t.status == "ENTERED" and t.outcome in ("TARGET_HIT", "STOP_HIT", "TIME_EXIT")]
    both = []
    for t in days:
        c = ctx[t.trading_date]
        orng = build_opening_range(c["rth"], t.trading_date, 15)
        p = simulate_p38(instrument=instrument, rth=c["rth"], orng=orng, family="close_1m", stop_mode="A_opposite", target_r=1.0, adverse_ticks=1.0)
        if p.status == "ENTERED" and p.outcome in ("TARGET_HIT", "STOP_HIT", "TIME_EXIT") and p.r_after_cost is not None and t.r_after_cost is not None:
            both.append((t, p))
    if not both:
        return {"n": 0}
    r39 = [float(a.r_after_cost) for a, _ in both]
    r38 = [float(b.r_after_cost) for _, b in both]
    s39 = [float(a.risk_points) for a, _ in both if a.risk_points]
    s38 = [float(b.risk_points) for _, b in both if b.risk_points]
    return {
        "n": len(both),
        "p39_e_r": statistics.mean(r39),
        "p38_e_r": statistics.mean(r38),
        "p39_wr": sum(1 for x in r39 if x > 0) / len(r39),
        "p38_wr": sum(1 for x in r38 if x > 0) / len(r38),
        "p39_avg_stop": None if not s39 else statistics.mean(s39),
        "p38_avg_stop": None if not s38 else statistics.mean(s38),
        "p39_avg_hold_sec": statistics.mean([float(a.hold_sec) for a, _ in both if a.hold_sec]),
        "p38_avg_hold_sec": statistics.mean([float(b.hold_sec) for _, b in both if b.hold_sec]),
        "delta_e_r": statistics.mean(r39) - statistics.mean(r38),
    }


def decide_one(full: dict, train: dict, hold: dict, years: list[dict], n_full: int, n_hold: int) -> str:
    e = full.get("expectancy_r")
    et = train.get("expectancy_r")
    eh = hold.get("expectancy_r")
    pos_y = sum(1 for r in years if (r.get("expectancy_r") or 0) > 0)
    y2020 = next((r for r in years if r.get("year") == 2020), None)
    if n_full < 80:
        return "ORB_RETEST_PROMISING_NEEDS_MORE_DATA" if e is not None and e > 0 and (eh or 0) > 0 else "ORB_RETEST_EDGE_REJECTED"
    if n_full < 200 or n_hold < 50:
        if e is not None and e > 0 and (eh or 0) > 0 and (et or 0) > 0:
            return "ORB_RETEST_PROMISING_NEEDS_MORE_DATA"
        return "ORB_RETEST_EDGE_REJECTED" if (e or 0) <= 0 else "ORB_RETEST_EDGE_WEAK"
    if e is not None and e > 0 and (et or 0) > 0 and (eh or 0) > 0 and pos_y >= 4 and (not y2020 or (y2020.get("expectancy_r") or 0) >= 0):
        return "ORB_RETEST_EDGE_FOUND"
    if e is not None and e > 0 and (eh or 0) > 0:
        return "ORB_RETEST_EDGE_WEAK"
    return "ORB_RETEST_EDGE_REJECTED"


def overall_status(es_s: str, nq_s: str) -> str:
    rank = {
        "ORB_RETEST_EDGE_FOUND": 5,
        "ORB_RETEST_PROMISING_NEEDS_MORE_DATA": 4,
        "ORB_RETEST_STRUCTURAL_EFFECT_ONLY": 3,
        "ORB_RETEST_EDGE_WEAK": 2,
        "ORB_RETEST_EDGE_REJECTED": 1,
    }
    return es_s if rank.get(es_s, 0) >= rank.get(nq_s, 0) else nq_s


def research_instrument(name: str, ctx: dict, dates: list[str]) -> dict[str, Any]:
    print(f"=== {name} Phase 39 ===", flush=True)
    p38 = reproduce_p38(name, ctx, dates)
    print(f"  P38 reproduce n={p38['n_resolved']} eR={p38['expectancy_r']}", flush=True)
    matrix = []
    for tgt in (0.5, 1.0, 1.5, 2.0, 3.0):
        print(f"  primary target {tgt}R", flush=True)
        tr = run_config(name, ctx, dates, or_minutes=15, trigger="T0_exact", fail_frac=0.10, confirm="B_close_1m", stop="A_retest_extreme", target_r=tgt, adverse=1.0)
        matrix.append({
            "target_r": tgt,
            "full": score(tr),
            "train": score(slice_dates(tr, "2020-01-02", TRAIN_END)),
            "holdout": score(slice_dates(tr, HOLDOUT_START, "2026-08-14")),
        })
    prim = run_config(name, ctx, dates, **{**PRIMARY, "or_minutes": 15, "target_r": 1.0})
    fills = {}
    for adv in (0.0, 1.0, 2.0):
        tr = run_config(name, ctx, dates, or_minutes=15, trigger="T0_exact", fail_frac=0.10, confirm="B_close_1m", stop="A_retest_extreme", target_r=1.0, adverse=adv)
        fills[f"{int(adv)}_tick"] = {"full": score(tr), "holdout": score(slice_dates(tr, HOLDOUT_START, "2026-08-14"))}
    stops = {}
    for sm in ("A_retest_extreme", "B_boundary_2ticks", "C_mid", "P38_opposite"):
        print(f"  stop {sm}", flush=True)
        tr = run_config(name, ctx, dates, or_minutes=15, trigger="T0_exact", fail_frac=0.10, confirm="B_close_1m", stop=sm, target_r=1.0, adverse=1.0)
        stops[sm] = {"full": score(tr), "holdout": score(slice_dates(tr, HOLDOUT_START, "2026-08-14")), "stop_geometry": stop_stats(tr, 50.0 if name == "ES" else 20.0)}
    confs = {}
    for cf in ("A_range", "B_close_1m", "C_close_5m"):
        print(f"  confirm {cf}", flush=True)
        tr = run_config(name, ctx, dates, or_minutes=15, trigger="T0_exact", fail_frac=0.10, confirm=cf, stop="A_retest_extreme", target_r=1.0, adverse=1.0)
        confs[cf] = {"full": score(tr), "holdout": score(slice_dates(tr, HOLDOUT_START, "2026-08-14")), "funnel": funnel(tr)}
    trigs = {}
    for tg in ("T0_exact", "T1_two_ticks", "T2_five_pct_width"):
        tr = run_config(name, ctx, dates, or_minutes=15, trigger=tg, fail_frac=0.10, confirm="B_close_1m", stop="A_retest_extreme", target_r=1.0, adverse=1.0)
        trigs[tg] = {"full": score(tr), "holdout": score(slice_dates(tr, HOLDOUT_START, "2026-08-14")), "n_entered": score(tr)["n_entered"]}
    fails = {}
    for ff in (0.0, 0.10, 0.25):
        tr = run_config(name, ctx, dates, or_minutes=15, trigger="T0_exact", fail_frac=ff, confirm="B_close_1m", stop="A_retest_extreme", target_r=1.0, adverse=1.0)
        fails[str(ff)] = {"full": score(tr), "holdout": score(slice_dates(tr, HOLDOUT_START, "2026-08-14")), "n_entered": score(tr)["n_entered"]}
    or_diag = {}
    for om in (5, 30):
        tr = run_config(name, ctx, dates, or_minutes=om, trigger="T0_exact", fail_frac=0.10, confirm="B_close_1m", stop="A_retest_extreme", target_r=1.0, adverse=1.0)
        or_diag[f"OR{om}"] = {"full": score(tr), "holdout": score(slice_dates(tr, HOLDOUT_START, "2026-08-14"))}
    years = year_rows(prim)
    sc_full = score(prim)
    sc_tr = score(slice_dates(prim, "2020-01-02", TRAIN_END))
    sc_ho = score(slice_dates(prim, HOLDOUT_START, "2026-08-14"))
    status = decide_one(sc_full, sc_tr, sc_ho, years, sc_full.get("n_resolved") or 0, sc_ho.get("n_resolved") or 0)
    tick1 = (fills.get("1_tick") or {}).get("full") or {}
    if status == "ORB_RETEST_EDGE_FOUND" and (tick1.get("expectancy_r") or 0) <= 0:
        status = "ORB_RETEST_EDGE_WEAK"
    depths = depth_buckets(prim)
    if status in ("ORB_RETEST_EDGE_REJECTED", "ORB_RETEST_EDGE_WEAK"):
        ers = [r.get("expectancy_r") for r in depths if r.get("expectancy_r") is not None]
        if len(ers) >= 3 and max(ers) > 0.08 and (max(ers) - min(ers)) > 0.20:
            status = "ORB_RETEST_STRUCTURAL_EFFECT_ONLY"
    usd = 50.0 if name == "ES" else 20.0
    out = {
        "instrument": name,
        "status": status,
        "phase38_reproduce": p38,
        "n_valid_rth_days": len(dates),
        "date_start": dates[0],
        "date_end": dates[-1],
        "funnel": funnel(prim),
        "primary": {"config": PRIMARY, "full": sc_full, "train": sc_tr, "holdout": sc_ho, "years": years},
        "fill_stress": fills,
        "stops": stops,
        "confirms": confs,
        "triggers": trigs,
        "fail_fracs": fails,
        "or_diagnostics": or_diag,
        "depth_buckets": depths,
        "timing_buckets": retest_timing(prim),
        "width_buckets": quintile_feature(prim, "or_width", "or_width"),
        "extension_buckets": quintile_feature(prim, "extension_over_width", "extension_over_width"),
        "long_short": long_short_rows(prim),
        "stop_geometry": stop_stats(prim, usd),
        "matched_phase38": matched_p38(name, ctx, prim),
        "slot_1000_entries": slot_1000(prim),
        "monte_carlo": monte_carlo(prim),
        "target_matrix": matrix,
        "portfolio_dvp": dvp_compare([t for t in prim if t.status == "ENTERED"]) if name == "NQ" else None,
    }
    tag = name.lower()
    _write_csv(REPORTS / f"phase39_{tag}_target_matrix.csv", [
        {"target_r": r["target_r"], "n": r["full"].get("n_resolved"), "wr": r["full"].get("win_rate"),
         "e_r": r["full"].get("expectancy_r"), "train_e_r": r["train"].get("expectancy_r"),
         "hold_n": r["holdout"].get("n_resolved"), "hold_e_r": r["holdout"].get("expectancy_r"),
         "pf": r["full"].get("profit_factor")}
        for r in matrix
    ])
    _write_csv(REPORTS / f"phase39_{tag}_years.csv", years)
    _write_csv(REPORTS / f"phase39_{tag}_depth.csv", depths)
    _write_csv(REPORTS / f"phase39_{tag}_timing.csv", retest_timing(prim))
    _write_csv(REPORTS / f"phase39_{tag}_fills.csv", [
        {"adverse_ticks": k, "n": v["full"].get("n_resolved"), "e_r": v["full"].get("expectancy_r"),
         "hold_e_r": v["holdout"].get("expectancy_r")}
        for k, v in fills.items()
    ])
    _write_csv(REPORTS / f"phase39_{tag}_stops.csv", [
        {"stop": k, "n": v["full"].get("n_resolved"), "e_r": v["full"].get("expectancy_r"),
         "hold_e_r": v["holdout"].get("expectancy_r"), "avg_stop": (v.get("stop_geometry") or {}).get("mean")}
        for k, v in stops.items()
    ])
    _write_csv(REPORTS / f"phase39_{tag}_long_short.csv", [
        {"side": r["side"], "n": r.get("n_resolved"), "wr": r.get("win_rate"), "e_r": r.get("expectancy_r")}
        for r in out["long_short"]
    ])
    _write_csv(REPORTS / f"phase39_{tag}_threshold.csv", [
        *[{"family": "trigger", "value": k, "n": v["n_entered"], "e_r": v["full"].get("expectancy_r"), "hold_e_r": v["holdout"].get("expectancy_r")} for k, v in trigs.items()],
        *[{"family": "fail_frac", "value": k, "n": v["n_entered"], "e_r": v["full"].get("expectancy_r"), "hold_e_r": v["holdout"].get("expectancy_r")} for k, v in fails.items()],
    ])
    return out


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
        bars, meta = load_instrument(name)
        if bars is None:
            results[name] = {"instrument": name, "status": "DATA_QUALITY_BLOCKED", "data": meta}
            continue
        print(f"loaded {name} {len(bars)}", flush=True)
        by_date = index_days(bars)
        dates = [d for d in valid_dates(by_date) if "2020-01-02" <= d <= "2026-08-14"]
        ctx = daily_context(by_date, dates)
        block = research_instrument(name, ctx, dates)
        block["data"] = meta
        results[name] = block
        prim = block["primary"]
        summary.append({
            "instrument": name,
            "status": block["status"],
            "p38_e_r": (block.get("phase38_reproduce") or {}).get("expectancy_r"),
            "full_n": prim["full"].get("n_resolved"),
            "full_e_r": prim["full"].get("expectancy_r"),
            "train_e_r": prim["train"].get("expectancy_r"),
            "hold_n": prim["holdout"].get("n_resolved"),
            "hold_e_r": prim["holdout"].get("expectancy_r"),
            "entered": block["funnel"].get("entered"),
            "expired": block["funnel"].get("expired"),
            "failed": block["funnel"].get("retest_failed"),
            "avg_stop": (block.get("stop_geometry") or {}).get("mean"),
        })
    es_s = results["ES"].get("status") or "ORB_RETEST_EDGE_REJECTED"
    nq_s = results["NQ"].get("status") or "ORB_RETEST_EDGE_REJECTED"
    verdict = overall_status(es_s, nq_s)
    rec = "CONTINUE_ORB_TO_FREEZE_VALIDATION" if verdict == "ORB_RETEST_EDGE_FOUND" else "CLOSE_ORB_RESEARCH_BRANCH"
    frozen_after = assert_frozen()
    _write_csv(REPORTS / "phase39_primary_summary.csv", summary)
    payload = {
        "ok": frozen_after["ok"],
        "phase": 39,
        "status": "RESEARCH_COMPLETE",
        "execution": "DRY_RUN_NO_BROKER",
        "verdict": verdict,
        "ES_RETEST_STATUS": es_s,
        "NQ_RETEST_STATUS": nq_s,
        "recommendation": rec,
        "branch": "CLOSE_ORB_RESEARCH_BRANCH" if rec == "CLOSE_ORB_RESEARCH_BRANCH" else "OPEN",
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
    if verdict == "ORB_RETEST_EDGE_FOUND":
        picks = []
        for inst in ("ES", "NQ"):
            if results[inst].get("status") == "ORB_RETEST_EDGE_FOUND":
                picks.append((inst, ((results[inst].get("primary") or {}).get("holdout") or {}).get("expectancy_r") or -9))
        if picks:
            picks.sort(key=lambda x: x[1], reverse=True)
            inst = picks[0][0]
            path = CANDIDATE_DIR / f"phase39_{inst}_ORB_RETEST.json"
            path.write_text(json.dumps({
                "status": "RESEARCH_CANDIDATE",
                "phase": 39,
                "instrument": inst,
                "family": "index_rth_orb_retest_v1",
                "candidate_id": "OR15_1M_BREAK_1M_RETEST_HOLD",
                "not_frozen": True,
                "rules": spec["primary_candidate"],
                "metrics": results[inst].get("primary"),
            }, indent=2, default=str), encoding="utf-8")
            payload["candidate_written"] = True
            payload["candidate_path"] = str(path)
    VALIDATION.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    print(json.dumps({"verdict": verdict, "ES": es_s, "NQ": nq_s, "rec": rec, "candidate": payload["candidate_path"]}, indent=2), flush=True)
    return payload


if __name__ == "__main__":
    main()
    sys.exit(0)
