"""Phase 49B — fast-pass evaluation optimization. DRY_RUN. Does not alter Phase 49/50/51 reports."""
from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any, Optional

import numpy as np

from aitrade_operating_policy import load_operating_policy
from phase34_validate import assert_frozen
from phase49_trade_audit import ES_SRC, NQ_SRC, load_phase46_csv, write_csv
from phase49_prop_sim import DEFAULT_STOP, trades_to_days
from phase49b_engine import (
    FastPassSpec,
    chain_times,
    classify_fast_pass,
    classify_tier,
    eval_objective,
    extra_cost_per_micro,
    max_executable_qty,
    pack_days,
    pool_from_batch,
    simulate_batch,
    summarize,
    unit_risk_usd,
)
from phase51_flywheel import FlywheelSpec, simulate_paths
from phase51_pools import FN, MFFU, _stable, build_eval_pool, build_funded_pool, fn_eval_prices, load_eval_matrix_row, mffu_eval_price_status

ROOT = Path(__file__).resolve().parent
VALIDATION = ROOT / "phase49b_validation.json"
DOCS = ROOT / "docs" / "PHASE49B_FAST_PASS_EVALUATION.md"
REGISTRY = ROOT / "docs" / "STRATEGY_REGISTRY.md"
POLICY_PATH = ROOT / "config" / "aitrade_operating_policy_v1.json"

FAST = ROOT / "reports" / "phase49b_fast_pass"
PARETO = ROOT / "reports" / "phase49b_pareto_frontier"
GOV = ROOT / "reports" / "phase49b_consistency_governor"
STATE = ROOT / "reports" / "phase49b_state_policy"
STRESS = ROOT / "reports" / "phase49b_stress"
FW = ROOT / "reports" / "phase49b_flywheel_impact"

N_SCREEN = 2_500
N_FINAL = 10_000
N_TOP = 25_000
N_CHRONO = 1_500
N_ES = 2_000
N_FW = 5_000
N_POOL_FUNDED = 2_500
N_POOL_EVAL_BASE = 4_000
TIERS = (10, 14, 20, 30)


def _write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, default=str), encoding="utf-8")


def _csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    write_csv(path, rows)


def _rng(*parts) -> np.random.Generator:
    acc = 4949
    s = "|".join(str(p) for p in parts)
    for i, c in enumerate(s):
        acc = (acc + (i + 1) * ord(c) * 131) % 2_147_483_647
    return np.random.default_rng(int(acc))


def qty_grid() -> list[FastPassSpec]:
    return [
        FastPassSpec(name="Q1_FIXED", qty_normal=1, qty_accel=1, qty_defensive=1, qty_approach=1, label="qty"),
        FastPassSpec(name="Q2_FIXED", qty_normal=2, qty_accel=2, qty_defensive=2, qty_approach=1, label="qty"),
        FastPassSpec(name="Q3_FIXED", qty_normal=3, qty_accel=3, qty_defensive=3, qty_approach=1, label="qty"),
        FastPassSpec(name="Q4_FIXED", qty_normal=4, qty_accel=4, qty_defensive=2, qty_approach=1, label="qty_stretch"),
        FastPassSpec(name="Q1_ACCEL2_C70", qty_normal=1, qty_accel=2, qty_defensive=1, accel_dd_frac=0.70, accel_min_pnl=0.0, label="qty"),
        FastPassSpec(name="Q1_ACCEL2_C80_P200", qty_normal=1, qty_accel=2, qty_defensive=1, accel_dd_frac=0.80, accel_min_pnl=200.0, label="qty"),
        FastPassSpec(name="Q1_ACCEL3_C80_P400", qty_normal=1, qty_accel=3, qty_defensive=1, accel_dd_frac=0.80, accel_min_pnl=400.0, label="qty"),
        FastPassSpec(name="Q2_DEF50", qty_normal=2, qty_accel=2, qty_defensive=1, defensive_dd_frac=0.50, label="qty"),
        FastPassSpec(name="Q2_ACCEL3_DEF40", qty_normal=2, qty_accel=3, qty_defensive=1, accel_dd_frac=0.80, defensive_dd_frac=0.40, label="qty"),
        FastPassSpec(name="Q3_DEF45", qty_normal=3, qty_accel=3, qty_defensive=1, defensive_dd_frac=0.45, label="qty"),
    ]


def run_spec(pack, *, book, profile, spec, n_paths, mode="bootstrap") -> dict[str, Any]:
    batch = simulate_batch(
        pack, book=book, profile_id=profile, spec=spec, n_paths=n_paths, rng=_rng(book, profile, spec.name, mode, n_paths), mode=mode
    )
    row = summarize(batch, profile_id=profile, spec=spec, book=book)
    row["mode"] = mode
    return {"row": row, "batch": batch}


def executable_table(book: str, profile: str) -> dict[str, Any]:
    stop = float(DEFAULT_STOP[book])
    per = unit_risk_usd(book, stop)
    max_q, note = max_executable_qty(book, profile, stop)
    rows = []
    for q in range(1, max(max_q, 3) + 1):
        usd = q * per
        rows.append(
            {
                "book": book,
                "profile": profile,
                "qty_micros": q,
                "stop_points": stop,
                "raw_stop_risk_usd": usd,
                "fits_initial_dd": usd <= (2000.0 if profile.startswith("MFFU") else 1500.0) + 1e-9,
                "fits_contract_cap": q <= 30,
                "status": "OK" if q <= max_q else "BLOCK_INSUFFICIENT_RISK_CAPACITY",
            }
        )
    return {"max_executable": max_q, "unit_risk_usd": per, "stop_points": stop, "note": note, "rows": rows}


def pick_named(rows: list[dict[str, Any]], name: str) -> Optional[dict[str, Any]]:
    for r in rows:
        if r.get("name") == name:
            return r
    return None


def overlay_name(base: str, tag: str) -> str:
    return f"{base}__{tag}"


def update_registry(verdict: str) -> None:
    text = REGISTRY.read_text(encoding="utf-8")
    marker = "### Fast-pass evaluation optimization (Phase 49B)"
    block = f"""### Fast-pass evaluation optimization (Phase 49B)

| Field | Value |
|-------|--------|
| Phase | 49B |
| Status | See `phase49b_validation.json` verdict (`{verdict}`) |
| Question | Can evaluation pass time be reduced (target 10–14 trading days) while preserving pass probability and improving Phase 51 replication speed? |
| Forbidden | Retune GC/NQ/ES; freeze ES; enable broker; write production risk; overwrite Phase 49/50/51 reports; martingale |
| Evidence | `docs/PHASE49B_FAST_PASS_EVALUATION.md`, `reports/phase49b_*`, `phase49b_validation.json`, `tests_phase49b.py` |
| Frozen impact | None. Phase 26 / 30 hashes unchanged. DRY_RUN only. EVAL_ACCELERATE added to account-state scaffold as research state. |

"""
    if marker in text:
        start = text.index(marker)
        rest = text[start + len(marker) :]
        cuts = [i for i in (rest.find("\n### "), rest.find("\n## ")) if i >= 0]
        end_rel = min(cuts) if cuts else len(rest)
        text = text[:start] + block + rest[end_rel:].lstrip("\n")
    else:
        needle = "### Prop account replication flywheel (Phase 51)"
        idx = text.find(needle)
        if idx < 0:
            needle = "## RESEARCH-ONLY / RETIRED"
            idx = text.find(needle)
            insert_at = text.find("\n", idx) + 1
            text = text[:insert_at] + "\n" + block + text[insert_at:]
        else:
            text = text[:idx] + block + "\n" + text[idx:]
    REGISTRY.write_text(text, encoding="utf-8")


def render_docs(payload: dict[str, Any]) -> str:
    bl = payload.get("phase49_baseline") or {}
    qty = payload.get("executable") or {}
    frontier = payload.get("pareto") or []
    tiers = payload.get("tiers") or {}
    rec = payload.get("recommendations") or {}
    fw = payload.get("flywheel_impact") or {}
    classes = payload.get("classifications") or {}
    frozen = payload.get("frozen") or {}
    return f"""# Phase 49B — Fast-Pass Evaluation Optimization

`DRY_RUN`. No broker. Frozen strategy logic was not modified. Phase 49/50/51 report files were not overwritten. Operating-policy numerics were not written. ES is not promoted. GC remains `PROP_PROFILE_UNSUITABLE`.

## 1. Phase 49 baseline

{json.dumps(bl, indent=2, default=str)}

These cells are **research baselines**, not production settings.

## 2. Executable quantity analysis

Stop ≈ 80 NQ points. 1 MNQ ≈ $160 raw stop risk (already includes Phase 46 1-tick + 0.20pt commission in R). Firm caps: 3 minis / 30 micros. Quantity is floored from dollar risk — never rounded up.

{json.dumps(qty, indent=2, default=str)}

If no executable quantity fits: `BLOCK_INSUFFICIENT_RISK_CAPACITY`.

## 3. Fastest achievable pass distributions

See `reports/phase49b_fast_pass/`. Screening used {payload.get("n_screen")} bootstrap paths; finalists {payload.get("n_final")}. Same-day clustering is preserved (day bootstrap). Chronological comparison is in the same folder.

## 4. 10 / 14 / 20 / 30-day frontier

{json.dumps(tiers, indent=2, default=str)}

A tier is `FAST_PASS_UNSUPPORTED` if no policy hits that median with P(pass)≥0.45 and P(breach)≤0.55.

## 5. P(pass) / P(breach) tradeoff

Pareto table: `reports/phase49b_pareto_frontier/pareto.csv`. Highlighted roles:

- SAFEST: {rec.get("SAFEST")}
- BALANCED: {rec.get("BALANCED")}
- FASTEST_ACCEPTABLE: {rec.get("FASTEST_ACCEPTABLE")}

## 6. Target-approach results

`reports/phase49b_state_policy/target_approach.csv` — full size vs reduce-near-target vs skip-when-remaining-target < risk.

## 7. Consistency governor results

`reports/phase49b_consistency_governor/`. Consistency excess expands the profit target; it is not an automatic fail. Governors: NO_GOVERNOR / SOFT / REDUCED_SIZE / HARD_DAY_STOP.

## 8. Daily stop results

`reports/phase49b_state_policy/daily_stops.csv` — none / dollar / R / fraction-of-remaining-DD.

## 9. Losing-streak results

`reports/phase49b_state_policy/streaks.csv`. Quantity never increases after a loss. NQ and ES are scored separately.

## 10. Degradation tests

`reports/phase49b_stress/`. Expectancy −10/−20%, win-rate flip, slippage +25/+50%, commissions +25%, block-clustered losses, fewer opportunity days.

## 11. Eval cost implications

FundedNext purchase prices are **confirmed** ($69.99 / $79.99). MFFU purchase price is **`REQUIRES_CONFIRMATION`** — attempts are reported; dollar cost uses the labeled hypothetical $100 grid only when shown.

## 12. Phase 51 flywheel improvement

Did not rerun the full Phase 51 grid. Fed 49B pass/duration arrays into the existing flywheel with unchanged funded pools and the same $500 / NEXT_ACCOUNT_FIRST research spec.

{json.dumps(fw, indent=2, default=str)}

## 13. DAYS_PER_NEW_FUNDED_ACCOUNT

See flywheel impact rows (`days_per_new_funded_account`, `funded_accounts_created_per_$100_eval_spend`) plus chain times (purchase → pass → first payout → next eval → next funded) in `reports/phase49b_flywheel_impact/chain_times.json`.

## 14–16. Role candidates

- **SAFEST**: {rec.get("SAFEST")}
- **BALANCED**: {rec.get("BALANCED")}
- **FASTEST_ACCEPTABLE**: {rec.get("FASTEST_ACCEPTABLE")}

Classifications: {json.dumps(classes, indent=2, default=str)}

## 17. Recommendation only — not a production lock

Do not write these quantities or governors into `aitrade_operating_policy_v1.json`. Do not buy evaluations. 10–14 day pass remains a research target; if unsupported, that is reported.

## 18. Frozen hashes

{json.dumps(frozen.get("hashes"), indent=2, default=str)}

## 19. Paper journals

Empty: {frozen.get("journals_empty")}

## 20. DRY_RUN

- execution_default: `{payload.get("execution_default")}`
- broker_execution: `{payload.get("broker_execution")}`
- risk_per_trade still null: `{payload.get("policy_risk_still_null")}`

## What this phase did not do

No strategy retune. No Phase 49/50/51 source-result overwrite. No ES freeze. No live execution. No production risk lock. No invented MFFU purchase price.
"""


def choose_roles(rows: list[dict[str, Any]], baseline: dict[str, Any]) -> dict[str, Any]:
    sized = [r for r in rows if (r.get("qty_normal_used") or 0) > 0 and r.get("median_days_to_pass") is not None]
    if not sized:
        return {"SAFEST": None, "BALANCED": None, "FASTEST_ACCEPTABLE": None}
    safe = max(sized, key=lambda r: (float(r.get("P(pass)") or 0) - 0.35 * float(r.get("P(breach)") or 0), -float(r.get("p90_days_to_pass") or 999)))
    fast_ok = [
        r
        for r in sized
        if float(r.get("P(pass)") or 0) >= max(0.45, float(baseline.get("P(pass)") or 0) - 0.12)
        and float(r.get("P(breach)") or 1) <= 0.55
    ]
    fastest = min(fast_ok, key=lambda r: float(r.get("median_days_to_pass") or 999)) if fast_ok else min(sized, key=lambda r: float(r.get("median_days_to_pass") or 999))
    balanced = max(sized, key=lambda r: eval_objective(r, baseline_median=baseline.get("median_days_to_pass")))
    return {
        "SAFEST": safe.get("name"),
        "BALANCED": balanced.get("name"),
        "FASTEST_ACCEPTABLE": fastest.get("name"),
        "SAFEST_row": safe,
        "BALANCED_row": balanced,
        "FASTEST_ACCEPTABLE_row": fastest,
    }


def flywheel_compare(eval_base, eval_fast, funded, *, book, profile, n_paths) -> dict[str, Any]:
    fn_px = fn_eval_prices()
    mffu_px = mffu_eval_price_status()
    pools_base = {
        "eval": {f"{book}->{profile}": eval_base, f"{book}->{MFFU}": eval_base, f"{book}->{FN}": eval_base},
        "funded": {f"{book}->{profile}": funded, f"{book}->{MFFU}": funded, f"{book}->{FN}": funded},
        "fn_price": fn_px,
        "mffu_price": mffu_px,
    }
    # keep both firm keys so expansion can resolve; override the active one
    pools_base["eval"][f"{book}->{profile}"] = eval_base
    pools_fast = {
        "eval": dict(pools_base["eval"]),
        "funded": pools_base["funded"],
        "fn_price": fn_px,
        "mffu_price": mffu_px,
    }
    pools_fast["eval"][f"{book}->{profile}"] = eval_fast
    expansion = "FUNDEDNEXT_ONLY" if profile == FN else "MFFU_ONLY"
    spec_b = FlywheelSpec(
        name=f"BASELINE_{book}_{expansion}",
        book=book,
        expansion=expansion,
        reinvest="REINVEST_NEXT_ACCOUNT_FIRST",
        start_cash=500.0,
        mffu_price=100.0,
        mffu_price_label="HYPOTHETICAL",
        fn_cap=3,
        fn_cap_label="HYPOTHETICAL",
        eval_mode="BASELINE_PHASE49",
        label="phase49_baseline_eval",
    )
    spec_f = replace(spec_b, name=f"FAST49B_{book}_{expansion}", label="phase49b_eval")
    rb = simulate_paths(spec_b, pools_base, n_paths=n_paths, rng=_rng("fw", book, profile, "base"))
    rf = simulate_paths(spec_f, pools_fast, n_paths=n_paths, rng=_rng("fw", book, profile, "fast"))
    keys = [
        "median_self_funding_day",
        "P(self_funding_by_60d)",
        "P(self_funding_by_90d)",
        "P(self_funding_by_180d)",
        "h90_expected_active_funded",
        "h180_expected_active_funded",
        "h365_expected_active_funded",
        "total_evaluation_spend",
        "external_capital_required",
        "total_trader_payout",
        "days_per_new_funded_account",
        "funded_accounts_created_per_$100_eval_spend",
        "classification",
    ]
    delta = {k: (rf.get(k), rb.get(k)) for k in keys}

    def _d(k):
        a, b = rf.get(k), rb.get(k)
        if a is None or b is None:
            return None
        try:
            return float(a) - float(b)
        except (TypeError, ValueError):
            return None

    improved_sf90 = (rf.get("P(self_funding_by_90d)") or 0) >= (rb.get("P(self_funding_by_90d)") or 0) - 1e-12
    improved_speed = (rf.get("h90_expected_active_funded") or 0) >= (rb.get("h90_expected_active_funded") or 0) - 1e-12
    return {
        "baseline": {k: rb.get(k) for k in keys},
        "fast_pass": {k: rf.get(k) for k in keys},
        "delta": {k: _d(k) for k in keys},
        "pairs": delta,
        "improved": bool(improved_sf90 or improved_speed),
        "baseline_full": rb,
        "fast_full": rf,
    }


def main() -> int:
    quick = "--quick" in sys.argv
    n_screen = 400 if quick else N_SCREEN
    n_final = 800 if quick else N_FINAL
    n_top = 800 if quick else N_TOP
    n_chrono = 200 if quick else N_CHRONO
    n_es = 400 if quick else N_ES
    n_fw = 400 if quick else N_FW
    n_funded = 400 if quick else N_POOL_FUNDED
    n_eval_base = 400 if quick else N_POOL_EVAL_BASE

    frozen = assert_frozen()
    pol = load_operating_policy()
    journals = []
    for rel in (
        "journal/phase26_gc_vwap_v2_paper/paper_trades.jsonl",
        "journal/phase30_nq_dvp_paper/paper_trades.jsonl",
        "journal/phase47_es_dvp_paper/paper_trades.jsonl",
    ):
        p = ROOT / rel
        journals.append({"path": rel, "empty": p.exists() and p.stat().st_size == 0})

    nq_trades = load_phase46_csv(NQ_SRC, strategy="NQ_DVP_FROZEN", instrument="NQ", cost_note="phase46")
    es_trades = load_phase46_csv(ES_SRC, strategy="ES_DVP_LOCKED", instrument="ES", cost_note="phase46")
    nq_days = trades_to_days(nq_trades, "NQ")
    es_days = trades_to_days(es_trades, "ES")
    packs = {"NQ": pack_days(nq_days, "NQ"), "ES": pack_days(es_days, "ES")}

    baseline = {}
    for book, profile in (("NQ", MFFU), ("NQ", FN), ("ES", MFFU), ("ES", FN)):
        baseline[f"{book}->{profile}"] = load_eval_matrix_row(book, profile)

    exec_info = {}
    exec_rows = []
    for book, profile in (("NQ", MFFU), ("NQ", FN), ("ES", MFFU), ("ES", FN)):
        info = executable_table(book, profile)
        exec_info[f"{book}->{profile}"] = info
        exec_rows.extend(info["rows"])
    _csv(FAST / "executable_quantity.csv", exec_rows)
    _write_json(FAST / "executable_quantity.json", exec_info)

    all_rows: list[dict[str, Any]] = []
    batches: dict[str, dict[str, Any]] = {}

    def add_run(book, profile, spec, n, mode="bootstrap"):
        out = run_spec(packs[book], book=book, profile=profile, spec=spec, n_paths=n, mode=mode)
        row = out["row"]
        key = f"{book}->{profile}->{spec.name}->{mode}"
        batches[key] = out["batch"]
        all_rows.append(row)
        return row

    # --- quantity + approach screen (NQ primary) ---
    approach_on = ["Q1_FIXED", "Q2_FIXED", "Q3_FIXED", "Q2_DEF50", "Q1_ACCEL2_C70", "Q2_ACCEL3_DEF40"]
    for profile in (FN, MFFU):
        for spec in qty_grid():
            add_run("NQ", profile, spec, n_screen)
        base_map = {s.name: s for s in qty_grid()}
        for bname in approach_on:
            b = base_map[bname]
            add_run("NQ", profile, replace(b, name=overlay_name(bname, "APPR_REDUCE"), approach="REDUCE", approach_mult=1.5, qty_approach=1), n_screen)
            add_run("NQ", profile, replace(b, name=overlay_name(bname, "APPR_SKIP"), approach="SKIP", approach_mult=1.0, qty_approach=1), n_screen)

    nq_fn_rows = [r for r in all_rows if r["book"] == "NQ" and r["profile"] == FN and r["mode"] == "bootstrap"]
    nq_mffu_rows = [r for r in all_rows if r["book"] == "NQ" and r["profile"] == MFFU and r["mode"] == "bootstrap"]

    def top_k(rows, k=4):
        return sorted(rows, key=lambda r: eval_objective(r), reverse=True)[:k]

    toppers = {FN: top_k(nq_fn_rows), MFFU: top_k(nq_mffu_rows)}

    def spec_from_row(row: dict[str, Any]) -> FastPassSpec:
        return FastPassSpec(
            name=row["name"],
            qty_normal=int(row["qty_normal"]),
            qty_accel=int(row["qty_accel"]),
            qty_defensive=int(row["qty_defensive"]),
            qty_approach=int(row["qty_approach"]),
            accel_dd_frac=float(row["accel_dd_frac"]),
            accel_min_pnl=float(row["accel_min_pnl"]),
            defensive_dd_frac=float(row["defensive_dd_frac"]),
            approach=str(row["approach"]),
            approach_mult=float(row.get("approach_mult") or 1.5),
            governor=str(row["governor"]),
            daily_stop=str(row["daily_stop"]),
            daily_stop_value=float(row["daily_stop_value"]),
            streak=str(row["streak"]),
            day_profit_frac=float(row["day_profit_frac"]),
        )

    # governors / daily / streak overlays on top qty names (strip approach suffix for base)
    gov_rows = []
    stop_rows = []
    streak_rows = []
    for profile, tops in toppers.items():
        seeds = []
        for r in tops:
            nm = r["name"].split("__")[0]
            seeds.append(next(s for s in qty_grid() if s.name == nm))
        seen = []
        for s in seeds:
            if s.name not in seen:
                seen.append(s.name)
                base = s
                for gov in ("soft", "reduced", "hard"):
                    sp = replace(base, name=overlay_name(base.name, f"GOV_{gov.upper()}"), governor=gov)
                    gov_rows.append(add_run("NQ", profile, sp, n_screen))
                for tag, dstop, val in (
                    ("DSTOP_USD400", "dollar", 400.0),
                    ("DSTOP_USD600", "dollar", 600.0),
                    ("DSTOP_R15", "R", 1.5),
                    ("DSTOP_FRAC35", "frac_dd", 0.35),
                ):
                    sp = replace(base, name=overlay_name(base.name, tag), daily_stop=dstop, daily_stop_value=val)
                    stop_rows.append(add_run("NQ", profile, sp, n_screen))
                for st in ("pause_2", "pause_3", "reduce_2", "reduce_3"):
                    sp = replace(base, name=overlay_name(base.name, f"STREAK_{st.upper()}"), streak=st)
                    streak_rows.append(add_run("NQ", profile, sp, n_screen))
                sp = replace(base, name=overlay_name(base.name, "DAYSHAPE70"), day_profit_frac=0.70)
                add_run("NQ", profile, sp, n_screen)

    # combine best pieces per firm
    combined = []
    for profile in (FN, MFFU):
        pool = [r for r in all_rows if r["book"] == "NQ" and r["profile"] == profile]
        best_qty = max((r for r in pool if r["label"] == "qty"), key=lambda r: eval_objective(r), default=None)
        if best_qty is None:
            continue
        bspec = spec_from_row(best_qty)
        best_gov = max((r for r in pool if r["governor"] != "none"), key=lambda r: eval_objective(r), default=None)
        best_stop = max((r for r in pool if r["daily_stop"] != "none"), key=lambda r: eval_objective(r), default=None)
        best_streak = max((r for r in pool if r["streak"] != "none"), key=lambda r: eval_objective(r), default=None)
        sp = bspec
        tags = [bspec.name]
        if best_gov and eval_objective(best_gov) >= eval_objective(best_qty) - 0.01:
            sp = replace(sp, governor=best_gov["governor"])
            tags.append(best_gov["governor"])
        if best_stop and eval_objective(best_stop) >= eval_objective(best_qty) - 0.01:
            sp = replace(sp, daily_stop=best_stop["daily_stop"], daily_stop_value=best_stop["daily_stop_value"])
            tags.append(best_stop["name"].split("__")[-1])
        if best_streak and eval_objective(best_streak) >= eval_objective(best_qty) - 0.02:
            sp = replace(sp, streak=best_streak["streak"])
            tags.append(best_streak["streak"])
        sp = replace(sp, name="COMBINED_" + "_".join(tags)[:80], label="combined")
        combined.append(add_run("NQ", profile, sp, n_screen))

    # ES secondary — compact grid
    es_specs = [
        FastPassSpec(name="Q1_FIXED", qty_normal=1, qty_accel=1, qty_defensive=1, label="es"),
        FastPassSpec(name="Q2_FIXED", qty_normal=2, qty_accel=2, qty_defensive=1, defensive_dd_frac=0.50, label="es"),
        FastPassSpec(name="Q2_DEF50_REDUCE2", qty_normal=2, qty_accel=2, qty_defensive=1, defensive_dd_frac=0.50, streak="reduce_2", label="es"),
        FastPassSpec(name="Q1_ACCEL2_C70", qty_normal=1, qty_accel=2, accel_dd_frac=0.70, label="es"),
    ]
    for profile in (FN, MFFU):
        for spec in es_specs:
            add_run("ES", profile, spec, n_es)

    # finals for NQ role candidates
    final_rows = []
    roles = {}
    for profile, pool in ((FN, nq_fn_rows), (MFFU, nq_mffu_rows)):
        pool_now = [r for r in all_rows if r["book"] == "NQ" and r["profile"] == profile]
        bl = baseline[f"NQ->{profile}"]
        roles[profile] = choose_roles(pool_now, bl)
        names = {roles[profile][k] for k in ("SAFEST", "BALANCED", "FASTEST_ACCEPTABLE") if roles[profile].get(k)}
        for nm in names:
            src = pick_named(pool_now, nm)
            if src is None:
                continue
            spec = spec_from_row(src)
            spec = replace(spec, name=src["name"], label="final")
            final_rows.append(add_run("NQ", profile, spec, n_final))

    # 25k on top BALANCED per primary firm
    top_final = []
    for profile in (FN, MFFU):
        nm = (roles.get(profile) or {}).get("BALANCED")
        src = pick_named(all_rows, nm) if nm else None
        if src is None:
            continue
        spec = spec_from_row(src)
        spec = replace(spec, name=src["name"] + "_25k", label="top25k")
        top_final.append(add_run("NQ", profile, spec, n_top))

    # chrono vs bootstrap for BALANCED FN
    chrono_cmp = []
    for profile in (FN, MFFU):
        nm = (roles.get(profile) or {}).get("BALANCED")
        src = pick_named(all_rows, nm) if nm else None
        if src is None:
            continue
        spec = spec_from_row(src)
        spec = replace(spec, name=src["name"] + "_CHRONO", label="chrono")
        chrono_cmp.append(add_run("NQ", profile, spec, n_chrono, mode="chrono"))

    # stress on FASTEST_ACCEPTABLE and BALANCED for NQ FN
    stress_rows = []
    stress_targets = []
    for role in ("BALANCED", "FASTEST_ACCEPTABLE"):
        nm = (roles.get(FN) or {}).get(role)
        src = pick_named(all_rows, nm) if nm else None
        if src:
            stress_targets.append((role, spec_from_row(src), src))
    stress_defs = [
        ("EXPECTANCY_M10", dict(expectancy_scale=0.90)),
        ("EXPECTANCY_M20", dict(expectancy_scale=0.80)),
        ("WR_FLIP_10", dict(wr_flip=0.10)),
        ("SLIP_P25", dict(slippage_mult=1.25)),
        ("SLIP_P50", dict(slippage_mult=1.50)),
        ("COMM_P25", dict(commission_mult=1.25)),
        ("CLUSTER3", dict(block_cluster=3)),
        ("FEWER_DAYS_20", dict(skip_day_p=0.20)),
    ]
    for role, spec, src in stress_targets:
        for tag, kw in stress_defs:
            sp = replace(spec, name=f"{src['name']}__STRESS_{tag}", label=f"stress_{role}", **kw)
            stress_rows.append(add_run("NQ", FN, sp, n_screen))

    # funded pools + baseline eval pools for flywheel
    day_map = {"NQ": nq_days, "ES": es_days}
    funded_pools = {}
    base_eval_pools = {}
    for profile in (FN, MFFU):
        funded_pools[profile] = build_funded_pool(day_map["NQ"], "NQ", profile, n_funded, np.random.default_rng(_stable(50, "NQ", profile)))
        base_eval_pools[profile] = build_eval_pool(day_map["NQ"], "NQ", profile, n_eval_base, np.random.default_rng(_stable(49, "NQ", profile)))

    fw_out = {}
    chain = {}
    for profile in (FN, MFFU):
        nm = (roles.get(profile) or {}).get("BALANCED")
        src = pick_named(all_rows, nm) if nm else None
        if src is None:
            continue
        key = f"NQ->{profile}->{src['name']}->bootstrap"
        # prefer 10k final batch
        final_key = None
        for k, b in batches.items():
            if k.startswith(f"NQ->{profile}->{src['name']}") and k.endswith("bootstrap") and "CHRONO" not in k and "_25k" not in k:
                final_key = k
        use_key = final_key or key
        # find largest n batch for this name
        cands = [(k, b) for k, b in batches.items() if f"NQ->{profile}->{src['name']}" in k and k.endswith("bootstrap") and "_CHRONO" not in k]
        if cands:
            use_key, batch = max(cands, key=lambda kv: len(kv[1]["terminal"]))
        else:
            batch = batches.get(use_key)
        if batch is None:
            continue
        fast_pool = pool_from_batch(batch, book="NQ", profile=profile)
        fw_out[profile] = flywheel_compare(
            base_eval_pools[profile], fast_pool, funded_pools[profile], book="NQ", profile=profile, n_paths=n_fw
        )
        fund_sum = {
            "expected_days_to_first_payout": None,
            "phase50_median_first_payout_day": None,
        }
        po = funded_pools[profile]
        first_days = []
        for i in range(int(po["n"])):
            if int(po["n_po"][i]) > 0:
                first_days.append(int(po["po_day"][i, 0]))
        if first_days:
            fund_sum["expected_days_to_first_payout"] = float(np.median(first_days))
        chain[profile] = {
            "baseline": chain_times(
                {
                    "P(pass)": baseline[f"NQ->{profile}"]["P(pass)"],
                    "median_days_to_pass": baseline[f"NQ->{profile}"].get("median_days_to_pass"),
                    "expected_number_of_attempts": (1.0 / float(baseline[f"NQ->{profile}"]["P(pass)"])) if baseline[f"NQ->{profile}"].get("P(pass)") else None,
                },
                fund_sum,
            ),
            "fast_pass": chain_times(src, fund_sum),
        }

    # tiers
    tiers = {}
    for profile, pool in ((FN, [r for r in all_rows if r["book"] == "NQ" and r["profile"] == FN]), (MFFU, [r for r in all_rows if r["book"] == "NQ" and r["profile"] == MFFU])):
        tiers[profile] = {str(td): classify_tier(pool, median_cap=td) for td in TIERS}

    # classifications for role rows
    classifications = {"GC": "PROP_PROFILE_UNSUITABLE"}
    for profile in (FN, MFFU):
        bl = baseline[f"NQ->{profile}"]
        fw_impr = (fw_out.get(profile) or {}).get("improved")
        for role in ("SAFEST", "BALANCED", "FASTEST_ACCEPTABLE"):
            nm = (roles.get(profile) or {}).get(role)
            src = pick_named(all_rows, nm) if nm else None
            if src is None:
                continue
            deg = None
            for r in stress_rows:
                if r.get("label") == f"stress_{role}" and "EXPECTANCY_M10" in r["name"] and r["profile"] == FN:
                    deg = r
                    break
            classifications[f"NQ->{profile}->{role}"] = classify_fast_pass(src, bl, degraded=deg, flywheel_improved=fw_impr)

    es_class = {}
    for profile in (FN, MFFU):
        pool = [r for r in all_rows if r["book"] == "ES" and r["profile"] == profile]
        if not pool:
            continue
        best = max(pool, key=lambda r: eval_objective(r))
        es_class[f"ES->{profile}"] = classify_fast_pass(best, baseline[f"ES->{profile}"])
        es_class[f"ES->{profile}_best"] = best["name"]
    classifications.update(es_class)

    # 10-14 day target statement
    fn_tiers = tiers.get(FN) or {}
    mffu_tiers = tiers.get(MFFU) or {}
    target_10_14 = "FAST_PASS_UNSUPPORTED"
    if (fn_tiers.get("14") or {}).get("status") == "HIT" or (mffu_tiers.get("14") or {}).get("status") == "HIT":
        target_10_14 = "HIT"
    if (fn_tiers.get("10") or {}).get("status") == "HIT" or (mffu_tiers.get("10") or {}).get("status") == "HIT":
        target_10_14 = "HIT_10D"

    rec_out = {
        "NQ_FUNDEDNEXT": {k: (roles.get(FN) or {}).get(k) for k in ("SAFEST", "BALANCED", "FASTEST_ACCEPTABLE")},
        "NQ_MFFU": {k: (roles.get(MFFU) or {}).get(k) for k in ("SAFEST", "BALANCED", "FASTEST_ACCEPTABLE")},
        "target_10_14": target_10_14,
        "SAFEST": (roles.get(FN) or {}).get("SAFEST"),
        "BALANCED": (roles.get(FN) or {}).get("BALANCED"),
        "FASTEST_ACCEPTABLE": (roles.get(FN) or {}).get("FASTEST_ACCEPTABLE"),
    }

    pareto = []
    for r in all_rows:
        if r["book"] != "NQ" or r["mode"] != "bootstrap":
            continue
        if r.get("label") in ("stress_BALANCED", "stress_FASTEST_ACCEPTABLE"):
            continue
        pareto.append(
            {
                "Policy": r["name"],
                "Quantity logic": f"n={r['qty_normal']} a={r['qty_accel']} d={r['qty_defensive']}",
                "Firm": r["profile"],
                "P(pass)": r["P(pass)"],
                "P(breach)": r["P(breach)"],
                "Median days": r["median_days_to_pass"],
                "P(pass <=10d)": r["P(pass <=10d)"],
                "P(pass <=14d)": r["P(pass <=14d)"],
                "P(pass <=20d)": r["P(pass <=20d)"],
                "Expected attempts": r["expected_number_of_attempts"],
                "Expected eval cost": r["expected_evaluation_cost"],
                "p90 days": r["p90_days_to_pass"],
                "governor": r["governor"],
                "daily_stop": r["daily_stop"],
                "streak": r["streak"],
                "approach": r["approach"],
            }
        )

    # write artifacts
    _csv(FAST / "all_policies.csv", all_rows)
    _write_json(FAST / "all_policies.json", [{k: v for k, v in r.items() if k != "batch"} for r in all_rows])
    _csv(FAST / "finalists.csv", [r for r in all_rows if r.get("label") in ("final", "top25k")])
    _csv(FAST / "chrono_vs_bootstrap.csv", [r for r in all_rows if r.get("label") == "chrono" or (r.get("label") == "final" and r["mode"] == "bootstrap")])
    _csv(PARETO / "pareto.csv", pareto)
    _write_json(PARETO / "pareto.json", pareto)
    _csv(GOV / "governors.csv", [r for r in all_rows if r["governor"] != "none" and "STRESS" not in r["name"]])
    _csv(STATE / "target_approach.csv", [r for r in all_rows if r["approach"] != "NONE"])
    _csv(STATE / "daily_stops.csv", [r for r in all_rows if r["daily_stop"] != "none" and "STRESS" not in r["name"]])
    _csv(STATE / "streaks.csv", [r for r in all_rows if r["streak"] != "none" and "STRESS" not in r["name"]])
    _csv(STRESS / "stress.csv", stress_rows)
    _write_json(STRESS / "stress.json", stress_rows)
    fw_slim = {}
    for profile, blob in fw_out.items():
        fw_slim[profile] = {k: blob[k] for k in ("baseline", "fast_pass", "delta", "improved") if k in blob}
        _csv(FW / f"NQ_{profile}_baseline.csv", [blob["baseline_full"]])
        _csv(FW / f"NQ_{profile}_fast.csv", [blob["fast_full"]])
    _write_json(FW / "flywheel_impact.json", fw_slim)
    _write_json(FW / "chain_times.json", chain)
    _write_json(FAST / "phase49_baseline.json", baseline)
    _write_json(FAST / "tiers.json", tiers)
    _write_json(PARETO / "roles.json", {str(k): {rk: rv for rk, rv in (v or {}).items() if rk.endswith("_row") is False} for k, v in roles.items()})

    tests = subprocess.run([sys.executable, str(ROOT / "tests_phase49b.py")], capture_output=True, text=True)
    test_ok = tests.returncode == 0

    policy_text = POLICY_PATH.read_text(encoding="utf-8")
    policy_untouched = '"risk_per_trade": null' in policy_text
    phase49_pass = float(baseline["NQ->MFFU_RAPID_EOD_50K"]["P(pass)"])
    phase49_intact = abs(phase49_pass - 0.7235) < 1e-4

    any_viable = any(v == "FAST_PASS_VIABLE" for k, v in classifications.items() if str(k).startswith("NQ->"))
    any_border = any(v == "FAST_PASS_BORDERLINE" for k, v in classifications.items() if str(k).startswith("NQ->"))
    ready = test_ok and frozen.get("ok") and policy_untouched and phase49_intact and all(j["empty"] for j in journals)
    # research can be READY even if 10-14d unsupported
    verdict = "PHASE49B_FAST_PASS_RESEARCH_READY" if ready else "PHASE49B_FAST_PASS_RESEARCH_BLOCKED"

    payload = {
        "phase": "49B",
        "verdict": verdict,
        "execution_default": pol.execution_default,
        "broker_execution": pol.broker_execution,
        "dry_run": pol.execution_default == "DRY_RUN",
        "policy_risk_still_null": pol.numerics.get("risk_per_trade") is None,
        "es_not_frozen": True,
        "n_screen": n_screen,
        "n_final": n_final,
        "phase49_baseline": {
            k: {
                "dd_frac": v.get("dd_frac"),
                "P(pass)": v.get("P(pass)"),
                "median_days_to_pass": v.get("median_days_to_pass"),
                "p75_days_to_pass": v.get("p75_days_to_pass"),
                "policy": v.get("policy"),
            }
            for k, v in baseline.items()
        },
        "executable": {k: {"max_executable": v["max_executable"], "unit_risk_usd": v["unit_risk_usd"], "note": v["note"]} for k, v in exec_info.items()},
        "mffu_price": mffu_eval_price_status(),
        "fn_price": fn_eval_prices(),
        "tiers": {str(p): {td: {"status": blob["status"], "best": blob.get("best"), "median": (blob.get("row") or {}).get("median_days_to_pass"), "P(pass)": (blob.get("row") or {}).get("P(pass)")} for td, blob in tdmap.items()} for p, tdmap in tiers.items()},
        "recommendations": rec_out,
        "classifications": classifications,
        "flywheel_impact": fw_slim,
        "chain_times": chain,
        "target_10_14": target_10_14,
        "any_nq_viable": any_viable,
        "any_nq_borderline": any_border,
        "gc": "PROP_PROFILE_UNSUITABLE",
        "frozen": {
            "ok": frozen.get("ok"),
            "hashes": frozen,
            "journals_empty": all(j["empty"] for j in journals),
            "journals": journals,
        },
        "phase49_eval_matrix_intact": phase49_intact,
        "policy_file_untouched": policy_untouched,
        "tests": {"ok": test_ok, "stdout": (tests.stdout or "")[-2000:], "stderr": (tests.stderr or "")[-1000:]},
        "extra_cost_note": "Primary runs use R already net of Phase 46 1-tick + commission. Stress adds extra_cost_per_micro via slippage/commission multipliers.",
        "EVAL_ACCELERATE": "account_state_engine research state; qty accel only with DD cushion, pnl floor, and zero consecutive losses",
    }
    _write_json(VALIDATION, payload)
    DOCS.write_text(render_docs(payload), encoding="utf-8")
    update_registry(verdict)
    print(json.dumps({"verdict": verdict, "classifications": classifications, "roles": rec_out, "target_10_14": target_10_14}, indent=2, default=str))
    print(verdict)
    return 0 if verdict.endswith("READY") else 1


if __name__ == "__main__":
    raise SystemExit(main())
