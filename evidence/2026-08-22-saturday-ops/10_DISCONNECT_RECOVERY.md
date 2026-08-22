# 10 — Disconnect / recovery

Verdict: **`DISCONNECT_RECOVERY_SAFE`**

Method: Control Center → Connections → **Kinetick – End Of Day (Free)** only. Simulation (Sim101) left connected. NT not closed.

NT log `Documents\NinjaTrader 8\log\log.20260822.00002.txt` (local times):

| Time | Event |
|---|---|
| 16:08:18–19 | Kinetick Connecting → Connected (delayed EOD) |
| 16:09:25–26 | Disconnecting → Disconnected |
| 16:10:30–31 | Connecting → Connected |
| 16:11:19–21 | Disconnecting → Disconnected (restored startup: ConnectOnStartup=false) |

## What this proved

| Observation | Evidence |
|---|---|
| AddOn dump stayed alive while data connection down | B2 polls: dump_age 0.2–0.9s, `alive=true` |
| Market state independent of telemetry | Dump LIVE; quotes DISCONNECTED (or CONNECTED_STALE when Kinetick up) |
| Delayed/stale not promoted to LIVE | B3: status **CONNECTED_STALE**, quality **DELAYED**, Safe Start **FAILED** |
| No `MARKET_DATA_RECOVERED` | notifications journal: event absent |
| Sim101 stayed FLAT_SAFE | B2/B3/B4 |
| FundedNext stayed READ_ONLY | B2/B3/B4 |
| Nothing auto-armed | DISARMED throughout |
| No orders | orders_transmitted=0 |
| No alert spam on disconnect hold | B2 `journal_new=0` over three snapshot polls |

Kinetick left **disconnected** after B4, matching NT startup.

Gate C: `OPTIONAL_TELEMETRY_INJECTION_NOT_RUN` (no safe AddOn-writer pause without closing NT or source hacks).
