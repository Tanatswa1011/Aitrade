"""Phase 49 — PROP_RULES_V1 account-path simulator. Research only. No martingale."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Optional, Sequence
from zoneinfo import ZoneInfo

import numpy as np

from family_port_engine import INSTRUMENTS
from prop_rules_v1 import (
    REQUIRES_CONFIRMATION,
    adjusted_required_profit,
    load_profile,
    mffu_payout_unlocked,
    trail_eod_mll_equity,
    trail_eod_mll_pnl,
)

NY = ZoneInfo("America/New_York")
CHICAGO = ZoneInfo("America/Chicago")

EVAL_FRACS = (0.05, 0.075, 0.10, 0.125, 0.15, 0.175, 0.20)
FUNDED_FRACS = (0.025, 0.05, 0.075, 0.10, 0.125)
GOVERNORS = ("none", "soft", "hard", "reduced")
N_PATHS_DEFAULT = 10_000
EVAL_HORIZON = 252
FUNDED_HORIZON = 504
SEED = 49

MICRO_SYMBOL = {"NQ": "MNQ", "ES": "MES", "GC": "MGC"}
POINT_USD_MICRO = {"NQ": 2.0, "ES": 5.0, "GC": 10.0}
DEFAULT_STOP = {"NQ": 80.0, "ES": 18.0, "GC": None}

# Typical winner R used by governors to estimate whether another trade would expand the target.
TYPICAL_WIN_R = {"NQ": 0.50, "ES": 0.50, "GC": 2.0}


@dataclass
class DayBundle:
    trading_date: str
    r: np.ndarray
    risk_points: np.ndarray


@dataclass
class PolicySpec:
    name: str
    governor: str = "none"
    # Evaluation state scales — never > 1 after a loss (no martingale).
    defensive_dd_ratio: Optional[float] = None
    defensive_scale: float = 0.5
    loss_streak_trigger: Optional[int] = None
    target_approach_mult: Optional[float] = None  # scale if distance_to_target < mult * risk_usd
    # Funded
    buffer_scale: float = 1.0
    payout_approach_scale: float = 1.0
    after_payout_scale: float = 1.0
    funded_defensive_dd_ratio: Optional[float] = None


FIXED = PolicySpec(name="FIXED")


def assert_no_martingale(policy: PolicySpec) -> None:
    for attr in (
        "defensive_scale",
        "buffer_scale",
        "payout_approach_scale",
        "after_payout_scale",
    ):
        if float(getattr(policy, attr)) > 1.0 + 1e-12:
            raise ValueError(f"martingale_forbidden:{attr}")


def stage_rules(profile_id: str, stage: str) -> dict[str, Any]:
    prof = load_profile(profile_id)
    st = prof.stage(stage)
    pay = prof.payout()
    return {"stage": st.raw, "payout": pay, "profile": prof}


def initial_max_loss(profile_id: str, stage: str) -> float:
    raw = load_profile(profile_id).stage(stage).raw
    ml = raw.get("max_loss")
    if ml is None:
        raise ValueError(f"missing_max_loss:{profile_id}:{stage}")
    return float(ml)


def max_micros(profile_id: str, stage: str) -> int:
    raw = load_profile(profile_id).stage(stage).raw
    return int(raw.get("max_micros") or 30)


def size_qty(book: str, risk_usd: float, risk_points: float, cap_micros: int) -> tuple[int, float]:
    """Map theoretical dollar risk onto executable micros. qty=0 means skip."""
    px = POINT_USD_MICRO[book]
    per = float(risk_points) * float(px)
    if per <= 0 or risk_usd <= 0:
        return 0, 0.0
    qty = int(risk_usd // per)
    qty = max(0, min(int(cap_micros), qty))
    return qty, qty * per


def trades_to_days(trades: Sequence[dict[str, Any]], book: str) -> list[DayBundle]:
    by: dict[str, list[tuple[float, float]]] = {}
    default_stop = DEFAULT_STOP[book]
    for t in trades:
        r = t.get("r_multiple")
        d = t.get("trading_date")
        if r is None or not d or d == "UNAVAILABLE":
            continue
        rp = t.get("risk_points")
        if rp in (None, "", "UNAVAILABLE"):
            rp = default_stop
        if rp is None:
            continue
        by.setdefault(str(d), []).append((float(r), float(rp)))
    days = []
    for d in sorted(by):
        arr = by[d]
        days.append(
            DayBundle(
                trading_date=d,
                r=np.array([a[0] for a in arr], dtype=np.float64),
                risk_points=np.array([a[1] for a in arr], dtype=np.float64),
            )
        )
    return days


def _scale_for_eval(
    policy: PolicySpec,
    *,
    remaining_dd: float,
    initial_dd: float,
    consec_losses: int,
    distance_to_target: float,
    risk_usd: float,
) -> float:
    scale = 1.0
    if policy.defensive_dd_ratio is not None and initial_dd > 0:
        if (remaining_dd / initial_dd) <= float(policy.defensive_dd_ratio):
            scale = min(scale, float(policy.defensive_scale))
    if policy.loss_streak_trigger is not None and consec_losses >= int(policy.loss_streak_trigger):
        scale = min(scale, float(policy.defensive_scale))
    if policy.target_approach_mult is not None and risk_usd > 0:
        if distance_to_target <= float(policy.target_approach_mult) * risk_usd:
            scale = min(scale, float(policy.defensive_scale))
    return max(0.0, min(1.0, scale))


def _scale_for_funded(
    policy: PolicySpec,
    *,
    first_payout_done: bool,
    remaining_dd: float,
    initial_dd: float,
    distance_to_payout: float,
    risk_usd: float,
    consec_losses: int,
) -> float:
    scale = 1.0
    if not first_payout_done:
        scale = min(scale, float(policy.buffer_scale))
    else:
        scale = min(scale, float(policy.after_payout_scale))
    if policy.funded_defensive_dd_ratio is not None and initial_dd > 0:
        if (remaining_dd / initial_dd) <= float(policy.funded_defensive_dd_ratio):
            scale = min(scale, float(policy.defensive_scale))
    if policy.loss_streak_trigger is not None and consec_losses >= int(policy.loss_streak_trigger):
        scale = min(scale, float(policy.defensive_scale))
    if first_payout_done is False and policy.payout_approach_scale < 1.0 and risk_usd > 0:
        if distance_to_payout <= 2.0 * risk_usd:
            scale = min(scale, float(policy.payout_approach_scale))
    return max(0.0, min(1.0, scale))


def _governor_skip(
    governor: str,
    *,
    book: str,
    day_pnl: float,
    best_day: float,
    pnl: float,
    base_target: float,
    ratio_max: float,
    next_risk: float,
) -> str:
    """Return '' to take trade, else skip reason. Does not change signals."""
    if governor == "none" or ratio_max <= 0 or next_risk <= 0:
        return ""
    typical = TYPICAL_WIN_R.get(book, 1.0) * next_risk
    projected_day = day_pnl + typical
    projected_best = max(best_day, projected_day, 0.0)
    projected_total = max(pnl + typical, 0.0)
    cap_day = ratio_max * base_target
    if governor == "hard":
        if day_pnl >= cap_day - 1e-9:
            return "hard_day_cap"
        if projected_day > cap_day + 1e-9 and day_pnl > 0:
            return "hard_would_exceed_cap"
        return ""
    if governor == "soft":
        if projected_total >= 0.5 * base_target and projected_best > ratio_max * max(projected_total, 1e-9) + 1e-12:
            if day_pnl > 0:
                return "soft_consistency"
        return ""
    if governor == "reduced":
        return ""  # size cut applied elsewhere
    return ""


def _reduced_mult(governor: str, day_pnl: float, base_target: float, ratio_max: float) -> float:
    if governor != "reduced" or ratio_max <= 0:
        return 1.0
    cap = ratio_max * base_target
    if cap <= 0:
        return 1.0
    if day_pnl >= 0.75 * cap:
        return 0.25
    if day_pnl >= 0.40 * cap:
        return 0.5
    return 1.0


def simulate_eval_path(
    days: Sequence[DayBundle],
    *,
    book: str,
    profile_id: str,
    dd_frac: float,
    policy: PolicySpec,
    rng: np.random.Generator,
    mode: str,
    horizon: int = EVAL_HORIZON,
) -> dict[str, Any]:
    assert_no_martingale(policy)
    st = load_profile(profile_id).stage("EVALUATION").raw
    max_loss = float(st["max_loss"])
    base_target = float(st["profit_target"])
    ratio_max = float(st.get("consistency_ratio_max") or 0.0)
    min_days = st.get("minimum_trading_days")
    min_days_i = 0 if min_days in (None, "NONE") else int(min_days)
    cap = max_micros(profile_id, "EVALUATION")
    n_hist = len(days)
    if n_hist == 0:
        return {"terminal": "NO_DATA"}

    is_fn = profile_id.startswith("FUNDEDNEXT")
    start_eq = float(st.get("nominal_account_size") or 50000)
    pnl = 0.0
    equity = start_eq
    mll_pnl = -max_loss
    mll_eq = start_eq - max_loss
    locked = False
    lock_at = st.get("mll_locks_at")
    best_day = 0.0
    trading_days = 0
    trades_n = 0
    max_dd = 0.0
    consec_loss = 0
    consistency_delayed = False
    skipped = 0
    sized_zero = 0
    idx_cursor = 0

    for step in range(int(horizon)):
        if mode == "bootstrap":
            i = int(rng.integers(0, n_hist))
        else:
            if idx_cursor >= n_hist:
                return _eval_timeout(
                    pnl, equity, start_eq, is_fn, trading_days, trades_n, max_dd, consistency_delayed, skipped, sized_zero, base_target, best_day, ratio_max
                )
            i = idx_cursor
            idx_cursor += 1
        day = days[i]
        remaining = (equity - mll_eq) if is_fn else (pnl - mll_pnl)
        max_dd = max(max_dd, max_loss - remaining)
        dist_target = max(0.0, base_target - (equity - start_eq if is_fn else pnl))
        day_pnl = 0.0
        took = False
        for r, rp in zip(day.r.tolist(), day.risk_points.tolist()):
            remaining = (equity - mll_eq) if is_fn else (pnl - mll_pnl)
            if remaining <= 0:
                break
            risk_budget = float(dd_frac) * max_loss
            scale = _scale_for_eval(
                policy,
                remaining_dd=remaining,
                initial_dd=max_loss,
                consec_losses=consec_loss,
                distance_to_target=dist_target,
                risk_usd=risk_budget,
            )
            scale *= _reduced_mult(policy.governor, day_pnl, base_target, ratio_max)
            risk_usd = risk_budget * scale
            skip = _governor_skip(
                policy.governor,
                book=book,
                day_pnl=day_pnl,
                best_day=best_day,
                pnl=(equity - start_eq if is_fn else pnl),
                base_target=base_target,
                ratio_max=ratio_max,
                next_risk=risk_usd,
            )
            if skip:
                skipped += 1
                continue
            qty, actual = size_qty(book, risk_usd, rp, cap)
            if qty <= 0:
                sized_zero += 1
                continue
            pnl_t = float(r) * actual
            pnl += pnl_t
            equity += pnl_t
            day_pnl += pnl_t
            trades_n += 1
            took = True
            dist_target = max(0.0, base_target - (equity - start_eq if is_fn else pnl))
            if r < 0:
                consec_loss += 1
            else:
                consec_loss = 0
            remaining = (equity - mll_eq) if is_fn else (pnl - mll_pnl)
            if remaining <= 0:
                return {
                    "terminal": "BREACH",
                    "days": trading_days + (1 if took else 0),
                    "trades": trades_n,
                    "max_dd": max_dd,
                    "consistency_delayed": consistency_delayed,
                    "adjusted_target": adjusted_required_profit(base_target=base_target, highest_profitable_day=max(best_day, day_pnl), ratio_max=ratio_max) if ratio_max else base_target,
                    "skipped": skipped,
                    "sized_zero": sized_zero,
                    "pnl": pnl if not is_fn else equity - start_eq,
                }
        if took:
            trading_days += 1
        best_day = max(best_day, day_pnl)
        if is_fn:
            mll_eq, locked = trail_eod_mll_equity(
                eod_equity=equity,
                previous_mll=mll_eq,
                locked=locked,
                lock_at=float(lock_at) if lock_at not in (None, REQUIRES_CONFIRMATION) else 50100.0,
                distance=max_loss,
            )
            if equity <= mll_eq + 1e-9:
                return {
                    "terminal": "BREACH",
                    "days": trading_days,
                    "trades": trades_n,
                    "max_dd": max_dd,
                    "consistency_delayed": consistency_delayed,
                    "adjusted_target": adjusted_required_profit(base_target=base_target, highest_profitable_day=best_day, ratio_max=ratio_max) if ratio_max else base_target,
                    "skipped": skipped,
                    "sized_zero": sized_zero,
                    "pnl": equity - start_eq,
                }
            realized = equity - start_eq
        else:
            mll_pnl = max(mll_pnl, pnl - max_loss)
            if pnl <= mll_pnl + 1e-9:
                return {
                    "terminal": "BREACH",
                    "days": trading_days,
                    "trades": trades_n,
                    "max_dd": max_dd,
                    "consistency_delayed": consistency_delayed,
                    "adjusted_target": adjusted_required_profit(base_target=base_target, highest_profitable_day=best_day, ratio_max=ratio_max) if ratio_max else base_target,
                    "skipped": skipped,
                    "sized_zero": sized_zero,
                    "pnl": pnl,
                }
            realized = pnl
        adj = adjusted_required_profit(base_target=base_target, highest_profitable_day=best_day, ratio_max=ratio_max) if ratio_max else base_target
        if adj > base_target + 1e-9 and realized >= base_target:
            consistency_delayed = True
        if realized + 1e-9 >= adj and trading_days >= min_days_i:
            return {
                "terminal": "PASS",
                "days": trading_days,
                "trades": trades_n,
                "max_dd": max_dd,
                "consistency_delayed": consistency_delayed,
                "adjusted_target": adj,
                "skipped": skipped,
                "sized_zero": sized_zero,
                "pnl": realized,
            }
    return _eval_timeout(pnl, equity, start_eq, is_fn, trading_days, trades_n, max_dd, consistency_delayed, skipped, sized_zero, base_target, best_day, ratio_max)


def _eval_timeout(pnl, equity, start_eq, is_fn, trading_days, trades_n, max_dd, consistency_delayed, skipped, sized_zero, base_target, best_day, ratio_max):
    adj = adjusted_required_profit(base_target=base_target, highest_profitable_day=best_day, ratio_max=ratio_max) if ratio_max else base_target
    return {
        "terminal": "TIMEOUT",
        "days": trading_days,
        "trades": trades_n,
        "max_dd": max_dd,
        "consistency_delayed": consistency_delayed,
        "adjusted_target": adj,
        "skipped": skipped,
        "sized_zero": sized_zero,
        "pnl": (equity - start_eq) if is_fn else pnl,
    }


def simulate_funded_path(
    days: Sequence[DayBundle],
    *,
    book: str,
    profile_id: str,
    dd_frac: float,
    policy: PolicySpec,
    rng: np.random.Generator,
    mode: str,
    horizon: int = FUNDED_HORIZON,
    payout_cushion_frac: float = 0.25,
) -> dict[str, Any]:
    assert_no_martingale(policy)
    prof = load_profile(profile_id)
    st = prof.stage("FUNDED").raw
    pay = prof.payout()
    max_loss = float(st["max_loss"])
    cap = max_micros(profile_id, "FUNDED")
    n_hist = len(days)
    if n_hist == 0:
        return {"terminal": "NO_DATA"}

    is_mffu = profile_id.startswith("MFFU")
    start_eq = 50000.0
    pnl = 0.0
    equity = start_eq
    mll_pnl = -max_loss
    mll_eq = start_eq - max_loss
    locked = False
    lock_pnl = float(st.get("mll_lock_level") or 100.0)
    lock_eq = float(st.get("mll_locks_at") or 50100.0)
    first_done = False
    since_payout = 0.0
    payouts = 0
    trader_payout = 0.0
    benchmark_days = 0
    consec_loss = 0
    ruin = False
    idx_cursor = 0
    days_elapsed = 0
    first_payout_day = None
    share = float(pay.get("trader_profit_share") or pay.get("reward_share") or (0.9 if is_mffu else 0.95))
    first_buf = pay.get("first_payout_required_buffer")
    subsequent = pay.get("subsequent_payout_profit_required")
    min_po = pay.get("minimum_payout")
    max_wd = pay.get("maximum_withdrawal")
    bench_need = int(st.get("benchmark_days_required") or 0) if st.get("benchmark_days_required") not in (None, "NONE") else 0
    bench_min = float(st.get("benchmark_day_min_profit_50k") or 0.0)
    first_buf_f = float(first_buf) if first_buf not in (None, REQUIRES_CONFIRMATION, "NONE") else None
    subsequent_f = float(subsequent) if subsequent not in (None, REQUIRES_CONFIRMATION, "NONE") else None
    min_po_f = float(min_po) if min_po not in (None, REQUIRES_CONFIRMATION, "NONE") else 0.0
    max_wd_f = float(max_wd) if max_wd not in (None, REQUIRES_CONFIRMATION, "NONE", "NONE_STATED") else None

    for step in range(int(horizon)):
        if mode == "bootstrap":
            i = int(rng.integers(0, n_hist))
        else:
            if idx_cursor >= n_hist:
                break
            i = idx_cursor
            idx_cursor += 1
        day = days[i]
        days_elapsed += 1
        remaining = (equity - mll_eq) if not is_mffu else (pnl - mll_pnl)
        if remaining <= 0:
            ruin = True
            break
        day_pnl = 0.0
        dist_pay = 0.0
        if is_mffu and first_buf_f is not None and not first_done:
            dist_pay = max(0.0, first_buf_f - pnl)
        for r, rp in zip(day.r.tolist(), day.risk_points.tolist()):
            remaining = (equity - mll_eq) if not is_mffu else (pnl - mll_pnl)
            risk_budget = float(dd_frac) * max_loss
            scale = _scale_for_funded(
                policy,
                first_payout_done=first_done,
                remaining_dd=remaining,
                initial_dd=max_loss,
                distance_to_payout=dist_pay,
                risk_usd=risk_budget,
                consec_losses=consec_loss,
            )
            qty, actual = size_qty(book, risk_budget * scale, rp, cap)
            if qty <= 0:
                continue
            pnl_t = float(r) * actual
            pnl += pnl_t
            equity += pnl_t
            since_payout += pnl_t
            day_pnl += pnl_t
            if r < 0:
                consec_loss += 1
            else:
                consec_loss = 0
            remaining = (equity - mll_eq) if not is_mffu else (pnl - mll_pnl)
            if remaining <= 0:
                ruin = True
                break
        if ruin:
            break
        if not is_mffu and day_pnl >= bench_min - 1e-12:
            benchmark_days += 1
        cushion = payout_cushion_frac * max_loss
        if is_mffu:
            mll_pnl, locked = trail_eod_mll_pnl(
                eod_pnl_high=pnl,
                previous_mll=mll_pnl,
                locked=locked,
                lock_level=lock_pnl,
                distance=max_loss,
            )
            if pnl <= mll_pnl + 1e-9:
                ruin = True
                break
            unlocked = mffu_payout_unlocked(
                realized_pnl=pnl,
                first_payout_completed=first_done,
                net_profit_since_last_payout=since_payout,
                first_buffer=first_buf_f or 2100.0,
                subsequent=subsequent_f or 500.0,
            )
            available = pnl - mll_pnl - cushion
            if unlocked and available >= max(min_po_f, 1.0):
                amt = available
                if max_wd_f is not None:
                    amt = min(amt, max_wd_f)
                pnl -= amt
                equity -= amt
                since_payout = 0.0
                trader_payout += amt * share
                payouts += 1
                if not first_done:
                    first_done = True
                    first_payout_day = days_elapsed
        else:
            mll_eq, locked = trail_eod_mll_equity(
                eod_equity=equity,
                previous_mll=mll_eq,
                locked=locked,
                lock_at=lock_eq,
                distance=max_loss,
            )
            if equity <= mll_eq + 1e-9:
                ruin = True
                break
            available = equity - mll_eq - cushion
            bench_ok = bench_need <= 0 or benchmark_days >= bench_need
            if bench_ok and available >= max(min_po_f, 1.0) and (first_done or True):
                # FundedNext: first payout buffer is REQUIRES_CONFIRMATION — require 5 benchmark days only.
                amt = available
                if max_wd_f is not None:
                    amt = min(amt, max_wd_f)
                if amt >= 1.0:
                    equity -= amt
                    pnl -= amt
                    trader_payout += amt * share
                    payouts += 1
                    if first_payout_day is None:
                        first_payout_day = days_elapsed
                    first_done = True
                    since_payout = 0.0
                    benchmark_days = 0  # next cycle needs 5 new benchmark days (research assumption)

    return {
        "terminal": "RUIN" if ruin else "SURVIVED",
        "payouts": payouts,
        "trader_payout": trader_payout,
        "first_payout_day": first_payout_day,
        "days": days_elapsed,
        "ruin": ruin,
        "pnl": pnl if is_mffu else equity - start_eq,
    }


def summarize_eval(results: list[dict[str, Any]], *, profile_id: str) -> dict[str, Any]:
    n = len(results) or 1
    passes = [r for r in results if r.get("terminal") == "PASS"]
    breaches = [r for r in results if r.get("terminal") == "BREACH"]
    p_pass = len(passes) / n
    p_breach = len(breaches) / n
    days = sorted(r["days"] for r in passes if r.get("days") is not None)
    trades = sorted(r["trades"] for r in passes if r.get("trades") is not None)
    dds = sorted(r["max_dd"] for r in passes if r.get("max_dd") is not None)
    adj = [r.get("adjusted_target") for r in results if r.get("adjusted_target") is not None]
    delayed = sum(1 for r in results if r.get("consistency_delayed")) / n

    def _pct(vals, p):
        if not vals:
            return None
        k = min(len(vals) - 1, max(0, int(round((p / 100.0) * (len(vals) - 1)))))
        return float(vals[k])

    expected_attempts = (1.0 / p_pass) if p_pass > 1e-12 else None
    st = load_profile(profile_id).stage("EVALUATION").raw
    p5 = st.get("first_5_purchase_price")
    p6 = st.get("purchase_6_plus_price")
    cost = None
    cost_note = "MFFU evaluation purchase price is REQUIRES_CONFIRMATION — attempts reported, dollar cost not invented"
    if p5 not in (None, REQUIRES_CONFIRMATION) and expected_attempts is not None:
        # FundedNext: first 5 at 69.99, thereafter 79.99
        att = expected_attempts
        if att <= 5:
            cost = att * float(p5)
        else:
            cost = 5 * float(p5) + (att - 5) * float(p6 or p5)
        cost_note = "FundedNext first_5=69.99 then 79.99; geometric attempts until first pass"

    return {
        "n_paths": len(results),
        "P(pass)": p_pass,
        "P(breach)": p_breach,
        "P(timeout)": sum(1 for r in results if r.get("terminal") == "TIMEOUT") / n,
        "median_days_to_pass": _pct(days, 50),
        "p75_days_to_pass": _pct(days, 75),
        "p95_days_to_pass": _pct(days, 95),
        "median_trades_to_pass": _pct(trades, 50),
        "median_max_drawdown_before_pass": _pct(dds, 50),
        "probability_consistency_rule_delays_pass": delayed,
        "average_adjusted_profit_target": float(np.mean(adj)) if adj else None,
        "expected_number_of_attempts": expected_attempts,
        "expected_evaluation_cost": cost,
        "expected_evaluation_cost_note": cost_note,
        "mean_sized_zero": float(np.mean([r.get("sized_zero") or 0 for r in results])),
    }


def summarize_funded(results: list[dict[str, Any]]) -> dict[str, Any]:
    n = len(results) or 1
    first = [r for r in results if (r.get("payouts") or 0) >= 1]
    second = [r for r in results if (r.get("payouts") or 0) >= 2]
    five = [r for r in results if (r.get("payouts") or 0) >= 5]
    survive = [r for r in results if not r.get("ruin")]
    days = sorted(r["first_payout_day"] for r in first if r.get("first_payout_day") is not None)
    payouts_before_breach = [r.get("trader_payout") or 0.0 for r in results]

    def _pct(vals, p):
        if not vals:
            return None
        k = min(len(vals) - 1, max(0, int(round((p / 100.0) * (len(vals) - 1)))))
        return float(vals[k])

    return {
        "n_paths": len(results),
        "probability_of_first_payout": len(first) / n,
        "expected_days_to_first_payout": _pct(days, 50),
        "account_survival_probability": len(survive) / n,
        "probability_of_second_payout": len(second) / n,
        "probability_of_5_payouts": len(five) / n,
        "expected_total_payout_before_breach": float(np.mean(payouts_before_breach)),
        "probability_of_ruin": sum(1 for r in results if r.get("ruin")) / n,
    }


def _stable_seed(*parts: Any) -> int:
    s = "|".join(str(p) for p in parts)
    acc = SEED
    for i, c in enumerate(s):
        acc = (acc + (i + 1) * ord(c) * 131) % 2_147_483_647
    return int(acc)


def _median_stop(days: Sequence[DayBundle], book: str) -> float:
    if DEFAULT_STOP[book]:
        return float(DEFAULT_STOP[book])
    pts = []
    for d in days:
        pts.extend(d.risk_points.tolist())
    if not pts:
        return 8.0
    return float(np.median(np.array(pts)))


def is_constant_risk(policy: PolicySpec) -> bool:
    return (
        policy.governor == "none"
        and policy.defensive_dd_ratio is None
        and policy.loss_streak_trigger is None
        and policy.target_approach_mult is None
        and abs(float(policy.buffer_scale) - 1.0) < 1e-12
        and abs(float(policy.payout_approach_scale) - 1.0) < 1e-12
        and abs(float(policy.after_payout_scale) - 1.0) < 1e-12
        and policy.funded_defensive_dd_ratio is None
    )


def _precompute_daily_usd(days: Sequence[DayBundle], book: str, risk_usd: float, cap: int) -> np.ndarray:
    out = np.zeros(len(days), dtype=np.float64)
    for i, d in enumerate(days):
        s = 0.0
        for r, rp in zip(d.r.tolist(), d.risk_points.tolist()):
            _qty, actual = size_qty(book, risk_usd, rp, cap)
            s += float(r) * actual
        out[i] = s
    return out


def batch_eval_fixed(
    days: Sequence[DayBundle],
    *,
    book: str,
    profile_id: str,
    dd_frac: float,
    n_paths: int,
    rng: np.random.Generator,
    horizon: int = EVAL_HORIZON,
) -> list[dict[str, Any]]:
    """Vectorized bootstrap for FIXED / no-governor evaluation."""
    st = load_profile(profile_id).stage("EVALUATION").raw
    max_loss = float(st["max_loss"])
    base_target = float(st["profit_target"])
    ratio_max = float(st.get("consistency_ratio_max") or 0.0)
    min_days = st.get("minimum_trading_days")
    min_days_i = 0 if min_days in (None, "NONE") else int(min_days)
    cap = max_micros(profile_id, "EVALUATION")
    risk_usd = float(dd_frac) * max_loss
    n_hist = len(days)
    if n_hist == 0:
        return [{"terminal": "NO_DATA"} for _ in range(int(n_paths))]
    daily = _precompute_daily_usd(days, book, risk_usd, cap)
    idx = rng.integers(0, n_hist, size=(int(n_paths), int(horizon)))
    stream = daily[idx]
    is_fn = profile_id.startswith("FUNDEDNEXT")
    start_eq = float(st.get("nominal_account_size") or 50000)
    n = int(n_paths)
    pnl = np.zeros(n)
    equity = np.full(n, start_eq)
    mll_pnl = np.full(n, -max_loss)
    mll_eq = np.full(n, start_eq - max_loss)
    eod_high_pnl = np.zeros(n)
    eod_high_eq = np.full(n, start_eq)
    locked = np.zeros(n, dtype=bool)
    best_day = np.zeros(n)
    peak = np.zeros(n)
    max_dd = np.zeros(n)
    terminal = np.zeros(n, dtype=np.int8)  # 0 open 1 pass 2 breach 3 timeout
    days_done = np.zeros(n, dtype=np.int32)
    trades_est = np.zeros(n, dtype=np.float64)
    delayed = np.zeros(n, dtype=bool)
    lock_at = float(st.get("mll_locks_at") or 50100.0)
    avg_trades = float(np.mean([len(d.r) for d in days])) if days else 0.0
    sized_zero = 1.0 if (daily == 0).all() else 0.0

    for t in range(int(horizon)):
        active = terminal == 0
        if not np.any(active):
            break
        dp = stream[:, t]
        pnl = np.where(active, pnl + dp, pnl)
        equity = np.where(active, equity + dp, equity)
        best_day = np.where(active, np.maximum(best_day, dp), best_day)
        days_done = np.where(active, days_done + 1, days_done)
        trades_est = np.where(active, trades_est + avg_trades, trades_est)
        if is_fn:
            eod_high_eq = np.where(active, np.maximum(eod_high_eq, equity), eod_high_eq)
            cand = eod_high_eq - max_loss
            mll_eq = np.where(active & ~locked, np.maximum(mll_eq, cand), mll_eq)
            hit_lock = active & ~locked & (mll_eq >= lock_at - 1e-12)
            mll_eq = np.where(hit_lock, lock_at, mll_eq)
            locked = locked | hit_lock
            realized = equity - start_eq
            remaining = equity - mll_eq
            peak = np.where(active, np.maximum(peak, realized), peak)
            max_dd = np.where(active, np.maximum(max_dd, peak - realized), max_dd)
            breach = active & (equity <= mll_eq + 1e-9)
        else:
            eod_high_pnl = np.where(active, np.maximum(eod_high_pnl, pnl), eod_high_pnl)
            mll_pnl = np.where(active, np.maximum(mll_pnl, eod_high_pnl - max_loss), mll_pnl)
            realized = pnl
            remaining = pnl - mll_pnl
            peak = np.where(active, np.maximum(peak, realized), peak)
            max_dd = np.where(active, np.maximum(max_dd, peak - realized), max_dd)
            breach = active & (pnl <= mll_pnl + 1e-9)
        if ratio_max > 0:
            adj = np.maximum(base_target, best_day / ratio_max)
        else:
            adj = np.full(n, base_target)
        delayed = delayed | (active & (adj > base_target + 1e-9) & (realized >= base_target))
        passed = active & (realized + 1e-9 >= adj) & (days_done >= min_days_i)
        terminal = np.where(breach, 2, terminal)
        still = terminal == 0
        terminal = np.where(passed & still, 1, terminal)

    terminal = np.where(terminal == 0, 3, terminal)
    if ratio_max > 0:
        adj = np.maximum(base_target, best_day / ratio_max)
    else:
        adj = np.full(n, base_target)
    code = {1: "PASS", 2: "BREACH", 3: "TIMEOUT"}
    out = []
    for i in range(n):
        out.append(
            {
                "terminal": code.get(int(terminal[i]), "TIMEOUT"),
                "days": int(days_done[i]),
                "trades": int(round(trades_est[i])),
                "max_dd": float(max_dd[i]),
                "consistency_delayed": bool(delayed[i]),
                "adjusted_target": float(adj[i]),
                "skipped": 0,
                "sized_zero": sized_zero,
                "pnl": float(pnl[i] if not is_fn else equity[i] - start_eq),
            }
        )
    return out


def batch_funded_fixed(
    days: Sequence[DayBundle],
    *,
    book: str,
    profile_id: str,
    dd_frac: float,
    n_paths: int,
    rng: np.random.Generator,
    horizon: int = FUNDED_HORIZON,
    payout_cushion_frac: float = 0.25,
) -> list[dict[str, Any]]:
    prof = load_profile(profile_id)
    st = prof.stage("FUNDED").raw
    pay = prof.payout()
    max_loss = float(st["max_loss"])
    cap = max_micros(profile_id, "FUNDED")
    risk_usd = float(dd_frac) * max_loss
    n_hist = len(days)
    if n_hist == 0:
        return [{"terminal": "NO_DATA", "payouts": 0, "trader_payout": 0.0, "ruin": True} for _ in range(int(n_paths))]
    daily = _precompute_daily_usd(days, book, risk_usd, cap)
    idx = rng.integers(0, n_hist, size=(int(n_paths), int(horizon)))
    stream = daily[idx]
    is_mffu = profile_id.startswith("MFFU")
    start_eq = 50000.0
    n = int(n_paths)
    pnl = np.zeros(n)
    equity = np.full(n, start_eq)
    mll_pnl = np.full(n, -max_loss)
    mll_eq = np.full(n, start_eq - max_loss)
    locked = np.zeros(n, dtype=bool)
    first_done = np.zeros(n, dtype=bool)
    since = np.zeros(n)
    payouts = np.zeros(n, dtype=np.int32)
    trader = np.zeros(n)
    bench = np.zeros(n, dtype=np.int32)
    ruin = np.zeros(n, dtype=bool)
    first_day = np.full(n, -1, dtype=np.int32)
    lock_pnl = float(st.get("mll_lock_level") or 100.0)
    lock_eq = float(st.get("mll_locks_at") or 50100.0)
    share = float(pay.get("trader_profit_share") or pay.get("reward_share") or (0.9 if is_mffu else 0.95))
    first_buf = pay.get("first_payout_required_buffer")
    subsequent = pay.get("subsequent_payout_profit_required")
    min_po = pay.get("minimum_payout")
    max_wd = pay.get("maximum_withdrawal")
    first_buf_f = float(first_buf) if first_buf not in (None, REQUIRES_CONFIRMATION, "NONE") else 2100.0
    subsequent_f = float(subsequent) if subsequent not in (None, REQUIRES_CONFIRMATION, "NONE") else 500.0
    min_po_f = float(min_po) if min_po not in (None, REQUIRES_CONFIRMATION, "NONE") else 0.0
    max_wd_f = float(max_wd) if max_wd not in (None, REQUIRES_CONFIRMATION, "NONE", "NONE_STATED") else None
    bench_need = int(st.get("benchmark_days_required") or 0) if st.get("benchmark_days_required") not in (None, "NONE") else 0
    bench_min = float(st.get("benchmark_day_min_profit_50k") or 0.0)
    cushion = payout_cushion_frac * max_loss
    alive = np.ones(n, dtype=bool)

    for t in range(int(horizon)):
        if not np.any(alive):
            break
        dp = stream[:, t]
        pnl = np.where(alive, pnl + dp, pnl)
        equity = np.where(alive, equity + dp, equity)
        since = np.where(alive, since + dp, since)
        if not is_mffu:
            bench = np.where(alive & (dp >= bench_min - 1e-12), bench + 1, bench)
        if is_mffu:
            eod = pnl
            cand = eod - max_loss
            mll_pnl = np.where(alive & ~locked, np.maximum(mll_pnl, cand), mll_pnl)
            hit = alive & ~locked & (mll_pnl >= lock_pnl - 1e-12)
            mll_pnl = np.where(hit, lock_pnl, mll_pnl)
            locked = locked | hit
            dead = alive & (pnl <= mll_pnl + 1e-9)
            ruin = ruin | dead
            alive = alive & ~dead
            need = np.where(first_done, subsequent_f, first_buf_f)
            have = np.where(first_done, since, pnl)
            unlocked = alive & (have + 1e-12 >= need)
            available = pnl - mll_pnl - cushion
            amt = np.maximum(available, 0.0)
            if max_wd_f is not None:
                amt = np.minimum(amt, max_wd_f)
            pay_now = unlocked & (amt >= max(min_po_f, 1.0))
            pnl = np.where(pay_now, pnl - amt, pnl)
            equity = np.where(pay_now, equity - amt, equity)
            trader = np.where(pay_now, trader + amt * share, trader)
            payouts = np.where(pay_now, payouts + 1, payouts)
            since = np.where(pay_now, 0.0, since)
            first_day = np.where(pay_now & (first_day < 0), t + 1, first_day)
            first_done = first_done | pay_now
        else:
            cand = equity - max_loss
            mll_eq = np.where(alive & ~locked, np.maximum(mll_eq, cand), mll_eq)
            hit = alive & ~locked & (mll_eq >= lock_eq - 1e-12)
            mll_eq = np.where(hit, lock_eq, mll_eq)
            locked = locked | hit
            dead = alive & (equity <= mll_eq + 1e-9)
            ruin = ruin | dead
            alive = alive & ~dead
            available = equity - mll_eq - cushion
            amt = np.maximum(available, 0.0)
            if max_wd_f is not None:
                amt = np.minimum(amt, max_wd_f)
            bench_ok = (bench_need <= 0) | (bench >= bench_need)
            pay_now = alive & bench_ok & (amt >= max(min_po_f, 1.0))
            equity = np.where(pay_now, equity - amt, equity)
            pnl = np.where(pay_now, pnl - amt, pnl)
            trader = np.where(pay_now, trader + amt * share, trader)
            payouts = np.where(pay_now, payouts + 1, payouts)
            first_day = np.where(pay_now & (first_day < 0), t + 1, first_day)
            first_done = first_done | pay_now
            bench = np.where(pay_now, 0, bench)

    out = []
    for i in range(n):
        fd = int(first_day[i])
        out.append(
            {
                "terminal": "RUIN" if bool(ruin[i]) else "SURVIVED",
                "payouts": int(payouts[i]),
                "trader_payout": float(trader[i]),
                "first_payout_day": None if fd < 0 else fd,
                "days": int(horizon if alive[i] or ruin[i] else horizon),
                "ruin": bool(ruin[i]),
                "pnl": float(pnl[i] if is_mffu else equity[i] - start_eq),
            }
        )
    return out


def run_eval_grid(
    days: Sequence[DayBundle],
    *,
    book: str,
    profile_id: str,
    n_paths: int = N_PATHS_DEFAULT,
    fracs: Sequence[float] = EVAL_FRACS,
    policy: PolicySpec = FIXED,
    mode: str = "bootstrap",
) -> list[dict[str, Any]]:
    rows = []
    med_stop = _median_stop(days, book)
    for frac in fracs:
        rng = np.random.default_rng(_stable_seed(book, profile_id, policy.name, frac, mode))
        if mode == "bootstrap" and is_constant_risk(policy):
            res = batch_eval_fixed(
                days, book=book, profile_id=profile_id, dd_frac=float(frac), n_paths=int(n_paths), rng=rng
            )
        else:
            res = [
                simulate_eval_path(
                    days, book=book, profile_id=profile_id, dd_frac=float(frac), policy=policy, rng=rng, mode=mode
                )
                for _ in range(int(n_paths))
            ]
        summ = summarize_eval(res, profile_id=profile_id)
        ml = initial_max_loss(profile_id, "EVALUATION")
        usd = float(frac) * ml
        qty_ex, actual = size_qty(book, usd, med_stop, max_micros(profile_id, "EVALUATION"))
        rows.append(
            {
                "book": book,
                "profile": profile_id,
                "stage": "EVALUATION",
                "dd_frac": frac,
                "risk_usd": usd,
                "policy": policy.name,
                "governor": policy.governor,
                "mode": mode,
                "example_qty_at_median_stop": qty_ex,
                "example_actual_risk_usd": actual,
                "median_stop_points": med_stop,
                **summ,
            }
        )
    return rows


def run_funded_grid(
    days: Sequence[DayBundle],
    *,
    book: str,
    profile_id: str,
    n_paths: int = N_PATHS_DEFAULT,
    fracs: Sequence[float] = FUNDED_FRACS,
    policy: PolicySpec = FIXED,
    mode: str = "bootstrap",
) -> list[dict[str, Any]]:
    rows = []
    for frac in fracs:
        rng = np.random.default_rng(_stable_seed("F", book, profile_id, policy.name, frac, mode))
        if mode == "bootstrap" and is_constant_risk(policy):
            res = batch_funded_fixed(
                days, book=book, profile_id=profile_id, dd_frac=float(frac), n_paths=int(n_paths), rng=rng
            )
        else:
            res = [
                simulate_funded_path(
                    days, book=book, profile_id=profile_id, dd_frac=float(frac), policy=policy, rng=rng, mode=mode
                )
                for _ in range(int(n_paths))
            ]
        summ = summarize_funded(res)
        ml = initial_max_loss(profile_id, "FUNDED")
        usd = float(frac) * ml
        rows.append(
            {
                "book": book,
                "profile": profile_id,
                "stage": "FUNDED",
                "dd_frac": frac,
                "risk_usd": usd,
                "policy": policy.name,
                "mode": mode,
                **summ,
            }
        )
    return rows


def chrono_eval(days, *, book, profile_id, dd_frac, policy=FIXED):
    rng = np.random.default_rng(0)
    return simulate_eval_path(days, book=book, profile_id=profile_id, dd_frac=dd_frac, policy=policy, rng=rng, mode="chrono", horizon=max(len(days), 1))


def chrono_funded(days, *, book, profile_id, dd_frac, policy=FIXED):
    rng = np.random.default_rng(0)
    return simulate_funded_path(days, book=book, profile_id=profile_id, dd_frac=dd_frac, policy=policy, rng=rng, mode="chrono", horizon=max(len(days), 1))


GOV_POLICIES = [
    PolicySpec(name="GOV_NONE", governor="none"),
    PolicySpec(name="GOV_SOFT", governor="soft"),
    PolicySpec(name="GOV_HARD", governor="hard"),
    PolicySpec(name="GOV_REDUCED", governor="reduced"),
]

EVAL_STATE_POLICIES = [
    FIXED,
    PolicySpec(name="EVAL_DEFENSIVE_DD40", defensive_dd_ratio=0.40, defensive_scale=0.5),
    PolicySpec(name="EVAL_DEFENSIVE_DD25", defensive_dd_ratio=0.25, defensive_scale=0.5),
    PolicySpec(name="EVAL_LOSS_STREAK_2", loss_streak_trigger=2, defensive_scale=0.5),
    PolicySpec(name="EVAL_LOSS_STREAK_3", loss_streak_trigger=3, defensive_scale=0.5),
    PolicySpec(name="EVAL_TARGET_APPROACH_2R", target_approach_mult=2.0, defensive_scale=0.5),
    PolicySpec(name="EVAL_COMBINED", defensive_dd_ratio=0.40, loss_streak_trigger=2, target_approach_mult=2.0, defensive_scale=0.5),
]

FUNDED_STATE_POLICIES = [
    FIXED,
    PolicySpec(name="FUNDED_BUFFER_BUILD", buffer_scale=0.5),
    PolicySpec(name="FUNDED_PAYOUT_APPROACH", payout_approach_scale=0.5, buffer_scale=0.75),
    PolicySpec(name="FUNDED_DEFENSIVE_DD40", funded_defensive_dd_ratio=0.40, defensive_scale=0.5),
    PolicySpec(name="FUNDED_AFTER_PAYOUT_CUT", after_payout_scale=0.5, buffer_scale=0.75),
    PolicySpec(name="FUNDED_LOSS_STREAK_2", loss_streak_trigger=2, defensive_scale=0.5),
]


def eval_objective(row: dict[str, Any]) -> float:
    """Maximize pass, penalize breach / time / attempts. Not fastest-pass-only."""
    p = float(row.get("P(pass)") or 0.0)
    b = float(row.get("P(breach)") or 0.0)
    days = float(row.get("median_days_to_pass") or 252)
    att = float(row.get("expected_number_of_attempts") or 10)
    return p - 0.45 * b - 0.0008 * days - 0.02 * min(att, 20)


def funded_objective(row: dict[str, Any]) -> float:
    if (row.get("example_qty_at_median_stop") or 0) <= 0 and (row.get("probability_of_first_payout") or 0) < 0.05:
        return -10.0
    return (
        0.25 * float(row.get("probability_of_first_payout") or 0)
        + 0.45 * float(row.get("account_survival_probability") or 0)
        + 0.10 * float(row.get("probability_of_second_payout") or 0)
        + 0.10 * float(row.get("probability_of_5_payouts") or 0)
        + 0.00008 * float(row.get("expected_total_payout_before_breach") or 0)
        - 0.55 * float(row.get("probability_of_ruin") or 0)
    )


def unsuitable(eval_rows: list[dict[str, Any]], dist: dict[str, Any]) -> Optional[str]:
    tradable = [r for r in eval_rows if (r.get("example_qty_at_median_stop") or 0) > 0]
    if not tradable:
        return "PROP_PROFILE_UNSUITABLE"
    best = max(tradable, key=eval_objective)
    exp = dist.get("expectancy_R")
    if (best.get("P(pass)") or 0) < 0.05 and (best.get("P(breach)") or 0) > 0.50:
        return "PROP_PROFILE_UNSUITABLE"
    if exp is not None and float(exp) < -0.05 and (best.get("P(pass)") or 0) < 0.15:
        return "PROP_PROFILE_UNSUITABLE"
    return None


def unsuitable_funded(funded_rows: list[dict[str, Any]], book: str, profile_id: str) -> Optional[str]:
    stop = DEFAULT_STOP[book]
    cap = max_micros(profile_id, "FUNDED")
    tradable = []
    for r in funded_rows:
        usd = float(r.get("risk_usd") or 0)
        rp = stop if stop is not None else float(r.get("median_stop_points") or 8.0)
        qty, _ = size_qty(book, usd, rp, cap)
        r["example_qty_at_median_stop"] = qty
        if qty > 0:
            tradable.append(r)
    if not tradable:
        return "PROP_PROFILE_UNSUITABLE"
    best = max(tradable, key=funded_objective)
    if (best.get("account_survival_probability") or 0) < 0.05 and (best.get("probability_of_ruin") or 0) >= 0.90:
        return "PROP_PROFILE_UNSUITABLE"
    return None
