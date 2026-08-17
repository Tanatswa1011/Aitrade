"""Deep LuxAlgo accessibility probe for Phase 1 v1."""
from __future__ import annotations

import asyncio
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from cdp import evaluate_js

JS = r"""
(() => {
  const c = window.TradingViewApi.activeChart();
  const studies = c.getAllStudies() || [];
  const hit = studies.find(s => /LuxAlgo|Market Structure with Inducements/i.test(s.name || ''));
  if (!hit) return { ok:false, error:'LuxAlgo not found', studies: studies.map(s=>({id:s.id,name:s.name})) };

  const api = c.getStudyById(hit.id);
  const inner = api.study?.() || api._study;
  const graphics = inner?._graphics;
  const indexes = graphics?._indexes || [];
  const prim = graphics?._primitivesCollection || {};

  const serializeData = (d, id) => {
    if (!d || typeof d !== 'object') return { id, raw: d };
    const o = { id };
    for (const k of Object.keys(d)) {
      const v = d[k];
      if (v == null || ['string','number','boolean'].includes(typeof v)) o[k] = v;
      else if (typeof v === 'bigint') o[k] = Number(v);
      else if (Array.isArray(v)) o[k] = v.slice(0, 8);
      else o[k] = typeof v;
    }
    return o;
  };

  const harvestKind = (kind) => {
    const items = [];
    const outer = prim[kind];
    if (!(outer instanceof Map)) return items;
    for (const [gk, mid] of outer) {
      if (!(mid instanceof Map)) continue;
      for (const [ik, collection] of mid) {
        const map = collection?._primitivesDataById;
        if (!(map instanceof Map)) continue;
        for (const [id, data] of map) {
          items.push({
            group: String(gk),
            innerKey: String(ik),
            ...serializeData(data, id),
            indexMapped: Array.isArray(indexes) && data && data.x != null ? indexes[data.x] : null,
            indexMapped1: Array.isArray(indexes) && data && data.x1 != null ? indexes[data.x1] : null,
            indexMapped2: Array.isArray(indexes) && data && data.x2 != null ? indexes[data.x2] : null,
          });
        }
      }
    }
    return items;
  };

  const labels = harvestKind('dwglabels');
  const lines = harvestKind('dwglines');
  const boxes = harvestKind('dwgboxes');
  const other = {};
  for (const k of Object.keys(prim)) {
    if (/dwglabels|dwglines|dwgboxes/.test(k)) continue;
    const items = harvestKind(k);
    if (items.length) other[k] = items.slice(0, 30);
  }

  // inputs
  const vals = api.getInputValues?.() || [];
  const infos = api.getInputsInfo?.() || [];
  const byId = Object.fromEntries(infos.map(i => [i.id, i]));
  const inputs = vals
    .filter(v => !['text','pineId','pineVersion','pineFeatures'].includes(v.id))
    .map(v => ({
      id: v.id,
      name: byId[v.id]?.name || null,
      type: byId[v.id]?.type || null,
      value: typeof v.value === 'string' && v.value.length > 120 ? v.value.slice(0,120)+'…' : v.value,
      options: byId[v.id]?.options || null,
    }));

  // styles / plots
  let styleInfo = [];
  try {
    const info = api.getStyleInfo?.() || {};
    styleInfo = Object.entries(info).map(([k,v]) => ({
      key: k,
      title: v?.title || null,
      visible: v?.visible?.value ?? v?.visible ?? null,
      color: v?.color?.value ?? v?.color ?? null,
    }));
  } catch (e) {}

  let plotMeta = [];
  try {
    const meta = inner?.metaInfo?.() || null;
    if (meta?.plots) plotMeta = meta.plots.map(p => ({ id:p.id, type:p.type, title:p.title }));
    if (meta?.styles) {
      plotMeta = plotMeta.concat(Object.entries(meta.styles).map(([k,v]) => ({ styleKey:k, title:v?.title, isHidden:v?.isHidden })));
    }
    if (meta?.inputs) {
      // already have inputs
    }
  } catch (e) {}

  // plot series sample: last non-null rows
  let plotSample = { size: null, lastRows: [], nonNullCounts: {} };
  try {
    const data = typeof inner.data === 'function' ? inner.data() : inner._data;
    const size = data?.size?.() ?? 0;
    plotSample.size = size;
    const unpack = (row) => {
      if (!row) return null;
      if (Array.isArray(row)) return row.slice(0, 40);
      if (typeof row === 'object') {
        // PlotRow-like
        if (typeof row.value === 'function') {
          try { return row.value(); } catch(e) {}
        }
        const keys = Object.keys(row);
        const o = {};
        for (const k of keys.slice(0, 40)) {
          const v = row[k];
          if (v == null || ['string','number','boolean'].includes(typeof v)) o[k] = v;
          else if (typeof v === 'function') {
            try { o[k] = v(); } catch(e) { o[k] = '[fn]'; }
          } else o[k] = typeof v;
        }
        return o;
      }
      return String(row).slice(0,80);
    };
    for (let i = Math.max(0, size - 25); i < size; i++) {
      let row = null;
      try { row = data.valueAt?.(i) ?? data.itemAt?.(i) ?? null; } catch(e) {}
      plotSample.lastRows.push({ i, row: unpack(row) });
    }
  } catch (e) {
    plotSample.error = String(e);
  }

  // legend/DOM fallback sample
  let legendTexts = [];
  try {
    const nodes = document.querySelectorAll('[class*="legend"], [class*="studyTitle"], [data-name*="legend"]');
    legendTexts = [...nodes].slice(0, 30).map(n => (n.textContent || '').trim()).filter(Boolean).slice(0, 40);
  } catch (e) {}

  return {
    ok: true,
    studyId: hit.id,
    name: hit.name,
    visible: api.isVisible?.(),
    dataLength: api.dataLength?.(),
    pineId: (vals.find(v => v.id==='pineId')||{}).value || null,
    pineVersion: (vals.find(v => v.id==='pineVersion')||{}).value || null,
    indexesLen: Array.isArray(indexes) ? indexes.length : null,
    indexesTail: Array.isArray(indexes) ? indexes.slice(-30) : null,
    counts: {
      labels: labels.length,
      lines: lines.length,
      boxes: boxes.length,
      otherKinds: Object.fromEntries(Object.entries(other).map(([k,v]) => [k, v.length])),
    },
    labelFieldUnion: [...new Set(labels.flatMap(l => Object.keys(l)))].sort(),
    lineFieldUnion: [...new Set(lines.flatMap(l => Object.keys(l)))].sort(),
    labels,
    lines,
    boxesSample: boxes.slice(0, 20),
    otherSample: other,
    inputs,
    styleInfo,
    plotMeta,
    plotSample,
    legendTexts,
    symbol: c.symbol?.(),
    resolution: c.resolution?.(),
  };
})()
"""


async def main():
    data = await evaluate_js(JS, timeout=90)
    path = Path(__file__).resolve().parent / "phase1_v1_lux_deep.json"
    path.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
    print("Wrote", path)

    if not data.get("ok"):
        print(json.dumps(data, indent=2))
        return

    labels = data.get("labels") or []
    lines = data.get("lines") or []

    text_counts = Counter(str(l.get("t") or l.get("text") or "").strip() for l in labels)
    # color / style distributions per text
    by_text = {}
    for l in labels:
        t = str(l.get("t") or l.get("text") or "").strip()
        by_text.setdefault(t, []).append(l)

    summary = {
        "studyId": data.get("studyId"),
        "name": data.get("name"),
        "visible": data.get("visible"),
        "dataLength": data.get("dataLength"),
        "counts": data.get("counts"),
        "labelFieldUnion": data.get("labelFieldUnion"),
        "lineFieldUnion": data.get("lineFieldUnion"),
        "labelTextCounts": dict(text_counts),
        "indexesLen": data.get("indexesLen"),
        "indexesTail": data.get("indexesTail"),
        "inputs": data.get("inputs"),
        "styleInfo": [s for s in (data.get("styleInfo") or []) if s.get("title")][:60],
        "plotMeta": data.get("plotMeta"),
        "plotSampleTail": (data.get("plotSample") or {}).get("lastRows", [])[-5:],
        "plotSampleSize": (data.get("plotSample") or {}).get("size"),
        "legendTexts": data.get("legendTexts"),
        "perTextStats": {},
        "lineStyleStats": {},
        "recentEvents": [],
        "sampleLabelsByType": {},
        "sampleLines": lines[:15],
        "linesWithMappedIndex": [
            ln for ln in lines if ln.get("indexMapped1") not in (None, -2000000) or ln.get("indexMapped2") not in (None, -2000000)
        ][:20],
    }

    for t, items in by_text.items():
        colors = Counter(str(i.get("color") or i.get("textColor") or i.get("style") or "") for i in items)
        ys = [i.get("y") for i in items if i.get("y") is not None]
        xs = [i.get("x") for i in items if i.get("x") is not None]
        mapped = [i.get("indexMapped") for i in items]
        mapped_ok = sum(1 for m in mapped if m not in (None, -2000000))
        summary["perTextStats"][t] = {
            "count": len(items),
            "colors": dict(colors),
            "yMin": min(ys) if ys else None,
            "yMax": max(ys) if ys else None,
            "xMin": min(xs) if xs else None,
            "xMax": max(xs) if xs else None,
            "mappedIndexOk": mapped_ok,
            "mappedIndexPlaceholder": sum(1 for m in mapped if m == -2000000),
            "samples": items[:3] + items[-2:],
        }
        summary["sampleLabelsByType"][t] = items[-5:]

    # line colors / y1 y2 patterns
    line_colors = Counter(str(l.get("color") or "") for l in lines)
    summary["lineStyleStats"] = {
        "colors": dict(line_colors),
        "extendCounts": Counter(str(l.get("extend") or l.get("extendLeft") or "") for l in lines),
        "withBothY": sum(1 for l in lines if l.get("y1") is not None and l.get("y2") is not None),
        "horizontalish": sum(1 for l in lines if l.get("y1") is not None and l.get("y1") == l.get("y2")),
        "sampleRecent": lines[-10:],
    }

    # recent non-placeholder events sorted by x
    events = []
    for l in labels:
        t = str(l.get("t") or "").strip()
        if t in ("CHoCH", "BOS", "IDM", "x"):
            events.append(l)
    events_sorted = sorted(events, key=lambda e: (e.get("x") is None, e.get("x") or -1))
    summary["recentEvents"] = events_sorted[-40:]

    # try correlate label with nearby line by price
    correlations = []
    for lab in events_sorted[-15:]:
        t = str(lab.get("t") or "").strip()
        y = lab.get("y")
        if y is None:
            continue
        near = []
        for ln in lines:
            ly = ln.get("y1") if ln.get("y1") == ln.get("y2") else None
            if ly is None:
                # still check y1
                ly = ln.get("y1")
            if ly is None:
                continue
            if abs(ly - y) < 1e-6 or abs(ly - y) < 0.01:
                near.append({
                    "lineId": ln.get("id"),
                    "y1": ln.get("y1"),
                    "y2": ln.get("y2"),
                    "x1": ln.get("x1"),
                    "x2": ln.get("x2"),
                    "color": ln.get("color"),
                    "index1": ln.get("indexMapped1"),
                    "index2": ln.get("indexMapped2"),
                })
        correlations.append({"label": t, "y": y, "x": lab.get("x"), "color": lab.get("color"), "nearLines": near[:5]})
    summary["labelLineCorrelations"] = correlations

    print(json.dumps(summary, indent=2, default=str))


if __name__ == "__main__":
    asyncio.run(main())
