# Phase 38 — Opening Range / Volatility Expansion Breakout

**Verdict: `BREAKOUT_EDGE_WEAK`**

**`ES_ORB_STATUS`: `BREAKOUT_EDGE_WEAK`**

**`NQ_ORB_STATUS`: `BREAKOUT_EDGE_WEAK`**

**Execution: DRY_RUN / no broker orders.** Nothing was written to `strategy_frozen/`. No `FROZEN_PHASE38`. No research candidate JSON.

Question asked:

> After the market establishes an opening range, does a breakout beyond that range continue far enough, often enough, to create positive expectancy after realistic costs and chronological validation?

Answer: **not as a Strategy #3 book.** Price almost always comes back inside the range after the first break (false-break rate 91–92%). The predeclared 1R opposite-boundary trade is a coin-flip with a wide stop, not a volatility-expansion continuation. ES train expectancy is negative. NQ’s small full-sample plus comes from a 45% day subset that survives an 80-point stop cap, fails 2020, and has MFE ≈ MAE.

This is not `BREAKOUT_EDGE_FOUND`. It is not `BREAKOUT_EDGE_REJECTED` either: several neighboring NQ cells are cost-adjusted positive. It is not a structural classifier that we failed to monetize — the continuation story itself is weak. Do not add width, timing, or volume filters to rescue the equity curve.

Predeclared primary (frozen in `phase38_spec.json` **before** P&L):

`OR15` → `1m close beyond OR_HIGH/OR_LOW` → `next 1m open ± 1 tick` → `stop = opposite OR boundary` → `1R` → flatten 15:55 ET.

ID: `OR15_B_STOPA_1R`.

---

## 1. Verdict

| Field | Value |
|-------|--------|
| Overall | `BREAKOUT_EDGE_WEAK` |
| ES | `BREAKOUT_EDGE_WEAK` |
| NQ | `BREAKOUT_EDGE_WEAK` |
| Why not `FOUND` | ES train E[R] = **−0.005**. ES 2-tick E[R] ≈ **0**. ES shorts lose. NQ 2020 E[R] = **−0.061**. NQ holdout N=69 with 2026 N=4. NQ `REJECT_WIDE_STOP` on **901 / 1651** days. False-break **91–92%**. MFE ≈ MAE. |
| Why not `REJECTED` | NQ predeclared 1R is positive on train (+0.051R), holdout (+0.200R), and 2-tick (+0.061R). Neighboring OR15/OR30 NQ cells agree in sign. Sample is large. |
| Why not `STRUCTURAL_EFFECT_ONLY` | Width, timing, relative volume, and expansion features are not a clean continuation/failure classifier. Compression is **inverted** on NQ (narrow Q1 loses). |
| Why not `PROMISING_NEEDS_MORE_DATA` | Full N is 1592 (ES) / 746 (NQ). The problem is quality of edge, not N. |
| Candidate JSON | **not written** |
| Freeze | **none** |

Automated numeric gate in `phase38_validate.py` printed `BREAKOUT_EDGE_FOUND` because it scored full-sample and holdout only. Research override (train sign, 2020, 2-tick ES, reject funnel, false-break, MFE/MAE) is stored in `phase38_validation.json` as `research_override`. The deliverable verdict is **WEAK**.

---

## 2. Frozen integrity

Confirmed before and after `phase38_validate.py`. Frozen files were not modified.

| Artifact | Hash |
|----------|------|
| GC V2 `frozen_config_hash` | `0695d7a881b6247033426bceb077b6895b96405b826c21f32a109a02f44b2d43` |
| NQ DVP `frozen_config_hash` | `935e3a616351b09dbfa8d2d0e2b5d6850be803fb385ec5e5f8e3593856a1212a` |
| GC file SHA-256 | `12615d19ef3deed36b7929c161ef7c377975cd2317c0ae6d06f1949bd327046f` |
| NQ DVP file SHA-256 | `34927fba0f268e56cf166cf8939bdfec6aa031510f7cee9acfab3909dd36b541` |

`tests_phase38.py`: 5 passed (frozen hashes, spec primary declared before P&L, OR5 incomplete with 4 bars, 1m-close fill is next open not `OR_HIGH`, same-bar stop+target = `AMBIGUOUS`).

---

## 3. Repository / data audit

Reused, read-only:

- Databento NQ 1m stitched (`databento_NQ_stitched`, AITRADE volume-crossover, activate 18:00 NY)
- `bar_dataset.load_dataset` / `write_dataset`
- `nq_pdh_pdl.local_ts` / `rth_bars` / `ny_date` (America/New_York, DST-aware)
- `phase34_validate.assert_frozen` / file SHA helpers
- Phase 36 scoring style (cost-adjusted R, `AMBIGUOUS`, chronological TRAIN/HOLDOUT)
- BLS 08:30 calendar (does not overlap 09:30 ± 5m)

New in this phase:

- `data/databento/ES/stitched/databento_ES_v0_1m.jsonl` — Databento `ES.v.0` ohlcv-1m, 2020-01-01 → 2026-08-16, **2,338,955** bars, cost **$8.54** (under $20 cap)
- `orb_index_engine.py`, `phase38_spec.json`, `phase38_validate.py`, `tests_phase38.py`

Not reused: GC 08:20 gold ORB (retired). Phase 33 news engine. Phase 35–37 sweep / MBP / delta. Frozen GC V2 and NQ DVP configs (read-only hash check + DVP journal overlap).

Databento warned of degraded quality on **2020-02-27, 2020-02-28, 2020-06-30**. Those days remain in the file; they were not dropped.

---

## 4. Dataset

| | ES | NQ |
|--|----|----|
| Contract | CME ES futures `ES.v.0` volume-continuous | CME NQ futures, AITRADE volume-crossover stitch |
| Bars | 2,338,955 1m | ~2.34M 1m (existing cache) |
| Valid RTH days | 1,651 (2020-01-02 → 2026-08-14) | 1,651 (same calendar filter) |
| Session | 09:30–16:00 America/New_York | same |
| Flatten | 15:55 ET | same |
| Min RTH 1m bars | 350 | 350 |
| Tick / point $ / RT commission | 0.25 / $50 / $4 = 0.08 pts | 0.25 / $20 / $4 = 0.20 pts |
| TRAIN | through 2024-12-31 | same |
| HOLDOUT | 2025-01-02 onward | same |
| Roll | Databento volume-continuous (not re-stitched from raw ES contracts) | AITRADE volume-crossover @ 18:00 NY |

ES and NQ are **never pooled**. No SPY, QQQ, CFD, or cash index.

Limitation: ES roll method is Databento `ES.v.0`, not the same code path as NQ’s AITRADE stitch. Both are actual CME futures.

OR15 complete: **1,648** days each (3 incomplete). Almost every RTH day produces a first break after OR15 (`NO_BREAK` = 2 ES, 1 NQ). The missing NQ trades are stop-size rejects, not missing breaks.

### Primary funnel (OR15, 1m close, Stop A, 1R, 1 tick)

| | Valid days | Incomplete OR | No break | `REJECT_WIDE_STOP` | Entered |
|--|------------|---------------|----------|--------------------|---------|
| ES | 1,651 | 3 | 2 | 54 | **1,592** (96%) |
| NQ | 1,651 | 3 | 1 | **901** | **746** (45%) |

NQ rejected widths: mean 118 pts, min 44.25, max 446. Risk = fill-to-opposite-boundary, so a close that has already run 30–40 pts beyond a 45-pt range can still exceed the 80-pt cap. The NQ sample is **days whose first 1m-close break still fits under 80 pts of structural risk**. That cap was predeclared, not searched — it still means 2025–26 evidence is thin (holdout N=69, 2026 N=4).

---

## 5. OR5 results

Primary stop A, 1-tick, cost-adjusted. ES and NQ separate.

### ES OR5

| Entry | Target | N | WR | Full E[R] | Train E[R] | Hold E[R] |
|-------|--------|---|----|-----------|------------|-----------|
| range_1m | 1R | 1641 | 48.8% | **−0.035** | −0.020 | −0.083 |
| close_1m | 1R | 1640 | 48.7% | **−0.035** | −0.027 | −0.061 |
| range_1m | 0.5R | 1639 | 64.6% | −0.040 | −0.038 | −0.043 |
| close_1m | 0.5R | 1640 | 63.0% | −0.060 | −0.056 | −0.075 |
| range_1m | 2R | 1641 | 35.2% | +0.020 | +0.038 | **−0.033** |
| close_1m | 3R | 1640 | 30.7% | +0.017 | +0.038 | **−0.048** |

ES OR5 does not survive holdout at any target in this family. Early 5-minute range breaks are not a tradable expansion edge on ES.

### NQ OR5

| Entry | Target | N | WR | Full E[R] | Train E[R] | Hold E[R] |
|-------|--------|---|----|-----------|------------|-----------|
| range_1m | 1R | 1359 | 50.2% | **0.000** | −0.006 | +0.028 |
| close_1m | 1R | 1204 | 51.3% | **+0.022** | +0.018 | +0.047 |
| close_1m | 3R | 1204 | 34.3% | +0.120 | +0.120 | +0.122 |

NQ OR5 close-1m is weakly positive and holdout-positive, but smaller than OR15 and still a ~50% WR 1R coin-flip. OR5 range-through 1R is dead on the full sample.

---

## 6. OR15 results

This is the predeclared window.

### ES OR15 — 1 tick, Stop A

| Entry | Target | N | WR | Full E[R] | Train E[R] | Hold E[R] | PF |
|-------|--------|---|----|-----------|------------|-----------|-----|
| close_1m | **1R (primary)** | 1592 | 51.3% | **+0.014** | **−0.005** | +0.079 | 1.04 |
| close_1m | 0.5R | 1592 | 67.3% | +0.008 | +0.011 | −0.002 | 1.03 |
| close_1m | 1.5R | 1592 | 45.3% | +0.063 | +0.057 | +0.084 | 1.12 |
| close_1m | 2R | 1592 | 42.2% | +0.079 | +0.075 | +0.092 | 1.13 |
| close_1m | 3R | 1592 | 40.1% | +0.098 | +0.100 | +0.091 | 1.15 |
| range_1m | 1R | 1606 | 50.6% | +0.002 | −0.004 | +0.020 | 1.02 |
| close_5m | 1R | 1559 | 52.1% | +0.026 | +0.013 | +0.071 | 1.07 |

Primary 1R **fails TRAIN**. Higher R on the same entry looks better on both train and holdout. That is **not** used to promote 2R/3R — targets were tracked independently and 1R was locked before P&L. It is a diagnostic that a wide structural stop needs more than 1R to show a trend-following payoff, not a license to pick the max cell.

False-break (return inside OR after entry): **91.4%**. Crossed opposite side: **37.6%**. Ambiguity: **0%** (opposite-OR stop and 1R target almost never complete in the same 1m bar).

Avg stop 16.9 ES pts ($843 / contract). p95 stop 32.8 pts. Max DD 618 pts. Worst day −40 pts. Avg hold 99 minutes. MFE 12.3 / MAE −11.7 (nearly symmetric).

### NQ OR15 — 1 tick, Stop A

| Entry | Target | N | WR | Full E[R] | Train E[R] | Hold E[R] | Hold N | PF |
|-------|--------|---|----|-----------|------------|-----------|--------|-----|
| close_1m | **1R (primary)** | 746 | 53.9% | **+0.065** | **+0.051** | +0.200 | 69 | 1.17 |
| close_1m | 0.5R | 746 | 66.5% | +0.003 | −0.003 | +0.062 | 69 | 1.03 |
| close_1m | 1.5R | 746 | 46.2% | +0.091 | +0.078 | +0.220 | 69 | 1.21 |
| close_1m | 2R | 746 | 43.8% | +0.129 | +0.130 | +0.116 | 69 | 1.28 |
| close_1m | 3R | 746 | 41.3% | +0.121 | +0.121 | +0.123 | 69 | 1.25 |
| range_1m | 1R | 860 | 54.3% | +0.084 | +0.078 | +0.133 | 88 | 1.22 |
| close_5m | 1R | 658 | 52.3% | +0.037 | +0.027 | +0.141 | 62 | 1.12 |

NQ primary is the only predeclared cell with train+, holdout+, 2-tick+, and N_full≥200. Holdout N=69 meets the numeric floor of 50 and is still small, especially 2026 (4 trades). False-break **92.2%**. MFE 42.8 / MAE −41.4. Avg stop **58.7 NQ pts** ($1,173). p95 **77.8** (against the 80-pt cap). Max DD **1,000 NQ pts**. Worst day −80 pts.

First-trade (`range_1m`) is slightly better than 1m-close on NQ at 1R. That was not the locked primary. Do not switch after seeing P&L.

---

## 7. OR30 results

### ES OR30 — 1R Stop A

| Entry | N | Full E[R] | Train E[R] | Hold E[R] |
|-------|---|-----------|------------|-----------|
| range_1m | 1506 | +0.015 | +0.015 | +0.015 |
| close_1m | 1477 | +0.009 | +0.001 | +0.038 |

Tiny, stable, not a book. 3R close_1m is +0.071 full / +0.068 train / +0.082 hold — same post-hoc target issue as OR15.

### NQ OR30 — 1R Stop A

| Entry | N | Hold N | Full E[R] | Train E[R] | Hold E[R] |
|-------|---|--------|-----------|------------|-----------|
| range_1m | 522 | 40 | +0.073 | +0.057 | +0.266 |
| close_1m | 437 | 29 | +0.057 | +0.047 | +0.189 |

Sign agrees with OR15, but holdout N is below 50. Wider OR → more `REJECT_WIDE_STOP` on NQ. Not a promotion path.

---

## 8. Entry-family comparison

On the locked OR15 + Stop A + 1R + 1 tick:

| | ES E[R] full / train / hold | NQ E[R] full / train / hold |
|--|-----------------------------|-----------------------------|
| A first-range-through | +0.002 / −0.004 / +0.020 | +0.084 / +0.078 / +0.133 |
| B 1m close (primary) | +0.014 / **−0.005** / +0.079 | +0.065 / +0.051 / +0.200 |
| C 5m close (diagnostic) | +0.026 / +0.013 / +0.071 | +0.037 / +0.027 / +0.141 |

No family turns ES 1R into a train-positive book. NQ A and B both work in sign; C is weaker. Confirmation is not the missing ingredient. The 1m-close fill is next open ± 1 tick, not a fill at `OR_HIGH`/`OR_LOW`.

---

## 9. Target matrix

Independent tracking. Gate uses **1R only**.

| | 0.5R | 1R | 1.5R | 2R | 3R |
|--|------|----|------|----|----|
| ES OR15 close_1m full E[R] | +0.008 | +0.014 | +0.063 | +0.079 | +0.098 |
| ES train | +0.011 | **−0.005** | +0.057 | +0.075 | +0.100 |
| ES holdout | −0.002 | +0.079 | +0.084 | +0.092 | +0.091 |
| NQ OR15 close_1m full E[R] | +0.003 | +0.065 | +0.091 | +0.129 | +0.121 |
| NQ train | −0.003 | +0.051 | +0.078 | +0.130 | +0.121 |
| NQ holdout | +0.062 | +0.200 | +0.220 | +0.116 | +0.123 |

0.5R is a high-WR scalp that does not pay after costs (NQ train negative). 2R/3R look like classic trend payoffs (lower WR, higher E[R]) **after** seeing the matrix. A later phase would have to lock 2R *before* a new holdout. This phase does not.

Time-to-target / time-to-stop: average hold ~100 minutes on 1R (mix of target ~47%, stop ~45%, time-exit ~8% on ES; 49% / 43% / 8% on NQ). End-of-day force-close at 15:55 is a minority of outcomes.

---

## 10. Opening-range width

Quintiles of raw `OR_WIDTH` on the primary trade set (entered days only).

### ES

| Q | Mean width (pts) | N | WR | E[R] |
|---|------------------|---|----|------|
| 1 narrow | 7.0 | 318 | 50.6% | +0.003 |
| 2 | 10.2 | 318 | 51.3% | +0.012 |
| 3 | 13.3 | 319 | 51.4% | +0.027 |
| 4 | 17.9 | 318 | 52.8% | +0.037 |
| 5 wide | 26.9 | 319 | 50.5% | −0.007 |

Almost flat. Slightly worse at the widest quintile. Not a compression-expansion gradient.

### NQ (tradable subset only — width already capped)

| Q | Mean width (pts) | N | WR | E[R] |
|---|------------------|---|----|------|
| 1 narrow | 35.3 | 149 | 47.7% | **−0.048** |
| 2 | 46.5 | 149 | 53.7% | +0.056 |
| 3 | 53.9 | 149 | 55.0% | +0.070 |
| 4 | 61.1 | 149 | 61.7% | +0.193 |
| 5 | 70.3 | 150 | 51.3% | +0.055 |

**Inverted compression.** The narrowest tradable NQ opening ranges lose. Medium-wide ranges that still fit under 80 pts of risk do better. The hypothesis “compressed early structure has more room to expand” is not supported.

`OR_WIDTH / ATR` (14-day prior RTH range): ES is an inverted-U (Q4 +0.071, Q1 and Q5 negative). NQ rises toward wider-relative ranges (Q5 +0.139). Same inversion.

---

## 11. Breakout timing

Minutes after OR15 completion to the confirming close.

| Bucket | ES N | ES E[R] | NQ N | NQ E[R] |
|--------|------|---------|------|---------|
| 0–15m | 1110 | +0.010 | 556 | +0.079 |
| 15–30m | 317 | +0.101 | 128 | −0.034 |
| 30–60m | 124 | **−0.135** | 45 | +0.295 |
| >60m | 41 | −0.073 | 17 | −0.243 |

ES late breaks are worse; the 15–30m bucket is the only clearly positive ES slice — not predeclared, not promoted. NQ 30–60m is a 45-trade cell. Early vs late is **not** a stable shared law across instruments. No timing filter added.

---

## 12. Relative volume

`breakout_bar_volume / median same-time-of-day volume` (prior 20 sessions, known at the break).

| | Median relVol | E[R] below | E[R] above |
|--|---------------|------------|------------|
| ES | 1.33 | +0.016 | +0.012 |
| NQ | 1.31 | +0.062 | +0.071 |

No incremental value. **Dropped.** Not a participation edge.

---

## 13. Volatility expansion

Breakout-bar range quintiles (primary):

- ES: Q4 +0.20R, Q5 **−0.10R** — not monotonic.
- NQ: Q1 (smallest bar) **+0.13R**, Q5 **−0.008R** — opposite of “expansion bar continues.”

Break distance beyond the boundary: ES noisy; NQ Q1 (smallest chase) +0.14R, other quintiles ~0.02–0.06. Mild “don’t chase” hint, not stable enough to add a rule.

ATR-stop diagnostic (Stop C): ES −0.013R (N=404); NQ N=4 because 1.0×14-day ATR usually exceeds the 80-pt cap. Unusable.

**Dropped as a strategy feature.**

---

## 14. Gap / overnight / prior-day

Diagnostics only. Not rules.

**Gap vs prior RTH close** (±2 pts = flat):

| | ES E[R] | NQ E[R] |
|--|---------|---------|
| Gap up | +0.039 (n=840) | +0.091 (n=438) |
| Flat | **−0.203** (n=119) | +0.107 (n=26, small) |
| Gap down | +0.021 (n=632) | +0.025 (n=281) |

ES flat-gap days are bad. Not converted into a filter (small ES flat bucket, NQ flat N=26).

**Overnight high/low:** ES broke-overnight +0.028 vs inside +0.007. NQ +0.106 vs +0.044. Directionally consistent, too small and not holdout-locked.

**Prior-day alignment** (break direction vs prior RTH open-to-close): ES +0.023 aligned vs +0.005 opposed. NQ +0.091 vs +0.041. Same caveat.

PDH/overnight data was **not** used in the baseline entry. No sweep logic.

---

## 15. Long / short

Do not pool.

| | ES long | ES short | NQ long | NQ short |
|--|---------|----------|---------|----------|
| N | 825 | 767 | 398 | 348 |
| WR | 53.2% | 49.3% | 56.3% | 51.1% |
| E[R] | **+0.047** | **−0.021** | **+0.106** | **+0.018** |
| E[pts] | +0.85 | −0.32 | +7.37 | +0.87 |

ES shorts lose. NQ shorts are near zero; longs carry the NQ plus. A long-only ORB would be a different, post-hoc strategy. Not promoted.

Holdout ES shorts are positive (+0.089R) — the full-sample short loss is a train/2020–24 phenomenon, another instability marker.

---

## 16. ES / NQ

| | ES | NQ |
|--|----|----|
| Status | `BREAKOUT_EDGE_WEAK` | `BREAKOUT_EDGE_WEAK` |
| Cleaner ORB? | Larger N, 96% of days trade, but **no train edge at 1R** | Small 1R edge on a **45% day subset**, 2020 fails, 2026 empty |
| OR5 | Holdout-negative | Weakly positive close-1m |
| 2-tick | Full E[R] ≈ 0 | Still +0.061 |
| Stop B midpoint | −0.017 / hold −0.033 | −0.026 |
| Distinct from DVP? | n/a | Daily P&L corr **0.19** vs frozen DVP (low) |

Neither instrument is a third book. NQ is less dead than ES on the locked primary; that is not the same as robust.

---

## 17. Cost stress

Primary OR15 B Stop A 1R.

| Fill | ES E[R] full / hold | NQ E[R] full / hold |
|------|---------------------|---------------------|
| Ideal (0 tick) | +0.034 / +0.084 | +0.068 / +0.200 |
| **1 tick (primary)** | +0.014 / +0.079 | +0.065 / +0.200 |
| 2 tick | **+0.000** / +0.073 | +0.061 / +0.171 |

ES **dies at 2 ticks** on the full sample. NQ’s 1R edge is not a fill artifact. Commission is inside all figures ($4 RT).

If a strategy works only under ideal fills, fail it. ES 1R is already a 1-tick-only sliver; 2 ticks wipe it. NQ 1R survives 2 ticks on the tradable subset.

---

## 18. Chronological validation

Predeclared: TRAIN through 2024-12-31, HOLDOUT from 2025-01-02. Walk-forward = calendar years 2020–2026. No shuffle.

| | ES 1R | NQ 1R |
|--|-------|-------|
| TRAIN N / E[R] | 1221 / **−0.005** | 677 / **+0.051** |
| HOLDOUT N / E[R] | 371 / +0.079 | 69 / +0.200 |
| Holdout WR | 54.2% | 60.9% |

Walk-forward years (primary):

| Year | ES N | ES E[R] | NQ N | NQ E[R] |
|------|------|---------|------|---------|
| 2020 | 237 | **−0.026** | 161 | **−0.061** |
| 2021 | 248 | +0.067 | 161 | +0.146 |
| 2022 | 242 | **−0.013** | 49 | +0.140 |
| 2023 | 247 | +0.002 | 169 | +0.010 |
| 2024 | 247 | **−0.057** | 137 | +0.092 |
| 2025 | 232 | +0.080 | 65 | +0.212 |
| 2026 YTD | 139 | +0.078 | **4** | −0.003 |

ES: three clearly negative years in TRAIN, plus 2023 ≈ 0. Holdout strength is 2025–26. That is not chronological stability.

NQ: 2020 (the vol regime we required) loses. 2023 is flat. 2022 N=49 and 2026 N=4 are the 80-pt cap biting. Five years are positive in sign, but the stress year is not.

---

## 19. Regime analysis

Coverage includes 2020 vol, 2021, 2022 tightening/bear, 2023–24, 2025, 2026 YTD through 2026-08-14. No synthetic history.

- **2020:** both instruments lose at 1R. The opening-range break did not pay in the high-vol regime.
- **2022:** ES loses; NQ plus on 49 trades (most NQ days rejected as too wide).
- **2025–26:** ES holdout plus; NQ holdout plus on a small selected sample.

A configuration that needs 2021 + 2025 and cannot tolerate 2020 is not a third frozen book.

---

## 20. Monte Carlo / drawdown

300 shuffles of trade **order** (sum of R is invariant; DD is the test).

| | ES | NQ |
|--|----|----|
| Terminal sum R (fixed) | +22.9 | +48.6 |
| Median shuffle max DD (R) | 35.3 | 16.6 |
| p05 shuffle max DD (R) | 51.3 | 26.8 |
| Historical max DD (points) | 618 ES pts | **1,000 NQ pts** |
| Max consecutive losses | 8 | 8 |

NQ’s +48.6R over 746 trades is +0.065R × 746. Path DD of 1,000 NQ points ($20,000 / contract at $20/pt) is not prop-compatible without a different stop. ES historical DD 618 pts × $50 = $30,900 / contract.

Ambiguity rate 0%. No target-first cheating.

---

## 21. Portfolio relationship

Read-only. No combined sizing.

**Frozen NQ DVP** (`journal/phase29_nq_drift_vwap/trades.jsonl`):

- Same-day overlap: 746 / 746 NQ ORB days also have a DVP trade in that journal (DVP is dense).
- Daily P&L correlation: **0.19**
- Distinct enough *if* ORB were a book. Correlation is not the blocker. Expectancy is.

**Frozen GC VWAP V2:** paper journal is empty (N=0 forward). No aligned daily P&L. Active-day overlap cannot be measured from live GC V2 paper fills.

ORB is conceptually different (breakout vs mean-reversion vs VWAP-drift pullback). Conceptual diversity does not create an edge.

---

## 22. Prop geometry

Diagnostic only. Not optimized to a named firm.

| | ES primary | NQ primary |
|--|------------|------------|
| Avg stop | 16.9 pts ($843) | 58.7 pts ($1,173) |
| p95 stop | 32.8 pts ($1,638) | 77.8 pts ($1,555) |
| Trades / valid day | 0.96 | 0.45 |
| Worst day | −40 ES pts (−$1,992) | −80 NQ pts (−$1,599) |
| Max consec losses | 8 | 8 |
| Avg time in market | 99 min | 101 min |
| Multiple losses same day | 0 (first break only, one trade/day) | 0 |

News: BLS 08:30 ± 5 minutes removes **0** RTH entries. No complete 10:00 ET or FOMC calendar was applied (limitation, not a hidden filter). Many 10:00 releases sit inside the ORB holding window; they were not blacked out.

NQ stop size is a 1-contract prop problem even if expectancy were trusted. The 80-pt cap is already the geometry.

---

## 23. Recommendation

Do **not** freeze. Do **not** write a research candidate. Do **not** start Phase 39 as “ORB + width + timing + volume.”

The simple hypothesis — *first break of a completed RTH opening range continues* — is not supported:

1. 91–92% of entries return inside the range.
2. MFE ≈ MAE.
3. ES 1R fails train and dies at 2 ticks.
4. NQ 1R is a selected 45% of days, fails 2020, and needs ~$1,200 average risk.
5. Compression, relative volume, and expansion-bar features do not rescue a continuation story.

Higher-R ES/NQ cells (2R/3R) look better in the **same** locked entry. Promoting them now would be target shopping. If this family is ever reopened, a new phase must lock one payoff **before** a holdout that is not 2025–26, and must treat the 80-pt NQ cap and long/short split as predeclared constraints — not as discoveries.

Strategy #3 research should move to a **new family**, not another opening-range filter pass.

`methodology_corrections`: none during the test. Research verdict overrode an over-permissive numeric gate after P&L was visible; that override is documented in `phase38_validation.json` and this report. Spec primary was not retuned.

---

## Files

| Path | Role |
|------|------|
| `docs/PHASE38_OPENING_RANGE_BREAKOUT_RESEARCH.md` | This report |
| `phase38_spec.json` | Definitions frozen before entries |
| `phase38_validation.json` | Machine-readable results + research override |
| `phase38_validate.py` | DRY_RUN validator |
| `orb_index_engine.py` | Leak-safe OR / break / path |
| `tests_phase38.py` | Isolation + leak tests |
| `reports/phase38_*.csv` | Matrices, years, width, timing, fills, stops, long/short, funnel |
| `strategy_frozen/` | Unchanged |
