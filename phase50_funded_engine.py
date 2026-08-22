"""Phase 50 — funded survival engine. Cushion, reserve, payout, dynamic risk. No martingale."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional, Sequence

import numpy as np

from phase49_prop_sim import (
    DEFAULT_STOP,
    POINT_USD_MICRO,
    DayBundle,
    initial_max_loss,
    max_micros,
    size_qty,
)
from prop_rules_v1 import REQUIRES_CONFIRMATION, load_profile

HORIZONS = (30, 60, 90, 180, 252, 504)
MAX_SLOTS = 8
SEED = 50
N_PATHS_CURVE = 8_000
N_PATHS_SEARCH = 2_500
N_PATHS_FINAL = 8_000

PAYOUT_MODES = (
    "PAYOUT_NONE",
    "PAYOUT_AS_SOON_AS_ELIGIBLE",
    "MINIMUM_PAYOUT_ONLY",
    "PARTIAL_SURPLUS_PAYOUT",
    "FIXED_INTERNAL_RESERVE",
    "DYNAMIC_RESERVE",
    "DELAYED_PAYOUT",
)

RESERVE_USD = (250.0, 500.0, 750.0, 1000.0, 1250.0, 1500.0)
RESERVE_FRACS = (0.25, 0.375, 0.50, 0.625, 0.75)

# AITRADE internal floor for "minimum payout only" — not a FundedNext firm rule.
AITRADE_INTERNAL_MIN_PAYOUT = 500.0

BREACH_MLL = 1
BREACH_POST_PAYOUT = 2
BREACH_STREAK = 3
BREACH_FLOOR = 4
BREACH_DAILY = 5
BREACH_CODE = {
    1: "MLL_TRAIL",
    2: "POST_PAYOUT_CUSHION",
    3: "LOSING_STREAK",
    4: "CONTRACT_FLOOR_FORCED",
    5: "DAILY_CLUSTER",
}


@dataclass
class FundedPolicy:
    name: str
    payout_mode: str = "PAYOUT_AS_SOON_AS_ELIGIBLE"
    reserve_usd: float = 1000.0
    reserve_frac_max_loss: Optional[float] = None
    partial_frac: float = 0.50
    delayed_extra_days: int = 10
    delayed_surplus_mult: float = 2.0
    use_dynamic_risk: bool = False
    healthy_cushion_frac: float = 0.12
    caution_thr: float = 0.50
    defensive_thr: float = 0.30
    critical_thr: float = 0.15
    caution_scale: float = 0.50
    defensive_scale: float = 0.25
    critical_scale: float = 0.0
    max_micros_cap: int = 3
    fixed_risk_usd: Optional[float] = None
    daily_stop: str = "none"
    daily_stop_r: float = 1.0
    daily_stop_cushion_frac: float = 0.25
    daily_stop_consec: int = 2
    streak_mode: str = "none"
    streak_scale: float = 0.50
    pre_lock_scale: float = 1.0
    post_lock_scale: float = 1.0
    floor_block_ratio: float = 1.0
    # Phase 50 default: skip rather than force a micro through remaining cushion.
    block_insufficient_capacity: bool = True
    cap_risk_to_cushion: bool = True
    extra: dict[str, Any] = field(default_factory=dict)

    def resolved_reserve(self, max_loss: float) -> float:
        usd = float(self.reserve_usd)
        if self.reserve_frac_max_loss is not None:
            usd = max(usd, float(self.reserve_frac_max_loss) * float(max_loss))
        if self.payout_mode == "DYNAMIC_RESERVE":
            usd = max(usd, 0.50 * float(max_loss))
        return float(usd)


def assert_no_martingale(p: FundedPolicy) -> None:
    for attr in (
        "caution_scale",
        "defensive_scale",
        "critical_scale",
        "streak_scale",
        "pre_lock_scale",
        "post_lock_scale",
        "partial_frac",
    ):
        if float(getattr(p, attr)) > 1.0 + 1e-12:
            raise ValueError(f"martingale_forbidden:{attr}")
    if p.healthy_cushion_frac > 0.50 + 1e-12:
        raise ValueError("martingale_forbidden:healthy_cushion_frac_too_large")


def pack_days(days: Sequence[DayBundle]) -> tuple[np.ndarray, np.ndarray]:
    n = len(days)
    r = np.full((n, MAX_SLOTS), np.nan)
    rp = np.full((n, MAX_SLOTS), np.nan)
    for i, d in enumerate(days):
        k = min(len(d.r), MAX_SLOTS)
        if k:
            r[i, :k] = d.r[:k]
            rp[i, :k] = d.risk_points[:k]
    return r, rp


def min_executable(book: str, days: Sequence[DayBundle]) -> dict[str, Any]:
    px = POINT_USD_MICRO[book]
    stops = []
    for d in days:
        stops.extend(d.risk_points.tolist())
    arr = np.array(stops, dtype=float) if stops else np.array([DEFAULT_STOP[book] or 8.0])
    arr = arr[np.isfinite(arr) & (arr > 0)]
    unit = arr * px
    return {
        "book": book,
        "micro": {"NQ": "MNQ", "ES": "MES", "GC": "MGC"}[book],
        "n_stops": int(len(arr)),
        "min_stop_points": float(np.min(arr)),
        "p10_stop_points": float(np.percentile(arr, 10)),
        "median_stop_points": float(np.median(arr)),
        "p90_stop_points": float(np.percentile(arr, 90)),
        "max_stop_points": float(np.max(arr)),
        "min_executable_usd": float(np.min(unit)),
        "p10_executable_usd": float(np.percentile(unit, 10)),
        "median_executable_usd": float(np.median(unit)),
        "p90_executable_usd": float(np.percentile(unit, 90)),
        "max_executable_usd": float(np.max(unit)),
        "point_usd_micro": px,
    }


def _payout_amount(
    *,
    mode: str,
    surplus: np.ndarray,
    min_po: float,
    max_wd: Optional[float],
    delayed_ok: np.ndarray,
) -> np.ndarray:
    amt = np.maximum(surplus, 0.0)
    if max_wd is not None:
        amt = np.minimum(amt, float(max_wd))
    if mode in ("PAYOUT_NONE",):
        return np.zeros_like(amt)
    if mode == "MINIMUM_PAYOUT_ONLY":
        take = np.minimum(amt, float(min_po))
        return np.where(amt + 1e-12 >= float(min_po), take, 0.0)
    if mode == "PARTIAL_SURPLUS_PAYOUT":
        take = 0.50 * amt
        return np.where(take + 1e-12 >= float(min_po), take, 0.0)
    if mode == "DELAYED_PAYOUT":
        need = float(min_po) * 2.0
        ok = delayed_ok | (amt + 1e-12 >= need)
        return np.where(ok, amt, 0.0)
    # asap / fixed_reserve / dynamic_reserve: all surplus above reserve (already subtracted)
    return np.where(amt + 1e-12 >= float(min_po), amt, 0.0)


def simulate_funded(
    days: Sequence[DayBundle],
    *,
    book: str,
    profile_id: str,
    policy: FundedPolicy,
    n_paths: int,
    rng: np.random.Generator,
    horizon: int = 504,
    record_payout_events: bool = False,
) -> dict[str, Any]:
    assert_no_martingale(policy)
    if not days:
        return {"ok": False, "error": "NO_DATA"}
    prof = load_profile(profile_id)
    st = prof.stage("FUNDED").raw
    pay = prof.payout()
    max_loss = float(st["max_loss"])
    cap = min(int(policy.max_micros_cap), max_micros(profile_id, "FUNDED"))
    px = POINT_USD_MICRO[book]
    R, RP = pack_days(days)
    n_hist = len(days)
    n = int(n_paths)
    H = int(horizon)
    idx = rng.integers(0, n_hist, size=(n, H))
    is_mffu = profile_id.startswith("MFFU")
    start_eq = 50000.0
    lock_pnl = float(st.get("mll_lock_level") or 100.0)
    lock_eq = float(st.get("mll_locks_at") or 50100.0)
    share = float(pay.get("trader_profit_share") or pay.get("reward_share") or (0.9 if is_mffu else 0.95))
    first_buf = pay.get("first_payout_required_buffer")
    subsequent = pay.get("subsequent_payout_profit_required")
    min_po_raw = pay.get("minimum_payout")
    max_wd = pay.get("maximum_withdrawal")
    first_buf_f = float(first_buf) if first_buf not in (None, REQUIRES_CONFIRMATION, "NONE") else None
    subsequent_f = float(subsequent) if subsequent not in (None, REQUIRES_CONFIRMATION, "NONE") else None
    if min_po_raw not in (None, REQUIRES_CONFIRMATION, "NONE"):
        min_po_f = float(min_po_raw)
    else:
        min_po_f = AITRADE_INTERNAL_MIN_PAYOUT  # AITRADE policy, not a firm-stated FN minimum
    max_wd_f = float(max_wd) if max_wd not in (None, REQUIRES_CONFIRMATION, "NONE", "NONE_STATED") else None
    bench_need = int(st.get("benchmark_days_required") or 0) if st.get("benchmark_days_required") not in (None, "NONE") else 0
    bench_min = float(st.get("benchmark_day_min_profit_50k") or 0.0)
    reserve = policy.resolved_reserve(max_loss)

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
    eligible_since = np.full(n, -1, dtype=np.int32)
    alive = np.ones(n, dtype=bool)
    consec = np.zeros(n, dtype=np.int32)
    next_day_scale = np.ones(n)
    breach_day = np.full(n, -1, dtype=np.int32)
    breach_code = np.zeros(n, dtype=np.int8)
    last_payout_day = np.full(n, -1000, dtype=np.int32)
    last_consec = np.zeros(n, dtype=np.int32)
    n_block_floor = np.zeros(n, dtype=np.int32)
    n_trades = np.zeros(n, dtype=np.int32)
    last_cushion = np.zeros(n)
    last_locked = np.zeros(n, dtype=bool)
    n_state = np.zeros((n, 5), dtype=np.int32)  # HEALTHY, CAUTION, DEFENSIVE, CRITICAL, LOCKOUT
    pre_cushion = np.full(n, np.nan)
    pre_locked = np.zeros(n, dtype=bool)
    pre_since_payout = np.full(n, np.nan)
    pre_consec = np.full(n, np.nan)
    snapshots: dict[int, dict[str, np.ndarray]] = {}
    payout_log: list[tuple[int, np.ndarray, np.ndarray]] = []

    for t in range(H):
        if not np.any(alive):
            for h in HORIZONS:
                if h >= t + 1 and h not in snapshots:
                    snapshots[h] = _snap(alive, payouts, trader, breach_day, n_trades)
            break
        di = idx[:, t]
        day_pnl = np.zeros(n)
        day_stopped = np.zeros(n, dtype=bool)
        day_losses = np.zeros(n, dtype=np.int32)
        scale_lock = np.where(locked, float(policy.post_lock_scale), float(policy.pre_lock_scale))
        scale_lock = np.minimum(scale_lock, next_day_scale)
        next_day_scale[:] = 1.0

        for slot in range(MAX_SLOTS):
            r = R[di, slot]
            rp = RP[di, slot]
            valid = alive & ~day_stopped & np.isfinite(r) & np.isfinite(rp) & (rp > 0)
            if not np.any(valid):
                continue
            if is_mffu:
                cushion = pnl - mll_pnl
            else:
                cushion = equity - mll_eq
            min_exec = rp * px
            ratio = np.divide(min_exec, np.maximum(cushion, 1e-9))
            block = np.zeros(n, dtype=bool)
            if policy.block_insufficient_capacity:
                block = valid & ((cushion <= min_exec + 1e-9) | (ratio > float(policy.floor_block_ratio) + 1e-12))
                n_block_floor = n_block_floor + block.astype(np.int32)
                valid = valid & ~block

            ml_ratio = np.divide(cushion, max_loss)
            state_scale = np.ones(n)
            if policy.use_dynamic_risk:
                state_scale = np.where(ml_ratio < policy.caution_thr, policy.caution_scale, state_scale)
                state_scale = np.where(ml_ratio < policy.defensive_thr, policy.defensive_scale, state_scale)
                state_scale = np.where(ml_ratio < policy.critical_thr, policy.critical_scale, state_scale)
                budget = policy.healthy_cushion_frac * np.maximum(cushion, 0.0) * state_scale
            elif policy.fixed_risk_usd is not None:
                budget = np.full(n, float(policy.fixed_risk_usd))
            else:
                budget = np.full(n, float(np.median(RP[np.isfinite(RP)]) * px))
            if policy.streak_mode in ("reduce2",) and slot >= 0:
                budget = np.where(consec >= 2, budget * policy.streak_scale, budget)
            if policy.streak_mode in ("reduce3",):
                budget = np.where(consec >= 3, budget * policy.streak_scale, budget)
            budget = budget * scale_lock
            if policy.cap_risk_to_cushion:
                budget = np.minimum(budget, np.maximum(cushion, 0.0))

            per = rp * px
            qty = np.floor(np.divide(budget, np.maximum(per, 1e-9)))
            qty = np.clip(qty, 0, cap)
            actual = qty * per
            take = valid & (qty >= 1)
            if policy.cap_risk_to_cushion:
                take = take & (actual <= cushion + 1e-9)
            # Occupancy: first slot of the day only (avoid 8× counting).
            if slot == 0:
                floor_lock = (cushion <= min_exec + 1e-9) | (state_scale <= 1e-12) | block
                n_state[:, 4] += (alive & floor_lock).astype(np.int32)
                n_state[:, 3] += (alive & ~floor_lock & (ml_ratio < policy.critical_thr)).astype(np.int32)
                n_state[:, 2] += (
                    alive & ~floor_lock & (ml_ratio >= policy.critical_thr) & (ml_ratio < policy.defensive_thr)
                ).astype(np.int32)
                n_state[:, 1] += (
                    alive & ~floor_lock & (ml_ratio >= policy.defensive_thr) & (ml_ratio < policy.caution_thr)
                ).astype(np.int32)
                n_state[:, 0] += (alive & ~floor_lock & (ml_ratio >= policy.caution_thr)).astype(np.int32)
            pnl_t = np.where(take, r * actual, 0.0)
            pnl = np.where(take, pnl + pnl_t, pnl)
            equity = np.where(take, equity + pnl_t, equity)
            since = np.where(take, since + pnl_t, since)
            day_pnl = day_pnl + pnl_t
            n_trades = n_trades + take.astype(np.int32)
            loss = take & (r < 0)
            win = take & (r > 0)
            consec = np.where(loss, consec + 1, consec)
            consec = np.where(win, 0, consec)
            day_losses = np.where(loss, day_losses + 1, day_losses)
            last_consec = np.where(take, consec, last_consec)
            if policy.streak_mode == "pause3":
                just = take & (consec >= 3)
                day_stopped = day_stopped | just
                next_day_scale = np.where(just, np.minimum(next_day_scale, policy.streak_scale), next_day_scale)

            if policy.daily_stop == "r_loss":
                day_stopped = day_stopped | (day_pnl <= -policy.daily_stop_r * np.maximum(actual, min_exec))
            elif policy.daily_stop == "cushion_frac":
                day_stopped = day_stopped | (day_pnl <= -policy.daily_stop_cushion_frac * np.maximum(cushion, 1e-9))
            elif policy.daily_stop == "consec":
                day_stopped = day_stopped | (day_losses >= int(policy.daily_stop_consec))

        if is_mffu:
            last_cushion = np.where(alive, pnl - mll_pnl, last_cushion)
        else:
            last_cushion = np.where(alive, equity - mll_eq, last_cushion)
        last_locked = np.where(alive, locked, last_locked)

        # EOD MLL
        if is_mffu:
            cand = pnl - max_loss
            mll_pnl = np.where(alive & ~locked, np.maximum(mll_pnl, cand), mll_pnl)
            hit = alive & ~locked & (mll_pnl >= lock_pnl - 1e-12)
            mll_pnl = np.where(hit, lock_pnl, mll_pnl)
            locked = locked | hit
            dead = alive & (pnl <= mll_pnl + 1e-9)
        else:
            cand = equity - max_loss
            mll_eq = np.where(alive & ~locked, np.maximum(mll_eq, cand), mll_eq)
            hit = alive & ~locked & (mll_eq >= lock_eq - 1e-12)
            mll_eq = np.where(hit, lock_eq, mll_eq)
            locked = locked | hit
            dead = alive & (equity <= mll_eq + 1e-9)
            bench = np.where(alive & (day_pnl >= bench_min - 1e-12), bench + 1, bench)

        if np.any(dead):
            cause = np.full(n, BREACH_MLL, dtype=np.int8)
            cause = np.where((t - last_payout_day) <= 8, BREACH_POST_PAYOUT, cause)
            cause = np.where(last_consec >= 3, BREACH_STREAK, cause)
            new_dead = dead & (breach_code == 0)
            breach_code = np.where(new_dead, cause, breach_code)
            breach_day = np.where(dead & (breach_day < 0), t + 1, breach_day)
            pre_cushion = np.where(new_dead, last_cushion, pre_cushion)
            pre_locked = np.where(new_dead, last_locked, pre_locked)
            pre_since_payout = np.where(new_dead, (t - last_payout_day).astype(float), pre_since_payout)
            pre_consec = np.where(new_dead, last_consec.astype(float), pre_consec)
            alive = alive & ~dead

        if not is_mffu:
            bench_ok = (bench_need <= 0) | (bench >= bench_need)
        else:
            bench_ok = np.ones(n, dtype=bool)

        if is_mffu:
            if first_buf_f is None:
                unlocked = np.zeros(n, dtype=bool)  # fail closed
            else:
                need = np.where(first_done, (subsequent_f if subsequent_f is not None else 1e18), first_buf_f)
                have = np.where(first_done, since, pnl)
                unlocked = alive & (have + 1e-12 >= need)
            surplus = pnl - mll_pnl - reserve
        else:
            # FN first-payout dollar buffer is REQUIRES_CONFIRMATION — eligibility is 5 benchmark days only.
            unlocked = alive & bench_ok
            surplus = equity - mll_eq - reserve

        eligible_since = np.where(unlocked & (eligible_since < 0), t, eligible_since)
        delayed_ok = (eligible_since >= 0) & ((t - eligible_since) >= int(policy.delayed_extra_days))
        amt = _payout_amount(
            mode=policy.payout_mode,
            surplus=surplus,
            min_po=min_po_f,
            max_wd=max_wd_f,
            delayed_ok=delayed_ok,
        )
        pay_now = unlocked & (amt >= min_po_f - 1e-9) & (amt > 1e-9) & alive
        if policy.payout_mode == "PAYOUT_NONE":
            pay_now = np.zeros(n, dtype=bool)
        pnl = np.where(pay_now, pnl - amt, pnl)
        equity = np.where(pay_now, equity - amt, equity)
        trader = np.where(pay_now, trader + amt * share, trader)
        payouts = np.where(pay_now, payouts + 1, payouts)
        if record_payout_events and np.any(pay_now):
            ix = np.flatnonzero(pay_now)
            payout_log.append((t + 1, ix, (amt[ix] * share).astype(float)))
        since = np.where(pay_now, 0.0, since)
        last_payout_day = np.where(pay_now, t, last_payout_day)
        first_done = first_done | pay_now
        if not is_mffu:
            bench = np.where(pay_now, 0, bench)
        eligible_since = np.where(pay_now, -1, eligible_since)

        dayn = t + 1
        if dayn in HORIZONS:
            snapshots[dayn] = _snap(alive, payouts, trader, breach_day, n_trades)

    for h in HORIZONS:
        if h not in snapshots:
            snapshots[h] = _snap(alive, payouts, trader, breach_day, n_trades)

    return {
        "ok": True,
        "book": book,
        "profile": profile_id,
        "policy": policy.name,
        "payout_mode": policy.payout_mode,
        "reserve_usd": reserve,
        "n_paths": n,
        "horizon": H,
        "firm_unknown_fn_first_buffer": (not is_mffu) and (first_buf_f is None),
        "aitrade_internal_min_payout": min_po_f,
        "share": share,
        "alive": alive,
        "payouts": payouts,
        "trader": trader,
        "breach_day": breach_day,
        "breach_code": breach_code,
        "n_block_floor": n_block_floor,
        "n_trades": n_trades,
        "snapshots": snapshots,
        "pre_cushion": pre_cushion,
        "pre_locked": pre_locked,
        "pre_since_payout": pre_since_payout,
        "pre_consec": pre_consec,
        "n_state": n_state,
        "payout_events": _unpack_payout_log(payout_log, n) if record_payout_events else None,
        "summary": summarize_run(
            snapshots,
            alive,
            payouts,
            trader,
            breach_day,
            breach_code,
            n_block_floor,
            n_trades,
            n,
            pre_cushion=pre_cushion,
            pre_locked=pre_locked,
            pre_since_payout=pre_since_payout,
            pre_consec=pre_consec,
            n_state=n_state,
        ),
    }


def _unpack_payout_log(log: list[tuple[int, np.ndarray, np.ndarray]], n: int) -> list[list[tuple[int, float]]]:
    out: list[list[tuple[int, float]]] = [[] for _ in range(n)]
    for day, ix, amts in log:
        for i, a in zip(ix.tolist(), amts.tolist()):
            out[int(i)].append((int(day), float(a)))
    return out


def _snap(alive, payouts, trader, breach_day, n_trades) -> dict[str, np.ndarray]:
    return {
        "alive": alive.copy(),
        "payouts": payouts.copy(),
        "trader": trader.copy(),
        "breach_day": breach_day.copy(),
        "n_trades": n_trades.copy(),
    }


def _pct(vals: np.ndarray, p: float) -> Optional[float]:
    v = np.sort(vals[np.isfinite(vals)])
    if len(v) == 0:
        return None
    k = min(len(v) - 1, max(0, int(round((p / 100.0) * (len(v) - 1)))))
    return float(v[k])


def summarize_run(
    snapshots,
    alive,
    payouts,
    trader,
    breach_day,
    breach_code,
    n_block_floor,
    n_trades,
    n,
    pre_cushion=None,
    pre_locked=None,
    pre_since_payout=None,
    pre_consec=None,
    n_state=None,
) -> dict[str, Any]:
    curves = {}
    for h, s in snapshots.items():
        po = s["payouts"]
        br = s["breach_day"]
        survived = s["alive"]
        breached = br > 0
        br_h = breached & (br <= h)
        curves[str(h)] = {
            "horizon": h,
            "P(survive)": float(np.mean(survived)),
            "P(first payout)": float(np.mean(po >= 1)),
            "P(2 payouts)": float(np.mean(po >= 2)),
            "P(5 payouts)": float(np.mean(po >= 5)),
            "P(10 payouts)": float(np.mean(po >= 10)),
            "median_payouts": _pct(po.astype(float), 50),
            "median_cumulative_trader_payout": _pct(s["trader"], 50),
            "expected_cumulative_trader_payout": float(np.mean(s["trader"])),
            "P(breach)": float(np.mean(br_h)),
            "median_time_to_breach": _pct(br[br > 0].astype(float), 50) if np.any(br > 0) else None,
            "mean_trades": float(np.mean(s["n_trades"])),
        }
    codes, counts = np.unique(breach_code[breach_code > 0], return_counts=True)
    cause = {BREACH_CODE.get(int(c), str(c)): int(k) / max(int(n), 1) for c, k in zip(codes, counts)}
    y = curves.get("252") or {}
    return {
        "curves": curves,
        "terminal_alive": float(np.mean(alive)),
        "P(first payout)": float(np.mean(payouts >= 1)),
        "P(5 payouts)": float(np.mean(payouts >= 5)),
        "P(10 payouts)": float(np.mean(payouts >= 10)),
        "P(survive_1y)": y.get("P(survive)"),
        "expected_cumulative_trader_payout": float(np.mean(trader)),
        "median_cumulative_trader_payout": _pct(trader, 50),
        "p05_trader_payout": _pct(trader, 5),
        "P(breach)": float(np.mean(breach_day > 0)),
        "median_time_to_breach": _pct(breach_day[breach_day > 0].astype(float), 50) if np.any(breach_day > 0) else None,
        "breach_causes": cause,
        "mean_floor_blocks": float(np.mean(n_block_floor)),
        "mean_trades": float(np.mean(n_trades)),
        "never_traded": float(np.mean(n_trades == 0)),
        "breach_timing": _breach_timing(breach_day, n),
        "pre_breach": _pre_breach_stats(breach_day, pre_cushion, pre_locked, pre_since_payout, pre_consec),
        "state_occupancy": _state_occupancy(n_state),
    }


def _breach_timing(breach_day: np.ndarray, n: int) -> dict[str, float]:
    edges = (30, 60, 90, 180, 252, 504)
    out: dict[str, float] = {}
    prev = 0
    br = breach_day
    for h in edges:
        out[f"P(breach_by_{h})"] = float(np.mean((br > 0) & (br <= h)))
        out[f"P(breach_{prev + 1}_to_{h})"] = float(np.mean((br > prev) & (br <= h)))
        prev = h
    out["P(never_breach_504)"] = float(np.mean((br <= 0) | (br > 504)))
    return out


def _pre_breach_stats(breach_day, pre_cushion, pre_locked, pre_since_payout, pre_consec) -> dict[str, Any]:
    if pre_cushion is None or not np.any(breach_day > 0):
        return {
            "n_breaches": 0,
            "median_cushion": None,
            "mean_cushion": None,
            "frac_locked": None,
            "median_days_since_payout": None,
            "median_consecutive_losses": None,
            "frac_payout_within_8d": None,
        }
    m = breach_day > 0
    c = pre_cushion[m]
    since = pre_since_payout[m] if pre_since_payout is not None else np.full(int(np.sum(m)), np.nan)
    return {
        "n_breaches": int(np.sum(m)),
        "median_cushion": _pct(c, 50),
        "mean_cushion": float(np.nanmean(c)) if np.any(np.isfinite(c)) else None,
        "frac_locked": float(np.mean(pre_locked[m])) if pre_locked is not None else None,
        "median_days_since_payout": _pct(since.astype(float), 50),
        "median_consecutive_losses": _pct(pre_consec[m].astype(float), 50) if pre_consec is not None else None,
        "frac_payout_within_8d": float(np.mean(since <= 8)) if np.any(np.isfinite(since)) else None,
    }


def _state_occupancy(n_state) -> dict[str, float]:
    names = ("HEALTHY", "CAUTION", "DEFENSIVE", "CRITICAL", "LOCKOUT")
    if n_state is None:
        return {k: None for k in names}
    tot = float(np.sum(n_state))
    if tot <= 0:
        return {k: 0.0 for k in names}
    return {k: float(np.sum(n_state[:, i]) / tot) for i, k in enumerate(names)}


def score_components(summary: dict[str, Any]) -> dict[str, float]:
    c252 = (summary.get("curves") or {}).get("252") or {}
    c504 = (summary.get("curves") or {}).get("504") or {}
    return {
        "P(first payout)": float(summary.get("P(first payout)") or 0),
        "P(5 payouts)": float(summary.get("P(5 payouts)") or 0),
        "P(10 payouts)": float(summary.get("P(10 payouts)") or 0),
        "P(survive 1 year)": float(summary.get("P(survive_1y)") or c252.get("P(survive)") or 0),
        "P(survive 504)": float(c504.get("P(survive)") or 0),
        "median_cumulative_payout": float(summary.get("median_cumulative_trader_payout") or 0),
        "expected_cumulative_payout": float(summary.get("expected_cumulative_trader_payout") or 0),
        "downside_tail_p05_payout": float(summary.get("p05_trader_payout") or 0),
        "P(breach)": float(summary.get("P(breach)") or 0),
        "never_traded": float(summary.get("never_traded") or 0),
    }


def composite_score(comp: dict[str, float]) -> float:
    """Shown alongside components. Penalize hide-and-survive and first-payout-then-death."""
    if comp.get("never_traded", 0) > 0.80:
        return -10.0
    s1 = comp["P(survive 1 year)"]
    s504 = comp["P(survive 504)"]
    p1 = comp["P(first payout)"]
    survival_adj = comp["expected_cumulative_payout"] * (0.35 + 0.40 * s1 + 0.25 * s504)
    hide = max(0.0, 0.25 - p1)
    return (
        0.12 * p1
        + 0.18 * comp["P(5 payouts)"]
        + 0.15 * comp["P(10 payouts)"]
        + 0.18 * s1
        + 0.10 * s504
        + 0.00010 * survival_adj
        + 0.00003 * comp["median_cumulative_payout"]
        + 0.00002 * comp["downside_tail_p05_payout"]
        - 0.22 * comp["P(breach)"]
        - 0.20 * hide
    )


def classify(comp: dict[str, float]) -> str:
    if comp.get("never_traded", 0) > 0.50:
        return "PROP_PROFILE_UNSUITABLE"
    s1 = comp["P(survive 1 year)"]
    s504 = comp["P(survive 504)"]
    p1 = comp["P(first payout)"]
    p5 = comp["P(5 payouts)"]
    ev = comp["expected_cumulative_payout"]
    if (
        s1 >= 0.40
        and s504 >= 0.25
        and p1 >= 0.30
        and p5 >= 0.10
        and ev > 0
        and comp["P(breach)"] <= 0.70
    ):
        return "FUNDED_COMPATIBLE_CANDIDATE"
    if s1 >= 0.20 and p1 >= 0.20 and p5 >= 0.02 and ev > 0:
        return "BORDERLINE"
    return "PROP_PROFILE_UNSUITABLE"


def phase49_baseline_policy(book: str, profile_id: str) -> FundedPolicy:
    """Reproduce Phase 49 funded behavior: fixed fraction of initial DD, thin 25% ML cushion, ASAP payout."""
    ml = initial_max_loss(profile_id, "FUNDED")
    px = POINT_USD_MICRO[book]
    stop = DEFAULT_STOP[book] or 5.48
    one = stop * px
    # executable 1-micro if 10% of ML allows it, else that 10%
    frac = 0.10 if book != "GC" else 0.125
    risk = max(one, frac * ml) if one <= frac * ml + 1e-9 else (one if one <= 0.125 * ml + 80 else 0.0)
    if book == "NQ" and profile_id.startswith("FUNDEDNEXT"):
        risk = 187.5  # 12.5% of 1500, first executable MNQ cell
    elif book == "NQ":
        risk = 200.0
    elif book == "ES":
        risk = 90.0 if profile_id.startswith("MFFU") else 112.5
    elif book == "GC":
        risk = 250.0 if profile_id.startswith("MFFU") else 262.5
    return FundedPolicy(
        name="PHASE49_BASELINE",
        payout_mode="PAYOUT_AS_SOON_AS_ELIGIBLE",
        reserve_usd=0.25 * ml,
        use_dynamic_risk=False,
        fixed_risk_usd=risk,
        floor_block_ratio=99.0,
        block_insufficient_capacity=False,
        cap_risk_to_cushion=False,  # Phase 49 applied sized day PnL even through remaining cushion
    )
