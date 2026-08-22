# 23 Aug 2026 — Unattended FundedNext prop canary (prelive)

Pack: `evidence/2026-08-23-unattended-prop-canary/`  
Prior: Saturday ops (`SATURDAY_OPS_READY`) and attended canary prep (`READY_FOR_PROP_CANARY_PRELIVE`).

This pack does **not** rewrite Saturday. It does **not** claim live FundedNext fills. No real order was transmitted.

## Objective

Operator starts AITRADE Monday, verifies one preflight, leaves. System may autonomously execute **at most one** genuine 1 MNQ FundedNext Flex 50K canary, then lock for the day.

Not general autonomous prop trading.

## Engineering verdicts

| Gate | Result |
|---|---|
| Implementation | **`READY_FOR_UNATTENDED_PROP_CANARY_PRELIVE`** |
| Dry-run | **`UNATTENDED_DRY_RUN_PASS`** |
| Broker-native protection survival | **`BROKER_PROTECTIVE_ORDER_SURVIVAL_PASS`** |
| Watchdog | **`WATCHDOG_PASS`** |
| Restart / no auto-rearm | **`UNATTENDED_NO_AUTO_REARM_PASS`** |

Not live permission. Monday still must prove `AUTOMATED_PHASE_55B_PASS` plus fresh FundedNext account/risk before `UNATTENDED_WAITING_DVP`.

## Absolute assertions (this engineering session)

- no real FundedNext order submitted
- `PROP_EXECUTION=false`
- unattended mode not live-armed today
- allowlist unchanged (`FNFTCHTANATSWAPHILMU92044` / `962841277` / `3969349`)
- max qty = 1 MNQ
- frozen NQ DVP hash unchanged
- Sim101 ineligible
- Telegram outbound-only

## Tests

`python -m unittest tests_unattended_prop_canary.py tests_prop_canary.py tests_notifications.py tests_phase55.py tests_phase55b.py`

**175 OK** (49 unattended + 40 attended canary + 28 notifications + 36 phase55 + 22 phase55b).
