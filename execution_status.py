"""Project-wide execution status — Phase 32 pause gate."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

PAUSE_PATH = Path("state") / "project_pause.json"

GC_FROZEN_HASH = "0695d7a881b6247033426bceb077b6895b96405b826c21f32a109a02f44b2d43"
NQ_FROZEN_HASH = "935e3a616351b09dbfa8d2d0e2b5d6850be803fb385ec5e5f8e3593856a1212a"

ALLOWED_MODES = frozenset({"DRY_RUN", "SIM_ONLY"})
BLOCKED_MODES = frozenset({"PROP_EVALUATION", "FUNDED", "LIVE_PERSONAL"})

# Phase 55A: explicit opt-in for Sim101 ATI submit. Never enables PROP_EXECUTION.
SIM_ONLY_ENV = "AITRADE_SIM_ONLY_EXECUTION"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def default_pause_document(*, reason: str = "Phase 32 deployment-prep pause") -> dict[str, Any]:
    return {
        "paused": True,
        "execution_status": "PAUSED",
        "reason": reason,
        "timestamp": utc_now_iso(),
        "phase": 32,
        "gc_frozen_hash": GC_FROZEN_HASH,
        "nq_frozen_hash": NQ_FROZEN_HASH,
        "allowed_modes": sorted(ALLOWED_MODES),
        "blocked_modes": sorted(BLOCKED_MODES),
        "default_runner_mode": "DRY_RUN",
        "resume_gate": [
            "prop firm chosen",
            "exact prop rules stored",
            "automation explicitly permitted",
            "NinjaTrader connection confirmed",
            "real-time feed available",
            "NQ feed equivalence passes",
            "GC feed equivalence passes",
            "strategy hashes pass",
            "DRY_RUN passes",
            "Sim101 execution passes",
            "risk limits configured",
            "explicit approval to enable prop-evaluation execution",
        ],
    }


def load_pause(path: Path = PAUSE_PATH) -> dict[str, Any]:
    if not path.exists():
        return {"paused": False, "execution_status": "UNKNOWN"}
    return json.loads(path.read_text(encoding="utf-8"))


def write_pause(doc: dict[str, Any], path: Path = PAUSE_PATH) -> dict[str, Any]:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, indent=2), encoding="utf-8")
    return doc


def ensure_project_paused(*, reason: str = "Phase 32 deployment-prep pause") -> dict[str, Any]:
    path = PAUSE_PATH
    if path.exists():
        doc = load_pause(path)
        doc.setdefault("paused", True)
        doc.setdefault("execution_status", "PAUSED")
        doc.setdefault("gc_frozen_hash", GC_FROZEN_HASH)
        doc.setdefault("nq_frozen_hash", NQ_FROZEN_HASH)
        return write_pause(doc, path)
    return write_pause(default_pause_document(reason=reason), path)


def is_execution_paused(path: Path = PAUSE_PATH) -> bool:
    doc = load_pause(path)
    return bool(doc.get("paused")) or doc.get("execution_status") == "PAUSED"


def sim_only_execution_armed() -> bool:
    """True only when the operator explicitly opts into SIM_ONLY Sim101 ATI.

    Does not lift PROP_EVALUATION / FUNDED / LIVE_PERSONAL. Does not set PROP_EXECUTION.
    """
    return os.environ.get(SIM_ONLY_ENV, "").strip().lower() in ("1", "true", "yes", "on")


def assert_sim_only_submit_allowed(*, prop_execution: bool = False) -> None:
    """Permit actual Sim101 OIF only when SIM_ONLY is armed. Prop remains blocked."""
    if prop_execution:
        raise PermissionError("PROP_EXECUTION_FORBIDDEN_PHASE55A")
    if not sim_only_execution_armed():
        raise PermissionError("SIM_ONLY_NOT_ARMED")


def assert_execution_allowed(*, requested_mode: str, sim_enable: bool = False) -> None:
    """Fail closed when project is paused or mode is blocked.

    Phase 55A: SIM_ONLY + sim_enable is allowed while the project remains paused for
    PROP_EVALUATION, but only when ``AITRADE_SIM_ONLY_EXECUTION`` is set.
    """
    mode = (requested_mode or "").upper()
    if mode in BLOCKED_MODES:
        raise PermissionError(f"EXECUTION_MODE_BLOCKED:{mode}")
    if mode == "SIM_ONLY" and sim_enable:
        assert_sim_only_submit_allowed()
        return
    if is_execution_paused():
        if sim_enable or mode not in ALLOWED_MODES:
            raise PermissionError("PROJECT_PAUSED")


def execution_summary() -> dict[str, Any]:
    doc = load_pause()
    return {
        "execution_status": doc.get("execution_status", "PAUSED" if doc.get("paused") else "UNKNOWN"),
        "paused": is_execution_paused(),
        "allowed_modes": sorted(ALLOWED_MODES),
        "blocked_modes": sorted(BLOCKED_MODES),
        "pause_path": str(PAUSE_PATH),
        "pause": doc,
    }
