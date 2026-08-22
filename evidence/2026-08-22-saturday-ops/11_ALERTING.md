# 11 — Alerting

Verdict: **`ALERTING_PARTIAL`**

Do not upgrade to full PASS. Many trade-lifecycle events are unit-tested only.

## Architecture

- Apprise **1.13.0** (`import apprise`; requirement `apprise>=1.9.0`)
- Outbound-only: `aitrade_notifications.py` module docstring; tests forbid `getUpdates` / `telegram.ext` / `Updater(`
- No inbound Telegram execution control
- Notifier exceptions isolated (`test_failed_notifier_does_not_crash`, `test_notification_exception_leaves_flags_unchanged`)
- Secrets masked (`test_secrets_are_masked`); this pack contains **no** bot token, Apprise URL, or chat id
- Dedup / family state in `state/aitrade_notifications.json`; stale events do not spam; recovery events boot-suppressed on first observe

## Unit tests

`python -m unittest tests_notifications.py` — **28 tests, all ok** (included in `07_TEST_RESULTS.txt`).

## Live Telegram delivery (journal `journal/phase54_ops/notifications.jsonl`, copy `raw/notifications_events.json`)

| Event | Event id | Delivered | Class |
|---|---|---|---|
| TEST (wrong chat, failed) | 6a067c084eaf4864, 3ed760469383472b | no | setup |
| **AITRADE TEST** | **997770edb53740a7** | **yes** | LIVE_PROVEN |
| planned ENGINE_STOP | 0043ab541ef94192 | yes | LIVE_PROVEN |
| NINJATRADER_DISCONNECTED | c76e0f46038148bb | yes | LIVE_PROVEN |
| MARKET_DATA_STALE | 5e799c98f48e4dcb | yes | LIVE_PROVEN |
| SAFE_START_FAILED | 7ac651fcf3c046c1 | yes | LIVE_PROVEN |
| NINJATRADER_RECONNECTED | ba30c18c530444e5 | yes | LIVE_PROVEN |
| RECOVERY_UNSAFE | 5637b4b60a314ef6 | yes | LIVE_PROVEN (transient; see note) |
| RECOVERY_FLAT_SAFE | 190706aa18df44c0 | yes | LIVE_PROVEN |

No `MARKET_DATA_RECOVERED` in the journal (including delayed Kinetick reconnect).

Disconnect polling (Gate B hold): **zero** new journal rows.

`TELEMETRY_RECOVERED`: unit-proven; Saturday reconnect boot-suppressed (telemetry family was never STALE).

## Note

14:19Z `RECOVERY_UNSAFE` then `RECOVERY_FLAT_SAFE` occurred after Gate B close, before pack capture. Named gates and capture still show FLAT_SAFE. Treat as a one-cycle unknown→flat transition, not a failed safety gate.

## Remaining unit-only (examples)

Engine start/failure/unexpected exit (except planned stop), telemetry stale/recovered, market recovered, order submitted/accepted/rejected, stop/target/close, LIVE_DVP_DETECTED, SIM_ONLY_ARMED.
