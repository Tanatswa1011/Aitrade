# Phase 51 — Prop Account Replication & Capital Flywheel

`DRY_RUN`. No broker. No account purchases. Frozen strategy logic was not modified. Phase 49/50 report files were not overwritten. Operating-policy numerics were not written.

## 1. Phase 49 / 50 inputs used

{
  "NQ->MFFU_eval": {
    "dd_frac": 0.1,
    "P(pass)": 0.7235,
    "median_days": 55.0,
    "policy": "FIXED",
    "note": "research only"
  },
  "NQ->FN_eval": {
    "dd_frac": 0.175,
    "P(pass)": 0.6537,
    "median_days": 40.0,
    "policy": "FIXED",
    "note": "research only"
  },
  "NQ_funded_phase50": {
    "NQ->MFFU_RAPID_EOD_50K": {
      "P(first)": 0.922,
      "P(5)": 0.8783333333333333,
      "P(10)": 0.8253333333333334,
      "P(survive_1y)": 0.9983333333333333,
      "E[payout]": 16274.913643287167,
      "payout_mode": "PARTIAL_SURPLUS_PAYOUT",
      "reserve_usd": 1500.0
    },
    "NQ->FUNDEDNEXT_FLEX_50K": {
      "P(first)": 0.824,
      "P(5)": 0.79,
      "P(10)": 0.7703333333333333,
      "P(survive_1y)": 0.9996666666666667,
      "E[payout]": 15450.561202176868,
      "payout_mode": "PARTIAL_SURPLUS_PAYOUT",
      "reserve_usd": 1500.0
    }
  },
  "mffu_price": {
    "status": "REQUIRES_CONFIRMATION",
    "confirmed_usd": null,
    "hypothetical_grid_usd": [
      60.0,
      80.0,
      100.0,
      125.0,
      150.0
    ],
    "note": "MFFU Rapid EOD 50K purchase/promo price is not a confirmed PROP_RULES_V1 field. Hypothetical cost scenarios only."
  },
  "fn_price": {
    "status": "CONFIRMED",
    "first_5_purchase_price": 69.99,
    "purchase_6_plus_price": 79.99,
    "reset_fee": 77.99,
    "listed_standard_price": 133.99,
    "note": "Retries are modeled as new purchases, not resets. Reset exists but is not auto-applied."
  },
  "caps": {
    "MFFU_RAPID_EOD_50K": {
      "value": 3,
      "status": "CONFIRMED"
    },
    "FUNDEDNEXT_FLEX_50K": {
      "value": null,
      "status": "REQUIRES_CONFIRMATION",
      "hypothetical_caps": [
        1,
        3,
        5
      ]
    },
    "copy_trading": {
      "MFFU": "REQUIRES_CONFIRMATION",
      "FUNDEDNEXT": "REQUIRES_CONFIRMATION",
      "note": "Copy trading is REQUIRES_CONFIRMATION. Accounts are independently executed."
    }
  }
}

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
| NQ_FN_ONLY_NEXT_500 | REPLICATION_VIABLE | 3.0 | 99.8% | 99.4% | 97.5% | 331 | 39810 | 33 | 0 | 98.5% |
| NQ_MFFU_ONLY_NEXT_500 | REPLICATION_VIABLE | 2.9 | 99.9% | 99.5% | 94.2% | 417 | 41324 | 42 | 0 | 99.6% |
| NQ_ALTERNATING_NEXT_500 | REPLICATION_VIABLE | 5.8 | 99.7% | 99.4% | 99.1% | 746 | 76597 | 305 | 0 | 99.2% |
| NQ_BEST_EV_NEXT_500 | REPLICATION_VIABLE | 5.8 | 99.9% | 99.6% | 99.5% | 746 | 78078 | 289 | 0 | 99.4% |

## 12–13. Self-funding date and probability by horizon

Primary FN_ONLY $500:

- median self-funding day: `107`
- P(30d) `0.0%`
- P(60d) `4.5%`
- P(90d) `30.6%`
- P(180d) `91.9%`
- P(1y) `98.5%`

## 14. Probability external bankroll is exhausted

See `reports/phase51_bankroll_risk/`. Headline P(exhausted before self-funding) = `0.0%`.

## 15. Replication-efficiency metrics

Headline: cost/funded `112`; days/new funded `243`; payout per eval dollar `135.61`; funded per $100 spend `1.00`.

## 16. Withdrawal vs growth frontier

`reports/phase51_reinvestment_policy/frontier.csv` — aggressive reinvestment vs 50% income-first vs cash-reserve-first. Components are not collapsed into one score.

## 17. Stress-test outcomes

`reports/phase51_stress_tests/`. Pass −10pp, first-payout haircut via payout −25%, costs +25%, duration +50%, expectancy scale 0.70.

## 18. Final replication classification

{
  "NQ_FN_ONLY_NEXT_500": "REPLICATION_VIABLE",
  "NQ_MFFU_ONLY_NEXT_500": "REPLICATION_VIABLE",
  "NQ_ALTERNATING_NEXT_500": "REPLICATION_VIABLE",
  "NQ_BEST_EV_NEXT_500": "REPLICATION_VIABLE",
  "GC": "PROP_PROFILE_UNSUITABLE",
  "ES_FUNDEDNEXT_ONLY": "REPLICATION_VIABLE",
  "ES_MFFU_ONLY": "REPLICATION_BORDERLINE",
  "STRESS_PASS_MINUS_10PP": "REPLICATION_VIABLE",
  "STRESS_PAYOUT_MINUS_25PCT": "REPLICATION_VIABLE",
  "STRESS_COST_PLUS_25PCT": "REPLICATION_VIABLE",
  "STRESS_DURATION_PLUS_50PCT": "REPLICATION_VIABLE",
  "STRESS_EXPECTANCY_DEGRADE": "REPLICATION_VIABLE",
  "STRESS_COMBINED_BEAR": "REPLICATION_VIABLE"
}

GC remains **PROP_PROFILE_UNSUITABLE** (not run as a replication candidate). ES is secondary research only and is **not promoted**. FundedNext cap and MFFU price stay unconfirmed for production conclusions.

## 19. Frozen-hash integrity

- GC: `0695d7a881b6247033426bceb077b6895b96405b826c21f32a109a02f44b2d43`
- NQ: `935e3a616351b09dbfa8d2d0e2b5d6850be803fb385ec5e5f8e3593856a1212a`
- Paper journals empty: `True`
- ES not frozen: `True`

## 20. DRY_RUN confirmation

- execution_default: `DRY_RUN`
- broker_execution: `False`
- operating policy risk_per_trade still null: `True`
- no accounts purchased or activated

## What this phase did not do

No strategy retune. No Phase 49/50 source-result overwrite. No ES freeze. No live execution. No production risk/payout lock. No invented MFFU purchase price. No invented FundedNext funded-account cap.
