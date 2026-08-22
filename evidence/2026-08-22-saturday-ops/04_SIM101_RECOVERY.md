# 04 — Sim101 position and FLAT_SAFE

## Current runtime (2026-08-22T14:21:49Z)

Dump `sim101`:

- present: true
- excluded: false (`sim101_excluded=false`)
- account: Sim101
- instrument: MNQ 09-26
- side: FLAT
- quantity: 0
- source: `NINJATRADER_ACCOUNT_POSITION`
- dump age: ~0.3s (not stale)

## Consumer chain (actual runtime, not a lone JSON field)

1. AddOn writes `outgoing/AITRADE_READONLY.json` (`AITRADE_NT_READONLY_V1`).
2. `NTReadOnly.runtime_snapshot()` reads the dump (tolerates mid-write).
3. `sim101_telemetry.parse_sim101_position(rt)` → `known=true`, `flat=true`, `stale=false`, `source=NINJATRADER_ACCOUNT_POSITION`.
4. `fundednext_must_not_substitute` keeps Sim101 and sets `fundednext_position_ignored=true`.
5. `BrokerAdapter.sim101_positions()` in `phase54_ops.py` is that parse + substitute guard.
6. `recovery_from_sim101(sim101, expected_flat=True, aittrade_orders=0)` → **`FLAT_SAFE`**.
7. Desk `snapshot()["sim101_recovery"]` = **`FLAT_SAFE`**.
8. Execution bridge `NinjaTraderExecutionBridge.reconcile()` (Saturday live Task 2): status **`FLAT_SAFE`**, entries blocked until that status. Not re-invoked for this pack (reconcile writes state).

Captured parse output: `raw/facts.json` → `parsed_sim101`, `recovery_from_sim101`, `fundednext_must_not_substitute`.

## Restart / reconnect

| Gate | sim101_recovery |
|---|---|
| A1 baseline (PID 6020) | FLAT_SAFE |
| A4 after Welcome (PID 15376) | FLAT_SAFE |
| A7 COLD_RESTART_SAFE | FLAT_SAFE |
| B2 Kinetick disconnected | FLAT_SAFE |
| B3 Kinetick reconnected | FLAT_SAFE |
| B4 restored | FLAT_SAFE |
| Pack capture | FLAT_SAFE |

## Note (not a failed gate)

At 14:19:02Z the notifier delivered `RECOVERY_UNSAFE` then immediately `RECOVERY_FLAT_SAFE` (14:19:03Z). Likely a one-cycle unknown parse (dump mid-write) then restore. Pack capture and all named recovery gates show **FLAT_SAFE**. See `11_ALERTING.md`.
