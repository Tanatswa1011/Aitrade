"""Phase 30 — freeze NQ DVP + paper-validation initialization (no retuning)."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from nq_dvp_freeze import (
    CANDIDATE_NAME,
    FROZEN_JSON,
    FROZEN_MD,
    FROZEN_STRATEGY_VERSION,
    PHASE26_HASH,
    assert_source_frozen_match,
    build_frozen_document,
    candidate_to_config,
    frozen_config_hash,
    load_phase29_benchmark,
    load_phase29_candidate,
    semantic_payload,
    write_frozen_files,
)
from nq_dvp_paper import (
    JOURNAL_DIR,
    PAPER_TRADES_PATH,
    ensure_journal_dir,
    paper_campaign_status,
    sample_label,
    summarize_paper_journal,
)
from phase22_validate import _write_csv

REPORTS = Path("reports")
VALIDATION_JSON = Path("phase30_validation.json")
PHASE26_FROZEN = Path("strategy_frozen") / "gc_vwap_v2_phase26.json"
PHASE26_PAPER = Path("journal") / "phase26_gc_vwap_v2_paper" / "paper_trades.jsonl"


def assert_phase26_untouched() -> dict[str, Any]:
    ok = True
    reasons = []
    if not PHASE26_FROZEN.exists():
        ok = False
        reasons.append("missing_frozen")
    else:
        doc = json.loads(PHASE26_FROZEN.read_text(encoding="utf-8"))
        if doc.get("frozen_config_hash") != PHASE26_HASH:
            ok = False
            reasons.append("hash_changed")
    paper_ok = PHASE26_PAPER.exists()
    return {
        "ok": ok,
        "reasons": reasons,
        "expected_hash": PHASE26_HASH,
        "paper_journal_exists": paper_ok,
    }


def build_forward_delta(oos: Optional[dict], paper: dict[str, Any]) -> list[dict[str, Any]]:
    metrics = [
        ("win_rate", "win_rate", paper.get("win_rate")),
        ("expectancy_points", "expectancy_points", paper.get("expectancy_points")),
        ("profit_factor", "profit_factor", paper.get("profit_factor")),
        ("average_win_points", "average_win_points", paper.get("average_win_points")),
        ("average_loss_points", "average_loss_points", paper.get("average_loss_points")),
        ("max_drawdown_points", "max_drawdown_points", paper.get("max_drawdown_points")),
        ("longest_losing_streak", "longest_losing_streak", paper.get("longest_losing_streak")),
        ("long_win_rate", None, (paper.get("long") or {}).get("win_rate")),
        ("short_win_rate", None, (paper.get("short") or {}).get("win_rate")),
    ]
    rows = []
    for label, oos_key, paper_val in metrics:
        if label == "long_win_rate":
            hist = None if not oos else (oos.get("long") or {}).get("win_rate")
        elif label == "short_win_rate":
            hist = None if not oos else (oos.get("short") or {}).get("win_rate")
        else:
            hist = None if not oos or not oos_key else oos.get(oos_key)
        delta = None
        if hist is not None and paper_val is not None:
            delta = float(paper_val) - float(hist)
        rows.append(
            {
                "metric": label,
                "historical_oos": hist,
                "paper": paper_val,
                "delta_paper_minus_oos": delta,
                "note": "N=0 — no forward conclusion" if int(paper.get("resolved") or 0) == 0 else "",
            }
        )
    return rows


def run_phase30_init() -> dict[str, Any]:
    REPORTS.mkdir(parents=True, exist_ok=True)
    ensure_journal_dir()

    paper_before = PHASE26_PAPER.read_text(encoding="utf-8") if PHASE26_PAPER.exists() else ""
    p26 = assert_phase26_untouched()
    if not p26["ok"]:
        raise RuntimeError(f"PHASE26_PROTECTION_FAILED:{p26}")

    source = load_phase29_candidate()
    cfg = candidate_to_config(source)
    match = assert_source_frozen_match(cfg, source)
    if not match["ok"]:
        raise RuntimeError(f"FROZEN_CONFIG_MISMATCH:{match}")

    write_info = write_frozen_files(
        build_frozen_document(freeze_timestamp=datetime.now(tz=timezone.utc).isoformat())
    )
    doc = json.loads(FROZEN_JSON.read_text(encoding="utf-8"))
    semantic = semantic_payload(cfg)
    assert doc["frozen_config_hash"] == frozen_config_hash(semantic)

    benchmark = load_phase29_benchmark()
    full = benchmark.get("full_sample") or {}
    oos = benchmark.get("out_of_sample") or {}
    wf = benchmark.get("walkforward") or []
    cost = benchmark.get("cost_sensitivity") or []

    paper = summarize_paper_journal()
    delta_rows = build_forward_delta(oos, paper)

    freeze_row = {
        "candidate_name": CANDIDATE_NAME,
        "strategy_version": FROZEN_STRATEGY_VERSION,
        "frozen_config_hash": doc["frozen_config_hash"],
        "engine_config_hash": doc["engine_config_hash"],
        "source_candidate_path": doc["source_candidate_path"],
        "source_hash_match": match["hashes_equal"],
        "instrument": "NQ",
        "exchange": "CME",
        "vwap_reset": "09:30 America/New_York",
        "trade_window": "10:30-15:30",
        "force_close": "15:55",
        "trend_tf": "15m",
        "execution_tf": "5m",
        "long_sl_tp": "80/40",
        "short_sl_tp": "80/50",
        "max_trades_day": 4,
        "max_losses_day": 2,
        "status": "PAPER_VALIDATION",
        "broker_execution": False,
    }

    paper_summary = [
        {
            "resolved_n": paper.get("resolved", 0),
            "wins": paper.get("wins"),
            "losses": paper.get("losses"),
            "timed_exits": paper.get("timed_exits"),
            "ambiguous": paper.get("ambiguous"),
            "win_rate": paper.get("win_rate"),
            "expectancy_points": paper.get("expectancy_points"),
            "profit_factor": paper.get("profit_factor"),
            "sample_label": paper.get("sample_label") or sample_label(0),
            "campaign_status": paper.get("campaign_status") or paper_campaign_status(0),
            "note": "Initialization — no forward paper observations yet"
            if int(paper.get("resolved") or 0) == 0
            else "",
        }
    ]

    cost_rows = []
    for c in cost:
        cost_rows.append(
            {
                "ticks_per_side": c.get("ticks_per_side"),
                "historical_oos_expectancy": c.get("expectancy_points"),
                "historical_oos_pf": c.get("profit_factor"),
                "paper_expectancy": None,
                "note": "paper N=0 at init",
            }
        )
    if not cost_rows:
        cost_rows = [
            {"ticks_per_side": 0, "historical_oos_expectancy": oos.get("expectancy_points"), "note": "ideal"},
            {"ticks_per_side": 1, "historical_oos_expectancy": None, "note": "primary paper assumption"},
            {"ticks_per_side": 2, "historical_oos_expectancy": None, "note": "overlay"},
        ]

    long_short = [
        {
            "side": "long",
            "historical_oos_n": (oos.get("long") or {}).get("n"),
            "historical_oos_wr": (oos.get("long") or {}).get("win_rate"),
            "historical_oos_expectancy": (oos.get("long") or {}).get("expectancy_points"),
            "paper_n": (paper.get("long") or {}).get("n"),
            "paper_wr": (paper.get("long") or {}).get("win_rate"),
            "paper_expectancy": (paper.get("long") or {}).get("expectancy_points"),
            "filter_applied": False,
        },
        {
            "side": "short",
            "historical_oos_n": (oos.get("short") or {}).get("n"),
            "historical_oos_wr": (oos.get("short") or {}).get("win_rate"),
            "historical_oos_expectancy": (oos.get("short") or {}).get("expectancy_points"),
            "paper_n": (paper.get("short") or {}).get("n"),
            "paper_wr": (paper.get("short") or {}).get("win_rate"),
            "paper_expectancy": (paper.get("short") or {}).get("expectancy_points"),
            "filter_applied": False,
        },
    ]

    guard_rows = [
        {
            "metric": "days_hit_four_trade_cap",
            "historical": None,
            "paper": 0,
            "note": "track during campaign; do not change guardrails",
        },
        {
            "metric": "days_hit_two_loss_cap",
            "historical": None,
            "paper": 0,
            "note": "any two losses; not consecutive-only",
        },
        {
            "metric": "setups_suppressed_after_trade_cap",
            "historical": None,
            "paper": 0,
        },
        {
            "metric": "setups_suppressed_after_loss_cap",
            "historical": None,
            "paper": 0,
        },
    ]

    _write_csv(REPORTS / "phase30_freeze.csv", [freeze_row])
    _write_csv(REPORTS / "phase30_paper_summary.csv", paper_summary)
    _write_csv(REPORTS / "phase30_backtest_vs_paper.csv", delta_rows)
    _write_csv(REPORTS / "phase30_cost_sensitivity.csv", cost_rows)
    _write_csv(REPORTS / "phase30_long_short.csv", long_short)
    _write_csv(REPORTS / "phase30_guardrails.csv", guard_rows)

    paper_after = PHASE26_PAPER.read_text(encoding="utf-8") if PHASE26_PAPER.exists() else ""

    payload = {
        "ok": True,
        "phase": 30,
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
            "full_n": full.get("resolved_n"),
            "full_wr": full.get("win_rate"),
            "full_expectancy": full.get("expectancy_points"),
            "full_pf": full.get("profit_factor"),
            "full_max_dd": full.get("max_drawdown_points"),
            "full_losing_streak": full.get("longest_losing_streak"),
            "oos_n": oos.get("resolved_n"),
            "oos_wr": oos.get("win_rate"),
            "oos_expectancy": oos.get("expectancy_points"),
            "oos_pf": oos.get("profit_factor"),
            "oos_max_dd": oos.get("max_drawdown_points"),
            "oos_long_wr": (oos.get("long") or {}).get("win_rate"),
            "oos_short_wr": (oos.get("short") or {}).get("win_rate"),
            "walkforward_class": benchmark.get("walkforward_class"),
            "walkforward_blocks": len(wf),
            "walkforward_all_positive": bool(wf)
            and all((b.get("expectancy_points") or 0) > 0 for b in wf),
            "cost_0_tick_expectancy": next(
                (c.get("expectancy_points") for c in cost if c.get("ticks_per_side") == 0), None
            ),
            "cost_1_tick_expectancy": next(
                (c.get("expectancy_points") for c in cost if c.get("ticks_per_side") == 1), None
            ),
            "cost_2_tick_expectancy": next(
                (c.get("expectancy_points") for c in cost if c.get("ticks_per_side") == 2), None
            ),
            "source_validation": benchmark.get("source_validation"),
        },
        "paper_campaign": {
            "minimum_resolved": 30,
            "preferred_resolved": 50,
            "strong_resolved": 100,
            "large_resolved": 250,
            "current_resolved_n": paper.get("resolved", 0),
            "sample_label": paper.get("sample_label") or sample_label(0),
            "status": paper.get("campaign_status") or paper_campaign_status(0),
            "primary_fill_assumption": "1_TICK_ADVERSE",
            "broker_execution": False,
        },
        "paper_metrics": {k: v for k, v in paper.items() if k != "rows"},
        "mcp_tools": ["tv_nq_dvp_paper_state"],
        "phase26_untouched": p26,
        "phase26_paper_unchanged": paper_before == paper_after,
        "forbidden": [
            "retune",
            "change_thresholds",
            "change_stops_targets",
            "broker_orders",
            "direction_disable",
            "rename_as_production",
        ],
        "generated_at": datetime.now(tz=timezone.utc).isoformat(),
        "write_info": write_info,
        "note": "Phase 30 initialization complete. Forward paper observation campaign starts with N=0.",
    }
    VALIDATION_JSON.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    return payload


if __name__ == "__main__":
    out = run_phase30_init()
    print(
        json.dumps(
            {
                k: out[k]
                for k in (
                    "ok",
                    "status",
                    "frozen_config_hash",
                    "paper_campaign",
                    "historical_benchmark",
                    "phase26_untouched",
                    "phase26_paper_unchanged",
                )
            },
            indent=2,
        )
    )
