"""Phase 31 validation — freeze integrity, hist/live equivalence, NQ/MNQ note."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from nq_dvp_freeze import FROZEN_JSON, FROZEN_STRATEGY_VERSION, load_frozen_document
from nq_dvp_live_runner import (
    PHASE26_FROZEN,
    PHASE26_HASH,
    assert_frozen_immutable,
    historical_live_equivalence,
    nq_mnq_equivalence_note,
)
from phase22_validate import _write_csv

REPORTS = Path("reports")
VALIDATION_JSON = Path("phase31_validation.json")


def run_phase31_init() -> dict[str, Any]:
    REPORTS.mkdir(parents=True, exist_ok=True)
    freeze = assert_frozen_immutable()
    doc = load_frozen_document()
    equiv = historical_live_equivalence(max_days=5)
    mnq = nq_mnq_equivalence_note()

    p26_ok = False
    if PHASE26_FROZEN.exists():
        p26 = json.loads(PHASE26_FROZEN.read_text(encoding="utf-8"))
        p26_ok = p26.get("frozen_config_hash") == PHASE26_HASH

    ready_sim = bool(freeze.get("ok")) and bool(equiv.get("ok"))
    forward_counts = False  # requires live NQ feed, not Databento-only / fake NT feed

    payload = {
        "ok": True,
        "phase": 31,
        "status": "INTEGRATION_READY_DRY_RUN" if ready_sim else "INTEGRATION_BLOCKED",
        "frozen": {
            "path": str(FROZEN_JSON).replace("\\", "/"),
            "version": doc.get("strategy_version"),
            "hash": doc.get("frozen_config_hash"),
            "semantic_mutation": freeze.get("semantic_mutation"),
            "expected_version": FROZEN_STRATEGY_VERSION,
        },
        "execution": {
            "account": "Sim101",
            "signal": "NQ",
            "execution_instrument": "MNQ SEP26",
            "quantity": 1,
            "default_mode": "DRY_RUN",
            "bridge": "OIF_FILL_THEN_OCO_CHILDREN",
            "enable_flag": "--enable-sim-execution",
        },
        "equivalence": equiv,
        "nq_mnq": mnq,
        "phase26_untouched": p26_ok,
        "phase30_untouched": freeze.get("ok"),
        "ready_for_nq_dvp_sim_execution": ready_sim,
        "forward_validation_trades_count": forward_counts,
        "remaining_prerequisite": (
            "Live/real-time NQ completed 5m/15m feed (not NT Simulated Data Feed alone; "
            "not Databento historical-only) before FORWARD_STRATEGY_VALIDATION counts."
        ),
        "generated_at": datetime.now(tz=timezone.utc).isoformat(),
    }

    _write_csv(
        REPORTS / "phase31_freeze.csv",
        [
            {
                "version": payload["frozen"]["version"],
                "hash": payload["frozen"]["hash"],
                "semantic_mutation": payload["frozen"]["semantic_mutation"],
            }
        ],
    )
    _write_csv(
        REPORTS / "phase31_equivalence.csv",
        [
            {
                "verdict": equiv.get("verdict"),
                "compared": equiv.get("compared"),
                "matched": equiv.get("matched"),
                "mismatch_count": equiv.get("mismatch_count"),
            }
        ],
    )
    _write_csv(
        REPORTS / "phase31_nq_mnq.csv",
        [
            {
                "verdict": mnq.get("verdict"),
                "forward_validation_counts": mnq.get("forward_validation_counts"),
                "note": mnq.get("note"),
            }
        ],
    )

    VALIDATION_JSON.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    return payload


if __name__ == "__main__":
    out = run_phase31_init()
    print(
        json.dumps(
            {
                k: out[k]
                for k in (
                    "ok",
                    "status",
                    "frozen",
                    "equivalence",
                    "nq_mnq",
                    "ready_for_nq_dvp_sim_execution",
                    "forward_validation_trades_count",
                    "remaining_prerequisite",
                    "phase26_untouched",
                    "phase30_untouched",
                )
            },
            indent=2,
            default=str,
        )
    )
