"""Databento NQ futures fetch, NY-aligned aggregation, and volume-crossover stitch."""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Sequence
from zoneinfo import ZoneInfo

from bar_dataset import load_dataset, write_dataset
from databento_history import (
    VOLUME_SEMANTICS,
    VOLUME_STATUS,
    DatabentoHistoricalDataProvider,
    databento_preflight,
    load_databento_credential,
    validate_bars_quality,
)
from gc_contract_stitch import (
    ContractSeries,
    decide_rolls,
    detect_roll_price_artifacts,
    stitch_contracts,
)
from models import Bar

NY = ZoneInfo("America/New_York")
DATA_ROOT = Path("data") / "databento" / "NQ"
DEFAULT_START = "2020-01-01"
DEFAULT_END = "2026-08-16"
NQ_TICK = 0.25
NQ_MONTH_CODES = "HMUZ"


def aggregate_1m_to_ny(bars_1m: Sequence[Bar], minutes: int) -> list[Bar]:
    """Floor to America/New_York clock boundaries (5 or 15)."""
    buckets: dict[int, list[Bar]] = {}
    for b in sorted(bars_1m, key=lambda x: int(x.time)):
        dt = datetime.fromtimestamp(int(b.time), tz=NY)
        floored_min = (dt.minute // minutes) * minutes
        local = dt.replace(minute=floored_min, second=0, microsecond=0)
        key = int(local.timestamp())
        buckets.setdefault(key, []).append(b)
    out: list[Bar] = []
    for key in sorted(buckets):
        chunk = buckets[key]
        vol = 0.0
        has_vol = False
        for b in chunk:
            if b.volume is not None:
                vol += float(b.volume)
                has_vol = True
        out.append(
            Bar(
                time=key,
                open=float(chunk[0].open),
                high=max(float(b.high) for b in chunk),
                low=min(float(b.low) for b in chunk),
                close=float(chunk[-1].close),
                volume=vol if has_vol else None,
            )
        )
    return out


def list_nq_raw_symbols(
    provider: DatabentoHistoricalDataProvider,
    *,
    start: str,
    end: str,
) -> dict[str, Any]:
    import databento as db

    client = provider._client()
    cont = client.symbology.resolve(
        dataset=provider.dataset,
        symbols=["NQ.v.0", "NQ.c.0", "NQ.n.0"],
        stype_in=db.SType.CONTINUOUS,
        stype_out=db.SType.INSTRUMENT_ID,
        start_date=start[:10],
        end_date=end[:10],
    )
    result = cont.get("result") if isinstance(cont, dict) else getattr(cont, "result", {})
    instrument_ids: list[str] = []
    for _sym, entries in (result or {}).items():
        for ent in entries or []:
            s = ent.get("s") if isinstance(ent, dict) else None
            if s is not None and str(s) not in instrument_ids:
                instrument_ids.append(str(s))
    raw_symbols: list[str] = []
    if instrument_ids:
        raw_res = client.symbology.resolve(
            dataset=provider.dataset,
            symbols=instrument_ids,
            stype_in=db.SType.INSTRUMENT_ID,
            stype_out=db.SType.RAW_SYMBOL,
            start_date=start[:10],
            end_date=end[:10],
        )
        raw_map = raw_res.get("result") if isinstance(raw_res, dict) else getattr(raw_res, "result", {})
        pat = re.compile(r"^NQ[HMUZ]\d$")
        for _iid, entries in (raw_map or {}).items():
            for ent in entries or []:
                sym = ent.get("s") if isinstance(ent, dict) else None
                if sym and pat.match(str(sym)) and str(sym) not in raw_symbols:
                    raw_symbols.append(str(sym))
    month_rank = {m: i for i, m in enumerate(NQ_MONTH_CODES)}
    raw_symbols.sort(key=lambda s: (int(s[-1]), month_rank.get(s[2], 99), s))
    return {
        "ok": bool(raw_symbols),
        "raw_symbols": raw_symbols,
        "instrument_ids": instrument_ids,
        "method": "continuous_v0_c0_n0_to_instrument_id_to_raw_symbol",
    }


def _enrich_meta(path: Path, extra: dict[str, Any]) -> None:
    if not path.exists():
        return
    meta = json.loads(path.read_text(encoding="utf-8"))
    meta.update(extra)
    path.write_text(json.dumps(meta, indent=2, default=str), encoding="utf-8")


def fetch_and_stitch_nq(
    *,
    start: str = DEFAULT_START,
    end: str = DEFAULT_END,
    force: bool = False,
) -> dict[str, Any]:
    """
    Download NQ outright 1m OHLCV, stitch by daily volume crossover (18:00 NY),
    persist stitched 1m / 5m / 15m.
    """
    load_databento_credential()
    pf = databento_preflight()
    out: dict[str, Any] = {"preflight": pf, "ok": False}
    if not pf.get("ok"):
        out["error_code"] = pf.get("error_code")
        return out

    stitched_dir = DATA_ROOT / "stitched"
    stitched_1m_path = stitched_dir / "databento_NQ_stitched_1m.jsonl"
    if stitched_1m_path.exists() and not force:
        loaded = load_dataset("databento_NQ_stitched", "1m", root=stitched_dir)
        bars = list(loaded.get("bars") or [])
        loaded5 = load_dataset("databento_NQ_stitched", "5m", root=stitched_dir)
        loaded15 = load_dataset("databento_NQ_stitched", "15m", root=stitched_dir)
        out.update(
            {
                "ok": True,
                "reused": True,
                "bars_1m": len(bars),
                "bars_5m": len(loaded5.get("bars") or []),
                "bars_15m": len(loaded15.get("bars") or []),
                "path_1m": str(stitched_1m_path).replace("\\", "/"),
                "start": start,
                "end": end,
            }
        )
        return out

    provider = DatabentoHistoricalDataProvider(data_root=DATA_ROOT)
    disc = list_nq_raw_symbols(provider, start=start, end=end)
    out["discovery"] = disc
    raw_symbols = list(disc.get("raw_symbols") or [])
    if not raw_symbols:
        out["error_code"] = "NQ_SYMBOL_DISCOVERY_FAILED"
        return out

    out["cost_estimate"] = provider.estimate_cost(
        symbols=raw_symbols, start=start, end=end, schema="ohlcv-1m", stype_in="raw_symbol"
    )

    series_list: list[ContractSeries] = []
    contracts_meta: list[dict[str, Any]] = []
    cdir = DATA_ROOT / "contracts"
    cdir.mkdir(parents=True, exist_ok=True)

    for i, sym in enumerate(raw_symbols):
        print(f"fetching {sym} ({i+1}/{len(raw_symbols)})...", flush=True)
        res = provider.fetch_ohlcv_1m([sym], start=start, end=end, stype_in="raw_symbol")
        if res.errors or not res.bars:
            print(f"  skip {sym}: {res.errors}", flush=True)
            continue
        all_bars = list(res.bars)
        write_dataset(
            all_bars,
            symbol=f"databento_NQ_{sym}",
            timeframe="1m",
            root=cdir,
            source="databento:GLBX.MDP3",
            expected_period_sec=None,
        )
        series_list.append(
            ContractSeries(
                contract_symbol=sym,
                bars=tuple(all_bars),
                first_seen=int(all_bars[0].time),
                last_seen=int(all_bars[-1].time),
                exchange="GLBX",
                root="NQ",
            )
        )
        contracts_meta.append(
            {
                "contract_symbol": sym,
                "first_seen": int(all_bars[0].time),
                "last_seen": int(all_bars[-1].time),
                "bars_1m": len(all_bars),
                "tick_size": NQ_TICK,
            }
        )

    if not series_list:
        out["error_code"] = "NQ_NO_CONTRACT_BARS"
        return out

    have = {s.contract_symbol for s in series_list}
    order = [s for s in raw_symbols if s in have]
    rolls = decide_rolls(series_list, calendar_order=order)
    stitched_1m, _prov = stitch_contracts(series_list, rolls)
    artifacts = detect_roll_price_artifacts(stitched_1m, rolls, min_jump=40.0)
    bars_5m = aggregate_1m_to_ny(stitched_1m, 5)
    bars_15m = aggregate_1m_to_ny(stitched_1m, 15)

    stitched_dir.mkdir(parents=True, exist_ok=True)
    write_dataset(
        stitched_1m,
        symbol="databento_NQ_stitched",
        timeframe="1m",
        root=stitched_dir,
        source="databento:GLBX.MDP3:aitrade_volume_crossover_unadjusted",
        expected_period_sec=None,
    )
    write_dataset(
        bars_5m,
        symbol="databento_NQ_stitched",
        timeframe="5m",
        root=stitched_dir,
        source="databento:GLBX.MDP3:agg_ny_5m_from_stitched_1m",
        expected_period_sec=None,
    )
    write_dataset(
        bars_15m,
        symbol="databento_NQ_stitched",
        timeframe="15m",
        root=stitched_dir,
        source="databento:GLBX.MDP3:agg_ny_15m_from_stitched_1m",
        expected_period_sec=None,
    )
    _enrich_meta(
        stitched_dir / "databento_NQ_stitched_1m.meta.json",
        {
            "root": "NQ",
            "schema": "ohlcv-1m",
            "continuous_choice": "aitrade_volume_crossover_unadjusted",
            "roll_activate": "18:00 America/New_York",
            "volume_semantics": VOLUME_SEMANTICS,
            "volume_status": VOLUME_STATUS,
            "contracts": contracts_meta,
            "tick_size": NQ_TICK,
            "phase": 29,
            "period": f"{start} → {end}",
        },
    )
    _enrich_meta(
        stitched_dir / "databento_NQ_stitched_5m.meta.json",
        {"aggregated_from": "1m", "alignment": "America/New_York", "tick_size": NQ_TICK},
    )
    _enrich_meta(
        stitched_dir / "databento_NQ_stitched_15m.meta.json",
        {"aggregated_from": "1m", "alignment": "America/New_York", "tick_size": NQ_TICK},
    )

    with (stitched_dir / "rolls.jsonl").open("w", encoding="utf-8") as fh:
        for r in rolls:
            fh.write(json.dumps(r.to_dict()) + "\n")

    out.update(
        {
            "ok": True,
            "reused": False,
            "bars_1m": len(stitched_1m),
            "bars_5m": len(bars_5m),
            "bars_15m": len(bars_15m),
            "contracts": contracts_meta,
            "rolls": [r.to_dict() for r in rolls],
            "roll_artifacts": artifacts,
            "qa_1m": validate_bars_quality(stitched_1m),
            "qa_5m": validate_bars_quality(bars_5m),
            "qa_15m": validate_bars_quality(bars_15m),
            "path_1m": str(stitched_1m_path).replace("\\", "/"),
            "path_5m": str(stitched_dir / "databento_NQ_stitched_5m.jsonl").replace("\\", "/"),
            "path_15m": str(stitched_dir / "databento_NQ_stitched_15m.jsonl").replace("\\", "/"),
            "start": start,
            "end": end,
            "continuous_choice": "aitrade_volume_crossover_unadjusted",
            "roll_rule": "next_daily_volume_exceeds_current; activate 18:00 America/New_York",
        }
    )
    return out


if __name__ == "__main__":
    info = fetch_and_stitch_nq(force=False)
    print(json.dumps({k: info.get(k) for k in ("ok", "error_code", "bars_1m", "bars_5m", "bars_15m", "cost_estimate", "reused")}, indent=2, default=str))
