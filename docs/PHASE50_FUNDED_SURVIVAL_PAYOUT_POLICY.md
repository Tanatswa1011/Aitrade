# Phase 50 — Funded Survival, Reserve & Payout Policy Research

`DRY_RUN`. No broker. Frozen strategy logic was not modified. ES was not promoted. Operating-policy numerics were not written.

Paths: curve=8000, search=2500, final=8000.

## 1. Primary reason funded accounts failed in Phase 49

**post-payout account cushion**

Phase 49 withdrew surplus down to MLL + 25% of *initial* max-loss, then kept **fixed** initial-DD risk. After MFFU lock at +$100 (FundedNext lock at equity $50,100), leftover cushion was small versus one micro plus an ordinary losing streak. The 504-day horizon then sampled enough of those events to produce near-certain ruin at tradable sizes.

That is **policy-driven ruin after lock**, not proof that 30–90 day funded operation is impossible. `DIAGNOSTIC_PAYOUT_NONE` vs `PHASE49_BASELINE` separates payout-stripping from a worthless edge:

- mean P(survive 504) baseline: `0.1%`
- mean P(survive 504) no-payout: `49.4%`
- mean P(breach) baseline: `99.9%`
- mean P(breach) no-payout: `50.6%`

Phase 49 baseline in this engine **forces** sized trades through remaining cushion (no `BLOCK_INSUFFICIENT_RISK_CAPACITY`). Phase 50 candidate policies skip when one micro exceeds available cushion.

Breach timing and the account state immediately preceding breach: `reports/phase50_funded_root_cause/`.

## 2. Survival curves by horizon

Horizons: 30, 60, 90, 180, 252, 504 trading days. Machine-readable: `reports/phase50_survival_curves/`.

### GC->MFFU_RAPID_EOD_50K

| Horizon | P(survive) | P(first) | P(2) | P(5) | P(10) | median payouts | median $ | E[$] | P(breach) | median t_breach |
|--------:|-----------:|---------:|-----:|-----:|------:|---------------:|---------:|-----:|----------:|----------------:|
| 30 | 100.0% | 9.4% | 0.9% | 0.0% | 0.0% | 0.0 | 0 | 84 | 0.0% | n/a |
| 60 | 100.0% | 15.3% | 3.1% | 0.0% | 0.0% | 0.0 | 0 | 148 | 0.0% | n/a |
| 90 | 100.0% | 17.7% | 4.7% | 0.0% | 0.0% | 0.0 | 0 | 180 | 0.0% | n/a |
| 180 | 100.0% | 19.8% | 6.3% | 0.1% | 0.0% | 0.0 | 0 | 213 | 0.0% | n/a |
| 252 | 100.0% | 20.6% | 7.2% | 0.2% | 0.0% | 0.0 | 0 | 227 | 0.0% | n/a |
| 504 | 100.0% | 22.2% | 9.0% | 0.4% | 0.0% | 0.0 | 0 | 260 | 0.0% | n/a |

### GC->FUNDEDNEXT_FLEX_50K

| Horizon | P(survive) | P(first) | P(2) | P(5) | P(10) | median payouts | median $ | E[$] | P(breach) | median t_breach |
|--------:|-----------:|---------:|-----:|-----:|------:|---------------:|---------:|-----:|----------:|----------------:|
| 30 | 100.0% | 21.8% | 0.6% | 0.0% | 0.0% | 0.0 | 0 | 111 | 0.0% | n/a |
| 60 | 100.0% | 28.9% | 4.1% | 0.0% | 0.0% | 0.0 | 0 | 170 | 0.0% | n/a |
| 90 | 100.0% | 31.1% | 5.7% | 0.2% | 0.0% | 0.0 | 0 | 199 | 0.0% | n/a |
| 180 | 100.0% | 34.1% | 7.8% | 0.6% | 0.0% | 0.0 | 0 | 242 | 0.0% | n/a |
| 252 | 100.0% | 35.6% | 8.8% | 0.7% | 0.1% | 0.0 | 0 | 262 | 0.0% | n/a |
| 504 | 100.0% | 38.2% | 10.4% | 0.9% | 0.1% | 0.0 | 0 | 294 | 0.0% | n/a |

### NQ->MFFU_RAPID_EOD_50K

| Horizon | P(survive) | P(first) | P(2) | P(5) | P(10) | median payouts | median $ | E[$] | P(breach) | median t_breach |
|--------:|-----------:|---------:|-----:|-----:|------:|---------------:|---------:|-----:|----------:|----------------:|
| 30 | 100.0% | 23.0% | 10.1% | 0.2% | 0.0% | 0.0 | 0 | 199 | 0.0% | 29 |
| 60 | 100.0% | 66.3% | 50.3% | 13.0% | 0.1% | 2.0 | 926 | 1077 | 0.0% | 29 |
| 90 | 100.0% | 84.0% | 76.2% | 40.9% | 4.0% | 4.0 | 2079 | 2193 | 0.0% | 37 |
| 180 | 99.9% | 91.9% | 90.6% | 84.4% | 57.5% | 11.0 | 5901 | 5708 | 0.1% | 72 |
| 252 | 99.9% | 92.0% | 90.9% | 87.4% | 78.3% | 16.0 | 8813 | 8312 | 0.1% | 101 |
| 504 | 99.8% | 92.0% | 90.9% | 87.5% | 82.8% | 32.0 | 18516 | 16210 | 0.2% | 186 |

### NQ->FUNDEDNEXT_FLEX_50K

| Horizon | P(survive) | P(first) | P(2) | P(5) | P(10) | median payouts | median $ | E[$] | P(breach) | median t_breach |
|--------:|-----------:|---------:|-----:|-----:|------:|---------------:|---------:|-----:|----------:|----------------:|
| 30 | 100.0% | 23.6% | 3.5% | 0.0% | 0.0% | 0.0 | 0 | 152 | 0.1% | 18 |
| 60 | 99.9% | 64.1% | 42.7% | 0.6% | 0.0% | 1.0 | 555 | 814 | 0.1% | 27 |
| 90 | 99.9% | 78.0% | 68.9% | 21.0% | 0.0% | 3.0 | 1754 | 1707 | 0.1% | 39 |
| 180 | 99.9% | 81.7% | 80.3% | 75.3% | 34.2% | 8.0 | 5287 | 4642 | 0.1% | 39 |
| 252 | 99.9% | 81.7% | 80.4% | 78.2% | 69.9% | 13.0 | 8253 | 7027 | 0.1% | 39 |
| 504 | 99.9% | 81.7% | 80.4% | 78.3% | 76.2% | 28.0 | 18930 | 15292 | 0.1% | 43 |

### ES->MFFU_RAPID_EOD_50K

| Horizon | P(survive) | P(first) | P(2) | P(5) | P(10) | median payouts | median $ | E[$] | P(breach) | median t_breach |
|--------:|-----------:|---------:|-----:|-----:|------:|---------------:|---------:|-----:|----------:|----------------:|
| 30 | 100.0% | 0.2% | 0.0% | 0.0% | 0.0% | 0.0 | 0 | 1 | 0.0% | n/a |
| 60 | 100.0% | 7.8% | 1.9% | 0.0% | 0.0% | 0.0 | 0 | 50 | 0.0% | 39 |
| 90 | 100.0% | 27.0% | 12.5% | 0.3% | 0.0% | 0.0 | 0 | 226 | 0.1% | 73 |
| 180 | 99.9% | 75.2% | 62.2% | 21.6% | 0.6% | 2.0 | 1030 | 1327 | 0.1% | 83 |
| 252 | 99.9% | 89.7% | 83.5% | 52.0% | 7.6% | 5.0 | 2424 | 2421 | 0.1% | 83 |
| 504 | 99.9% | 96.4% | 95.8% | 92.8% | 75.1% | 13.0 | 6527 | 6419 | 0.1% | 117 |

### ES->FUNDEDNEXT_FLEX_50K

| Horizon | P(survive) | P(first) | P(2) | P(5) | P(10) | median payouts | median $ | E[$] | P(breach) | median t_breach |
|--------:|-----------:|---------:|-----:|-----:|------:|---------------:|---------:|-----:|----------:|----------------:|
| 30 | 100.0% | 1.4% | 0.0% | 0.0% | 0.0% | 0.0 | 0 | 8 | 0.0% | n/a |
| 60 | 100.0% | 19.8% | 0.8% | 0.0% | 0.0% | 0.0 | 0 | 121 | 0.1% | 47 |
| 90 | 99.9% | 45.6% | 10.0% | 0.0% | 0.0% | 0.0 | 0 | 358 | 0.1% | 47 |
| 180 | 99.8% | 83.4% | 64.7% | 1.5% | 0.0% | 2.0 | 1401 | 1531 | 0.2% | 102 |
| 252 | 99.8% | 89.2% | 82.9% | 20.6% | 0.0% | 3.0 | 2622 | 2635 | 0.2% | 109 |
| 504 | 99.6% | 90.8% | 88.9% | 82.3% | 22.5% | 8.0 | 6880 | 6432 | 0.4% | 175 |

## 3. Effect of payout frequency

### Payout-mode grid (search paths, averaged across pairs)

| payout_mode | P(first) | P(5) | P(10) | 1y survive | 504 survive | E[payout] | P(breach) | n |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| DELAYED_PAYOUT | 43.0% | 20.9% | 6.2% | 98.5% | 94.5% | 2032 | 5.5% | 6 |
| DYNAMIC_RESERVE | 44.3% | 24.0% | 9.7% | 98.2% | 94.3% | 1917 | 5.7% | 6 |
| FIXED_INTERNAL_RESERVE | 44.2% | 23.8% | 9.5% | 98.4% | 94.6% | 1900 | 5.4% | 6 |
| MINIMUM_PAYOUT_ONLY | 44.3% | 29.0% | 17.9% | 98.6% | 94.9% | 2505 | 5.1% | 6 |
| PARTIAL_SURPLUS_PAYOUT | 41.2% | 29.3% | 16.9% | 98.3% | 94.7% | 2760 | 5.3% | 6 |
| PAYOUT_AS_SOON_AS_ELIGIBLE | 44.1% | 23.6% | 9.4% | 98.3% | 94.4% | 1887 | 5.6% | 6 |
| PAYOUT_NONE | 0.0% | 0.0% | 0.0% | 98.3% | 94.4% | 0 | 5.6% | 6 |


A payout removes only surplus above the internal reserve. `PAYOUT_NONE` is diagnostic, not a production recommendation. Maximum withdrawal is not assumed optimal.

## 4. Effect of retained reserve

### Reserve grid (USD and fraction-of-max-loss cells, averaged)

| reserve_usd | P(first) | P(5) | P(10) | 1y survive | 504 survive | E[payout] | P(breach) | n |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| 1000.0 | 50.6% | 16.3% | 4.1% | 98.4% | 94.8% | 1367 | 5.2% | 9 |
| 1125.0 | 41.2% | 14.7% | 3.3% | 97.6% | 92.7% | 1431 | 7.3% | 3 |
| 1250.0 | 46.9% | 20.6% | 6.9% | 98.5% | 94.9% | 1592 | 5.1% | 9 |
| 1500.0 | 46.1% | 25.1% | 10.6% | 98.5% | 95.0% | 1920 | 5.0% | 9 |
| 250.0 | 53.3% | 0.5% | 0.0% | 97.7% | 93.7% | 884 | 6.3% | 6 |
| 375.0 | 54.5% | 1.2% | 0.0% | 97.2% | 92.1% | 829 | 7.9% | 3 |
| 500.0 | 52.1% | 4.3% | 0.2% | 98.4% | 94.7% | 1018 | 5.3% | 9 |
| 562.5 | 53.6% | 3.8% | 0.2% | 97.3% | 92.6% | 917 | 7.4% | 3 |
| 750.0 | 51.3% | 9.2% | 1.2% | 97.9% | 93.8% | 1121 | 6.1% | 12 |
| 937.5 | 52.1% | 12.6% | 2.2% | 97.3% | 92.1% | 1242 | 7.9% | 3 |


## 5. Effect of dynamic / cushion-dependent risk

### Fixed 1-micro vs dynamic HEALTHY/CAUTION/DEFENSIVE/CRITICAL/LOCKOUT

| use_dynamic_risk | P(first) | P(5) | P(10) | 1y survive | 504 survive | E[payout] | P(breach) | n |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| False | 42.6% | 28.6% | 16.7% | 98.5% | 94.6% | 2817 | 5.4% | 6 |
| True | 16.4% | 2.4% | 0.6% | 100.0% | 100.0% | 271 | 0.0% | 18 |


Risk is not allowed to increase after losses. Thresholds were searched, not assumed.

## 6. Effect of internal daily stops

### Internal daily-stop grid

| daily_stop | P(first) | P(5) | P(10) | 1y survive | 504 survive | E[payout] | P(breach) | n |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| consec | 48.5% | 28.4% | 16.3% | 99.5% | 99.3% | 2816 | 0.7% | 6 |
| cushion_frac | 52.8% | 30.9% | 18.1% | 99.7% | 99.6% | 3179 | 0.4% | 6 |
| none | 51.1% | 28.3% | 16.4% | 99.5% | 99.4% | 2856 | 0.6% | 6 |
| r_loss | 68.8% | 56.8% | 42.9% | 99.9% | 99.9% | 7484 | 0.1% | 6 |


Phase 48 firms may have no DLL. These are AITRADE internal stops.

## 7. Effect of loss-streak controls

### Streak grid (no martingale; pause resumes next session reduced)

| streak_mode | P(first) | P(5) | P(10) | 1y survive | 504 survive | E[payout] | P(breach) | n |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| none | 70.2% | 57.0% | 42.6% | 99.9% | 99.9% | 7496 | 0.1% | 6 |
| pause3 | 69.3% | 57.5% | 39.5% | 99.9% | 99.9% | 6858 | 0.1% | 6 |
| reduce2 | 9.2% | 0.1% | 0.0% | 100.0% | 100.0% | 76 | 0.0% | 6 |
| reduce3 | 11.0% | 0.3% | 0.0% | 100.0% | 100.0% | 104 | 0.0% | 6 |


### Pre vs post MLL-lock risk scale

| post_lock_scale | P(first) | P(5) | P(10) | 1y survive | 504 survive | E[payout] | P(breach) | n |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| 0.5 | 10.4% | 0.1% | 0.0% | 99.7% | 99.7% | 91 | 0.3% | 6 |
| 1.0 | 50.9% | 28.5% | 16.6% | 99.6% | 99.4% | 2870 | 0.6% | 6 |


## 8. Minimum executable contract-floor analysis

| Book | Micro | n stops | min $ | median $ | p90 $ | max $ |
|------|-------|--------:|------:|---------:|------:|------:|
| GC | MGC | 430 | 2.04 | 54.80 | 143.99 | 1049.57 |
| NQ | MNQ | 5000 | 160.00 | 160.00 | 160.00 | 160.00 |
| ES | MES | 5574 | 90.00 | 90.00 | 90.00 | 90.00 |

After Phase 49 ASAP payout, leftover cushion was 25% of max-loss ($500 MFFU / $375 FundedNext). One MNQ ($160) or a few MGC at median stop plus a 3–4 loss streak exceeds that leftover if size is forced. Candidate policies apply **BLOCK_INSUFFICIENT_RISK_CAPACITY** instead of forcing a micro. Full distribution: `reports/phase50_funded_root_cause/contract_floor.csv`.

## 9–15. Best survival-adjusted policy per strategy × firm

Selection uses a composite **and** the component metrics. Hide-and-survive (high survival, almost no payouts) is not preferred when a paying alternative exists. First-payout-then-death is penalized.

| Pair | Class | Payout | Reserve | Dynamic | Daily stop | Streak | P(first) | P(5) | P(10) | 1y | 504 | E[payout] | P(breach) |
|------|-------|--------|--------:|---------|------------|--------|---------:|-----:|------:|----:|----:|----------:|----------:|
| GC->MFFU_RAPID_EOD_50K | PROP_PROFILE_UNSUITABLE | FIXED_INTERNAL_RESERVE | 1250 | True | none | none | 22.2% | 0.4% | 0.0% | 100.0% | 100.0% | 260 | 0.0% |
| GC->FUNDEDNEXT_FLEX_50K | PROP_PROFILE_UNSUITABLE | PARTIAL_SURPLUS_PAYOUT | 500 | True | cushion_frac | none | 38.2% | 0.9% | 0.1% | 100.0% | 100.0% | 294 | 0.0% |
| NQ->MFFU_RAPID_EOD_50K | FUNDED_COMPATIBLE_CANDIDATE | PARTIAL_SURPLUS_PAYOUT | 1500 | False | r_loss | none | 92.0% | 87.5% | 82.8% | 99.9% | 99.8% | 16210 | 0.2% |
| NQ->FUNDEDNEXT_FLEX_50K | FUNDED_COMPATIBLE_CANDIDATE | PARTIAL_SURPLUS_PAYOUT | 1500 | False | r_loss | none | 81.7% | 78.3% | 76.2% | 99.9% | 99.9% | 15292 | 0.1% |
| ES->MFFU_RAPID_EOD_50K | FUNDED_COMPATIBLE_CANDIDATE | PARTIAL_SURPLUS_PAYOUT | 1500 | False | r_loss | none | 96.4% | 92.8% | 75.1% | 99.9% | 99.9% | 6419 | 0.1% |
| ES->FUNDEDNEXT_FLEX_50K | FUNDED_COMPATIBLE_CANDIDATE | PAYOUT_AS_SOON_AS_ELIGIBLE | 1500 | False | r_loss | none | 90.8% | 82.3% | 22.5% | 99.8% | 99.6% | 6432 | 0.4% |

Classifications:

{
  "GC->MFFU_RAPID_EOD_50K": "PROP_PROFILE_UNSUITABLE",
  "GC->FUNDEDNEXT_FLEX_50K": "PROP_PROFILE_UNSUITABLE",
  "NQ->MFFU_RAPID_EOD_50K": "FUNDED_COMPATIBLE_CANDIDATE",
  "NQ->FUNDEDNEXT_FLEX_50K": "FUNDED_COMPATIBLE_CANDIDATE",
  "ES->MFFU_RAPID_EOD_50K": "FUNDED_COMPATIBLE_CANDIDATE",
  "ES->FUNDEDNEXT_FLEX_50K": "FUNDED_COMPATIBLE_CANDIDATE"
}

### Required metric snapshot (stitched policy, final paths)

| Pair | P(first) | P(5) | P(10) | 1y survival | E[cumulative trader payout] | class |
|------|---------:|-----:|------:|------------:|----------------------------:|-------|
| GC->MFFU_RAPID_EOD_50K | 22.2% | 0.4% | 0.0% | 100.0% | 260 | PROP_PROFILE_UNSUITABLE |
| GC->FUNDEDNEXT_FLEX_50K | 38.2% | 0.9% | 0.1% | 100.0% | 294 | PROP_PROFILE_UNSUITABLE |
| NQ->MFFU_RAPID_EOD_50K | 92.0% | 87.5% | 82.8% | 99.9% | 16210 | FUNDED_COMPATIBLE_CANDIDATE |
| NQ->FUNDEDNEXT_FLEX_50K | 81.7% | 78.3% | 76.2% | 99.9% | 15292 | FUNDED_COMPATIBLE_CANDIDATE |
| ES->MFFU_RAPID_EOD_50K | 96.4% | 92.8% | 75.1% | 99.9% | 6419 | FUNDED_COMPATIBLE_CANDIDATE |
| ES->FUNDEDNEXT_FLEX_50K | 90.8% | 82.3% | 22.5% | 99.8% | 6432 | FUNDED_COMPATIBLE_CANDIDATE |

Full stitched policy JSON: `reports/phase50_dynamic_risk/best_stitched.json`.

## 16. Frozen-hash integrity

- GC: `0695d7a881b6247033426bceb077b6895b96405b826c21f32a109a02f44b2d43`
- NQ: `935e3a616351b09dbfa8d2d0e2b5d6850be803fb385ec5e5f8e3593856a1212a`
- Paper journals empty: `True`
- ES not frozen: `True`

## 17. DRY_RUN confirmation

- execution_default: `DRY_RUN`
- broker_execution: `False`
- operating policy risk_per_trade still null: `True`

## Evaluation (unchanged — not production-locked)

{
  "NQ->MFFU_RAPID_EOD_50K": "Phase 49 eval research cell ~10% initial-DD (1 MNQ / $200). Not production-locked.",
  "NQ->FUNDEDNEXT_FLEX_50K": "Phase 49 eval executable 1-MNQ cells (\u226512.5% of $1,500 DD). Not production-locked.",
  "ES->MFFU_RAPID_EOD_50K": "Phase 49 eval finding: reduce after 2 losses improved P(pass) at 10%. Not production-locked."
}

## What this phase did not do

No strategy retune. No frozen file edits. No ES freeze. No live execution. No production payout/risk values in `aitrade_operating_policy_v1.json`.

`FUNDED_COMPATIBLE_CANDIDATE` is a research label on bootstrap paths of historical trade distributions. It is not a production lock, not a live-enable, and **does not promote ES**. GC remains unsuitable for repeated funded payouts under every policy searched. Dynamic-risk cells often survive by shrinking size so far that payout extraction collapses — those cells were not treated as funded-compatible.
