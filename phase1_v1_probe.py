"""Phase 1 v1 — ICT + LuxAlgo accessibility using proven graphics path."""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from cdp import evaluate_js, health_check

OUT = Path(__file__).resolve().parent

JS = r"""
(async () => {
  const c = window.TradingViewApi.activeChart();
  const studies = c.getAllStudies() || [];

  const serializeVal = (v) => {
    if (v == null || ['string','number','boolean'].includes(typeof v)) return v;
    if (typeof v === 'function') return '[Function]';
    if (typeof v === 'bigint') return Number(v);
    if (Array.isArray(v)) return v.slice(0, 12).map(serializeVal);
    if (v instanceof Map) {
      const o = { __mapSize: v.size };
      let i = 0;
      for (const [k, val] of v) {
        o[String(k)] = serializeVal(val);
        if (++i >= 8) break;
      }
      return o;
    }
    try {
      const o = {};
      for (const k of Object.keys(v).slice(0, 40)) {
        const x = v[k];
        if (x == null || ['string','number','boolean'].includes(typeof x)) o[k] = x;
        else if (Array.isArray(x)) o[k] = x.slice(0, 6);
        else o[k] = typeof x;
      }
      return o;
    } catch (e) {
      return String(v).slice(0, 80);
    }
  };

  const getCollection = (prim, kind, group) => {
    const outer = prim?.[kind];
    if (!outer) return null;
    for (const [gk, mid] of outer) {
      if (String(gk) !== group) continue;
      if (mid instanceof Map) {
        for (const [ik, collection] of mid) return { groupKey: String(gk), innerKey: String(ik), collection };
      }
      return { groupKey: String(gk), collection: mid };
    }
    // fallback first
    for (const [gk, mid] of outer) {
      if (mid instanceof Map) {
        for (const [ik, collection] of mid) return { groupKey: String(gk), innerKey: String(ik), collection };
      }
      return { groupKey: String(gk), collection: mid };
    }
    return null;
  };

  const dumpPrimItems = (collection, limit = 80) => {
    if (!collection) return [];
    let data = null;
    try {
      if (typeof collection.data === 'function') data = collection.data();
      else if (collection._data) data = collection._data;
      else if (collection instanceof Map) data = collection;
    } catch (e) {
      return [{ error: String(e) }];
    }
    const items = [];
    try {
      if (data instanceof Map) {
        let i = 0;
        for (const [id, prim] of data) {
          const d = prim?.data?.() || prim?._data || prim;
          items.push({ id: String(id), ...(serializeVal(d) || {}) });
          if (++i >= limit) break;
        }
      } else if (Array.isArray(data)) {
        for (const prim of data.slice(0, limit)) {
          const d = prim?.data?.() || prim?._data || prim;
          items.push(serializeVal(d));
        }
      } else if (data && typeof data.size === 'function') {
        const n = Math.min(limit, data.size());
        for (let i = 0; i < n; i++) {
          const prim = data.valueAt?.(i) || data.itemAt?.(i);
          const d = prim?.data?.() || prim?._data || prim;
          items.push(serializeVal(d));
        }
      }
    } catch (e) {
      items.push({ error: String(e) });
    }
    return items;
  };

  const probe = async (s) => {
    const api = c.getStudyById(s.id);
    try {
      if (typeof api.graphicsViewsReady === 'function') {
        await Promise.race([
          api.graphicsViewsReady(),
          new Promise((r) => setTimeout(r, 1500)),
        ]);
      }
    } catch (e) {}

    const inner = api.study?.() || api._study;
    const graphics = inner?._graphics;
    const indexes = graphics?._indexes || [];
    const prim = graphics?._primitivesCollection || {};

    const kinds = {};
    try {
      for (const k of Object.keys(prim)) {
        const outer = prim[k];
        const groups = [];
        if (outer instanceof Map) {
          for (const [gk, mid] of outer) {
            let size = null;
            let sampleKeys = [];
            if (mid instanceof Map) {
              size = mid.size;
              sampleKeys = [...mid.keys()].slice(0, 5).map(String);
            } else {
              size = mid?.size ?? null;
            }
            groups.push({ group: String(gk), size, sampleKeys });
          }
        }
        kinds[k] = groups;
      }
    } catch (e) {
      kinds.__error = String(e);
    }

    const boxesInfo = getCollection(prim, 'dwgboxes', 'boxes') || getCollection(prim, 'dwgboxes', 'boxes');
    const labelsInfo = getCollection(prim, 'dwglabels', 'labels');
    const linesInfo = getCollection(prim, 'dwglines', 'lines');

    // try all groups under each kind
    const allBoxes = [];
    const allLabels = [];
    const allLines = [];
    const allShapes = [];

    const harvest = (kind, sink) => {
      const outer = prim[kind];
      if (!(outer instanceof Map)) return;
      for (const [gk, mid] of outer) {
        if (mid instanceof Map) {
          for (const [ik, collection] of mid) {
            const items = dumpPrimItems(collection, 120);
            sink.push(...items.map((it) => ({ group: String(gk), inner: String(ik), ...it })));
          }
        } else {
          sink.push(...dumpPrimItems(mid, 120).map((it) => ({ group: String(gk), ...it })));
        }
      }
    };

    harvest('dwgboxes', allBoxes);
    harvest('dwglabels', allLabels);
    harvest('dwglines', allLines);
    for (const k of Object.keys(prim)) {
      if (!/dwgboxes|dwglabels|dwglines/.test(k)) harvest(k, allShapes);
    }

    // inputs
    const vals = api.getInputValues?.() || [];
    const infos = api.getInputsInfo?.() || [];
    const byId = Object.fromEntries(infos.map((i) => [i.id, i]));
    const inputs = vals
      .filter((v) => !['text', 'pineId', 'pineVersion', 'pineFeatures'].includes(v.id))
      .map((v) => ({
        id: v.id,
        name: byId[v.id]?.name || null,
        value:
          typeof v.value === 'string' && v.value.length > 120
            ? v.value.slice(0, 120) + '…'
            : v.value,
      }));

    const keyInputs = inputs.filter((v) => {
      const n = `${v.name || ''} ${v.id} ${v.value}`;
      return /asia|london|new.?york|session|time.?zone|timezone|kill|hide|gmt|est|utc|choch|bos|idm|induc|sweep|structure|swing/i.test(
        n
      );
    });

    // style titles
    let styleTitles = [];
    try {
      const info = api.getStyleInfo?.() || {};
      styleTitles = Object.entries(info)
        .map(([k, v]) => ({ key: k, title: v?.title || null, visible: v?.visible?.value ?? v?.visible ?? null }))
        .filter((x) => x.title);
    } catch (e) {}

    // plot meta
    let plotMeta = [];
    try {
      const meta = inner?.metaInfo?.() || null;
      if (meta?.plots) plotMeta = meta.plots.map((p) => ({ id: p.id, type: p.type, title: p.title }));
      if (meta?.styles) {
        plotMeta = plotMeta.concat(
          Object.entries(meta.styles).map(([k, v]) => ({ styleKey: k, title: v?.title }))
        );
      }
    } catch (e) {}

    // session derivation for ICT
    const labelTexts = [
      ...new Set(allLabels.map((l) => l.t || l.text || l.txt).filter(Boolean)),
    ];
    const sessions = {};
    for (const lab of allLabels) {
      const text = String(lab.t || lab.text || lab.txt || '').trim();
      if (!/^(Asia|London|New York)$/i.test(text)) continue;
      const y = lab.y;
      let best = null;
      let bestDiff = Infinity;
      for (const b of allBoxes) {
        if (b.y1 == null || b.y2 == null) continue;
        const hi = Math.max(b.y1, b.y2);
        const lo = Math.min(b.y1, b.y2);
        const d = Math.abs(hi - y);
        if (d < bestDiff) {
          bestDiff = d;
          best = { high: hi, low: lo, x1: b.x1, x2: b.x2, y1: b.y1, y2: b.y2, id: b.id };
        }
      }
      const key = text;
      if (!sessions[key]) sessions[key] = [];
      sessions[key].push({
        labelY: y,
        labelX: lab.x,
        box: best,
        matchDiff: bestDiff,
      });
    }
    const sessionsLatest = Object.fromEntries(
      Object.entries(sessions).map(([k, arr]) => [k, arr[arr.length - 1]])
    );

    // LuxAlgo-like event texts
    const eventTexts = allLabels
      .map((l) => ({
        text: l.t || l.text || l.txt,
        y: l.y,
        x: l.x,
        y1: l.y1,
        y2: l.y2,
      }))
      .filter((l) => l.text);
    const interesting = eventTexts.filter((l) =>
      /choch|bos|idm|induc|sweep|mss|hh|hl|lh|ll|break|structure|ob|fvg/i.test(String(l.text))
    );

    return {
      id: s.id,
      name: s.name,
      visible: (() => { try { return api.isVisible?.(); } catch (e) { return null; } })(),
      dataLength: (() => { try { return api.dataLength?.(); } catch (e) { return null; } })(),
      hasGraphics: !!graphics,
      indexesLen: Array.isArray(indexes) ? indexes.length : null,
      indexesTail: Array.isArray(indexes) ? indexes.slice(-20) : null,
      primitiveKinds: kinds,
      boxCount: allBoxes.length,
      labelCount: allLabels.length,
      lineCount: allLines.length,
      shapeCount: allShapes.length,
      labelTexts,
      sessionsLatest,
      sessionsCounts: Object.fromEntries(Object.entries(sessions).map(([k, a]) => [k, a.length])),
      boxesSample: allBoxes.slice(0, 8),
      labelsSample: allLabels.slice(0, 15),
      linesSample: allLines.slice(0, 10),
      shapesSample: allShapes.slice(0, 20),
      interestingEvents: interesting.slice(0, 80),
      allEventTexts: eventTexts.slice(0, 100),
      keyInputs,
      inputsCount: inputs.length,
      styleTitles: styleTitles.slice(0, 80),
      plotMeta: plotMeta.slice(0, 80),
      pineId: (vals.find((v) => v.id === 'pineId') || {}).value || null,
      pineVersion: (vals.find((v) => v.id === 'pineVersion') || {}).value || null,
    };
  };

  const results = [];
  for (const s of studies) results.push(await probe(s));

  return {
    ok: true,
    symbol: c.symbol?.(),
    resolution: c.resolution?.(),
    studies: studies.map((s) => ({ id: s.id, name: s.name })),
    results,
  };
})()
"""


async def main():
    print("HEALTH", json.dumps(health_check(), indent=2))
    data = await evaluate_js(JS, timeout=90)
    path = OUT / "phase1_v1_probe_result.json"
    path.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
    print("Wrote", path)

    summary = {
        "symbol": data.get("symbol"),
        "resolution": data.get("resolution"),
        "studies": data.get("studies"),
        "reports": [],
    }
    for r in data.get("results") or []:
        summary["reports"].append(
            {
                "name": r.get("name"),
                "id": r.get("id"),
                "visible": r.get("visible"),
                "dataLength": r.get("dataLength"),
                "boxCount": r.get("boxCount"),
                "labelCount": r.get("labelCount"),
                "lineCount": r.get("lineCount"),
                "shapeCount": r.get("shapeCount"),
                "labelTexts": r.get("labelTexts"),
                "sessionsLatest": r.get("sessionsLatest"),
                "sessionsCounts": r.get("sessionsCounts"),
                "interestingEvents": (r.get("interestingEvents") or [])[:25],
                "allEventTextsSample": (r.get("allEventTexts") or [])[:25],
                "keyInputs": r.get("keyInputs"),
                "styleTitles": [
                    t for t in (r.get("styleTitles") or []) if t.get("title")
                ][:40],
                "plotMeta": r.get("plotMeta"),
                "primitiveKinds": r.get("primitiveKinds"),
                "pineId": r.get("pineId"),
                "pineVersion": r.get("pineVersion"),
            }
        )
    print(json.dumps(summary, indent=2, default=str))


if __name__ == "__main__":
    asyncio.run(main())
