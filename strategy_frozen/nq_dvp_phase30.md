# Frozen Strategy — NQ Drift VWAP Pullback (Phase 30)

## Status

```text
PAPER_VALIDATION
NOT production
NO broker execution
```

## Thesis

When NQ shows a confirmed 15m directional drift relative to session VWAP, the first opposing 5m pullback candle may offer a continuation entry with fixed asymmetric point exits.

## Identity

| Field | Value |
|------|-------|
| Strategy family | `nq_drift_vwap_pullback_v1` |
| Strategy version | `nq_drift_vwap_pullback_v1.DVP_ORIGINAL.FROZEN_PHASE30` |
| Candidate | `DVP_ORIGINAL` |
| Frozen config hash | `935e3a616351b09dbfa8d2d0e2b5d6850be803fb385ec5e5f8e3593856a1212a` |
| Engine config hash | `e314f828eee7eca5` |
| Source | `strategy_candidates/phase29_DVP_ORIGINAL.json` |
| Freeze timestamp | `2026-08-17T09:46:02.284303+00:00` |

## Exact rules (immutable)

### Market

- CME NQ futures only (no GC/ES/CFD/cash index signal substitution)

### Session (America/New_York, DST-aware)

- VWAP reset: **09:30**
- No trading before: **10:30**
- No new trades after: **15:30**
- Force-close: **15:55**

### VWAP

- typical_price = (H+L+C)/3 — `IMPLEMENTATION_ASSUMPTION`
- VWAP = Σ(tp×vol)/Σ(vol) from 09:30

### Drift (15m completed bars)

- Long: close > VWAP, VWAP rising vs prior 15m, 1h return ≥ +0.10%
- Short: close < VWAP, VWAP falling vs prior 15m, 1h return ≤ −0.10%

### Entry (5m)

- Long: first red 5m after POSITIVE_DRIFT → next bar open
- Short: first green 5m after NEGATIVE_DRIFT → next bar open

### Risk (points, fixed)

- Long: SL 80 / TP 40
- Short: SL 80 / TP 50

### Position rules

- One position at a time
- Max **4** trades/day
- Stop new trades after **any 2 losing trades** in the day

## Historical evidence (Phase 29)

FULL: N=5714, WR=0.6664333216660833, E=5.951697584879244, PF=1.2685863440176592

OOS 2025+: N=1420, WR=0.678169014084507, E=6.503169014084507, PF=1.2853236520933107, maxDD=1127.5

Walk-forward: STABLE_POSITIVE

## Paper-validation criteria

- Minimum resolved: **30** (preferred 50, strong 100, large 250)
- Before N=30: only `PAPER_VALIDATION_IN_PROGRESS`
- Primary fill overlay: **1 tick adverse** (also report ideal / 2-tick)

## What is NOT allowed to change

Any change to thresholds, stops/targets, session times, or guardrails creates a **new** strategy version and must not contaminate Phase 30 paper statistics.
