# Phase 55D Monday one-command authorization

This is a permission-to-preflight command, not permission to trade. It creates a
new cryptographically random authorization ID at invocation time, binds the
request to the Phase 55D account/contracts/session and expires it after 30
minutes. The runtime claims it once, runs the 19 ordered gates, and enters
`UNATTENDED_WAITING_DVP` only when every gate passes.

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

## Monday authorization (run once)

```powershell
python phase55d_session_authorization.py AUTHORIZE_PHASE55D_ONE_SHOT_CANARY --valid-until (Get-Date).ToUniversalTime().AddMinutes(30).ToString('o')
```

Expected immediate response is `PENDING_PREFLIGHT` plus the newly generated
authorization ID. The running Phase 54 supervisor claims the request. Confirm
the dashboard's `unattended.state`; success is `UNATTENDED_WAITING_DVP`.
`REJECTED`, `UNATTENDED_BLOCKED`, or a named numbered gate means no request may
be sent. Do not retry: investigate and complete a fresh operator review.

The retained restart/manual-revalidation latch is cleared only by the runtime,
after an authenticated request and gates 1–18 pass on current evidence. The
audit record includes the authorization ID and UTC time. Restart invalidates
pending/accepted/consumed authorization; the one-shot attempt lock remains in
the separate unattended state and is never cleared by authorization.
