# Phase 41 — Volume profile / auction market structure

Research only. `DRY_RUN`. No broker. Nothing frozen.

**Profile source: `DEGRADED_1M_VOLUME_PROFILE`.** 1m bar volume is spread uniformly across each bar's tick range. This is not a trade-print volume profile. Databento `trades` is ~$0.30 per NQ RTH day; 2020–2026 ES+NQ is not economically feasible here.

## 1. Verdict

- **Overall:** `VOLUME_PROFILE_STRUCTURAL_EFFECT_ONLY`
- **ES_VOLUME_PROFILE_STATUS:** `VOLUME_PROFILE_EDGE_REJECTED`
- **NQ_VOLUME_PROFILE_STATUS:** `VOLUME_PROFILE_STRUCTURAL_EFFECT_ONLY`
- **Recommendation:** `DO_NOT_FORCE_TRADE_STRUCTURAL_ONLY`

Primary locked before P&L: `VP_OUTSIDE_ACCEPT_POC` (open outside prior 70% value, 1m close inside, next open ±1 tick toward POC, stop = outside excursion extreme ±1 tick, flatten 15:55).

## 2. Frozen integrity

Verified before and after. Frozen files were not modified.

- GC VWAP V2: `0695d7a881b6247033426bceb077b6895b96405b826c21f32a109a02f44b2d43`
- NQ DVP: `935e3a616351b09dbfa8d2d0e2b5d6850be803fb385ec5e5f8e3593856a1212a`
- File SHA GC: `12615d19ef3deed36b7929c161ef7c377975cd2317c0ae6d06f1949bd327046f`
- File SHA NQ: `34927fba0f268e56cf166cf8939bdfec6aa031510f7cee9acfab3909dd36b541`

## 3. Repository / data audit

- Reused Phase 38 ES/NQ 1m RTH session handling, holidays, flatten 15:55, cost ticks, chronological split, walk-forward years, frozen-hash checks.
- No MBO/MBP/DOM. No ORB/sweep/TSMOM logic.
- Trade-tape reconstruction skipped (cost). Result is labeled degraded.

## 4. Dataset

- **ES:** n_bars=2338955 roll=databento_ES.v.0 next-days=1650 profiles=1651
- **NQ:** n_bars=2338684 roll=aitrade_volume_crossover next-days=1650 profiles=1651

Coverage: valid RTH days 2020-01-02 → 2026-08-14. TRAIN ≤ 2024-12-31. HOLDOUT ≥ 2025-01-02.

## 5. POC / VAH / VAL construction

- Tick bucket: 0.25.
- Volume: each 1m bar's volume split equally across ticks from low to high.
- POC: max-volume tick; ties → closest to session VWAP of typical price; remaining ties → lower tick.
- Value area: 70%. Single-row expansion from POC; equal adjacent volume expands the lower-price side first.
- Profile for day T uses only the prior completed RTH session.

## 6. Open-location frequencies

- **ES:** {'OPEN_BELOW_VAL': 0.2684848484848485, 'OPEN_INSIDE_VALUE': 0.3387878787878788, 'OPEN_ABOVE_VAH': 0.3927272727272727} counts={'OPEN_BELOW_VAL': 443, 'OPEN_INSIDE_VALUE': 559, 'OPEN_ABOVE_VAH': 648}
- **NQ:** {'OPEN_BELOW_VAL': 0.27151515151515154, 'OPEN_ABOVE_VAH': 0.3903030303030303, 'OPEN_INSIDE_VALUE': 0.3381818181818182} counts={'OPEN_BELOW_VAL': 448, 'OPEN_ABOVE_VAH': 644, 'OPEN_INSIDE_VALUE': 558}

## 7. Structural transition probabilities

| Instrument | Open class | N | P(enter value) | P(touch POC) | P(traverse) | P(continue away) | P(return VAH) | P(return VAL) |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| ES | OPEN_ABOVE_VAH | 648 | 0.586 | 0.417 | 0.273 | 0.412 | 0.588 | 0.273 |
| ES | OPEN_INSIDE_VALUE | 559 | 1.000 | 0.809 | 0.372 | 0.000 | 0.698 | 0.640 |
| ES | OPEN_BELOW_VAL | 443 | 0.594 | 0.375 | 0.255 | 0.402 | 0.255 | 0.598 |
| NQ | OPEN_ABOVE_VAH | 644 | 0.632 | 0.460 | 0.278 | 0.368 | 0.632 | 0.278 |
| NQ | OPEN_INSIDE_VALUE | 558 | 1.000 | 0.792 | 0.360 | 0.000 | 0.708 | 0.629 |
| NQ | OPEN_BELOW_VAL | 448 | 0.616 | 0.406 | 0.252 | 0.384 | 0.252 | 0.616 |

## 8. Acceptance

| Instrument | Class | P(1m close inside) | P(two 1m) | P(5m) |
|---|---|---:|---:|---:|
| ES | OPEN_ABOVE_VAH | 0.559 | 0.535 | 0.525 |
| ES | OPEN_BELOW_VAL | 0.587 | 0.571 | 0.569 |
| ES | OPEN_INSIDE_VALUE | 0.991 | 0.980 | 0.971 |
| NQ | OPEN_ABOVE_VAH | 0.601 | 0.579 | 0.565 |
| NQ | OPEN_BELOW_VAL | 0.585 | 0.576 | 0.578 |
| NQ | OPEN_INSIDE_VALUE | 0.980 | 0.970 | 0.941 |

## 9. Rejection

| Instrument | Class | P(1m reject) |
|---|---|---:|
| ES | OPEN_ABOVE_VAH | 0.350 |
| ES | OPEN_BELOW_VAL | 0.318 |
| NQ | OPEN_ABOVE_VAH | 0.328 |
| NQ | OPEN_BELOW_VAL | 0.315 |

## 10. Outside-value rejection candidate (`VP_OUTSIDE_REJECT_1R`)

- **ES:** entered=320 resolved=309 E[R]=-0.072 E[pts]=-1.250 hit=0.453 PF=0.80 train=-0.063 holdout=-0.102 long={'n': 112, 'win_rate': 0.4732142857142857, 'expectancy_points': -0.17598214285714248, 'expectancy_r': -0.04930677823669233} short={'n': 197, 'win_rate': 0.4416243654822335, 'expectancy_points': -1.860456852791878, 'expectancy_r': -0.08456923918891082}
- **NQ:** entered=202 resolved=179 E[R]=0.060 E[pts]=4.746 hit=0.531 PF=1.30 train=0.090 holdout=-0.066 long={'n': 67, 'win_rate': 0.47761194029850745, 'expectancy_points': 0.5574626865671631, 'expectancy_r': -0.06806116660620982} short={'n': 112, 'win_rate': 0.5625, 'expectancy_points': 7.250892857142856, 'expectancy_r': 0.1369468857668529}

## 11. Outside-value acceptance candidate (`VP_OUTSIDE_ACCEPT_POC`) — PRIMARY

| Instrument | N | E[R] | E[pts] | Hit | PF | Train E[R] | Holdout E[R] | 1R E[R] | Opposite E[R] | Status |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|
| ES | 498 | -0.033 | -0.149 | 0.588 | 0.97 | -0.023 | -0.072 | 0.046 | 0.057 | `VOLUME_PROFILE_EDGE_REJECTED` |
| NQ | 292 | -0.010 | 1.402 | 0.575 | 1.09 | -0.052 | 0.251 | -0.017 | 0.031 | `VOLUME_PROFILE_STRUCTURAL_EFFECT_ONLY` |

## 12. Inside-value rotation candidate (`VP_INSIDE_ROTATE_POC`)

- **ES:** entered=536 resolved=528 E[R]=-0.112 hit=0.496 PF=1.07 train=-0.115 holdout=-0.105 long={'n': 270, 'win_rate': 0.5481481481481482, 'expectancy_points': 1.282962962962963, 'expectancy_r': 0.02151901646137215} short={'n': 258, 'win_rate': 0.4418604651162791, 'expectancy_points': -0.8813565891472868, 'expectancy_r': -0.25210791334968125}
- **NQ:** entered=453 resolved=443 E[R]=-0.101 hit=0.485 PF=1.11 train=-0.092 holdout=-0.134 long={'n': 224, 'win_rate': 0.5133928571428571, 'expectancy_points': 4.548883928571429, 'expectancy_r': 0.05813732674544782} short={'n': 219, 'win_rate': 0.45662100456621, 'expectancy_points': -2.3563926940639273, 'expectancy_r': -0.26406280890664285}

## 13. POC analysis

- **ES:** {'P_touch_poc_open_above': 0.4166666666666667, 'P_touch_poc_open_below': 0.3747178329571106, 'P_touch_poc_open_inside': 0.8085867620751341}
- **NQ:** {'P_touch_poc_open_above': 0.45962732919254656, 'P_touch_poc_open_below': 0.40625, 'P_touch_poc_open_inside': 0.7921146953405018}

## 14. Value-width analysis

Outside-value days, width/ATR quintiles vs P(enter) / P(continue away).

- **ES Q1:** n=217 mean_w/ATR=0.216 P(enter)=0.631 P(away)=0.369 P(POC)=0.530
- **ES Q2:** n=218 mean_w/ATR=0.319 P(enter)=0.601 P(away)=0.399 P(POC)=0.450
- **ES Q3:** n=217 mean_w/ATR=0.425 P(enter)=0.576 P(away)=0.415 P(POC)=0.369
- **ES Q4:** n=218 mean_w/ATR=0.569 P(enter)=0.550 P(away)=0.445 P(POC)=0.312
- **ES Q5:** n=218 mean_w/ATR=0.929 P(enter)=0.583 P(away)=0.417 P(POC)=0.339
- **NQ Q1:** n=217 mean_w/ATR=0.221 P(enter)=0.673 P(away)=0.327 P(POC)=0.539
- **NQ Q2:** n=218 mean_w/ATR=0.322 P(enter)=0.596 P(away)=0.404 P(POC)=0.459
- **NQ Q3:** n=217 mean_w/ATR=0.420 P(enter)=0.627 P(away)=0.373 P(POC)=0.479
- **NQ Q4:** n=218 mean_w/ATR=0.554 P(enter)=0.638 P(away)=0.362 P(POC)=0.413
- **NQ Q5:** n=218 mean_w/ATR=0.884 P(enter)=0.587 P(away)=0.413 P(POC)=0.298

## 15. Gap / overnight context

Gap and overnight-inside-value are recorded on `reports/phase41_*_structural.csv`. They are diagnostics, not rules.

## 16. Long / short

See candidate sections. Sides are not pooled.

## 17. ES / NQ

Not pooled. Statuses in section 1.

## 18. Cost stress

- **ES primary:** ideal E[R]=-0.004 1-tick=-0.033 2-tick=-0.054
- **NQ primary:** ideal E[R]=0.000 1-tick=-0.010 2-tick=-0.019

## 19. Train / holdout

- **ES:** train n=389 E[R]=-0.023; holdout n=109 E[R]=-0.072
- **NQ:** train n=252 E[R]=-0.052; holdout n=40 E[R]=0.251

## 20. Walk-forward

| Instrument | Year | N | E[R] | Hit |
|---|---:|---:|---:|---:|
| ES | 2020 | 74 | 0.034 | 0.649 |
| ES | 2021 | 79 | 0.047 | 0.646 |
| ES | 2022 | 61 | -0.012 | 0.557 |
| ES | 2023 | 95 | -0.145 | 0.537 |
| ES | 2024 | 80 | -0.006 | 0.575 |
| ES | 2025 | 69 | -0.083 | 0.551 |
| ES | 2026 | 40 | -0.054 | 0.625 |
| NQ | 2020 | 63 | 0.090 | 0.603 |
| NQ | 2021 | 66 | 0.004 | 0.636 |
| NQ | 2022 | 26 | -0.068 | 0.500 |
| NQ | 2023 | 51 | -0.325 | 0.392 |
| NQ | 2024 | 46 | -0.012 | 0.630 |
| NQ | 2025 | 29 | 0.203 | 0.690 |
| NQ | 2026 | 11 | 0.377 | 0.545 |

## 21. Portfolio relationship

- NQ vs frozen DVP: {'overlap_days': 292, 'n_days_for_corr': 292, 'daily_pnl_correlation': 0.10405403951289367, 'note': 'Read-only vs Phase 29 NQ DVP. No combination.'}
- GC VWAP V2 journal has no comparable historical daily series in this repo.

## 22. Prop geometry

- **ES:** {'avg_stop_points': 16.81777108433735, 'p95_stop_points': 35.0, 'avg_usd_risk': 840.8885542168674, 'max_consec_losses': 5, 'flatten': '15:55', 'overnight': False}
- **NQ:** {'avg_stop_points': 46.23287671232877, 'p95_stop_points': 76.75, 'avg_usd_risk': 924.6575342465753, 'max_consec_losses': 5, 'flatten': '15:55', 'overnight': False}

## 23. Recommendation

Prior value **slightly** changes next-session location: outside opens re-enter value about 59–63% of the time (not a magnet; 37–41% never even touch the boundary). Prior POC is touched on only ~40% of outside-value days. The locked acceptance-to-POC trade, the rejection 1R trade, and inside rotation all fail cost-adjusted expectancy on ES; NQ acceptance is ~0 R in-sample with a 40-trade holdout bounce. Stops are large (NQ avg 46 pts / ~$925 per contract).

Do not freeze. Do not add HVN/LVN, 68/75% value-area search, or Phase 42 filters. **Do not make this Strategy #3.** A later tape-based profile would be a new data project, not a rescue of these geometries.

Execution remained `DRY_RUN`. `strategy_frozen/` was not written.
No candidate JSON.
