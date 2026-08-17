# Deployment runbook

Resume procedure for AITRADE after Phase 32 pause. **Do not enable live or prop-funded accounts** until explicit approval.

## Prerequisites

- Private repo access: `https://github.com/Tanatswa1011/Aitrade.git`
- NinjaTrader 8 with ATI enabled (`IsAtiEnabled=true`)
- Local `.env` with credentials (never committed)
- Prop firm selected and rules loaded into `config/prop_risk_template.json`

## Resume procedure

### 1. Pull latest private repo

```bash
git clone https://github.com/Tanatswa1011/Aitrade.git
cd Aitrade
git pull origin main
```

### 2. Create virtual environment

```bash
python -m venv .venv
.venv\Scripts\activate
```

### 3. Install requirements

```bash
pip install -r requirements.txt
```

### 4. Restore local `.env`

Copy `.env.example` → `.env` and fill:

- `DATABENTO_API_KEY` (historical research)
- `TIINGO_TOKEN` / `TIINGO_API_KEY` (spot/FX research)
- Do not commit `.env`

### 5. Verify frozen hashes

```bash
python phase32_validate.py
```

Expected:

- GC: `0695d7a881b6247033426bceb077b6895b96405b826c21f32a109a02f44b2d43`
- NQ: `935e3a616351b09dbfa8d2d0e2b5d6850be803fb385ec5e5f8e3593856a1212a`

If mismatch: **STOP** — do not proceed.

### 6. Run full tests

```bash
python -m unittest discover -s . -p "tests_phase*.py"
python -m unittest tests_ninjatrader_execution
```

Baseline: 419+ phase tests, 19 NT bridge tests — no regressions.

### 7. Start NinjaTrader

- Open NinjaTrader 8
- Confirm ATI incoming folder exists: `Documents\NinjaTrader 8\incoming`
- Use **Sim101** account only during validation

### 8. Connect approved market-data / prop feed

- Connect Rithmic or firm-approved real-time feed
- Do not rely on Simulated Data Feed alone for forward validation

### 9. Verify active contracts

- MNQ front month matches `nq_dvp_nt_exec.EXEC_INSTRUMENT` (currently `MNQ SEP26`)
- Update execution metadata if rollover required — **do not change frozen strategy JSON**

### 10. Start DRY_RUN

```bash
python nq_dvp_live_runner.py --status
python nq_dvp_live_runner.py --once
```

Confirm `mode: DRY_RUN`, `project_paused: false` (after clearing pause), no OIF files written.

### 11. Verify signal calculations

```bash
python nq_dvp_live_runner.py --equivalence
```

Expect `LIVE_EQUIVALENCE_OK` on historical bar replay.

### 12. Enable Sim101 only

```bash
python nq_dvp_live_runner.py --enable-sim-execution --once
```

Verify account = Sim101, instrument = MNQ, qty = 1.

### 13. Verify entry / stop / target behavior

- Long: 80pt stop / 40pt target from fill
- Short: 80pt stop / 50pt target from fill
- Bracket mechanism: `OIF_FILL_THEN_OCO_CHILDREN`

### 14. Verify journal

Check:

- `journal/phase31_nq_dvp_sim/live_events.jsonl`
- `journal/phase31_nq_dvp_sim/executions.jsonl`
- `journal/phase31_nq_dvp_sim/runner_state.json`

### 15. Emergency halt procedure

```bash
python nq_dvp_live_runner.py --halt
```

Creates `journal/phase31_nq_dvp_sim/HALT` and blocks further submission.

Resume only after manual review:

```bash
python nq_dvp_live_runner.py --resume
```

### 16. Emergency SIM flatten procedure

```bash
python nt_ati.py FLATTEN_SIM
```

Cancels DVP-owned orders and closes Sim101 MNQ position.

## Not documented here

- Live personal account activation
- Prop evaluation / funded account enablement
- Automatic runner startup as a service

These require explicit approval after all resume-gate items pass.
