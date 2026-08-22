# Architecture — FundedNext one-shot canary

## Authoritative sources

| Role | Source | Notes |
|---|---|---|
| Account / equity / MLL / rules | FundedNext MCP (`fundednext_mcp.py`) | Read-only. Cannot place orders. |
| Market data / NQ bars | NinjaTrader AddOn dump | Live provenance `phase54_live` |
| Order submission | NinjaTrader ATI OIF `incoming` | Dedicated canary builders in `prop_canary_nt_exec.py` |
| Order status / fill | NinjaTrader log + account | Same mechanism as Sim101, **different account lock** |
| Position recon | NT account position + MCP | Canary requires `PROP_FLAT_SAFE` |

Tradovate REST remains deprecated for money/risk. MCP is not an order API.

## Route (explicit, not Sim101)

```
phase54_live DVP
  → live provenance gate (not shadow/warmup/history/replay)
  → FundedNext account/risk gate (MCP + NT identity)
  → PROP_CANARY gate (flag + in-memory ARM + one-shot latch)
  → exact account allowlist (name + login + id)
  → qty hard cap = 1 MNQ
  → prop_canary_nt_exec (refuses Sim101)
  → broker (NT ATI PLACE MNQ SEP26)
  → fill → frozen DVP STOPMARKET+LIMIT OCO on the same account/qty/instrument
  → recon FLAT
  → automatic DISARM
```

Sim101 continues to use `nt_ati.assert_sim101` / `nq_dvp_nt_exec.EXEC_ACCOUNT=Sim101` / `NinjaTraderExecutionBridge.executable_accounts={Sim101}`. Those paths cannot emit the FundedNext account. The canary path cannot emit Sim101.

## Allowlist (fail closed)

Must match **all** of:

- NT account name `FNFTCHTANATSWAPHILMU92044`
- platform login `962841277`
- FundedNext internal id `3969349`

Reject AUTO / AUTO_FUNDEDNEXT / empty / second FN account / personal / generic broker / Sim101.

## Quantity

Phase 52 FAST policy still allows 2 MNQ in general policy math. The canary **ignores FAST** and rejects any requested qty ≠ 1 immediately before OIF write (`validate_canary_oif_line`).

## State model

`PROP_LOCKED` → `PROP_CANARY_READY` → `PROP_CANARY_ARMED` → `PROP_CANARY_IN_FLIGHT` → `PROP_CANARY_COMPLETE` → `PROP_CANARY_DISARMED`

Failures: `PROP_CANARY_BLOCKED`

`AITRADE_PROP_CANARY_EXECUTION` missing/unset/false = DISARMED. ARMED is process memory only. Restart while in-flight → BLOCKED.

## Protective orders

Frozen DVP distances from `nq_dvp_nt_exec.frozen_risk_for_direction` (unchanged). Mechanism remains `OIF_FILL_THEN_OCO_CHILDREN`. Stop qty/account/instrument must equal the entry. Stop rejection → CRITICAL + existing flatten path (`CLOSEPOSITION` on the exact FN account). Telegram remains informational only.

## Files

- `prop_canary.py` — gates, arm, one-shot, dry-run, restart semantics
- `prop_canary_nt_exec.py` — FN-only OIF builders
- `phase54_ops.py` — snapshot + engine hook (separate from Sim101)
- `dashboard/ops-console/*` — GENERAL PROP LOCKED + CANARY state
- `aitrade_notifications.py` — PROP_CANARY_* + lifecycle events
- `tests_prop_canary.py`
