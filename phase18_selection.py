"""Phase 18 candidate construction, freeze, and holdout stability."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Optional, Sequence

from phase18_metrics import scorecard_from_pairs
from sample_quality import sample_quality_label


@dataclass(frozen=True)
class StrategyCandidate:
    candidate_id: str
    execution_timeframe: str
    htf_policy: str
    entry_mode: str
    stop_mode: str
    confirmation_timeout_bars: Optional[int] = None
    fvg_timeout_bars: Optional[int] = None
    retrace_timeout_bars: Optional[int] = None
    target_evaluation: str = "2R primary research target; opposite liquidity tracked"
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def freeze_key(self) -> tuple:
        return (
            self.candidate_id,
            self.execution_timeframe,
            self.htf_policy,
            self.entry_mode,
            self.stop_mode,
            self.confirmation_timeout_bars,
            self.fvg_timeout_bars,
            self.retrace_timeout_bars,
        )


HTF_POLICIES = (
    "POLICY_A",  # no filter
    "POLICY_B",  # daily aligned
    "POLICY_C",  # h4 aligned
    "POLICY_D",  # daily AND h4
    "POLICY_E",  # reject opposed_both only
)


def apply_htf_policy(record: Any, policy: str) -> bool:
    """Evaluation-layer filter only — does not alter journal generation."""
    def g(k, default="unknown"):
        if isinstance(record, dict):
            return (record.get(k) or default)
        return getattr(record, k, None) or default

    vs_d = str(g("setup_vs_daily")).lower()
    vs_h = str(g("setup_vs_h4")).lower()
    if policy == "POLICY_A":
        return True
    if policy == "POLICY_B":
        return vs_d == "aligned"
    if policy == "POLICY_C":
        return vs_h == "aligned"
    if policy == "POLICY_D":
        return vs_d == "aligned" and vs_h == "aligned"
    if policy == "POLICY_E":
        return not (vs_d == "opposed" and vs_h == "opposed")
    raise ValueError(f"unknown HTF policy: {policy}")


def apply_expiry_policy(
    record: Any,
    *,
    confirmation_timeout: Optional[int],
    fvg_timeout: Optional[int],
    retrace_timeout: Optional[int],
) -> str:
    """
    Post-hoc expiry retention on TRAIN evidence.
    Returns: RETAINED | EXPIRED_CONFIRMATION | EXPIRED_FVG | EXPIRED_RETRACE
    Uses observed bar distances only (no future leakage beyond recorded timings).
    """
    def g(k, default=None):
        if isinstance(record, dict):
            return record.get(k, default)
        return getattr(record, k, default)

    if confirmation_timeout is not None:
        b = g("bars_sweep_to_choch")
        if b is None:
            # never confirmed — if status expired waiting confirmation, count expired
            if g("confirmation_timestamp") is None and g("status") == "EXPIRED":
                return "EXPIRED_CONFIRMATION"
        elif int(b) > int(confirmation_timeout):
            return "EXPIRED_CONFIRMATION"

    if fvg_timeout is not None and g("confirmation_timestamp") is not None:
        b = g("bars_choch_to_fvg")
        if b is None and g("fvg_created_timestamp") is None and g("status") == "EXPIRED":
            return "EXPIRED_FVG"
        if b is not None and int(b) > int(fvg_timeout):
            return "EXPIRED_FVG"

    if retrace_timeout is not None and g("fvg_created_timestamp") is not None:
        bfe = g("bars_fvg_to_entry") or {}
        # use min positive across modes if dict
        vals = []
        if isinstance(bfe, dict):
            vals = [int(v) for v in bfe.values() if v is not None]
        elif bfe is not None:
            vals = [int(bfe)]
        if vals and min(vals) > int(retrace_timeout):
            return "EXPIRED_RETRACE"
        if not vals and g("status") == "EXPIRED" and not any(
            (_g_triggered(e) for e in (g("entry_results") or []))
        ):
            return "EXPIRED_RETRACE"
    return "RETAINED"


def _g_triggered(e: Any) -> bool:
    if isinstance(e, dict):
        return bool(e.get("triggered"))
    return bool(getattr(e, "triggered", False))


def rank_candidates(
    scorecards: Sequence[dict[str, Any]],
    *,
    max_ambiguity_warn: float = 0.45,
) -> list[dict[str, Any]]:
    """
    Multi-criteria ranking (not a single weighted score).
    Prefer: larger resolved N, lower ambiguity, non-negative 2R expectancy, valid risk.
    """
    ranked = []
    for sc in scorecards:
        warnings = []
        rn = int(sc.get("resolved_n") or 0)
        amb = sc.get("ambiguity_pct")
        if rn < 20:
            warnings.append("INSUFFICIENT_SAMPLE")
        if amb is not None and amb >= max_ambiguity_warn:
            warnings.append("HIGH_AMBIGUITY")
        e2 = sc.get("theoretical_2r_expectancy")
        # Sort keys: sample ok, then expectancy, then lower stop, then lower amb
        sample_ok = 1 if rn >= 20 else 0
        amb_pen = 0 if amb is None else float(amb)
        ranked.append(
            {
                **sc,
                "selection_warnings": warnings,
                "_sort": (
                    sample_ok,
                    0 if "HIGH_AMBIGUITY" in warnings else 1,
                    float(e2) if e2 is not None else -999,
                    -amb_pen,
                    rn,
                ),
            }
        )
    ranked.sort(key=lambda x: x["_sort"], reverse=True)
    for i, row in enumerate(ranked, 1):
        row["train_rank"] = i
        row.pop("_sort", None)
    return ranked


def select_finalists(
    ranked: Sequence[dict[str, Any]],
    *,
    max_finalists: int = 3,
) -> list[dict[str, Any]]:
    """Pick top robust candidates; never auto-pick insufficient/high-amb alone."""
    finalists = []
    for row in ranked:
        warns = row.get("selection_warnings") or []
        if "INSUFFICIENT_SAMPLE" in warns:
            continue
        if "HIGH_AMBIGUITY" in warns and (row.get("theoretical_2r_expectancy") or -1) < 0:
            continue
        finalists.append(row)
        if len(finalists) >= max_finalists:
            break
    return finalists


def classify_stability(
    train: dict[str, Any],
    holdout: dict[str, Any],
) -> str:
    hn = int(holdout.get("resolved_n") or 0)
    if hn < 20:
        return "INSUFFICIENT_HOLDOUT_SAMPLE"
    te = train.get("theoretical_2r_expectancy")
    he = holdout.get("theoretical_2r_expectancy")
    tr1 = train.get("r1_rate")
    hr1 = holdout.get("r1_rate")
    ta = train.get("ambiguity_pct") or 0
    ha = holdout.get("ambiguity_pct") or 0

    if te is None or he is None:
        return "INSUFFICIENT_HOLDOUT_SAMPLE"

    sign_ok = (te >= 0 and he >= 0) or (te < 0 and he < 0)
    # collapse: holdout 1R rate drops by >50% relative or expectancy flips badly
    collapse = False
    if tr1 is not None and hr1 is not None and tr1 > 0 and hr1 < 0.5 * tr1:
        collapse = True
    if te > 0 and he < -0.25:
        collapse = True
    amb_blowup = ha > ta + 0.25 and ha > 0.5

    if collapse or amb_blowup:
        return "UNSTABLE"
    if sign_ok and abs((he or 0) - (te or 0)) <= 0.75:
        return "STABLE"
    if sign_ok:
        return "WEAKLY_STABLE"
    return "UNSTABLE"


def recommendation_category(
    *,
    finalists: Sequence[dict[str, Any]],
    stability: dict[str, str],
    need_intrabar: bool,
) -> str:
    if need_intrabar and not finalists:
        return "NEED_MORE_INTRABAR_RESOLUTION"
    if not finalists:
        return "NO_EDGE_OBSERVED"
    stables = [fid for fid, st in stability.items() if st == "STABLE"]
    weak = [fid for fid, st in stability.items() if st == "WEAKLY_STABLE"]
    insuf = [fid for fid, st in stability.items() if st == "INSUFFICIENT_HOLDOUT_SAMPLE"]
    if len(insuf) == len(stability) and stability:
        return "NEED_MORE_DATA"
    if len(stables) == 1 and not weak:
        return "LOCK_CANDIDATE_FOR_PAPER_VALIDATION"
    if len(stables) + len(weak) >= 2:
        return "KEEP_MULTIPLE_CANDIDATES"
    if stables:
        return "LOCK_CANDIDATE_FOR_PAPER_VALIDATION"
    if weak:
        return "KEEP_MULTIPLE_CANDIDATES"
    return "NO_EDGE_OBSERVED"
