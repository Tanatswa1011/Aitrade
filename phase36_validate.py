"""Phase 36 — shallow PDH/PDL sweep reclaim strategy construction.

DRY_RUN. No broker. No freeze. No DOM.
Primary candidate B (1m close reclaim, 5m expiry, 1.5R, 1-tick fill) was declared
in phase36_spec.json before this validator inspected trade P&L.
"""
from __future__ import annotations

import csv
import hashlib
import json
import math
import statistics
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

from bar_dataset import load_dataset
from nq_microstructure_models import FROZEN_GC_HASH, FROZEN_NQ_HASH, NQ_TICK, SweepEvent
from nq_shallow_sweep_engine import (
    COMMISSION_POINTS,
    FLATTEN_LOCAL,
    MAX_STOP_POINTS,
    POINT_USD,
    SweepTrade,
    simulate,
    simulate_continuation_diagnostic,
)
from phase34_validate import GC_FILE_SHA, GC_FROZEN, NQ_FILE_SHA, NQ_FROZEN, assert_frozen, file_sha256
from phase35_validate import CONTRACT_ROOT, load_contract_bars, news_dates

ROOT = Path(__file__).resolve().parent
NY = ZoneInfo("America/New_York")
REPORTS = ROOT / "reports"
JOURNAL = ROOT / "journal" / "phase36_nq_shallow_sweep"
VALIDATION = ROOT / "phase36_validation.json"
SPEC_PATH = ROOT / "phase36_spec.json"
EVENTS_CSV = REPORTS / "phase35_sweep_events.csv"
CANDIDATE = ROOT / "strategy_candidates" / "phase36_NQ_SHALLOW_SWEEP_RECLAIM.json"

PRIMARY = {
    "id": "B",
    "reclaim_mode": "close_1m",
    "expiry_sec": 300,
    "sl_buffer_ticks": 1,
    "target_r": 1.5,
    "entry_adverse_ticks": 1.0,
}


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


def load_events() -> list[SweepEvent]:
    rows = list(csv.DictReader(EVENTS_CSV.open(encoding="utf-8")))
    out = []
    for r in rows:
        extras = json.loads(r["extras"]) if r.get("extras") else {}
        if r.get("contract"):
            extras["contract"] = r["contract"]
        out.append(
            SweepEvent(
                event_id=r["event_id"],
                trading_date=r["trading_date"],
                side=r["side"],
                level=float(r["level"]),
                sweep_bar_time=int(float(r["sweep_bar_time"])),
                sweep_ts=int(float(r["sweep_ts"])),
                extreme=float(r["extreme"]),
                penetration_points=float(r["penetration_points"]),
                rth_open_ts=int(float(r["rth_open_ts"])),
                seconds_from_rth_open=int(float(r["seconds_from_rth_open"])),
                atr_1m_14=None if r.get("atr_1m_14") in ("", None) else float(r["atr_1m_14"]),
                volume_sweep_bar=None if r.get("volume_sweep_bar") in ("", None) else float(r["volume_sweep_bar"]),
                prior_rth_high=float(r["prior_rth_high"]),
                prior_rth_low=float(r["prior_rth_low"]),
                extras=extras,
            )
        )
    out.sort(key=lambda e: (e.trading_date, e.sweep_bar_time, e.side))
    return out


def first_of_day_ids(events: list[SweepEvent]) -> set[str]:
    best: dict[str, SweepEvent] = {}
    for e in events:
        cur = best.get(e.trading_date)
        if cur is None or (e.sweep_bar_time, e.side) < (cur.sweep_bar_time, cur.side):
            best[e.trading_date] = e
    return {e.event_id for e in best.values()}


def score(trades: list[SweepTrade], *, use_cost: bool = True) -> dict[str, Any]:
    resolved = [t for t in trades if t.outcome in ("TARGET_HIT", "STOP_HIT", "TIME_EXIT")]
    amb = [t for t in trades if t.outcome == "AMBIGUOUS"]
    expired = [t for t in trades if t.status == "EXPIRED"]
    entered = [t for t in trades if t.status == "ENTERED"]
    pts_attr = "points_after_cost" if use_cost else "points"
    r_attr = "r_after_cost" if use_cost else "r_multiple"

    def _val(t, attr):
        v = getattr(t, attr)
        return None if v is None else float(v)

    pts = [p for t in resolved if (p := _val(t, pts_attr)) is not None]
    rs = [p for t in resolved if (p := _val(t, r_attr)) is not None]
    wins = [t for t in resolved if (_val(t, pts_attr) or 0) > 0]
    losses = [t for t in resolved if (_val(t, pts_attr) or 0) <= 0]
    win_pts = [p for t in wins if (p := _val(t, pts_attr)) is not None]
    loss_pts = [abs(p) for t in losses if (p := _val(t, pts_attr)) is not None]
    equity = peak = 0.0
    max_dd = 0.0
    streak = max_streak = 0
    for t in resolved:
        p = _val(t, pts_attr)
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
        p = _val(t, pts_attr)
        if p is None:
            continue
        day_pnl[t.trading_date] += p
    gross_win = sum(win_pts)
    gross_loss = sum(loss_pts)
    longs = [t for t in resolved if t.direction == "LONG"]
    shorts = [t for t in resolved if t.direction == "SHORT"]

    def _side(rows):
        pp = [p for t in rows if (p := _val(t, pts_attr)) is not None]
        if not pp:
            return {"n": 0, "win_rate": None, "expectancy_points": None, "expectancy_r": None}
        w = sum(1 for x in pp if x > 0)
        rr = [p for t in rows if (p := _val(t, r_attr)) is not None]
        return {
            "n": len(rows),
            "win_rate": w / len(rows),
            "expectancy_points": statistics.mean(pp),
            "expectancy_r": None if not rr else statistics.mean(rr),
        }

    risks = [float(t.risk_points) for t in entered if t.risk_points]
    daily_loss = [v for v in day_pnl.values() if v < 0]
    return {
        "n_setups": len(trades),
        "n_expired": len(expired),
        "n_entered": len(entered),
        "n_resolved": len(resolved),
        "n_ambiguous": len(amb),
        "n_target": sum(1 for t in resolved if t.outcome == "TARGET_HIT"),
        "n_stop": sum(1 for t in resolved if t.outcome == "STOP_HIT"),
        "n_time": sum(1 for t in resolved if t.outcome == "TIME_EXIT"),
        "win_rate": None if not resolved else len(wins) / len(resolved),
        "expectancy_points": None if not pts else statistics.mean(pts),
        "expectancy_r": None if not rs else statistics.mean(rs),
        "median_r": None if not rs else statistics.median(rs),
        "profit_factor": None if gross_loss <= 0 else gross_win / gross_loss,
        "max_dd_points": abs(max_dd),
        "max_consec_losses": max_streak,
        "n_days": len(day_pnl),
        "worst_day_points": None if not daily_loss else min(daily_loss),
        "avg_stop_points": None if not risks else statistics.mean(risks),
        "p90_stop_points": None if len(risks) < 5 else sorted(risks)[max(0, int(math.ceil(0.9 * len(risks)) - 1))],
        "p95_stop_points": None if len(risks) < 5 else sorted(risks)[max(0, int(math.ceil(0.95 * len(risks)) - 1))],
        "avg_risk_usd_nq": None if not risks else statistics.mean(risks) * POINT_USD,
        "max_trades_one_day": None if not day_pnl else max(
            sum(1 for t in resolved if t.trading_date == d) for d in day_pnl
        ),
        "long": _side(longs),
        "short": _side(shorts),
        "cost_adjusted": use_cost,
    }


def run_set(
    events: list[SweepEvent],
    bars_by_c: dict[str, list],
    first_ids: set[str],
    *,
    candidate: str,
    reclaim_mode: str,
    expiry_sec: int,
    sl_buffer_ticks: int,
    target_r: float,
    entry_adverse_ticks: float,
    exit_adverse_ticks: float = 0.0,
    first_only: bool = True,
    shallow_max: float = 18.25,
    require_shallow: bool = True,
) -> list[SweepTrade]:
    trades = []
    for e in events:
        fod = e.event_id in first_ids
        if first_only and not fod:
            continue
        if require_shallow and float(e.penetration_points) > shallow_max:
            continue
        contract = str((e.extras or {}).get("contract") or "")
        bars = bars_by_c[contract]
        trades.append(
            simulate(
                e,
                bars,
                candidate=candidate,
                reclaim_mode=reclaim_mode,
                expiry_sec=expiry_sec,
                sl_buffer_ticks=sl_buffer_ticks,
                target_r=target_r,
                entry_adverse_ticks=entry_adverse_ticks,
                exit_adverse_ticks=exit_adverse_ticks,
                first_of_day=fod,
            )
        )
    return trades


def slice_dates(trades: list[SweepTrade], start: str, end: str) -> list[SweepTrade]:
    return [t for t in trades if start <= t.trading_date <= end]


def dvp_compare(entered: list[SweepTrade]) -> dict[str, Any]:
    path = ROOT / "journal" / "phase29_nq_drift_vwap" / "trades.jsonl"
    dvp = []
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                dvp.append(json.loads(line))
    sweep_days = {t.trading_date for t in entered if t.status == "ENTERED"}
    dvp_by_day: dict[str, list] = defaultdict(list)
    for row in dvp:
        dvp_by_day[row["trading_date"]].append(row)
    same_day = sweep_days & set(dvp_by_day)
    same_hour = agree = conflict = 0
    pnl_pairs = []
    loss_days_s = {t.trading_date for t in entered if t.points_after_cost is not None and t.points_after_cost <= 0}
    loss_days_d = set()
    day_s: dict[str, float] = defaultdict(float)
    day_d: dict[str, float] = defaultdict(float)
    for t in entered:
        if t.points_after_cost is None:
            continue
        day_s[t.trading_date] += float(t.points_after_cost)
    for row in dvp:
        if row.get("points") is None:
            continue
        day_d[row["trading_date"]] += float(row["points"])
        if float(row["points"]) <= 0:
            loss_days_d.add(row["trading_date"])
    for t in entered:
        if t.status != "ENTERED" or t.entry_ts is None:
            continue
        day = dvp_by_day.get(t.trading_date) or []
        if any(abs(int(r.get("entry_timestamp") or 0) - int(t.entry_ts)) <= 3600 for r in day):
            same_hour += 1
        dirs = {r.get("direction") for r in day}
        long = t.direction == "LONG"
        if long and "bullish" in dirs:
            agree += 1
        elif (not long) and "bearish" in dirs:
            agree += 1
        elif long and "bearish" in dirs:
            conflict += 1
        elif (not long) and "bullish" in dirs:
            conflict += 1
    common = sorted(set(day_s) & set(day_d))
    corr = None
    if len(common) >= 8:
        xs = [day_s[d] for d in common]
        ys = [day_d[d] for d in common]
        mx, my = statistics.mean(xs), statistics.mean(ys)
        num = sum((a - mx) * (b - my) for a, b in zip(xs, ys))
        den = math.sqrt(sum((a - mx) ** 2 for a in xs) * sum((b - my) ** 2 for b in ys))
        corr = None if den == 0 else num / den
        pnl_pairs = len(common)
    return {
        "n_entered_days": len(sweep_days),
        "same_day_overlap": len(same_day),
        "same_hour_overlap_trades": same_hour,
        "direction_agree": agree,
        "direction_conflict": conflict,
        "daily_pnl_correlation": corr,
        "n_days_for_corr": pnl_pairs,
        "losing_day_overlap": len(loss_days_s & loss_days_d),
        "note": "Read-only vs frozen NQ DVP historical trades. No combination.",
    }


def decide(primary_full: dict, primary_hold: dict, wf_rows: list[dict], n_resolved_full: int, n_hold: int) -> str:
    if n_resolved_full < 40:
        return "INSUFFICIENT_TRADE_SAMPLE"
    e_full = primary_full.get("expectancy_r")
    e_hold = primary_hold.get("expectancy_r")
    pos_blocks = sum(1 for r in wf_rows if (r.get("expectancy_r") or 0) > 0)
    hold_ok = n_hold >= 20 and e_hold is not None and e_hold > 0
    full_ok = e_full is not None and e_full > 0
    if n_resolved_full >= 100 and n_hold >= 30 and full_ok and hold_ok and pos_blocks >= 3:
        return "STRUCTURAL_STRATEGY_EDGE_FOUND"
    if full_ok and (hold_ok or pos_blocks >= 3) and n_resolved_full >= 50:
        return "STRUCTURAL_STRATEGY_PROMISING_NEEDS_MORE_DATA"
    if n_resolved_full >= 50 and not full_ok:
        return "STRUCTURAL_CLASSIFIER_NOT_TRADABLE"
    if e_full is not None and e_full <= 0 and (e_hold is None or e_hold <= 0):
        return "STRUCTURAL_STRATEGY_EDGE_REJECTED"
    return "STRUCTURAL_CLASSIFIER_NOT_TRADABLE"


def main() -> dict[str, Any]:
    frozen_before = assert_frozen()
    spec = json.loads(SPEC_PATH.read_text(encoding="utf-8"))
    events = load_events()
    first_ids = first_of_day_ids(events)
    news = news_dates()
    contracts = sorted({str((e.extras or {}).get("contract")) for e in events})
    print("loading contracts", contracts, flush=True)
    bars_by_c = {c: load_contract_bars(c) for c in contracts}

    thresh = float(spec["shallow"]["threshold_points"])
    shallow = [e for e in events if e.penetration_points <= thresh]
    first_all = [e for e in events if e.event_id in first_ids]
    first_shallow = [e for e in first_all if e.penetration_points <= thresh]
    deep_first = [e for e in first_all if e.penetration_points > thresh]

    funnel = {
        "raw_eligible_phase35": len(events),
        "first_sweep_of_day": len(first_all),
        "shallow_any": len(shallow),
        "first_and_shallow": len(first_shallow),
        "first_and_deep": len(deep_first),
        "news_dates_in_calendar": len(news),
        "n_removed_pm55": 0,
        "shallow_threshold": thresh,
    }
    print("funnel", funnel, flush=True)

    kw = dict(
        events=events,
        bars_by_c=bars_by_c,
        first_ids=first_ids,
        candidate="B",
        reclaim_mode="close_1m",
        expiry_sec=300,
        sl_buffer_ticks=1,
        target_r=1.5,
        entry_adverse_ticks=1.0,
        first_only=True,
        shallow_max=thresh,
    )
    primary = run_set(**kw)
    JOURNAL.mkdir(parents=True, exist_ok=True)
    with (JOURNAL / "trades_primary.jsonl").open("w", encoding="utf-8") as fh:
        for t in primary:
            fh.write(json.dumps(t.to_dict(), default=str) + "\n")
    _write_csv(REPORTS / "phase36_trades_primary.csv", [t.to_dict() for t in primary])

    full_sc = score(primary)
    train = slice_dates(primary, "2025-06-17", "2026-04-09")
    hold = slice_dates(primary, "2026-04-13", "2026-08-14")
    train_sc = score(train)
    hold_sc = score(hold)

    blocks = spec["chrono"]["walkforward_blocks"]
    wf_rows = []
    for a, b in blocks:
        sub = slice_dates(primary, a, b)
        sc = score(sub)
        wf_rows.append({"start": a, "end": b, **sc})
    _write_csv(REPORTS / "phase36_walkforward.csv", wf_rows)

    # Target matrix on candidate B
    matrix = []
    for cand, mode in (("A", "range_1m"), ("B", "close_1m"), ("C", "close_5m")):
        for r in (1.0, 1.5, 2.0, 3.0):
            tr = run_set(
                events, bars_by_c, first_ids,
                candidate=cand, reclaim_mode=mode, expiry_sec=300,
                sl_buffer_ticks=1, target_r=r, entry_adverse_ticks=1.0,
                first_only=True, shallow_max=thresh,
            )
            sc = score(tr)
            hs = score(slice_dates(tr, "2026-04-13", "2026-08-14"))
            matrix.append({
                "candidate": cand, "reclaim_mode": mode, "target_r": r,
                "full": sc, "holdout": hs,
                "n_resolved": sc.get("n_resolved"),
                "wr": sc.get("win_rate"),
                "exp_r": sc.get("expectancy_r"),
                "pf": sc.get("profit_factor"),
                "max_dd": sc.get("max_dd_points"),
                "holdout_n": hs.get("n_resolved"),
                "holdout_exp_r": hs.get("expectancy_r"),
            })
    _write_csv(
        REPORTS / "phase36_target_matrix.csv",
        [{k: v for k, v in row.items() if k not in ("full", "holdout")} | {
            "full_wr": (row["full"] or {}).get("win_rate"),
            "full_exp_pts": (row["full"] or {}).get("expectancy_points"),
        } for row in matrix],
    )

    fills = []
    for ticks in (0.0, 1.0, 2.0):
        tr = run_set(
            events, bars_by_c, first_ids,
            candidate="B", reclaim_mode="close_1m", expiry_sec=300,
            sl_buffer_ticks=1, target_r=1.5, entry_adverse_ticks=ticks,
            first_only=True, shallow_max=thresh,
        )
        sc = score(tr)
        fills.append({"entry_adverse_ticks": ticks, **sc})
    # exit stress 1 tick on primary params
    tr_ex = run_set(
        events, bars_by_c, first_ids,
        candidate="B", reclaim_mode="close_1m", expiry_sec=300,
        sl_buffer_ticks=1, target_r=1.5, entry_adverse_ticks=1.0,
        exit_adverse_ticks=1.0, first_only=True, shallow_max=thresh,
    )
    fills.append({"entry_adverse_ticks": 1.0, "exit_adverse_ticks": 1.0, **score(tr_ex)})
    _write_csv(REPORTS / "phase36_fill_stress.csv", fills)

    neigh = []
    for cap in spec["shallow"]["neighborhood_for_robustness_only"]:
        tr = run_set(
            events, bars_by_c, first_ids,
            candidate="B", reclaim_mode="close_1m", expiry_sec=300,
            sl_buffer_ticks=1, target_r=1.5, entry_adverse_ticks=1.0,
            first_only=True, shallow_max=float(cap),
        )
        neigh.append({
            "shallow_max": cap,
            "full": score(tr),
            "holdout": score(slice_dates(tr, "2026-04-13", "2026-08-14")),
        })
    _write_csv(
        REPORTS / "phase36_threshold_robustness.csv",
        [{"shallow_max": n["shallow_max"], **{f"full_{k}": v for k, v in n["full"].items() if not isinstance(v, dict)}} for n in neigh],
    )

    expiry_rows = []
    for sec in spec["expiry"]["family_sec"]:
        tr = run_set(
            events, bars_by_c, first_ids,
            candidate="B", reclaim_mode="close_1m", expiry_sec=int(sec),
            sl_buffer_ticks=1, target_r=1.5, entry_adverse_ticks=1.0,
            first_only=True, shallow_max=thresh,
        )
        expiry_rows.append({"expiry_sec": sec, **score(tr)})
    _write_csv(REPORTS / "phase36_expiry.csv", expiry_rows)

    buf_rows = []
    for buf in (0, 1, 2):
        tr = run_set(
            events, bars_by_c, first_ids,
            candidate="B", reclaim_mode="close_1m", expiry_sec=300,
            sl_buffer_ticks=buf, target_r=1.5, entry_adverse_ticks=1.0,
            first_only=True, shallow_max=thresh,
        )
        buf_rows.append({"sl_buffer_ticks": buf, **score(tr)})
    _write_csv(REPORTS / "phase36_stop_buffer.csv", buf_rows)

    # reclaim speed on entered primary
    speed_rows = []
    for label, lo, hi in (("le_60s", 0, 60), ("61_180s", 61, 180), ("181_300s", 181, 300)):
        sub = [t for t in primary if t.status == "ENTERED" and t.reclaim_lag_sec is not None and lo <= t.reclaim_lag_sec <= hi]
        speed_rows.append({"bucket": label, **score(sub)})
    _write_csv(REPORTS / "phase36_reclaim_speed.csv", speed_rows)

    session_rows = []
    for bucket in ("0930_1000", "1000_1130", "1130_1330", "1330_1530"):
        sub = [t for t in primary if t.session_bucket == bucket]
        session_rows.append({"session": bucket, **score(sub)})
    open_bar = [t for t in primary if t.opening_bar]
    later = [t for t in primary if not t.opening_bar]
    session_rows.append({"session": "first_rth_bar", **score(open_bar)})
    session_rows.append({"session": "later_session", **score(later)})
    _write_csv(REPORTS / "phase36_session.csv", session_rows)

    # all shallow diagnostic (not first-only)
    all_sh = run_set(
        events, bars_by_c, first_ids,
        candidate="B_all_shallow", reclaim_mode="close_1m", expiry_sec=300,
        sl_buffer_ticks=1, target_r=1.5, entry_adverse_ticks=1.0,
        first_only=False, shallow_max=thresh,
    )
    all_sh_sc = score(all_sh)

    # deep continuation diagnostic — one frozen config, no grid
    deep_tr = []
    for e in deep_first:
        t = simulate_continuation_diagnostic(
            e,
            bars_by_c[str((e.extras or {}).get("contract"))],
        )
        t.extras["note"] = "diagnostic_only_same_reclaim_engine_not_a_candidate"
        deep_tr.append(t)
    deep_sc = score(deep_tr)
    _write_csv(REPORTS / "phase36_deep_continuation_diagnostic.csv", [t.to_dict() for t in deep_tr])

    entered = [t for t in primary if t.status == "ENTERED"]
    dvp = dvp_compare(entered)
    gc_paper = ROOT / "journal" / "phase26_gc_vwap_v2_paper" / "paper_trades.jsonl"
    gc = {
        "gc_paper_empty": gc_paper.exists() and gc_paper.stat().st_size == 0,
        "note": "GC is a different market. Phase 36 has NQ entries; GC paper journal is empty so loss-day overlap is undefined.",
    }

    funnel["n_reclaimed_primary"] = sum(1 for t in primary if t.reclaim_ts is not None)
    funnel["n_entered_primary"] = full_sc["n_entered"]
    funnel["n_resolved_primary"] = full_sc["n_resolved"]
    funnel["n_ambiguous_primary"] = full_sc["n_ambiguous"]
    funnel["n_expired_primary"] = full_sc["n_expired"]

    verdict = decide(full_sc, hold_sc, wf_rows, full_sc["n_resolved"], hold_sc["n_resolved"])
    frozen_after = assert_frozen()

    # Best reported candidate for a future freeze-validation phase: predeclared primary if it is not rejected;
    # otherwise still name B so we do not shop the matrix.
    rec = {
        "candidate_id": "B",
        "name": "SHALLOW_FIRST_SWEEP + 1M_CLOSE_RECLAIM",
        "params": PRIMARY,
        "status": "RESEARCH_CANDIDATE" if verdict in (
            "STRUCTURAL_STRATEGY_EDGE_FOUND",
            "STRUCTURAL_STRATEGY_PROMISING_NEEDS_MORE_DATA",
        ) else "RESEARCH_ONLY_NOT_PROMOTED",
        "note": "Predeclared primary. Not selected by scanning the target matrix.",
    }

    payload = {
        "ok": frozen_before["ok"] and frozen_after["ok"],
        "phase": 36,
        "status": "RESEARCH_COMPLETE",
        "verdict": verdict,
        "execution": "DRY_RUN_NO_BROKER",
        "frozen_before": frozen_before,
        "frozen_after": frozen_after,
        "spec": spec,
        "funnel": funnel,
        "primary": {
            "rules": PRIMARY,
            "full": full_sc,
            "train": train_sc,
            "holdout": hold_sc,
        },
        "walkforward": wf_rows,
        "target_matrix": [{k: v for k, v in m.items() if k not in ("full", "holdout")} for m in matrix],
        "fill_stress": fills,
        "threshold_neighborhood": [
            {"shallow_max": n["shallow_max"], "full_exp_r": n["full"].get("expectancy_r"), "holdout_exp_r": n["holdout"].get("expectancy_r"), "n": n["full"].get("n_resolved")}
            for n in neigh
        ],
        "expiry": expiry_rows,
        "reclaim_speed": speed_rows,
        "session": [{k: v for k, v in r.items() if not isinstance(v, dict) or k == "session"} for r in session_rows],
        "all_shallow_diagnostic": all_sh_sc,
        "deep_continuation_diagnostic": deep_sc,
        "dvp": dvp,
        "gc": gc,
        "recommendation": rec,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    VALIDATION.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    if rec["status"] == "RESEARCH_CANDIDATE":
        CANDIDATE.write_text(
            json.dumps(
                {
                    "phase": "phase36",
                    "strategy_family": "nq_shallow_pdh_pdl_sweep_reclaim_v1",
                    "strategy_version": "v1.phase36",
                    "status": "RESEARCH_CANDIDATE",
                    "verdict": verdict,
                    "rules": PRIMARY,
                    "shallow_threshold_points": thresh,
                    "n_resolved": full_sc.get("n_resolved"),
                    "holdout_expectancy_r": hold_sc.get("expectancy_r"),
                    "note": "RESEARCH CANDIDATE. Not frozen. DRY_RUN. No broker execution.",
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
                "funnel": out.get("funnel"),
                "full": {k: (out.get("primary") or {}).get("full", {}).get(k) for k in (
                    "n_resolved", "win_rate", "expectancy_r", "expectancy_points", "profit_factor", "max_dd_points", "n_ambiguous"
                )},
                "holdout": {k: (out.get("primary") or {}).get("holdout", {}).get(k) for k in (
                    "n_resolved", "win_rate", "expectancy_r", "profit_factor"
                )},
                "frozen": (out.get("frozen_after") or {}).get("ok"),
            },
            indent=2,
            default=str,
        )
    )
