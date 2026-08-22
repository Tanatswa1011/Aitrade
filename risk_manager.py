"""Risk Manager — Phase 52 prop contract qty. Still DRY_RUN. No broker."""
from __future__ import annotations

from typing import Any

from aitrade_operating_policy import load_operating_policy
from phase52_policy import FAST_QTY, MAX_LOSS, allowed_qty, session_daily_stop_threshold


def propose_size(
    **kwargs: Any,
) -> dict[str, Any]:
    policy = load_operating_policy()
    rpt = policy.numerics.get("risk_per_trade")
    if not isinstance(rpt, dict) or rpt.get("mode") != "PROP_CONTRACT_QTY":
        return {
            "status": "SIZE_PENDING_SIMULATION",
            "quantity": None,
            "risk_per_trade": rpt,
            "execution_default": policy.execution_default,
            "broker_execution": False,
            "note": "No prop contract-qty lock loaded.",
        }
    remaining_dd = float(kwargs.get("remaining_dd") or MAX_LOSS)
    daily_capacity = kwargs.get("daily_capacity")
    if daily_capacity is None:
        daily_capacity = session_daily_stop_threshold(remaining_dd)
    requested = int(kwargs.get("requested_qty") or kwargs.get("proposed_quantity") or FAST_QTY)
    state = str(kwargs.get("state") or "EVAL_FAST")
    q, why = allowed_qty(
        state=state,
        requested=requested,
        remaining_dd=remaining_dd,
        daily_capacity=float(daily_capacity),
        demoted=bool(kwargs.get("demoted") or False),
        consecutive_losses=int(kwargs.get("consecutive_losses") or 0),
        last_qty=int(kwargs.get("last_qty") or FAST_QTY),
    )
    return {
        "status": "PROP_QTY_LOCKED" if q > 0 else why,
        "quantity": int(q) if q > 0 else 0,
        "why": why,
        "risk_per_trade": rpt,
        "execution_default": policy.execution_default,
        "broker_execution": False,
        "note": "FundedNext/NQ Phase 52 contract qty. DRY_RUN. 3 MNQ rejected. Not 1% of $50k.",
    }
