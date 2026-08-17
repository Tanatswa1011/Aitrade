"""Analyze LuxAlgo direction encoding and swing plots."""
from __future__ import annotations

import asyncio
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from cdp import evaluate_js

# Colors from inputs
BULL = 4286683400
BEAR = 4283585279
ACCENT = 4287003512  # inducement/sweep color


def direction_from_tci(tci):
    if tci == BULL:
        return "bullish"
    if tci == BEAR:
        return "bearish"
    if tci == ACCENT:
        return "accent/idm-sweep"
    return f"other:{tci}"


JS_PLOTS = r"""
(() => {
  const c = window.TradingViewApi.activeChart();
  const hit = (c.getAllStudies()||[]).find(s => /LuxAlgo|Market Structure with Inducements/i.test(s.name||''));
  const api = c.getStudyById(hit.id);
  const inner = api.study?.() || api._study;
  const data = typeof inner.data === 'function' ? inner.data() : inner._data;
  const size = data?.size?.() ?? 0;

  const unpackRow = (row) => {
    if (row == null) return null;
    // Try common TV plot row shapes
    const out = { ctor: row.constructor?.name };
    try {
      if (typeof row === 'object') {
        const proto = Object.getOwnPropertyNames(Object.getPrototypeOf(row)||{});
        out.proto = proto.slice(0, 40);
        // value() often returns array of plot values
        if (typeof row.value === 'function') {
          try { out.value = row.value(); } catch(e) { out.valueErr = String(e); }
        }
        if (typeof row.index === 'function') {
          try { out.index = row.index(); } catch(e) {}
        }
        for (const k of ['values','_values','items','_items']) {
          try {
            if (row[k] != null) {
              const v = typeof row[k] === 'function' ? row[k]() : row[k];
              out[k] = Array.isArray(v) ? v.slice(0, 20) : (v?.constructor?.name || typeof v);
            }
          } catch(e) {}
        }
      }
    } catch(e) { out.err = String(e); }
    return out;
  };

  // find non-null rows by scanning
  const samples = [];
  let nonNull = 0;
  for (let i = 0; i < size; i++) {
    let row = null;
    try { row = data.valueAt?.(i); } catch(e) {}
    if (row == null) continue;
    nonNull++;
    if (samples.length < 5 || i >= size - 5) {
      samples.push({ i, unpacked: unpackRow(row) });
    }
  }

  // also try performance / last
  let first=null, last=null;
  try { first = unpackRow(data.first?.()); } catch(e) {}
  try { last = unpackRow(data.last?.()); } catch(e) {}

  // materialize labels again with direction fields only summary done in python

  return {
    size,
    nonNull,
    samples,
    first,
    last,
    metaPlots: (inner.metaInfo?.()?.plots || []).map(p => ({id:p.id, type:p.type})),
    metaStyles: Object.entries(inner.metaInfo?.()?.styles || {}).map(([k,v]) => ({key:k, title:v?.title})),
  };
})()
"""


async def main():
    deep = json.loads(Path("phase1_v1_lux_deep.json").read_text(encoding="utf-8"))
    labels = deep["labels"]
    lines = deep["lines"]

    # Direction analysis by text + st + tci
    matrix = defaultdict(Counter)
    for lab in labels:
        t = str(lab.get("t") or "").strip()
        st = lab.get("st")
        tci = lab.get("tci")
        ci = lab.get("ci")
        direction = direction_from_tci(tci)
        matrix[t][(st, direction, tci)] += 1

    # Lines: style/color vs nearest label type
    line_by_y = {}
    for ln in lines:
        y = ln.get("y1")
        if y is None:
            continue
        line_by_y.setdefault(y, []).append(ln)

    pair_stats = Counter()
    for lab in labels:
        t = str(lab.get("t") or "").strip()
        y = lab.get("y")
        if y is None:
            continue
        for ln in line_by_y.get(y, []):
            pair_stats[(t, ln.get("st"), direction_from_tci(ln.get("ci")), ln.get("ci"))] += 1

    # Recent structured events with decoded direction
    events = []
    for lab in labels:
        t = str(lab.get("t") or "").strip()
        if t not in ("CHoCH", "BOS", "IDM", "x"):
            continue
        y = lab.get("y")
        matched_line = None
        for ln in line_by_y.get(y, []) if y is not None else []:
            matched_line = ln
            break
        events.append({
            "type": t,
            "price": y,
            "drawX": lab.get("x"),
            "barIndex": lab.get("indexMapped"),
            "labelStyle": lab.get("st"),  # lup / ldn
            "textColor": lab.get("tci"),
            "direction": direction_from_tci(lab.get("tci")),
            "line": None if not matched_line else {
                "id": matched_line.get("id"),
                "x1": matched_line.get("x1"),
                "x2": matched_line.get("x2"),
                "style": matched_line.get("st"),  # sol / dot
                "color": matched_line.get("ci"),
                "lineDirection": direction_from_tci(matched_line.get("ci")),
                "bar1": matched_line.get("indexMapped1"),
                "bar2": matched_line.get("indexMapped2"),
            },
        })
    events.sort(key=lambda e: (e["drawX"] is None, e["drawX"] or -1))

    # Count mapped timestamps
    map_stats = {}
    for t in ("CHoCH", "BOS", "IDM", "x"):
        subset = [e for e in events if e["type"] == t]
        map_stats[t] = {
            "count": len(subset),
            "withPrice": sum(1 for e in subset if e["price"] is not None),
            "withBarIndex": sum(1 for e in subset if e["barIndex"] not in (None, -2000000)),
            "placeholderBar": sum(1 for e in subset if e["barIndex"] == -2000000),
            "withMatchedLine": sum(1 for e in subset if e["line"]),
            "directionCounts": dict(Counter(e["direction"] for e in subset)),
            "styleCounts": dict(Counter(e["labelStyle"] for e in subset)),
        }

    plot_info = await evaluate_js(JS_PLOTS, timeout=60)

    # Check st vs direction correlation for CHoCH/BOS
    choch_corr = Counter()
    bos_corr = Counter()
    for e in events:
        if e["type"] == "CHoCH":
            choch_corr[(e["labelStyle"], e["direction"])] += 1
        if e["type"] == "BOS":
            bos_corr[(e["labelStyle"], e["direction"])] += 1

    # Active/current structure: latest bullish and bearish CHoCH/BOS by x
    def latest(typ, direction=None):
        cand = [e for e in events if e["type"] == typ and e["price"] is not None]
        if direction:
            cand = [e for e in cand if e["direction"] == direction]
        return cand[-1] if cand else None

    out = {
        "bullColor": BULL,
        "bearColor": BEAR,
        "accentColor": ACCENT,
        "labelMatrix": {k: {str(kk): vv for kk, vv in v.items()} for k, v in matrix.items()},
        "labelLinePairStats": {str(k): v for k, v in pair_stats.most_common(40)},
        "mapStats": map_stats,
        "chochStyleDirection": {str(k): v for k, v in choch_corr.items()},
        "bosStyleDirection": {str(k): v for k, v in bos_corr.items()},
        "latest": {
            "CHoCH_any": latest("CHoCH"),
            "CHoCH_bullish": latest("CHoCH", "bullish"),
            "CHoCH_bearish": latest("CHoCH", "bearish"),
            "BOS_any": latest("BOS"),
            "BOS_bullish": latest("BOS", "bullish"),
            "BOS_bearish": latest("BOS", "bearish"),
            "IDM_any": latest("IDM"),
            "sweep_x_any": latest("x"),
        },
        "recent40": events[-40:],
        "plotInfo": plot_info,
        # Frequency: CHoCH rarer than BOS — good for "first shift"
        "frequency": {t: map_stats[t]["count"] for t in map_stats},
    }

    path = Path("phase1_v1_lux_analysis.json")
    path.write_text(json.dumps(out, indent=2, default=str), encoding="utf-8")
    print(json.dumps({
        "mapStats": map_stats,
        "chochStyleDirection": out["chochStyleDirection"],
        "bosStyleDirection": out["bosStyleDirection"],
        "labelLinePairStatsTop": dict(list(out["labelLinePairStats"].items())[:20]),
        "latest": out["latest"],
        "frequency": out["frequency"],
        "plotInfo": {
            "size": plot_info.get("size"),
            "nonNull": plot_info.get("nonNull"),
            "metaStyles": plot_info.get("metaStyles"),
            "sample0": (plot_info.get("samples") or [None])[0],
            "last": plot_info.get("last"),
        },
        "recent10": events[-10:],
    }, indent=2, default=str))


if __name__ == "__main__":
    asyncio.run(main())
