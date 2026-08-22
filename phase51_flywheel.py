"""Phase 51 — prop capital flywheel. Event-driven, empirical Phase 49/50 pools."""
from __future__ import annotations

import heapq
from dataclasses import dataclass
from typing import Any, Optional

import numpy as np

from phase51_pools import FN, MFFU
from prop_rules_v1 import REQUIRES_CONFIRMATION

CAL_PER_TD = 365.0 / 252.0
HORIZONS_CAL = (30, 60, 90, 180, 365, 730)
MAX_ACCOUNTS = 8

REINVEST_POLICIES = (
    "REINVEST_NEXT_ACCOUNT_FIRST",
    "REINVEST_50_PERCENT",
    "REINVEST_FIXED_DOLLAR",
    "REINVEST_ALL_UNTIL_ACCOUNT_CAP",
    "CASH_RESERVE_FIRST",
)
EXPANSIONS = ("MFFU_ONLY", "FUNDEDNEXT_ONLY", "ALTERNATING_FIRMS", "BEST_EXPECTED_VALUE_FIRM")


def td_to_cal(td: int) -> int:
    if td <= 0:
        return 0
    return max(1, int(round(float(td) * CAL_PER_TD)))


@dataclass
class FlywheelSpec:
    name: str
    book: str = "NQ"
    expansion: str = "FUNDEDNEXT_ONLY"
    reinvest: str = "REINVEST_NEXT_ACCOUNT_FIRST"
    start_cash: float = 500.0
    cash_reserve: float = 0.0
    reinvest_fixed: float = 80.0
    mffu_price: float = 100.0
    mffu_price_label: str = "HYPOTHETICAL"
    mffu_cap: int = 3
    mffu_cap_label: str = "CONFIRMED"
    fn_cap: int = 3
    fn_cap_label: str = "HYPOTHETICAL"
    eval_mode: str = "BASELINE_PHASE49"
    fast_pass_p: Optional[float] = None
    fast_pass_median_td: Optional[int] = None
    horizon_cal: int = 730
    p_pass_delta: float = 0.0
    payout_scale: float = 1.0
    cost_scale: float = 1.0
    duration_scale: float = 1.0
    expectancy_scale: float = 1.0  # extra payout haircut + earlier breach
    activation_delay_cal: int = 0  # 0; firm activation delay REQUIRES_CONFIRMATION
    label: str = ""


def _price_fn(n_prior: int, first5: float, plus: float) -> float:
    return float(first5 if n_prior < 5 else plus)


def classify_replication(m: dict[str, float]) -> str:
    p_sf = float(m.get("P(self_funding_by_1y)") or 0)
    p_ex = float(m.get("probability_bankroll_exhausted_before_self_funding") or 0)
    e_fun = float(m.get("h365_expected_active_funded") or 0)
    ratio = float(m.get("payout_dollars_per_eval_dollar") or 0)
    if e_fun >= 0.80 and p_sf >= 0.50 and p_ex <= 0.35 and ratio >= 2.0:
        return "REPLICATION_VIABLE"
    if e_fun >= 0.35 and p_sf >= 0.20 and ratio >= 1.0:
        return "REPLICATION_BORDERLINE"
    return "REPLICATION_UNSUPPORTED"


def _sample_eval(pool: dict[str, Any], rng: np.random.Generator, spec: FlywheelSpec) -> tuple[bool, int]:
    p_emp = float(pool["empirical_P(pass)"])
    passed_a = pool["passed"]
    days_a = pool["days"]
    p = p_emp + float(spec.p_pass_delta)
    if spec.eval_mode == "FAST_PASS_TARGET_SCENARIO" and spec.fast_pass_p is not None:
        p = float(spec.fast_pass_p) + float(spec.p_pass_delta)
    p = min(0.99, max(0.01, p))
    ok = bool(rng.random() < p)
    if spec.eval_mode == "FAST_PASS_TARGET_SCENARIO" and spec.fast_pass_median_td:
        med = max(1.0, float(spec.fast_pass_median_td) * spec.duration_scale)
        dur = int(round(rng.lognormal(np.log(med), 0.35)))
        lo = 4 if pool["profile"].startswith("MFFU") else 1
        dur = max(lo, min(252, dur))
        if not ok:
            fail = days_a[~passed_a]
            base = int(np.median(fail)) if len(fail) else 40
            dur = max(lo, int(round(base * spec.duration_scale * (med / max(1.0, float(pool.get("phase49_median_days_to_pass") or base))))))
        return ok, dur
    src = days_a[passed_a] if ok else days_a[~passed_a]
    if len(src) == 0:
        src = days_a
    dur = int(src[int(rng.integers(0, len(src)))])
    dur = max(1, int(round(dur * spec.duration_scale)))
    return ok, min(252, dur)


def _sample_funded(pool: dict[str, Any], rng: np.random.Generator, spec: FlywheelSpec) -> tuple[np.ndarray, np.ndarray, int]:
    i = int(rng.integers(0, int(pool["n"])))
    k = int(pool["n_po"][i])
    days = pool["po_day"][i, :k].astype(int).copy()
    amts = pool["po_amt"][i, :k].astype(float).copy() * spec.payout_scale * spec.expectancy_scale
    br = int(pool["breach_day"][i])
    if spec.expectancy_scale < 1.0 - 1e-12 and br > 0:
        br = max(5, int(round(br * spec.expectancy_scale)))
    return days, amts, br


def _next_firm(spec: FlywheelSpec, last: Optional[str], mffu_used: int, fn_used: int, pools: dict) -> Optional[str]:
    mffu_room = mffu_used < spec.mffu_cap
    fn_room = fn_used < spec.fn_cap
    if spec.expansion == "MFFU_ONLY":
        return MFFU if mffu_room else None
    if spec.expansion == "FUNDEDNEXT_ONLY":
        return FN if fn_room else None
    if spec.expansion == "ALTERNATING_FIRMS":
        prefer = FN if last == MFFU else MFFU
        if prefer == MFFU and mffu_room:
            return MFFU
        if prefer == FN and fn_room:
            return FN
        if mffu_room:
            return MFFU
        if fn_room:
            return FN
        return None
    # BEST_EXPECTED_VALUE_FIRM
    scores = []
    if mffu_room:
        ev = pools["eval"][f"{spec.book}->{MFFU}"]
        fu = pools["funded"][f"{spec.book}->{MFFU}"]
        scores.append((float(ev["empirical_P(pass)"]) * float(fu["phase50_expected_payout"]) / max(spec.mffu_price, 1.0), MFFU))
    if fn_room:
        ev = pools["eval"][f"{spec.book}->{FN}"]
        fu = pools["funded"][f"{spec.book}->{FN}"]
        px = 69.99
        scores.append((float(ev["empirical_P(pass)"]) * float(fu["phase50_expected_payout"]) / px, FN))
    if not scores:
        return None
    scores.sort(reverse=True)
    return scores[0][1]


def simulate_paths(spec: FlywheelSpec, pools: dict[str, Any], *, n_paths: int, rng: np.random.Generator) -> dict[str, Any]:
    fn_px = pools["fn_price"]
    first5 = float(fn_px["first_5_purchase_price"]) * spec.cost_scale
    plus = float(fn_px["purchase_6_plus_price"]) * spec.cost_scale
    mffu_px = float(spec.mffu_price) * spec.cost_scale
    H = int(spec.horizon_cal)
    n = int(n_paths)

    snap_h = {h: [] for h in HORIZONS_CAL}
    metrics = {
        "self_fund_day": np.full(n, -1, dtype=np.int32),
        "exhausted": np.zeros(n, dtype=bool),
        "exhausted_before_sf": np.zeros(n, dtype=bool),
        "eval_attempts": np.zeros(n, dtype=np.int32),
        "eval_fail": np.zeros(n, dtype=np.int32),
        "eval_spend": np.zeros(n),
        "trader_payout": np.zeros(n),
        "gross_payout": np.zeros(n),
        "reinvested": np.zeros(n),
        "withdrawn": np.zeros(n),
        "end_cash": np.zeros(n),
        "end_pool": np.zeros(n),
        "funded_created": np.zeros(n, dtype=np.int32),
        "end_funded": np.zeros(n, dtype=np.int32),
        "max_funded": np.zeros(n, dtype=np.int32),
    }

    pri = {"PAY": 0, "EVAL": 1, "BREACH": 2}

    for p in range(n):
        personal = float(spec.start_cash)
        pool = 0.0
        withdrawn = 0.0
        eval_spend = 0.0
        trader = 0.0
        reinvested = 0.0
        fn_buys = 0
        last_firm = None
        sf_day = -1
        evals: list[dict[str, Any]] = []
        fundeds: list[dict[str, Any]] = []
        heap: list[tuple[int, int, int, str, int]] = []
        seq = 0
        max_fun = 0
        created = 0
        failed = 0

        def push(day: int, kind: str, idx: int) -> None:
            nonlocal seq
            heapq.heappush(heap, (int(day), pri[kind], seq, kind, idx))
            seq += 1

        def slots(firm: str) -> int:
            cap = spec.mffu_cap if firm == MFFU else spec.fn_cap
            n_e = sum(1 for e in evals if e["firm"] == firm and not e["done"])
            n_f = sum(1 for f in fundeds if f["firm"] == firm and f["alive"])
            return cap - n_e - n_f

        def occupied(firm: str) -> int:
            return sum(1 for x in evals if x["firm"] == firm and not x["done"]) + sum(
                1 for x in fundeds if x["firm"] == firm and x["alive"]
            )

        def try_buy(day: int) -> None:
            nonlocal personal, pool, eval_spend, reinvested, fn_buys, last_firm, sf_day
            while True:
                firm = _next_firm(spec, last_firm, occupied(MFFU), occupied(FN), pools)
                if firm is None or slots(firm) <= 0:
                    return
                price = mffu_px if firm == MFFU else _price_fn(fn_buys, first5, plus)
                reserve = float(spec.cash_reserve) if spec.reinvest == "CASH_RESERVE_FIRST" else 0.0
                usable_personal = max(0.0, personal - reserve)
                if pool + usable_personal + 1e-9 < price:
                    return
                from_pool = min(pool, price)
                from_pers = price - from_pool
                if from_pers <= 1e-9 and sf_day < 0 and eval_spend > 1e-9:
                    sf_day = day
                pool -= from_pool
                personal -= from_pers
                reinvested += from_pool
                eval_spend += price
                if firm == FN:
                    fn_buys += 1
                last_firm = firm
                key = f"{spec.book}->{firm}"
                ok, td = _sample_eval(pools["eval"][key], rng, spec)
                evals.append({"firm": firm, "ok": ok, "done": False})
                push(day + td_to_cal(td), "EVAL", len(evals) - 1)

        def mark_sf(day: int) -> None:
            nonlocal sf_day
            if sf_day >= 0 or eval_spend <= 1e-9:
                return
            cands = []
            if spec.expansion != "FUNDEDNEXT_ONLY":
                cands.append(mffu_px)
            if spec.expansion != "MFFU_ONLY":
                cands.append(_price_fn(fn_buys, first5, plus))
            need = min(cands) if cands else first5
            if pool + 1e-9 >= need:
                sf_day = day

        def on_payout(day: int, amt: float) -> None:
            nonlocal personal, pool, withdrawn, trader
            trader += amt
            mode = spec.reinvest
            if mode == "REINVEST_50_PERCENT":
                pool += 0.50 * amt
                withdrawn += 0.50 * amt
            elif mode == "REINVEST_FIXED_DOLLAR":
                take = min(float(spec.reinvest_fixed), amt)
                pool += take
                withdrawn += amt - take
            elif mode == "REINVEST_ALL_UNTIL_ACCOUNT_CAP":
                if spec.expansion == "MFFU_ONLY":
                    full = slots(MFFU) <= 0
                elif spec.expansion == "FUNDEDNEXT_ONLY":
                    full = slots(FN) <= 0
                else:
                    full = slots(MFFU) <= 0 and slots(FN) <= 0
                if full:
                    withdrawn += amt
                else:
                    pool += amt
            elif mode == "CASH_RESERVE_FIRST":
                need = max(0.0, float(spec.cash_reserve) - personal)
                to_cash = min(need, amt)
                personal += to_cash
                pool += amt - to_cash
            else:
                pool += amt
            try_buy(day)
            mark_sf(day)

        def n_funded_at(h: int) -> int:
            return sum(1 for f in fundeds if f["start"] <= h < f["breach_cal"])

        def record_horizon(h: int) -> None:
            snap_h[h].append(
                {
                    "funded": n_funded_at(h),
                    "eval_spend": eval_spend,
                    "trader": trader,
                    "reinvested": reinvested,
                    "withdrawn": withdrawn,
                    "attempts": len(evals),
                    "fails": failed,
                }
            )

        try_buy(0)
        next_h = 0
        hs = list(HORIZONS_CAL)
        while heap:
            day, _pr, _s, kind, ai = heapq.heappop(heap)
            if day > H:
                break
            while next_h < len(hs) and hs[next_h] < day:
                record_horizon(hs[next_h])
                next_h += 1
            if kind == "EVAL":
                e = evals[ai]
                if e["done"]:
                    continue
                e["done"] = True
                if not e["ok"]:
                    failed += 1
                    try_buy(day)
                else:
                    key = f"{spec.book}->{e['firm']}"
                    po_d, po_a, br = _sample_funded(pools["funded"][key], rng, spec)
                    start = day + spec.activation_delay_cal
                    created += 1
                    fobj = {
                        "firm": e["firm"],
                        "start": start,
                        "alive": True,
                        "breach_cal": (start + td_to_cal(max(1, br))) if br > 0 else 10**9,
                        "pay_cursor": 0,
                        "pay_days": [start + td_to_cal(int(d)) for d in po_d],
                        "pay_amts": [float(a) for a in po_a],
                    }
                    fundeds.append(fobj)
                    fi = len(fundeds) - 1
                    for pd in fobj["pay_days"]:
                        if pd <= fobj["breach_cal"]:
                            push(pd, "PAY", fi)
                    push(fobj["breach_cal"], "BREACH", fi)
                    try_buy(day)
            elif kind == "PAY":
                f = fundeds[ai]
                if not f["alive"] or day >= f["breach_cal"]:
                    continue
                am = 0.0
                cur = f["pay_cursor"]
                while cur < len(f["pay_days"]) and f["pay_days"][cur] == day:
                    am += f["pay_amts"][cur]
                    cur += 1
                f["pay_cursor"] = cur
                if am > 0:
                    on_payout(day, am)
            elif kind == "BREACH":
                f = fundeds[ai]
                if f["alive"] and day >= f["breach_cal"]:
                    f["alive"] = False
                    try_buy(day)
            max_fun = max(max_fun, n_funded_at(day))

        while next_h < len(hs):
            record_horizon(hs[next_h])
            next_h += 1

        nfun_end = n_funded_at(H)
        cheapest = min(mffu_px, first5)
        reserve = float(spec.cash_reserve) if spec.reinvest == "CASH_RESERVE_FIRST" else 0.0
        can_buy = (pool + max(0.0, personal - reserve)) >= cheapest - 1e-9
        in_eval = any(not e["done"] for e in evals)
        exhausted = (not can_buy) and nfun_end == 0 and not in_eval
        metrics["self_fund_day"][p] = sf_day
        metrics["exhausted"][p] = exhausted
        metrics["exhausted_before_sf"][p] = bool(exhausted and sf_day < 0)
        metrics["eval_attempts"][p] = len(evals)
        metrics["eval_fail"][p] = failed
        metrics["eval_spend"][p] = eval_spend
        metrics["trader_payout"][p] = trader
        metrics["gross_payout"][p] = trader
        metrics["reinvested"][p] = reinvested
        metrics["withdrawn"][p] = withdrawn
        metrics["end_cash"][p] = personal
        metrics["end_pool"][p] = pool
        metrics["funded_created"][p] = created
        metrics["end_funded"][p] = nfun_end
        metrics["max_funded"][p] = max_fun

    return _summarize(spec, metrics, snap_h, n, first5, mffu_px)


def _summarize(spec: FlywheelSpec, m: dict, snap_h: dict, n: int, first5: float, mffu_px: float) -> dict[str, Any]:
    def mean(a):
        return float(np.mean(a))

    def med(a):
        return float(np.median(a))

    sf = m["self_fund_day"]
    out: dict[str, Any] = {
        "name": spec.name,
        "book": spec.book,
        "expansion": spec.expansion,
        "reinvest": spec.reinvest,
        "start_cash": spec.start_cash,
        "mffu_price": spec.mffu_price,
        "mffu_price_label": spec.mffu_price_label,
        "fn_cap": spec.fn_cap,
        "fn_cap_label": spec.fn_cap_label,
        "mffu_cap": spec.mffu_cap,
        "mffu_cap_label": spec.mffu_cap_label,
        "eval_mode": spec.eval_mode,
        "n_paths": n,
        "label": spec.label,
    }
    for h in HORIZONS_CAL:
        rows = snap_h[h]
        fun = np.array([r["funded"] for r in rows], dtype=float) if rows else np.zeros(n)
        out[f"h{h}_expected_active_funded"] = mean(fun)
        out[f"h{h}_median_active_funded"] = med(fun)
        out[f"h{h}_P(1 funded)"] = float(np.mean(fun >= 1))
        out[f"h{h}_P(2 funded)"] = float(np.mean(fun >= 2))
        out[f"h{h}_P(3 funded)"] = float(np.mean(fun >= 3))
        cap = spec.mffu_cap if spec.expansion == "MFFU_ONLY" else spec.fn_cap if spec.expansion == "FUNDEDNEXT_ONLY" else spec.mffu_cap + spec.fn_cap
        out[f"h{h}_P(max firm capacity)"] = float(np.mean(fun >= cap - 1e-9)) if spec.expansion in ("MFFU_ONLY", "FUNDEDNEXT_ONLY") else float(np.mean(fun >= min(spec.mffu_cap, spec.fn_cap)))
        out[f"h{h}_eval_spend"] = mean(np.array([r["eval_spend"] for r in rows])) if rows else 0.0
        out[f"h{h}_trader_payout"] = mean(np.array([r["trader"] for r in rows])) if rows else 0.0
        out[f"h{h}_reinvested"] = mean(np.array([r["reinvested"] for r in rows])) if rows else 0.0
        out[f"h{h}_withdrawn"] = mean(np.array([r["withdrawn"] for r in rows])) if rows else 0.0
        out[f"h{h}_attempts"] = mean(np.array([r["attempts"] for r in rows], dtype=float)) if rows else 0.0
        out[f"h{h}_fails"] = mean(np.array([r["fails"] for r in rows], dtype=float)) if rows else 0.0

    spend = m["eval_spend"]
    created = m["funded_created"].astype(float)
    payout = m["trader_payout"]
    out["expected_active_funded_end"] = mean(m["end_funded"])
    out["median_active_funded_end"] = med(m["end_funded"])
    out["total_evaluation_attempts"] = mean(m["eval_attempts"])
    out["total_failed_evaluations"] = mean(m["eval_fail"])
    out["total_evaluation_spend"] = mean(spend)
    out["total_trader_payout"] = mean(payout)
    out["total_reinvested"] = mean(m["reinvested"])
    out["total_personal_cash_withdrawn"] = mean(m["withdrawn"])
    out["external_capital_required"] = float(spec.start_cash) - mean(m["end_cash"])
    out["ending_personal_cash"] = mean(m["end_cash"])
    out["ending_reinvestment_pool"] = mean(m["end_pool"])
    out["attempts_per_funded_account"] = float(np.mean(np.divide(m["eval_attempts"], np.maximum(created, 1.0))))
    out["cost_per_funded_account"] = float(np.mean(np.divide(spend, np.maximum(created, 1.0))))
    out["payout_dollars_per_eval_dollar"] = float(np.mean(np.divide(payout, np.maximum(spend, 1e-9))))
    out["funded_accounts_created_per_$100_eval_spend"] = float(np.mean(np.divide(created * 100.0, np.maximum(spend, 1e-9))))
    # days per new funded: horizon / created for those with created>0
    with_c = created > 0
    if np.any(with_c):
        out["days_per_new_funded_account"] = float(np.median(spec.horizon_cal / created[with_c]))
    else:
        out["days_per_new_funded_account"] = None
    out["median_self_funding_day"] = float(np.median(sf[sf >= 0])) if np.any(sf >= 0) else None
    out["P(self_funding_by_30d)"] = float(np.mean((sf >= 0) & (sf <= 30)))
    out["P(self_funding_by_60d)"] = float(np.mean((sf >= 0) & (sf <= 60)))
    out["P(self_funding_by_90d)"] = float(np.mean((sf >= 0) & (sf <= 90)))
    out["P(self_funding_by_180d)"] = float(np.mean((sf >= 0) & (sf <= 180)))
    out["P(self_funding_by_1y)"] = float(np.mean((sf >= 0) & (sf <= 365)))
    out["probability_bankroll_exhausted_before_self_funding"] = mean(m["exhausted_before_sf"])
    out["P(bankroll_exhausted)"] = mean(m["exhausted"])
    out["h365_expected_active_funded"] = out["h365_expected_active_funded"]
    out["conservation_gap"] = abs(
        float(spec.start_cash) + mean(payout) - mean(spend) - mean(m["withdrawn"]) - mean(m["end_cash"]) - mean(m["end_pool"])
    )
    out["classification"] = classify_replication(out)
    out["fn_price_first5"] = first5
    out["mffu_price_used"] = mffu_px
    out["REQUIRES_CONFIRMATION_notes"] = [
        x
        for x in (
            "MFFU_EVAL_PRICE=REQUIRES_CONFIRMATION" if spec.mffu_price_label != "CONFIRMED" else None,
            "FUNDEDNEXT_MAX_FUNDED_ACCOUNTS=REQUIRES_CONFIRMATION" if spec.fn_cap_label != "CONFIRMED" else None,
            "copy_trading=REQUIRES_CONFIRMATION (independent accounts)",
            "funded_activation_delay=REQUIRES_CONFIRMATION (modeled as 0 calendar days)",
        )
        if x
    ]
    return out
