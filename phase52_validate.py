"""Phase 52 — Prop Execution Policy Layer. DRY_RUN. Does not alter frozen strategies or prior research reports."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import numpy as np

from aitrade_operating_policy import load_operating_policy
from nq_microstructure_models import FROZEN_GC_HASH, FROZEN_NQ_HASH
from phase34_validate import assert_frozen, file_sha256
from phase49_prop_sim import trades_to_days
from phase49_trade_audit import NQ_SRC, load_phase46_csv, write_csv
from phase49b_engine import pack_days
from phase52_policy import (
    DAILY_STOP_FRAC,
    FAST_QTY,
    FROZEN_NQ_HASH as POLICY_NQ_HASH,
    KILL_SWITCHES,
    SAFE_QTY,
    UNIT_RISK_USD,
    fn_eval_rules_catalog,
)
from phase52_sim import simulate_policy, summarize
from risk_manager import propose_size

ROOT = Path(__file__).resolve().parent
VALIDATION = ROOT / "phase52_validation.json"
DOCS = ROOT / "docs" / "PHASE52_PROP_EXECUTION_POLICY_LOCK.md"
REGISTRY = ROOT / "docs" / "STRATEGY_REGISTRY.md"
POLICY_PATH = ROOT / "config" / "aitrade_operating_policy_v1.json"
PROP_POLICY_PATH = ROOT / "config" / "aitrade_prop_execution_policy_v1.json"

OUT_POLICY = ROOT / "reports" / "phase52_prop_policy"
OUT_STATE = ROOT / "reports" / "phase52_state_machine"
OUT_RULES = ROOT / "reports" / "phase52_rule_engine"
OUT_RISK = ROOT / "reports" / "phase52_risk_policy"
OUT_DEG = ROOT / "reports" / "phase52_degradation"
OUT_STRESS = ROOT / "reports" / "phase52_stress"
OUT_PARETO = ROOT / "reports" / "phase52_pareto"

PRIOR_REPORTS = (
    ROOT / "reports" / "phase49_eval_simulation" / "eval_matrix.json",
    ROOT / "reports" / "phase49_strategy_distributions" / "nq_distribution.json",
    ROOT / "reports" / "phase50_dynamic_risk" / "best_stitched.json",
    ROOT / "reports" / "phase51_account_flywheel" / "primary_models.json",
    ROOT / "phase49_validation.json",
    ROOT / "phase50_validation.json",
    ROOT / "phase51_validation.json",
)
PHASE49B_VAL = ROOT / "phase49b_validation.json"

N_SCREEN = 2_500
N_FINAL = 10_000
N_STRESS = 5_000
N_NEAR = 2_500

VARIANTS = (
    ("SAFE1", "A. 1 MNQ SAFE"),
    ("RAW2", "B. 2 MNQ RAW"),
    ("GOV2", "C. 2 MNQ + 35% DD daily governor"),
    ("GOV2_DEMOTE", "D. 2 MNQ + governor + degradation demotion"),
    ("GOV2_DEMOTE_NEAR", "E. 2 MNQ + governor + demotion + near-target"),
)


def _write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, default=str), encoding="utf-8")


def _csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    flat = []
    for r in rows:
        nr = {}
        for k, v in r.items():
            nr[k] = json.dumps(v) if isinstance(v, (dict, list)) else v
        flat.append(nr)
    write_csv(path, flat)


def _rng(*parts) -> np.random.Generator:
    acc = 5252
    s = "|".join(str(p) for p in parts)
    for i, c in enumerate(s):
        acc = (acc + (i + 1) * ord(c) * 131) % 2_147_483_647
    return np.random.default_rng(int(acc))


def _fp(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"path": str(path.as_posix()), "exists": False, "sha256": None}
    return {"path": str(path.as_posix()), "exists": True, "sha256": file_sha256(path), "bytes": path.stat().st_size}


def journals() -> list[dict[str, Any]]:
    rows = []
    for rel in (
        "journal/phase26_gc_vwap_v2_paper/paper_trades.jsonl",
        "journal/phase30_nq_dvp_paper/paper_trades.jsonl",
        "journal/phase47_es_dvp_paper/paper_trades.jsonl",
    ):
        p = ROOT / rel
        rows.append({"path": rel, "empty": p.exists() and p.stat().st_size == 0, "bytes": p.stat().st_size if p.exists() else None})
    return rows


def run_variant(pack, *, variant: str, n_paths: int, near_rule: str = "ONE_FAST_R", **kw) -> dict[str, Any]:
    batch = simulate_policy(
        pack,
        variant=variant,
        n_paths=n_paths,
        rng=_rng(variant, near_rule, n_paths, *sorted((str(k), str(v)) for k, v in kw.items())),
        near_rule=near_rule,
        **kw,
    )
    row = summarize(batch)
    row["label"] = dict(VARIANTS).get(variant, variant)
    return {"row": row, "batch": batch}


def select_policy(rows: list[dict[str, Any]]) -> dict[str, Any]:
    by = {r["variant"]: r for r in rows}
    c = by["GOV2"]
    d = by["GOV2_DEMOTE"]
    e = by["GOV2_DEMOTE_NEAR"]
    ranked = []
    for key, complexity, safety, interpret in (
        ("GOV2_DEMOTE_NEAR", 5, 5, 3),
        ("GOV2_DEMOTE", 4, 5, 4),
        ("GOV2", 3, 4, 5),
        ("SAFE1", 1, 5, 5),
        ("RAW2", 1, 1, 5),
    ):
        r = by[key]
        p = float(r["P(pass)"])
        b = float(r["P(breach)"])
        med = r["median_days_to_pass"]
        ranked.append(
            {
                **{k: r[k] for k in r if k != "breach_causes"},
                "breach_causes": r.get("breach_causes"),
                "complexity_score": complexity,
                "prop_rule_safety_score": safety,
                "interpretability_score": interpret,
                "survival_ok": p > b,
                "fast_eval_ok": med is not None and float(med) <= 25.0,
            }
        )
    chosen = "GOV2"
    reason = "fallback_49b_governor"
    if float(e["P(pass)"]) >= float(c["P(pass)"]) - 0.03 and float(e["P(breach)"]) <= float(c["P(breach)"]) + 0.02:
        e_med = e["median_days_to_pass"]
        c_med = c["median_days_to_pass"]
        if e_med is not None and c_med is not None and float(e_med) <= float(c_med) + 3.0:
            chosen = "GOV2_DEMOTE_NEAR"
            reason = "E preserves C pass/breach/speed and adds demotion+near-target"
    if chosen == "GOV2":
        if float(d["P(pass)"]) >= float(c["P(pass)"]) - 0.03 and float(d["P(breach)"]) <= float(c["P(breach)"]) + 0.02:
            d_med = d["median_days_to_pass"]
            c_med = c["median_days_to_pass"]
            if d_med is not None and c_med is not None and float(d_med) <= float(c_med) + 3.0:
                chosen = "GOV2_DEMOTE"
                reason = "D preserves C metrics and adds FAST→PROTECTED demotion"
    if float(by[chosen]["P(pass)"]) <= float(by[chosen]["P(breach)"]):
        if float(by["SAFE1"]["P(pass)"]) > float(by["SAFE1"]["P(breach)"]):
            chosen = "SAFE1"
            reason = "FAST variants fail survival; fall back to 1 MNQ SAFE"
    return {"selected_variant": chosen, "reason": reason, "ranked": ranked, "c": c, "d": d, "e": e}


def _pct(x) -> str:
    if x is None:
        return "n/a"
    return f"{100.0 * float(x):.1f}%"


def _num(x, nd=1) -> str:
    if x is None:
        return "n/a"
    return f"{float(x):.{nd}f}"


def write_docs(payload: dict[str, Any]) -> None:
    sel = payload["selected"]
    srow = payload["selected_row"]
    deg10 = payload["stress"].get("expectancy_-10pct") or {}
    flip10 = payload["stress"].get("wr_flip_10pct") or {}
    rc = payload["requires_confirmation"]
    frozen = payload["frozen_after"]
    tests = payload["tests"]
    DOCS.write_text(
        f"""# Phase 52 — Prop Execution Policy Lock

## Executive summary

Phase 52 builds a **machine-enforceable prop execution policy** between the frozen NQ Drift VWAP Pullback signals and order intent. It does not retune the strategy. Execution remains **DRY_RUN**.

**Verdict: `{payload["verdict"]}`**

Selected FundedNext Flex 50K policy: **{sel}** — {payload.get("selection_reason")}

| Metric | Selected policy |
| --- | --- |
| FAST quantity | {payload["fast_qty"]} MNQ |
| SAFE quantity | {payload["safe_qty"]} MNQ |
| Daily governor | `daily_loss >= 0.35 * remaining_dd_at_session_open` |
| Baseline P(pass) | {_pct(srow.get("P(pass)"))} |
| Baseline P(breach) | {_pct(srow.get("P(breach)"))} |
| Median pass days | {_num(srow.get("median_days_to_pass"), 0)} |
| P(pass ≤14d) among passers | {_pct(srow.get("P(pass <=14d)"))} |
| Expectancy −10% P(pass) | {_pct(deg10.get("P(pass)"))} |
| 10% winner→loser flip P(pass) | {_pct(flip10.get("P(pass)"))} |

FAST is a privilege. Degradation demotes `EVAL_FAST → EVAL_PROTECTED` (1 MNQ). Quantity never increases after losses, to catch up, or to recover drawdown. 3 MNQ is rejected.

## 1. Freeze integrity

- GC hash: `{frozen.get("gc")}`
- NQ hash: `{frozen.get("nq")}`
- assert_frozen: `{frozen.get("ok")}`
- Paper journals empty: `{payload.get("journals_empty")}`
- Prior Phase 49/50/51 report fingerprints unchanged: `{payload.get("prior_reports_unchanged")}`
- execution_default: `{payload.get("execution_default")}`

## 2. Account state machine

States: `EVAL_SAFE`, `EVAL_FAST`, `EVAL_PROTECTED`, `EVAL_NEAR_TARGET`, `EVAL_DAILY_STOPPED`, `EVAL_BREACHED`, `EVAL_PASSED`, `FUNDED_SAFE`, `FUNDED_PROTECTED`, `PAUSED`.

FundedNext candidate starts `EVAL_FAST`. Transitions are functions of equity, MLL, daily governor, near-target remainder, degradation flag, and integrity kills — no discretionary interpretation. See `reports/phase52_state_machine/`.

## 3. FundedNext Flex 50K rule engine

Canonical catalog: `reports/phase52_rule_engine/fn_flex_50k_catalog.json`.

Material evaluation-survival rules are sourced from `config/PROP_RULES_V1.json`. Firm daily loss limit is **NONE**; the 35% remaining-DD stop is an **AITRADE internal governor**. Firm news trading is **ALLOWED**; AITRADE still enforces ±5 minutes and fail-closed calendar handling.

## 4. Daily governor equation

At each Chicago Globex session open (17:00 CT):

```
remaining_drawdown_open = session_open_equity - mll
daily_stop_threshold    = 0.35 * remaining_drawdown_open
daily_loss              = session_open_equity - marked_equity
```

`marked_equity` includes realized P&L, unrealized P&L, and fees already in fills. Threshold is frozen for the session (EOD trailing MLL does not move intra-day). On trigger: block new entries, cancel pending entries, **do not flatten** (protective stops may remain) unless the firm mandatory-flat window requires it. Gap-through clips the fill to the remaining daily room in simulation, then `EVAL_DAILY_STOPPED` until the next 17:00 CT session.

## 5. Position sizing

```
allowed_qty = min(strategy_qty_cap, prop_contract_cap, drawdown_based_qty_cap, daily_governor_qty_cap, state_based_qty_cap)
```

- FAST = 2 MNQ, SAFE/PROTECTED/NEAR_TARGET = 1 MNQ, max = 2, reject 3.
- Not 1% of $50,000. `risk_per_trade.mode = PROP_CONTRACT_QTY`, unit risk $160 / MNQ at the frozen 80-pt stop.
- `propose_size` returns `PROP_QTY_LOCKED` in DRY_RUN.

## 6. FAST → PROTECTED demotion

Transparent rolling window (min 20 trades, roll 30):

- Warning: E[R] < 50% frozen, WR −8pp, loss streak 4, winner/loser degradation.
- Hard demote: E[R] < 0, WR −12pp, streak 5, WR collapse ≥18pp (winner→loser flip proxy).
- Recovery requires 40 subsequent qualifying trades and **does not restore FAST during the evaluation**.

## 7. Kill switches

See `reports/phase52_prop_policy/kill_switches.json`. Safety blocks/cancels/pauses; flatten only when position integrity, hash mismatch, unknown equity, invalid DD, max position, or imminent breach require it. Daily stop does **not** flatten.

## 8. News blackout

Internal AITRADE lock: **±5 minutes** around restricted events (`nq_post_news_models` defaults) plus family-port clock window **08:25–08:35 ET**. Missing/stale calendar → fail closed. Existing protective stops may remain.

## 9. Near-target

Selected near-target rule: `{payload.get("near_rule")}`. Execution size only; frozen signal logic unchanged. Size 2 MNQ → 1 MNQ in `EVAL_NEAR_TARGET` without changing frozen signals.

## 10–11. Stress and Pareto

Baseline variants A–E: `reports/phase52_pareto/`. Degradation and execution stress: `reports/phase52_stress/`. Policy is **not** optimized for sub-14-day passing.

## 12. Machine-readable policy

- `config/aitrade_prop_execution_policy_v1.json`
- `config/aitrade_operating_policy_v1.json` (`execution_default=DRY_RUN`, `broker_execution=false`)

## 13. Tests

`tests_phase52.py` returncode `{tests.get("returncode")}` — `{tests.get("ok")}`.

## 14. Remaining REQUIRES_CONFIRMATION (not eval-survival)

{json.dumps(rc, indent=2)}

## 15. What this phase did not do

No live trading. No evaluation account purchase. No frozen-strategy edit. No overwrite of Phase 49/49B/50/51 research reports. No sub-14-day speed search.
""",
        encoding="utf-8",
    )


def patch_registry(verdict: str) -> None:
    text = REGISTRY.read_text(encoding="utf-8")
    block = f"""### Prop execution policy layer (Phase 52)

| Field | Value |
|-------|--------|
| Phase | 52 |
| Status | See `phase52_validation.json` verdict (`{verdict}`) |
| Question | Can AITRADE deterministically translate frozen NQ DVP into a FundedNext Flex 50K-safe evaluation policy? |
| Forbidden | Retune GC/NQ/ES; freeze ES; enable broker; live execution; buy/activate accounts; overwrite Phase 49/49B/50/51 reports |
| Evidence | `docs/PHASE52_PROP_EXECUTION_POLICY_LOCK.md`, `reports/phase52_*`, `phase52_validation.json`, `tests_phase52.py` |
| Frozen impact | None. Phase 26 / 30 hashes unchanged. DRY_RUN only. `risk_per_trade` is now PROP_CONTRACT_QTY (2 MNQ FAST / 1 MNQ SAFE), not 1% of $50k. |
"""
    import re
    pat = re.compile(r"### Prop execution policy layer \(Phase 52\).*?(?=\n### |\n## |\Z)", re.S)
    if pat.search(text):
        text = pat.sub(block + "\n", text)
    else:
        marker = "## RESEARCH-ONLY / RETIRED\n"
        insert = f"## RESEARCH-ONLY / RETIRED\n\n{block}\n"
        text = text.replace(marker, insert, 1) if marker in text else text + "\n" + block
    REGISTRY.write_text(text, encoding="utf-8")


def decide_verdict(payload: dict[str, Any]) -> str:
    if not payload["frozen_before"].get("ok") or not payload["frozen_after"].get("ok"):
        return "STOP_PHASE52_FREEZE_INTEGRITY_FAILURE"
    if payload["frozen_before"].get("gc") != payload["frozen_after"].get("gc"):
        return "STOP_PHASE52_FREEZE_INTEGRITY_FAILURE"
    if payload["frozen_before"].get("nq") != payload["frozen_after"].get("nq"):
        return "STOP_PHASE52_FREEZE_INTEGRITY_FAILURE"
    if payload["frozen_before"].get("gc") != FROZEN_GC_HASH or payload["frozen_after"].get("nq") != FROZEN_NQ_HASH:
        return "STOP_PHASE52_FREEZE_INTEGRITY_FAILURE"
    if not payload.get("prior_reports_unchanged"):
        return "STOP_PHASE52_FREEZE_INTEGRITY_FAILURE"
    if payload.get("execution_default") != "DRY_RUN" or payload.get("broker_execution"):
        return "PROP_POLICY_IMPLEMENTATION_FAILED"
    if not payload["tests"].get("ok"):
        return "PROP_POLICY_IMPLEMENTATION_FAILED"
    material_rc = [r for r in payload["catalog"] if r.get("material_eval_survival") and r.get("status") == "REQUIRES_CONFIRMATION"]
    if material_rc:
        return "PROP_RULE_CONFIRMATION_BLOCKED"
    srow = payload["selected_row"]
    p = float(srow.get("P(pass)") if srow.get("P(pass)") is not None else 0)
    if p < 0.40:
        return "PROP_POLICY_STRESS_FAILED"
    deg10 = payload["stress"].get("expectancy_-10pct") or {}
    d10 = deg10.get("P(pass)")
    if float(d10 if d10 is not None else 0) < 0.35:
        return "PROP_POLICY_STRESS_FAILED"
    if payload["selected_variant"] not in ("GOV2_DEMOTE", "GOV2_DEMOTE_NEAR", "GOV2", "SAFE1"):
        return "PROP_POLICY_IMPLEMENTATION_FAILED"
    if propose_size()["status"] != "PROP_QTY_LOCKED":
        return "PROP_POLICY_IMPLEMENTATION_FAILED"
    return "PROP_EXECUTION_POLICY_LOCKED"


def run(*, quick: bool = False) -> dict[str, Any]:
    n_screen = 400 if quick else N_SCREEN
    n_final = 800 if quick else N_FINAL
    n_stress = 400 if quick else N_STRESS
    n_near = 400 if quick else N_NEAR

    frozen_before = assert_frozen()
    if not frozen_before.get("ok"):
        raise RuntimeError(f"STOP_PHASE52_FREEZE_INTEGRITY_FAILURE:{frozen_before}")
    prior_before = [_fp(p) for p in PRIOR_REPORTS]
    if PHASE49B_VAL.exists():
        prior_before.append(_fp(PHASE49B_VAL))

    for d in (OUT_POLICY, OUT_STATE, OUT_RULES, OUT_RISK, OUT_DEG, OUT_STRESS, OUT_PARETO):
        d.mkdir(parents=True, exist_ok=True)

    nq_trades = load_phase46_csv(NQ_SRC, strategy="NQ_DVP_FROZEN", instrument="NQ", cost_note="phase46")
    pack = pack_days(trades_to_days(nq_trades, "NQ"), "NQ")

    catalog = fn_eval_rules_catalog()
    _write_json(OUT_RULES / "fn_flex_50k_catalog.json", catalog)
    _csv(OUT_RULES / "fn_flex_50k_catalog.csv", catalog)

    near_rows = []
    for rule in ("ONE_FAST_R", "ONE_SAFE_R", "PCT_80", "PCT_90", "PCT_95"):
        out = run_variant(pack, variant="GOV2_DEMOTE_NEAR", n_paths=n_near, near_rule=rule)
        row = out["row"]
        row["near_rule"] = rule
        near_rows.append(row)
    near_pick = "ONE_FAST_R"
    base_near = next(r for r in near_rows if r["near_rule"] == "ONE_FAST_R")
    for r in near_rows:
        if r["near_rule"] == "ONE_FAST_R":
            continue
        if float(r["P(pass)"]) >= float(base_near["P(pass)"]) - 0.005:
            if r["median_days_to_pass"] is not None and base_near["median_days_to_pass"] is not None:
                if float(r["median_days_to_pass"]) <= float(base_near["median_days_to_pass"]) + 2.0:
                    if float(r["P(breach)"]) <= float(base_near["P(breach)"]) + 0.005:
                        near_pick = r["near_rule"]
                        base_near = r
    _csv(OUT_POLICY / "near_target_grid.csv", near_rows)
    _write_json(OUT_POLICY / "near_target_grid.json", {"selected": near_pick, "rows": near_rows})

    variant_rows = []
    for variant, _label in VARIANTS:
        nr = near_pick if variant == "GOV2_DEMOTE_NEAR" else "ONE_FAST_R"
        n = n_final if variant in ("GOV2", "GOV2_DEMOTE", "GOV2_DEMOTE_NEAR") else n_screen
        out = run_variant(pack, variant=variant, n_paths=n, near_rule=nr)
        variant_rows.append(out["row"])
    _csv(OUT_PARETO / "variants.csv", variant_rows)
    _write_json(OUT_PARETO / "variants.json", variant_rows)

    selection = select_policy(variant_rows)
    selected_variant = selection["selected_variant"]
    selected_row = next(r for r in variant_rows if r["variant"] == selected_variant)
    _write_json(OUT_PARETO / "selection.json", selection)

    stress_specs = [
        ("baseline", {}),
        ("expectancy_-5pct", {"expectancy_scale": 0.95}),
        ("expectancy_-10pct", {"expectancy_scale": 0.90}),
        ("expectancy_-20pct", {"expectancy_scale": 0.80}),
        ("wr_degrade", {"wr_flip": 0.03}),
        ("wr_flip_5pct", {"wr_flip": 0.05}),
        ("wr_flip_10pct", {"wr_flip": 0.10}),
        ("loss_cluster_3", {"block_cluster": 3}),
        ("slippage_+100pct", {"slippage_mult": 2.0}),
        ("commission_+100pct", {"commission_mult": 2.0}),
        ("delayed_exit_10pct", {"delayed_exit_p": 0.10}),
        ("missed_entry_10pct", {"miss_entry_p": 0.10}),
        ("skip_day_5pct", {"skip_day_p": 0.05}),
    ]
    stress_rows = []
    stress_map: dict[str, Any] = {}
    for name, kw in stress_specs:
        out = run_variant(pack, variant=selected_variant, n_paths=n_stress, near_rule=near_pick, **kw)
        row = out["row"]
        row["stress"] = name
        stress_rows.append(row)
        stress_map[name] = row
    _csv(OUT_STRESS / "stress_grid.csv", stress_rows)
    _write_json(OUT_STRESS / "stress_grid.json", stress_map)

    state_doc = {
        "states": [
            "EVAL_SAFE",
            "EVAL_FAST",
            "EVAL_PROTECTED",
            "EVAL_NEAR_TARGET",
            "EVAL_DAILY_STOPPED",
            "EVAL_BREACHED",
            "EVAL_PASSED",
            "FUNDED_SAFE",
            "FUNDED_PROTECTED",
            "PAUSED",
        ],
        "initial": "EVAL_FAST",
        "transitions": {
            "EVAL_FAST→EVAL_PROTECTED": "degradation hard threshold",
            "EVAL_FAST→EVAL_NEAR_TARGET": f"remaining_profit <= rule {near_pick}",
            "EVAL_FAST→EVAL_DAILY_STOPPED": "daily_loss >= 0.35 * remaining_dd_open",
            "ANY→PAUSED": "integrity/kill switch",
            "ANY→EVAL_BREACHED": "equity <= MLL",
            "ANY→EVAL_PASSED": "profit target + 40% consistency expansion",
            "EVAL_DAILY_STOPPED→EVAL_FAST|PROTECTED": "17:00 CT session reset",
        },
    }
    _write_json(OUT_STATE / "states.json", state_doc)
    _write_json(
        OUT_RISK / "sizing.json",
        {
            "fast_qty": FAST_QTY,
            "safe_qty": SAFE_QTY,
            "max_qty": 2,
            "reject_qty": 3,
            "unit_risk_usd": UNIT_RISK_USD,
            "daily_stop_frac": DAILY_STOP_FRAC,
            "formula": "min(strategy_qty_cap, prop_contract_cap, drawdown_based_qty_cap, daily_governor_qty_cap, state_based_qty_cap)",
            "not_percent_of_50k": True,
        },
    )
    _write_json(
        OUT_DEG / "monitor.json",
        {
            "min_sample": 20,
            "rolling_window": 30,
            "hard": ["E[R]<0", "WR-12pp", "streak 5", "WR collapse 18pp"],
            "hysteresis": "FAST does not resume during the evaluation",
        },
    )
    _write_json(OUT_POLICY / "kill_switches.json", KILL_SWITCHES)
    _write_json(
        OUT_POLICY / "governor.json",
        {
            "equation": "daily_stop_threshold = 0.35 * remaining_dd_at_session_open; trigger if session_open_equity - marked_equity >= threshold",
            "includes_unrealized": True,
            "flatten_on_trigger": False,
            "session_reset": "17:00 America/Chicago",
        },
    )

    proc = subprocess.run(
        [sys.executable, "-m", "unittest", "tests_phase52", "-v"],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
    )
    frozen_after = assert_frozen()
    prior_after = [_fp(p) for p in PRIOR_REPORTS]
    if PHASE49B_VAL.exists():
        prior_after.append(_fp(PHASE49B_VAL))
    prior_ok = [a["sha256"] for a in prior_before] == [a["sha256"] for a in prior_after]
    pol = load_operating_policy()
    jn = journals()
    rc_items = [r for r in catalog if r.get("status") == "REQUIRES_CONFIRMATION"]

    payload: dict[str, Any] = {
        "phase": 52,
        "frozen_before": frozen_before,
        "frozen_after": frozen_after,
        "prior_before": prior_before,
        "prior_after": prior_after,
        "prior_reports_unchanged": prior_ok,
        "journals": jn,
        "journals_empty": all(x["empty"] for x in jn),
        "execution_default": pol.execution_default,
        "broker_execution": pol.broker_execution,
        "catalog": catalog,
        "requires_confirmation": rc_items,
        "near_rule": near_pick,
        "near_grid": near_rows,
        "variants": variant_rows,
        "selected_variant": selected_variant,
        "selected": selected_variant,
        "selection_reason": selection["reason"],
        "selected_row": selected_row,
        "stress": stress_map,
        "fast_qty": FAST_QTY,
        "safe_qty": SAFE_QTY,
        "policy_nq_hash": POLICY_NQ_HASH,
        "tests": {
            "returncode": proc.returncode,
            "ok": proc.returncode == 0,
            "stdout_tail": (proc.stdout or "")[-4000:],
            "stderr_tail": (proc.stderr or "")[-2000:],
        },
        "quick": quick,
        "policy_path": str(PROP_POLICY_PATH.as_posix()),
        "operating_policy_path": str(POLICY_PATH.as_posix()),
    }
    payload["verdict"] = decide_verdict(payload)
    _write_json(VALIDATION, payload)
    _write_json(
        OUT_POLICY / "executive.json",
        {
            "verdict": payload["verdict"],
            "selected": selected_variant,
            "P(pass)": selected_row.get("P(pass)"),
            "P(breach)": selected_row.get("P(breach)"),
            "median_days": selected_row.get("median_days_to_pass"),
            "P(pass <=14d)": selected_row.get("P(pass <=14d)"),
            "near_rule": near_pick,
            "DRY_RUN": True,
        },
    )
    write_docs(payload)
    patch_registry(payload["verdict"])
    return payload


if __name__ == "__main__":
    quick = "--quick" in sys.argv
    out = run(quick=quick)
    print(json.dumps({"verdict": out["verdict"], "selected": out["selected_variant"], "quick": quick}, indent=2))
    if out["verdict"] == "STOP_PHASE52_FREEZE_INTEGRITY_FAILURE":
        sys.exit(2)
    if not out["tests"]["ok"]:
        sys.exit(1)
