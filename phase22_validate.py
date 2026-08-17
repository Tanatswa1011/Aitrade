"""Phase 22 — GC ORB + volume validation (isolated futures family)."""

from __future__ import annotations

import csv
import json
import statistics
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, Sequence

from bar_dataset import load_dataset
from gc_orb_engine import build_opening_range, trading_dates_in_bars
from gc_orb_models import (
    DISPLACEMENT_BODY_OR_RATIO,
    PHASE22_CANDIDATES,
    STRATEGY_FAMILY,
    STRATEGY_VERSION,
    VOLUME_RVOL_THRESHOLD,
    GCORBStrategyConfig,
)
from gc_orb_replay import collect_or30_events, replay_all_candidates
from phase18_eligibility import ELIG_AMBIGUOUS, ELIG_EXPIRED, ELIG_INVALID, ELIG_RESOLVED
from phase18_metrics import (
    iter_entry_pairs,
    median_or_none,
    progressive_rr_hit,
    safe_rate,
    scorecard_from_pairs,
    theoretical_fixed_target_expectancy,
)
from setup_journal import append_journal_records, load_journal_records

DATA_ROOT = Path("data") / "openbb" / "yfinance"
JOURNAL_DIR = Path("journal") / "phase22_gc_orb"
REPORTS = Path("reports")
VALIDATION_JSON = Path("phase22_validation.json")
CANDIDATES_DIR = Path("strategy_candidates")
MIN_TRAIN_N = 30


def load_gc_bars() -> list:
    loaded = load_dataset("openbb_yfinance_GC", "5m", root=DATA_ROOT)
    return list(loaded.get("bars") or [])


def filter_dates(rows: list[dict], start: str, end: str) -> list[dict]:
    out = []
    for r in rows:
        td = str(r.get("trading_date") or "")[:10]
        if td and start <= td <= end:
            out.append(r)
    return out


def chronological_date_split(rows: list[dict], train_fraction: float = 0.70) -> tuple[list, list, dict]:
    dates = sorted({str(r.get("trading_date"))[:10] for r in rows if r.get("trading_date")})
    if len(dates) < 4:
        cut = max(1, len(dates) // 2)
    else:
        cut = max(1, min(len(dates) - 1, int(round(len(dates) * train_fraction))))
    train_d, hold_d = set(dates[:cut]), set(dates[cut:])
    train = [r for r in rows if str(r.get("trading_date"))[:10] in train_d]
    hold = [r for r in rows if str(r.get("trading_date"))[:10] in hold_d]
    meta = {
        "train_start": dates[0] if dates else None,
        "train_end": dates[cut - 1] if dates else None,
        "holdout_start": dates[cut] if cut < len(dates) else None,
        "holdout_end": dates[-1] if dates else None,
        "train_dates": len(train_d),
        "holdout_dates": len(hold_d),
        "train_fraction": train_fraction,
        "method": "chronological_trading_date_70_30",
    }
    return train, hold, meta


def evaluate_rows(rows: list[dict], cfg: GCORBStrategyConfig) -> dict[str, Any]:
    subset = [r for r in rows if (r.get("extras") or {}).get("candidate_id") == cfg.candidate_id]
    # Exclude roll artifacts from canonical performance
    canonical = [r for r in subset if "ROLL_ARTIFACT" not in (r.get("reliability_flags") or [])]
    pairs = iter_entry_pairs(canonical, entry_mode=cfg.entry_mode)
    sc = scorecard_from_pairs(pairs, label=cfg.candidate_id)
    # 1.5R if present in targets
    resolved = [p for p in pairs if p["eligibility"] == ELIG_RESOLVED]
    r15 = 0
    for p in resolved:
        e = p["entry"]
        targets = e.get("fixed_rr_targets") if isinstance(e, dict) else e.fixed_rr_targets
        # progressive via event timestamps if 1.5 stored — approximate via mfe
        mfe = e.get("mfe_r") if isinstance(e, dict) else e.mfe_r
        if mfe is not None and float(mfe) >= 1.5:
            r15 += 1
    rn = len(resolved)
    stop_n = sc.get("stop_n") or 0
    sc["r15_n"] = r15
    sc["r15_rate"] = safe_rate(r15, rn)
    sc["theoretical_1_5r_expectancy"] = theoretical_fixed_target_expectancy(
        target_r=1.5, target_hits=r15, stop_hits=stop_n, resolved_n=rn
    )
    funnel = {
        "rows": len(subset),
        "canonical_rows": len(canonical),
        "roll_artifacts": sum(1 for r in subset if "ROLL_ARTIFACT" in (r.get("reliability_flags") or [])),
        "triggered": sc.get("triggered_n"),
        "resolved": sc.get("resolved_n"),
        "ambiguous": sc.get("ambiguous_n"),
        "expired": sc.get("expired_n"),
        "invalid": sc.get("invalid_n"),
    }
    return {**sc, **cfg.to_dict(), "funnel": funnel}


def select_finalists(train_metrics: list[dict]) -> list[dict]:
    eligible = [m for m in train_metrics if (m.get("resolved_n") or 0) >= MIN_TRAIN_N]
    if not eligible:
        eligible = sorted(train_metrics, key=lambda m: m.get("resolved_n") or 0, reverse=True)[:3]
        for m in eligible:
            m["selection_note"] = "below_min_train_n"
        return eligible[:3]

    def key(m):
        e2 = m.get("theoretical_2r_expectancy")
        e2 = float(e2) if e2 is not None else -999
        n = m.get("resolved_n") or 0
        stop = float(m.get("stop_rate") or 1)
        amb = m.get("ambiguous_n") or 0
        return (e2 > 0, e2, n, -stop, -amb)

    ranked = sorted(eligible, key=key, reverse=True)
    out = ranked[:3]
    for m in out:
        m["selection_note"] = "train_rank_e2r_n_stop_amb"
    return out


def classify_stability(train: dict, hold: dict) -> str:
    hn = hold.get("resolved_n") or 0
    if hn < 15:
        return "INSUFFICIENT_SAMPLE"
    te = train.get("theoretical_2r_expectancy")
    he = hold.get("theoretical_2r_expectancy")
    if te is None or he is None:
        return "UNSTABLE"
    te, he = float(te), float(he)
    if te > 0 and he > 0:
        return "STABLE_POSITIVE"
    if te <= 0 and he <= 0:
        return "STABLE_NEGATIVE"
    if te > 0 and he <= 0:
        return "UNSTABLE"
    return "WEAK_POSITIVE"


def rvol_bucket(rvol: Optional[float]) -> str:
    if rvol is None:
        return "unknown"
    if rvol < 1.0:
        return "<1"
    if rvol < 1.25:
        return "1-1.25"
    if rvol < 1.5:
        return "1.25-1.5"
    if rvol < 2.0:
        return "1.5-2"
    return ">2"


def body_bucket(ratio: Optional[float]) -> str:
    if ratio is None:
        return "unknown"
    if ratio < 0.25:
        return "<0.25"
    if ratio < 0.5:
        return "0.25-0.50"
    if ratio < 0.75:
        return "0.50-0.75"
    if ratio < 1.0:
        return "0.75-1.0"
    return ">1.0"


def bucket_outcomes(rows: list[dict], key_fn) -> list[dict]:
    groups: dict[str, list] = {}
    for r in rows:
        if "ROLL_ARTIFACT" in (r.get("reliability_flags") or []):
            continue
        ex = r.get("extras") or {}
        k = key_fn(ex)
        groups.setdefault(k, []).append(r)
    out = []
    for k, subset in sorted(groups.items()):
        pairs = iter_entry_pairs(subset)
        sc = scorecard_from_pairs(pairs, label=k)
        out.append(
            {
                "bucket": k,
                "n_rows": len(subset),
                "resolved_n": sc.get("resolved_n"),
                "stop_rate": sc.get("stop_rate"),
                "r1_rate": sc.get("r1_rate"),
                "r2_rate": sc.get("r2_rate"),
                "r3_rate": sc.get("r3_rate"),
                "theoretical_2r_expectancy": sc.get("theoretical_2r_expectancy"),
                "median_mfe_r": sc.get("median_mfe_r"),
                "median_mae_r": sc.get("median_mae_r"),
            }
        )
    return out


def decide_verdict(holdouts, stability, *, bar_count: int, days: int) -> str:
    # Hard floor: yfinance ~60d is below preferred 6–12m research depth.
    if days < 90:
        return "DATA_SOURCE_UNSUITABLE"
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
    weak_pos = [
        h
        for h in holdouts
        if (h.get("theoretical_2r_expectancy") or -1) > 0
        and (h.get("resolved_n") or 0) >= 15
        and stability.get(h["candidate_id"]) in ("STABLE_POSITIVE", "WEAK_POSITIVE")
    ]
    if any(stability.get(h["candidate_id"]) == "STABLE_POSITIVE" for h in holdouts) and pos and days >= 180:
        return "EDGE_OBSERVED"
    if weak_pos and days >= 120:
        return "WEAK_EDGE_OBSERVED"
    meaningful = [h for h in holdouts if (h.get("resolved_n") or 0) >= 20]
    if meaningful and all((h.get("theoretical_2r_expectancy") or 0) <= 0 for h in meaningful):
        return "NO_EDGE_OBSERVED"
    return "INSUFFICIENT_SAMPLE"


def volume_conclusion(vol_buckets: list[dict], lift: dict) -> str:
    usable = [b for b in vol_buckets if (b.get("resolved_n") or 0) >= 8]
    if len(usable) < 2:
        return "INSUFFICIENT_SAMPLE"
    # compare high RVOL (>1.5) vs low (<1.25)
    high = [b for b in usable if b["bucket"] in ("1.5-2", ">2")]
    low = [b for b in usable if b["bucket"] in ("<1", "1-1.25")]
    if not high or not low:
        return "INSUFFICIENT_SAMPLE"
    he = statistics.mean([b.get("theoretical_2r_expectancy") or 0 for b in high])
    le = statistics.mean([b.get("theoretical_2r_expectancy") or 0 for b in low])
    delta = lift.get("e2r_delta")
    if he > le + 0.05 and (delta is None or delta > 0):
        return "YES_PRELIMINARY"
    if abs(he - le) < 0.05:
        return "MIXED"
    if he < le - 0.05:
        return "NO"
    return "MIXED"


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
            w.writerow(
                {
                    k: json.dumps(v, default=str) if isinstance(v, (dict, list)) else v
                    for k, v in {k: r.get(k) for k in keys}.items()
                }
            )


def freeze_candidate(cfg, split_meta, train_m, hold_m) -> Path:
    CANDIDATES_DIR.mkdir(parents=True, exist_ok=True)
    path = CANDIDATES_DIR / f"phase22_gc_{cfg.candidate_id}.json"
    payload = {
        "phase": "phase22",
        "strategy_family": STRATEGY_FAMILY,
        "strategy_version": STRATEGY_VERSION,
        "instrument": "GC",
        "provider": "openbb:yfinance",
        "candidate": cfg.to_dict(),
        "predeclared": {
            "rvol_threshold": VOLUME_RVOL_THRESHOLD,
            "displacement_body_or_ratio": DISPLACEMENT_BODY_OR_RATIO,
            "or_anchor": "08:20 America/New_York",
        },
        "selection_split": split_meta,
        "train_snapshot": {
            "resolved_n": train_m.get("resolved_n"),
            "theoretical_2r_expectancy": train_m.get("theoretical_2r_expectancy"),
        },
        "holdout_snapshot": {
            "resolved_n": hold_m.get("resolved_n"),
            "theoretical_2r_expectancy": hold_m.get("theoretical_2r_expectancy"),
        },
        "note": "NOT production default — Phase 22 research only",
    }
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    return path


def run_phase22(*, force_replay: bool = False) -> dict[str, Any]:
    REPORTS.mkdir(parents=True, exist_ok=True)
    JOURNAL_DIR.mkdir(parents=True, exist_ok=True)
    bars = load_gc_bars()
    meta_path = DATA_ROOT / "openbb_yfinance_GC_5m.meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}

    opening_ranges, events, roll_flags = collect_or30_events(bars)
    complete_ors = [o for o in opening_ranges if o.complete]
    # OR15/OR60 descriptive counts
    dates = trading_dates_in_bars(bars)
    or15 = sum(1 for d in dates if build_opening_range(bars, d, or_minutes=15).complete)
    or60 = sum(1 for d in dates if build_opening_range(bars, d, or_minutes=60).complete)

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
    train_rows, hold_rows, split_meta = chronological_date_split(rows, 0.70)

    train_metrics = [evaluate_rows(train_rows, cfg) for cfg in PHASE22_CANDIDATES]
    finalists = select_finalists(train_metrics)
    finalist_cfgs = []
    for f in finalists:
        for cfg in PHASE22_CANDIDATES:
            if cfg.candidate_id == f["candidate_id"]:
                finalist_cfgs.append(cfg)
                break
    hold_metrics = [evaluate_rows(hold_rows, cfg) for cfg in finalist_cfgs]
    stability = {t["candidate_id"]: classify_stability(t, h) for t, h in zip(finalists, hold_metrics)}

    # Volume / displacement analysis on G1 baseline population (all breakouts, breakout-close)
    g1_rows = [r for r in rows if (r.get("extras") or {}).get("candidate_id") == "G1_OR30_bo_volOFF_dispOFF"]
    g2_rows = [r for r in rows if (r.get("extras") or {}).get("candidate_id") == "G2_OR30_bo_volON_dispOFF"]
    vol_buckets = bucket_outcomes(g1_rows, lambda ex: rvol_bucket(ex.get("rvol")))
    disp_buckets = bucket_outcomes(g1_rows, lambda ex: body_bucket(ex.get("body_or_ratio")))

    g1_sc = scorecard_from_pairs(iter_entry_pairs([r for r in g1_rows if "ROLL_ARTIFACT" not in (r.get("reliability_flags") or [])]))
    g2_sc = scorecard_from_pairs(iter_entry_pairs([r for r in g2_rows if "ROLL_ARTIFACT" not in (r.get("reliability_flags") or [])]))
    lift = {
        "g1_resolved": g1_sc.get("resolved_n"),
        "g2_resolved": g2_sc.get("resolved_n"),
        "opportunity_reduction": None
        if not g1_sc.get("triggered_n")
        else 1 - (g2_sc.get("triggered_n") or 0) / max(g1_sc.get("triggered_n"), 1),
        "stop_rate_delta": None
        if g1_sc.get("stop_rate") is None or g2_sc.get("stop_rate") is None
        else (g2_sc["stop_rate"] - g1_sc["stop_rate"]),
        "r1_delta": None
        if g1_sc.get("r1_rate") is None or g2_sc.get("r1_rate") is None
        else (g2_sc["r1_rate"] - g1_sc["r1_rate"]),
        "r2_delta": None
        if g1_sc.get("r2_rate") is None or g2_sc.get("r2_rate") is None
        else (g2_sc["r2_rate"] - g1_sc["r2_rate"]),
        "r3_delta": None
        if g1_sc.get("r3_rate") is None or g2_sc.get("r3_rate") is None
        else (g2_sc["r3_rate"] - g1_sc["r3_rate"]),
        "e2r_delta": None
        if g1_sc.get("theoretical_2r_expectancy") is None or g2_sc.get("theoretical_2r_expectancy") is None
        else (g2_sc["theoretical_2r_expectancy"] - g1_sc["theoretical_2r_expectancy"]),
        "mfe_delta": None
        if g1_sc.get("median_mfe_r") is None or g2_sc.get("median_mfe_r") is None
        else (g2_sc["median_mfe_r"] - g1_sc["median_mfe_r"]),
        "mae_delta": None
        if g1_sc.get("median_mae_r") is None or g2_sc.get("median_mae_r") is None
        else (g2_sc["median_mae_r"] - g1_sc["median_mae_r"]),
    }
    vol_answer = volume_conclusion(vol_buckets, lift)

    # Retest comparison G1 vs G5
    g5_rows = [r for r in rows if (r.get("extras") or {}).get("candidate_id") == "G5_OR30_rt_volOFF_dispOFF"]
    g5_sc = scorecard_from_pairs(iter_entry_pairs([r for r in g5_rows if "ROLL_ARTIFACT" not in (r.get("reliability_flags") or [])]))
    retest_report = {
        "breakouts": len([e for e in events if not e.roll_artifact]),
        "retests_g5_triggered": g5_sc.get("triggered_n"),
        "breakout_close": {
            "resolved_n": g1_sc.get("resolved_n"),
            "stop_rate": g1_sc.get("stop_rate"),
            "r2_rate": g1_sc.get("r2_rate"),
            "e2r": g1_sc.get("theoretical_2r_expectancy"),
        },
        "retest_close": {
            "resolved_n": g5_sc.get("resolved_n"),
            "stop_rate": g5_sc.get("stop_rate"),
            "r2_rate": g5_sc.get("r2_rate"),
            "e2r": g5_sc.get("theoretical_2r_expectancy"),
        },
    }

    # Walk-forward
    dates_all = sorted({str(r.get("trading_date"))[:10] for r in rows if r.get("trading_date")})
    wf = []
    regime = {}
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
            m = evaluate_rows(br, cfg)
            row = {
                "candidate_id": cfg.candidate_id,
                "block": i + 1,
                "resolved_n": m.get("resolved_n"),
                "e1r": m.get("theoretical_1r_expectancy"),
                "e2r": m.get("theoretical_2r_expectancy"),
                "e3r": m.get("theoretical_3r_expectancy"),
                "stop_rate": m.get("stop_rate"),
                "median_mfe_r": m.get("median_mfe_r"),
                "median_mae_r": m.get("median_mae_r"),
            }
            wf.append(row)
            block_metrics.append(row)
        pos = [b for b in block_metrics if (b.get("e2r") or -1) > 0 and (b.get("resolved_n") or 0) >= 5]
        usable = [b for b in block_metrics if (b.get("resolved_n") or 0) >= 5]
        regime[cfg.candidate_id] = len(pos) == 1 and len(usable) >= 3

    # Cost sensitivity on best holdout (illustrative 0/1/2 ticks; GC tick=$0.10 → $10/contract; use price ticks as R fraction)
    cost_sens = []
    for h in hold_metrics:
        e2 = h.get("theoretical_2r_expectancy")
        rd = h.get("median_risk_distance") or 5.0
        for ticks in (0, 1, 2):
            # round-trip friction in R: (2 sides * ticks * 0.1) / risk_distance
            friction_r = (2 * ticks * 0.1) / max(float(rd), 0.1)
            adj = None if e2 is None else float(e2) - friction_r
            cost_sens.append(
                {
                    "candidate_id": h.get("candidate_id"),
                    "ticks_per_side": ticks,
                    "friction_r": friction_r,
                    "e2r_raw": e2,
                    "e2r_after_friction": adj,
                }
            )

    bull = sum(1 for e in events if e.side == "bullish" and not e.roll_artifact)
    bear = sum(1 for e in events if e.side == "bearish" and not e.roll_artifact)
    vol_qual = sum(1 for e in events if e.volume_ok and not e.roll_artifact)
    disp_qual = sum(1 for e in events if e.displacement_ok and not e.roll_artifact)
    roll_n = sum(1 for e in events if e.roll_artifact)

    or_sizes = [o.range_size for o in complete_ors if o.range_size > 0]
    or_size_dist = {
        "n": len(or_sizes),
        "median": median_or_none(or_sizes),
        "p25": None if not or_sizes else sorted(or_sizes)[len(or_sizes) // 4],
        "p75": None if not or_sizes else sorted(or_sizes)[(3 * len(or_sizes)) // 4],
        "min": min(or_sizes) if or_sizes else None,
        "max": max(or_sizes) if or_sizes else None,
    }

    days = len(dates)
    verdict = decide_verdict(hold_metrics, stability, bar_count=len(bars), days=days)

    frozen = []
    for cfg, tm, hm in zip(finalist_cfgs, finalists, hold_metrics):
        frozen.append(str(freeze_candidate(cfg, split_meta, tm, hm)))

    provider_row = [{
        "provider": "openbb",
        "underlying": "yfinance",
        "symbol": "GC",
        "yahoo_alias": "GC=F",
        "exchange": meta.get("exchange") or "CMX",
        "continuous": True,
        "contract_note": meta.get("front_month_note"),
        "volume_field": meta.get("volume_field"),
        "bars": len(bars),
        "volume_completeness": 1.0,
        "period_start": meta.get("earliest_bar") or meta.get("actual_start"),
        "period_end": meta.get("latest_bar") or meta.get("actual_end"),
    }]
    _write_csv(REPORTS / "phase22_provider.csv", provider_row)
    _write_csv(
        REPORTS / "phase22_contracts.csv",
        [{
            "representation": "Yahoo continuous front-month GC=F",
            "roll_methodology": "provider_controlled_yahoo_front_month",
            "aitrade_stitched": False,
            "roll_gap_flags": len(roll_flags),
        }],
    )
    _write_csv(REPORTS / "phase22_funnel.csv", [
        {"candidate_id": m["candidate_id"], **(m.get("funnel") or {}), "resolved_n": m.get("resolved_n")}
        for m in train_metrics
    ])
    _write_csv(REPORTS / "phase22_volume.csv", vol_buckets)
    _write_csv(REPORTS / "phase22_displacement.csv", disp_buckets)
    _write_csv(REPORTS / "phase22_retests.csv", [retest_report])
    _write_csv(REPORTS / "phase22_train.csv", finalists)
    _write_csv(REPORTS / "phase22_holdout.csv", hold_metrics)
    _write_csv(REPORTS / "phase22_walkforward.csv", wf)
    _write_csv(
        REPORTS / "phase22_roll_artifacts.csv",
        [{"roll_flag_ts": t, "iso": datetime.fromtimestamp(t, tz=timezone.utc).isoformat()} for t in sorted(roll_flags)],
    )

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

    payload = {
        "ok": True,
        "phase": 22,
        "strategy_family": STRATEGY_FAMILY,
        "strategy_version": STRATEGY_VERSION,
        "prior_families_untouched": [
            "session_sweep_choch_fvg",
            "liquidity_reclaim_v1",
        ],
        "generated_at": datetime.now(tz=timezone.utc).isoformat(),
        "futures_data": {
            "provider": "openbb:yfinance",
            "symbol": "GC",
            "yahoo_alias": "GC=F",
            "continuous": True,
            "exchange": "CMX",
            "roll_methodology": "provider_controlled_yahoo_front_month",
            "bars_5m": len(bars),
            "trading_days": days,
            "volume_completeness": 1.0,
            "volume_zeros": sum(1 for b in bars if b.volume == 0),
            "limitation": "yfinance 5m futures history ~60 days; below preferred 6–12 months",
            "meta": {k: meta.get(k) for k in (
                "front_month_note", "volume_caveat", "timezone", "earliest_bar", "latest_bar"
            )},
        },
        "opening_range": {
            "anchor_timezone": "America/New_York",
            "anchor_local": "08:20",
            "anchor_note": "US morning gold activity research anchor; not exclusive Globex open",
            "or15_complete": or15,
            "or30_complete": len(complete_ors),
            "or60_complete": or60,
            "or_size_distribution": or_size_dist,
        },
        "funnel": {
            "or30_valid_days": len(complete_ors),
            "bullish_breakouts": bull,
            "bearish_breakouts": bear,
            "volume_qualified": vol_qual,
            "displacement_qualified": disp_qual,
            "roll_artifacts": roll_n,
            "retest_report": retest_report,
        },
        "volume": {
            "threshold_predeclared": VOLUME_RVOL_THRESHOLD,
            "buckets": vol_buckets,
            "lift_g1_vs_g2": lift,
            "conclusion": vol_answer,
        },
        "displacement": {
            "threshold_predeclared": DISPLACEMENT_BODY_OR_RATIO,
            "buckets": disp_buckets,
        },
        "split": split_meta,
        "train_metrics": train_metrics,
        "finalists": finalists,
        "holdout_metrics": hold_metrics,
        "stability": stability,
        "regime_sensitive": regime,
        "walkforward": wf,
        "cost_sensitivity": cost_sens,
        "verdict": verdict,
        "best_candidate": None if not best else best.get("candidate_id"),
        "frozen_paths": frozen,
        "paper_validation_justified": verdict in ("EDGE_OBSERVED", "WEAK_EDGE_OBSERVED"),
        "replay": replay_meta,
        "break_even": {"1R": 0.5, "1.5R": 0.4, "2R": 1 / 3, "3R": 0.25},
        "limitations": [
            "Only ~60 days of yfinance GC 5m history available",
            "Continuous front-month GC=F; provider-controlled rolls",
            "Volume is Yahoo-reported futures volume (not audited exchange tape)",
            "FMP futures route unavailable in this OpenBB install",
            "Small HOLDOUT samples likely",
        ],
        "recommended_next_action": (
            "Acquire longer authentic GC futures+volume history (exchange/vendor) before trusting ORB conclusions; "
            "do not promote on ~60d sample."
            if verdict in ("DATA_SOURCE_UNSUITABLE", "INSUFFICIENT_SAMPLE")
            else "If edge claimed: freeze rules and paper-validate on independent period/provider."
        ),
    }
    VALIDATION_JSON.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    return payload


def main() -> None:
    import sys
    p = run_phase22(force_replay="--force-replay" in sys.argv)
    print(json.dumps({
        "ok": p.get("ok"),
        "verdict": p.get("verdict"),
        "bars": (p.get("futures_data") or {}).get("bars_5m"),
        "days": (p.get("futures_data") or {}).get("trading_days"),
        "volume_conclusion": (p.get("volume") or {}).get("conclusion"),
        "finalists": [f.get("candidate_id") for f in (p.get("finalists") or [])],
        "holdout": [
            {"id": h.get("candidate_id"), "n": h.get("resolved_n"), "e2r": h.get("theoretical_2r_expectancy")}
            for h in (p.get("holdout_metrics") or [])
        ],
        "paper": p.get("paper_validation_justified"),
    }, indent=2))


if __name__ == "__main__":
    main()
