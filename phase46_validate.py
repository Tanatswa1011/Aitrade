"""Phase 46 — strategy-family portability (ES/CL). DRY_RUN. No freeze. No paper-journal writes."""
from __future__ import annotations

import csv
import json
import sys
from pathlib import Path
from typing import Any, Optional

from bar_dataset import load_dataset
from family_port_engine import (
    CLOCKS,
    INSTRUMENTS,
    PortTrade,
    TRAIN_END,
    V2_CFG,
    dvp_scaled_cfg,
    daily_corr,
    median_train_atr,
    mr_session_for,
    run_dvp,
    score,
    simulate_mr_trades,
    structural_mr,
)
from gc_vwap_paper import summarize_paper_journal as gc_paper_summary
from nq_databento import aggregate_1m_to_ny
from nq_drift_vwap_models import DVP_ORIGINAL
from nq_dvp_paper import summarize_paper_journal as nq_paper_summary
from nq_microstructure_models import FROZEN_GC_HASH, FROZEN_NQ_HASH
from phase34_validate import GC_FILE_SHA, NQ_FILE_SHA, assert_frozen, file_sha256

ROOT = Path(__file__).resolve().parent
REPORTS = ROOT / "reports"
VALIDATION = ROOT / "phase46_validation.json"
SPEC_PATH = ROOT / "phase46_spec.json"
DOCS = ROOT / "docs" / "PHASE46_STRATEGY_FAMILY_PORTABILITY.md"
CANDIDATE_DIR = ROOT / "strategy_candidates"

PATHS = {
    "ES": (ROOT / "data" / "databento" / "ES" / "stitched", "databento_ES_v0"),
    "CL": (ROOT / "data" / "databento" / "CL" / "stitched", "databento_CL_v0"),
    "NQ": (ROOT / "data" / "databento" / "NQ" / "stitched", "databento_NQ_stitched"),
    "GC": (ROOT / "data" / "databento" / "GC" / "stitched", "databento_GC_v0"),
}


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    keys = sorted({k for r in rows for k in r.keys()})
    with path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=keys, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: json.dumps(v, default=str) if isinstance(v, (dict, list)) else v for k, v in r.items()})


def load_1m(name: str) -> tuple[Optional[list], dict[str, Any]]:
    root, symbol = PATHS[name]
    loaded = load_dataset(symbol, "1m", root=root)
    meta = {"instrument": name, "path": str(root), "symbol": symbol}
    if not loaded.get("ok"):
        return None, {**meta, "ok": False, "error": loaded.get("error")}
    bars = list(loaded["bars"])
    meta.update({"ok": True, "n_bars": len(bars), "earliest": int(bars[0].time), "latest": int(bars[-1].time)})
    return bars, meta


def pack_candidate(name: str, family: str, trades, adverse_packs: dict, struct: dict, extra: dict) -> dict[str, Any]:
    primary = score(trades)
    inst = INSTRUMENTS[name]
    usd = float(inst["point_usd"])
    micro = float(inst["micro_usd"])
    avg_stop = (primary["full"] or {}).get("avg_stop_points")
    return {
        "instrument": name,
        "family": family,
        **extra,
        "structural": struct,
        "primary": primary,
        "ideal": adverse_packs.get("0"),
        "stress_2tick": adverse_packs.get("2"),
        "risk_usd": None if not avg_stop else {
            "full_contract": avg_stop * usd,
            "micro": avg_stop * micro,
        },
    }


def decide_port(*, coverage_ok, n_full, n_hold, full, hold, years, thresh_stable, corr_nq=None, corr_gc=None, stress_e=None) -> str:
    if not coverage_ok:
        return "DATA_QUALITY_BLOCKED"
    e = full.get("expectancy_r") if full.get("expectancy_r") is not None else full.get("expectancy_points")
    eh = hold.get("expectancy_r") if hold.get("expectancy_r") is not None else hold.get("expectancy_points")
    if e is None:
        return "PORTABLE_EDGE_REJECTED"
    pos_y = sum(1 for r in years if (r.get("n_resolved") or 0) >= 8 and (r.get("expectancy_r") or r.get("expectancy_points") or 0) > 0)
    y_n = sum(1 for r in years if (r.get("n_resolved") or 0) >= 8)
    costs_ok = stress_e is None or stress_e > 0
    one_year = y_n >= 4 and pos_y <= 2
    strong = (
        e > 0
        and (eh or 0) > 0
        and (full.get("profit_factor") or 0) > 1.05
        and thresh_stable
        and not one_year
        and y_n >= 4
        and pos_y >= max(3, y_n // 2)
    )
    redundant = False
    for c in (corr_nq, corr_gc):
        if c is not None and c.get("daily_pnl_correlation") is not None and abs(c["daily_pnl_correlation"]) >= 0.70:
            redundant = True
    if strong and n_full >= 100 and n_hold >= 25 and (full.get("expectancy_points") or 0) > 0:
        if not costs_ok:
            return "PORTABLE_EDGE_WEAK"
        if redundant:
            return "EDGE_FOUND_BUT_PORTFOLIO_REDUNDANT"
        return "PORTABLE_EDGE_FOUND"
    if strong and n_full >= 40:
        return "PORTABLE_PROMISING_NEEDS_MORE_DATA"
    if e > 0 and (eh or 0) <= 0:
        return "PORTABLE_EDGE_WEAK"
    if e <= 0:
        return "PORTABLE_EDGE_REJECTED"
    return "PORTABLE_EDGE_WEAK"


def research_mr(name: str, bars_1m, bars_5m, candidate: str, *, extras: bool = True) -> dict[str, Any]:
    print(f"  MR structural {name}", flush=True)
    struct = structural_mr(name, bars_5m, bars_1m, sigma=2.0)
    print(f"  MR trades {name} 2-sigma", flush=True)
    t_theo = simulate_mr_trades(name, bars_5m, bars_1m, sigma=2.0, target_r=2.0, adverse=0.0, candidate=candidate)
    tick = float(INSTRUMENTS[name]["tick"])

    def overlay(trades, adverse):
        return [
            PortTrade(
                instrument=t.instrument, family=t.family, candidate=t.candidate, trading_date=t.trading_date,
                direction=t.direction, entry_ts=t.entry_ts, entry=t.entry, stop=t.stop, target=t.target,
                exit_ts=t.exit_ts, exit=t.exit, outcome=t.outcome,
                points=None if t.points is None else float(t.points) - 2.0 * adverse * tick,
                r_multiple=(None if t.points is None or not (t.extras or {}).get("risk") else (float(t.points) - 2.0 * adverse * tick) / float(t.extras["risk"])),
                mfe=t.mfe, mae=t.mae, hold_sec=t.hold_sec, news_blackout=t.news_blackout, extras=t.extras,
            )
            for t in trades
        ]

    t1 = overlay(t_theo, 1.0)
    t2 = overlay(t_theo, 2.0)
    neighbors = {}
    targets = {}
    if extras:
        for sig in (1.5, 2.5):
            tn = simulate_mr_trades(name, bars_5m, bars_1m, sigma=sig, target_r=2.0, adverse=0.0, candidate=candidate)
            neighbors[str(sig)] = score(overlay(tn, 1.0))["full"]
        for r in (1.0, 1.5, 3.0):
            tr = simulate_mr_trades(name, bars_5m, bars_1m, sigma=2.0, target_r=r, adverse=0.0, candidate=candidate)
            targets[str(r)] = score(overlay(tr, 1.0))["full"]
    pack = pack_candidate(name, "VWAP_MR", t1, {"0": score(t_theo)["full"], "2": score(t2)["full"]}, struct, {"candidate": candidate, "sigma_neighbors": neighbors, "targets": targets})
    base_e = pack["primary"]["full"].get("expectancy_r") or pack["primary"]["full"].get("expectancy_points")
    neigh_e = [neighbors[k].get("expectancy_r") or neighbors[k].get("expectancy_points") for k in neighbors]
    stable = True
    if extras and base_e is not None and base_e > 0 and any(x is not None and x <= 0 for x in neigh_e):
        stable = False
    pack["flags"] = {"threshold_stable": stable}
    pack["trades"] = t1
    return pack


def research_dvp(name: str, bars_1m, bars_5m, bars_15m, cfg, candidate: str, literal_cfg=None, *, extras: bool = True) -> dict[str, Any]:
    print(f"  DVP {name} {candidate}", flush=True)
    t_theo, guard = run_dvp(name, bars_1m, bars_5m, bars_15m, cfg, adverse=0.0, candidate=candidate)
    n = len([t for t in t_theo if t.outcome not in ("NEWS_BLACKOUT",)])
    pos = sum(1 for t in t_theo if t.points is not None and t.points > 0)
    struct = {
        "n_trades_gross": n,
        "win_rate_gross": None if not n else pos / n,
        **guard,
    }
    tick = float(INSTRUMENTS[name]["tick"])

    def overlay(trades, adverse):
        return [
            PortTrade(
                instrument=t.instrument, family=t.family, candidate=t.candidate, trading_date=t.trading_date,
                direction=t.direction, entry_ts=t.entry_ts, entry=t.entry, stop=t.stop, target=t.target,
                exit_ts=t.exit_ts, exit=t.exit, outcome=t.outcome,
                points=None if t.points is None else float(t.points) - 2.0 * adverse * tick,
                r_multiple=(None if t.points is None or not (t.extras or {}).get("risk") else (float(t.points) - 2.0 * adverse * tick) / float(t.extras["risk"])),
                mfe=t.mfe, mae=t.mae, hold_sec=t.hold_sec, news_blackout=t.news_blackout, extras=t.extras,
            )
            for t in trades
        ]

    t1 = overlay(t_theo, 1.0)
    t2 = overlay(t_theo, 2.0)
    neighbors = {}
    literal = None
    if extras:
        scale = float((cfg.extras or {}).get("scale") or 1.0)
        for nb in (0.9, 1.1):
            ncfg = dvp_scaled_cfg(scale, tick, neighbor=nb)
            tn, _ = run_dvp(name, bars_1m, bars_5m, bars_15m, ncfg, adverse=0.0, candidate=candidate)
            neighbors[str(nb)] = score(overlay(tn, 1.0))["full"]
        if literal_cfg is not None:
            tl, _ = run_dvp(name, bars_1m, bars_5m, bars_15m, literal_cfg, adverse=0.0, candidate=candidate + "_LITERAL")
            literal = score(overlay(tl, 1.0))["full"]
    pack = pack_candidate(name, "DVP", t1, {"0": score(t_theo)["full"], "2": score(t2)["full"]}, struct, {
        "candidate": candidate,
        "cfg": cfg.to_dict(),
        "neighbors": neighbors,
        "literal_80_40_50": literal,
        "guard": guard,
    })
    base_e = pack["primary"]["full"].get("expectancy_r") or pack["primary"]["full"].get("expectancy_points")
    neigh_e = [neighbors[k].get("expectancy_r") or neighbors[k].get("expectancy_points") for k in neighbors]
    stable = True
    if base_e is not None and base_e > 0 and any(x is not None and x <= 0 for x in neigh_e):
        stable = False
    pack["flags"] = {"threshold_stable": stable}
    pack["trades"] = t1
    return pack


def slim(pack: dict[str, Any]) -> dict[str, Any]:
    out = {k: v for k, v in pack.items() if k != "trades"}
    return out


def overall_status(cells: dict[str, str]) -> str:
    vals = [cells.get(k) for k in ("ES_MR", "ES_DVP", "CL_MR", "CL_DVP")]
    found = {"PORTABLE_EDGE_FOUND", "EDGE_FOUND_BUT_PORTFOLIO_REDUNDANT", "PORTABLE_PROMISING_NEEDS_MORE_DATA"}
    if any(v == "DATA_QUALITY_BLOCKED" for v in vals) and all(v in (None, "DATA_QUALITY_BLOCKED") for v in vals):
        return "DATA_QUALITY_BLOCKED"
    if any(v == "PORTABLE_EDGE_FOUND" for v in vals):
        return "STRATEGY_FAMILY_PORTABILITY_CONFIRMED"
    if any(v in found for v in vals):
        return "PARTIAL_PORTABILITY_FOUND"
    if all(v in ("PORTABLE_EDGE_REJECTED", "PORTABLE_EDGE_WEAK", "PORTABLE_STRUCTURAL_EFFECT_ONLY", None) for v in vals):
        return "ORIGINAL_MARKETS_ONLY"
    return "PORTABILITY_RESEARCH_INCONCLUSIVE"


def recommendation(verdict: str, cells: dict[str, str]) -> str:
    if verdict == "STRATEGY_FAMILY_PORTABILITY_CONFIRMED":
        return "PROMOTE_PORT_FOR_FURTHER_VALIDATION"
    if verdict == "PARTIAL_PORTABILITY_FOUND" and cells.get("ES_DVP") == "EDGE_FOUND_BUT_PORTFOLIO_REDUNDANT" and cells.get("CL_MR") in ("PORTABLE_EDGE_REJECTED", "PORTABLE_EDGE_WEAK") and cells.get("CL_DVP") in ("PORTABLE_EDGE_REJECTED", "PORTABLE_EDGE_WEAK"):
        return "TEST_TIER_2_MARKETS"
    if verdict in ("ORIGINAL_MARKETS_ONLY", "PORTABILITY_RESEARCH_INCONCLUSIVE"):
        return "KEEP_ONLY_ORIGINAL_GC_AND_NQ"
    if verdict == "PARTIAL_PORTABILITY_FOUND":
        return "PROMOTE_PORT_FOR_FURTHER_VALIDATION"
    return "KEEP_ONLY_ORIGINAL_GC_AND_NQ"


def main() -> dict[str, Any]:
    frozen_before = assert_frozen()
    spec = json.loads(SPEC_PATH.read_text(encoding="utf-8"))
    gc_fwd = {k: v for k, v in gc_paper_summary().items() if k != "rows"}
    nq_fwd = {k: v for k, v in nq_paper_summary().items() if k != "rows"}
    n_gc = int(gc_fwd.get("resolved") or gc_fwd.get("resolved_n") or 0)
    n_nq = int(nq_fwd.get("resolved") or nq_fwd.get("resolved_n") or nq_fwd.get("n_resolved") or 0)

    print("loading NQ (ATR + frozen DVP proxy + NQ MR diagnostic)", flush=True)
    nq_bars, nq_meta = load_1m("NQ")
    results: dict[str, Any] = {}
    atr = {}
    nq_dvp_daily = {}
    gc_mr_daily = {}
    if nq_bars:
        nq5 = aggregate_1m_to_ny(nq_bars, 5)
        nq15 = aggregate_1m_to_ny(nq_bars, 15)
        atr["NQ"] = median_train_atr(nq_bars, CLOCKS["NQ_DVP"]["vwap_reset"], CLOCKS["NQ_DVP"]["force_close"])
        print(f"  ATR14 median TRAIN NQ={atr['NQ']}", flush=True)
        nq_dvp = research_dvp("NQ", nq_bars, nq5, nq15, DVP_ORIGINAL, "NQ_DVP_FROZEN", extras=False)
        nq_dvp["status"] = "FROZEN"
        nq_dvp_daily = (nq_dvp["primary"]["full"].get("daily_pnl") or {})
        nq_mr = research_mr("NQ", nq_bars, nq5, "NQ_VWAP_MR_DIAGNOSTIC", extras=False)
        nq_mr["status"] = decide_port(
            coverage_ok=True,
            n_full=nq_mr["primary"]["full"].get("n_resolved") or 0,
            n_hold=nq_mr["primary"]["holdout"].get("n_resolved") or 0,
            full=nq_mr["primary"]["full"],
            hold=nq_mr["primary"]["holdout"],
            years=nq_mr["primary"]["full"].get("years") or [],
            thresh_stable=nq_mr["flags"]["threshold_stable"],
            stress_e=(nq_mr.get("stress_2tick") or {}).get("expectancy_r") or (nq_mr.get("stress_2tick") or {}).get("expectancy_points"),
        )
        results["NQ_DVP"] = slim(nq_dvp)
        results["NQ_MR"] = slim(nq_mr)
        _write_csv(REPORTS / "phase46_nq_dvp_frozen_proxy.csv", [t.to_dict() for t in nq_dvp["trades"][:5000]])
        nq_bars = nq5 = nq15 = None
    else:
        results["NQ_DVP"] = {"status": "DATA_QUALITY_BLOCKED", "data": nq_meta}
        results["NQ_MR"] = {"status": "DATA_QUALITY_BLOCKED", "data": nq_meta}

    print("loading ES", flush=True)
    es_bars, es_meta = load_1m("ES")
    if es_bars and atr.get("NQ"):
        es5 = aggregate_1m_to_ny(es_bars, 5)
        es15 = aggregate_1m_to_ny(es_bars, 15)
        atr["ES"] = median_train_atr(es_bars, CLOCKS["ES_DVP"]["vwap_reset"], CLOCKS["ES_DVP"]["force_close"])
        scale_es = (atr["ES"] or 0) / atr["NQ"]
        print(f"  ATR ES={atr['ES']} scale={scale_es}", flush=True)
        es_mr = research_mr("ES", es_bars, es5, "ES_VWAP_MR_V2_PORT")
        es_cfg = dvp_scaled_cfg(scale_es, 0.25, neighbor=1.0)
        es_dvp = research_dvp("ES", es_bars, es5, es15, es_cfg, "ES_DVP_PORT", literal_cfg=DVP_ORIGINAL)
        corr_es_mr_nq = daily_corr(es_mr["primary"]["full"].get("daily_pnl") or {}, nq_dvp_daily)
        corr_es_dvp_nq = daily_corr(es_dvp["primary"]["full"].get("daily_pnl") or {}, nq_dvp_daily)
        es_mr["status"] = decide_port(
            coverage_ok=True,
            n_full=es_mr["primary"]["full"].get("n_resolved") or 0,
            n_hold=es_mr["primary"]["holdout"].get("n_resolved") or 0,
            full=es_mr["primary"]["full"], hold=es_mr["primary"]["holdout"],
            years=es_mr["primary"]["full"].get("years") or [],
            thresh_stable=es_mr["flags"]["threshold_stable"],
            corr_nq=corr_es_mr_nq,
            stress_e=(es_mr.get("stress_2tick") or {}).get("expectancy_r") or (es_mr.get("stress_2tick") or {}).get("expectancy_points"),
        )
        es_dvp["status"] = decide_port(
            coverage_ok=True,
            n_full=es_dvp["primary"]["full"].get("n_resolved") or 0,
            n_hold=es_dvp["primary"]["holdout"].get("n_resolved") or 0,
            full=es_dvp["primary"]["full"], hold=es_dvp["primary"]["holdout"],
            years=es_dvp["primary"]["full"].get("years") or [],
            thresh_stable=es_dvp["flags"]["threshold_stable"],
            corr_nq=corr_es_dvp_nq,
            stress_e=(es_dvp.get("stress_2tick") or {}).get("expectancy_r") or (es_dvp.get("stress_2tick") or {}).get("expectancy_points"),
        )
        es_mr["corr_nq_dvp"] = corr_es_mr_nq
        es_dvp["corr_nq_dvp"] = corr_es_dvp_nq
        es_dvp["atr_scale"] = scale_es
        _write_csv(REPORTS / "phase46_es_vwap_mr.csv", [t.to_dict() for t in es_mr["trades"]])
        _write_csv(REPORTS / "phase46_es_dvp.csv", [t.to_dict() for t in es_dvp["trades"]])
        results["ES_MR"] = slim(es_mr)
        results["ES_DVP"] = slim(es_dvp)
        es_bars = es5 = es15 = None
    else:
        results["ES_MR"] = {"status": "DATA_QUALITY_BLOCKED", "data": es_meta}
        results["ES_DVP"] = {"status": "DATA_QUALITY_BLOCKED", "data": es_meta}

    print("loading CL", flush=True)
    cl_bars, cl_meta = load_1m("CL")
    if cl_bars is None:
        print("  CL missing — attempting download", flush=True)
        from phase46_download import main as dl
        dl()
        cl_bars, cl_meta = load_1m("CL")
    if cl_bars and atr.get("NQ"):
        cl5 = aggregate_1m_to_ny(cl_bars, 5)
        cl15 = aggregate_1m_to_ny(cl_bars, 15)
        atr["CL"] = median_train_atr(cl_bars, CLOCKS["CL_DVP"]["vwap_reset"], CLOCKS["CL_DVP"]["force_close"])
        scale_cl = (atr["CL"] or 0) / atr["NQ"]
        print(f"  ATR CL={atr['CL']} scale={scale_cl}", flush=True)
        cl_mr = research_mr("CL", cl_bars, cl5, "CL_VWAP_MR_V2_PORT")
        cl_cfg = dvp_scaled_cfg(scale_cl, 0.01, neighbor=1.0)
        from nq_drift_vwap_models import DVPStrategyConfig
        literal_cl = DVPStrategyConfig(long_stop_points=80, long_target_points=40, short_stop_points=80, short_target_points=50)
        cl_dvp = research_dvp("CL", cl_bars, cl5, cl15, cl_cfg, "CL_DVP_PORT", literal_cfg=literal_cl)
        corr_cl_mr = daily_corr(cl_mr["primary"]["full"].get("daily_pnl") or {}, nq_dvp_daily)
        corr_cl_dvp = daily_corr(cl_dvp["primary"]["full"].get("daily_pnl") or {}, nq_dvp_daily)
        cl_mr["status"] = decide_port(
            coverage_ok=True,
            n_full=cl_mr["primary"]["full"].get("n_resolved") or 0,
            n_hold=cl_mr["primary"]["holdout"].get("n_resolved") or 0,
            full=cl_mr["primary"]["full"], hold=cl_mr["primary"]["holdout"],
            years=cl_mr["primary"]["full"].get("years") or [],
            thresh_stable=cl_mr["flags"]["threshold_stable"],
            corr_nq=corr_cl_mr,
            stress_e=(cl_mr.get("stress_2tick") or {}).get("expectancy_r") or (cl_mr.get("stress_2tick") or {}).get("expectancy_points"),
        )
        cl_dvp["status"] = decide_port(
            coverage_ok=True,
            n_full=cl_dvp["primary"]["full"].get("n_resolved") or 0,
            n_hold=cl_dvp["primary"]["holdout"].get("n_resolved") or 0,
            full=cl_dvp["primary"]["full"], hold=cl_dvp["primary"]["holdout"],
            years=cl_dvp["primary"]["full"].get("years") or [],
            thresh_stable=cl_dvp["flags"]["threshold_stable"],
            corr_nq=corr_cl_dvp,
            stress_e=(cl_dvp.get("stress_2tick") or {}).get("expectancy_r") or (cl_dvp.get("stress_2tick") or {}).get("expectancy_points"),
        )
        cl_mr["corr_nq_dvp"] = corr_cl_mr
        cl_dvp["corr_nq_dvp"] = corr_cl_dvp
        cl_dvp["atr_scale"] = scale_cl
        _write_csv(REPORTS / "phase46_cl_vwap_mr.csv", [t.to_dict() for t in cl_mr["trades"]])
        _write_csv(REPORTS / "phase46_cl_dvp.csv", [t.to_dict() for t in cl_dvp["trades"]])
        results["CL_MR"] = slim(cl_mr)
        results["CL_DVP"] = slim(cl_dvp)
        cl_bars = cl5 = cl15 = None
    else:
        results["CL_MR"] = {"status": "DATA_QUALITY_BLOCKED", "data": cl_meta}
        results["CL_DVP"] = {"status": "DATA_QUALITY_BLOCKED", "data": cl_meta}

    print("loading GC (V2 proxy + DVP diagnostic)", flush=True)
    gc_bars, gc_meta = load_1m("GC")
    if gc_bars and atr.get("NQ"):
        gc5 = aggregate_1m_to_ny(gc_bars, 5)
        gc15 = aggregate_1m_to_ny(gc_bars, 15)
        atr["GC"] = median_train_atr(gc_bars, CLOCKS["GC_DVP"]["vwap_reset"], CLOCKS["GC_DVP"]["force_close"])
        scale_gc = (atr["GC"] or 0) / atr["NQ"]
        gc_mr = research_mr("GC", gc_bars, gc5, "GC_VWAP_MR_GCv0_PROXY", extras=False)
        gc_mr["status"] = "FROZEN_MECHANISM_PROXY_NOT_THE_FROZEN_FILE"
        gc_mr_daily = gc_mr["primary"]["full"].get("daily_pnl") or {}
        gc_cfg = dvp_scaled_cfg(scale_gc, 0.10, neighbor=1.0)
        gc_dvp = research_dvp("GC", gc_bars, gc5, gc15, gc_cfg, "GC_DVP_DIAGNOSTIC", extras=False)
        gc_dvp["status"] = decide_port(
            coverage_ok=True,
            n_full=gc_dvp["primary"]["full"].get("n_resolved") or 0,
            n_hold=gc_dvp["primary"]["holdout"].get("n_resolved") or 0,
            full=gc_dvp["primary"]["full"], hold=gc_dvp["primary"]["holdout"],
            years=gc_dvp["primary"]["full"].get("years") or [],
            thresh_stable=gc_dvp["flags"]["threshold_stable"],
            stress_e=(gc_dvp.get("stress_2tick") or {}).get("expectancy_r") or (gc_dvp.get("stress_2tick") or {}).get("expectancy_points"),
        )
        gc_dvp["atr_scale"] = scale_gc
        results["GC_MR"] = slim(gc_mr)
        results["GC_DVP"] = slim(gc_dvp)
        # attach GC correlation onto ES/CL if present
        for key in ("ES_MR", "ES_DVP", "CL_MR", "CL_DVP"):
            if key in results and "primary" in results[key]:
                results[key]["corr_gc_v2_proxy"] = daily_corr(results[key]["primary"]["full"].get("daily_pnl") or {}, gc_mr_daily)
        gc_bars = gc5 = gc15 = None
    else:
        results["GC_MR"] = {"status": "DATA_QUALITY_BLOCKED", "data": gc_meta}
        results["GC_DVP"] = {"status": "DATA_QUALITY_BLOCKED", "data": gc_meta}

    cells = {
        "GC_MR": "FROZEN",
        "NQ_DVP": "FROZEN",
        "ES_MR": (results.get("ES_MR") or {}).get("status"),
        "ES_DVP": (results.get("ES_DVP") or {}).get("status"),
        "CL_MR": (results.get("CL_MR") or {}).get("status"),
        "CL_DVP": (results.get("CL_DVP") or {}).get("status"),
        "NQ_MR": (results.get("NQ_MR") or {}).get("status"),
        "GC_DVP": (results.get("GC_DVP") or {}).get("status"),
    }
    verdict = overall_status(cells)
    rec = recommendation(verdict, cells)
    frozen_after = assert_frozen()
    matrix_rows = [
        {"family": "VWAP_MR", "GC": "FROZEN", "NQ": cells["NQ_MR"], "ES": cells["ES_MR"], "CL": cells["CL_MR"]},
        {"family": "DVP", "GC": cells["GC_DVP"], "NQ": "FROZEN", "ES": cells["ES_DVP"], "CL": cells["CL_DVP"]},
    ]
    _write_csv(REPORTS / "phase46_portability_matrix.csv", matrix_rows)

    best = None
    order = ["PORTABLE_EDGE_FOUND", "EDGE_FOUND_BUT_PORTFOLIO_REDUNDANT", "PORTABLE_PROMISING_NEEDS_MORE_DATA"]
    for st in order:
        for key in ("CL_MR", "ES_MR", "CL_DVP", "ES_DVP"):
            if (results.get(key) or {}).get("status") == st:
                best = key
                break
        if best:
            break

    candidate_written = False
    candidate_path = None
    if best and (results[best].get("status") == "PORTABLE_EDGE_FOUND"):
        inst, fam = best.split("_", 1)
        path = CANDIDATE_DIR / f"phase46_{inst}_{fam}.json"
        path.write_text(json.dumps({
            "status": "RESEARCH_CANDIDATE",
            "phase": 46,
            "instrument": inst,
            "family": fam,
            "not_frozen": True,
            "metrics": results[best],
        }, indent=2, default=str), encoding="utf-8")
        candidate_written = True
        candidate_path = str(path)

    payload = {
        "ok": frozen_after["ok"],
        "phase": 46,
        "status": "RESEARCH_COMPLETE",
        "execution": "DRY_RUN_NO_BROKER",
        "verdict": verdict,
        "recommendation": rec,
        "GC_FORWARD_N": n_gc,
        "NQ_FORWARD_N": n_nq,
        "forward": {"gc": gc_fwd, "nq": nq_fwd, "note": "Journals read-only; no synthetic forward trades."},
        "frozen_before": {**frozen_before, "gc": FROZEN_GC_HASH, "nq": FROZEN_NQ_HASH},
        "frozen_after": {**frozen_after, "gc": FROZEN_GC_HASH, "nq": FROZEN_NQ_HASH},
        "file_sha": {
            "gc": file_sha256(ROOT / "strategy_frozen" / "gc_vwap_v2_phase26.json"),
            "nq": file_sha256(ROOT / "strategy_frozen" / "nq_dvp_phase30.json"),
            "gc_expected": GC_FILE_SHA,
            "nq_expected": NQ_FILE_SHA,
        },
        "atr_train_median": atr,
        "cells": cells,
        "best_portable_candidate": best,
        "candidate_written": candidate_written,
        "candidate_path": candidate_path,
        "spec": spec,
        "results": results,
        "V2_cfg_id": V2_CFG.candidate_id,
        "train_end": TRAIN_END,
    }
    # strip daily_pnl blobs from JSON size
    for v in payload["results"].values():
        prim = v.get("primary") or {}
        for block in (prim.get("full"), prim.get("train"), prim.get("holdout")):
            if isinstance(block, dict):
                block.pop("daily_pnl", None)
    VALIDATION.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    print(json.dumps({"verdict": verdict, "rec": rec, "cells": cells, "GC_FORWARD_N": n_gc, "NQ_FORWARD_N": n_nq, "atr": atr, "best": best}, indent=2), flush=True)
    return payload


if __name__ == "__main__":
    main()
    sys.exit(0)
