"""Phase 27 — London VWAP mean-reversion falsification (isolated from Phase 26 NY V2)."""

from __future__ import annotations

import json
import statistics
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

from bar_dataset import load_dataset
from gc_vwap_engine import collect_all_sequences, config_hash, time_to_vwap_touch
from gc_vwap_london_engine import (
    collect_all_london_sequences,
    compute_london_vwap_series,
    london_trading_dates,
)
from gc_vwap_london_models import (
    LONDON_SESSION,
    NO_NEW_SETUP_AFTER_LOCAL,
    PHASE27_CANDIDATES,
    SESSION_END_LOCAL,
    SESSION_NOTE,
    SESSION_START_LOCAL,
    STRATEGY_FAMILY,
    STRATEGY_VERSION,
)
from gc_vwap_london_replay import replay_all_london_candidates
from gc_vwap_models import DEFAULT_NY_SESSION, GCVWAPStrategyConfig
from phase18_eligibility import ELIG_RESOLVED
from phase18_metrics import iter_entry_pairs, median_or_none, safe_rate, scorecard_from_pairs
from phase22_validate import _write_csv, chronological_date_split, evaluate_rows
from phase25_validate import evaluate as evaluate_vwap_rows
from setup_journal import append_journal_records, load_journal_records

DATA_ROOT = Path("data") / "databento" / "GC" / "stitched"
JOURNAL_DIR = Path("journal") / "phase27_gc_vwap_london"
NY_JOURNAL = Path("journal") / "phase25_gc_vwap" / "setups.jsonl"
REPORTS = Path("reports")
VALIDATION_JSON = Path("phase27_validation.json")
CANDIDATES_DIR = Path("strategy_candidates")
# Must not touch Phase 26 paper journal
PHASE26_PAPER = Path("journal") / "phase26_gc_vwap_v2_paper"
GC_TICK = 0.1
MIN_TRAIN_N = 30
LDN = ZoneInfo(LONDON_SESSION.timezone)
NY = ZoneInfo(DEFAULT_NY_SESSION.timezone)
NY_V2_ID = "V2_BAND_RECLAIM_2SIG_RETEST"
L2_ID = "L2_BAND_RECLAIM_2SIG_RETEST"


def load_bars():
    loaded = load_dataset("databento_GC_stitched", "5m", root=DATA_ROOT)
    return list(loaded.get("bars") or [])


def select_finalists(train_metrics: list[dict]) -> list[dict]:
    """Max 2 finalists. L0 control cannot be the only automatic pick unless best eligible."""
    eligible = [m for m in train_metrics if (m.get("resolved_n") or 0) >= MIN_TRAIN_N]

    def key(m):
        e2 = m.get("theoretical_2r_expectancy")
        e2 = float(e2) if e2 is not None else -999
        n = m.get("resolved_n") or 0
        stop = float(m.get("stop_rate") or 1)
        # Prefer non-control when e2 comparable
        is_control = 1 if str(m.get("candidate_id", "")).startswith("L0_") else 0
        return (e2 > 0, e2, n, -stop, -is_control)

    if not eligible:
        ranked = sorted(train_metrics, key=key, reverse=True)
        out = ranked[:2]
        for m in out:
            m["selection_note"] = "below_min_train_n"
        return out

    ranked = sorted(eligible, key=key, reverse=True)
    out = ranked[:2]
    for m in out:
        m["selection_note"] = "train_rank_e2r_n_stop_noncontrol"
    return out


def classify_stability(train: dict, hold: dict) -> str:
    hn = hold.get("resolved_n") or 0
    if hn < 15:
        return "INSUFFICIENT"
    te = train.get("theoretical_2r_expectancy")
    he = hold.get("theoretical_2r_expectancy")
    if te is None or he is None:
        return "INSUFFICIENT"
    te, he = float(te), float(he)
    if te > 0 and he > 0:
        return "STABLE_POSITIVE"
    if te <= 0 and he <= 0:
        return "STABLE_NEGATIVE"
    return "MIXED"


def classify_wf(block_metrics: list[dict]) -> str:
    usable = [b for b in block_metrics if (b.get("resolved_n") or 0) >= 5]
    if len(usable) < 2:
        return "INSUFFICIENT"
    pos = [b for b in usable if (b.get("e2r") or -1) > 0]
    if len(pos) == len(usable):
        return "STABLE_POSITIVE"
    if len(pos) == 0:
        return "STABLE_NEGATIVE"
    return "MIXED"


def decide_verdict(holdouts, stability, wf_regime, *, days: int, cost_ok: bool) -> str:
    if days < 90 or not holdouts:
        return "INSUFFICIENT_SAMPLE"
    if all(stability.get(h["candidate_id"]) == "INSUFFICIENT" for h in holdouts):
        return "INSUFFICIENT_SAMPLE"

    def _pos(h):
        return (h.get("theoretical_2r_expectancy") or -1) > 0 and (h.get("resolved_n") or 0) >= 30

    def _weak(h):
        return (h.get("theoretical_2r_expectancy") or -1) > 0 and (h.get("resolved_n") or 0) >= 15

    strong = []
    weak = []
    for h in holdouts:
        cid = h["candidate_id"]
        st = stability.get(cid)
        wf = wf_regime.get(cid)
        if _pos(h) and st in ("STABLE_POSITIVE", "MIXED") and wf in ("STABLE_POSITIVE", "MIXED") and cost_ok:
            if st == "STABLE_POSITIVE" and wf == "STABLE_POSITIVE" and days >= 180:
                strong.append(h)
            else:
                weak.append(h)
        elif _weak(h):
            weak.append(h)

    if strong:
        return "EDGE_OBSERVED"
    if weak:
        return "WEAK_EDGE_OBSERVED"
    meaningful = [h for h in holdouts if (h.get("resolved_n") or 0) >= 20]
    if meaningful and all((h.get("theoretical_2r_expectancy") or 0) <= 0 for h in meaningful):
        return "NO_EDGE_OBSERVED"
    if meaningful:
        return "NO_EDGE_OBSERVED"
    return "INSUFFICIENT_SAMPLE"


def freeze_finalist(cfg, split, train_m, hold_m) -> Path:
    CANDIDATES_DIR.mkdir(parents=True, exist_ok=True)
    path = CANDIDATES_DIR / f"phase27_{cfg.candidate_id}.json"
    path.write_text(
        json.dumps(
            {
                "phase": "phase27",
                "strategy_family": STRATEGY_FAMILY,
                "strategy_version": STRATEGY_VERSION,
                "instrument": "GC",
                "provider": "databento:GLBX.MDP3",
                "candidate": cfg.to_dict(),
                "config_hash": config_hash(cfg),
                "predeclared": {
                    "session": f"{SESSION_START_LOCAL}-{SESSION_END_LOCAL} Europe/London",
                    "no_new_setups_after": NO_NEW_SETUP_AFTER_LOCAL,
                    "sigma": 2.0,
                    "min_vwap_bars": 6,
                    "max_entry_bars": 6,
                    "volume_filter": False,
                    "session_note": SESSION_NOTE,
                },
                "selection_split": split,
                "train_snapshot": {
                    "resolved_n": train_m.get("resolved_n"),
                    "theoretical_2r_expectancy": train_m.get("theoretical_2r_expectancy"),
                    "vwap_hit_rate": train_m.get("vwap_hit_rate"),
                },
                "holdout_snapshot": {
                    "resolved_n": hold_m.get("resolved_n"),
                    "theoretical_2r_expectancy": hold_m.get("theoretical_2r_expectancy"),
                    "vwap_hit_rate": hold_m.get("vwap_hit_rate"),
                },
                "note": "NOT production — Phase 27 London research only; not frozen Phase 26 NY V2",
            },
            indent=2,
            default=str,
        ),
        encoding="utf-8",
    )
    return path


def _triggered_dates(rows: list[dict], candidate_id: str) -> set[str]:
    out = set()
    for r in rows:
        if (r.get("extras") or {}).get("candidate_id") != candidate_id:
            continue
        if "ROLL_ARTIFACT" in (r.get("reliability_flags") or []):
            continue
        for er in r.get("entry_results") or []:
            if isinstance(er, dict) and er.get("triggered") and er.get("entry_price") is not None:
                out.add(str(r.get("trading_date"))[:10])
                break
    return out


def _resolved_outcomes(rows: list[dict], candidate_id: str) -> list[dict[str, Any]]:
    """One row per resolved trade with signed approx R for correlation (2R binary-ish)."""
    subset = [
        r
        for r in rows
        if (r.get("extras") or {}).get("candidate_id") == candidate_id
        and "ROLL_ARTIFACT" not in (r.get("reliability_flags") or [])
    ]
    pairs = iter_entry_pairs(subset)
    out = []
    for p in pairs:
        if p.get("eligibility") != ELIG_RESOLVED:
            continue
        e = p["entry"]
        mfe = e.get("mfe_r") if isinstance(e, dict) else getattr(e, "mfe_r", None)
        outcome = str(e.get("outcome") if isinstance(e, dict) else getattr(e, "outcome", "") or "")
        # Approximate trade R: +2 if 2R reached before stop classification via progressive; else -1 on stop
        hit2 = mfe is not None and float(mfe) >= 2.0
        stop = outcome == "STOP_HIT"
        if hit2:
            r_approx = 2.0
            win = True
        elif stop:
            r_approx = -1.0
            win = False
        else:
            # other resolved (e.g. target without 2R) — use mfe clipped
            r_approx = float(mfe) if mfe is not None else 0.0
            win = r_approx > 0
        td = str(p.get("trading_date") or "")[:10]
        if not td:
            # from record
            rec = p.get("record") or {}
            td = str(rec.get("trading_date") or "")[:10]
        out.append({"trading_date": td, "r_approx": r_approx, "win": win, "mfe_r": mfe, "outcome": outcome})
    return out


def _day_best_r(outcomes: list[dict]) -> dict[str, float]:
    by: dict[str, list[float]] = {}
    for o in outcomes:
        td = o["trading_date"]
        if not td:
            continue
        by.setdefault(td, []).append(float(o["r_approx"]))
    return {d: sum(vs) / len(vs) for d, vs in by.items()}


def portfolio_sim(day_r: dict[str, float]) -> dict[str, Any]:
    if not day_r:
        return {"n_days": 0, "e2r_proxy": None, "max_dd_r": None, "longest_losing_streak": None}
    dates = sorted(day_r)
    equity = 0.0
    peak = 0.0
    max_dd = 0.0
    streak = 0
    max_streak = 0
    daily = []
    for d in dates:
        r = float(day_r[d])
        equity += r
        peak = max(peak, equity)
        max_dd = min(max_dd, equity - peak)
        if r < 0:
            streak += 1
            max_streak = max(max_streak, streak)
        else:
            streak = 0
        daily.append(r)
    # Approximate E2R as mean daily R / mean trades not available — report mean day R and total R / n_days
    return {
        "n_days": len(dates),
        "total_r": equity,
        "mean_daily_r": equity / len(dates),
        "max_dd_r": abs(max_dd),
        "longest_losing_streak": max_streak,
        "worst_day": min(daily) if daily else None,
        "best_day": max(daily) if daily else None,
    }


def prop_250_sim(trade_rs: list[float], dates: list[str]) -> dict[str, Any]:
    """$250 fixed risk, 2R framing — descriptive only."""
    if not trade_rs:
        return {"ok": False}
    risk = 250.0
    pnls = [r * risk for r in trade_rs]
    # monthly buckets
    by_m: dict[str, float] = {}
    for d, p in zip(dates, pnls):
        if not d:
            continue
        key = d[:7]
        by_m[key] = by_m.get(key, 0.0) + p
    months = list(by_m.values())
    # week approx
    weeks = max(1, len(set(dates)) / 5.0) if dates else 1
    return {
        "ok": True,
        "risk_per_trade": risk,
        "n_trades": len(trade_rs),
        "avg_trades_per_week": len(trade_rs) / weeks,
        "avg_trades_per_month": len(trade_rs) / max(1, len(by_m)),
        "mean_monthly_pnl": None if not months else statistics.mean(months),
        "median_monthly_pnl": None if not months else statistics.median(months),
        "worst_historical_month": None if not months else min(months),
        "max_historical_drawdown_pnl": None,
        "max_same_day_loss": None,
    }


def run_phase27(*, force_replay: bool = True) -> dict[str, Any]:
    assert not str(JOURNAL_DIR).startswith(str(PHASE26_PAPER)), "must not use phase26 journal"
    REPORTS.mkdir(parents=True, exist_ok=True)
    JOURNAL_DIR.mkdir(parents=True, exist_ok=True)

    bars = load_bars()
    if len(bars) < 1000:
        payload = {
            "ok": False,
            "phase": 27,
            "verdict": "INSUFFICIENT_SAMPLE",
            "error": "missing_databento_stitched_bars",
        }
        VALIDATION_JSON.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return payload

    dates = london_trading_dates(bars)
    # Dataset period filter note: use full stitched series (~2025-08 → 2026-08)
    valid_sessions = 0
    session_issues = 0
    for td in dates:
        states = compute_london_vwap_series(bars, td)
        if any(s.valid for s in states):
            valid_sessions += 1
        elif states:
            session_issues += 1
        elif not states:
            session_issues += 1

    seqs = collect_all_london_sequences(bars)
    clean = [s for s in seqs if not s.get("roll_artifact")]
    upper = [s for s in clean if s["side"] == "above"]
    lower = [s for s in clean if s["side"] == "below"]
    days_with_ext = {s["trading_date"] for s in clean}
    touch_stats = [time_to_vwap_touch(s) for s in clean]
    upper_touch = [t for t, s in zip(touch_stats, clean) if s["side"] == "above"]
    lower_touch = [t for t, s in zip(touch_stats, clean) if s["side"] == "below"]

    def _p_touch(rows):
        if not rows:
            return None
        return sum(1 for r in rows if r.get("touched")) / len(rows)

    minutes = [r["minutes_after"] for r in touch_stats if r.get("touched") and r.get("minutes_after") is not None]
    mean_min = None if not minutes else statistics.mean(minutes)
    structural = {
        "n_extensions": len(clean),
        "upper": len(upper),
        "lower": len(lower),
        "p_vwap_touch_upper": _p_touch(upper_touch),
        "p_vwap_touch_lower": _p_touch(lower_touch),
        "p_vwap_touch_overall": _p_touch(touch_stats),
        "median_minutes_to_vwap": median_or_none(minutes),
        "mean_minutes_to_vwap": mean_min,
        "no_reversion_rate": None if not touch_stats else 1 - (_p_touch(touch_stats) or 0),
        "valid_london_sessions": valid_sessions,
        "sessions_with_extension": len(days_with_ext),
        "extensions_per_session": None if valid_sessions == 0 else len(clean) / valid_sessions,
    }

    journal_path = JOURNAL_DIR / "setups.jsonl"
    if force_replay or not journal_path.exists():
        by_cand = replay_all_london_candidates(bars)
        all_recs = []
        for recs in by_cand.values():
            all_recs.extend(recs)
        if journal_path.exists():
            journal_path.unlink()
        append_journal_records(all_recs, path=journal_path)
        replay_meta = {"reused": False, "records": len(all_recs)}
    else:
        replay_meta = {"reused": True}

    rows = load_journal_records(path=journal_path)
    train_rows, hold_rows, split = chronological_date_split(rows, 0.70)
    split = {**split, "method": "chronological_trading_date_70_30_databento_phase27_london"}

    # Freeze finalists from TRAIN only before reading HOLDOUT metrics
    train_metrics = [evaluate_vwap_rows(train_rows, c) for c in PHASE27_CANDIDATES]
    finalists = select_finalists(train_metrics)
    finalist_cfgs: list[GCVWAPStrategyConfig] = []
    for f in finalists:
        for cfg in PHASE27_CANDIDATES:
            if cfg.candidate_id == f["candidate_id"]:
                finalist_cfgs.append(cfg)
                break

    hold_metrics = [evaluate_vwap_rows(hold_rows, cfg) for cfg in finalist_cfgs]
    stability = {t["candidate_id"]: classify_stability(t, h) for t, h in zip(finalists, hold_metrics)}

    # Walk-forward
    dates_all = sorted({str(r.get("trading_date"))[:10] for r in rows if r.get("trading_date")})
    wf = []
    wf_regime = {}
    n_blocks = 4
    size = max(1, len(dates_all) // n_blocks) if dates_all else 1
    for cfg in finalist_cfgs:
        cand_rows = [r for r in rows if (r.get("extras") or {}).get("candidate_id") == cfg.candidate_id]
        block_metrics = []
        for i in range(n_blocks):
            s = i * size
            e = (i + 1) * size if i < n_blocks - 1 else len(dates_all)
            dset = set(dates_all[s:e])
            br = [r for r in cand_rows if str(r.get("trading_date"))[:10] in dset]
            m = evaluate_vwap_rows(br, cfg)
            row = {
                "candidate_id": cfg.candidate_id,
                "block": i + 1,
                "resolved_n": m.get("resolved_n"),
                "stop_rate": m.get("stop_rate"),
                "r2_rate": m.get("r2_rate"),
                "e1r": m.get("theoretical_1r_expectancy"),
                "e2r": m.get("theoretical_2r_expectancy"),
                "e3r": m.get("theoretical_3r_expectancy"),
            }
            wf.append(row)
            block_metrics.append(row)
        wf_regime[cfg.candidate_id] = classify_wf(block_metrics)

    cost_sens = []
    cost_ok = True
    for h in hold_metrics:
        e2 = h.get("theoretical_2r_expectancy")
        rd = h.get("median_risk_distance") or 5.0
        for ticks in (0, 1, 2):
            friction = (2 * ticks * GC_TICK) / max(float(rd), GC_TICK)
            adj = None if e2 is None else float(e2) - friction
            survives = None if adj is None else adj > 0
            if ticks == 2 and (h.get("theoretical_2r_expectancy") or 0) > 0 and survives is False:
                cost_ok = False
            cost_sens.append(
                {
                    "candidate_id": h.get("candidate_id"),
                    "ticks_per_side": ticks,
                    "tick_size": GC_TICK,
                    "friction_r": friction,
                    "e2r_raw": e2,
                    "e2r_after_friction": adj,
                    "survives": survives,
                }
            )

    verdict = decide_verdict(hold_metrics, stability, wf_regime, days=valid_sessions, cost_ok=cost_ok)

    # Direction (L2 focus if present else first finalist)
    focus_id = L2_ID if any(c.candidate_id == L2_ID for c in PHASE27_CANDIDATES) else finalists[0]["candidate_id"]
    focus_rows = [r for r in rows if (r.get("extras") or {}).get("candidate_id") == focus_id]
    direction_rows = []
    for side, label in (("bearish", "upper_extension_short"), ("bullish", "lower_extension_long")):
        sub = [r for r in focus_rows if r.get("direction") == side]
        sc = scorecard_from_pairs(iter_entry_pairs(sub))
        vt = sum(
            1
            for r in sub
            for er in (r.get("entry_results") or [])
            if isinstance(er, dict) and er.get("triggered") and ((r.get("extras") or {}).get("vwap_touch") or {}).get("vwap_hit")
        )
        trig = sum(
            1
            for r in sub
            for er in (r.get("entry_results") or [])
            if isinstance(er, dict) and er.get("triggered")
        )
        direction_rows.append(
            {
                "bucket": label,
                "direction": side,
                "resolved_n": sc.get("resolved_n"),
                "stop_rate": sc.get("stop_rate"),
                "r2_rate": sc.get("r2_rate"),
                "e2r": sc.get("theoretical_2r_expectancy"),
                "vwap_hit_rate": safe_rate(vt, trig),
            }
        )

    # Time of day London
    tod_buckets = {
        "08:30-09:00": (830, 900),
        "09:00-10:00": (900, 1000),
        "10:00-11:00": (1000, 1100),
        "11:00-12:00": (1100, 1200),
    }
    l2_rows = [r for r in rows if (r.get("extras") or {}).get("candidate_id") == L2_ID]
    tod_rows = []
    for name, (a, b) in tod_buckets.items():
        n_ext = 0
        touches = 0
        for s in clean:
            dt = datetime.fromtimestamp(int(s["first_ts"]), tz=LDN)
            hm = dt.hour * 100 + dt.minute
            if a <= hm < b:
                n_ext += 1
                t = time_to_vwap_touch(s)
                if t.get("touched"):
                    touches += 1
        # L2 entries in bucket
        bucket_l2 = []
        for r in l2_rows:
            ts = r.get("sweep_timestamp") or (r.get("extras") or {}).get("first_extension_ts")
            if ts is None:
                continue
            dt = datetime.fromtimestamp(int(ts), tz=LDN)
            hm = dt.hour * 100 + dt.minute
            if a <= hm < b:
                bucket_l2.append(r)
        sc = evaluate_vwap_rows(bucket_l2, next(c for c in PHASE27_CANDIDATES if c.candidate_id == L2_ID))
        entry_n = sc.get("triggered_n") or 0
        tod_rows.append(
            {
                "bucket": name,
                "extensions": n_ext,
                "vwap_touch_rate": safe_rate(touches, n_ext),
                "l2_triggered": entry_n,
                "l2_entry_rate": safe_rate(entry_n, n_ext) if n_ext else None,
                "r2_rate": sc.get("r2_rate"),
                "e2r": sc.get("theoretical_2r_expectancy"),
                "filter_applied": False,
            }
        )

    # NY V2 comparison (read-only Phase 25 journal — do not touch Phase 26)
    ny_rows = load_journal_records(path=NY_JOURNAL) if NY_JOURNAL.exists() else []
    ny_trig_days = _triggered_dates(ny_rows, NY_V2_ID)
    lon_trig_days = _triggered_dates(rows, L2_ID)
    # Align on calendar dates present in either
    all_cal = set(dates)
    # NY journal uses NY trading dates — compare by ISO date string overlap
    both = ny_trig_days & lon_trig_days
    lon_only = lon_trig_days - ny_trig_days
    ny_only = ny_trig_days - lon_trig_days
    neither = all_cal - ny_trig_days - lon_trig_days

    ny_trig_n = sum(
        1
        for r in ny_rows
        if (r.get("extras") or {}).get("candidate_id") == NY_V2_ID
        for er in (r.get("entry_results") or [])
        if isinstance(er, dict) and er.get("triggered")
    )
    lon_trig_n = sum(
        1
        for r in rows
        if (r.get("extras") or {}).get("candidate_id") == L2_ID
        for er in (r.get("entry_results") or [])
        if isinstance(er, dict) and er.get("triggered")
    )
    ny_sessions = len({str(r.get("trading_date"))[:10] for r in ny_rows if r.get("trading_date")}) or 1
    freq = {
        "london_l2_triggered": lon_trig_n,
        "ny_v2_triggered": ny_trig_n,
        "london_setups_per_session": lon_trig_n / max(valid_sessions, 1),
        "ny_setups_per_session": ny_trig_n / max(ny_sessions, 1),
        "days_london_only": len(lon_only),
        "days_ny_only": len(ny_only),
        "days_both": len(both),
        "days_neither": len(neither & all_cal),
        "pct_london_trades_on_ny_days": None
        if not lon_trig_days
        else len(lon_trig_days & ny_trig_days) / len(lon_trig_days),
        "pct_ny_trades_on_london_days": None
        if not ny_trig_days
        else len(ny_trig_days & lon_trig_days) / len(ny_trig_days),
    }

    # Correlation on overlap days
    lon_out = _resolved_outcomes(rows, L2_ID)
    ny_out = _resolved_outcomes(ny_rows, NY_V2_ID)
    lon_day = _day_best_r(lon_out)
    ny_day = _day_best_r(ny_out)
    overlap_days = sorted(set(lon_day) & set(ny_day))
    contingency = {"lon_win_ny_win": 0, "lon_win_ny_loss": 0, "lon_loss_ny_win": 0, "lon_loss_ny_loss": 0}
    xs, ys = [], []
    for d in overlap_days:
        lw = lon_day[d] > 0
        nw = ny_day[d] > 0
        if lw and nw:
            contingency["lon_win_ny_win"] += 1
        elif lw and not nw:
            contingency["lon_win_ny_loss"] += 1
        elif (not lw) and nw:
            contingency["lon_loss_ny_win"] += 1
        else:
            contingency["lon_loss_ny_loss"] += 1
        xs.append(lon_day[d])
        ys.append(ny_day[d])
    corr = None
    if len(xs) >= 5:
        try:
            corr = statistics.correlation(xs, ys)
        except Exception:  # noqa: BLE001
            corr = None
    n_ov = len(overlap_days) or 1
    diversifies = None
    if overlap_days:
        both_win = contingency["lon_win_ny_win"] / n_ov
        both_loss = contingency["lon_loss_ny_loss"] / n_ov
        # Diversifies if correlation low/moderate and not dominated by both-loss clustering
        diversifies = (corr is None or corr < 0.5) and both_loss < 0.55
    else:
        both_win = both_loss = None

    correlation_report = {
        "overlap_resolved_days": len(overlap_days),
        "pearson_r_day_mean": corr,
        **contingency,
        "both_win_rate": both_win,
        "both_loss_rate": both_loss,
        "genuinely_diversifies": diversifies,
        "note": "Descriptive day-level mean R; not claim of statistical independence",
    }

    # Combined portfolio only if London independently positive
    london_passes = verdict in ("EDGE_OBSERVED", "WEAK_EDGE_OBSERVED")
    combined_rows = []
    prop = {"ok": False, "skipped": True, "reason": "london_did_not_independently_pass"}
    if london_passes:
        # Use L2 if in finalists else best holdout
        best = max(hold_metrics, key=lambda h: (h.get("theoretical_2r_expectancy") or -999))
        best_id = best.get("candidate_id") or L2_ID
        # Trade-level R series for L2 / NY V2
        lon_trades = _resolved_outcomes(rows, best_id if best_id.startswith("L") else L2_ID)
        # Prefer L2 for portfolio
        lon_trades = _resolved_outcomes(rows, L2_ID) or lon_trades
        ny_trades = _resolved_outcomes(ny_rows, NY_V2_ID)

        def _trade_day_map(trades):
            # sum R per day
            by: dict[str, float] = {}
            for t in trades:
                td = t["trading_date"]
                by[td] = by.get(td, 0.0) + float(t["r_approx"])
            return by

        lon_map = _trade_day_map(lon_trades)
        ny_map = _trade_day_map(ny_trades)
        combined_map = {d: lon_map.get(d, 0.0) + ny_map.get(d, 0.0) for d in set(lon_map) | set(ny_map)}
        ny_sim = portfolio_sim(ny_map)
        lon_sim = portfolio_sim(lon_map)
        comb_sim = portfolio_sim(combined_map)
        combined_rows = [
            {"portfolio": "NY_V2_only", **ny_sim, "e2r_holdout_ref": None},
            {"portfolio": "London_only", **lon_sim, "e2r_holdout_ref": best.get("theoretical_2r_expectancy")},
            {"portfolio": "NY_plus_London", **comb_sim, "e2r_holdout_ref": None},
        ]
        # Approximate combined E2R as mean trade R of concatenated trades
        all_r = [t["r_approx"] for t in lon_trades] + [t["r_approx"] for t in ny_trades]
        all_d = [t["trading_date"] for t in lon_trades] + [t["trading_date"] for t in ny_trades]
        prop = prop_250_sim(all_r, all_d)
        # max same-day loss
        if combined_map:
            prop["max_same_day_loss"] = min(combined_map.values()) * 250.0
            # drawdown in $ from cumulative
            eq = 0.0
            peak = 0.0
            max_dd = 0.0
            for d in sorted(combined_map):
                eq += combined_map[d] * 250.0
                peak = max(peak, eq)
                max_dd = min(max_dd, eq - peak)
            prop["max_historical_drawdown_pnl"] = abs(max_dd)
            prop["skipped"] = False

    frozen_paths = []
    if finalist_cfgs:
        frozen_paths = [
            str(freeze_finalist(cfg, split, tm, hm)).replace("\\", "/")
            for cfg, tm, hm in zip(finalist_cfgs, finalists, hold_metrics)
        ]

    paper_candidate = None
    if london_passes and hold_metrics:
        best = max(hold_metrics, key=lambda h: (h.get("theoretical_2r_expectancy") or -999))
        if (best.get("theoretical_2r_expectancy") or 0) > 0:
            paper_path = CANDIDATES_DIR / "phase27_gc_london_paper_candidate.json"
            paper_path.write_text(
                json.dumps(
                    {
                        "phase": "phase27",
                        "status": "candidate_for_separate_paper_validation",
                        "not_frozen": True,
                        "not_phase26": True,
                        "strategy_family": STRATEGY_FAMILY,
                        "candidate_id": best.get("candidate_id"),
                        "verdict": verdict,
                        "holdout_e2r": best.get("theoretical_2r_expectancy"),
                        "holdout_n": best.get("resolved_n"),
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
            paper_candidate = str(paper_path).replace("\\", "/")

    _write_csv(REPORTS / "phase27_london_structural.csv", [structural])
    _write_csv(REPORTS / "phase27_candidates_train.csv", train_metrics)
    _write_csv(REPORTS / "phase27_candidates_holdout.csv", hold_metrics)
    _write_csv(REPORTS / "phase27_walkforward.csv", wf)
    _write_csv(REPORTS / "phase27_cost_sensitivity.csv", cost_sens)
    _write_csv(REPORTS / "phase27_direction.csv", direction_rows)
    _write_csv(REPORTS / "phase27_time_of_day.csv", tod_rows)
    _write_csv(REPORTS / "phase27_frequency_vs_ny.csv", [freq])
    _write_csv(
        REPORTS / "phase27_ny_london_overlap.csv",
        [
            {
                "days_both": freq["days_both"],
                "days_london_only": freq["days_london_only"],
                "days_ny_only": freq["days_ny_only"],
                "pct_london_on_ny_days": freq["pct_london_trades_on_ny_days"],
                "pct_ny_on_london_days": freq["pct_ny_trades_on_london_days"],
            }
        ],
    )
    _write_csv(REPORTS / "phase27_ny_london_correlation.csv", [correlation_report])
    if london_passes:
        _write_csv(REPORTS / "phase27_combined_portfolio.csv", combined_rows + [{"prop_250": prop}])
    else:
        _write_csv(
            REPORTS / "phase27_combined_portfolio.csv",
            [{"status": "SKIPPED", "reason": "London did not independently pass"}],
        )

    best_london = None
    if hold_metrics:
        best_london = max(hold_metrics, key=lambda h: (h.get("theoretical_2r_expectancy") or -999))
    elif train_metrics:
        best_london = max(train_metrics, key=lambda h: (h.get("theoretical_2r_expectancy") or -999))

    independently_positive = bool(
        london_passes and best_london and (best_london.get("theoretical_2r_expectancy") or 0) > 0
    )
    increases_freq = bool(freq["london_l2_triggered"] > 0 and freq["days_london_only"] > 0)

    payload = {
        "ok": True,
        "phase": 27,
        "verdict": verdict,
        "strategy_family": STRATEGY_FAMILY,
        "strategy_version": STRATEGY_VERSION,
        "session": LONDON_SESSION.to_dict(),
        "phase26_untouched": True,
        "phase26_paper_journal_untouched": True,
        "dataset": {
            "provider": "databento:GLBX.MDP3",
            "bars_5m": len(bars),
            "london_calendar_days": len(dates),
            "valid_london_sessions": valid_sessions,
            "session_issues": session_issues,
            "period_note": "canonical Phase23 stitched GC ~2025-08-01 → 2026-08-14",
            "path": str(DATA_ROOT / "databento_GC_stitched_5m.jsonl").replace("\\", "/"),
        },
        "structural_reversion": structural,
        "frequency_vs_ny": freq,
        "split": split,
        "train_metrics": train_metrics,
        "finalists": [f.get("candidate_id") for f in finalists],
        "holdout_metrics": hold_metrics,
        "stability": stability,
        "walkforward": wf,
        "walkforward_regime": wf_regime,
        "cost_sensitivity": cost_sens,
        "direction": direction_rows,
        "time_of_day": tod_rows,
        "correlation": correlation_report,
        "combined_portfolio": combined_rows if london_passes else [],
        "prop_250": prop,
        "frozen_candidates": frozen_paths,
        "paper_candidate": paper_candidate,
        "best_london_candidate": None if not best_london else best_london.get("candidate_id"),
        "independently_positive": independently_positive,
        "increases_useful_frequency": increases_freq,
        "ready_for_separate_london_paper": bool(paper_candidate),
        "replay": replay_meta,
        "generated_at": datetime.now(tz=timezone.utc).isoformat(),
        "note": "Falsification study only — does not retune or contaminate frozen NY V2 Phase 26",
    }
    VALIDATION_JSON.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    return payload


if __name__ == "__main__":
    out = run_phase27(force_replay=True)
    print(
        json.dumps(
            {
                "ok": out.get("ok"),
                "verdict": out.get("verdict"),
                "finalists": out.get("finalists"),
                "best": out.get("best_london_candidate"),
                "structural_n": (out.get("structural_reversion") or {}).get("n_extensions"),
                "independently_positive": out.get("independently_positive"),
            },
            indent=2,
        )
    )
