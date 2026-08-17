"""Phase 18 analysis eligibility — do not mix categories in denominators."""

from __future__ import annotations

from typing import Any, Optional

from journal_models import (
    OUTCOME_AMBIGUOUS_INTRABAR,
    OUTCOME_EXPIRED_WITHOUT_EXIT,
    OUTCOME_NO_RISK_PLAN,
    OUTCOME_NOT_TRIGGERED,
)

ELIG_RESOLVED = "RESOLVED"
ELIG_AMBIGUOUS = "AMBIGUOUS"
ELIG_INVALID = "INVALID"
ELIG_UNTRIGGERED = "UNTRIGGERED"
ELIG_EXPIRED = "EXPIRED"
ELIG_INSUFFICIENT_DATA = "INSUFFICIENT_DATA"

RESOLVED_OUTCOMES = frozenset(
    {
        "STOP_HIT",
        "1R_HIT",
        "2R_HIT",
        "3R_HIT",
        "OPPOSITE_LIQUIDITY_HIT",
    }
)


def _get(obj: Any, key: str, default=None):
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def categorize_entry(
    record: Any,
    entry: Any,
    *,
    intrabar_resolution: Optional[str] = None,
) -> str:
    """
    Categorize one (setup, entry_mode) pair for analysis eligibility.

    intrabar_resolution: optional 15m→5m (or 5m→1m) resolver result applied first.
    """
    status = str(_get(record, "status") or "")
    invalidation = _get(record, "invalidation_reason")
    expiry_reason = _get(record, "expiry_reason")
    triggered = bool(_get(entry, "triggered"))
    outcome = str(_get(entry, "outcome") or "")
    flags = list(_get(entry, "ambiguity_flags") or [])
    risk = _get(entry, "risk_distance")

    if intrabar_resolution == "INSUFFICIENT_DATA":
        return ELIG_INSUFFICIENT_DATA
    if intrabar_resolution in ("ENTRY_THEN_STOP", "STOP_BEFORE_ENTRY", "RESOLVED_NO_STOP"):
        # Chronology resolved — treat as resolved for eligibility (outcome may be remapped upstream).
        return ELIG_RESOLVED
    if intrabar_resolution == "STILL_AMBIGUOUS":
        return ELIG_AMBIGUOUS

    if status == "INVALIDATED" or invalidation:
        # Invalid setup / directional stop — not a resolved trade outcome
        if not triggered or outcome == OUTCOME_NO_RISK_PLAN or risk is None or risk <= 0:
            return ELIG_INVALID

    if not triggered or outcome in (OUTCOME_NOT_TRIGGERED, ""):
        if status == "EXPIRED" or expiry_reason:
            return ELIG_EXPIRED
        return ELIG_UNTRIGGERED

    if outcome == OUTCOME_AMBIGUOUS_INTRABAR or "TRIGGER_BAR_STOP_AMBIGUITY" in flags:
        return ELIG_AMBIGUOUS

    if outcome == OUTCOME_NO_RISK_PLAN or risk is None or risk <= 0:
        return ELIG_INVALID

    if outcome == OUTCOME_EXPIRED_WITHOUT_EXIT:
        return ELIG_EXPIRED

    if outcome in RESOLVED_OUTCOMES:
        return ELIG_RESOLVED

    if status == "EXPIRED" and not triggered:
        return ELIG_EXPIRED

    return ELIG_INSUFFICIENT_DATA


def eligibility_counts(pairs: list[tuple[Any, Any, str]]) -> dict[str, int]:
    """pairs: (record, entry, eligibility)."""
    out = {
        ELIG_RESOLVED: 0,
        ELIG_AMBIGUOUS: 0,
        ELIG_INVALID: 0,
        ELIG_UNTRIGGERED: 0,
        ELIG_EXPIRED: 0,
        ELIG_INSUFFICIENT_DATA: 0,
    }
    for _, _, elig in pairs:
        out[elig] = out.get(elig, 0) + 1
    return out
