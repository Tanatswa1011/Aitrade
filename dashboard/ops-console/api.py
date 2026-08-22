"""AITRADE Phase 54 Control API — shadow-connected, PROP_EXECUTION=false."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from typing import Literal

from phase54_ops import (
    EngineSupervisor,
    append_event,
    assert_prop_execution_disabled,
    prop_execution_allowed,
    safe_start_checks,
    snapshot,
)

DIR = Path(__file__).resolve().parent
app = FastAPI(title="AITRADE Control API", version="0.2.0-phase54")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://127.0.0.1:8765", "http://localhost:8765", "null"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class ModeBody(BaseModel):
    mode: Literal["DRY_RUN", "EVALUATION", "FUNDED", "DRY RUN"]


class PauseBody(BaseModel):
    paused: bool


class KillBody(BaseModel):
    confirm: str


@app.get("/health")
def health():
    snap = snapshot()
    md = snap.get("market_data")
    freshness = md.get("freshness") if isinstance(md, dict) else md
    return {
        "ok": True,
        "engine": snap["engine"],
        "market_data": freshness,
        "market_data_status": snap.get("market_data_status") or freshness,
        "fundednext_connection": snap["fundednext_connection"],
        "fundednext_permission": snap["fundednext_permission"],
        "policy_engine": snap["policy_engine"],
        "order_execution": "DISABLED",
        "PROP_EXECUTION": False,
        "mode": snap["mode"],
        "entries_paused": snap["entries_paused"],
        "policy_risk_state": snap["risk"]["lane"],
        "heartbeat_ts": snap["heartbeat_ts"],
        "prop_execution_allowed": prop_execution_allowed(),
    }


@app.get("/api/snapshot")
def api_snapshot():
    return snapshot()


@app.get("/safe-start/checks")
def checks():
    c = safe_start_checks()
    return {
        "ok": c["ok_to_run_engine"],
        "checks": c["checks"],
        "display": c["display"],
        "safe_start_result": c["safe_start_result"],
        "order_execution": "DISABLED",
        "PROP_EXECUTION": False,
        "market_data_status": c.get("market_data_status"),
        "execution_permission_value": "DISABLED",
    }


@app.post("/control/start")
def start():
    try:
        assert_prop_execution_disabled()
    except PermissionError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    out = EngineSupervisor.start()
    if not out.get("ok"):
        raise HTTPException(status_code=409, detail={"message": "Safe Start failed", "checks": out.get("checks")})
    return out


@app.post("/control/stop")
def stop():
    return EngineSupervisor.stop_gracefully()


@app.post("/control/pause-entries")
def pause_entries(body: PauseBody):
    return EngineSupervisor.pause_entries(body.paused)


@app.post("/control/mode")
def set_mode(body: ModeBody):
    return EngineSupervisor.set_mode(body.mode)


@app.post("/control/emergency-kill")
def emergency_kill(body: KillBody):
    if body.confirm != "CONFIRM":
        raise HTTPException(status_code=400, detail="Typed confirmation required")
    return EngineSupervisor.emergency_flatten_stop()


@app.post("/control/prop-canary/dry-run")
def prop_canary_dry_run():
    """Build FundedNext canary payload. Never transmits."""
    from prop_canary import context_from_ops_snapshot, dry_run

    snap = snapshot()
    return dry_run(context_from_ops_snapshot(snap))


@app.post("/control/prop-canary/arm")
def prop_canary_arm():
    """Operator one-shot arm. Fail-closed. Does not place a trade."""
    from prop_canary import arm, context_from_ops_snapshot

    snap = snapshot()
    out = arm(context_from_ops_snapshot(snap))
    if not out.get("ok"):
        raise HTTPException(status_code=409, detail=out)
    append_event("WARN", "PROP_CANARY ARMED by operator", state=out.get("state"), PROP_EXECUTION=False)
    return out


@app.post("/control/prop-canary/disarm")
def prop_canary_disarm():
    from prop_canary import disarm

    out = disarm("OPERATOR")
    append_event("INFO", "PROP_CANARY DISARMED by operator", PROP_EXECUTION=False)
    return out


@app.post("/control/unattended/enable")
def unattended_enable():
    """Operator day-enable. Fail-closed. Does not place a trade."""
    from unattended_prop_canary import context_from_ops_snapshot, enable, unattended_flag_enabled

    if not unattended_flag_enabled():
        raise HTTPException(status_code=409, detail={"ok": False, "errors": ["UNATTENDED_FLAG_DISABLED"], "PROP_EXECUTION": False})
    snap = snapshot()
    out = enable(context_from_ops_snapshot(snap))
    if not out.get("ok"):
        raise HTTPException(status_code=409, detail=out)
    append_event("WARN", "UNATTENDED_PROP_CANARY enabled by operator", state=out.get("state"), PROP_EXECUTION=False)
    return out


@app.post("/control/unattended/disable")
def unattended_disable():
    from unattended_prop_canary import disable

    out = disable("OPERATOR")
    append_event("INFO", "UNATTENDED_PROP_CANARY disabled by operator", PROP_EXECUTION=False)
    return out


@app.post("/control/unattended/dry-run")
def unattended_dry_run_api():
    from unattended_prop_canary import context_from_ops_snapshot, unattended_dry_run

    snap = snapshot()
    return unattended_dry_run(context_from_ops_snapshot(snap))


@app.get("/control/unattended/status")
def unattended_status():
    from unattended_prop_canary import context_from_ops_snapshot, public_snapshot as u_pub

    snap = snapshot()
    return u_pub(context_from_ops_snapshot(snap))


@app.get("/")
def index():
    return FileResponse(DIR / "index.html")


app.mount("/", StaticFiles(directory=str(DIR), html=True), name="static")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8765, reload=False)
