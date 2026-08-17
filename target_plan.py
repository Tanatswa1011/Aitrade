"""Pure target planning: fixed RR + same-session opposite liquidity."""

from __future__ import annotations

from typing import Optional

from models import (
    EntryCandidate,
    FixedRRTarget,
    LiquiditySweep,
    RiskPlan,
    SessionRange,
    StructureDirection,
    SweepSide,
    TargetConfig,
    TargetPlan,
)


def opposite_liquidity_target(
    session_range: SessionRange,
    sweep: LiquiditySweep,
) -> tuple[Optional[str], Optional[float], Optional[str]]:
    """
    Same-session opposite side.

    Low swept → session High; High swept → session Low.
    Returns (label, price, error).
    """
    name = session_range.name or sweep.session
    if sweep.side in (SweepSide.LOW.value, "low"):
        if session_range.high is None:
            return None, None, "missing_session_high"
        return f"{name} High", float(session_range.high), None
    if sweep.side in (SweepSide.HIGH.value, "high"):
        if session_range.low is None:
            return None, None, "missing_session_low"
        return f"{name} Low", float(session_range.low), None
    return None, None, "unknown_sweep_side"


def rr_to_target(
    *,
    direction: str,
    entry_price: float,
    stop_price: float,
    target_price: float,
) -> tuple[Optional[float], Optional[float], bool]:
    """Return (reward, rr, valid)."""
    if direction == StructureDirection.BULLISH.value:
        risk = entry_price - stop_price
        reward = target_price - entry_price
    elif direction == StructureDirection.BEARISH.value:
        risk = stop_price - entry_price
        reward = entry_price - target_price
    else:
        return None, None, False

    if risk <= 0 or reward <= 0:
        return reward, None, False
    return reward, reward / risk, True


def build_target_plan(
    session_range: SessionRange,
    sweep: LiquiditySweep,
    entry_candidate: EntryCandidate,
    risk_plan: RiskPlan,
    config: Optional[TargetConfig] = None,
) -> TargetPlan:
    """
    Build fixed-RR and opposite-liquidity targets for a RiskPlan.

    Requires a valid RiskPlan with positive risk_distance.
    """
    cfg = config or TargetConfig()
    setup_ref = dict(risk_plan.setup_reference or {})

    def _empty(reason: str, **extra) -> TargetPlan:
        return TargetPlan(
            fixed_rr_targets=[],
            opposite_liquidity=bool(cfg.use_opposite_liquidity),
            opposite_liquidity_label=None,
            opposite_liquidity_price=None,
            rr_to_opposite=None,
            opposite_target_valid=False,
            valid=False,
            setup_reference=setup_ref,
            extras={"reason": reason, "config": cfg.to_dict(), **extra},
        )

    if not risk_plan.valid:
        return _empty(
            "risk_plan_invalid",
            risk_invalidation_reason=risk_plan.invalidation_reason,
        )

    if risk_plan.risk_distance is None or risk_plan.risk_distance <= 0:
        return _empty("non_positive_risk_distance")

    if risk_plan.stop_price is None:
        return _empty("missing_stop_price")

    entry = float(risk_plan.entry_price)
    stop = float(risk_plan.stop_price)
    risk = float(risk_plan.risk_distance)
    direction = risk_plan.direction

    fixed: list[FixedRRTarget] = []
    for rr in cfg.fixed_rr:
        r = float(rr)
        if r <= 0:
            continue
        distance = risk * r
        if direction == StructureDirection.BULLISH.value:
            price = entry + distance
        else:
            price = entry - distance
        fixed.append(FixedRRTarget(rr=r, price=price, distance=distance))

    opp_label = None
    opp_price = None
    rr_opp = None
    opp_valid = False
    opp_extras: dict = {}

    if cfg.use_opposite_liquidity:
        if cfg.opposite_liquidity_mode != "same_session":
            opp_extras["opposite_mode_error"] = (
                f"unsupported_mode:{cfg.opposite_liquidity_mode}"
            )
        else:
            label, price, err = opposite_liquidity_target(session_range, sweep)
            if err or price is None:
                opp_extras["opposite_error"] = err or "missing_price"
            else:
                opp_label = label
                opp_price = float(price)
                reward, rr_opp, opp_valid = rr_to_target(
                    direction=direction,
                    entry_price=entry,
                    stop_price=stop,
                    target_price=opp_price,
                )
                opp_extras["reward_to_opposite"] = reward
                if not opp_valid:
                    opp_extras["opposite_invalid_reason"] = (
                        "non_positive_reward_or_risk"
                    )

    # Fixed RR list alone is enough for a valid target plan when risk is valid.
    plan_valid = len(fixed) > 0

    return TargetPlan(
        fixed_rr_targets=fixed,
        opposite_liquidity=bool(cfg.use_opposite_liquidity),
        opposite_liquidity_label=opp_label,
        opposite_liquidity_price=opp_price,
        rr_to_opposite=rr_opp,
        opposite_target_valid=opp_valid,
        valid=plan_valid,
        setup_reference=setup_ref,
        extras={
            "config": cfg.to_dict(),
            "entry_mode": entry_candidate.mode,
            "risk_distance": risk,
            **opp_extras,
        },
    )
