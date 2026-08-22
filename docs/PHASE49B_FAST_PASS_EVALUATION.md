# Phase 49B — Fast-Pass Evaluation Optimization

`DRY_RUN`. No broker. Frozen strategy logic was not modified. Phase 49/50/51 report files were not overwritten. Operating-policy numerics were not written. ES is not promoted. GC remains `PROP_PROFILE_UNSUITABLE`.

## 1. Phase 49 baseline

{
  "NQ->MFFU_RAPID_EOD_50K": {
    "dd_frac": 0.1,
    "P(pass)": 0.7235,
    "median_days_to_pass": 55.0,
    "p75_days_to_pass": 78.0,
    "policy": "FIXED"
  },
  "NQ->FUNDEDNEXT_FLEX_50K": {
    "dd_frac": 0.175,
    "P(pass)": 0.6537,
    "median_days_to_pass": 40.0,
    "p75_days_to_pass": 57.0,
    "policy": "FIXED"
  },
  "ES->MFFU_RAPID_EOD_50K": {
    "dd_frac": 0.05,
    "P(pass)": 0.47,
    "median_days_to_pass": 149.0,
    "p75_days_to_pass": 191.0,
    "policy": "FIXED"
  },
  "ES->FUNDEDNEXT_FLEX_50K": {
    "dd_frac": 0.075,
    "P(pass)": 0.5031,
    "median_days_to_pass": 119.0,
    "p75_days_to_pass": 164.0,
    "policy": "FIXED"
  }
}

These cells are **research baselines**, not production settings.

## 2. Executable quantity analysis

Stop ≈ 80 NQ points. 1 MNQ ≈ $160 raw stop risk (already includes Phase 46 1-tick + 0.20pt commission in R). Firm caps: 3 minis / 30 micros. Quantity is floored from dollar risk — never rounded up.

{
  "NQ->MFFU_RAPID_EOD_50K": {
    "max_executable": 12,
    "unit_risk_usd": 160.0,
    "note": "OK"
  },
  "NQ->FUNDEDNEXT_FLEX_50K": {
    "max_executable": 9,
    "unit_risk_usd": 160.0,
    "note": "OK"
  },
  "ES->MFFU_RAPID_EOD_50K": {
    "max_executable": 22,
    "unit_risk_usd": 90.0,
    "note": "OK"
  },
  "ES->FUNDEDNEXT_FLEX_50K": {
    "max_executable": 16,
    "unit_risk_usd": 90.0,
    "note": "OK"
  }
}

If no executable quantity fits: `BLOCK_INSUFFICIENT_RISK_CAPACITY`.

## 3. Fastest achievable pass distributions

See `reports/phase49b_fast_pass/`. Screening used 2500 bootstrap paths; finalists 10000. Same-day clustering is preserved (day bootstrap). Chronological comparison is in the same folder.

## 4. 10 / 14 / 20 / 30-day frontier

{
  "FUNDEDNEXT_FLEX_50K": {
    "10": {
      "status": "FAST_PASS_UNSUPPORTED",
      "best": null,
      "median": null,
      "P(pass)": null
    },
    "14": {
      "status": "HIT",
      "best": "Q2_FIXED__DSTOP_FRAC35",
      "median": 14.0,
      "P(pass)": 0.5718
    },
    "20": {
      "status": "HIT",
      "best": "Q2_FIXED__DSTOP_FRAC35_CHRONO",
      "median": 15.0,
      "P(pass)": 0.5993333333333334
    },
    "30": {
      "status": "HIT",
      "best": "Q2_FIXED__DSTOP_FRAC35_CHRONO",
      "median": 15.0,
      "P(pass)": 0.5993333333333334
    }
  },
  "MFFU_RAPID_EOD_50K": {
    "10": {
      "status": "FAST_PASS_UNSUPPORTED",
      "best": null,
      "median": null,
      "P(pass)": null
    },
    "14": {
      "status": "FAST_PASS_UNSUPPORTED",
      "best": null,
      "median": null,
      "P(pass)": null
    },
    "20": {
      "status": "HIT",
      "best": "COMBINED_Q2_FIXED__DSTOP_FRAC35_DSTOP_FRAC35_CHRONO",
      "median": 19.0,
      "P(pass)": 0.6266666666666667
    },
    "30": {
      "status": "HIT",
      "best": "COMBINED_Q2_FIXED__DSTOP_FRAC35_DSTOP_FRAC35_CHRONO",
      "median": 19.0,
      "P(pass)": 0.6266666666666667
    }
  }
}

A tier is `FAST_PASS_UNSUPPORTED` if no policy hits that median with P(pass)≥0.45 and P(breach)≤0.55.

## 5. P(pass) / P(breach) tradeoff

Pareto table: `reports/phase49b_pareto_frontier/pareto.csv`. Highlighted roles:

- SAFEST: Q1_FIXED__APPR_REDUCE
- BALANCED: Q2_FIXED__DSTOP_FRAC35
- FASTEST_ACCEPTABLE: Q2_FIXED__DSTOP_FRAC35

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

{
  "FUNDEDNEXT_FLEX_50K": {
    "baseline": {
      "median_self_funding_day": 107.0,
      "P(self_funding_by_60d)": 0.0522,
      "P(self_funding_by_90d)": 0.3036,
      "P(self_funding_by_180d)": 0.9202,
      "h90_expected_active_funded": 1.825,
      "h180_expected_active_funded": 2.6998,
      "h365_expected_active_funded": 2.9642,
      "total_evaluation_spend": 329.87553599999995,
      "external_capital_required": 295.947946,
      "total_trader_payout": 39939.64404747384,
      "days_per_new_funded_account": 243.33333333333334,
      "funded_accounts_created_per_$100_eval_spend": 1.0086427268946798,
      "classification": "REPLICATION_VIABLE"
    },
    "fast_pass": {
      "median_self_funding_day": 73.0,
      "P(self_funding_by_60d)": 0.2798,
      "P(self_funding_by_90d)": 0.7308,
      "P(self_funding_by_180d)": 0.968,
      "h90_expected_active_funded": 2.684,
      "h180_expected_active_funded": 2.9196,
      "h365_expected_active_funded": 2.95,
      "total_evaluation_spend": 373.51780399999996,
      "external_capital_required": 332.79592300000013,
      "total_trader_payout": 43060.01122070903,
      "days_per_new_funded_account": 243.33333333333334,
      "funded_accounts_created_per_$100_eval_spend": 0.9029208947501883,
      "classification": "REPLICATION_VIABLE"
    },
    "delta": {
      "median_self_funding_day": -34.0,
      "P(self_funding_by_60d)": 0.2276,
      "P(self_funding_by_90d)": 0.4272,
      "P(self_funding_by_180d)": 0.047799999999999954,
      "h90_expected_active_funded": 0.8590000000000002,
      "h180_expected_active_funded": 0.21979999999999977,
      "h365_expected_active_funded": -0.014199999999999768,
      "total_evaluation_spend": 43.642268,
      "external_capital_required": 36.84797700000013,
      "total_trader_payout": 3120.367173235187,
      "days_per_new_funded_account": 0.0,
      "funded_accounts_created_per_$100_eval_spend": -0.10572183214449149,
      "classification": null
    },
    "improved": true
  },
  "MFFU_RAPID_EOD_50K": {
    "baseline": {
      "median_self_funding_day": 124.0,
      "P(self_funding_by_60d)": 0.0188,
      "P(self_funding_by_90d)": 0.1758,
      "P(self_funding_by_180d)": 0.8662,
      "h90_expected_active_funded": 1.3072,
      "h180_expected_active_funded": 2.4458,
      "h365_expected_active_funded": 2.9246,
      "total_evaluation_spend": 419.36,
      "external_capital_required": 376.15999999999997,
      "total_trader_payout": 41097.35513357824,
      "days_per_new_funded_account": 243.33333333333334,
      "funded_accounts_created_per_$100_eval_spend": 0.7709238888888889,
      "classification": "REPLICATION_VIABLE"
    },
    "fast_pass": {
      "median_self_funding_day": 81.0,
      "P(self_funding_by_60d)": 0.2086,
      "P(self_funding_by_90d)": 0.6226,
      "P(self_funding_by_180d)": 0.97,
      "h90_expected_active_funded": 2.4708,
      "h180_expected_active_funded": 2.8496,
      "h365_expected_active_funded": 2.951,
      "total_evaluation_spend": 501.04,
      "external_capital_required": 428.36098200000004,
      "total_trader_payout": 45128.47532905738,
      "days_per_new_funded_account": 243.33333333333334,
      "funded_accounts_created_per_$100_eval_spend": 0.6571267582417583,
      "classification": "REPLICATION_VIABLE"
    },
    "delta": {
      "median_self_funding_day": -43.0,
      "P(self_funding_by_60d)": 0.1898,
      "P(self_funding_by_90d)": 0.44680000000000003,
      "P(self_funding_by_180d)": 0.1038,
      "h90_expected_active_funded": 1.1636000000000002,
      "h180_expected_active_funded": 0.40379999999999994,
      "h365_expected_active_funded": 0.0264000000000002,
      "total_evaluation_spend": 81.68,
      "external_capital_required": 52.20098200000007,
      "total_trader_payout": 4031.1201954791395,
      "days_per_new_funded_account": 0.0,
      "funded_accounts_created_per_$100_eval_spend": -0.1137971306471306,
      "classification": null
    },
    "improved": true
  }
}

## 13. DAYS_PER_NEW_FUNDED_ACCOUNT

See flywheel impact rows (`days_per_new_funded_account`, `funded_accounts_created_per_$100_eval_spend`) plus chain times (purchase → pass → first payout → next eval → next funded) in `reports/phase49b_flywheel_impact/chain_times.json`.

## 14–16. Role candidates

- **SAFEST**: Q1_FIXED__APPR_REDUCE
- **BALANCED**: Q2_FIXED__DSTOP_FRAC35
- **FASTEST_ACCEPTABLE**: Q2_FIXED__DSTOP_FRAC35

Classifications: {
  "GC": "PROP_PROFILE_UNSUITABLE",
  "NQ->FUNDEDNEXT_FLEX_50K->SAFEST": "FAST_PASS_UNSUPPORTED",
  "NQ->FUNDEDNEXT_FLEX_50K->BALANCED": "FAST_PASS_VIABLE",
  "NQ->FUNDEDNEXT_FLEX_50K->FASTEST_ACCEPTABLE": "FAST_PASS_VIABLE",
  "NQ->MFFU_RAPID_EOD_50K->SAFEST": "FAST_PASS_VIABLE",
  "NQ->MFFU_RAPID_EOD_50K->BALANCED": "FAST_PASS_BORDERLINE",
  "NQ->MFFU_RAPID_EOD_50K->FASTEST_ACCEPTABLE": "FAST_PASS_BORDERLINE",
  "ES->FUNDEDNEXT_FLEX_50K": "FAST_PASS_BORDERLINE",
  "ES->FUNDEDNEXT_FLEX_50K_best": "Q2_FIXED",
  "ES->MFFU_RAPID_EOD_50K": "FAST_PASS_BORDERLINE",
  "ES->MFFU_RAPID_EOD_50K_best": "Q2_FIXED"
}

## 17. Recommendation only — not a production lock

Do not write these quantities or governors into `aitrade_operating_policy_v1.json`. Do not buy evaluations. 10–14 day pass remains a research target; if unsupported, that is reported.

## 18. Frozen hashes

{
  "ok": true,
  "reasons": [],
  "gc": "0695d7a881b6247033426bceb077b6895b96405b826c21f32a109a02f44b2d43",
  "nq": "935e3a616351b09dbfa8d2d0e2b5d6850be803fb385ec5e5f8e3593856a1212a"
}

## 19. Paper journals

Empty: True

## 20. DRY_RUN

- execution_default: `DRY_RUN`
- broker_execution: `False`
- risk_per_trade still null: `True`

## What this phase did not do

No strategy retune. No Phase 49/50/51 source-result overwrite. No ES freeze. No live execution. No production risk lock. No invented MFFU purchase price.
