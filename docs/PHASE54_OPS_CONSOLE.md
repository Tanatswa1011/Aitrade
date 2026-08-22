# Phase 54 — Operations console (read-only)

Dashboard → AITRADE Control API → `phase54_ops` → NinjaTrader (market data / position) + FundedNext MCP (account/risk) → Policy Engine.

The dashboard contains **no trading logic**. `PROP_EXECUTION=false` is enforced in `phase54_ops.py` and the operating policy. There is **no Enable Execution** control. No OIF is written to NinjaTrader `incoming`. FundedNext MCP is account-read only.

## Integration map

| Console field | Authoritative module |
| --- | --- |
| Engine RUNNING/STOPPED | `phase54_ops.EngineSupervisor` → `state/phase54_ops.json` |
| Market data LIVE/STALE/DISCONNECTED | Read-only AddOn Last/Bid/Ask **print time** in `outgoing/AITRADE_READONLY.json`. File mtime (including `db/cache/*.ntb`) is **never** LIVE. NT connection / `outgoing/Simulation.txt` is **not** LIVE. |
| Market quality | `LIVE` / `DELAYED` / `SIMULATED`. Simulated Data Feed cannot pass Safe Start. Delayed is labeled and fails `fresh_market_data`. |
| FundedNext CONNECTED / READ_ONLY permission | FundedNext MCP read-only adapter (`FundedNextMCPReadOnlyAdapter`), never Sim101 |
| Policy ACTIVE/DEGRADED | `phase52_policy` + frozen hash + `PROP_RULES_V1` + FundedNext MCP equity/risk + recon YES + live NT market data with quality LIVE |
| Order execution DISABLED | `aitrade_operating_policy.broker_execution` + `PROP_EXECUTION` lock |
| Last signal / policy decision | Live `evaluate_intent` on the latest operator signal (`journal/phase54_ops/signals.jsonl`, else Phase 53 shadow labeled as such) |
| Execution decision | Always `BLOCKED · PROP_EXECUTION=false` |
| Position | Three-way: NinjaTrader FundedNext vs FundedNext MCP `runningTrades` vs AITRADE expected |
| Risk FAST/SAFE/PROTECTED/NEAR | Live FundedNext MCP equity + official remaining buffer via `live_eval_state()` / `evaluate_intent` |
| Equity / MLL | FundedNext MCP money/risk. Never Sim101, never $50,000 nominal, never Phase 53 shadow, never NT zeros, never failed Tradovate values. Missing → unavailable. `equity_source = FUNDEDNEXT_MCP`, `risk_source = FUNDEDNEXT_MCP`. |
| Equity curve | `journal/phase54_ops/telemetry.jsonl` from FundedNext MCP snapshots (`source=FUNDEDNEXT_MCP`) |
| Soak | `journal/phase54_ops/soak.json` — heartbeats, MCP reads, signals, policy, blocked execution, mismatches. No fabricated P&amp;L. Tests use `AITRADE_PHASE54_TEST=1` and a separate journal dir. |
| Hashes | `assert_frozen()` — NQ remains `935e3a616351b09dbfa8d2d0e2b5d6850be803fb385ec5e5f8e3593856a1212a` |

## Phase 54F — live NinjaTrader market data + shadow loop

Independent statuses:

```text
fundednext_account_status     FundedNext MCP
ninjatrader_market_connection NinjaTrader price feed
market_data_freshness         LIVE / STALE / DISCONNECTED
```

`/api/snapshot` `market_data` is a nested object: `source`, `instrument`, `last`, `bid`, `ask`, `timestamp`, `age_seconds`, `freshness`, `quality`, `connection`. Compact dashboard badge still uses freshness.

LIVE requires: connected-enough feed, a real Last (or Bid) print, `last_update` age ≤ `stale_market_sec` (120), quality ≠ SIMULATED. The AddOn write clock is not a print. Cache `.ntb` mtime is not a print.

Contracts (CME quarterly, 3rd Friday): on 2026-08-21 front month is **NQ 09-26** / **MNQ 09-26** (expiry 2026-09-18). Mapping remains **NQ signal → MNQ evaluation size**. Frozen NQ strategy hash is unchanged.

When Safe Start passes: `ENGINE=RUNNING`, `ORDER_EXECUTION=DISABLED`, `PROP_EXECUTION=false`. New entries halt if market leaves LIVE/quality LIVE or MCP leaves LIVE. Policy approvals journal `BLOCKED · PROP_EXECUTION=false`.

Install/recompile the AddOn: `ninjascript/AITRADEReadOnlySnapshot.cs` (copy to `Documents/NinjaTrader 8/bin/Custom/AddOns/`), then restart NinjaTrader. It subscribes MNQ+NQ, persists OnMarketData Last/Bid/Ask, and never writes `incoming`.

## Phase 54B — live read-only validation

Target operator state when **real** MNQ/NQ prints and FundedNext account values are available:

```text
ENGINE: RUNNING
MARKET DATA: LIVE
FUNDEDNEXT: CONNECTED · READ_ONLY
POLICY ENGINE: ACTIVE
EQUITY / MLL: real FundedNext values
POSITION: actual FundedNext position
RECONCILED: YES
ORDER EXECUTION: DISABLED
PROP_EXECUTION=false
```

Safe Start still requires every check except that **execution permission checked = PASS** means the disabled state was verified (`execution_permission_value = DISABLED`). Passing Safe Start starts the engine observer loop; it does not enable orders.

### Market-data freshness

`Market Data: LIVE` only when a genuine MNQ/NQ last-data artifact updated within `stale_market_sec` (default 120s). Authoritative sources, in practice:

1. Read-only AddOn JSON `Documents/NinjaTrader 8/outgoing/AITRADE_READONLY.json` (`MarketData.Last.Time`)
2. Newest MNQ/NQ `*.Last.ncd` / `*.Last.ntb` under `db/minute`, `db/cache`, `db/tick`, `db/day`

NinjaTrader **connected** (including `outgoing/Simulation.txt` CONNECTED, or a Simulation price-feed log line) is **not** LIVE. Global simulation mode with idle last-files is STALE.

### FundedNext equity / MLL

Preferred source: the same read-only AddOn JSON (`Account.Get` CashValue / NetLiquidation for the `FN*` account only).

Fallbacks: sqlite `AccountItems` for that account id; unredacted NT trace `CashValue` (ignore `*****`).

If none of those yield a number, equity and MLL stay **unavailable** and Safe Start fails `equity_mll_available`. MLL is derived from real equity via Phase 52 (`MLL_LOCK_AT` / `START_EQUITY - MAX_LOSS`) and is never invented.

Install the AddOn: compile `ninjascript/AITRADEReadOnlySnapshot.cs` (copied to `Documents/NinjaTrader 8/bin/Custom/AddOns/`) in the NinjaScript editor, then restart NinjaTrader. The AddOn writes JSON only. It does not submit orders or write `incoming`.

### Isolation and execution block

Sim101 equity and positions are excluded from FundedNext evaluation fields. Emergency flatten journals `REQUESTED_NOT_TRANSMITTED` and does not call `drop_oif`. Mode switching cannot enable execution.

## Phase 54C — compiled read-only AddOn

The AddOn is compiled into `NinjaTrader.Custom.dll` and writes `outgoing/AITRADE_READONLY.json` about once per second.

Runtime result on this machine:

* FundedNext id `FNFTCHTANATSWAPHILMU92044` is detected.
* Sim101 is present and excluded.
* `Account.Get` CashValue / NetLiquidation / P&amp;L / margin items return `0`, which is treated as **unavailable** (not a real $0 eval).
* MNQ instrument is resolved (`MNQ 09-26`) but Last/Bid/Ask are null under global simulation with no subscribed prints.
* `FUNDEDNEXT_ACCOUNT_VALUE_SOURCE = UNAVAILABLE_FROM_NINJATRADER_RUNTIME`

Safe Start therefore still fails `fresh_market_data` when NinjaTrader has no current MNQ/NQ prints. That remains correct.

## Phase 54D — Tradovate read-only FundedNext account telemetry

Architecture:

```text
Dashboard → Control API → phase54_ops
  → NinjaTraderReadOnlyAdapter   market data, broker position, reconciliation
  → TradovateReadOnlyAccountAdapter   FundedNext identity, equity/NetLiq, P&L, status
  → Policy Engine
```

Neither adapter submits orders. `TradovateReadOnlyAccountAdapter` allowlists only `/auth/accesstokenrequest`, `/auth/me`, `/account/list`, `/account/find`, `/position/list`, `/cashBalance/list`, `/cashBalance/getcashbalancesnapshot`. There is no `place_order` / `submit_order` / `cancel_order` / `modify_order` / `flatten` / `liquidate` / `close_position` / `drop_oif`.

### Auth

`POST https://live.tradovateapi.com/v1/auth/accesstokenrequest` (or demo if `TRADOVATE_ENV=demo`) using `TRADOVATE_USERNAME`, `TRADOVATE_PASSWORD`, `TRADOVATE_APP_ID`, `TRADOVATE_APP_VERSION`, `TRADOVATE_CID`, `TRADOVATE_SEC` from `.env`. Tokens are kept in memory only. They are never journaled, logged, or returned in Control API snapshots.

### Money hierarchy (FundedNext evaluation)

1. Tradovate `cashBalance/getcashbalancesnapshot.netLiq` → equity
2. unavailable

No Sim101, no $50,000 nominal, no Phase 53 shadow, no NinjaTrader zeros, no stale cache beyond `tradovate.stale_account_sec` (default 60s). `equity_source = TRADOVATE_READ_ONLY` when real.

Normalization: `netLiq` is equity / net liquidation; `totalCashValue` is cash balance; `realizedPnL` / `openPnL` / `totalPnL` are P&L. Zero `netLiq` and zero `totalCashValue` together are treated as unavailable.

### MLL

Tradovate supplies facts. Policy derives MLL from PROP_RULES_V1 Flex 50K: lock at `mll_locks_at` ($50,100) is reconstructable once equity ≥ that lock. Below lock, trailing MLL needs an EOD high-water mark that is not yet available, so MLL stays unavailable and Safe Start fails `equity_mll_available`.

### Identity

The adapter matches `FNFTCHTANATSWAPHILMU92044` by exact name (never first account, never Sim101, never by balance). Unmatched account fails Safe Start `correct_account_id`.

### Reconciliation

Three-way: Tradovate position vs NinjaTrader FundedNext position vs AITRADE expected (flat in Phase 54D). Mismatch → `RECONCILED: NO`, policy DEGRADED, Safe Start fail.

### Safe Start

`fundednext_authenticated` is Tradovate LIVE + matched account, not the Phase 53 confirmation flag. Passing Safe Start still means `ENGINE: RUNNING`, `ORDER EXECUTION: DISABLED`, `PROP_EXECUTION=false`.

Credentials live in `.env` (gitignored) or `state/fundednext_mcp_oauth.json` (gitignored). Copy `.env.example` placeholders; never commit secrets.

## Phase 54E — FundedNext MCP authoritative account/risk telemetry

Custom Tradovate REST telemetry (`AUTH_FAILED` without `TRADOVATE_*` credentials) is **deprecated / fallback-disabled**. Official FundedNext MCP is the money/risk source.

```text
Dashboard → Control API → phase54_ops
  → NinjaTraderReadOnlyAdapter     NQ/MNQ market data, NT FundedNext position, heartbeat
  → FundedNextMCPReadOnlyAdapter   identity, balance/equity, max-loss, remaining DD, P&L, status, rules, futures trades
  → Policy Engine
PROP_EXECUTION=false
ORDER_EXECUTION=DISABLED
```

Integration method: AITRADE calls `https://mcp.fundednext.com` over Streamable HTTP with a Bearer token. This is independent of Cursor chat. Tokens are read from `FUNDEDNEXT_MCP_ACCESS_TOKEN` / `FUNDEDNEXT_MCP_REFRESH_TOKEN` or gitignored `state/fundednext_mcp_oauth.json`. Refresh uses `grant_type=refresh_token` and scope `mcp:read` only. Tokens are never journaled.

Allowlisted tools: `get_accounts_v2`, `get_accounts`, `get_account_overview`, `get_account_applicable_rules`, `get_futures_trade_history`, `resolve_account`.

Denied: `create_free_trial_account`, `register_competition`, `record_ai_feedback`, and any tool not on the allowlist (`PermissionError`). No order place/cancel/modify/flatten tools are exposed.

Account match requires name + platform login + internal id + plan + active. Never first-account.

`runningTrades.total=0` with empty `data` normalizes to known FLAT. Empty closed-history total is not used to infer FLAT if `runningTrades` is missing.

MCP facts feed the existing policy engine. Survival-critical mismatches vs `PROP_RULES_V1` (plan size, profit target, max loss, daily loss, consistency) degrade the policy engine and fail Safe Start. News add-on off on Flex is informational (base product still allows news).

If MCP is live and NinjaTrader market data is stale, Safe Start still fails on `fresh_market_data`. That is correct.

`python tools/fundednext_mcp_login.py` opens the FundedNext authorization page (authorization code + PKCE S256, scope `mcp:read`). Session is stored in gitignored `state/fundednext_mcp_oauth.json`. Never prints tokens. Cursor OAuth is not used.

## Run

Control API and dashboard (same process, bound to 127.0.0.1):

```text
python dashboard/ops-console/api.py
```

Then open `http://127.0.0.1:8765/`.

Do not enable live evaluation trading. Closing the browser does not grant or revoke execution permission.
