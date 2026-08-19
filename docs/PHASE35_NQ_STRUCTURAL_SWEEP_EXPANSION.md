# Phase 35 — NQ Structural Liquidity Sweep Expansion

**Verdict: `STRUCTURAL_ONLY_EDGE_FOUND`**

**MBO: `DO_NOT_ESCALATE_TO_MBO`**

**Execution: DRY_RUN / no broker orders.** Nothing was frozen. Frozen GC VWAP V2 and NQ DVP hashes are unchanged.

Question asked:

> Do shallow NQ PDH/PDL sweeps remain materially more likely to reverse across a much larger chronological sample, and does MBP-10 order-book information provide stable incremental predictive value after sweep structure is already known?

Answer: **yes to the geometry, no to the DOM.** Across 269 eligible CME NQ PDH/PDL sweeps (2025-06-17 → 2026-08-14, five front-month contracts, book on 269/269), deeper penetration is monotonically associated with continuation. The relationship survives train/holdout, four walk-forward blocks, PDH vs PDL, and the opening 30 minutes. Adding MBP-10 features to a structural logistic model **worsens** holdout Brier score. Phase 34’s 70% shallow-reversal rate was not a 20-event fluke, but it was also not an order-flow edge.

No entries, stops, targets, or size rules were optimized. Candidate JSON is **RESEARCH_ONLY**.

---

## 1. Verdict

| Status | `STRUCTURAL_ONLY_EDGE_FOUND` |
|--------|------------------------------|
| Why this status | N=269 ≥ 100; Spearman(penetration, reversal) = −0.45; Q1−Q5 = +60pp; holdout Spearman −0.50 with train-median shallow 74% vs deep 28% |
| Why not `MBP_INCREMENTAL_EDGE_FOUND` | Holdout Brier Model 2 (structure) 0.222 vs Model 6 (structure+MBP) 0.226. Required lift was ≥ 0.01 Brier. The book lost. |
| Why not `STRUCTURAL_SWEEP_PROMISING_NEEDS_MORE_DATA` | Holdout and all four chronological blocks keep the depth gradient |
| Why not `MICROSTRUCTURE_EDGE_REJECTED` | Structure is useful. MBP is not. That is a successful research outcome, not a rejection of the sweep itself |
| MBO | `DO_NOT_ESCALATE_TO_MBO` |
| Next | Phase 36: **structural strategy construction** (no DOM, no freeze unless a later phase authorizes it) |

Predeclared gates were coded in `phase35_validate.py` before looking at the expanded labels. They were not retuned after seeing results.

---

## 2. Frozen integrity

Confirmed before and after the validator (`assert_frozen`):

| Artifact | Hash |
|----------|------|
| GC V2 `frozen_config_hash` | `0695d7a881b6247033426bceb077b6895b96405b826c21f32a109a02f44b2d43` |
| NQ DVP `frozen_config_hash` | `935e3a616351b09dbfa8d2d0e2b5d6850be803fb385ec5e5f8e3593856a1212a` |
| `strategy_frozen/gc_vwap_v2_phase26.json` SHA-256 | `12615d19ef3deed36b7929c161ef7c377975cd2317c0ae6d06f1949bd327046f` |
| `strategy_frozen/nq_dvp_phase30.json` SHA-256 | `34927fba0f268e56cf166cf8939bdfec6aa031510f7cee9acfab3909dd36b541` |

`tests_phase35.py`: frozen hashes, carried-forward 8pt/12pt/300s definitions, roll-day RTH stays on the old contract.

Nothing was written to `strategy_frozen/`.

---

## 3. Reproduction audit

Phase 34 was rerun with the same detector, outcome labels, and NQM6 1m file.

| Check | Result |
|-------|--------|
| N sweeps | 20 / expected 20 |
| P(reversal) at 300s | 0.45 / expected 0.45 |
| PDH / PDL | 15 / 5 |
| Discrepancy | none |
| `methodology_corrections` | `[]` |

Definitions were frozen in `phase35_spec.json` **before** expansion. Download window remains **T−60s → T+120s** (not T−120/T+300) so OFI lookback and the Phase 34 cache stay consistent. Predictive features still ignore records after sweep-bar close.

Phase 34’s sample median 26.375 points was **not** reused as “shallow.” Phase 35 uses train-only median (18.25 pts), quintiles, and Spearman.

---

## 4. Data expansion

| Item | Value |
|------|-------|
| Dates | 2025-06-17 → 2026-08-14 |
| Contracts (front-month RTH) | NQU5, NQZ5, NQH6, NQM6, NQU6 |
| Raw PDH/PDL sweeps | 294 |
| Removed (full CPI/NFP day) | 25 |
| Removed (strict ±5m around 08:30) | 0 (RTH starts 09:30) |
| Eligible | **269** (PDH 155 / PDL 114) |
| Events with MBP-10 at cutoff | **269 / 269** |
| Window | T−60s → T+120s per sweep, same raw symbol as the RTH day |
| Cached files | 269 MBP-10 + 269 trades |
| Bytes on disk | **1,464 MB MBP-10 + 32 MB trades = 1,496 MB** |
| Cost estimate (Phase 34 unit prices) | **$46.70** (cap $50). Per-window `get_cost` was skipped after a serial API stall; we did not re-bill cached Phase 34 NQM6 slices |
| Degraded Databento days | 2025-11-28, 2026-03-16 (files still usable; 269/269 `has_book`) |
| Truncated DBN warning | one incomplete-record warning during read; coverage still 269/269 |

### Rolls (volume-crossover, activate 18:00 America/New_York)

| Decision date | Old → new | First RTH on new |
|---------------|-----------|------------------|
| 2025-06-16 | NQM5 → NQU5 | 2025-06-17 |
| 2025-09-15 | NQU5 → NQZ5 | 2025-09-16 |
| 2025-12-15 | NQZ5 → NQH6 | 2025-12-16 |
| 2026-03-16 | NQH6 → NQM6 | 2026-03-17 |
| 2026-06-14 | NQM6 → NQU6 | 2026-06-15 |

PDH/PDL are computed on the **same raw contract** as the sweep RTH day. Roll-day RTH stays on the old contract because activation is 18:00, after 16:00. No synthetic back-adjustment. No CFD / cash-index substitution.

Eligible events by contract: NQU5 55, NQZ5 59, NQH6 57, NQM6 60, NQU6 38.

---

## 5. Structural baseline (300s, 8 pt reversal / 12 pt continuation)

Unresolved = NEITHER + AMBIGUOUS. Primary rate treats unresolved as non-reversal.

| Slice | N | Reversal | Continuation | Unresolved | P(rev) | Wilson 95% | P(rev \| decided) |
|-------|---|----------|--------------|------------|--------|------------|-------------------|
| Any PDH/PDL sweep | 269 | 115 | 84 | 70 | **42.8%** | 37.0–48.7% | 57.8% |
| PDH | 155 | 66 | 43 | 46 | 42.6% | 35.1–50.5% | 60.6% |
| PDL | 114 | 49 | 41 | 24 | 43.0% | 34.3–52.2% | 54.4% |

Phase 34’s PDH 53% vs PDL 20% (N=15/5) **does not replicate**. Upside and downside sweeps reverse at the same rate once the sample is large. Pooling after side-specific penetration is supported for the structural question.

Unlabeled events are not hidden: 70/269 = 26% unresolved. Among decided events the reversal rate is 58%, not 43%. Both numbers are reported; neither was chosen to look stronger.

---

## 6. Penetration-depth analysis

Spearman(penetration points, reversal) = **−0.448**.

| Quintile | N | Mean pts | P(reversal) | Wilson 95% |
|----------|---|----------|-------------|------------|
| Q1 (shallow) | 53 | 2.5 | **66.0%** | 53–77% |
| Q2 | 54 | 7.4 | 63.0% | 50–75% |
| Q3 | 54 | 20.3 | 50.0% | 37–63% |
| Q4 | 54 | 60.8 | 29.6% | 19–43% |
| Q5 (deep) | 54 | 240.7 | **5.6%** | 2–15% |

Q1 − Q5 = **+60pp**. Neighboring median / p60 / p70 / p80 splits are all ≤ −42pp (`THRESHOLD_STABLE`). There is no magical cutoff. Deeper → less reversal.

**Normalized depth**

| Measure | Spearman | Median-split lift (hi−lo) | Notes |
|---------|----------|---------------------------|--------|
| Raw points | −0.45 | −42pp | Stable across p50–p80 |
| Points / ATR(1m,14) | −0.45 | −38pp | Same shape; Q1 66% vs Q5 3.7% |
| Points / median prior 1m range | −0.22 | −29pp | N=138 only — opening-bar sweeps have no same-session history |
| Volume on sweep bar | −0.28 | −27pp | Related, weaker than depth |
| Seconds from RTH open | +0.27 | +23pp | Later session reverses more; see §7 |

Raw points and ATR-normalized depth tell the same story. Same-session median range is missing for the 131 first-bar events, so it is not the preferred normalizer. Chronological stability (walk-forward Spearman all between −0.38 and −0.56) is already strong on **raw points**. We do not switch to ATR because it scored slightly prettier in-sample; both are usable. Phase 36 should keep the train-fold median / quantile, not a frozen 18.25-pt magic number.

Train-only median = **18.25 pts** (not the Phase 34 26.375).

| Split | Shallow P(rev) | Deep P(rev) | Gap |
|-------|----------------|-------------|-----|
| Train (N=188) | 61.5% (59/96) | 19.6% (18/92) | +42pp |
| Holdout (N=81) | **73.5% (25/34)** | **27.7% (13/47)** | **+46pp** |

---

## 7. Time of day / opening drive

185/269 events (69%) occur in the first 30 minutes of RTH. The opening drive is the typical sweep, not an exception.

| Bucket | N | P(reversal) |
|--------|---|-------------|
| Open 30m | 185 | 35.7% |
| Open 60m (not in first 30) | 22 | 54.5% |
| Midday | 52 | 61.5% |
| Afternoon | 10 | 50.0% |
| Not in open 30m | 84 | 58.3% |
| First RTH 1m bar (gap / through PDH/PDL at 09:30) | 131 | 28.2% |
| Later than first bar | 138 | 56.5% |
| Aligned with 30m opening direction | 58 | 55.2% |
| Against 30m opening direction | 24 | 70.8% |

**The shallow/deep relationship is not a time-of-day artifact.** Inside the opening 30 minutes, using a 20-pt diagnostic split: shallow 64% (43/67) vs deep 19% (23/118). Midday shallow 65% vs deep 33% (deep N=6). PDH and PDL both show the split (PDH 66% vs 21%; PDL 61% vs 23%).

Opening-drive *deep* sweeps continue. Opening-drive *shallow* sweeps still reverse ~2/3 of the time. A later-session-only rule would throw away a large, still-informative slice.

Detector is **first through the level that day**. Repeated sweeps of the same PDH/PDL on the same session are excluded by construction.

---

## 8. MBP-10 feature results

Direction-normalized so that **positive is supposed to support reversal** (PDL: bid-heavy / buy-flow; PDH: ask-heavy / sell-flow). Documented in `nq_microstructure_features.py`.

Book at cutoff: 269/269.

| Feature | Spearman | Median lift (hi−lo) | Status |
|---------|----------|---------------------|--------|
| `imb_for_reversal_top1` | −0.18 | −19pp | THRESHOLD_STABLE **wrong way** |
| `imb_for_reversal_top3` | −0.06 | +4pp | THRESHOLD_UNSTABLE |
| `imb_for_reversal_top5` | −0.10 | −12pp | THRESHOLD_STABLE **wrong way** |
| `imb_for_reversal_top10` | −0.03 | +1pp | THRESHOLD_UNSTABLE |
| `signed_flow_for_reversal` | −0.04 | −6pp | THRESHOLD_UNSTABLE |
| `ofi_for_reversal` | −0.17 | −8pp | THRESHOLD_STABLE **wrong way** |
| `absorption_proxy` | −0.05 | −3pp | THRESHOLD_UNSTABLE |
| `executed_to_displayed` | −0.05 | −5pp | THRESHOLD_UNSTABLE |
| `slope_for_reversal` | −0.00 | +4pp | THRESHOLD_UNSTABLE |
| `persistence_top1_swept_side` | −0.13 | −10pp | THRESHOLD_STABLE **wrong way vs reload-reversal** |
| `withdrawal_proxy` | +0.11 | +10pp | THRESHOLD_STABLE (1 − persistence) |
| `price_impact_per_lot` | +0.05 | +3pp | THRESHOLD_UNSTABLE |

Phase 34’s top-10 +30pp median split **does not replicate** (now +1pp, unstable). Absorption still does not help. Signed flow / executed-to-displayed do not help.

Conditional probabilities the question asked for:

- P(reversal \| shallow) = **64.6%** (84/130, train-median)
- P(reversal \| shallow + high reversal-aligned top-10 imb) = **65.6%** (42/64)
- P(reversal \| shallow + low top-10 imb) = **63.6%** (42/66)

The book does not move the shallow-sweep rate.

---

## 9. Incremental-value analysis

Chronological 70/30 by unique dates. Train 188 events (through 2026-04-09), holdout 81 (2026-04-13 → 2026-08-14). Logistic regression on z-scored features. No XGBoost, no giant search.

| Model | Features | Train Brier | Holdout Brier | Holdout log loss |
|-------|----------|-------------|---------------|------------------|
| 0 | sweep side | 0.242 | 0.253 | 0.699 |
| 1 | side + penetration | 0.227 | 0.226 | 0.640 |
| **2** | **side + pen + TOD + volume** | **0.223** | **0.222** | **0.629** |
| 3 | Model 2 + top-10 imb | 0.221 | 0.226 | 0.640 |
| 4 | Model 2 + OFI | 0.218 | 0.220 | 0.625 |
| 5 | Model 2 + absorption | 0.223 | 0.222 | 0.630 |
| 6 | Model 2 + top-10 + OFI + absorption + signed flow | 0.214 | **0.226** | 0.638 |

OFI’s holdout Brier improvement vs Model 2 is **0.0019**, below the predeclared 0.01 gate, and OFI’s median split points the wrong way. Model 6 overfits train and **loses** on holdout.

**The sweep geometry matters; the DOM does not add enough.**

---

## 10. Chronological robustness

Walk-forward four blocks (not shuffled):

| Block | Dates | N | P(rev) | Spearman(pen) | Q1 P(rev) | Q5 P(rev) |
|-------|-------|---|--------|---------------|-----------|-----------|
| 1 | 2025-06-17 → 2025-09-29 | 65 | 44.6% | −0.56 | 77% | **0%** |
| 2 | 2025-09-30 → 2026-01-16 | 67 | 34.3% | −0.38 | 46% | **0%** |
| 3 | 2026-01-20 → 2026-04-24 | 69 | 47.8% | −0.42 | 77% | 14% |
| 4 | 2026-04-28 → 2026-08-14 | 68 | 44.1% | −0.49 | 62% | **0%** |

Holdout (block-4 overlapping later sample) shallow 74% vs deep 28%. Monthly reversal rates vary (2025-11 13%, 2026-06 63%) but the **depth gradient** does not flip.

Contract-level P(rev): NQH6 49%, NQU6 50%, NQU5 45%, NQM6 40%, NQZ5 32%. NQZ5 (Sep–Dec 2025) is the weakest month-cluster; even there Q5 is continuation-heavy.

---

## 11. Threshold stability

Structural penetration: stable at p50/p60/p70/p80, all strongly negative.

MBP features that look “stable” are stable **in the wrong direction** relative to the reversal-supporting construction (top-1 imbalance, OFI, persistence). Withdrawal (+10pp) is the mechanical inverse of persistence and is small next to the 42pp structural split. Top-10 imbalance is `THRESHOLD_UNSTABLE`, matching Phase 34’s warning that a single percentile can lie.

---

## 12. Prop compatibility

| Filter | Count |
|--------|-------|
| Raw sweeps | 294 |
| Full CPI/NFP publication dates removed | 25 |
| Strict T−5m → T+5m around 08:30 | **0** (no RTH sweep can fall in 08:25–08:35) |
| Eligible research sample | 269 |

Default research blackout remains T−5m → T+5m around high-impact scheduled prints. Because those prints are 08:30 ET, they never overlap RTH 09:30–16:00 setups. Full-day CPI/NFP exclusion is the conservative Phase 34 carry-forward and is what produced N=269.

No entries were built, so prop daily-loss / news-flatten rules are not yet an execution constraint. If Phase 36 builds trades: no passive fill assumed at the sweep extreme; cost model if used later is market/stop after reclaim, 1 NQ tick = $5.

---

## 13. Portfolio comparison (read-only)

Frozen strategies were not modified.

**NQ DVP** (`journal/phase29_nq_drift_vwap/trades.jsonl`):

| Metric | Value |
|--------|-------|
| Sweep days | 247 |
| Same-day overlap with a DVP fill | 246 |
| Sweeps with a DVP fill within ±1 hour | 70 |
| Sweeps opposing that day’s DVP drift | 191 |
| Sweeps agreeing with DVP drift | 77 |

Almost every RTH sweep day is also a DVP-active day because DVP trades 10:30–15:30 on the same NQ session. That is **calendar overlap**, not alpha overlap. Phase 35 still has no entries, so P&L correlation and drawdown-period overlap are undefined. Directionally, most sweeps oppose the DVP drift label (PDH sweep on a negative-drift day, or PDL on positive-drift). A later structural-reversal book could be a different *return source* than DVP’s drift pullback — but only after entries exist.

**GC VWAP V2:** paper journal is still empty. Different market. Active-day P&L overlap is undefined.

Do not combine. Do not size. This is only to note that Strategy #3 would not be a disjoint calendar from NQ DVP.

---

## 14. MBO recommendation

**`DO_NOT_ESCALATE_TO_MBO`**

The validator’s automated flag fired `MBO_TARGETED_STUDY_JUSTIFIED` because `persistence_top1_swept_side` and `withdrawal_proxy` passed the 8pp neighboring-threshold rule. Phase T of the research brief is stricter: escalate only if a **precise unresolved hypothesis requires order identity**.

That bar is not met:

1. Persistence is already an MBP-10 top-of-book size ratio. It does not need MBO to exist.
2. Its stable lift is **negative** (high persistence → continuation), the opposite of “reload predicts reversal.”
3. `withdrawal_proxy` is `1 − persistence`.
4. `executed_to_displayed` is `THRESHOLD_UNSTABLE`.
5. Adding the MBP set to the structural model **raises** holdout Brier.

No MBO feature is specified. Do not buy full-history MBO because MBP failed to beat geometry.

---

## 15. Next recommendation

**Phase 36 should be structural strategy construction** — not targeted MBO, not another 20-event pilot, and not rejection.

Suggested scope (not executed here):

- Universe: first PDH/PDL sweep of the RTH session on the actual front-month NQ contract.
- Classifier already in hand: penetration depth (raw and/or ATR-normalized), optionally time-of-day as a *modifier*, not a substitute.
- Then, and only then: reclaim entry after the sweep bar is closed, stop/target, cost, prop flatten, chronological OOS expectancy.
- Compare occupancy with frozen NQ DVP (same session) without combining books.
- Still `DRY_RUN`. Still no freeze unless a dedicated freeze phase authorizes it.

Do not put MBP-10 in the live feature set just because we paid ~$47. The larger sample confirmed Phase 34’s real finding: **the geometry of the sweep is the signal.**

---

## Files

- `docs/PHASE35_NQ_STRUCTURAL_SWEEP_EXPANSION.md` (this file)
- `phase35_spec.json`, `phase35_validation.json`, `phase35_validate.py`, `phase35_download.py`
- `nq_front_month.py`, `tests_phase35.py`
- `reports/phase35_sweep_baseline.csv`, `phase35_sweep_events.csv`, `phase35_structure_quantiles.csv`
- `reports/phase35_time_of_day.csv`, `phase35_walkforward.csv`
- `reports/phase35_micro_features.csv`, `phase35_micro_median_splits.csv`, `phase35_incremental.json`
- `reports/phase35_cost_estimate.json`, `phase35_window_cost.json`
- `journal/phase35_nq_structural_sweep/sweeps.jsonl`
- `strategy_candidates/phase35_NQ_STRUCTURAL_SWEEP.json` (**RESEARCH_ONLY**, not a freeze)
- Cached DBN (gitignored): `data/databento/NQ/microstructure/{mbp-10,trades}/`

**Not touched:** frozen GC V2, frozen NQ DVP, their journals, their validation files.
