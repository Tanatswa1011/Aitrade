"""Phase 51 — prop account replication flywheel. DRY_RUN. Does not alter Phase 49/50 reports."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import numpy as np

from aitrade_operating_policy import load_operating_policy
from phase34_validate import assert_frozen
from phase49_trade_audit import write_csv
from phase51_flywheel import FlywheelSpec, HORIZONS_CAL, simulate_paths
from phase51_pools import FN, MFFU, _stable, build_all_pools, load_eval_matrix_row

ROOT = Path(__file__).resolve().parent
VALIDATION = ROOT / "phase51_validation.json"
DOCS = ROOT / "docs" / "PHASE51_PROP_ACCOUNT_REPLICATION_FLYWHEEL.md"
REGISTRY = ROOT / "docs" / "STRATEGY_REGISTRY.md"
POLICY_PATH = ROOT / "config" / "aitrade_operating_policy_v1.json"
FW = ROOT / "reports" / "phase51_account_flywheel"
REINV = ROOT / "reports" / "phase51_reinvestment_policy"
GROWTH = ROOT / "reports" / "phase51_growth_curves"
BANK = ROOT / "reports" / "phase51_bankroll_risk"
STRESS = ROOT / "reports" / "phase51_stress_tests"

N_PATHS_FINAL = 10_000
N_PATHS_GRID = 2_500
N_PATHS_FAST = 2_000
N_POOL_EVAL = 4_000
N_POOL_FUNDED = 3_000


def _pctf(x: Any) -> str:
    if x is None:
        return "n/a"
    try:
        return f"{100.0 * float(x):.1f}%"
    except (TypeError, ValueError):
        return "n/a"


def _numf(x: Any, nd: int = 1) -> str:
    if x is None:
        return "n/a"
    try:
        return f"{float(x):.{nd}f}"
    except (TypeError, ValueError):
        return "n/a"


def update_registry() -> None:
    text = REGISTRY.read_text(encoding="utf-8")
    marker = "### Prop account replication flywheel (Phase 51)"
    block = """### Prop account replication flywheel (Phase 51)

| Field | Value |
|-------|--------|
| Phase | 51 |
| Status | See `phase51_validation.json` verdict |
| Question | Can payouts from funded NQ (and secondary ES) accounts finance additional evaluations into a self-funding prop-capital flywheel? |
| Forbidden | Retune GC/NQ/ES; freeze ES; enable broker; write production risk/payout; buy/activate accounts; invent MFFU price or FundedNext funded-account cap |
| Evidence | `docs/PHASE51_PROP_ACCOUNT_REPLICATION_FLYWHEEL.md`, `reports/phase51_*`, `phase51_validation.json`, `tests_phase51.py` |
| Frozen impact | None. Phase 26 / 30 hashes unchanged. DRY_RUN only. |

"""
    if marker in text:
        start = text.index(marker)
        rest = text[start + len(marker) :]
        cuts = [i for i in (rest.find("\n### "), rest.find("\n## ")) if i >= 0]
        end_rel = min(cuts) if cuts else len(rest)
        text = text[:start] + block + rest[end_rel:].lstrip("\n")
    else:
        needle = "## RESEARCH-ONLY / RETIRED"
        idx = text.find(needle)
        insert_at = text.find("\n", idx) + 1
        text = text[:insert_at] + "\n" + block + text[insert_at:]
    REGISTRY.write_text(text, encoding="utf-8")


def _base_fn(**kw) -> FlywheelSpec:
    d = dict(
        book="NQ",
        expansion="FUNDEDNEXT_ONLY",
        reinvest="REINVEST_NEXT_ACCOUNT_FIRST",
        start_cash=500.0,
        fn_cap=3,
        fn_cap_label="HYPOTHETICAL",
        mffu_price=100.0,
        mffu_price_label="HYPOTHETICAL",
        mffu_cap=3,
        mffu_cap_label="CONFIRMED",
    )
    d.update(kw)
    return FlywheelSpec(**d)


def render_docs(payload: dict[str, Any]) -> str:
    prim = payload.get("primary") or {}
    classes = payload.get("classifications") or {}
    inputs = payload.get("phase49_50_inputs") or {}
    return f"""# Phase 51 — Prop Account Replication & Capital Flywheel

`DRY_RUN`. No broker. No account purchases. Frozen strategy logic was not modified. Phase 49/50 report files were not overwritten. Operating-policy numerics were not written.

## 1. Phase 49 / 50 inputs used

{json.dumps(inputs, indent=2, default=str)}

These are **research cells**, not production risk settings. Architecture can swap in Phase 49B fast-pass distributions later (`eval_mode=FAST_PASS_TARGET_SCENARIO`).

## 2. Starting-bankroll assumptions

Primary: `$500` external. Sensitivity: `$100`, `$250`, `$500`, `$1,000`. No unlimited retries. No top-ups after t=0.

## 3. Evaluation prices

- FundedNext Flex 50K: **confirmed** first 5 = `$69.99`, 6+ = `$79.99`, reset = `$77.99` (retries modeled as **new purchases**, not resets).
- MFFU Rapid EOD 50K: **`REQUIRES_CONFIRMATION`**. Hypothetical grid `$60/$80/$100/$125/$150` only, labeled HYPOTHETICAL.

## 4. Account-cap assumptions

- MFFU max funded accounts = **3 (confirmed)**.
- FundedNext max funded accounts = **`REQUIRES_CONFIRMATION`**. Sensitivity caps `1/3/5` are **hypothetical**. Copy trading is unconfirmed → accounts run independently.

## 5. Reinvestment policies

`REINVEST_NEXT_ACCOUNT_FIRST` (primary), `REINVEST_50_PERCENT`, `REINVEST_FIXED_DOLLAR` ($80), `REINVEST_ALL_UNTIL_ACCOUNT_CAP`, `CASH_RESERVE_FIRST` ($250).

Payout split is tracked as `amount_reinvested` / remaining pool / `amount` withdrawn personally.

## 6–11. Growth, spend, payouts, reinvestment, personal cash

See `reports/phase51_growth_curves/` and the primary table below. Horizons are **calendar days** (trading-day durations converted at 365/252).

| Model | Class | E[funded] 1y | P(1) | P(2) | P(3) | eval spend | trader payout | reinvested | withdrawn | P(self-fund 1y) |
|-------|-------|-------------:|-----:|-----:|-----:|-----------:|--------------:|-----------:|----------:|------------------:|
{chr(10).join(
    f"| {k} | {v.get('classification')} | {_numf(v.get('h365_expected_active_funded'))} | {_pctf(v.get('h365_P(1 funded)'))} | {_pctf(v.get('h365_P(2 funded)'))} | {_pctf(v.get('h365_P(3 funded)'))} | {_numf(v.get('total_evaluation_spend'), 0)} | {_numf(v.get('total_trader_payout'), 0)} | {_numf(v.get('total_reinvested'), 0)} | {_numf(v.get('total_personal_cash_withdrawn'), 0)} | {_pctf(v.get('P(self_funding_by_1y)'))} |"
    for k, v in (payload.get("primary_rows") or prim).items()
)}

## 12–13. Self-funding date and probability by horizon

Primary FN_ONLY $500:

- median self-funding day: `{_numf((payload.get("headline") or {{}}).get("median_self_funding_day"), 0)}`
- P(30d) `{_pctf((payload.get("headline") or {{}}).get("P(self_funding_by_30d)"))}`
- P(60d) `{_pctf((payload.get("headline") or {{}}).get("P(self_funding_by_60d)"))}`
- P(90d) `{_pctf((payload.get("headline") or {{}}).get("P(self_funding_by_90d)"))}`
- P(180d) `{_pctf((payload.get("headline") or {{}}).get("P(self_funding_by_180d)"))}`
- P(1y) `{_pctf((payload.get("headline") or {{}}).get("P(self_funding_by_1y)"))}`

## 14. Probability external bankroll is exhausted

See `reports/phase51_bankroll_risk/`. Headline P(exhausted before self-funding) = `{_pctf((payload.get("headline") or {{}}).get("probability_bankroll_exhausted_before_self_funding"))}`.

## 15. Replication-efficiency metrics

Headline: cost/funded `{_numf((payload.get("headline") or {{}}).get("cost_per_funded_account"), 0)}`; days/new funded `{_numf((payload.get("headline") or {{}}).get("days_per_new_funded_account"), 0)}`; payout per eval dollar `{_numf((payload.get("headline") or {{}}).get("payout_dollars_per_eval_dollar"), 2)}`; funded per $100 spend `{_numf((payload.get("headline") or {{}}).get("funded_accounts_created_per_$100_eval_spend"), 2)}`.

## 16. Withdrawal vs growth frontier

`reports/phase51_reinvestment_policy/frontier.csv` — aggressive reinvestment vs 50% income-first vs cash-reserve-first. Components are not collapsed into one score.

## 17. Stress-test outcomes

`reports/phase51_stress_tests/`. Pass −10pp, first-payout haircut via payout −25%, costs +25%, duration +50%, expectancy scale 0.70.

## 18. Final replication classification

{json.dumps(classes, indent=2)}

GC remains **PROP_PROFILE_UNSUITABLE** (not run as a replication candidate). ES is secondary research only and is **not promoted**. FundedNext cap and MFFU price stay unconfirmed for production conclusions.

## 19. Frozen-hash integrity

- GC: `{payload["frozen"]["gc"]}`
- NQ: `{payload["frozen"]["nq"]}`
- Paper journals empty: `{payload["frozen"].get("paper_journals_empty")}`
- ES not frozen: `{payload.get("es_not_frozen")}`

## 20. DRY_RUN confirmation

- execution_default: `{payload.get("execution_default")}`
- broker_execution: `{payload.get("broker_execution")}`
- operating policy risk_per_trade still null: `{payload.get("policy_risk_still_null")}`
- no accounts purchased or activated

## What this phase did not do

No strategy retune. No Phase 49/50 source-result overwrite. No ES freeze. No live execution. No production risk/payout lock. No invented MFFU purchase price. No invented FundedNext funded-account cap.
"""


def run(n_final: int = N_PATHS_FINAL, n_grid: int = N_PATHS_GRID, n_fast: int = N_PATHS_FAST, n_eval: int = N_POOL_EVAL, n_funded: int = N_POOL_FUNDED) -> dict[str, Any]:
    frozen_before = assert_frozen()
    if not frozen_before.get("ok"):
        raise RuntimeError(f"FROZEN_INTEGRITY_FAIL_BEFORE:{frozen_before}")
    for d in (FW, REINV, GROWTH, BANK, STRESS):
        d.mkdir(parents=True, exist_ok=True)

    pools = build_all_pools(n_eval=n_eval, n_funded=n_funded, books=("NQ", "ES"))
    nq_mffu = load_eval_matrix_row("NQ", MFFU)
    nq_fn = load_eval_matrix_row("NQ", FN)
    inputs = {
        "NQ->MFFU_eval": {"dd_frac": nq_mffu["dd_frac"], "P(pass)": nq_mffu["P(pass)"], "median_days": nq_mffu["median_days_to_pass"], "policy": "FIXED", "note": "research only"},
        "NQ->FN_eval": {"dd_frac": nq_fn["dd_frac"], "P(pass)": nq_fn["P(pass)"], "median_days": nq_fn["median_days_to_pass"], "policy": "FIXED", "note": "research only"},
        "NQ_funded_phase50": {
            k: {
                "P(first)": pools["funded"][k]["phase50_P(first payout)"],
                "P(5)": pools["funded"][k]["phase50_P(5 payouts)"],
                "P(10)": pools["funded"][k]["phase50_P(10 payouts)"],
                "P(survive_1y)": pools["funded"][k]["phase50_P(survive_1y)"],
                "E[payout]": pools["funded"][k]["phase50_expected_payout"],
                "payout_mode": pools["funded"][k]["payout_mode"],
                "reserve_usd": pools["funded"][k]["reserve_usd"],
            }
            for k in ("NQ->MFFU_RAPID_EOD_50K", "NQ->FUNDEDNEXT_FLEX_50K")
        },
        "mffu_price": pools["mffu_price"],
        "fn_price": pools["fn_price"],
        "caps": pools["caps"],
    }

    rng = np.random.default_rng(_stable("flywheel"))
    primary_specs = [
        _base_fn(name="NQ_FN_ONLY_NEXT_500", expansion="FUNDEDNEXT_ONLY", start_cash=500.0, label="primary"),
        _base_fn(name="NQ_MFFU_ONLY_NEXT_500", expansion="MFFU_ONLY", start_cash=500.0, label="primary_mffu_price_hypothetical"),
        _base_fn(name="NQ_ALTERNATING_NEXT_500", expansion="ALTERNATING_FIRMS", start_cash=500.0, label="primary_mixed_hypothetical"),
        _base_fn(name="NQ_BEST_EV_NEXT_500", expansion="BEST_EXPECTED_VALUE_FIRM", start_cash=500.0, label="primary_mixed_hypothetical"),
    ]
    primary_rows = {}
    for spec in primary_specs:
        primary_rows[spec.name] = simulate_paths(spec, pools, n_paths=n_final, rng=rng)

    # bankroll
    bank_rows = []
    for cash in (100.0, 250.0, 500.0, 1000.0):
        spec = _base_fn(name=f"NQ_FN_BANK_{int(cash)}", start_cash=cash)
        bank_rows.append(simulate_paths(spec, pools, n_paths=n_grid, rng=rng))

    # reinvestment / frontier
    reinv_rows = []
    for mode, extra in (
        ("REINVEST_NEXT_ACCOUNT_FIRST", {}),
        ("REINVEST_50_PERCENT", {}),
        ("REINVEST_FIXED_DOLLAR", {"reinvest_fixed": 80.0}),
        ("REINVEST_ALL_UNTIL_ACCOUNT_CAP", {}),
        ("CASH_RESERVE_FIRST", {"cash_reserve": 250.0, "start_cash": 500.0}),
    ):
        spec = _base_fn(name=f"NQ_FN_{mode}", reinvest=mode, **extra)
        reinv_rows.append(simulate_paths(spec, pools, n_paths=n_grid, rng=rng))

    # FN cap hypothetical
    cap_rows = []
    for cap in (1, 3, 5):
        spec = _base_fn(name=f"NQ_FN_CAP_{cap}", fn_cap=cap, fn_cap_label="HYPOTHETICAL")
        cap_rows.append(simulate_paths(spec, pools, n_paths=n_grid, rng=rng))

    # MFFU hypothetical prices
    mffu_price_rows = []
    for px in (60.0, 80.0, 100.0, 125.0, 150.0):
        spec = _base_fn(name=f"NQ_MFFU_PRICE_{int(px)}", expansion="MFFU_ONLY", mffu_price=px, mffu_price_label="HYPOTHETICAL")
        mffu_price_rows.append(simulate_paths(spec, pools, n_paths=n_grid, rng=rng))

    # fast-pass sensitivity
    fast_rows = []
    for med in (10, 14, 20):
        for p in (0.40, 0.50, 0.60, 0.65, 0.70, 0.75):
            spec = _base_fn(
                name=f"NQ_FN_FAST_m{med}_p{int(p*100)}",
                eval_mode="FAST_PASS_TARGET_SCENARIO",
                fast_pass_median_td=med,
                fast_pass_p=p,
                label="sensitivity_until_phase49B",
            )
            fast_rows.append(simulate_paths(spec, pools, n_paths=n_fast, rng=rng))

    # stress on headline
    stress_rows = []
    for name, kw in (
        ("PASS_MINUS_10PP", {"p_pass_delta": -0.10}),
        ("PAYOUT_MINUS_25PCT", {"payout_scale": 0.75}),
        ("COST_PLUS_25PCT", {"cost_scale": 1.25}),
        ("DURATION_PLUS_50PCT", {"duration_scale": 1.50}),
        ("EXPECTANCY_DEGRADE", {"expectancy_scale": 0.70, "payout_scale": 0.70}),
        ("COMBINED_BEAR", {"p_pass_delta": -0.10, "payout_scale": 0.75, "cost_scale": 1.25, "duration_scale": 1.50}),
    ):
        spec = _base_fn(name=f"STRESS_{name}", **kw)
        stress_rows.append(simulate_paths(spec, pools, n_paths=n_grid, rng=rng))

    # ES secondary
    es_rows = []
    for exp in ("FUNDEDNEXT_ONLY", "MFFU_ONLY"):
        spec = _base_fn(name=f"ES_{exp}_500", book="ES", expansion=exp, start_cash=500.0, label="secondary_es_not_promoted")
        es_rows.append(simulate_paths(spec, pools, n_paths=n_fast, rng=rng))

    headline = primary_rows["NQ_FN_ONLY_NEXT_500"]
    classes = {k: v["classification"] for k, v in primary_rows.items()}
    classes["GC"] = "PROP_PROFILE_UNSUITABLE"
    classes["ES_FUNDEDNEXT_ONLY"] = es_rows[0]["classification"]
    classes["ES_MFFU_ONLY"] = es_rows[1]["classification"]
    for r in stress_rows:
        classes[r["name"]] = r["classification"]

    write_csv(FW / "primary_models.csv", list(primary_rows.values()))
    (FW / "primary_models.json").write_text(json.dumps(primary_rows, indent=2, default=str), encoding="utf-8")
    (FW / "phase49_50_inputs.json").write_text(json.dumps(inputs, indent=2, default=str), encoding="utf-8")
    write_csv(REINV / "reinvestment_grid.csv", reinv_rows)
    write_csv(REINV / "frontier.csv", reinv_rows)
    write_csv(GROWTH / "growth_curves.csv", list(primary_rows.values()) + bank_rows + cap_rows)
    (GROWTH / "horizons.json").write_text(json.dumps(list(HORIZONS_CAL)), encoding="utf-8")
    write_csv(BANK / "bankroll_grid.csv", bank_rows)
    write_csv(BANK / "mffu_hypothetical_prices.csv", mffu_price_rows)
    write_csv(BANK / "fn_hypothetical_caps.csv", cap_rows)
    write_csv(STRESS / "stress_grid.csv", stress_rows)
    write_csv(STRESS / "fast_pass_sensitivity.csv", fast_rows)
    write_csv(STRESS / "es_secondary.csv", es_rows)
    (REINV / "reinvestment_grid.json").write_text(json.dumps(reinv_rows, indent=2, default=str), encoding="utf-8")
    (STRESS / "stress_grid.json").write_text(json.dumps(stress_rows, indent=2, default=str), encoding="utf-8")

    proc = subprocess.run([sys.executable, "-m", "unittest", "tests_phase51", "-v"], cwd=str(ROOT), capture_output=True, text=True)
    frozen_after = assert_frozen()
    policy = load_operating_policy()
    policy_doc = json.loads(POLICY_PATH.read_text(encoding="utf-8"))
    tests_ok = proc.returncode == 0
    frozen_ok = bool(frozen_after.get("ok"))
    risk_null = policy_doc.get("numerics_pending_simulation", {}).get("risk_per_trade") is None
    p49_untouched = EVAL_MATRIX_OK()
    verdict = (
        "PHASE51_PROP_FLYWHEEL_RESEARCH_READY"
        if tests_ok and frozen_ok and risk_null and p49_untouched
        else "PHASE51_PROP_FLYWHEEL_RESEARCH_BLOCKED"
    )
    payload = {
        "phase": 51,
        "verdict": verdict,
        "execution_default": policy.execution_default,
        "broker_execution": False,
        "dry_run": True,
        "policy_risk_still_null": risk_null,
        "es_not_frozen": not (ROOT / "strategy_frozen" / "es_dvp_phase47.json").exists(),
        "n_paths_final": n_final,
        "n_paths_grid": n_grid,
        "phase49_50_inputs": inputs,
        "headline": headline,
        "primary_rows": primary_rows,
        "classifications": classes,
        "gc_status": "PROP_PROFILE_UNSUITABLE",
        "production_lock_blocked_reasons": [
            "MFFU evaluation purchase price REQUIRES_CONFIRMATION",
            "FundedNext max_funded_accounts REQUIRES_CONFIRMATION",
            "copy_trading REQUIRES_CONFIRMATION",
            "Phase 49/50 research cells not production-locked",
        ],
        "frozen_before": frozen_before,
        "frozen_after": frozen_after,
        "frozen": {"gc": frozen_after.get("gc"), "nq": frozen_after.get("nq"), "paper_journals_empty": frozen_ok},
        "tests": {"returncode": proc.returncode, "ran": proc.stderr.count(" ... ok") + proc.stdout.count(" ... ok"), "tail": (proc.stderr or proc.stdout)[-2000:]},
        "phase49_50_reports_not_overwritten": p49_untouched,
    }
    DOCS.parent.mkdir(parents=True, exist_ok=True)
    DOCS.write_text(render_docs(payload), encoding="utf-8")
    VALIDATION.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    update_registry()
    return payload


def EVAL_MATRIX_OK() -> bool:
    p = ROOT / "reports" / "phase49_eval_simulation" / "eval_matrix.json"
    st = ROOT / "reports" / "phase50_dynamic_risk" / "best_stitched.json"
    if not p.exists() or not st.exists():
        return False
    nq = load_eval_matrix_row("NQ", MFFU)
    return abs(float(nq["P(pass)"]) - 0.7235) < 1e-9


def main() -> int:
    nf, ng, nfast, ne, nfu = N_PATHS_FINAL, N_PATHS_GRID, N_PATHS_FAST, N_POOL_EVAL, N_POOL_FUNDED
    if "--quick" in sys.argv:
        nf, ng, nfast, ne, nfu = 250, 120, 80, 400, 400
    payload = run(n_final=nf, n_grid=ng, n_fast=nfast, n_eval=ne, n_funded=nfu)
    print(json.dumps({"verdict": payload["verdict"], "classifications": payload["classifications"]}, indent=2, default=str))
    print(payload["verdict"])
    return 0 if payload["verdict"] == "PHASE51_PROP_FLYWHEEL_RESEARCH_READY" else 1


if __name__ == "__main__":
    raise SystemExit(main())
