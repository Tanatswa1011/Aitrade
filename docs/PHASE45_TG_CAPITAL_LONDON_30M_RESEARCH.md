<!-- POLISHED_PHASE45_REPORT -->
# Phase 45 — TG Capital London 30m model

Research only. `DRY_RUN`. No broker. Nothing frozen.

Mechanized chain, locked before P&L: **London 07:00–11:00 `Europe/London`** → completed **4H close vs EMA200** → **30m EMA200 + EMA20/50/200 stack** → trend-aligned **3-candle FVG** → **50% midpoint** → **trident approximation** → **next 30m open ±1 tick** → **FVG-boundary stop** → **2R**, flatten **15:55 ET**.

No TG Capital source file exists in this repository. Approximations are labeled below and were not changed after seeing P&L.

## 1. Verdict

- **Overall:** `TG_LONDON_EDGE_WEAK`
- **GC_TG_LONDON_STATUS:** `TG_LONDON_EDGE_WEAK`
- **NQ_TG_LONDON_STATUS:** `TG_LONDON_EDGE_REJECTED`
- **Recommendation:** `CLOSE_TG_LONDON_BRANCH`

Trend-aligned London FVGs **mostly fill** (GC P(full fill)=0.81, P(through)=0.81). That is imbalance mean-reversion, not continuation. The locked trident+2R book is **+0.10R** on GC in-sample (N=101, PF=1.37) but **holdout −0.058R** and **2-tick −0.039R**. Candidate A (doji) and C (simple close) are worse. NQ portability is negative. Do not freeze. Do not add indicators. Do not decorate GC VWAP V2 with FVG.

## 2. Frozen integrity

Verified before and after. `strategy_frozen/` was not modified.

- GC VWAP V2: `0695d7a881b6247033426bceb077b6895b96405b826c21f32a109a02f44b2d43`
- NQ DVP: `935e3a616351b09dbfa8d2d0e2b5d6850be803fb385ec5e5f8e3593856a1212a`
- File SHA GC: `12615d19ef3deed36b7929c161ef7c377975cd2317c0ae6d06f1949bd327046f`
- File SHA NQ: `34927fba0f268e56cf166cf8939bdfec6aa031510f7cee9acfab3909dd36b541`

## 3. Source-rule fidelity

| Rule | Status |
|------|--------|
| 3-candle FVG (`high[1] < low[3]` / inverse) | **EXACT** |
| FVG midpoint 50% | **EXACT** |
| EMA200 length 200, completed bars | **EXACT** |
| London 07:00–11:00 Europe/London | `MECHANIZED_APPROXIMATION` |
| EMA20 > EMA50 > EMA200 stack | `MECHANIZED_APPROXIMATION` |
| 4H Globex (18:00 ET) close vs EMA200 | `MECHANIZED_APPROXIMATION` |
| Doji body/range ≤ 0.25 | `MECHANIZED_APPROXIMATION` |
| Trident: wick ≥ body, close in trend half | `TRIDENT_MECHANIZED_APPROXIMATION` |
| Next 30m open, FVG-boundary stop, flatten 15:55 ET | `MECHANIZED_APPROXIMATION` |

This is not `TG_MODEL_DEFINITION_BLOCKED`: every visual term was given a minimal numerical rule before P&L. It is also not the unpublished discretionary TG playbook.

## 4. Dataset

| | GC (primary) | NQ (portability) |
|---|---|---|
| Series | Databento `GC.v.0` ohlcv-1m | AITRADE volume-crossover 1m |
| Bars | 2,314,207 | 2,338,684 |
| 30m (London clock) | 78,153 | 78,221 |
| 4h (Globex 18:00 ET) | 10,226 | 10,205 |
| Range | 2020-01-01 → 2026-08-16 | 2020-01-01 → 2026-08-14 |
| Cost | **$8.45** (quoted then downloaded this phase) | already local |
| Tick / $point / commission | 0.10 / $100 / 0.04 pts | 0.25 / $20 / 0.20 pts |

Local GC 5m stitch only covered Aug 2025–Aug 2026. 1m 2020–2026 was purchased so the preferred TRAIN/HOLDOUT split is real. Databento flagged some 2020 days as degraded quality; bars were not interpolated.

## 5. London timing

`ZoneInfo("Europe/London")` — DST-aware. 07:00 London = **02:00 ET** in both winter and summer. Window is `[07:00, 11:00)` on the 30m **open**. Diagnostic 06:00–10:00 and 08:00–12:00 were **not** selected by P&L; aligned P(mid) is ~0.83–0.86 on all three.

## 6. HTF bias

Completed 4H close vs EMA200. Full alignment (4H + 30m EMA200 + stack) is **not** assumed better a priori.

## 7–8. EMA200 and EMA stack (incremental)

GC London-window FVGs, P(resume after mid) / P(full fill) / N:

| Filter | N | P(mid) | P(fill) | P(resume) | P(new extreme) |
|---|---:|---:|---:|---:|---:|
| London FVG only | 2697 | 0.847 | **0.810** | 0.897 | 0.775 |
| + EMA200 side | 1666 | 0.828 | 0.791 | 0.908 | 0.782 |
| + EMA stack | 1077 | 0.846 | 0.812 | 0.908 | 0.795 |
| + 4H full align | 766 | 0.846 | **0.815** | 0.921 | 0.809 |

The stack **cuts N** and barely moves fill or resume. Resume ~90% over a 12-hour horizon **coexists with ~81% full fill**. That is not a continuation signature; it is “price trades both ways after the gap.”

NQ is the same shape: aligned N=831, P(fill)=0.90, P(resume)=0.91.

## 9. FVG statistics

- GC: 2,697 London FVGs (~385/year); 766 fully aligned (~109/year).
- NQ: 2,927 London FVGs (~418/year); 831 aligned (~119/year).
- Width was recorded; no size filter was applied.

## 10. FVG midpoint

Among aligned GC FVGs: P(edge)=0.87, P(mid)=0.85, P(fill)=0.81, mean bars to mid ≈ 4.1 (≈2 hours). Midpoint is not special versus a full fill — most mids that are touched are filled.

## 11. Reaction candles

Aligned GC: P(doji on a mid-interacting candle path)=0.43, trident=0.45, directional close=0.65.

Conditional P(resume) given the pattern is 0.97–0.98 for all three — **no incremental information**. The pattern does not separate continuation from fill.

## 12. Structural transition probabilities

Before trading: trend-aligned London FVG → mid touch is common → **full fill is also common** → later session extreme is common in **both** directions (GC mean MFE after mid 18.2 vs MAE 15.8). There is no 2R–5R skew in the raw FVG path.

## 13. Primary candidate

`TG_GC_30M_LONDON_FVG50_REACTION` — trident, FVG-boundary stop, 2R, 1 tick, max 1/London session, flatten 15:55 ET.

| Instrument | N | E[R] | E[pts] | WR | PF | Train E[R] | Holdout E[R] | P(2R) | P(3R) | Status |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|
| GC | 101 | **+0.101** | +0.78 | 0.376 | 1.37 | +0.277 | **−0.058** | 0.382 | 0.029 | `WEAK` |
| NQ | 106 | **−0.179** | −1.86 | 0.283 | 0.85 | −0.002 | **−0.338** | 0.303 | 0.028 | `REJECTED` |

Ambiguous same-bar SL+TP resolved on 1m: GC 1, NQ 3. Frequency: ~14–15 trades/year. Low frequency was not relaxed.

Variants at 2R / FVG stop: **A doji** GC E[R]=−0.210 (N=86); **C close** −0.006 (N=147). Trident is the least-bad reaction and still fails OOS. Complexity does not add value.

## 14. Stop comparison

| | GC N | GC E[R] | NQ N | NQ E[R] |
|---|---:|---:|---:|---:|
| FVG boundary (primary) | 101 | +0.101 | 106 | −0.179 |
| Reaction extreme | 110 | **−0.063** | 121 | +0.078 |

Neither family is robust on both names. Primary remains FVG-boundary as locked.

## 15. R-target matrix (GC / NQ, 1 tick)

| R | GC E[R] | GC WR | NQ E[R] | NQ WR |
|---:|---:|---:|---:|---:|
| 1.0 | −0.057 | 0.485 | −0.018 | 0.505 |
| 1.5 | +0.061 | 0.436 | −0.132 | 0.358 |
| **2.0 locked** | +0.101 | 0.376 | −0.179 | 0.283 |
| 3.0 | +0.085 | 0.284 | −0.387 | 0.160 |
| 4.0 | +0.026 | 0.235 | −0.368 | 0.132 |
| 5.0 | +0.141 | 0.225 | −0.236 | 0.132 |

GC 1R is negative; 5R is a small-N WR 22% lottery. High-R was **not** promoted. NQ is negative at every R.

## 16. MFE / MAE

- GC: avg MFE 4.70, MAE 3.16, avg stop 3.49 pts, P(1R)=0.49, P(2R)=0.38, P(3R)=0.03, hold ~67 min.
- NQ: MFE 19.3, MAE 16.2, avg stop 16.6 pts, P(2R)=0.30, P(3R)=0.03, hold ~54 min.

P(3R) ≈ 3% — this is not a 3R–5R engine after a tight FVG stop.

## 17. Long / short

- GC: long N=63 E[R]=+0.118 E[pts]=+0.05; short N=38 E[R]=+0.074 E[pts]=+1.97. Shorts earn more *points* (wider metals), similar R. Not one-sided.
- NQ: long N=81 E[R]=−0.218; short N=25 E[R]=−0.054. Longs are the hole.

## 18. GC results

Primary instrument. Full-sample 2R trident is the only mildly positive book. It fails the FOUND bar: holdout negative, 2-tick negative, t-stat 0.94, 2023 E[R]=−0.80 (WR 8%). Stop ~$349/GC contract ($35 MGC).

## 19. NQ results (portability only)

Does not port. Negative at 0/1/2 ticks and in holdout. 2022 is the only green year. GC is **not** failed because NQ failed; NQ is reported separately.

## 20. Cost stress

| | 0-tick E[R] | 1-tick | 2-tick |
|---|---:|---:|---:|
| GC | +0.113 | +0.101 | **−0.039** |
| NQ | −0.165 | −0.179 | −0.203 |

A 1-tick GC edge that dies at 2 ticks is not a Book 3.

## 21. Train / holdout

Predeclared TRAIN through `2022-12-30`, HOLDOUT from `2023-01-03`.

- GC: train N=48 E[R]=+0.277; holdout N=53 E[R]=**−0.058**
- NQ: train N=50 E[R]=−0.002; holdout N=56 E[R]=**−0.338**

No holdout tuning.

## 22. Walk-forward

Year blocks are the WF (low frequency; ~10–20 trades/year).

## 23. Year-by-year

| Year | GC N | GC E[R] | GC WR | NQ N | NQ E[R] | NQ WR |
|---|---:|---:|---:|---:|---:|---:|
| 2020 | 19 | +0.235 | 0.42 | 15 | −0.026 | 0.33 |
| 2021 | 9 | −0.032 | 0.33 | 18 | −0.188 | 0.28 |
| 2022 | 20 | +0.456 | 0.50 | 17 | +0.217 | 0.41 |
| 2023 | 13 | **−0.801** | 0.08 | 17 | −0.317 | 0.24 |
| 2024 | 14 | +0.038 | 0.36 | 14 | −0.166 | 0.29 |
| 2025 | 16 | +0.489 | 0.50 | 15 | −0.420 | 0.20 |
| 2026 YTD | 10 | −0.105 | 0.30 | 10 | −0.492 | 0.20 |

A high-R story that lives in 2022+2025 and dies in 2023 is not stable.

## 24. Threshold stability

Trident wick/body 0.8 / **1.0** / 1.2: GC E[R] +0.074 / +0.101 / +0.109 (same sign). Doji 0.20 / 0.25 / 0.30 all **negative**. Flag: doji family is not an edge; trident neighbors do not flip GC in-sample but do not repair holdout. Not `PATTERN_THRESHOLD_UNSTABLE` on the locked trident ratio.

## 25. News impact

08:30 ET = 13:30 London: **0** entries removed (window closed at 11:00). GC 16 / NQ 14 entries fall in 08:25–08:35 **London** (possible UK prints). No complete UK calendar was invented as a filter.

## 26. Risk geometry

| | GC | MGC | NQ | MNQ |
|---|---:|---:|---:|---:|
| Avg stop | 3.49 pts | same pts | 16.6 pts | same pts |
| Median / p95 stop | 2.50 / 10.2 | — | 13.3 / 49.5 | — |
| $ risk / contract | **$349** | **$35** | **$333** | **$33** |
| Max consec losses | 10 | — | 12 | — |
| Worst day | −16.0 pts | — | −69.2 pts | — |
| Avg hold | ~67 min | — | ~54 min | — |
| Overnight | No | — | No | — |
| Trades/day | 1 | — | 1 | — |

Prop-shaped (intraday, 1/day, flat 15:55) but not an edge.

## 27. Frozen-book relationship

- **GC VWAP V2:** paper journal N=0. Mechanism is different by construction: London 02:00–06:00 ET trend-FVG continuation vs NY RTH VWAP mean reversion. This is **not** `GC VWAP V2 with FVG decoration`. It is also not a better GC book.
- **NQ DVP:** daily P&L correlation on the sparse overlapping days is not a clone (DVP is RTH 10:30–15:30 VWAP; this is London 30m). NQ candidate loses anyway.

## 28. Recommendation

`CLOSE_TG_LONDON_BRANCH`

Identify **no** candidate. Do not write `strategy_candidates/phase45_GC_TG_LONDON_30M.json`. Do not freeze.

Answers to the four research questions:

1. **Do trend-aligned London FVGs have continuation edge?** They have a **fill** edge: ~81–90% are fully retraced. That is the opposite of the intended continuation.
2. **Does 50% improve the conditional distribution?** Mid-touch and full-fill rates are almost the same. 50% is not magical.
3. **Does trident/doji add value?** Doji and simple close are worse than trident. Trident is the least-bad candle and still fails holdout and 2-tick on GC; NQ fails all three reactions.
4. **Can it be monetized at 2R–5R after costs?** No. GC 2R in-sample is thin and OOS-negative; 3R+ hit rates are 3%/22% lottery; NQ is negative at every R.

Closed families stay closed. Strategy #3 is still not this book.
