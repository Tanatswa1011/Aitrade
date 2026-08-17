"""TradingView annotation renderer for TradeSetup (drawing only; no strategy)."""

from __future__ import annotations

import json
from typing import Any, Optional, Sequence, Union

from annotation_plan import AnnotationPlan, PlannedAnnotation, plan_annotations
from cdp import CdpError, evaluate_js, take_screenshot
from models import TradeSetup

SETUP_ANNOTATIONS_KEY = "__aitradeSetupAnnotations"


def _clear_setup_js(setup_id: Optional[str]) -> str:
    args = json.dumps({"setupId": setup_id})
    return f"""
(async () => {{
  const args = {args};
  const c = window.TradingViewApi?.activeChart?.();
  if (!c || typeof c.removeEntity !== "function") {{
    return {{ ok: false, error: "removeEntity unavailable" }};
  }}
  window.{SETUP_ANNOTATIONS_KEY} = window.{SETUP_ANNOTATIONS_KEY} || {{}};
  const store = window.{SETUP_ANNOTATIONS_KEY};
  const targets = [];
  if (args.setupId) {{
    targets.push(String(args.setupId));
  }} else {{
    targets.push(...Object.keys(store));
  }}
  const removed = [];
  const failed = [];
  for (const sid of targets) {{
    const ids = Array.isArray(store[sid]) ? store[sid].slice() : [];
    for (const id of ids) {{
      try {{
        c.removeEntity(id);
        removed.push({{ setupId: sid, id }});
      }} catch (err) {{
        failed.push({{ setupId: sid, id, error: String(err) }});
      }}
    }}
    store[sid] = [];
    if (args.setupId) delete store[sid];
  }}
  if (!args.setupId) window.{SETUP_ANNOTATIONS_KEY} = {{}};
  return {{ ok: true, removed, failed, removed_count: removed.length }};
}})()
""".strip()


def _render_plan_js(plan: AnnotationPlan) -> str:
    payload = {
        "setupId": plan.setup_id,
        "items": [i.to_dict() for i in plan.items],
    }
    args = json.dumps(payload)
    return f"""
(async () => {{
  const args = {args};
  const c = window.TradingViewApi?.activeChart?.();
  if (!c || typeof c.createShape !== "function") {{
    return {{ ok: false, error: "createShape unavailable" }};
  }}
  window.{SETUP_ANNOTATIONS_KEY} = window.{SETUP_ANNOTATIONS_KEY} || {{}};
  const store = window.{SETUP_ANNOTATIONS_KEY};
  const setupId = String(args.setupId);

  // Idempotency: clear previous shapes for this setup_id first.
  const prev = Array.isArray(store[setupId]) ? store[setupId].slice() : [];
  const cleared = [];
  for (const id of prev) {{
    try {{ c.removeEntity(id); cleared.push(id); }} catch (e) {{}}
  }}
  store[setupId] = [];

  const created = [];
  const failed = [];
  const skipped_render = [];

  const pushId = (id, meta) => {{
    if (id == null) return;
    store[setupId].push(id);
    created.push({{ id, ...meta }});
  }};

  for (const item of args.items || []) {{
    try {{
      if (item.kind === "horizontal_line") {{
        if (typeof item.price !== "number") {{
          skipped_render.push({{ role: item.role, reason: "missing_price" }});
          continue;
        }}
        const point = {{ price: item.price }};
        // Do not invent time markers; price-level line only.
        const id = await c.createShape(point, {{
          shape: "horizontal_line",
          text: item.label || "",
          disableSave: true,
          overrides: {{
            linecolor: item.color || "#546E7A",
            linewidth: item.linewidth || 2,
            linestyle: item.role === "status" ? 2 : 0,
            showLabel: true,
            showPrice: true,
            textcolor: item.color || "#546E7A",
            fontsize: 11,
            horzLabelsAlign: "right",
            vertLabelsAlign: "bottom"
          }}
        }});
        pushId(id, {{ role: item.role, label: item.label, kind: item.kind, price: item.price }});
      }} else if (item.kind === "rectangle") {{
        if (
          typeof item.price !== "number" ||
          typeof item.price_secondary !== "number" ||
          typeof item.time !== "number" ||
          typeof item.time_secondary !== "number"
        ) {{
          skipped_render.push({{ role: item.role, reason: "incomplete_rectangle_geometry" }});
          continue;
        }}
        const maker = c.createMultipointShape || c.createShape;
        if (typeof c.createMultipointShape === "function") {{
          const id = await c.createMultipointShape(
            [
              {{ time: item.time, price: item.price }},
              {{ time: item.time_secondary, price: item.price_secondary }}
            ],
            {{
              shape: "rectangle",
              text: item.label || "",
              disableSave: true,
              overrides: {{
                backgroundColor: item.color || "#00897B",
                color: item.color || "#00897B",
                linewidth: 1,
                transparency: 80,
                filled: true,
                fillBackground: true,
                showLabel: true,
                textcolor: item.color || "#00897B"
              }}
            }}
          );
          pushId(id, {{ role: item.role, label: item.label, kind: "rectangle" }});
        }} else {{
          // Fallback: two horizontals for zone bounds
          for (const [p, lab] of [
            [item.price, (item.label || "FVG") + " High"],
            [item.price_secondary, (item.label || "FVG") + " Low"]
          ]) {{
            const id = await c.createShape({{ price: p }}, {{
              shape: "horizontal_line",
              text: lab,
              disableSave: true,
              overrides: {{
                linecolor: item.color || "#00897B",
                linewidth: 2,
                showLabel: true,
                showPrice: true,
                textcolor: item.color || "#00897B",
                fontsize: 11,
                horzLabelsAlign: "right"
              }}
            }});
            pushId(id, {{ role: item.role, label: lab, kind: "horizontal_line_fallback", price: p }});
          }}
          skipped_render.push({{ role: item.role, reason: "rectangle_api_unavailable_used_hline_fallback" }});
        }}
      }} else {{
        skipped_render.push({{ role: item.role, reason: "unsupported_kind:" + item.kind }});
      }}
    }} catch (err) {{
      failed.push({{ role: item.role, error: String(err) }});
    }}
  }}

  return {{
    ok: true,
    setup_id: setupId,
    created,
    created_count: created.length,
    cleared_previous: cleared,
    failed,
    skipped_render,
    symbol: c.symbol?.() || null,
    resolution: c.resolution?.() || null
  }};
}})()
""".strip()


async def clear_setup_annotations(setup_id: Optional[str] = None) -> dict[str, Any]:
    """Clear only AITRADE setup annotations (optionally one setup_id)."""
    value = await evaluate_js(_clear_setup_js(setup_id), timeout=30)
    if not isinstance(value, dict) or not value.get("ok"):
        raise CdpError(
            str((value or {}).get("error") or "Failed to clear setup annotations"),
            code="JS_EXCEPTION",
        )
    return value


async def render_annotation_plan(plan: AnnotationPlan) -> dict[str, Any]:
    """Idempotently render an AnnotationPlan onto the active chart."""
    value = await evaluate_js(_render_plan_js(plan), timeout=60)
    if not isinstance(value, dict):
        raise CdpError(f"Unexpected annotate result: {value!r}", code="MALFORMED_RESPONSE")
    if not value.get("ok"):
        raise CdpError(
            str(value.get("error") or "Failed to annotate setup"),
            code="JS_EXCEPTION",
        )
    return value


async def annotate_trade_setup(
    setup: Union[TradeSetup, dict[str, Any]],
    *,
    entry_mode: str = "all",
    show_fixed_rr: bool = True,
    show_opposite_liquidity: bool = True,
    fixed_rr_to_show: Optional[Sequence[float]] = None,
    take_screenshot_after: bool = False,
) -> dict[str, Any]:
    """
    TradeSetup → AnnotationPlan → TradingView drawings.

    Never recalculates strategy fields.
    """
    plan = plan_annotations(
        setup,
        entry_mode=entry_mode,
        show_fixed_rr=show_fixed_rr,
        show_opposite_liquidity=show_opposite_liquidity,
        fixed_rr_to_show=fixed_rr_to_show,
    )
    rendered = await render_annotation_plan(plan)
    out: dict[str, Any] = {
        "ok": True,
        "setup_id": plan.setup_id,
        "status": plan.status,
        "plan": plan.to_dict(),
        "annotations_created": rendered.get("created") or [],
        "annotations_created_count": rendered.get("created_count") or 0,
        "annotations_skipped_plan": plan.skipped,
        "annotations_skipped_render": rendered.get("skipped_render") or [],
        "cleared_previous": rendered.get("cleared_previous") or [],
        "failed": rendered.get("failed") or [],
        "symbol": rendered.get("symbol"),
        "resolution": rendered.get("resolution"),
    }
    if take_screenshot_after:
        shot = await take_screenshot()
        out["screenshot"] = shot
        out["screenshot_path"] = shot.get("path")
    return out
