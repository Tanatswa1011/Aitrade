"""Enhanced LuxAlgo capture persistence for Phase 20 (CHoCH only)."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

from models import StructureConfirmation

DEFAULT_CAPTURE_PATH = Path("data") / "luxalgo_captures" / "choch_events.jsonl"
LEGACY_CAPTURE_PATH = Path("data") / "luxalgo_choch_captures.jsonl"


def event_dedupe_key(
    event: StructureConfirmation | dict[str, Any],
    *,
    symbol: Optional[str] = None,
    timeframe: Optional[str] = None,
) -> str:
    if isinstance(event, StructureConfirmation):
        return "|".join(
            [
                str(symbol or ""),
                str(timeframe or ""),
                str(event.direction),
                str(event.level),
                str(event.event_timestamp),
                str(event.event_bar_index),
                str(event.study_id or ""),
                str(event.raw_id or ""),
                str(event.timing_confidence),
            ]
        )
    return "|".join(
        [
            str(symbol if symbol is not None else event.get("symbol") or ""),
            str(timeframe if timeframe is not None else event.get("timeframe") or ""),
            str(event.get("direction")),
            str(event.get("level")),
            str(event.get("timestamp") or event.get("event_timestamp")),
            str(event.get("bar_index") or event.get("event_bar_index")),
            str(event.get("study_id") or ""),
            str((event.get("study_identity") or {}).get("raw_id") or event.get("raw_id") or ""),
            str(event.get("timing_confidence") or ""),
        ]
    )


def _event_id(row: dict[str, Any]) -> str:
    raw = "|".join(
        [
            str(row.get("symbol")),
            str(row.get("timeframe")),
            str(row.get("direction")),
            str(row.get("level")),
            str(row.get("event_timestamp")),
            str(row.get("event_bar_index")),
            str(row.get("study_id") or ""),
        ]
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]


def event_to_capture_row(
    event: StructureConfirmation,
    *,
    symbol: str,
    timeframe: str,
    bars_by_series_index: Optional[dict] = None,
    study_name: Optional[str] = None,
    include_unreliable: bool = True,
) -> Optional[dict[str, Any]]:
    if event.kind != "CHoCH":
        return None
    ts = event.event_timestamp
    mapping_method = None
    mapping_status = "unresolved"
    if ts is not None and event.timing_confidence == "exact":
        mapping_method = "exact_timestamp"
        mapping_status = "mapped"
    elif ts is not None and event.timing_confidence == "derived":
        mapping_method = "derived_bar_or_line_anchor"
        mapping_status = "mapped"
    elif event.event_bar_index is not None and bars_by_series_index:
        ts = bars_by_series_index.get(int(event.event_bar_index))
        if ts is None:
            ts = bars_by_series_index.get(str(event.event_bar_index))
        if ts is not None:
            mapping_method = "bar_index_to_timestamp"
            mapping_status = "mapped"

    reliable = (
        event.direction is not None
        and event.level is not None
        and event.timing_confidence in ("exact", "derived")
        and ts is not None
        and mapping_status == "mapped"
    )
    if not reliable and not include_unreliable:
        return None
    if event.timing_confidence == "unavailable" and (
        (event.extras or {}).get("placeholder_bar")
        or (event.event_bar_index is not None and int(event.event_bar_index) < 0)
    ):
        # Keep for diagnostics; never treat as reliable exact-time
        pass

    extras = dict(event.extras or {})
    row = {
        "event_id": None,  # filled below
        "symbol": symbol,
        "timeframe": timeframe,
        "direction": event.direction,
        "level": event.level,
        "event_timestamp": None if ts is None else int(ts),
        "timestamp": None if ts is None else int(ts),  # legacy alias
        "event_bar_index": event.event_bar_index,
        "bar_index": event.event_bar_index,
        "capture_timestamp": datetime.now(tz=timezone.utc).isoformat(),
        "captured_at": datetime.now(tz=timezone.utc).isoformat(),
        "timing_confidence": event.timing_confidence,
        "source": "luxalgo",
        "study_name": study_name,
        "study_id": event.study_id,
        "label_text": "CHoCH",
        "paired_line_information": {
            "line_id": extras.get("line_id"),
            "line_style": extras.get("line_style"),
            "line_x1": extras.get("line_x1"),
            "line_x2": extras.get("line_x2"),
        },
        "raw_metadata": extras,
        "mapping_method": mapping_method,
        "mapping_status": mapping_status,
        "reliable": reliable,
        "study_identity": {
            "study_id": event.study_id,
            "source": event.source,
            "raw_id": event.raw_id,
            "study_name": study_name,
        },
        "dedupe_key": None,
    }
    row["event_id"] = _event_id(row)
    row["dedupe_key"] = event_dedupe_key(row, symbol=symbol, timeframe=timeframe)
    return row


def append_luxalgo_captures(
    events: Sequence[StructureConfirmation],
    *,
    symbol: str,
    timeframe: str,
    path: Path = DEFAULT_CAPTURE_PATH,
    bars_by_series_index: Optional[dict] = None,
    study_name: Optional[str] = None,
    include_unreliable: bool = False,
) -> dict[str, Any]:
    path.parent.mkdir(parents=True, exist_ok=True)
    existing: set[str] = set()
    if path.exists():
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                    existing.add(
                        str(
                            row.get("dedupe_key")
                            or event_dedupe_key(
                                row,
                                symbol=row.get("symbol"),
                                timeframe=row.get("timeframe"),
                            )
                        )
                    )
                except json.JSONDecodeError:
                    continue

    written = 0
    skipped = 0
    unreliable_written = 0
    with path.open("a", encoding="utf-8") as fh:
        for ev in events:
            row = event_to_capture_row(
                ev,
                symbol=symbol,
                timeframe=timeframe,
                bars_by_series_index=bars_by_series_index,
                study_name=study_name,
                include_unreliable=include_unreliable,
            )
            if row is None:
                skipped += 1
                continue
            key = row["dedupe_key"]
            if key in existing:
                skipped += 1
                continue
            fh.write(json.dumps(row, default=str) + "\n")
            existing.add(key)
            written += 1
            if not row.get("reliable"):
                unreliable_written += 1
    # Mirror reliable rows to legacy path only from the canonical Phase-20 store
    if path == DEFAULT_CAPTURE_PATH:
        _mirror_reliable_to_legacy(path, LEGACY_CAPTURE_PATH)
    return {
        "ok": True,
        "path": str(path),
        "written": written,
        "unreliable_written": unreliable_written,
        "skipped": skipped,
        "total_keys": len(existing),
    }


def _mirror_reliable_to_legacy(src: Path, dest: Path) -> None:
    if not src.exists():
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    existing: set[str] = set()
    if dest.exists():
        with dest.open("r", encoding="utf-8") as fh:
            for line in fh:
                try:
                    existing.add(str(json.loads(line).get("dedupe_key")))
                except Exception:  # noqa: BLE001
                    continue
    with src.open("r", encoding="utf-8") as fh, dest.open("a", encoding="utf-8") as out:
        for line in fh:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not row.get("reliable"):
                continue
            key = str(row.get("dedupe_key"))
            if key in existing:
                continue
            # legacy-shaped subset
            legacy = {
                "symbol": row.get("symbol"),
                "timeframe": row.get("timeframe"),
                "direction": row.get("direction"),
                "level": row.get("level"),
                "timestamp": row.get("event_timestamp"),
                "bar_index": row.get("event_bar_index"),
                "timing_confidence": row.get("timing_confidence"),
                "study_id": row.get("study_id"),
                "study_identity": row.get("study_identity"),
                "captured_at": row.get("capture_timestamp"),
                "dedupe_key": key,
            }
            out.write(json.dumps(legacy, default=str) + "\n")
            existing.add(key)


def semantic_event_key(row: dict[str, Any]) -> str:
    """Stable identity across legacy/new capture schemas (ignores capture_timestamp)."""
    return "|".join(
        [
            str(row.get("symbol") or ""),
            str(row.get("timeframe") or ""),
            str(row.get("direction")),
            str(row.get("level")),
            str(row.get("event_timestamp") or row.get("timestamp")),
            str(row.get("timing_confidence") or ""),
            str(row.get("study_id") or ""),
        ]
    )


def load_luxalgo_captures(
    *,
    path: Optional[Path] = None,
    symbol: Optional[str] = None,
    timeframe: Optional[str] = None,
    reliable_only: bool = False,
) -> list[dict[str, Any]]:
    paths = []
    if path is not None:
        paths.append(path)
    else:
        # Prefer Phase-20 store; fall back to legacy only if empty/missing
        if DEFAULT_CAPTURE_PATH.exists():
            paths.append(DEFAULT_CAPTURE_PATH)
            # Also read legacy for older reliable rows not yet mirrored,
            # but semantic-dedupe below prevents double count.
            if LEGACY_CAPTURE_PATH.exists():
                paths.append(LEGACY_CAPTURE_PATH)
        elif LEGACY_CAPTURE_PATH.exists():
            paths.append(LEGACY_CAPTURE_PATH)
    rows: list[dict[str, Any]] = []
    seen = set()
    for p in paths:
        if not p.exists():
            continue
        with p.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                row = json.loads(line)
                if symbol and row.get("symbol") != symbol:
                    continue
                if timeframe and row.get("timeframe") != timeframe:
                    continue
                # normalize aliases
                if row.get("event_timestamp") is None and row.get("timestamp") is not None:
                    row["event_timestamp"] = row["timestamp"]
                if "reliable" not in row:
                    row["reliable"] = row.get("timing_confidence") in ("exact", "derived") and row.get(
                        "event_timestamp"
                    ) is not None
                if reliable_only and not row.get("reliable"):
                    continue
                key = semantic_event_key(row)
                if key in seen:
                    continue
                seen.add(key)
                rows.append(row)
    return rows


def captures_to_confirmations(rows: Iterable[dict[str, Any]]) -> list[StructureConfirmation]:
    out = []
    for row in rows:
        ts = row.get("event_timestamp", row.get("timestamp"))
        out.append(
            StructureConfirmation(
                kind="CHoCH",
                direction=str(row.get("direction")),
                level=float(row["level"]),
                event_timestamp=int(ts) if ts is not None else None,
                event_bar_index=row.get("event_bar_index", row.get("bar_index")),
                source="luxalgo",
                study_id=row.get("study_id"),
                raw_id=str((row.get("study_identity") or {}).get("raw_id") or row.get("study_id") or ""),
                timing_confidence=str(row.get("timing_confidence") or "unavailable"),
                extras=dict(row.get("raw_metadata") or {}),
            )
        )
    return out
