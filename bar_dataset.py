"""Local OHLC dataset persistence + integrity checks (no gap filling)."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, Sequence

from models import Bar
from timeframe import normalize_timeframe

DATA_DIR = Path("data")


@dataclass(frozen=True)
class DatasetMeta:
    symbol: str
    timeframe: str
    source: str
    capture_time: str
    earliest_bar: Optional[int]
    latest_bar: Optional[int]
    bar_count: int
    extras: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def dataset_path(symbol: str, timeframe: str, *, root: Path = DATA_DIR) -> Path:
    sym = symbol.replace(":", "_").replace("/", "_")
    tf = normalize_timeframe(timeframe) or timeframe
    root.mkdir(parents=True, exist_ok=True)
    return root / f"{sym}_{tf}.jsonl"


def meta_path(symbol: str, timeframe: str, *, root: Path = DATA_DIR) -> Path:
    p = dataset_path(symbol, timeframe, root=root)
    return p.with_suffix(".meta.json")


def bar_to_row(bar: Bar) -> dict[str, Any]:
    return {
        "time": int(bar.time),
        "open": float(bar.open),
        "high": float(bar.high),
        "low": float(bar.low),
        "close": float(bar.close),
        "volume": bar.volume,
    }


def row_to_bar(row: dict[str, Any]) -> Bar:
    return Bar(
        time=int(row["time"]),
        open=float(row["open"]),
        high=float(row["high"]),
        low=float(row["low"]),
        close=float(row["close"]),
        volume=row.get("volume"),
    )


def validate_bars(
    bars: Sequence[Bar],
    *,
    symbol: str,
    timeframe: str,
    expected_period_sec: Optional[int] = None,
) -> dict[str, Any]:
    """Integrity report — does not invent/fill gaps."""
    ordered = sorted(bars, key=lambda b: int(b.time))
    dupes = []
    ohlc_bad = []
    seen: set[int] = set()
    gaps = []
    for i, b in enumerate(ordered):
        t = int(b.time)
        if t in seen:
            dupes.append(t)
        seen.add(t)
        if not (b.low <= b.open <= b.high and b.low <= b.close <= b.high):
            ohlc_bad.append(t)
        if b.high < b.low:
            ohlc_bad.append(t)
        if i > 0 and expected_period_sec:
            dt = t - int(ordered[i - 1].time)
            if dt != expected_period_sec and dt > 0:
                # Record missing intervals; do not fill.
                if dt > expected_period_sec:
                    gaps.append(
                        {
                            "from": int(ordered[i - 1].time),
                            "to": t,
                            "delta_sec": dt,
                            "expected_period_sec": expected_period_sec,
                        }
                    )
    return {
        "ok": not dupes and not ohlc_bad,
        "symbol": symbol,
        "timeframe": normalize_timeframe(timeframe) or timeframe,
        "bar_count": len(ordered),
        "sorted": list(ordered) == list(bars) or all(
            int(ordered[i].time) <= int(ordered[i + 1].time)
            for i in range(max(0, len(ordered) - 1))
        ),
        "duplicate_timestamps": dupes[:50],
        "duplicate_count": len(dupes),
        "ohlc_invalid_count": len(ohlc_bad),
        "ohlc_invalid_timestamps": ohlc_bad[:50],
        "gap_count": len(gaps),
        "gaps_head": gaps[:20],
        "earliest": None if not ordered else int(ordered[0].time),
        "latest": None if not ordered else int(ordered[-1].time),
        "utc_normalized": True,
    }


def dedupe_bars(bars: Sequence[Bar]) -> list[Bar]:
    """Keep last occurrence per timestamp; preserve chronological order."""
    by_t: dict[int, Bar] = {}
    for b in bars:
        by_t[int(b.time)] = b
    return [by_t[t] for t in sorted(by_t)]


def write_dataset(
    bars: Sequence[Bar],
    *,
    symbol: str,
    timeframe: str,
    source: str = "tradingview_native",
    root: Path = DATA_DIR,
    expected_period_sec: Optional[int] = None,
) -> dict[str, Any]:
    clean = dedupe_bars(bars)
    integrity = validate_bars(
        clean, symbol=symbol, timeframe=timeframe, expected_period_sec=expected_period_sec
    )
    path = dataset_path(symbol, timeframe, root=root)
    with path.open("w", encoding="utf-8") as fh:
        for b in clean:
            fh.write(json.dumps(bar_to_row(b)) + "\n")
    meta = DatasetMeta(
        symbol=symbol,
        timeframe=normalize_timeframe(timeframe) or timeframe,
        source=source,
        capture_time=datetime.now(tz=timezone.utc).isoformat(),
        earliest_bar=integrity.get("earliest"),
        latest_bar=integrity.get("latest"),
        bar_count=len(clean),
        extras={"integrity": integrity, "path": str(path)},
    )
    meta_path(symbol, timeframe, root=root).write_text(
        json.dumps(meta.to_dict(), indent=2), encoding="utf-8"
    )
    return {"ok": integrity.get("ok"), "path": str(path), "meta": meta.to_dict(), "integrity": integrity}


def load_dataset(
    symbol: str,
    timeframe: str,
    *,
    root: Path = DATA_DIR,
) -> dict[str, Any]:
    path = dataset_path(symbol, timeframe, root=root)
    mp = meta_path(symbol, timeframe, root=root)
    if not path.exists():
        return {"ok": False, "error": "missing_dataset", "path": str(path)}
    bars = []
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            bars.append(row_to_bar(json.loads(line)))
    meta = {}
    if mp.exists():
        meta = json.loads(mp.read_text(encoding="utf-8"))
    return {
        "ok": True,
        "path": str(path),
        "bars": bars,
        "meta": meta,
        "bar_count": len(bars),
    }


def merge_and_write(
    existing: Sequence[Bar],
    new_bars: Sequence[Bar],
    *,
    symbol: str,
    timeframe: str,
    source: str,
    root: Path = DATA_DIR,
    expected_period_sec: Optional[int] = None,
) -> dict[str, Any]:
    merged = dedupe_bars(list(existing) + list(new_bars))
    return write_dataset(
        merged,
        symbol=symbol,
        timeframe=timeframe,
        source=source,
        root=root,
        expected_period_sec=expected_period_sec,
    )
