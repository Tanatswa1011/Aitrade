# Phase 55D Monday one-command authorization

This command grants permission to run preflight, not permission to trade. It
creates a new cryptographically random authorization ID, binds the request to
the Phase 55D account, contracts and session date, and separates two clocks:

- `claim_valid_until` — operator-supplied claim lease, maximum 45 minutes.
  The runtime must claim the request before this instant. After `CONSUMED` it
  is no longer the runtime expiry.
- `session_valid_until` — frozen no-new-entry deadline for the authorized
  session date. Derived from NQ DVP (`10:30`–`15:30` America/New_York). The
  operator does not type this value. `--valid-until` is a deprecated alias of
  `--claim-valid-until` and never overrides the frozen session end.

For 2026-08-24 the bound session window is:

- session date: `2026-08-24`
- entry start: `2026-08-24T14:30:00Z` (16:30 Europe/Berlin, 10:30 ET)
- session end: `2026-08-24T19:30:00Z` (21:30 Europe/Berlin, 15:30 ET)

A morning authorization at 08:00 Europe/Berlin must be claimed by 08:45. After
successful claim and ordered preflight the runtime waits in
`UNATTENDED_WAITING_SESSION` until 16:30 Europe/Berlin, then
`UNATTENDED_WAITING_DVP` only if every current gate still passes. No entry is
accepted before 10:30 ET. No new entry is accepted at or after 15:30 ET.
Restart invalidates consumed runtime permission and does not clear the one-shot
attempt lock. A serious failure blocks the day; dependency recovery does not
re-arm.

## Tonight standby

1. Keep NinjaTrader strategies and ATM strategies disabled.
2. Leave NinjaTrader and the AITRADE runtime/dashboard running.
3. Leave only the approved account/data connection running.
4. Confirm the snapshot is advancing and account/order fields are authoritative.
5. Confirm `PROP_EXECUTION=false`, execution `DISARMED`, canary blocked, and no
   incoming OIF exists.
6. Do not run the authorization command tonight.

## Monday status (read-only)

```powershell
python phase55d_session_authorization.py status
```

## Monday morning authorization (run once, before leaving)

Claim lease only. Session end is derived from the frozen strategy window.

```powershell
python phase55d_session_authorization.py AUTHORIZE_PHASE55D_ONE_SHOT_CANARY --claim-valid-until (Get-Date).ToUniversalTime().AddMinutes(45).ToString('yyyy-MM-ddTHH:mm:ss+00:00')
```

Expected immediate response is `PENDING_PREFLIGHT` plus the newly generated
authorization ID, `claim_valid_until`, `session_entry_start` and
`session_valid_until`. The running Phase 54 supervisor claims the request.
Confirm the dashboard's `unattended.state`:

- before 10:30 ET: `UNATTENDED_WAITING_SESSION`
- at/after 10:30 ET with all gates current: `UNATTENDED_WAITING_DVP`

`REJECTED`, `UNATTENDED_BLOCKED`, or a named numbered gate means no request may
be sent. Do not retry: investigate and complete a fresh operator review.

The retained restart/manual-revalidation latch is cleared only by the runtime,
after an authenticated request and gates 1–18 pass on current evidence. The
audit record includes the authorization ID and UTC time. Restart invalidates
pending/accepted/consumed authorization; the one-shot attempt lock remains in
the separate unattended state and is never cleared by authorization.
