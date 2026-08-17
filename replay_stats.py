"""Descriptive statistics from historical setup journal records."""

from __future__ import annotations

import statistics
from collections import defaultdict
from typing import Any, Iterable, Optional, Sequence

from journal_models import (
    OUTCOME_1R_HIT,
    OUTCOME_2R_HIT,
    OUTCOME_3R_HIT,
    OUTCOME_AMBIGUOUS_INTRABAR,
    OUTCOME_NO_RISK_PLAN,
    OUTCOME_NOT_TRIGGERED,
    OUTCOME_OPPOSITE_LIQUIDITY_HIT,
    OUTCOME_STOP_HIT,
    SetupJournalRecord,
)


def _percentile(values: Sequence[float], p: float) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return float(ordered[0])
    k = (len(ordered) - 1) * (p / 100.0)
    f = int(k)
    c = min(f + 1, len(ordered) - 1)
    if f == c:
        return float(ordered[f])
    return float(ordered[f] + (ordered[c] - ordered[f]) * (k - f))


def _safe_rate(n: int, d: int) -> Optional[float]:
    if d <= 0:
        return None
    return n / d


def _entry_rows(records: Iterable[SetupJournalRecord]) -> list[dict[str, Any]]:
    rows = []
    for rec in records:
        for er in rec.entry_results:
            rows.append({"record": rec, "entry": er})
    return rows


def compute_replay_statistics(
    records: Sequence[SetupJournalRecord],
) -> dict[str, Any]:
    """
    Basic descriptive stats — not profitability.

    Hit rates state their denominator explicitly.
    Ambiguous outcomes excluded from clean hit denominators.
    """
    total = len(records)
    with_sweep = [r for r in records if r.sweep_timestamp is not None]
    with_conf = [r for r in records if r.confirmation_timestamp is not None]
    with_fvg = [r for r in records if r.fvg_created_timestamp is not None]

    entry_rows = _entry_rows(records)
    triggered = [x for x in entry_rows if x["entry"].triggered]
    excluded = {
        OUTCOME_AMBIGUOUS_INTRABAR,
        "EXPIRED_WITHOUT_EXIT",
        OUTCOME_NOT_TRIGGERED,
        OUTCOME_NO_RISK_PLAN,
    }
    resolved = [x for x in triggered if x["entry"].outcome not in excluded]
    ambiguous = [
        x for x in triggered if x["entry"].outcome == OUTCOME_AMBIGUOUS_INTRABAR
    ]
    unresolved = [
        x for x in triggered if x["entry"].outcome == "EXPIRED_WITHOUT_EXIT"
    ]
    no_risk = [x for x in triggered if x["entry"].outcome == OUTCOME_NO_RISK_PLAN]

    def hit_rate(outcome: str) -> dict[str, Any]:
        n = sum(1 for x in resolved if x["entry"].outcome == outcome)
        # Also count higher RR as having hit lower? For raw outcome equality only.
        # Separate: progressive — 2R_HIT implies 1R was reached historically via events
        return {
            "count": n,
            "denominator_resolved_triggered": len(resolved),
            "rate": _safe_rate(n, len(resolved)),
        }

    def progressive_rr_rate(min_rr_outcome: str) -> dict[str, Any]:
        rank = {OUTCOME_1R_HIT: 1, OUTCOME_2R_HIT: 2, OUTCOME_3R_HIT: 3}
        need = rank[min_rr_outcome]
        n = 0
        for x in resolved:
            oc = x["entry"].outcome
            ev = x["entry"].event_timestamps or {}
            rr_hits = ev.get("rr_hits") or {}
            if oc in rank and rank[oc] >= need:
                n += 1
            elif any(rank.get(k, 0) >= need for k in rr_hits):
                n += 1
            elif oc == OUTCOME_OPPOSITE_LIQUIDITY_HIT and need <= 3:
                # opposite may exceed RR — count via rr_hits only
                if any(rank.get(k, 0) >= need for k in rr_hits):
                    n += 1
        return {
            "count": n,
            "denominator_resolved_triggered": len(resolved),
            "rate": _safe_rate(n, len(resolved)),
            "note": "excludes AMBIGUOUS_INTRABAR from denominator",
        }

    mfe_r = [x["entry"].mfe_r for x in triggered if x["entry"].mfe_r is not None]
    mae_r = [x["entry"].mae_r for x in triggered if x["entry"].mae_r is not None]

    timing = {
        "sweep_to_choch": [r.bars_sweep_to_choch for r in with_conf if r.bars_sweep_to_choch is not None],
        "choch_to_fvg": [r.bars_choch_to_fvg for r in with_fvg if r.bars_choch_to_fvg is not None],
        "fvg_to_entry": [],
    }
    for r in records:
        if r.bars_fvg_to_entry:
            for v in r.bars_fvg_to_entry.values():
                if v is not None:
                    timing["fvg_to_entry"].append(v)

    def timing_block(vals: list[int]) -> dict[str, Any]:
        fvals = [float(v) for v in vals]
        return {
            "n": len(fvals),
            "median": statistics.median(fvals) if fvals else None,
            "p50": _percentile(fvals, 50),
            "p75": _percentile(fvals, 75),
            "p90": _percentile(fvals, 90),
            "p95": _percentile(fvals, 95),
        }

    by_mode: dict[str, Any] = {}
    for mode in ("first_touch", "boundary", "ce"):
        subset = [x for x in entry_rows if x["entry"].mode == mode]
        trig = [x for x in subset if x["entry"].triggered]
        res = [
            x
            for x in trig
            if x["entry"].outcome
            not in (
                OUTCOME_AMBIGUOUS_INTRABAR,
                "EXPIRED_WITHOUT_EXIT",
                OUTCOME_NO_RISK_PLAN,
            )
        ]
        risks = [
            x["entry"].risk_distance
            for x in trig
            if x["entry"].risk_distance is not None
        ]
        rr_opp = [
            x["entry"].rr_to_opposite
            for x in trig
            if x["entry"].rr_to_opposite is not None
        ]
        by_mode[mode] = {
            "trigger_count": len(trig),
            "average_risk_distance": statistics.mean(risks) if risks else None,
            "avg_rr_to_opposite": statistics.mean(rr_opp) if rr_opp else None,
            "1R_hit": progressive_rr_rate_for(res, OUTCOME_1R_HIT),
            "2R_hit": progressive_rr_rate_for(res, OUTCOME_2R_HIT),
            "3R_hit": progressive_rr_rate_for(res, OUTCOME_3R_HIT),
            "stop_rate": _safe_rate(
                sum(1 for x in res if x["entry"].outcome == OUTCOME_STOP_HIT),
                len(res),
            ),
            "avg_mfe_r": statistics.mean(
                [x["entry"].mfe_r for x in trig if x["entry"].mfe_r is not None]
            )
            if any(x["entry"].mfe_r is not None for x in trig)
            else None,
            "avg_mae_r": statistics.mean(
                [x["entry"].mae_r for x in trig if x["entry"].mae_r is not None]
            )
            if any(x["entry"].mae_r is not None for x in trig)
            else None,
            "ambiguous_count": sum(
                1 for x in trig if x["entry"].outcome == OUTCOME_AMBIGUOUS_INTRABAR
            ),
            "resolved_count": len(res),
        }

    return {
        "totals": {
            "setups": total,
            "liquidity_sweeps": len(with_sweep),
            "confirmations": len(with_conf),
            "fvgs": len(with_fvg),
        },
        "rates": {
            "confirmation_rate": {
                "count": len(with_conf),
                "denominator_sweeps": len(with_sweep),
                "rate": _safe_rate(len(with_conf), len(with_sweep)),
            },
            "fvg_formation_rate": {
                "count": len(with_fvg),
                "denominator_confirmations": len(with_conf),
                "rate": _safe_rate(len(with_fvg), len(with_conf)),
            },
            "entry_trigger_rate": {
                "count": len({id(x["record"]) for x in triggered}),
                "denominator_fvgs": len(with_fvg),
                "note": "setups with ≥1 triggered entry mode",
                "rate": _safe_rate(
                    len({r.setup_id for r in records if any(e.triggered for e in r.entry_results)}),
                    len(with_fvg),
                ),
            },
        },
        "outcome_buckets": {
            "resolved_triggered_entries": len(resolved),
            "ambiguous_triggered_entries": len(ambiguous),
            "unresolved_expired_entries": len(unresolved),
            "no_risk_plan_entries": len(no_risk),
        },
        "hit_rates_resolved_only": {
            "1R": progressive_rr_rate(OUTCOME_1R_HIT),
            "2R": progressive_rr_rate(OUTCOME_2R_HIT),
            "3R": progressive_rr_rate(OUTCOME_3R_HIT),
            "opposite_liquidity": hit_rate(OUTCOME_OPPOSITE_LIQUIDITY_HIT),
            "stop": hit_rate(OUTCOME_STOP_HIT),
            "ambiguous_share_of_triggered": {
                "count": len(ambiguous),
                "denominator_triggered": len(triggered),
                "rate": _safe_rate(len(ambiguous), len(triggered)),
            },
        },
        "mfe_mae": {
            "avg_mfe_r": statistics.mean(mfe_r) if mfe_r else None,
            "avg_mae_r": statistics.mean(mae_r) if mae_r else None,
            "n_mfe": len(mfe_r),
            "n_mae": len(mae_r),
        },
        "expiry_timing_distributions": {
            "bars_sweep_to_choch": timing_block(timing["sweep_to_choch"]),
            "bars_choch_to_fvg": timing_block(timing["choch_to_fvg"]),
            "bars_fvg_to_entry": timing_block(timing["fvg_to_entry"]),
        },
        "entry_mode_comparison": by_mode,
        "by_session": _group_counts(records, key=lambda r: r.session),
        "by_direction": _group_counts(records, key=lambda r: r.direction or "none"),
    }


def progressive_rr_rate_for(resolved_rows: list, min_rr_outcome: str) -> dict[str, Any]:
    rank = {OUTCOME_1R_HIT: 1, OUTCOME_2R_HIT: 2, OUTCOME_3R_HIT: 3}
    need = rank[min_rr_outcome]
    n = 0
    for x in resolved_rows:
        oc = x["entry"].outcome
        ev = x["entry"].event_timestamps or {}
        rr_hits = ev.get("rr_hits") or {}
        if oc in rank and rank[oc] >= need:
            n += 1
        elif any(rank.get(k, 0) >= need for k in rr_hits):
            n += 1
    return {
        "count": n,
        "denominator_resolved": len(resolved_rows),
        "rate": _safe_rate(n, len(resolved_rows)),
    }


def _group_counts(records: Sequence[SetupJournalRecord], key) -> dict[str, int]:
    out: dict[str, int] = defaultdict(int)
    for r in records:
        out[str(key(r))] += 1
    return dict(out)


def compare_stop_modes(
    records_by_mode: dict[str, Sequence[SetupJournalRecord]],
) -> dict[str, Any]:
    """Side-by-side descriptive comparison for explicit stop-mode replays."""
    out = {}
    for mode, recs in records_by_mode.items():
        stats = compute_replay_statistics(list(recs))
        out[mode] = {
            "setups": stats["totals"]["setups"],
            "hit_rates_resolved_only": stats["hit_rates_resolved_only"],
            "mfe_mae": stats["mfe_mae"],
            "entry_mode_comparison": stats["entry_mode_comparison"],
        }
    return out
