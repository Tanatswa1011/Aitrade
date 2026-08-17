"""Resume older OpenBB/Tiingo history without re-downloading known ranges."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from bar_dataset import load_dataset, write_dataset
from historical_data_provider import integrity_report
from ohlc_resample import resample_ohlc
from openbb_history import OpenBBHistoricalDataProvider, load_dotenv_credentials
from timeframe import timeframe_seconds
from trading_day_config import load_confirmed_trading_day_from_evidence


TIINGO_ROOT = Path("data") / "openbb" / "tiingo"
DATASET_SYMBOL = "openbb_tiingo_XAUUSD"
SOURCE_SYMBOL = "XAUUSD"


def current_5m_span(*, root: Path = TIINGO_ROOT) -> dict[str, Any]:
    loaded = load_dataset(DATASET_SYMBOL, "5m", root=root)
    bars = loaded.get("bars") or []
    if not bars:
        return {"ok": False, "bar_count": 0, "earliest": None, "latest": None}
    return {
        "ok": True,
        "bar_count": len(bars),
        "earliest": int(bars[0].time),
        "latest": int(bars[-1].time),
        "path": loaded.get("path"),
    }


def extend_tiingo_5m_backward(
    *,
    target_earliest_ts: int,
    chunk_days: int = 14,
    root: Path = TIINGO_ROOT,
) -> dict[str, Any]:
    """
    Fetch only bars older than the local earliest timestamp, merge/dedupe, persist.

    Preserves OpenBB/Tiingo/XAUUSD provenance. Does not interpolate gaps.
    """
    load_dotenv_credentials()
    span = current_5m_span(root=root)
    if not span.get("ok"):
        # cold start — fetch full window
        end_ts = int(datetime.now(tz=timezone.utc).timestamp())
        start_ts = int(target_earliest_ts)
        prov = OpenBBHistoricalDataProvider(underlying_provider="tiingo", route="currency")
        res = prov.fetch_chunked(
            SOURCE_SYMBOL,
            "5m",
            start_ts=start_ts,
            end_ts=end_ts,
            chunk_days=chunk_days,
            persist=True,
        )
        return {
            "mode": "cold_start",
            "requested_start": start_ts,
            "requested_end": end_ts,
            "bars_fetched": len(res.bars),
            "disk_after": current_5m_span(root=root),
            "errors": list(res.errors),
            "warnings": list(res.warnings),
            "provenance": {
                "data_provider": "openbb",
                "underlying_provider": "tiingo",
                "source_symbol": SOURCE_SYMBOL,
                "feed_equivalence_class": "CLOSE_EQUIVALENT",
            },
        }

    earliest = int(span["earliest"])
    if target_earliest_ts >= earliest:
        return {
            "mode": "noop",
            "reason": "local_already_covers_target",
            "disk": span,
            "provenance": {
                "data_provider": "openbb",
                "underlying_provider": "tiingo",
                "source_symbol": SOURCE_SYMBOL,
                "feed_equivalence_class": "CLOSE_EQUIVALENT",
            },
        }

    # Overlap 1 day into existing so merge/dedupe is safe; avoid re-fetching whole known range.
    fetch_end = earliest + 86400
    prov = OpenBBHistoricalDataProvider(underlying_provider="tiingo", route="currency")
    res = prov.fetch_chunked(
        SOURCE_SYMBOL,
        "5m",
        start_ts=int(target_earliest_ts),
        end_ts=int(fetch_end),
        chunk_days=chunk_days,
        persist=True,
    )
    after = current_5m_span(root=root)
    return {
        "mode": "resume_older",
        "requested_start": int(target_earliest_ts),
        "requested_end": int(fetch_end),
        "prior_earliest": earliest,
        "bars_fetched": len(res.bars),
        "disk_before": span,
        "disk_after": after,
        "errors": list(res.errors),
        "warnings": list(res.warnings),
        "provenance": {
            "data_provider": "openbb",
            "underlying_provider": "tiingo",
            "source_symbol": SOURCE_SYMBOL,
            "feed_equivalence_class": "CLOSE_EQUIVALENT",
            "instrument": "spot_fx_metals",
        },
    }


def rebuild_derived_timeframes(*, root: Path = TIINGO_ROOT) -> dict[str, Any]:
    loaded = load_dataset(DATASET_SYMBOL, "5m", root=root)
    bars5 = loaded.get("bars") or []
    if not bars5:
        return {"ok": False, "error": "missing_5m"}
    td = load_confirmed_trading_day_from_evidence()
    res15 = resample_ohlc(bars5, "15m")
    res4h = resample_ohlc(bars5, "4H", trading_day=td)
    res1d = resample_ohlc(bars5, "1D", trading_day=td)
    out = {}
    for series, tf, tag in (
        (res15, "15m", "resampled_from_openbb_5m"),
        (res4h, "4H", "resampled_from_openbb_5m"),
        (res1d, "1D", "resampled_from_openbb_5m"),
    ):
        written = write_dataset(
            list(series.bars),
            symbol=DATASET_SYMBOL,
            timeframe=tf,
            source=tag,
            root=root,
            expected_period_sec=timeframe_seconds(tf) if tf == "15m" else None,
        )
        out[tf] = {
            "bar_count": len(series.bars),
            "path": written.get("path"),
            "extras": getattr(series, "extras", None) or {},
        }
    from historical_data_provider import HistoricalCoverageMeta, HistoricalDataset

    meta = HistoricalCoverageMeta(
        provider="openbb",
        symbol=DATASET_SYMBOL,
        timeframe="5m",
        source_symbol=SOURCE_SYMBOL,
        timezone="UTC",
        price_precision=None,
        capture_timestamp=datetime.now(tz=timezone.utc).isoformat(),
        requested_start=int(bars5[0].time),
        requested_end=int(bars5[-1].time),
        actual_start=int(bars5[0].time),
        actual_end=int(bars5[-1].time),
        bar_count=len(bars5),
        source="openbb:tiingo",
        extras={
            "underlying_provider": "tiingo",
            "feed_equivalence_class": "CLOSE_EQUIVALENT",
            "instrument_type": "spot_fx_metals",
        },
    )
    ds = HistoricalDataset(bars=tuple(bars5), meta=meta)
    integ = integrity_report(ds, trading_day=td)
    return {
        "ok": True,
        "bars_5m": len(bars5),
        "derived": out,
        "integrity": integ,
        "daily_boundary": "17:00 America/New_York",
        "provenance": {
            "data_provider": "openbb",
            "underlying_provider": "tiingo",
            "source_symbol": SOURCE_SYMBOL,
            "feed_equivalence_class": "CLOSE_EQUIVALENT",
        },
    }


def default_one_year_target_ts(*, latest_ts: Optional[int] = None) -> int:
    end = (
        datetime.fromtimestamp(int(latest_ts), tz=timezone.utc)
        if latest_ts
        else datetime.now(tz=timezone.utc)
    )
    return int((end - timedelta(days=365)).timestamp())


def default_eighteen_month_target_ts(*, latest_ts: Optional[int] = None) -> int:
    end = (
        datetime.fromtimestamp(int(latest_ts), tz=timezone.utc)
        if latest_ts
        else datetime.now(tz=timezone.utc)
    )
    return int((end - timedelta(days=548)).timestamp())
