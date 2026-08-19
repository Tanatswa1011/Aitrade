# Phase 36 — NQ Shallow PDH/PDL Sweep Reclaim Strategy

**Verdict: `STRUCTURAL_CLASSIFIER_NOT_TRADABLE`**

**Execution: DRY_RUN / no broker orders.** Nothing was frozen. No file was written to `strategy_frozen/`. No Phase 36 candidate was promoted.

Question asked:

> Can the validated shallow-sweep reversal tendency be converted into a realistic, cost-adjusted, out-of-sample trading strategy using a simple reclaim entry and disciplined exits?

Answer: **no.** Phase 35’s classifier still stands. Once a leak-safe reclaim, next-bar market fill, sweep-extreme stop, and 1-tick costs are applied, expectancy is negative on the predeclared primary and worse on holdout. Ideal fills are also negative. The structural phenomenon did not survive conversion into trades.

This is an acceptable research outcome. Do not force the classifier into production.

---

## 1. Verdict

| Status | `STRUCTURAL_CLASSIFIER_NOT_TRADABLE` |
|--------|--------------------------------------|
| Why this status | Predeclared Candidate B, 1.5R, 1-tick fill: full E[R] = **−0.088** (N=86), holdout E[R] = **−0.304** (N=21). Walk-forward 2/4 blocks positive. Neighborhood around 18.25 pts stays negative. Ideal fills still lose. |
| Why not `EDGE_FOUND` | Cost-adjusted holdout is negative. Full sample is negative. Not a fill-only failure. |
| Why not `REJECTED` | Phase 35 geometry is unchanged. We did not falsify “shallow reverses more than deep.” We falsified “a simple reclaim trade captures that.” |
| Why not `INSUFFICIENT_TRADE_SAMPLE` | 86 resolved trades (90 entries) is enough to see the sign of expectancy. Holdout N=21 is small, but it agrees with the full-sample loss. |
| Candidate JSON | **not written** (`RESEARCH_ONLY_NOT_PROMOTED`) |

Predeclared primary (frozen in `phase36_spec.json` **before** P&L was computed):

`first shallow sweep of the day` → `1m close reclaim within 5 minutes` → `next 1m open + 1 tick adverse` → `sweep-extreme stop − 1 tick` → `1.5R` → flatten 15:55.

---

## 2. Frozen integrity

Confirmed before and after `phase36_validate.py`:

| Artifact | Hash |
|----------|------|
| GC V2 `frozen_config_hash` | `0695d7a881b6247033426bceb077b6895b96405b826c21f32a109a02f44b2d43` |
| NQ DVP `frozen_config_hash` | `935e3a616351b09dbfa8d2d0e2b5d6850be803fb385ec5e5f8e3593856a1212a` |
| GC file SHA-256 | `12615d19ef3deed36b7929c161ef7c377975cd2317c0ae6d06f1949bd327046f` |
| NQ DVP file SHA-256 | `34927fba0f268e56cf166cf8939bdfec6aa031510f7cee9acfab3909dd36b541` |

`tests_phase36.py`: 5 passed (frozen hashes, Phase 35 train median 18.25 reused, same-bar stop+target = AMBIGUOUS, wick reclaim on the sweep bar is not taken, fill is next open + adverse not at PDL).

---

## 3. Phase 35 dependency

Universe = `reports/phase35_sweep_events.csv` (269 news-filtered events). Not re-detected with new sweep rules. `methodology_corrections: []`.

Shallow = `penetration_points <= 18.25`, the Phase 35 **train-only** median. Not the Phase 34 26.375 sample median. Not searched against Phase 36 P&L.

No MBP, MBO, CVD, FVG, CHoCH, RSI, or moving averages in the live feature set.

---

## 4. Candidate rules (predeclared primary B)

| Step | Rule |
|------|------|
| Session | RTH 09:30–16:00 America/New_York. No new sweep after 15:45. Flatten 15:55. |
| Setup | First eligible PDH or PDL sweep of the day, and that sweep is shallow (≤ 18.25 pts). |
| Long | PDL sweep, then 1m **close** back ≥ PDL. |
| Short | PDH sweep, then 1m **close** back ≤ PDH. |
| Confirmation | Sweep known at bar close. Reclaim known at reclaim-bar close. |
| Entry | **Next 1m open** after the confirming close. Never a fill at PDL/PDH or at the sweep extreme. |
| Fill overlay | Primary +1 tick adverse (long pays up / short sells down). Also report 0 and 2 ticks. |
| Stop | Sweep extreme ± 1 tick. Reject if risk > 40 pts. |
| Target | 1.5R (matrix also reports 1R / 2R / 3R independently). |
| Expiry | Reclaim close must occur within **300s** of sweep confirmation. |
| Ambiguous | Same 1m bar hits stop and target → `AMBIGUOUS`, target is **not** assumed first. |
| Cost | $4 round-turn commission = 0.20 NQ points, plus fill overlay. |

Candidate A (range-through 1m) and C (5m close) were run as the predeclared small family. C is diagnostic. The gate uses **B only**.

---

## 5. Sample

| Stage | N |
|-------|---|
| Phase 35 eligible sweeps | 269 |
| First sweep of day | 247 |
| Any shallow | 130 |
| **First and shallow (primary setups)** | **114** |
| Reclaimed within 5 minutes | 95 |
| Entered | 90 |
| Resolved (target / stop / time) | 86 |
| Ambiguous | 4 (4.4% of entries) |
| Expired (no 1m close reclaim in 5 min) | 19 |
| Rejected wide/tight stop | 0 |
| News ±5m around 08:30 removed | 0 |

---

## 6. Target matrix (1-tick fill, first+shallow, 5 min expiry)

Cost-adjusted. Do **not** promote the largest cell.

| Candidate | R | N | WR | E[R] | E[pts] | PF | Max DD pts | Holdout N | Holdout E[R] |
|-----------|---|---|----|------|--------|----|------------|-----------|--------------|
| A range | 1.0 | 83 | 47% | −0.08 | −0.39 | 0.95 | 219 | 19 | −0.28 |
| A range | 1.5 | 88 | 40% | −0.03 | +0.86 | 1.11 | 158 | 22 | −0.34 |
| A range | 2.0 | 90 | 37% | +0.08 | +2.79 | 1.34 | 123 | 23 | −0.37 |
| A range | 3.0 | 90 | 26% | +0.01 | +2.66 | 1.28 | 169 | 23 | −0.32 |
| **B close (primary)** | **1.0** | 82 | 44% | **−0.14** | −2.18 | 0.76 | 258 | 18 | −0.24 |
| **B close (primary)** | **1.5** | **86** | **37%** | **−0.09** | **−0.99** | **0.90** | **207** | **21** | **−0.30** |
| B close | 2.0 | 88 | 34% | +0.00 | +0.95 | 1.09 | 162 | 22 | −0.34 |
| B close | 3.0 | 88 | 25% | −0.01 | +0.87 | 1.08 | 177 | 22 | −0.29 |
| C 5m (diagnostic) | 1.5 | 38 | 58% | +0.43 | +8.88 | 2.44 | 110 | **6** | +0.65 |

Candidate A 2R is the only A/B cell with positive full-sample E[R], and **holdout is still −0.37**. That is not an edge.

Candidate C looks strong and is **not promoted**: diagnostic-only, N=38, holdout N=6. A 5-minute close inside a 5-minute expiry mostly selects the first RTH 5m bar. That is a different, tiny subset — not a robustness check of B.

---

## 7. Fill stress (primary B, 1.5R)

| Overlay | N | WR | E[R] | E[pts] | PF |
|---------|---|----|------|--------|----|
| Ideal (0 tick) | 85 | 39% | **−0.048** | −0.62 | 0.94 |
| **1 tick entry (primary)** | 86 | 37% | **−0.088** | −0.99 | 0.90 |
| 2 tick entry | 87 | 37% | −0.098 | −1.07 | 0.89 |
| 1 tick entry + 1 tick exit | 86 | 37% | −0.111 | −1.24 | 0.88 |

A candidate that survives only ideal fills must not pass. This one **does not survive ideal fills either**.

---

## 8. Train / holdout (primary B, 1.5R, 1-tick)

| Split | Dates | N | WR | E[R] | PF |
|-------|-------|---|----|------|----|
| Train | 2025-06-17 → 2026-04-09 | 65 | 40% | −0.02 | 0.91 |
| Holdout | 2026-04-13 → 2026-08-14 | 21 | 29% | **−0.30** | 0.88 |
| Full | 2025-06-17 → 2026-08-14 | 86 | 37% | −0.09 | 0.90 |

Holdout is worse than train, not a lucky full-sample loss.

---

## 9. Walk-forward (same four Phase 35 blocks)

| Block | Dates | N | WR | E[R] | PF |
|-------|-------|---|----|------|----|
| 1 | 2025-06-17 → 2025-09-29 | 26 | 50% | +0.23 | 1.17 |
| 2 | 2025-09-30 → 2026-01-16 | 23 | 26% | −0.37 | 0.54 |
| 3 | 2026-01-20 → 2026-04-24 | 20 | 45% | +0.11 | 1.50 |
| 4 | 2026-04-28 → 2026-08-14 | 17 | 24% | −0.43 | 0.67 |

Two of four blocks are positive. The losing blocks are larger in magnitude. Not reasonably stable.

---

## 10. Threshold robustness

Phase 35 train median ±10%:

| Shallow cap | Setups | Resolved | Full E[R] |
|-------------|--------|----------|-----------|
| 16.425 | 105 | 82 | −0.104 |
| **18.25** | 114 | 86 | **−0.088** |
| 20.075 | 119 | 88 | −0.081 |

Nearby thresholds remain negative. The loss is not an 18.25-point knife-edge.

---

## 11. Reclaim analysis

- **A (range 1m):** slightly better full-sample 1.5R than B, still holdout-negative. Sweep-bar wick-only reclaim is conservatively refused (order of sweep vs reclaim unknown on 1m).
- **B (1m close):** predeclared primary. Leak-safe. Fails.
- **C (5m close):** prettier, tiny N. Not a candidate.

Slower expiry (10 min) on B: E[R] = −0.035 (N=89). Faster (1 min): E[R] = −0.033 (N=71). None of the predeclared expiry family is a tradable edge.

---

## 12. Reclaim-speed analysis

Among primary entries, lag from confirmation to reclaim close:

| Bucket | N | E[R] |
|--------|---|------|
| ≤60s | 71 | −0.03 |
| 61–180s | 12 | −0.18 |
| 181–300s | 3 | −1.02 |

Most reclaims are immediate (same bar close or next minute). Fast reclaim is not a hidden gold mine; it is still slightly negative. Not added as a rule.

---

## 13. Session analysis

| Slice | N | WR | E[R] |
|-------|---|----|------|
| 09:30–10:00 | 48 | 40% | −0.02 |
| 10:00–11:30 | 21 | 43% | +0.05 |
| 11:30–13:30 | 11 | 27% | −0.34 |
| 13:30–15:30 | 6 | 17% | −0.61 |
| First RTH 1m bar | 15 | 40% | −0.01 |
| Later session | 71 | 37% | −0.10 |

The strategy is concentrated in the opening hour (48/86). Later-session N is too small to claim a tradable pocket. First-bar events were **not** excluded (train/holdout did not consistently support a hidden filter).

---

## 14. Long / short (primary B 1.5R)

| Side | N | WR | E[R] | E[pts] |
|------|---|----|------|--------|
| Long (PDL) | 35 | 34% | −0.16 | −3.45 |
| Short (PDH) | 51 | 39% | −0.04 | +0.69 |

Neither side is a standalone edge after costs. Phase 35’s PDH/PDL classification symmetry does not become a profitable short book.

---

## 15. News compatibility

Full CPI/NFP days were already removed in Phase 35 (25 sweeps). Strict T−5m → T+5m around 08:30 removes **0** RTH entries. Phase 36 is not a hidden news strategy. No trade required stop/target changes inside an 08:30 blackout.

---

## 16. Portfolio relationship (read-only)

**NQ DVP** historical trades vs 90 Phase 36 entries:

| Metric | Value |
|--------|-------|
| Entered days | 90 |
| Same-day overlap | 89 |
| Same-hour overlap | 37 |
| Direction agree / conflict | 36 / 53 |
| Daily P&L correlation | **−0.08** (85 common days) |
| Losing-day overlap | 31 |

Calendars overlap almost completely, as Phase 35 warned. Realized P&L is **uncorrelated**, not a hedge and not a clone. That is moot: the sweep reclaim book loses money.

**GC V2** paper journal is still empty. Loss-day overlap undefined.

---

## 17. Prop geometry (primary entries)

| Metric | Value |
|--------|-------|
| Average stop | 15.3 NQ pts (**$306** per 1 NQ) |
| p90 stop | 26.5 pts ($530) |
| p95 stop | 33.0 pts ($660) |
| Max trades / day | 1 (first-of-day rule) |
| Max consecutive losses | 6 |
| Worst historical day | −39.2 pts (**−$784** / NQ) |
| Ambiguity rate | 4/90 = 4.4% |

Stops are prop-sizable on MNQ. The problem is expectancy, not geometry.

---

## 18. Recommendation

**Do not freeze. Do not paper this as Strategy #3.**

Keep Phase 35 on the shelf as a **research classifier**: shallow PDH/PDL sweeps reverse more often than deep ones. Phase 36 shows that a simple, realistic reclaim entry with a sweep-extreme stop does not harvest that tendency after costs and chronological split.

Candidate C is not a consolation prize. N=38 / holdout 6 is `INSUFFICIENT_TRADE_SAMPLE` if it were the primary.

Possible later work (not authorized here):

- Tick/trade data to time the reclaim inside the sweep bar without assuming target-first — only if a later phase explicitly asks, and still without DOM features.
- Leave deep-sweep continuation as a **future research family**. The one-config diagnostic (enter with the sweep, stop at the level) had average risk **113 pts** ($2,265 / NQ) and E[R] ≈ 0. That is not a strategy.

Phase 35 finding is preserved. Frozen GC V2 and NQ DVP are untouched.

---

## Files

- `docs/PHASE36_NQ_SHALLOW_SWEEP_STRATEGY.md` (this file)
- `phase36_spec.json`, `phase36_validation.json`, `phase36_validate.py`
- `nq_shallow_sweep_engine.py`, `tests_phase36.py`
- `reports/phase36_*.csv`
- `journal/phase36_nq_shallow_sweep/trades_primary.jsonl`

**Not created:** `strategy_candidates/phase36_NQ_SHALLOW_SWEEP_RECLAIM.json` (not justified).  
**Not touched:** `strategy_frozen/`.
