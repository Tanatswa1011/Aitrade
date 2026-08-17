"""TradingView LuxAlgo adapter → normalized StructureConfirmation (CHoCH).

I/O only: locate study, extract primitives, normalize. No sweep/setup logic.
"""

from __future__ import annotations

import json
from typing import Any, Mapping, Optional, Sequence

from cdp import evaluate_js
from models import (
    StructureConfirmation,
    StructureDirection,
    StructureKind,
    TimingConfidence,
)

LUXALGO_STUDY_ID_HINT = "smUEv2"
PLACEHOLDER_BAR_INDEX = -2000000

# Defaults from Phase 1 when input colors are not readable.
DEFAULT_BULL_COLOR = 4286683400
DEFAULT_BEAR_COLOR = 4283585279

# Module-level cache: last discovered LuxAlgo study id for current chart state.
_CACHED_STUDY_ID: Optional[str] = None
_CACHED_STUDY_NAME: Optional[str] = None


LUXALGO_PRIMITIVES_JS = r"""
(() => {
  const c = window.TradingViewApi?.activeChart?.();
  if (!c) return { ok: false, error: "activeChart unavailable" };

  const preferredId = __PREFERRED_ID__;
  const studies = c.getAllStudies?.() || [];
  let hit = null;
  if (preferredId) {
    hit = studies.find((s) => s.id === preferredId);
    if (hit && !/LuxAlgo|Market Structure with Inducements/i.test(hit.name || "")) {
      hit = null;
    }
  }
  if (!hit) {
    hit = studies.find((s) =>
      /LuxAlgo|Market Structure with Inducements/i.test(s.name || "")
    );
  }
  if (!hit) {
    return {
      ok: false,
      error: "LuxAlgo study not found",
      studies: studies.map((s) => ({ id: s.id, name: s.name })),
    };
  }

  const api = c.getStudyById(hit.id);
  const inner = api.study?.() || api._study;
  const graphics = inner?._graphics;
  const indexes = graphics?._indexes || [];
  const prim = graphics?._primitivesCollection || {};

  const serialize = (d, id) => {
    if (!d || typeof d !== "object") return { id, raw: d };
    const o = { id };
    for (const k of Object.keys(d)) {
      const v = d[k];
      if (v == null || ["string", "number", "boolean"].includes(typeof v)) o[k] = v;
      else if (typeof v === "bigint") o[k] = Number(v);
    }
    return o;
  };

  const mapIndex = (x) => {
    if (x == null || !Array.isArray(indexes)) return null;
    const v = indexes[x];
    return typeof v === "number" ? v : null;
  };

  const harvest = (kind) => {
    const items = [];
    const outer = prim[kind];
    if (!(outer instanceof Map)) return items;
    for (const [gk, mid] of outer) {
      if (!(mid instanceof Map)) continue;
      for (const [ik, collection] of mid) {
        const map = collection?._primitivesDataById;
        if (!(map instanceof Map)) continue;
        for (const [id, data] of map) {
          const row = serialize(data, id);
          items.push({
            group: String(gk),
            innerKey: String(ik),
            ...row,
            indexMapped: mapIndex(data?.x),
            indexMapped1: mapIndex(data?.x1),
            indexMapped2: mapIndex(data?.x2),
          });
        }
      }
    }
    return items;
  };

  const vals = api.getInputValues?.() || [];
  const infos = api.getInputsInfo?.() || [];
  const byId = Object.fromEntries(infos.map((i) => [i.id, i]));
  const inputs = vals
    .filter((v) => !["text", "pineId", "pineVersion", "pineFeatures"].includes(v.id))
    .map((v) => ({
      id: v.id,
      name: byId[v.id]?.name || null,
      value: v.value,
    }));

  const colorOf = (nameRe) => {
    const hitIn = inputs.find((i) => nameRe.test(i.name || ""));
    return hitIn && typeof hitIn.value === "number" ? hitIn.value : null;
  };

  const series = c.getSeries?.() || c.chartModel?.()?.mainSeries?.();
  const data = typeof series?.data === "function" ? series.data() : series?._data;
  const barsBySeriesIndex = {};
  if (data && typeof data.size === "function") {
    const size = data.size();
    for (let i = 0; i < size; i++) {
      let row = null;
      try { row = data.valueAt?.(i); } catch (e) {}
      if (row == null) continue;
      let time = null;
      if (Array.isArray(row)) time = row[0];
      else if (typeof row === "object") {
        const v = typeof row.value === "function" ? row.value() : row;
        if (Array.isArray(v)) time = v[0];
        else if (v && typeof v === "object") time = v.time ?? v.timestamp ?? null;
        if ((time == null || typeof time !== "number") && typeof row.index === "function") {
          try { time = row.index(); } catch (e) {}
        }
      }
      if (typeof time === "number" && time > 1e12) time = Math.floor(time / 1000);
      if (typeof time === "number") barsBySeriesIndex[i] = time;
    }
  }

  return {
    ok: true,
    studyId: hit.id,
    studyName: hit.name,
    indexesLen: Array.isArray(indexes) ? indexes.length : 0,
    indexesNonPlaceholder: Array.isArray(indexes)
      ? indexes.filter((x) => typeof x === "number" && x !== -2000000 && x >= 0).length
      : 0,
    bullColor: colorOf(/bullish elements/i),
    bearColor: colorOf(/bearish elements/i),
    labels: harvest("dwglabels"),
    lines: harvest("dwglines"),
    inputs,
    barsBySeriesIndex,
  };
})()
"""


def _valid_bar_index(idx: Any) -> bool:
    return isinstance(idx, (int, float)) and int(idx) != PLACEHOLDER_BAR_INDEX and int(idx) >= 0


def _direction_from_color(
    color: Any, *, bull: int, bear: int
) -> Optional[str]:
    if color == bull:
        return StructureDirection.BULLISH.value
    if color == bear:
        return StructureDirection.BEARISH.value
    return None


def _direction_from_label_style(st: Any) -> Optional[str]:
    # Phase 1: ldn ↔ bullish, lup ↔ bearish for CHoCH/BOS.
    if st == "ldn":
        return StructureDirection.BULLISH.value
    if st == "lup":
        return StructureDirection.BEARISH.value
    return None


def _lookup_bar_time(
    bar_index: Optional[int],
    bars_by_series_index: Optional[Mapping[Any, int]],
) -> Optional[int]:
    if bar_index is None or bars_by_series_index is None:
        return None
    if bar_index in bars_by_series_index:
        return int(bars_by_series_index[bar_index])
    key = str(bar_index)
    if key in bars_by_series_index:
        return int(bars_by_series_index[key])
    return None


def _resolve_timing(
    *,
    label_bar: Any,
    line_bar1: Any,
    line_bar2: Any,
    bars_by_series_index: Optional[Mapping[Any, int]],
) -> tuple[Optional[int], Optional[int], str]:
    """
    Resolve event time/bar without fabricating.

    Preference: label mapped index → line end (x2) → line start (x1).
    exact   = mapped series index with known bar timestamp
    derived = usable bar index without timestamp, or secondary mapped index
    unavailable = only placeholder / missing indexes
    """
    ordered: list[tuple[str, Any]] = [
        ("label", label_bar),
        ("line_end", line_bar2),
        ("line_start", line_bar1),
    ]
    first_valid_idx: Optional[int] = None
    for source, raw in ordered:
        if not _valid_bar_index(raw):
            continue
        idx = int(raw)
        if first_valid_idx is None:
            first_valid_idx = idx
        ts = _lookup_bar_time(idx, bars_by_series_index)
        if ts is not None:
            confidence = (
                TimingConfidence.EXACT.value
                if source == "label"
                else TimingConfidence.DERIVED.value
            )
            return ts, idx, confidence

    if first_valid_idx is not None:
        return None, first_valid_idx, TimingConfidence.DERIVED.value
    return None, None, TimingConfidence.UNAVAILABLE.value


def _pair_dashed_line(label: dict, lines_by_y: dict[float, list]) -> Optional[dict]:
    y = label.get("y")
    if y is None:
        return None
    try:
        yf = float(y)
    except (TypeError, ValueError):
        return None
    for ln in lines_by_y.get(yf, []):
        if ln.get("st") == "dsh":
            return ln
    # Fallback: any line at same y (Phase 1 pairing often unique).
    cands = lines_by_y.get(yf) or []
    return cands[0] if cands else None


def normalize_choch_events(
    payload: dict[str, Any],
    *,
    bars_by_series_index: Optional[Mapping[Any, int]] = None,
) -> list[StructureConfirmation]:
    """Normalize raw LuxAlgo primitive payload into CHoCH StructureConfirmations."""
    if not payload.get("ok"):
        return []

    bull = int(payload.get("bullColor") or DEFAULT_BULL_COLOR)
    bear = int(payload.get("bearColor") or DEFAULT_BEAR_COLOR)
    study_id = payload.get("studyId")

    lines_by_y: dict[float, list] = {}
    for ln in payload.get("lines") or []:
        y = ln.get("y1")
        if y is None:
            continue
        try:
            lines_by_y.setdefault(float(y), []).append(ln)
        except (TypeError, ValueError):
            continue

    events: list[StructureConfirmation] = []
    for lab in payload.get("labels") or []:
        text = str(lab.get("t") or "").strip()
        if text != StructureKind.CHOCH.value:
            continue

        level_raw = lab.get("y")
        if level_raw is None:
            continue
        try:
            level = float(level_raw)
        except (TypeError, ValueError):
            continue

        line = _pair_dashed_line(lab, lines_by_y)
        direction = _direction_from_color(lab.get("tci"), bull=bull, bear=bear)
        if direction is None and line is not None:
            direction = _direction_from_color(line.get("ci"), bull=bull, bear=bear)
        if direction is None:
            direction = _direction_from_label_style(lab.get("st"))
        if direction is None:
            continue

        ts, bar_idx, confidence = _resolve_timing(
            label_bar=lab.get("indexMapped"),
            line_bar1=None if line is None else line.get("indexMapped1"),
            line_bar2=None if line is None else line.get("indexMapped2"),
            bars_by_series_index=bars_by_series_index,
        )

        events.append(
            StructureConfirmation(
                kind=StructureKind.CHOCH.value,
                direction=direction,
                level=level,
                event_timestamp=ts,
                event_bar_index=bar_idx,
                source="luxalgo",
                study_id=None if study_id is None else str(study_id),
                raw_id=None if lab.get("id") is None else str(lab.get("id")),
                timing_confidence=confidence,
                extras={
                    "draw_x": lab.get("x"),
                    "label_style": lab.get("st"),
                    "text_color": lab.get("tci"),
                    "line_id": None if line is None else line.get("id"),
                    "line_style": None if line is None else line.get("st"),
                    "line_x1": None if line is None else line.get("x1"),
                    "line_x2": None if line is None else line.get("x2"),
                    "placeholder_bar": lab.get("indexMapped") == PLACEHOLDER_BAR_INDEX,
                },
            )
        )

    def sort_key(e: StructureConfirmation) -> tuple:
        # Prefer chronological; unavailable timing sorts last by draw_x if present.
        if e.event_timestamp is not None:
            return (0, e.event_timestamp, e.level)
        if e.event_bar_index is not None:
            return (1, e.event_bar_index, e.level)
        draw_x = (e.extras or {}).get("draw_x")
        return (2, draw_x if isinstance(draw_x, (int, float)) else 10**12, e.level)

    events.sort(key=sort_key)
    return events


async def fetch_luxalgo_choch(
    *,
    bars_by_series_index: Optional[Mapping[Any, int]] = None,
) -> dict[str, Any]:
    """Fetch and normalize LuxAlgo CHoCH observations from the active chart."""
    global _CACHED_STUDY_ID, _CACHED_STUDY_NAME
    # Prefer semantic rediscovery; cache id only for current chart state.
    pref_js = "null" if not _CACHED_STUDY_ID else json.dumps(_CACHED_STUDY_ID)
    js = LUXALGO_PRIMITIVES_JS.replace("__PREFERRED_ID__", pref_js)
    raw = await evaluate_js(js, timeout=60)
    if not isinstance(raw, dict) or not raw.get("ok"):
        # Clear stale cache on miss so next call rediscovers by name.
        _CACHED_STUDY_ID = None
        _CACHED_STUDY_NAME = None
        return {
            "ok": False,
            "error": (raw or {}).get("error")
            if isinstance(raw, dict)
            else "luxalgo fetch failed",
            "events": [],
            "counts": {"CHoCH": 0},
        }

    sid = raw.get("studyId")
    if sid:
        _CACHED_STUDY_ID = str(sid)
        _CACHED_STUDY_NAME = raw.get("studyName")

    chart_map_raw = raw.get("barsBySeriesIndex") or {}
    merged_map: dict[Any, int] = {}
    if chart_map_raw:
        merged_map.update({int(k): int(v) for k, v in chart_map_raw.items()})
    if bars_by_series_index:
        merged_map.update({int(k): int(v) for k, v in bars_by_series_index.items()})

    events = normalize_choch_events(
        raw, bars_by_series_index=merged_map if merged_map else None
    )
    timing_counts = {
        TimingConfidence.EXACT.value: 0,
        TimingConfidence.DERIVED.value: 0,
        TimingConfidence.UNAVAILABLE.value: 0,
    }
    for e in events:
        timing_counts[e.timing_confidence] = timing_counts.get(e.timing_confidence, 0) + 1

    return {
        "ok": True,
        "study_id": raw.get("studyId"),
        "study_name": raw.get("studyName"),
        "bull_color": raw.get("bullColor") or DEFAULT_BULL_COLOR,
        "bear_color": raw.get("bearColor") or DEFAULT_BEAR_COLOR,
        "events": events,
        "bars_by_series_index": merged_map,
        "indexes_len": raw.get("indexesLen"),
        "indexes_non_placeholder": raw.get("indexesNonPlaceholder"),
        "counts": {
            "CHoCH": len(events),
            "bullish": sum(
                1 for e in events if e.direction == StructureDirection.BULLISH.value
            ),
            "bearish": sum(
                1 for e in events if e.direction == StructureDirection.BEARISH.value
            ),
            "timing": timing_counts,
        },
        "raw_labels": raw.get("labels") or [],
        "raw_lines": raw.get("lines") or [],
    }


def choch_only(events: Sequence[StructureConfirmation]) -> list[StructureConfirmation]:
    """Filter to CHoCH; BOS/IDM/x are never confirmation triggers in Phase 3."""
    return [e for e in events if e.kind == StructureKind.CHOCH.value]
