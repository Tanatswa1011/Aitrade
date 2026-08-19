"""Download Databento GC.v.0 ohlcv-1m 2020-2026 for Phase 45 (quoted ~$8.45)."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from bar_dataset import write_dataset
from databento_history import load_databento_credential, ohlcv_records_to_bars

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "data" / "databento" / "GC" / "stitched"
JSONL_META_HINT = OUT / "databento_GC_v0_1m.meta.json"
START = "2020-01-01"
END = "2026-08-17"


def main() -> dict:
    OUT.mkdir(parents=True, exist_ok=True)
    existing = OUT / "databento_GC_v0_1m.jsonl"
    if existing.exists() and existing.stat().st_size > 10_000_000:
        return {"ok": True, "cached": True, "path": str(existing), "bytes": existing.stat().st_size}
    cred = load_databento_credential()
    if not cred.get("credential_present"):
        return {"ok": False, "error": "DATABENTO_CREDENTIAL_REQUIRED"}
    import databento as db

    client = db.Historical()
    cost = client.metadata.get_cost(
        dataset="GLBX.MDP3",
        symbols=["GC.v.0"],
        schema="ohlcv-1m",
        start=START,
        end=END,
        stype_in="continuous",
    )
    print(f"quoted_cost_usd={cost}", flush=True)
    dbn = OUT / "databento_GC_v0_1m.dbn.zst"
    if not (dbn.exists() and dbn.stat().st_size > 1_000_000):
        print("downloading DBN...", flush=True)
        data = client.timeseries.get_range(
            dataset="GLBX.MDP3",
            symbols=["GC.v.0"],
            schema="ohlcv-1m",
            start=START,
            end=END,
            stype_in="continuous",
        )
        data.to_file(dbn)
        print(f"dbn_bytes={dbn.stat().st_size}", flush=True)
    print("converting to jsonl...", flush=True)
    store = db.DBNStore.from_file(dbn)
    bars = ohlcv_records_to_bars(store)
    written = write_dataset(
        bars,
        symbol="databento_GC_v0",
        timeframe="1m",
        source="databento:GLBX.MDP3:GC.v.0_volume_continuous",
        root=OUT,
        expected_period_sec=60,
    )
    meta_path = OUT / "databento_GC_v0_1m.meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta.update({
        "root": "GC",
        "schema": "ohlcv-1m",
        "continuous_choice": "databento_GC.v.0_volume_continuous",
        "tick_size": 0.1,
        "phase": 45,
        "period": f"{START} -> {END}",
        "cost_usd": float(cost),
        "dbn_path": str(dbn),
        "capture_note": datetime.now(tz=timezone.utc).isoformat(),
    })
    meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")
    print(json.dumps({"ok": True, "n": len(bars), "cost": cost, "path": written.get("path")}, indent=2), flush=True)
    return {"ok": True, "n": len(bars), "cost": cost, **written}


if __name__ == "__main__":
    main()
