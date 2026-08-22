# Monday operator procedure (unattended)

Do not enable general `PROP_EXECUTION`. Do not treat READY as a green “PROP ON” light.

Desk: `http://127.0.0.1:8765/`

## Before leaving

1. Start Windows/PC normally.
2. Start NinjaTrader. Reach Control Center. Confirm broker/data connection.
3. Do not press F5 unless AddOn source changed.
4. Start AITRADE desk: `python dashboard/ops-console/api.py`
5. Open the desk. Confirm exact account `FNFTCHTANATSWAPHILMU92044`.
6. Confirm **GENERAL PROP: LOCKED**, Sim101 DISARMED, canary not a generic green execution light.
7. Set `AITRADE_UNATTENDED_PROP_CANARY=true` for this process (missing/false stays DISABLED).
8. Explicitly enable the unattended canary for **this day** (`POST /control/unattended/enable` or the operator control).
9. Wait for **`UNATTENDED_PREFLIGHT_PASS`**.

At this point the desk may still show **`WAITING LIVE DATA`**. Do not manually arm later. Do not stay to click through 55B.

If preflight fails: **do not leave**. Diagnose. Do not work around the gate.

## While away

AITRADE may only:

- WAIT safely (`WAITING_LIVE_DATA` / `WAITING_SESSION` / `WAITING_DVP`)
- BLOCK safely (`UNATTENDED_BLOCKED` — locked for the day)
- execute exactly one qualified 1 MNQ FundedNext round trip with broker-confirmed protection
- lock itself (`COMPLETE` or `COMPLETE_NO_TRADE`)

Nothing else. No second attempt. No Sim101. No general prop.

Window: **10:30–15:30 America/New_York**. No DVP → `NO_VALID_DVP_EVENT` → locked.

Telegram is outbound only. No Telegram flatten commands.

## Restart / disconnect

Restart after enable, before a submit: `UNATTENDED_BLOCKED_RESTART` until the operator re-enables. No silent resume.

Restart after an entry attempt: daily latch remains used. No second trade.

Disconnect while flat: block for the day. Disconnect while open: monitor/recon only; never a second entry.

## Remaining live-only proofs (Monday)

- `AUTOMATED_PHASE_55B_PASS` from real Globex NQ 1m
- fresh FundedNext MCP equity/MLL/`PROP_FLAT_SAFE`
- real broker fill + WORKING stop/target
- real flatten/recon after the round trip

Until those exist, the engineering verdict remains **prelive**.
