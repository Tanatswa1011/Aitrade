"""OHLC bar data provider via TradingView CDP (I/O only, no strategy logic)."""

from __future__ import annotations

from typing import Any, Optional

from cdp import evaluate_js
from models import Bar

FETCH_BARS_JS = r"""
(() => {
  const c = window.TradingViewApi?.activeChart?.();
  if (!c) return { ok: false, error: "activeChart unavailable" };

  const series = c.getSeries?.() || c.chartModel?.()?.mainSeries?.();
  if (!series) return { ok: false, error: "main series unavailable" };

  const data = typeof series.data === "function" ? series.data() : series._data;
  if (!data || typeof data.size !== "function") {
    return { ok: false, error: "series data unavailable" };
  }

  const size = data.size();
  const bars = [];
  for (let i = 0; i < size; i++) {
    let row = null;
    try { row = data.valueAt?.(i); } catch (e) {}
    if (row == null) continue;

    let time = null, open = null, high = null, low = null, close = null, volume = null;

    if (Array.isArray(row)) {
      // Common shapes: [time, open, high, low, close, volume?] or nested
      time = row[0];
      open = row[1];
      high = row[2];
      low = row[3];
      close = row[4];
      volume = row.length > 5 ? row[5] : null;
    } else if (typeof row === "object") {
      const v = typeof row.value === "function" ? row.value() : row;
      if (Array.isArray(v)) {
        time = v[0]; open = v[1]; high = v[2]; low = v[3]; close = v[4];
        volume = v.length > 5 ? v[5] : null;
      } else if (v && typeof v === "object") {
        time = v.time ?? v.timestamp ?? row.timeIndex ?? null;
        open = v.open ?? v.o ?? null;
        high = v.high ?? v.h ?? null;
        low = v.low ?? v.l ?? null;
        close = v.close ?? v.c ?? null;
        volume = v.volume ?? v.v ?? null;
        // PlotRow style often stores [o,h,l,c] under value with separate time via index
      }
      if ((time == null || typeof time !== "number") && typeof row.index === "function") {
        try { time = row.index(); } catch (e) {}
      }
      if (typeof row.value === "function" && (open == null || high == null)) {
        try {
          const vals = row.value();
          if (Array.isArray(vals) && vals.length >= 4) {
            // Sometimes value() is [o,h,l,c] without time
            if (vals.length === 4 || (vals.length >= 4 && vals[0] < 1e11)) {
              open = vals[0]; high = vals[1]; low = vals[2]; close = vals[3];
              volume = vals.length > 4 ? vals[4] : volume;
            }
          }
        } catch (e) {}
      }
    }

    if (typeof time !== "number" || time > 1e12) {
      // ns or ms → seconds
      if (typeof time === "number" && time > 1e12) time = Math.floor(time / 1000);
    }
    if (typeof time !== "number" || ![open, high, low, close].every(x => typeof x === "number")) {
      continue;
    }
    bars.push({
      seriesIndex: i,
      time,
      open,
      high,
      low,
      close,
      volume: typeof volume === "number" ? volume : null
    });
  }

  return {
    ok: true,
    symbol: c.symbol?.() || null,
    resolution: c.resolution?.() || null,
    count: bars.length,
    bars,
  };
})()
"""


async def fetch_bars(limit: Optional[int] = None) -> dict[str, Any]:
    """Fetch OHLC bars from the active TradingView chart series."""
    raw = await evaluate_js(FETCH_BARS_JS, timeout=60)
    if not isinstance(raw, dict) or not raw.get("ok"):
        return {
            "ok": False,
            "error": (raw or {}).get("error") if isinstance(raw, dict) else "bar fetch failed",
            "bars": [],
        }

    raw_bars = list(raw.get("bars") or [])
    if limit is not None and limit > 0:
        raw_bars = raw_bars[-limit:]

    bars = [
        Bar(
            time=int(b["time"]),
            open=float(b["open"]),
            high=float(b["high"]),
            low=float(b["low"]),
            close=float(b["close"]),
            volume=None if b.get("volume") is None else float(b["volume"]),
        )
        for b in raw_bars
    ]
    # Preserve TradingView series indexes for ICT drawing joins.
    by_index = {
        int(b["seriesIndex"]): int(b["time"])
        for b in raw_bars
        if b.get("seriesIndex") is not None
    }

    return {
        "ok": True,
        "symbol": raw.get("symbol"),
        "resolution": raw.get("resolution"),
        "count": len(bars),
        "bars": bars,
        "bars_by_series_index": by_index,
    }
