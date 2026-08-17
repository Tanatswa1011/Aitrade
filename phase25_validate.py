"""Phase 25 — GC VWAP mean-reversion validation on Databento stitched series."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

from bar_dataset import load_dataset
from gc_vwap_engine import (
    collect_all_sequences,
    compute_session_vwap_series,
    config_hash,
    session_window,
    time_to_vwap_touch,
)
from gc_vwap_models import (
    OR_TIMEZONE,
    PHASE25_CANDIDATES,
    SESSION_END_LOCAL,
    SESSION_NOTE,
    SESSION_START_LOCAL,
    STRATEGY_FAMILY,
    STRATEGY_VERSION,
    GCVWAPStrategyConfig,
)
from gc_vwap_replay import replay_all_candidates
from gc_orb_engine import trading_dates_in_bars
from phase18_metrics import iter_entry_pairs, median_or_none, scorecard_from_pairs, safe_rate
from phase22_validate import _write_csv, chronological_date_split, evaluate_rows, rvol_bucket
from setup_journal import append_journal_records, load_journal_records

DATA_ROOT = Path("data") / "databento" / "GC" / "stitched"
JOURNAL_DIR = Path("journal") / "phase25_gc_vwap"
REPORTS = Path("reports")
VALIDATION_JSON = Path("phase25_validation.json")
CANDIDATES_DIR = Path("strategy_candidates")
GC_TICK = 0.1
MIN_TRAIN_N = 30
NY = ZoneInfo(OR_TIMEZONE)


def load_bars():
    loaded = load_dataset("databento_GC_stitched", "5m", root=DATA_ROOT)
    return list(loaded.get("bars") or [])


def evaluate(rows: list[dict], cfg: GCVWAPStrategyConfig) -> dict[str, Any]:
    class _C:
        candidate_id = cfg.candidate_id
        entry_mode = cfg.entry_mode

        def to_dict(self):
            return cfg.to_dict()

    sc = evaluate_rows(rows, _C())  # type: ignore[arg-type]
    # VWAP hit among triggered canonical rows
    subset = [
        r
        for r in rows
        if (r.get("extras") or {}).get("candidate_id") == cfg.candidate_id
        and "ROLL_ARTIFACT" not in (r.get("reliability_flags") or [])
    ]
    triggered = []
    vwap_hits = 0
    for r in subset:
        ers = r.get("entry_results") or []
        if not ers:
            continue
        e0 = ers[0] if isinstance(ers[0], dict) else {}
        if isinstance(ers[0], dict) and not ers[0].get("triggered"):
            continue
        # prefer journal triggered via extras vwap_touch on entry-ready
        if r.get("status") not in ("ENTRY_READY", "TRIGGERED") and not (
            isinstance(e0, dict) and e0.get("triggered")
        ):
            # still count if entry_results say triggered
            if not (isinstance(e0, dict) and e0.get("triggered")):
                continue
        triggered.append(r)
        vt = (r.get("extras") or {}).get("vwap_touch") or {}
        if vt.get("vwap_hit"):
            vwap_hits += 1
    # Better: count rows with entry_results triggered
    trig_rows = []
    for r in subset:
        for er in r.get("entry_results") or []:
            if isinstance(er, dict) and er.get("triggered") and er.get("entry_price") is not None:
                trig_rows.append(r)
                break
    vh = sum(1 for r in trig_rows if ((r.get("extras") or {}).get("vwap_touch") or {}).get("vwap_hit"))
    sc["vwap_hit_n"] = vh
    sc["vwap_hit_rate"] = safe_rate(vh, len(trig_rows))
    sc["vwap_trig_n"] = len(trig_rows)
    return sc


def select_finalists(train_metrics: list[dict]) -> list[dict]:
    eligible = [m for m in train_metrics if (m.get("resolved_n") or 0) >= MIN_TRAIN_N]
    # exclude pure control from being sole finalist unless needed — still allow V0 if best
    if not eligible:
        eligible = sorted(train_metrics, key=lambda m: m.get("resolved_n") or 0, reverse=True)[:3]
        for m in eligible:
            m["selection_note"] = "below_min_train_n"
        return eligible[:3]

    def key(m):
        e2 = m.get("theoretical_2r_expectancy")
        e2 = float(e2) if e2 is not None else -999
        vw = float(m.get("vwap_hit_rate") or 0)
        n = m.get("resolved_n") or 0
        stop = float(m.get("stop_rate") or 1)
        amb = m.get("ambiguous_n") or 0
        return (e2 > 0, e2, vw, n, -stop, -amb)

    ranked = sorted(eligible, key=key, reverse=True)
    out = ranked[:3]
    for m in out:
        m["selection_note"] = "train_rank_e2r_vwap_n_stop_amb"
    return out


def classify_stability(train: dict, hold: dict) -> str:
    hn = hold.get("resolved_n") or 0
    if hn < 15:
        return "INSUFFICIENT_SAMPLE"
    te = train.get("theoretical_2r_expectancy")
    he = hold.get("theoretical_2r_expectancy")
    if te is None or he is None:
        return "INSUFFICIENT_SAMPLE"
    te, he = float(te), float(he)
    if te > 0 and he > 0:
        return "STABLE_POSITIVE"
    if te <= 0 and he <= 0:
        return "STABLE_NEGATIVE"
    if te > 0 and he <= 0:
        return "REGIME_SENSITIVE"
    return "WEAK_POSITIVE"


def decide_verdict(holdouts, stability, *, days: int) -> str:
    if days < 90 or not holdouts:
        return "INSUFFICIENT_SAMPLE"
    if all(stability.get(h["candidate_id"]) == "INSUFFICIENT_SAMPLE" for h in holdouts):
        return "INSUFFICIENT_SAMPLE"
    pos = [
        h
        for h in holdouts
        if (h.get("theoretical_2r_expectancy") or -1) > 0
        and (h.get("resolved_n") or 0) >= 30
        and stability.get(h["candidate_id"]) in ("STABLE_POSITIVE", "WEAK_POSITIVE")
    ]
    weak = [
        h
        for h in holdouts
        if (h.get("theoretical_2r_expectancy") or -1) > 0
        and (h.get("resolved_n") or 0) >= 15
        and stability.get(h["candidate_id"]) in ("STABLE_POSITIVE", "WEAK_POSITIVE")
    ]
    if pos and days >= 180:
        return "EDGE_OBSERVED"
    if weak and days >= 120:
        return "WEAK_EDGE_OBSERVED"
    meaningful = [h for h in holdouts if (h.get("resolved_n") or 0) >= 20]
    if meaningful and all((h.get("theoretical_2r_expectancy") or 0) <= 0 for h in meaningful):
        return "NO_EDGE_OBSERVED"
    return "INSUFFICIENT_SAMPLE"


def freeze_finalist(cfg, split, train_m, hold_m) -> Path:
    CANDIDATES_DIR.mkdir(parents=True, exist_ok=True)
    path = CANDIDATES_DIR / f"phase25_{cfg.candidate_id}.json"
    path.write_text(
        json.dumps(
            {
                "phase": "phase25",
                "strategy_family": STRATEGY_FAMILY,
                "strategy_version": STRATEGY_VERSION,
                "instrument": "GC",
                "provider": "databento:GLBX.MDP3",
                "candidate": cfg.to_dict(),
                "config_hash": config_hash(cfg),
                "predeclared": {
                    "session": f"{SESSION_START_LOCAL}-{SESSION_END_LOCAL} America/New_York",
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
                "note": "NOT production default — Phase 25 research only",
            },
            indent=2,
            default=str,
        ),
        encoding="utf-8",
    )
    return path


def run_phase25(*, force_replay: bool = False) -> dict[str, Any]:
    REPORTS.mkdir(parents=True, exist_ok=True)
    JOURNAL_DIR.mkdir(parents=True, exist_ok=True)
    bars = load_bars()
    if len(bars) < 1000:
        payload = {
            "ok": False,
            "phase": 25,
            "verdict": "INSUFFICIENT_SAMPLE",
            "error": "missing_databento_stitched_bars",
        }
        VALIDATION_JSON.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return payload

    dates = trading_dates_in_bars(bars)
    # Valid VWAP sessions: at least min bars with volume
    valid_sessions = 0
    session_issues = 0
    for td in dates:
        states = compute_session_vwap_series(bars, td)
        if any(s.valid for s in states):
            valid_sessions += 1
        elif states:
            session_issues += 1

    seqs = collect_all_sequences(bars)
    upper = [s for s in seqs if s["side"] == "above" and not s.get("roll_artifact")]
    lower = [s for s in seqs if s["side"] == "below" and not s.get("roll_artifact")]
    days_upper = {s["trading_date"] for s in upper}
    days_lower = {s["trading_date"] for s in lower}
    days_both = days_upper & days_lower
    days_none = set(dates) - days_upper - days_lower

    # Structural reversion diagnostics
    touch_stats = [time_to_vwap_touch(s) for s in seqs if not s.get("roll_artifact")]
    upper_touch = [t for t, s in zip(touch_stats, [x for x in seqs if not x.get("roll_artifact")]) if s["side"] == "above"]
    lower_touch = [t for t, s in zip(touch_stats, [x for x in seqs if not x.get("roll_artifact")]) if s["side"] == "below"]

    def _p_touch(rows):
        if not rows:
            return None
        return sum(1 for r in rows if r.get("touched")) / len(rows)

    minutes = [r["minutes_after"] for r in touch_stats if r.get("touched") and r.get("minutes_after") is not None]
    structural = {
        "n_extensions": len(touch_stats),
        "p_vwap_touch_upper": _p_touch(upper_touch),
        "p_vwap_touch_lower": _p_touch(lower_touch),
        "p_vwap_touch_overall": _p_touch(touch_stats),
        "median_minutes_to_vwap": median_or_none(minutes),
        "p75_minutes": None if not minutes else sorted(minutes)[(3 * len(minutes)) // 4],
        "p90_minutes": None if not minutes else sorted(minutes)[int(0.9 * (len(minutes) - 1))],
        "no_reversion_rate": None if not touch_stats else 1 - (_p_touch(touch_stats) or 0),
        "days_upper": len(days_upper),
        "days_lower": len(days_lower),
        "days_both": len(days_both),
        "days_none": len(days_none),
        "upper_extensions": len(upper),
        "lower_extensions": len(lower),
    }

    journal_path = JOURNAL_DIR / "setups.jsonl"
    if force_replay or not journal_path.exists():
        by_cand = replay_all_candidates(bars)
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
    split = {**split, "method": "chronological_trading_date_70_30_databento_phase25"}

    train_metrics = [evaluate(train_rows, c) for c in PHASE25_CANDIDATES]
    finalists = select_finalists(train_metrics)
    finalist_cfgs = []
    for f in finalists:
        for cfg in PHASE25_CANDIDATES:
            if cfg.candidate_id == f["candidate_id"]:
                finalist_cfgs.append(cfg)
                break
    hold_metrics = [evaluate(hold_rows, cfg) for cfg in finalist_cfgs]
    stability = {t["candidate_id"]: classify_stability(t, h) for t, h in zip(finalists, hold_metrics)}

    # Direction on V1 if present else first reclaim candidate
    focus_id = "V1_BAND_RECLAIM_CLOSE"
    focus_rows = [r for r in rows if (r.get("extras") or {}).get("candidate_id") == focus_id]
    direction_rows = []
    for side, label in (("bearish", "upper_extension_short"), ("bullish", "lower_extension_long")):
        sub = [r for r in focus_rows if r.get("direction") == side]
        sc = scorecard_from_pairs(iter_entry_pairs(sub))
        direction_rows.append({"bucket": label, "direction": side, **sc})

    # Time of day buckets for first extension
    tod_buckets = {
        "08:50-09:30": (850, 930),
        "09:30-10:30": (930, 1030),
        "10:30-11:30": (1030, 1130),
        "11:30-12:30": (1130, 1230),
    }
    tod_rows = []
    for name, (a, b) in tod_buckets.items():
        n = 0
        touches = 0
        for s in seqs:
            if s.get("roll_artifact"):
                continue
            dt = datetime.fromtimestamp(int(s["first_ts"]), tz=NY)
            hm = dt.hour * 100 + dt.minute
            if a <= hm < b:
                n += 1
                t = time_to_vwap_touch(s)
                if t.get("touched"):
                    touches += 1
        tod_rows.append({"bucket": name, "extensions": n, "vwap_touch_rate": safe_rate(touches, n)})

    # Volume diagnostic on extension first bar (descriptive)
    vol_rows = []
    # use extras max_abs_z buckets instead if RVOL hard — compute simple prior median vol
    from gc_orb_engine import rolling_median_volume

    ordered = sorted(bars, key=lambda x: int(x.time))
    index_by_ts = {int(b.time): i for i, b in enumerate(ordered)}
    z_buckets = {"1-1.5": [], "1.5-2": [], "2-2.5": [], "2.5-3": [], ">3": []}
    for s in seqs:
        if s.get("roll_artifact"):
            continue
        z = abs(float(s["first_z"]))
        t = time_to_vwap_touch(s)
        key = (
            "1-1.5"
            if z < 1.5
            else "1.5-2"
            if z < 2
            else "2-2.5"
            if z < 2.5
            else "2.5-3"
            if z < 3
            else ">3"
        )
        # only extensions are >=2 by definition for primary; still bucket max
        z = abs(float(s["max_abs_z"]))
        key = (
            "2-2.5"
            if z < 2.5
            else "2.5-3"
            if z < 3
            else ">3"
            if z >= 3
            else "1.5-2"
            if z < 2
            else "1-1.5"
        )
        z_buckets.setdefault(key, []).append(1 if t.get("touched") else 0)
        # RVOL of first extension bar
        idx = index_by_ts.get(int(s["first_ts"]))
        if idx is None:
            continue
        ref = rolling_median_volume(ordered, idx, lookback=20)
        vol = ordered[idx].volume
        rvol = None if ref is None or ref <= 0 or vol is None else float(vol) / float(ref)
        bucket = rvol_bucket(rvol)
        vol_rows.append({"rvol_bucket": bucket, "touched": bool(t.get("touched")), "rvol": rvol})

    vol_summary = []
    by_b: dict[str, list] = {}
    for r in vol_rows:
        by_b.setdefault(r["rvol_bucket"], []).append(r)
    for bname, items in sorted(by_b.items()):
        vol_summary.append(
            {
                "rvol_bucket": bname,
                "n": len(items),
                "vwap_touch_rate": safe_rate(sum(1 for i in items if i["touched"]), len(items)),
            }
        )

    maxz_summary = []
    for k, hits in sorted(z_buckets.items()):
        if not hits:
            continue
        maxz_summary.append({"max_z_bucket": k, "n": len(hits), "vwap_touch_rate": safe_rate(sum(hits), len(hits))})

    # Walk-forward
    dates_all = sorted({str(r.get("trading_date"))[:10] for r in rows if r.get("trading_date")})
    wf = []
    n_blocks = 4
    size = max(1, len(dates_all) // n_blocks) if dates_all else 1
    regime = {}
    for cfg in finalist_cfgs:
        cand_rows = [r for r in rows if (r.get("extras") or {}).get("candidate_id") == cfg.candidate_id]
        block_metrics = []
        for i in range(n_blocks):
            s = i * size
            e = (i + 1) * size if i < n_blocks - 1 else len(dates_all)
            dset = set(dates_all[s:e])
            br = [r for r in cand_rows if str(r.get("trading_date"))[:10] in dset]
            m = evaluate(br, cfg)
            row = {
                "candidate_id": cfg.candidate_id,
                "block": i + 1,
                "resolved_n": m.get("resolved_n"),
                "vwap_hit_rate": m.get("vwap_hit_rate"),
                "stop_rate": m.get("stop_rate"),
                "r1_rate": m.get("r1_rate"),
                "r2_rate": m.get("r2_rate"),
                "r3_rate": m.get("r3_rate"),
                "e1r": m.get("theoretical_1r_expectancy"),
                "e2r": m.get("theoretical_2r_expectancy"),
                "e3r": m.get("theoretical_3r_expectancy"),
                "median_mfe_r": m.get("median_mfe_r"),
                "median_mae_r": m.get("median_mae_r"),
            }
            wf.append(row)
            block_metrics.append(row)
        pos = [b for b in block_metrics if (b.get("e2r") or -1) > 0 and (b.get("resolved_n") or 0) >= 5]
        usable = [b for b in block_metrics if (b.get("resolved_n") or 0) >= 5]
        if len(usable) < 2:
            regime[cfg.candidate_id] = "INSUFFICIENT_SAMPLE"
        elif len(pos) == len(usable):
            regime[cfg.candidate_id] = "STABLE_POSITIVE"
        elif len(pos) == 0:
            regime[cfg.candidate_id] = "STABLE_NEGATIVE"
        elif len(pos) == 1 and len(usable) >= 3:
            regime[cfg.candidate_id] = "REGIME_SENSITIVE"
        else:
            regime[cfg.candidate_id] = "WEAK_POSITIVE"

    cost_sens = []
    for h in hold_metrics:
        e2 = h.get("theoretical_2r_expectancy")
        rd = h.get("median_risk_distance") or 5.0
        for ticks in (0, 1, 2):
            friction = (2 * ticks * GC_TICK) / max(float(rd), GC_TICK)
            adj = None if e2 is None else float(e2) - friction
            cost_sens.append(
                {
                    "candidate_id": h.get("candidate_id"),
                    "ticks_per_side": ticks,
                    "tick_size": GC_TICK,
                    "friction_r": friction,
                    "e2r_raw": e2,
                    "e2r_after_friction": adj,
                    "survives": None if adj is None else adj > 0,
                }
            )

    days = len(dates)
    verdict = decide_verdict(hold_metrics, stability, days=days)
    if any(regime.get(c.candidate_id) == "REGIME_SENSITIVE" for c in finalist_cfgs):
        if verdict == "EDGE_OBSERVED":
            verdict = "WEAK_EDGE_OBSERVED"

    frozen = [str(freeze_finalist(cfg, split, tm, hm)) for cfg, tm, hm in zip(finalist_cfgs, finalists, hold_metrics)]

    # V0 vs V1 improvement
    by_id = {m["candidate_id"]: m for m in train_metrics}
    v0 = by_id.get("V0_NAIVE_2SIG_FADE") or {}
    v1 = by_id.get("V1_BAND_RECLAIM_CLOSE") or {}
    reclaim_improves = None
    if v0.get("theoretical_2r_expectancy") is not None and v1.get("theoretical_2r_expectancy") is not None:
        reclaim_improves = float(v1["theoretical_2r_expectancy"]) > float(v0["theoretical_2r_expectancy"])

    paper = verdict in ("EDGE_OBSERVED", "WEAK_EDGE_OBSERVED")
    best = None
    if hold_metrics:
        best = max(
            hold_metrics,
            key=lambda h: (
                h.get("theoretical_2r_expectancy") is not None,
                h.get("theoretical_2r_expectancy") or -999,
                h.get("resolved_n") or 0,
            ),
        )

    _write_csv(REPORTS / "phase25_funnel.csv", [
        {"candidate_id": m["candidate_id"], **(m.get("funnel") or {}), "resolved_n": m.get("resolved_n"), "vwap_hit_rate": m.get("vwap_hit_rate")}
        for m in train_metrics
    ])
    _write_csv(REPORTS / "phase25_extensions.csv", [
        {
            "event_id": f"{s['trading_date']}|{s['side']}|{s['first_ts']}",
            "trading_date": s["trading_date"],
            "side": s["side"],
            "max_abs_z": s["max_abs_z"],
            "first_z": s["first_z"],
            "reclaim": s["reclaim_bar"] is not None,
        }
        for s in seqs[:2000]
    ])
    _write_csv(REPORTS / "phase25_reversion_probability.csv", [structural])
    _write_csv(REPORTS / "phase25_candidates.csv", train_metrics)
    _write_csv(REPORTS / "phase25_train.csv", finalists)
    _write_csv(REPORTS / "phase25_holdout.csv", hold_metrics)
    _write_csv(REPORTS / "phase25_walkforward.csv", wf)
    _write_csv(REPORTS / "phase25_direction.csv", direction_rows)
    _write_csv(REPORTS / "phase25_time_of_day.csv", tod_rows)
    _write_csv(REPORTS / "phase25_volume.csv", vol_summary)
    _write_csv(REPORTS / "phase25_cost.csv", cost_sens)
    # paired sample
    paired = []
    by_event: dict[str, dict] = {}
    for r in rows:
        eid = (r.get("extras") or {}).get("vwap_extension_event_id")
        cid = (r.get("extras") or {}).get("candidate_id")
        if not eid or not cid:
            continue
        slot = by_event.setdefault(eid, {"vwap_extension_event_id": eid})
        ers = r.get("entry_results") or []
        e0 = ers[0] if ers and isinstance(ers[0], dict) else {}
        slot[cid] = {
            "triggered": bool(e0.get("triggered")),
            "entry_price": e0.get("entry_price"),
            "risk_distance": e0.get("risk_distance"),
            "outcome": e0.get("outcome"),
            "vwap_hit": ((r.get("extras") or {}).get("vwap_touch") or {}).get("vwap_hit"),
        }
    paired = list(by_event.values())[:400]
    _write_csv(REPORTS / "phase25_paired.csv", paired)

    payload = {
        "ok": True,
        "phase": 25,
        "strategy_family": STRATEGY_FAMILY,
        "strategy_version": STRATEGY_VERSION,
        "prior_families_untouched": [
            "session_sweep_choch_fvg",
            "liquidity_reclaim_v1",
            "gc_orb_volume_v1",
            "gc_orb15_retest_fvg_v1",
        ],
        "generated_at": datetime.now(tz=timezone.utc).isoformat(),
        "dataset": {
            "provider": "databento:GLBX.MDP3",
            "bars_5m": len(bars),
            "trading_days": days,
            "valid_vwap_sessions": valid_sessions,
            "session_data_issues": session_issues,
            "path": str(DATA_ROOT / "databento_GC_stitched_5m.jsonl"),
        },
        "vwap": {
            "session": f"{SESSION_START_LOCAL}-{SESSION_END_LOCAL} America/New_York",
            "methodology": "typical_price=(H+L+C)/3; VWAP=sum(tp*vol)/sum(vol); vol-weighted std vs running VWAP",
            "sigma_threshold": 2.0,
            "min_vwap_bars": 6,
            "session_note": SESSION_NOTE,
            "extension_counts": {
                "upper": len(upper),
                "lower": len(lower),
                "total": len(upper) + len(lower),
            },
        },
        "structural_reversion": structural,
        "max_z_buckets": maxz_summary,
        "split": split,
        "train_metrics": train_metrics,
        "finalists": finalists,
        "holdout_metrics": hold_metrics,
        "stability": stability,
        "walkforward": wf,
        "regime": regime,
        "direction": direction_rows,
        "time_of_day": tod_rows,
        "volume_diagnostic": vol_summary,
        "cost_sensitivity": cost_sens,
        "reclaim_improves_vs_naive_train": reclaim_improves,
        "structural_vwap_mean_reversion_supported": (
            structural.get("p_vwap_touch_overall") is not None
            and float(structural["p_vwap_touch_overall"]) >= 0.55
        ),
        "verdict": verdict,
        "best_candidate": None if not paper or not best else best.get("candidate_id"),
        "frozen_paths": frozen,
        "paper_validation_justified": paper,
        "replay": replay_meta,
        "break_even": {"1R": 0.5, "1.5R": 0.4, "2R": 1 / 3, "3R": 0.25},
        "limitations": [
            "Dynamic VWAP target tracked separately from fixed-R outcome engine",
            "V2/V3 mid/retest entries may reduce sample / increase ambiguity",
            "Session 08:20–13:30 is a research definition, not exclusive Globex hours",
        ],
        "recommended_next_action": (
            "READY_FOR_PAPER_VALIDATION"
            if paper
            else "Retire gc_vwap_mean_reversion_v1 — do not add volume/ORB/FVG rescue filters"
            if verdict == "NO_EDGE_OBSERVED"
            else "Inspect sample/ambiguity before claiming edge"
        ),
    }
    VALIDATION_JSON.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    return payload


def main() -> None:
    import sys

    p = run_phase25(force_replay="--force-replay" in sys.argv)
    print(
        json.dumps(
            {
                "ok": p.get("ok"),
                "verdict": p.get("verdict"),
                "extensions": (p.get("vwap") or {}).get("extension_counts"),
                "structural": {
                    k: (p.get("structural_reversion") or {}).get(k)
                    for k in (
                        "p_vwap_touch_overall",
                        "p_vwap_touch_upper",
                        "p_vwap_touch_lower",
                        "median_minutes_to_vwap",
                        "no_reversion_rate",
                    )
                },
                "finalists": [f.get("candidate_id") for f in (p.get("finalists") or [])],
                "holdout": [
                    {
                        "id": h.get("candidate_id"),
                        "n": h.get("resolved_n"),
                        "e2r": h.get("theoretical_2r_expectancy"),
                        "vwap": h.get("vwap_hit_rate"),
                    }
                    for h in (p.get("holdout_metrics") or [])
                ],
                "reclaim_improves": p.get("reclaim_improves_vs_naive_train"),
                "structural_supported": p.get("structural_vwap_mean_reversion_supported"),
                "paper": p.get("paper_validation_justified"),
                "recommended_next_action": p.get("recommended_next_action"),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
