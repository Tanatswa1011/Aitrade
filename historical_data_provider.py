"""Historical OHLC provider abstraction (replay-agnostic)."""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, Sequence

from bar_dataset import DatasetMeta, load_dataset, validate_bars, write_dataset
from gap_classify import classify_bar_gaps
from models import Bar
from timeframe import normalize_timeframe, timeframe_seconds
from trading_day_config import DEFAULT_TRADING_DAY_CONFIG, TradingDayConfig


@dataclass(frozen=True)
class HistoricalCoverageMeta:
    """Provenance + coverage for one historical dataset."""

    provider: str
    symbol: str
    timeframe: str
    source_symbol: str
    timezone: str
    price_precision: Optional[int]
    capture_timestamp: str
    requested_start: Optional[int]
    requested_end: Optional[int]
    actual_start: Optional[int]
    actual_end: Optional[int]
    bar_count: int
    source: str = "unknown"
    extras: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class HistoricalDataset:
    bars: tuple[Bar, ...]
    meta: HistoricalCoverageMeta

    def to_dict(self) -> dict[str, Any]:
        return {
            "bars": [
                {
                    "time": int(b.time),
                    "open": float(b.open),
                    "high": float(b.high),
                    "low": float(b.low),
                    "close": float(b.close),
                    "volume": b.volume,
                }
                for b in self.bars
            ],
            "meta": self.meta.to_dict(),
        }


class HistoricalDataProvider(ABC):
    """Fetch canonical Bar[] + coverage metadata. Replay must not care which provider."""

    name: str = "base"

    @abstractmethod
    def fetch(
        self,
        symbol: str,
        timeframe: str,
        *,
        start_ts: Optional[int] = None,
        end_ts: Optional[int] = None,
    ) -> HistoricalDataset:
        raise NotImplementedError

    def persist(
        self,
        dataset: HistoricalDataset,
        *,
        root: Path = Path("data"),
    ) -> dict[str, Any]:
        """Write JSONL + enriched metadata sidecar (source identity retained)."""
        tf = normalize_timeframe(dataset.meta.timeframe) or dataset.meta.timeframe
        period = timeframe_seconds(tf)
        written = write_dataset(
            list(dataset.bars),
            symbol=dataset.meta.symbol,
            timeframe=tf,
            source=dataset.meta.source,
            root=root,
            expected_period_sec=period,
        )
        # Enrich sidecar with full provider provenance (do not drop DatasetMeta fields).
        mp = Path(written["meta"]["extras"]["path"]).with_suffix(".meta.json")
        if not mp.exists():
            from bar_dataset import meta_path

            mp = meta_path(dataset.meta.symbol, tf, root=root)
        side = {
            **written.get("meta", {}),
            "provider_meta": dataset.meta.to_dict(),
            "provider": dataset.meta.provider,
            "source_symbol": dataset.meta.source_symbol,
            "timezone": dataset.meta.timezone,
            "price_precision": dataset.meta.price_precision,
            "requested_start": dataset.meta.requested_start,
            "requested_end": dataset.meta.requested_end,
            "actual_start": dataset.meta.actual_start,
            "actual_end": dataset.meta.actual_end,
        }
        mp.write_text(json.dumps(side, indent=2), encoding="utf-8")
        return {"ok": True, "path": written.get("path"), "meta_path": str(mp), "meta": side}


def _filter_range(
    bars: Sequence[Bar],
    start_ts: Optional[int],
    end_ts: Optional[int],
) -> list[Bar]:
    out = []
    for b in sorted(bars, key=lambda x: int(x.time)):
        t = int(b.time)
        if start_ts is not None and t < int(start_ts):
            continue
        if end_ts is not None and t > int(end_ts):
            continue
        out.append(b)
    return out


def _infer_precision(bars: Sequence[Bar]) -> Optional[int]:
    if not bars:
        return None
    max_dec = 0
    for b in bars[:50]:
        for v in (b.open, b.high, b.low, b.close):
            s = f"{float(v):.10f}".rstrip("0")
            if "." in s:
                max_dec = max(max_dec, len(s.split(".")[1]))
    return max_dec


class LocalJsonlProvider(HistoricalDataProvider):
    """Load previously persisted TradingView/OANDA captures from disk."""

    name = "local_jsonl"

    def __init__(self, root: Path | str = Path("data")) -> None:
        self.root = Path(root)

    def fetch(
        self,
        symbol: str,
        timeframe: str,
        *,
        start_ts: Optional[int] = None,
        end_ts: Optional[int] = None,
    ) -> HistoricalDataset:
        loaded = load_dataset(symbol, timeframe, root=self.root)
        if not loaded.get("ok"):
            raise FileNotFoundError(loaded.get("error") or "missing_dataset")
        bars = _filter_range(loaded["bars"], start_ts, end_ts)
        file_meta = loaded.get("meta") or {}
        capture = file_meta.get("capture_time") or datetime.now(tz=timezone.utc).isoformat()
        source = str(file_meta.get("source") or "local_jsonl")
        tf = normalize_timeframe(timeframe) or timeframe
        meta = HistoricalCoverageMeta(
            provider=self.name,
            symbol=symbol,
            timeframe=tf,
            source_symbol=str(file_meta.get("symbol") or symbol),
            timezone="UTC",
            price_precision=_infer_precision(bars),
            capture_timestamp=str(capture),
            requested_start=start_ts,
            requested_end=end_ts,
            actual_start=None if not bars else int(bars[0].time),
            actual_end=None if not bars else int(bars[-1].time),
            bar_count=len(bars),
            source=source,
            extras={
                "path": loaded.get("path"),
                "file_meta": {k: v for k, v in file_meta.items() if k != "extras"},
            },
        )
        return HistoricalDataset(bars=tuple(bars), meta=meta)


class TradingViewDesktopProvider(HistoricalDataProvider):
    """
    TradingView Desktop series.data() path (typically ~300 bars).

    Offline: falls back to LocalJsonlProvider for previously captured series.
    """

    name = "tradingview_desktop"

    def __init__(
        self,
        *,
        root: Path | str = Path("data"),
        allow_live: bool = False,
    ) -> None:
        self.root = Path(root)
        self.allow_live = allow_live
        self._local = LocalJsonlProvider(self.root)

    def fetch(
        self,
        symbol: str,
        timeframe: str,
        *,
        start_ts: Optional[int] = None,
        end_ts: Optional[int] = None,
    ) -> HistoricalDataset:
        # Prefer live only when explicitly enabled; Phase 15 offline path uses disk.
        if self.allow_live:
            try:
                live = self._fetch_live(symbol, timeframe)
                bars = _filter_range(live, start_ts, end_ts)
                tf = normalize_timeframe(timeframe) or timeframe
                meta = HistoricalCoverageMeta(
                    provider=self.name,
                    symbol=symbol,
                    timeframe=tf,
                    source_symbol=symbol,
                    timezone="UTC",
                    price_precision=_infer_precision(bars),
                    capture_timestamp=datetime.now(tz=timezone.utc).isoformat(),
                    requested_start=start_ts,
                    requested_end=end_ts,
                    actual_start=None if not bars else int(bars[0].time),
                    actual_end=None if not bars else int(bars[-1].time),
                    bar_count=len(bars),
                    source="tradingview_native",
                    extras={"live": True, "approx_ceiling_bars": 300},
                )
                return HistoricalDataset(bars=tuple(bars), meta=meta)
            except Exception as exc:  # noqa: BLE001 — fall back to local
                pass
        ds = self._local.fetch(symbol, timeframe, start_ts=start_ts, end_ts=end_ts)
        m = ds.meta
        return HistoricalDataset(
            bars=ds.bars,
            meta=HistoricalCoverageMeta(
                provider=self.name,
                symbol=m.symbol,
                timeframe=m.timeframe,
                source_symbol=m.source_symbol,
                timezone=m.timezone,
                price_precision=m.price_precision,
                capture_timestamp=m.capture_timestamp,
                requested_start=start_ts,
                requested_end=end_ts,
                actual_start=m.actual_start,
                actual_end=m.actual_end,
                bar_count=m.bar_count,
                source=m.source,
                extras={**m.extras, "via": "local_jsonl_fallback"},
            ),
        )

    def _fetch_live(self, symbol: str, timeframe: str) -> list[Bar]:
        import asyncio

        from bars import fetch_bars
        from chart_symbol import get_chart_symbol, set_chart_symbol
        from chart_timeframe import get_chart_resolution, set_chart_resolution

        async def _run() -> list[Bar]:
            prev_sym = await get_chart_symbol()
            prev_tf = await get_chart_resolution()
            try:
                await set_chart_symbol(symbol)
                await set_chart_resolution(timeframe)
                payload = await fetch_bars()
                raw = payload.get("bars") or []
                return [
                    Bar(
                        time=int(b["time"]),
                        open=float(b["open"]),
                        high=float(b["high"]),
                        low=float(b["low"]),
                        close=float(b["close"]),
                        volume=b.get("volume"),
                    )
                    for b in raw
                ]
            finally:
                if prev_tf.get("resolution"):
                    await set_chart_resolution(prev_tf["resolution"])
                if prev_sym.get("symbol"):
                    await set_chart_symbol(prev_sym["symbol"])

        return asyncio.get_event_loop().run_until_complete(_run())


class TradingViewHistoryProvider(HistoricalDataProvider):
    """
    Attempt TradingView authenticated/session deeper-history mechanisms.

    Phase 14/15 audit: Desktop series window remains ~300 bars; requestMoreData /
    loadDataTo / setInitialRequestOptions do not expand usable history.
    This provider documents the attempt and returns the desktop ceiling dataset
    with explicit limitation metadata — it does not invent bars.
    """

    name = "tradingview_history"

    def __init__(self, desktop: Optional[TradingViewDesktopProvider] = None) -> None:
        self.desktop = desktop or TradingViewDesktopProvider(allow_live=False)

    def fetch(
        self,
        symbol: str,
        timeframe: str,
        *,
        start_ts: Optional[int] = None,
        end_ts: Optional[int] = None,
    ) -> HistoricalDataset:
        ds = self.desktop.fetch(symbol, timeframe, start_ts=start_ts, end_ts=end_ts)
        extras = {
            **ds.meta.extras,
            "deeper_history_available": False,
            "limitation": (
                "TradingView Desktop series.data() capped near ~300 bars; "
                "CDP requestMoreData / loadDataTo / setVisibleRange / "
                "setInitialRequestOptions({count:5000}) did not expand usable history."
            ),
            "status": "desktop_ceiling_only",
        }
        meta = HistoricalCoverageMeta(
            provider=self.name,
            symbol=ds.meta.symbol,
            timeframe=ds.meta.timeframe,
            source_symbol=ds.meta.source_symbol,
            timezone=ds.meta.timezone,
            price_precision=ds.meta.price_precision,
            capture_timestamp=ds.meta.capture_timestamp,
            requested_start=start_ts,
            requested_end=end_ts,
            actual_start=ds.meta.actual_start,
            actual_end=ds.meta.actual_end,
            bar_count=ds.meta.bar_count,
            source=ds.meta.source,
            extras=extras,
        )
        return HistoricalDataset(bars=ds.bars, meta=meta)


def integrity_report(
    dataset: HistoricalDataset,
    *,
    trading_day: Optional[TradingDayConfig] = None,
) -> dict[str, Any]:
    tf = dataset.meta.timeframe
    period = timeframe_seconds(tf)
    base = validate_bars(
        list(dataset.bars),
        symbol=dataset.meta.symbol,
        timeframe=tf,
        expected_period_sec=period,
    )
    gaps = (
        classify_bar_gaps(
            list(dataset.bars),
            expected_period_sec=period,
            trading_day=trading_day or DEFAULT_TRADING_DAY_CONFIG,
        )
        if period
        else {"gap_count": 0, "quality_ok": True, "by_category": {}, "unexpected_count": 0}
    )
    return {
        **base,
        "provider": dataset.meta.provider,
        "source_symbol": dataset.meta.source_symbol,
        "gap_classification": gaps,
        "integrity_ok": bool(base.get("ok")) and bool(gaps.get("quality_ok")),
    }


class LocalDatasetProvider(LocalJsonlProvider):
    """Alias for LocalJsonlProvider (Phase 16 naming)."""

    name = "local_dataset"


# Optional OpenBB import — must not break live TV if OpenBB missing.
try:
    from openbb_history import OpenBBHistoricalDataProvider as OpenBBHistoricalDataProvider
except Exception:  # noqa: BLE001
    OpenBBHistoricalDataProvider = None  # type: ignore[misc, assignment]


# Documented external options — NOT integrated without explicit approval.
EXTERNAL_HISTORY_OPTIONS: list[dict[str, Any]] = [
    {
        "id": "yahoo_gc_f",
        "name": "Yahoo Finance GC=F (COMEX gold futures)",
        "status": "not_integrated_pending_approval",
        "approx_5m_coverage": "~60d / ~17k bars (public chart API)",
        "symbol": "GC=F",
        "feed": "CMX futures — NOT OANDA:XAUUSD spot",
        "licensing": "Yahoo public chart endpoints; ToS / redistribution constraints apply",
        "symbol_differences": (
            "Futures vs OANDA CFD/spot; contract rolls; different session hours; "
            "prices not interchangeable with OANDA:XAUUSD"
        ),
        "daily_roll": "Exchange session — not confirmed 17:00 America/New_York FX roll",
        "spread_quote": "Futures last/settlement semantics differ from OANDA bid/ask mid",
    },
    {
        "id": "dukascopy_bi5",
        "name": "Dukascopy historical ticks/bi5 (XAUUSD)",
        "status": "not_integrated_pending_approval",
        "approx_coverage": "multi-year tick/bi5 when accessible",
        "symbol": "XAUUSD (Dukascopy)",
        "feed": "Dukascopy — NOT OANDA",
        "licensing": "Dukascopy data terms; freeserv chart JSON probed 403 in Phase 15",
        "symbol_differences": "Different liquidity provider; spreads/quotes differ from OANDA",
        "daily_roll": "Must be validated separately vs OANDA 17:00 NY",
        "spread_quote": "Provider-specific",
    },
    {
        "id": "oanda_api",
        "name": "OANDA REST candle history (instrument XAU_USD)",
        "status": "not_integrated_no_credentials",
        "approx_coverage": "API-dependent (practice/live account)",
        "symbol": "XAU_USD",
        "feed": "OANDA — preferred exact feed if credentials/approved project mechanism exist",
        "licensing": "Requires OANDA account API token; not invented/bypassed",
        "symbol_differences": "Closest match to TradingView OANDA:XAUUSD if same account type",
        "daily_roll": "Should be validated against confirmed 17:00 NY evidence",
        "note": "No OANDA credentials or approved export present in project",
    },
]
