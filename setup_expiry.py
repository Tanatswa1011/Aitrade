"""Deterministic setup expiry evaluation (separate from sweep/FVG/entry detectors)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Optional, Sequence, Union

from expiry_config import (
    DEFAULT_EXPIRY_CONFIG,
    EXPIRABLE_STATUSES,
    EXPIRY_REASON_CONFIRMATION_TIMEOUT,
    EXPIRY_REASON_FVG_TIMEOUT,
    EXPIRY_REASON_NEW_SESSION_STARTED,
    EXPIRY_REASON_OPPOSITE_LIQUIDITY_EVENT,
    EXPIRY_REASON_RETRACE_TIMEOUT,
    ExpiryConfig,
)
from models import (
    Bar,
    LiquiditySweep,
    SessionRange,
    SetupStatus,
    TradeSetup,
)
from session_time import ResolvedSessionWindow, resolve_session_window
from sessions_config import SESSION_DEFINITIONS


@dataclass(frozen=True)
class SessionContext:
    """
    Active strategy session context for expiry.

    Built from DST-aware resolved windows — not wall-clock alone.
    """

    # Session currently selected / under analysis (the setup's own session).
    setup_session_name: str
    setup_trading_date: Optional[str]
    setup_window: Optional[ResolvedSessionWindow]

    # Later / active primary context relative to evaluation time.
    active_session_name: Optional[str] = None
    active_trading_date: Optional[str] = None
    active_window: Optional[ResolvedSessionWindow] = None

    # Evaluation clock (prefer last bar time).
    now_ts: Optional[int] = None

    # Optional opposite-session sweep evidence (pre-detected; no strategy invent).
    opposite_session_sweeps: tuple[LiquiditySweep, ...] = ()

    extras: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "setup_session_name": self.setup_session_name,
            "setup_trading_date": self.setup_trading_date,
            "setup_window": None
            if self.setup_window is None
            else self.setup_window.to_dict(),
            "active_session_name": self.active_session_name,
            "active_trading_date": self.active_trading_date,
            "active_window": None
            if self.active_window is None
            else self.active_window.to_dict(),
            "now_ts": self.now_ts,
            "opposite_session_sweep_count": len(self.opposite_session_sweeps),
            "extras": dict(self.extras),
        }


@dataclass(frozen=True)
class ExpiryDecision:
    expired: bool
    reason: Optional[str]
    detail: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "expired": self.expired,
            "reason": self.reason,
            "detail": self.detail,
        }


def _as_setup_dict(setup: Union[TradeSetup, dict[str, Any]]) -> dict[str, Any]:
    if isinstance(setup, TradeSetup):
        return setup.to_dict()
    return dict(setup)


def _parse_trading_date(raw: Optional[str]) -> Optional[date]:
    if not raw:
        return None
    try:
        return date.fromisoformat(str(raw)[:10])
    except ValueError:
        return None


def resolve_setup_window(
    session_name: str,
    trading_date: Optional[str],
    session_range: Optional[Union[SessionRange, dict[str, Any]]] = None,
) -> Optional[ResolvedSessionWindow]:
    """Resolve DST-aware window for a setup's session identity."""
    definition = SESSION_DEFINITIONS.get(session_name)
    if definition is None:
        return None
    td = _parse_trading_date(trading_date)
    if td is None and isinstance(session_range, SessionRange):
        extras = session_range.extras or {}
        rw = extras.get("resolved_window") or {}
        td = _parse_trading_date(rw.get("trading_date"))
        if td is None and session_range.start is not None:
            # Fallback: local date of start in reference TZ via resolve from UTC date pad.
            td = datetime.fromtimestamp(
                int(session_range.start), tz=timezone.utc
            ).date()
    elif td is None and isinstance(session_range, dict):
        extras = session_range.get("extras") or {}
        rw = extras.get("resolved_window") or {}
        td = _parse_trading_date(rw.get("trading_date"))
        if td is None and session_range.get("start") is not None:
            td = datetime.fromtimestamp(
                int(session_range["start"]), tz=timezone.utc
            ).date()
    if td is None:
        return None
    return resolve_session_window(definition, td)


def find_active_primary_session(
    now_ts: int,
    *,
    lookback_days: int = 3,
) -> Optional[ResolvedSessionWindow]:
    """
    Pick the primary Asia/London window that contains now_ts, else the most
    recently started window that has already begun (utc_start <= now_ts).
    """
    from datetime import timedelta

    if now_ts is None:
        return None
    ref_day = datetime.fromtimestamp(int(now_ts), tz=timezone.utc).date()
    windows: list[ResolvedSessionWindow] = []
    for name, definition in SESSION_DEFINITIONS.items():
        for offset in range(-lookback_days, lookback_days + 1):
            windows.append(
                resolve_session_window(definition, ref_day + timedelta(days=offset))
            )

    containing = [w for w in windows if w.utc_start <= now_ts < w.utc_end]
    if containing:
        containing.sort(key=lambda w: w.utc_start, reverse=True)
        return containing[0]

    started = [w for w in windows if w.utc_start <= now_ts]
    if not started:
        return None
    started.sort(key=lambda w: w.utc_start, reverse=True)
    return started[0]


def build_session_context(
    *,
    setup_session_name: str,
    setup_trading_date: Optional[str],
    session_range: Optional[Union[SessionRange, dict[str, Any]]] = None,
    now_ts: Optional[int] = None,
    opposite_session_sweeps: Sequence[LiquiditySweep] = (),
) -> SessionContext:
    """Build context from setup identity + evaluation clock."""
    setup_window = resolve_setup_window(
        setup_session_name, setup_trading_date, session_range
    )
    active = None
    if now_ts is not None:
        active = find_active_primary_session(int(now_ts))
    return SessionContext(
        setup_session_name=setup_session_name,
        setup_trading_date=setup_trading_date,
        setup_window=setup_window,
        active_session_name=None if active is None else active.session,
        active_trading_date=None if active is None else active.trading_date,
        active_window=active,
        now_ts=now_ts,
        opposite_session_sweeps=tuple(opposite_session_sweeps),
    )


def _bars_after(bars: Sequence[Bar], event_ts: int) -> int:
    """Count OHLC bars strictly after event_ts (bar order by time)."""
    ordered = sorted(bars, key=lambda b: int(b.time))
    return sum(1 for b in ordered if int(b.time) > int(event_ts))


def _new_session_started(
    setup_window: Optional[ResolvedSessionWindow],
    context: SessionContext,
) -> tuple[bool, Optional[str]]:
    """
    Canonical NEW_SESSION_STARTED rule:

    An unfinished setup for primary session window W expires when a later
    primary Asia/London window A has become the active strategy context, where:
      A.utc_start >= W.utc_end
    and (A.session, A.trading_date) != (W.session, W.trading_date).

    Active context comes from DST-aware resolved windows vs now_ts — not
    wall-clock alone.
    """
    if setup_window is None or context.active_window is None:
        return False, None
    active = context.active_window
    if (
        active.session == setup_window.session
        and active.trading_date == setup_window.trading_date
    ):
        return False, None
    if active.utc_start >= setup_window.utc_end:
        detail = (
            f"setup={setup_window.session}@{setup_window.trading_date} "
            f"ended@{setup_window.utc_end}; "
            f"active={active.session}@{active.trading_date} "
            f"started@{active.utc_start}"
        )
        return True, detail
    return False, None


def evaluate_setup_expiry(
    trade_setup: Union[TradeSetup, dict[str, Any]],
    bars: Sequence[Bar],
    session_context: SessionContext,
    expiry_config: Optional[ExpiryConfig] = None,
) -> ExpiryDecision:
    """
    Evaluate whether a TradeSetup should become EXPIRED.

    Does not mutate detectors. Never rescues unreliable setups — only applies
    lifecycle rules to already-computed statuses.
    """
    config = expiry_config or DEFAULT_EXPIRY_CONFIG
    if not config.enabled:
        return ExpiryDecision(expired=False, reason=None, detail="expiry_disabled")

    d = _as_setup_dict(trade_setup)
    status = str(d.get("status") or "")
    if status not in EXPIRABLE_STATUSES:
        return ExpiryDecision(
            expired=False, reason=None, detail=f"status_not_expirable:{status}"
        )

    # --- New session (primary Phase 9 rule) ---
    if config.expire_on_new_session:
        setup_window = session_context.setup_window
        if setup_window is None:
            setup_window = resolve_setup_window(
                str(d.get("session") or session_context.setup_session_name),
                d.get("trading_date") or session_context.setup_trading_date,
                d.get("session_range"),
            )
        fired, detail = _new_session_started(setup_window, session_context)
        if fired:
            return ExpiryDecision(
                expired=True,
                reason=EXPIRY_REASON_NEW_SESSION_STARTED,
                detail=detail,
            )

    # --- Opposite session sweep (optional) ---
    if config.expire_on_opposite_session_sweep and session_context.opposite_session_sweeps:
        setup_window = session_context.setup_window
        for sw in session_context.opposite_session_sweeps:
            if setup_window is not None and int(sw.sweep_timestamp) >= int(
                setup_window.utc_end
            ):
                return ExpiryDecision(
                    expired=True,
                    reason=EXPIRY_REASON_OPPOSITE_LIQUIDITY_EVENT,
                    detail=(
                        f"opposite_sweep={sw.session} {sw.side} @ {sw.sweep_timestamp}"
                    ),
                )

    # --- Bar timeouts (only when thresholds configured) ---
    sweep = d.get("sweep") or {}
    conf = d.get("confirmation") or {}
    fvg = d.get("fvg") or {}

    if (
        status == SetupStatus.WAITING_FOR_CONFIRMATION.value
        and config.max_bars_to_confirmation is not None
        and sweep.get("sweep_timestamp") is not None
    ):
        n = _bars_after(bars, int(sweep["sweep_timestamp"]))
        if n >= int(config.max_bars_to_confirmation):
            return ExpiryDecision(
                expired=True,
                reason=EXPIRY_REASON_CONFIRMATION_TIMEOUT,
                detail=f"bars_after_sweep={n} threshold={config.max_bars_to_confirmation}",
            )

    if (
        status == SetupStatus.WAITING_FOR_FVG.value
        and config.max_bars_to_fvg is not None
        and conf.get("event_timestamp") is not None
    ):
        n = _bars_after(bars, int(conf["event_timestamp"]))
        if n >= int(config.max_bars_to_fvg):
            return ExpiryDecision(
                expired=True,
                reason=EXPIRY_REASON_FVG_TIMEOUT,
                detail=f"bars_after_choch={n} threshold={config.max_bars_to_fvg}",
            )

    if (
        status == SetupStatus.WAITING_FOR_RETRACE.value
        and config.max_bars_to_retrace is not None
    ):
        fvg_ts = fvg.get("created_timestamp") or fvg.get("candle3_timestamp")
        if fvg_ts is not None:
            n = _bars_after(bars, int(fvg_ts))
            if n >= int(config.max_bars_to_retrace):
                return ExpiryDecision(
                    expired=True,
                    reason=EXPIRY_REASON_RETRACE_TIMEOUT,
                    detail=f"bars_after_fvg={n} threshold={config.max_bars_to_retrace}",
                )

    return ExpiryDecision(expired=False, reason=None, detail=None)


def apply_expiry_to_setup(
    setup: TradeSetup,
    decision: ExpiryDecision,
) -> TradeSetup:
    """
    Return a TradeSetup with EXPIRED status if decision.expired.

    Preserves deterministic setup id and all prior fields.
    """
    if not decision.expired:
        return setup

    from setup_explain import explain_setup
    from models import replace_trade_setup

    meta = dict(setup.source_metadata or {})
    meta["expiry_detail"] = decision.detail
    meta["status_before_expiry"] = setup.status

    updated = replace_trade_setup(
        setup,
        status=SetupStatus.EXPIRED.value,
        updated_at=datetime.now(tz=timezone.utc).isoformat(),
        expiry_reason=decision.reason,
        source_metadata=meta,
        explanation="",
    )
    return replace_trade_setup(updated, explanation=explain_setup(updated))

