"""Phase 19 — extend history, expand holdout, 1m ambiguity, CHoCH overlap."""

from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from bar_dataset import load_dataset
from bias_provider import StructureBiasProvider
from chrono_split import assert_no_split_leakage, chronological_split
from historical_structure import detect_internal_choch
from history_extend import (
    TIINGO_ROOT,
    current_5m_span,
    default_eighteen_month_target_ts,
    default_one_year_target_ts,
    extend_tiingo_5m_backward,
    rebuild_derived_timeframes,
)
from luxalgo_capture import captures_to_confirmations, load_luxalgo_captures
from luxalgo_overlap import compare_choch_overlap, tolerances_for_timeframe
from phase18_metrics import iter_entry_pairs, scorecard_from_pairs, timing_distribution
from phase18_selection import (
    StrategyCandidate,
    apply_expiry_policy,
    apply_htf_policy,
    classify_stability,
    recommendation_category,
)
from phase19_1m import (
    fetch_1m_windows,
    identify_5m_ambiguous_windows,
    resolve_5m_with_1m,
)
from replay_engine import replay_historical_mtf_setups
from sample_quality import sample_quality_label
from setup_journal import append_journal_records, load_journal_records
from strategy_config import DEFAULT_STRATEGY_CONFIG
from strategy_version import STRATEGY_VERSION
from timeframe import timeframe_seconds


PHASE19_VERSION = "v1.phase19"
SYMBOL_TV = "OANDA:XAUUSD"
REPORTS = Path("reports")
CANDIDATES_DIR = Path("strategy_candidates")
JOURNAL_PHASE19 = Path("journal") / "phase19_deep"
FROZEN_FINALIST_PATHS = (
    CANDIDATES_DIR / "phase18_c4_5m_first_touch.json",
    CANDIDATES_DIR / "phase18_c3_5m_ce.json",
    CANDIDATES_DIR / "phase18_c12_5m_htf_c.json",
)

LUXALGO_WARNING = (
    "Historical strategy statistics depend on internal_structure CHoCH v1. "
    "Live confirmation currently depends on LuxAlgo. "
    "Their equivalence remains unvalidated unless diagnostics say otherwise."
)

PHASE18_VERDICT_PRESERVED = {
    "phase18_verdict": "NEED_MORE_DATA",
    "phase18_production": "NO_PRODUCTION_RULESET_SELECTED",
    "note": "Phase 18 result preserved; Phase 19 does not invent new strategy logic.",
}


def _scrub(obj: Any) -> Any:
    secret_keys = {"token", "api_key", "apikey", "password", "secret", "tiingo_token"}
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            lk = str(k).lower()
            if any(s in lk for s in secret_keys):
                out[k] = "<redacted>" if not isinstance(v, (bool, type(None))) else v
            else:
                out[k] = _scrub(v)
        return out
    if isinstance(obj, list):
        return [_scrub(x) for x in obj]
    return obj


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return str(path)
    keys: list[str] = []
    seen = set()
    for r in rows:
        for k in r:
            if k not in seen:
                seen.add(k)
                keys.append(k)
    with path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=keys, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            flat = {
                k: (json.dumps(v, default=str) if isinstance(v, (dict, list)) else v)
                for k, v in r.items()
            }
            w.writerow(flat)
    return str(path)


def load_frozen_finalists() -> list[tuple[dict[str, Any], StrategyCandidate, str]]:
    """Load exact Phase 18 candidate configs; return (raw_json, candidate, path)."""
    out = []
    for path in FROZEN_FINALIST_PATHS:
        raw = json.loads(path.read_text(encoding="utf-8"))
        frozen = deepcopy(raw)
        c = frozen["candidate"]
        cand = StrategyCandidate(
            candidate_id=c["candidate_id"],
            execution_timeframe=c["execution_timeframe"],
            htf_policy=c["htf_policy"],
            entry_mode=c["entry_mode"],
            stop_mode=c["stop_mode"],
            confirmation_timeout_bars=c.get("confirmation_timeout_bars"),
            fvg_timeout_bars=c.get("fvg_timeout_bars"),
            retrace_timeout_bars=c.get("retrace_timeout_bars"),
            target_evaluation=c.get(
                "target_evaluation",
                "2R primary research target; opposite liquidity tracked",
            ),
            notes=c.get("notes") or "",
        )
        out.append((frozen, cand, str(path)))
    return out


def assert_candidate_unchanged(original: dict[str, Any], path: str) -> None:
    current = json.loads(Path(path).read_text(encoding="utf-8"))
    if current.get("candidate") != original.get("candidate"):
        raise AssertionError(f"frozen candidate mutated on disk: {path}")


def choose_split_fraction(n_dates: int) -> float:
    """
    Choose split for chronological evaluation size — not performance.
    Prefer 70/30; widen holdout fraction only if needed for sample size.
    """
    if n_dates >= 200:
        return 0.70
    if n_dates >= 120:
        return 0.65
    return 0.60


def load_bars_by_tf() -> dict[str, Any]:
    out = {}
    for tf in ("5m", "15m", "4H", "1D"):
        loaded = load_dataset("openbb_tiingo_XAUUSD", tf, root=TIINGO_ROOT)
        out[tf] = loaded.get("bars") or []
    return out


def ensure_phase19_journal(bars_by_tf: dict[str, list]) -> dict[str, Any]:
    path = JOURNAL_PHASE19 / "setups.jsonl"
    existing = load_journal_records(path=path) if path.exists() else []
    # Rebuild if bar history grew substantially vs prior journal coverage
    need_rebuild = len(existing) < 400
    if existing and not need_rebuild:
        return {
            "executed": True,
            "reused": True,
            "journal_path": str(path),
            "journal_size": len(existing),
        }

    result = replay_historical_mtf_setups(
        bars_by_tf,
        symbol=SYMBOL_TV,
        strategy_config=DEFAULT_STRATEGY_CONFIG,
        execution_timeframes=("5m", "15m"),
        bias_provider=StructureBiasProvider(),
    )
    from dataclasses import replace

    enriched = []
    for rec in result.journal_records:
        extras = dict(rec.extras or {})
        extras.update(
            {
                "data_provider": "openbb",
                "underlying_provider": "tiingo",
                "source_symbol": "XAUUSD",
                "feed_equivalence_class": "CLOSE_EQUIVALENT",
                "phase": "phase19",
            }
        )
        enriched.append(
            replace(rec, extras=extras, strategy_version=PHASE19_VERSION)
        )
    JOURNAL_PHASE19.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    written = append_journal_records(enriched, path=path)
    complete = result.coverage.complete_sessions if result.coverage else 0
    return {
        "executed": True,
        "reused": False,
        "journal_path": str(written),
        "journal_size": len(enriched),
        "complete_sessions": complete,
        "complete_sessions_sample_quality": sample_quality_label(complete),
    }


def evaluate_frozen_candidate(
    candidate: StrategyCandidate,
    rows: list[dict[str, Any]],
    resolutions: Optional[dict[tuple[str, str], str]] = None,
) -> dict[str, Any]:
    subset = [
        r
        for r in rows
        if (r.get("execution_timeframe") or r.get("timeframe")) == candidate.execution_timeframe
    ]
    subset = [r for r in subset if apply_htf_policy(r, candidate.htf_policy)]
    retained = []
    exp_counts: Counter[str] = Counter()
    for r in subset:
        tag = apply_expiry_policy(
            r,
            confirmation_timeout=candidate.confirmation_timeout_bars,
            fvg_timeout=candidate.fvg_timeout_bars,
            retrace_timeout=candidate.retrace_timeout_bars,
        )
        exp_counts[tag] += 1
        if tag == "RETAINED":
            retained.append(r)
    pairs = iter_entry_pairs(
        retained,
        entry_mode=candidate.entry_mode,
        execution_tf=candidate.execution_timeframe,
        resolutions=resolutions,
    )
    sc = scorecard_from_pairs(pairs, label=candidate.candidate_id)
    return {
        **sc,
        **candidate.to_dict(),
        "opportunities": len(retained),
        "expiry_counts": dict(exp_counts),
        "frozen": True,
    }


def walk_forward_blocks(rows: list[dict], n_blocks: int = 4) -> list[list[dict]]:
    dates = sorted({str(r.get("trading_date"))[:10] for r in rows if r.get("trading_date")})
    if len(dates) < n_blocks:
        return [rows]
    size = max(1, len(dates) // n_blocks)
    blocks = []
    for i in range(n_blocks):
        start = i * size
        end = (i + 1) * size if i < n_blocks - 1 else len(dates)
        dset = set(dates[start:end])
        blocks.append([r for r in rows if str(r.get("trading_date"))[:10] in dset])
    return blocks


def flag_regime_sensitivity(block_metrics: list[dict[str, Any]]) -> Optional[str]:
    """Mark REGIME_SENSITIVE if nearly all positive expectancy sits in one block."""
    usable = [b for b in block_metrics if (b.get("resolved_n") or 0) >= 10]
    if len(usable) < 2:
        return None
    pos = [b for b in usable if (b.get("theoretical_2r_expectancy") or -1) > 0]
    if len(pos) == 1 and len(usable) >= 3:
        return "REGIME_SENSITIVE"
    # single block dominates resolved N
    total = sum(b.get("resolved_n") or 0 for b in usable)
    if total > 0 and max(b.get("resolved_n") or 0 for b in usable) / total >= 0.7:
        return "REGIME_SENSITIVE"
    return None


def luxalgo_equivalence_report(bars_by_tf: dict[str, list]) -> dict[str, Any]:
    out: dict[str, Any] = {"by_timeframe": {}}
    for tf in ("5m", "15m"):
        # Prefer TV bars for Lux overlap when available; else Tiingo bars for internal only note
        tv = load_dataset(SYMBOL_TV, tf)
        bars = tv.get("bars") or bars_by_tf.get(tf) or []
        internal = detect_internal_choch(bars) if bars else []
        caps = load_luxalgo_captures(symbol=SYMBOL_TV, timeframe=tf)
        lux = captures_to_confirmations(caps)
        tol = tolerances_for_timeframe(tf)
        period = timeframe_seconds(tf) or (300 if tf == "5m" else 900)
        ov = compare_choch_overlap(
            internal,
            lux,
            time_tolerance_sec=int(tol["time_tolerance_sec"]),
            level_tolerance=float(tol["level_tolerance"]),
            max_bar_distance=int(tol["max_bar_distance"]),
            period_sec=int(period),
            timeframe=tf,
        )
        out["by_timeframe"][tf] = ov
    statuses = [v.get("equivalence_status") for v in out["by_timeframe"].values()]
    if any(s == "partially_validated" for s in statuses):
        overall = "partially_validated"
    else:
        overall = "unvalidated_against_luxalgo"
    out["equivalence_status"] = overall
    out["luxalgo_warning"] = LUXALGO_WARNING
    return out


def descriptive_evidence_update(rows: list[dict]) -> dict[str, Any]:
    from phase18_metrics import iter_entry_pairs, scorecard_from_pairs

    def filter_htf(rs, pol):
        return [r for r in rs if apply_htf_policy(r, pol)]

    tf5 = scorecard_from_pairs(iter_entry_pairs(rows, execution_tf="5m"), label="5m")
    tf15 = scorecard_from_pairs(iter_entry_pairs(rows, execution_tf="15m"), label="15m")
    entries = []
    for mode in ("first_touch", "boundary", "ce"):
        for tf in ("5m", "15m"):
            sc = scorecard_from_pairs(
                iter_entry_pairs(rows, entry_mode=mode, execution_tf=tf),
                label=f"{tf}:{mode}",
            )
            sc["entry_mode"] = mode
            sc["execution_timeframe"] = tf
            entries.append(sc)
    htf = []
    for pol in ("POLICY_A", "POLICY_B", "POLICY_C", "POLICY_D", "POLICY_E"):
        retained = filter_htf(rows, pol)
        sc = scorecard_from_pairs(iter_entry_pairs(retained), label=pol)
        sc["htf_policy"] = pol
        sc["retained_setups"] = len(retained)
        htf.append(sc)
    timing = {
        "sweep_to_choch": timing_distribution(
            [r["bars_sweep_to_choch"] for r in rows if r.get("bars_sweep_to_choch") is not None]
        ),
        "choch_to_fvg": timing_distribution(
            [r["bars_choch_to_fvg"] for r in rows if r.get("bars_choch_to_fvg") is not None]
        ),
    }
    return {
        "timeframe": {"5m": tf5, "15m": tf15},
        "entry_modes": entries,
        "htf_policies": htf,
        "timing": timing,
        "stop_default": "beyond_sweep",
        "notes": {
            "5m_vs_15m": "descriptive only; finalists frozen",
            "htf": "POLICY_A remains research default unless evidence clearly changes",
            "stop": "beyond_sweep remains research default",
            "expiry": "distributions updated; no timeout lock",
        },
    }


def phase19_verdict(
    *,
    holdout_results: list[dict[str, Any]],
    stability: dict[str, str],
    amb_report: dict[str, Any],
    lux: dict[str, Any],
    complete_sessions: int,
) -> str:
    # Preserve need-more-data path if holdout still tiny
    if all(stability.get(h["candidate_id"]) == "INSUFFICIENT_HOLDOUT_SAMPLE" for h in holdout_results):
        return "NEED_MORE_DATA"

    lux_status = lux.get("equivalence_status") or "unvalidated_against_luxalgo"
    still_amb = int(amb_report.get("still_ambiguous") or 0)
    before = int(amb_report.get("ambiguous_before_1m") or 0)
    high_residual_amb = before >= 10 and still_amb / max(before, 1) > 0.5

    stables = [cid for cid, st in stability.items() if st == "STABLE"]
    weaks = [cid for cid, st in stability.items() if st == "WEAKLY_STABLE"]

    adequate = [h for h in holdout_results if (h.get("resolved_n") or 0) >= 30]
    if adequate and all((h.get("theoretical_2r_expectancy") or 0) < 0 for h in adequate):
        # Meaningful holdout sample exists but no positive theoretical edge observed
        return "NO_EDGE_OBSERVED"

    if high_residual_amb and not stables:
        return "NEED_MORE_INTRABAR_RESOLUTION"

    if stables and lux_status != "unvalidated_against_luxalgo":
        return "READY_FOR_PAPER_VALIDATION"
    if stables and lux_status == "unvalidated_against_luxalgo":
        # Historical stability without LuxAlgo equivalence — do not paper yet
        return "NEED_CHOCH_VALIDATION"
    if len(stables) + len(weaks) >= 2:
        return "KEEP_MULTIPLE_CANDIDATES"
    if weaks:
        return "KEEP_MULTIPLE_CANDIDATES"
    return "NEED_MORE_DATA"


def run_phase19(
    *,
    write_artifacts: bool = True,
    target_months: int = 12,
    skip_extend: bool = False,
) -> dict[str, Any]:
    span0 = current_5m_span()
    extend_info: dict[str, Any] = {"skipped": skip_extend}
    if not skip_extend:
        latest = span0.get("latest")
        if target_months >= 18:
            target = default_eighteen_month_target_ts(latest_ts=latest)
        else:
            target = default_one_year_target_ts(latest_ts=latest)
        extend_info = extend_tiingo_5m_backward(target_earliest_ts=target, chunk_days=14)
        # If 12m worked and we want more, optionally push toward 18m when cheap
        after = extend_info.get("disk_after") or current_5m_span()
        if target_months >= 12 and after.get("ok"):
            # try a bit older only if we already have ~1y
            older = default_eighteen_month_target_ts(latest_ts=after.get("latest"))
            if older < int(after.get("earliest") or 0):
                extra = extend_tiingo_5m_backward(target_earliest_ts=older, chunk_days=14)
                extend_info["eighteen_month_attempt"] = {
                    k: extra.get(k)
                    for k in ("mode", "bars_fetched", "disk_after", "errors")
                }

    derived = rebuild_derived_timeframes()
    bars_by_tf = load_bars_by_tf()
    journal_meta = ensure_phase19_journal(bars_by_tf)
    rows = load_journal_records(path=Path(journal_meta["journal_path"]))

    dates = sorted({str(r.get("trading_date"))[:10] for r in rows if r.get("trading_date")})
    frac = choose_split_fraction(len(dates))
    train_rows, holdout_rows, split = chronological_split(rows, train_fraction=frac)
    assert_no_split_leakage(split)
    overlap = set(split.train_liquidity_event_ids) & set(split.holdout_liquidity_event_ids)

    frozen_bundle = load_frozen_finalists()
    # Snapshot originals for mutation guard
    originals = [(deepcopy(raw), path) for raw, _, path in frozen_bundle]

    # --- 1m ambiguity on full journal (evidence quality), metrics applied carefully ---
    amb_windows = identify_5m_ambiguous_windows(rows)
    fetch1 = fetch_1m_windows([w["parent_bar_time"] for w in amb_windows], persist=True)
    amb_report = resolve_5m_with_1m(amb_windows, fetch1.get("bars") or [])
    resolutions: dict[tuple[str, str], str] = {}
    for row in amb_report.get("rows") or []:
        sid, mode, result = row.get("setup_id"), row.get("entry_mode"), row.get("result")
        if sid and mode and result:
            resolutions[(str(sid), str(mode))] = str(result)

    # Evaluate frozen finalists — TRAIN then HOLDOUT once; no retune
    train_metrics = []
    holdout_metrics = []
    stability: dict[str, str] = {}
    for raw, cand, path in frozen_bundle:
        assert_candidate_unchanged(raw, path)
        tr = evaluate_frozen_candidate(cand, train_rows, resolutions)
        ho = evaluate_frozen_candidate(cand, holdout_rows, resolutions)
        st = classify_stability(tr, ho)
        # Raise bar note for preferred N
        if (ho.get("resolved_n") or 0) < 20:
            st = "INSUFFICIENT_HOLDOUT_SAMPLE"
        stability[cand.candidate_id] = st
        train_metrics.append(tr)
        holdout_metrics.append(
            {
                **ho,
                "stability": st,
                "train_resolved_n": tr.get("resolved_n"),
                "train_theoretical_2r_expectancy": tr.get("theoretical_2r_expectancy"),
                "train_r1_rate": tr.get("r1_rate"),
                "train_ambiguity_pct": tr.get("ambiguity_pct"),
                "delta_2r_expectancy": (
                    None
                    if tr.get("theoretical_2r_expectancy") is None
                    or ho.get("theoretical_2r_expectancy") is None
                    else ho["theoretical_2r_expectancy"] - tr["theoretical_2r_expectancy"]
                ),
                "preferred_holdout_n_ge_30": (ho.get("resolved_n") or 0) >= 30,
                "strong_holdout_n_ge_50": (ho.get("resolved_n") or 0) >= 50,
            }
        )
        assert_candidate_unchanged(raw, path)

    # Walk-forward (no retune)
    blocks = walk_forward_blocks(rows, 4)
    wf_rows = []
    regime_flags = {}
    for raw, cand, path in frozen_bundle:
        block_metrics = []
        for i, block in enumerate(blocks):
            sc = evaluate_frozen_candidate(cand, block, resolutions)
            bm = {
                "candidate_id": cand.candidate_id,
                "block": i + 1,
                "resolved_n": sc.get("resolved_n"),
                "stop_rate": sc.get("stop_rate"),
                "r1_rate": sc.get("r1_rate"),
                "r2_rate": sc.get("r2_rate"),
                "r3_rate": sc.get("r3_rate"),
                "theoretical_2r_expectancy": sc.get("theoretical_2r_expectancy"),
                "median_mfe_r": sc.get("median_mfe_r"),
                "median_mae_r": sc.get("median_mae_r"),
                "ambiguity_pct": sc.get("ambiguity_pct"),
            }
            block_metrics.append(bm)
            wf_rows.append(bm)
        regime_flags[cand.candidate_id] = flag_regime_sensitivity(block_metrics)
        assert_candidate_unchanged(raw, path)

    lux = luxalgo_equivalence_report(bars_by_tf)
    evidence = descriptive_evidence_update(train_rows)

    complete_sessions = journal_meta.get("complete_sessions")
    if complete_sessions is None:
        complete_sessions = len({(r.get("session"), r.get("trading_date")) for r in rows})

    verdict = phase19_verdict(
        holdout_results=holdout_metrics,
        stability=stability,
        amb_report=amb_report,
        lux=lux,
        complete_sessions=int(complete_sessions or 0),
    )

    paper_path = None
    selected = None
    if verdict == "READY_FOR_PAPER_VALIDATION":
        # pick best stable by holdout resolved N then e2
        stables = [h for h in holdout_metrics if h.get("stability") == "STABLE"]
        if stables:
            stables.sort(
                key=lambda h: (
                    h.get("resolved_n") or 0,
                    h.get("theoretical_2r_expectancy") or -999,
                ),
                reverse=True,
            )
            selected = next(
                c for _, c, _ in frozen_bundle if c.candidate_id == stables[0]["candidate_id"]
            )
            payload = {
                "phase": "phase19",
                "strategy_version": PHASE19_VERSION,
                "frozen_from": "phase18",
                "candidate": selected.to_dict(),
                "provenance": {
                    "data_provider": "openbb",
                    "underlying_provider": "tiingo",
                    "source_symbol": "XAUUSD",
                    "feed_equivalence_class": "CLOSE_EQUIVALENT",
                },
                "confirmation_equivalence_status": lux.get("equivalence_status"),
                "luxalgo_warning": LUXALGO_WARNING,
                "note": "Paper/observation only — NOT DEFAULT_STRATEGY_CONFIG",
            }
            paper_path = str(CANDIDATES_DIR / "phase19_paper_candidate.json")
            if write_artifacts:
                Path(paper_path).write_text(json.dumps(payload, indent=2), encoding="utf-8")

    # Final mutation check
    for raw, path in originals:
        assert_candidate_unchanged(raw, path)

    span = current_5m_span()
    artifacts = {}
    if write_artifacts:
        REPORTS.mkdir(parents=True, exist_ok=True)
        artifacts["phase19_dataset"] = _write_csv(
            REPORTS / "phase19_dataset.csv",
            [
                {
                    "bars_5m": span.get("bar_count"),
                    "bars_15m": len(bars_by_tf.get("15m") or []),
                    "bars_4H": len(bars_by_tf.get("4H") or []),
                    "bars_1D": len(bars_by_tf.get("1D") or []),
                    "complete_sessions": complete_sessions,
                    "journal_rows": len(rows),
                    "train_fraction": frac,
                    "earliest": span.get("earliest"),
                    "latest": span.get("latest"),
                }
            ],
        )
        artifacts["phase19_holdout"] = _write_csv(REPORTS / "phase19_holdout.csv", holdout_metrics)
        artifacts["phase19_walkforward"] = _write_csv(REPORTS / "phase19_walkforward.csv", wf_rows)
        artifacts["phase19_ambiguity_resolution"] = _write_csv(
            REPORTS / "phase19_ambiguity_resolution.csv",
            amb_report.get("rows") or [{"summary": True, **{k: v for k, v in amb_report.items() if k != "rows"}}],
        )
        lux_rows = []
        for tf, block in (lux.get("by_timeframe") or {}).items():
            lux_rows.append(
                {
                    "timeframe": tf,
                    "luxalgo_reliable": block.get("luxalgo_reliable_count"),
                    "internal": block.get("internal_count"),
                    "full_matches": block.get("matched_count"),
                    "luxalgo_only": block.get("luxalgo_only_count"),
                    "internal_only": block.get("internal_only_count"),
                    "equivalence_status": block.get("equivalence_status"),
                }
            )
        artifacts["phase19_luxalgo_overlap"] = _write_csv(
            REPORTS / "phase19_luxalgo_overlap.csv", lux_rows
        )
        artifacts["phase19_finalists"] = _write_csv(
            REPORTS / "phase19_finalists.csv",
            [{**t, "split": "TRAIN"} for t in train_metrics]
            + [{**h, "split": "HOLDOUT"} for h in holdout_metrics],
        )

    report = {
        "ok": True,
        "phase": 19,
        "strategy_version": PHASE19_VERSION,
        "baseline_strategy_version": STRATEGY_VERSION,
        "phase18_preserved": PHASE18_VERDICT_PRESERVED,
        "provenance": {
            "data_provider": "openbb",
            "underlying_provider": "tiingo",
            "source_symbol": "XAUUSD",
            "feed_equivalence_class": "CLOSE_EQUIVALENT",
            "live_benchmark": SYMBOL_TV,
        },
        "history_extension": extend_info,
        "derived": derived,
        "dataset": {
            "bars_5m": span.get("bar_count"),
            "bars_15m": len(bars_by_tf.get("15m") or []),
            "bars_4H": len(bars_by_tf.get("4H") or []),
            "bars_1D": len(bars_by_tf.get("1D") or []),
            "earliest": span.get("earliest"),
            "latest": span.get("latest"),
            "earliest_iso": datetime.fromtimestamp(span["earliest"], tz=timezone.utc).isoformat()
            if span.get("earliest")
            else None,
            "latest_iso": datetime.fromtimestamp(span["latest"], tz=timezone.utc).isoformat()
            if span.get("latest")
            else None,
            "complete_sessions": complete_sessions,
            "journal": journal_meta,
            "journal_rows": len(rows),
        },
        "split": {
            **split.to_dict(),
            "chosen_train_fraction": frac,
            "liquidity_event_overlap_count": len(overlap),
            "liquidity_event_overlap_ok": len(overlap) == 0,
        },
        "ambiguity_1m": {
            **{k: v for k, v in amb_report.items() if k != "rows"},
            "fetch": {k: v for k, v in fetch1.items() if k != "bars"},
        },
        "luxalgo": lux,
        "finalists": {
            "frozen_paths": [p for _, _, p in frozen_bundle],
            "train": train_metrics,
            "holdout": holdout_metrics,
            "stability": stability,
            "walk_forward": wf_rows,
            "regime_sensitivity": regime_flags,
        },
        "updated_evidence": evidence,
        "recommendation": {
            "verdict": verdict,
            "selected_candidate": selected.to_dict() if selected else None,
            "paper_candidate_json": paper_path,
            "production_default_unchanged": True,
        },
        "artifacts": artifacts,
        "limitations": [
            LUXALGO_WARNING,
            f"CHoCH equivalence_status={lux.get('equivalence_status')}",
            "Frozen Phase 18 finalists were not retuned on expanded data",
            "Theoretical expectancy is not realized PnL",
            "1m does not invent tick order inside a single 1m candle",
        ],
    }

    if write_artifacts:
        Path("phase19_validation.json").write_text(
            json.dumps(_scrub(report), indent=2, default=str),
            encoding="utf-8",
        )
        report["artifacts"]["phase19_validation"] = "phase19_validation.json"
    return _scrub(report)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-artifacts", action="store_true")
    ap.add_argument("--skip-extend", action="store_true")
    ap.add_argument("--months", type=int, default=12)
    args = ap.parse_args()
    report = run_phase19(
        write_artifacts=not args.no_artifacts,
        target_months=args.months,
        skip_extend=args.skip_extend,
    )
    print(
        json.dumps(
            {
                "ok": report.get("ok"),
                "verdict": (report.get("recommendation") or {}).get("verdict"),
                "bars_5m": (report.get("dataset") or {}).get("bars_5m"),
                "complete_sessions": (report.get("dataset") or {}).get("complete_sessions"),
                "split": {
                    k: (report.get("split") or {}).get(k)
                    for k in (
                        "train_start",
                        "train_end",
                        "holdout_start",
                        "holdout_end",
                        "chosen_train_fraction",
                        "liquidity_event_overlap_ok",
                    )
                },
                "stability": (report.get("finalists") or {}).get("stability"),
                "luxalgo": (report.get("luxalgo") or {}).get("equivalence_status"),
            },
            indent=2,
            default=str,
        )
    )


if __name__ == "__main__":
    main()
