"""Chart symbol get/set with restore (CDP I/O only)."""

from __future__ import annotations

from typing import Any

from cdp import evaluate_js


async def get_chart_symbol() -> dict[str, Any]:
    js = """
(() => {
  const c = window.TradingViewApi?.activeChart?.();
  if (!c) return { ok: false, error: "activeChart unavailable" };
  const sym = (typeof c.symbol === "function") ? c.symbol() : null;
  return { ok: true, symbol: sym };
})()
""".strip()
    raw = await evaluate_js(js)
    if not isinstance(raw, dict) or not raw.get("ok"):
        return {"ok": False, "error": (raw or {}).get("error") if isinstance(raw, dict) else "symbol read failed"}
    return {"ok": True, "symbol": raw.get("symbol")}


async def set_chart_symbol(symbol: str) -> dict[str, Any]:
    js = f"""
(() => {{
  const c = window.TradingViewApi?.activeChart?.();
  if (!c) return {{ ok: false, error: "activeChart unavailable" }};
  if (typeof c.setSymbol !== "function") return {{ ok: false, error: "setSymbol unavailable" }};
  try {{
    const ret = c.setSymbol({symbol!r});
    return {{ ok: true, requested: {symbol!r}, returned: String(ret) }};
  }} catch (err) {{
    return {{ ok: false, error: String(err) }};
  }}
}})()
""".strip()
    raw = await evaluate_js(js)
    if not isinstance(raw, dict) or not raw.get("ok"):
        return {
            "ok": False,
            "error": (raw or {}).get("error") if isinstance(raw, dict) else "setSymbol failed",
        }
    return {"ok": True, "requested": symbol, "raw": raw}
