# Phase 43 — Small-cap gap-up short (data feasibility)

Research only. `DRY_RUN`. No broker. Nothing frozen. No equity data was purchased.

Primary locked before P&L: `SMALLCAP_GAP50_OR5_BREAKDOWN` (US common, gap ≥ +50%, 09:30–09:35 OR, 1m close below OR low, short next 1m open, stop = OR high, 1R, cover 15:50). **Not tested.** The data gate failed first.

## 1. Verdict

- **Overall:** `SMALLCAP_DATA_QUALITY_BLOCKED`
- **FLOAT_DATA_UNAVAILABLE:** `True`
- **HALT_MODEL_DEGRADED:** `True`
- **BORROW_HISTORY_UNAVAILABLE:** `True`
- **Recommendation:** `DO_NOT_PURCHASE_BLINDLY_DO_NOT_FAKE_UNIVERSE`

This is not `SMALLCAP_GAP_SHORT_EDGE_REJECTED`. The market effect was not measured. A later phase can test Gap-Up Short, First Red Day, or Bounce Short only after a survivorship-safe 1m universe plus halt and borrow assumptions exist.

## 2. Frozen futures integrity

Verified before and after. Frozen files were not modified. `strategy_frozen/` was not written.

- GC VWAP V2: `0695d7a881b6247033426bceb077b6895b96405b826c21f32a109a02f44b2d43`
- NQ DVP: `935e3a616351b09dbfa8d2d0e2b5d6850be803fb385ec5e5f8e3593856a1212a`
- File SHA GC: `12615d19ef3deed36b7929c161ef7c377975cd2317c0ae6d06f1949bd327046f`
- File SHA NQ: `34927fba0f268e56cf166cf8939bdfec6aa031510f7cee9acfab3909dd36b541`

## 3. Data feasibility

AITRADE on disk is futures (ES/NQ/GC) plus XAUUSD. There are **zero** local US equity bar files, security masters, delist files, or corporate-action tables.

Tiingo via OpenBB `equity.price.historical` returns AAPL daily bars and returns an empty response for bankrupt BBBY. That is a survivorship gap on the only equity route already credentialed. A backtest on today's listed names would be invalid.

Declared TRAIN end is 2022-12-30. Databento EQUS.MINI / IEXG.TOPS / DBEQ.BASIC history starts **2023-03-28**. Even a paid Mini 1m tape would leave TRAIN empty.

## 4. Data sources

Metadata/`get_cost` only. No timeseries download. Credentials present: Databento yes, Tiingo yes, FMP/Polygon/Alpaca/Nasdaq Data Link no.

| Source | History start | 1m ALL cost (quoted, not bought) | Halt schema | Notes |
|---|---|---:|---|---|
| EQUS.MINI | 2023-03-28 | $541.300256878138 (2023–2026) | no | Cheapest NMS-like 1m; too short; no float/borrow |
| EQUS.MINI daily | 2023-03-28 | $12.213696911931 | no | Cannot test OR5 |
| IEXG.TOPS 1m 1-day | 2023-03-28 | $0.346594423056 | yes | IEX volume ≠ small-cap NMS volume |
| XNAS.ITCH 1m | 2018-05-01 | $1501.963886618614 | yes | Nasdaq only; ~$1502; still no cap/float/borrow |
| DBEQ.BASIC 1m | 2023-03-28 | $1907.078280933201 | yes | ~$1907; TRAIN empty |

Polygon/Alpaca/FMP/CRSP/Sharadar are not in this repo and were not subscribed.

## 5. Historical universe

None constructed. Included: n/a. Excluded: n/a. Building a Yahoo/Tiingo surviving-name list was refused.

## 6. Market cap / float quality

`FLOAT_DATA_UNAVAILABLE`. Point-in-time market cap is also unavailable. Today's float must not be applied historically. A degraded cap-from-price×shares model was not invented.

## 7. Gap distributions

Not computed. No valid universe.

## 8. Structural behavior

Not computed. Open-to-close, HOD timing, P(close < open), halt probability: n/a.

## 9. OR5 breakdown

Not computed. Candidate A requires 1m bars on a PIT small-cap universe.

## 10. VWAP loss

Not computed.

## 11. Primary candidate

`SMALLCAP_GAP50_OR5_BREAKDOWN` remains the locked definition for a future data phase. Phase 43 entered **zero** trades. Status: not tested.

## 12. Target matrix

n/a

## 13. Gap-size analysis

n/a

## 14. Market-cap / float analysis

n/a (`FLOAT_DATA_UNAVAILABLE`)

## 15. Price analysis

n/a

## 16. Volume / float rotation

n/a

## 17. Halts

`HALT_MODEL_DEGRADED`. EQUS.MINI does not support `status`. XNAS.ITCH and DBEQ.BASIC do, but were not purchased. No halt timestamps are on disk. A continuous-tradability assumption would be invalid for this strategy family.

## 18. SSR

Not reconstructed. Rule 201 can be inferred from a prior-day ≥10% decline only after a valid daily universe exists. Even then, SSR changes *how* a short is routed, not whether shares exist.

## 19. Borrow / locate

Unavailable. Scenarios 1–3 were not run because there are no theoretical trades. Label if a later phase tests prices anyway: `UNCONSTRAINED_THEORETICAL`. A profitable tape without locates is not Book 3.

## 20. Slippage stress

Predeclared overlays 0 / 0.25% / 0.50% / 1.00% adverse. Not applied. Small-cap fills are not 1-tick futures fills.

## 21. Train / holdout

Predeclared TRAIN through 2022-12-30, HOLDOUT from 2023-01-03. EQUS.MINI cannot populate TRAIN. No split was run.

## 22. Walk-forward

n/a

## 23. Year-by-year

n/a. Explicitly not a 2020–2021-only study, because no years were studied.

## 24. MFE / MAE

n/a

## 25. Operational feasibility

AITRADE cannot execute this sleeve today. There is no equity scanner, locate workflow, stock broker adapter, or halt-aware equity router. NinjaTrader Sim101 is MNQ. Even a valid backtest would still fail Phase AM until those pieces exist.

## 26. Portfolio relationship

No equity P&L series. No comparison to GC VWAP V2 or NQ DVP. Read-only frozen books were not modified.

## 27. Recommendation

Do not freeze. Do not test First Red Day or Bounce Short on the same missing universe. Do not download EQUS.MINI as a shortcut: TRAIN through 2022 would be empty, float and borrow are still missing, and Mini has no halt status. XNAS.ITCH 1m ALL 2018–2026 quotes at about $1502 and is still Nasdaq-only without PIT cap/float/borrow. Acquire a survivorship-safe master first, then 1m + halts + a conservative locate model, then run the locked OR5 candidate.

Execution remained `DRY_RUN`. No candidate JSON.

If a later phase acquires data, minimum acceptable bundle:

1. Survivorship-safe US common-stock master (delisted, ticker changes, corporate actions) covering 2018–present.
2. Point-in-time market cap (float if possible; otherwise labelled degraded).
3. Regular-hours 1m OHLCV including dead names, reverse-split consistent.
4. Halt/LULD timestamps (or conservative halt stress).
5. Borrow/locate model that is not 'every name shortable at 0 fee'.
6. Then, and only then, test the locked `SMALLCAP_GAP50_OR5_BREAKDOWN` without holdout tuning.
