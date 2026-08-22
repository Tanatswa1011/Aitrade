"""Universal AITRADE operating-policy placeholders. Numerics unset pending simulation."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
POLICY_PATH = ROOT / "config" / "aitrade_operating_policy_v1.json"


@dataclass(frozen=True)
class OperatingPolicy:
    flags: dict[str, bool]
    numerics: dict[str, Any]
    execution_default: str
    broker_execution: bool

    @property
    def consistency_governor_blocks(self) -> bool:
        return bool(self.numerics.get("consistency_governor_block"))

    @property
    def consistency_governor_mode(self) -> str:
        return str(self.numerics.get("consistency_governor_mode") or "ADVISORY")


def load_operating_policy(path: Path = POLICY_PATH) -> OperatingPolicy:
    doc = json.loads(path.read_text(encoding="utf-8"))
    return OperatingPolicy(
        flags=dict(doc.get("flags") or {}),
        numerics=dict(doc.get("numerics_pending_simulation") or {}),
        execution_default=str(doc.get("execution_default") or "DRY_RUN"),
        broker_execution=bool(doc.get("broker_execution")),
    )
