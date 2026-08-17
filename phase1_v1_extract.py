"""Extract ICT session levels + wait/check for LuxAlgo."""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from cdp import evaluate_js

JS = r"""
(() => {
  const c = window.TradingViewApi.activeChart();
  const studies = c.getAllStudies() || [];

  const serializeData = (d) => {
    if (!d || typeof d !== 'object') return d;
    const o = { id: d.id };
    for (const k of Object.keys(d)) {
      const v = d[k];
      if (v == null || ['string','number','boolean'].includes(typeof v)) o[k] = v;
      else if (typeof v === 'bigint') o[k] = Number(v);
    }
    return o;
  };

  const harvest = (collection) => {
    const items = [];
    if (!collection) return items;
    const map = collection._primitivesDataById;
    if (!(map instanceof Map)) return items;
    for (const [id, data] of map) {
      items.push(serializeData({ id, ...(data || {}) }));
    }
    return items;
  };

  const probe = (s) => {
    const api = c.getStudyById(s.id);
    const inner = api.study?.() || api._study;
    const graphics = inner?._graphics;
    const indexes = graphics?._indexes || [];
    const prim = graphics?._primitivesCollection || {};

    const boxes = [];
    const labels = [];
    const lines = [];
    const other = {};

    for (const [kind, outer] of Object.entries(prim)) {
      if (!(outer instanceof Map)) continue;
      for (const [, mid] of outer) {
        if (!(mid instanceof Map)) continue;
        for (const [, collection] of mid) {
          const items = harvest(collection);
          if (/box/i.test(kind)) boxes.push(...items);
          else if (/label/i.test(kind)) labels.push(...items);
          else if (/line/i.test(kind)) lines.push(...items);
          else {
            if (!other[kind]) other[kind] = [];
            other[kind].push(...items.slice(0, 40));
          }
        }
      }
    }

    const vals = api.getInputValues?.() || [];
    const infos = api.getInputsInfo?.() || [];
    const byId = Object.fromEntries(infos.map((i) => [i.id, i]));
    const inputs = vals
      .filter((v) => !['text','pineId','pineVersion','pineFeatures'].includes(v.id))
      .map((v) => ({
        id: v.id,
        name: byId[v.id]?.name || null,
        value: typeof v.value === 'string' && v.value.length > 100 ? v.value.slice(0,100)+'…' : v.value,
      }));

    let styleTitles = [];
    try {
      const info = api.getStyleInfo?.() || {};
      styleTitles = Object.entries(info).map(([k,v]) => ({ key:k, title:v?.title || null })).filter(x => x.title);
    } catch (e) {}

    let plotMeta = [];
    try {
      const meta = inner?.metaInfo?.() || null;
      if (meta?.styles) {
        plotMeta = Object.entries(meta.styles).map(([k,v]) => ({ styleKey:k, title:v?.title }));
      }
      if (meta?.plots) {
        plotMeta = plotMeta.concat(meta.plots.map(p => ({ id:p.id, type:p.type })));
      }
    } catch (e) {}

    // session levels
    const sessions = {};
    for (const lab of labels) {
      const text = String(lab.t || lab.text || '').trim();
      if (!/^(Asia|London|New York)$/i.test(text)) continue;
      const y = lab.y;
      let best = null, bestDiff = Infinity;
      for (const b of boxes) {
        if (b.y1 == null || b.y2 == null) continue;
        const hi = Math.max(b.y1, b.y2);
        const lo = Math.min(b.y1, b.y2);
        const d = Math.abs(hi - y);
        if (d < bestDiff) {
          bestDiff = d;
          best = {
            id: b.id,
            high: hi,
            low: lo,
            x1: b.x1,
            x2: b.x2,
            y1: b.y1,
            y2: b.y2,
            index1: Array.isArray(indexes) ? indexes[b.x1] : null,
            index2: Array.isArray(indexes) ? indexes[b.x2] : null,
          };
        }
      }
      if (!sessions[text]) sessions[text] = [];
      sessions[text].push({
        labelY: y,
        labelX: lab.x,
        index: Array.isArray(indexes) ? indexes[lab.x] : null,
        box: best,
        matchDiff: bestDiff,
      });
    }

    const sessionsLatest = Object.fromEntries(
      Object.entries(sessions).map(([k, arr]) => [k, arr[arr.length - 1]])
    );

    const eventTexts = labels.map(l => ({
      text: l.t || l.text,
      y: l.y,
      x: l.x,
      index: Array.isArray(indexes) ? indexes[l.x] : null,
    })).filter(l => l.text);

    const interesting = eventTexts.filter(l =>
      /choch|c\.?ho\.?c\.?h|bos|idm|induc|sweep|mss|break|structure|\bHH\b|\bHL\b|\bLH\b|\bLL\b/i.test(String(l.text))
    );

    return {
      id: s.id,
      name: s.name,
      visible: (() => { try { return api.isVisible?.(); } catch(e){ return null; }})(),
      dataLength: (() => { try { return api.dataLength?.(); } catch(e){ return null; }})(),
      indexesLen: Array.isArray(indexes) ? indexes.length : null,
      indexesTail: Array.isArray(indexes) ? indexes.slice(-25) : null,
      boxCount: boxes.length,
      labelCount: labels.length,
      lineCount: lines.length,
      labelTexts: [...new Set(eventTexts.map(e => e.text))],
      sessionsCounts: Object.fromEntries(Object.entries(sessions).map(([k,a]) => [k, a.length])),
      sessionsLatest,
      // keep last 3 of each session for history proof
      sessionsRecent: Object.fromEntries(
        Object.entries(sessions).map(([k, arr]) => [k, arr.slice(-3)])
      ),
      boxesSample: boxes.slice(-6),
      labelsSample: labels.slice(-12),
      linesSample: lines.slice(0, 8),
      otherSample: Object.fromEntries(Object.entries(other).map(([k,v]) => [k, v.slice(0, 10)])),
      interestingEvents: interesting.slice(0, 100),
      allEventTexts: eventTexts.slice(0, 120),
      keyInputs: inputs.filter(v => {
        const n = `${v.name||''} ${v.id} ${v.value}`;
        return /asia|london|new.?york|session|time.?zone|timezone|kill|hide|gmt|choch|bos|idm|induc|sweep|structure|swing|mss/i.test(n);
      }),
      timezone: inputs.find(v => /timezone/i.test(v.name||'')),
      styleTitles: styleTitles.slice(0, 100),
      plotMeta: plotMeta.slice(0, 100),
      pineId: (vals.find(v => v.id==='pineId')||{}).value || null,
      pineVersion: (vals.find(v => v.id==='pineVersion')||{}).value || null,
    };
  };

  return {
    ok: true,
    symbol: c.symbol?.(),
    resolution: c.resolution?.(),
    studies: studies.map(s => ({ id:s.id, name:s.name })),
    hasLuxAlgo: studies.some(s => /luxalgo|market structure.*inducement|inducements.*sweep/i.test(s.name)),
    hasICT: studies.some(s => /ICT Sessions/i.test(s.name)),
    results: studies.map(probe),
  };
})()
"""

async def main():
    data = await evaluate_js(JS, timeout=60)
    path = Path(__file__).resolve().parent / "phase1_v1_extract.json"
    path.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
    print("Wrote", path)
    summary = {
        "symbol": data.get("symbol"),
        "resolution": data.get("resolution"),
        "studies": data.get("studies"),
        "hasLuxAlgo": data.get("hasLuxAlgo"),
        "hasICT": data.get("hasICT"),
        "reports": [],
    }
    for r in data.get("results") or []:
        summary["reports"].append({
            "name": r.get("name"),
            "id": r.get("id"),
            "visible": r.get("visible"),
            "dataLength": r.get("dataLength"),
            "boxCount": r.get("boxCount"),
            "labelCount": r.get("labelCount"),
            "lineCount": r.get("lineCount"),
            "labelTexts": r.get("labelTexts"),
            "sessionsCounts": r.get("sessionsCounts"),
            "sessionsLatest": r.get("sessionsLatest"),
            "sessionsRecent": r.get("sessionsRecent"),
            "interestingEvents": (r.get("interestingEvents") or [])[:40],
            "allEventTextsSample": (r.get("allEventTexts") or [])[:40],
            "timezone": r.get("timezone"),
            "keyInputs": r.get("keyInputs"),
            "styleTitles": [t for t in (r.get("styleTitles") or []) if t.get("title")][:50],
            "plotMeta": r.get("plotMeta"),
            "indexesTail": r.get("indexesTail"),
            "pineId": r.get("pineId"),
            "pineVersion": r.get("pineVersion"),
        })
    print(json.dumps(summary, indent=2, default=str))

if __name__ == "__main__":
    asyncio.run(main())
