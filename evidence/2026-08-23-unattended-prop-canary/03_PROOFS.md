# Dry-run, protection, watchdog, restart

Captured from fixtures. No incoming OIF write. No broker order.

## `UNATTENDED_DRY_RUN_PASS`

```
transmitted: false
second_blocked: true
account: FNFTCHTANATSWAPHILMU92044
qty: 1
sim101_in_payload: false
PROP_EXECUTION: false
```

Lifecycle exercised: preflight → live-validation fixture → session → genuine phase54_live → JIT → payload → simulated fill → simulated broker-working OCO → simulated exit → flat → daily lockout.

## `BROKER_PROTECTIVE_ORDER_SURVIVAL_PASS`

Protective orders are NinjaTrader ATI `PLACE STOPMARKET` + `LIMIT` sharing an OCO id on the exact FundedNext account / `MNQ SEP26` / qty 1 (`OIF_FILL_THEN_OCO_CHILDREN`). They are not Python-held.

Crash surface:

- Python engine crash while OPEN: `cancel_stop=false`, `broker_native_stop_survives=true`, alert `UNATTENDED_ENGINE_LOST_POSITION_OPEN`
- Dashboard crash while OPEN: same (no cancel)
- Telegram failure: cannot alter execution; does not cancel stop

If stop cannot be confirmed after fill: `PROTECTION_FAILURE_CRITICAL` + FundedNext-only emergency flatten.

## `WATCHDOG_PASS`

Independent module `unattended_watchdog.py`. No trading decisions. No OIF. No Sim101 flatten. Flat+critical → BLOCK_DAY. Open+lost engine → alert only. Open+missing stop → FN flatten recommendation.

## `UNATTENDED_NO_AUTO_REARM_PASS`

Restart before entry → `UNATTENDED_BLOCKED_RESTART`. Restart after submit → daily latch persists, enable refused. Reconnect does not restore WAITING_DVP.
