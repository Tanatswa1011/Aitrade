"""Phase 50 — funded survival, reserve, payout-policy research. DRY_RUN only."""
from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

import numpy as np

from aitrade_operating_policy import load_operating_policy
from phase34_validate import assert_frozen
from phase49_prop_sim import trades_to_days
from phase49_trade_audit import ES_SRC, NQ_SRC, load_or_reconstruct_gc, load_phase46_csv, write_csv
from phase50_funded_engine import (
    HORIZONS,
    N_PATHS_CURVE,
    N_PATHS_FINAL,
    N_PATHS_SEARCH,
    PAYOUT_MODES,
    RESERVE_FRACS,
    RESERVE_USD,
    SEED,
    FundedPolicy,
    classify,
    composite_score,
    min_executable,
    phase49_baseline_policy,
    score_components,
    simulate_funded,
)
from phase50_root_cause import analyze_book, phase49_failure_narrative

ROOT = Path(__file__).resolve().parent
VALIDATION = ROOT / "phase50_validation.json"
DOCS = ROOT / "docs" / "PHASE50_FUNDED_SURVIVAL_PAYOUT_POLICY.md"
REGISTRY = ROOT / "docs" / "STRATEGY_REGISTRY.md"
POLICY_PATH = ROOT / "config" / "aitrade_operating_policy_v1.json"
CAUSE = ROOT / "reports" / "phase50_funded_root_cause"
CURVES = ROOT / "reports" / "phase50_survival_curves"
PAYOUT_DIR = ROOT / "reports" / "phase50_payout_policy"
RESERVE_DIR = ROOT / "reports" / "phase50_reserve_policy"
DYN_DIR = ROOT / "reports" / "phase50_dynamic_risk"

BOOKS = ("GC", "NQ", "ES")
PROFILES = ("MFFU_RAPID_EOD_50K", "FUNDEDNEXT_FLEX_50K")

EVAL_PRESERVED = {
    "NQ->MFFU_RAPID_EOD_50K": "Phase 49 eval research cell ~10% initial-DD (1 MNQ / $200). Not production-locked.",
    "NQ->FUNDEDNEXT_FLEX_50K": "Phase 49 eval executable 1-MNQ cells (≥12.5% of $1,500 DD). Not production-locked.",
    "ES->MFFU_RAPID_EOD_50K": "Phase 49 eval finding: reduce after 2 losses improved P(pass) at 10%. Not production-locked.",
}


def _load_days() -> dict[str, list]:
    nq = load_phase46_csv(NQ_SRC, strategy="NQ_DVP_FROZEN", instrument="NQ", cost_note="phase46")
    es = load_phase46_csv(ES_SRC, strategy="ES_DVP_LOCKED", instrument="ES", cost_note="phase46")
    gc, _meta = load_or_reconstruct_gc()
    return {
        "GC": trades_to_days(gc, "GC"),
        "NQ": trades_to_days(nq, "NQ"),
        "ES": trades_to_days(es, "ES"),
    }


def _row(book: str, profile: str, pol: FundedPolicy, out: dict[str, Any]) -> dict[str, Any]:
    s = out["summary"]
    c = score_components(s)
    flat = {
        "book": book,
        "profile": profile,
        "policy": pol.name,
        "payout_mode": pol.payout_mode,
        "reserve_usd": out.get("reserve_usd"),
        "use_dynamic_risk": pol.use_dynamic_risk,
        "fixed_risk_usd": pol.fixed_risk_usd,
        "healthy_cushion_frac": pol.healthy_cushion_frac,
        "daily_stop": pol.daily_stop,
        "streak_mode": pol.streak_mode,
        "pre_lock_scale": pol.pre_lock_scale,
        "post_lock_scale": pol.post_lock_scale,
        "block_insufficient_capacity": pol.block_insufficient_capacity,
        "cap_risk_to_cushion": pol.cap_risk_to_cushion,
        "n_paths": out.get("n_paths"),
        "classification": classify(c),
        "composite_score": composite_score(c),
        **c,
        "mean_floor_blocks": s.get("mean_floor_blocks"),
        "mean_trades": s.get("mean_trades"),
        "breach_causes": json.dumps(s.get("breach_causes") or {}),
        "pre_breach": json.dumps(s.get("pre_breach") or {}),
        "breach_timing": json.dumps(s.get("breach_timing") or {}),
        "state_occupancy": json.dumps(s.get("state_occupancy") or {}),
    }
    for k, v in (s.get("pre_breach") or {}).items():
        flat[f"pre_{k}"] = v
    for k, v in (s.get("breach_timing") or {}).items():
        flat[k] = v
    for k, v in (s.get("state_occupancy") or {}).items():
        flat[f"state_{k}"] = v
    for h, blob in (s.get("curves") or {}).items():
        for k, v in blob.items():
            if k == "horizon":
                continue
            flat[f"h{h}_{k}"] = v
    return flat


def _seed(s: str) -> int:
    acc = SEED
    for i, c in enumerate(s):
        acc = (acc + (i + 1) * ord(c) * 131) % 2_147_483_647
    return int(acc)


def _run(days, book, profile, pol, n, seed_key) -> dict[str, Any]:
    rng = np.random.default_rng(_seed(str(seed_key)))
    return simulate_funded(days, book=book, profile_id=profile, policy=pol, n_paths=n, rng=rng)


def one_micro_policy(book: str, days, **kwargs) -> FundedPolicy:
    fl = min_executable(book, days)
    risk = float(fl["median_executable_usd"])
    kw = dict(
        name=kwargs.pop("name", "ONE_MICRO"),
        payout_mode=kwargs.pop("payout_mode", "PAYOUT_AS_SOON_AS_ELIGIBLE"),
        reserve_usd=kwargs.pop("reserve_usd", 1000.0),
        use_dynamic_risk=False,
        fixed_risk_usd=risk,
        floor_block_ratio=1.0,
    )
    kw.update(kwargs)
    return FundedPolicy(**kw)


def best_of(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    tradable = [r for r in rows if float(r.get("never_traded") or 0) < 0.5]
    pool = tradable or rows
    if not pool:
        return None
    prefer = [
        r
        for r in pool
        if float(r.get("P(first payout)") or 0) >= 0.20 and float(r.get("P(survive 1 year)") or 0) >= 0.25
    ]
    use = prefer or pool
    return max(use, key=lambda r: float(r.get("composite_score") or -99))


def update_registry() -> None:
    text = REGISTRY.read_text(encoding="utf-8")
    marker = "### Funded survival, reserve & payout policy (Phase 50)"
    block = """### Funded survival, reserve & payout policy (Phase 50)

| Field | Value |
|-------|--------|
| Phase | 50 |
| Status | See `phase50_validation.json` verdict |
| Question | Can a funded-account reserve, payout, and cushion-dependent risk policy produce repeated payouts without near-certain long-horizon ruin? |
| Forbidden | Retune GC/NQ/ES; freeze ES; enable broker; write production risk/payout into operating policy; martingale |
| Evidence | `docs/PHASE50_FUNDED_SURVIVAL_PAYOUT_POLICY.md`, `reports/phase50_*`, `phase50_validation.json`, `tests_phase50.py` |
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


def _pctf(x: Any) -> str:
    if x is None:
        return "n/a"
    try:
        return f"{100.0 * float(x):.1f}%"
    except (TypeError, ValueError):
        return "n/a"


def _numf(x: Any, nd: int = 0) -> str:
    if x is None:
        return "n/a"
    try:
        return f"{float(x):.{nd}f}"
    except (TypeError, ValueError):
        return "n/a"


def _group_means(rows: list[dict[str, Any]], key: str) -> list[dict[str, Any]]:
    buckets: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        buckets.setdefault(str(r.get(key)), []).append(r)
    out = []
    metrics = (
        "P(first payout)",
        "P(5 payouts)",
        "P(10 payouts)",
        "P(survive 1 year)",
        "P(survive 504)",
        "expected_cumulative_payout",
        "P(breach)",
    )
    for name, grp in sorted(buckets.items()):
        row = {key: name, "n": len(grp)}
        for m in metrics:
            vs = [float(g[m]) for g in grp if g.get(m) is not None]
            row[m] = float(np.mean(vs)) if vs else None
        out.append(row)
    return out


def _effects(all_pay, all_res, all_dyn, all_ctrl, all_curves) -> dict[str, Any]:
    baseline = [r for r in all_curves if r.get("policy") == "PHASE49_BASELINE"]
    none = [r for r in all_curves if r.get("policy") == "DIAGNOSTIC_PAYOUT_NONE"]
    return {
        "payout_mode": _group_means(all_pay, "payout_mode"),
        "reserve_usd": _group_means(all_res, "reserve_usd"),
        "dynamic_risk": _group_means(all_dyn, "use_dynamic_risk"),
        "daily_stop": _group_means([r for r in all_ctrl if str(r.get("policy", "")).startswith("DSTOP_")], "daily_stop"),
        "streak_mode": _group_means([r for r in all_ctrl if str(r.get("policy", "")).startswith("STREAK_")], "streak_mode"),
        "post_lock_scale": _group_means(
            [r for r in all_ctrl if str(r.get("policy", "")).startswith("POSTLOCK_")], "post_lock_scale"
        ),
        "phase49_vs_no_payout": {
            "baseline_mean_P(survive 504)": float(np.mean([r["P(survive 504)"] for r in baseline])) if baseline else None,
            "no_payout_mean_P(survive 504)": float(np.mean([r["P(survive 504)"] for r in none])) if none else None,
            "baseline_mean_P(breach)": float(np.mean([r["P(breach)"] for r in baseline])) if baseline else None,
            "no_payout_mean_P(breach)": float(np.mean([r["P(breach)"] for r in none])) if none else None,
        },
    }


def _md_group(title: str, rows: list[dict[str, Any]], key: str) -> str:
    if not rows:
        return f"### {title}\n\n(no rows)\n"
    lines = [
        f"### {title}",
        "",
        f"| {key} | P(first) | P(5) | P(10) | 1y survive | 504 survive | E[payout] | P(breach) | n |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for r in rows:
        lines.append(
            f"| {r.get(key)} | {_pctf(r.get('P(first payout)'))} | {_pctf(r.get('P(5 payouts)'))} | "
            f"{_pctf(r.get('P(10 payouts)'))} | {_pctf(r.get('P(survive 1 year)'))} | "
            f"{_pctf(r.get('P(survive 504)'))} | {_numf(r.get('expected_cumulative_payout'), 0)} | "
            f"{_pctf(r.get('P(breach)'))} | {r.get('n')} |"
        )
    return "\n".join(lines) + "\n"


def _md_best_table(best: dict[str, Any]) -> str:
    lines = [
        "| Pair | Class | Payout | Reserve | Dynamic | Daily stop | Streak | P(first) | P(5) | P(10) | 1y | 504 | E[payout] | P(breach) |",
        "|------|-------|--------|--------:|---------|------------|--------|---------:|-----:|------:|----:|----:|----------:|----------:|",
    ]
    for tag, blob in best.items():
        m = blob.get("metrics") or {}
        p = blob.get("policy") or {}
        lines.append(
            f"| {tag} | {blob.get('classification')} | {p.get('payout_mode')} | "
            f"{_numf(p.get('reserve_usd'), 0)} | {p.get('use_dynamic_risk')} | {p.get('daily_stop')} | "
            f"{p.get('streak_mode')} | {_pctf(m.get('P(first payout)'))} | {_pctf(m.get('P(5 payouts)'))} | "
            f"{_pctf(m.get('P(10 payouts)'))} | {_pctf(m.get('P(survive 1 year)'))} | "
            f"{_pctf(m.get('P(survive 504)'))} | {_numf(m.get('expected_cumulative_payout'), 0)} | "
            f"{_pctf(m.get('P(breach)'))} |"
        )
    return "\n".join(lines)


def _md_horizons(best: dict[str, Any]) -> str:
    chunks = []
    for tag, blob in best.items():
        m = blob.get("metrics") or {}
        lines = [
            f"### {tag}",
            "",
            "| Horizon | P(survive) | P(first) | P(2) | P(5) | P(10) | median payouts | median $ | E[$] | P(breach) | median t_breach |",
            "|--------:|-----------:|---------:|-----:|-----:|------:|---------------:|---------:|-----:|----------:|----------------:|",
        ]
        for h in HORIZONS:
            lines.append(
                f"| {h} | {_pctf(m.get(f'h{h}_P(survive)'))} | {_pctf(m.get(f'h{h}_P(first payout)'))} | "
                f"{_pctf(m.get(f'h{h}_P(2 payouts)'))} | {_pctf(m.get(f'h{h}_P(5 payouts)'))} | "
                f"{_pctf(m.get(f'h{h}_P(10 payouts)'))} | {_numf(m.get(f'h{h}_median_payouts'), 1)} | "
                f"{_numf(m.get(f'h{h}_median_cumulative_trader_payout'), 0)} | "
                f"{_numf(m.get(f'h{h}_expected_cumulative_trader_payout'), 0)} | "
                f"{_pctf(m.get(f'h{h}_P(breach)'))} | {_numf(m.get(f'h{h}_median_time_to_breach'), 0)} |"
            )
        chunks.append("\n".join(lines))
    return "\n\n".join(chunks)


def render_docs(payload: dict[str, Any]) -> str:
    best = payload.get("best_policies") or {}
    fx = payload.get("effects") or {}
    vs = fx.get("phase49_vs_no_payout") or {}
    return f"""# Phase 50 — Funded Survival, Reserve & Payout Policy Research

`DRY_RUN`. No broker. Frozen strategy logic was not modified. ES was not promoted. Operating-policy numerics were not written.

Paths: curve={payload.get("n_paths_curve")}, search={payload.get("n_paths_search")}, final={payload.get("n_paths_final")}.

## 1. Primary reason funded accounts failed in Phase 49

**{payload.get("primary_reason")}**

Phase 49 withdrew surplus down to MLL + 25% of *initial* max-loss, then kept **fixed** initial-DD risk. After MFFU lock at +$100 (FundedNext lock at equity $50,100), leftover cushion was small versus one micro plus an ordinary losing streak. The 504-day horizon then sampled enough of those events to produce near-certain ruin at tradable sizes.

That is **policy-driven ruin after lock**, not proof that 30–90 day funded operation is impossible. `DIAGNOSTIC_PAYOUT_NONE` vs `PHASE49_BASELINE` separates payout-stripping from a worthless edge:

- mean P(survive 504) baseline: `{_pctf(vs.get("baseline_mean_P(survive 504)"))}`
- mean P(survive 504) no-payout: `{_pctf(vs.get("no_payout_mean_P(survive 504)"))}`
- mean P(breach) baseline: `{_pctf(vs.get("baseline_mean_P(breach)"))}`
- mean P(breach) no-payout: `{_pctf(vs.get("no_payout_mean_P(breach)"))}`

Phase 49 baseline in this engine **forces** sized trades through remaining cushion (no `BLOCK_INSUFFICIENT_RISK_CAPACITY`). Phase 50 candidate policies skip when one micro exceeds available cushion.

Breach timing and the account state immediately preceding breach: `reports/phase50_funded_root_cause/`.

## 2. Survival curves by horizon

Horizons: 30, 60, 90, 180, 252, 504 trading days. Machine-readable: `reports/phase50_survival_curves/`.

{_md_horizons(best)}

## 3. Effect of payout frequency

{_md_group("Payout-mode grid (search paths, averaged across pairs)", fx.get("payout_mode") or [], "payout_mode")}

A payout removes only surplus above the internal reserve. `PAYOUT_NONE` is diagnostic, not a production recommendation. Maximum withdrawal is not assumed optimal.

## 4. Effect of retained reserve

{_md_group("Reserve grid (USD and fraction-of-max-loss cells, averaged)", fx.get("reserve_usd") or [], "reserve_usd")}

## 5. Effect of dynamic / cushion-dependent risk

{_md_group("Fixed 1-micro vs dynamic HEALTHY/CAUTION/DEFENSIVE/CRITICAL/LOCKOUT", fx.get("dynamic_risk") or [], "use_dynamic_risk")}

Risk is not allowed to increase after losses. Thresholds were searched, not assumed.

## 6. Effect of internal daily stops

{_md_group("Internal daily-stop grid", fx.get("daily_stop") or [], "daily_stop")}

Phase 48 firms may have no DLL. These are AITRADE internal stops.

## 7. Effect of loss-streak controls

{_md_group("Streak grid (no martingale; pause resumes next session reduced)", fx.get("streak_mode") or [], "streak_mode")}

{_md_group("Pre vs post MLL-lock risk scale", fx.get("post_lock_scale") or [], "post_lock_scale")}

## 8. Minimum executable contract-floor analysis

See `reports/phase50_funded_root_cause/contract_floor.csv`. MNQ / MES / MGC under each book's actual stop distribution. When `min_executable / available_cushion > 1`, candidate policies apply **BLOCK_INSUFFICIENT_RISK_CAPACITY**.

## 9–15. Best survival-adjusted policy per strategy × firm

Selection uses a composite **and** the component metrics. Hide-and-survive (high survival, almost no payouts) is not preferred when a paying alternative exists. First-payout-then-death is penalized.

{_md_best_table(best)}

Classifications:

{json.dumps(payload.get("classifications"), indent=2, default=str)}

### Required metric snapshot (stitched policy, final paths)

| Pair | P(first) | P(5) | P(10) | 1y survival | E[cumulative trader payout] | class |
|------|---------:|-----:|------:|------------:|----------------------------:|-------|
{chr(10).join(
    f"| {tag} | {_pctf((b.get('metrics') or {{}}).get('P(first payout)'))} | {_pctf((b.get('metrics') or {{}}).get('P(5 payouts)'))} | {_pctf((b.get('metrics') or {{}}).get('P(10 payouts)'))} | {_pctf((b.get('metrics') or {{}}).get('P(survive 1 year)'))} | {_numf((b.get('metrics') or {{}}).get('expected_cumulative_payout'), 0)} | {b.get('classification')} |"
    for tag, b in best.items()
)}

Full stitched policy JSON: `reports/phase50_dynamic_risk/best_stitched.json`.

## 16. Frozen-hash integrity

- GC: `{payload["frozen"]["gc"]}`
- NQ: `{payload["frozen"]["nq"]}`
- Paper journals empty: `{payload["frozen"].get("paper_journals_empty")}`
- ES not frozen: `{payload.get("es_not_frozen")}`

## 17. DRY_RUN confirmation

- execution_default: `{payload.get("execution_default")}`
- broker_execution: `{payload.get("broker_execution")}`
- operating policy risk_per_trade still null: `{payload.get("policy_risk_still_null")}`

## Evaluation (unchanged — not production-locked)

{json.dumps(EVAL_PRESERVED, indent=2)}

## What this phase did not do

No strategy retune. No frozen file edits. No ES freeze. No live execution. No production payout/risk values in `aitrade_operating_policy_v1.json`.
"""


def run_pair(book: str, profile: str, days: list, n_search: int, n_curve: int, n_final: int) -> dict[str, Any]:
    stats = analyze_book(book, days)
    narrative = phase49_failure_narrative(book, profile, stats)
    fl = stats["contract_floor"]
    one = float(fl["median_executable_usd"])
    baseline = phase49_baseline_policy(book, profile)
    no_pay = replace(baseline, name="DIAGNOSTIC_PAYOUT_NONE", payout_mode="PAYOUT_NONE")
    curve_rows = []
    for pol in (baseline, no_pay):
        out = _run(days, book, profile, pol, n_curve, f"curve:{book}:{profile}:{pol.name}")
        curve_rows.append(_row(book, profile, pol, out))

    reserve_rows = []
    for usd in RESERVE_USD:
        pol = one_micro_policy(book, days, name=f"RESERVE_{int(usd)}", reserve_usd=usd)
        out = _run(days, book, profile, pol, n_search, f"res:{book}:{profile}:{usd}")
        reserve_rows.append(_row(book, profile, pol, out))
    for frac in RESERVE_FRACS:
        pol = one_micro_policy(
            book,
            days,
            name=f"RESERVE_FRAC_{frac}",
            reserve_usd=0.0,
            reserve_frac_max_loss=frac,
        )
        out = _run(days, book, profile, pol, n_search, f"resf:{book}:{profile}:{frac}")
        reserve_rows.append(_row(book, profile, pol, out))
    best_res = best_of(reserve_rows)
    res_usd = float((best_res or {}).get("reserve_usd") or 1000.0)

    payout_rows = []
    for mode in PAYOUT_MODES:
        pol = one_micro_policy(book, days, name=f"PAY_{mode}", payout_mode=mode, reserve_usd=res_usd)
        out = _run(days, book, profile, pol, n_search, f"pay:{book}:{profile}:{mode}")
        payout_rows.append(_row(book, profile, pol, out))
    pay_for_best = [r for r in payout_rows if r["payout_mode"] != "PAYOUT_NONE"]
    best_pay = best_of(pay_for_best)
    mode = str((best_pay or {}).get("payout_mode") or "FIXED_INTERNAL_RESERVE")

    dyn_rows = []
    dyn_specs = [
        FundedPolicy(name="FIXED_ONE_MICRO", payout_mode=mode, reserve_usd=res_usd, use_dynamic_risk=False, fixed_risk_usd=one),
        FundedPolicy(name="DYN_A", payout_mode=mode, reserve_usd=res_usd, use_dynamic_risk=True, healthy_cushion_frac=0.10, caution_thr=0.55, defensive_thr=0.35, critical_thr=0.18),
        FundedPolicy(name="DYN_B", payout_mode=mode, reserve_usd=res_usd, use_dynamic_risk=True, healthy_cushion_frac=0.12, caution_thr=0.50, defensive_thr=0.30, critical_thr=0.15),
        FundedPolicy(name="DYN_C", payout_mode=mode, reserve_usd=res_usd, use_dynamic_risk=True, healthy_cushion_frac=0.08, caution_thr=0.60, defensive_thr=0.40, critical_thr=0.20),
    ]
    for pol in dyn_specs:
        out = _run(days, book, profile, pol, n_search, f"dyn:{book}:{profile}:{pol.name}")
        dyn_rows.append(_row(book, profile, pol, out))
    best_dyn = best_of(dyn_rows)
    use_dyn = bool((best_dyn or {}).get("use_dynamic_risk"))
    hfrac = float((best_dyn or {}).get("healthy_cushion_frac") or 0.12)

    ctrl_rows = []
    for ds in ("none", "r_loss", "cushion_frac", "consec"):
        pol = FundedPolicy(
            name=f"DSTOP_{ds}",
            payout_mode=mode,
            reserve_usd=res_usd,
            use_dynamic_risk=use_dyn,
            healthy_cushion_frac=hfrac,
            fixed_risk_usd=None if use_dyn else one,
            daily_stop=ds,
        )
        out = _run(days, book, profile, pol, n_search, f"ds:{book}:{profile}:{ds}")
        ctrl_rows.append(_row(book, profile, pol, out))
    for sm in ("none", "reduce2", "reduce3", "pause3"):
        pol = FundedPolicy(
            name=f"STREAK_{sm}",
            payout_mode=mode,
            reserve_usd=res_usd,
            use_dynamic_risk=use_dyn,
            healthy_cushion_frac=hfrac,
            fixed_risk_usd=None if use_dyn else one,
            daily_stop=str((best_of(ctrl_rows) or {}).get("daily_stop") or "none"),
            streak_mode=sm,
        )
        out = _run(days, book, profile, pol, n_search, f"st:{book}:{profile}:{sm}")
        ctrl_rows.append(_row(book, profile, pol, out))
    for post in (1.0, 0.50):
        pol = FundedPolicy(
            name=f"POSTLOCK_{post}",
            payout_mode=mode,
            reserve_usd=res_usd,
            use_dynamic_risk=use_dyn,
            healthy_cushion_frac=hfrac,
            fixed_risk_usd=None if use_dyn else one,
            post_lock_scale=post,
        )
        out = _run(days, book, profile, pol, n_search, f"pl:{book}:{profile}:{post}")
        ctrl_rows.append(_row(book, profile, pol, out))

    stitched = FundedPolicy(
        name="STITCHED_BEST",
        payout_mode=mode,
        reserve_usd=res_usd,
        use_dynamic_risk=use_dyn,
        healthy_cushion_frac=hfrac,
        fixed_risk_usd=None if use_dyn else one,
        daily_stop=str((best_of([r for r in ctrl_rows if str(r["policy"]).startswith("DSTOP_")]) or {}).get("daily_stop") or "none"),
        streak_mode=str((best_of([r for r in ctrl_rows if str(r["policy"]).startswith("STREAK_")]) or {}).get("streak_mode") or "none"),
        post_lock_scale=float((best_of([r for r in ctrl_rows if str(r["policy"]).startswith("POSTLOCK_")]) or {}).get("post_lock_scale") or 1.0),
    )
    final = _run(days, book, profile, stitched, n_final, f"final:{book}:{profile}")
    final_row = _row(book, profile, stitched, final)
    return {
        "stats": stats,
        "narrative": narrative,
        "contract_floor": fl,
        "curve_rows": curve_rows,
        "reserve_rows": reserve_rows,
        "payout_rows": payout_rows,
        "dyn_rows": dyn_rows,
        "ctrl_rows": ctrl_rows,
        "final_row": final_row,
        "final_summary": final["summary"],
        "stitched_policy": stitched.__dict__,
        "classification": final_row["classification"],
    }


def run(n_search: int = N_PATHS_SEARCH, n_curve: int = N_PATHS_CURVE, n_final: int = N_PATHS_FINAL) -> dict[str, Any]:
    frozen_before = assert_frozen()
    if not frozen_before.get("ok"):
        raise RuntimeError(f"FROZEN_INTEGRITY_FAIL_BEFORE:{frozen_before}")
    for d in (CAUSE, CURVES, PAYOUT_DIR, RESERVE_DIR, DYN_DIR):
        d.mkdir(parents=True, exist_ok=True)

    day_map = _load_days()
    all_curves, all_res, all_pay, all_dyn, all_ctrl, floors, narratives, best, classes = [], [], [], [], [], [], [], {}, {}
    for book in BOOKS:
        days = day_map[book]
        floors.append(min_executable(book, days))
        for profile in PROFILES:
            pack = run_pair(book, profile, days, n_search, n_curve, n_final)
            narratives.append(pack["narrative"])
            all_curves.extend(pack["curve_rows"])
            all_res.extend(pack["reserve_rows"])
            all_pay.extend(pack["payout_rows"])
            all_dyn.extend(pack["dyn_rows"])
            all_ctrl.extend(pack["ctrl_rows"])
            tag = f"{book}->{profile}"
            best[tag] = {
                "policy": pack["stitched_policy"],
                "metrics": pack["final_row"],
                "classification": pack["classification"],
            }
            classes[tag] = pack["classification"]

    write_csv(CAUSE / "contract_floor.csv", floors)
    write_csv(CAUSE / "phase49_failure_decomp.csv", narratives)
    (CAUSE / "phase49_failure_decomp.json").write_text(json.dumps(narratives, indent=2), encoding="utf-8")
    write_csv(CAUSE / "breach_timing_phase49_baseline.csv", [r for r in all_curves if r.get("policy") == "PHASE49_BASELINE"])
    write_csv(CURVES / "survival_curves.csv", all_curves)
    (CURVES / "survival_curves.json").write_text(json.dumps(all_curves, indent=2, default=str), encoding="utf-8")
    write_csv(RESERVE_DIR / "reserve_grid.csv", all_res)
    write_csv(PAYOUT_DIR / "payout_grid.csv", all_pay)
    write_csv(DYN_DIR / "dynamic_risk_grid.csv", all_dyn)
    write_csv(DYN_DIR / "daily_stop_streak_postlock.csv", all_ctrl)
    write_csv(DYN_DIR / "best_stitched.csv", [v["metrics"] for v in best.values()])
    (DYN_DIR / "best_stitched.json").write_text(json.dumps(best, indent=2, default=str), encoding="utf-8")
    effects = _effects(all_pay, all_res, all_dyn, all_ctrl, all_curves)
    (PAYOUT_DIR / "payout_mode_means.json").write_text(json.dumps(effects["payout_mode"], indent=2), encoding="utf-8")
    (RESERVE_DIR / "reserve_means.json").write_text(json.dumps(effects["reserve_usd"], indent=2), encoding="utf-8")
    (DYN_DIR / "control_means.json").write_text(json.dumps({k: effects[k] for k in ("dynamic_risk", "daily_stop", "streak_mode", "post_lock_scale")}, indent=2), encoding="utf-8")

    primary = narratives[0]["primary_reason"] if narratives else "post-payout account cushion"
    # majority primary
    reasons = [n["primary_reason"] for n in narratives]
    primary = max(set(reasons), key=reasons.count) if reasons else primary

    proc = subprocess.run([sys.executable, "-m", "unittest", "tests_phase50", "-v"], cwd=str(ROOT), capture_output=True, text=True)
    frozen_after = assert_frozen()
    policy = load_operating_policy()
    policy_doc = json.loads(POLICY_PATH.read_text(encoding="utf-8"))
    tests_ok = proc.returncode == 0
    frozen_ok = bool(frozen_after.get("ok"))
    risk_null = policy_doc.get("numerics_pending_simulation", {}).get("risk_per_trade") is None
    has_days = all(len(day_map[b]) > 0 for b in BOOKS)
    verdict = (
        "PHASE50_FUNDED_SURVIVAL_RESEARCH_READY"
        if tests_ok and frozen_ok and risk_null and has_days
        else "PHASE50_FUNDED_SURVIVAL_RESEARCH_BLOCKED"
    )
    payload = {
        "phase": 50,
        "verdict": verdict,
        "execution_default": policy.execution_default,
        "broker_execution": False,
        "dry_run": True,
        "policy_risk_still_null": risk_null,
        "es_not_frozen": not (ROOT / "strategy_frozen" / "es_dvp_phase47.json").exists(),
        "n_paths_curve": n_curve,
        "n_paths_search": n_search,
        "n_paths_final": n_final,
        "horizons": list(HORIZONS),
        "primary_reason": primary,
        "classifications": classes,
        "best_policies": best,
        "effects": effects,
        "eval_research_preserved": EVAL_PRESERVED,
        "frozen_before": frozen_before,
        "frozen_after": frozen_after,
        "frozen": {"gc": frozen_after.get("gc"), "nq": frozen_after.get("nq"), "paper_journals_empty": frozen_ok},
        "tests": {"returncode": proc.returncode, "ran": proc.stderr.count(" ... ok") + proc.stdout.count(" ... ok"), "tail": (proc.stderr or proc.stdout)[-2000:]},
        "no_martingale": True,
        "fn_unknown_rules_fail_closed": True,
    }
    DOCS.parent.mkdir(parents=True, exist_ok=True)
    DOCS.write_text(render_docs(payload), encoding="utf-8")
    VALIDATION.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    update_registry()
    return payload


def main() -> int:
    ns, nc, nf = N_PATHS_SEARCH, N_PATHS_CURVE, N_PATHS_FINAL
    if "--quick" in sys.argv:
        ns, nc, nf = 80, 120, 120
    payload = run(n_search=ns, n_curve=nc, n_final=nf)
    print(json.dumps({"verdict": payload["verdict"], "classifications": payload["classifications"]}, indent=2))
    print(payload["verdict"])
    return 0 if payload["verdict"] == "PHASE50_FUNDED_SURVIVAL_RESEARCH_READY" else 1


if __name__ == "__main__":
    raise SystemExit(main())
