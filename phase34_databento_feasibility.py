"""Phase 34 Databento microstructure feasibility probe — metadata/cost only, no bulk download."""
from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from databento_history import databento_preflight, load_databento_credential

OUT = Path("reports") / "phase34_databento_feasibility.json"
SCHEMAS = ("mbo", "mbp-10", "mbp-1", "tbbo", "trades", "ohlcv-1s", "ohlcv-1m")
# One quiet-ish RTH window for cost: 2026-06-02 (Tue) through 2026-06-03, NQM6 front month.
PILOT_START = "2026-06-02T13:30:00"
PILOT_END = "2026-06-02T20:00:00"  # 09:30-16:00 ET ≈ 13:30-20:00 UTC (EDT)
MONTH_START = "2026-06-01"
MONTH_END = "2026-07-01"
YEAR_START = "2025-08-01"
YEAR_END = "2026-08-01"
SYMBOL = "NQM6"  # June 2026 NQ, liquid in June 2026
DATASET = "GLBX.MDP3"


def _client():
    import databento as db

    cred = load_databento_credential()
    if not cred.get("credential_present"):
        return None
    import os

    return db.Historical(key=os.environ["DATABENTO_API_KEY"])


def _safe(fn, **kwargs) -> dict[str, Any]:
    try:
        val = fn(**kwargs)
        if hasattr(val, "to_dict"):
            return {"ok": True, "value": val.to_dict()}
        if hasattr(val, "__float__") and not isinstance(val, (dict, list, str)):
            return {"ok": True, "value": float(val)}
        return {"ok": True, "value": val}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}:{exc}"}


def main() -> dict[str, Any]:
    pf = databento_preflight()
    out: dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "preflight": pf,
        "dataset": DATASET,
        "probe_symbol": SYMBOL,
        "note": "Cost/record-count metadata only. No bulk MBO/MBP download in this probe.",
        "schemas": {},
        "range": None,
        "unit_prices": None,
    }
    if not pf.get("ok"):
        OUT.parent.mkdir(parents=True, exist_ok=True)
        OUT.write_text(json.dumps(out, indent=2, default=str), encoding="utf-8")
        print(json.dumps(out, indent=2, default=str)[:4000])
        return out

    client = _client()
    assert client is not None

    # Dataset range / schema list
    out["schemas_available"] = _safe(client.metadata.list_schemas, dataset=DATASET)
    out["range"] = _safe(client.metadata.get_dataset_range, dataset=DATASET)
    try:
        fields = {}
        for sch in ("mbo", "mbp-10", "trades"):
            try:
                fields[sch] = client.metadata.list_fields(schema=sch)
                if hasattr(fields[sch], "to_dict"):
                    fields[sch] = fields[sch].to_dict()
                elif hasattr(fields[sch], "to_json"):
                    fields[sch] = json.loads(fields[sch].to_json())
                else:
                    fields[sch] = str(fields[sch])[:2000]
            except Exception as exc:  # noqa: BLE001
                fields[sch] = f"ERR:{type(exc).__name__}:{exc}"
        out["fields"] = fields
    except Exception as exc:  # noqa: BLE001
        out["fields"] = f"ERR:{exc}"

    # Unit prices if API supports it
    try:
        up = client.metadata.list_unit_prices(dataset=DATASET, mode="historical-streaming")
        out["unit_prices"] = up if isinstance(up, (dict, list, str, int, float)) else str(up)[:3000]
    except Exception as exc:  # noqa: BLE001
        out["unit_prices_error"] = f"{type(exc).__name__}:{exc}"
        try:
            up = client.metadata.list_unit_prices(dataset=DATASET)
            out["unit_prices"] = up if isinstance(up, (dict, list, str, int, float)) else str(up)[:4000]
        except Exception as exc2:  # noqa: BLE001
            out["unit_prices_error2"] = f"{type(exc2).__name__}:{exc2}"

    windows = {
        "rth_one_day": (PILOT_START, PILOT_END),
        "one_month": (MONTH_START, MONTH_END),
        "one_year": (YEAR_START, YEAR_END),
    }
    for sch in SCHEMAS:
        row: dict[str, Any] = {"schema": sch}
        for wname, (start, end) in windows.items():
            cost = _safe(
                client.metadata.get_cost,
                dataset=DATASET,
                symbols=SYMBOL,
                schema=sch,
                start=start,
                end=end,
                stype_in="raw_symbol",
            )
            recs = _safe(
                client.metadata.get_record_count,
                dataset=DATASET,
                symbols=SYMBOL,
                schema=sch,
                start=start,
                end=end,
                stype_in="raw_symbol",
            )
            row[wname] = {"cost_usd": cost, "record_count": recs}
        out["schemas"][sch] = row

    # Continuous mapping check
    try:
        import databento as db

        res = client.symbology.resolve(
            dataset=DATASET,
            symbols=["NQ.v.0"],
            stype_in=db.SType.CONTINUOUS,
            stype_out=db.SType.RAW_SYMBOL,
            start_date="2026-06-01",
            end_date="2026-06-30",
        )
        out["continuous_mapping_sample"] = res if isinstance(res, dict) else getattr(res, "result", str(res)[:2000])
    except Exception as exc:  # noqa: BLE001
        out["continuous_mapping_sample"] = f"ERR:{type(exc).__name__}:{exc}"

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(out, indent=2, default=str), encoding="utf-8")
    print(json.dumps({k: out[k] for k in ("preflight", "schemas_available", "unit_prices_error", "unit_prices_error2") if k in out}, indent=2, default=str))
    print("--- schema costs ---")
    for sch, row in out["schemas"].items():
        def _c(w):
            v = ((row.get(w) or {}).get("cost_usd") or {})
            if v.get("ok"):
                return v.get("value")
            return v.get("error", "?")[:120]
        def _n(w):
            v = ((row.get(w) or {}).get("record_count") or {})
            if v.get("ok"):
                return v.get("value")
            return v.get("error", "?")[:80]
        print(f"{sch:10} 1d_cost={_c('rth_one_day')} 1d_n={_n('rth_one_day')}  mo_cost={_c('one_month')}  yr_cost={_c('one_year')}")
    return out


if __name__ == "__main__":
    main()
