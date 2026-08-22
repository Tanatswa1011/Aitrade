"""Account-state scaffolding for EVALUATION and FUNDED. No risk-per-trade numbers."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional

from prop_rules_v1 import (
    REQUIRES_CONFIRMATION,
    AccountMetrics,
    FirmProfile,
    adjusted_required_profit,
    calendar_days_inactive,
    is_none_policy,
    is_unknown,
    load_profile,
    mffu_payout_unlocked,
)

EVAL_STATES = (
    "EVAL_NORMAL",
    "EVAL_ACCELERATE",
    "EVAL_DEFENSIVE",
    "EVAL_TARGET_APPROACH",
    "EVAL_LOCKOUT",
    "PASSED",
)
FUNDED_STATES = (
    "FUNDED_BUFFER_BUILD",
    "FUNDED_NORMAL",
    "FUNDED_PAYOUT_APPROACH",
    "FUNDED_DEFENSIVE",
    "FUNDED_LOCKOUT",
)


@dataclass
class AccountSnapshot:
    firm_profile: str
    account_stage: str
    state: str
    lockout_reason: Optional[str] = None
    notes: list[str] | None = None
    extras: dict[str, Any] | None = None


def _eval_required_profit(profile: FirmProfile, metrics: AccountMetrics) -> Optional[float]:
    stage = profile.stage("EVALUATION")
    base = stage.get("profit_target")
    if is_unknown(base) or is_none_policy(base) or base is None:
        return None
    base_f = float(base)
    ratio = stage.get("consistency_ratio_max")
    best = metrics.highest_profitable_day
    if ratio is None or is_unknown(ratio) or best is None:
        return base_f
    return adjusted_required_profit(base_target=base_f, highest_profitable_day=float(best), ratio_max=float(ratio))


def inactivity_breached(profile: FirmProfile, account_stage: str, metrics: AccountMetrics, now: datetime) -> bool:
    stage = profile.stage(account_stage)
    days_allowed = stage.get("inactivity_days")
    if is_unknown(days_allowed) or is_none_policy(days_allowed) or days_allowed is None:
        return False
    elapsed = calendar_days_inactive(now, metrics.last_trade_timestamp)
    if elapsed is None:
        return False
    return int(elapsed) >= int(days_allowed)


def drawdown_breached(metrics: AccountMetrics) -> bool:
    if metrics.remaining_drawdown is not None and metrics.remaining_drawdown <= 0:
        return True
    if metrics.mll_locked and metrics.realized_pnl is not None and metrics.current_mll is not None:
        if metrics.realized_pnl < metrics.current_mll:
            return True
    if metrics.current_equity is not None and metrics.current_mll is not None:
        if metrics.current_equity < metrics.current_mll:
            return True
    return False


def classify_account_state(
    *,
    firm_profile: str | FirmProfile,
    account_stage: str,
    metrics: AccountMetrics,
    now: Optional[datetime] = None,
    explicit_state: Optional[str] = None,
) -> AccountSnapshot:
    profile = firm_profile if isinstance(firm_profile, FirmProfile) else load_profile(str(firm_profile))
    stage = str(account_stage).upper()
    notes: list[str] = []
    now = now or datetime.now()

    if inactivity_breached(profile, stage, metrics, now):
        lock_state = "EVAL_LOCKOUT" if stage in ("EVALUATION", "CHALLENGE") else "FUNDED_LOCKOUT"
        return AccountSnapshot(profile.profile_id, stage, lock_state, lockout_reason="BLOCK_INACTIVITY", notes=["inactivity_breach"])
    if drawdown_breached(metrics):
        lock_state = "EVAL_LOCKOUT" if stage in ("EVALUATION", "CHALLENGE") else "FUNDED_LOCKOUT"
        return AccountSnapshot(profile.profile_id, stage, lock_state, lockout_reason="BLOCK_DRAWDOWN", notes=["drawdown_or_mll_breach"])

    if stage in ("EVALUATION", "CHALLENGE"):
        required = _eval_required_profit(profile, metrics)
        min_days = profile.stage("EVALUATION").get("minimum_trading_days")
        days_ok = True
        if min_days is not None and not is_none_policy(min_days) and not is_unknown(min_days):
            days_ok = (metrics.trading_days or 0) >= int(min_days)
        if required is not None and metrics.realized_pnl is not None and metrics.realized_pnl >= required and days_ok:
            return AccountSnapshot(profile.profile_id, stage, "PASSED", notes=["evaluation_target_met"])
        state = "EVAL_NORMAL"
        init_dd = None
        if metrics.extras:
            init_dd = metrics.extras.get("initial_drawdown")
        if (
            metrics.distance_to_target is not None
            and metrics.remaining_drawdown is not None
            and metrics.distance_to_target > 0
            and metrics.distance_to_target <= metrics.remaining_drawdown
        ):
            state = "EVAL_TARGET_APPROACH"
            notes.append("distance_to_target_within_remaining_drawdown")
        elif (
            init_dd is not None
            and float(init_dd) > 0
            and metrics.remaining_drawdown is not None
            and (metrics.remaining_drawdown / float(init_dd)) >= 0.70
            and (metrics.realized_pnl or 0.0) >= 0.0
            and (metrics.consecutive_losses or 0) == 0
        ):
            state = "EVAL_ACCELERATE"
            notes.append("healthy_drawdown_cushion_allows_acceleration")
        if (
            metrics.remaining_drawdown is not None
            and metrics.open_pnl is not None
            and metrics.remaining_drawdown > 0
            and metrics.remaining_drawdown <= abs(metrics.open_pnl)
        ):
            state = "EVAL_DEFENSIVE"
            notes.append("open_pnl_consumes_remaining_drawdown")
        if explicit_state in EVAL_STATES and explicit_state not in ("EVAL_LOCKOUT", "PASSED"):
            state = explicit_state
        return AccountSnapshot(profile.profile_id, stage, state, notes=notes)

    if stage in ("FUNDED", "SIM_FUNDED"):
        payout = profile.payout()
        first_buf = payout.get("first_payout_required_buffer")
        state = "FUNDED_NORMAL"
        if not metrics.first_payout_completed:
            state = "FUNDED_BUFFER_BUILD"
            notes.append("first_payout_not_completed")
        elif (
            metrics.net_profit_since_last_payout is not None
            and metrics.remaining_drawdown is not None
            and not is_unknown(payout.get("subsequent_payout_profit_required"))
            and not is_none_policy(payout.get("subsequent_payout_profit_required"))
        ):
            remaining_payout = float(payout["subsequent_payout_profit_required"]) - float(metrics.net_profit_since_last_payout)
            if remaining_payout > 0 and remaining_payout <= metrics.remaining_drawdown:
                state = "FUNDED_PAYOUT_APPROACH"
                notes.append("payout_unlock_within_remaining_drawdown")
        if (
            metrics.remaining_drawdown is not None
            and metrics.open_pnl is not None
            and metrics.remaining_drawdown > 0
            and metrics.remaining_drawdown <= abs(metrics.open_pnl)
        ):
            state = "FUNDED_DEFENSIVE"
            notes.append("open_pnl_consumes_remaining_drawdown")
        if explicit_state in FUNDED_STATES and explicit_state != "FUNDED_LOCKOUT":
            state = explicit_state
        extras = {}
        if profile.profile_id == "MFFU_RAPID_EOD_50K" and metrics.realized_pnl is not None:
            extras["mffu_payout_unlocked"] = mffu_payout_unlocked(
                realized_pnl=metrics.realized_pnl,
                first_payout_completed=metrics.first_payout_completed,
                net_profit_since_last_payout=float(metrics.net_profit_since_last_payout or 0.0),
            )
            extras["first_payout_required_buffer"] = first_buf
        return AccountSnapshot(profile.profile_id, stage, state, notes=notes, extras=extras)

    return AccountSnapshot(
        profile.profile_id,
        stage,
        REQUIRES_CONFIRMATION,
        notes=["unknown_account_stage"],
    )
