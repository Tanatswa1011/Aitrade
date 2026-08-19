"""NQ DVP + ES DVP family overlap / concentration monitor. Does not merge books."""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Optional

from es_dvp_paper import load_paper_trades as load_es
from gc_vwap_paper import load_paper_trades as load_gc
from nq_dvp_paper import load_paper_trades as load_nq

INSUFFICIENT = "INSUFFICIENT_FORWARD_SAMPLE"


def _resolved(rows: list[dict[str, Any]], *, date_key: str, pts_keys: tuple[str, ...]) -> list[dict[str, Any]]:
    out = []
    for r in rows:
        outcome = str(r.get("outcome") or r.get("exit_reason") or r.get("state") or "")
        if outcome in ("TARGET_HIT", "STOP_HIT", "TIME_EXIT", "FORCE_CLOSE", "TARGET", "STOP"):
            pts = None
            for k in pts_keys:
                if r.get(k) is not None:
                    pts = float(r[k])
                    break
            d = r.get(date_key) or r.get("trading_date") or r.get("session_date")
            if d:
                out.append({**r, "_date": str(d), "_pts": pts, "_dir": str(r.get("direction") or "").upper()})
    return out


def _is_short(direction: str) -> bool:
    return direction.lower() in ("bearish", "short")


def _r_of(row: dict[str, Any], default_stop: float) -> Optional[float]:
    if row.get("r_result") is not None:
        return float(row["r_result"])
    if row.get("r_multiple") is not None:
        return float(row["r_multiple"])
    pts = row.get("_pts")
    if pts is None:
        return None
    return float(pts) / float(default_stop)


def _entry_ts(row: dict[str, Any]) -> Optional[int]:
    for k in ("entry_timestamp", "entry_ts"):
        if row.get(k) is not None:
            return int(row[k])
    return None


def _exit_ts(row: dict[str, Any]) -> Optional[int]:
    for k in ("exit_timestamp", "exit_ts"):
        if row.get(k) is not None:
            return int(row[k])
    return None


def _overlap_interval(a: dict[str, Any], b: dict[str, Any]) -> bool:
    ae, ax = _entry_ts(a), _exit_ts(a)
    be, bx = _entry_ts(b), _exit_ts(b)
    if None in (ae, ax, be, bx):
        return False
    return ae < bx and be < ax


def _corr(xs: list[float], ys: list[float]) -> Optional[float]:
    if len(xs) < 10:
        return None
    n = len(xs)
    mx = sum(xs) / n
    my = sum(ys) / n
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    dx = sum((x - mx) ** 2 for x in xs) ** 0.5
    dy = sum((y - my) ** 2 for y in ys) ** 0.5
    if dx == 0 or dy == 0:
        return None
    return num / (dx * dy)


def family_monitor() -> dict[str, Any]:
    nq = _resolved(load_nq(), date_key="trading_date", pts_keys=("net_points", "gross_points"))
    es = _resolved(load_es(), date_key="session_date", pts_keys=("net_pnl_points", "raw_pnl_points"))
    nq_n = len(nq)
    es_n = len(es)
    if nq_n == 0 and es_n == 0:
        return {
            "status": INSUFFICIENT,
            "nq_forward_trades": 0,
            "es_forward_trades": 0,
            "same_day_overlap": INSUFFICIENT,
            "same_direction_overlap": INSUFFICIENT,
            "simultaneous_position_overlap": INSUFFICIENT,
            "p_es_active_given_nq": INSUFFICIENT,
            "p_nq_active_given_es": INSUFFICIENT,
            "forward_pnl_correlation": INSUFFICIENT,
            "combined_family_dd_r": INSUFFICIENT,
            "worst_same_day_loss_r": INSUFFICIENT,
            "concentration_warnings": [],
            "daily": [],
            "note": "No genuine forward DVP trades yet. Historical Phase 46 daily P&L corr was 0.60 — not a forward statistic.",
        }

    nq_days = defaultdict(list)
    es_days = defaultdict(list)
    for r in nq:
        nq_days[r["_date"]].append(r)
    for r in es:
        es_days[r["_date"]].append(r)
    union = sorted(set(nq_days) | set(es_days))
    nq_active_days = set(nq_days)
    es_active_days = set(es_days)
    both = nq_active_days & es_active_days

    same_dir = opp_dir = sim_open = 0
    warnings: list[dict[str, Any]] = []
    daily_rows: list[dict[str, Any]] = []
    family_r_series: list[float] = []
    paired_nq: list[float] = []
    paired_es: list[float] = []

    for d in union:
        nq_rows = nq_days.get(d, [])
        es_rows = es_days.get(d, [])
        nq_r = sum(_r_of(r, 80.0) or 0.0 for r in nq_rows)
        es_r = sum(_r_of(r, 18.0) or 0.0 for r in es_rows)
        comb = nq_r + es_r
        family_r_series.append(comb)
        nq_dirs = {_is_short(r["_dir"]) for r in nq_rows}
        es_dirs = {_is_short(r["_dir"]) for r in es_rows}
        same = False
        opp = False
        if nq_rows and es_rows:
            if nq_dirs == es_dirs and len(nq_dirs) == 1:
                same = True
                same_dir += 1
            elif nq_dirs.isdisjoint(es_dirs) and nq_dirs and es_dirs:
                opp = True
                opp_dir += 1
            if any(_overlap_interval(a, b) for a in nq_rows for b in es_rows):
                sim_open += 1
            if same:
                warnings.append(
                    {
                        "state": "DVP_FAMILY_CONCENTRATION",
                        "session_date": d,
                        "nq_r": nq_r,
                        "es_r": es_r,
                        "combined_r": comb,
                        "note": "Diagnostic only. Does not block trades.",
                    }
                )
            paired_nq.append(nq_r)
            paired_es.append(es_r)
        daily_rows.append(
            {
                "session_date": d,
                "nq_active": bool(nq_rows),
                "es_active": bool(es_rows),
                "both_active": bool(nq_rows and es_rows),
                "same_direction": same,
                "opposite_direction": opp,
                "simultaneous_position": bool(nq_rows and es_rows and any(_overlap_interval(a, b) for a in nq_rows for b in es_rows)),
                "nq_r": nq_r,
                "es_r": es_r,
                "combined_r": comb,
            }
        )

    equity = peak = max_dd = 0.0
    for x in family_r_series:
        equity += x
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
    worst = min((r["combined_r"] for r in daily_rows), default=None)

    p_es_nq = (len(both) / len(nq_active_days)) if nq_active_days else INSUFFICIENT
    p_nq_es = (len(both) / len(es_active_days)) if es_active_days else INSUFFICIENT
    corr = _corr(paired_nq, paired_es)
    return {
        "status": "OK" if (nq_n >= 30 and es_n >= 30) else INSUFFICIENT if (nq_n < 10 or es_n < 10) else "EARLY",
        "nq_forward_trades": nq_n,
        "es_forward_trades": es_n,
        "same_day_overlap": len(both) if both else (0 if union else INSUFFICIENT),
        "same_direction_overlap": same_dir,
        "opposite_direction_overlap": opp_dir,
        "simultaneous_position_overlap": sim_open,
        "p_es_active_given_nq": p_es_nq if nq_active_days else INSUFFICIENT,
        "p_nq_active_given_es": p_nq_es if es_active_days else INSUFFICIENT,
        "forward_pnl_correlation": corr if corr is not None else INSUFFICIENT,
        "combined_family_dd_r": max_dd if family_r_series else INSUFFICIENT,
        "worst_same_day_loss_r": worst if worst is not None else INSUFFICIENT,
        "concentration_warnings": warnings,
        "weighting": "equal_risk_diagnostic_NQ_R_plus_ES_R",
        "blocks_trades": False,
        "daily": daily_rows,
        "asof": datetime.now(tz=timezone.utc).isoformat(),
    }


def gc_diversification_monitor() -> dict[str, Any]:
    gc = _resolved(load_gc(), date_key="trading_date", pts_keys=("net_points", "gross_points", "mfe_points"))
    nq = _resolved(load_nq(), date_key="trading_date", pts_keys=("net_points", "gross_points"))
    es = _resolved(load_es(), date_key="session_date", pts_keys=("net_pnl_points", "raw_pnl_points"))
    if not gc or (not nq and not es):
        return {
            "status": INSUFFICIENT,
            "note": "Do not claim diversification from a tiny or empty forward sample.",
            "gc_forward_n": len(gc),
            "dvp_forward_n": len(nq) + len(es),
        }
    gc_days = {r["_date"] for r in gc}
    dvp_days = {r["_date"] for r in nq} | {r["_date"] for r in es}
    both = gc_days & dvp_days
    gc_loss = {r["_date"] for r in gc if (r.get("_pts") or 0) <= 0}
    dvp_by = defaultdict(float)
    for r in nq + es:
        dvp_by[r["_date"]] += r.get("_pts") or 0.0
    dvp_loss = {d for d, p in dvp_by.items() if p <= 0}
    return {
        "status": INSUFFICIENT if len(both) < 10 else "EARLY",
        "gc_forward_n": len(gc),
        "dvp_forward_n": len(nq) + len(es),
        "same_day_activity": len(both),
        "losing_day_overlap": len(gc_loss & dvp_loss),
        "rolling_forward_correlation": INSUFFICIENT,
        "note": "Diagnostic only. Do not claim diversification until a larger forward N exists.",
    }
