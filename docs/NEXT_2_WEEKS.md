# Next two weeks (post Phase 32)

**No new strategy research.** Priority is backup, security, prop selection, risk modeling, and operational reliability.

## Priority 1 — Prop firm selection

Shortlist firms using:

- Futures focus (NQ/MNQ, GC/MGC)
- Algo/automation explicitly allowed
- NinjaTrader compatibility
- Rithmic or equivalent feed included or supported
- Evaluation price within budget
- Drawdown model (EOD vs intraday trailing)
- Payout and consistency rules
- Automation/API restrictions documented

Output: firm choice + rules copied into `config/prop_risk_template.json`.

## Priority 2 — Prop-rule simulation

When firm is selected:

```bash
python -c "from prop_firm_simulator import *; ..."
```

Load exact rules; replay GC V2 and NQ DVP historical trades separately and combined. Record pass/fail — do not tune strategies.

## Priority 3 — Risk sizing

Decide MNQ and MGC quantities from:

- Prop max drawdown and daily loss limits
- Frozen strategy stops (NQ: 80pt; GC: dynamic)
- Target max loss per trade
- `position_sizing.py` + `config/execution_metadata.json`

## Priority 4 — Feed equivalence

Once prop/live data exists:

- Compare NinjaTrader/Rithmic NQ vs Databento research feed
- Compare GC live vs research feed
- Confirm: bars, VWAP, triggers, session timing, trade levels

## Priority 5 — Final pre-evaluation dry run

Only after Priorities 1–4:

1. Clear pause gate items in `state/project_pause.json`
2. DRY_RUN full session
3. Sim101 bracket QA (no live account)
4. Journal review
5. Explicit approval for prop evaluation mode

## Blockers (current)

- Prop firm not selected
- No paid live/prop market data
- No evaluation purchased
- NQ/MNQ feed equivalence not measured
- GC live feed equivalence not measured
- Project paused (`execution_status = PAUSED`)
