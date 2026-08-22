"""Phase 49B — fast-pass evaluation simulator. Research only. No martingale. DRY_RUN."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional, Sequence

import numpy as np

from account_state_engine import EVAL_STATES
from phase49_prop_sim import (
    DEFAULT_STOP,
    EVAL_HORIZON,
    POINT_USD_MICRO,
    DayBundle,
    max_micros,
)
from prop_rules_v1 import REQUIRES_CONFIRMATION, load_profile

SEED = 4949
TYPICAL_WIN_R = {"NQ": 0.50, "ES": 0.50, "GC": 2.0}

# R already includes Phase 46 1-tick + commission. Extra overlays are stress-only.
BASE_SLIP_USD = {
    "NQ": 2.0 * 0.25 * 2.0,  # 2-way 1 tick * $2/pt micro
    "ES": 2.0 * 0.25 * 5.0,
    "GC": 2.0 * 0.10 * 10.0,
}
BASE_COMM_USD = {
    "NQ": 0.20 * 2.0,
    "ES": 0.08 * 5.0,
    "GC": 0.04 * 10.0,
}


@dataclass(frozen=True)
class FastPassSpec:
    name: str
    qty_normal: int = 1
    qty_accel: int = 1
    qty_defensive: int = 1
    qty_approach: int = 1
    accel_dd_frac: float = 1.01  # remaining/initial >= this AND pnl>=0 AND consec==0
    accel_min_pnl: float = 0.0
    defensive_dd_frac: float = 0.0  # remaining/initial <= this → defensive qty
    approach: str = "NONE"  # NONE | REDUCE | SKIP
    approach_mult: float = 1.5  # dist <= mult * current_unit_risk
    governor: str = "none"  # none | soft | reduced | hard
    daily_stop: str = "none"  # none | dollar | R | frac_dd
    daily_stop_value: float = 0.0
    streak: str = "none"  # none | pause_2 | pause_3 | reduce_2 | reduce_3
    day_profit_frac: float = 0.0  # stop new trades after day_pnl >= frac * consistency_cap
    expectancy_scale: float = 1.0
    slippage_mult: float = 1.0
    commission_mult: float = 1.0
    wr_flip: float = 0.0  # P(flip a winner to -1R)
    skip_day_p: float = 0.0
    block_cluster: int = 1  # 1 = iid day bootstrap; >1 consecutive historical days
    label: str = ""

    def __post_init__(self) -> None:
        for q in (self.qty_normal, self.qty_accel, self.qty_defensive, self.qty_approach):
            if int(q) < 0:
                raise ValueError("qty_negative")
        if self.qty_accel > max(self.qty_normal, 1) and self.accel_dd_frac > 1.0 + 1e-12:
            # accel disabled if threshold unreachable — allowed
            pass
        if self.qty_accel > self.qty_normal and self.qty_accel < 1:
            raise ValueError("accel_qty")


def assert_no_martingale(spec: FastPassSpec) -> None:
    if spec.qty_accel > spec.qty_normal and spec.streak.startswith("pause"):
        return
    # Acceleration after a loss is forbidden at decision time (enforced in sim).
    if spec.qty_defensive > spec.qty_normal:
        raise ValueError("martingale_forbidden:defensive_qty")
    if spec.qty_approach > spec.qty_normal:
        raise ValueError("martingale_forbidden:approach_qty")


def unit_risk_usd(book: str, stop_pts: float) -> float:
    return float(stop_pts) * float(POINT_USD_MICRO[book])


def max_executable_qty(book: str, profile_id: str, stop_pts: float) -> tuple[int, str]:
    """Largest micro qty that fits initial max_loss and firm contract cap. Never rounds up."""
    cap = max_micros(profile_id, "EVALUATION")
    ml = float(load_profile(profile_id).stage("EVALUATION").raw["max_loss"])
    per = unit_risk_usd(book, stop_pts)
    if per <= 0:
        return 0, "BLOCK_INSUFFICIENT_RISK_CAPACITY"
    by_dd = int(ml // per)
    qty = max(0, min(int(cap), by_dd))
    if qty <= 0:
        return 0, "BLOCK_INSUFFICIENT_RISK_CAPACITY"
    return qty, "OK"


def extra_cost_per_micro(book: str, spec: FastPassSpec) -> float:
    slip = float(BASE_SLIP_USD.get(book, 0.0)) * max(0.0, float(spec.slippage_mult) - 1.0)
    comm = float(BASE_COMM_USD.get(book, 0.0)) * max(0.0, float(spec.commission_mult) - 1.0)
    return slip + comm


def pack_days(days: Sequence[DayBundle], book: str) -> dict[str, Any]:
    n = len(days)
    mt = max((len(d.r) for d in days), default=1)
    R = np.zeros((n, mt), dtype=np.float64)
    RP = np.full((n, mt), float(DEFAULT_STOP[book] or 80.0), dtype=np.float64)
    NT = np.zeros(n, dtype=np.int32)
    for i, d in enumerate(days):
        k = len(d.r)
        NT[i] = k
        if k:
            R[i, :k] = d.r
            RP[i, :k] = d.risk_points
    px = float(POINT_USD_MICRO[book])
    unit = R * RP * px
    return {"n": n, "mt": int(mt), "R": R, "RP": RP, "NT": NT, "unit": unit, "px": px}


def _sample_day_index(n_hist: int, n_paths: int, horizon: int, rng: np.random.Generator, block: int) -> np.ndarray:
    block = max(1, int(block))
    if block <= 1:
        return rng.integers(0, n_hist, size=(n_paths, horizon))
    idx = np.empty((n_paths, horizon), dtype=np.int64)
    for p in range(n_paths):
        t = 0
        while t < horizon:
            start = int(rng.integers(0, n_hist))
            take = min(block, horizon - t)
            for k in range(take):
                idx[p, t + k] = (start + k) % n_hist
            t += take
    return idx


def simulate_batch(
    pack: dict[str, Any],
    *,
    book: str,
    profile_id: str,
    spec: FastPassSpec,
    n_paths: int,
    rng: np.random.Generator,
    mode: str = "bootstrap",
    horizon: int = EVAL_HORIZON,
) -> dict[str, Any]:
    """Vectorized-over-paths evaluation. Qty never increases after a loss."""
    assert_no_martingale(spec)
    st = load_profile(profile_id).stage("EVALUATION").raw
    max_loss = float(st["max_loss"])
    base_target = float(st["profit_target"])
    ratio_max = float(st.get("consistency_ratio_max") or 0.0)
    min_days = st.get("minimum_trading_days")
    min_days_i = 0 if min_days in (None, "NONE") else int(min_days)
    cap = max_micros(profile_id, "EVALUATION")
    stop_pts = float(DEFAULT_STOP[book] or 80.0)
    max_q, cap_note = max_executable_qty(book, profile_id, stop_pts)
    qn = min(int(spec.qty_normal), max_q, cap)
    qa = min(int(spec.qty_accel), max_q, cap)
    qd = min(int(spec.qty_defensive), max_q, cap)
    qp = min(int(spec.qty_approach), max_q, cap)
    if qn <= 0:
        n = int(n_paths)
        return {
            "terminal": np.full(n, "NO_SIZE", dtype=object),
            "days": np.zeros(n, dtype=np.int32),
            "trades": np.zeros(n, dtype=np.int32),
            "pnl": np.zeros(n),
            "max_dd": np.zeros(n),
            "consistency_delayed": np.zeros(n, dtype=bool),
            "adjusted_target": np.full(n, base_target),
            "skipped": np.zeros(n, dtype=np.int32),
            "n_accel": np.zeros(n, dtype=np.int32),
            "cap_note": cap_note,
            "max_executable": max_q,
        }

    n_hist = int(pack["n"])
    mt = int(pack["mt"])
    UNIT = pack["unit"]
    R = pack["R"]
    NT = pack["NT"]
    extra = extra_cost_per_micro(book, spec)
    unit_r = unit_risk_usd(book, stop_pts)
    n = int(n_paths)
    is_fn = profile_id.startswith("FUNDEDNEXT")
    start_eq = float(st.get("nominal_account_size") or 50000)
    lock_at = st.get("mll_locks_at")
    lock_at_f = float(lock_at) if lock_at not in (None, REQUIRES_CONFIRMATION) else 50100.0

    if mode == "chrono":
        idx = np.zeros((n, horizon), dtype=np.int64)
        for p in range(n):
            start = int(rng.integers(0, n_hist)) if n > 1 else 0
            for t in range(horizon):
                idx[p, t] = (start + t) % n_hist
    else:
        idx = _sample_day_index(n_hist, n, horizon, rng, spec.block_cluster)

    pnl = np.zeros(n)
    equity = np.full(n, start_eq)
    mll_pnl = np.full(n, -max_loss)
    mll_eq = np.full(n, start_eq - max_loss)
    locked = np.zeros(n, dtype=bool)
    best_day = np.zeros(n)
    trading_days = np.zeros(n, dtype=np.int32)
    trades_n = np.zeros(n, dtype=np.int32)
    skipped = np.zeros(n, dtype=np.int32)
    n_accel = np.zeros(n, dtype=np.int32)
    consec = np.zeros(n, dtype=np.int32)
    pause_left = np.zeros(n, dtype=np.int32)
    peak = np.zeros(n)
    max_dd = np.zeros(n)
    delayed = np.zeros(n, dtype=bool)
    terminal = np.zeros(n, dtype=np.int8)  # 0 open 1 pass 2 breach 3 timeout
    done_days = np.zeros(n, dtype=np.int32)
    flip_p = float(spec.wr_flip)
    skip_p = float(spec.skip_day_p)
    esc = float(spec.expectancy_scale)
    typical = TYPICAL_WIN_R.get(book, 0.5)

    streak_n = 0
    streak_pause = spec.streak.startswith("pause_")
    streak_reduce = spec.streak.startswith("reduce_")
    if spec.streak.endswith("2"):
        streak_n = 2
    elif spec.streak.endswith("3"):
        streak_n = 3

    for t in range(int(horizon)):
        active = terminal == 0
        if not np.any(active):
            break
        di = idx[:, t]
        remaining = np.where(is_fn, equity - mll_eq, pnl - mll_pnl)
        realized = np.where(is_fn, equity - start_eq, pnl)
        dist = np.maximum(0.0, base_target - realized)
        dd_frac = np.divide(remaining, max_loss, out=np.zeros(n), where=max_loss > 0)

        qty = np.full(n, qn, dtype=np.int32)
        can_accel = (
            (dd_frac >= float(spec.accel_dd_frac) - 1e-12)
            & (realized >= float(spec.accel_min_pnl) - 1e-12)
            & (consec == 0)
            & (qa > qn)
        )
        qty = np.where(can_accel, qa, qty)
        n_accel = np.where(active & can_accel, n_accel + 1, n_accel)
        if spec.defensive_dd_frac > 0:
            qty = np.where(dd_frac <= float(spec.defensive_dd_frac) + 1e-12, np.minimum(qty, qd), qty)
        if streak_reduce and streak_n:
            qty = np.where(consec >= streak_n, np.minimum(qty, qd if qd > 0 else 1), qty)
        # never increase after a loss
        qty = np.where(consec > 0, np.minimum(qty, qn), qty)

        if spec.approach == "REDUCE":
            risk_now = qty.astype(np.float64) * unit_r
            near = (dist <= float(spec.approach_mult) * risk_now) & (dist > 0)
            qty = np.where(near, np.minimum(qty, qp), qty)
        skip_rest = np.zeros(n, dtype=bool)
        if spec.approach == "SKIP":
            risk_now = np.maximum(qty, 1).astype(np.float64) * unit_r
            skip_rest = (dist > 0) & (dist <= float(spec.approach_mult) * risk_now)

        if streak_pause and streak_n:
            start_pause = active & (pause_left == 0) & (consec >= streak_n)
            pause_left = np.where(start_pause, 1, pause_left)
        paused = pause_left > 0
        if skip_p > 0:
            skipped_day = rng.random(n) < skip_p
        else:
            skipped_day = np.zeros(n, dtype=bool)

        qty = np.where(paused | skipped_day | skip_rest, 0, qty)
        qty = np.maximum(0, np.minimum(qty, max_q))

        day_pnl = np.zeros(n)
        took = np.zeros(n, dtype=bool)
        stop_usd = np.zeros(n)
        if spec.daily_stop == "dollar":
            stop_usd = np.full(n, float(spec.daily_stop_value))
        elif spec.daily_stop == "R":
            stop_usd = float(spec.daily_stop_value) * qty.astype(np.float64) * unit_r
        elif spec.daily_stop == "frac_dd":
            stop_usd = float(spec.daily_stop_value) * np.maximum(remaining, 0.0)
        stopped = np.zeros(n, dtype=bool)
        cap_day = ratio_max * base_target if ratio_max > 0 else 0.0
        profit_cap = cap_day * float(spec.day_profit_frac) if spec.day_profit_frac > 0 and cap_day > 0 else 0.0

        nt = NT[di]
        for k in range(mt):
            alive = active & (k < nt) & (qty > 0) & ~stopped
            if not np.any(alive):
                continue
            r_use = R[di, k]
            if flip_p > 0:
                flip = alive & (r_use > 0) & (rng.random(n) < flip_p)
                r_use = np.where(flip, -1.0, r_use)
            raw = qty.astype(np.float64) * r_use * pack["RP"][di, k] * pack["px"] * esc
            cost = qty.astype(np.float64) * extra
            tr = raw - cost

            next_risk = qty.astype(np.float64) * unit_r
            gov_skip = np.zeros(n, dtype=bool)
            if spec.governor == "hard" and cap_day > 0:
                gov_skip = day_pnl >= cap_day - 1e-9
                proj = day_pnl + typical * next_risk
                gov_skip = gov_skip | ((proj > cap_day + 1e-9) & (day_pnl > 0))
            elif spec.governor == "soft" and cap_day > 0:
                proj_day = day_pnl + typical * next_risk
                proj_best = np.maximum(best_day, np.maximum(proj_day, 0.0))
                proj_tot = np.maximum(realized + typical * next_risk, 0.0)
                gov_skip = (proj_tot >= 0.5 * base_target) & (proj_best > ratio_max * np.maximum(proj_tot, 1e-9) + 1e-12) & (day_pnl > 0)
            if profit_cap > 0:
                gov_skip = gov_skip | ((day_pnl >= profit_cap - 1e-9) & (day_pnl > 0))
            if spec.governor == "reduced" and cap_day > 0:
                red = np.ones(n)
                red = np.where(day_pnl >= 0.75 * cap_day, 0.25, red)
                red = np.where((day_pnl >= 0.40 * cap_day) & (day_pnl < 0.75 * cap_day), 0.5, red)
                q2 = np.maximum(0, np.floor(qty * red + 1e-12)).astype(np.int32)
                q2 = np.minimum(q2, qty)
                # never round up
                scale = np.divide(q2.astype(np.float64), np.maximum(qty, 1), out=np.zeros(n), where=qty > 0)
                tr = np.where(alive, tr * scale, tr)
                qty_eff_zero = alive & (q2 <= 0)
                gov_skip = gov_skip | qty_eff_zero

            apply = alive & ~gov_skip
            skipped = np.where(alive & gov_skip, skipped + 1, skipped)
            if spec.daily_stop != "none":
                hit = apply & (stop_usd > 0) & ((day_pnl + tr) <= -stop_usd + 1e-12)
                # clip the hitting trade to the stop
                room = -stop_usd - day_pnl
                tr = np.where(hit, np.minimum(tr, room), tr)
                stopped = stopped | hit

            pnl = np.where(apply, pnl + tr, pnl)
            equity = np.where(apply, equity + tr, equity)
            day_pnl = np.where(apply, day_pnl + tr, day_pnl)
            trades_n = np.where(apply, trades_n + 1, trades_n)
            took = took | apply
            consec = np.where(apply & (tr < 0), consec + 1, consec)
            consec = np.where(apply & (tr >= 0), 0, consec)

            remaining = np.where(is_fn, equity - mll_eq, pnl - mll_pnl)
            brk = active & apply & (remaining <= 0)
            terminal = np.where(brk, 2, terminal)
            done_days = np.where(brk, trading_days + took.astype(np.int32), done_days)

        if took.any():
            trading_days = np.where(active & took, trading_days + 1, trading_days)
        pause_left = np.where(paused & active, np.maximum(0, pause_left - 1), pause_left)
        consec = np.where(paused & active, 0, consec)  # cool-off after a pause day
        best_day = np.where(active, np.maximum(best_day, day_pnl), best_day)

        if is_fn:
            cand = np.maximum(mll_eq, equity - max_loss)
            mll_eq = np.where(active & ~locked, np.maximum(mll_eq, cand), mll_eq)
            hit_lock = active & ~locked & (mll_eq >= lock_at_f - 1e-12)
            mll_eq = np.where(hit_lock, lock_at_f, mll_eq)
            locked = locked | hit_lock
            realized = equity - start_eq
            remaining = equity - mll_eq
            eod_breach = active & (terminal == 0) & (equity <= mll_eq + 1e-9)
        else:
            mll_pnl = np.where(active, np.maximum(mll_pnl, pnl - max_loss), mll_pnl)
            realized = pnl
            remaining = pnl - mll_pnl
            eod_breach = active & (terminal == 0) & (pnl <= mll_pnl + 1e-9)
        terminal = np.where(eod_breach, 2, terminal)
        done_days = np.where(eod_breach, trading_days, done_days)

        peak = np.where(active, np.maximum(peak, realized), peak)
        max_dd = np.where(active, np.maximum(max_dd, peak - realized), max_dd)

        if ratio_max > 0:
            adj = np.maximum(base_target, np.divide(best_day, ratio_max, out=np.full(n, base_target), where=best_day > 0))
        else:
            adj = np.full(n, base_target)
        delayed = delayed | ((adj > base_target + 1e-9) & (realized >= base_target) & (terminal == 0))
        passed = (terminal == 0) & (realized + 1e-9 >= adj) & (trading_days >= min_days_i)
        terminal = np.where(passed, 1, terminal)
        done_days = np.where(passed, trading_days, done_days)

    still = terminal == 0
    terminal = np.where(still, 3, terminal)
    done_days = np.where(still, trading_days, done_days)
    labels = np.array(["OPEN", "PASS", "BREACH", "TIMEOUT"], dtype=object)
    term = labels[terminal]
    realized_end = (equity - start_eq) if is_fn else pnl
    if ratio_max > 0:
        adj_end = np.maximum(base_target, np.divide(best_day, ratio_max, out=np.full(n, base_target), where=best_day > 0))
    else:
        adj_end = np.full(n, base_target)
    return {
        "terminal": term,
        "days": done_days.astype(np.int32),
        "trades": trades_n.astype(np.int32),
        "pnl": realized_end,
        "max_dd": max_dd,
        "consistency_delayed": delayed,
        "adjusted_target": adj_end,
        "skipped": skipped.astype(np.int32),
        "n_accel": n_accel.astype(np.int32),
        "cap_note": cap_note,
        "max_executable": max_q,
        "qty_normal_used": qn,
        "qty_accel_used": qa,
    }


def _pct(vals: np.ndarray, p: float) -> Optional[float]:
    if vals.size == 0:
        return None
    return float(np.percentile(vals, p))


def summarize(batch: dict[str, Any], *, profile_id: str, spec: FastPassSpec, book: str) -> dict[str, Any]:
    term = np.asarray(batch["terminal"], dtype=object)
    n = len(term) or 1
    passed = term == "PASS"
    breach = term == "BREACH"
    timeout = term == "TIMEOUT"
    p_pass = float(np.mean(passed))
    p_breach = float(np.mean(breach))
    days = np.asarray(batch["days"], dtype=float)[passed]
    trades = np.asarray(batch["trades"], dtype=float)[passed]
    dds = np.asarray(batch["max_dd"], dtype=float)[passed]
    expected_attempts = (1.0 / p_pass) if p_pass > 1e-12 else None
    st = load_profile(profile_id).stage("EVALUATION").raw
    p5 = st.get("first_5_purchase_price")
    p6 = st.get("purchase_6_plus_price")
    cost = None
    cost_note = "MFFU evaluation purchase price is REQUIRES_CONFIRMATION — attempts reported, dollar cost not invented"
    cost_status = REQUIRES_CONFIRMATION
    if p5 not in (None, REQUIRES_CONFIRMATION) and expected_attempts is not None:
        att = float(expected_attempts)
        if att <= 5:
            cost = att * float(p5)
        else:
            cost = 5 * float(p5) + (att - 5) * float(p6 or p5)
        cost_note = "FundedNext first_5=69.99 then 79.99; geometric attempts until first pass"
        cost_status = "CONFIRMED"
    se_pass = float(np.sqrt(p_pass * (1.0 - p_pass) / n)) if n else 0.0
    return {
        "name": spec.name,
        "book": book,
        "profile": profile_id,
        "label": spec.label,
        "qty_normal": spec.qty_normal,
        "qty_accel": spec.qty_accel,
        "qty_defensive": spec.qty_defensive,
        "qty_approach": spec.qty_approach,
        "accel_dd_frac": spec.accel_dd_frac,
        "accel_min_pnl": spec.accel_min_pnl,
        "defensive_dd_frac": spec.defensive_dd_frac,
        "approach": spec.approach,
        "approach_mult": spec.approach_mult,
        "governor": spec.governor,
        "daily_stop": spec.daily_stop,
        "daily_stop_value": spec.daily_stop_value,
        "streak": spec.streak,
        "day_profit_frac": spec.day_profit_frac,
        "n_paths": int(len(term)),
        "P(pass)": p_pass,
        "P(pass)_se": se_pass,
        "P(breach)": p_breach,
        "P(timeout)": float(np.mean(timeout)),
        "median_days_to_pass": _pct(days, 50),
        "p75_days_to_pass": _pct(days, 75),
        "p90_days_to_pass": _pct(days, 90),
        "p95_days_to_pass": _pct(days, 95),
        "P(pass <=10d)": float(np.mean(days <= 10)) if days.size else 0.0,
        "P(pass <=14d)": float(np.mean(days <= 14)) if days.size else 0.0,
        "P(pass <=20d)": float(np.mean(days <= 20)) if days.size else 0.0,
        "P(pass <=30d)": float(np.mean(days <= 30)) if days.size else 0.0,
        "median_trades_to_pass": _pct(trades, 50),
        "median_max_drawdown_before_pass": _pct(dds, 50),
        "probability_consistency_rule_delays_pass": float(np.mean(batch["consistency_delayed"])),
        "average_adjusted_profit_target": float(np.mean(batch["adjusted_target"])),
        "expected_number_of_attempts": expected_attempts,
        "expected_evaluation_cost": cost,
        "expected_evaluation_cost_note": cost_note,
        "expected_evaluation_cost_status": cost_status,
        "mean_accel_days": float(np.mean(batch["n_accel"])),
        "max_executable": batch.get("max_executable"),
        "cap_note": batch.get("cap_note"),
        "qty_normal_used": batch.get("qty_normal_used"),
        "qty_accel_used": batch.get("qty_accel_used"),
        "EVAL_STATES": list(EVAL_STATES),
    }


def pool_from_batch(batch: dict[str, Any], *, book: str, profile: str, dd_frac_note: str = "executable_qty") -> dict[str, Any]:
    term = np.asarray(batch["terminal"], dtype=object)
    passed = term == "PASS"
    dur = np.asarray(batch["days"], dtype=np.int32)
    return {
        "book": book,
        "profile": profile,
        "dd_frac": dd_frac_note,
        "n": int(len(term)),
        "empirical_P(pass)": float(np.mean(passed)),
        "passed": passed,
        "days": dur,
        "phase49_median_days_to_pass": _pct(dur[passed].astype(float), 50) if np.any(passed) else None,
    }


def eval_objective(row: dict[str, Any], *, baseline_median: Optional[float] = None) -> float:
    """Pass/breach/speed/tail — not fastest-median-only."""
    p = float(row.get("P(pass)") or 0.0)
    b = float(row.get("P(breach)") or 0.0)
    med = float(row.get("median_days_to_pass") or 252)
    p90 = float(row.get("p90_days_to_pass") or 252)
    p10 = float(row.get("P(pass <=10d)") or 0.0)
    p14 = float(row.get("P(pass <=14d)") or 0.0)
    att = float(row.get("expected_number_of_attempts") or 10)
    tail_pen = 0.0
    if p < 0.40:
        tail_pen += 0.25
    if b > 0.55:
        tail_pen += 0.20
    if p90 > 120:
        tail_pen += 0.08
    speed = 0.012 * min(med, 80) + 0.004 * min(p90, 150)
    hit = 0.15 * p14 + 0.08 * p10
    base = p - 0.50 * b - speed + hit - 0.03 * min(att, 12) - tail_pen
    if baseline_median and med > baseline_median * 1.05:
        base -= 0.05
    return float(base)


def classify_fast_pass(row: dict[str, Any], baseline: dict[str, Any], *, degraded: Optional[dict[str, Any]] = None, flywheel_improved: Optional[bool] = None) -> str:
    p = float(row.get("P(pass)") or 0)
    b = float(row.get("P(breach)") or 0)
    med = row.get("median_days_to_pass")
    p90 = row.get("p90_days_to_pass")
    bp = float(baseline.get("P(pass)") or 0)
    bb = float(baseline.get("P(breach)") or 0)
    bm = float(baseline.get("median_days_to_pass") or 252)
    if med is None:
        return "FAST_PASS_UNSUPPORTED"
    med = float(med)
    faster = med <= 0.85 * bm or (bm - med) >= 8
    pass_ok = p >= max(0.45, bp - 0.12)
    breach_ok = b <= min(0.55, bb + 0.15)
    tail_ok = (p90 is None) or float(p90) <= max(90.0, 1.15 * float(baseline.get("p90_days_to_pass") or 90))
    deg_ok = True
    if degraded is not None:
        deg_ok = float(degraded.get("P(pass)") or 0) >= 0.38 and float(degraded.get("P(breach)") or 1) <= 0.62
    fw_ok = True if flywheel_improved is None else bool(flywheel_improved)
    if faster and pass_ok and breach_ok and tail_ok and deg_ok and fw_ok:
        return "FAST_PASS_VIABLE"
    if faster and p >= 0.35:
        return "FAST_PASS_BORDERLINE"
    return "FAST_PASS_UNSUPPORTED"


def classify_tier(rows: list[dict[str, Any]], *, median_cap: int) -> dict[str, Any]:
    hit = [r for r in rows if r.get("median_days_to_pass") is not None and float(r["median_days_to_pass"]) <= median_cap]
    robust = [r for r in hit if float(r.get("P(pass)") or 0) >= 0.45 and float(r.get("P(breach)") or 1) <= 0.55]
    if not robust:
        return {"tier_days": median_cap, "status": "FAST_PASS_UNSUPPORTED", "best": None, "n_hit": len(hit)}
    best = max(robust, key=lambda r: eval_objective(r))
    return {"tier_days": median_cap, "status": "HIT", "best": best["name"], "n_hit": len(hit), "row": best}


def chain_times(eval_row: dict[str, Any], funded_summary: dict[str, Any]) -> dict[str, Any]:
    """Purchase → pass → first payout → next eval purchase → next funded (trading days)."""
    med = eval_row.get("median_days_to_pass")
    p = float(eval_row.get("P(pass)") or 0)
    att = eval_row.get("expected_number_of_attempts")
    first_po = funded_summary.get("expected_days_to_first_payout") or funded_summary.get("phase50_median_first_payout_day")
    # geometric wait for a pass
    wait_pass = None
    if med is not None and att is not None:
        wait_pass = float(med) * float(att)  # includes failed-attempt time approximation
    to_first_payout = None
    if wait_pass is not None and first_po is not None:
        to_first_payout = float(wait_pass) + float(first_po)
    to_next_eval = to_first_payout  # payout finances next purchase same day in Phase 51 model
    to_next_funded = None
    if to_next_eval is not None and med is not None:
        to_next_funded = float(to_next_eval) + float(med)
    return {
        "expected_time_to_funded_td": wait_pass,
        "expected_time_to_first_payout_td": to_first_payout,
        "expected_time_to_next_evaluation_purchase_td": to_next_eval,
        "expected_time_to_next_funded_account_td": to_next_funded,
        "P(pass)": p,
        "median_days_to_pass": med,
    }
