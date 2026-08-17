# Prop firm readiness checklist

Complete before purchasing any evaluation. **Do not select a firm automatically** — user decision required.

## Firm selection

- [ ] Prop firm selected
- [ ] Evaluation price within budget
- [ ] Automated/algo trading explicitly permitted (written rule)
- [ ] NinjaTrader supported
- [ ] Rithmic or equivalent real-time feed confirmed

## Instrument support

- [ ] NQ allowed
- [ ] MNQ allowed
- [ ] GC allowed
- [ ] MGC allowed

## Account rules documented

Load exact values into `config/prop_risk_template.json` when known.

- [ ] Profit target documented
- [ ] Max drawdown documented
- [ ] EOD vs intraday trailing drawdown documented
- [ ] Daily loss limit documented
- [ ] Contract limit documented
- [ ] Consistency rules documented
- [ ] Forced-flatten time documented
- [ ] Payout rules documented

## Restrictions

- [ ] Automation/API restrictions documented
- [ ] Copy-trading restrictions documented
- [ ] Prohibited strategy/activity rules documented
- [ ] Reset/renewal fees documented

## AITRADE integration (after firm selected)

- [ ] Rules loaded into `config/prop_risk_template.json`
- [ ] Historical pass/fail run via `prop_firm_simulator.py` (GC V2, NQ DVP, combined)
- [ ] MNQ/MGC sizing decided from drawdown + daily limits + frozen stops
- [ ] Feed equivalence: NQ bars/VWAP/triggers vs research feed
- [ ] Feed equivalence: GC bars/VWAP/triggers vs research feed
- [ ] Final pre-evaluation DRY_RUN + Sim101 dry run
- [ ] Explicit approval recorded to clear `state/project_pause.json`

## Simulation only

Prop simulator answers: *Given this firm's exact rules and fixed contract sizing, would the frozen strategy historically pass or fail?*

It does **not** optimize strategies against prop rules.
