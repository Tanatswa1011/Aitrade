"""Phase 43 — US equity small-cap data feasibility (metadata/cost only).

No bulk download. No broker. Do not subscribe or spend money here.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from databento_history import databento_preflight, load_databento_credential
from openbb_history import load_dotenv_credentials

OUT = Path("reports") / "phase43_data_feasibility.json"

EQUITY_DATASETS = (
    "EQUS.MINI",
    "EQUS.SUMMARY",
    "IEXG.TOPS",
    "XNAS.ITCH",
    "XNAS.BASIC",
    "DBEQ.BASIC",
)


def _safe(fn, **kwargs) -> dict[str, Any]:
    try:
        val = fn(**kwargs)
        if hasattr(val, "to_dict"):
            return {"ok": True, "value": val.to_dict()}
        if isinstance(val, (int, float)):
            return {"ok": True, "value": float(val)}
        if isinstance(val, (list, dict, str, bool)) or val is None:
            return {"ok": True, "value": val}
        return {"ok": True, "value": str(val)[:4000]}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}:{exc}"}


def _client():
    import databento as db

    cred = load_databento_credential()
    if not cred.get("credential_present"):
        return None
    return db.Historical(key=os.environ["DATABENTO_API_KEY"])


def probe_tiingo_equity() -> dict[str, Any]:
    """Tiny route check: one live name and one bankrupt name. No universe download."""
    from openbb_history import OpenBBHistoricalDataProvider

    load_dotenv_credentials()
    prov = OpenBBHistoricalDataProvider(underlying_provider="tiingo", route="equity")
    out: dict[str, Any] = {"route": "obb.equity.price.historical", "provider": "tiingo"}
    live = prov.fetch("AAPL", "1D", start_ts=1704153600, end_ts=1706832000)
    out["aapl_daily"] = {
        "bar_count": len(live.bars) if hasattr(live, "bars") else None,
        "errors": (live.meta.extras.get("errors") if hasattr(live, "meta") else []) or [],
        "actual_start": getattr(getattr(live, "meta", None), "actual_start", None),
        "actual_end": getattr(getattr(live, "meta", None), "actual_end", None),
    }
    dead = prov.fetch("BBBY", "1D", start_ts=1672531200, end_ts=1696118400)
    out["bbby_daily"] = {
        "bar_count": len(dead.bars) if hasattr(dead, "bars") else None,
        "errors": (dead.meta.extras.get("errors") if hasattr(dead, "meta") else []) or [],
        "actual_start": getattr(getattr(dead, "meta", None), "actual_start", None),
        "actual_end": getattr(getattr(dead, "meta", None), "actual_end", None),
        "note": "BBBY filed bankruptcy 2023; zero bars implies delisted coverage gap on this route.",
    }
    return out


def main() -> dict[str, Any]:
    pf = databento_preflight()
    creds = load_dotenv_credentials()
    out: dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "note": "Metadata and get_cost only. No timeseries download. No new subscription.",
        "preflight": pf,
        "credential_names_present": {
            "DATABENTO_API_KEY": bool(creds.get("DATABENTO_API_KEY")),
            "TIINGO_TOKEN": bool(creds.get("TIINGO_TOKEN")),
            "FMP_API_KEY": bool(creds.get("FMP_API_KEY")),
            "POLYGON_API_KEY": bool(os.environ.get("POLYGON_API_KEY", "").strip()),
            "ALPACA_API_KEY": bool(os.environ.get("ALPACA_API_KEY", "").strip()),
        },
        "datasets": {},
        "costs": {},
        "tiingo_equity_probe": None,
        "local_equity_files": [],
    }
    data_root = Path("data")
    for p in data_root.rglob("*"):
        if p.is_file() and any(x in p.name.lower() for x in ("equity", "stock", "nasdaq", "nyse", "otc")):
            if "databento_NQ" in str(p) or "macro" in str(p).replace("\\", "/"):
                continue
            out["local_equity_files"].append(str(p))

    if not pf.get("ok"):
        OUT.parent.mkdir(parents=True, exist_ok=True)
        OUT.write_text(json.dumps(out, indent=2, default=str), encoding="utf-8")
        print("databento preflight failed", flush=True)
        return out

    client = _client()
    assert client is not None
    out["list_datasets"] = _safe(client.metadata.list_datasets)
    for ds in EQUITY_DATASETS:
        block: dict[str, Any] = {}
        block["range"] = _safe(client.metadata.get_dataset_range, dataset=ds)
        block["schemas"] = _safe(client.metadata.list_schemas, dataset=ds)
        out["datasets"][ds] = block
        print(f"dataset {ds} range_ok={block['range'].get('ok')} schemas_ok={block['schemas'].get('ok')}", flush=True)

    # Cost quotes: one RTH day of 1m OHLCV, all symbols, if the dataset exists
    day_start, day_end = "2024-06-03", "2024-06-04"
    year_start, year_end = "2023-03-28", "2026-08-14"
    for ds, schema, start, end, label in [
        ("EQUS.MINI", "ohlcv-1m", day_start, day_end, "equs_mini_1m_all_1day"),
        ("EQUS.MINI", "ohlcv-1d", year_start, year_end, "equs_mini_1d_all_2023_2026"),
        ("EQUS.MINI", "definition", day_start, day_end, "equs_mini_definition_1day"),
        ("EQUS.MINI", "status", day_start, day_end, "equs_mini_status_1day"),
        ("IEXG.TOPS", "ohlcv-1m", day_start, day_end, "iex_1m_all_1day"),
        ("IEXG.TOPS", "ohlcv-1d", "2018-01-02", "2026-08-14", "iex_1d_all_2018_2026"),
        ("EQUS.SUMMARY", "ohlcv-1d", "2018-01-02", "2026-08-14", "equs_summary_1d_2018_2026"),
        ("EQUS.MINI", "ohlcv-1m", year_start, year_end, "equs_mini_1m_all_2023_2026"),
    ]:
        print(f"cost {label}...", flush=True)
        out["costs"][label] = _safe(
            client.metadata.get_cost,
            dataset=ds,
            schema=schema,
            symbols="ALL_SYMBOLS",
            start=start,
            end=end,
        )

    try:
        out["tiingo_equity_probe"] = probe_tiingo_equity()
    except Exception as exc:  # noqa: BLE001
        out["tiingo_equity_probe"] = {"ok": False, "error": f"{type(exc).__name__}:{exc}"}

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(out, indent=2, default=str), encoding="utf-8")
    print("wrote", OUT, flush=True)
    return out


if __name__ == "__main__":
    main()
