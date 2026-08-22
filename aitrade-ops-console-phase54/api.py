"""
AITRADE Phase 54 Control API skeleton.
Phase 54 explicitly separates connectivity from execution permission.
"""
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from typing import Literal
import time

app = FastAPI(title="AITRADE Control API", version="0.2.0-phase54")

STATE = {
    "engine": "RUNNING",
    "market_data": "CONNECTED",
    "fundednext_connection": "CONNECTED",
    "fundednext_permission": "READ_ONLY",
    "policy_engine": "ACTIVE",
    "order_execution": "DISABLED",
    "entries_paused": False,
    "mode": "DRY_RUN",
    "policy_risk_state": "SAFE",
    "heartbeat_ts": time.time(),
}

class ModeBody(BaseModel):
    mode: Literal["DRY_RUN", "EVALUATION", "FUNDED"]

class PauseBody(BaseModel):
    paused: bool

class KillBody(BaseModel):
    confirm: str

def safe_start_checks():
    return {
        "fresh_market_data": True,
        "fundednext_authenticated": True,
        "correct_account_id": True,
        "equity_mll_available": True,
        "broker_positions_reconciled": True,
        "prop_rules_loaded": True,
        "frozen_nq_hash_verified": True,
        "no_stale_orders": True,
        "risk_limits_valid": True,
        "news_gate_valid": True,
        "execution_permission_checked": True,
    }

@app.get("/health")
def health():
    return {"ok": True, **STATE}

@app.get("/safe-start/checks")
def checks():
    c = safe_start_checks()
    return {
        "ok": all(c.values()),
        "checks": c,
        "safe_start_result": "ENGINE_MAY_RUN",
        "order_execution": "DISABLED",
    }

@app.post("/control/start")
def start():
    c = safe_start_checks()
    if not all(c.values()):
        raise HTTPException(status_code=409, detail={"message":"Safe Start failed","checks":c})
    STATE["engine"] = "RUNNING"
    STATE["entries_paused"] = False
    # Critical: Phase 54 never enables prop execution here.
    STATE["order_execution"] = "DISABLED"
    STATE["fundednext_permission"] = "READ_ONLY"
    return {
        "ok": True,
        "engine": "RUNNING",
        "order_execution": "DISABLED",
        "fundednext_permission": "READ_ONLY",
    }

@app.post("/control/stop")
def stop():
    STATE["engine"] = "STOPPED"
    STATE["entries_paused"] = True
    STATE["order_execution"] = "DISABLED"
    return {"ok": True}

@app.post("/control/pause-entries")
def pause_entries(body: PauseBody):
    STATE["entries_paused"] = body.paused
    return {"ok": True, "entries_paused": body.paused}

@app.post("/control/mode")
def set_mode(body: ModeBody):
    # Mode selection is descriptive/configuration state during Phase 54.
    # It does NOT imply execution permission.
    STATE["mode"] = body.mode
    STATE["order_execution"] = "DISABLED"
    return {"ok": True, "mode": body.mode, "order_execution": "DISABLED"}

@app.post("/control/emergency-kill")
def emergency_kill(body: KillBody):
    if body.confirm != "CONFIRM":
        raise HTTPException(status_code=400, detail="Typed confirmation required")
    STATE["entries_paused"] = True
    STATE["engine"] = "STOPPED"
    STATE["order_execution"] = "DISABLED"
    return {"ok": True, "engine": "STOPPED", "order_execution": "DISABLED"}
