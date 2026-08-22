# Phase 49 — Strategy Distribution Audit + Prop Risk Simulation

`DRY_RUN`. No broker. Frozen strategy logic was not modified. ES was not promoted into `strategy_frozen/`.
Operating-policy numerics were **not** written. Values below are research recommendations only.

## 1. Verdict

**`PHASE49_RISK_RESEARCH_READY`**

## 2. Frozen integrity

- GC config hash: `0695d7a881b6247033426bceb077b6895b96405b826c21f32a109a02f44b2d43` file SHA match
- NQ config hash: `935e3a616351b09dbfa8d2d0e2b5d6850be803fb385ec5e5f8e3593856a1212a` file SHA match
- ES not frozen
- Paper journals remain empty: `True`

## 3. Data sources (not fabricated, originals not overwritten)

### GC

- Source: `data/databento/GC/stitched/databento_GC_stitched_5m.jsonl`
- Trades: 430
- Date range: ['2025-08-04', '2026-08-14']
- Method: run_frozen_v2_on_bars persist=False + 2R path resolve on 5m
- Warning: gross_pnl USD at account size is UNAVAILABLE — R and 1-micro point dollars only
- Warning: T1 news flags not applied to simulation paths (historical trades are not news-labeled except phase46 news_blackout skips)
- Warning: GC sample is the frozen 5m stitch window (~2025-08 to 2026-08), not the 2020–2026 v0 continuous used for NQ/ES
- Warning: GC realized R uses frozen 2R target vs stop vs session flatten on 5m; not the paper journal

### NQ

- Source: `C:/Users/tanam/OneDrive/Desktop/aitrade/reports/phase46_nq_dvp_frozen_proxy.csv`
- Trades: 5000
- Date range: ['2020-01-02', '2025-10-21']
- Method: phase46 frozen-rule DVP replay CSV (not paper journal)
- Warning: gross_pnl USD at account size is UNAVAILABLE — R and 1-micro point dollars only
- Warning: T1 news flags not applied to simulation paths (historical trades are not news-labeled except phase46 news_blackout skips)
- Warning: NQ: MAE/MFE columns empty in phase46 CSV
- Warning: NQ: hold_sec empty in phase46 CSV

### ES

- Source: `C:/Users/tanam/OneDrive/Desktop/aitrade/reports/phase46_es_dvp.csv`
- Trades: 5574
- Date range: ['2020-01-02', '2026-08-14']
- Method: phase46 ES DVP port CSV; locked candidate Phase 47; not frozen
- Warning: gross_pnl USD at account size is UNAVAILABLE — R and 1-micro point dollars only
- Warning: T1 news flags not applied to simulation paths (historical trades are not news-labeled except phase46 news_blackout skips)
- Warning: ES: MAE/MFE columns empty in phase46 CSV
- Warning: ES: hold_sec empty in phase46 CSV
- Warning: ES is LOCKED_FORWARD_VALIDATION_CANDIDATE — not frozen, not in strategy_frozen/

## 4. Distribution statistics

See `reports/phase49_strategy_distributions/{gc,nq,es}_distribution.json`.
GC, NQ, and ES are computed separately.

## 5. Simulation inputs

- `*_chronological_trade_stream.csv` — historical order, same-day clusters preserved
- `*_bootstrap_trade_distribution.csv` — resampling universe (copy; originals untouched)

Bootstrap resamples **days** (not independent trades) so same-day clustering is preserved inside a day.
Inactivity calendar gaps are stressed on chronological replay only.

## 6. Evaluation simulation matrix

Risk is a fraction of initial permitted drawdown from `PROP_RULES_V1`, not of $50,000.
Grid: [0.05, 0.075, 0.1, 0.125, 0.15, 0.175, 0.2]. Paths: 10000.

See `reports/phase49_eval_simulation/`.

## 7. Funded simulation matrix

Grid: [0.025, 0.05, 0.075, 0.1, 0.125]. Paths: 10000.
MFFU: $2,100 first buffer, MLL lock +$100, $500 subsequent, 90/10.
FundedNext: 5×$200 benchmark days, $1,500 max withdrawal, 95% share.
FundedNext first-payout dollar buffer is REQUIRES_CONFIRMATION — not invented; eligibility uses benchmark days + MLL cushion.

See `reports/phase49_funded_simulation/`.

## 8. Efficient frontiers

- Eval: `reports/phase49_eval_simulation/efficient_frontier_pass.csv` (dd_frac vs P(pass), P(breach), cost, days)
- Funded: `reports/phase49_funded_simulation/efficient_frontier_payout_survival.csv`

## 9. Consistency governors (simulation-only; signals unchanged)

{'GC->MFFU_RAPID_EOD_50K': {'best_governor': 'reduced', 'best_P(pass)': 0.25266666666666665, 'none_P(pass)': 0.23866666666666667, 'best_avg_adjusted_target': 3000.0, 'none_avg_adjusted_target': 3159.934039401797, 'note': 'Governor is simulation-only and does not change strategy signals.'}, 'GC->FUNDEDNEXT_FLEX_50K': {'best_governor': 'reduced', 'best_P(pass)': 0.17266666666666666, 'none_P(pass)': 0.15666666666666668, 'best_avg_adjusted_target': 2500.0, 'none_avg_adjusted_target': 2500.0, 'note': 'Governor is simulation-only and does not change strategy signals.'}, 'NQ->MFFU_RAPID_EOD_50K': {'best_governor': 'hard', 'best_P(pass)': 0.734, 'none_P(pass)': 0.7253333333333334, 'best_avg_adjusted_target': 3000.0, 'none_avg_adjusted_target': 3000.0, 'note': 'Governor is simulation-only and does not change strategy signals.'}, 'NQ->FUNDEDNEXT_FLEX_50K': {'best_governor': 'none', 'best_P(pass)': 0.0, 'none_P(pass)': 0.0, 'best_avg_adjusted_target': 2500.0, 'none_avg_adjusted_target': 2500.0, 'note': 'Governor is simulation-only and does not change strategy signals.'}, 'ES->MFFU_RAPID_EOD_50K': {'best_governor': 'hard', 'best_P(pass)': 0.452, 'none_P(pass)': 0.43733333333333335, 'best_avg_adjusted_target': 3000.0, 'none_avg_adjusted_target': 3000.0, 'note': 'Governor is simulation-only and does not change strategy signals.'}, 'ES->FUNDEDNEXT_FLEX_50K': {'best_governor': 'hard', 'best_P(pass)': 0.506, 'none_P(pass)': 0.49933333333333335, 'best_avg_adjusted_target': 2500.0, 'none_avg_adjusted_target': 2500.0, 'note': 'Governor is simulation-only and does not change strategy signals.'}}

## 10. State-transition findings (research-derived, not production)

{'evaluation': {'GC->MFFU_RAPID_EOD_50K': {'best_policy': 'EVAL_TARGET_APPROACH_2R', 'best_P(pass)': 0.23333333333333334, 'fixed_10pct_P(pass)': 0.2557, 'improves_vs_fixed': False}, 'GC->FUNDEDNEXT_FLEX_50K': {'best_policy': 'EVAL_TARGET_APPROACH_2R', 'best_P(pass)': 0.18266666666666667, 'fixed_10pct_P(pass)': 0.1789, 'improves_vs_fixed': True}, 'NQ->MFFU_RAPID_EOD_50K': {'best_policy': 'EVAL_DEFENSIVE_DD25', 'best_P(pass)': 0.5366666666666666, 'fixed_10pct_P(pass)': 0.7235, 'improves_vs_fixed': False}, 'NQ->FUNDEDNEXT_FLEX_50K': {'best_policy': 'EVAL_DEFENSIVE_DD40', 'best_P(pass)': 0.0, 'fixed_10pct_P(pass)': 0.0, 'improves_vs_fixed': False}, 'ES->MFFU_RAPID_EOD_50K': {'best_policy': 'EVAL_LOSS_STREAK_2', 'best_P(pass)': 0.618, 'fixed_10pct_P(pass)': 0.455, 'improves_vs_fixed': True}, 'ES->FUNDEDNEXT_FLEX_50K': {'best_policy': 'EVAL_DEFENSIVE_DD25', 'best_P(pass)': 0.39066666666666666, 'fixed_10pct_P(pass)': 0.5046, 'improves_vs_fixed': True}}, 'funded': {'GC->MFFU_RAPID_EOD_50K': {'best_policy': 'FUNDED_BUFFER_BUILD', 'best_survival': 0.7375, 'best_expected_payout': 373.0649449825454, 'fixed_5pct_survival': 0.0053, 'improves_vs_fixed': True}, 'GC->FUNDEDNEXT_FLEX_50K': {'best_policy': 'FUNDED_BUFFER_BUILD', 'best_survival': 0.92, 'best_expected_payout': 0.0, 'fixed_5pct_survival': 0.0004, 'improves_vs_fixed': True}, 'NQ->MFFU_RAPID_EOD_50K': {'best_policy': 'FUNDED_BUFFER_BUILD', 'best_survival': 1.0, 'best_expected_payout': 0.0, 'fixed_5pct_survival': 1.0, 'improves_vs_fixed': False}, 'NQ->FUNDEDNEXT_FLEX_50K': {'best_policy': 'FUNDED_BUFFER_BUILD', 'best_survival': 1.0, 'best_expected_payout': 0.0, 'fixed_5pct_survival': 1.0, 'improves_vs_fixed': False}, 'ES->MFFU_RAPID_EOD_50K': {'best_policy': 'FUNDED_DEFENSIVE_DD40', 'best_survival': 1.0, 'best_expected_payout': 595.8187874999968, 'fixed_5pct_survival': 0.005, 'improves_vs_fixed': True}, 'ES->FUNDEDNEXT_FLEX_50K': {'best_policy': 'FUNDED_BUFFER_BUILD', 'best_survival': 1.0, 'best_expected_payout': 0.0, 'fixed_5pct_survival': 1.0, 'improves_vs_fixed': False}}}

## 11. PROP_PROFILE_UNSUITABLE

{
  "GC->MFFU_RAPID_EOD_50K": {
    "evaluation": null,
    "funded": "PROP_PROFILE_UNSUITABLE"
  },
  "GC->FUNDEDNEXT_FLEX_50K": {
    "evaluation": null,
    "funded": "PROP_PROFILE_UNSUITABLE"
  },
  "NQ->MFFU_RAPID_EOD_50K": {
    "evaluation": null,
    "funded": "PROP_PROFILE_UNSUITABLE"
  },
  "NQ->FUNDEDNEXT_FLEX_50K": {
    "evaluation": null,
    "funded": "PROP_PROFILE_UNSUITABLE"
  },
  "ES->MFFU_RAPID_EOD_50K": {
    "evaluation": null,
    "funded": "PROP_PROFILE_UNSUITABLE"
  },
  "ES->FUNDEDNEXT_FLEX_50K": {
    "evaluation": null,
    "funded": "PROP_PROFILE_UNSUITABLE"
  }
}

## 12. Research recommendations (NOT written into operating policy)

{
  "GC->MFFU_RAPID_EOD_50K": {
    "evaluation": {
      "book": "GC",
      "profile": "MFFU_RAPID_EOD_50K",
      "stage": "EVALUATION",
      "dd_frac": 0.125,
      "risk_usd": 250.0,
      "policy": "FIXED",
      "governor": "none",
      "mode": "bootstrap",
      "example_qty_at_median_stop": 4,
      "example_actual_risk_usd": 219.19958312966628,
      "median_stop_points": 5.479989578241657,
      "n_paths": 10000,
      "P(pass)": 0.2692,
      "P(breach)": 0.7308,
      "P(timeout)": 0.0,
      "median_days_to_pass": 32.0,
      "p75_days_to_pass": 47.0,
      "p95_days_to_pass": 75.0,
      "median_trades_to_pass": 66.0,
      "median_max_drawdown_before_pass": 1201.1729130868066,
      "probability_consistency_rule_delays_pass": 0.2722,
      "average_adjusted_profit_target": 3421.7471172054725,
      "expected_number_of_attempts": 3.7147102526002973,
      "expected_evaluation_cost": null,
      "expected_evaluation_cost_note": "MFFU evaluation purchase price is REQUIRES_CONFIRMATION \u2014 attempts reported, dollar cost not invented",
      "mean_sized_zero": 0.0
    },
    "funded": "PROP_PROFILE_UNSUITABLE",
    "unsuitable": {
      "evaluation": null,
      "funded": "PROP_PROFILE_UNSUITABLE"
    },
    "note": "Research only. Not written into aitrade_operating_policy_v1.json.",
    "funded_research_note": "Executable micro size on the funded grid produces high first-payout then near-certain ruin over 504 trading days. Not a production funded risk. Survive-by-not-trading rows (qty=0) were excluded."
  },
  "GC->FUNDEDNEXT_FLEX_50K": {
    "evaluation": {
      "book": "GC",
      "profile": "FUNDEDNEXT_FLEX_50K",
      "stage": "EVALUATION",
      "dd_frac": 0.175,
      "risk_usd": 262.5,
      "policy": "FIXED",
      "governor": "none",
      "mode": "bootstrap",
      "example_qty_at_median_stop": 4,
      "example_actual_risk_usd": 219.19958312966628,
      "median_stop_points": 5.479989578241657,
      "n_paths": 10000,
      "P(pass)": 0.2619,
      "P(breach)": 0.7381,
      "P(timeout)": 0.0,
      "median_days_to_pass": 21.0,
      "p75_days_to_pass": 30.0,
      "p95_days_to_pass": 54.0,
      "median_trades_to_pass": 43.0,
      "median_max_drawdown_before_pass": 971.2061092229633,
      "probability_consistency_rule_delays_pass": 0.1301,
      "average_adjusted_profit_target": 2673.358173916943,
      "expected_number_of_attempts": 3.818251240931653,
      "expected_evaluation_cost": 267.2394043528064,
      "expected_evaluation_cost_note": "FundedNext first_5=69.99 then 79.99; geometric attempts until first pass",
      "mean_sized_zero": 0.0
    },
    "funded": "PROP_PROFILE_UNSUITABLE",
    "unsuitable": {
      "evaluation": null,
      "funded": "PROP_PROFILE_UNSUITABLE"
    },
    "note": "Research only. Not written into aitrade_operating_policy_v1.json.",
    "funded_research_note": "Executable micro size on the funded grid produces high first-payout then near-certain ruin over 504 trading days. Not a production funded risk. Survive-by-not-trading rows (qty=0) were excluded."
  },
  "NQ->MFFU_RAPID_EOD_50K": {
    "evaluation": {
      "book": "NQ",
      "profile": "MFFU_RAPID_EOD_50K",
      "stage": "EVALUATION",
      "dd_frac": 0.1,
      "risk_usd": 200.0,
      "policy": "FIXED",
      "governor": "none",
      "mode": "bootstrap",
      "example_qty_at_median_stop": 1,
      "example_actual_risk_usd": 160.0,
      "median_stop_points": 80.0,
      "n_paths": 10000,
      "P(pass)": 0.7235,
      "P(breach)": 0.2765,
      "P(timeout)": 0.0,
      "median_days_to_pass": 55.0,
      "p75_days_to_pass": 78.0,
      "p95_days_to_pass": 122.0,
      "median_trades_to_pass": 185.0,
      "median_max_drawdown_before_pass": 1108.8000000000002,
      "probability_consistency_rule_delays_pass": 0.0,
      "average_adjusted_profit_target": 3000.0,
      "expected_number_of_attempts": 1.38217000691085,
      "expected_evaluation_cost": null,
      "expected_evaluation_cost_note": "MFFU evaluation purchase price is REQUIRES_CONFIRMATION \u2014 attempts reported, dollar cost not invented",
      "mean_sized_zero": 0.0
    },
    "funded": "PROP_PROFILE_UNSUITABLE",
    "unsuitable": {
      "evaluation": null,
      "funded": "PROP_PROFILE_UNSUITABLE"
    },
    "note": "Research only. Not written into aitrade_operating_policy_v1.json.",
    "funded_research_note": "Executable micro size on the funded grid produces high first-payout then near-certain ruin over 504 trading days. Not a production funded risk. Survive-by-not-trading rows (qty=0) were excluded."
  },
  "NQ->FUNDEDNEXT_FLEX_50K": {
    "evaluation": {
      "book": "NQ",
      "profile": "FUNDEDNEXT_FLEX_50K",
      "stage": "EVALUATION",
      "dd_frac": 0.175,
      "risk_usd": 262.5,
      "policy": "FIXED",
      "governor": "none",
      "mode": "bootstrap",
      "example_qty_at_median_stop": 1,
      "example_actual_risk_usd": 160.0,
      "median_stop_points": 80.0,
      "n_paths": 10000,
      "P(pass)": 0.6537,
      "P(breach)": 0.3463,
      "P(timeout)": 0.0,
      "median_days_to_pass": 40.0,
      "p75_days_to_pass": 57.0,
      "p95_days_to_pass": 90.0,
      "median_trades_to_pass": 135.0,
      "median_max_drawdown_before_pass": 917.7999999999956,
      "probability_consistency_rule_delays_pass": 0.0,
      "average_adjusted_profit_target": 2500.0,
      "expected_number_of_attempts": 1.529753709652746,
      "expected_evaluation_cost": 107.06746213859569,
      "expected_evaluation_cost_note": "FundedNext first_5=69.99 then 79.99; geometric attempts until first pass",
      "mean_sized_zero": 0.0
    },
    "funded": "PROP_PROFILE_UNSUITABLE",
    "unsuitable": {
      "evaluation": null,
      "funded": "PROP_PROFILE_UNSUITABLE"
    },
    "note": "Research only. Not written into aitrade_operating_policy_v1.json.",
    "funded_research_note": "Executable micro size on the funded grid produces high first-payout then near-certain ruin over 504 trading days. Not a production funded risk. Survive-by-not-trading rows (qty=0) were excluded."
  },
  "ES->MFFU_RAPID_EOD_50K": {
    "evaluation": {
      "book": "ES",
      "profile": "MFFU_RAPID_EOD_50K",
      "stage": "EVALUATION",
      "dd_frac": 0.05,
      "risk_usd": 100.0,
      "policy": "FIXED",
      "governor": "none",
      "mode": "bootstrap",
      "example_qty_at_median_stop": 1,
      "example_actual_risk_usd": 90.0,
      "median_stop_points": 18.0,
      "n_paths": 10000,
      "P(pass)": 0.47,
      "P(breach)": 0.3117,
      "P(timeout)": 0.2183,
      "median_days_to_pass": 149.0,
      "p75_days_to_pass": 191.0,
      "p95_days_to_pass": 238.0,
      "median_trades_to_pass": 496.0,
      "median_max_drawdown_before_pass": 1057.6,
      "probability_consistency_rule_delays_pass": 0.0,
      "average_adjusted_profit_target": 3000.0,
      "expected_number_of_attempts": 2.127659574468085,
      "expected_evaluation_cost": null,
      "expected_evaluation_cost_note": "MFFU evaluation purchase price is REQUIRES_CONFIRMATION \u2014 attempts reported, dollar cost not invented",
      "mean_sized_zero": 0.0
    },
    "funded": "PROP_PROFILE_UNSUITABLE",
    "unsuitable": {
      "evaluation": null,
      "funded": "PROP_PROFILE_UNSUITABLE"
    },
    "note": "Research only. Not written into aitrade_operating_policy_v1.json.",
    "funded_research_note": "Executable micro size on the funded grid produces high first-payout then near-certain ruin over 504 trading days. Not a production funded risk. Survive-by-not-trading rows (qty=0) were excluded."
  },
  "ES->FUNDEDNEXT_FLEX_50K": {
    "evaluation": {
      "book": "ES",
      "profile": "FUNDEDNEXT_FLEX_50K",
      "stage": "EVALUATION",
      "dd_frac": 0.075,
      "risk_usd": 112.5,
      "policy": "FIXED",
      "governor": "none",
      "mode": "bootstrap",
      "example_qty_at_median_stop": 1,
      "example_actual_risk_usd": 90.0,
      "median_stop_points": 18.0,
      "n_paths": 10000,
      "P(pass)": 0.5031,
      "P(breach)": 0.4273,
      "P(timeout)": 0.0696,
      "median_days_to_pass": 119.0,
      "p75_days_to_pass": 164.0,
      "p95_days_to_pass": 225.0,
      "median_trades_to_pass": 396.0,
      "median_max_drawdown_before_pass": 953.4999999999854,
      "probability_consistency_rule_delays_pass": 0.0,
      "average_adjusted_profit_target": 2500.0,
      "expected_number_of_attempts": 1.9876764062810575,
      "expected_evaluation_cost": 139.1174716756112,
      "expected_evaluation_cost_note": "FundedNext first_5=69.99 then 79.99; geometric attempts until first pass",
      "mean_sized_zero": 0.0
    },
    "funded": "PROP_PROFILE_UNSUITABLE",
    "unsuitable": {
      "evaluation": null,
      "funded": "PROP_PROFILE_UNSUITABLE"
    },
    "note": "Research only. Not written into aitrade_operating_policy_v1.json.",
    "funded_research_note": "Executable micro size on the funded grid produces high first-payout then near-certain ruin over 504 trading days. Not a production funded risk. Survive-by-not-trading rows (qty=0) were excluded."
  }
}

## 13. DRY_RUN / policy lock

- execution_default: `DRY_RUN`
- broker_execution: `False`
- operating policy risk_per_trade still null: `True`
- No martingale / loss-chasing / doubling policies were simulated.

## 14. What this phase did not do

No strategy retune. No frozen file edits. No ES freeze. No live execution. No final production risk values.

