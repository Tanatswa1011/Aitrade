# Tests and files

## Tests

`tests_unattended_prop_canary.py` — 49 cases covering the requested failure injections:

missing flag, wrong account, stale MCP, unknown MLL, not flat, working order, wrong instrument, qty≠1, delayed/stale market, no/duplicate/non-advancing bars, 5m/15m broken, warmup, shadow/historical/stale/pre-readiness/outside-session DVP, second DVP after latch, reject, OIF exception, partial fill, stop/target failure/timeout, engine crash flat/open, dashboard crash open, Telegram failure, NT disconnect flat/open, MCP stale waiting/open, restart before/after entry, reconnect no re-arm, session close no DVP, latch persists, PROP_EXECUTION never enabled, Sim101 ineligible, emergency cannot hit another account.

Combined with existing suites: **175 OK**.

## Files

- `unattended_prop_canary.py` — mode, preflight, automated 55B, session, JIT, latch, dry-run
- `unattended_watchdog.py` — independent monitor
- `tests_unattended_prop_canary.py`
- `aitrade_notifications.py` — UNATTENDED_* events
- `phase54_ops.py` — snapshot + engine tick (`allow_entry` only in the engine loop)
- `dashboard/ops-console/{api.py,index.html,app.js,styles.css}`
- `.env.example`, `.gitignore`

Frozen NQ DVP, strategy window, VWAP, drift/pullback, and hash were not modified.
