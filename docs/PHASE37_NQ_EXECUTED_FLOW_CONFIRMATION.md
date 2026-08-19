# Phase 37 — NQ Shallow Sweep + Executed Volume / Delta Confirmation

**Verdict: `FLOW_CONFIRMED_STRATEGY_NOT_TRADABLE`**

**Branch: `CLOSE_NQ_SWEEP_RESEARCH_BRANCH`**

**Execution: DRY_RUN / no broker orders.** Nothing was frozen. No file was written to `strategy_frozen/`. No Phase 37 candidate JSON was promoted.

Question asked:

> Among already-validated shallow NQ PDH/PDL sweeps, does executed trade-flow behavior materially improve reversal prediction and produce a tradable entry that survives holdout, costs, and walk-forward validation?

Answer: **classification, weakly yes; a trade, no.** Among first-of-day shallow sweeps, reversal-aligned executed delta during the sweep bar is associated with higher Phase 35 reversal rates. That relationship has the expected sign on train, holdout, and all four walk-forward blocks. A leak-safe 1-minute close reclaim with a sweep-extreme stop still loses after costs, including on ideal fills. Volume burst, flow efficiency, and exhaustion proxies did not survive holdout.

This closes the standalone NQ PDH/PDL sweep strategy branch. Phase 35’s structural classifier remains research knowledge. Strategy #3 research should move to a different family. Do not add MBO, DOM, or more filters.

---

## 1. Verdict

| Status | `FLOW_CONFIRMED_STRATEGY_NOT_TRADABLE` |
|--------|----------------------------------------|
| Why this status | Predeclared classification gate passed on reversal-aligned sweep-bar delta. Phase 36 geometry on the confirmed subset: full E[R]=**−0.091** (N=46), holdout E[R]=**−0.180** (N=12). Ideal fills also negative. |
| Why not `EXECUTED_FLOW_EDGE_FOUND` | Realistic reclaim+stop does not become positive. Holdout trade sample is small and still loses. |
| Why not `NO_INCREMENTAL_FLOW_VALUE` | `ndelta_rev_sweep60` median split is +35pp on the full first+shallow sample, +19pp on holdout, same sign in 4/4 walk-forward blocks, `THRESHOLD_STABLE`. |
| Why not `PROMISING_NEEDS_MORE_DATA` | The trade failure matches Phase 36’s geometry problem (WR still ~37% at 1.5R). More data is unlikely to turn a −1R median into an edge. |
| Candidate JSON | **not written** |
| Branch | **`CLOSE_NQ_SWEEP_RESEARCH_BRANCH`** |

Predeclared confirmation used in the (failed) trade overlay:

`first shallow sweep` AND `reversal-aligned normalized delta during the sweep bar > TRAIN median (−0.092)` → Phase 36 Candidate B geometry.

---

## 2. Frozen integrity

Confirmed before and after `phase37_validate.py`:

| Artifact | Hash |
|----------|------|
| GC V2 `frozen_config_hash` | `0695d7a881b6247033426bceb077b6895b96405b826c21f32a109a02f44b2d43` |
| NQ DVP `frozen_config_hash` | `935e3a616351b09dbfa8d2d0e2b5d6850be803fb385ec5e5f8e3593856a1212a` |
| GC file SHA-256 | `12615d19ef3deed36b7929c161ef7c377975cd2317c0ae6d06f1949bd327046f` |
| NQ DVP file SHA-256 | `34927fba0f268e56cf166cf8939bdfec6aa031510f7cee9acfab3909dd36b541` |

`tests_phase37.py`: 8 passed (frozen hashes, Phase 35 18.25 reused, Ask=sell / Bid=buy, unsigned excluded from delta, post-cutoff trades ignored in classification features).

`methodology_corrections: []`. Nothing written to `strategy_frozen/`.

---

## 3. Phase 35/36 reproduction

Universe = `reports/phase35_sweep_events.csv` (269 news-filtered events). Labels = Phase 35 `label_300s`. Shallow = **18.25 pts** (Phase 35 train-only median). Not re-detected.

| Baseline | N | P(reversal) |
|----------|---|-------------|
| All Phase 35 eligible | 269 | 42.8% |
| First sweep of day | 247 | 40.1% |
| First + shallow + usable trades | **114** | **61.4%** |
| Train (through 2026-04-09) | 84 | 58.3% |
| Holdout (from 2026-04-13) | 30 | 70.0% |

Phase 36 primary B (no flow filter): full E[R]=−0.088, holdout E[R]=−0.304. Phase 37 does not re-search that geometry.

---

## 4. Data audit

| Item | Value |
|------|-------|
| Schema | Databento `GLBX.MDP3` `trades` |
| Cache | `data/databento/NQ/microstructure/trades/` (Phase 35 windows, **no new download**) |
| Files | **269 / 269** events covered. Missing = 0. None dropped silently. |
| Window | T_open−60s → T_open+120s |
| Timestamps | matching-engine `ts_event` (ns) |
| Side | Databento aggressor, used directly |
| Unsigned | **0** in this sample. Every trade had Ask or Bid. |
| Mean trades at cutoff (first+shallow) | 3,364 |
| Mean trades in sweep bar | 2,099 |
| Cash index / CFD | not used |
| MBP / MBO | not loaded |

Signing (official Databento trades schema):

| `side` | Label |
|--------|--------|
| Ask / `A` | `AGGRESSIVE_SELL` |
| Bid / `B` | `AGGRESSIVE_BUY` |
| None / `N` | `UNSIGNED` (volume only; none observed) |

No quote test, no tick rule, no future quotes.

Classification features use only trades with `ts <= sweep_bar_close`. Post-cutoff windows are diagnostic and were excluded from Models 0–6.

---

## 5. Structural baseline

P(reversal | first + shallow) = **61.4%** (70/114). That is already well above the 42.8% all-sweep rate. The Phase 37 question is whether executed flow lifts this further, not whether shallow sweeps reverse.

---

## 6. Delta

Primary leak-safe feature: **reversal-aligned normalized delta during the sweep bar** (`ndelta_rev_sweep60`).

- PDL: `(ABV−ASV)/(ABV+ASV)` — buying is positive.
- PDH: opposite sign — selling is positive.

| Sample | Spearman | Median split lo → hi | Lift |
|--------|----------|----------------------|------|
| Full first+shallow | **+0.38** | 44% → 79% | **+35pp** |
| Train | (train-median split) | 40% → 78% | **+39pp** |
| Holdout (train threshold) | +0.18 | 58% → 78% | **+19pp** |

Holdout high cell vs shallow holdout baseline 70%: **+8pp** (77.8%). Neighbor train percentiles 50/60/70/80 all keep a positive holdout lift. Status: **`THRESHOLD_STABLE`**.

Raw (not direction-normalized) delta is not the reported feature. Direction normalization was verified separately on PDL and PDH (section 15).

---

## 7. Cumulative delta

Event-local CVD is accumulated signed size from the cached window. `cvd_change_sweep` is CVD at cutoff minus CVD at bar open — equivalent in sign to sweep-bar delta. `cvd_divergence` therefore duplicates the binary `delta_divergence` flag and is not a second independent edge.

---

## 8. Volume participation

`volume_burst = vol(sweep_60) / max(vol(pre_60), 1)`. Pre-event denominator only. No cross-day same-TOD median (those trades were not cached).

Full-sample median split lift = **0**. Holdout Spearman **negative**. Opening-bar mean burst is 5.3 vs ~2.4 later — the feature largely tags the open. **`THRESHOLD_UNSTABLE`**. Not used as a rule.

---

## 9. Flow efficiency / exhaustion

`flow_efficiency = opposing_aggressive_volume / max(penetration, 0.25)` and `exhaustion_score = opposing_vol_ratio / (1 + penetration_ticks)`.

Both have weak full-sample Spearman and **flip sign on holdout**. Not incremental. Do not interpret as absorption.

---

## 10. Divergence

Binary `delta_divergence`: reversal-aligned ndelta > 0 during the sweep bar (flow already opposing the sweep). Full-sample hi cell 75% vs 58%. Holdout lift vs complementary cell = **0** (both 70%). Train-only. Not a rule.

---

## 11. Reclaim flow shift

These windows overlap the Phase 35 outcome path. Diagnostic only. Not in Models 0–6.

| Diagnostic | N | Median lift |
|------------|---|-------------|
| Post-cutoff 60s reversal-aligned ndelta | 114 | +32pp |
| Flow flip (sweep with sweep, post with reversal) | 114 | +28pp |
| Reclaim-window ndelta (reclaim inside cache) | 80 | +23pp |

They look strong because they are partly contemporaneous with the label. They were not used to pass the gate and must not be used as live confirmation without a later leak-safe design.

---

## 12. Incremental model

Universe = first + shallow + flow (train 84 / holdout 30). L2 logistic, TRAIN z-score only.

| Model | Holdout Brier |
|-------|----------------|
| 0 intercept (shallow baseline) | **0.224** |
| 1 penetration | 0.224 |
| 2 pen + TOD + opening bar | 0.241 |
| 3 Model 2 + ndelta | 0.230 |
| 4 Model 2 + volume burst | 0.241 |
| 5 Model 2 + efficiency | 0.241 |
| 6 Model 2 + ndelta + burst + exhaustion | 0.228 |

Predeclared gate: Model 6 holdout Brier ≤ Model 2 − 0.01. Observed lift **+0.013**. Gate **passes**.

Honest overlay: Model 6 is **0.005 worse** than intercept-only on holdout. Model 2 overfit time-of-day; flow partly undoes that damage. The **conditional frequency table for ndelta**, not the logistic stack, is the actual classification evidence.

Burst and efficiency add nothing once ndelta is present.

---

## 13. Chronological validation

`ndelta_rev_sweep60` train-median split, same sign in every Phase 35 block:

| Block | Dates | N hi / lo | P hi | P lo | Lift |
|-------|-------|-----------|------|------|------|
| 1 | 2025-06-17 → 2025-09-29 | 17 / 15 | 100% | 27% | +73pp |
| 2 | 2025-09-30 → 2026-01-16 | 17 / 14 | 59% | 43% | +16pp |
| 3 | 2026-01-20 → 2026-04-24 | 12 / 14 | 75% | 50% | +25pp |
| 4 | 2026-04-28 → 2026-08-14 | 13 / 12 | 77% | 58% | +19pp |

Block 1 is large. Blocks 2–4 remain positive. Sign does not flip.

First+shallow reversal rates by block: 66% / 52% / 62% / 68%.

---

## 14. Threshold stability

For ndelta, holdout lift stays positive at train p50 / p60 / p70 / p80 (19pp / 15pp / 11pp / 12pp). Marked **`THRESHOLD_STABLE`**.

Volume burst, efficiency, exhaustion, and price-impact are **`THRESHOLD_UNSTABLE`** or holdout-sign-flip.

---

## 15. Long / short

Direction-normalized ndelta works on both sides.

| Side | N | P(rev) | Ndelta Spearman | Median lift |
|------|---|--------|-----------------|-------------|
| PDL (long reversal) | 51 | 57% | +0.45 | 35% → 80% (+45pp) |
| PDH (short reversal) | 63 | 65% | +0.35 | 50% → 81% (+31pp) |

Not a one-sided artifact.

---

## 16. Session regime

| Slice | N | P(rev) | Mean volume burst |
|-------|---|--------|-------------------|
| First RTH 1m bar | 19 | 79% | 5.33 |
| First 30m | 64 | 64% | 2.50 |
| First 60m | 75 | 64% | 2.37 |
| Later session | 39 | 56% | 2.43 |

Ndelta is not merely an opening-auction dummy: later-session N is smaller, but burst (the open-sensitive feature) already failed holdout. First-bar events were not excluded.

---

## 17. Strategy construction

Classification gate passed, so Phase 36 geometry was applied **only** to first+shallow events with ndelta above the train median (59 setups).

Candidate B (1m close reclaim, 5 min, 1.5R, 1-tick, $4 RT):

| Split | Setups | Entered | Resolved | WR | E[R] | PF | Max DD pts |
|-------|--------|---------|----------|----|------|----|------------|
| Full | 59 | 49 | 46 | 37% | **−0.091** | 0.77 | 251 |
| Holdout | 18 | 13 | 12 | 33% | **−0.180** | 0.72 | 90 |

Ambiguous 3/49 (6%). Expired 6. Win rate is the same ~37% as unfiltered Phase 36 B. Selecting higher-label-reversal events did not raise 1.5R hit rate enough to pay the stop. Median R ≈ −1.0.

Candidate A (range reclaim) full E[R]=**−0.008** — still not positive.

No further variants.

---

## 18. Fill / cost stress (B, 1.5R)

| Overlay | Full E[R] | Holdout E[R] |
|---------|-----------|--------------|
| Ideal 0 tick | **−0.092** | −0.180 |
| **1 tick entry (primary)** | **−0.091** | **−0.180** |
| 2 tick entry | −0.111 | −0.180 |

A candidate that survives only ideal fills must not pass. This one **does not survive ideal fills either**.

---

## 19. Portfolio relationship

Read-only vs frozen NQ DVP historical trades (49 entered days):

| Metric | Value |
|--------|-------|
| Same-day overlap | 48 / 49 |
| Same-hour overlap | 12 |
| Direction agree / conflict | 20 / 28 |
| Daily P&L correlation | **−0.04** |
| Losing-day overlap | 15 |

Calendars overlap; P&L is uncorrelated. Moot: the sweep-flow book loses. GC V2 paper journal is empty.

---

## 20. Final branch recommendation

**`CLOSE_NQ_SWEEP_RESEARCH_BRANCH`**

Do not freeze. Do not paper this as Strategy #3. Do not escalate to MBO. Do not add DOM, CVD-as-magic, or more microstructure filters.

What is preserved as research:

1. Phase 35: shallow PDH/PDL sweeps reverse more often than deep ones.
2. Phase 37: among those shallow first sweeps, **reversal-aligned executed delta during the sweep bar** further grades reversal probability (~44% vs ~79% at the median split; holdout 58% vs 78%).
3. Phases 36–37: a simple reclaim, next-bar market fill, and sweep-extreme stop **does not monetize** that classifier after realistic costs.

Strategy #3 research should start a **different family**.

---

## Files

- `docs/PHASE37_NQ_EXECUTED_FLOW_CONFIRMATION.md` (this file)
- `phase37_spec.json`, `phase37_validation.json`, `phase37_validate.py`
- `nq_executed_flow_features.py`, `tests_phase37.py`
- `reports/phase37_*.csv`, `reports/phase37_incremental.json`
- `journal/phase37_nq_executed_flow/trades_flow.jsonl`

**Not created:** `strategy_candidates/phase37_NQ_SHALLOW_SWEEP_FLOW.json` (not justified).  
**Not touched:** `strategy_frozen/`.
