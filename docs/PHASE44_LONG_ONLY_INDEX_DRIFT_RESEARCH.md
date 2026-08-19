# Phase 44 — Long-only equity-index drift / pullback

Research only. `DRY_RUN`. No broker. Nothing frozen.

This is a **new** long-only hypothesis, not Phase 40 minus shorts and not Phase 42 minus shorts. Locked before P&L:

- State: `LONG_STATE_20D_POSITIVE` — prior completed 20-session roll-cleaned return > 0 → next session eligible for longs, else **flat**. Never short.
- Baseline: `BULL_STATE_RTH_OPEN_LONG` — if bullish, buy next RTH 09:30 ±1 tick, flatten 15:55.
- Candidate: `LONG20_FIRST_RED_GREEN_5M` — first red 5m after 09:30, then first green 5m, enter **next 5m open** ±1 tick, stop = pullback low − 1 tick, flatten 15:55. No VWAP.

## 1. Verdict

- **Overall:** `LONG_DRIFT_BETA_ONLY`
- **ES_LONG_ONLY_STATUS:** `LONG_ONLY_EDGE_REJECTED`
- **NQ_LONG_ONLY_STATUS:** `LONG_DRIFT_BETA_ONLY`
- **Recommendation:** `CLOSE_LONG_ONLY_AS_BETA_NOT_BOOK_3`

A 20-session positive-return filter does **not** identify a favorable long regime for the next RTH session. On both ES and NQ, days after a non-positive 20d state have **higher** cost-adjusted RTH open→15:55 expectancy than days after 20d > 0. Unconditional RTH long exposure is the thing that makes money. The locked first-pullback candidate is negative on both instruments and all ES years. Do not freeze. Do not add shorts. Do not reopen two-sided TSMOM.

## 2. Frozen integrity

Verified before and after. Frozen files were not modified. Nothing was written to `strategy_frozen/`.

- GC VWAP V2 config hash: `0695d7a881b6247033426bceb077b6895b96405b826c21f32a109a02f44b2d43`
- NQ DVP config hash: `935e3a616351b09dbfa8d2d0e2b5d6850be803fb385ec5e5f8e3593856a1212a`
- File SHA GC: `12615d19ef3deed36b7929c161ef7c377975cd2317c0ae6d06f1949bd327046f`
- File SHA NQ: `34927fba0f268e56cf166cf8939bdfec6aa031510f7cee9acfab3909dd36b541`

## 3. Data and roll methodology

- Daily signal series: Databento ohlcv-1d `.v.0` via Phase 40 `load_instrument`. Sunday Globex stubs dropped. Lookbacks use roll-cleaned close-to-close (`mark_rolls`: instrument_id sidecar else 8× trailing-60 median overnight **and** 80 bps). ES 67 roll flags, NQ 94.
- State for RTH date `D` uses the last daily bar with `date < D`. Every one of 1,651 RTH dates has a same-calendar daily bar; none of those unfinished bars were used at 09:30.
- Daily coverage: **2010-06-07 → 2026-08-14**, 4,196 weekday sessions each.
- Intraday: Phase 38 ES/NQ 1m, NY RTH, holidays, flatten 15:55, 1-tick primary + commissions. **2020-01-02 → 2026-08-14**, 1,651 RTH days each (ES 2,338,955 1m bars; NQ 2,338,684). ES roll Databento `.v.0`; NQ AITRADE volume-crossover.
- Chronology locked: TRAIN through `2022-12-30`, HOLDOUT from `2023-01-03`.
- News: 08:30 T±5m removes **0** RTH entries. No complete 10:00 calendar was invented. ~26% of pullback entries fall in 09:55–10:05; they were **not** filtered.
- Pullback C (percent of morning impulse) was not tested: it was not an objectively locked rule.

## 4. Unconditional long drift

Always-long RTH: buy every valid 09:30, flatten 15:55, 1 tick adverse + commissions. This is the beta benchmark.

| Instrument | N | E[pts] | WR | Total pts | Max DD | Train E | Holdout E |
|---|---:|---:|---:|---:|---:|---:|---:|
| ES | 1651 | +0.380 | 0.532 | +628 | 1037 | (see years) | (see years) |
| NQ | 1651 | +2.300 | 0.542 | +3797 | 4849 | (see years) | (see years) |

RTH equity drift is real and positive after 1-tick costs. That is **not** a Strategy #3. It is the thing a conditional long book must beat on return/risk.

## 5. Bullish-state forward returns

Mean cost-adjusted RTH open→15:55 points after a completed prior-session state. Off = not in that state.

| Instrument | State | N on | Mean on | Hit on | N off | Mean off | Diff (on−off) |
|---|---|---:|---:|---:|---:|---:|---:|
| ES | 10d > 0 | 1045 | +0.139 | 0.524 | 606 | +0.796 | −0.656 |
| ES | **20d > 0 PRIMARY** | 1105 | **−0.286** | 0.533 | 546 | **+1.730** | **−2.016** |
| ES | 60d > 0 | 1267 | −1.031 | 0.523 | 384 | +5.038 | −6.070 |
| ES | EMA20 rising | — | — | — | — | — | −3.286 |
| ES | 20d>0 and 5d>0 | — | — | — | — | — | −0.532 |
| NQ | 10d > 0 | 1033 | +1.647 | 0.542 | 618 | +3.390 | −1.743 |
| NQ | **20d > 0 PRIMARY** | 1070 | **+0.611** | 0.546 | 581 | **+5.409** | **−4.798** |
| NQ | 60d > 0 | 1206 | −1.961 | 0.539 | 445 | +13.847 | −15.808 |
| NQ | EMA20 rising | — | — | — | — | — | −2.191 |
| NQ | 20d>0 and 5d>0 | — | — | — | — | — | −1.148 |

Hit rate barely moves (~53–55%). The entire effect is in the **left tail of non-bullish days**: after a down 20d/60d stretch, the next RTH session has large positive expectancy (bounce). Medium-term strength is not a license to buy the open.

## 6. Primary 20d-positive state

- ES bull share: **66.9%** of RTH days (1105 / 1651).
- NQ bull share: **64.8%** (1070 / 1651).
- Strength tertiles of *positive* 20d returns (diagnostic only; threshold stayed at > 0):
  - ES: weak −3.25 pts, medium +0.80, strong +0.97. Weak-positive 20d is the worst bucket.
  - NQ: weak −8.93, medium −10.51, strong +13.32. Strong 20d is the only NQ bucket with a clear positive t-stat (~2.07). That is **not** a license to move the threshold: 20d > +1.0% open-long is −2.44 NQ pts.
- Volatility / gap / close location were not promoted to filters.

## 7. Short-term dip inside bullish state

2×2: prior completed 20d sign × prior 1d sign. Forward = always-long RTH points (1 tick).

| Instrument | 20d | Prior 1d | N | Mean RTH pts | Hit | t |
|---|---|---|---:|---:|---:|---:|
| ES | Positive | Positive | 666 | +0.296 | 0.526 | 0.25 |
| ES | Positive | Negative | 439 | −1.170 | 0.544 | −0.70 |
| ES | Non-positive | Positive | 231 | −3.817 | 0.502 | −1.11 |
| ES | Non-positive | Negative | 315 | **+5.797** | 0.552 | 1.80 |
| NQ | Positive | Positive | 638 | −1.769 | 0.536 | −0.31 |
| NQ | Positive | Negative | 432 | +4.127 | 0.560 | 0.51 |
| NQ | Non-positive | Positive | 267 | −14.105 | 0.524 | −0.97 |
| NQ | Non-positive | Negative | 314 | **+22.003** | 0.545 | 1.54 |

The largest long expectancy is **not** `UPTREND + DIP → BOUNCE`. It is **non-positive 20d + down day → bounce**. Buying a dip *inside* a 20d uptrend is negative on ES and only modestly positive on NQ. That is a mean-reversion story after weakness, and it is the same asymmetry that killed two-sided TSMOM shorts. It is **not** a predeclared Phase 44 candidate, and it is not turned into a strategy here.

Prior 3d cumulative, same pattern: ES/NQ non-positive 20d + negative 3d is the best cell (ES +2.97, NQ +19.03). 20d-positive + negative 3d is negative on both.

## 8. Continuation vs recovery

Within 20d > 0:

| Instrument | Near 20d high | Modest/deep dip |
|---|---|---|
| ES | n=935, mean +0.12, hit 0.534 | n=170, mean **−2.53**, hit 0.529 |
| NQ | n=757, mean −1.76, hit 0.542 | n=313, mean +6.35, hit 0.556 |

ES long drift inside a 20d uptrend is continuation-ish and tiny. NQ’s only interesting cell is a modest dip, and even that is weaker than the *non-bullish* dip cell in section 7. Neither sub-hypothesis is a strategy.

## 9. RTH open-long baseline

`BULL_STATE_RTH_OPEN_LONG` on 20d-positive days only.

| Instrument | N | E[pts] | WR | PF | Train | Holdout | 0-tick | 2-tick |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| ES | 1105 | **−0.286** | 0.533 | 0.98 | −0.889 | +0.212 | +0.294 | −0.786 |
| NQ | 1070 | +0.611 | 0.546 | 1.01 | +0.144 | +0.973 | +1.311 | +0.111 |

ES baseline is negative after 1 tick. Per the predeclared gate, first-pullback is **not** a rescue. NQ baseline is a thin slice of unconditional drift (always-long is +2.30). 2-tick NQ still barely positive; ES 2-tick is worse.

Neighbors (open-long, 1 tick): ES 10d +0.31, 60d −0.91, 20d>+0.5% −0.23, 20d>+1% +0.15. NQ 10d +3.92, 60d −0.94, 20d>+0.5% +0.11, 20d>+1% −2.44. Profitability is not stable across the locked neighborhood. NQ `threshold_stable` is **False**.

TOD diagnostic (bullish days only): ES 12:00 −0.79, 14:00 −0.03, 15:55 −0.29. NQ 12:00 −1.23, 14:00 +1.68, 15:55 +0.61. Primary remains 15:55. Minute-level exits were not optimized.

## 10. First-pullback candidate

`LONG20_FIRST_RED_GREEN_5M`. One trade/day. No VWAP. Stop = pullback low − 1 tick. Primary 1R, else flatten 15:55.

| Instrument | N | E[R] | E[pts] | WR | PF | Train E[R] | Holdout E[R] | P(1R) | P(2R) | Ambiguous |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| ES | 1100 | **−0.126** | −0.790 | 0.445 | 0.79 | −0.122 | −0.129 | 0.458 | 0.010 | 5 |
| NQ | 1066 | **−0.006** | −0.408 | 0.502 | 0.98 | −0.002 | −0.009 | 0.504 | 0.010 | 4 |

The pullback **does not** improve geometry versus buying the open on the same days. It shrinks the stop and kills the open-to-close drift. ATR 0.5× 5m pullback is diagnostic only and was not promoted.

## 11. Target matrix

1-tick, stop = pullback low − 1 tick. Independent of full-sample selection.

| Instrument | 0.5R | 1R (locked) | 1.5R | 2R | 3R |
|---|---|---|---|---|---|
| ES E[R] | −0.070 | −0.126 | −0.137 | −0.132 | −0.096 |
| NQ E[R] | −0.025 | −0.006 | +0.001 | +0.004 | +0.024 |

NQ 1.5R–3R are economically zero and were **not** chosen after seeing P&L. Locked primary remains 1R, which loses.

## 12. MFE / MAE

- **ES pullback:** avg MFE 4.98, avg MAE 5.54, avg stop 6.80 pts (~$340/contract), P(0.5R)=0.63 at 0.5R target path / P(1R)=0.458, P(2R)=0.010, median hold ~17.6 minutes. Open-long MFE/MAE is a full-session path (avg hold 23,040s) and is not a comparable RR.
- **NQ pullback:** avg MFE 26.3, avg MAE 27.2, avg stop 34.5 pts (~$690/contract, p95 stop 73.3 pts / $1,465), P(1R)=0.504, P(2R)=0.010, similar ~17.5 min hold.

First red/green does not produce a positive skew 2R tail. P(2R) ≈ 1%.

## 13. ES results

Status: `LONG_ONLY_EDGE_REJECTED`.

- Always-long RTH is +0.38 pts/day. 20d-positive open-long is **−0.29**. Calendar total −316 vs always-long +628. Calendar DD 1,062 vs 1,037 (filter **increases** DD). Exposure 66.9%.
- Pullback E[R]=−0.126, every year 2020–2026 negative, holdout −0.129, 2-tick −1.04 pts, block-bootstrap 95% CI for expectancy **entirely below zero** [−1.20, −0.38].
- 2022: bull share falls to 40.4% (off-switch works somewhat) but remaining open-long E=−1.29. Non-bullish days are the bounce.
- Mode 2 overnight 1/3/5 session longs on 20d>0 earn less than always-long the same hold (hold 5: +5.65 vs +8.02). Overnight is not a hidden rescue and is not prop-primary.

## 14. NQ results

Status: `LONG_DRIFT_BETA_ONLY`.

- Always-long RTH is +2.30 pts/day, total +3,797, DD 4,849. 20d-positive open-long is +0.61, calendar total +654, **worse** DD 5,419. Exposure 64.8%. Per active day the filter is weaker than the days it skips (+0.61 vs +5.41 off-state).
- Pullback E[R]=−0.006, PF 0.98, holdout −0.009. 0-tick +0.016 (gross noise). 2-tick −0.54.
- 2022: bull share 35.2%, open-long E=−1.01. Always-long 2022 E=−11.24 — the filter reduces activity in the bear but does not produce a positive remaining book, and it forgoes the bounce days.
- Mode 2: bullish hold 5 E=+25.8 vs always-long +31.4. Same story: the state withholds the better days.

## 15. Cost stress

| Instrument | Open 0-tick | Open 1-tick | Open 2-tick | PB 0-tick | PB 1-tick | PB 2-tick |
|---|---:|---:|---:|---:|---:|---:|
| ES | +0.294 | −0.286 | −0.786 | −0.378 | −0.790 | −1.038 |
| NQ | +1.311 | +0.611 | +0.111 | +0.016 | −0.408 | −0.541 |

A tiny gross open-long edge on ES disappears at 1 tick. NQ open-long survives 2 ticks only as a diluted always-long. Pullback fails all three overlays on ES and the primary/2-tick overlays on NQ.

Stop-buffer 0 vs 2 ticks on the pullback does not flip the sign.

## 16. Train / holdout

Predeclared: TRAIN through `2022-12-30`, HOLDOUT from `2023-01-03`. No holdout threshold changes.

| Instrument | Series | Train N | Train E | Holdout N | Holdout E |
|---|---|---:|---:|---:|---:|
| ES | open-long pts | 500 | −0.889 | 605 | +0.212 |
| ES | pullback R | 499 | −0.122 | 601 | −0.129 |
| NQ | open-long pts | 467 | +0.144 | 603 | +0.973 |
| NQ | pullback R | 465 | −0.002 | 601 | −0.009 |

ES open-long holdout is slightly positive after a negative train — not a stable edge, and still worse than skipping the filter. Pullback holdout is negative on both.

## 17. Walk-forward

Year blocks on the locked rules. FOUND required multiple positive blocks.

**Open-long E[pts] by year**

| Year | ES N | ES E | NQ N | NQ E |
|---|---:|---:|---:|---:|
| 2020 | 199 | −1.62 | 204 | +0.46 |
| 2021 | 200 | +0.04 | 175 | +0.36 |
| 2022 | 101 | −1.29 | 88 | −1.01 |
| 2023 | 153 | +4.27 | 178 | +15.63 |
| 2024 | 200 | −1.31 | 191 | −3.46 |
| 2025 | 167 | −2.17 | 161 | −10.48 |
| 2026 YTD | 85 | +1.17 | 73 | +2.08 |

ES: 3/7 years positive. NQ: 4/7 years positive, with 2023 carrying the mean and 2024–2025 deep red. Not stable.

**Pullback E[R] by year:** ES all 7 years negative. NQ mixed and small (best 2021 +0.065).

## 18. Year-by-year

Always-long RTH (the beta that the filter is trying to time):

| Year | ES E[pts] | ES WR | ES DD | NQ E[pts] | NQ WR | NQ DD |
|---|---:|---:|---:|---:|---:|---:|
| 2020 | +0.02 | 0.566 | 336 | +5.62 | 0.570 | 1451 |
| 2021 | +1.73 | 0.530 | 267 | +5.44 | 0.542 | 1145 |
| 2022 | **−1.91** | 0.488 | 807 | **−11.24** | 0.496 | 3989 |
| 2023 | +1.78 | 0.569 | 512 | +14.89 | 0.585 | 1854 |
| 2024 | −1.41 | 0.518 | 701 | −7.03 | 0.510 | 3026 |
| 2025 | +1.06 | 0.538 | 550 | +2.43 | 0.563 | 2744 |
| 2026 YTD | +2.02 | 0.510 | 534 | +8.33 | 0.523 | 3656 |

A long-only index book will lose in 2022. The question was whether 20d>0 keeps expectancy and DD acceptable. It does not: ES filtered 2022 still −1.29; NQ filtered 2022 still −1.01; calendar DD is not improved.

## 19. Bear-regime behavior

Does 20d>0 turn off quickly enough?

- ES 2022 bull share **40.4%** vs 66.9% full sample. NQ **35.2%** vs 64.8%. The switch dimmed, it did not go dark, and it dimmed **after** the damage was already in the 20d window.
- Remaining 2022 open-long trades lose (ES −1.29, NQ −1.01). Worst ES open-long day −142 pts; NQ −520 pts.
- The days the filter is **off** in 2022 include the bounce cells of section 7. Turning off after a completed 20d decline withholds some of the best long RTH days.

## 20. Always-long comparison

| Instrument | Always total | Bull-open total | Always DD | Bull DD | Calmar always | Calmar bull | Distinct? | Filter improves? |
|---|---:|---:|---:|---:|---:|---:|---|---|
| ES | +628 | −316 | 1037 | 1062 | 0.61 | −0.30 | No | No |
| NQ | +3797 | +654 | 4849 | 5419 | 0.78 | 0.12 | No | No |

The conditional state does not improve return or risk versus buying every RTH session. On both names it selects the **worse** subset of days. That is `LONG_DRIFT_BETA_ONLY` for NQ (longs still print a small plus) and `LONG_ONLY_EDGE_REJECTED` for ES (the strategy itself loses).

## 21. Exposure efficiency

- ES: always mean/calendar +0.380 vs bull mean/calendar **−0.192**, mean/active −0.286, exposure 66.9%.
- NQ: always mean/calendar +2.300 vs bull mean/calendar +0.396, mean/active +0.611, exposure 64.8%.
- Return per exposure day is below the skipped days. Flat days are not “risk reduction”; they are missed bounce days plus still-large DD on the days you are long.

## 22. DVP comparison

Read-only vs frozen NQ DVP journal (`journal/phase29_nq_drift_vwap/trades.jsonl`, 5,714 trades).

- Calendar overlap with 20d-positive days is ~99.9% because DVP is active on almost every RTH day. That is **not** `DVP_DEPENDENT`.
- Daily P&L correlation: **−0.019**.
- Same-time overlap (≤900s): 0. Direction agree vs DVP mixed long/short: 0 (Phase 44 is long-only).
- Losing-day overlap: 248. Flag `dvp_dependent`: **False**.

Phase 44 is independent of DVP. It is also not a better book.

## 23. GC V2 comparison

Phase 26 paper journal has **N=0** forward trades. Overlap and correlation are undefined. No portfolio optimization was performed.

## 24. Prop geometry

Intraday pullback (the only prop-shaped expression):

| | ES | NQ |
|---|---:|---:|
| Avg / median / p95 stop | 6.80 / 5.75 / 14.25 pts | 34.5 / 29.8 / 73.3 pts |
| $ risk / contract (avg, p95) | $340 / $713 | $690 / $1,465 |
| Max consec losses | 10 | 11 |
| Worst day | −39.3 pts | −138.7 pts |
| Avg hold | ~17.6 min | ~17.5 min |
| Trades/day | 1 | 1 |
| Overnight | No | No |
| 08:30 blackout removed | 0 | 0 |
| Block-bootstrap E CI | [−1.20, −0.38] pts | [−2.78, +1.73] pts |

Stops are pullback-extreme, not DVP 80-pt. Geometry is tight and still loses. This is not a prop candidate.

## 25. Recommendation

`CLOSE_LONG_ONLY_AS_BETA_NOT_BOOK_3`

Identify **no** candidate. Do not write `strategy_candidates/phase44_*.json`. Do not freeze.

The research question was whether a simple long-only bullish-state filter identifies ES or NQ conditions with stable positive expectancy after costs, while avoiding the structurally weak short side. Answer:

1. Unconditional RTH long exposure has positive expectancy (index drift).
2. Prior 20d > 0 does **not** mark the good days. It marks the worse days. Off-state and especially `20d ≤ 0 + prior day down` is where the next RTH bounce lives.
3. The locked first 5m red/green pullback is negative on ES (all years) and flat-to-negative on NQ. It is not a rescue of a negative open-long baseline.
4. This is not Phase 40 minus shorts and not Phase 42 minus shorts. It is an independent rejection of the long-only state model.

Closed branches remain closed: two-sided TSMOM, HTF pullback, ORB, NQ sweep, post-news macro, small-cap gap-up. Strategy #3 is still not this family.
