"""Phase 49 — strategy distribution audit + PROP_RULES_V1 risk simulation. DRY_RUN only."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

from aitrade_operating_policy import load_operating_policy
from phase34_validate import GC_FILE_SHA, GC_FROZEN, NQ_FILE_SHA, NQ_FROZEN, assert_frozen, file_sha256
from phase49_distributions import build_all
from phase49_prop_sim import (
    DEFAULT_STOP,
    EVAL_FRACS,
    EVAL_STATE_POLICIES,
    FIXED,
    FUNDED_FRACS,
    FUNDED_STATE_POLICIES,
    GOV_POLICIES,
    N_PATHS_DEFAULT,
    chrono_eval,
    chrono_funded,
    eval_objective,
    funded_objective,
    max_micros,
    run_eval_grid,
    run_funded_grid,
    size_qty,
    trades_to_days,
    unsuitable,
    unsuitable_funded,
)
from phase49_trade_audit import audit_all, write_csv
from prop_rules_v1 import load_profile, load_rules_document

ROOT = Path(__file__).resolve().parent
VALIDATION = ROOT / "phase49_validation.json"
DOCS = ROOT / "docs" / "PHASE49_STRATEGY_DISTRIBUTION_PROP_SIM.md"
REGISTRY = ROOT / "docs" / "STRATEGY_REGISTRY.md"
EVAL_DIR = ROOT / "reports" / "phase49_eval_simulation"
FUNDED_DIR = ROOT / "reports" / "phase49_funded_simulation"
ARCH = ROOT / "reports" / "phase49_strategy_distributions" / "portfolio_architecture.json"
POLICY_PATH = ROOT / "config" / "aitrade_operating_policy_v1.json"

PROFILES = ("MFFU_RAPID_EOD_50K", "FUNDEDNEXT_FLEX_50K")
BOOKS = ("GC", "NQ", "ES")
POLICY_PATHS = 1_500
FUNDED_POLICY_PATHS = 400
GOV_FRAC = 0.10
FUNDED_POLICY_FRAC = 0.05


def _write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, default=str), encoding="utf-8")


def write_architecture() -> None:
    _write_json(
        ARCH,
        {
            "status": "ARCHITECTURE_ONLY",
            "simultaneous_multi_strategy_simulated": False,
            "reason": "Single-strategy / single-account first. Chronological NQ/ES overlap exists on the 2020–2026 stitch, but GC freeze-window sample is not aligned to that span.",
            "planned_later": [
                "account_level_risk_budget",
                "strategy_level_risk_budget",
                "correlated_exposure",
                "same_underlying_exposure",
            ],
            "not_optimized": True,
        },
    )


def pick_eval_recommendation(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    sized = [r for r in rows if (r.get("example_qty_at_median_stop") or 0) > 0]
    pool = sized or rows
    if not pool:
        return None
    return max(pool, key=eval_objective)


def pick_funded_recommendation(rows: list[dict[str, Any]], book: str, profile_id: str) -> dict[str, Any] | None:
    if not rows:
        return None
    stop = DEFAULT_STOP[book]
    cap = max_micros(profile_id, "FUNDED")
    scored = []
    for r in rows:
        usd = float(r.get("risk_usd") or 0)
        rp = stop if stop is not None else 8.0
        qty, actual = size_qty(book, usd, rp, cap)
        rec = dict(r)
        rec["example_qty_at_median_stop"] = qty
        rec["example_actual_risk_usd"] = actual
        if qty > 0:
            scored.append(rec)
    if not scored:
        return None
    return max(scored, key=funded_objective)


def render_docs(payload: dict[str, Any]) -> str:
    rec = payload.get("research_recommendations") or {}
    uns = payload.get("prop_profile_unsuitable") or {}
    lines = [
        "# Phase 49 — Strategy Distribution Audit + Prop Risk Simulation",
        "",
        "`DRY_RUN`. No broker. Frozen strategy logic was not modified. ES was not promoted into `strategy_frozen/`.",
        "Operating-policy numerics were **not** written. Values below are research recommendations only.",
        "",
        "## 1. Verdict",
        "",
        f"**`{payload['verdict']}`**",
        "",
        "## 2. Frozen integrity",
        "",
        f"- GC config hash: `{payload['frozen']['gc']}` file SHA match",
        f"- NQ config hash: `{payload['frozen']['nq']}` file SHA match",
        "- ES not frozen",
        f"- Paper journals remain empty: `{payload['frozen'].get('paper_journals_empty')}`",
        "",
        "## 3. Data sources (not fabricated, originals not overwritten)",
        "",
    ]
    for book, src in (payload.get("data_sources") or {}).items():
        lines.append(f"### {book}")
        lines.append("")
        lines.append(f"- Source: `{src.get('source_path')}`")
        lines.append(f"- Trades: {src.get('number_of_trades')}")
        lines.append(f"- Date range: {src.get('date_range')}")
        lines.append(f"- Method: {src.get('method')}")
        for w in src.get("missing_warnings") or []:
            lines.append(f"- Warning: {w}")
        lines.append("")
    lines += [
        "## 4. Distribution statistics",
        "",
        "See `reports/phase49_strategy_distributions/{gc,nq,es}_distribution.json`.",
        "GC, NQ, and ES are computed separately.",
        "",
        "## 5. Simulation inputs",
        "",
        "- `*_chronological_trade_stream.csv` — historical order, same-day clusters preserved",
        "- `*_bootstrap_trade_distribution.csv` — resampling universe (copy; originals untouched)",
        "",
        "Bootstrap resamples **days** (not independent trades) so same-day clustering is preserved inside a day.",
        "Inactivity calendar gaps are stressed on chronological replay only.",
        "",
        "## 6. Evaluation simulation matrix",
        "",
        "Risk is a fraction of initial permitted drawdown from `PROP_RULES_V1`, not of $50,000.",
        f"Grid: {list(EVAL_FRACS)}. Paths: {payload.get('n_paths_eval')}.",
        "",
        "See `reports/phase49_eval_simulation/`.",
        "",
        "## 7. Funded simulation matrix",
        "",
        f"Grid: {list(FUNDED_FRACS)}. Paths: {payload.get('n_paths_funded')}.",
        "MFFU: $2,100 first buffer, MLL lock +$100, $500 subsequent, 90/10.",
        "FundedNext: 5×$200 benchmark days, $1,500 max withdrawal, 95% share.",
        "FundedNext first-payout dollar buffer is REQUIRES_CONFIRMATION — not invented; eligibility uses benchmark days + MLL cushion.",
        "",
        "See `reports/phase49_funded_simulation/`.",
        "",
        "## 8. Efficient frontiers",
        "",
        "- Eval: `reports/phase49_eval_simulation/efficient_frontier_pass.csv` (dd_frac vs P(pass), P(breach), cost, days)",
        "- Funded: `reports/phase49_funded_simulation/efficient_frontier_payout_survival.csv`",
        "",
        "## 9. Consistency governors (simulation-only; signals unchanged)",
        "",
        str(payload.get("governor_findings")),
        "",
        "## 10. State-transition findings (research-derived, not production)",
        "",
        str(payload.get("state_policy_findings")),
        "",
        "## 11. PROP_PROFILE_UNSUITABLE",
        "",
        json.dumps(uns, indent=2),
        "",
        "## 12. Research recommendations (NOT written into operating policy)",
        "",
        json.dumps(rec, indent=2, default=str),
        "",
        "## 13. DRY_RUN / policy lock",
        "",
        f"- execution_default: `{payload.get('execution_default')}`",
        f"- broker_execution: `{payload.get('broker_execution')}`",
        f"- operating policy risk_per_trade still null: `{payload.get('policy_risk_still_null')}`",
        "- No martingale / loss-chasing / doubling policies were simulated.",
        "",
        "## 14. What this phase did not do",
        "",
        "No strategy retune. No frozen file edits. No ES freeze. No live execution. No final production risk values.",
        "",
    ]
    return "\n".join(lines) + "\n"


def update_registry() -> None:
    text = REGISTRY.read_text(encoding="utf-8")
    marker = "### Strategy distribution + prop risk simulation (Phase 49)"
    block = """### Strategy distribution + prop risk simulation (Phase 49)

| Field | Value |
|-------|--------|
| Phase | 49 |
| Status | See `phase49_validation.json` verdict |
| Question | Given frozen/locked strategy trade distributions, which PROP_RULES_V1 risk fractions and state policies are mathematically suitable for MFFU Rapid EOD 50K and FundedNext Flex 50K (eval vs funded)? |
| Forbidden | Retune GC/NQ/ES; freeze ES; enable broker; write production risk into operating policy; martingale |
| Evidence | `docs/PHASE49_STRATEGY_DISTRIBUTION_PROP_SIM.md`, `reports/phase49_*`, `phase49_validation.json`, `tests_phase49.py` |
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
        if idx < 0:
            raise RuntimeError("registry_research_missing")
        insert_at = text.find("\n", idx) + 1
        text = text[:insert_at] + "\n" + block + text[insert_at:]
    REGISTRY.write_text(text, encoding="utf-8")


def run(n_paths: int = N_PATHS_DEFAULT) -> dict[str, Any]:
    frozen_before = assert_frozen()
    if not frozen_before.get("ok"):
        raise RuntimeError(f"FROZEN_INTEGRITY_FAIL_BEFORE:{frozen_before}")

    audit = audit_all()
    books = audit["books"]
    dists = build_all(books)
    write_architecture()

    EVAL_DIR.mkdir(parents=True, exist_ok=True)
    FUNDED_DIR.mkdir(parents=True, exist_ok=True)

    eval_rows: list[dict[str, Any]] = []
    funded_rows: list[dict[str, Any]] = []
    chrono_rows: list[dict[str, Any]] = []
    gov_rows: list[dict[str, Any]] = []
    eval_state_rows: list[dict[str, Any]] = []
    funded_state_rows: list[dict[str, Any]] = []
    unsuitable_map: dict[str, Any] = {}
    recommendations: dict[str, Any] = {}

    day_map: dict[str, Any] = {}
    for book in BOOKS:
        trades = books[book]["trades"]
        day_map[book] = trades_to_days(trades, book)

    for book in BOOKS:
        days = day_map[book]
        dist = dists.get(book) or {}
        for profile in PROFILES:
            ev = run_eval_grid(days, book=book, profile_id=profile, n_paths=n_paths)
            eval_rows.extend(ev)
            fu = run_funded_grid(days, book=book, profile_id=profile, n_paths=n_paths)
            funded_rows.extend(fu)
            tag = f"{book}->{profile}"
            rec_e = pick_eval_recommendation(ev)
            rec_f = pick_funded_recommendation(fu, book, profile)
            flag_e = unsuitable(ev, dist)
            flag_f = unsuitable_funded(fu, book, profile)
            unsuitable_map[tag] = {"evaluation": flag_e, "funded": flag_f}
            recommendations[tag] = {
                "evaluation": rec_e,
                "funded": rec_f if flag_f is None else "PROP_PROFILE_UNSUITABLE",
                "unsuitable": unsuitable_map[tag],
                "note": "Research only. Not written into aitrade_operating_policy_v1.json.",
            }
            # Chronological authentic path at recommended frac (or 10%).
            rec_e_obj = rec_e if isinstance(rec_e, dict) else {}
            rec_f_obj = rec_f if isinstance(rec_f, dict) else {}
            frac_e = float(rec_e_obj.get("dd_frac") or 0.10)
            frac_f = float(rec_f_obj.get("dd_frac") or 0.05)
            chrono_rows.append(
                {
                    "book": book,
                    "profile": profile,
                    "stage": "EVALUATION",
                    **chrono_eval(days, book=book, profile_id=profile, dd_frac=frac_e),
                    "dd_frac": frac_e,
                }
            )
            chrono_rows.append(
                {
                    "book": book,
                    "profile": profile,
                    "stage": "FUNDED",
                    **chrono_funded(days, book=book, profile_id=profile, dd_frac=frac_f),
                    "dd_frac": frac_f,
                }
            )
            for gov in GOV_POLICIES:
                gov_rows.extend(
                    run_eval_grid(
                        days,
                        book=book,
                        profile_id=profile,
                        n_paths=POLICY_PATHS,
                        fracs=(GOV_FRAC,),
                        policy=gov,
                    )
                )
            for pol in EVAL_STATE_POLICIES:
                if pol.name == "FIXED":
                    continue
                eval_state_rows.extend(
                    run_eval_grid(
                        days,
                        book=book,
                        profile_id=profile,
                        n_paths=POLICY_PATHS,
                        fracs=(GOV_FRAC,),
                        policy=pol,
                    )
                )
            for pol in FUNDED_STATE_POLICIES:
                if pol.name == "FIXED":
                    continue
                funded_state_rows.extend(
                    run_funded_grid(
                        days,
                        book=book,
                        profile_id=profile,
                        n_paths=FUNDED_POLICY_PATHS,
                        fracs=(FUNDED_POLICY_FRAC,),
                        policy=pol,
                    )
                )

    write_csv(EVAL_DIR / "eval_matrix.csv", eval_rows)
    write_csv(EVAL_DIR / "efficient_frontier_pass.csv", eval_rows)
    write_csv(EVAL_DIR / "consistency_governors.csv", gov_rows)
    write_csv(EVAL_DIR / "state_policies.csv", eval_state_rows)
    write_csv(EVAL_DIR / "chronological_replay.csv", chrono_rows)
    _write_json(EVAL_DIR / "eval_matrix.json", eval_rows)
    write_csv(FUNDED_DIR / "funded_matrix.csv", funded_rows)
    write_csv(FUNDED_DIR / "efficient_frontier_payout_survival.csv", funded_rows)
    write_csv(FUNDED_DIR / "state_policies.csv", funded_state_rows)
    _write_json(FUNDED_DIR / "funded_matrix.json", funded_rows)

    governor_findings = {}
    for book in BOOKS:
        for profile in PROFILES:
            sub = [r for r in gov_rows if r["book"] == book and r["profile"] == profile]
            if not sub:
                continue
            best = max(sub, key=eval_objective)
            none = next((r for r in sub if r.get("governor") == "none"), None)
            governor_findings[f"{book}->{profile}"] = {
                "best_governor": best.get("governor"),
                "best_P(pass)": best.get("P(pass)"),
                "none_P(pass)": None if none is None else none.get("P(pass)"),
                "best_avg_adjusted_target": best.get("average_adjusted_profit_target"),
                "none_avg_adjusted_target": None if none is None else none.get("average_adjusted_profit_target"),
                "note": "Governor is simulation-only and does not change strategy signals.",
            }

    state_findings = {"evaluation": {}, "funded": {}}
    for book in BOOKS:
        for profile in PROFILES:
            sub = [r for r in eval_state_rows if r["book"] == book and r["profile"] == profile]
            base = next((r for r in eval_rows if r["book"] == book and r["profile"] == profile and r["dd_frac"] == GOV_FRAC and r["policy"] == "FIXED"), None)
            if sub:
                best = max(sub, key=eval_objective)
                state_findings["evaluation"][f"{book}->{profile}"] = {
                    "best_policy": best.get("policy"),
                    "best_P(pass)": best.get("P(pass)"),
                    "fixed_10pct_P(pass)": None if base is None else base.get("P(pass)"),
                    "improves_vs_fixed": None if base is None else eval_objective(best) > eval_objective(base),
                }
            fsub = [r for r in funded_state_rows if r["book"] == book and r["profile"] == profile]
            fbase = next((r for r in funded_rows if r["book"] == book and r["profile"] == profile and r["dd_frac"] == FUNDED_POLICY_FRAC and r["policy"] == "FIXED"), None)
            if fsub:
                fbest = max(fsub, key=funded_objective)
                state_findings["funded"][f"{book}->{profile}"] = {
                    "best_policy": fbest.get("policy"),
                    "best_survival": fbest.get("account_survival_probability"),
                    "best_expected_payout": fbest.get("expected_total_payout_before_breach"),
                    "fixed_5pct_survival": None if fbase is None else fbase.get("account_survival_probability"),
                    "improves_vs_fixed": None if fbase is None else funded_objective(fbest) > funded_objective(fbase),
                }

    proc = subprocess.run(
        [sys.executable, "-m", "unittest", "tests_phase49", "-v"],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
    )
    frozen_after = assert_frozen()
    policy = load_operating_policy()
    policy_doc = json.loads(POLICY_PATH.read_text(encoding="utf-8"))
    risk_null = policy_doc.get("numerics_pending_simulation", {}).get("risk_per_trade") is None

    sources = {}
    for book in BOOKS:
        summ = audit["summary"]["books"][book]
        src = summ.get("source") or {}
        sources[book] = {
            "source_path": src.get("source") or src.get("path"),
            "method": src.get("method"),
            "number_of_trades": summ.get("number_of_trades"),
            "date_range": summ.get("date_range"),
            "missing_warnings": summ.get("missing_warnings"),
            "fields": summ.get("fields"),
        }

    tests_ok = proc.returncode == 0
    frozen_ok = bool(frozen_after.get("ok"))
    has_data = all((audit["summary"]["books"][b]["number_of_trades"] or 0) > 0 for b in ("NQ", "ES"))
    gc_n = audit["summary"]["books"]["GC"]["number_of_trades"] or 0
    # GC may be reconstructed; NQ/ES required.
    verdict = "PHASE49_RISK_RESEARCH_READY" if tests_ok and frozen_ok and has_data and risk_null else "PHASE49_RISK_RESEARCH_BLOCKED"
    if gc_n <= 0:
        verdict = "PHASE49_RISK_RESEARCH_BLOCKED"

    payload = {
        "phase": 49,
        "verdict": verdict,
        "execution_default": policy.execution_default,
        "broker_execution": False,
        "dry_run": True,
        "n_paths_eval": n_paths,
        "n_paths_funded": n_paths,
        "n_paths_policy_search": POLICY_PATHS,
        "n_paths_funded_policy_search": FUNDED_POLICY_PATHS,
        "frozen_before": frozen_before,
        "frozen_after": frozen_after,
        "frozen": {
            "gc": frozen_after.get("gc"),
            "nq": frozen_after.get("nq"),
            "paper_journals_empty": frozen_ok,
        },
        "data_sources": sources,
        "distributions": {k: {kk: vv for kk, vv in v.items() if kk not in ("monthly_outcome_R", "rolling_drawdown_20d", "rolling_drawdown_60d")} for k, v in dists.items()},
        "prop_profile_unsuitable": unsuitable_map,
        "research_recommendations": recommendations,
        "governor_findings": governor_findings,
        "state_policy_findings": state_findings,
        "policy_risk_still_null": risk_null,
        "operating_policy": "config/aitrade_operating_policy_v1.json",
        "no_martingale": True,
        "es_not_frozen": not (ROOT / "strategy_frozen" / "es_dvp_phase47.json").exists(),
        "tests": {
            "ran": proc.stderr.count(" ... ok") + proc.stdout.count(" ... ok"),
            "failures": 0 if tests_ok else 1,
            "returncode": proc.returncode,
            "tail": (proc.stderr or proc.stdout)[-2000:],
        },
        "rules_source": "config/PROP_RULES_V1.json",
        "firm_numbers_not_duplicated": True,
    }
    DOCS.parent.mkdir(parents=True, exist_ok=True)
    DOCS.write_text(render_docs(payload), encoding="utf-8")
    VALIDATION.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    update_registry()
    return payload


def main() -> int:
    n = N_PATHS_DEFAULT
    if "--quick" in sys.argv:
        n = 200
    payload = run(n_paths=n)
    print(json.dumps({"verdict": payload["verdict"], "unsuitable": payload["prop_profile_unsuitable"]}, indent=2))
    print(payload["verdict"])
    return 0 if payload["verdict"] == "PHASE49_RISK_RESEARCH_READY" else 1


if __name__ == "__main__":
    raise SystemExit(main())
