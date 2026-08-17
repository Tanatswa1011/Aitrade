"""Databento historical futures adapter (COMEX GC / GLBX.MDP3)."""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Optional, Sequence

from historical_data_provider import HistoricalCoverageMeta, HistoricalDataProvider, HistoricalDataset
from models import Bar

DEFAULT_DATASET = "GLBX.MDP3"
DEFAULT_SCHEMA_1M = "ohlcv-1m"
ROOT = "GC"
DATA_ROOT = Path("data") / "databento" / "GC"

# Documented by Databento OHLCV schema: volume = total volume traded in the interval
# (aggregated from trades). Not independently audited against CME official settlement volumes.
VOLUME_SEMANTICS = (
    "Databento GLBX.MDP3 ohlcv-1m volume = total trade volume aggregated over the 1m interval "
    "(CME MDP 3.0 electronic session trades). Units: contract lots traded. "
    "May differ from official venue settlement volumes that include block/OTC."
)
VOLUME_STATUS = "PROVIDER_DOCUMENTED_TRADE_VOLUME"


@dataclass(frozen=True)
class DatabentoHistoricalResult:
    bars: tuple[Bar, ...]
    provider: str = "databento"
    dataset: Optional[str] = None
    requested_symbol: str = "GC"
    resolved_symbol: Optional[str] = None
    symbology_type: Optional[str] = None
    schema: Optional[str] = None
    requested_start: Optional[int] = None
    requested_end: Optional[int] = None
    actual_start: Optional[int] = None
    actual_end: Optional[int] = None
    contracts: tuple[dict[str, Any], ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)
    warnings: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()

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


def databento_package_info() -> dict[str, Any]:
    try:
        import databento as db

        return {
            "databento_package_available": True,
            "databento_version": getattr(db, "__version__", "unknown"),
            "historical_client_available": hasattr(db, "Historical"),
            "dataset_glbx_mdp3": str(getattr(db.Dataset, "GLBX_MDP3", "GLBX.MDP3")),
            "schema_ohlcv_1m": "ohlcv-1m",
            "schema_ohlcv_5m_native": False,
            "note": "5m built by aggregating ohlcv-1m",
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "databento_package_available": False,
            "databento_version": None,
            "historical_client_available": False,
            "error": f"{type(exc).__name__}:{exc}",
        }


def load_databento_credential() -> dict[str, Any]:
    """Load .env without exposing secret values."""
    env_path = Path(".env")
    if env_path.exists():
        try:
            from dotenv import load_dotenv

            load_dotenv(env_path, override=False)
        except Exception:  # noqa: BLE001
            for line in env_path.read_text(encoding="utf-8", errors="ignore").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k, v = k.strip(), v.strip().strip('"').strip("'")
                if k and v and k not in os.environ:
                    os.environ[k] = v
    present = bool(os.environ.get("DATABENTO_API_KEY", "").strip())
    return {
        "credential_required": True,
        "credential_present": present,
        "credential_env_var": "DATABENTO_API_KEY",
        "loaded_dotenv": env_path.exists(),
    }


def databento_preflight() -> dict[str, Any]:
    pkg = databento_package_info()
    cred = load_databento_credential()
    out = {
        **pkg,
        **cred,
        "ok": bool(pkg.get("databento_package_available") and cred.get("credential_present")),
    }
    if not pkg.get("databento_package_available"):
        out["error_code"] = "DATABENTO_PACKAGE_MISSING"
    elif not cred.get("credential_present"):
        out["error_code"] = "DATABENTO_CREDENTIAL_REQUIRED"
    return out


def _ns_to_unix(ts_ns: int) -> int:
    # Databento timestamps are nanoseconds
    return int(ts_ns // 1_000_000_000)


def ohlcv_records_to_bars(records: Sequence[Any]) -> list[Bar]:
    bars: list[Bar] = []
    for rec in records:
        # DBN OHLCVMsg fields: hd.ts_event, open, high, low, close, volume
        ts = getattr(rec, "ts_event", None)
        if ts is None:
            continue
        if int(ts) > 10_000_000_000_000:  # ns
            t = _ns_to_unix(int(ts))
        else:
            t = int(ts)
        # Prices often fixed-point int; databento usually exposes pretty floats via .pretty_*
        o = getattr(rec, "pretty_open", None)
        h = getattr(rec, "pretty_high", None)
        l = getattr(rec, "pretty_low", None)
        c = getattr(rec, "pretty_close", None)
        if o is None:
            # fallback: scale raw ints if present
            scale = 1e-9
            o = float(getattr(rec, "open")) * scale
            h = float(getattr(rec, "high")) * scale
            l = float(getattr(rec, "low")) * scale
            c = float(getattr(rec, "close")) * scale
        vol = getattr(rec, "volume", None)
        bars.append(
            Bar(
                time=t,
                open=float(o),
                high=float(h),
                low=float(l),
                close=float(c),
                volume=None if vol is None else float(vol),
            )
        )
    bars.sort(key=lambda b: int(b.time))
    return _dedupe(bars)


def _dedupe(bars: Sequence[Bar]) -> list[Bar]:
    out: list[Bar] = []
    seen: set[int] = set()
    for b in bars:
        t = int(b.time)
        if t in seen:
            continue
        seen.add(t)
        out.append(b)
    return out


def aggregate_1m_to_5m(bars_1m: Sequence[Bar]) -> list[Bar]:
    """Deterministic UTC 5m buckets from 1m OHLCV (floor to 300s)."""
    buckets: dict[int, list[Bar]] = {}
    for b in sorted(bars_1m, key=lambda x: int(x.time)):
        key = (int(b.time) // 300) * 300
        buckets.setdefault(key, []).append(b)
    out: list[Bar] = []
    for key in sorted(buckets):
        chunk = buckets[key]
        if len(chunk) < 1:
            continue
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


def validate_bars_quality(bars: Sequence[Bar]) -> dict[str, Any]:
    """Deterministic bar QA — no interpolation."""
    ordered = sorted(bars, key=lambda b: int(b.time))
    dupes = 0
    seen: set[int] = set()
    ohlc_bad = 0
    neg_vol = 0
    unordered = 0
    prev_t: Optional[int] = None
    for b in ordered:
        t = int(b.time)
        if t in seen:
            dupes += 1
        seen.add(t)
        if prev_t is not None and t < prev_t:
            unordered += 1
        prev_t = t
        o, h, l, c = float(b.open), float(b.high), float(b.low), float(b.close)
        if not (h >= max(o, c) and l <= min(o, c) and h >= l):
            ohlc_bad += 1
        if b.volume is not None and float(b.volume) < 0:
            neg_vol += 1
    weekend = 0
    for b in ordered:
        wd = datetime.fromtimestamp(int(b.time), tz=timezone.utc).weekday()
        if wd >= 5:
            weekend += 1
    return {
        "bar_count": len(ordered),
        "duplicates": dupes,
        "ohlc_violations": ohlc_bad,
        "negative_volume": neg_vol,
        "unordered": unordered,
        "weekend_utc_bars": weekend,
        "ok": dupes == 0 and ohlc_bad == 0 and neg_vol == 0 and unordered == 0,
    }


class DatabentoHistoricalDataProvider(HistoricalDataProvider):
    """Fetch GLBX.MDP3 GC OHLCV via Databento Historical client."""

    name = "databento"

    def __init__(
        self,
        *,
        dataset: str = DEFAULT_DATASET,
        data_root: Path | str = DATA_ROOT,
        default_stype: str = "raw_symbol",
    ) -> None:
        self.dataset = dataset
        self.data_root = Path(data_root)
        self.default_stype = default_stype

    def preflight(self) -> dict[str, Any]:
        return databento_preflight()

    def fetch(
        self,
        symbol: str,
        timeframe: str,
        *,
        start_ts: Optional[int] = None,
        end_ts: Optional[int] = None,
    ) -> HistoricalDataset:
        """HistoricalDataProvider interface — 5m via 1m aggregation."""
        start = (
            datetime.fromtimestamp(int(start_ts), tz=timezone.utc).date().isoformat()
            if start_ts is not None
            else "2025-08-01"
        )
        end = (
            datetime.fromtimestamp(int(end_ts), tz=timezone.utc).date().isoformat()
            if end_ts is not None
            else date.today().isoformat()
        )
        res = self.fetch_5m([symbol], start=start, end=end, stype_in=self.default_stype)
        if res.errors:
            raise RuntimeError(";".join(res.errors))
        meta = HistoricalCoverageMeta(
            provider=self.name,
            symbol=symbol,
            timeframe=timeframe,
            source_symbol=str(res.resolved_symbol or symbol),
            timezone="UTC",
            price_precision=None,
            capture_timestamp=datetime.now(tz=timezone.utc).isoformat(),
            requested_start=res.requested_start,
            requested_end=res.requested_end,
            actual_start=res.actual_start,
            actual_end=res.actual_end,
            bar_count=len(res.bars),
            source=f"databento:{self.dataset}",
            extras={
                "dataset": res.dataset,
                "schema": res.schema,
                "symbology_type": res.symbology_type,
                "volume_semantics": (res.metadata or {}).get("volume_semantics"),
                "volume_status": (res.metadata or {}).get("volume_status"),
            },
        )
        return HistoricalDataset(bars=res.bars, meta=meta)

    def _client(self):
        pf = self.preflight()
        if not pf.get("ok"):
            raise RuntimeError(pf.get("error_code") or "DATABENTO_UNAVAILABLE")
        import databento as db

        return db.Historical()  # reads DATABENTO_API_KEY

    def list_gc_raw_symbols(
        self,
        *,
        start: str,
        end: str,
        parent: str = "GC.FUT",
    ) -> dict[str, Any]:
        """
        Discover GC outright raw symbols via continuous volume/calendar mappings
        (GLBX.MDP3 does not support parent→raw_symbol resolve).
        """
        import re

        client = self._client()
        try:
            import databento as db

            cont = client.symbology.resolve(
                dataset=self.dataset,
                symbols=["GC.v.0", "GC.c.0", "GC.n.0"],
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
                    dataset=self.dataset,
                    symbols=instrument_ids,
                    stype_in=db.SType.INSTRUMENT_ID,
                    stype_out=db.SType.RAW_SYMBOL,
                    start_date=start[:10],
                    end_date=end[:10],
                )
                raw_map = raw_res.get("result") if isinstance(raw_res, dict) else getattr(raw_res, "result", {})
                pat = re.compile(r"^GC[FGHJKMNQUVXZ]\d$")
                for _iid, entries in (raw_map or {}).items():
                    for ent in entries or []:
                        sym = ent.get("s") if isinstance(ent, dict) else None
                        if sym and pat.match(str(sym)) and str(sym) not in raw_symbols:
                            raw_symbols.append(str(sym))
            # Calendar-ish order by CME month code then year digit
            month_rank = {m: i for i, m in enumerate("FGHJKMNQUVXZ")}
            raw_symbols.sort(key=lambda s: (int(s[-1]), month_rank.get(s[2], 99), s))
            return {
                "ok": bool(raw_symbols),
                "parent": parent,
                "method": "continuous_v0_c0_n0_to_instrument_id_to_raw_symbol",
                "instrument_ids": instrument_ids,
                "raw_symbols": raw_symbols,
                "error": None if raw_symbols else "no_outright_raw_symbols_resolved",
            }
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"{type(exc).__name__}:{exc}", "raw_symbols": []}

    def estimate_cost(
        self,
        *,
        symbols: Sequence[str],
        start: str,
        end: str,
        schema: str = DEFAULT_SCHEMA_1M,
        stype_in: str = "raw_symbol",
    ) -> dict[str, Any]:
        client = self._client()
        try:
            cost = client.metadata.get_cost(
                dataset=self.dataset,
                symbols=list(symbols),
                schema=schema,
                start=start,
                end=end,
                stype_in=stype_in,
            )
            size = client.metadata.get_billable_size(
                dataset=self.dataset,
                symbols=list(symbols),
                schema=schema,
                start=start,
                end=end,
                stype_in=stype_in,
            )
            return {"ok": True, "cost": cost, "billable_size": size, "symbols": list(symbols)}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"{type(exc).__name__}:{exc}"}

    def fetch_ohlcv_1m(
        self,
        symbols: Sequence[str],
        *,
        start: str,
        end: str,
        stype_in: str = "raw_symbol",
        limit: Optional[int] = None,
    ) -> DatabentoHistoricalResult:
        pf = self.preflight()
        if not pf.get("ok"):
            return DatabentoHistoricalResult(
                bars=(),
                dataset=self.dataset,
                requested_symbol=",".join(symbols),
                symbology_type=stype_in,
                schema=DEFAULT_SCHEMA_1M,
                errors=(pf.get("error_code") or "DATABENTO_UNAVAILABLE",),
                metadata={"preflight": pf},
            )
        client = self._client()
        warnings: list[str] = []
        try:
            store = client.timeseries.get_range(
                dataset=self.dataset,
                symbols=list(symbols),
                schema=DEFAULT_SCHEMA_1M,
                start=start,
                end=end,
                stype_in=stype_in,
                limit=limit,
            )
            records = list(store)
            bars = ohlcv_records_to_bars(records)
        except Exception as exc:  # noqa: BLE001
            return DatabentoHistoricalResult(
                bars=(),
                dataset=self.dataset,
                requested_symbol=",".join(symbols),
                symbology_type=stype_in,
                schema=DEFAULT_SCHEMA_1M,
                errors=(f"{type(exc).__name__}:{exc}",),
                metadata={"preflight": pf},
            )

        return DatabentoHistoricalResult(
            bars=tuple(bars),
            dataset=self.dataset,
            requested_symbol=",".join(symbols),
            resolved_symbol=",".join(symbols),
            symbology_type=stype_in,
            schema=DEFAULT_SCHEMA_1M,
            requested_start=_date_to_ts(start),
            requested_end=_date_to_ts(end),
            actual_start=None if not bars else int(bars[0].time),
            actual_end=None if not bars else int(bars[-1].time),
            contracts=tuple({"contract_symbol": s, "root": ROOT} for s in symbols),
            metadata={
                "preflight": pf,
                "volume_semantics": VOLUME_SEMANTICS,
                "volume_status": VOLUME_STATUS,
                "native_timezone": "UTC",
                "record_count": len(records),
                "publisher_exchange": "CME Globex / COMEX (via GLBX.MDP3)",
            },
            warnings=tuple(warnings),
        )

    def fetch_5m(
        self,
        symbols: Sequence[str],
        *,
        start: str,
        end: str,
        stype_in: str = "raw_symbol",
        limit: Optional[int] = None,
    ) -> DatabentoHistoricalResult:
        raw = self.fetch_ohlcv_1m(symbols, start=start, end=end, stype_in=stype_in, limit=limit)
        if raw.errors:
            return raw
        bars_5m = aggregate_1m_to_5m(raw.bars)
        meta = dict(raw.metadata)
        meta["aggregated_from"] = DEFAULT_SCHEMA_1M
        meta["target_timeframe"] = "5m"
        return DatabentoHistoricalResult(
            bars=tuple(bars_5m),
            dataset=raw.dataset,
            requested_symbol=raw.requested_symbol,
            resolved_symbol=raw.resolved_symbol,
            symbology_type=raw.symbology_type,
            schema="ohlcv-5m(agg-from-1m)",
            requested_start=raw.requested_start,
            requested_end=raw.requested_end,
            actual_start=None if not bars_5m else int(bars_5m[0].time),
            actual_end=None if not bars_5m else int(bars_5m[-1].time),
            contracts=raw.contracts,
            metadata=meta,
            warnings=raw.warnings + ("5m aggregated from ohlcv-1m",),
            errors=(),
        )


def _date_to_ts(d: str) -> Optional[int]:
    try:
        # YYYY-MM-DD
        dt = datetime.fromisoformat(d[:10]).replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    except Exception:  # noqa: BLE001
        return None


def persist_contract_bars(
    bars: Sequence[Bar],
    *,
    contract: str,
    root: Path = DATA_ROOT,
    timeframe: str = "5m",
    extras: Optional[dict[str, Any]] = None,
) -> Path:
    from bar_dataset import write_dataset

    contracts_dir = Path(root) / "contracts"
    contracts_dir.mkdir(parents=True, exist_ok=True)
    # write_dataset uses symbol in filename
    result = write_dataset(
        list(bars),
        symbol=f"databento_GC_{contract}",
        timeframe=timeframe,
        source="databento:GLBX.MDP3",
        root=contracts_dir,
        expected_period_sec=300 if timeframe == "5m" else 60,
    )
    meta_path = Path(result["path"]).with_suffix(".meta.json")
    if meta_path.exists() and extras:
        import json

        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        meta.update(extras)
        meta_path.write_text(json.dumps(meta, indent=2, default=str), encoding="utf-8")
    return Path(result["path"])
