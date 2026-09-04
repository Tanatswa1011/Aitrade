# AITRADE

## Overview

AITRADE is a **Python research and deployment-preparation framework** for systematic futures strategies. It is designed around deterministic validation, execution safeguards, and fail-closed deployment controls. The framework separates signal generation from execution, enforces explicit safety gates, and provides comprehensive testing and logging to prepare strategies for potential live deployment.

---

## Technical Snapshot

| Aspect | Details |
|--------|---------|
| **Language** | Python 3.10+ |
| **Domain** | Quantitative research & automation |
| **Architecture** | Signal generation → Validation → Risk controls → Execution → Logging |
| **Focus** | Deterministic validation, safety, testability, deployment controls |
| **Status** | Research and deployment preparation |
| **Live Trading** | **Disabled** by default and by design |

---

## Why I Built It

The goal was to explore how systematic trading strategies can move from research into controlled deployment without allowing unsafe or accidental live execution. Rather than treating deployment as an afterthought, AITRADE bakes safety into every layer:

- **Deterministic validation**: Every signal is validated against frozen, immutable configurations.
- **Execution safeguards**: Orders can only execute under explicit, verified conditions (Sim101 account only, MNQ micro contracts only, etc.).
- **Fail-closed defaults**: The system is paused by default. Live execution requires active operator choice, repeated confirmation, and environmental checks.
- **State recovery**: If the system restarts unexpectedly, corrupt state fails closed—never proceeds blindly.

This is engineering practice, not financial advice. The framework demonstrates how to build automated systems that prioritize safety and observability.

---

## Architecture

```
Market Data (NinjaTrader / Databento)
    ↓
Strategy Signal Logic (frozen rule engines)
    ↓
Validation & Stale-Data Checks
    ↓
Risk / Position / Account Limits
    ↓
Execution Safeguards (account/qty/instrument locks)
    ↓
Execution Layer (NinjaTrader ATI bridge, SIM_ONLY mode)
    ↓
State Persistence & Logging (journals, telemetry)
```

### Key Components

| Module | Purpose |
|--------|---------|
| `nq_drift_vwap_engine.py` | Core NQ pullback/continuation strategy engine (frozen) |
| `nq_dvp_live_signal.py` | Live signal generation from completed bars only |
| `nq_dvp_nt_exec.py` | NinjaTrader execution adapter (Sim101 MNQ only) |
| `nt_ati.py` | NinjaTrader ATI bridge (OIF format, bracket mechanics, position parsing) |
| `execution_status.py` | Pause/resume gate and deployment state manager |
| `phase32_validate.py` | Freeze integrity checks, secret scan, test gate |
| `position_sizing.py` | Deterministic micro-contract sizing from frozen stops |
| `prop_firm_simulator.py` | Generic prop-firm rule validator (no strategy tuning) |
| `tests_phase32.py` | Integration tests (419+ test cases) |
| `tests_ninjatrader_execution.py` | NinjaTrader bridge tests (19 test cases) |

---

## Core Engineering Features

✅ **Modular Python architecture**
- Separation of signal logic, validation, execution, and logging
- Frozen strategy configs with SHA256 integrity hashes
- Deterministic, reproducible behavior

✅ **Strategy configuration**
- Frozen strategy parameters stored in immutable JSON files
- Hash-verified configuration integrity
- No live tuning permitted during deployment

✅ **Research and validation workflows**
- Historical backtesting against Databento archives
- Walk-forward and holdout sample validation
- Dry-run pipelines for forward testing

✅ **Dry-run operation**
- Default safe mode: no orders placed, no live capital used
- Full signal evaluation without execution
- Complete journaling of what would have executed

✅ **Fail-closed execution**
- Live execution **disabled** by default in `state/project_pause.json`
- Multiple safety gates before any order can be placed
- Corrupt state fails closed (never proceeds after restart with missing/invalid state)

✅ **Stale-data safeguards**
- Blocks entry if market data is older than threshold
- Refuses to trade on data that is out-of-sync with wall clock
- Validates bar completeness before signal generation

✅ **Position and account limits**
- Hard locks: Sim101 account only, MNQ micro contract only, qty=1 only
- Daily trade caps (max 4 trades/day for NQ DVP)
- Daily loss caps (stop after 2 losing trades/day)
- Floating position limits (one position at a time)

✅ **Execution deployment controls**
- Phase 32 pause state (paused by default)
- Explicit operator gate required to enable SIM_ONLY mode
- Risk checklist before each order (account, data, hash, limits)

✅ **Logging and observability**
- Comprehensive JSONL journal of all signals, entries, exits
- Event log with block/allow/error reasons
- State snapshots for recovery after restart
- Telemetry from broker (FundedNext MCP read-only, NinjaTrader position reads)

✅ **Comprehensive test coverage**
- 419+ phase-specific tests
- 19 NinjaTrader bridge tests
- Tests for freeze integrity, pause state, position sizing, risk checks, state recovery
- Tests for disabled execution paths and safety gates

---

## Safety-First Design

### Live Execution Is Disabled

- The system starts in **DRY_RUN** mode.
- Execution is paused in `state/project_pause.json` with explicit blockers.
- No orders can be placed without active operator engagement and multi-stage verification.

### Dry-Run Operation

- Signals are generated and validated without submitting any orders.
- Full logging of intended orders: entry price, stop, target, account, instrument.
- Can be run indefinitely for confidence-building forward testing.

### Explicit Deployment Gates

- **Phase 32 Pause State**: Project-wide pause/resume control.
- **Execution Status**: `DRY_RUN`, `SIM_ONLY` (armed), or `PROP_EVALUATION` (all blocked).
- **Safe-Start Checks**: Hash verification, account/feed confirmation, position reconciliation.
- **Operator Confirmation**: SIM_ONLY mode requires operator to set env var `AITRADE_SIM_ONLY_EXECUTION=1`.

### Fail-Closed Behavior

- If state file is missing or corrupted, system fails to startup.
- If strategy hash mismatches frozen config, entry is blocked.
- If account is not Sim101, order is rejected.
- If data is stale, signal is suppressed.
- If position is not flat, new entry is blocked.

### Stale-Data Handling

- Tracks age of latest completed 5m bar.
- Blocks entry if data gap exceeds 2.5 bars (~7.5 minutes).
- Prevents trading on delayed or disconnected feeds.

### Position Safeguards

- Reads position from NinjaTrader log on every submission.
- Refuses entry if position is not flat.
- Automatic bracket exit (stop + target OCO) on entry fill.
- Position reconciliation against broker before new trades.

### Shutdown and Halt

- Manual halt flag in state file prevents new entries.
- Emergency flatten command cancels all AITRADE-owned orders and closes position.
- Controlled restart after manual review.

---

## Strategies

### GC VWAP Mean Reversion (Phase 26, Frozen)

| Aspect | Detail |
|--------|--------|
| **Market** | GC (COMEX gold futures) |
| **Concept** | VWAP band reclaim + 2σ retest mean reversion |
| **Status** | Frozen for paper validation (Phase 26) |
| **Hash** | `0695d7a881b6247033426bceb077b6895b96405b826c21f32a109a02f44b2d43` |

### NQ Drift VWAP Pullback (Phase 30, Frozen)

| Aspect | Detail |
|--------|--------|
| **Market** | NQ (E-mini Nasdaq futures) |
| **Signal Instrument** | NQ (Databento stitched bars) |
| **Execution Instrument** | MNQ (micro, Sim101 only) |
| **Concept** | Drift confirmation on 15m bars → pullback retest entry on 5m → fixed asymmetric exits |
| **Entry Condition** | 15m drift (close > VWAP, VWAP rising, 1h return ≥ +0.10%) → first opposing 5m candle → entry next 5m open |
| **Exits** | Long: 80pt stop / 40pt target; Short: 80pt stop / 50pt target |
| **Position Rules** | One at a time, max 4 trades/day, stop after any 2 losses/day |
| **Status** | Frozen for paper validation (Phase 30) |
| **Hash** | `935e3a616351b09dbfa8d2d0e2b5d6850be803fb385ec5e5f8e3593856a1212a` |
| **Historical Benchmark** (Phase 29) | Full sample 5714 trades, OOS expectancy ~5.95 pts/trade edge observed |

**Note:** Strategy parameters are immutable. Do not modify stops/targets/timeframes without creating a new frozen phase. This preserves research/forward-split integrity.

---

## Repository Structure

```
Aitrade/
├── README.md                          ← You are here
├── .gitignore                         ← Excludes .env, logs, caches, secrets
├── .env.example                       ← Template for API keys (DATABENTO, TIINGO)
├── requirements.txt                   ← Python dependencies
│
├── docs/
│   ├── ARCHITECTURE.md               ← Component design & data flow
│   ├── SAFETY.md                     ← Safety mechanisms & assumptions
│   ├── DEPLOYMENT_RUNBOOK.md         ← Step-by-step setup & operation
│   ├── STRATEGY_REGISTRY.md          ← Historical phases & research branches
│   └── PROP_FIRM_READINESS.md        ← Checklist for prop firm integration
│
├── state/
│   └── project_pause.json            ← Execution pause/resume gate
│
├── strategy_frozen/
│   ├── gc_vwap_v2_phase26.json       ← GC VWAP immutable config + benchmark
│   └── nq_dvp_phase30.json           ← NQ DVP immutable config + benchmark
│
├── config/
│   ├── execution_metadata.json       ← Contract specs (point value, tick size)
│   └── prop_risk_template.json       ← Template for prop firm rules
│
├── journal/
│   ├── phase26_gc_vwap_v2_paper/     ← GC paper trades & validation
│   ├── phase30_nq_dvp_paper/         ← NQ paper trades & validation
│   └── phase31_nq_dvp_sim/           ← Sim101 execution logs
│
├── Core Strategy Modules
│   ├── nq_drift_vwap_engine.py       ← NQ DVP signal logic (frozen)
│   ├── nq_drift_vwap_models.py       ← DVP config dataclass + frozen constants
│   ├── nq_dvp_live_signal.py         ← Live signal snapshot generation
│   ├── nq_dvp_nt_exec.py             ← NinjaTrader execution adapter
│   ├── nq_dvp_freeze.py              ← Phase 30 freeze & validation
│   └── models.py                     ← Canonical data structures (Bar, TradeSetup, etc.)
│
├── NinjaTrader Bridge
│   ├── nt_ati.py                     ← ATI OIF format & bracket mechanics
│   ├── execution_status.py           ← Pause gate & execution state
│   ├── account_risk.py               ← Account locks & risk checks
│   ├── position_sizing.py            ← Deterministic micro-contract sizing
│   └── nq_dvp_live_runner.py         ← Main entry point for DRY_RUN/SIM_ONLY
│
├── Validation & Testing
│   ├── phase32_validate.py           ← Freeze integrity, secret scan, tests
│   ├── tests_phase32.py              ← 419+ integration tests
│   ├── tests_ninjatrader_execution.py ← 19 NinjaTrader bridge tests
│   └── prop_firm_simulator.py        ← Generic prop-firm rule validator
│
└── [Phase 43-54 research modules]     ← Archived research & experiments
    └── See STRATEGY_REGISTRY.md for historical context
```

---

## Installation

### Prerequisites

- Python 3.10+
- NinjaTrader 8 (optional, required for Sim101 execution testing)
- Databento account (optional, for historical research)

### Setup

```bash
# Clone the repository
git clone https://github.com/Tanatswa1011/Aitrade.git
cd Aitrade

# Create and activate virtual environment
python -m venv .venv
source .venv/bin/activate          # Linux/macOS
# or
.venv\Scripts\activate              # Windows

# Install dependencies
pip install -r requirements.txt

# Copy environment template
cp .env.example .env
# Edit .env with your API keys (DATABENTO_API_KEY, TIINGO_API_KEY, etc.)
# Never commit .env
```

---

## Configuration

Configuration is file-driven and environment-variable-based. Secrets are never committed.

### Environment Variables

Create `.env` from `.env.example`:

```bash
# Historical data
DATABENTO_API_KEY=your_databento_key
TIINGO_API_KEY=your_tiingo_key

# Optional: testing only (does not enable live execution)
# AITRADE_PHASE54_TEST=1           # Test mode (disables live feeds)
```

### Frozen Strategy Configuration

Strategies are frozen in JSON files with immutable hashes:

- `strategy_frozen/gc_vwap_v2_phase26.json` — GC VWAP mean reversion
- `strategy_frozen/nq_dvp_phase30.json` — NQ DVP pullback/continuation

Do not edit these files manually. To update a strategy, create a new phase and new freeze.

### Execution Metadata

`config/execution_metadata.json` defines contract specs:

```json
{
  "MNQ": {
    "point_value_usd": 2.0,
    "tick_size_points": 0.25,
    "dvp_frozen_brackets_points": {
      "long": {"stop": 80.0, "target": 40.0},
      "short": {"stop": 80.0, "target": 50.0}
    }
  }
}
```

### Prop Firm Rules

To validate against a prop firm's rules, load them into `config/prop_risk_template.json`:

```json
{
  "firm_name": "FundedNext",
  "profit_target": 3000.0,
  "max_drawdown": 2000.0,
  "drawdown_type": "EOD",
  "daily_loss_limit": 2000.0,
  "max_contracts": 2
}
```

Then run:

```bash
python prop_firm_simulator.py
```

---

## Running the Project

### 1. Validate Frozen Strategies

```bash
python phase32_validate.py
```

**Output:** JSON report with freeze hash verification, pause state, secret scan results, and test status.

**Expected:** All hashes match, execution is paused, no secrets found.

---

### 2. Run Full Test Suite

```bash
# Phase tests (419+ cases)
python -m unittest discover -s . -p "tests_phase*.py"

# NinjaTrader bridge tests (19 cases)
python -m unittest tests_ninjatrader_execution
```

**Expected:** All tests pass. No regressions.

---

### 3. Dry-Run Execution

Generates signals and validates risk checks without placing orders:

```bash
python nq_dvp_live_runner.py --status       # Check current state
python nq_dvp_live_runner.py --once         # Run one signal evaluation (DRY_RUN mode)
python nq_dvp_live_runner.py --equivalence  # Test historical bar replay
```

**Output:** Signal snapshots, validation results, dry-run orders logged to journal.

**Note:** `--once` runs in DRY_RUN mode by default. No orders are placed.

---

### 4. Check Execution Status

```bash
python nq_dvp_live_runner.py --status
```

**Output:** Current mode (DRY_RUN), project pause state, position, daily P&L.

---

### 5. Emergency Halt

```bash
python nq_dvp_live_runner.py --halt
```

Creates `journal/phase31_nq_dvp_sim/HALT` and blocks further submissions.

Resume only after manual review:

```bash
python nq_dvp_live_runner.py --resume
```

---

## Testing

AITRADE includes comprehensive test coverage:

### Test Coverage Summary

| Category | Count | Purpose |
|----------|-------|---------|
| Freeze integrity | 2 | Verify GC/NQ hash immutability |
| Pause gate | 2 | Ensure execution blocked when paused |
| Prop simulator | 4 | Test account pass/fail rules |
| Position sizing | 3 | Verify contract math and risk limits |
| Account risk | 4 | Test risk checks (account, hash, data, limits) |
| State recovery | 3 | Verify restart resilience & corruption handling |
| Halt/resume | 1 | Test emergency pause cycle |
| NinjaTrader safety | 7 | Sim101, MNQ, qty locks |
| Bracket math | 4 | OCO pricing, tick rounding |
| OIF format | 3 | Order formatting & parsing |
| Guard logic | 4 | Position & active state guards |
| Dry-run execution | 6 | No-submit paths |

### Run Tests

```bash
# All tests
python -m unittest discover

# Specific test file
python -m unittest tests_phase32.py

# Specific test class
python -m unittest tests_phase32.TestPhase32FreezeIntegrity

# Specific test method
python -m unittest tests_phase32.TestPhase32FreezeIntegrity.test_nq_hash
```

---

## Current Status

### Research and Deployment Preparation

- ✅ **Strategy Research**: NQ DVP and GC VWAP strategies frozen after research phases.
- ✅ **Validation**: Historical backtests and walk-forward validation complete.
- ✅ **Safety Engineering**: Execution safeguards, state management, and testing in place.
- ✅ **Execution Adapter**: NinjaTrader ATI bridge built for Sim101 (MNQ only).
- ✅ **Live Execution**: **Disabled** by default and by design.

### What This Is NOT

This repository is **not**:

- A financial product or service.
- A recommendation to trade any instrument.
- Proven to be profitable or to generate returns.
- A substitute for professional investment advice.
- Ready for live personal or prop account deployment without additional due diligence.

### What's Required Before Live Deployment

- [ ] Prop firm selected and rules documented
- [ ] Real-time data feed confirmed (not simulated)
- [ ] Feed equivalence validated (live bars ≡ research bars)
- [ ] NinjaTrader ATI bridge tested on target account
- [ ] Risk limits configured and reviewed
- [ ] Emergency halt procedure rehearsed
- [ ] Explicit operator approval to enable SIM_ONLY or PROP_EVALUATION

---

## Engineering Lessons

This project demonstrates:

✅ **Safe Automation**
- Separation of research from execution logic
- Deterministic validation at every stage
- Fail-closed defaults (paused, DRY_RUN, no live capital)

✅ **Deterministic Checks Around Non-Deterministic Systems**
- Market data staleness validation
- Exact account/instrument/quantity locks before execution
- State recovery after unexpected restarts

✅ **Separation of Research and Execution**
- Frozen strategy configs (immutable, hashed)
- Research can evolve independently without affecting live rules
- New phases preserve historical evidence

✅ **Stateful Recovery**
- Corruption fails closed (not blindly forward)
- State snapshots for debugging restart sequences
- Journaling of all decision logic

✅ **Configuration Integrity**
- Strategy hashes prevent accidental mutations
- Freeze phases preserve evidence splits
- Prop firm rules decoupled from strategy logic

✅ **Fail-Closed Design**
- Default: paused, DRY_RUN, no orders
- To execute: operator must actively choose, enable env vars, pass safety checks
- Corrupt state blocks restart

✅ **Observability**
- Comprehensive JSONL logging of signals, entries, exits, blocks
- Event log with reasoning (why a signal was blocked?)
- Telemetry snapshots for manual review and audit

---

## Disclaimer

AITRADE is a **software engineering and research project**, not financial advice. It is provided as-is for educational purposes. Trading futures and derivatives carries substantial risk of loss and is not suitable for all investors. The author assumes no responsibility for losses, performance, or misuse. This project is intended to demonstrate automated trading system design principles, not to guarantee profitability or safe returns. Consult a financial advisor before trading.

---

## Next Steps

1. Review `docs/ARCHITECTURE.md` for system design details.
2. Read `docs/SAFETY.md` for fail-closed mechanisms and assumptions.
3. Follow `docs/DEPLOYMENT_RUNBOOK.md` to set up and test locally.
4. Review `docs/STRATEGY_REGISTRY.md` for historical phases and research branches.
5. Run `python phase32_validate.py` to verify frozen strategies.
6. Run tests: `python -m unittest discover`.
7. Start with dry-run: `python nq_dvp_live_runner.py --once`.

---

## License

This project is provided without a license. Use as reference material only. Consult with legal and compliance teams before deploying any variant in production or with real capital.
