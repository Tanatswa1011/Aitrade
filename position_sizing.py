"""Deterministic micro-contract position sizing — no strategy mutation."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

METADATA_PATH = Path("config") / "execution_metadata.json"


@dataclass(frozen=True)
class SizingInput:
    strategy: str
    signal_instrument: str
    execution_instrument: str
    entry: float
    stop: float
    max_dollar_risk: float
    contract_point_value: float
    contract_tick_size: float


@dataclass(frozen=True)
class SizingResult:
    permitted_quantity: int
    actual_dollar_risk: float
    rejected: bool
    reason: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "permitted_quantity": self.permitted_quantity,
            "actual_dollar_risk": self.actual_dollar_risk,
            "rejected": self.rejected,
            "reason": self.reason,
        }


def load_execution_metadata(path: Path = METADATA_PATH) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def stop_distance_points(entry: float, stop: float) -> float:
    return abs(float(entry) - float(stop))


def dollar_risk_per_contract(
    *,
    entry: float,
    stop: float,
    point_value: float,
) -> float:
    return stop_distance_points(entry, stop) * float(point_value)


def size_position(inp: SizingInput) -> SizingResult:
    """Return max whole contracts that fit within max_dollar_risk."""
    if inp.max_dollar_risk <= 0:
        return SizingResult(0, 0.0, True, "max_dollar_risk_non_positive")
    per_contract = dollar_risk_per_contract(
        entry=inp.entry,
        stop=inp.stop,
        point_value=inp.contract_point_value,
    )
    if per_contract <= 0:
        return SizingResult(0, 0.0, True, "zero_stop_distance")
    if per_contract > inp.max_dollar_risk + 1e-9:
        return SizingResult(0, per_contract, True, "minimum_contract_exceeds_max_risk")
    qty = int(math.floor(inp.max_dollar_risk / per_contract))
    if qty < 1:
        return SizingResult(0, per_contract, True, "minimum_contract_exceeds_max_risk")
    actual = qty * per_contract
    return SizingResult(qty, actual, False, None)


def size_from_metadata(
    *,
    execution_root: str,
    entry: float,
    stop: float,
    max_dollar_risk: float,
    strategy: str = "",
    signal_instrument: str = "",
) -> SizingResult:
    meta = load_execution_metadata()
    key = execution_root.upper()
    spec = meta.get(key) or {}
    point_value = float(spec.get("point_value_usd") or spec.get("point_value") or 0)
    tick_size = float(spec.get("tick_size_points") or spec.get("tick_size") or 0.25)
    return size_position(
        SizingInput(
            strategy=strategy,
            signal_instrument=signal_instrument,
            execution_instrument=key,
            entry=entry,
            stop=stop,
            max_dollar_risk=max_dollar_risk,
            contract_point_value=point_value,
            contract_tick_size=tick_size,
        )
    )


def mnq_dvp_frozen_risk(quantity: int = 1) -> dict[str, float]:
    """Frozen DVP 80-point stop on MNQ — execution metadata, not strategy change."""
    meta = load_execution_metadata()["MNQ"]
    pt = float(meta["point_value_usd"])
    stop = float(meta["dvp_frozen_brackets_points"]["long"]["stop"])
    long_tp = float(meta["dvp_frozen_brackets_points"]["long"]["target"])
    short_tp = float(meta["dvp_frozen_brackets_points"]["short"]["target"])
    q = max(1, int(quantity))
    return {
        "stop_points": stop,
        "stop_dollar_risk": stop * pt * q,
        "long_target_dollars": long_tp * pt * q,
        "short_target_dollars": short_tp * pt * q,
    }
