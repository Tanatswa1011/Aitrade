"""Phase 30 — freeze Phase 29 NQ Drift VWAP Pullback for paper validation."""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from nq_drift_vwap_engine import config_hash
from nq_drift_vwap_models import (
    FORCE_CLOSE_LOCAL,
    NO_NEW_TRADES_AFTER_LOCAL,
    OR_TIMEZONE,
    STRATEGY_FAMILY,
    TRADE_START_LOCAL,
    VWAP_BASIS_STATUS,
    VWAP_PRICE_BASIS,
    VWAP_RESET_LOCAL,
    DVPStrategyConfig,
)

PHASE29_CANDIDATE = Path("strategy_candidates") / "phase29_DVP_ORIGINAL.json"
PHASE29_VALIDATION = Path("phase29_validation.json")
FROZEN_DIR = Path("strategy_frozen")
FROZEN_JSON = FROZEN_DIR / "nq_dvp_phase30.json"
FROZEN_MD = FROZEN_DIR / "nq_dvp_phase30.md"

FROZEN_STRATEGY_VERSION = "nq_drift_vwap_pullback_v1.DVP_ORIGINAL.FROZEN_PHASE30"
CANDIDATE_NAME = "DVP_ORIGINAL"
STATUS = "PAPER_VALIDATION"
PHASE26_HASH = "0695d7a881b6247033426bceb077b6895b96405b826c21f32a109a02f44b2d43"


def load_phase29_candidate() -> dict[str, Any]:
    if not PHASE29_CANDIDATE.exists():
        raise FileNotFoundError(f"missing_canonical_candidate:{PHASE29_CANDIDATE}")
    return json.loads(PHASE29_CANDIDATE.read_text(encoding="utf-8"))


def candidate_to_config(raw: dict[str, Any]) -> DVPStrategyConfig:
    c = raw.get("candidate") or {}
    return DVPStrategyConfig(
        strategy_family=c.get("strategy_family", STRATEGY_FAMILY),
        candidate_id=c["candidate_id"],
        hour_return_threshold=float(c.get("hour_return_threshold", 0.001)),
        long_stop_points=float(c.get("long_stop_points", 80.0)),
        long_target_points=float(c.get("long_target_points", 40.0)),
        short_stop_points=float(c.get("short_stop_points", 80.0)),
        short_target_points=float(c.get("short_target_points", 50.0)),
        max_trades_per_day=int(c.get("max_trades_per_day", 4)),
        max_losses_per_day=int(c.get("max_losses_per_day", 2)),
        extras=dict(c.get("extras") or {}),
    )


def semantic_payload(cfg: DVPStrategyConfig) -> dict[str, Any]:
    return {
        "strategy_family": cfg.strategy_family,
        "candidate_id": cfg.candidate_id,
        "hour_return_threshold": float(cfg.hour_return_threshold),
        "long_stop_points": float(cfg.long_stop_points),
        "long_target_points": float(cfg.long_target_points),
        "short_stop_points": float(cfg.short_stop_points),
        "short_target_points": float(cfg.short_target_points),
        "max_trades_per_day": int(cfg.max_trades_per_day),
        "max_losses_per_day": int(cfg.max_losses_per_day),
        "timezone": OR_TIMEZONE,
        "vwap_reset": VWAP_RESET_LOCAL,
        "trade_start": TRADE_START_LOCAL,
        "no_new_trades_after": NO_NEW_TRADES_AFTER_LOCAL,
        "force_close": FORCE_CLOSE_LOCAL,
        "vwap_price_basis": VWAP_PRICE_BASIS,
        "vwap_basis_status": VWAP_BASIS_STATUS,
        "vwap_formula": "sum(typical_price*volume)/sum(volume)",
        "trend_timeframe": "15m",
        "execution_timeframe": "5m",
        "long_drift": "close>VWAP AND VWAP rising AND 1h_return>=+0.10%",
        "short_drift": "close<VWAP AND VWAP falling AND 1h_return<=-0.10%",
        "long_trigger": "first completed red 5m; entry next 5m open",
        "short_trigger": "first completed green 5m; entry next 5m open",
        "loss_cap_interpretation": "any two losing trades in the day",
        "one_position_at_a_time": True,
        "instrument": "NQ",
    }


def frozen_config_hash(semantic: dict[str, Any]) -> str:
    blob = json.dumps(semantic, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def assert_source_frozen_match(cfg: DVPStrategyConfig, source: dict[str, Any]) -> dict[str, Any]:
    source_hash = str(source.get("config_hash") or "")
    live_hash = config_hash(cfg)
    c = source.get("candidate") or {}
    mismatches = []
    checks = [
        ("candidate_id", cfg.candidate_id, c.get("candidate_id")),
        ("hour_return_threshold", float(cfg.hour_return_threshold), float(c.get("hour_return_threshold", -1))),
        ("long_stop_points", float(cfg.long_stop_points), float(c.get("long_stop_points", -1))),
        ("long_target_points", float(cfg.long_target_points), float(c.get("long_target_points", -1))),
        ("short_stop_points", float(cfg.short_stop_points), float(c.get("short_stop_points", -1))),
        ("short_target_points", float(cfg.short_target_points), float(c.get("short_target_points", -1))),
        ("max_trades_per_day", int(cfg.max_trades_per_day), int(c.get("max_trades_per_day", -1))),
        ("max_losses_per_day", int(cfg.max_losses_per_day), int(c.get("max_losses_per_day", -1))),
    ]
    for name, a, b in checks:
        if a != b:
            mismatches.append({"field": name, "live": a, "source": b})
    ok = (live_hash == source_hash if source_hash else True) and not mismatches
    if not source_hash:
        ok = not mismatches
    return {
        "ok": ok,
        "source_config_hash": source_hash or None,
        "live_config_hash": live_hash,
        "hashes_equal": (live_hash == source_hash) if source_hash else None,
        "mismatches": mismatches,
        "error_code": None if ok else "FROZEN_CONFIG_MISMATCH",
    }


def load_phase29_benchmark() -> dict[str, Any]:
    if not PHASE29_VALIDATION.exists():
        return {"ok": False, "error": "missing_phase29_validation"}
    p = json.loads(PHASE29_VALIDATION.read_text(encoding="utf-8"))
    return {
        "ok": True,
        "verdict": p.get("verdict"),
        "full_sample": p.get("full_sample"),
        "out_of_sample": p.get("out_of_sample"),
        "walkforward_class": p.get("walkforward_class"),
        "walkforward": p.get("walkforward"),
        "cost_sensitivity": p.get("cost_sensitivity"),
        "source_validation": str(PHASE29_VALIDATION),
    }


def build_frozen_document(*, freeze_timestamp: Optional[str] = None) -> dict[str, Any]:
    source = load_phase29_candidate()
    cfg = candidate_to_config(source)
    if cfg.candidate_id != CANDIDATE_NAME:
        raise ValueError(f"unexpected_candidate_id:{cfg.candidate_id}")
    match = assert_source_frozen_match(cfg, source)
    if not match["ok"]:
        raise RuntimeError(f"FROZEN_CONFIG_MISMATCH:{match}")

    semantic = semantic_payload(cfg)
    fhash = frozen_config_hash(semantic)
    ts = freeze_timestamp or datetime.now(tz=timezone.utc).isoformat()
    benchmark = load_phase29_benchmark()

    return {
        "phase": 30,
        "status": STATUS,
        "strategy_family": STRATEGY_FAMILY,
        "strategy_version": FROZEN_STRATEGY_VERSION,
        "candidate_name": CANDIDATE_NAME,
        "candidate_id": CANDIDATE_NAME,
        "instrument": {
            "root": "NQ",
            "name": "CME E-mini Nasdaq-100 Futures",
            "exchange": "CME",
            "forbid_substitutes": ["GC", "ES", "MNQ_as_signal", "NASDAQ_CFD", "cash_index"],
            "mnq_note": "MNQ may be execution-equivalence only; not a different signal source unless separately validated",
        },
        "data_assumptions": {
            "historical_provider": "databento",
            "dataset": "GLBX.MDP3",
            "schema": "ohlcv-1m aggregated to NY-aligned 5m/15m",
            "contract_stitch": "aitrade_volume_crossover_unadjusted",
            "canonical_bars": "data/databento/NQ/stitched/",
        },
        "session": {
            "timezone": OR_TIMEZONE,
            "vwap_reset": VWAP_RESET_LOCAL,
            "trade_start": TRADE_START_LOCAL,
            "no_new_trades_after": NO_NEW_TRADES_AFTER_LOCAL,
            "force_close": FORCE_CLOSE_LOCAL,
            "dst_aware": True,
        },
        "vwap": {
            "price_basis": VWAP_PRICE_BASIS,
            "basis_status": VWAP_BASIS_STATUS,
            "formula": "sum(typical_price*volume)/sum(volume)",
            "reset": "09:30 America/New_York each RTH day",
        },
        "timeframes": {"trend": "15m", "execution": "5m", "completed_bars_only": True},
        "drift": {
            "hour_return_threshold": 0.001,
            "long": semantic["long_drift"],
            "short": semantic["short_drift"],
        },
        "entry": {
            "long_trigger": semantic["long_trigger"],
            "short_trigger": semantic["short_trigger"],
            "no_vwap_touch_required": True,
            "no_min_pullback_depth": True,
        },
        "risk": {
            "long_stop_points": 80.0,
            "long_target_points": 40.0,
            "short_stop_points": 80.0,
            "short_target_points": 50.0,
            "no_volatility_adjust": True,
            "no_dynamic_r": True,
        },
        "position_rules": {
            "one_position_at_a_time": True,
            "max_trades_per_day": 4,
            "max_losses_per_day": 2,
            "loss_cap_interpretation": "any two losing trades in the day",
        },
        "cost_model_assumptions": {
            "tick_size_research": 0.25,
            "primary_paper_fill": "1_TICK_ADVERSE",
            "overlays": ["IDEAL_TOUCH", "1_TICK_ADVERSE", "2_TICK_ADVERSE"],
            "scenarios_ticks_per_side": [0, 1, 2],
        },
        "candidate": cfg.to_dict(),
        "source_candidate_path": str(PHASE29_CANDIDATE).replace("\\", "/"),
        "source_candidate_hash": match["source_config_hash"],
        "engine_config_hash": match["live_config_hash"],
        "semantic": semantic,
        "frozen_config_hash": fhash,
        "source_frozen_semantic_match": match,
        "freeze_timestamp": ts,
        "historical_benchmark": benchmark,
        "paper_campaign": {
            "minimum_resolved": 30,
            "preferred_resolved": 50,
            "strong_resolved": 100,
            "large_resolved": 250,
            "status_before_n30": "PAPER_VALIDATION_IN_PROGRESS",
            "broker_execution": False,
            "primary_fill_assumption": "1_TICK_ADVERSE",
        },
        "phase26_protection": {
            "frozen_hash_must_remain": PHASE26_HASH,
            "journal_must_remain_untouched": "journal/phase26_gc_vwap_v2_paper/paper_trades.jsonl",
        },
        "forbidden_mutations": [
            "hour_return_threshold",
            "long_stop_points",
            "long_target_points",
            "short_stop_points",
            "short_target_points",
            "vwap_reset",
            "trade_start",
            "no_new_trades_after",
            "force_close",
            "max_trades_per_day",
            "max_losses_per_day",
            "trend_timeframe",
            "execution_timeframe",
        ],
        "note": "Immutable Phase 30 freeze for NQ DVP paper validation — NOT production/broker execution",
    }


def write_frozen_files(doc: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    if os.environ.get("AITRADE_PHASE54_TEST") == "1":
        existing = load_frozen_document()
        if doc is not None and doc.get("frozen_config_hash") != existing.get("frozen_config_hash"):
            raise RuntimeError("TEST_FROZEN_REGEN_REJECTED:config_hash_mismatch")
        return {
            "ok": True,
            "test_write_rejected": True,
            "frozen_json": str(FROZEN_JSON).replace("\\", "/"),
            "frozen_md": str(FROZEN_MD).replace("\\", "/"),
            "frozen_config_hash": existing["frozen_config_hash"],
            "engine_config_hash": existing["engine_config_hash"],
            "source_match": existing["source_frozen_semantic_match"],
        }
    FROZEN_DIR.mkdir(parents=True, exist_ok=True)
    doc = doc or build_frozen_document()
    FROZEN_JSON.write_text(json.dumps(doc, indent=2, default=str), encoding="utf-8")
    FROZEN_MD.write_text(_render_md(doc), encoding="utf-8")
    return {
        "ok": True,
        "frozen_json": str(FROZEN_JSON).replace("\\", "/"),
        "frozen_md": str(FROZEN_MD).replace("\\", "/"),
        "frozen_config_hash": doc["frozen_config_hash"],
        "engine_config_hash": doc["engine_config_hash"],
        "source_match": doc["source_frozen_semantic_match"],
    }


def load_frozen_document() -> dict[str, Any]:
    if not FROZEN_JSON.exists():
        raise FileNotFoundError("missing_frozen_file")
    return json.loads(FROZEN_JSON.read_text(encoding="utf-8"))


def load_frozen_strategy_config(doc: Optional[dict[str, Any]] = None) -> DVPStrategyConfig:
    doc = doc or load_frozen_document()
    return candidate_to_config({"candidate": doc["candidate"]})


def assert_runtime_matches_frozen(
    cfg: DVPStrategyConfig,
    doc: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    doc = doc or load_frozen_document()
    frozen_cfg = load_frozen_strategy_config(doc)
    mismatches = []
    for field in (
        "candidate_id",
        "hour_return_threshold",
        "long_stop_points",
        "long_target_points",
        "short_stop_points",
        "short_target_points",
        "max_trades_per_day",
        "max_losses_per_day",
    ):
        a = getattr(cfg, field)
        b = getattr(frozen_cfg, field)
        if a != b:
            mismatches.append({"field": field, "runtime": a, "frozen": b})
    sess = doc.get("session") or {}
    if sess.get("vwap_reset") != VWAP_RESET_LOCAL:
        mismatches.append({"field": "vwap_reset", "runtime": VWAP_RESET_LOCAL, "frozen": sess.get("vwap_reset")})
    if sess.get("trade_start") != TRADE_START_LOCAL:
        mismatches.append({"field": "trade_start", "runtime": TRADE_START_LOCAL, "frozen": sess.get("trade_start")})
    ok = not mismatches and config_hash(cfg) == config_hash(frozen_cfg)
    return {
        "ok": ok,
        "error_code": None if ok else "FROZEN_CONFIG_MISMATCH",
        "mismatches": mismatches,
        "frozen_config_hash": doc.get("frozen_config_hash"),
        "runtime_engine_hash": config_hash(cfg),
    }


def _render_md(doc: dict[str, Any]) -> str:
    bm = doc.get("historical_benchmark") or {}
    full = bm.get("full_sample") or {}
    oos = bm.get("out_of_sample") or {}
    return f"""# Frozen Strategy — NQ Drift VWAP Pullback (Phase 30)

## Status

```text
PAPER_VALIDATION
NOT production
NO broker execution
```

## Thesis

When NQ shows a confirmed 15m directional drift relative to session VWAP, the first opposing 5m pullback candle may offer a continuation entry with fixed asymmetric point exits.

## Identity

| Field | Value |
|------|-------|
| Strategy family | `{doc['strategy_family']}` |
| Strategy version | `{doc['strategy_version']}` |
| Candidate | `{doc['candidate_id']}` |
| Frozen config hash | `{doc['frozen_config_hash']}` |
| Engine config hash | `{doc['engine_config_hash']}` |
| Source | `{doc['source_candidate_path']}` |
| Freeze timestamp | `{doc['freeze_timestamp']}` |

## Exact rules (immutable)

### Market

- CME NQ futures only (no GC/ES/CFD/cash index signal substitution)

### Session (America/New_York, DST-aware)

- VWAP reset: **09:30**
- No trading before: **10:30**
- No new trades after: **15:30**
- Force-close: **15:55**

### VWAP

- typical_price = (H+L+C)/3 — `IMPLEMENTATION_ASSUMPTION`
- VWAP = Σ(tp×vol)/Σ(vol) from 09:30

### Drift (15m completed bars)

- Long: close > VWAP, VWAP rising vs prior 15m, 1h return ≥ +0.10%
- Short: close < VWAP, VWAP falling vs prior 15m, 1h return ≤ −0.10%

### Entry (5m)

- Long: first red 5m after POSITIVE_DRIFT → next bar open
- Short: first green 5m after NEGATIVE_DRIFT → next bar open

### Risk (points, fixed)

- Long: SL 80 / TP 40
- Short: SL 80 / TP 50

### Position rules

- One position at a time
- Max **4** trades/day
- Stop new trades after **any 2 losing trades** in the day

## Historical evidence (Phase 29)

FULL: N={full.get('resolved_n')}, WR={full.get('win_rate')}, E={full.get('expectancy_points')}, PF={full.get('profit_factor')}

OOS 2025+: N={oos.get('resolved_n')}, WR={oos.get('win_rate')}, E={oos.get('expectancy_points')}, PF={oos.get('profit_factor')}, maxDD={oos.get('max_drawdown_points')}

Walk-forward: {bm.get('walkforward_class')}

## Paper-validation criteria

- Minimum resolved: **30** (preferred 50, strong 100, large 250)
- Before N=30: only `PAPER_VALIDATION_IN_PROGRESS`
- Primary fill overlay: **1 tick adverse** (also report ideal / 2-tick)

## What is NOT allowed to change

Any change to thresholds, stops/targets, session times, or guardrails creates a **new** strategy version and must not contaminate Phase 30 paper statistics.
"""


if __name__ == "__main__":
    info = write_frozen_files()
    print(json.dumps(info, indent=2))
