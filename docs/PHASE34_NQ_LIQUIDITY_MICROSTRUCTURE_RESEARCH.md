# Phase 34 — NQ Liquidity Sweep + Order-Flow Microstructure Research

**Verdict: `MICROSTRUCTURE_PROMISING_NEEDS_MORE_DATA`**

**Execution: DRY_RUN / no broker orders.** Frozen hashes for GC VWAP V2 and NQ DVP were identical before and after this phase. Nothing was frozen.

Question asked:

> When NQ objectively sweeps a known structural liquidity level, does exchange-level order-flow / order-book behavior contain enough information to distinguish a genuine reversal from continued price discovery?

Answer on this pilot: **not yet, and not in the direction the ICT-style story predicts.** Twenty NQM6 PDH/PDL sweeps with windowed CME MBP-10 show that the **structural sweep itself** already carries more signal than the book features that were supposed to refine it. Shallow penetrations reverse; deep open-drive sweeps continue. Absorption, signed aggressive flow, and top-of-book imbalance aligned *for* reversal do not raise P(reversal) in a threshold-stable way. One unsigned depth metric (top-10 bid share) showed a +30 percentage-point median split that **collapsed at the 70th percentile** on N=10 per half.

This candidate is **not frozen**. No entry logic was built.

---

## 1. Executive conclusion

| Status | `MICROSTRUCTURE_PROMISING_NEEDS_MORE_DATA` |
|--------|--------------------------------------------|
| Why this status, not FOUND | N=20; no walk-forward of a book rule; largest book lift is not threshold-stable |
| Why not REJECTED | Data are feasible and cheap when windowed; one depth metric is suggestive; need a bearish regime and a larger sample before falsifying |
| Why not DATA_NOT_FEASIBLE | Credential works. MBO, MBP-10, trades exist on GLBX.MDP3. Pilot MBP-10 windows cost **$2.92** |
| Why not EDGE_FOUND / WEAK | FOUND would require a stable lift on holdout. WEAK would require a larger sample that still fails. This sample cannot decide |

OHLCV was never treated as a substitute for the book. Features used Databento `mbp-10` snapshots plus `trades` with a hard cutoff at the sweep-bar close.

---

## 2. Frozen strategy integrity

Confirmed before and after the validator:

| Artifact | Hash |
|----------|------|
| GC V2 `frozen_config_hash` | `0695d7a881b6247033426bceb077b6895b96405b826c21f32a109a02f44b2d43` |
| NQ DVP `frozen_config_hash` | `935e3a616351b09dbfa8d2d0e2b5d6850be803fb385ec5e5f8e3593856a1212a` |
| `strategy_frozen/gc_vwap_v2_phase26.json` SHA-256 | `12615d19ef3deed36b7929c161ef7c377975cd2317c0ae6d06f1949bd327046f` |
| `strategy_frozen/nq_dvp_phase30.json` SHA-256 | `34927fba0f268e56cf166cf8939bdfec6aa031510f7cee9acfab3909dd36b541` |
| Phase 26 / 30 paper journals | still empty (`e3b0c442…`) |

`tests_phase34.py`: 9 passed (prior-session PDH/PDL, holiday skip, first-touch only, same-bar ambiguous, outcome path starts after sweep bar, future-horizon isolation, feature cutoff ignores post-close records, span merge, frozen bytes).

---

## 3. Repository audit — what was reused

Inspected, not used to mutate frozen files:

| Component | Reuse |
|-----------|--------|
| Databento adapter + credential | `databento_history.py`, `.env` `DATABENTO_API_KEY` |
| NQM6 1m OHLCV | `bar_dataset.load_dataset` on `data/databento/NQ/contracts/` (front month only; no stitch mix) |
| Rolls | `data/databento/NQ/stitched/rolls.jsonl` — NQM6 front 2026-03-16 → 2026-06-14 |
| Closed-bar helpers | `closed_candles.bar_close_ts`, `filter_closed_bars` |
| NY/DST clock | `zoneinfo.ZoneInfo("America/New_York")` |
| Frozen DVP journal (read-only) | `journal/phase29_nq_drift_vwap/trades.jsonl` |
| Candidate / registry pattern | `strategy_candidates/`, `docs/STRATEGY_REGISTRY.md` |
| Isolation | SHA-256 snapshot of freeze JSON and empty paper journals |

**Existing liquidity code — not this study:**

- `liquidity_sweep.py` / Phase 17–20: session high/low + CHoCH/FVG on OHLC. Retired. Not PDH/PDL + book.
- Phase 21 `liquidity_reclaim_v1`: OHLC reclaim, no MBO/MBP.
- No CVD, no order-book reconstruction, no OFI engine in the repo before this phase.

**New (research-only):**

- `nq_microstructure_models.py`, `nq_pdh_pdl.py`, `nq_microstructure_features.py`
- `phase34_validate.py`, `phase34_databento_feasibility.py`, `tests_phase34.py`
- `journal/phase34_nq_microstructure/`, `reports/phase34_*`, `data/databento/NQ/microstructure/` (gitignored `.dbn.zst`)
- `strategy_candidates/phase34_NQ_LIQUIDITY_MICROSTRUCTURE.json` (**not a freeze**)

Look-ahead controls: PDH/PDL from a **completed prior RTH only**; first sweep per side per day; outcome path starts at sweep-bar **close**; predictive book/trade features use `ts <= sweep_bar_time + 60`. Memorial Day 2026-05-25 is skipped as a PDH/PDL source (Friday 05-22 is used instead).

---

## 4. Data feasibility

Dataset: `GLBX.MDP3`. Credential present. Probe symbol `NQM6`.

| Schema | History start | RTH 1-day $ | RTH 1-day records | 1 month $ | 1 year $ |
|--------|---------------|-------------|-------------------|-----------|----------|
| `mbo` | 2017-05-21 | 1.20 | 12.8M | 32.21 | 165.53 |
| `mbp-10` | 2010-06-06 | 1.74 | 10.2M | 47.79 | 243.91 |
| `mbp-1` | 2010-06-06 | 0.88 | 6.5M | 23.13 | 118.85 |
| `tbbo` | 2010-06-06 | 0.51 | 244k | 11.09 | 54.40 |
| `trades` | 2010-06-06 | 0.31 | 244k | 6.66 | 32.64 |
| `ohlcv-1m` | 2010-06-06 | ~0.00 | 390 | 0.07 | 0.42 |

**MBO vs windowed MBP-10:** Databento warns that MBO ranges should start at UTC midnight so a snapshot exists. A mid-session MBO slice **cannot reconstruct the book**. MBP-10 carries 10 levels on every record and **is** reconstructable in a ±60s window.

Continuous-contract `stype_in=continuous` is **not** supported on GLBX.MDP3 for this symbology pair. This pilot used **raw `NQM6` only**. Rolls are explicit: do not mix NQH6/NQM6/NQU6 in one event.

API permission: historical metadata and `timeseries.get_range` succeeded for `mbp-10` and `trades` on `NQM6`.

Full probe: `reports/phase34_databento_feasibility.json`.

---

## 5. Pilot data specification

| Field | Value |
|-------|--------|
| Instrument | CME E-mini Nasdaq-100 futures |
| Contract | **NQM6 only** (no CFD, no index, no month mix) |
| Schema | `mbp-10` + `trades` (windowed). MBO not downloaded |
| Session | RTH 09:30–16:00 America/New_York (DST-aware) |
| Sweep cutoff | no new sweep after 15:45 |
| Dates | 2026-05-13 → 2026-06-12 weekdays |
| Excluded | Memorial Day 2026-05-25; NFP 2026-06-05; CPI 2026-06-10 |
| Trading days in sample | 20 |
| Sweeps | 20 (15 PDH, 5 PDL) |
| Feature window | T−60s through **bar close** (T+60s). Download stored through T+120s but post-close records are ignored for features |
| Roll | NQM6 is front-month from volume crossover 2026-03-16 until 2026-06-14 |

Window cost: MBP-10 **$2.92** (17.1M records) + trades **$0.55** (439k records). Compressed local size **137 MB** (40 files). Under the $25 cap.

---

## 6. Structural sweep definition

**PDH / PDL** = high / low of the previous New York date that has at least 30 RTH 1-minute bars, skipping weekends and listed holidays.

**Upside sweep:** first RTH 1m bar with `high > PDH`.

**Downside sweep:** first RTH 1m bar with `low < PDL`.

A touch that does not trade through the level is not a sweep. Only the first event per side per day is kept. The sweep is an **event anchor**, not a trade.

Recorded: level, bar time, penetration (extreme − level), seconds from 09:30, 14-period 1m ATR at the bar, sweep-bar volume, source date of PDH/PDL.

---

## 7. Outcome definition (declared before features)

Labels are computed on 1m OHLC **after** the sweep bar close. They were not retuned after seeing book results.

| Parameter | Value |
|-----------|--------|
| Horizons | 30s, 60s, 180s, 300s, 900s |
| Primary | 300 seconds |
| Reversal target | 8.0 NQ points (32 ticks) beyond the structural level after reclaim |
| Continuation | 12.0 points beyond the sweep extreme before a qualifying reversal |
| PDL reversal | reclaim `high ≥ PDL`, then MFE ≥ 8 pts above PDL, without first printing `low ≤ extreme − 12` |
| PDH reversal | inverse |
| Same 1m bar hits both | `AMBIGUOUS` |
| Neither condition inside horizon | `NEITHER` |

**Limitation (predeclared, not patched):** 30s and 60s horizons are empty on 1m data because the outcome path starts at bar close. Those rows are all `NEITHER` and are **not** evidence that sub-minute reversals do not exist.

---

## 8. Feature definitions

Cutoff: `t_cut = sweep_bar_time + 60`. Records with `ts_event > t_cut` are counted then discarded.

Let `B_k`, `A_k` be displayed bid / ask size summed over the top *k* levels of the last MBP-10 record with `ts ≤ t_cut`. Let `B^pre`, `A^pre` be the same at the first record with `ts` in `[t_cut−120, t_open+1]` (lookback book). Aggressive buy / sell size uses Databento aggressor `side`: `B` = buy aggressor, `A` = sell aggressor, summed over trades with `t_open−60 ≤ ts ≤ t_cut`.

| Feature | Formula | Notes |
|---------|---------|--------|
| `trade_imbalance` | buy − sell | lots |
| `signed_flow_for_reversal` | +(buy−sell) on PDL, −(buy−sell) on PDH | hypothesis: positive favors reversal |
| `book_imbalance_topk` | `B_k / (B_k + A_k)` | 0 if both empty |
| `imb_for_reversal_top1` | imbalance on PDL, `1 − imbalance` on PDH | hypothesis: >0.5 favors reversal |
| `ofi_top1_proxy` | `(B_1 − B_1^pre) − (A_1 − A_1^pre)` | not true Cont OFI; MBP delta only |
| `ofi_for_reversal` | side-signed OFI proxy | |
| `absorption_proxy` | `(buy+sell) / (max_px − min_px)` | 0 displacement → undefined |
| `price_impact_per_lot` | range / (buy+sell) | inverse absorption |
| `executed_to_displayed` | aggressive size into the swept side / top-1 displayed | **reload proxy, not an iceberg proof** |
| `bid_slope`, `ask_slope` | top-10 size / \|px_09 − px_00\| | lots per point |
| `bid_ask_slope_ratio` | bid_slope / ask_slope | thick below vs thick above |

**Not measured in this pilot (need full-day MBO + order IDs):** replenishment_ratio, persistence_ratio, pre-touch cancellation, survival time. Neutral language only — no spoofing / iceberg claims.

---

## 9. Baseline — sweep only

Primary horizon **300s**, N=20:

| Side | N | Reversal | Continuation | Neither | Ambiguous | P(rev) | P(rev \| decided) |
|------|---|----------|--------------|---------|-----------|--------|-------------------|
| PDL | 5 | 1 | 2 | 1 | 1 | 0.20 | 0.33 |
| PDH | 15 | 8 | 5 | 2 | 0 | 0.53 | 0.62 |
| ALL | 20 | 9 | 7 | 3 | 1 | **0.45** | **0.56** |

Other horizons (ALL): 30s/60s = 0 (unusable on 1m); 180s P(rev)=0.40; 900s P(rev)=0.50.

Chronological 70/30 by date: train P(rev)=0.38 (N=13), holdout P(rev)=0.57 (N=7). The **unconditional baseline is not stable** across a month.

**Structure-only (not microstructure), 300s reversal, median split N=10/10:**

| Feature | Spearman | P(rev) low | P(rev) high | Lift |
|---------|----------|------------|-------------|------|
| penetration_points | −0.50 | 0.70 | 0.20 | **−50 pp** |
| volume_sweep_bar | −0.41 | 0.70 | 0.20 | **−50 pp** |
| seconds_from_rth_open | +0.47 | 0.30 | 0.60 | +30 pp |

Deep, high-volume, opening-drive sweeps continue. Shallow later sweeps reverse more often. That is the bar the book has to beat.

News blackout: excluding CPI/NFP days removed **1** sweep from the weekday set. Default research blackout remains configurable `T−5m → T+5m`; this pilot dropped whole print days.

---

## 10. Microstructure comparison

All 20 events had an MBP-10 book at cutoff (`n_events_with_book = 20`).

Baseline P(rev)=0.45. Median split (feature > sample median):

| Feature | Spearman | P(rev) ≤ med | P(rev) > med | Lift | Threshold-stable? |
|---------|----------|--------------|--------------|------|-------------------|
| `book_imbalance_top10` | +0.18 | 0.30 | 0.60 | **+30 pp** | same sign; **p70 lift only +7 pp** |
| `imb_for_reversal_top1` | −0.25 | 0.50 | 0.40 | −10 pp | stable **opposite** the hypothesis |
| `absorption_proxy` | −0.24 | 0.50 | 0.40 | −10 pp | no (and wrong sign) |
| `signed_flow_for_reversal` | −0.06 | 0.40 | 0.50 | +10 pp | no |
| `ofi_for_reversal` | +0.02 | 0.40 | 0.50 | +10 pp | no |
| `executed_to_displayed` | −0.08 | 0.40 | 0.50 | +10 pp | no; median ratio ~4900 (top-1 size is tiny vs volume) |
| `price_impact_per_lot` | +0.24 | 0.40 | 0.50 | +10 pp | no |
| `bid_ask_slope_ratio` | +0.04 | 0.40 | 0.50 | +10 pp | weak (19 / 8 / 7 pp) |

Quartiles for top-10 bid share: Q1 0.20, Q2 0.40, Q3 0.60, Q4 0.60 — monotone-ish, but N=5 per cell.

**Does the book beat the sweep?** Against unconditional 45%, top-10 bid share 60% looks like a lift. Against **shallow penetration (70%)**, it does not. Absorption (the core “aggressive volume, little displacement” hypothesis) points the **wrong way**. Top-1 reversal-aligned imbalance also points the wrong way.

`executed_to_displayed` in the thousands is a scaling artifact (top-1 lots vs thousands of lots traded), not evidence of hidden liquidity.

No logistic model, no tree, no genetic search. Deciles with n=2 are noise and were not used for the verdict.

---

## 11. Robustness

- Chronological holdout reversal rate 57% vs train 38% — labels themselves move with the May–June 2026 grind higher (15 PDH vs 5 PDL).
- Neighboring thresholds: only `book_imbalance_top10` kept a positive sign, and the effect **fades by p70**. That fails the “works at 7.0 and 7.5” spirit of the brief.
- Permutation / walk-forward of a book rule: **not run** (N too small; would overfit).
- 30s/60s labels: unusable until trade-time T is used for outcomes.
- Regime: one bullish NQ month. A selloff sample is required before any claim about PDL reversals.

---

## 12. Cost / execution stress

No entries were simulated. If a later phase ever tests a rule, the first fill model is a **market/stop after objective reclaim**, not a bid at the sweep extreme.

| Friction | Assumption |
|----------|------------|
| Tick | 0.25 index pts = $5 / NQ, $0.50 / MNQ |
| 1 tick adverse | $5 NQ |
| 2 tick adverse | $10 NQ |
| Round-turn exchange/clearing | typically a few dollars NQ / under $1 MNQ (prop accounts differ) |
| Queue / passive at the low | **rejected** as unmodelable in this phase |
| Latency | not modeled; research is classification at bar close |

An 8-point reversal target is 32 ticks ($160 NQ) before costs. That is irrelevant until classification actually works.

---

## 13. Portfolio relationship

**NQ DVP (read-only Phase 29 journal):**

- Same-day overlap: **19 / 20** sweep days (DVP trades almost every RTH day).
- Same-hour overlap: **9 / 20** sweeps.
- Hypothetical reversal vs DVP drift: **14 oppose**, 6 agree (PDH sweep during `POSITIVE_DRIFT`, PDL sweep during `NEGATIVE_DRIFT`).
- DVP mean points on those overlap days: +44.7 (DVP’s own P&L, not this strategy).
- P&L correlation: **undefined** — this family has no trades.
- Ensemble flags logged to `journal/phase34_nq_microstructure/ensemble_flags.jsonl` (e.g. `PDH_SWEEP+DVP_POSITIVE_DRIFT`). Not an ensemble. Standalone edge first.

**GC VWAP V2:** paper journal still empty. Different market. Active-day overlap with frozen paper = 0. No P&L correlation.

---

## 14. Recommendation

**Do not freeze. Do not build entries. Do not buy a year of full-day MBO yet.**

Worth additional **windowed MBP-10** research if, and only if, the sample can cover:

1. Several hundred sweeps across **both** bull and bear months.
2. Trade-time (not 1m bar-open) event timestamps so 30s–180s labels are real.
3. A predeclared comparison against the **shallow-vs-deep penetration** baseline, not only P(rev \| sweep).
4. Side-stratified book features (PDH and PDL separately), because this month is PDH-dominated.

Cost to scale windows: this 20-day slice was **$3.47** all-in. A year of similar windows is tens of dollars, not terabytes. Full RTH MBP-10 is ~$48/month and ~2 GB compressed/month. Full-day MBO (~$32/month) is justified only after MBP-10 shows a stable edge that specifically needs order-id reload/persistence.

MBP-10 is the right cheap representation until that happens. Top-of-book / trades-only (`tbbo`) would drop 10-level slope and depth; do not switch down until MBP-10 is shown to be redundant.

---

## Storage / processing (Phase S)

| Item | Estimate |
|------|----------|
| Pilot compressed | 137 MB / 40 files |
| MBP-10 bytes/RTH day (scaled) | ~80 MB compressed |
| 1 month full RTH MBP-10 | ~1.8 GB / ~$48 |
| 3 months | ~5.4 GB / ~$143 |
| 1 year | ~22 GB / ~$244 |
| MBO 1 year (full days, required for snapshots) | similar size, ~$166, **cannot** window |
| Pilot processing | ~12 minutes including download on this machine |

`reports/phase34_storage_estimate.json`.

---

## Files

- `docs/PHASE34_NQ_LIQUIDITY_MICROSTRUCTURE_RESEARCH.md` (this file)
- `phase34_validation.json`
- `reports/phase34_sweep_baseline.csv`, `phase34_sweep_events.csv`, `phase34_structure_deciles.csv`
- `reports/phase34_micro_features.csv`, `phase34_micro_median_splits.csv`, `phase34_micro_deciles.csv`
- `reports/phase34_window_cost.json`, `phase34_databento_feasibility.json`, `phase34_storage_estimate.json`
- `journal/phase34_nq_microstructure/sweeps.jsonl`, `ensemble_flags.jsonl`
- `strategy_candidates/phase34_NQ_LIQUIDITY_MICROSTRUCTURE.json`
- `tests_phase34.py`

**Not touched:** frozen GC V2, frozen NQ DVP, their journals, their validation files.
