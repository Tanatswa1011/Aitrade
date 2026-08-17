# Strategy registry

Evidence-preserving record of AITRADE strategies. Frozen configs must not be edited without a new formal freeze phase.

## ACTIVE / FROZEN

### GC VWAP V2

| Field | Value |
|-------|-------|
| Market | GC (COMEX gold) |
| Strategy | VWAP mean reversion — band reclaim + 2σ retest |
| Candidate | `V2_BAND_RECLAIM_2SIG_RETEST` |
| Frozen version | `gc_vwap_mean_reversion_v1.V2.FROZEN_PHASE26` |
| Frozen hash | `0695d7a881b6247033426bceb077b6895b96405b826c21f32a109a02f44b2d43` |
| Frozen files | `strategy_frozen/gc_vwap_v2_phase26.json`, `.md` |
| Session | NY RTH VWAP reset 09:30 ET |
| Phase 25 benchmark | Holdout E[2R] ≈ 0.345; walk-forward all blocks E[2R] positive; cost survives 1–2 ticks |
| Phase 26 status | Frozen; paper validation in progress (N=0 resolved forward trades) |
| Execution plan | MGC micro (sizing via dynamic stop — see `config/execution_metadata.json`) |
| Journal | `journal/phase26_gc_vwap_v2_paper/` |

### NQ Drift VWAP Pullback

| Field | Value |
|-------|-------|
| Signal market | NQ (Databento GLBX.MDP3 stitched) |
| Execution plan | MNQ Sim101 (`MNQ SEP26`, qty=1) |
| Frozen version | `nq_drift_vwap_pullback_v1.DVP_ORIGINAL.FROZEN_PHASE30` |
| Frozen hash | `935e3a616351b09dbfa8d2d0e2b5d6850be803fb385ec5e5f8e3593856a1212a` |
| Frozen files | `strategy_frozen/nq_dvp_phase30.json`, `.md` |
| Session | 09:30 VWAP; trade 10:30–15:30; flatten 15:55 ET |
| Brackets | Long SL80/TP40; Short SL80/TP50 (index points) |
| Guardrails | Max 4 trades/day; stop after 2 losses; one position at a time |
| Phase 29 benchmark | Full sample 5714 trades; OOS expectancy ~5.95 pts/trade; edge observed |
| Phase 30 status | Frozen; paper validation (N=0 forward) |
| Phase 31 status | NT Sim101 bridge integrated; DRY_RUN default; hist/live equivalence 15/15 |
| Journal | `journal/phase30_nq_dvp_paper/`, `journal/phase31_nq_dvp_sim/` |

## RESEARCH-ONLY / RETIRED

### Original liquidity sweep / CHoCH / FVG

| Field | Value |
|-------|-------|
| Family | `session_sweep_choch_fvg` (Phase 17–20 lineage) |
| Status | Retired — no edge observed (Phase 19/20 verdict preserved) |
| Evidence | `phase18_validation.json`, `phase19_validation.json`, `phase20_validation.json` |
| Note | LuxAlgo live equivalence unvalidated |

### Liquidity reclaim

| Field | Value |
|-------|-------|
| Family | `liquidity_reclaim_v1` |
| Phase | 21 |
| Status | Research-only — did not graduate to freeze |
| Evidence | `phase21_validation.json`, candidates `strategy_candidates/phase21_R*.json` |

### GC ORB (opening range breakout)

| Field | Value |
|-------|-------|
| Family | `gc_orb_volume_v1` |
| Phase | 22 |
| Status | Research-only |
| Evidence | `phase22_validation.json`, `strategy_candidates/phase22_gc_G*.json` |

### OR15 retest / FVG

| Field | Value |
|-------|-------|
| Phase | 24 |
| Candidates | ORB15 breakout, retest touch, FVG touch |
| Status | Research-only |
| Evidence | `phase24_validation.json`, `strategy_candidates/phase24_*.json` |

### London VWAP L2

| Field | Value |
|-------|-------|
| Candidate | `L2_BAND_RECLAIM_2SIG_RETEST` |
| Phase | 27 |
| Session | London 08:00–12:00 Europe/London |
| Status | Falsification / research-only — independent of frozen NY GC V2 |
| Evidence | `phase27_validation.json`, `strategy_candidates/phase27_L2_*.json` |

### NY momentum continuation

| Field | Value |
|-------|-------|
| Family | `gc_ny_momentum_continuation_v1` |
| Phase | 28 |
| Status | Falsification — momentum did not independently pass |
| Evidence | `phase28_validation.json`, `strategy_candidates/phase28_C*.json` |

## Phase 25 GC candidates (non-frozen)

V3, V5 and other Phase 25 variants remain in `strategy_candidates/` for evidence. Only **V2** was frozen in Phase 26.
