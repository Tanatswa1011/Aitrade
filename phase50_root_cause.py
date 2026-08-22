"""Phase 50 — static root-cause of Phase 49 funded ruin. No strategy changes."""
from __future__ import annotations

from typing import Any, Sequence

import numpy as np

from phase49_prop_sim import DayBundle, initial_max_loss
from phase50_funded_engine import POINT_USD_MICRO, min_executable


def _loss_streaks(rs: Sequence[float]) -> list[int]:
    out, cur = [], 0
    for r in rs:
        if r < -1e-12:
            cur += 1
        elif cur:
            out.append(cur)
            cur = 0
    if cur:
        out.append(cur)
    return out or [0]


def _max_dd_r(rs: Sequence[float]) -> float:
    eq = peak = 0.0
    dd = 0.0
    for r in rs:
        eq += r
        peak = max(peak, eq)
        dd = max(dd, peak - eq)
    return dd


def analyze_book(book: str, days: Sequence[DayBundle]) -> dict[str, Any]:
    floor = min_executable(book, days)
    rs = []
    daily = []
    for d in days:
        rs.extend(d.r.tolist())
        daily.append(float(np.sum(d.r)))
    streaks = _loss_streaks(rs)
    px = POINT_USD_MICRO[book]
    min_u = floor["min_executable_usd"]
    med_u = floor["median_executable_usd"]
    p95_streak = float(np.percentile(streaks, 95))
    max_streak = int(max(streaks))
    worst_day_r = float(min(daily)) if daily else 0.0
    return {
        "book": book,
        "contract_floor": floor,
        "n_days": len(days),
        "n_trades": len(rs),
        "expectancy_R": float(np.mean(rs)) if rs else None,
        "max_consecutive_losses": max_streak,
        "p90_losing_streak": float(np.percentile(streaks, 90)),
        "p95_losing_streak": p95_streak,
        "max_historical_drawdown_R": _max_dd_r(rs),
        "worst_day_R": worst_day_r,
        "p05_daily_R": float(np.percentile(daily, 5)) if daily else None,
        "dollar_p95_streak_at_min_micro": p95_streak * min_u,
        "dollar_max_streak_at_min_micro": max_streak * min_u,
        "dollar_max_dd_at_median_micro": _max_dd_r(rs) * med_u,
        "dollar_worst_day_at_median_micro": abs(min(0.0, worst_day_r)) * med_u,
        "trades_per_day_mean": (len(rs) / len(days)) if days else None,
    }


def phase49_failure_narrative(book: str, profile_id: str, stats: dict[str, Any]) -> dict[str, Any]:
    ml = initial_max_loss(profile_id, "FUNDED")
    is_mffu = profile_id.startswith("MFFU")
    phase49_post_payout_cushion = 0.25 * ml  # Phase 49 leftover after ASAP payout
    lock = 100.0 if is_mffu else 100.0  # FN lock is equity 50100 → cushion vs 50100
    drivers = []
    p95_usd = stats["dollar_p95_streak_at_min_micro"]
    max_dd_usd = stats["dollar_max_dd_at_median_micro"]
    worst_day = stats["dollar_worst_day_at_median_micro"]
    min_u = stats["contract_floor"]["min_executable_usd"]
    if min_u > phase49_post_payout_cushion:
        drivers.append("contract-floor risk")
    if p95_usd > phase49_post_payout_cushion:
        drivers.append("strategy losing streaks")
    if max_dd_usd > ml:
        drivers.append("historical drawdown clustering")
    drivers.append("trailing MLL behavior")
    drivers.append("post-payout account cushion")
    drivers.append("payout size")
    drivers.append("payout frequency")
    drivers.append("fixed-risk policy")
    if (stats.get("trades_per_day_mean") or 0) > 2:
        drivers.append("trade frequency")
        drivers.append("daily clustering")
    drivers.append("commissions/slippage")
    primary = "post-payout account cushion"
    if p95_usd > phase49_post_payout_cushion and min_u * 3 > phase49_post_payout_cushion:
        primary = "post-payout cushion vs contract-floor × losing-streak"
    return {
        "book": book,
        "profile": profile_id,
        "phase49_leftover_cushion_usd": phase49_post_payout_cushion,
        "locked_mll_pnl_mffu": 100 if is_mffu else None,
        "fundednext_lock_equity": 50100 if not is_mffu else None,
        "min_executable_usd": min_u,
        "min_exec_over_phase49_cushion": min_u / phase49_post_payout_cushion,
        "p95_streak_usd_over_cushion": p95_usd / phase49_post_payout_cushion,
        "max_dd_usd_at_median_micro": max_dd_usd,
        "worst_day_usd_at_median_micro": worst_day,
        "primary_reason": primary,
        "drivers_ranked": drivers,
        "mechanics_note": (
            "Phase 49 withdrew surplus down to MLL + 25% of initial max-loss. "
            "After MFFU lock at +$100 that leftover is small versus one micro and ordinary streaks. "
            "Fixed risk of initial-DD fraction does not shrink after payout. "
            "Long 504-day horizon then samples enough streaks to breach."
        ),
    }
