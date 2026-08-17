"""Chrome DevTools Protocol helper for TradingView Desktop."""

from __future__ import annotations

import asyncio
import base64
import json
from datetime import datetime
from pathlib import Path
from typing import Any

import requests
import websockets

CDP_HOST = "127.0.0.1"
CDP_PORT = 9222
CDP_BASE = f"http://{CDP_HOST}:{CDP_PORT}"
CHART_URL_HINT = "tradingview.com/chart"

HTTP_TIMEOUT_SECONDS = 5
WS_TIMEOUT_SECONDS = 10
SCREENSHOT_TIMEOUT_SECONDS = 20
SCREENSHOT_DIR = Path(__file__).resolve().parent / "screenshots"
MCP_DRAWINGS_KEY = "__aitradeMcpDrawings"


class CdpError(Exception):
    """Raised when CDP discovery or evaluation fails."""

    def __init__(self, message: str, code: str = "CDP_ERROR") -> None:
        super().__init__(message)
        self.code = code
        self.message = message

    def to_dict(self) -> dict[str, Any]:
        return {"ok": False, "error": self.message, "code": self.code}


def _http_get_json(path: str) -> Any:
    url = f"{CDP_BASE}{path}"
    try:
        response = requests.get(url, timeout=HTTP_TIMEOUT_SECONDS)
        response.raise_for_status()
    except requests.exceptions.ConnectionError as exc:
        raise CdpError(
            f"CDP port {CDP_PORT} is not reachable at {CDP_BASE}. "
            "Is TradingView Desktop running with --remote-debugging-port=9222?",
            code="CDP_UNAVAILABLE",
        ) from exc
    except requests.exceptions.Timeout as exc:
        raise CdpError(
            f"Timed out connecting to CDP at {CDP_BASE}.",
            code="CDP_UNAVAILABLE",
        ) from exc
    except requests.exceptions.HTTPError as exc:
        raise CdpError(
            f"CDP HTTP error from {url}: {exc}",
            code="CDP_UNAVAILABLE",
        ) from exc

    try:
        return response.json()
    except ValueError as exc:
        raise CdpError(
            f"Malformed CDP JSON from {url}.",
            code="MALFORMED_RESPONSE",
        ) from exc


def list_targets() -> list[dict[str, Any]]:
    """Return all CDP page targets from /json."""
    payload = _http_get_json("/json")
    if not isinstance(payload, list):
        raise CdpError(
            "CDP /json did not return a list of targets.",
            code="MALFORMED_RESPONSE",
        )
    return payload


def _target_summary(target: dict[str, Any]) -> dict[str, Any]:
    return {
        "target_id": target.get("id"),
        "title": target.get("title"),
        "url": target.get("url"),
        "type": target.get("type"),
        "webSocketDebuggerUrl": target.get("webSocketDebuggerUrl"),
    }


def find_tradingview_chart() -> dict[str, Any]:
    """Find the TradingView chart page among CDP targets."""
    targets = list_targets()
    pages = [
        target
        for target in targets
        if isinstance(target, dict) and target.get("webSocketDebuggerUrl")
    ]

    def url_of(target: dict[str, Any]) -> str:
        return str(target.get("url") or "").lower()

    def title_of(target: dict[str, Any]) -> str:
        return str(target.get("title") or "").lower()

    chart = next((t for t in pages if CHART_URL_HINT in url_of(t)), None)
    if chart is None:
        chart = next((t for t in pages if "tradingview.com" in url_of(t)), None)
    if chart is None:
        chart = next(
            (
                t
                for t in pages
                if "tradingview" in title_of(t) and t.get("type") == "page"
            ),
            None,
        )

    if chart is None:
        titles = [str(t.get("title") or t.get("url") or "unknown") for t in pages]
        raise CdpError(
            "No TradingView chart target found. "
            f"Visible CDP targets: {titles or 'none'}.",
            code="TV_NOT_FOUND",
        )

    return _target_summary(chart)


def health_check() -> dict[str, Any]:
    """Check CDP reachability and locate the TradingView chart target."""
    try:
        target = find_tradingview_chart()
    except CdpError as exc:
        return {
            "cdp_connected": exc.code != "CDP_UNAVAILABLE",
            "tradingview_found": False,
            "error": exc.message,
            "code": exc.code,
        }

    return {
        "cdp_connected": True,
        "tradingview_found": True,
        "target_id": target["target_id"],
        "title": target["title"],
        "url": target["url"],
        "webSocketDebuggerUrl": target["webSocketDebuggerUrl"],
    }


async def _recv_by_id(
    ws: Any, message_id: int, timeout: float = WS_TIMEOUT_SECONDS
) -> dict[str, Any]:
    while True:
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
        except asyncio.TimeoutError as exc:
            raise CdpError(
                f"Timed out waiting for CDP response id={message_id}.",
                code="WEBSOCKET_ERROR",
            ) from exc
        except websockets.exceptions.WebSocketException as exc:
            raise CdpError(
                f"WebSocket error while waiting for CDP response: {exc}",
                code="WEBSOCKET_ERROR",
            ) from exc

        try:
            message = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise CdpError(
                "Malformed CDP WebSocket JSON.",
                code="MALFORMED_RESPONSE",
            ) from exc

        if message.get("id") == message_id:
            return message


def _parse_evaluate_result(message: dict[str, Any]) -> Any:
    if "error" in message:
        error = message["error"]
        raise CdpError(
            f"CDP Runtime.evaluate failed: {error}",
            code="WEBSOCKET_ERROR",
        )

    result = message.get("result")
    if not isinstance(result, dict):
        raise CdpError(
            "Malformed Runtime.evaluate response (missing result).",
            code="MALFORMED_RESPONSE",
        )

    if "exceptionDetails" in result:
        details = result["exceptionDetails"]
        text = details.get("text") or "JavaScript exception"
        exception = details.get("exception") or {}
        description = exception.get("description") or exception.get("value") or text
        raise CdpError(
            f"JavaScript error in TradingView page: {description}",
            code="JS_EXCEPTION",
        )

    inner = result.get("result")
    if not isinstance(inner, dict):
        raise CdpError(
            "Malformed Runtime.evaluate response (missing result.value).",
            code="MALFORMED_RESPONSE",
        )

    if inner.get("type") == "undefined":
        return None
    return inner.get("value")


async def evaluate_js(
    expression: str,
    ws_url: str | None = None,
    timeout: float = WS_TIMEOUT_SECONDS,
) -> Any:
    """Execute JavaScript in the TradingView chart page via Runtime.evaluate."""
    if not ws_url:
        target = find_tradingview_chart()
        ws_url = target.get("webSocketDebuggerUrl")

    if not ws_url:
        raise CdpError(
            "TradingView chart target has no webSocketDebuggerUrl.",
            code="TV_NOT_FOUND",
        )

    try:
        async with websockets.connect(
            ws_url,
            open_timeout=timeout,
            close_timeout=5,
            max_size=None,
        ) as ws:
            await ws.send(json.dumps({"id": 1, "method": "Runtime.enable"}))
            await _recv_by_id(ws, 1, timeout=timeout)

            payload = {
                "id": 2,
                "method": "Runtime.evaluate",
                "params": {
                    "expression": expression,
                    "returnByValue": True,
                    "awaitPromise": True,
                },
            }
            await ws.send(json.dumps(payload))
            message = await _recv_by_id(ws, 2, timeout=timeout)
            return _parse_evaluate_result(message)
    except CdpError:
        raise
    except websockets.exceptions.WebSocketException as exc:
        raise CdpError(
            f"Failed to open CDP WebSocket: {exc}",
            code="WEBSOCKET_ERROR",
        ) from exc
    except OSError as exc:
        raise CdpError(
            f"Failed to open CDP WebSocket: {exc}",
            code="WEBSOCKET_ERROR",
        ) from exc
    except json.JSONDecodeError as exc:
        raise CdpError(
            "Malformed CDP WebSocket JSON.",
            code="MALFORMED_RESPONSE",
        ) from exc


CHART_INFO_JS = """
(() => {
  const c = window.TradingViewApi?.activeChart?.();

  if (!c) {
    return {
      available: false,
      error: "TradingViewApi.activeChart() unavailable"
    };
  }

  return {
    available: true,
    symbol: c.symbol(),
    resolution: c.resolution()
  };
})()
""".strip()


async def get_chart_info() -> dict[str, Any]:
    """Return the active chart symbol and resolution via injected JavaScript."""
    value = await evaluate_js(CHART_INFO_JS)
    if not isinstance(value, dict):
        raise CdpError(
            f"Unexpected chart-info result: {value!r}",
            code="MALFORMED_RESPONSE",
        )
    return value


SCREENSHOT_JS = """
(async () => {
  const api = window.TradingViewApi;
  const c = api?.activeChart?.();
  if (!api?.takeClientScreenshot) {
    return {
      ok: false,
      error: "TradingViewApi.takeClientScreenshot() unavailable"
    };
  }

  const canvas = await api.takeClientScreenshot();
  return {
    ok: true,
    width: canvas.width,
    height: canvas.height,
    mimeType: "image/png",
    dataUrl: canvas.toDataURL("image/png"),
    symbol: c?.symbol?.() || null,
    resolution: c?.resolution?.() || null
  };
})()
""".strip()


def _save_data_url_png(data_url: str) -> Path:
    if not isinstance(data_url, str) or "," not in data_url:
        raise CdpError(
            "Screenshot did not return a PNG data URL.",
            code="MALFORMED_RESPONSE",
        )
    _header, _, encoded = data_url.partition(",")
    try:
        raw = base64.b64decode(encoded)
    except (ValueError, TypeError) as exc:
        raise CdpError(
            "Screenshot data URL was not valid base64.",
            code="MALFORMED_RESPONSE",
        ) from exc
    if not raw:
        raise CdpError("Screenshot PNG was empty.", code="MALFORMED_RESPONSE")

    SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = SCREENSHOT_DIR / f"tv_{stamp}.png"
    path.write_bytes(raw)
    return path


async def take_screenshot() -> dict[str, Any]:
    """Capture the active chart via TradingViewApi.takeClientScreenshot()."""
    value = await evaluate_js(SCREENSHOT_JS, timeout=SCREENSHOT_TIMEOUT_SECONDS)
    if not isinstance(value, dict):
        raise CdpError(
            f"Unexpected screenshot result: {value!r}",
            code="MALFORMED_RESPONSE",
        )
    if not value.get("ok"):
        raise CdpError(
            str(value.get("error") or "Screenshot failed"),
            code="JS_EXCEPTION",
        )

    path = _save_data_url_png(value.get("dataUrl"))
    return {
        "ok": True,
        "path": str(path),
        "width": value.get("width"),
        "height": value.get("height"),
        "symbol": value.get("symbol"),
        "resolution": value.get("resolution"),
        "mimeType": "image/png",
    }


def _draw_horizontal_line_js(price: float, label: str, color: str = "#2962FF") -> str:
    args = json.dumps(
        {"price": float(price), "label": str(label), "color": str(color)}
    )
    return f"""
(async () => {{
  const args = {args};
  const c = window.TradingViewApi?.activeChart?.();
  if (!c || typeof c.createShape !== "function") {{
    return {{
      ok: false,
      error: "TradingViewApi.activeChart().createShape() unavailable"
    }};
  }}

  const id = await c.createShape(
    {{ price: args.price }},
    {{
      shape: "horizontal_line",
      text: args.label,
      disableSave: true,
      overrides: {{
        linecolor: args.color,
        linewidth: 2,
        showLabel: true,
        showPrice: true,
        textcolor: args.color,
        fontsize: 12,
        horzLabelsAlign: "right",
        vertLabelsAlign: "bottom"
      }}
    }}
  );

  window.{MCP_DRAWINGS_KEY} = window.{MCP_DRAWINGS_KEY} || [];
  window.{MCP_DRAWINGS_KEY}.push(id);

  return {{
    ok: true,
    id,
    price: args.price,
    label: args.label,
    symbol: c.symbol(),
    resolution: c.resolution()
  }};
}})()
""".strip()


async def draw_horizontal_line(
    price: float, label: str, color: str = "#2962FF"
) -> dict[str, Any]:
    """Draw a labeled horizontal line on the active TradingView chart."""
    value = await evaluate_js(_draw_horizontal_line_js(price, label, color=color))
    if not isinstance(value, dict):
        raise CdpError(
            f"Unexpected draw result: {value!r}",
            code="MALFORMED_RESPONSE",
        )
    if not value.get("ok"):
        raise CdpError(
            str(value.get("error") or "Failed to draw horizontal line"),
            code="JS_EXCEPTION",
        )
    return value


CLEAR_DRAWINGS_JS = f"""
(() => {{
  const c = window.TradingViewApi?.activeChart?.();
  if (!c || typeof c.removeEntity !== "function") {{
    return {{
      ok: false,
      error: "TradingViewApi.activeChart().removeEntity() unavailable"
    }};
  }}

  const ids = Array.isArray(window.{MCP_DRAWINGS_KEY})
    ? window.{MCP_DRAWINGS_KEY}.slice()
    : [];
  const removed = [];
  const failed = [];

  for (const id of ids) {{
    try {{
      c.removeEntity(id);
      removed.push(id);
    }} catch (err) {{
      failed.push({{ id, error: String(err) }});
    }}
  }}

  window.{MCP_DRAWINGS_KEY} = [];
  return {{
    ok: true,
    removed,
    failed,
    removed_count: removed.length
  }};
}})()
""".strip()


async def clear_drawings() -> dict[str, Any]:
    """Remove horizontal lines previously created by this MCP bridge."""
    value = await evaluate_js(CLEAR_DRAWINGS_JS)
    if not isinstance(value, dict):
        raise CdpError(
            f"Unexpected clear-drawings result: {value!r}",
            code="MALFORMED_RESPONSE",
        )
    if not value.get("ok"):
        raise CdpError(
            str(value.get("error") or "Failed to clear drawings"),
            code="JS_EXCEPTION",
        )
    return value
