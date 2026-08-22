# Desk and Telegram

## Desk

Strip: **FUNDEDNEXT UNATTENDED CANARY** (`dashboard/ops-console/index.html`).

Shown as separate fields, not one green light:

- state
- exact account `FNFTCHTANATSWAPHILMU92044`
- daily attempt UNUSED / USED
- market
- Stage 55B
- FN position
- `PROP_FLAT_SAFE`
- MLL / remaining drawdown
- stop / target
- watchdog
- alerting
- last change
- GENERAL PROP LOCKED

Controls: `POST /control/unattended/enable|disable|dry-run`, `GET /control/unattended/status`.

Desk poll uses `tick(..., allow_entry=False)` so a live signal during snapshot cannot burn the latch.

## Telegram (outbound only)

Event types:

`UNATTENDED_PREFLIGHT_PASS`, `UNATTENDED_PREFLIGHT_FAIL`, `LIVE_BAR_VALIDATION_PASS`, `UNATTENDED_WAITING_DVP`, `UNATTENDED_BLOCKED`, `UNATTENDED_DVP_DETECTED`, `UNATTENDED_ORDER_SUBMITTED`, `UNATTENDED_ORDER_ACCEPTED`, `UNATTENDED_ORDER_REJECTED`, `UNATTENDED_POSITION_OPENED`, `UNATTENDED_STOP_CONFIRMED`, `UNATTENDED_TARGET_CONFIRMED`, `UNATTENDED_ENGINE_LOST_POSITION_OPEN`, `UNATTENDED_PROTECTION_FAILURE`, `UNATTENDED_POSITION_CLOSED`, `UNATTENDED_COMPLETE`, `UNATTENDED_COMPLETE_NO_TRADE`.

No Telegram command can flatten or arm.
