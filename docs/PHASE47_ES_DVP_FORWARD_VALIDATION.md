# Phase 47 — ES DVP candidate lock + multi-book forward validation

`DRY_RUN`. `NO BROKER EXECUTION`. Not a production freeze.

Question: **Do the frozen/locked rules continue behaving after research ends and no further tuning is allowed?**

Phase 46 established that DVP is portable to ES. Phase 47 does not search more markets. It locks the ES candidate and opens a prospective paper stream beside the two frozen books.

## 1. Verdict

- **ES candidate status:** `ES_DVP_FORWARD_VALIDATION_READY`
- **Portfolio status:** `MULTI_BOOK_FORWARD_VALIDATION_READY`
- **Overall:** `FORWARD_VALIDATION_READY`
- **ES locked hash:** `28d12b8f3a6631b8c6526d6c300244396c0c2ba5628a2d5baa143f5489f4b3c4`
- **Broker execution:** `False`

ES DVP is a **locked research candidate**, not `FROZEN`. Do not promote on early wins. Do not demote on early losses. Before N=30 the book stays in forward validation.

## 2. Frozen integrity

Verified before and after. `strategy_frozen/` was not written.

| Book | Config hash | File SHA | Intact |
|------|-------------|----------|--------|
| GC VWAP V2 | `0695d7a881b6247033426bceb077b6895b96405b826c21f32a109a02f44b2d43` | `12615d19ef3deed36b7929c161ef7c377975cd2317c0ae6d06f1949bd327046f` | YES |
| NQ DVP | `935e3a616351b09dbfa8d2d0e2b5d6850be803fb385ec5e5f8e3593856a1212a` | `34927fba0f268e56cf166cf8939bdfec6aa031510f7cee9acfab3909dd36b541` | YES |

## 3. Phase 46 ES DVP candidate (authoritative source)

Read from `strategy_candidates/phase46_ES_DVP.json`. Parameters were not reconstructed from memory.

| Field | Value |
|-------|-------|
| Family | DVP (`nq_drift_vwap_pullback_v1`) |
| Instrument | ES |
| Version / candidate | `ES_DVP_PORT` → locked as `es_dvp_v1.PORT.LOCKED_PHASE47` |
| Signal | 15m drift (close vs session VWAP + VWAP slope + 1h return ±0.10%); first opposing completed 5m; entry next 5m open |
| Session | America/New_York; VWAP 09:30; trade 10:30; no new 15:30; flatten 15:55 |
| Drift threshold | 0.001 (0.10%) |
| Stop methodology | TRAIN median session ATR14 scale 0.22547932918744962 vs frozen NQ 80/40/50 |
| Long | stop 18 / target 9 |
| Short | stop 18 / target 11.25 |
| Daily loss | stop after 2 losing trades |
| Max trades/day | 4; one position at a time |
| Costs | 1-tick adverse round-turn + 0.08 pt commission (overlays 0 / 2 ticks diagnostic only) |
| TRAIN | N=2511 E[R]=0.01875901588565867 WR=0.6407805655117483 PF=1.0635077217156308 |
| HOLDOUT | N=3063 E[R]=0.03842384009866871 WR=0.6571988246816847 PF=1.1316876981413564 |
| Full 1-tick | N=5574 E[R]=0.029565143722840182 WR=0.6498026551847865 PF=1.100766501141735 |
| Walk-forward | Phase 46 year blocks (not retuned): 2020 negative; 2021–2026 positive |
| Corr vs NQ DVP | daily P&L 0.6043162895478795 on 1673 overlapping days (under 0.70 redundancy bar) |

## 4. Locked candidate

- File: `strategy_candidates/phase47_ES_DVP_LOCKED_CANDIDATE.json`
- Status: `LOCKED_FORWARD_VALIDATION_CANDIDATE`
- Flags: `NOT_PRODUCTION` `DRY_RUN_ONLY`
- Hash: `28d12b8f3a6631b8c6526d6c300244396c0c2ba5628a2d5baa143f5489f4b3c4`
- Lock timestamp: `2026-08-19T00:27:41.130916+00:00`

This hash must be checked on every future ES paper run. Any rule change requires a new version, a new hash, and a new forward journal. Never silently alter this campaign.

News (Phase 46 locked rule): T−5m → T+5m around 08:30 ET. RTH entries start 10:30, so the 08:30 window never overlaps entries (Phase 46 `n_news_removed` = 0). Frozen GC/NQ news behavior was not modified.

## 5. Forward journals

| Book | Journal | Forward N | Policy |
|------|---------|----------:|--------|
| GC VWAP V2 | `journal/phase26_gc_vwap_v2_paper/` | 0 | append-only; historical contents not rewritten |
| NQ DVP | `journal/phase30_nq_dvp_paper/` | 0 | append-only; historical contents not rewritten |
| ES DVP | `journal/phase47_es_dvp_paper/` | 0 | new; empty; no backtest replay |

`GC_FORWARD_N = 0 / 30`

`NQ_FORWARD_N = 0 / 30`

`ES_FORWARD_N = 0 / 30`

A trade counts toward forward N only if the setup occurs after the lock timestamp, uses information available then, and the simulated entry/stop/target are determined prospectively. Historical replay does not count. Only resolved entered positions increment N. Non-entered setups go to `setups.jsonl`.

State machine: `NO_SETUP` → `SETUP_ARMED` → `ENTRY_PENDING` → `OPEN_POSITION` → `TARGET` | `STOP` | `FORCE_CLOSE` | `SESSION_CANCEL` | `INVALIDATED_BEFORE_ENTRY`.

## 6. Paper engine safety

- Mode: `DRY_RUN` only. No `--enable-sim-execution`. No NinjaTrader/broker path.
- Duplicate key: `strategy + instrument + session_date + setup_timestamp + direction`.
- Idempotent JSONL append. Restart restores session date, armed setup, open paper position, daily counts, and hash from `runner_state.json` + journals.
- Config hash validated every run. Stale-data block reused from DVP live signal (`STALE_5M_SECONDS`). Completed bars only.
- Primary cost overlay 1 tick adverse (Phase 46: 2-way tick + 0.08 commission). 0-tick and 2-tick stored as diagnostics; they do not change signals.
- Signals from ES. MES dollars are a sizing reference ($5/pt vs $50/pt).

## 7. Live data

- CDP module present: `True`
- GC live: `FORWARD_DATA_BLOCKED` — missing: real-time CME GC futures OHLCV via TradingView/CDP (get_chart_info + fetch_bars on the matching futures chart)
- NQ live: `FORWARD_DATA_BLOCKED` — missing: real-time CME NQ futures OHLCV via TradingView/CDP (get_chart_info + fetch_bars on the matching futures chart)
- ES live: `FORWARD_DATA_BLOCKED` — missing: real-time CME ES futures OHLCV via TradingView/CDP (get_chart_info + fetch_bars on the matching futures chart)

Forward validation can be structurally ready while live bars are blocked. **Do not invent paper trades** to fill N. Evaluation accounts are not required; the engine must work against real-time data in DRY_RUN first.

## 8. DVP family monitor

NQ and ES remain separate books. Monitor only, equal-risk diagnostic `NQ_DVP_R + ES_DVP_R`. `DVP_FAMILY_CONCENTRATION` is a warning, not a trade block.

- Same-day overlap: `INSUFFICIENT_FORWARD_SAMPLE`
- Same-direction overlap: `INSUFFICIENT_FORWARD_SAMPLE`
- Simultaneous-position overlap: `INSUFFICIENT_FORWARD_SAMPLE`
- P(ES active \| NQ active): `INSUFFICIENT_FORWARD_SAMPLE`
- Forward P&L correlation: `INSUFFICIENT_FORWARD_SAMPLE`
- Combined family DD: `INSUFFICIENT_FORWARD_SAMPLE`

With N=0 this is `INSUFFICIENT_FORWARD_SAMPLE`. Historical Phase 46 daily P&L correlation (~0.60) is a research prior, not a forward statistic.

## 9. What Phase 47 did not do

No retune of ES stops, drift, thresholds, or session. No NQ/GC edits. No RTY, YM, 6E, CL, FVG, ORB, order flow, TSMOM, volume profile, or new VWAP/EMA variants. No freeze of ES. No historical trades copied into the ES journal.

## 10. Decision checklist

1. GC and NQ frozen hashes intact? **True**
2. ES Phase 46 candidate locked? **True**
3. ES candidate hash? `28d12b8f3a6631b8c6526d6c300244396c0c2ba5628a2d5baa143f5489f4b3c4`
4. Three journals valid append-only? **True**
5. Forward N: GC 0, NQ 0, ES 0
6. Real-time data available? **FORWARD_DATA_BLOCKED**
7. Restart/duplicate protections present? **True**
8. DVP family overlap monitoring operational? **True**
9. Broker execution enabled? **False**
10. Next: Attach real-time CME GC, NQ, and ES futures charts and let the locked/frozen rules generate the first genuine forward trades. Do not retune. Do not backfill.

## 11. Files

- `strategy_candidates/phase47_ES_DVP_LOCKED_CANDIDATE.json`
- `journal/phase47_es_dvp_paper/paper_trades.jsonl`
- `es_dvp_lock.py`, `es_dvp_paper.py`, `es_dvp_paper_runner.py`, `es_dvp_live.py`
- `dvp_family_monitor.py`, `multi_book_forward.py`
- `reports/phase47_forward_scorecard.csv`, `reports/phase47_dvp_family_overlap.csv`
- `phase47_validation.json`

