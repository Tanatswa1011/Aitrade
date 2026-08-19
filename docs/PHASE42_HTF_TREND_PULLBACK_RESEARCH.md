# Phase 42 — Higher-timeframe trend + intraday pullback continuation

Research only. `DRY_RUN`. No broker. Nothing frozen.

Primary locked before P&L: `HTF_1H_TREND_FIRST_PULLBACK_5M_CONFIRM` — completed 1h return over 4 intervals at ±0.20%, first medium (40–60%) session-impulse pullback, first subsequent 5m continuation candle, next 5m open ±1 tick, stop at pullback extreme ±1 tick, 1R target, flatten 15:55, max 1 trade/day.

## 1. Verdict

- **Overall:** `HTF_PULLBACK_EDGE_WEAK`
- **ES_HTF_PULLBACK_STATUS:** `HTF_PULLBACK_EDGE_REJECTED`
- **NQ_HTF_PULLBACK_STATUS:** `HTF_PULLBACK_EDGE_WEAK`
- **Recommendation:** `DO_NOT_FREEZE`

## 2. Frozen integrity

Verified before and after. Frozen files were not modified.

- GC VWAP V2: `0695d7a881b6247033426bceb077b6895b96405b826c21f32a109a02f44b2d43`
- NQ DVP: `935e3a616351b09dbfa8d2d0e2b5d6850be803fb385ec5e5f8e3593856a1212a`
- File SHA GC: `12615d19ef3deed36b7929c161ef7c377975cd2317c0ae6d06f1949bd327046f`
- File SHA NQ: `34927fba0f268e56cf166cf8939bdfec6aa031510f7cee9acfab3909dd36b541`

## 3. Repository / data audit

- Reused Phase 38 ES/NQ 1m loaders, NY RTH session, holidays, flatten 15:55, 1-tick primary cost, TRAIN≤2024-12-31 / HOLDOUT≥2025-01-02, walk-forward years, frozen-hash checks, `resolve_path` AMBIGUOUS rule.
- 5m and 1h bars are NY-clock aggregations of stitched 1m. 4h bars use CME Globex 18:00 ET alignment.
- ES roll: Databento `.v.0`. NQ roll: AITRADE volume-crossover, activate 18:00 ET.
- VWAP is diagnostic only. This is not NQ DVP (DVP uses 15m hour-return AND session VWAP drift, 10:30 start, fixed 80-pt stop).
- News: 08:30 T±5m does not overlap RTH entries. No complete 10:00 calendar — not invented as a daily filter.

## 4. HTF trend definitions

- **1H:** last completed clock-hour close / close four hours earlier − 1. Bullish if ≥ +0.20%, bearish if ≤ −0.20%. Unfinished hour omitted.
- **4H:** last completed Globex 4h close / close three 4h intervals earlier − 1. Same ±0.20% threshold.
- Threshold was locked in `phase42_spec.json` before P&L. TRAIN |return| distribution is feasibility only.

- **ES:** n_1m=2338955 5m=468140 1h=39127 4h=10206 days=1651 2020-01-02→2026-08-14 TRAIN P(|1h ret|≥0.20%)=0.468 median |ret|=0.00185
  1h share={'NEUTRAL': 903, 'BEARISH': 365, 'BULLISH': 383} 4h share={'NEUTRAL': 702, 'BEARISH': 415, 'BULLISH': 534}
- **NQ:** n_1m=2338684 5m=467985 1h=39122 4h=10205 days=1651 2020-01-02→2026-08-14 TRAIN P(|1h ret|≥0.20%)=0.580 median |ret|=0.00243
  1h share={'NEUTRAL': 715, 'BEARISH': 450, 'BULLISH': 486} 4h share={'NEUTRAL': 530, 'BEARISH': 480, 'BULLISH': 641}

## 5. Structural trend persistence

| Instrument | N days | P(close up) uncond | P(close with 1h) | Long | Short | P(close with 4h) | 1h∩4h aligned | Flag |
|---|---:|---:|---:|---:|---:|---:|---:|---|
| ES | 1651 | 0.542 | 0.497 | 0.527 | 0.466 | 0.493 | 0.162 | False |
| NQ | 1651 | 0.544 | 0.494 | 0.527 | 0.458 | 0.519 | 0.224 | False |

## 6. Pullback depth

First 25–75% session-impulse pullback after a live 1h trend (completed bars; trend may turn on after 10:00). Continuation = a later session extreme in the trend direction. This is **not** a 1R win rate: it only says the day often makes another extreme after an already-expanded impulse. Shallow pullbacks have higher P(close with trend) than deep; there is no hump that favors the locked medium bucket.

- **ES:** n_events=692 P(continue extreme)=0.780 P(close with trend)=0.757 pullback_adds=True depth_continue=[{'bucket': 'deep', 'n': 71, 'rate': 0.7746478873239436}, {'bucket': 'medium', 'n': 223, 'rate': 0.7713004484304933}, {'bucket': 'shallow', 'n': 398, 'rate': 0.7864321608040201}] depth_close=[{'bucket': 'deep', 'n': 71, 'rate': 0.5915492957746479}, {'bucket': 'medium', 'n': 223, 'rate': 0.6995515695067265}, {'bucket': 'shallow', 'n': 398, 'rate': 0.8190954773869347}]
- **NQ:** n_events=553 P(continue extreme)=0.808 P(close with trend)=0.787 pullback_adds=True depth_continue=[{'bucket': 'deep', 'n': 74, 'rate': 0.7162162162162162}, {'bucket': 'medium', 'n': 181, 'rate': 0.8176795580110497}, {'bucket': 'shallow', 'n': 298, 'rate': 0.825503355704698}] depth_close=[{'bucket': 'deep', 'n': 74, 'rate': 0.6621621621621622}, {'bucket': 'medium', 'n': 181, 'rate': 0.7403314917127072}, {'bucket': 'shallow', 'n': 298, 'rate': 0.8456375838926175}]

## 7. Trend strength

- **ES:** [{'bucket': 'medium', 'n': 214, 'rate': 0.7710280373831776}, {'bucket': 'strong', 'n': 35, 'rate': 0.7142857142857143}, {'bucket': 'weak', 'n': 443, 'rate': 0.7900677200902935}]
- **NQ:** [{'bucket': 'medium', 'n': 227, 'rate': 0.7797356828193832}, {'bucket': 'strong', 'n': 81, 'rate': 0.8271604938271605}, {'bucket': 'weak', 'n': 245, 'rate': 0.8285714285714286}]

## 8. Trend age

- **ES:** [{'bucket': 'new_1', 'n': 692, 'rate': 0.7803468208092486}]
- **NQ:** [{'bucket': 'new_1', 'n': 553, 'rate': 0.8083182640144665}]
Age is measured at the first 5m when the 1h regime is live, so almost all events sit in `new_1`. That is a definition artifact, not evidence that mature trends were tested.

## 9. 1H candidate (PRIMARY)

| Instrument | N | E[R] | E[pts] | WR | PF | Train E[R] | Holdout E[R] | P(1R) | P(2R) | Status |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|
| ES | 1119 | -0.074 | -0.740 | 0.472 | 0.83 | -0.064 | -0.101 | 0.472 | 0.008 | `HTF_PULLBACK_EDGE_REJECTED` |
| NQ | 1179 | 0.021 | 0.780 | 0.515 | 1.04 | 0.011 | 0.051 | 0.514 | 0.007 | `HTF_PULLBACK_EDGE_WEAK` |

## 10. 4H candidate

- **ES `HTF_4H_TREND_FIRST_PULLBACK_5M_CONFIRM`:** N=818 E[R]=-0.017 WR=0.500 PF=0.89 holdout=0.050 long={'n': 440, 'win_rate': 0.525, 'expectancy_points': 0.07284090909090908, 'expectancy_r': 0.028389613271639233} short={'n': 378, 'win_rate': 0.4708994708994709, 'expectancy_points': -1.1467989417989417, 'expectancy_r': -0.06898190283099635}
- **NQ `HTF_4H_TREND_FIRST_PULLBACK_5M_CONFIRM`:** N=940 E[R]=0.045 WR=0.527 PF=1.10 holdout=0.069 long={'n': 499, 'win_rate': 0.5591182364729459, 'expectancy_points': 3.353607214428857, 'expectancy_r': 0.11268810139147627} short={'n': 441, 'win_rate': 0.4897959183673469, 'expectancy_points': -0.03219954648526159, 'expectancy_r': -0.03144088330289687}

## 11. Entry confirmation

- **ES:** candle E[R]=-0.074 N=1119; 5m break E[R]=-0.049 N=1104
- **NQ:** candle E[R]=0.021 N=1179; 5m break E[R]=0.033 N=1162

## 12. Target matrix

| Instrument | 0.5R | 1R | 1.5R | 2R | 3R |
|---|---:|---:|---:|---:|---:|
| ES | -0.063 | -0.074 | -0.060 | -0.020 | 0.008 |
| NQ | -0.016 | 0.021 | 0.065 | 0.027 | 0.034 |

## 13. Stop geometry

- **ES:** 0-tick E[R]=-0.070 1-tick=-0.074 2-tick=-0.061 avg stop=8.154464285714285 p95=18.75
- **NQ:** 0-tick E[R]=0.024 1-tick=0.021 2-tick=0.020 avg stop=37.46228813559322 p95=96.25

## 14. MFE / MAE

- **ES:** avg MFE=6.036 avg MAE=6.384 median MFE/MAE=0.784 P(reach 1R)=0.472 P(reach 2R)=0.008
- **NQ:** avg MFE=28.380 avg MAE=28.644 median MFE/MAE=1.153 P(reach 1R)=0.514 P(reach 2R)=0.007

## 15. Long / short

- **ES:** long={'n': 560, 'win_rate': 0.5071428571428571, 'expectancy_points': -0.2661607142857143, 'expectancy_r': -0.00759174199436162} short={'n': 559, 'win_rate': 0.4364937388193202, 'expectancy_points': -1.2150626118067978, 'expectancy_r': -0.14000062850935766}
- **NQ:** long={'n': 604, 'win_rate': 0.5463576158940397, 'expectancy_points': 3.0450331125827805, 'expectancy_r': 0.08644878476179231} short={'n': 575, 'win_rate': 0.4817391304347826, 'expectancy_points': -1.5986956521739137, 'expectancy_r': -0.04751860116094649}

## 16. ES / NQ

Not pooled. Statuses in section 1.

## 17. Cost stress

- **ES:** ideal E[R]=-0.013 1-tick=-0.074 2-tick=-0.109
- **NQ:** ideal E[R]=0.038 1-tick=0.021 2-tick=0.012

## 18. Train / holdout

- **ES:** train n=837 E[R]=-0.064; holdout n=282 E[R]=-0.101
- **NQ:** train n=881 E[R]=0.011; holdout n=298 E[R]=0.051

## 19. Walk-forward

| Instrument | Year | N | E[R] | WR | PF |
|---|---:|---:|---:|---:|---:|
| ES | 2020 | 179 | -0.162 | 0.430 | 0.66 |
| ES | 2021 | 163 | -0.152 | 0.436 | 0.74 |
| ES | 2022 | 176 | 0.021 | 0.517 | 1.02 |
| ES | 2023 | 159 | -0.087 | 0.472 | 0.86 |
| ES | 2024 | 160 | 0.063 | 0.537 | 1.36 |
| ES | 2025 | 172 | -0.051 | 0.483 | 0.66 |
| ES | 2026 | 110 | -0.180 | 0.409 | 0.76 |
| NQ | 2020 | 184 | 0.001 | 0.505 | 0.80 |
| NQ | 2021 | 174 | 0.112 | 0.563 | 1.25 |
| NQ | 2022 | 173 | 0.050 | 0.532 | 1.17 |
| NQ | 2023 | 177 | -0.004 | 0.503 | 1.00 |
| NQ | 2024 | 173 | -0.104 | 0.451 | 0.82 |
| NQ | 2025 | 183 | 0.028 | 0.519 | 1.10 |
| NQ | 2026 | 115 | 0.088 | 0.539 | 1.19 |

## 20. Threshold stability

- **ES:** stable=True neighbors={'thresh_0.18pct': {'n': 1140, 'E[R]': -0.07901183486749198}, 'thresh_0.22pct': {'n': 1081, 'E[R]': -0.06521334498050194}, 'depth_35_55': {'n': 1189, 'E[R]': -0.03906908232968943}, 'depth_45_65': {'n': 1036, 'E[R]': -0.050895951436366146}}
- **NQ:** stable=True neighbors={'thresh_0.18pct': {'n': 1185, 'E[R]': 0.01757958638434562}, 'thresh_0.22pct': {'n': 1165, 'E[R]': 0.007446645153250754}, 'depth_35_55': {'n': 1259, 'E[R]': 0.0010782338926702479}, 'depth_45_65': {'n': 1064, 'E[R]': 0.02595035960198991}}

## 21. 1H / 4H alignment

- **ES:** share of primary trades with 1h∩4h agreement=0.475. Dual-timeframe was not used as a primary filter.
- **NQ:** share of primary trades with 1h∩4h agreement=0.553. Dual-timeframe was not used as a primary filter.

## 22. DVP similarity

- NQ vs frozen DVP: {'overlap_days': 1179, 'n_htf_days': 1179, 'n_dvp_days': 1690, 'n_days_for_corr': 1179, 'daily_pnl_correlation': 0.16931019652260262, 'same_time_overlap_15m': 475, 'direction_agree_rate': 0.7742892459826947, 'losing_day_overlap': 292, 'note': 'Read-only vs Phase 29 NQ DVP. No combination. VWAP is diagnostic only.'}
- NQ VWAP-aligned split (diagnostic): {'aligned': {'n_entered': 1131, 'n_resolved': 1130, 'n_ambiguous': 1, 'win_rate': 0.5106194690265486, 'expectancy_points': 0.713274336283185, 'expectancy_r': 0.012917160218216947, 'profit_factor': 1.038577790966766, 'max_dd_points': 1259.400000000001, 'max_consec_losses': 9, 'avg_stop_points': 38.28713527851459, 'median_stop_points': 30.75, 'p95_stop_points': 98.0, 'avg_mfe': 28.911061946902656, 'avg_mae': 29.235840707964602, 'p_reach_1r': 0.5092838196286472, 'p_reach_2r': 0.00618921308576481, 'median_mfe_mae': 1.0802377414561664, 'avg_hold_sec': 1510.9380530973451, 'n_days': 1130, 'worst_day_points': -191.95, 'long': {'n': 576, 'win_rate': 0.5434027777777778, 'expectancy_points': 3.050434027777777, 'expectancy_r': 0.08088332977336615}, 'short': {'n': 554, 'win_rate': 0.47653429602888087, 'expectancy_points': -1.7166967509025277, 'expectancy_r': -0.057748026900494145}, 'use_cost': True, 'vwap_aligned_share': 1.0, 'tf_aligned_share': 0.5473032714412025}, 'not_aligned': {'n_entered': 49, 'n_resolved': 49, 'n_ambiguous': 0, 'win_rate': 0.6122448979591837, 'expectancy_points': 2.325510204081633, 'expectancy_r': 0.2101118220814932, 'profit_factor': 1.289727943046021, 'max_dd_points': 122.80000000000004, 'max_consec_losses': 4, 'avg_stop_points': 18.4234693877551, 'median_stop_points': 16.75, 'p95_stop_points': 40.5, 'avg_mfe': 16.142857142857142, 'avg_mae': 14.98469387755102, 'p_reach_1r': 0.6122448979591837, 'p_reach_2r': 0.02040816326530612, 'median_mfe_mae': 1.8202479338842976, 'avg_hold_sec': 407.7551020408163, 'n_days': 49, 'worst_day_points': -52.7, 'long': {'n': 28, 'win_rate': 0.6071428571428571, 'expectancy_points': 2.933928571428572, 'expectancy_r': 0.20093814452370168}, 'short': {'n': 21, 'win_rate': 0.6190476190476191, 'expectancy_points': 1.5142857142857145, 'expectancy_r': 0.2223433921585486}, 'use_cost': True, 'vwap_aligned_share': 0.0, 'tf_aligned_share': 0.6938775510204082}}
- dvp_clone_flag=False
- GC VWAP V2 paper journal has no comparable historical daily series here.

## 23. Prop geometry

- **ES:** {'avg_stop_points': 8.154464285714285, 'median_stop_points': 6.25, 'p95_stop_points': 18.75, 'avg_usd_risk': 407.7232142857143, 'p95_usd_risk': 937.5, 'max_consec_losses': 10, 'worst_day_points': -208.08, 'avg_hold_sec': 1545.14745308311, 'max_trades_per_day': 1, 'flatten': '15:55', 'overnight': False}
- **NQ:** {'avg_stop_points': 37.46228813559322, 'median_stop_points': 30.0, 'p95_stop_points': 96.25, 'avg_usd_risk': 749.2457627118644, 'p95_usd_risk': 1925.0, 'max_consec_losses': 9, 'worst_day_points': -191.95, 'avg_hold_sec': 1465.089058524173, 'max_trades_per_day': 1, 'flatten': '15:55', 'overnight': False}

## 24. Recommendation

NQ 1h first-pullback is barely positive after 1-tick costs (E[R]≈+0.02, PF≈1.04) with 2023–2024 losing years, large stops, and shorts that do not work. ES is negative. Overnight 1h direction does not predict the RTH close. Do not freeze. Do not expand confirmation, VWAP, EMA, or dual-TF as a rescue. This is not Book 3.

ATR pullback diagnostic (not a family):
- **ES 0.5×ATR** E[R]=-0.068 N=1548
- **ES 1.0×ATR** E[R]=-0.079 N=1543
- **NQ 0.5×ATR** E[R]=0.008 N=1584
- **NQ 1.0×ATR** E[R]=0.013 N=1583

Time-of-day (primary):
- **ES 0930_1030:** N=438 E[R]=-0.032
- **ES 1030_1200:** N=348 E[R]=-0.110
- **ES 1200_1400:** N=254 E[R]=-0.098
- **ES 1400_1530:** N=79 E[R]=-0.065
- **NQ 0930_1030:** N=530 E[R]=0.043
- **NQ 1030_1200:** N=363 E[R]=-0.020
- **NQ 1200_1400:** N=219 E[R]=-0.006
- **NQ 1400_1530:** N=67 E[R]=0.161

Execution remained `DRY_RUN`. `strategy_frozen/` was not written.
No candidate JSON.
