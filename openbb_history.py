"""OpenBB historical OHLC adapter (integration layer — not strategy logic)."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Optional, Sequence

from bar_dataset import dedupe_bars, load_dataset, write_dataset
from historical_data_provider import (
    HistoricalCoverageMeta,
    HistoricalDataProvider,
    HistoricalDataset,
)
from models import Bar
from timeframe import normalize_timeframe, timeframe_seconds


INTEGRATION_LAYER = "openbb"


@dataclass(frozen=True)
class HistoricalDataResult:
    """Canonical OpenBB fetch result — no raw OpenBB objects leak outward."""

    bars: tuple[Bar, ...]
    provider: str
    underlying_provider: Optional[str]
    requested_symbol: str
    source_symbol: str
    instrument_type: str
    timeframe: str
    requested_start: Optional[int]
    requested_end: Optional[int]
    actual_start: Optional[int]
    actual_end: Optional[int]
    metadata: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def to_dataset(self) -> HistoricalDataset:
        meta = HistoricalCoverageMeta(
            provider=self.provider,
            symbol=self.requested_symbol,
            timeframe=self.timeframe,
            source_symbol=self.source_symbol,
            timezone="UTC",
            price_precision=None,
            capture_timestamp=datetime.now(tz=timezone.utc).isoformat(),
            requested_start=self.requested_start,
            requested_end=self.requested_end,
            actual_start=self.actual_start,
            actual_end=self.actual_end,
            bar_count=len(self.bars),
            source=f"{INTEGRATION_LAYER}:{self.underlying_provider or 'unknown'}",
            extras={
                "integration_layer": INTEGRATION_LAYER,
                "underlying_provider": self.underlying_provider,
                "instrument_type": self.instrument_type,
                **self.metadata,
                "warnings": list(self.warnings),
                "errors": list(self.errors),
            },
        )
        return HistoricalDataset(bars=self.bars, meta=meta)

    def to_dict(self) -> dict[str, Any]:
        return {
            "bar_count": len(self.bars),
            "provider": self.provider,
            "underlying_provider": self.underlying_provider,
            "requested_symbol": self.requested_symbol,
            "source_symbol": self.source_symbol,
            "instrument_type": self.instrument_type,
            "timeframe": self.timeframe,
            "requested_start": self.requested_start,
            "requested_end": self.requested_end,
            "actual_start": self.actual_start,
            "actual_end": self.actual_end,
            "metadata": self.metadata,
            "warnings": list(self.warnings),
            "errors": list(self.errors),
        }


def openbb_version() -> Optional[str]:
    try:
        import importlib.metadata

        return importlib.metadata.version("openbb")
    except Exception:  # noqa: BLE001
        try:
            import openbb

            return getattr(openbb, "__version__", None)
        except Exception:  # noqa: BLE001
            return None


def inspect_openbb() -> dict[str, Any]:
    """Runtime capability inventory — no assumptions from docs alone."""
    out: dict[str, Any] = {
        "installed": False,
        "openbb_version": openbb_version(),
        "python_version": None,
        "extensions": [],
        "routes_relevant": [],
        "currency_history_providers": [],
        "credential_keys": [],
        "credentials_set": {},
        "errors": [],
    }
    import sys

    out["python_version"] = sys.version
    try:
        from openbb import obb
    except Exception as exc:  # noqa: BLE001
        out["errors"].append(f"import_failed:{exc}")
        return out

    out["installed"] = True
    try:
        out["extensions"] = sorted(
            [
                n
                for n in (
                    "openbb-currency",
                    "openbb-tiingo",
                    "openbb-yfinance",
                    "openbb-fmp",
                    "openbb-commodity",
                    "openbb-derivatives",
                    "openbb-economy",
                    "openbb-fred",
                    "openbb-federal-reserve",
                )
                if _pkg_version(n)
            ]
        )
        out["extension_versions"] = {n: _pkg_version(n) for n in out["extensions"]}
    except Exception as exc:  # noqa: BLE001
        out["errors"].append(f"extensions:{exc}")

    # Relevant routes from coverage
    try:
        cmds = list(obb.coverage.commands.keys()) if hasattr(obb.coverage, "commands") else []
        interesting = [
            c
            for c in cmds
            if any(
                k in c.lower()
                for k in (
                    "currency",
                    "commodity",
                    "futures",
                    "economy",
                    "fred",
                    "cpi",
                    "calendar",
                    "interest",
                    "federal",
                    "unemployment",
                )
            )
        ]
        out["routes_relevant"] = interesting[:120]
    except Exception as exc:  # noqa: BLE001
        out["errors"].append(f"routes:{exc}")

    # Currency historical providers from docstring / reference
    try:
        doc = obb.currency.price.historical.__doc__ or ""
        out["currency_historical_doc_excerpt"] = doc[:500]
        # Default priority mentioned in doc
        if "tiingo" in doc.lower():
            out["currency_history_providers"].append("tiingo")
        if "yfinance" in doc.lower():
            out["currency_history_providers"].append("yfinance")
        if "fmp" in doc.lower():
            out["currency_history_providers"].append("fmp")
        out["currency_history_providers"] = sorted(set(out["currency_history_providers"]))
    except Exception as exc:  # noqa: BLE001
        out["errors"].append(f"currency_doc:{exc}")

    try:
        creds = obb.user.credentials
        d = creds.model_dump() if hasattr(creds, "model_dump") else {}
        out["credential_keys"] = sorted(d.keys())
        out["credentials_set"] = {k: bool(v) for k, v in d.items()}
    except Exception as exc:  # noqa: BLE001
        out["errors"].append(f"credentials:{exc}")

    # Macro inventory (inspect only)
    out["macro_inventory"] = _macro_inventory(obb)
    out["openbb_mcp_note"] = (
        "Optional future integration only. Phase 16 uses Python OpenBB SDK "
        "for deterministic historical replay; TradingView MCP unchanged."
    )
    return out


def _pkg_version(name: str) -> Optional[str]:
    try:
        import importlib.metadata

        return importlib.metadata.version(name)
    except Exception:  # noqa: BLE001
        return None


def _macro_inventory(obb: Any) -> list[dict[str, Any]]:
    rows = []
    candidates = [
        (".economy.calendar", "economic calendar", ["tradingeconomics", "fmp"]),
        (".economy.cpi", "CPI", ["fred", "oecd"]),
        (".economy.unemployment", "NFP / employment proxy", ["fred", "oecd"]),
        (".economy.interest_rates", "interest rates", ["fred", "oecd"]),
        (".economy.fomc_documents", "Federal Reserve FOMC docs", ["federal_reserve"]),
        (".economy.pce", "PCE", ["fred"]),
        (".regulators.sec", "SEC regulators", ["sec"]),
    ]
    cmds = set()
    try:
        cmds = set(obb.coverage.commands.keys())
    except Exception:  # noqa: BLE001
        pass
    for route, label, providers in candidates:
        rows.append(
            {
                "label": label,
                "openbb_route": route,
                "available": route in cmds or route.lstrip(".") in {
                    c.lstrip(".") for c in cmds
                },
                "typical_providers": providers,
                "credentials": "provider-dependent",
                "frequency": "unknown_until_fetched",
                "note": "Inspect-only; not wired to strategy",
            }
        )
    # Direct attribute probes
    eco = getattr(obb, "economy", None)
    if eco is not None:
        for name in (
            "calendar",
            "cpi",
            "unemployment",
            "interest_rates",
            "fomc_documents",
            "pce",
        ):
            rows.append(
                {
                    "label": name,
                    "openbb_route": f"obb.economy.{name}",
                    "available": hasattr(eco, name),
                    "typical_providers": [],
                    "credentials": "provider-dependent",
                    "frequency": "unknown_until_fetched",
                }
            )
    return rows


def classify_instrument(symbol: str, route: str, underlying: Optional[str]) -> str:
    s = (symbol or "").upper().replace("=", "").replace("/", "")
    r = (route or "").lower()
    if "futures" in r or s in {"GC", "GCF", "MGC"} or s.endswith("F") and "GC" in s:
        return "futures"
    if "XAU" in s or "GOLD" in s:
        if "currency" in r:
            return "spot_fx_metals"
        return "spot_gold"
    if "currency" in r:
        return "spot_fx_metals"
    return "unknown"


def _ts_to_unix(value: Any) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return int(value.astimezone(timezone.utc).timestamp())
    if isinstance(value, date) and not isinstance(value, datetime):
        dt = datetime(value.year, value.month, value.day, tzinfo=timezone.utc)
        return int(dt.timestamp())
    # pandas Timestamp / string
    try:
        import pandas as pd

        ts = pd.Timestamp(value)
        if ts.tzinfo is None:
            ts = ts.tz_localize("UTC")
        else:
            ts = ts.tz_convert("UTC")
        return int(ts.timestamp())
    except Exception:  # noqa: BLE001
        return None


def _date_str(ts: Optional[int]) -> Optional[str]:
    if ts is None:
        return None
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).date().isoformat()


def normalize_openbb_rows(
    rows: Sequence[Any],
    *,
    underlying_provider: str,
    source_symbol: str,
    instrument_type: str,
) -> list[Bar]:
    """Map OpenBB result rows → canonical UTC Bar[]."""
    bars: list[Bar] = []
    for row in rows:
        if hasattr(row, "model_dump"):
            d = row.model_dump()
        elif isinstance(row, dict):
            d = row
        else:
            d = {
                "date": getattr(row, "date", None),
                "open": getattr(row, "open", None),
                "high": getattr(row, "high", None),
                "low": getattr(row, "low", None),
                "close": getattr(row, "close", None),
                "volume": getattr(row, "volume", None),
            }
        raw_ts = d.get("date") or d.get("datetime") or d.get("timestamp")
        t = _ts_to_unix(raw_ts)
        if t is None or d.get("open") is None:
            continue
        bars.append(
            Bar(
                time=int(t),
                open=float(d["open"]),
                high=float(d["high"]),
                low=float(d["low"]),
                close=float(d["close"]),
                volume=None if d.get("volume") is None else float(d["volume"]),
            )
        )
    return dedupe_bars(bars)


class OpenBBHistoricalDataProvider(HistoricalDataProvider):
    """
    OpenBB integration-layer provider.

    Live TradingView analysis must remain isolated — failures here must not
    break chart CDP paths.
    """

    name = "openbb"

    # Credential env / OpenBB user.credentials mapping
    CREDENTIAL_ENV = {
        "tiingo": ("tiingo_token", "TIINGO_TOKEN", "TIINGO_API_KEY"),
        "fmp": ("fmp_api_key", "FMP_API_KEY"),
        "fred": ("fred_api_key", "FRED_API_KEY"),
    }

    def __init__(
        self,
        *,
        underlying_provider: Optional[str] = None,
        route: str = "currency",  # currency | futures | equity
        data_root: Path | str = Path("data") / "openbb",
    ) -> None:
        self.underlying_provider = underlying_provider
        self.route = route
        self.data_root = Path(data_root)

    def credential_status(self, underlying: Optional[str] = None) -> dict[str, Any]:
        u = (underlying or self.underlying_provider or "").lower()
        keys = self.CREDENTIAL_ENV.get(u, ())
        if not keys:
            return {
                "credential_required": False,
                "underlying_provider": u or None,
                "note": "yfinance typically needs no API key",
            }
        openbb_key = keys[0]
        env_keys = list(keys[1:]) if len(keys) > 1 else []
        set_via = None
        # env first
        for ek in env_keys:
            if os.environ.get(ek):
                set_via = f"env:{ek}"
                break
        # openbb credentials
        try:
            from openbb import obb

            creds = obb.user.credentials.model_dump()
            if creds.get(openbb_key):
                set_via = set_via or f"openbb.user.credentials.{openbb_key}"
        except Exception:  # noqa: BLE001
            pass
        return {
            "credential_required": True,
            "credential_key": openbb_key,
            "environment_variable_names": env_keys,
            "configuration_mechanism": (
                "Set env var or `obb.user.credentials.<key>` / OpenBB user settings. "
                "Never commit secrets."
            ),
            "present": bool(set_via),
            "present_via": set_via,
            "underlying_provider": u,
        }

    def fetch(
        self,
        symbol: str,
        timeframe: str,
        *,
        start_ts: Optional[int] = None,
        end_ts: Optional[int] = None,
    ) -> HistoricalDataset:
        result = self.fetch_result(
            symbol,
            timeframe,
            start_ts=start_ts,
            end_ts=end_ts,
            underlying_provider=self.underlying_provider,
            route=self.route,
        )
        if result.errors and not result.bars:
            raise RuntimeError("; ".join(result.errors))
        return result.to_dataset()

    def fetch_result(
        self,
        symbol: str,
        timeframe: str,
        *,
        start_ts: Optional[int] = None,
        end_ts: Optional[int] = None,
        underlying_provider: Optional[str] = None,
        route: Optional[str] = None,
    ) -> HistoricalDataResult:
        tf = normalize_timeframe(timeframe) or timeframe
        underlying = (underlying_provider or self.underlying_provider or "yfinance").lower()
        use_route = (route or self.route or "currency").lower()
        warnings: list[str] = []
        errors: list[str] = []
        meta: dict[str, Any] = {
            "integration_layer": INTEGRATION_LAYER,
            "openbb_version": openbb_version(),
            "route": use_route,
        }

        # Inject env credentials into OpenBB if present (no hardcoding)
        self._sync_env_credentials(underlying)

        cred = self.credential_status(underlying)
        meta["credential_status"] = cred
        if cred.get("credential_required") and not cred.get("present"):
            return HistoricalDataResult(
                bars=(),
                provider=self.name,
                underlying_provider=underlying,
                requested_symbol=symbol,
                source_symbol=symbol,
                instrument_type=classify_instrument(symbol, use_route, underlying),
                timeframe=tf,
                requested_start=start_ts,
                requested_end=end_ts,
                actual_start=None,
                actual_end=None,
                metadata=meta,
                warnings=warnings,
                errors=[
                    f"missing_credential:{cred.get('credential_key')}",
                    str(cred.get("configuration_mechanism")),
                ],
            )

        try:
            from openbb import obb
        except Exception as exc:  # noqa: BLE001
            return HistoricalDataResult(
                bars=(),
                provider=self.name,
                underlying_provider=underlying,
                requested_symbol=symbol,
                source_symbol=symbol,
                instrument_type="unknown",
                timeframe=tf,
                requested_start=start_ts,
                requested_end=end_ts,
                actual_start=None,
                actual_end=None,
                metadata=meta,
                errors=[f"openbb_unavailable:{exc}"],
            )

        start_d = _date_str(start_ts)
        end_d = _date_str(end_ts)
        interval = self._map_interval(tf)
        source_symbol = symbol
        instrument_type = classify_instrument(symbol, use_route, underlying)

        try:
            if use_route == "currency":
                resp = obb.currency.price.historical(
                    symbol=symbol,
                    provider=underlying,
                    start_date=start_d,
                    end_date=end_d,
                    interval=interval,
                )
            elif use_route == "futures":
                instrument_type = "futures"
                resp = obb.derivatives.futures.historical(
                    symbol=symbol,
                    provider=underlying,
                    start_date=start_d,
                    end_date=end_d,
                    interval=interval,
                )
            elif use_route == "equity":
                resp = obb.equity.price.historical(
                    symbol=symbol,
                    provider=underlying,
                    start_date=start_d,
                    end_date=end_d,
                    interval=interval,
                )
            else:
                return HistoricalDataResult(
                    bars=(),
                    provider=self.name,
                    underlying_provider=underlying,
                    requested_symbol=symbol,
                    source_symbol=symbol,
                    instrument_type=instrument_type,
                    timeframe=tf,
                    requested_start=start_ts,
                    requested_end=end_ts,
                    actual_start=None,
                    actual_end=None,
                    metadata=meta,
                    errors=[f"unsupported_route:{use_route}"],
                )
        except Exception as exc:  # noqa: BLE001
            return HistoricalDataResult(
                bars=(),
                provider=self.name,
                underlying_provider=underlying,
                requested_symbol=symbol,
                source_symbol=symbol,
                instrument_type=instrument_type,
                timeframe=tf,
                requested_start=start_ts,
                requested_end=end_ts,
                actual_start=None,
                actual_end=None,
                metadata=meta,
                errors=[f"{type(exc).__name__}:{exc}"],
            )

        rows = list(getattr(resp, "results", None) or [])
        # Detect provider-mutated symbol (e.g. yfinance appends =X)
        if hasattr(resp, "provider"):
            meta["openbb_response_provider"] = str(resp.provider)
        bars = normalize_openbb_rows(
            rows,
            underlying_provider=underlying,
            source_symbol=source_symbol,
            instrument_type=instrument_type,
        )
        if start_ts is not None or end_ts is not None:
            filtered = []
            for b in bars:
                if start_ts is not None and int(b.time) < int(start_ts):
                    continue
                if end_ts is not None and int(b.time) > int(end_ts):
                    continue
                filtered.append(b)
            bars = filtered

        if not bars:
            errors.append("empty_provider_response")

        # Futures hard warning
        if instrument_type == "futures":
            warnings.append(
                "FUTURES_NOT_OANDA_SPOT: must not pass XAUUSD feed-equivalence gate "
                "merely due to price correlation."
            )

        actual_start = None if not bars else int(bars[0].time)
        actual_end = None if not bars else int(bars[-1].time)
        meta["provider_native_timezone"] = "UTC_normalized"
        meta["bar_extras_template"] = {
            "source": INTEGRATION_LAYER,
            "underlying_provider": underlying,
            "source_symbol": source_symbol,
            "instrument_type": instrument_type,
        }
        return HistoricalDataResult(
            bars=tuple(bars),
            provider=self.name,
            underlying_provider=underlying,
            requested_symbol=symbol,
            source_symbol=source_symbol,
            instrument_type=instrument_type,
            timeframe=tf,
            requested_start=start_ts,
            requested_end=end_ts,
            actual_start=actual_start,
            actual_end=actual_end,
            metadata=meta,
            warnings=warnings,
            errors=errors,
        )

    def persist_result(
        self,
        result: HistoricalDataResult,
        *,
        root: Optional[Path] = None,
    ) -> dict[str, Any]:
        """Write under data/openbb/<underlying>/ — never overwrite TV OANDA files."""
        underlying = result.underlying_provider or "unknown"
        base = Path(root) if root else self.data_root / underlying
        base.mkdir(parents=True, exist_ok=True)
        sym = result.source_symbol.replace(":", "_").replace("/", "_").replace("=", "")
        tf = result.timeframe
        # Must read/write the same path write_dataset uses (openbb_<underlying>_<sym>_<tf>).
        dataset_symbol = f"openbb_{underlying}_{sym}"
        existing_loaded = load_dataset(dataset_symbol, tf, root=base)
        existing: list[Bar] = list(existing_loaded.get("bars") or [])
        merged = dedupe_bars(list(existing) + list(result.bars))
        period = timeframe_seconds(tf)
        written = write_dataset(
            merged,
            symbol=dataset_symbol,
            timeframe=tf,
            source=f"openbb:{underlying}",
            root=base,
            expected_period_sec=period,
        )
        # Enrich sidecar
        meta_path = Path(str(written["path"])).with_suffix(".meta.json")
        side = {
            **(written.get("meta") or {}),
            "aitrade_data_provider": self.name,
            "integration_layer": INTEGRATION_LAYER,
            "underlying_provider": underlying,
            "requested_symbol": result.requested_symbol,
            "provider_symbol": result.source_symbol,
            "instrument_type": result.instrument_type,
            "requested_timeframe": result.timeframe,
            "actual_timeframe": result.timeframe,
            "requested_start": result.requested_start,
            "requested_end": result.requested_end,
            "actual_start": result.actual_start if result.bars else (
                None if not merged else int(merged[0].time)
            ),
            "actual_end": result.actual_end if result.bars else (
                None if not merged else int(merged[-1].time)
            ),
            "bar_count": len(merged),
            "openbb_version": openbb_version(),
            "download_timestamp": datetime.now(tz=timezone.utc).isoformat(),
            "warnings": list(result.warnings),
            "errors": list(result.errors),
        }
        meta_path.write_text(json.dumps(side, indent=2), encoding="utf-8")
        return {"ok": True, "path": written.get("path"), "meta_path": str(meta_path), "bar_count": len(merged)}

    def fetch_chunked(
        self,
        symbol: str,
        timeframe: str,
        *,
        start_ts: int,
        end_ts: int,
        chunk_days: int = 30,
        underlying_provider: Optional[str] = None,
        route: Optional[str] = None,
        persist: bool = True,
    ) -> HistoricalDataResult:
        """Fetch overlapping chunks, dedupe timestamps, optionally persist."""
        from datetime import timedelta

        chunk = max(1, int(chunk_days))
        cursor = int(start_ts)
        end = int(end_ts)
        all_bars: list[Bar] = []
        warnings: list[str] = []
        errors: list[str] = []
        last_meta: dict[str, Any] = {}
        underlying = underlying_provider or self.underlying_provider
        use_route = route or self.route
        instrument_type = "unknown"
        source_symbol = symbol

        while cursor < end:
            chunk_end = min(
                end,
                int(
                    (
                        datetime.fromtimestamp(cursor, tz=timezone.utc)
                        + timedelta(days=chunk)
                    ).timestamp()
                ),
            )
            part = self.fetch_result(
                symbol,
                timeframe,
                start_ts=cursor,
                end_ts=chunk_end,
                underlying_provider=underlying,
                route=use_route,
            )
            warnings.extend(part.warnings)
            errors.extend(part.errors)
            all_bars.extend(part.bars)
            last_meta = part.metadata
            instrument_type = part.instrument_type
            source_symbol = part.source_symbol
            # advance; small overlap handled by dedupe — persist once after merge
            cursor = chunk_end if chunk_end > cursor else cursor + chunk * 86400

        merged = dedupe_bars(all_bars)
        result = HistoricalDataResult(
            bars=tuple(merged),
            provider=self.name,
            underlying_provider=underlying,
            requested_symbol=symbol,
            source_symbol=source_symbol,
            instrument_type=instrument_type,
            timeframe=normalize_timeframe(timeframe) or timeframe,
            requested_start=start_ts,
            requested_end=end_ts,
            actual_start=None if not merged else int(merged[0].time),
            actual_end=None if not merged else int(merged[-1].time),
            metadata={**last_meta, "chunk_days": chunk, "chunked": True},
            warnings=list(dict.fromkeys(warnings)),
            errors=list(dict.fromkeys(errors)),
        )
        if persist and merged:
            self.persist_result(result)
        return result

    @staticmethod
    def _map_interval(tf: str) -> str:
        t = normalize_timeframe(tf) or tf
        mapping = {
            "1m": "1m",
            "5m": "5m",
            "15m": "15m",
            "1H": "1h",
            "4H": "4h",
            "1D": "1d",
        }
        return mapping.get(t, t.lower() if isinstance(t, str) else "5m")

    def _sync_env_credentials(self, underlying: str) -> None:
        keys = self.CREDENTIAL_ENV.get(underlying.lower(), ())
        if len(keys) < 2:
            return
        openbb_key = keys[0]
        for ek in keys[1:]:
            val = os.environ.get(ek)
            if not val:
                continue
            try:
                from openbb import obb

                # setattr on credentials model if supported
                if hasattr(obb.user.credentials, openbb_key):
                    setattr(obb.user.credentials, openbb_key, val)
            except Exception:  # noqa: BLE001
                pass
            break


def probe_xauusd_symbols(
    *,
    underlying: str,
    route: str = "currency",
    start_ts: Optional[int] = None,
    end_ts: Optional[int] = None,
) -> list[dict[str, Any]]:
    """Test only appropriate XAUUSD candidate forms — no unrelated instruments."""
    candidates = ["XAUUSD", "XAU/USD"]
    if route == "futures":
        # Documented research only — not normal XAUUSD gate
        candidates = ["GC", "GC=F"]
    prov = OpenBBHistoricalDataProvider(underlying_provider=underlying, route=route)
    rows = []
    for sym in candidates:
        res = prov.fetch_result(
            sym,
            "5m",
            start_ts=start_ts,
            end_ts=end_ts,
            underlying_provider=underlying,
            route=route,
        )
        rows.append(
            {
                "candidate_symbol": sym,
                "provider": underlying,
                "route": route,
                "accepted": bool(res.bars) and not res.errors,
                "rejected": bool(res.errors) or not res.bars,
                "returned_instrument": res.source_symbol,
                "instrument_type": res.instrument_type,
                "bar_count": len(res.bars),
                "errors": list(res.errors),
                "warnings": list(res.warnings),
                "credential_status": {
                    "credential_required": bool(
                        (res.metadata.get("credential_status") or {}).get(
                            "credential_required"
                        )
                    ),
                    "credential_present": bool(
                        (res.metadata.get("credential_status") or {}).get("present")
                    ),
                    "credential_key": (res.metadata.get("credential_status") or {}).get(
                        "credential_key"
                    ),
                },
            }
        )
    return rows


def load_dotenv_credentials() -> dict[str, bool]:
    """Load `.env` if present. Returns credential-name presence only — never values."""
    before = {
        "TIINGO_TOKEN": bool(
            os.environ.get("TIINGO_TOKEN") or os.environ.get("TIINGO_API_KEY")
        ),
        "FMP_API_KEY": bool(os.environ.get("FMP_API_KEY")),
        "DATABENTO_API_KEY": bool(os.environ.get("DATABENTO_API_KEY")),
    }
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
    after = {
        "TIINGO_TOKEN": bool(
            os.environ.get("TIINGO_TOKEN") or os.environ.get("TIINGO_API_KEY")
        ),
        "FMP_API_KEY": bool(os.environ.get("FMP_API_KEY")),
        "DATABENTO_API_KEY": bool(os.environ.get("DATABENTO_API_KEY")),
    }
    return {
        **after,
        "loaded_dotenv": env_path.exists(),
        "changed": before != after,
    }


def provider_preflight(underlying: str, *, route: str = "currency") -> dict[str, Any]:
    """Preflight: version / extension / credential present / route — no secrets."""
    load_dotenv_credentials()
    prov = OpenBBHistoricalDataProvider(underlying_provider=underlying, route=route)
    # Re-sync after dotenv
    prov._sync_env_credentials(underlying)
    cred = prov.credential_status(underlying)
    route_name = (
        "obb.derivatives.futures.historical"
        if route == "futures"
        else "obb.currency.price.historical"
    )
    route_ok = False
    try:
        from openbb import obb

        if route == "futures":
            route_ok = hasattr(obb, "derivatives") and hasattr(
                obb.derivatives, "futures"
            )
        else:
            route_ok = hasattr(obb, "currency") and hasattr(obb.currency, "price")
    except Exception as exc:  # noqa: BLE001
        return {
            "openbb_version": openbb_version(),
            "provider": underlying,
            "extension_installed": False,
            "credential_required": bool(cred.get("credential_required")),
            "credential_present": bool(cred.get("present")),
            "route_available": False,
            "route": route_name,
            "ok": False,
            "error": f"openbb_import:{type(exc).__name__}",
        }

    pkg = {
        "tiingo": "openbb-tiingo",
        "fmp": "openbb-fmp",
        "yfinance": "openbb-yfinance",
    }.get(underlying.lower())
    ext_ver = None
    if pkg:
        try:
            import importlib.metadata

            ext_ver = importlib.metadata.version(pkg)
        except Exception:  # noqa: BLE001
            ext_ver = None

    ok = bool(route_ok) and (
        not cred.get("credential_required") or bool(cred.get("present"))
    )
    return {
        "openbb_version": openbb_version(),
        "provider": underlying,
        "extension_installed": bool(ext_ver),
        "extension_version": ext_ver,
        "credential_required": bool(cred.get("credential_required")),
        "credential_present": bool(cred.get("present")),
        "credential_key": cred.get("credential_key"),
        "environment_variable_names": cred.get("environment_variable_names"),
        "route_available": bool(route_ok),
        "route": route_name,
        "ok": ok,
    }