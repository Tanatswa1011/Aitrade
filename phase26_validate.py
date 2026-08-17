"""Phase 26 — freeze V2 + paper-validation initialization (no retuning)."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from gc_vwap_freeze import (
    CANDIDATE_NAME,
    FROZEN_JSON,
    FROZEN_MD,
    FROZEN_STRATEGY_VERSION,
    assert_source_frozen_match,
    build_frozen_document,
    candidate_to_config,
    frozen_config_hash,
    load_phase25_v2_benchmark,
    load_phase25_v2_candidate,
    semantic_payload,
    write_frozen_files,
)
from gc_vwap_paper import (
    JOURNAL_DIR,
    PAPER_TRADES_PATH,
    ensure_journal_dir,
    paper_campaign_status,
    sample_label,
    summarize_paper_journal,
)
from phase18_metrics import theoretical_fixed_target_expectancy
from phase22_validate import _write_csv

REPORTS = Path("reports")
VALIDATION_JSON = Path("phase26_validation.json")


def _hold_field(hold: Optional[dict], *keys, default=None):
    if not hold:
        return default
    for k in keys:
        if hold.get(k) is not None:
            return hold.get(k)
    return default


def build_forward_delta(hold: Optional[dict], paper: dict[str, Any]) -> list[dict[str, Any]]:
    """paper - historical holdout; empty metrics when N=0."""
    metrics = [
        ("stop_rate", "stop_rate", paper.get("stop_rate")),
        ("r1_rate", "r1_rate", paper.get("r1_rate")),
        ("r15_rate", "r15_rate", paper.get("r15_rate")),
        ("r2_rate", "r2_rate", paper.get("r2_rate")),
        ("r3_rate", "r3_rate", paper.get("r3_rate")),
        ("E1R", "theoretical_1r_expectancy", paper.get("e1r")),
        ("E1.5R", "theoretical_1_5r_expectancy", paper.get("e15r")),
        ("E2R", "theoretical_2r_expectancy", paper.get("e2r")),
        ("E3R", "theoretical_3r_expectancy", paper.get("e3r")),
        ("median_mfe_r", "median_mfe_r", paper.get("median_mfe_r")),
        ("median_mae_r", "median_mae_r", paper.get("median_mae_r")),
    ]
    rows = []
    for label, hold_key, paper_val in metrics:
        hist = None if not hold else hold.get(hold_key)
        delta = None
        if hist is not None and paper_val is not None:
            delta = float(paper_val) - float(hist)
        rows.append(
            {
                "metric": label,
                "historical_holdout": hist,
                "paper": paper_val,
                "delta_paper_minus_holdout": delta,
                "note": "N=0 — no forward conclusion" if paper.get("resolved", 0) == 0 else "",
            }
        )
    return rows


def paper_metrics_from_journal() -> dict[str, Any]:
    summary = summarize_paper_journal()
    rows = [
        r
        for r in summary.get("rows") or []
        if r.get("entry_price") is not None
        and r.get("status") != "AMBIGUOUS"
        and r.get("outcome") != "AMBIGUOUS_INTRABAR"
        and (
            r.get("status")
            in (
                "STOP_HIT",
                "TARGET_1R_HIT",
                "TARGET_1_5R_HIT",
                "TARGET_2R_HIT",
                "TARGET_3R_HIT",
                "VWAP_HIT",
            )
            or r.get("outcome") in ("STOP_HIT", "TARGET_HIT")
            or r.get("stop_hit") is True
            or r.get("hit_1r") is not None
        )
    ]
    # Prefer rows with resolved excursion outcomes
    resolved = [
        r
        for r in rows
        if r.get("mfe_r") is not None
        or r.get("stop_hit") is not None
        or r.get("outcome") in ("STOP_HIT", "TARGET_HIT")
    ]
    n = len(resolved)
    if n == 0:
        return {
            **{k: summary[k] for k in ("paper_trades", "triggered", "resolved", "ambiguous", "expired", "invalid")},
            "stop_rate": None,
            "r1_rate": None,
            "r15_rate": None,
            "r2_rate": None,
            "r3_rate": None,
            "vwap_hit_rate": None,
            "e1r": None,
            "e15r": None,
            "e2r": None,
            "e3r": None,
            "median_mfe_r": None,
            "median_mae_r": None,
            "mean_mfe_r": None,
            "mean_mae_r": None,
            "sample_label": sample_label(0),
            "campaign_status": paper_campaign_status(0),
        }

    stop_n = sum(1 for r in resolved if r.get("stop_hit") or r.get("outcome") == "STOP_HIT" or r.get("status") == "STOP_HIT")
    r1 = sum(1 for r in resolved if r.get("hit_1r"))
    r15 = sum(1 for r in resolved if r.get("hit_1_5r"))
    r2 = sum(1 for r in resolved if r.get("hit_2r"))
    r3 = sum(1 for r in resolved if r.get("hit_3r"))
    vwap_n = sum(1 for r in resolved if r.get("vwap_hit"))
    mfe = [float(r["mfe_r"]) for r in resolved if r.get("mfe_r") is not None]
    mae = [float(r["mae_r"]) for r in resolved if r.get("mae_r") is not None]

    def _med(xs):
        if not xs:
            return None
        s = sorted(xs)
        mid = len(s) // 2
        return s[mid] if len(s) % 2 else 0.5 * (s[mid - 1] + s[mid])

    def _mean(xs):
        return None if not xs else sum(xs) / len(xs)

    return {
        "paper_trades": summary["paper_trades"],
        "triggered": summary["triggered"],
        "resolved": n,
        "ambiguous": summary["ambiguous"],
        "expired": summary["expired"],
        "invalid": summary["invalid"],
        "stop_rate": stop_n / n,
        "r1_rate": r1 / n,
        "r15_rate": r15 / n,
        "r2_rate": r2 / n,
        "r3_rate": r3 / n,
        "vwap_hit_rate": vwap_n / n if n else None,
        "e1r": theoretical_fixed_target_expectancy(target_r=1.0, target_hits=r1, stop_hits=stop_n, resolved_n=n),
        "e15r": theoretical_fixed_target_expectancy(target_r=1.5, target_hits=r15, stop_hits=stop_n, resolved_n=n),
        "e2r": theoretical_fixed_target_expectancy(target_r=2.0, target_hits=r2, stop_hits=stop_n, resolved_n=n),
        "e3r": theoretical_fixed_target_expectancy(target_r=3.0, target_hits=r3, stop_hits=stop_n, resolved_n=n),
        "median_mfe_r": _med(mfe),
        "median_mae_r": _med(mae),
        "mean_mfe_r": _mean(mfe),
        "mean_mae_r": _mean(mae),
        "sample_label": sample_label(n),
        "campaign_status": paper_campaign_status(n),
    }


def run_phase26_init() -> dict[str, Any]:
    REPORTS.mkdir(parents=True, exist_ok=True)
    ensure_journal_dir()

    source = load_phase25_v2_candidate()
    cfg = candidate_to_config(source)
    match = assert_source_frozen_match(cfg, source)
    if not match["ok"]:
        raise RuntimeError(f"FROZEN_CONFIG_MISMATCH:{match}")

    # Rewrite freeze with current semantic payload (deterministic hash)
    write_info = write_frozen_files(build_frozen_document(freeze_timestamp=datetime.now(tz=timezone.utc).isoformat()))
    doc = json.loads(FROZEN_JSON.read_text(encoding="utf-8"))
    semantic = semantic_payload(cfg)
    # Recompute hash must be stable
    assert doc["frozen_config_hash"] == frozen_config_hash(semantic)

    benchmark = load_phase25_v2_benchmark()
    train = benchmark.get("train") or {}
    hold = benchmark.get("holdout") or {}
    wf = benchmark.get("walkforward") or []
    cost = benchmark.get("cost_sensitivity") or []
    struct = benchmark.get("structural_reversion") or {}

    paper = paper_metrics_from_journal()
    delta_rows = build_forward_delta(hold, paper)

    freeze_row = {
        "candidate_name": CANDIDATE_NAME,
        "strategy_version": FROZEN_STRATEGY_VERSION,
        "frozen_config_hash": doc["frozen_config_hash"],
        "engine_config_hash": doc["engine_config_hash"],
        "source_candidate_path": doc["source_candidate_path"],
        "source_hash_match": match["hashes_equal"],
        "session": f"{doc['session']['start']}-{doc['session']['end']} {doc['session']['timezone']}",
        "sigma_threshold": 2.0,
        "confirmation": "BAND_RECLAIM",
        "entry": "FROZEN_2SIG_RETEST",
        "max_entry_bars": 6,
        "stop": "extension_sequence_extreme",
        "targets": "1R,1.5R,2R,3R,VWAP_diagnostic",
        "status": "paper_validation",
        "broker_execution": False,
    }

    paper_summary = [
        {
            "resolved_n": paper.get("resolved", 0),
            "triggered": paper.get("triggered", 0),
            "ambiguous": paper.get("ambiguous", 0),
            "expired": paper.get("expired", 0),
            "invalid": paper.get("invalid", 0),
            "sample_label": paper.get("sample_label"),
            "campaign_status": paper.get("campaign_status"),
            "note": "Initialization — no forward paper observations yet"
            if int(paper.get("resolved") or 0) == 0
            else "",
        }
    ]

    fill_rows = [
        {"scenario": "IDEAL_TOUCH", "ticks_adverse": 0, "role": "overlay", "primary": False},
        {"scenario": "1_TICK_ADVERSE", "ticks_adverse": 1, "role": "primary_paper_assumption", "primary": True},
        {"scenario": "2_TICK_ADVERSE", "ticks_adverse": 2, "role": "overlay", "primary": False},
    ]

    direction_rows = [
        {"direction": "upper_extension_to_short", "filter_applied": False, "note": "diagnostic only"},
        {"direction": "lower_extension_to_long", "filter_applied": False, "note": "diagnostic only"},
    ]

    tod_rows = [
        {"bucket": "08:50-09:30", "filter_applied": False, "note": "diagnostic only — no TOD filter in Phase26"},
        {"bucket": "09:30-10:30", "filter_applied": False, "note": "diagnostic only — no TOD filter in Phase26"},
        {"bucket": "10:30-11:30", "filter_applied": False, "note": "diagnostic only — no TOD filter in Phase26"},
        {"bucket": "11:30-12:30", "filter_applied": False, "note": "diagnostic only — no TOD filter in Phase26"},
    ]

    structural_rows = [
        {
            "source": "phase25_holdout_benchmark",
            "p_vwap_touch_overall": struct.get("p_vwap_touch_overall"),
            "median_minutes_to_vwap": struct.get("median_minutes_to_vwap"),
            "n_extensions": struct.get("n_extensions"),
            "forward_paper_n": 0,
            "note": "Forward structural journal empty at init",
        }
    ]

    _write_csv(REPORTS / "phase26_freeze.csv", [freeze_row])
    _write_csv(REPORTS / "phase26_paper_summary.csv", paper_summary)
    _write_csv(REPORTS / "phase26_backtest_vs_paper.csv", delta_rows)
    _write_csv(REPORTS / "phase26_fill_sensitivity.csv", fill_rows)
    _write_csv(REPORTS / "phase26_direction.csv", direction_rows)
    _write_csv(REPORTS / "phase26_time_of_day.csv", tod_rows)
    _write_csv(REPORTS / "phase26_structural_reversion.csv", structural_rows)

    wf_all_pos = bool(wf) and all((b.get("e2r") or 0) > 0 for b in wf)
    cost_survives = all(bool(c.get("survives")) for c in cost if c.get("ticks_per_side") in (1, 2))

    payload = {
        "ok": True,
        "phase": 26,
        "status": paper.get("campaign_status") or "PAPER_VALIDATION_IN_PROGRESS",
        "strategy_family": doc["strategy_family"],
        "strategy_version": FROZEN_STRATEGY_VERSION,
        "candidate_name": CANDIDATE_NAME,
        "frozen_config_hash": doc["frozen_config_hash"],
        "engine_config_hash": doc["engine_config_hash"],
        "source_candidate_path": doc["source_candidate_path"],
        "source_frozen_semantic_match": match,
        "freeze_files": {
            "json": str(FROZEN_JSON).replace("\\", "/"),
            "md": str(FROZEN_MD).replace("\\", "/"),
        },
        "journal": {
            "dir": str(JOURNAL_DIR).replace("\\", "/"),
            "paper_trades": str(PAPER_TRADES_PATH).replace("\\", "/"),
        },
        "historical_benchmark": {
            "train_n": train.get("resolved_n"),
            "train_e2r": train.get("theoretical_2r_expectancy"),
            "holdout_n": hold.get("resolved_n"),
            "holdout_stop_rate": hold.get("stop_rate"),
            "holdout_1r": hold.get("r1_rate"),
            "holdout_1_5r": hold.get("r15_rate"),
            "holdout_2r": hold.get("r2_rate"),
            "holdout_3r": hold.get("r3_rate"),
            "holdout_e1r": hold.get("theoretical_1r_expectancy"),
            "holdout_e1_5r": hold.get("theoretical_1_5r_expectancy"),
            "holdout_e2r": hold.get("theoretical_2r_expectancy"),
            "holdout_e3r": hold.get("theoretical_3r_expectancy"),
            "holdout_median_mfe_r": hold.get("median_mfe_r"),
            "holdout_median_mae_r": hold.get("median_mae_r"),
            "walkforward_all_blocks_e2r_positive": wf_all_pos,
            "walkforward_blocks": len(wf),
            "cost_survives_1_2_ticks": cost_survives,
            "structural_p_vwap_touch": struct.get("p_vwap_touch_overall"),
            "structural_median_minutes": struct.get("median_minutes_to_vwap"),
        },
        "paper_campaign": {
            "minimum_resolved": 30,
            "preferred_resolved": 50,
            "strong_resolved": 100,
            "current_resolved_n": paper.get("resolved", 0),
            "sample_label": paper.get("sample_label"),
            "status": paper.get("campaign_status"),
            "primary_fill_assumption": "1_TICK_ADVERSE",
            "broker_execution": False,
        },
        "paper_metrics": paper,
        "mcp_tools": [
            "tv_analyze_gc_vwap_reversion",
            "tv_gc_vwap_v2_paper_state",
        ],
        "forbidden": [
            "retune",
            "tod_filter",
            "direction_filter",
            "broker_orders",
            "rename_as_production",
        ],
        "generated_at": datetime.now(tz=timezone.utc).isoformat(),
        "write_info": write_info,
        "note": "Phase 26 initialization complete. Forward paper observation campaign starts with N=0.",
    }
    VALIDATION_JSON.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    return payload


if __name__ == "__main__":
    out = run_phase26_init()
    print(json.dumps({k: out[k] for k in ("ok", "status", "frozen_config_hash", "paper_campaign", "historical_benchmark")}, indent=2))
