<!-- POLISHED_PHASE46_REPORT -->
# Phase 46 — Strategy family portability

Research only. `DRY_RUN`. No broker. Frozen books were not modified.

Question locked before P&L: do the two surviving AITRADE mechanisms travel to ES and CL without a parameter search?

## 1. Verdict

- **Overall:** `STRATEGY_FAMILY_PORTABILITY_CONFIRMED`
- **Recommendation:** `PROMOTE_PORT_FOR_FURTHER_VALIDATION`
- **Best portable candidate:** `ES_DVP_PORT` (`strategy_candidates/phase46_ES_DVP.json`, `RESEARCH_CANDIDATE`, not frozen)

The **drift / first-opposing-5m** family ports to ES after TRAIN ATR scaling. The **VWAP 2σ reclaim/retest** family does **not** port to ES, CL, or NQ on this implementation. CL does not host either family after costs.

## 2. Frozen integrity

Verified before and after. `strategy_frozen/` was not written.

| Book | Config hash | File SHA |
|------|-------------|----------|
| GC VWAP V2 | `0695d7a881b6247033426bceb077b6895b96405b826c21f32a109a02f44b2d43` | `12615d19ef3deed36b7929c161ef7c377975cd2317c0ae6d06f1949bd327046f` |
| NQ DVP | `935e3a616351b09dbfa8d2d0e2b5d6850be803fb385ec5e5f8e3593856a1212a` | `34927fba0f268e56cf166cf8939bdfec6aa031510f7cee9acfab3909dd36b541` |

## 3. Forward validation (Workstream A)

Read-only. No synthetic forward trades.

- **GC_FORWARD_N:** 0 (`PAPER_VALIDATION_IN_PROGRESS`)
- **NQ_FORWARD_N:** 0 (`PAPER_VALIDATION_IN_PROGRESS`)

Paper journals remain the highest-priority work. Phase 46 did not append to them.

## 4. What was locked before P&L

Structural (unchanged): session VWAP typical-price formula; MR 2σ → band reclaim → frozen 2σ retest, stop = extension extreme; DVP 15m drift + 0.10% 1h return + first opposing 5m + next 5m open; max 4/day; stop after 2 losses.

Session-dependent (locked, not optimized):

- ES MR: 09:30–15:55 ET, no new after 14:55
- ES DVP: identical to NQ (09:30 / 10:30 / 15:30 / 15:55)
- CL MR: 09:00–14:30 ET (`MECHANIZED_APPROXIMATION` of NYMEX pit / US energy cash)
- CL DVP: 09:00 / 10:00 / 13:30 / 14:25

DVP distances: Version 1 literal 80/40/50 (diagnostic only). Version 2 PRIMARY: `distance * median_TRAIN_ATR14_target / median_TRAIN_ATR14_NQ`.

TRAIN ATR14 (session range, Wilder, prior days only): NQ 223.7, ES 50.4, CL 1.65, GC 18.4. ES scale **0.225** → stop 18 / TP 9 long / 11.25 short. CL scale **0.00735** → stop 0.59.

## 5. Portability matrix

| Family | GC | NQ | ES | CL |
|--------|----|----|----|----|
| VWAP mean reversion | **FROZEN** | `PORTABLE_EDGE_WEAK` | `PORTABLE_EDGE_REJECTED` | `PORTABLE_EDGE_REJECTED` |
| VWAP drift pullback | `PORTABLE_EDGE_REJECTED` | **FROZEN** | **`PORTABLE_EDGE_FOUND`** | `PORTABLE_EDGE_REJECTED` |

## 6. ES VWAP mean reversion — `ES_VWAP_MR_V2_PORT`

**`PORTABLE_EDGE_REJECTED`**

N=1387, E[R]=**−0.157**, E[pts]=−1.05, WR=0.35, PF=0.75. Every year 2020–2026 is negative. Holdout −0.128. 2-tick −0.317. Ideal ≈ 0.

Structural: P(reclaim)=0.998, P(retest entry)=0.70, P(VWAP touch)=0.66, **P(continue after reclaim)=0.79**. ES extends, reclaims the band, then often **breaks the extension again**. That is not GC-style reversion. Sigma 1.5/2.5 neighbors were not used to pick a winner; the 2σ book already loses.

Avg stop 5.75 ES pts (~$287 ES / $29 MES). Correlation vs NQ DVP −0.11 (different mechanism, still not an edge).

## 7. ES DVP — `ES_DVP_PORT`

**`PORTABLE_EDGE_FOUND`**

Same architecture as frozen NQ DVP. Stops/targets TRAIN-ATR scaled, not 80 ES points.

| | N | E[R] | E[pts] | WR | PF |
|---|---:|---:|---:|---:|---:|
| Full 1-tick | 5574 | **+0.030** | +0.53 | 0.650 | 1.10 |
| Train | 2511 | +0.019 | | | |
| Holdout | 3063 | **+0.038** | | 0.657 | 1.13 |
| 0-tick | 5574 | +0.057 | | | |
| 2-tick | 5574 | **+0.0018** | | | |

Long E[R]=+0.031 (N=3075); short +0.027 (N=2499). Both sides positive; do not retrofit one-sided.

Years E[R]: 2020 **−0.047**; 2021 +0.055; 2022 +0.047; 2023 +0.057; 2024 +0.050; 2025 +0.011; 2026 +0.032. Six of seven years green. 2020 is the exception, not the whole sample.

Neighbors 0.9× / 1.1×: E[R] +0.021 / +0.032 (same sign). Literal 80/40/50 ES points also +0.034 — diagnostic only; **do not use $4k ES stops**. Primary remains 18 / 9 / 11.25.

Stop $900/ES ($90 MES). Daily P&L correlation vs NQ DVP **0.60** (below the locked 0.70 redundancy bar). This is a sister-index book, not a clone, and not a new unrelated strategy. 2-tick is thin: the port survives the gate but is more fragile than frozen NQ (NQ 2-tick E[R]=+0.059).

## 8. CL VWAP mean reversion — `CL_VWAP_MR_V2_PORT`

**`PORTABLE_EDGE_REJECTED`**

N=1324, E[R]=**−0.207**, holdout −0.176, 2-tick −0.388. Only 2022 and 2026 YTD are positive. Same structural pattern as ES: reclaim ≈ 1.00, continuation after reclaim 0.77. Crude does not fade 2σ the way frozen gold does.

EIA Wednesday 10:25–10:35 approximation removed **20** MR entries. The losing book is not an inventory strategy.

## 9. CL DVP — `CL_DVP_PORT`

**`PORTABLE_EDGE_REJECTED`**

N=5490, WR=0.63 (looks like NQ) but E[R]=**−0.021**, holdout −0.012. Ideal +0.013 — costs consume a tiny gross drift. 2-tick −0.055. Neighbors 0.9/1.1 both negative. Literal 80 CL points ≈ 0. Correlation vs NQ DVP 0.01 (economically different, still not an edge).

EIA window removed **263** entries. Remaining sample still loses. Avg stop 0.59 CL pts ($590 CL / $59 MCL).

## 10. Off-diagonal diagnostics

- **NQ VWAP MR:** E[R]=+0.037 but E[pts]=**−1.17** and PF=0.94. R/points disagree because small-stop trades inflate R. Not a portable MR book. `PORTABLE_EDGE_WEAK`.
- **GC DVP:** E[R]=−0.014, holdout −0.023. Gold does not host the NQ drift family. `PORTABLE_EDGE_REJECTED`.
- **GC VWAP MR on GC.v.0 1m:** E[R]=−0.129. This is a **proxy on a different stitch**, not a re-score of frozen V2 (frozen V2 used volume-crossover 5m and remains frozen). Do not unfreeze gold.

## 11. Portfolio correlation

| Port | vs NQ DVP daily P&L | vs GC V2 proxy |
|------|---------------------|----------------|
| ES DVP | **0.60** | 0.01 |
| ES MR | −0.11 | −0.01 |
| CL DVP | 0.01 | −0.03 |
| CL MR | 0.02 | 0.09 |

ES DVP is the only profitable port and is **correlated with NQ DVP** (same family, equity index). It is not `EDGE_FOUND_BUT_PORTFOLIO_REDUNDANT` under the predeclared 0.70 cutoff. A later freeze phase should still treat ES+NQ DVP as **one family, two listings**, not two unrelated books.

## 12. Comparison to originals

NQ DVP replay on the same stitch: N=5714, E[R]=+0.066, E[pts]=+5.25, WR=0.67 — matches the frozen historical shape (commission overlay slightly below the freeze file’s +5.95 pts).

ES DVP keeps that shape at smaller points: WR 0.65 vs 0.67, both sides positive, ~3 trades/day before caps. Magnitude is weaker (~0.53 ES pts vs ~5.3 NQ pts) because the stop is 18 vs 80.

GC V2’s reclaim→retest→VWAP sequence does not show a cost-adjusted edge on ES or CL. High reclaim rates are not continuation-to-VWAP edges.

## 13. Recommendation

`PROMOTE_PORT_FOR_FURTHER_VALIDATION`

Promote **only** `ES_DVP_PORT` as a research candidate. Do not freeze in Phase 46.

Do **not** promote ES/CL VWAP MR. Do **not** promote CL DVP. Do **not** open Tier 2 (RTY/YM/6E) until ES DVP has its own paper journal, separate from frozen NQ.

Keep collecting genuine forward trades for frozen GC V2 and frozen NQ DVP (`N=0` still).

Nothing in `strategy_frozen/` changed.
