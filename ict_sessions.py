"""ICT Sessions & Killzones adapter → canonical SessionRange."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any, Optional

from datetime import timedelta
from zoneinfo import ZoneInfo

from cdp import evaluate_js
from models import CoverageStatus, PRIMARY_SESSIONS, SessionRange
from session_time import resolve_session_window
from sessions_config import (
    ICT_INDICATOR_TIMEZONE_INPUT,
    SESSION_DEFINITIONS,
    parse_hhmm_range,
)

# Cache discovered ICT study id for the active chart state only.
_CACHED_STUDY_ID: Optional[str] = None

ICT_PRIMITIVES_JS = r"""
(() => {
  const c = window.TradingViewApi?.activeChart?.();
  if (!c) return { ok: false, error: "activeChart unavailable" };

  const preferredId = __PREFERRED_ID__;
  const studies = c.getAllStudies?.() || [];
  let hit = null;
  if (preferredId) {
    hit = studies.find((s) => s.id === preferredId);
    if (hit && !/ICT Sessions/i.test(hit.name || "")) hit = null;
  }
  if (!hit) {
    hit = studies.find((s) => /ICT Sessions/i.test(s.name || ""));
  }
  if (!hit) {
    return {
      ok: false,
      error: "ICT Sessions study not found",
      studies: studies.map((s) => ({ id: s.id, name: s.name })),
    };
  }

  const api = c.getStudyById(hit.id);
  const inner = api.study?.() || api._study;
  const graphics = inner?._graphics;
  const indexes = graphics?._indexes || [];
  const prim = graphics?._primitivesCollection || {};

  const harvest = (kind) => {
    const items = [];
    const outer = prim[kind];
    if (!(outer instanceof Map)) return items;
    for (const [, mid] of outer) {
      if (!(mid instanceof Map)) continue;
      for (const [, collection] of mid) {
        const map = collection?._primitivesDataById;
        if (!(map instanceof Map)) continue;
        for (const [id, data] of map) {
          const d = data || {};
          items.push({
            id,
            t: d.t ?? d.text ?? null,
            x: d.x ?? null,
            y: d.y ?? null,
            x1: d.x1 ?? null,
            x2: d.x2 ?? null,
            y1: d.y1 ?? null,
            y2: d.y2 ?? null,
          });
        }
      }
    }
    return items;
  };

  const labels = harvest("dwglabels");
  const boxes = harvest("dwgboxes");

  const vals = api.getInputValues?.() || [];
  const infos = api.getInputsInfo?.() || [];
  const byId = Object.fromEntries(infos.map((i) => [i.id, i]));
  const inputs = vals.map((v) => ({
    id: v.id,
    name: byId[v.id]?.name || null,
    value: v.value,
  }));

  const mapIndex = (x) => {
    if (x == null || !Array.isArray(indexes)) return null;
    const v = indexes[x];
    return typeof v === "number" ? v : null;
  };

  return {
    ok: true,
    studyId: hit.id,
    studyName: hit.name,
    timezone: (inputs.find((i) => /timezone/i.test(i.name || "")) || {}).value || null,
    inputs,
    indexesLen: Array.isArray(indexes) ? indexes.length : 0,
    labels: labels.map((l) => ({
      ...l,
      barIndex: mapIndex(l.x),
    })),
    boxes: boxes.map((b) => ({
      ...b,
      barIndex1: mapIndex(b.x1),
      barIndex2: mapIndex(b.x2),
    })),
    symbol: c.symbol?.() || null,
    resolution: c.resolution?.() || null,
  };
})()
"""


def _norm_label(text: Any) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def _session_windows_from_inputs(inputs: list[dict[str, Any]]) -> dict[str, tuple[int, int]]:
    """Extract Asia/London HHMM ranges from adjacent ICT input values."""
    windows: dict[str, tuple[int, int]] = {}
    values = [i.get("value") for i in inputs]
    for idx, value in enumerate(values):
        if value in PRIMARY_SESSIONS and idx + 1 < len(values):
            nxt = values[idx + 1]
            if isinstance(nxt, str) and re.match(r"^\d{4}-\d{4}$", nxt.strip()):
                try:
                    windows[str(value)] = parse_hhmm_range(nxt)
                except ValueError:
                    pass
    return windows


def _match_box(label_y: float, boxes: list[dict[str, Any]]) -> Optional[dict[str, Any]]:
    best = None
    best_diff = float("inf")
    for box in boxes:
        y1, y2 = box.get("y1"), box.get("y2")
        if y1 is None or y2 is None:
            continue
        high = max(float(y1), float(y2))
        low = min(float(y1), float(y2))
        diff = abs(high - float(label_y))
        if diff < best_diff:
            best_diff = diff
            best = {
                "high": high,
                "low": low,
                "x1": box.get("x1"),
                "x2": box.get("x2"),
                "barIndex1": box.get("barIndex1"),
                "barIndex2": box.get("barIndex2"),
                "id": box.get("id"),
                "match_diff": diff,
            }
    return best


def _usable_bar_index(value: Any) -> Optional[int]:
    if isinstance(value, (int, float)) and value != -2000000:
        return int(value)
    return None


def _bar_time_lookup(bars_by_index: Optional[dict[int, int]], index: Optional[int]) -> Optional[int]:
    if bars_by_index is None or index is None:
        return None
    return bars_by_index.get(index)


def _resolved_bounds_for_anchor(name: str, anchor_ts: int):
    """
    Map an anchor timestamp to DST-aware [start, end) via SessionDefinition.

    Prices still come from ICT boxes. Semantic times come from AITRADE's
    canonical America/New_York definitions (not chart timezone, not frozen GMT).
    """
    definition = SESSION_DEFINITIONS.get(name)
    if definition is None:
        return None, None
    tz = ZoneInfo(definition.reference_timezone)
    local = datetime.fromtimestamp(anchor_ts, tz=timezone.utc).astimezone(tz)
    for delta in (0, -1, 1):
        trading_date = local.date() + timedelta(days=delta)
        window = resolve_session_window(definition, trading_date)
        if window.utc_start - 3600 <= anchor_ts <= window.utc_end + 3600:
            return window, definition
    window = resolve_session_window(definition, local.date())
    return window, definition


def raw_to_session_ranges(
    raw: dict[str, Any],
    *,
    bars_by_series_index: Optional[dict[int, int]] = None,
    now_ts: Optional[int] = None,
) -> list[SessionRange]:
    """Normalize ICT primitive payload into SessionRange list (Asia/London only)."""
    if not raw.get("ok"):
        return []

    tz_input = str(raw.get("timezone") or ICT_INDICATOR_TIMEZONE_INPUT)
    labels = raw.get("labels") or []
    boxes = raw.get("boxes") or []
    now_ts = now_ts or int(datetime.now(tz=timezone.utc).timestamp())

    # Pair each Asia/London label with nearest box high.
    occurrences: dict[str, list[dict[str, Any]]] = {n: [] for n in PRIMARY_SESSIONS}
    for lab in labels:
        name = _norm_label(lab.get("t"))
        if name not in PRIMARY_SESSIONS:
            continue
        y = lab.get("y")
        if y is None:
            continue
        box = _match_box(float(y), boxes)
        if not box or box["match_diff"] > 1e-4:
            if not box or box["match_diff"] > 0.05:
                continue
        occurrences[name].append(
            {
                "label_y": float(y),
                "label_x": lab.get("x"),
                "label_bar": _usable_bar_index(lab.get("barIndex")),
                "box": box,
            }
        )

    for name in occurrences:
        occurrences[name].sort(key=lambda o: (o.get("label_x") is None, o.get("label_x") or -1))

    ranges: list[SessionRange] = []

    for name in PRIMARY_SESSIONS:
        definition = SESSION_DEFINITIONS.get(name)
        for pos, item in enumerate(occurrences[name]):
            box = item["box"]
            b1 = _usable_bar_index(box.get("barIndex1"))
            b2 = _usable_bar_index(box.get("barIndex2"))
            draw_start = _bar_time_lookup(bars_by_series_index, b1)
            draw_end = _bar_time_lookup(bars_by_series_index, b2)
            anchor = draw_start or draw_end or _bar_time_lookup(
                bars_by_series_index, item.get("label_bar")
            )

            start_ts = end_ts = None
            time_source = None
            resolved = None
            if anchor is not None:
                resolved, _defn = _resolved_bounds_for_anchor(name, anchor)
                if resolved is not None:
                    start_ts, end_ts = resolved.utc_start, resolved.utc_end
                    time_source = "session_definition+anchor"
            if start_ts is None and draw_start is not None and draw_end is not None:
                start_ts, end_ts = draw_start, draw_end
                time_source = "drawing_indexes"

            if start_ts is not None and end_ts is not None:
                coverage = CoverageStatus.PARTIAL.value
            elif box.get("high") is not None:
                coverage = CoverageStatus.PRICE_ONLY.value
            else:
                coverage = CoverageStatus.UNKNOWN.value

            is_latest = pos == len(occurrences[name]) - 1
            if not is_latest:
                complete = True
            else:
                complete = bool(end_ts is not None and now_ts >= end_ts)

            identity = None
            if start_ts is not None:
                identity = f"{name}:{start_ts}"
            elif item.get("label_x") is not None:
                identity = f"{name}:drawX:{item['label_x']}"

            ref_tz = (
                definition.reference_timezone
                if definition is not None
                else tz_input
            )
            ranges.append(
                SessionRange(
                    name=name,
                    timezone=ref_tz,
                    start=start_ts,
                    end=end_ts,
                    high=float(box["high"]),
                    low=float(box["low"]),
                    high_timestamp=None,
                    low_timestamp=None,
                    complete=complete,
                    source="ict_sessions",
                    coverage_status=coverage,
                    identity=identity,
                    extras={
                        "study_id": raw.get("studyId"),
                        "draw_x": item.get("label_x"),
                        "box_id": box.get("id"),
                        "match_diff": box.get("match_diff"),
                        "is_latest": is_latest,
                        "time_source": time_source,
                        "drawing_start": draw_start,
                        "drawing_end": draw_end,
                        "ict_indicator_timezone_input": tz_input,
                        "resolved_window": None if resolved is None else resolved.to_dict(),
                        "definition": None if definition is None else definition.to_dict(),
                    },
                )
            )

    return ranges


async def fetch_ict_session_ranges(
    *,
    bars_by_series_index: Optional[dict[int, int]] = None,
) -> dict[str, Any]:
    """Read ICT Sessions from the live chart and return canonical SessionRanges."""
    global _CACHED_STUDY_ID
    pref_js = "null" if not _CACHED_STUDY_ID else json.dumps(_CACHED_STUDY_ID)
    js = ICT_PRIMITIVES_JS.replace("__PREFERRED_ID__", pref_js)
    raw = await evaluate_js(js, timeout=60)
    if not isinstance(raw, dict) or not raw.get("ok"):
        _CACHED_STUDY_ID = None
        return {
            "ok": False,
            "error": (raw or {}).get("error") if isinstance(raw, dict) else "ICT read failed",
            "ranges": [],
            "latest": {},
        }

    if raw.get("studyId"):
        _CACHED_STUDY_ID = str(raw.get("studyId"))

    ranges = raw_to_session_ranges(raw, bars_by_series_index=bars_by_series_index)
    latest = {}
    for name in PRIMARY_SESSIONS:
        named = [r for r in ranges if r.name == name]
        if named:
            latest[name] = named[-1]

    return {
        "ok": True,
        "study_id": raw.get("studyId"),
        "study_name": raw.get("studyName"),
        "timezone": raw.get("timezone") or ICT_INDICATOR_TIMEZONE_INPUT,
        "symbol": raw.get("symbol"),
        "resolution": raw.get("resolution"),
        "ranges": ranges,
        "latest": latest,
        "counts": {name: sum(1 for r in ranges if r.name == name) for name in PRIMARY_SESSIONS},
    }


def latest_primary_levels(ranges: list[SessionRange]) -> dict[str, SessionRange]:
    out: dict[str, SessionRange] = {}
    for name in PRIMARY_SESSIONS:
        named = [r for r in ranges if r.name == name]
        if named:
            out[name] = named[-1]
    return out
