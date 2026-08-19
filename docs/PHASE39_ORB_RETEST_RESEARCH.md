# Phase 39 — Opening-Range Breakout Retest Continuation

**Verdict: `ORB_RETEST_EDGE_REJECTED`**

**`ES_RETEST_STATUS`: `ORB_RETEST_EDGE_REJECTED`**

**`NQ_RETEST_STATUS`: `ORB_RETEST_EDGE_REJECTED`**

**Recommendation: `CLOSE_ORB_RESEARCH_BRANCH`**

**Execution: DRY_RUN / no broker orders.** Nothing was written to `strategy_frozen/`. No research candidate JSON.

Question asked:

> Among valid opening-range breakouts, do those that return to the broken boundary and then successfully hold/reclaim it exhibit materially better continuation expectancy than the immediate breakout baseline?

Answer: **no.** The retest hold is a worse trade than Phase 38’s already-weak immediate entry. On matched days the retest model loses 0.18–0.22R relative to entering the first 1m-close break. Tight retest-extreme stops turn the 91–92% “return inside” behavior into a noise-stop. Every calendar year is negative on both ES and NQ.

This is the **final ORB research phase**. Do not start Phase 40 ORB rescue work.

Predeclared primary (frozen in `phase39_spec.json` **before** P&L):

`OR15` → `1m close break` (pending only) → `first return to exact OR boundary` → `penetration ≤ 10% of OR width` → `1m close back outside` → `next open ± 1 tick` → `stop = retest extreme ± 1 tick` → `1R` → flatten 15:55. Expiry 30 minutes after breakout confirmation.

ID: `OR15_1M_BREAK_1M_RETEST_HOLD`.

---

## 1. Verdict

| Field | Value |
|-------|--------|
| Overall | `ORB_RETEST_EDGE_REJECTED` |
| ES | `ORB_RETEST_EDGE_REJECTED` |
| NQ | `ORB_RETEST_EDGE_REJECTED` |
| Branch | `CLOSE_ORB_RESEARCH_BRANCH` |
| Why rejected | Primary 1R 1-tick: ES E[R]=**−0.180** (train −0.182, hold −0.174). NQ E[R]=**−0.093** (train −0.107, hold −0.049). All 7 years negative. Matched Phase 38 is better on the same days. Ideal fills still lose on ES. |
| Why not `FOUND` | Cost-adjusted expectancy is negative on train, holdout, and full sample. |
| Why not `STRUCTURAL_EFFECT_ONLY` | Shallow vs 5–10% depth is both negative. A 1-trade 10–25% cell is not a classifier. |
| Why not `PROMISING` | N is large (ES 758 / NQ 703 resolved). The sign is the problem. |
| Candidate JSON | **not written** |
| Freeze | **none** |

An automated depth-bucket rule briefly tagged NQ as `STRUCTURAL_EFFECT_ONLY` because one trade in the 10–25% bucket printed +0.99R. Research override rejects that. Stored in `phase39_validation.json` as `research_override`.

---

## 2. Frozen integrity

Confirmed before and after `phase39_validate.py`. Frozen files were not modified.

| Artifact | Hash |
|----------|------|
| GC V2 `frozen_config_hash` | `0695d7a881b6247033426bceb077b6895b96405b826c21f32a109a02f44b2d43` |
| NQ DVP `frozen_config_hash` | `935e3a616351b09dbfa8d2d0e2b5d6850be803fb385ec5e5f8e3593856a1212a` |
| GC file SHA-256 | `12615d19ef3deed36b7929c161ef7c377975cd2317c0ae6d06f1949bd327046f` |
| NQ DVP file SHA-256 | `34927fba0f268e56cf166cf8939bdfec6aa031510f7cee9acfab3909dd36b541` |

`tests_phase39.py`: 6 passed (frozen hashes, spec primary locked, immediate breakout is not the entry, deep retest fails, opposite-side invalidation, same-bar stop+target = `AMBIGUOUS`).

---

## 3. Phase 38 reproduction

Same engine, same OR15 / 1m close / opposite-OR stop / 1R / 1 tick, 2020-01-02 → 2026-08-14.

| | Phase 38 report | Phase 39 reproduce |
|--|-----------------|--------------------|
| ES N / E[R] / WR | 1592 / +0.0144 / 51.3% | **1592 / +0.0144 / 51.3%** |
| NQ N / E[R] / WR | 746 / +0.0652 / 53.9% | **746 / +0.0652 / 53.9%** |

Baseline matches. Phase 38 remains `BREAKOUT_EDGE_WEAK`. It is not evidence that retests work.

---

## 4. Phase 39 specification

Locked before P&L in `phase39_spec.json`. `methodology_corrections: []`.

| Step | Rule |
|------|------|
| Session | RTH 09:30–16:00 America/New_York. Flatten 15:55. |
| OR | OR15 09:30–09:45. Valid only after window close. |
| Breakout | First 1m **close** beyond OR_HIGH / OR_LOW. Activates `WAITING_FOR_RETEST`. Does **not** enter. |
| Retest T0 (primary) | Long: 1m low ≤ OR_HIGH. Short: 1m high ≥ OR_LOW. |
| Fail | Max penetration > **10% of OR_WIDTH** → `RETEST_FAILED`. |
| Confirm B (primary) | 1m close back outside the broken boundary. Entry **next 1m open ± 1 tick**. |
| Expiry | 30 minutes after breakout confirmation. |
| Invalidation | Range trades through the opposite OR boundary → `BREAKOUT_INVALIDATED`. No fade. |
| Stop A (primary) | Retest extreme ± 1 tick. Reject risk < 2 ticks or > 40 ES / 80 NQ pts. |
| Targets | 0.5 / 1 / 1.5 / 2 / 3 R independent. Gate uses **1R**. |
| First retest only | One opportunity per day. |

Neighborhood (not used to retune the primary): T1 = 2-tick approach, T2 = 5% width cap 8 ticks, fail 0% / 25%, confirms A/C, stops B/C and Phase 38 opposite-OR as read-only.

---

## 5. Funnel

OR15 complete: 1,648 days each.

| | ES | NQ |
|--|----|----|
| No break | 2 | 1 |
| Expired (no valid retest/hold in 30m) | 185 | 193 |
| Retest failed (too deep) | **644** | **670** |
| Opposite invalidated | 3 | 1 |
| Entered | 814 | 782 |
| Resolved | 758 | 703 |
| Ambiguous | 56 (6.9%) | 79 (10.1%) |

Most Phase 38 “returned inside range” events are **failed deep retests**, not shallow boundary holds. The shallow-hold subset is still large enough to test (N>200 / holdout>50) and it loses.

---

## 6. Retest depth

Primary fail cap is 10%, so entered trades are 0–10% by construction.

| Depth | ES N | ES E[R] | NQ N | NQ E[R] |
|-------|------|---------|------|---------|
| 0–5% | 508 | **−0.170** | 462 | **−0.090** |
| 5–10% | 244 | **−0.195** | 240 | **−0.104** |
| 10–25% | 6 | −0.363 | 1 | +0.99 (one trade) |

Shallow is not different from “slightly less shallow.” Both lose. This is **not** a structural continuation classifier.

---

## 7. Retest timing

| Bucket | ES N | ES E[R] | NQ N | NQ E[R] |
|--------|------|---------|------|---------|
| ≤5 min | 642 | −0.182 | 589 | −0.081 |
| 5–15 min | 88 | −0.228 | 81 | −0.214 |
| 15–30 min | 28 | +0.024 | 33 | −0.007 |

Fast retests dominate and lose. The 15–30m ES cell is 28 trades. Not a filter.

---

## 8. Entry-family comparison

OR15, T0, fail 10%, Stop A, 1R, 1 tick.

| Confirm | ES N / E[R] / hold E[R] | NQ N / E[R] / hold E[R] |
|---------|-------------------------|-------------------------|
| A first-range-through | 973 / −0.192 / −0.198 | 984 / −0.101 / −0.078 |
| **B 1m close (primary)** | **758 / −0.180 / −0.174** | **703 / −0.093 / −0.049** |
| C 5m close (diagnostic) | 172 / −0.124 / +0.027 | 186 / +0.149 / +0.085 |

Candidate C on NQ is the only clearly positive cell. It was **diagnostic**, N=186 < 200, and was not locked. Promoting it would be the filter-and-rescue path this phase forbids.

---

## 9. Stop-family comparison

| Stop | ES E[R] (avg stop pts) | NQ E[R] (avg stop pts) |
|------|------------------------|------------------------|
| **A retest extreme (primary)** | −0.180 (**2.7**) | −0.093 (**11.0**) |
| B boundary −2 ticks | −0.193 (2.4) | −0.091 (7.9) |
| C OR midpoint (diagnostic) | −0.039 (10.4) | −0.006 (53.0) |
| P38 opposite-OR (read-only) | +0.008 (18.9) | +0.034 (98.7, 777/783 >30 pts) |

Tighter structural stops, the intended upgrade from Phase 38’s coin-flip geometry, **make expectancy worse**. Putting the Phase 38 opposite-OR stop onto retest entries recovers a sliver of the old weak plus and brings back huge NQ risk. That is not a new edge.

ES Stop A: median 2.25 pts ($113), p95 6.0 pts ($300). Tight enough for prop size; too tight for 1m noise. Ambiguity 7–10% vs Phase 38’s 0%.

---

## 10. Target matrix

Primary path, 1 tick. Gate uses 1R only.

| Target | ES full / train / hold | NQ full / train / hold |
|--------|------------------------|------------------------|
| 0.5R | −0.182 / −0.196 / −0.134 | −0.085 / −0.096 / −0.046 |
| **1R** | **−0.180 / −0.182 / −0.174** | **−0.093 / −0.107 / −0.049** |
| 1.5R | −0.197 / −0.202 / −0.181 | −0.098 / −0.118 / −0.031 |
| 2R | −0.179 / −0.202 / −0.105 | −0.078 / −0.086 / −0.054 |
| 3R | −0.160 / −0.204 / −0.022 | −0.078 / −0.089 / −0.043 |

No target is train-and-holdout positive. Do not pick 3R because holdout is “less negative.”

---

## 11. Immediate vs retest entry

Matched days where both Phase 38 immediate 1R and Phase 39 retest 1R resolved.

| | ES (n=727) | NQ (n=310) |
|--|------------|------------|
| P39 E[R] | **−0.191** | **−0.103** |
| P38 E[R] | +0.024 | +0.077 |
| Delta (retest − immediate) | **−0.215** | **−0.180** |
| P39 WR / P38 WR | 42.5% / 52.0% | 46.5% / 53.9% |
| P39 avg stop / P38 avg stop | 2.7 / 17.2 pts | 9.0 / 59.8 pts |
| Avg hold | ~3.5 min vs ~112 min | ~2.6 min vs ~108 min |

Retest does not “wait for a better continuation.” It converts a wide, slow coin-flip into a tight, fast loser.

---

## 12. ES results

| Split | N | WR | E[R] | PF |
|-------|---|----|------|----|
| TRAIN through 2024-12-31 | 577 | 43.0% | **−0.182** | 0.84 |
| HOLDOUT from 2025-01-02 | 181 | 43.1% | **−0.174** | 0.65 |
| FULL | 758 | 43.0% | **−0.180** | 0.78 |

Walk-forward E[R]: 2020 −0.244, 2021 −0.112, 2022 −0.144, 2023 −0.086, 2024 −0.341, 2025 −0.155, 2026 −0.204. **0/7 years positive.**

Long −0.135R, short −0.231R. Max DD 300 ES pts. Worst day −39 pts. Avg hold 107 seconds on resolved 1R (many stops).

OR5 / OR30 diagnostics: −0.271 / −0.229. Worse.

---

## 13. NQ results

| Split | N | WR | E[R] | PF |
|-------|---|----|------|----|
| TRAIN | 536 | 46.1% | **−0.107** | 0.82 |
| HOLDOUT | 167 | 48.5% | **−0.049** | 0.94 |
| FULL | 703 | 46.7% | **−0.093** | 0.86 |

Walk-forward E[R]: 2020 −0.055, 2021 −0.201, 2022 −0.081, 2023 −0.139, 2024 −0.082, 2025 −0.065, 2026 −0.016. **0/7 years positive.**

Long −0.116R, short −0.068R. Max DD 722 NQ pts. Avg stop 11.0 pts ($221), p95 26 pts ($520).

---

## 14. Long / short

Do not pool.

| | ES long | ES short | NQ long | NQ short |
|--|---------|----------|---------|----------|
| N | 406 | 352 | 364 | 339 |
| WR | 45.3% | 40.3% | 45.6% | 47.8% |
| E[R] | **−0.135** | **−0.231** | **−0.116** | **−0.068** |

No direction is tradable.

---

## 15. Cost stress

| Fill | ES full / hold | NQ full / hold |
|------|----------------|----------------|
| Ideal 0 tick | **−0.052 / −0.052** | −0.046 / +0.011 |
| **1 tick (primary)** | **−0.180 / −0.174** | **−0.093 / −0.049** |
| 2 tick | −0.253 / −0.258 | −0.121 / −0.072 |

ES fails even with perfect fills. NQ’s ideal holdout sliver dies at 1 tick. Commission is inside all figures ($4 RT).

---

## 16. Threshold stability

| Family | Values | Result |
|--------|--------|--------|
| Trigger T0 / T1 / T2 | ES −0.180 / −0.117 / −0.127 | All negative |
| Fail 0% / 10% / 25% | ES −0.267 / −0.180 / −0.149 | All negative; 0% N=222 |
| Trigger T0 / T1 / T2 | NQ −0.093 / −0.097 / −0.098 | Flat and negative |
| Fail 0% / 10% / 25% | NQ −0.190 (n=45) / −0.093 / −0.098 | 0% too small; others lose |

Neighbors agree. This is `THRESHOLD_STABLE` in the wrong direction — not `THRESHOLD_UNSTABLE`. Changing T or fail% does not create an edge.

---

## 17. Regime diagnostics

OR width quintiles: every ES and NQ bucket negative. Narrow ES Q1 is worst (−0.378R). Extension-before-retest quintiles: all negative, no “don’t chase a huge breakout” law.

Relative volume stayed `DIAGNOSTIC_ONLY` (Phase 38 already showed no value). No DOM / delta / MBO.

Gap / overnight / ATR were not applied as filters.

---

## 18. Prop compatibility

BLS 08:30 ± 5m removes **0** RTH entries.

Would-be entries in 09:55–10:05 ET (unfiltered diagnostic): ES **238**, NQ **184**. No invented 10:00/FOMC calendar.

Stop A is prop-sized on ES (~$135 average / 1 contract) and moderate on NQ (~$221). Geometry is not the blocker — expectancy is. Ambiguity 7–10% is material with these tight stops (do not assume target first).

---

## 19. Portfolio relationship

No positive candidate series. Read-only NQ DVP overlap is reported anyway: 781/782 days overlap, daily P&L correlation **−0.02**. GC V2 paper journal remains empty. No combination.

---

## 20. Recommendation

**`CLOSE_ORB_RESEARCH_BRANCH`**

Do **not** `CONTINUE_ORB_TO_FREEZE_VALIDATION`.

Phase 38 tested `BREAK → ENTER` and found a weak coin-flip. Phase 39 tested `BREAK → RETEST → HOLD → ENTER` and found a **worse** trade: tighter stops, lower win rate, negative expectancy in every year, worse than immediate entry on matched days.

Do not add volume, ICT labels, or a 5-minute-close rescue. Strategy #3 research must move to a **new family**.

---

## Files

| Path | Role |
|------|------|
| `docs/PHASE39_ORB_RETEST_RESEARCH.md` | This report |
| `phase39_spec.json` | Definitions frozen before entries |
| `phase39_validation.json` | Machine-readable results |
| `phase39_validate.py` / `orb_retest_engine.py` / `tests_phase39.py` | DRY_RUN research |
| `reports/phase39_*.csv` | Matrices, years, depth, timing, fills, stops, thresholds, funnel |
| `strategy_frozen/` | Unchanged |
