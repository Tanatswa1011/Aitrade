"""Deterministic GC contract stitching by daily volume crossover (no future leakage)."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional, Sequence
from zoneinfo import ZoneInfo

from models import Bar

NY = ZoneInfo("America/New_York")


@dataclass(frozen=True)
class ContractSeries:
    contract_symbol: str
    bars: tuple[Bar, ...]
    instrument_id: Optional[str] = None
    expiration: Optional[str] = None
    first_seen: Optional[int] = None
    last_seen: Optional[int] = None
    exchange: str = "GLBX"
    root: str = "GC"
    extras: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["bars"] = [
            {
                "time": int(b.time),
                "open": float(b.open),
                "high": float(b.high),
                "low": float(b.low),
                "close": float(b.close),
                "volume": b.volume,
            }
            for b in self.bars
        ]
        return d


@dataclass(frozen=True)
class RollEvent:
    old_contract: str
    new_contract: str
    decision_date: str
    roll_timestamp: int
    old_volume: float
    new_volume: float
    roll_reason: str = "next_daily_volume_exceeds_current"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _ny_date(ts: int) -> str:
    return datetime.fromtimestamp(int(ts), tz=NY).date().isoformat()


def daily_volume_by_date(bars: Sequence[Bar]) -> dict[str, float]:
    out: dict[str, float] = {}
    for b in bars:
        d = _ny_date(int(b.time))
        out[d] = out.get(d, 0.0) + float(b.volume or 0.0)
    return out


def session_boundary_ts(trading_date: str) -> int:
    """Activate new contract from 18:00 NY on decision date (CME daily break end)."""
    d = date.fromisoformat(trading_date)
    # Next Globex open after decision day close: 18:00 NY same calendar day
    local = datetime(d.year, d.month, d.day, 18, 0, tzinfo=NY)
    return int(local.timestamp())


def decide_rolls(
    series: Sequence[ContractSeries],
    *,
    calendar_order: Optional[Sequence[str]] = None,
) -> list[RollEvent]:
    """
    Predeclared roll rule (no strategy feedback):
    Roll from current → next when next contract's NY-session daily volume
    exceeds current contract's daily volume for that same trading date.
    Activation at 18:00 America/New_York on the decision date (no intraday switch).
    """
    if len(series) < 2:
        return []
    by_sym = {s.contract_symbol: s for s in series}
    order = list(calendar_order) if calendar_order else sorted(by_sym.keys())
    order = [s for s in order if s in by_sym]
    daily = {s: daily_volume_by_date(by_sym[s].bars) for s in order}

    rolls: list[RollEvent] = []
    active_i = 0
    # Union of dates across remaining contracts
    all_dates = sorted({d for vols in daily.values() for d in vols})
    for d in all_dates:
        if active_i >= len(order) - 1:
            break
        cur = order[active_i]
        nxt = order[active_i + 1]
        cv = daily[cur].get(d)
        nv = daily[nxt].get(d)
        if cv is None or nv is None:
            continue
        if nv > cv:
            rolls.append(
                RollEvent(
                    old_contract=cur,
                    new_contract=nxt,
                    decision_date=d,
                    roll_timestamp=session_boundary_ts(d),
                    old_volume=float(cv),
                    new_volume=float(nv),
                )
            )
            active_i += 1
    return rolls


def stitch_contracts(
    series: Sequence[ContractSeries],
    rolls: Sequence[RollEvent],
) -> tuple[list[Bar], list[dict[str, Any]]]:
    """
    Build unadjusted stitched 5m series.
    Each calendar segment uses exactly one active contract (no intraday roll).
    """
    if not series:
        return [], []
    by_sym = {s.contract_symbol: s for s in series}
    order = sorted(by_sym.keys()) if not rolls else None

    # Build timeline of active contract
    # Start with earliest first_seen contract
    first = min(series, key=lambda s: int(s.first_seen or (s.bars[0].time if s.bars else 0)))
    segments: list[tuple[int, Optional[int], str]] = []  # start_ts, end_ts, contract
    active = first.contract_symbol
    start = int(first.first_seen or (first.bars[0].time if first.bars else 0))
    for roll in sorted(rolls, key=lambda r: int(r.roll_timestamp)):
        segments.append((start, int(roll.roll_timestamp), active))
        active = roll.new_contract
        start = int(roll.roll_timestamp)
    # final
    last_ts = max(
        int(s.last_seen or (s.bars[-1].time if s.bars else 0)) for s in series
    )
    segments.append((start, last_ts + 1, active))

    stitched: list[Bar] = []
    provenance: list[dict[str, Any]] = []
    seen_ts: set[int] = set()
    for seg_start, seg_end, contract in segments:
        bars = by_sym.get(contract)
        if bars is None:
            continue
        for b in bars.bars:
            t = int(b.time)
            if t < seg_start or (seg_end is not None and t >= seg_end):
                continue
            if t in seen_ts:
                continue
            seen_ts.add(t)
            stitched.append(b)
            provenance.append(
                {
                    "time": t,
                    "contract": contract,
                    "seg_start": seg_start,
                    "seg_end": seg_end,
                }
            )
    stitched.sort(key=lambda b: int(b.time))
    return stitched, provenance


def detect_roll_price_artifacts(
    stitched: Sequence[Bar],
    rolls: Sequence[RollEvent],
    *,
    window_sec: int = 86400,
    min_jump: float = 8.0,
) -> list[dict[str, Any]]:
    """Flag large discontinuities near roll timestamps (descriptive)."""
    if not stitched:
        return []
    ordered = sorted(stitched, key=lambda b: int(b.time))
    flags = []
    roll_ts = {int(r.roll_timestamp) for r in rolls}
    for a, b in zip(ordered, ordered[1:]):
        jump = abs(float(b.open) - float(a.close))
        near = any(abs(int(b.time) - rt) <= window_sec for rt in roll_ts)
        if near and jump >= min_jump:
            flags.append(
                {
                    "timestamp": int(b.time),
                    "jump": jump,
                    "prev_close": float(a.close),
                    "open": float(b.open),
                    "near_roll": True,
                }
            )
    return flags


def persist_stitched(
    bars: Sequence[Bar],
    *,
    rolls: Sequence[RollEvent],
    root: Path,
    meta_extras: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    from bar_dataset import write_dataset

    stitched_dir = Path(root) / "stitched"
    stitched_dir.mkdir(parents=True, exist_ok=True)
    result = write_dataset(
        list(bars),
        symbol="databento_GC_stitched",
        timeframe="5m",
        source="databento:GLBX.MDP3:stitched_volume_crossover",
        root=stitched_dir,
        expected_period_sec=300,
    )
    rolls_path = stitched_dir / "rolls.jsonl"
    with rolls_path.open("w", encoding="utf-8") as fh:
        for r in rolls:
            fh.write(json.dumps(r.to_dict()) + "\n")
    meta_path = Path(result["path"]).with_suffix(".meta.json")
    if meta_path.exists():
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        meta.update(
            {
                "provider": "databento",
                "dataset": "GLBX.MDP3",
                "root": "GC",
                "continuous": True,
                "continuous_method": "aitrade_volume_crossover_unadjusted",
                "back_adjusted": False,
                "rolls_path": str(rolls_path),
                "roll_count": len(rolls),
                **(meta_extras or {}),
            }
        )
        meta_path.write_text(json.dumps(meta, indent=2, default=str), encoding="utf-8")
    return {"bars_path": result["path"], "rolls_path": str(rolls_path), "meta_path": str(meta_path)}
