"""Pure LuxAlgo CHoCH confirmation after a LiquiditySweep (no TradingView I/O)."""

from __future__ import annotations

from typing import Any, Optional, Sequence

from models import (
    ConfirmationDecision,
    LiquiditySweep,
    StructureConfirmation,
    StructureDirection,
    StructureKind,
    SweepSide,
    TimingConfidence,
)


def required_direction_for_sweep(sweep: LiquiditySweep) -> str:
    """
    Low swept → subsequent bullish CHoCH.
    High swept → subsequent bearish CHoCH.
    """
    if sweep.side == SweepSide.LOW.value or sweep.side == "low":
        return StructureDirection.BULLISH.value
    if sweep.side == SweepSide.HIGH.value or sweep.side == "high":
        return StructureDirection.BEARISH.value
    raise ValueError(f"Unknown sweep side: {sweep.side!r}")


def _ordering_after_sweep(
    event: StructureConfirmation,
    sweep: LiquiditySweep,
    *,
    sweep_bar_index: Optional[int] = None,
) -> tuple[Optional[bool], str]:
    """
    Prove event occurs strictly after the sweep.

    Returns (True/False/None, detail). None means ordering cannot be established
    (fail closed — do not confirm).
    """
    if event.timing_confidence == TimingConfidence.UNAVAILABLE.value:
        return None, "timing_unavailable"

    if event.event_timestamp is not None:
        if event.event_timestamp > sweep.sweep_timestamp:
            return True, "timestamp_after_sweep"
        return False, "timestamp_not_after_sweep"

    if (
        event.event_bar_index is not None
        and sweep_bar_index is not None
        and event.timing_confidence
        in (TimingConfidence.EXACT.value, TimingConfidence.DERIVED.value)
    ):
        if event.event_bar_index > sweep_bar_index:
            return True, "bar_index_after_sweep"
        return False, "bar_index_not_after_sweep"

    return None, "insufficient_ordering_evidence"


def confirm_after_sweep(
    sweep: LiquiditySweep,
    choch_events: Sequence[StructureConfirmation],
    *,
    sweep_bar_index: Optional[int] = None,
) -> ConfirmationDecision:
    """
    Select the first direction-aligned CHoCH that reliably occurs after the sweep.

    LuxAlgo never originates a setup: without a prior LiquiditySweep this is
    not called. BOS / IDM / x are ignored even if present in the sequence.
    """
    required = required_direction_for_sweep(sweep)
    rejected: list[dict[str, Any]] = []
    seen = 0

    # Stable order: timestamp → bar index → original order for unavailable.
    indexed = list(enumerate(choch_events))

    def sort_key(item: tuple[int, StructureConfirmation]) -> tuple:
        i, e = item
        if e.event_timestamp is not None:
            return (0, e.event_timestamp, i)
        if e.event_bar_index is not None:
            return (1, e.event_bar_index, i)
        return (2, i)

    ordered = [e for _, e in sorted(indexed, key=sort_key)]

    for event in ordered:
        seen += 1
        if event.kind != StructureKind.CHOCH.value:
            rejected.append(
                {
                    "raw_id": event.raw_id,
                    "reason": "not_choch",
                    "kind": event.kind,
                }
            )
            continue

        if event.direction != required:
            rejected.append(
                {
                    "raw_id": event.raw_id,
                    "reason": "wrong_direction",
                    "direction": event.direction,
                    "required": required,
                    "level": event.level,
                }
            )
            continue

        after, detail = _ordering_after_sweep(
            event, sweep, sweep_bar_index=sweep_bar_index
        )
        if after is None:
            rejected.append(
                {
                    "raw_id": event.raw_id,
                    "reason": "unreliable_ordering",
                    "detail": detail,
                    "timing_confidence": event.timing_confidence,
                    "level": event.level,
                    "direction": event.direction,
                }
            )
            continue
        if not after:
            rejected.append(
                {
                    "raw_id": event.raw_id,
                    "reason": "before_or_at_sweep",
                    "detail": detail,
                    "level": event.level,
                    "direction": event.direction,
                    "event_timestamp": event.event_timestamp,
                    "event_bar_index": event.event_bar_index,
                }
            )
            continue

        # First valid subsequent aligned CHoCH.
        return ConfirmationDecision(
            confirmed=True,
            confirmation=event,
            reason="confirmed",
            required_direction=required,
            candidates_seen=seen,
            rejected=rejected,
        )

    if not ordered:
        reason = "no_choch_events"
    else:
        aligned_rejects = [
            r
            for r in rejected
            if r.get("reason") in ("unreliable_ordering", "before_or_at_sweep")
        ]
        wrong_dir = [r for r in rejected if r.get("reason") == "wrong_direction"]
        not_choch = [r for r in rejected if r.get("reason") == "not_choch"]
        if aligned_rejects and all(
            r.get("reason") == "unreliable_ordering" for r in aligned_rejects
        ):
            reason = "no_reliable_ordering"
        elif aligned_rejects and all(
            r.get("reason") == "before_or_at_sweep" for r in aligned_rejects
        ):
            reason = "no_choch_after_sweep"
        elif wrong_dir and not aligned_rejects:
            reason = "no_direction_aligned_choch"
        elif not_choch and not wrong_dir and not aligned_rejects:
            reason = "no_choch_events"
        else:
            reason = "no_valid_confirmation"

    return ConfirmationDecision(
        confirmed=False,
        confirmation=None,
        reason=reason,
        required_direction=required,
        candidates_seen=seen,
        rejected=rejected,
    )


def bars_after_sweep(
    sweep: LiquiditySweep,
    confirmation: StructureConfirmation,
    *,
    bar_seconds: Optional[int] = None,
) -> dict[str, Any]:
    """Diagnostic deltas between sweep and confirmation (when timestamps exist)."""
    out: dict[str, Any] = {
        "seconds_after_sweep": None,
        "bars_after_sweep_approx": None,
    }
    if confirmation.event_timestamp is None:
        return out
    delta = int(confirmation.event_timestamp) - int(sweep.sweep_timestamp)
    out["seconds_after_sweep"] = delta
    if bar_seconds and bar_seconds > 0:
        out["bars_after_sweep_approx"] = delta / float(bar_seconds)
    return out
