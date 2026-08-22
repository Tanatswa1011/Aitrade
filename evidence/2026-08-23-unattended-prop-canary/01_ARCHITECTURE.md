# Architecture and state machine

## Flags

| Flag | Role |
|---|---|
| `PROP_EXECUTION` | Hard false. General prop stays LOCKED. |
| `AITRADE_PROP_CANARY_EXECUTION` | Attended one-shot canary only. **Not sufficient** for unattended. |
| `AITRADE_UNATTENDED_PROP_CANARY` | Dedicated unattended flag. Missing/unset = DISABLED. |

Operator must set the unattended flag **and** call explicit day-enable (`POST /control/unattended/enable`). Enable is not an order.

## State machine

```
UNATTENDED_DISABLED
  → (flag + operator enable + preflight) UNATTENDED_PREFLIGHT
  → UNATTENDED_WAITING_LIVE_DATA
  → AUTOMATED_PHASE_55B_PASS
  → UNATTENDED_WAITING_SESSION   (before 10:30 ET)
  → UNATTENDED_WAITING_DVP       (10:30–15:30 ET)
  → UNATTENDED_ENTRY_PENDING     (one broker-boundary attempt)
  → UNATTENDED_POSITION_OPEN     (broker-native OCO WORKING)
  → UNATTENDED_EXIT_PENDING
  → UNATTENDED_COMPLETE          (flat + locked for day)

No DVP by 15:30 ET → UNATTENDED_COMPLETE_NO_TRADE
Any required fail → UNATTENDED_BLOCKED (locked for day)
Process restart → UNATTENDED_BLOCKED_RESTART (no silent resume)
```

## Preflight (`UNATTENDED_PREFLIGHT_PASS`)

Runtime, exact FundedNext identity (name+login+id, no wildcard/fallback), account/risk freshness, `PROP_FLAT_SAFE`, qty=1 MNQ, MNQ 09-26 only, frozen hash, notifications healthy, OIF route available, daily latch unused.

Failure: `UNATTENDED_PREFLIGHT_FAIL` → `UNATTENDED_BLOCKED`.

## Automated 55B

`AUTOMATED_PHASE_55B_PASS` only from runtime truth: NQ 1m count increasing, monotonic timestamps, no duplicate IDs, 5m/15m advancing, NY timezone, warmup complete, market LIVE not DELAYED/SIMULATED/PLAYBACK/EOD. Production never invents LIVE.

## Just-in-time

Immediately before the broker boundary, re-read market, bars, account, flat, MLL, policy, qty, instrument, latch, watchdog. Any fail: no submit, `UNATTENDED_BLOCKED`, Telegram.

## Daily latch

One **entry submission attempt** per NY trading day. Burned when the attempt crosses the broker boundary (including reject/exception). Survives restart. New calendar day clears it but still requires a new operator enable.

ARMED/WAITING_DVP is **not** persisted.

## Broker protection

Frozen DVP STOPMARKET + LIMIT OCO children on the exact FundedNext NT account, qty 1, `MNQ SEP26`. Python crash / desk crash / Telegram failure **do not cancel** those orders. Missing stop while OPEN → `PROTECTION_FAILURE_CRITICAL` + FN-only emergency flatten.

## Watchdog

`unattended_watchdog.py` is monitor/escalate only. It does not arm, does not write OIF, does not cancel stops, does not flatten Sim101.

- Flat + critical fail → BLOCK for day
- Open + engine lost → CRITICAL alert; broker stop must remain
- Open + stop missing → FN emergency flatten recommendation only
