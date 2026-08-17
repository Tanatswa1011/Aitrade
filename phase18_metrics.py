"""Phase 18 metric helpers — resolved-only denominators, theoretical expectancy."""

from __future__ import annotations

import statistics
from typing import Any, Iterable, Optional, Sequence

from journal_models import (
    OUTCOME_1R_HIT,
    OUTCOME_2R_HIT,
    OUTCOME_3R_HIT,
    OUTCOME_OPPOSITE_LIQUIDITY_HIT,
    OUTCOME_STOP_HIT,
)
from phase18_eligibility import (
    ELIG_AMBIGUOUS,
    ELIG_EXPIRED,
    ELIG_INSUFFICIENT_DATA,
    ELIG_INVALID,
    ELIG_RESOLVED,
    ELIG_UNTRIGGERED,
    categorize_entry,
)
from sample_quality import sample_quality_label


def _g(obj: Any, key: str, default=None):
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def percentile(values: Sequence[float], p: float) -> Optional[float]:
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


def safe_rate(n: int, d: int) -> Optional[float]:
    if d <= 0:
        return None
    return n / d


def mean_or_none(vals: Sequence[float]) -> Optional[float]:
    return statistics.mean(vals) if vals else None


def median_or_none(vals: Sequence[float]) -> Optional[float]:
    return statistics.median(vals) if vals else None


def progressive_rr_hit(entry: Any, need: int) -> bool:
    rank = {OUTCOME_1R_HIT: 1, OUTCOME_2R_HIT: 2, OUTCOME_3R_HIT: 3}
    oc = str(_g(entry, "outcome") or "")
    ev = _g(entry, "event_timestamps") or {}
    rr_hits = ev.get("rr_hits") or {}
    if oc in rank and rank[oc] >= need:
        return True
    if any(rank.get(k, 0) >= need for k in rr_hits):
        return True
    if oc == OUTCOME_OPPOSITE_LIQUIDITY_HIT:
        return any(rank.get(k, 0) >= need for k in rr_hits)
    return False


def theoretical_fixed_target_expectancy(
    *,
    target_r: float,
    target_hits: int,
    stop_hits: int,
    resolved_n: int,
) -> Optional[float]:
    """
    Illustrative E[R] = P(target)*target_R - P(stop)*1R on resolved outcomes only.
    Not realized PnL. Assumes binary fixed-target vs stop (no partial exits).
    """
    if resolved_n <= 0:
        return None
    p_t = target_hits / resolved_n
    p_s = stop_hits / resolved_n
    return p_t * float(target_r) - p_s * 1.0


def iter_entry_pairs(
    records: Iterable[Any],
    *,
    entry_mode: Optional[str] = None,
    execution_tf: Optional[str] = None,
    resolutions: Optional[dict[tuple[str, str], str]] = None,
) -> list[dict[str, Any]]:
    """Flatten journal rows into analyzed entry pairs with eligibility."""
    out: list[dict[str, Any]] = []
    for rec in records:
        tf = str(_g(rec, "execution_timeframe") or _g(rec, "timeframe") or "")
        if execution_tf and tf != execution_tf:
            continue
        entries = _g(rec, "entry_results") or []
        for e in entries:
            mode = str(_g(e, "mode") or "")
            if entry_mode and mode != entry_mode:
                continue
            sid = str(_g(rec, "setup_id") or "")
            res_key = (sid, mode)
            outcome = str(_g(e, "outcome") or "")
            flags = list(_g(e, "ambiguity_flags") or [])
            truly_ambiguous = (
                outcome == "AMBIGUOUS_INTRABAR"
                or "TRIGGER_BAR_STOP_AMBIGUITY" in flags
            )
            # Only apply intrabar remap when this entry is actually unresolved.
            # reliability_flags on the setup are too broad (set on many resolved rows).
            intrabar = None
            if truly_ambiguous and resolutions:
                intrabar = resolutions.get(res_key)
            elig = categorize_entry(rec, e, intrabar_resolution=intrabar)
            # If 15m resolver cleared ambiguity, remap outcome for metric purposes
            effective_outcome = outcome
            if truly_ambiguous and intrabar == "STOP_BEFORE_ENTRY":
                effective_outcome = OUTCOME_STOP_HIT
                elig = ELIG_RESOLVED
            elif truly_ambiguous and intrabar == "ENTRY_THEN_STOP":
                # Entry filled then stop — still a stop for binary fixed-target view
                effective_outcome = OUTCOME_STOP_HIT
                elig = ELIG_RESOLVED
            elif truly_ambiguous and intrabar == "RESOLVED_NO_STOP":
                # Entry/stop order cleared on trigger bar; if journal left AMBIGUOUS, do not invent a win/loss.
                if outcome == "AMBIGUOUS_INTRABAR":
                    elig = ELIG_AMBIGUOUS
                else:
                    elig = ELIG_RESOLVED
            elif truly_ambiguous and intrabar == "STILL_AMBIGUOUS":
                elig = ELIG_AMBIGUOUS
            elif truly_ambiguous and intrabar == "INSUFFICIENT_DATA":
                elig = ELIG_INSUFFICIENT_DATA
            out.append(
                {
                    "record": rec,
                    "entry": e,
                    "eligibility": elig,
                    "execution_timeframe": tf,
                    "entry_mode": mode,
                    "effective_outcome": effective_outcome,
                    "liquidity_event_id": _g(rec, "liquidity_event_id"),
                    "setup_id": sid,
                    "session": _g(rec, "session"),
                    "swept_side": _g(rec, "swept_side"),
                    "htf_alignment": _g(rec, "htf_alignment"),
                    "setup_vs_daily": _g(rec, "setup_vs_daily"),
                    "setup_vs_h4": _g(rec, "setup_vs_h4"),
                    "trading_date": _g(rec, "trading_date"),
                }
            )
    return out


def scorecard_from_pairs(pairs: Sequence[dict[str, Any]], *, label: str = "") -> dict[str, Any]:
    triggered = [p for p in pairs if _g(p["entry"], "triggered")]
    resolved = [p for p in pairs if p["eligibility"] == ELIG_RESOLVED]
    ambiguous = [p for p in pairs if p["eligibility"] == ELIG_AMBIGUOUS]
    invalid = [p for p in pairs if p["eligibility"] == ELIG_INVALID]
    expired = [p for p in pairs if p["eligibility"] == ELIG_EXPIRED]
    untriggered = [p for p in pairs if p["eligibility"] == ELIG_UNTRIGGERED]

    valid_risk = [
        p
        for p in triggered
        if (_g(p["entry"], "risk_distance") or 0) > 0
        and str(_g(p["entry"], "outcome") or "") != "NO_RISK_PLAN"
    ]

    def _oc(p: dict[str, Any]) -> str:
        return str(p.get("effective_outcome") or _g(p["entry"], "outcome") or "")

    stop_n = sum(1 for p in resolved if _oc(p) == OUTCOME_STOP_HIT)
    r1 = sum(1 for p in resolved if progressive_rr_hit(p["entry"], 1))
    r2 = sum(1 for p in resolved if progressive_rr_hit(p["entry"], 2))
    r3 = sum(1 for p in resolved if progressive_rr_hit(p["entry"], 3))
    opp = sum(1 for p in resolved if _oc(p) == OUTCOME_OPPOSITE_LIQUIDITY_HIT)

    mfe = [float(_g(p["entry"], "mfe_r")) for p in resolved if _g(p["entry"], "mfe_r") is not None]
    mae = [float(_g(p["entry"], "mae_r")) for p in resolved if _g(p["entry"], "mae_r") is not None]
    risk = [
        float(_g(p["entry"], "risk_distance"))
        for p in valid_risk
        if _g(p["entry"], "risk_distance") is not None
    ]
    rr_opp = [
        float(_g(p["entry"], "rr_to_opposite"))
        for p in valid_risk
        if _g(p["entry"], "rr_to_opposite") is not None
    ]
    depths = [
        float(_g(p["entry"], "entry_depth"))
        for p in triggered
        if _g(p["entry"], "entry_depth") is not None
    ]
    retr = [
        float(_g(p["entry"], "max_retrace_depth"))
        for p in pairs
        if _g(p["entry"], "max_retrace_depth") is not None
    ]

    rn = len(resolved)
    tn = len(triggered)
    return {
        "label": label,
        "opportunities": len({p["setup_id"] for p in pairs}),
        "entry_rows": len(pairs),
        "triggered_n": tn,
        "resolved_n": rn,
        "ambiguous_n": len(ambiguous),
        "invalid_n": len(invalid),
        "expired_n": len(expired),
        "untriggered_n": len(untriggered),
        "ambiguity_pct": safe_rate(len(ambiguous), tn),
        "resolved_rate": safe_rate(rn, tn),
        "valid_risk_n": len(valid_risk),
        "valid_risk_rate": safe_rate(len(valid_risk), tn),
        "stop_n": stop_n,
        "stop_rate": safe_rate(stop_n, rn),
        "r1_n": r1,
        "r1_rate": safe_rate(r1, rn),
        "r2_n": r2,
        "r2_rate": safe_rate(r2, rn),
        "r3_n": r3,
        "r3_rate": safe_rate(r3, rn),
        "opposite_n": opp,
        "opposite_rate": safe_rate(opp, rn),
        "mean_mfe_r": mean_or_none(mfe),
        "median_mfe_r": median_or_none(mfe),
        "mean_mae_r": mean_or_none(mae),
        "median_mae_r": median_or_none(mae),
        "n_mfe": len(mfe),
        "n_mae": len(mae),
        "median_risk_distance": median_or_none(risk),
        "median_rr_to_opposite": median_or_none(rr_opp),
        "median_entry_depth": median_or_none(depths),
        "median_max_retrace_depth": median_or_none(retr),
        "theoretical_1r_expectancy": theoretical_fixed_target_expectancy(
            target_r=1.0, target_hits=r1, stop_hits=stop_n, resolved_n=rn
        ),
        "theoretical_2r_expectancy": theoretical_fixed_target_expectancy(
            target_r=2.0, target_hits=r2, stop_hits=stop_n, resolved_n=rn
        ),
        "theoretical_3r_expectancy": theoretical_fixed_target_expectancy(
            target_r=3.0, target_hits=r3, stop_hits=stop_n, resolved_n=rn
        ),
        "sample_quality": sample_quality_label(rn),
        "sample_quality_triggered": sample_quality_label(tn),
    }


def timing_distribution(values: Sequence[float]) -> dict[str, Any]:
    vals = [float(v) for v in values if v is not None]
    return {
        "n": len(vals),
        "median": median_or_none(vals),
        "p75": percentile(vals, 75),
        "p90": percentile(vals, 90),
        "p95": percentile(vals, 95),
        "max": max(vals) if vals else None,
    }


def mfe_distribution(pairs: Sequence[dict[str, Any]]) -> dict[str, Any]:
    resolved = [p for p in pairs if p["eligibility"] == ELIG_RESOLVED]
    mfe = [float(_g(p["entry"], "mfe_r")) for p in resolved if _g(p["entry"], "mfe_r") is not None]
    return {
        "n": len(mfe),
        "median": median_or_none(mfe),
        "p75": percentile(mfe, 75),
        "p90": percentile(mfe, 90),
        "p95": percentile(mfe, 95),
    }
