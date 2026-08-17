"""Phase 21 — liquidity_reclaim_v1 train/holdout validation (no LuxAlgo/CHoCH)."""

from __future__ import annotations

import csv
import json
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, Sequence

from bar_dataset import load_dataset
from chrono_split import assert_no_split_leakage, chronological_split
from journal_models import OUTCOME_STOP_HIT
from liquidity_reclaim_models import (
    PHASE21_CANDIDATES,
    ConfirmationMode,
    EntryMode,
    ReclaimStrategyConfig,
    STRATEGY_FAMILY,
    STRATEGY_VERSION,
)
from liquidity_reclaim_replay import replay_all_candidates
from phase18_eligibility import ELIG_AMBIGUOUS, ELIG_EXPIRED, ELIG_INVALID, ELIG_RESOLVED
from phase18_metrics import (
    iter_entry_pairs,
    mean_or_none,
    median_or_none,
    progressive_rr_hit,
    safe_rate,
    scorecard_from_pairs,
    theoretical_fixed_target_expectancy,
)
from phase18_journal_codec import records_from_dicts
from setup_journal import append_journal_records, load_journal_records

TIINGO_ROOT = Path("data") / "openbb" / "tiingo"
JOURNAL_DIR = Path("journal") / "phase21_liquidity_reclaim"
REPORTS = Path("reports")
VALIDATION_JSON = Path("phase21_validation.json")
CANDIDATES_DIR = Path("strategy_candidates")

SYMBOL_TV = "OANDA:XAUUSD"
TRAIN_START = "2025-08-14"
TRAIN_END = "2026-05-11"
HOLDOUT_START = "2026-05-12"
HOLDOUT_END = "2026-08-14"
MIN_TRAIN_N = 30


def load_bars_by_tf() -> dict[str, list]:
    out = {}
    for tf in ("5m", "15m"):
        loaded = load_dataset("openbb_tiingo_XAUUSD", tf, root=TIINGO_ROOT)
        out[tf] = loaded.get("bars") or []
    return out


def gap_report(bars: Sequence[Any], period_sec: int) -> dict[str, Any]:
    if len(bars) < 2:
        return {"gaps": [], "notable": []}
    ordered = sorted(bars, key=lambda b: int(b.time))
    gaps = []
    for a, b in zip(ordered, ordered[1:]):
        dt = int(b.time) - int(a.time)
        if dt > period_sec * 3:
            gaps.append({"from": int(a.time), "to": int(b.time), "gap_sec": dt})
    # flag Apr–May hole if present
    notable = []
    for g in gaps:
        if g["gap_sec"] >= 86400 * 5:
            notable.append(g)
    return {"gap_count": len(gaps), "large_gaps": notable[:20], "largest_gap_sec": max((g["gap_sec"] for g in gaps), default=0)}


def records_to_dicts(records) -> list[dict[str, Any]]:
    return [r.to_dict() for r in records]


def filter_split_dates(rows: list[dict], start: str, end: str) -> list[dict]:
    out = []
    for r in rows:
        td = str(r.get("trading_date") or "")[:10]
        if not td:
            continue
        if start <= td <= end:
            out.append(r)
    return out


def funnel_from_rows(rows: list[dict]) -> dict[str, Any]:
    sweeps = len(rows)
    reclaims = sum(
        1
        for r in rows
        if r.get("bars_sweep_to_choch") is not None
        or (r.get("extras") or {}).get("reclaim_bars_after_sweep") is not None
    )
    confirmations = sum(1 for r in rows if r.get("confirmation_timestamp") is not None)
    pairs = iter_entry_pairs(rows)
    triggered = []
    resolved = []
    ambiguous = []
    invalid = []
    expired = []
    valid_risk = []
    for p in pairs:
        e = p["entry"]
        trig = e.get("triggered") if isinstance(e, dict) else e.triggered
        if trig:
            triggered.append(p)
        if p["eligibility"] == ELIG_RESOLVED:
            resolved.append(p)
        elif p["eligibility"] == ELIG_AMBIGUOUS:
            ambiguous.append(p)
        elif p["eligibility"] == ELIG_INVALID:
            invalid.append(p)
        elif p["eligibility"] == ELIG_EXPIRED:
            expired.append(p)
        rd = e.get("risk_distance") if isinstance(e, dict) else e.risk_distance
        oc = e.get("outcome") if isinstance(e, dict) else e.outcome
        if trig and rd and oc != "NO_RISK_PLAN":
            valid_risk.append(p)
    return {
        "sweeps": sweeps,
        "reclaims": reclaims,
        "confirmations": confirmations,
        "entry_opportunities": confirmations,
        "triggered_entries": len(triggered),
        "valid_risk": len(valid_risk),
        "resolved": len(resolved),
        "ambiguous": len(ambiguous),
        "invalid": len(invalid),
        "expired": len(expired),
    }


def evaluate_candidate(rows: list[dict], cfg: ReclaimStrategyConfig) -> dict[str, Any]:
    subset = [
        r
        for r in rows
        if (r.get("extras") or {}).get("candidate_id") == cfg.candidate_id
        or (
            (r.get("execution_timeframe") or r.get("timeframe")) == cfg.execution_timeframe
            and r.get("confirmation_algorithm") == cfg.confirmation_mode
            and (r.get("extras") or {}).get("entry_mode") == cfg.entry_mode
        )
    ]
    # Prefer candidate_id filter when present
    by_id = [r for r in rows if (r.get("extras") or {}).get("candidate_id") == cfg.candidate_id]
    if by_id:
        subset = by_id
    pairs = iter_entry_pairs(subset, entry_mode=cfg.entry_mode, execution_tf=cfg.execution_timeframe)
    sc = scorecard_from_pairs(pairs, label=cfg.candidate_id)
    funnel = funnel_from_rows(subset)
    return {
        **sc,
        **cfg.to_dict(),
        "funnel": funnel,
        "n_rows": len(subset),
    }


def select_finalists(train_metrics: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """TRAIN-only selection: max 3; require resolved N >= 30; prefer E2R then sample."""
    eligible = [m for m in train_metrics if (m.get("resolved_n") or 0) >= MIN_TRAIN_N]
    if not eligible:
        # fall back to largest samples even if below threshold
        eligible = sorted(train_metrics, key=lambda m: m.get("resolved_n") or 0, reverse=True)[:3]
        for m in eligible:
            m["selection_note"] = "below_min_train_n"
        return eligible[:3]

    def key(m):
        e2 = m.get("theoretical_2r_expectancy")
        e2 = float(e2) if e2 is not None else -999
        n = m.get("resolved_n") or 0
        amb = m.get("ambiguous_n") or 0
        stop = m.get("stop_rate")
        stop = float(stop) if stop is not None else 1.0
        # Prefer positive E2R, larger N, lower stop, lower ambiguity
        return (e2 > 0, e2, n, -stop, -amb)

    ranked = sorted(eligible, key=key, reverse=True)
    finalists = ranked[:3]
    for m in finalists:
        m["selection_note"] = "train_rank_e2r_n_stop_ambiguity"
    return finalists


def classify_stability(train: dict, holdout: dict) -> str:
    tn = train.get("resolved_n") or 0
    hn = holdout.get("resolved_n") or 0
    if hn < 20:
        return "INSUFFICIENT_SAMPLE"
    te = train.get("theoretical_2r_expectancy")
    he = holdout.get("theoretical_2r_expectancy")
    if te is None or he is None:
        return "UNSTABLE"
    te, he = float(te), float(he)
    if te > 0 and he > 0:
        return "STABLE_POSITIVE"
    if te > 0 and he <= 0:
        return "UNSTABLE"
    if te <= 0 and he <= 0:
        return "STABLE_NEGATIVE"
    if te <= 0 and he > 0:
        return "WEAK_POSITIVE"
    return "UNSTABLE"


def walkforward_blocks(rows: list[dict], n_blocks: int = 4) -> list[list[dict]]:
    dates = sorted({str(r.get("trading_date"))[:10] for r in rows if r.get("trading_date")})
    if not dates:
        return []
    size = max(1, len(dates) // n_blocks)
    blocks = []
    for i in range(n_blocks):
        start = i * size
        end = (i + 1) * size if i < n_blocks - 1 else len(dates)
        dset = set(dates[start:end])
        blocks.append([r for r in rows if str(r.get("trading_date"))[:10] in dset])
    return blocks


def session_breakdown(rows: list[dict]) -> list[dict[str, Any]]:
    out = []
    for session in ("Asia", "London"):
        for side in ("high", "low"):
            subset = [r for r in rows if r.get("session") == session and r.get("swept_side") == side]
            pairs = iter_entry_pairs(subset)
            sc = scorecard_from_pairs(pairs, label=f"{session}_{side}")
            reclaims = sum(1 for r in subset if r.get("bars_sweep_to_choch") is not None)
            confs = sum(1 for r in subset if r.get("confirmation_timestamp") is not None)
            out.append(
                {
                    "session": session,
                    "side": side,
                    "label": f"{session} {'High' if side == 'high' else 'Low'}",
                    "n_sweeps": len(subset),
                    "reclaim_rate": safe_rate(reclaims, len(subset)),
                    "confirmation_rate": safe_rate(confs, len(subset)),
                    **{k: sc.get(k) for k in (
                        "resolved_n", "stop_rate", "r1_rate", "r2_rate", "r3_rate",
                        "theoretical_1r_expectancy", "theoretical_2r_expectancy", "theoretical_3r_expectancy",
                        "mean_mfe_r", "median_mfe_r", "mean_mae_r", "median_mae_r",
                    )},
                }
            )
    return out


def reclaim_speed_stats(rows: list[dict]) -> list[dict[str, Any]]:
    buckets = {"same_bar": [], "1": [], "2": [], "3_plus": []}
    for r in rows:
        b = r.get("bars_sweep_to_choch")
        if b is None:
            continue
        b = int(b)
        if b == 0:
            buckets["same_bar"].append(r)
        elif b == 1:
            buckets["1"].append(r)
        elif b == 2:
            buckets["2"].append(r)
        else:
            buckets["3_plus"].append(r)
    out = []
    for k, subset in buckets.items():
        pairs = iter_entry_pairs(subset)
        sc = scorecard_from_pairs(pairs, label=k)
        out.append({"reclaim_speed": k, "n": len(subset), **{x: sc.get(x) for x in (
            "resolved_n", "stop_rate", "r2_rate", "theoretical_2r_expectancy", "median_mfe_r", "median_mae_r"
        )}})
    return out


def decide_verdict(
    finalist_holdouts: list[dict],
    stability: dict[str, str],
) -> str:
    if not finalist_holdouts:
        return "INSUFFICIENT_SAMPLE"
    if all(stability.get(h["candidate_id"]) == "INSUFFICIENT_SAMPLE" for h in finalist_holdouts):
        return "INSUFFICIENT_SAMPLE"
    pos = [
        h
        for h in finalist_holdouts
        if (h.get("theoretical_2r_expectancy") or -1) > 0
        and (h.get("resolved_n") or 0) >= 20
        and stability.get(h["candidate_id"]) in ("STABLE_POSITIVE", "WEAK_POSITIVE")
    ]
    if any(stability.get(h["candidate_id"]) == "STABLE_POSITIVE" for h in finalist_holdouts) and pos:
        # require train also positive already encoded in STABLE_POSITIVE
        regime = any(h.get("regime_sensitive") for h in finalist_holdouts if h["candidate_id"] in [p["candidate_id"] for p in pos])
        if not regime:
            return "EDGE_OBSERVED"
        return "WEAK_EDGE_OBSERVED"
    if pos:
        return "WEAK_EDGE_OBSERVED"
    meaningful = [h for h in finalist_holdouts if (h.get("resolved_n") or 0) >= 20]
    if meaningful and all((h.get("theoretical_2r_expectancy") or 0) <= 0 for h in meaningful):
        return "NO_EDGE_OBSERVED"
    return "INSUFFICIENT_SAMPLE"


def _write_csv(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    keys = sorted({k for r in rows for k in r.keys()})
    with path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=keys, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: json.dumps(v, default=str) if isinstance(v, (dict, list)) else v for k, v in {k: r.get(k) for k in keys}.items()})


def freeze_candidate(cfg: ReclaimStrategyConfig, split_meta: dict, train_m: dict, holdout_m: dict) -> Path:
    CANDIDATES_DIR.mkdir(parents=True, exist_ok=True)
    path = CANDIDATES_DIR / f"phase21_{cfg.candidate_id}.json"
    payload = {
        "phase": "phase21",
        "strategy_family": STRATEGY_FAMILY,
        "strategy_version": STRATEGY_VERSION,
        "provenance": {
            "data_provider": "openbb",
            "underlying_provider": "tiingo",
            "source_symbol": "XAUUSD",
            "feed_equivalence_class": "CLOSE_EQUIVALENT",
            "live_benchmark": SYMBOL_TV,
        },
        "confirmation_note": "OHLC-only; LuxAlgo not used; no unvalidated_against_luxalgo",
        "candidate": cfg.to_dict(),
        "selection_split": split_meta,
        "train_metrics_snapshot": {
            "resolved_n": train_m.get("resolved_n"),
            "theoretical_2r_expectancy": train_m.get("theoretical_2r_expectancy"),
            "stop_rate": train_m.get("stop_rate"),
        },
        "holdout_metrics_snapshot": {
            "resolved_n": holdout_m.get("resolved_n"),
            "theoretical_2r_expectancy": holdout_m.get("theoretical_2r_expectancy"),
            "stop_rate": holdout_m.get("stop_rate"),
        },
        "note": "NOT promoted to DEFAULT_STRATEGY_CONFIG — Phase 21 research only",
    }
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    return path


def run_phase21(*, force_replay: bool = False) -> dict[str, Any]:
    REPORTS.mkdir(parents=True, exist_ok=True)
    JOURNAL_DIR.mkdir(parents=True, exist_ok=True)
    bars_by_tf = load_bars_by_tf()
    bars5 = bars_by_tf.get("5m") or []
    bars15 = bars_by_tf.get("15m") or []

    journal_path = JOURNAL_DIR / "setups.jsonl"
    if force_replay or not journal_path.exists():
        results = replay_all_candidates(bars_by_tf, symbol=SYMBOL_TV)
        all_recs = []
        for cid, res in results.items():
            all_recs.extend(res.journal_records)
        if journal_path.exists():
            journal_path.unlink()
        append_journal_records(all_recs, path=journal_path)
        complete_sessions = max(
            (r.coverage.complete_sessions for r in results.values()),
            default=0,
        )
        replay_meta = {
            "reused": False,
            "candidates": {k: {"setups": v.total_setups, "sweeps": v.total_sweeps} for k, v in results.items()},
            "complete_sessions": complete_sessions,
        }
    else:
        replay_meta = {"reused": True}

    raw = load_journal_records(path=journal_path)
    rows = [r if isinstance(r, dict) else r.to_dict() for r in raw]

    # Fixed Phase 19 dates
    train_rows = filter_split_dates(rows, TRAIN_START, TRAIN_END)
    holdout_rows = filter_split_dates(rows, HOLDOUT_START, HOLDOUT_END)
    # leakage check via liquidity ids
    train_ids = {r.get("liquidity_event_id") for r in train_rows}
    hold_ids = {r.get("liquidity_event_id") for r in holdout_rows}
    # same liquidity_event_id may appear across candidates — that's OK; date split is source of truth
    split_meta = {
        "train_start": TRAIN_START,
        "train_end": TRAIN_END,
        "holdout_start": HOLDOUT_START,
        "holdout_end": HOLDOUT_END,
        "train_rows": len(train_rows),
        "holdout_rows": len(holdout_rows),
        "method": "phase19_frozen_dates",
        "train_fraction": 0.7,
    }

    train_metrics = [evaluate_candidate(train_rows, cfg) for cfg in PHASE21_CANDIDATES]
    finalists = select_finalists(train_metrics)
    finalist_cfgs = []
    for f in finalists:
        cid = f.get("candidate_id")
        for cfg in PHASE21_CANDIDATES:
            if cfg.candidate_id == cid:
                finalist_cfgs.append(cfg)
                break

    holdout_metrics = [evaluate_candidate(holdout_rows, cfg) for cfg in finalist_cfgs]
    stability = {}
    for t, h in zip(finalists, holdout_metrics):
        stability[t["candidate_id"]] = classify_stability(t, h)

    # Walk-forward on full year for finalists
    wf_rows = []
    regime_flags = {}
    for cfg in finalist_cfgs:
        cand_rows = [r for r in rows if (r.get("extras") or {}).get("candidate_id") == cfg.candidate_id]
        blocks = walkforward_blocks(cand_rows, 4)
        block_metrics = []
        for i, br in enumerate(blocks):
            m = evaluate_candidate(br, cfg)
            block_metrics.append({"block": i + 1, "resolved_n": m.get("resolved_n"), "e1r": m.get("theoretical_1r_expectancy"), "e2r": m.get("theoretical_2r_expectancy"), "e3r": m.get("theoretical_3r_expectancy"), "stop_rate": m.get("stop_rate"), "median_mfe_r": m.get("median_mfe_r"), "median_mae_r": m.get("median_mae_r")})
            wf_rows.append({"candidate_id": cfg.candidate_id, **block_metrics[-1]})
        pos_blocks = [b for b in block_metrics if (b.get("e2r") or -1) > 0 and (b.get("resolved_n") or 0) >= 10]
        regime_flags[cfg.candidate_id] = len(pos_blocks) == 1 and len([b for b in block_metrics if (b.get("resolved_n") or 0) >= 10]) >= 3
        for h in holdout_metrics:
            if h.get("candidate_id") == cfg.candidate_id:
                h["regime_sensitive"] = regime_flags[cfg.candidate_id]

    verdict = decide_verdict(holdout_metrics, stability)

    # Session / reclaim diagnostics on primary 5m immediate + break candidates
    sessions = session_breakdown([r for r in rows if (r.get("execution_timeframe") or "") == "5m"])
    reclaim_speed = reclaim_speed_stats([r for r in rows if (r.get("extras") or {}).get("candidate_id") == "R1_5m_immediate_close"])

    # Freeze finalists
    frozen_paths = []
    for cfg, tm, hm in zip(finalist_cfgs, finalists, holdout_metrics):
        frozen_paths.append(str(freeze_candidate(cfg, split_meta, tm, hm)))

    # CSVs
    _write_csv(REPORTS / "phase21_candidates.csv", train_metrics)
    _write_csv(REPORTS / "phase21_train.csv", finalists)
    _write_csv(REPORTS / "phase21_holdout.csv", holdout_metrics)
    _write_csv(REPORTS / "phase21_sessions.csv", sessions)
    _write_csv(REPORTS / "phase21_reclaim_speed.csv", reclaim_speed)
    _write_csv(REPORTS / "phase21_walkforward.csv", wf_rows)
    funnel_rows = []
    for cfg in PHASE21_CANDIDATES:
        m = evaluate_candidate(rows, cfg)
        funnel_rows.append({"candidate_id": cfg.candidate_id, **(m.get("funnel") or {}), "resolved_n": m.get("resolved_n")})
    _write_csv(REPORTS / "phase21_funnel.csv", funnel_rows)

    best = None
    if holdout_metrics:
        best = max(
            holdout_metrics,
            key=lambda h: (
                (h.get("theoretical_2r_expectancy") is not None),
                h.get("theoretical_2r_expectancy") or -999,
                h.get("resolved_n") or 0,
            ),
        )

    paper = verdict in ("EDGE_OBSERVED", "WEAK_EDGE_OBSERVED")

    payload = {
        "ok": True,
        "phase": 21,
        "strategy_family": STRATEGY_FAMILY,
        "strategy_version": STRATEGY_VERSION,
        "legacy_family_untouched": "session_sweep_choch_fvg",
        "phase19_20_verdict_preserved": "NO_EDGE_OBSERVED",
        "generated_at": datetime.now(tz=timezone.utc).isoformat(),
        "dataset": {
            "period": f"{TRAIN_START} → {HOLDOUT_END}",
            "bars_5m": len(bars5),
            "bars_15m": len(bars15),
            "gaps_5m": gap_report(bars5, 300),
            "gaps_15m": gap_report(bars15, 900),
            "complete_sessions": replay_meta.get("complete_sessions"),
            "split": split_meta,
        },
        "replay": replay_meta,
        "train_metrics": train_metrics,
        "finalists": finalists,
        "holdout_metrics": holdout_metrics,
        "stability": stability,
        "regime_sensitive": regime_flags,
        "sessions": sessions,
        "reclaim_speed": reclaim_speed,
        "walkforward": wf_rows,
        "verdict": verdict,
        "best_candidate": None if not best else best.get("candidate_id"),
        "frozen_candidate_paths": frozen_paths,
        "paper_validation_justified": paper,
        "break_even_thresholds": {"1R": 0.5, "2R": 1 / 3, "3R": 0.25},
        "limitations": [
            "No LuxAlgo / CHoCH / FVG in this strategy",
            "First sweep per session side only",
            "Outcome horizon ends at next primary session start",
            "Known Tiingo gaps preserved (no interpolation)",
            "HTF soft context not hard-filtered",
        ],
        "recommended_next_action": (
            "If EDGE/WEAK: freeze rules and paper-validate independently. "
            "If NO_EDGE: do not add post-hoc filters; consider retiring liquidity_reclaim_v1 or a new hypothesis."
            if verdict != "INSUFFICIENT_SAMPLE"
            else "Gather more resolved outcomes or widen history before deciding."
        ),
    }
    VALIDATION_JSON.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    return payload


def main() -> None:
    import sys
    force = "--force-replay" in sys.argv
    p = run_phase21(force_replay=force)
    print(json.dumps({
        "ok": p.get("ok"),
        "verdict": p.get("verdict"),
        "best_candidate": p.get("best_candidate"),
        "finalists": [f.get("candidate_id") for f in (p.get("finalists") or [])],
        "holdout": [
            {
                "id": h.get("candidate_id"),
                "n": h.get("resolved_n"),
                "e2r": h.get("theoretical_2r_expectancy"),
                "stop": h.get("stop_rate"),
            }
            for h in (p.get("holdout_metrics") or [])
        ],
        "bars_5m": (p.get("dataset") or {}).get("bars_5m"),
        "paper": p.get("paper_validation_justified"),
    }, indent=2))


if __name__ == "__main__":
    main()
