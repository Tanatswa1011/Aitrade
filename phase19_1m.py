"""Fetch sparse 1m windows and resolve 5m trigger-bar ambiguities."""

from __future__ import annotations

from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Optional, Sequence

from bar_dataset import load_dataset, write_dataset
from intrabar_resolver import (
    ENTRY_THEN_STOP,
    INSUFFICIENT_DATA,
    RESOLVED_NO_STOP,
    STOP_BEFORE_ENTRY,
    STOP_THEN_TARGET,
    STILL_AMBIGUOUS,
    TARGET_THEN_STOP,
    resolver_5m_from_1m,
)
from models import Bar
from openbb_history import OpenBBHistoricalDataProvider, load_dotenv_credentials
from timeframe import timeframe_seconds


TIINGO_ROOT = Path("data") / "openbb" / "tiingo"
ONE_M_SYMBOL = "openbb_tiingo_XAUUSD"


def identify_5m_ambiguous_windows(records: Sequence[Any]) -> list[dict[str, Any]]:
    """Extract unresolved 5m ambiguity windows (entry/stop or target/stop)."""
    out = []
    for r in records:
        tf = ""
        if isinstance(r, dict):
            tf = str(r.get("execution_timeframe") or r.get("timeframe") or "")
            direction = r.get("direction") or ""
            setup_id = r.get("setup_id")
            entries = r.get("entry_results") or []
        else:
            tf = str(getattr(r, "execution_timeframe", None) or getattr(r, "timeframe", "") or "")
            direction = getattr(r, "direction", None) or ""
            setup_id = getattr(r, "setup_id", None)
            entries = getattr(r, "entry_results", []) or []
        if tf not in ("5m", "5"):
            continue
        for e in entries:
            if isinstance(e, dict):
                flags = list(e.get("ambiguity_flags") or [])
                outcome = e.get("outcome")
                mode = e.get("mode")
                entry_ts = e.get("entry_timestamp")
                entry_price = e.get("entry_price")
                stop_price = e.get("stop_price")
                triggered = bool(e.get("triggered"))
                ev = e.get("event_timestamps") or {}
            else:
                flags = list(getattr(e, "ambiguity_flags", []) or [])
                outcome = getattr(e, "outcome", None)
                mode = getattr(e, "mode", None)
                entry_ts = getattr(e, "entry_timestamp", None)
                entry_price = getattr(e, "entry_price", None)
                stop_price = getattr(e, "stop_price", None)
                triggered = bool(getattr(e, "triggered", False))
                ev = getattr(e, "event_timestamps", None) or {}
            if not triggered:
                continue
            amb = outcome == "AMBIGUOUS_INTRABAR" or "TRIGGER_BAR_STOP_AMBIGUITY" in flags
            if not amb:
                continue
            if entry_ts is None or entry_price is None or stop_price is None:
                continue
            # Align to 5m candle open
            parent_ts = int(entry_ts) - (int(entry_ts) % 300)
            out.append(
                {
                    "setup_id": setup_id,
                    "entry_mode": mode,
                    "direction": direction,
                    "parent_bar_time": parent_ts,
                    "entry_price": float(entry_price),
                    "stop_price": float(stop_price),
                    "kind": "entry_stop",
                    "event_timestamps": ev,
                }
            )
    # de-dupe by setup/mode/parent
    seen = set()
    uniq = []
    for row in out:
        key = (row["setup_id"], row["entry_mode"], row["parent_bar_time"], row["kind"])
        if key in seen:
            continue
        seen.add(key)
        uniq.append(row)
    return uniq


def fetch_1m_windows(
    parent_times: Sequence[int],
    *,
    persist: bool = True,
    root: Path = TIINGO_ROOT,
    sleep_sec: float = 0.35,
) -> dict[str, Any]:
    """Fetch only constituent 1m bars for given 5m parent opens."""
    import time

    load_dotenv_credentials()
    times = sorted({int(t) for t in parent_times})
    if not times:
        return {"ok": True, "windows_requested": 0, "bars": [], "fetched_windows": 0}

    prov = OpenBBHistoricalDataProvider(underlying_provider="tiingo", route="currency")
    all_bars: list[Bar] = []
    errors: list[str] = []
    fetched = 0
    rate_limited = False
    for i, ts in enumerate(times):
        if rate_limited:
            break
        try:
            res = prov.fetch_result(
                "XAUUSD",
                "1m",
                start_ts=ts - 60,
                end_ts=ts + 5 * 60,
            )
            if res.bars:
                all_bars.extend(res.bars)
                fetched += 1
            err_text = " ".join(str(e) for e in (res.errors or []))
            if "hourly request allocation" in err_text.lower() or "rate" in err_text.lower():
                rate_limited = True
                errors.append(err_text[:200])
            elif res.errors:
                errors.extend(list(res.errors)[:1])
        except Exception as exc:  # noqa: BLE001
            msg = str(exc)[:200]
            errors.append(msg)
            if "allocation" in msg.lower():
                rate_limited = True
        if sleep_sec and i + 1 < len(times) and not rate_limited:
            time.sleep(float(sleep_sec))

    # merge with any existing 1m cache
    existing = load_dataset(ONE_M_SYMBOL, "1m", root=root)
    merged_map: dict[int, Bar] = {}
    for b in list(existing.get("bars") or []) + all_bars:
        merged_map[int(b.time)] = b
    merged = [merged_map[t] for t in sorted(merged_map)]
    if persist and merged:
        write_dataset(
            merged,
            symbol=ONE_M_SYMBOL,
            timeframe="1m",
            source="openbb:tiingo:sparse_1m_windows",
            root=root,
            expected_period_sec=timeframe_seconds("1m"),
        )
    return {
        "ok": True,
        "windows_requested": len(times),
        "fetched_windows": fetched,
        "rate_limited": rate_limited,
        "bars": merged,
        "bar_count": len(merged),
        "errors_head": errors[:5],
        "note": "1m used as intrabar evidence only; not a strategy execution TF",
        "provenance": {
            "data_provider": "openbb",
            "underlying_provider": "tiingo",
            "source_symbol": "XAUUSD",
            "feed_equivalence_class": "CLOSE_EQUIVALENT",
        },
    }


def resolve_5m_with_1m(
    windows: Sequence[dict[str, Any]],
    bars_1m: Sequence[Bar],
) -> dict[str, Any]:
    resolver = resolver_5m_from_1m()
    rows = []
    for w in windows:
        res = resolver.resolve_entry_stop(
            direction=str(w.get("direction") or ""),
            entry_price=float(w["entry_price"]),
            stop_price=float(w["stop_price"]),
            parent_bar_time=int(w["parent_bar_time"]),
            child_bars=bars_1m,
        )
        # Update note language for 1m
        note = res.note.replace("5m", "1m") if res.note else ""
        if res.result == STILL_AMBIGUOUS:
            note = "entry_and_stop_inside_same_1m_candle;_no_tick_ordering"
        rows.append(
            {
                "setup_id": w.get("setup_id"),
                "entry_mode": w.get("entry_mode"),
                "parent_bar_time": w.get("parent_bar_time"),
                "kind": w.get("kind"),
                **{**res.to_dict(), "note": note},
            }
        )

    by_result = Counter(r["result"] for r in rows)
    by_mode: dict[str, Counter] = defaultdict(Counter)
    for r in rows:
        by_mode[str(r.get("entry_mode") or "unknown")][r["result"]] += 1

    resolved_states = {
        ENTRY_THEN_STOP,
        STOP_BEFORE_ENTRY,
        TARGET_THEN_STOP,
        STOP_THEN_TARGET,
        RESOLVED_NO_STOP,
    }
    return {
        "ambiguous_before_1m": len(windows),
        "windows_evaluated": len(rows),
        "resolved_with_1m": sum(1 for r in rows if r["result"] in resolved_states),
        "still_ambiguous": by_result.get(STILL_AMBIGUOUS, 0),
        "insufficient_data": by_result.get(INSUFFICIENT_DATA, 0),
        "by_result": dict(by_result),
        "by_entry_mode": {m: dict(c) for m, c in by_mode.items()},
        "rows": rows,
    }
