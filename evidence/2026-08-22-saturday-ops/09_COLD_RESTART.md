# 09 — Cold restart / recovery

Verdict: **`COLD_RESTART_SAFE`**  
Journal: `journal/phase54_ops/saturday_recovery_gates.jsonl` labels A1–A7. Index: `raw/gate_labels.json`.

## PIDs

| Stage | NT PID | Start | Notes |
|---|---|---|---|
| F5 compile / first new-schema verify | 19580 | 2026-08-22 12:57:30 | ADDON_RELOAD_CONFIRMED |
| A1 baseline | **6020** | 2026-08-22 13:55:09 | dump alive |
| A3 closed | (none) | 15:38 local exit | dump froze at 2026-08-22T13:37:55Z |
| A4 relaunch | **15376** | 2026-08-22 15:38:54 | Welcome then Control Center; **no F5** |
| Pack capture | 15376 | still running | MainWindow Control Center - Accounts |

Desk API:

| Stage | Process | Notifications |
|---|---|---|
| A1–A4 | uvicorn 26436 (started ~12:51Z) | NOT_CONFIGURED (started before `.env` URL) |
| A5+ | uvicorn **21252** / `http://127.0.0.1:8765/` | HEALTHY / TELEGRAM |

## Assertions that held after restore

- AddOn auto-loaded without F5; DLL mtime `1787400202.0502925` unchanged
- Telemetry restored; dump age < 1s; timestamps advancing
- Sim101 restored; MNQ FLAT qty 0
- recovery `FLAT_SAFE`
- FundedNext CONNECTED · READ_ONLY
- PROP_EXECUTION=false
- SIM_ONLY DISARMED; env unset
- Safe Start re-evaluated → SAFE_START_FAILED (stale market; not loosened)
- Engine STOPPED (not auto-started)
- Zero orders / empty OIF
- No `MARKET_DATA_RECOVERED`

Telegram after desk restart: `NINJATRADER_RECONNECTED` event `ba30c18c530444e5` delivered. Repeat snapshots: no spam.
