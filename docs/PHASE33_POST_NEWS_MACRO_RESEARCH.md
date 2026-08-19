# Phase 33 — Post-News Macro Repricing Research

**Verdict: `MACRO_EDGE_REJECTED`**

**Execution: DRY_RUN / no broker orders.** Frozen hashes for GC VWAP V2 and NQ DVP were identical before and after this phase.

Question asked:

> After a high-impact scheduled economic release, and after the prop-firm news blackout has expired, is there a persistent, reproducible, cost-adjusted directional edge in NQ/ES/GC that is sufficiently different from frozen NQ DVP and GC VWAP V2 to deserve a place in the AITRADE portfolio?

Answer: **No.** The 08:30–08:35 impulse does not leave a statistically usable continuation regime after `08:35` ET. Event-range breakouts lose money. The only cells with positive expectancy are small-N first-pullback variants that fail walk-forward and concentrate in a subset of history.

This candidate is **not frozen**.

---

## 1. Repository audit — what was reused

Inspected, not modified:

| Component | Reuse |
|-----------|--------|
| Candidate JSON pattern | `strategy_candidates/` (new family only) |
| Chrono split / walk-forward / cost overlays | `phase29_validate.py` (`score_trades`, `split_dev_oos`, `walkforward`) |
| Closed-bar helpers | `closed_candles.filter_closed_bars`, `bar_close_ts` |
| NQ Databento 1m/5m stitched | `bar_dataset.load_dataset` + `data/databento/NQ/stitched/` (2020-01-01 → 2026-08-14) |
| GC Databento 5m stitched | limited sample 2025-08-01 → 2026-08-14 |
| NY/DST session clock | `zoneinfo.ZoneInfo("America/New_York")` |
| OpenBB macro inventory | Phase 16 inspect-only routes; calendar **not fetchable** (missing FMP / TradingEconomics / FRED keys) |
| Frozen DVP journal (read-only) | `journal/phase29_nq_drift_vwap/trades.jsonl` |
| GC V2 | in-memory Phase 25 replay; **did not write** Phase 26 paper journal |
| Isolation | SHA-256 file snapshot of freeze JSON/MD and empty paper journals |

**New (research-only):**

- `macro_calendar.py`, `nq_post_news_models.py`, `nq_post_news_engine.py`
- `phase33_validate.py`, `tests_phase33.py`
- `data/macro/`, `journal/phase33_post_news_macro/`, `reports/phase33_*.csv`
- `strategy_candidates/phase33_POST_NEWS_MACRO.json` (documentation of a residual cell, **not a freeze**)

**Not touched:**

- `strategy_frozen/gc_vwap_v2_phase26.json` hash `0695d7a881b6247033426bceb077b6895b96405b826c21f32a109a02f44b2d43`
- `strategy_frozen/nq_dvp_phase30.json` hash `935e3a616351b09dbfa8d2d0e2b5d6850be803fb385ec5e5f8e3593856a1212a`
- Phase 26 / 30 paper journals (still 0 bytes)

**Could not reuse (does not exist):**

- Economic calendar / surprise store
- ES futures history
- Treasury or DXY series
- ATR engine (computed a simple 14-period 5m mean TR instead)
- Portfolio manager

News blackout is a **configurable** `PropFirmNewsProfile` (`DEFAULT_CONSERVATIVE_PM_5_5`). It is not hard-coded as every firm’s rule.

---

## 2. Data audit

### 2.1 Event calendar

| Field | Value |
|-------|--------|
| Source | BLS archived news-release index HTML (`/bls/news-release/cpi.htm`, `empsit.htm`) |
| Publication dates | Filename `cpi_MMDDYYYY.htm` / `empsit_MMDDYYYY.htm` |
| Timezone | America/New_York, DST-aware |
| Timestamp | `08:30` ET unless an embargo line says otherwise |
| Coverage | 2020-01-10 → 2026-08-12 |
| CPI events | 79 |
| NFP / Employment Situation events | 79 |
| Store | `data/macro/bls_events.jsonl` |

Sample embargo lines from live HTML (previously fetched):

- NFP 2021-01-08: `8:30 a.m. (ET) Friday, January 8, 2021`
- CPI 2021-01-13: `8:30 a.m. (ET) January 13, 2021`
- NFP 2026-08-07: `8:30 a.m. (ET) Friday, August 7, 2026`
- CPI 2026-08-12: `8:30 a.m. (ET) Wednesday, August 12, 2026`

### 2.2 Consensus / surprise — unavailable

`raw_surprise = actual − consensus` was **not computed**.

| Attempt | Result |
|---------|--------|
| OpenBB `obb.economy.calendar` FMP / TradingEconomics / FRED | Missing credentials |
| Nasdaq `api/calendar/economicevents?date=` | Ignores historical date; returns the current session only |
| FRED / ALFRED vintages | No `FRED_API_KEY` |

Headline first-prints were **not bulk-downloaded** (BLS HTML fetch hung / rate-limited). Dates and 08:30 timestamps are sufficient for the market-confirmed test. Actual/consensus/surprise fields in the event file are explicitly `UNAVAILABLE`.

**Implication:** this phase tests *market-confirmed* post-blackout direction, not “CPI below forecast ⇒ buy NQ.” That is aligned with Phase D of the brief. A later phase can add leakage-safe survey vintages; it would not rescue a continuation effect that already fails after 08:35.

### 2.3 Price data

| Instrument | Store | Span | Used as signal? |
|------------|--------|------|-----------------|
| CME NQ | Databento GLBX.MDP3 stitched 1m/5m | 2020-01-01 → 2026-08-14 | **Primary** |
| CME GC | Databento 5m stitch | 2025-08-01 → 2026-08-14 | Limited (~12 prints/family) |
| CME ES | **None** | — | Not substituted with cash/CFD |
| UST / DXY | **None** | — | Omitted |

Known calendar hole: October 2025 Employment Situation and CPI were disrupted by the federal appropriations lapse. Those prints are whatever BLS archived (including delayed `cpi_10242025`).

---

## 3. Hypotheses tested (deterministic, predeclared)

**Blackout (default profile):** no new order, close, stop change, target change, or size change from `release − 5m` through `release + 5m` (typically `08:25:00`–`08:35:00` ET). The engine never trades the 08:30 print.

**Regime at 08:35** (completed 1m bars in `[08:30, 08:35)` only):

```
ref           = last completed 1m close at 08:30 (the 08:29 bar)
event_range   = high−low of those 1m bars
signed_move   = event_close − ref
ATR           = 14-period 5m mean true range, bars completed before 08:25
MACRO_BULLISH if signed_move ≥ 0.50·ATR
              and event_range ≥ 0.75·ATR
              and retention ≥ 0.50
              and event_close ≥ mid-range
MACRO_BEARISH = inverse
else MACRO_NEUTRAL
```

Retention = fraction of the directional extreme still held at 08:35.

**Entry families (independent, one trade per event, flatten 15:55, 1.0R, fail-closed if stop and target hit the same bar):**

| ID | Rule after blackout |
|----|---------------------|
| A Event-range breakout | First 1m **close** beyond event high/low, stop = opposite extreme |
| B First pullback | First opposing 5m close, then 5m close beyond that pullback bar, stop = pullback extreme |
| C 5m close confirm | First eligible completed 5m bar must close beyond event close; else skip |
| D Cash-open regime | Lock regime at 08:35; require price still on the same side of `ref` at 09:30; first 5m close after 09:30 must confirm |

Delays tested: **+5 / +10 / +15 / +30 / +60 minutes** after 08:30 (60 minutes = 09:30). CPI and NFP never pooled for inference. Long and short scored separately.

Look-ahead: 5m bar timestamp `T` is not used until `T+300s`. Tests in `tests_phase33.py` (6/6 passed), including “a 10:00 spike does not change the 08:35 regime.”

---

## 4. Experimental results

### 4.1 Regime sample (NQ)

| Family | N prints | Bullish | Bearish | Neutral | Missing bars |
|--------|----------|---------|---------|---------|--------------|
| CPI | 79 | 38 | 25 | 15 | 1 |
| NFP | 79 | 36 | 23 | 20 | 0 |

### 4.2 Unconditional continuation after 08:35 (no entry mechanics)

Mean **signed** NQ points from event close, same direction as the regime. Positive = continuation.

| Cell | N | +5m | +10m | +15m | +30m | +60m / 09:30 | 15:55 |
|------|---|-----|------|------|------|----------------|-------|
| CPI bullish | 38 | −2.4 (hit 55%) | −2.6 | +0.3 | −2.2 | −11.0 / −3.5 | −21.2 |
| CPI bearish | 25 | **+9.8 (60%)** | +8.8 | +9.3 | +15.0 | +4.0 / +7.4 | −8.5 |
| NFP bullish | 36 | −4.8 (50%) | −8.9 | −4.8 | −2.9 | −12.5 / −14.0 | −34.5 |
| NFP bearish | 23 | −7.6 (35%) | −7.5 | −6.4 | +1.6 | −15.2 / −19.4 | −5.6 |

Three of four cells mean-revert as soon as the blackout ends. CPI bearish is the only positive pocket, N=25, and it is gone by the cash close. That is not a portfolio-grade edge.

### 4.3 Entry families (ideal fills, NQ points)

**Immediately after blackout (delay = 5 minutes):**

| Family | Event | N | Win% | E[pts] | PF | Max DD | Long E | Short E |
|--------|-------|---|------|--------|----|--------|--------|---------|
| A breakout | CPI | 55 | 42% | **−33.9** | 0.53 | 1897 | −1.1 | −76.3 |
| A breakout | NFP | 51 | 39% | **−32.7** | 0.48 | 1809 | −36.8 | −26.4 |
| B pullback | CPI | 41 | 41% | −5.5 | 0.82 | 537 | −20.8 | +14.1 |
| B pullback | NFP | 47 | 55% | −2.1 | 0.93 | 655 | +3.0 | −10.3 |
| C 5m close | both | 0 | — | — | — | — | — | — |
| D cash open | both | 0 | — | — | — | — | — | — |

Family C at +5m has N=0 because the first eligible 5m bar **is** the event bar; its close cannot be strictly beyond itself. Family D has N=0 because the first 5m after 09:30 rarely confirms under the strict first-bar rule while price is still on the regime side of `ref`. Those zeros are implementation-strict, not hidden profits.

**Family A stays negative at every delay (5–60m), CPI and NFP, long and short.** Breakout continuation of the event range is the cleanest test of the stated hypothesis, and it fails.

**Least-bad cells (Family B, later delays) — residual, not a candidate:**

| Config | N | Win% | E[pts] | PF | OOS N | OOS E | 1-tick E |
|--------|---|------|--------|----|-------|-------|----------|
| B NFP d15 | 49 | 65% | +9.7 | 1.44 | 11 | +14.5 | +9.2 |
| B NFP d30 | 42 | 57% | +5.6 | 1.28 | 10 | +6.7 | +5.1 |
| B NFP d60 | 37 | 57% | +8.1 | 1.32 | **8** | +38.3 | +7.6 |
| B CPI d60 | 41 | 51% | +10.8 | 1.55 | 12 | **−17.5** | +10.3 |

OOS N of 8–12 is not evidence. Ranking by OOS expectancy on eight trades is how you manufacture a “best” config. It was recorded, then discarded.

MAE is systematically large versus MFE on Family A (event-range stops are wide; the market comes back through them). Family B MAE/MFE are closer (~38 vs ~44 pts on NFP d15).

### 4.4 GC (5m only, Aug 2025–Aug 2026)

N = 8–10 per cell. Unusable. Example: A/NFP E=+7.9 on N=10; A/CPI E=−13.3 on N=9. Not interpreted.

### 4.5 ES

No futures history. Not tested. Cash indices were not substituted.

---

## 5. Robustness

Applied to the mechanically ranked residual cell **B NFP d60** (N=37) — and it still fails the freeze bar.

| Test | Result |
|------|--------|
| Chronological train / OOS | OOS N=8, E=+38 — noise, not confirmation |
| Walk-forward 4 blocks | **MIXED**: +41.9, **−33.1**, **−18.6**, +38.7 (N≈9/block) |
| Pre-2020 | N=1 |
| COVID 2020–21 | N=10, WR 80%, E=+35.8 |
| Hiking 2022–mid-2023 | N=9, WR **11%**, E=**−43.1** |
| Post-2023-08 | N=17, WR 65%, E=+18.0 |
| CPI vs NFP | Effects are **not** the same; CPI breakout shorts are uniquely bad |
| Long vs short | Do not pool |
| 1-tick / 2-tick / 4-tick event stress | Residual B NFP d60 stays slightly positive (E 7.6 / 7.1 / 6.1) — costs are not the reason to reject |
| Monte Carlo reshuffle 10k | Terminal PnL is invariant under reorder (sum = +298 pts). Max DD p50=294, p95=471 — path-dependent DD is large relative to N |
| Parameter grid on Family C @ +5m | N=0 at all three ATR/retention settings (same first-bar identity) |

**Rejection is not because costs erased a robust edge.** Rejection is because the continuation hypothesis is false after 08:35, walk-forward is unstable, and the only green cells are small-N pullbacks concentrated outside the 2022 hiking regime.

---

## 6. Delay-decay analysis

The edge does **not** survive the prohibited window.

- **+5 minutes:** continuation mean negative for CPI long, NFP long, NFP short. Only CPI short is positive (N=25).
- **+10 / +15:** same pattern.
- **+30:** CPI short still positive; NFP mixed near zero.
- **+60 / 09:30:** remaining signed means mostly negative.
- **15:55:** all four cells negative or noisy with huge variance (stdev 180–260 pts).

Family A (the strategy that actually *trades* continuation) is negative at every delay. If the effect died at +5m we were instructed to reject rather than retune. That instruction was followed.

---

## 7. Portfolio comparison (frozen strategies untouched)

Read-only DVP journal: `journal/phase29_nq_drift_vwap/trades.jsonl` (not rewritten). GC V2: in-memory Phase 25 replay only.

| Metric | Value |
|--------|--------|
| Macro vs DVP daily PnL correlation (overlap days) | **0.28** on 37 days |
| Same-day overlap | 37 (by construction: one NFP day per month in the residual cell) |
| Simultaneous open exposure after 10:30 | 3 days |
| DVP mean daily pts **on** CPI/NFP dates | **+7.1** |
| DVP mean daily pts **off** those dates | **+21.4** |
| Ensemble `MACRO_REGIME` vs DVP drift | 67 / 121 agree (**55%**) |
| GC V2 overlap | 2 days (GC history too short) |

The macro idea is **not a clone of DVP** (correlation 0.28; DVP is already weaker on print days). Diversification would have been the argument *if* standalone expectancy were real. It is not. A slightly worse standalone strategy is only useful when it is still an edge. This one is not.

---

## 8. Recommendation

```
MACRO_EDGE_REJECTED
```

Reasons mapped to the Phase N rejection list:

1. Results vanish (or reverse) after +5 minutes for the continuation hypothesis.
2. Event-range breakout, the cleanest continuation rule, loses ~30 pts/trade after costs.
3. Positive cells are one family (pullback) × one event type (NFP) × later delays, N≈40, OOS N≤11.
4. Walk-forward mixed; 2022–mid-2023 hiking block is a wipeout.
5. CPI and NFP are not the same effect; pooling would be cheating.
6. Survey surprise timestamps/consensus could not be validated — so even a “surprise filter” sequel is blocked until paid calendar vintages exist.
7. ES and UST/DXY confirmation were unavailable; they were omitted, not faked.
8. GC sample is too small to claim a gold-specific print edge.

Do not optimize around this. Do not freeze. Do not paper-trade it.

---

## 9. Best candidate

**None.** Not frozen. Not incubated.

A residual curiosity — **not a next-phase config**:

`B_FIRST_PULLBACK` × NFP × delay 15 minutes, N=49, E=+9.7 pts, PF 1.44, 1-tick E=+9.2.

It is still a pullback rule on a mean-reverting impulse, with 11 OOS trades and a failed hiking-era block. It does not meet `CANDIDATE → ROBUSTNESS → INCUBATION`.

If macro work resumes after a leakage-safe consensus dataset exists, start from **event-study continuation (this report’s §4.2)**, not from fitting Family B.

---

## Look-ahead / blackout controls

- `tests_phase33.py`: 6 passed (closed 08:30 5m bar, future 10:00 spike isolation, no blackout entries, tiny-move ⇒ NEUTRAL, delay-10 entry ≥ 08:40, frozen file bytes).
- Engine blackout check on produced trades: `ok`.
- Frozen file SHA-256 unchanged vs the Phase 33 start snapshot.

## Philosophy

AITRADE is moving toward `RESEARCH → CANDIDATE → ROBUSTNESS → INCUBATION → FROZEN → PAPER → ACTIVE`. This phase is a completed **RESEARCH** falsification. The portfolio still has two distinct edges (GC mean reversion, NQ VWAP-drift continuation). Post-news macro continuation is not a third.
