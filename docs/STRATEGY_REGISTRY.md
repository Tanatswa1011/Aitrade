# Strategy registry

Evidence-preserving record of AITRADE strategies. Frozen configs must not be edited without a new formal freeze phase.

## ACTIVE / FROZEN

### GC VWAP V2

| Field | Value |
|-------|-------|
| Market | GC (COMEX gold) |
| Strategy | VWAP mean reversion — band reclaim + 2σ retest |
| Candidate | `V2_BAND_RECLAIM_2SIG_RETEST` |
| Frozen version | `gc_vwap_mean_reversion_v1.V2.FROZEN_PHASE26` |
| Frozen hash | `0695d7a881b6247033426bceb077b6895b96405b826c21f32a109a02f44b2d43` |
| Frozen files | `strategy_frozen/gc_vwap_v2_phase26.json`, `.md` |
| Session | NY RTH VWAP reset 09:30 ET |
| Phase 25 benchmark | Holdout E[2R] ≈ 0.345; walk-forward all blocks E[2R] positive; cost survives 1–2 ticks |
| Phase 26 status | Frozen; paper validation in progress (N=0 resolved forward trades) |
| Execution plan | MGC micro (sizing via dynamic stop — see `config/execution_metadata.json`) |
| Journal | `journal/phase26_gc_vwap_v2_paper/` |

### NQ Drift VWAP Pullback

| Field | Value |
|-------|-------|
| Signal market | NQ (Databento GLBX.MDP3 stitched) |
| Execution plan | MNQ Sim101 (`MNQ SEP26`, qty=1) |
| Frozen version | `nq_drift_vwap_pullback_v1.DVP_ORIGINAL.FROZEN_PHASE30` |
| Frozen hash | `935e3a616351b09dbfa8d2d0e2b5d6850be803fb385ec5e5f8e3593856a1212a` |
| Frozen files | `strategy_frozen/nq_dvp_phase30.json`, `.md` |
| Session | 09:30 VWAP; trade 10:30–15:30; flatten 15:55 ET |
| Brackets | Long SL80/TP40; Short SL80/TP50 (index points) |
| Guardrails | Max 4 trades/day; stop after 2 losses; one position at a time |
| Phase 29 benchmark | Full sample 5714 trades; OOS expectancy ~5.95 pts/trade; edge observed |
| Phase 30 status | Frozen; paper validation (N=0 forward) |
| Phase 31 status | NT Sim101 bridge integrated; DRY_RUN default; hist/live equivalence 15/15 |
| Journal | `journal/phase30_nq_dvp_paper/`, `journal/phase31_nq_dvp_sim/` |

### ES DVP locked candidate (Phase 47) — not frozen

| Field | Value |
|-------|-------|
| Market | ES (CME E-mini S&P 500). MES is sizing reference only |
| Family | `nq_drift_vwap_pullback_v1` port (`ES_DVP_PORT`) |
| Status | `LOCKED_FORWARD_VALIDATION_CANDIDATE` — **not** `strategy_frozen/` |
| Version | `es_dvp_v1.PORT.LOCKED_PHASE47` |
| Config hash | `28d12b8f3a6631b8c6526d6c300244396c0c2ba5628a2d5baa143f5489f4b3c4` |
| Source | `strategy_candidates/phase46_ES_DVP.json` |
| Locked file | `strategy_candidates/phase47_ES_DVP_LOCKED_CANDIDATE.json` |
| Session | 09:30 VWAP; trade 10:30–15:30; flatten 15:55 ET |
| Brackets | Long SL18/TP9; Short SL18/TP11.25 (TRAIN ATR scale 0.22547932918744962) |
| Guardrails | Max 4/day; stop after 2 losses; one position |
| News | T−5m→T+5m around 08:30 ET (does not overlap RTH entries) |
| Phase 46 | TRAIN E[R]+0.019 N=2511; HOLDOUT +0.038 N=3063; full +0.030 N=5574; corr vs NQ DVP 0.60 |
| Journal | `journal/phase47_es_dvp_paper/` (empty until genuine forward fills) |
| Execution | `DRY_RUN_ONLY` `NOT_PRODUCTION` — no broker |

## RESEARCH-ONLY / RETIRED

### Operations console (Phase 54)

| Field | Value |
|-------|--------|
| Phase | 54 |
| Status | Phase 54F live NT market-data heartbeat + FundedNext MCP read-only account/risk. Custom Tradovate path deprecated. `PROP_EXECUTION=false`. |
| Question | Can operators observe engine / NT market / FundedNext (not Sim101) state without a path that sends real eval orders? |
| Forbidden | Enable broker execution; send FundedNext orders; assign GC to the eval account; redesign frozen strategy logic |
| Evidence | `docs/PHASE54_OPS_CONSOLE.md`, `dashboard/ops-console/`, `phase54_ops.py`, `tests_phase54.py` |
| Frozen impact | None. DRY_RUN only. |

### Pre-evaluation shadow validation (Phase 53)

| Field | Value |
|-------|--------|
| Phase | 53 |
| Status | See `phase53_validation.json` verdict (`READY_TO_PURCHASE_EVALUATION`) |
| Question | Does the frozen NQ → Phase 52 policy → simulated-order pipeline behave correctly on the most recent NQ sequencing, enough to justify buying one FundedNext Flex 50K evaluation? |
| Forbidden | Retune GC/NQ; enable broker; purchase/connect eval; overwrite Phase 49–52 reports |
| Evidence | `docs/PHASE53_PRE_EVALUATION_SHADOW_VALIDATION.md`, `reports/phase53_*`, `phase53_validation.json`, `tests_phase53.py` |
| Frozen impact | None. DRY_RUN only. |


### Prop execution policy layer (Phase 52)

| Field | Value |
|-------|--------|
| Phase | 52 |
| Status | See `phase52_validation.json` verdict (`PROP_EXECUTION_POLICY_LOCKED`) |
| Question | Can AITRADE deterministically translate frozen NQ DVP into a FundedNext Flex 50K-safe evaluation policy? |
| Forbidden | Retune GC/NQ/ES; freeze ES; enable broker; live execution; buy/activate accounts; overwrite Phase 49/49B/50/51 reports |
| Evidence | `docs/PHASE52_PROP_EXECUTION_POLICY_LOCK.md`, `reports/phase52_*`, `phase52_validation.json`, `tests_phase52.py` |
| Frozen impact | None. Phase 26 / 30 hashes unchanged. DRY_RUN only. `risk_per_trade` is now PROP_CONTRACT_QTY (2 MNQ FAST / 1 MNQ SAFE), not 1% of $50k. |


### Fast-pass evaluation optimization (Phase 49B)

| Field | Value |
|-------|--------|
| Phase | 49B |
| Status | See `phase49b_validation.json` verdict (`PHASE49B_FAST_PASS_RESEARCH_READY`) |
| Question | Can evaluation pass time be reduced (target 10–14 trading days) while preserving pass probability and improving Phase 51 replication speed? |
| Forbidden | Retune GC/NQ/ES; freeze ES; enable broker; write production risk; overwrite Phase 49/50/51 reports; martingale |
| Evidence | `docs/PHASE49B_FAST_PASS_EVALUATION.md`, `reports/phase49b_*`, `phase49b_validation.json`, `tests_phase49b.py` |
| Frozen impact | None. Phase 26 / 30 hashes unchanged. DRY_RUN only. EVAL_ACCELERATE added to account-state scaffold as research state. |

### Prop account replication flywheel (Phase 51)

| Field | Value |
|-------|--------|
| Phase | 51 |
| Status | See `phase51_validation.json` verdict |
| Question | Can payouts from funded NQ (and secondary ES) accounts finance additional evaluations into a self-funding prop-capital flywheel? |
| Forbidden | Retune GC/NQ/ES; freeze ES; enable broker; write production risk/payout; buy/activate accounts; invent MFFU price or FundedNext funded-account cap |
| Evidence | `docs/PHASE51_PROP_ACCOUNT_REPLICATION_FLYWHEEL.md`, `reports/phase51_*`, `phase51_validation.json`, `tests_phase51.py` |
| Frozen impact | None. Phase 26 / 30 hashes unchanged. DRY_RUN only. |

### Funded survival, reserve & payout policy (Phase 50)

| Field | Value |
|-------|--------|
| Phase | 50 |
| Status | See `phase50_validation.json` verdict |
| Question | Can a funded-account reserve, payout, and cushion-dependent risk policy produce repeated payouts without near-certain long-horizon ruin? |
| Forbidden | Retune GC/NQ/ES; freeze ES; enable broker; write production risk/payout into operating policy; martingale |
| Evidence | `docs/PHASE50_FUNDED_SURVIVAL_PAYOUT_POLICY.md`, `reports/phase50_*`, `phase50_validation.json`, `tests_phase50.py` |
| Frozen impact | None. Phase 26 / 30 hashes unchanged. DRY_RUN only. |

### Strategy distribution + prop risk simulation (Phase 49)

| Field | Value |
|-------|--------|
| Phase | 49 |
| Status | See `phase49_validation.json` verdict |
| Question | Given frozen/locked strategy trade distributions, which PROP_RULES_V1 risk fractions and state policies are mathematically suitable for MFFU Rapid EOD 50K and FundedNext Flex 50K (eval vs funded)? |
| Forbidden | Retune GC/NQ/ES; freeze ES; enable broker; write production risk into operating policy; martingale |
| Evidence | `docs/PHASE49_STRATEGY_DISTRIBUTION_PROP_SIM.md`, `reports/phase49_*`, `phase49_validation.json`, `tests_phase49.py` |
| Frozen impact | None. Phase 26 / 30 hashes unchanged. DRY_RUN only. |

### Prop Rule Engine V1 (Phase 48)

| Field | Value |
|-------|--------|
| Phase | 48 |
| Status | `PROP_RULE_ENGINE_V1_READY`. Compliance layer only. |
| Primary profiles | `MFFU_RAPID_EOD_50K`, `FUNDEDNEXT_FLEX_50K` |
| Source | `config/PROP_RULES_V1.json` |
| API | `prop_rule_engine.evaluate_trade` |
| Question | Can the existing strategy engine operate under named prop-firm rules without embedding those rules in strategy code? |
| Forbidden | Retune GC/NQ/ES; invent unstated firm rules; choose risk-per-trade; enable broker execution |
| Evidence | `docs/PHASE48_PROP_RULE_ENGINE.md`, `phase48_validation.json`, `tests_phase48.py` |
| Frozen impact | None. Phase 26 / 30 hashes unchanged. DRY_RUN only. |


### ES DVP forward lock + multi-book paper (Phase 47)

| Field | Value |
|-------|--------|
| Phase | 47 |
| Status | `ES_DVP_FORWARD_VALIDATION_READY` / `MULTI_BOOK_FORWARD_VALIDATION_READY`. ES is locked, not frozen. |
| Question | Do locked/frozen rules continue to behave after research ends and no further tuning is allowed? |
| Forbidden | Retune ES/NQ/GC; add RTY/YM/CL/FVG/ORB/TSMOM; replay history into forward journals; broker execution |
| Evidence | `docs/PHASE47_ES_DVP_FORWARD_VALIDATION.md`, `phase47_validation.json`, `reports/phase47_*.csv` |
| Frozen impact | None. Phase 26 / 30 hashes unchanged. |

### Strategy family portability — ES/CL (Phase 46)

| Field | Value |
|-------|--------|
| Family | Port of `gc_vwap_mean_reversion_v1` and `nq_drift_vwap_pullback_v1` |
| Phase | 46 |
| Status | Research-only — `STRATEGY_FAMILY_PORTABILITY_CONFIRMED`. ES DVP `PORTABLE_EDGE_FOUND`. ES/CL VWAP MR and CL DVP `REJECTED`. |
| Question | Do the two surviving mechanisms generalize to ES and CL without a parameter search? |
| Predeclared | Sessions locked (ES RTH; CL 09:00–14:30 energy window). DVP distances = NQ 80/40/50 × TRAIN median session-ATR14. MR remains 2σ reclaim/retest. Locked in `phase46_spec.json`. |
| Gate | Structural stats before P&L. TRAIN through 2022-12-30. 1-tick primary. Redundancy if daily P&L corr ≥ 0.70 vs a frozen book. |
| Verdict | DVP ports to ES: N=5574, E[R]=+0.030, holdout +0.038, 2-tick +0.002, corr vs NQ DVP 0.60. VWAP MR does not port (ES continue-after-reclaim 0.79). CL DVP WR 0.63 but E[R]−0.021 after costs. Candidate `ES_DVP_PORT` only. **Not frozen.** Forward paper N still 0/0. |
| Evidence | `docs/PHASE46_STRATEGY_FAMILY_PORTABILITY.md`, `phase46_validation.json`, `reports/phase46_*.csv`, `strategy_candidates/phase46_ES_DVP.json` |
| Frozen impact | None. Phase 26 / 30 hashes unchanged. DRY_RUN only. CL.v.0 1m 2020–2026 downloaded this phase ($8.46). |

### TG Capital London 30m FVG (Phase 45) — not a Book 3

| Field | Value |
|-------|--------|
| Family | `tg_capital_london_30m_v1` |
| Phase | 45 |
| Status | Research-only — `TG_LONDON_EDGE_WEAK` (GC `WEAK`, NQ `REJECTED`) |
| Question | Do trend-aligned London 30m FVGs, after a 50% retrace and an objective reaction candle, produce cost-adjusted continuation expectancy at 2R–5R? |
| Predeclared | `TG_GC_30M_LONDON_FVG50_REACTION`: London 07:00–11:00 Europe/London, 4H EMA200, 30m stack, 3-candle FVG, midpoint, trident approximation, next 30m open, FVG-boundary stop, 2R, flatten 15:55 ET. Locked in `phase45_spec.json`. Trident/stack/window labeled `MECHANIZED_APPROXIMATION` (no TG source in-repo). |
| Gate | Structural FVG stats before trades. Always-on 1-tick costs. TRAIN through 2022-12-30. GC primary; NQ portability only. |
| Verdict | Aligned FVGs **fill** (GC P(fill)=0.81) rather than continue. Trident+2R GC E[R]=+0.101 / holdout −0.058 / 2-tick −0.039. Doji and simple close worse. NQ E[R]=−0.179. Not a V2 clone (London vs NY VWAP). No freeze. No candidate. |
| Evidence | `docs/PHASE45_TG_CAPITAL_LONDON_30M_RESEARCH.md`, `phase45_validation.json`, `reports/phase45_*.csv` |
| Frozen impact | None. Phase 26 / 30 hashes unchanged. DRY_RUN only. GC.v.0 1m 2020–2026 downloaded this phase ($8.45). |

### ES/NQ long-only index drift / pullback (Phase 44) — not a Book 3

| Field | Value |
|-------|--------|
| Family | `long_only_index_drift_v1` |
| Phase | 44 |
| Status | Research-only — `LONG_DRIFT_BETA_ONLY` (ES `LONG_ONLY_EDGE_REJECTED`, NQ `LONG_DRIFT_BETA_ONLY`) |
| Question | Does a simple long-only 20d-positive state identify ES/NQ conditions with stable RTH long expectancy after costs, vs always-long, without ever shorting? |
| Predeclared | `LONG_STATE_20D_POSITIVE`; baseline `BULL_STATE_RTH_OPEN_LONG`; candidate `LONG20_FIRST_RED_GREEN_5M`. Locked in `phase44_spec.json` before P&L. No VWAP. No shorts. |
| Gate | Independent of Phase 40/42. Always-long RTH required. If the filter does not beat buying every day → `LONG_DRIFT_BETA_ONLY`. |
| Verdict | 20d>0 marks the **worse** next RTH day (ES on −0.29 vs off +1.73; NQ on +0.61 vs off +5.41). Always-long is the beta. Pullback E[R] ES −0.126 all years negative; NQ −0.006. Best 2×2 cell is non-positive 20d + down day (bounce), not uptrend+dip. No freeze. No candidate. Do not add shorts. Do not reopen TSMOM. |
| Evidence | `docs/PHASE44_LONG_ONLY_INDEX_DRIFT_RESEARCH.md`, `phase44_validation.json`, `reports/phase44_*.csv` |
| Frozen impact | None. Phase 26 / 30 hashes unchanged. DRY_RUN only. |

### US small-cap gap-up short (Phase 43) — equity sleeve, not Book 3

| Field | Value |
|-------|--------|
| Family | `smallcap_gap_up_short_v1` |
| Phase | 43 |
| Status | Research-only — `SMALLCAP_DATA_QUALITY_BLOCKED` |
| Sleeve | US small-cap equity short. **Not** ES/NQ/GC. Not pooled with frozen futures. |
| Question | When a US small-cap gaps dramatically on abnormal volume, does objective OR5 breakdown produce cost-adjusted short expectancy after borrow, halt, and slippage? |
| Predeclared primary | `SMALLCAP_GAP50_OR5_BREAKDOWN`: gap ≥ +50%, 09:30–09:35 OR, 1m close below OR low, next open short, stop = OR high, cover 15:50. Locked in `phase43_spec.json`. **Not tested.** |
| Gate | No local equity universe. Tiingo/OpenBB BBBY daily is empty (survivorship). `FLOAT_DATA_UNAVAILABLE`. No halt tape (`HALT_MODEL_DEGRADED`; EQUS.MINI has no `status`). No borrow history. EQUS.MINI/IEX/DBEQ start 2023-03-28 so declared TRAIN through 2022 is empty. Mini 1m ALL ~$541 and XNAS.ITCH 1m ALL ~$1502 were quoted, not bought; they still lack PIT cap/float/borrow. |
| Verdict | Do not fake a surviving-name backtest. Do not freeze. Do not start First Red Day / Bounce Short on the same missing data. AITRADE has no equity locate/broker path. |
| Evidence | `docs/PHASE43_SMALLCAP_GAP_UP_SHORT_RESEARCH.md`, `phase43_validation.json`, `reports/phase43_*.csv` |
| Frozen impact | None. Phase 26 / 30 hashes unchanged. DRY_RUN only. |

### ES/NQ HTF trend + first pullback (Phase 42) — not a Book 3

| Field | Value |
|-------|--------|
| Family | `htf_trend_first_pullback_v1` |
| Phase | 42 |
| Status | Research-only — `HTF_PULLBACK_EDGE_WEAK` |
| Instruments | CME ES and CME NQ, **not pooled**. `ES_HTF_PULLBACK_STATUS` = REJECTED. `NQ_HTF_PULLBACK_STATUS` = WEAK. |
| Style | Completed 1h/4h return regime → first RTH session-impulse pullback → 5m confirmation. VWAP diagnostic only. Not NQ DVP. |
| Question | When ES or NQ has an objectively defined 1h or 4h trend, does the first intraday pullback into a predeclared retracement zone produce cost-adjusted continuation expectancy? |
| Predeclared primary | `HTF_1H_TREND_FIRST_PULLBACK_5M_CONFIRM`: 4 completed 1h bars, ±0.20%, first medium 40–60% pullback vs 09:30 open, next 5m after a continuation candle, stop = pullback extreme ±1 tick, 1R, flatten 15:55, max 1/day. Locked in `phase42_spec.json` before P&L. |
| Verdict | Overnight 1h direction does **not** predict the RTH close (P≈0.49 vs uncond ≈0.54). ES primary E[R]=−0.074, holdout −0.101. NQ E[R]=+0.021, PF=1.04, holdout +0.051, but 2023–2024 negative, shorts lose, P(reach 2R)≈0.7%, avg stop ≈37 NQ pts. DVP daily P&L corr=0.17 (not a clone). No freeze. No candidate. Do not add VWAP/EMA/dual-TF rescue. |
| Evidence | `docs/PHASE42_HTF_TREND_PULLBACK_RESEARCH.md`, `phase42_validation.json`, `reports/phase42_*.csv` |
| Frozen impact | None. Phase 26 / 30 hashes unchanged. DRY_RUN only. |

### ES/NQ prior-RTH volume profile (Phase 41) — not a Book 3

| Field | Value |
|-------|--------|
| Family | `index_rth_volume_profile_v1` |
| Phase | 41 |
| Status | Research-only — `VOLUME_PROFILE_STRUCTURAL_EFFECT_ONLY` |
| Instruments | CME ES and CME NQ, **not pooled**. `ES_VOLUME_PROFILE_STATUS` = REJECTED. `NQ_VOLUME_PROFILE_STATUS` = STRUCTURAL_EFFECT_ONLY. |
| Profile | **Degraded 1m** volume-at-price (uniform tick fill of each RTH 1m bar). Not a trade tape. 70% value area, POC tie → VWAP then lower tick. |
| Question | Does prior-session VAH/VAL/POC change next-session acceptance/rejection enough to trade after costs? |
| Predeclared primary | `VP_OUTSIDE_ACCEPT_POC`: open outside, 1m close inside, next open ±1 tick toward POC, stop = outside excursion extreme ±1 tick, flatten 15:55. Locked in `phase41_spec.json` before P&L. |
| Verdict | Mild location effect only (P(enter value) ≈ 59–63%; P(continue away) ≈ 37–41%; POC from outside ≈ 40%). ES primary E[R]=−0.033, all years after 2021 ≤0. NQ E[R]=−0.010, train negative, holdout N=40. Rejection and inside-rotation also fail ES; NQ rejection holdout negative. No freeze. No candidate. Do not run Phase 42 VP rescue. |
| Evidence | `docs/PHASE41_VOLUME_PROFILE_AUCTION_RESEARCH.md`, `phase41_validation.json`, `reports/phase41_*.csv` |
| Frozen impact | None. Phase 26 / 30 hashes unchanged. DRY_RUN only. |

### Medium-horizon time-series momentum (Phase 40) — branch closed

| Field | Value |
|-------|--------|
| Family | `futures_tsmom_v1` |
| Phase | 40 |
| Status | Research-only — overall `TREND_EDGE_WEAK`; **branch closed** |
| Instruments | CME ES, NQ, GC independently. `ES_TREND_STATUS` = WEAK. `NQ_TREND_STATUS` = WEAK. `GC_TREND_STATUS` = REJECTED. |
| Question | Does the sign of past N-session futures returns predict future multi-day returns strongly enough for a cost-adjusted 1-contract book? |
| Predeclared primary | `TSMOM_20D_5D`: 20-session roll-cleaned return sign at prior close, enter next open ±1 tick, hold 5 sessions, 1 contract. Locked in `phase40_spec.json` before P&L. |
| Verdict | Two-sided TSMOM fails. ES/NQ longs earn equity drift; shorts bounce (ES short E=−12.0, NQ −47.1). Primary PF≈1.03, t≈0.27, max DD 1,109 ES pts. 5d/10d neighbors lose. Mode 2 (no overnight) negative on all three. No freeze. No candidate. **`CLOSE_TSMOM_RESEARCH_BRANCH`.** Move Strategy #3 elsewhere. Do not retrofit long-only. |
| Evidence | `docs/PHASE40_TIME_SERIES_MOMENTUM_RESEARCH.md`, `phase40_validation.json`, `reports/phase40_*.csv` |
| Frozen impact | None. Phase 26 / 30 hashes unchanged. DRY_RUN only. |

### ES/NQ ORB boundary retest (Phase 39) — branch closed

| Field | Value |
|-------|--------|
| Family | `index_rth_orb_retest_v1` |
| Phase | 39 |
| Status | Research-only — `ORB_RETEST_EDGE_REJECTED` |
| Branch | **`CLOSE_ORB_RESEARCH_BRANCH`** |
| Question | After an OR15 1m-close break, does a shallow boundary retest + 1m close hold produce a better continuation trade than Phase 38 immediate entry? |
| Predeclared primary | `OR15_1M_BREAK_1M_RETEST_HOLD`: T0 exact touch, fail >10% OR width, 1m close hold, next open + 1 tick, stop = retest extreme ±1 tick, 1R. Locked in `phase39_spec.json` before P&L. |
| Verdict | No. ES E[R]=−0.180, NQ E[R]=−0.093, all 7 years negative, 1-tick required. Matched vs Phase 38: ES −0.215R, NQ −0.180R. Tight stops are noise. No freeze. No candidate. Do not run Phase 40 ORB rescue. Move Strategy #3 to a new family. |
| Evidence | `docs/PHASE39_ORB_RETEST_RESEARCH.md`, `phase39_validation.json`, `reports/phase39_*.csv` |
| Frozen impact | None. Phase 26 / 30 hashes unchanged. DRY_RUN only. |

### ES/NQ RTH opening-range breakout (Phase 38)

| Field | Value |
|-------|--------|
| Family | `index_rth_opening_range_breakout_v1` |
| Phase | 38 |
| Status | Research-only — `BREAKOUT_EDGE_WEAK` |
| Instruments | CME ES and CME NQ, **not pooled**. `ES_ORB_STATUS` = WEAK. `NQ_ORB_STATUS` = WEAK. |
| Question | After a completed 09:30 ET opening range, does the first break continue far enough, often enough, for cost-adjusted expectancy? |
| Predeclared primary | OR15, 1m close confirmation, opposite-OR stop, 1R, 1-tick fill, flatten 15:55. Locked in `phase38_spec.json` before P&L. |
| Verdict | Continuation hypothesis fails (false-break 91–92%, MFE≈MAE). ES 1R train E[R]=−0.005, 2-tick≈0, shorts lose. NQ 1R train/holdout/2-tick positive but 2020 negative, 901/1651 days `REJECT_WIDE_STOP`, holdout N=69 (2026 N=4). No freeze. No candidate JSON. **Phase 39 closed the ORB branch.** |
| Evidence | `docs/PHASE38_OPENING_RANGE_BREAKOUT_RESEARCH.md`, `phase38_validation.json`, `reports/phase38_*.csv` |
| Frozen impact | None. Phase 26 / 30 hashes unchanged. DRY_RUN only. |

### NQ shallow sweep + executed flow (Phase 37)

| Field | Value |
|-------|--------|
| Family | `nq_shallow_pdh_pdl_sweep_flow_v1` |
| Phase | 37 |
| Status | Research-only — `FLOW_CONFIRMED_STRATEGY_NOT_TRADABLE` |
| Question | Among shallow first PDH/PDL sweeps, does executed delta/volume improve reversal prediction and produce a tradable reclaim? |
| Verdict | Classification: reversal-aligned sweep-bar delta grades P(rev) (median 44%→79%, holdout 58%→78%, 4/4 WF same sign). Trade: Phase 36 geometry on that subset still loses (full E[R]=−0.09, holdout −0.18, ideal fills also negative). Branch closed. No freeze. No candidate JSON. No MBO. |
| Branch | `CLOSE_NQ_SWEEP_RESEARCH_BRANCH` |
| Evidence | `docs/PHASE37_NQ_EXECUTED_FLOW_CONFIRMATION.md`, `phase37_validation.json` |
| Frozen impact | None. Phase 26 / 30 hashes unchanged. DRY_RUN only. |

### NQ shallow PDH/PDL sweep reclaim (Phase 36)

| Field | Value |
|-------|--------|
| Family | `nq_shallow_pdh_pdl_sweep_reclaim_v1` |
| Phase | 36 |
| Status | Research-only — `STRUCTURAL_CLASSIFIER_NOT_TRADABLE` |
| Question | Can Phase 35's shallow-sweep reversal be turned into a cost-adjusted reclaim strategy? |
| Verdict | No. Predeclared Candidate B (1m close reclaim, 5 min, 1.5R, 1-tick fill): full E[R]=−0.09 N=86, holdout E[R]=−0.30 N=21. Ideal fills also negative. Phase 35 classifier stands; the trade does not. Not frozen. No candidate JSON. |
| Evidence | `docs/PHASE36_NQ_SHALLOW_SWEEP_STRATEGY.md`, `phase36_validation.json` |
| Frozen impact | None. Phase 26 / 30 hashes unchanged. DRY_RUN only. |

### NQ structural liquidity sweep (Phase 35)

| Field | Value |
|-------|--------|
| Family | `nq_liquidity_microstructure_reversal_v1` |
| Phase | 35 |
| Status | Research-only — `STRUCTURAL_ONLY_EDGE_FOUND` |
| Question | Do shallow NQ PDH/PDL sweeps still reverse on a 100–250 event sample, and does MBP-10 add stable incremental value after structure is known? |
| Verdict | Geometry yes, DOM no. N=269, Spearman(penetration, reversal)=−0.45, holdout shallow 74% vs deep 28%. Structural+MBP holdout Brier is worse than structure alone. MBO: `DO_NOT_ESCALATE_TO_MBO`. No entries. Not frozen. |
| Evidence | `docs/PHASE35_NQ_STRUCTURAL_SWEEP_EXPANSION.md`, `phase35_validation.json`, `strategy_candidates/phase35_NQ_STRUCTURAL_SWEEP.json` |
| Frozen impact | None. Phase 26 / 30 hashes and paper journals unchanged. DRY_RUN only. |

### NQ liquidity sweep + order-flow microstructure (Phase 34)

| Field | Value |
|-------|--------|
| Family | `nq_liquidity_microstructure_reversal_v1` |
| Phase | 34 |
| Status | Research-only — `MICROSTRUCTURE_PROMISING_NEEDS_MORE_DATA` |
| Question | When NQ sweeps prior-day RTH high/low, does MBP-10 / trade flow distinguish reversal from continuation better than the sweep itself? |
| Verdict | Not yet. Sweep-only P(reversal) = 45% at 300s (N=20). Top-10 book imbalance +30pp median split but N=10 per half and the lift dies at the 70th percentile. Absorption / signed flow did not support the hypothesis. No entries. Not frozen. |
| Evidence | `docs/PHASE34_NQ_LIQUIDITY_MICROSTRUCTURE_RESEARCH.md`, `phase34_validation.json`, `strategy_candidates/phase34_NQ_LIQUIDITY_MICROSTRUCTURE.json` |
| Frozen impact | None. Phase 26 / 30 hashes and paper journals unchanged. DRY_RUN only. |

### Post-news macro repricing (Phase 33)

| Field | Value |
|-------|--------|
| Family | `nq_post_news_macro_repricing_v1` |
| Phase | 33 |
| Status | Falsified / research-only — `MACRO_EDGE_REJECTED` |
| Question | After CPI/NFP and a ±5-minute prop blackout, does NQ (or ES/GC) show cost-adjusted continuation? |
| Verdict | No. Event-range breakouts lose ~30 pts/trade. Raw signed continuation after 08:35 is negative in 3/4 regime cells. |
| Evidence | `docs/PHASE33_POST_NEWS_MACRO_RESEARCH.md`, `phase33_validation.json`, `strategy_candidates/phase33_POST_NEWS_MACRO.json` |
| Frozen impact | None. Phase 26 / 30 hashes and paper journals unchanged. DRY_RUN only. |

### Original liquidity sweep / CHoCH / FVG

| Field | Value |
|-------|-------|
| Family | `session_sweep_choch_fvg` (Phase 17–20 lineage) |
| Status | Retired — no edge observed (Phase 19/20 verdict preserved) |
| Evidence | `phase18_validation.json`, `phase19_validation.json`, `phase20_validation.json` |
| Note | LuxAlgo live equivalence unvalidated |

### Liquidity reclaim

| Field | Value |
|-------|-------|
| Family | `liquidity_reclaim_v1` |
| Phase | 21 |
| Status | Research-only — did not graduate to freeze |
| Evidence | `phase21_validation.json`, candidates `strategy_candidates/phase21_R*.json` |

### GC ORB (opening range breakout)

| Field | Value |
|-------|-------|
| Family | `gc_orb_volume_v1` |
| Phase | 22 |
| Status | Research-only |
| Evidence | `phase22_validation.json`, `strategy_candidates/phase22_gc_G*.json` |

### OR15 retest / FVG

| Field | Value |
|-------|-------|
| Phase | 24 |
| Candidates | ORB15 breakout, retest touch, FVG touch |
| Status | Research-only |
| Evidence | `phase24_validation.json`, `strategy_candidates/phase24_*.json` |

### London VWAP L2

| Field | Value |
|-------|-------|
| Candidate | `L2_BAND_RECLAIM_2SIG_RETEST` |
| Phase | 27 |
| Session | London 08:00–12:00 Europe/London |
| Status | Falsification / research-only — independent of frozen NY GC V2 |
| Evidence | `phase27_validation.json`, `strategy_candidates/phase27_L2_*.json` |

### NY momentum continuation

| Field | Value |
|-------|-------|
| Family | `gc_ny_momentum_continuation_v1` |
| Phase | 28 |
| Status | Falsification — momentum did not independently pass |
| Evidence | `phase28_validation.json`, `strategy_candidates/phase28_C*.json` |

## Phase 25 GC candidates (non-frozen)

V3, V5 and other Phase 25 variants remain in `strategy_candidates/` for evidence. Only **V2** was frozen in Phase 26.
