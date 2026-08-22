"""Phase 52 — Monte Carlo of the actual execution policy on frozen NQ day-bootstrap paths."""
from __future__ import annotations

from typing import Any, Optional

import numpy as np

from phase49_prop_sim import DEFAULT_STOP, EVAL_HORIZON, POINT_USD_MICRO
from phase49b_engine import pack_days
from phase52_degradation import DegradationMonitor
from phase52_policy import (
    CONSISTENCY_RATIO,
    DAILY_STOP_FRAC,
    FAST_QTY,
    MAX_LOSS,
    MLL_LOCK_AT,
    PROFIT_TARGET,
    SAFE_QTY,
    START_EQUITY,
    UNIT_RISK_USD,
)

FN = "FUNDEDNEXT_FLEX_50K"


def simulate_policy(
    pack: dict[str, Any],
    *,
    variant: str,
    n_paths: int,
    rng: np.random.Generator,
    near_rule: str = "ONE_FAST_R",
    expectancy_scale: float = 1.0,
    wr_flip: float = 0.0,
    slippage_mult: float = 1.0,
    commission_mult: float = 1.0,
    block_cluster: int = 1,
    skip_day_p: float = 0.0,
    delayed_exit_p: float = 0.0,
    miss_entry_p: float = 0.0,
    horizon: int = EVAL_HORIZON,
) -> dict[str, Any]:
    """Variants: SAFE1, RAW2, GOV2, GOV2_DEMOTE, GOV2_DEMOTE_NEAR."""
    n_hist = int(pack["n"])
    mt = int(pack["mt"])
    R = pack["R"]
    RP = pack["RP"]
    NT = pack["NT"]
    px = pack["px"]
    n = int(n_paths)
    extra = (slippage_mult - 1.0) * 1.0 + (commission_mult - 1.0) * 0.40  # NQ $ extra / micro from 49b bases

    if block_cluster <= 1:
        idx = rng.integers(0, n_hist, size=(n, horizon))
    else:
        idx = np.empty((n, horizon), dtype=np.int64)
        for p in range(n):
            t = 0
            while t < horizon:
                start = int(rng.integers(0, n_hist))
                take = min(block_cluster, horizon - t)
                for k in range(take):
                    idx[p, t + k] = (start + k) % n_hist
                t += take

    use_gov = variant in ("GOV2", "GOV2_DEMOTE", "GOV2_DEMOTE_NEAR")
    use_demote = variant in ("GOV2_DEMOTE", "GOV2_DEMOTE_NEAR")
    use_near = variant == "GOV2_DEMOTE_NEAR"
    base_qty = SAFE_QTY if variant == "SAFE1" else FAST_QTY

    pnl = np.zeros(n)
    equity = np.full(n, START_EQUITY)
    mll = np.full(n, START_EQUITY - MAX_LOSS)
    locked = np.zeros(n, dtype=bool)
    best_day = np.zeros(n)
    trading_days = np.zeros(n, dtype=np.int32)
    trades_n = np.zeros(n, dtype=np.int32)
    consec = np.zeros(n, dtype=np.int32)
    last_qty = np.full(n, base_qty, dtype=np.int32)
    demoted = np.zeros(n, dtype=bool)
    n_demote = np.zeros(n, dtype=np.int32)
    n_dstop = np.zeros(n, dtype=np.int32)
    n_near = np.zeros(n, dtype=np.int32)
    max_daily_loss = np.zeros(n)
    terminal = np.zeros(n, dtype=np.int8)  # 0 open 1 pass 2 breach 3 timeout
    done_days = np.zeros(n, dtype=np.int32)
    breach_why = np.full(n, "", dtype=object)
    # per-path monitors (Python objects — n_paths up to 10k is OK)
    mons = [DegradationMonitor() for _ in range(n)] if use_demote else None

    for t in range(horizon):
        active = terminal == 0
        if not np.any(active):
            break
        di = idx[:, t]
        session_eq = equity.copy()
        rem_open = equity - mll
        day_pnl = np.zeros(n)
        stopped = np.zeros(n, dtype=bool)
        took = np.zeros(n, dtype=bool)
        skipped_day = (rng.random(n) < skip_day_p) if skip_day_p > 0 else np.zeros(n, dtype=bool)
        nt = NT[di]
        for k in range(mt):
            alive = active & (k < nt) & ~stopped & ~skipped_day
            if not np.any(alive):
                continue
            if miss_entry_p > 0:
                alive = alive & (rng.random(n) >= miss_entry_p)
            realized = equity - START_EQUITY
            dist = np.maximum(0.0, PROFIT_TARGET - realized)
            rem = equity - mll
            near = np.zeros(n, dtype=bool)
            if use_near:
                if near_rule == "ONE_FAST_R":
                    near = dist <= UNIT_RISK_USD * FAST_QTY + 1e-9
                elif near_rule == "ONE_SAFE_R":
                    near = dist <= UNIT_RISK_USD * SAFE_QTY + 1e-9
                elif near_rule == "PCT_90":
                    near = dist <= 0.10 * PROFIT_TARGET
                elif near_rule == "PCT_95":
                    near = dist <= 0.05 * PROFIT_TARGET
                elif near_rule == "PCT_80":
                    near = dist <= 0.20 * PROFIT_TARGET
            n_near = np.where(alive & near, n_near + 1, n_near)
            daily_used = session_eq - equity
            if use_gov:
                thr = np.maximum(0.0, DAILY_STOP_FRAC * rem_open)
                daily_cap = np.maximum(0.0, thr - daily_used)
            else:
                thr = np.zeros(n)
                daily_cap = rem
            cap2 = (rem >= 2 * UNIT_RISK_USD - 1e-9) & (daily_cap >= 2 * UNIT_RISK_USD - 1e-9)
            cap1 = (rem >= UNIT_RISK_USD - 1e-9) & (daily_cap >= UNIT_RISK_USD - 1e-9)
            state_is_safe = demoted | near | (variant == "SAFE1")
            qty = np.where(state_is_safe, np.where(cap1, SAFE_QTY, 0), np.where(cap2, FAST_QTY, np.where(cap1, SAFE_QTY, 0))).astype(np.int32)
            qty = np.where(consec > 0, np.minimum(qty, np.maximum(last_qty, 0)), qty)
            qty = np.minimum(qty, FAST_QTY)
            alive = alive & (qty > 0)
            r_use = R[di, k]
            if wr_flip > 0:
                flip = alive & (r_use > 0) & (rng.random(n) < wr_flip)
                r_use = np.where(flip, -1.0, r_use)
            if delayed_exit_p > 0:
                delay = alive & (rng.random(n) < delayed_exit_p)
                r_use = np.where(delay & (r_use > 0), r_use * 0.85, r_use)
            raw = qty.astype(np.float64) * r_use * RP[di, k] * px * expectancy_scale
            cost = qty.astype(np.float64) * extra
            tr = raw - cost
            if use_gov:
                trial_eq = equity + tr
                loss_after = session_eq - trial_eq
                hit = alive & (thr > 0) & (loss_after + 1e-9 >= thr)
                max_loss_left = np.maximum(0.0, thr - (session_eq - equity))
                tr = np.where(hit, -max_loss_left, tr)
                stopped = stopped | hit
                n_dstop = np.where(hit, n_dstop + 1, n_dstop)
            pnl = np.where(alive, pnl + tr, pnl)
            equity = np.where(alive, equity + tr, equity)
            day_pnl = np.where(alive, day_pnl + tr, day_pnl)
            trades_n = np.where(alive, trades_n + 1, trades_n)
            took = took | alive
            last_qty = np.where(alive, qty, last_qty)
            consec = np.where(alive & (tr < 0), consec + 1, consec)
            consec = np.where(alive & (tr >= 0), 0, consec)
            if use_demote and mons is not None:
                for i in np.flatnonzero(alive):
                    unit = qty[i] * RP[di[i], k] * px
                    r_obs = (tr[i] / unit) if unit else 0.0
                    info = mons[i].observe(r_obs)
                    if info["demoted"] and not demoted[i]:
                        demoted[i] = True
                        n_demote[i] += 1
            rem = equity - mll
            brk = active & alive & (rem <= 0)
            terminal = np.where(brk, 2, terminal)
            done_days = np.where(brk, trading_days + took.astype(np.int32), done_days)
            breach_why = np.where(brk, "MLL", breach_why)

        if took.any():
            trading_days = np.where(active & took, trading_days + 1, trading_days)
        daily_loss = np.maximum(0.0, session_eq - equity)
        max_daily_loss = np.where(active, np.maximum(max_daily_loss, daily_loss), max_daily_loss)
        best_day = np.where(active, np.maximum(best_day, day_pnl), best_day)
        cand = equity - MAX_LOSS
        mll = np.where(active & ~locked, np.maximum(mll, cand), mll)
        hit_lock = active & ~locked & (mll >= MLL_LOCK_AT - 1e-12)
        mll = np.where(hit_lock, MLL_LOCK_AT, mll)
        locked = locked | hit_lock
        eod_breach = active & (terminal == 0) & (equity <= mll + 1e-9)
        terminal = np.where(eod_breach, 2, terminal)
        done_days = np.where(eod_breach, trading_days, done_days)
        breach_why = np.where(eod_breach, "EOD_MLL", breach_why)
        realized = equity - START_EQUITY
        adj = np.maximum(PROFIT_TARGET, np.divide(best_day, CONSISTENCY_RATIO, out=np.full(n, PROFIT_TARGET), where=best_day > 0))
        passed = (terminal == 0) & (realized + 1e-9 >= adj)
        terminal = np.where(passed, 1, terminal)
        done_days = np.where(passed, trading_days, done_days)

    still = terminal == 0
    terminal = np.where(still, 3, terminal)
    done_days = np.where(still, trading_days, done_days)
    labels = np.array(["OPEN", "PASS", "BREACH", "TIMEOUT"], dtype=object)
    return {
        "terminal": labels[terminal],
        "days": done_days.astype(np.int32),
        "trades": trades_n.astype(np.int32),
        "n_daily_stop": n_dstop.astype(np.int32),
        "n_demote": n_demote.astype(np.int32),
        "n_near": n_near.astype(np.int32),
        "max_daily_loss": max_daily_loss,
        "demoted": demoted,
        "breach_why": breach_why,
        "pnl": equity - START_EQUITY,
        "variant": variant,
        "near_rule": near_rule,
    }


def summarize(batch: dict[str, Any]) -> dict[str, Any]:
    term = np.asarray(batch["terminal"], dtype=object)
    n = len(term) or 1
    passed = term == "PASS"
    days = np.asarray(batch["days"], dtype=float)[passed]

    def pct(a, p):
        return float(np.percentile(a, p)) if a.size else None

    p_pass = float(np.mean(passed))
    all_days = np.asarray(batch["days"], dtype=float)
    why = batch["breach_why"]
    causes = {}
    for w in set(why.tolist()):
        if w:
            causes[str(w)] = float(np.mean(why == w))
    return {
        "variant": batch.get("variant"),
        "near_rule": batch.get("near_rule"),
        "n_paths": int(n),
        "P(pass)": p_pass,
        "P(pass)_se": float(np.sqrt(p_pass * (1 - p_pass) / n)) if n else 0.0,
        "P(breach)": float(np.mean(term == "BREACH")),
        "P(timeout)": float(np.mean(term == "TIMEOUT")),
        "P(fail)": float(1.0 - p_pass),
        "median_days_to_pass": pct(days, 50),
        "p75_days_to_pass": pct(days, 75),
        "p90_days_to_pass": pct(days, 90),
        "P(pass <=14d)": float(np.mean(days <= 14)) if days.size else 0.0,
        "P(pass <=20d)": float(np.mean(days <= 20)) if days.size else 0.0,
        "P(pass_and_<=14d)": float(np.mean(passed & (all_days <= 14))),
        "P(pass_and_<=20d)": float(np.mean(passed & (all_days <= 20))),
        "max_observed_daily_loss_mean": float(np.mean(batch["max_daily_loss"])),
        "daily_stop_frequency": float(np.mean(np.asarray(batch["n_daily_stop"]) > 0)),
        "mean_daily_stop_events": float(np.mean(batch["n_daily_stop"])),
        "demotion_frequency": float(np.mean(batch["demoted"])),
        "breach_causes": causes,
        "expected_attempts": (1.0 / p_pass) if p_pass > 1e-12 else None,
    }
