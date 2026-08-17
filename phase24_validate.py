"""Phase 24 — GC OR15 retest / FVG validation on Databento stitched series."""

from __future__ import annotations

import csv
import json
import statistics
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from bar_dataset import load_dataset
from gc_orb15_engine import (
    collect_or15_events,
    config_hash,
    find_boundary_retest,
    find_first_breakout_fvg,
    find_fvg_retrace_entry,
)
from gc_orb15_models import (
    OR_ANCHOR_LOCAL,
    OR_MINUTES,
    PHASE24_CANDIDATES,
    STRATEGY_FAMILY,
    STRATEGY_VERSION,
    EntryMode,
    ORB15StrategyConfig,
)
from gc_orb15_replay import replay_all_candidates
from gc_orb_engine import trading_dates_in_bars
from phase18_metrics import iter_entry_pairs, median_or_none, scorecard_from_pairs, theoretical_fixed_target_expectancy, safe_rate
from phase18_eligibility import ELIG_RESOLVED
from phase22_validate import _write_csv, chronological_date_split, evaluate_rows
from setup_journal import append_journal_records, load_journal_records

DATA_ROOT = Path("data") / "databento" / "GC" / "stitched"
JOURNAL_DIR = Path("journal") / "phase24_gc_orb15"
REPORTS = Path("reports")
VALIDATION_JSON = Path("phase24_validation.json")
CANDIDATES_DIR = Path("strategy_candidates")
GC_TICK = 0.1
MIN_TRAIN_N = 30


def load_bars():
    loaded = load_dataset("databento_GC_stitched", "5m", root=DATA_ROOT)
    return list(loaded.get("bars") or [])


def evaluate(rows: list[dict], cfg: ORB15StrategyConfig) -> dict[str, Any]:
    # Adapt Phase22 evaluate_rows shape via fake cfg fields
    class _C:
        candidate_id = cfg.candidate_id
        entry_mode = cfg.entry_mode

        def to_dict(self):
            return cfg.to_dict()

    return evaluate_rows(rows, _C())  # type: ignore[arg-type]


def select_finalists(train_metrics: list[dict]) -> list[dict]:
    eligible = [m for m in train_metrics if (m.get("resolved_n") or 0) >= MIN_TRAIN_N]
    if not eligible:
        eligible = sorted(train_metrics, key=lambda m: m.get("resolved_n") or 0, reverse=True)[:3]
        for m in eligible:
            m["selection_note"] = "below_min_train_n"
        return eligible[:3]

    def key(m):
        e2 = m.get("theoretical_2r_expectancy")
        e3 = m.get("theoretical_3r_expectancy")
        e2 = float(e2) if e2 is not None else -999
        e3 = float(e3) if e3 is not None else -999
        n = m.get("resolved_n") or 0
        stop = float(m.get("stop_rate") or 1)
        amb = m.get("ambiguous_n") or 0
        # Prefer positive E2R, then E3R, then N, lower stop/amb
        return (e2 > 0, e2, e3, n, -stop, -amb)

    ranked = sorted(eligible, key=key, reverse=True)
    out = ranked[:3]
    for m in out:
        m["selection_note"] = "train_rank_e2r_e3r_n_stop_amb"
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
    if days < 90:
        return "INSUFFICIENT_SAMPLE"
    if not holdouts:
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


def freeze_finalist(cfg: ORB15StrategyConfig, split, train_m, hold_m) -> Path:
    CANDIDATES_DIR.mkdir(parents=True, exist_ok=True)
    path = CANDIDATES_DIR / f"phase24_{cfg.candidate_id}.json"
    payload = {
        "phase": "phase24",
        "strategy_family": STRATEGY_FAMILY,
        "strategy_version": STRATEGY_VERSION,
        "instrument": "GC",
        "provider": "databento:GLBX.MDP3",
        "candidate": cfg.to_dict(),
        "config_hash": config_hash(cfg),
        "predeclared": {
            "or_anchor": f"{OR_ANCHOR_LOCAL} America/New_York",
            "or_minutes": OR_MINUTES,
            "max_retest_bars": cfg.max_retest_bars,
            "max_fvg_creation_bars": cfg.max_fvg_creation_bars,
            "max_fvg_retrace_bars": cfg.max_fvg_retrace_bars,
            "volume_filter": False,
            "displacement_filter": False,
        },
        "selection_split": split,
        "train_snapshot": {
            "resolved_n": train_m.get("resolved_n"),
            "theoretical_2r_expectancy": train_m.get("theoretical_2r_expectancy"),
            "theoretical_3r_expectancy": train_m.get("theoretical_3r_expectancy"),
        },
        "holdout_snapshot": {
            "resolved_n": hold_m.get("resolved_n"),
            "theoretical_2r_expectancy": hold_m.get("theoretical_2r_expectancy"),
            "theoretical_3r_expectancy": hold_m.get("theoretical_3r_expectancy"),
        },
        "note": "NOT production default — Phase 24 research only",
    }
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    return path


def paired_analysis(rows: list[dict], events) -> list[dict]:
    by_event: dict[str, dict[str, Any]] = {}
    for r in rows:
        ex = r.get("extras") or {}
        eid = ex.get("orb_breakout_event_id")
        if not eid:
            continue
        cid = ex.get("candidate_id")
        slot = by_event.setdefault(
            eid,
            {
                "orb_breakout_event_id": eid,
                "trading_date": r.get("trading_date"),
                "direction": r.get("direction"),
            },
        )
        pairs = iter_entry_pairs([r], entry_mode=ex.get("entry_mode") or "BREAKOUT_CLOSE")
        sc = scorecard_from_pairs(pairs)
        slot[cid] = {
            "triggered": bool(sc.get("triggered_n")),
            "resolved_n": sc.get("resolved_n"),
            "entry_price": None,
            "risk_distance": None,
            "stop_rate": sc.get("stop_rate"),
            "r1_rate": sc.get("r1_rate"),
            "r2_rate": sc.get("r2_rate"),
            "r3_rate": sc.get("r3_rate"),
            "median_mfe_r": sc.get("median_mfe_r"),
            "median_mae_r": sc.get("median_mae_r"),
        }
        # pull entry/risk from journal entry_results if present
        ers = r.get("entry_results") or []
        if ers:
            e0 = ers[0] if isinstance(ers[0], dict) else getattr(ers[0], "__dict__", {})
            if isinstance(e0, dict):
                slot[cid]["entry_price"] = e0.get("entry_price")
                slot[cid]["risk_distance"] = e0.get("risk_distance") or e0.get("initial_risk")
    return list(by_event.values())


def run_phase24(*, force_replay: bool = False) -> dict[str, Any]:
    REPORTS.mkdir(parents=True, exist_ok=True)
    JOURNAL_DIR.mkdir(parents=True, exist_ok=True)
    bars = load_bars()
    if len(bars) < 1000:
        payload = {
            "ok": False,
            "phase": 24,
            "verdict": "INSUFFICIENT_SAMPLE",
            "error": "missing_databento_stitched_bars",
            "path": str(DATA_ROOT),
        }
        VALIDATION_JSON.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return payload

    opening_ranges, events, roll_flags = collect_or15_events(bars)
    complete_ors = [o for o in opening_ranges if o.complete]
    dates = trading_dates_in_bars(bars)

    journal_path = JOURNAL_DIR / "setups.jsonl"
    if force_replay or not journal_path.exists():
        by_cand = replay_all_candidates(bars, candidates=PHASE24_CANDIDATES)
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
    # Force method label for Phase 24
    split = {**split, "method": "chronological_trading_date_70_30_databento_phase24"}

    train_metrics = [evaluate(train_rows, c) for c in PHASE24_CANDIDATES]
    finalists = select_finalists(train_metrics)
    finalist_cfgs = []
    for f in finalists:
        for cfg in PHASE24_CANDIDATES:
            if cfg.candidate_id == f["candidate_id"]:
                finalist_cfgs.append(cfg)
                break
    hold_metrics = [evaluate(hold_rows, cfg) for cfg in finalist_cfgs]
    stability = {t["candidate_id"]: classify_stability(t, h) for t, h in zip(finalists, hold_metrics)}

    # Funnel / pullback diagnostics on full sample
    ordered = sorted(bars, key=lambda b: int(b.time))
    retest_n = 0
    fvg_n = 0
    fvg_retrace_touch = 0
    fvg_retrace_ce = 0
    fvg_gaps = []
    fvg_overlap_boundary = 0
    fvg_outside = 0
    for ev in events:
        if ev.roll_artifact:
            continue
        if find_boundary_retest(ordered, ev, require_hold=True, max_retest_bars=12):
            retest_n += 1
        fvg = find_first_breakout_fvg(ordered, ev)
        if fvg:
            fvg_n += 1
            fvg_gaps.append(fvg.gap_size)
            # overlap OR boundary?
            if ev.direction == "bullish":
                overlaps = fvg.low <= ev.or_high <= fvg.high or fvg.low < ev.or_high
                fully_out = fvg.low >= ev.or_high
            else:
                overlaps = fvg.low <= ev.or_low <= fvg.high or fvg.high > ev.or_low
                fully_out = fvg.high <= ev.or_low
            if fully_out:
                fvg_outside += 1
            else:
                fvg_overlap_boundary += 1
            if find_fvg_retrace_entry(ordered, fvg, mode=EntryMode.FVG_TOUCH.value):
                fvg_retrace_touch += 1
            if find_fvg_retrace_entry(ordered, fvg, mode=EntryMode.FVG_CE.value):
                fvg_retrace_ce += 1

    n_bo = len([e for e in events if not e.roll_artifact])
    pullback = {
        "breakouts": n_bo,
        "boundary_retest_hold_rate": None if not n_bo else retest_n / n_bo,
        "fvg_creation_rate": None if not n_bo else fvg_n / n_bo,
        "fvg_retrace_touch_rate_given_fvg": None if not fvg_n else fvg_retrace_touch / fvg_n,
        "fvg_retrace_ce_rate_given_fvg": None if not fvg_n else fvg_retrace_ce / fvg_n,
        "missed_retest": n_bo - retest_n,
        "missed_fvg": n_bo - fvg_n,
        "fvg_created_no_touch_retrace": fvg_n - fvg_retrace_touch,
        "fvg_overlap_boundary": fvg_overlap_boundary,
        "fvg_fully_outside_or": fvg_outside,
        "median_fvg_gap": median_or_none(fvg_gaps),
    }

    # Risk distance by candidate (triggered rows)
    risk_by = {}
    for cfg in PHASE24_CANDIDATES:
        subset = [
            r
            for r in rows
            if (r.get("extras") or {}).get("candidate_id") == cfg.candidate_id
            and "ROLL_ARTIFACT" not in (r.get("reliability_flags") or [])
        ]
        dists = []
        for r in subset:
            for er in r.get("entry_results") or []:
                d = er.get("risk_distance") if isinstance(er, dict) else getattr(er, "risk_distance", None)
                if d is None and isinstance(er, dict):
                    d = er.get("initial_risk")
                if d is not None:
                    dists.append(float(d))
        risk_by[cfg.candidate_id] = {
            "n": len(dists),
            "median_risk_distance": median_or_none(dists),
        }

    # Direction split on A
    a_rows = [r for r in rows if (r.get("extras") or {}).get("candidate_id") == "A_ORB15_BREAKOUT_CLOSE"]
    direction_rows = []
    for side in ("bullish", "bearish"):
        sub = [r for r in a_rows if r.get("direction") == side]
        sc = scorecard_from_pairs(iter_entry_pairs(sub))
        direction_rows.append({"direction": side, **sc})

    # Walk-forward on finalists
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
                "date_start": dates_all[s] if dates_all else None,
                "date_end": dates_all[e - 1] if dates_all and e else None,
                "resolved_n": m.get("resolved_n"),
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

    # Cost sensitivity for positive holdout finalists
    cost_sens = []
    for h in hold_metrics:
        e2 = h.get("theoretical_2r_expectancy")
        rd = h.get("median_risk_distance") or 5.0
        for ticks in (0, 1, 2):
            friction_r = (2 * ticks * GC_TICK) / max(float(rd), GC_TICK)
            adj = None if e2 is None else float(e2) - friction_r
            cost_sens.append(
                {
                    "candidate_id": h.get("candidate_id"),
                    "ticks_per_side": ticks,
                    "tick_size": GC_TICK,
                    "friction_r": friction_r,
                    "e2r_raw": e2,
                    "e2r_after_friction": adj,
                    "survives": None if adj is None else adj > 0,
                }
            )

    or_sizes = [o.range_size for o in complete_ors if o.range_size > 0]
    or_dist = {
        "n": len(or_sizes),
        "median": median_or_none(or_sizes),
        "p25": None if not or_sizes else sorted(or_sizes)[len(or_sizes) // 4],
        "p75": None if not or_sizes else sorted(or_sizes)[(3 * len(or_sizes)) // 4],
        "p90": None if not or_sizes else sorted(or_sizes)[int(0.9 * (len(or_sizes) - 1))],
    }

    # 3R question on best holdout finalist by E3R then E2R
    three_r_conclusion = "INSUFFICIENT_SAMPLE"
    best_hold = None
    if hold_metrics:
        best_hold = max(
            hold_metrics,
            key=lambda h: (
                h.get("theoretical_3r_expectancy") is not None,
                h.get("theoretical_3r_expectancy") or -999,
                h.get("theoretical_2r_expectancy") or -999,
                h.get("resolved_n") or 0,
            ),
        )
        r3 = best_hold.get("r3_rate")
        n = best_hold.get("resolved_n") or 0
        e2 = best_hold.get("theoretical_2r_expectancy")
        e3 = best_hold.get("theoretical_3r_expectancy")
        if n < 30 or r3 is None:
            three_r_conclusion = "INSUFFICIENT_SAMPLE"
        elif float(r3) > 0.25 + 0.05 and (e3 is not None and float(e3) > 0) and (e2 is None or float(e2) > -0.05):
            three_r_conclusion = "YES_PRELIMINARY"
        elif float(r3) >= 0.22:
            three_r_conclusion = "MIXED"
        else:
            three_r_conclusion = "NO"

    days = len(dates)
    verdict = decide_verdict(hold_metrics, stability, days=days)
    # Downgrade if regime-sensitive dominates
    if any(regime.get(c.candidate_id) == "REGIME_SENSITIVE" for c in finalist_cfgs):
        if verdict == "EDGE_OBSERVED":
            verdict = "WEAK_EDGE_OBSERVED"
        if all(
            (h.get("theoretical_2r_expectancy") or 0) <= 0
            or regime.get(h["candidate_id"]) == "REGIME_SENSITIVE"
            for h in hold_metrics
        ) and all((h.get("theoretical_2r_expectancy") or 0) <= 0 for h in hold_metrics):
            verdict = "NO_EDGE_OBSERVED"

    frozen = []
    for cfg, tm, hm in zip(finalist_cfgs, finalists, hold_metrics):
        frozen.append(str(freeze_finalist(cfg, split, tm, hm)))

    # Pairing sample (cap for file size)
    paired = paired_analysis(rows, events)
    paired_sample = paired[:500]

    # Compare A vs pullbacks on TRAIN E2R
    by_id = {m["candidate_id"]: m for m in train_metrics}
    a_e2 = (by_id.get("A_ORB15_BREAKOUT_CLOSE") or {}).get("theoretical_2r_expectancy")
    improve = {}
    for cid in (
        "B1_ORB15_RETEST_TOUCH",
        "B2_ORB15_RETEST_CLOSE",
        "C_ORB15_FVG_TOUCH",
        "D_ORB15_FVG_CE",
    ):
        e2 = (by_id.get(cid) or {}).get("theoretical_2r_expectancy")
        improve[cid] = None if a_e2 is None or e2 is None else float(e2) - float(a_e2)

    bull = sum(1 for e in events if e.direction == "bullish" and not e.roll_artifact)
    bear = sum(1 for e in events if e.direction == "bearish" and not e.roll_artifact)

    # Candidate trigger/resolved summary
    cand_summary = []
    for cfg in PHASE24_CANDIDATES:
        tm = next(m for m in train_metrics if m["candidate_id"] == cfg.candidate_id)
        # full-sample scorecard
        sub = [r for r in rows if (r.get("extras") or {}).get("candidate_id") == cfg.candidate_id]
        sc = scorecard_from_pairs(iter_entry_pairs(sub, entry_mode=cfg.entry_mode))
        cand_summary.append(
            {
                "candidate_id": cfg.candidate_id,
                "entry_mode": cfg.entry_mode,
                "triggered_n": sc.get("triggered_n"),
                "resolved_n": sc.get("resolved_n"),
                "ambiguous_n": sc.get("ambiguous_n"),
                "expired_n": sc.get("expired_n"),
                "train": tm,
            }
        )

    paper = verdict in ("EDGE_OBSERVED", "WEAK_EDGE_OBSERVED")
    best_id = None if not best_hold else best_hold.get("candidate_id")

    _write_csv(REPORTS / "phase24_funnel.csv", [
        {
            "candidate_id": m["candidate_id"],
            **(m.get("funnel") or {}),
            "resolved_n": m.get("resolved_n"),
            "triggered_n": m.get("triggered_n"),
        }
        for m in train_metrics
    ])
    _write_csv(REPORTS / "phase24_candidates.csv", [{**c, "train": json.dumps(c.get("train"), default=str)} for c in cand_summary])
    _write_csv(REPORTS / "phase24_paired_entries.csv", paired_sample)
    _write_csv(REPORTS / "phase24_train.csv", finalists)
    _write_csv(REPORTS / "phase24_holdout.csv", hold_metrics)
    _write_csv(REPORTS / "phase24_walkforward.csv", wf)
    _write_csv(REPORTS / "phase24_direction.csv", direction_rows)
    _write_csv(REPORTS / "phase24_fvg.csv", [pullback])
    _write_csv(REPORTS / "phase24_or_size.csv", [or_dist])
    _write_csv(REPORTS / "phase24_cost.csv", cost_sens)

    payload = {
        "ok": True,
        "phase": 24,
        "strategy_family": STRATEGY_FAMILY,
        "strategy_version": STRATEGY_VERSION,
        "prior_families_untouched": [
            "gc_orb_volume_v1",
            "session_sweep_choch_fvg",
            "liquidity_reclaim_v1",
        ],
        "generated_at": datetime.now(tz=timezone.utc).isoformat(),
        "dataset": {
            "provider": "databento:GLBX.MDP3",
            "path": str(DATA_ROOT / "databento_GC_stitched_5m.jsonl"),
            "bars_5m": len(bars),
            "trading_days": days,
            "period_note": "Phase 23 canonical stitched GC",
        },
        "opening_range": {
            "anchor": f"{OR_ANCHOR_LOCAL} America/New_York",
            "or_minutes": OR_MINUTES,
            "or15_complete": len(complete_ors),
            "or15_missing": max(0, days - len(complete_ors)),
            "or_size_distribution": or_dist,
        },
        "breakouts": {
            "total_first": len(events),
            "canonical_non_roll": n_bo,
            "bullish": bull,
            "bearish": bear,
            "roll_artifacts": sum(1 for e in events if e.roll_artifact),
            "opposite_break_after_first": sum(1 for e in events if e.opposite_break_after_first),
        },
        "pullback": pullback,
        "risk_distance_by_mode": risk_by,
        "e2r_improvement_vs_A_train": improve,
        "split": split,
        "train_metrics": train_metrics,
        "finalists": finalists,
        "holdout_metrics": hold_metrics,
        "stability": stability,
        "walkforward": wf,
        "regime": regime,
        "direction": direction_rows,
        "cost_sensitivity": cost_sens,
        "three_r": {
            "best_holdout_candidate": best_id,
            "r3_rate": None if not best_hold else best_hold.get("r3_rate"),
            "break_even": 0.25,
            "conclusion": three_r_conclusion,
        },
        "verdict": verdict,
        "best_candidate": best_id if paper else None,
        "frozen_paths": frozen if paper else frozen,  # still freeze TRAIN finalists for audit
        "paper_validation_justified": paper,
        "retest_fvg_improved_vs_breakout_close": any(
            (v is not None and v > 0.05) for v in improve.values()
        ),
        "replay": replay_meta,
        "break_even": {"1R": 0.5, "1.5R": 0.4, "2R": 1 / 3, "3R": 0.25},
        "roll_gap_flags": len(roll_flags),
        "limitations": [
            "1m intrabar resolver tested in unit tests; production replay uses 5m ambiguity flags",
            "B1 RETEST_TOUCH uses OR midpoint stop to avoid look-ahead on touch bar extremes",
            "No volume/displacement/CHoCH filters by design",
        ],
        "recommended_next_action": (
            "READY_FOR_PAPER_VALIDATION"
            if paper
            else "Retire gc_orb15_retest_fvg_v1 hypothesis or explore a new family — do not optimize timeouts/filters to rescue HOLDOUT"
            if verdict == "NO_EDGE_OBSERVED"
            else "Inspect sample depth / ambiguity before claiming edge"
        ),
    }
    VALIDATION_JSON.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    return payload


def main() -> None:
    import sys

    p = run_phase24(force_replay="--force-replay" in sys.argv)
    print(
        json.dumps(
            {
                "ok": p.get("ok"),
                "verdict": p.get("verdict"),
                "or15_complete": (p.get("opening_range") or {}).get("or15_complete"),
                "breakouts": (p.get("breakouts") or {}).get("total_first"),
                "finalists": [f.get("candidate_id") for f in (p.get("finalists") or [])],
                "holdout": [
                    {
                        "id": h.get("candidate_id"),
                        "n": h.get("resolved_n"),
                        "e2r": h.get("theoretical_2r_expectancy"),
                        "e3r": h.get("theoretical_3r_expectancy"),
                        "r3": h.get("r3_rate"),
                    }
                    for h in (p.get("holdout_metrics") or [])
                ],
                "three_r": (p.get("three_r") or {}).get("conclusion"),
                "paper": p.get("paper_validation_justified"),
                "recommended_next_action": p.get("recommended_next_action"),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
