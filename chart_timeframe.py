"""Chart timeframe get/set with restore — for multi-TF live reads."""

from __future__ import annotations

from typing import Any, Optional

from cdp import evaluate_js
from timeframe import normalize_timeframe


CANONICAL_TO_TV = {
    "1m": "1",
    "5m": "5",
    "15m": "15",
    "1H": "60",
    "4H": "240",
    "1D": "1D",
}


async def get_chart_resolution() -> dict[str, Any]:
    js = """
(() => {
  const c = window.TradingViewApi?.activeChart?.();
  if (!c) return { ok: false, error: "activeChart unavailable" };
  const raw = (typeof c.resolution === "function") ? c.resolution() : null;
  return { ok: true, resolution: raw };
})()
""".strip()
    raw = await evaluate_js(js)
    if not isinstance(raw, dict) or not raw.get("ok"):
        return {
            "ok": False,
            "error": (raw or {}).get("error")
            if isinstance(raw, dict)
            else "resolution failed",
        }
    res = raw.get("resolution")
    return {
        "ok": True,
        "resolution": res,
        "canonical": normalize_timeframe(str(res) if res is not None else None),
    }


async def set_chart_resolution(resolution: str) -> dict[str, Any]:
    """Set active chart resolution. Accepts canonical (5m) or TV raw (5)."""
    import json

    canon = normalize_timeframe(resolution) or resolution
    tv = CANONICAL_TO_TV.get(canon, resolution)
    args = json.dumps({"resolution": tv})
    js = f"""
(async () => {{
  const args = {args};
  const c = window.TradingViewApi?.activeChart?.();
  if (!c) return {{ ok: false, error: "activeChart unavailable" }};
  if (typeof c.setResolution !== "function") {{
    return {{
      ok: false,
      error: "setResolution unavailable",
      resolution: c.resolution?.() || null
    }};
  }}
  try {{
    await new Promise((resolve, reject) => {{
      try {{
        c.setResolution(args.resolution, () => resolve(true));
      }} catch (err) {{
        reject(err);
      }}
      setTimeout(() => resolve(true), 2500);
    }});
  }} catch (err) {{
    return {{ ok: false, error: String(err) }};
  }}
  const now = c.resolution?.() || null;
  return {{ ok: true, resolution: now, requested: args.resolution }};
}})()
""".strip()
    raw = await evaluate_js(js, timeout=30)
    if not isinstance(raw, dict):
        return {"ok": False, "error": "setResolution returned non-object"}
    return raw


async def with_chart_resolution(target: str, coro_factory):
    """
    Temporarily switch chart resolution, run async factory, restore original.

    Does not leave the chart on the temporary timeframe when restore works.
    """
    original = await get_chart_resolution()
    orig_raw = original.get("resolution")
    orig_canon = original.get("canonical")
    target_canon = normalize_timeframe(target) or target
    result: Optional[dict[str, Any]] = None
    error: Optional[Exception] = None

    switched = False
    if orig_canon != target_canon:
        set_res = await set_chart_resolution(target_canon)
        if not set_res.get("ok"):
            return {
                "ok": False,
                "error": set_res.get("error") or "failed to set resolution",
                "original_chart_timeframe": orig_raw,
                "requested_execution_timeframe": target_canon,
                "chart_timeframe_after_analysis": orig_raw,
                "switched": False,
            }
        switched = True

    try:
        result = await coro_factory()
        if isinstance(result, dict):
            result = {
                **result,
                "original_chart_timeframe": orig_raw,
                "requested_execution_timeframe": target_canon,
                "switched": switched,
            }
    except Exception as exc:  # noqa: BLE001
        error = exc
    finally:
        after_info = await get_chart_resolution()
        if switched and orig_raw is not None:
            restore = await set_chart_resolution(str(orig_raw))
            after_info = await get_chart_resolution()
            restore_ok = bool(restore.get("ok"))
        else:
            restore_ok = True
        if isinstance(result, dict):
            result["chart_timeframe_after_analysis"] = after_info.get("resolution")
            result["restore_ok"] = restore_ok

    if error is not None:
        raise error
    return result