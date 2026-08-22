# Sunday 23 Aug 2026 — FundedNext prop-canary prep

Pack: `evidence/2026-08-23-prop-canary-prep/`  
Follows Saturday `evidence/2026-08-22-saturday-ops/` (`SATURDAY_OPS_READY`).

`Saturday evidence proved read-only FundedNext safety. Prop canary execution is a new phase added after the Saturday pack.`

This pack does **not** rewrite Saturday. It does **not** claim a live FundedNext fill. No real order was transmitted during engineering.

## Objective

One explicit FundedNext Flex 50K canary — not general prop execution.

`PROP_EXECUTION=false` remains the general lock.  
`AITRADE_PROP_CANARY_EXECUTION` (default missing/false = DISARMED) is the dedicated canary flag.

## Verdicts

### Current implementation

`READY_FOR_PROP_CANARY_PRELIVE`

The dedicated route, allowlist, qty cap, one-shot latch, dry-run builder, desk/alert surface, and tests exist. The live-data gate is still closed until Globex proves `PHASE_55B_0_PASS`.

Not `READY_FOR_PROP_CANARY` (that is a Monday live-ops gate).

### Dry-run

`PROP_CANARY_DRY_RUN_PASS` — structural fixture (armed + genuine `phase54_live` signal, no incoming write, no broker ack).

A live-runtime dry-run on a closed/stale Saturday–Sunday market must still fail the live-data gate. That is correct fail-closed behaviour, not a reason to fake LIVE.

### Remaining live-only gate

Final permission still requires:

`PHASE_55B_0_PASS`

before any live FundedNext arming.

## What this phase built

| Item | Detail |
|---|---|
| Sequence | `SATURDAY_OPS_READY` → `SUNDAY_PROP_CANARY_PREP` → `PHASE_55B_0_PASS` → `READY_FOR_PROP_CANARY` → `PROP_CANARY_ARMED` → 1 MNQ round trip → `PROP_CANARY_DISARMED` |
| Fail | `PROP_CANARY_BLOCKED` |
| Account allowlist | Exact NT `FNFTCHTANATSWAPHILMU92044` + login `962841277` + MCP id `3969349`. No AUTO, no first-available, no Sim101 fallback |
| Instruments | Signal `NQ 09-26` → exec `MNQ 09-26` / `MNQ SEP26`. NQ/ES/GC/CL rejected |
| Qty | Hard cap exactly 1 MNQ at the OIF boundary |
| Route | `phase54_live` → provenance → FN account/risk → PROP_CANARY → allowlist → qty cap → `prop_canary_nt_exec` ATI OIF → NT → recon → disarm |
| MCP | Authoritative **account/risk** source. **Cannot submit orders** |
| Orders | NinjaTrader ATI `PLACE` into the exact FN NT account |
| Recon | `PROP_FLAT_SAFE` (distinct from Sim101 `FLAT_SAFE`) |
| One-shot | Arm is in-memory only. Restart/reconnect DISARMED. Reject/exception/stale/disconnect DISARM |
| General prop | Stays LOCKED. Completing the canary does not set `PROP_EXECUTION=true` |

## Frozen product (unchanged)

NQ Drift VWAP Pullback hash `935e3a616351b09dbfa8d2d0e2b5d6850be803fb385ec5e5f8e3593856a1212a`. No strategy research or parameter optimization.

## Tests

`python -m unittest tests_prop_canary.py` plus existing `tests_notifications` / `tests_phase55` / `tests_phase55b`: **126 OK** (40 canary + 28 notifications + 36 phase55 + 22 phase55b).

No real FundedNext or Sim101 order was sent in this engineering phase.
