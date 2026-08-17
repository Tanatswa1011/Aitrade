"""Setup expiry / lifecycle configuration (Phase 9)."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Optional


# Canonical expiry reasons — only emit when the matching rule is enabled.
EXPIRY_REASON_NEW_SESSION_STARTED = "NEW_SESSION_STARTED"
EXPIRY_REASON_CONFIRMATION_TIMEOUT = "CONFIRMATION_TIMEOUT"
EXPIRY_REASON_FVG_TIMEOUT = "FVG_TIMEOUT"
EXPIRY_REASON_RETRACE_TIMEOUT = "RETRACE_TIMEOUT"
EXPIRY_REASON_OPPOSITE_LIQUIDITY_EVENT = "OPPOSITE_LIQUIDITY_EVENT"

# Statuses that remain eligible for lifecycle expiry (not terminal dead-ends).
EXPIRABLE_STATUSES = frozenset(
    {
        "WAITING_FOR_SESSION",
        "WAITING_FOR_SWEEP",
        "WAITING_FOR_CONFIRMATION",
        "WAITING_FOR_FVG",
        "WAITING_FOR_RETRACE",
        "ENTRY_READY",
    }
)


@dataclass(frozen=True)
class ExpiryConfig:
    """
    Deterministic setup expiry thresholds.

    Bar thresholds default to None (disabled) until historical evidence exists.
    Primary Phase 9 rule: expire_on_new_session.
    """

    max_bars_to_confirmation: Optional[int] = None
    max_bars_to_fvg: Optional[int] = None
    max_bars_to_retrace: Optional[int] = None
    expire_on_new_session: bool = True
    expire_on_opposite_session_sweep: bool = False
    enabled: bool = True
    extras: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


DEFAULT_EXPIRY_CONFIG = ExpiryConfig()
