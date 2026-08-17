"""Expand loaded TradingView series history by scrolling the visible range."""

from __future__ import annotations

import asyncio
from typing import Any, Optional

from bars import fetch_bars
from cdp import evaluate_js
from models import Bar


async def scroll_history_left(*, bars: int = 200) -> dict[str, Any]:
    """Scroll chart left to encourage TradingView to load older bars."""
    js = f"""
(() => {{
  const c = window.TradingViewApi?.activeChart?.();
  if (!c) return {{ ok: false, error: "activeChart unavailable" }};
  try {{
    if (typeof c.scrollChartByBar === "function") {{
      c.scrollChartByBar(-{int(bars)});
      return {{ ok: true, method: "scrollChartByBar", bars: {int(bars)} }};
    }}
    if (typeof c.setVisibleRange === "function" && typeof c.getVisibleRange === "function") {{
      const vr = c.getVisibleRange();
      if (vr && typeof vr.from === "number" && typeof vr.to === "number") {{
        const span = vr.to - vr.from;
        c.setVisibleRange({{ from: vr.from - span, to: vr.to - span }});
        return {{ ok: true, method: "setVisibleRange", from: vr.from - span, to: vr.to - span }};
      }}
    }}
    return {{ ok: false, error: "no scroll API" }};
  }} catch (err) {{
    return {{ ok: false, error: String(err) }};
  }}
}})()
""".strip()
    raw = await evaluate_js(js)
    return raw if isinstance(raw, dict) else {"ok": False, "error": "scroll failed"}


async def fetch_expanded_bars(
    *,
    scrolls: int = 8,
    scroll_bars: int = 250,
    settle_ms: int = 900,
    max_bars: int = 5000,
) -> dict[str, Any]:
    """
    Scroll left repeatedly and merge unique OHLC bars from the loaded series.

    Does not invent gaps. Returns the union of observed bars only.
    """
    by_t: dict[int, Bar] = {}
    scrolls_ok = 0
    errors: list[str] = []
    symbol = None
    resolution = None
    for i in range(max(1, scrolls)):
        payload = await fetch_bars()
        if not payload.get("ok"):
            errors.append(str(payload.get("error")))
            break
        symbol = payload.get("symbol") or symbol
        resolution = payload.get("resolution") or resolution
        for b in payload.get("bars") or []:
            by_t[int(b.time)] = b
        if len(by_t) >= max_bars:
            break
        if i + 1 < scrolls:
            sc = await scroll_history_left(bars=scroll_bars)
            if sc.get("ok"):
                scrolls_ok += 1
            else:
                errors.append(str(sc.get("error")))
            await asyncio.sleep(settle_ms / 1000.0)

    bars = [by_t[t] for t in sorted(by_t)]
    return {
        "ok": bool(bars),
        "bars": bars,
        "bar_count": len(bars),
        "scrolls_ok": scrolls_ok,
        "symbol": symbol,
        "resolution": resolution,
        "errors": errors,
        "earliest": None if not bars else int(bars[0].time),
        "latest": None if not bars else int(bars[-1].time),
        "bars_by_series_index": {},  # indexes shift after scroll; not stable
    }
