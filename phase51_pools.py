"""Phase 51 — consume Phase 49 eval + Phase 50 funded engines as empirical pools.

Does not overwrite reports/phase49_* or reports/phase50_*.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np

from phase49_prop_sim import batch_eval_fixed, trades_to_days
from phase49_trade_audit import ES_SRC, NQ_SRC, load_or_reconstruct_gc, load_phase46_csv
from phase50_funded_engine import FundedPolicy, simulate_funded
from prop_rules_v1 import REQUIRES_CONFIRMATION, load_profile

ROOT = Path(__file__).resolve().parent
EVAL_MATRIX = ROOT / "reports" / "phase49_eval_simulation" / "eval_matrix.json"
STITCHED = ROOT / "reports" / "phase50_dynamic_risk" / "best_stitched.json"

# Exact Phase 49 recommended FIXED cells (research only, not production).
EVAL_CELLS = {
    ("NQ", "MFFU_RAPID_EOD_50K"): {"dd_frac": 0.10, "source": "phase49_eval_matrix"},
    ("NQ", "FUNDEDNEXT_FLEX_50K"): {"dd_frac": 0.175, "source": "phase49_eval_matrix"},
    ("ES", "MFFU_RAPID_EOD_50K"): {"dd_frac": 0.05, "source": "phase49_eval_matrix"},
    ("ES", "FUNDEDNEXT_FLEX_50K"): {"dd_frac": 0.075, "source": "phase49_eval_matrix"},
}

MFFU = "MFFU_RAPID_EOD_50K"
FN = "FUNDEDNEXT_FLEX_50K"
MAX_PAYOUTS = 80


def load_eval_matrix_row(book: str, profile: str) -> dict[str, Any]:
    rows = json.loads(EVAL_MATRIX.read_text(encoding="utf-8"))
    cell = EVAL_CELLS[(book, profile)]
    for r in rows:
        if (
            r.get("book") == book
            and r.get("profile") == profile
            and r.get("stage") == "EVALUATION"
            and r.get("policy") == "FIXED"
            and abs(float(r.get("dd_frac") or 0) - float(cell["dd_frac"])) < 1e-12
        ):
            return dict(r)
    raise KeyError(f"phase49_cell_missing:{book}:{profile}:{cell['dd_frac']}")


def mffu_eval_price_status() -> dict[str, Any]:
    st = load_profile(MFFU).stage("EVALUATION").raw
    raw = st.get("purchase_price", REQUIRES_CONFIRMATION)
    if "first_5_purchase_price" not in st and raw in (None, REQUIRES_CONFIRMATION, "REQUIRES_CONFIRMATION"):
        return {
            "status": REQUIRES_CONFIRMATION,
            "confirmed_usd": None,
            "hypothetical_grid_usd": (60.0, 80.0, 100.0, 125.0, 150.0),
            "note": "MFFU Rapid EOD 50K purchase/promo price is not a confirmed PROP_RULES_V1 field. Hypothetical cost scenarios only.",
        }
    return {"status": "CONFIRMED", "confirmed_usd": float(raw), "hypothetical_grid_usd": ()}


def fn_eval_prices() -> dict[str, Any]:
    st = load_profile(FN).stage("EVALUATION").raw
    return {
        "status": "CONFIRMED",
        "first_5_purchase_price": float(st["first_5_purchase_price"]),
        "purchase_6_plus_price": float(st["purchase_6_plus_price"]),
        "reset_fee": float(st["reset_fee"]),
        "listed_standard_price": float(st.get("listed_standard_price") or 133.99),
        "note": "Retries are modeled as new purchases, not resets. Reset exists but is not auto-applied.",
    }


def funded_caps() -> dict[str, Any]:
    mffu = load_profile(MFFU).stage("FUNDED").raw.get("max_funded_accounts")
    fn = load_profile(FN).stage("FUNDED").raw.get("max_funded_accounts")
    return {
        "MFFU_RAPID_EOD_50K": {"value": int(mffu), "status": "CONFIRMED"} if mffu not in (None, REQUIRES_CONFIRMATION) else {"value": None, "status": REQUIRES_CONFIRMATION},
        "FUNDEDNEXT_FLEX_50K": {"value": int(fn), "status": "CONFIRMED"} if fn not in (None, REQUIRES_CONFIRMATION) else {"value": None, "status": REQUIRES_CONFIRMATION, "hypothetical_caps": (1, 3, 5)},
        "copy_trading": {
            "MFFU": load_profile(MFFU).raw.get("general_compliance", {}).get("copy_trading"),
            "FUNDEDNEXT": load_profile(FN).raw.get("general_compliance", {}).get("copy_trading"),
            "note": "Copy trading is REQUIRES_CONFIRMATION. Accounts are independently executed.",
        },
    }


def policy_from_stitched(book: str, profile: str) -> FundedPolicy:
    blob = json.loads(STITCHED.read_text(encoding="utf-8"))
    pol = blob[f"{book}->{profile}"]["policy"]
    fields = set(FundedPolicy.__dataclass_fields__)
    kw = {k: v for k, v in pol.items() if k in fields}
    return FundedPolicy(**kw)


def _load_days() -> dict[str, list]:
    nq = load_phase46_csv(NQ_SRC, strategy="NQ_DVP_FROZEN", instrument="NQ", cost_note="phase46")
    es = load_phase46_csv(ES_SRC, strategy="ES_DVP_LOCKED", instrument="ES", cost_note="phase46")
    gc, _meta = load_or_reconstruct_gc()
    return {
        "GC": trades_to_days(gc, "GC"),
        "NQ": trades_to_days(nq, "NQ"),
        "ES": trades_to_days(es, "ES"),
    }


def build_eval_pool(days, book: str, profile: str, n: int, rng: np.random.Generator) -> dict[str, Any]:
    meta = load_eval_matrix_row(book, profile)
    dd = float(meta["dd_frac"])
    res = batch_eval_fixed(days, book=book, profile_id=profile, dd_frac=dd, n_paths=int(n), rng=rng)
    passed = np.array([r["terminal"] == "PASS" for r in res], dtype=bool)
    dur = np.array([int(r["days"]) for r in res], dtype=np.int32)
    return {
        "book": book,
        "profile": profile,
        "dd_frac": dd,
        "n": int(n),
        "phase49_P(pass)": float(meta["P(pass)"]),
        "phase49_median_days_to_pass": meta.get("median_days_to_pass"),
        "phase49_p75_days_to_pass": meta.get("p75_days_to_pass"),
        "phase49_p95_days_to_pass": meta.get("p95_days_to_pass"),
        "empirical_P(pass)": float(np.mean(passed)),
        "passed": passed,
        "days": dur,
    }


def build_funded_pool(days, book: str, profile: str, n: int, rng: np.random.Generator) -> dict[str, Any]:
    pol = policy_from_stitched(book, profile)
    out = simulate_funded(
        days, book=book, profile_id=profile, policy=pol, n_paths=int(n), rng=rng, record_payout_events=True
    )
    events = out["payout_events"] or [[] for _ in range(int(n))]
    po_d = np.full((n, MAX_PAYOUTS), -1, dtype=np.int32)
    po_a = np.zeros((n, MAX_PAYOUTS), dtype=np.float64)
    n_po = np.zeros(n, dtype=np.int32)
    for i, ev in enumerate(events):
        k = min(len(ev), MAX_PAYOUTS)
        n_po[i] = k
        for j in range(k):
            po_d[i, j] = int(ev[j][0])
            po_a[i, j] = float(ev[j][1])
    return {
        "book": book,
        "profile": profile,
        "policy_name": pol.name,
        "payout_mode": pol.payout_mode,
        "reserve_usd": float(out["reserve_usd"]),
        "n": int(n),
        "po_day": po_d,
        "po_amt": po_a,
        "n_po": n_po,
        "breach_day": np.array(out["breach_day"], dtype=np.int32),
        "trader": np.array(out["trader"], dtype=np.float64),
        "phase50_P(first payout)": float(out["summary"]["P(first payout)"]),
        "phase50_P(5 payouts)": float(out["summary"]["P(5 payouts)"]),
        "phase50_P(10 payouts)": float(out["summary"]["P(10 payouts)"]),
        "phase50_P(survive_1y)": float(out["summary"].get("P(survive_1y)") or 0),
        "phase50_expected_payout": float(out["summary"]["expected_cumulative_trader_payout"]),
    }


def build_all_pools(*, n_eval: int, n_funded: int, books: tuple[str, ...] = ("NQ", "ES")) -> dict[str, Any]:
    day_map = _load_days()
    eval_pools, funded_pools = {}, {}
    for book in books:
        for profile in (MFFU, FN):
            ev_rng = np.random.default_rng(_stable(49, book, profile))
            fu_rng = np.random.default_rng(_stable(50, book, profile))
            key = f"{book}->{profile}"
            eval_pools[key] = build_eval_pool(day_map[book], book, profile, n_eval, ev_rng)
            funded_pools[key] = build_funded_pool(day_map[book], book, profile, n_funded, fu_rng)
    return {
        "eval": eval_pools,
        "funded": funded_pools,
        "mffu_price": mffu_eval_price_status(),
        "fn_price": fn_eval_prices(),
        "caps": funded_caps(),
        "eval_cells": {f"{b}->{p}": v for (b, p), v in EVAL_CELLS.items() if b in books},
    }


def _stable(*parts) -> int:
    acc = 51
    s = "|".join(str(p) for p in parts)
    for i, c in enumerate(s):
        acc = (acc + (i + 1) * ord(c) * 131) % 2_147_483_647
    return int(acc)
