# AITRADE

Analysis and deployment-prep framework for frozen futures strategies. **Phase 32: paused** — no live capital, no prop evaluation, execution disabled by default.

## Current status

- Research and deployment work **paused** (`execution_status = PAUSED`)
- No live capital deployed
- No current prop evaluation purchased
- Execution disabled by default (`DRY_RUN`; SIM blocked while paused)
- Phase 31 NinjaTrader Sim101 bridge integrated but not running automatically

## Validated strategies

### GC — `V2_BAND_RECLAIM_2SIG_RETEST`

- **Version:** `gc_vwap_mean_reversion_v1.V2.FROZEN_PHASE26`
- **Frozen hash:** `0695d7a881b6247033426bceb077b6895b96405b826c21f32a109a02f44b2d43`
- **Status:** Historical edge observed; frozen Phase 26; paper forward validation pending
- **Journal:** `journal/phase26_gc_vwap_v2_paper/`

### NQ — `DRIFT_VWAP_PULLBACK`

- **Version:** `nq_drift_vwap_pullback_v1.DVP_ORIGINAL.FROZEN_PHASE30`
- **Frozen hash:** `935e3a616351b09dbfa8d2d0e2b5d6850be803fb385ec5e5f8e3593856a1212a`
- **Status:** Independently replicated edge; frozen Phase 30; NinjaTrader SIM bridge (Phase 31); forward validation pending
- **Journal:** `journal/phase30_nq_dvp_paper/`, `journal/phase31_nq_dvp_sim/`

## Safety

- **DRY_RUN default** — runner does not submit orders without `--enable-sim-execution`
- **Project pause gate** — `state/project_pause.json` blocks SIM while paused
- **Sim101-only** — `LIVE_ACCOUNT_BLOCKED` for any other account
- **Quantity lock** — qty = 1 (`QUANTITY_BLOCKED` otherwise)
- **MNQ instrument lock** — full-size NQ refused at execution layer
- **Stale-data block** — `STALE_DATA_BLOCK` on aged 5m bars
- **Trigger dedupe** — persisted `seen_triggers` in `runner_state.json`
- **Restart recovery** — state restored from journal; corrupt state fails closed
- **Halt switch** — `--halt` / `--resume` + `journal/phase31_nq_dvp_sim/HALT`
- **Protection-failure flatten** — orphan bracket detection → flatten Sim101 MNQ

## Setup

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
# Fill DATABENTO_API_KEY, TIINGO_TOKEN locally — never commit .env
```

## Commands

```bash
python nq_dvp_live_runner.py
python nq_dvp_live_runner.py --status
python nq_dvp_live_runner.py --halt
python nq_dvp_live_runner.py --resume
python nt_ati.py FLATTEN_SIM
python phase32_validate.py
python -m unittest discover -s . -p "tests_phase*.py"
python -m unittest tests_ninjatrader_execution
```

Optional (blocked while Phase 32 pause active):

```bash
python nq_dvp_live_runner.py --enable-sim-execution
```

## Documentation

- `docs/DEPLOYMENT_RUNBOOK.md` — resume procedure
- `docs/STRATEGY_REGISTRY.md` — frozen and retired strategies
- `docs/PROP_FIRM_READINESS.md` — pre-evaluation checklist
- `docs/NEXT_2_WEEKS.md` — priority plan

## Resume gate

AITRADE remains paused until prop firm selected, rules configured, feeds verified, hashes pass, DRY_RUN/Sim101 QA complete, and explicit approval given. See `state/project_pause.json`.
