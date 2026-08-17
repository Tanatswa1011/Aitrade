"""Descriptive HTF / MTF journal reporting (no hard filters)."""

from __future__ import annotations

import statistics
from collections import defaultdict
from typing import Any, Optional, Sequence

from journal_models import (
    OUTCOME_1R_HIT,
    OUTCOME_2R_HIT,
    OUTCOME_3R_HIT,
    OUTCOME_AMBIGUOUS_INTRABAR,
    OUTCOME_OPPOSITE_LIQUIDITY_HIT,
    OUTCOME_STOP_HIT,
    SetupJournalRecord,
)
from replay_stats import compute_replay_statistics, progressive_rr_rate_for
from sample_quality import mark_sample, sample_quality_label

INSUFFICIENT_SAMPLE_N = 20

HTF_REPORT_BUCKETS = (
    "aligned_both",
    "aligned_daily_only",
    "aligned_h4_only",
    "opposed_both",
    "mixed",
    "neutral_or_unknown",
)

D_VS_H4_GROUPS = (
    "setup_aligns_d_and_h4",
    "setup_aligns_d_only",
    "setup_aligns_h4_only",
    "setup_aligns_neither",
)


def htf_report_bucket(rec: SetupJournalRecord) -> str:
    """Descriptive reporting bucket (does not affect setup status)."""
    vs_d = (rec.setup_vs_daily or "unknown").lower()
    vs_h = (rec.setup_vs_h4 or "unknown").lower()
    align = (rec.htf_alignment or "unknown").lower()

    if vs_d == "aligned" and vs_h == "aligned":
        return "aligned_both"
    if vs_d == "aligned" and vs_h != "aligned":
        return "aligned_daily_only"
    if vs_h == "aligned" and vs_d != "aligned":
        return "aligned_h4_only"
    if vs_d == "opposed" and vs_h == "opposed":
        return "opposed_both"
    if align == "mixed" or (vs_d == "aligned") != (vs_h == "aligned"):
        if vs_d in ("aligned", "opposed") and vs_h in ("aligned", "opposed") and vs_d != vs_h:
            return "mixed"
        if vs_d == "opposed" or vs_h == "opposed":
            return "mixed"
    if vs_d in ("neutral", "unknown") and vs_h in ("neutral", "unknown"):
        return "neutral_or_unknown"
    if align in ("partial", "unknown") or vs_d in ("neutral", "unknown") or vs_h in (
        "neutral",
        "unknown",
    ):
        return "neutral_or_unknown"
    return "mixed"


def d_vs_h4_group(rec: SetupJournalRecord) -> str:
    vs_d = (rec.setup_vs_daily or "unknown").lower()
    vs_h = (rec.setup_vs_h4 or "unknown").lower()
    d_ok = vs_d == "aligned"
    h_ok = vs_h == "aligned"
    if d_ok and h_ok:
        return "setup_aligns_d_and_h4"
    if d_ok and not h_ok:
        return "setup_aligns_d_only"
    if h_ok and not d_ok:
        return "setup_aligns_h4_only"
    return "setup_aligns_neither"


def _mark_sample(n: int) -> dict[str, Any]:
    """Delegate to sample_quality (Phase 15 labels); keep legacy keys."""
    return mark_sample(n)


def _funnel(records: Sequence[SetupJournalRecord]) -> dict[str, Any]:
    with_sweep = [r for r in records if r.sweep_timestamp is not None]
    with_conf = [r for r in records if r.confirmation_timestamp is not None]
    with_fvg = [r for r in records if r.fvg_created_timestamp is not None]
    retrace = [
        r
        for r in with_fvg
        if any(e.max_retrace_depth is not None for e in r.entry_results)
    ]
    triggered = [r for r in records if any(e.triggered for e in r.entry_results)]
    valid_risk = [
        r
        for r in triggered
        if any(e.risk_distance is not None and e.risk_distance > 0 for e in r.entry_results)
    ]
    resolved = []
    excluded = {
        OUTCOME_AMBIGUOUS_INTRABAR,
        "EXPIRED_WITHOUT_EXIT",
        "NOT_TRIGGERED",
        "NO_RISK_PLAN",
    }
    for r in triggered:
        for e in r.entry_results:
            if e.triggered and e.outcome not in excluded:
                resolved.append(r)
                break
    base = _mark_sample(len(records))
    return {
        **base,
        "liquidity_sweeps": len(with_sweep),
        "choch_confirmations": len(with_conf),
        "fvg_formations": len(with_fvg),
        "retracements": len(retrace),
        "triggered_entries": len(triggered),
        "valid_risk_plans": len(valid_risk),
        "resolved_outcomes": len(resolved),
    }


def _outcome_block(records: Sequence[SetupJournalRecord]) -> dict[str, Any]:
    stats = compute_replay_statistics(list(records))
    entry_rows = []
    for r in records:
        for e in r.entry_results:
            if e.triggered:
                entry_rows.append({"record": r, "entry": e})
    resolved = [
        x
        for x in entry_rows
        if x["entry"].outcome
        not in (
            OUTCOME_AMBIGUOUS_INTRABAR,
            "EXPIRED_WITHOUT_EXIT",
            "NOT_TRIGGERED",
            "NO_RISK_PLAN",
        )
    ]
    ambiguous = [
        x for x in entry_rows if x["entry"].outcome == OUTCOME_AMBIGUOUS_INTRABAR
    ]
    mfe = [x["entry"].mfe_r for x in entry_rows if x["entry"].mfe_r is not None]
    mae = [x["entry"].mae_r for x in entry_rows if x["entry"].mae_r is not None]

    def rate(outcome: str) -> Optional[float]:
        if not resolved:
            return None
        return sum(1 for x in resolved if x["entry"].outcome == outcome) / len(resolved)

    return {
        **_mark_sample(len(records)),
        "count": len(records),
        "1R_hit": progressive_rr_rate_for(resolved, OUTCOME_1R_HIT),
        "2R_hit": progressive_rr_rate_for(resolved, OUTCOME_2R_HIT),
        "3R_hit": progressive_rr_rate_for(resolved, OUTCOME_3R_HIT),
        "stop_hit": {
            "count": sum(1 for x in resolved if x["entry"].outcome == OUTCOME_STOP_HIT),
            "rate": rate(OUTCOME_STOP_HIT),
            "denominator_resolved": len(resolved),
        },
        "opposite_liquidity_hit": {
            "count": sum(
                1
                for x in resolved
                if x["entry"].outcome == OUTCOME_OPPOSITE_LIQUIDITY_HIT
            ),
            "rate": rate(OUTCOME_OPPOSITE_LIQUIDITY_HIT),
            "denominator_resolved": len(resolved),
        },
        "average_mfe_r": statistics.mean(mfe) if mfe else None,
        "median_mfe_r": statistics.median(mfe) if mfe else None,
        "average_mae_r": statistics.mean(mae) if mae else None,
        "ambiguity_rate": (
            None
            if not entry_rows
            else len(ambiguous) / len(entry_rows)
        ),
        "hit_rates_resolved_only": stats.get("hit_rates_resolved_only"),
    }


def funnel_by_htf_bucket(records: Sequence[SetupJournalRecord]) -> dict[str, Any]:
    groups: dict[str, list[SetupJournalRecord]] = defaultdict(list)
    for r in records:
        groups[htf_report_bucket(r)].append(r)
    out = {}
    for bucket in HTF_REPORT_BUCKETS:
        rows = groups.get(bucket, [])
        out[bucket] = _funnel(rows)
    # include any unexpected
    for k, rows in groups.items():
        if k not in out:
            out[k] = _funnel(rows)
    return out


def outcomes_by_alignment(records: Sequence[SetupJournalRecord]) -> dict[str, Any]:
    """Canonical alignment labels: aligned_bullish/bearish, mixed, partial, opposed, unknown."""
    groups: dict[str, list[SetupJournalRecord]] = defaultdict(list)
    for r in records:
        a = (r.htf_alignment or "unknown").lower()
        if a in ("aligned_bullish", "aligned_bearish", "mixed", "partial", "unknown"):
            groups[a].append(r)
        elif (r.setup_vs_daily, r.setup_vs_h4) == ("opposed", "opposed"):
            groups["opposed"].append(r)
        else:
            groups[a or "unknown"].append(r)
    # Also expose opposed via setup_vs when both opposed
    for r in records:
        if (r.setup_vs_daily or "").lower() == "opposed" and (
            r.setup_vs_h4 or ""
        ).lower() == "opposed":
            if r not in groups["opposed"]:
                groups["opposed"].append(r)
    keys = (
        "aligned_bullish",
        "aligned_bearish",
        "mixed",
        "partial",
        "opposed",
        "unknown",
    )
    return {k: _outcome_block(groups.get(k, [])) for k in keys}


def metrics_by_execution_timeframe(
    records: Sequence[SetupJournalRecord],
) -> dict[str, Any]:
    groups: dict[str, list[SetupJournalRecord]] = defaultdict(list)
    for r in records:
        tf = r.execution_timeframe or r.timeframe or "unknown"
        groups[str(tf)].append(r)

    def timing_medians(rows: Sequence[SetupJournalRecord]) -> dict[str, Any]:
        def med_sec(getter) -> Optional[float]:
            vals = []
            for r in rows:
                a, b = getter(r)
                if a is not None and b is not None:
                    vals.append(float(b) - float(a))
            return statistics.median(vals) if vals else None

        return {
            "median_sec_sweep_to_confirmation": med_sec(
                lambda r: (r.sweep_timestamp, r.confirmation_timestamp)
            ),
            "median_sec_confirmation_to_fvg": med_sec(
                lambda r: (r.confirmation_timestamp, r.fvg_created_timestamp)
            ),
            "median_sec_fvg_to_entry": med_sec(
                lambda r: (
                    r.fvg_created_timestamp,
                    next(
                        (
                            e.entry_timestamp
                            for e in r.entry_results
                            if e.triggered and e.entry_timestamp is not None
                        ),
                        None,
                    ),
                )
            ),
        }

    out = {}
    for tf, rows in sorted(groups.items()):
        stats = compute_replay_statistics(rows)
        with_sweep = [r for r in rows if r.sweep_timestamp is not None]
        with_conf = [r for r in rows if r.confirmation_timestamp is not None]
        with_fvg = [r for r in rows if r.fvg_created_timestamp is not None]
        triggered = [r for r in rows if any(e.triggered for e in r.entry_results)]
        valid_risk = [
            r
            for r in triggered
            if any(
                e.risk_distance is not None and e.risk_distance > 0 for e in r.entry_results
            )
        ]
        block = _outcome_block(rows)
        out[tf] = {
            **_mark_sample(len(rows)),
            "setup_count": len(rows),
            "confirmation_rate": {
                "n": len(with_conf),
                "denominator_sweeps": len(with_sweep),
                "rate": (len(with_conf) / len(with_sweep)) if with_sweep else None,
            },
            "fvg_rate": {
                "n": len(with_fvg),
                "denominator_confirmations": len(with_conf),
                "rate": (len(with_fvg) / len(with_conf)) if with_conf else None,
            },
            "entry_trigger_rate": {
                "n": len(triggered),
                "denominator_fvgs": len(with_fvg),
                "rate": (len(triggered) / len(with_fvg)) if with_fvg else None,
            },
            "valid_risk_count": len(valid_risk),
            "stop_rate": block["stop_hit"],
            "1R_hit": block["1R_hit"],
            "2R_hit": block["2R_hit"],
            "3R_hit": block["3R_hit"],
            "opposite_liquidity_hit": block["opposite_liquidity_hit"],
            "average_mfe_r": block["average_mfe_r"],
            "average_mae_r": block["average_mae_r"],
            "ambiguity_rate": block["ambiguity_rate"],
            **timing_medians(rows),
            "raw_totals": stats.get("totals"),
        }
    return out


def paired_execution_comparison(
    records: Sequence[SetupJournalRecord],
) -> dict[str, Any]:
    """Same liquidity_event_id with both 5m and 15m analyses."""
    by_event: dict[str, dict[str, SetupJournalRecord]] = defaultdict(dict)
    for r in records:
        eid = r.liquidity_event_id or r.setup_id.split("|exec:")[0]
        tf = r.execution_timeframe or r.timeframe or ""
        by_event[eid][tf] = r

    pairs = []
    for eid, tfs in sorted(by_event.items()):
        if "5m" not in tfs or "15m" not in tfs:
            continue
        a = tfs["5m"]
        b = tfs["15m"]

        def first_ts(x: Optional[int], y: Optional[int]) -> Optional[str]:
            if x is None and y is None:
                return None
            if x is None:
                return "15m"
            if y is None:
                return "5m"
            if x < y:
                return "5m"
            if y < x:
                return "15m"
            return "tie"

        pairs.append(
            {
                "liquidity_event_id": eid,
                "5m": {
                    "setup_id": a.setup_id,
                    "status": a.status,
                    "confirmation_timestamp": a.confirmation_timestamp,
                    "fvg_created_timestamp": a.fvg_created_timestamp,
                    "triggered": any(e.triggered for e in a.entry_results),
                    "entry_prices": [
                        e.entry_price for e in a.entry_results if e.triggered
                    ],
                    "risk_distances": [
                        e.risk_distance
                        for e in a.entry_results
                        if e.triggered and e.risk_distance is not None
                    ],
                    "rr_to_opposite": [
                        e.rr_to_opposite
                        for e in a.entry_results
                        if e.triggered and e.rr_to_opposite is not None
                    ],
                    "outcomes": [e.outcome for e in a.entry_results if e.triggered],
                },
                "15m": {
                    "setup_id": b.setup_id,
                    "status": b.status,
                    "confirmation_timestamp": b.confirmation_timestamp,
                    "fvg_created_timestamp": b.fvg_created_timestamp,
                    "triggered": any(e.triggered for e in b.entry_results),
                    "entry_prices": [
                        e.entry_price for e in b.entry_results if e.triggered
                    ],
                    "risk_distances": [
                        e.risk_distance
                        for e in b.entry_results
                        if e.triggered and e.risk_distance is not None
                    ],
                    "rr_to_opposite": [
                        e.rr_to_opposite
                        for e in b.entry_results
                        if e.triggered and e.rr_to_opposite is not None
                    ],
                    "outcomes": [e.outcome for e in b.entry_results if e.triggered],
                },
                "which_confirmed_first": first_ts(
                    a.confirmation_timestamp, b.confirmation_timestamp
                ),
                "which_fvg_first": first_ts(
                    a.fvg_created_timestamp, b.fvg_created_timestamp
                ),
                "same_htf_context": (
                    a.daily_bias == b.daily_bias
                    and a.h4_bias == b.h4_bias
                    and a.htf_alignment == b.htf_alignment
                ),
            }
        )
    return {
        **_mark_sample(len(pairs)),
        "paired_event_count": len(pairs),
        "pairs": pairs,
    }


def d_vs_h4_contribution(records: Sequence[SetupJournalRecord]) -> dict[str, Any]:
    groups: dict[str, list[SetupJournalRecord]] = defaultdict(list)
    for r in records:
        groups[d_vs_h4_group(r)].append(r)
    return {k: {**_funnel(groups.get(k, [])), **_outcome_block(groups.get(k, []))} for k in D_VS_H4_GROUPS}


def compute_mtf_journal_report(records: Sequence[SetupJournalRecord]) -> dict[str, Any]:
    return {
        "journal_size": len(records),
        "htf_alignment_distribution": {
            k: len([r for r in records if htf_report_bucket(r) == k])
            for k in HTF_REPORT_BUCKETS
        },
        "canonical_htf_alignment_counts": _count_by(
            records, lambda r: r.htf_alignment or "unknown"
        ),
        "funnel_by_htf_bucket": funnel_by_htf_bucket(records),
        "outcomes_by_alignment": outcomes_by_alignment(records),
        "metrics_by_execution_timeframe": metrics_by_execution_timeframe(records),
        "paired_5m_15m": paired_execution_comparison(records),
        "paired_5m_15m_summary": paired_execution_summary(records),
        "daily_vs_h4_contribution": d_vs_h4_contribution(records),
        "context_group_stats": context_group_stats(records),
        "session_funnel": session_side_funnel(records),
        "invalidation_breakdown": invalidation_breakdown(records),
        "trigger_bar_ambiguity": trigger_bar_ambiguity_report(records),
        "intrabar_ambiguity": intrabar_ambiguity_report(records),
        "timing_distributions": timing_distributions(records),
        "insufficient_sample_threshold": INSUFFICIENT_SAMPLE_N,
    }


def _percentile(values: Sequence[float], p: float) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(float(v) for v in values)
    if len(ordered) == 1:
        return ordered[0]
    k = (len(ordered) - 1) * (p / 100.0)
    f = int(k)
    c = min(f + 1, len(ordered) - 1)
    if f == c:
        return ordered[f]
    return ordered[f] + (ordered[c] - ordered[f]) * (k - f)


def _timing_dist(vals: list[float]) -> dict[str, Any]:
    return {
        "count": len(vals),
        "median": statistics.median(vals) if vals else None,
        "p75": _percentile(vals, 75),
        "p90": _percentile(vals, 90),
        "p95": _percentile(vals, 95),
    }


def timing_distributions(records: Sequence[SetupJournalRecord]) -> dict[str, Any]:
    by_tf: dict[str, dict[str, list[float]]] = defaultdict(
        lambda: {"sweep_to_choch": [], "choch_to_fvg": [], "fvg_to_entry": []}
    )
    for r in records:
        tf = r.execution_timeframe or r.timeframe or "unknown"
        if r.sweep_timestamp is not None and r.confirmation_timestamp is not None:
            by_tf[tf]["sweep_to_choch"].append(
                float(r.confirmation_timestamp - r.sweep_timestamp)
            )
        if r.confirmation_timestamp is not None and r.fvg_created_timestamp is not None:
            by_tf[tf]["choch_to_fvg"].append(
                float(r.fvg_created_timestamp - r.confirmation_timestamp)
            )
        for e in r.entry_results:
            if (
                e.triggered
                and e.entry_timestamp is not None
                and r.fvg_created_timestamp is not None
            ):
                by_tf[tf]["fvg_to_entry"].append(
                    float(e.entry_timestamp - r.fvg_created_timestamp)
                )
                break
    return {
        tf: {k: _timing_dist(v) for k, v in buckets.items()}
        for tf, buckets in sorted(by_tf.items())
    }


def context_group_stats(records: Sequence[SetupJournalRecord]) -> dict[str, Any]:
    groups: dict[str, list[SetupJournalRecord]] = defaultdict(list)
    for r in records:
        groups[htf_report_bucket(r)].append(r)
    out = {}
    for bucket in HTF_REPORT_BUCKETS:
        rows = groups.get(bucket, [])
        block = _outcome_block(rows)
        funnel = _funnel(rows)
        mfe = [
            e.mfe_r
            for r in rows
            for e in r.entry_results
            if e.triggered and e.mfe_r is not None
        ]
        mae = [
            e.mae_r
            for r in rows
            for e in r.entry_results
            if e.triggered and e.mae_r is not None
        ]
        out[bucket] = {
            **_mark_sample(len(rows)),
            "N": len(rows),
            "sweep_count": funnel["liquidity_sweeps"],
            "confirmation_count": funnel["choch_confirmations"],
            "fvg_count": funnel["fvg_formations"],
            "entry_count": funnel["triggered_entries"],
            "valid_risk_count": funnel["valid_risk_plans"],
            "STOP_HIT": block["stop_hit"],
            "1R": block["1R_hit"],
            "2R": block["2R_hit"],
            "3R": block["3R_hit"],
            "opposite_liquidity_hit": block["opposite_liquidity_hit"],
            "AMBIGUOUS_INTRABAR": {
                "count": sum(
                    1
                    for r in rows
                    for e in r.entry_results
                    if e.outcome == OUTCOME_AMBIGUOUS_INTRABAR
                )
            },
            "average_mfe_r": statistics.mean(mfe) if mfe else None,
            "median_mfe_r": statistics.median(mfe) if mfe else None,
            "average_mae_r": statistics.mean(mae) if mae else None,
            "median_mae_r": statistics.median(mae) if mae else None,
        }
    return out


def session_side_funnel(records: Sequence[SetupJournalRecord]) -> dict[str, Any]:
    def key_fn(r: SetupJournalRecord) -> list[str]:
        keys = ["overall", r.session or "unknown"]
        side = (r.swept_side or "").lower()
        sess = r.session or ""
        if sess and side in ("high", "low"):
            keys.append(f"{sess} {side.capitalize()}")
        return keys

    groups: dict[str, list[SetupJournalRecord]] = defaultdict(list)
    for r in records:
        for k in key_fn(r):
            groups[k].append(r)

    wanted = (
        "overall",
        "Asia",
        "London",
        "Asia High",
        "Asia Low",
        "London High",
        "London Low",
    )
    out = {}
    for k in wanted:
        rows = groups.get(k, [])
        funnel = _funnel(rows)
        invalidated = sum(1 for r in rows if (r.status or "").upper() == "INVALIDATED")
        expired = sum(1 for r in rows if (r.status or "").upper() == "EXPIRED")
        ambiguous = sum(
            1
            for r in rows
            for e in r.entry_results
            if e.outcome == OUTCOME_AMBIGUOUS_INTRABAR
        )
        out[k] = {
            **funnel,
            "completed_sessions": len(rows),
            "invalidated": invalidated,
            "expired": expired,
            "ambiguous": ambiguous,
        }
    return out


def invalidation_breakdown(records: Sequence[SetupJournalRecord]) -> dict[str, Any]:
    rows = []
    for r in records:
        reason = r.invalidation_reason or ""
        for e in r.entry_results:
            cat = "other"
            low = reason.lower()
            if "invalidate" in low or (r.status or "").upper() == "INVALIDATED":
                if "before" in low or "pre_entry" in low or "pre-entry" in low:
                    cat = "invalidated_before_entry"
                elif "directional" in low or "stop_not_directional" in low:
                    cat = "invalid_directional_stop"
                elif "full_fill" in low or "full-fill" in low:
                    cat = "full_fill_related"
                elif reason:
                    cat = "other"
                else:
                    cat = "other"
            rows.append(
                {
                    "category": cat if (r.status or "").upper() == "INVALIDATED" else "not_invalidated",
                    "execution_timeframe": r.execution_timeframe,
                    "entry_mode": e.mode,
                    "session": r.session,
                    "status": r.status,
                    "reason": reason,
                    "outcome": e.outcome,
                }
            )
    by_cat: dict[str, int] = defaultdict(int)
    by_tf: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    by_mode: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    by_session: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for row in rows:
        if row["category"] == "not_invalidated":
            continue
        by_cat[row["category"]] += 1
        by_tf[str(row["execution_timeframe"])][row["category"]] += 1
        by_mode[str(row["entry_mode"])][row["category"]] += 1
        by_session[str(row["session"])][row["category"]] += 1
    return {
        "by_category": dict(by_cat),
        "by_execution_timeframe": {k: dict(v) for k, v in by_tf.items()},
        "by_entry_mode": {k: dict(v) for k, v in by_mode.items()},
        "by_session": {k: dict(v) for k, v in by_session.items()},
        "invalidated_setup_count": sum(
            1 for r in records if (r.status or "").upper() == "INVALIDATED"
        ),
        "no_risk_plan_entry_count": sum(
            1
            for r in records
            for e in r.entry_results
            if e.outcome == "NO_RISK_PLAN"
        ),
    }


def trigger_bar_ambiguity_report(records: Sequence[SetupJournalRecord]) -> dict[str, Any]:
    triggered = [
        (r, e)
        for r in records
        for e in r.entry_results
        if e.triggered
    ]
    flagged = [
        (r, e)
        for r, e in triggered
        if "TRIGGER_BAR_STOP_AMBIGUITY" in (r.reliability_flags or [])
        or "TRIGGER_BAR_STOP_AMBIGUITY" in (e.ambiguity_flags or [])
    ]
    by_tf: dict[str, list] = defaultdict(list)
    by_mode: dict[str, list] = defaultdict(list)
    for r, e in flagged:
        by_tf[str(r.execution_timeframe or r.timeframe)].append(e)
        by_mode[str(e.mode)].append(e)
    return {
        "count": len(flagged),
        "denominator_triggered_candidates": len(triggered),
        "rate": (len(flagged) / len(triggered)) if triggered else None,
        "by_execution_timeframe": {k: len(v) for k, v in by_tf.items()},
        "by_entry_mode": {k: len(v) for k, v in by_mode.items()},
        "phase15_candidate": bool(triggered) and (len(flagged) / len(triggered) >= 0.15),
    }


def intrabar_ambiguity_report(records: Sequence[SetupJournalRecord]) -> dict[str, Any]:
    triggered = [
        e for r in records for e in r.entry_results if e.triggered
    ]
    amb = [e for e in triggered if e.outcome == OUTCOME_AMBIGUOUS_INTRABAR]
    return {
        "count": len(amb),
        "denominator_triggered": len(triggered),
        "rate": (len(amb) / len(triggered)) if triggered else None,
        "note": "OHLC path unknown; AMBIGUOUS_INTRABAR is fail-closed labeling only",
    }


def paired_execution_summary(records: Sequence[SetupJournalRecord]) -> dict[str, Any]:
    paired = paired_execution_comparison(records)
    pairs = paired.get("pairs") or []
    both_conf = sum(
        1
        for p in pairs
        if p["5m"]["confirmation_timestamp"] is not None
        and p["15m"]["confirmation_timestamp"] is not None
    )
    only5 = sum(
        1
        for p in pairs
        if p["5m"]["confirmation_timestamp"] is not None
        and p["15m"]["confirmation_timestamp"] is None
    )
    only15 = sum(
        1
        for p in pairs
        if p["5m"]["confirmation_timestamp"] is None
        and p["15m"]["confirmation_timestamp"] is not None
    )
    neither = sum(
        1
        for p in pairs
        if p["5m"]["confirmation_timestamp"] is None
        and p["15m"]["confirmation_timestamp"] is None
    )
    both_ent = sum(1 for p in pairs if p["5m"]["triggered"] and p["15m"]["triggered"])
    ent5 = sum(1 for p in pairs if p["5m"]["triggered"] and not p["15m"]["triggered"])
    ent15 = sum(1 for p in pairs if p["15m"]["triggered"] and not p["5m"]["triggered"])

    conf_leads = []
    entry_leads = []
    risk_deltas = []
    rr_deltas = []
    outcome_pairs = defaultdict(int)
    for p in pairs:
        c5, c15 = p["5m"]["confirmation_timestamp"], p["15m"]["confirmation_timestamp"]
        if c5 is not None and c15 is not None:
            conf_leads.append(float(c5 - c15))  # negative => 5m earlier
        # entry lead: earliest triggered entry ts
        def earliest_entry(side):
            # not stored directly; use confirmation/fvg proxy unavailable — skip if no prices
            return None

        r5 = p["5m"]["risk_distances"]
        r15 = p["15m"]["risk_distances"]
        if r5 and r15:
            risk_deltas.append(float(r5[0]) - float(r15[0]))
        rr5 = p["5m"]["rr_to_opposite"]
        rr15 = p["15m"]["rr_to_opposite"]
        if rr5 and rr15:
            rr_deltas.append(float(rr5[0]) - float(rr15[0]))
        o5 = ",".join(p["5m"]["outcomes"]) or "none"
        o15 = ",".join(p["15m"]["outcomes"]) or "none"
        outcome_pairs[f"{o5}||{o15}"] += 1

    return {
        **_mark_sample(len(pairs)),
        "paired_event_count": len(pairs),
        "both_confirmed": both_conf,
        "5m_only_confirmed": only5,
        "15m_only_confirmed": only15,
        "neither_confirmed": neither,
        "both_entered": both_ent,
        "5m_only_entered": ent5,
        "15m_only_entered": ent15,
        "confirmation_lead_time_sec_5m_minus_15m": _timing_dist(conf_leads),
        "risk_distance_delta_5m_minus_15m": _timing_dist(risk_deltas),
        "rr_to_opposite_delta_5m_minus_15m": _timing_dist(rr_deltas),
        "outcome_pairing_counts": dict(outcome_pairs),
        "note": "Lead times descriptive only; faster is not better.",
    }


def _count_by(records: Sequence[SetupJournalRecord], key) -> dict[str, int]:
    out: dict[str, int] = defaultdict(int)
    for r in records:
        out[str(key(r))] += 1
    return dict(out)
