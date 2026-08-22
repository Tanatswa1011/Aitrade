# 12 — Monday operator runbook (revised after Saturday pack)

Desk: `http://127.0.0.1:8765/`  
Saturday pack: `evidence/2026-08-22-saturday-ops/`  
Canary prep: `evidence/2026-08-23-prop-canary-prep/`

`Saturday evidence proved read-only FundedNext safety. Prop canary execution is a new phase added after the Saturday pack.`

Do not enable general `PROP_EXECUTION`. Do not treat a green canary READY as “PROP EXECUTION ON.” Do not press F5 unless AddOn source changed.

**Monday execution goal**

`FundedNext one-shot canary if READY_FOR_PROP_CANARY`

Not Sim101-first. Not general prop trading.

If and only if every live-data, account-reconciliation, risk, routing, and safety gate passes, AITRADE may execute **exactly one** genuine 1 MNQ live canary on the FundedNext Flex 50K evaluation:

- 1 account only (`FNFTCHTANATSWAPHILMU92044` / login `962841277` / id `3969349`)
- 1 MNQ maximum
- 1 genuine frozen `phase54_live` DVP
- 1 round trip maximum
- explicit operator arm
- automatic disarm after the round trip
- no automatic transition into normal prop execution

Sequence:

`SATURDAY_OPS_READY` → `SUNDAY_PROP_CANARY_PREP` → `PHASE_55B_0_PASS` → `READY_FOR_PROP_CANARY` → `PROP_CANARY_ARMED` → one genuine 1 MNQ FundedNext round trip → `PROP_CANARY_DISARMED`

Any required gate fail: `PROP_CANARY_BLOCKED`

---

## Stage 1 — live market validation

Execution disarmed. Canary DISARMED. General prop LOCKED.

1. Start NinjaTrader. Reach Control Center.
2. Do not press F5 unless `ninjascript/AITRADEReadOnlySnapshot.cs` changed. Expected DLL mtime remains `2026-08-22T12:03:22Z` / SHA256 `1cc5f74ec5cd9c48b3e08fb10f3200f2ea048daab7a718f4fb2b5855b771912d` until a new compile.
3. Confirm AddOn telemetry (`schema=AITRADE_NT_READONLY_V1`).
4. Start the Python desk if needed (`python dashboard/ops-console/api.py`).
5. Confirm FundedNext = CONNECTED, **GENERAL PROP: LOCKED**, **CANARY: LOCKED/DISARMED**.
6. Confirm `PROP_EXECUTION=false` and `AITRADE_PROP_CANARY_EXECUTION` unset/false.
7. Confirm SIM_ONLY DISARMED.
8. Do not bypass Safe Start.
9. Observe NQ 09-26 live 1m bars arriving; timestamps advancing; 5m and 15m aggregations advancing; no parser/schema errors; warmup complete; market classified genuinely LIVE; `America/New_York` handling intact.

**Gate:** `PHASE_55B_0_PASS`  
Do not arm the FundedNext canary before this gate.

If Globex is open but Safe Start still fails, stop and diagnose. Do not loosen LIVE quote requirements.

---

## Stage 2 — prop canary readiness

Verify all FundedNext account / risk / routing / reconciliation conditions:

- exact account allowlist: NT `FNFTCHTANATSWAPHILMU92044`, login `962841277`, MCP id `3969349`
- no wildcard / first-available / Sim101 fallback
- account connected, trade-enabled, state fresh
- equity, balance, MLL / remaining drawdown known
- MNQ position known and FLAT
- no working MNQ orders
- recon `PROP_FLAT_SAFE` (not Sim101 `FLAT_SAFE`)
- locked FundedNext Flex 50K policy PASS
- instrument NQ 09-26 → MNQ 09-26 only
- qty hard cap = 1 MNQ
- `LIVE_DVP_REQUIRED`
- Telegram/Apprise healthy (outbound only)
- kill/flatten path available
- broker route = NinjaTrader ATI OIF to that exact account (MCP cannot submit orders)
- stop/target path structurally the frozen DVP OCO children

**Gate:** `READY_FOR_PROP_CANARY`

Optional structural check (does not transmit):

`POST /control/prop-canary/dry-run`

Expect `PROP_CANARY_DRY_RUN_PASS` only when Stage 1–2 gates are actually true. A fail-closed dry-run is correct. Do not fake LIVE.

---

## Stage 3 — operator arm

Only after Stage 2.

Set `AITRADE_PROP_CANARY_EXECUTION=true` in the running process environment **and** call the explicit arm action (`POST /control/prop-canary/arm`). Missing/unset flag remains DISARMED.

Arming does **not** place a trade. It means: the next qualifying genuine `phase54_live` event newer than arm time may execute exactly once.

State: `PROP_CANARY_ARMED`

Restart, reconnect, or process recycle returns DISARMED. Do not persist ARMED.

---

## Stage 4 — wait for genuine DVP

Frozen window: **10:30–15:30 ET**

Do not force a trade. Do not use the Aug 14 shadow SHORT. Do not use warmup/history/replay/cached signals.

If no genuine `phase54_live` event:

`NO_VALID_DVP_EVENT`

Disarm. That is a valid observation day.

---

## Stage 5 — one live trade maximum

If a genuine `phase54_live` event appears after arm time:

- exact FundedNext account
- MNQ 09-26 / `MNQ SEP26`
- qty 1
- one round trip only
- protective STOPMARKET + LIMIT on the same account/qty/instrument after fill
- if stop cannot be confirmed: CRITICAL + existing flatten/emergency path

After completion: `PROP_CANARY_COMPLETE` then `PROP_CANARY_DISARMED`

If entry is rejected, execution throws, account becomes unsafe, data goes stale, or NT disconnects: DISARM. No second trade.

---

## Hard stop — cancel the live canary

Return `PROP_CANARY_BLOCKED` and do not work around the gate if any of:

- PHASE_55B_0 fails
- market not LIVE
- FundedNext data stale
- MLL/risk state unknown
- account not flat
- working order exists
- reconciliation uncertain
- Telegram unavailable (monitoring mandatory for this canary)
- broker route unproven in this runtime
- stop protection path unproven
- Safe Start fails
- wrong account
- wrong instrument
- quantity != 1
- live provenance unavailable
- runtime modified since evidence without revalidation

---

## Telegram

Outbound only. No Telegram flatten commands. A notify failure must not change execution state.

## Frozen product

Do not change NQ Drift VWAP Pullback, frozen hash, window, VWAP, 15m drift, 5m pullback, thresholds, or DVP provenance rules.
