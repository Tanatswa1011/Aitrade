"""Parallel windowed MBP-10 + trades download for Phase 35.

Reads already-detected events from reports/phase35_sweep_events.csv so we do not
re-scan 1m jsonl. Skips cached files. Cache-safe: never re-purchases an existing
slice with size > 1000 bytes.
"""
from __future__ import annotations

import csv
import json
import sys
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

from nq_microstructure_features import merge_spans

ROOT = Path(__file__).resolve().parent
REPORTS = ROOT / "reports"
WINDOW_BEFORE_SEC = 60
WINDOW_AFTER_SEC = 120
MIN_CACHE_BYTES = 1000
WORKERS = 4


def windows_from_csv() -> list[dict]:
    path = REPORTS / "phase35_sweep_events.csv"
    rows = list(csv.DictReader(path.open(encoding="utf-8")))
    groups: dict[tuple[str, str], list[tuple[int, int]]] = {}
    for r in rows:
        sym = r.get("contract") or "NQM6"
        td = r["trading_date"]
        t = int(float(r["sweep_bar_time"]))
        groups.setdefault((sym, td), []).append((t - WINDOW_BEFORE_SEC, t + WINDOW_AFTER_SEC))
    parts = []
    for (sym, td), spans in sorted(groups.items()):
        for lo, hi in merge_spans(spans):
            parts.append({
                "symbol": sym,
                "date": td,
                "start": datetime.fromtimestamp(lo, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"),
                "end": datetime.fromtimestamp(hi, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"),
                "start_unix": lo,
                "end_unix": hi,
            })
    return parts


def dest_path(schema: str, w: dict) -> Path:
    dest = ROOT / "data" / "databento" / "NQ" / "microstructure" / schema
    dest.mkdir(parents=True, exist_ok=True)
    return dest / f"{w['symbol']}_{schema}_{w['date']}_{int(w['start_unix'])}.dbn.zst"


def download_one(schema: str, w: dict) -> dict:
    from databento_history import load_databento_credential
    import databento as db

    load_databento_credential()
    path = dest_path(schema, w)
    if path.exists() and path.stat().st_size > MIN_CACHE_BYTES:
        return {"ok": True, "cached": True, "path": str(path), "bytes": path.stat().st_size, "schema": schema}
    try:
        client = db.Historical()
        data = client.timeseries.get_range(
            dataset="GLBX.MDP3",
            symbols=w["symbol"],
            schema=schema,
            start=w["start"],
            end=w["end"],
            stype_in="raw_symbol",
        )
        data.to_file(path)
        return {"ok": True, "cached": False, "path": str(path), "bytes": path.stat().st_size, "schema": schema}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "path": str(path), "error": f"{type(exc).__name__}:{exc}", "schema": schema}


def main() -> dict:
    schemas = ["mbp-10", "trades"]
    if "--mbp-only" in sys.argv:
        schemas = ["mbp-10"]
    if "--trades-only" in sys.argv:
        schemas = ["trades"]
    wins = windows_from_csv()
    jobs = [(sch, w) for sch in schemas for w in wins]
    pending = []
    cached = 0
    for sch, w in jobs:
        p = dest_path(sch, w)
        if p.exists() and p.stat().st_size > MIN_CACHE_BYTES:
            cached += 1
        else:
            pending.append((sch, w))
    print(json.dumps({"n_windows": len(wins), "n_jobs": len(jobs), "cached": cached, "to_download": len(pending), "workers": WORKERS}), flush=True)
    n_new = n_fail = 0
    n_done = 0
    with ProcessPoolExecutor(max_workers=WORKERS) as ex:
        futs = [ex.submit(download_one, sch, w) for sch, w in pending]
        for fut in as_completed(futs):
            rec = fut.result()
            n_done += 1
            if rec.get("ok") and not rec.get("cached"):
                n_new += 1
                print(f"saved {Path(rec['path']).name} ({rec.get('bytes')} bytes)  {n_done}/{len(pending)}", flush=True)
            elif not rec.get("ok"):
                n_fail += 1
                print(f"FAILED {rec.get('path')}: {rec.get('error')}", flush=True)
            elif n_done % 25 == 0:
                print(f"progress {n_done}/{len(pending)} cached_hits_in_flight={rec.get('cached')}", flush=True)
    out = {"ok": n_fail == 0, "n_windows": len(wins), "cached_before": cached, "n_new": n_new, "n_fail": n_fail}
    print(json.dumps(out), flush=True)
    return out


if __name__ == "__main__":
    main()
