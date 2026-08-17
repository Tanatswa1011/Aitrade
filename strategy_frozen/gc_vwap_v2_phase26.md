# Frozen Strategy — GC VWAP V2 (Phase 26)

## Status

```text
paper_validation
NOT production
NO broker execution
```

## Thesis

After COMEX GC becomes statistically stretched beyond session VWAP ±2σ, objective band reclaim followed by a retest of the **frozen** 2σ band may offer positive fixed-R mean-reversion expectancy.

## Identity

| Field | Value |
|------|-------|
| Strategy family | `gc_vwap_mean_reversion_v1` |
| Strategy version | `gc_vwap_mean_reversion_v1.V2.FROZEN_PHASE26` |
| Candidate | `V2_BAND_RECLAIM_2SIG_RETEST` |
| Frozen config hash | `0695d7a881b6247033426bceb077b6895b96405b826c21f32a109a02f44b2d43` |
| Engine config hash | `da630a519397ec84` |
| Source candidate | `strategy_candidates/phase25_V2_BAND_RECLAIM_2SIG_RETEST.json` |
| Freeze timestamp | `2026-08-17T09:46:02.205512+00:00` |

## Exact rules (immutable)

### Session (America/New_York, DST-aware)

- Start: **08:20**
- End / trade expire: **13:30**
- No new setups after: **12:30**

### VWAP

- typical_price = (high + low + close) / 3
- VWAP = Σ(typical × volume) / Σ(volume) from session start
- Resets each trading day; no overnight carry; no future bars

### Sigma

- Volume-weighted std of typical prices vs **running** session VWAP
- Extension threshold: **±2.0σ**
- Warm-up: **6** completed 5m bars

### Confirmation

- `BAND_RECLAIM` only
- Upper: close returns below +2σ → short
- Lower: close returns above −2σ → long
- Forbidden: CHoCH, FVG, candle patterns, volume filter

### Entry

- `FROZEN_2SIG_RETEST`
- `entry_band_price` = 2σ band from the **first extension bar** (Phase 25 `frozen_2sig`)
- That level is preserved through reclaim confirmation and the retest wait
- Pending entry must **not** move with later VWAP/σ
- Timeout: **6** bars after confirmation

### Stop

- Extension sequence extreme (high for short, low for long)
- Buffer: **0**
- No trail / no break-even move

### Targets (tracked independently)

- 1R, 1.5R, 2R, 3R
- VWAP touch (diagnostic)

## State progression

```text
WAITING_FOR_SESSION → VWAP_WARMUP → WAITING_FOR_EXTENSION
→ EXTENDED → RECLAIM_CONFIRMED → WAITING_FOR_RETEST
→ ENTRY_TRIGGERED → STOP / TARGET_* / VWAP_HIT / EXPIRED / AMBIGUOUS
```

## Historical evidence (Phase 25 source of truth)

TRAIN V2: N=184, E2R=0.11413043478260865

HOLDOUT V2:

- N=87
- stop=0.5977011494252874
- 1R=0.6551724137931034 1.5R=0.5517241379310345 2R=0.47126436781609193 3R=0.367816091954023
- E1R=0.05747126436781602 E1.5R=0.2298850574712643
- E2R=0.34482758620689646 E3R=0.5057471264367815
- median MFE=1.77240441363276 MAE=1.123458532238931

Walk-forward: all 4 blocks E2R > 0 (see `phase25_validation.json`).

Cost: HOLDOUT E2R remains positive after 1–2 ticks/side.

## Paper-validation criteria

- Minimum resolved: **30** (preferred 50, strong 100)
- Before N=30: only `PAPER_VALIDATION_IN_PROGRESS`
- After N≥30: `FORWARD_EDGE_SUPPORTED` / `WEAK` / `NOT_SUPPORTED` / `STILL_INSUFFICIENT`
- Primary paper fill overlay: **1 tick adverse** (also report ideal and 2-tick)

## What is NOT allowed to change

Any change to sigma, session times, reclaim, entry freeze semantics, timeout, stop, or targets creates a **new** strategy version and must not contaminate Phase 26 V2 paper statistics.
