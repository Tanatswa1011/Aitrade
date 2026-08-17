"""Live-first LuxAlgo CHoCH capture for Phase 20 (5m / 15m independent pools)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Optional, Sequence

from luxalgo_capture import DEFAULT_CAPTURE_PATH, append_luxalgo_captures, load_luxalgo_captures

SYMBOL = "OANDA:XAUUSD"
TIMEFRAMES = ("5m", "15m")


async def capture_luxalgo_choch_once(
    *,
    timeframes: Sequence[str] = TIMEFRAMES,
    path: Path = DEFAULT_CAPTURE_PATH,
    include_unreliable: bool = True,
    restore_resolution: bool = True,
) -> dict[str, Any]:
    """Switch TF, fetch LuxAlgo CHoCH, persist with dedupe. Read-only chart ops."""
    from bars import fetch_bars
    from chart_timeframe import get_chart_resolution, set_chart_resolution
    from luxalgo_structure import fetch_luxalgo_choch

    out: dict[str, Any] = {"ok": True, "by_timeframe": {}, "path": str(path)}
    prev = None
    try:
        prev = await get_chart_resolution()
    except Exception as exc:  # noqa: BLE001
        out["resolution_probe_error"] = str(exc)

    for tf in timeframes:
        try:
            await set_chart_resolution(tf)
            await asyncio.sleep(1.5)
            bars_payload = await fetch_bars()
            by_index = bars_payload.get("bars_by_series_index") or {}
            lux = await fetch_luxalgo_choch(bars_by_series_index=by_index)
            persist = {"ok": False, "written": 0}
            if lux.get("ok"):
                persist = append_luxalgo_captures(
                    lux.get("events") or [],
                    symbol=SYMBOL,
                    timeframe=tf,
                    path=path,
                    bars_by_series_index=by_index or lux.get("bars_by_series_index"),
                    study_name=lux.get("study_name"),
                    include_unreliable=include_unreliable,
                )
            # Persist raw label context snapshot for divergence diagnostics
            ctx_path = path.parent / f"raw_context_{tf}.jsonl"
            _append_raw_context(ctx_path, lux, symbol=SYMBOL, timeframe=tf)

            out["by_timeframe"][tf] = {
                "fetch_ok": lux.get("ok"),
                "study_id": lux.get("study_id"),
                "study_name": lux.get("study_name"),
                "counts": lux.get("counts"),
                "indexes_non_placeholder": lux.get("indexes_non_placeholder"),
                "persist": persist,
                "error": lux.get("error"),
            }
        except Exception as exc:  # noqa: BLE001
            out["by_timeframe"][tf] = {"ok": False, "error": str(exc)}
            out["ok"] = False

    if restore_resolution and prev and prev.get("resolution") is not None:
        try:
            await set_chart_resolution(str(prev["resolution"]))
            await asyncio.sleep(0.8)
            out["restored_resolution"] = str(prev["resolution"])
        except Exception as exc:  # noqa: BLE001
            out["restore_error"] = str(exc)

    out["store_summary"] = summarize_capture_store(path=path)
    return out


def _append_raw_context(path: Path, lux: dict[str, Any], *, symbol: str, timeframe: str) -> None:
    import json
    from datetime import datetime, timezone

    if not lux.get("ok"):
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    row = {
        "captured_at": datetime.now(tz=timezone.utc).isoformat(),
        "symbol": symbol,
        "timeframe": timeframe,
        "study_id": lux.get("study_id"),
        "study_name": lux.get("study_name"),
        "label_texts": [
            str(x.get("t") or "") for x in (lux.get("raw_labels") or [])
        ],
        "raw_labels": lux.get("raw_labels") or [],
        "raw_lines_count": len(lux.get("raw_lines") or []),
    }
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, default=str) + "\n")


def summarize_capture_store(
    *,
    path: Optional[Path] = None,
    symbol: str = SYMBOL,
) -> dict[str, Any]:
    rows = load_luxalgo_captures(path=path, symbol=symbol)
    by_tf: dict[str, Any] = {}
    for tf in TIMEFRAMES:
        subset = [r for r in rows if r.get("timeframe") == tf]
        reliable = [r for r in subset if r.get("reliable")]
        bull = sum(1 for r in reliable if r.get("direction") == "bullish")
        bear = sum(1 for r in reliable if r.get("direction") == "bearish")
        confs: dict[str, int] = {}
        for r in subset:
            c = str(r.get("timing_confidence") or "unavailable")
            confs[c] = confs.get(c, 0) + 1
        ts_list = [int(r["event_timestamp"]) for r in reliable if r.get("event_timestamp") is not None]
        by_tf[tf] = {
            "total_rows": len(subset),
            "reliable": len(reliable),
            "bullish": bull,
            "bearish": bear,
            "timing_confidence": confs,
            "date_coverage_unix": {
                "min": min(ts_list) if ts_list else None,
                "max": max(ts_list) if ts_list else None,
            },
        }
    return {
        "path": str(path or DEFAULT_CAPTURE_PATH),
        "total_rows": len(rows),
        "reliable_total": sum(int(by_tf[tf]["reliable"]) for tf in TIMEFRAMES),
        "by_timeframe": by_tf,
    }


async def main() -> None:
    report = await capture_luxalgo_choch_once()
    import json

    print(json.dumps(report, indent=2, default=str))


if __name__ == "__main__":
    asyncio.run(main())
