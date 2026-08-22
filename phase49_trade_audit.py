"""Phase 49 — audit and normalize historical trade datasets. Does not write paper journals."""
from __future__ import annotations

import ast
import csv
import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from bar_dataset import load_dataset
from family_port_engine import INSTRUMENTS, resolve_path, signed_points
from gc_vwap_engine import session_window
from gc_vwap_paper import run_frozen_v2_on_bars
from nq_pdh_pdl import ny_date
from phase34_validate import assert_frozen

ROOT = Path(__file__).resolve().parent
REPORTS = ROOT / "reports" / "phase49_strategy_distributions"
NQ_SRC = ROOT / "reports" / "phase46_nq_dvp_frozen_proxy.csv"
ES_SRC = ROOT / "reports" / "phase46_es_dvp.csv"
GC_5M_ROOT = ROOT / "data" / "databento" / "GC" / "stitched"
GC_CACHE = REPORTS / "gc_v2_reconstructed_trades.csv"
GC_CACHE_META = REPORTS / "gc_v2_reconstructed_meta.json"

UNAVAILABLE = "UNAVAILABLE"

FIELD_SCHEMA = [
    "strategy",
    "source_path",
    "instrument",
    "trading_date",
    "entry_ts",
    "exit_ts",
    "entry_time",
    "exit_time",
    "direction",
    "outcome",
    "r_multiple",
    "pnl_R",
    "points",
    "gross_pnl",
    "net_pnl",
    "win_loss",
    "holding_time",
    "mae",
    "mfe",
    "mae_r",
    "mfe_r",
    "risk_points",
    "stop",
    "target",
    "fees_slippage_assumption",
    "session_day",
    "news_blackout",
]


def _parse_extras(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    text = str(raw or "").strip()
    if not text:
        return {}
    try:
        val = json.loads(text)
        return val if isinstance(val, dict) else {}
    except json.JSONDecodeError:
        try:
            val = ast.literal_eval(text)
            return val if isinstance(val, dict) else {}
        except (SyntaxError, ValueError):
            return {}


def _iso_time(ts: Any) -> str:
    if ts in (None, "", UNAVAILABLE):
        return UNAVAILABLE
    try:
        return datetime.fromtimestamp(int(float(ts)), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (TypeError, ValueError, OSError, OverflowError):
        return UNAVAILABLE


def _float(val: Any) -> Optional[float]:
    if val in (None, "", UNAVAILABLE):
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def _win_loss(r: Optional[float]) -> str:
    if r is None:
        return UNAVAILABLE
    if r > 1e-12:
        return "WIN"
    if r < -1e-12:
        return "LOSS"
    return "SCRATCH"


def _usd_from_points(instrument: str, points: Optional[float]) -> Optional[float]:
    if points is None:
        return None
    spec = INSTRUMENTS.get(instrument.upper())
    if not spec:
        return None
    return float(points) * float(spec["micro_usd"])


def normalize_row(row: dict[str, Any], *, strategy: str, source: str, instrument: str) -> dict[str, Any]:
    extras = _parse_extras(row.get("extras"))
    r = _float(row.get("r_multiple") if row.get("r_multiple") not in (None, "") else row.get("pnl_R"))
    points = _float(row.get("points"))
    risk = _float(row.get("risk_points") or extras.get("risk") or extras.get("risk_points"))
    entry_ts = row.get("entry_ts") or row.get("entry_timestamp")
    exit_ts = row.get("exit_ts") or row.get("exit_timestamp")
    hold = row.get("hold_sec")
    if hold in (None, ""):
        hold = row.get("holding_time")
    hold_val = _float(hold)
    net = _usd_from_points(instrument, points)
    mae = row.get("mae") if row.get("mae") not in (None, "") else row.get("mae_points")
    mfe = row.get("mfe") if row.get("mfe") not in (None, "") else row.get("mfe_points")
    return {
        "strategy": strategy,
        "source_path": source,
        "instrument": instrument,
        "trading_date": str(row.get("trading_date") or UNAVAILABLE),
        "entry_ts": entry_ts if entry_ts not in (None, "") else UNAVAILABLE,
        "exit_ts": exit_ts if exit_ts not in (None, "") else UNAVAILABLE,
        "entry_time": _iso_time(entry_ts),
        "exit_time": _iso_time(exit_ts),
        "direction": row.get("direction") or UNAVAILABLE,
        "outcome": row.get("outcome") or UNAVAILABLE,
        "r_multiple": r,
        "pnl_R": r,
        "points": points,
        "gross_pnl": UNAVAILABLE,
        "net_pnl": net if net is not None else UNAVAILABLE,
        "win_loss": _win_loss(r),
        "holding_time": hold_val if hold_val is not None else UNAVAILABLE,
        "mae": _float(mae) if mae not in (None, "") else UNAVAILABLE,
        "mfe": _float(mfe) if mfe not in (None, "") else UNAVAILABLE,
        "mae_r": _float(row.get("mae_r")) if row.get("mae_r") not in (None, "") else UNAVAILABLE,
        "mfe_r": _float(row.get("mfe_r")) if row.get("mfe_r") not in (None, "") else UNAVAILABLE,
        "risk_points": risk if risk is not None else UNAVAILABLE,
        "stop": _float(row.get("stop")) if row.get("stop") not in (None, "") else UNAVAILABLE,
        "target": _float(row.get("target")) if row.get("target") not in (None, "") else UNAVAILABLE,
        "fees_slippage_assumption": row.get("fees_slippage_assumption") or UNAVAILABLE,
        "session_day": str(row.get("trading_date") or UNAVAILABLE),
        "news_blackout": row.get("news_blackout"),
    }


def load_phase46_csv(path: Path, *, strategy: str, instrument: str, cost_note: str) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            if str(row.get("news_blackout")).lower() == "true":
                continue
            r = _float(row.get("r_multiple"))
            if r is None:
                continue
            rec = normalize_row(row, strategy=strategy, source=str(path.as_posix()), instrument=instrument)
            rec["fees_slippage_assumption"] = cost_note
            out.append(rec)
    out.sort(key=lambda x: (str(x["trading_date"]), str(x["entry_ts"])))
    return out


def reconstruct_gc_v2(*, persist: bool = False) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Replay frozen V2 on the canonical 5m stitch. persist must stay False."""
    if persist:
        raise RuntimeError("gc_replay_must_not_persist_to_paper_journal")
    frozen = assert_frozen()
    loaded = load_dataset("databento_GC_stitched", "5m", root=GC_5M_ROOT)
    meta: dict[str, Any] = {
        "ok": bool(loaded.get("ok")),
        "source": "data/databento/GC/stitched/databento_GC_stitched_5m.jsonl",
        "method": "run_frozen_v2_on_bars persist=False + 2R path resolve on 5m",
        "frozen": frozen,
        "paper_journal_written": False,
    }
    if not loaded.get("ok"):
        meta["error"] = loaded.get("error")
        return [], meta
    bars = list(loaded["bars"])
    replay = run_frozen_v2_on_bars(bars, persist=False, fill_ticks_adverse=1.0)
    if not replay.get("ok"):
        meta["error"] = replay.get("error_code") or replay
        return [], meta
    by_date: dict[str, list] = defaultdict(list)
    for b in bars:
        by_date[ny_date(int(b.time))].append(b)
    tick = float(INSTRUMENTS["GC"]["tick"])
    comm = float(INSTRUMENTS["GC"]["commission_points"])
    rows: list[dict[str, Any]] = []
    skipped_ambiguous = 0
    skipped_untriggered = 0
    for t in replay.get("trades") or []:
        if t.entry_trigger_timestamp is None or t.paper_fill_price is None or t.stop_price is None:
            skipped_untriggered += 1
            continue
        if not t.risk_points or float(t.risk_points) <= 0:
            skipped_untriggered += 1
            continue
        fill = float(t.paper_fill_price)
        stop = float(t.stop_price)
        risk = abs(fill - stop)
        if risk < tick:
            skipped_untriggered += 1
            continue
        bullish = t.direction in ("bullish", "long")
        target = fill + 2.0 * risk if bullish else fill - 2.0 * risk
        _, session_end, _ = session_window(t.trading_date)
        path = resolve_path(
            by_date.get(t.trading_date, []),
            entry_ts=int(t.entry_trigger_timestamp),
            direction="bullish" if bullish else "bearish",
            entry=fill,
            stop=stop,
            target=target,
            flatten_ts=int(session_end),
        )
        if path.get("outcome") == "AMBIGUOUS":
            skipped_ambiguous += 1
            continue
        pts = signed_points("bullish" if bullish else "bearish", fill, path.get("exit"))
        if pts is None:
            skipped_ambiguous += 1
            continue
        pts = float(pts) - tick - comm
        r = pts / risk
        hold = None
        if path.get("exit_ts") is not None:
            hold = int(path["exit_ts"]) - int(t.entry_trigger_timestamp)
        rec = normalize_row(
            {
                "trading_date": t.trading_date,
                "entry_ts": t.entry_trigger_timestamp,
                "exit_ts": path.get("exit_ts"),
                "direction": t.direction,
                "outcome": path.get("outcome"),
                "r_multiple": r,
                "points": pts,
                "hold_sec": hold,
                "mae": path.get("mae"),
                "mfe": path.get("mfe"),
                "mae_r": (float(path["mae"]) / risk) if path.get("mae") is not None else None,
                "mfe_r": (float(path["mfe"]) / risk) if path.get("mfe") is not None else None,
                "risk_points": risk,
                "stop": stop,
                "target": target,
                "news_blackout": False,
            },
            strategy="GC_VWAP_V2_FROZEN",
            source="reconstructed:frozen_v2_5m_stitch",
            instrument="GC",
        )
        rec["fees_slippage_assumption"] = "1_TICK_ADVERSE_ENTRY + 1_TICK_EXIT + 0.04pt_commission (family_port GC model)"
        rec["frozen_status"] = t.status
        rec["hit_2r_mfe"] = t.hit_2r
        rec["stop_hit_flag"] = t.stop_hit
        rows.append(rec)
    rows.sort(key=lambda x: (str(x["trading_date"]), str(x["entry_ts"])))
    meta.update(
        {
            "ok": True,
            "n_engine_rows": replay.get("trade_count"),
            "n_triggered": replay.get("triggered"),
            "n_resolved_engine": replay.get("resolved_n"),
            "n_sim_trades": len(rows),
            "skipped_untriggered": skipped_untriggered,
            "skipped_ambiguous": skipped_ambiguous,
            "frozen_config_hash": replay.get("frozen_config_hash"),
            "bar_count": len(bars),
            "cost_note": "1 tick adverse entry already in fill; minus 1 tick exit + 0.04 commission points",
        }
    )
    return rows, meta


def load_or_reconstruct_gc() -> tuple[list[dict[str, Any]], dict[str, Any]]:
    REPORTS.mkdir(parents=True, exist_ok=True)
    if GC_CACHE.exists() and GC_CACHE_META.exists():
        meta = json.loads(GC_CACHE_META.read_text(encoding="utf-8"))
        rows = []
        with GC_CACHE.open("r", encoding="utf-8", newline="") as fh:
            for rec in csv.DictReader(fh):
                rec["r_multiple"] = _float(rec.get("r_multiple"))
                rec["pnl_R"] = rec["r_multiple"]
                rec["points"] = _float(rec.get("points"))
                rec["risk_points"] = _float(rec.get("risk_points"))
                rec["net_pnl"] = _float(rec.get("net_pnl")) if rec.get("net_pnl") not in ("", UNAVAILABLE, None) else UNAVAILABLE
                rows.append(rec)
        meta["loaded_from_cache"] = True
        return rows, meta
    rows, meta = reconstruct_gc_v2(persist=False)
    if rows:
        write_csv(GC_CACHE, rows, schema=FIELD_SCHEMA)
        GC_CACHE_META.write_text(json.dumps(meta, indent=2, default=str), encoding="utf-8")
    meta["loaded_from_cache"] = False
    return rows, meta


def write_csv(path: Path, rows: list[dict[str, Any]], *, schema: Optional[list[str]] = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    keys = list(schema) if schema else []
    seen = set(keys)
    extra = []
    for r in rows:
        for k in r:
            if k not in seen:
                extra.append(k)
                seen.add(k)
    keys.extend(extra)
    with path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=keys, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)


def audit_all() -> dict[str, Any]:
    nq = load_phase46_csv(
        NQ_SRC,
        strategy="NQ_DVP_FROZEN",
        instrument="NQ",
        cost_note="phase46: 2*1tick + 0.20pt commission already in points/r_multiple",
    )
    es = load_phase46_csv(
        ES_SRC,
        strategy="ES_DVP_LOCKED_PHASE47_SOURCE_PHASE46",
        instrument="ES",
        cost_note="phase46: 2*1tick + 0.08pt commission already in points/r_multiple",
    )
    gc, gc_meta = load_or_reconstruct_gc()
    books = {
        "GC": {"trades": gc, "meta": gc_meta},
        "NQ": {
            "trades": nq,
            "meta": {
                "ok": bool(nq),
                "source": str(NQ_SRC.as_posix()),
                "method": "phase46 frozen-rule DVP replay CSV (not paper journal)",
            },
        },
        "ES": {
            "trades": es,
            "meta": {
                "ok": bool(es),
                "source": str(ES_SRC.as_posix()),
                "method": "phase46 ES DVP port CSV; locked candidate Phase 47; not frozen",
            },
        },
    }
    REPORTS.mkdir(parents=True, exist_ok=True)
    summaries = {}
    for name, blob in books.items():
        trades = blob["trades"]
        write_csv(REPORTS / f"{name.lower()}_chronological_trade_stream.csv", trades, schema=FIELD_SCHEMA)
        write_csv(REPORTS / f"{name.lower()}_bootstrap_trade_distribution.csv", trades, schema=FIELD_SCHEMA)
        rs = [float(t["r_multiple"]) for t in trades if t.get("r_multiple") is not None]
        dates = [t["trading_date"] for t in trades if t.get("trading_date") and t["trading_date"] != UNAVAILABLE]
        summaries[name] = {
            "source": blob["meta"],
            "number_of_trades": len(trades),
            "date_range": [min(dates), max(dates)] if dates else None,
            "instruments": sorted({t["instrument"] for t in trades}),
            "fields": {
                "entry_time": "available" if any(t.get("entry_time") != UNAVAILABLE for t in trades) else UNAVAILABLE,
                "exit_time": "available" if any(t.get("exit_time") != UNAVAILABLE for t in trades) else UNAVAILABLE,
                "gross_pnl": UNAVAILABLE,
                "net_pnl": "1_micro_usd_from_points" if any(t.get("net_pnl") != UNAVAILABLE for t in trades) else UNAVAILABLE,
                "pnl_R": "available" if rs else UNAVAILABLE,
                "win_loss": "derived_from_r_multiple",
                "holding_time": "available" if any(t.get("holding_time") != UNAVAILABLE for t in trades) else UNAVAILABLE,
                "MAE": "available" if any(t.get("mae") not in (None, "", UNAVAILABLE) for t in trades) else UNAVAILABLE,
                "MFE": "available" if any(t.get("mfe") not in (None, "", UNAVAILABLE) for t in trades) else UNAVAILABLE,
                "fees_slippage": trades[0]["fees_slippage_assumption"] if trades else UNAVAILABLE,
                "session_day_grouping": "trading_date",
            },
            "missing_warnings": _missing_warnings(name, trades),
        }
    payload = {
        "forward_paper_journals": {
            "gc": "journal/phase26_gc_vwap_v2_paper/paper_trades.jsonl N=0 — not used",
            "nq": "journal/phase30_nq_dvp_paper/paper_trades.jsonl N=0 — not used",
            "es": "journal/phase47_es_dvp_paper/paper_trades.jsonl N=0 — not used",
        },
        "originals_not_overwritten": [str(NQ_SRC.as_posix()), str(ES_SRC.as_posix())],
        "books": summaries,
    }
    (REPORTS / "audit_summary.json").write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    return {"books": books, "summary": payload}


def _missing_warnings(name: str, trades: list[dict[str, Any]]) -> list[str]:
    warns = [
        "gross_pnl USD at account size is UNAVAILABLE — R and 1-micro point dollars only",
        "T1 news flags not applied to simulation paths (historical trades are not news-labeled except phase46 news_blackout skips)",
    ]
    if not trades:
        warns.append(f"{name}: no trade-level dataset — do not fabricate")
    if name == "GC":
        warns.append("GC sample is the frozen 5m stitch window (~2025-08 to 2026-08), not the 2020–2026 v0 continuous used for NQ/ES")
        warns.append("GC realized R uses frozen 2R target vs stop vs session flatten on 5m; not the paper journal")
    if name in ("NQ", "ES"):
        if trades and all(t.get("mae") in (None, "", UNAVAILABLE) for t in trades):
            warns.append(f"{name}: MAE/MFE columns empty in phase46 CSV")
        if trades and all(t.get("holding_time") in (None, "", UNAVAILABLE) for t in trades):
            warns.append(f"{name}: hold_sec empty in phase46 CSV")
    if name == "ES":
        warns.append("ES is LOCKED_FORWARD_VALIDATION_CANDIDATE — not frozen, not in strategy_frozen/")
    return warns
