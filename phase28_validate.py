"""Phase 28 — GC NY momentum/continuation falsification (isolated from Phase 26/27)."""

from __future__ import annotations

import json
import statistics
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

from bar_dataset import load_dataset
from gc_momentum_engine import (
    collect_all_impulses,
    config_hash,
    detect_impulses,
    momentum_session_window,
    session_directional_efficiency,
    vwap_context_at_impulse,
)
from gc_momentum_models import (
    NO_NEW_SETUP_AFTER_LOCAL,
    OR_TIMEZONE,
    PHASE28_CANDIDATES,
    SESSION_END_LOCAL,
    SESSION_NOTE,
    SESSION_START_LOCAL,
    STRATEGY_FAMILY,
    STRATEGY_VERSION,
    GCMomentumStrategyConfig,
)
from gc_momentum_replay import replay_all_momentum_candidates
from gc_orb_engine import detect_roll_gap_timestamps, trading_dates_in_bars
from gc_vwap_engine import compute_session_vwap_series
from phase18_eligibility import ELIG_RESOLVED
from phase18_metrics import iter_entry_pairs, median_or_none, safe_rate, scorecard_from_pairs
from phase22_validate import _write_csv, chronological_date_split
from phase25_validate import evaluate as evaluate_candidate_rows
from setup_journal import append_journal_records, load_journal_records

DATA_ROOT = Path("data") / "databento" / "GC" / "stitched"
JOURNAL_DIR = Path("journal") / "phase28_gc_momentum"
NY_V2_JOURNAL = Path("journal") / "phase25_gc_vwap" / "setups.jsonl"
PHASE26_FROZEN = Path("strategy_frozen") / "gc_vwap_v2_phase26.json"
PHASE26_PAPER = Path("journal") / "phase26_gc_vwap_v2_paper" / "paper_trades.jsonl"
PHASE26_HASH = "0695d7a881b6247033426bceb077b6895b96405b826c21f32a109a02f44b2d43"
REPORTS = Path("reports")
VALIDATION_JSON = Path("phase28_validation.json")
CANDIDATES_DIR = Path("strategy_candidates")
GC_TICK = 0.1
MIN_TRAIN_N = 30
NY = ZoneInfo(OR_TIMEZONE)
NY_V2_ID = "V2_BAND_RECLAIM_2SIG_RETEST"


def load_bars():
    loaded = load_dataset("databento_GC_stitched", "5m", root=DATA_ROOT)
    return list(loaded.get("bars") or [])


def assert_phase26_untouched() -> dict[str, Any]:
    ok = True
    reasons = []
    if not PHASE26_FROZEN.exists():
        ok = False
        reasons.append("missing_frozen_file")
    else:
        doc = json.loads(PHASE26_FROZEN.read_text(encoding="utf-8"))
        if doc.get("frozen_config_hash") != PHASE26_HASH:
            ok = False
            reasons.append("frozen_hash_changed")
    if PHASE26_PAPER.exists() and PHASE26_PAPER.stat().st_size > 0:
        # allow empty journal; non-empty is ok if we didn't write — check we never write here
        pass
    return {"ok": ok, "reasons": reasons, "expected_hash": PHASE26_HASH}


def select_finalists(train_metrics: list[dict]) -> list[dict]:
    eligible = [m for m in train_metrics if (m.get("resolved_n") or 0) >= MIN_TRAIN_N]

    def key(m):
        e2 = m.get("theoretical_2r_expectancy")
        e2 = float(e2) if e2 is not None else -999
        n = m.get("resolved_n") or 0
        stop = float(m.get("stop_rate") or 1)
        is_control = 1 if str(m.get("candidate_id", "")).startswith("C0_") else 0
        return (e2 > 0, e2, n, -stop, -is_control)

    if not eligible:
        ranked = sorted(train_metrics, key=key, reverse=True)[:3]
        for m in ranked:
            m["selection_note"] = "below_min_train_n"
        return ranked
    ranked = sorted(eligible, key=key, reverse=True)[:3]
    for m in ranked:
        m["selection_note"] = "train_rank_e2r_n_stop"
    return ranked


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


def classify_wf(blocks: list[dict]) -> str:
    usable = [b for b in blocks if (b.get("resolved_n") or 0) >= 5]
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

    strong, weak = [], []
    for h in holdouts:
        cid = h["candidate_id"]
        e2 = h.get("theoretical_2r_expectancy") or -1
        n = h.get("resolved_n") or 0
        st = stability.get(cid)
        wf = wf_regime.get(cid)
        if e2 > 0 and n >= 30 and st in ("STABLE_POSITIVE", "MIXED") and wf in ("STABLE_POSITIVE", "MIXED"):
            if st == "STABLE_POSITIVE" and wf == "STABLE_POSITIVE" and cost_ok and days >= 180:
                strong.append(h)
            else:
                weak.append(h)
        elif e2 > 0 and n >= 15:
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
    path = CANDIDATES_DIR / f"phase28_{cfg.candidate_id}.json"
    path.write_text(
        json.dumps(
            {
                "phase": "phase28",
                "strategy_family": STRATEGY_FAMILY,
                "strategy_version": STRATEGY_VERSION,
                "instrument": "GC",
                "provider": "databento:GLBX.MDP3",
                "candidate": cfg.to_dict(),
                "config_hash": config_hash(cfg),
                "predeclared": {
                    "session": f"{SESSION_START_LOCAL}-{SESSION_END_LOCAL} America/New_York",
                    "no_new_setups_after": NO_NEW_SETUP_AFTER_LOCAL,
                    "range_multiplier": 1.5,
                    "rvol_threshold": 1.5,
                    "max_entry_bars": 4,
                    "session_note": SESSION_NOTE,
                },
                "selection_split": split,
                "train_snapshot": {
                    "resolved_n": train_m.get("resolved_n"),
                    "theoretical_2r_expectancy": train_m.get("theoretical_2r_expectancy"),
                },
                "holdout_snapshot": {
                    "resolved_n": hold_m.get("resolved_n"),
                    "theoretical_2r_expectancy": hold_m.get("theoretical_2r_expectancy"),
                },
                "note": "NOT production — Phase 28 momentum research; does not modify Phase 26 V2",
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


def _resolved_day_outcomes(rows: list[dict], candidate_id: str) -> dict[str, dict[str, Any]]:
    """trading_date -> {win, r_approx, e2_style}."""
    subset = [
        r
        for r in rows
        if (r.get("extras") or {}).get("candidate_id") == candidate_id
        and "ROLL_ARTIFACT" not in (r.get("reliability_flags") or [])
    ]
    by: dict[str, list] = {}
    for p in iter_entry_pairs(subset):
        if p.get("eligibility") != ELIG_RESOLVED:
            continue
        e = p["entry"]
        mfe = e.get("mfe_r") if isinstance(e, dict) else getattr(e, "mfe_r", None)
        outcome = str(e.get("outcome") if isinstance(e, dict) else getattr(e, "outcome", "") or "")
        hit2 = mfe is not None and float(mfe) >= 2.0
        stop = outcome == "STOP_HIT"
        if hit2:
            r_approx, win = 2.0, True
        elif stop:
            r_approx, win = -1.0, False
        else:
            r_approx = float(mfe) if mfe is not None else 0.0
            win = r_approx > 0
        td = str(p.get("trading_date") or (p.get("record") or {}).get("trading_date") or "")[:10]
        if not td:
            continue
        by.setdefault(td, []).append({"r_approx": r_approx, "win": win, "hit2": hit2, "stop": stop, "mfe_r": mfe})
    out = {}
    for td, items in by.items():
        rs = [i["r_approx"] for i in items]
        out[td] = {
            "r_mean": sum(rs) / len(rs),
            "r_sum": sum(rs),
            "win": sum(rs) > 0,
            "n": len(items),
            "hit2_n": sum(1 for i in items if i["hit2"]),
            "stop_n": sum(1 for i in items if i["stop"]),
        }
    return out


def _e2_on_days(rows: list[dict], cand_id: str, days: set[str]) -> dict[str, Any]:
    subset = [
        r
        for r in rows
        if (r.get("extras") or {}).get("candidate_id") == cand_id
        and str(r.get("trading_date"))[:10] in days
        and "ROLL_ARTIFACT" not in (r.get("reliability_flags") or [])
    ]
    if not subset:
        return {"resolved_n": 0, "r2_rate": None, "e2r": None}

    class _C:
        candidate_id = cand_id
        entry_mode = "x"

        def to_dict(self):
            return {"candidate_id": cand_id, "entry_mode": "x"}

    sc = evaluate_candidate_rows(subset, _C())  # type: ignore[arg-type]
    return {
        "resolved_n": sc.get("resolved_n"),
        "r2_rate": sc.get("r2_rate"),
        "e2r": sc.get("theoretical_2r_expectancy"),
        "stop_rate": sc.get("stop_rate"),
    }


def portfolio_sim(day_r: dict[str, float]) -> dict[str, Any]:
    if not day_r:
        return {"n_days": 0}
    dates = sorted(day_r)
    equity = peak = 0.0
    max_dd = 0.0
    streak = max_streak = 0
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
    return {
        "n_days": len(dates),
        "total_r": equity,
        "mean_daily_r": equity / len(dates),
        "max_dd_r": abs(max_dd),
        "longest_losing_streak": max_streak,
        "worst_day": min(daily),
        "best_day": max(daily),
    }


def run_phase28(*, force_replay: bool = True) -> dict[str, Any]:
    REPORTS.mkdir(parents=True, exist_ok=True)
    JOURNAL_DIR.mkdir(parents=True, exist_ok=True)
    p26 = assert_phase26_untouched()
    if not p26["ok"]:
        payload = {"ok": False, "phase": 28, "verdict": "INSUFFICIENT_SAMPLE", "error": "phase26_integrity", "p26": p26}
        VALIDATION_JSON.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return payload

    paper_size_before = PHASE26_PAPER.stat().st_size if PHASE26_PAPER.exists() else 0

    bars = load_bars()
    if len(bars) < 1000:
        payload = {"ok": False, "phase": 28, "verdict": "INSUFFICIENT_SAMPLE", "error": "missing_bars"}
        VALIDATION_JSON.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return payload

    dates = trading_dates_in_bars(bars)
    valid_sessions = 0
    session_issues = 0
    for td in dates:
        start, end, _ = momentum_session_window(td)
        sess = [b for b in bars if start <= int(b.time) < end]
        if len(sess) >= 6:
            valid_sessions += 1
        elif sess:
            session_issues += 1
        else:
            session_issues += 1

    ordered = sorted(bars, key=lambda b: int(b.time))
    roll = detect_roll_gap_timestamps(ordered)
    impulses = collect_all_impulses(bars)
    clean = [s for s in impulses if not s.get("roll_artifact")]
    bull = [s for s in clean if s["direction"] == "bullish"]
    bear = [s for s in clean if s["direction"] == "bearish"]
    rvols = [float(s["rvol"]) for s in clean if s.get("rvol") is not None]
    impulse_stats = {
        "total": len(clean),
        "bullish": len(bull),
        "bearish": len(bear),
        "avg_per_session": None if valid_sessions == 0 else len(clean) / valid_sessions,
        "rvol_median": median_or_none(rvols),
        "rvol_p25": None if not rvols else sorted(rvols)[len(rvols) // 4],
        "rvol_p75": None if not rvols else sorted(rvols)[(3 * len(rvols)) // 4],
        "rvol_ge_1_5_rate": safe_rate(sum(1 for x in rvols if x >= 1.5), len(rvols)),
    }

    journal_path = JOURNAL_DIR / "setups.jsonl"
    if force_replay or not journal_path.exists():
        by_cand = replay_all_momentum_candidates(bars)
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
    split = {**split, "method": "chronological_trading_date_70_30_databento_phase28"}

    # Adapter: evaluate expects cfg with candidate_id/entry_mode/to_dict
    train_metrics = [evaluate_candidate_rows(train_rows, c) for c in PHASE28_CANDIDATES]
    finalists = select_finalists(train_metrics)
    finalist_cfgs = []
    for f in finalists:
        for cfg in PHASE28_CANDIDATES:
            if cfg.candidate_id == f["candidate_id"]:
                finalist_cfgs.append(cfg)
                break

    hold_metrics = [evaluate_candidate_rows(hold_rows, cfg) for cfg in finalist_cfgs]
    stability = {t["candidate_id"]: classify_stability(t, h) for t, h in zip(finalists, hold_metrics)}

    dates_all = sorted({str(r.get("trading_date"))[:10] for r in rows if r.get("trading_date")})
    wf, wf_regime = [], {}
    n_blocks = 4
    size = max(1, len(dates_all) // n_blocks) if dates_all else 1
    for cfg in finalist_cfgs:
        cand_rows = [r for r in rows if (r.get("extras") or {}).get("candidate_id") == cfg.candidate_id]
        blocks = []
        for i in range(n_blocks):
            s = i * size
            e = (i + 1) * size if i < n_blocks - 1 else len(dates_all)
            dset = set(dates_all[s:e])
            br = [r for r in cand_rows if str(r.get("trading_date"))[:10] in dset]
            m = evaluate_candidate_rows(br, cfg)
            row = {
                "candidate_id": cfg.candidate_id,
                "block": i + 1,
                "resolved_n": m.get("resolved_n"),
                "r2_rate": m.get("r2_rate"),
                "e2r": m.get("theoretical_2r_expectancy"),
                "stop_rate": m.get("stop_rate"),
            }
            wf.append(row)
            blocks.append(row)
        wf_regime[cfg.candidate_id] = classify_wf(blocks)

    cost_sens = []
    cost_ok = True
    for h in hold_metrics:
        e2 = h.get("theoretical_2r_expectancy")
        rd = h.get("median_risk_distance") or 5.0
        for ticks in (0, 1, 2):
            friction = (2 * ticks * GC_TICK) / max(float(rd), GC_TICK)
            adj = None if e2 is None else float(e2) - friction
            survives = None if adj is None else adj > 0
            if ticks >= 1 and (e2 or 0) > 0 and survives is False:
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

    focus_id = finalists[0]["candidate_id"] if finalists else PHASE28_CANDIDATES[1].candidate_id
    focus_rows = [r for r in rows if (r.get("extras") or {}).get("candidate_id") == focus_id]
    direction_rows = []
    for side, label in (("bullish", "bullish_continuation"), ("bearish", "bearish_continuation")):
        sub = [r for r in focus_rows if r.get("direction") == side]
        sc = scorecard_from_pairs(iter_entry_pairs(sub))
        direction_rows.append(
            {
                "bucket": label,
                "direction": side,
                "resolved_n": sc.get("resolved_n"),
                "stop_rate": sc.get("stop_rate"),
                "r2_rate": sc.get("r2_rate"),
                "e2r": sc.get("theoretical_2r_expectancy"),
            }
        )

    tod_buckets = {
        "08:20-09:30": (820, 930),
        "09:30-10:30": (930, 1030),
        "10:30-11:30": (1030, 1130),
        "11:30-12:30": (1130, 1230),
    }
    focus_cfg = next(c for c in PHASE28_CANDIDATES if c.candidate_id == focus_id)
    tod_rows = []
    for name, (a, b) in tod_buckets.items():
        n_imp = 0
        for s in clean:
            dt = datetime.fromtimestamp(int(s["timestamp"]), tz=NY)
            hm = dt.hour * 100 + dt.minute
            if a <= hm < b:
                n_imp += 1
        bucket_rows = []
        for r in focus_rows:
            ts = r.get("sweep_timestamp")
            if ts is None:
                continue
            dt = datetime.fromtimestamp(int(ts), tz=NY)
            hm = dt.hour * 100 + dt.minute
            if a <= hm < b:
                bucket_rows.append(r)
        sc = evaluate_candidate_rows(bucket_rows, focus_cfg)
        tod_rows.append(
            {
                "bucket": name,
                "impulses": n_imp,
                "entries_triggered": sc.get("triggered_n"),
                "r2_rate": sc.get("r2_rate"),
                "e2r": sc.get("theoretical_2r_expectancy"),
                "filter_applied": False,
            }
        )

    # VWAP context descriptive
    vwap_rows = []
    by_side: dict[str, list] = {}
    for s in clean[:]:  # all impulses
        ctx = vwap_context_at_impulse(bars, s)
        by_side.setdefault(ctx["vwap_side"], []).append(ctx)
    for side, items in sorted(by_side.items()):
        zs = [float(i["z"]) for i in items if i.get("z") is not None]
        vwap_rows.append(
            {
                "vwap_side": side,
                "n_impulses": len(items),
                "median_z": median_or_none(zs),
                "note": "descriptive_only",
            }
        )

    # Efficiency sample
    effs = []
    for td in dates[::5]:
        e = session_directional_efficiency(bars, td)
        if e is not None:
            effs.append(e)

    # vs V2
    ny_rows = load_journal_records(path=NY_V2_JOURNAL) if NY_V2_JOURNAL.exists() else []
    mom_days = _triggered_dates(rows, focus_id)
    v2_days = _triggered_dates(ny_rows, NY_V2_ID)
    both = mom_days & v2_days
    mom_only = mom_days - v2_days
    v2_only = v2_days - mom_days
    neither = set(dates) - mom_days - v2_days

    mom_day_out = _resolved_day_outcomes(rows, focus_id)
    v2_day_out = _resolved_day_outcomes(ny_rows, NY_V2_ID)
    overlap = sorted(set(mom_day_out) & set(v2_day_out))
    contingency = {"mom_win_v2_win": 0, "mom_win_v2_loss": 0, "mom_loss_v2_win": 0, "mom_loss_v2_loss": 0}
    xs, ys = [], []
    for d in overlap:
        mw = bool(mom_day_out[d]["win"])
        vw = bool(v2_day_out[d]["win"])
        if mw and vw:
            contingency["mom_win_v2_win"] += 1
        elif mw and not vw:
            contingency["mom_win_v2_loss"] += 1
        elif (not mw) and vw:
            contingency["mom_loss_v2_win"] += 1
        else:
            contingency["mom_loss_v2_loss"] += 1
        xs.append(mom_day_out[d]["r_mean"])
        ys.append(v2_day_out[d]["r_mean"])
    corr = None
    if len(xs) >= 5:
        try:
            corr = statistics.correlation(xs, ys)
        except Exception:  # noqa: BLE001
            corr = None
    n_ov = len(overlap) or 1
    both_win = contingency["mom_win_v2_win"] / n_ov if overlap else None
    both_loss = contingency["mom_loss_v2_loss"] / n_ov if overlap else None

    v2_loss_days = {d for d, o in v2_day_out.items() if not o["win"]}
    mom_loss_days = {d for d, o in mom_day_out.items() if not o["win"]}
    mom_on_v2_loss = _e2_on_days(rows, focus_id, v2_loss_days)
    v2_on_mom_loss = _e2_on_days(ny_rows, NY_V2_ID, mom_loss_days)

    diversifies = None
    if overlap:
        # Diversifies if low/neg correlation OR positive mom E2R on V2-loss days
        mom_helps = (mom_on_v2_loss.get("e2r") or 0) > 0 and (mom_on_v2_loss.get("resolved_n") or 0) >= 10
        diversifies = (corr is None or corr < 0.35) and (mom_helps or (both_loss or 1) < 0.45)

    overlap_report = {
        "momentum_only_days": len(mom_only),
        "v2_only_days": len(v2_only),
        "both_days": len(both),
        "neither_days": len(neither),
        "focus_candidate": focus_id,
        "pearson_r_day_mean": corr,
        **contingency,
        "both_win_rate": both_win,
        "both_loss_rate": both_loss,
        "diversifies_v2": diversifies,
    }
    conditional = {
        "momentum_on_v2_loss_days": mom_on_v2_loss,
        "v2_on_momentum_loss_days": v2_on_mom_loss,
        "v2_loss_days_n": len(v2_loss_days),
        "momentum_loss_days_n": len(mom_loss_days),
    }

    passes = verdict in ("EDGE_OBSERVED", "WEAK_EDGE_OBSERVED")
    combined_rows = []
    prop = {"ok": False, "skipped": True, "reason": "momentum_did_not_independently_pass"}
    if passes and hold_metrics:
        best = max(hold_metrics, key=lambda h: (h.get("theoretical_2r_expectancy") or -999))
        best_id = best["candidate_id"]
        mom_map = {d: o["r_sum"] for d, o in _resolved_day_outcomes(rows, best_id).items()}
        v2_map = {d: o["r_sum"] for d, o in v2_day_out.items()}
        comb = {d: mom_map.get(d, 0.0) + v2_map.get(d, 0.0) for d in set(mom_map) | set(v2_map)}
        combined_rows = [
            {"portfolio": "V2_only", **portfolio_sim(v2_map)},
            {"portfolio": "momentum_only", **portfolio_sim(mom_map), "e2r_holdout": best.get("theoretical_2r_expectancy")},
            {"portfolio": "V2_plus_momentum", **portfolio_sim(comb)},
        ]
        # prop $250
        trades_r = []
        trades_d = []
        for d, o in _resolved_day_outcomes(rows, best_id).items():
            # expand approximate per-trade as day mean repeated n times is crude — use day sum as one unit
            trades_r.append(o["r_sum"])
            trades_d.append(d)
        for d, o in v2_day_out.items():
            trades_r.append(o["r_sum"])
            trades_d.append(d)
        risk = 250.0
        pnls = [r * risk for r in trades_r]
        by_m: dict[str, float] = {}
        for d, p in zip(trades_d, pnls):
            by_m[d[:7]] = by_m.get(d[:7], 0.0) + p
        months = list(by_m.values())
        weeks = max(1.0, len(set(trades_d)) / 5.0)
        multi_loss_days = sum(1 for d, o in {**_resolved_day_outcomes(rows, best_id), **{}}.items() if o.get("n", 0) > 1 and o["r_sum"] < 0)
        # recount multi-loss on combined day map
        multi_loss_days = sum(1 for d, r in comb.items() if r < -1.0)
        eq = peak = 0.0
        max_dd = 0.0
        for d in sorted(comb):
            eq += comb[d] * risk
            peak = max(peak, eq)
            max_dd = min(max_dd, eq - peak)
        prop = {
            "ok": True,
            "skipped": False,
            "risk_per_trade": risk,
            "avg_trades_per_week": (len(mom_map) + len(v2_map)) / weeks,
            "avg_trades_per_month": (len(mom_map) + len(v2_map)) / max(1, len(by_m)),
            "mean_monthly_pnl": None if not months else statistics.mean(months),
            "median_monthly_pnl": None if not months else statistics.median(months),
            "worst_historical_month": None if not months else min(months),
            "max_historical_drawdown_pnl": abs(max_dd),
            "max_same_day_loss": None if not comb else min(comb.values()) * risk,
            "days_with_multiple_losses_proxy": multi_loss_days,
        }

    frozen_paths = [
        str(freeze_finalist(cfg, split, tm, hm)).replace("\\", "/")
        for cfg, tm, hm in zip(finalist_cfgs, finalists, hold_metrics)
    ]

    paper_candidate = None
    if passes and hold_metrics:
        best = max(hold_metrics, key=lambda h: (h.get("theoretical_2r_expectancy") or -999))
        if (best.get("theoretical_2r_expectancy") or 0) > 0:
            paper_path = CANDIDATES_DIR / "phase28_gc_momentum_paper_candidate.json"
            paper_path.write_text(
                json.dumps(
                    {
                        "phase": "phase28",
                        "status": "candidate_for_separate_paper_validation",
                        "not_frozen": True,
                        "not_phase26": True,
                        "strategy_family": STRATEGY_FAMILY,
                        "candidate_id": best.get("candidate_id"),
                        "verdict": verdict,
                        "holdout_e2r": best.get("theoretical_2r_expectancy"),
                        "holdout_n": best.get("resolved_n"),
                        "cost_survives_1tick": cost_ok,
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
            paper_candidate = str(paper_path).replace("\\", "/")

    _write_csv(REPORTS / "phase28_candidates_train.csv", train_metrics)
    _write_csv(REPORTS / "phase28_candidates_holdout.csv", hold_metrics)
    _write_csv(REPORTS / "phase28_walkforward.csv", wf)
    _write_csv(REPORTS / "phase28_cost_sensitivity.csv", cost_sens)
    _write_csv(REPORTS / "phase28_direction.csv", direction_rows)
    _write_csv(REPORTS / "phase28_time_of_day.csv", tod_rows)
    _write_csv(REPORTS / "phase28_vwap_context.csv", vwap_rows)
    _write_csv(REPORTS / "phase28_vs_v2_overlap.csv", [overlap_report])
    _write_csv(REPORTS / "phase28_v2_loss_days.csv", [conditional])
    if passes:
        _write_csv(REPORTS / "phase28_combined_portfolio.csv", combined_rows + [{"prop_250": prop}])
    else:
        _write_csv(
            REPORTS / "phase28_combined_portfolio.csv",
            [{"status": "SKIPPED", "reason": "momentum_did_not_independently_pass"}],
        )

    best = None
    if hold_metrics:
        best = max(hold_metrics, key=lambda h: (h.get("theoretical_2r_expectancy") or -999))
    elif train_metrics:
        best = max(train_metrics, key=lambda h: (h.get("theoretical_2r_expectancy") or -999))

    paper_size_after = PHASE26_PAPER.stat().st_size if PHASE26_PAPER.exists() else 0

    payload = {
        "ok": True,
        "phase": 28,
        "verdict": verdict,
        "strategy_family": STRATEGY_FAMILY,
        "strategy_version": STRATEGY_VERSION,
        "phase26_untouched": assert_phase26_untouched(),
        "phase26_paper_size_before": paper_size_before,
        "phase26_paper_size_after": paper_size_after,
        "phase26_paper_unchanged": paper_size_before == paper_size_after,
        "dataset": {
            "provider": "databento:GLBX.MDP3",
            "bars_5m": len(bars),
            "trading_days": len(dates),
            "valid_ny_sessions": valid_sessions,
            "session_issues": session_issues,
            "period": "2025-08-01 → 2026-08-14",
        },
        "session": {
            "timezone": OR_TIMEZONE,
            "start": SESSION_START_LOCAL,
            "end": SESSION_END_LOCAL,
            "no_new_setups_after": NO_NEW_SETUP_AFTER_LOCAL,
        },
        "impulses": impulse_stats,
        "directional_efficiency_median": median_or_none(effs),
        "split": split,
        "train_metrics": train_metrics,
        "finalists": [f.get("candidate_id") for f in finalists],
        "holdout_metrics": hold_metrics,
        "stability": stability,
        "walkforward": wf,
        "walkforward_regime": wf_regime,
        "cost_sensitivity": cost_sens,
        "cost_survives_1_2_ticks": cost_ok,
        "direction": direction_rows,
        "time_of_day": tod_rows,
        "vwap_context": vwap_rows,
        "vs_v2": overlap_report,
        "conditional_vs_v2": conditional,
        "combined_portfolio": combined_rows if passes else [],
        "prop_250": prop,
        "frozen_candidates": frozen_paths,
        "paper_candidate": paper_candidate,
        "best_momentum_candidate": None if not best else best.get("candidate_id"),
        "independently_positive": bool(passes and best and (best.get("theoretical_2r_expectancy") or 0) > 0),
        "diversifies_v2": diversifies,
        "ready_for_separate_paper": bool(paper_candidate),
        "replay": replay_meta,
        "generated_at": datetime.now(tz=timezone.utc).isoformat(),
        "note": "Falsification only — does not retune Phase 26 V2 or Phase 27 London",
    }
    VALIDATION_JSON.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    return payload


if __name__ == "__main__":
    out = run_phase28(force_replay=True)
    print(
        json.dumps(
            {
                "ok": out.get("ok"),
                "verdict": out.get("verdict"),
                "finalists": out.get("finalists"),
                "best": out.get("best_momentum_candidate"),
                "impulses": (out.get("impulses") or {}).get("total"),
                "p26_untouched": out.get("phase26_untouched"),
                "paper_unchanged": out.get("phase26_paper_unchanged"),
            },
            indent=2,
        )
    )
