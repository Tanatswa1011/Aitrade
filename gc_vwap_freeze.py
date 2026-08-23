"""Phase 26 — freeze Phase 25 V2 VWAP mean-reversion for paper validation."""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from gc_vwap_engine import config_hash
from gc_vwap_models import (
    MAX_ENTRY_BARS,
    MIN_VWAP_BARS,
    NO_NEW_SETUP_AFTER_LOCAL,
    OR_TIMEZONE,
    SESSION_END_LOCAL,
    SESSION_NOTE,
    SESSION_START_LOCAL,
    SIGMA_THRESHOLD,
    STRATEGY_FAMILY,
    ConfirmationMode,
    EntryMode,
    GCVWAPStrategyConfig,
)

PHASE25_V2_PATH = Path("strategy_candidates") / "phase25_V2_BAND_RECLAIM_2SIG_RETEST.json"
PHASE25_PAPER_PATH = Path("strategy_candidates") / "phase25_gc_paper_candidate.json"
PHASE25_VALIDATION = Path("phase25_validation.json")
FROZEN_DIR = Path("strategy_frozen")
FROZEN_JSON = FROZEN_DIR / "gc_vwap_v2_phase26.json"
FROZEN_MD = FROZEN_DIR / "gc_vwap_v2_phase26.md"

FROZEN_STRATEGY_VERSION = "gc_vwap_mean_reversion_v1.V2.FROZEN_PHASE26"
CANDIDATE_NAME = "V2_BAND_RECLAIM_2SIG_RETEST"
STATUS = "paper_validation"


def load_phase25_v2_candidate() -> dict[str, Any]:
    if not PHASE25_V2_PATH.exists():
        raise FileNotFoundError(f"missing_canonical_candidate:{PHASE25_V2_PATH}")
    return json.loads(PHASE25_V2_PATH.read_text(encoding="utf-8"))


def candidate_to_config(raw: dict[str, Any]) -> GCVWAPStrategyConfig:
    c = raw.get("candidate") or {}
    return GCVWAPStrategyConfig(
        strategy_family=c.get("strategy_family", STRATEGY_FAMILY),
        candidate_id=c["candidate_id"],
        confirmation_mode=str(c.get("confirmation_mode")),
        entry_mode=str(c.get("entry_mode")),
        sigma_threshold=float(c.get("sigma_threshold", SIGMA_THRESHOLD)),
        max_entry_bars=int(c.get("max_entry_bars", MAX_ENTRY_BARS)),
        min_vwap_bars=int(c.get("min_vwap_bars", MIN_VWAP_BARS)),
        volume_filter=bool(c.get("volume_filter", False)),
        execution_timeframe=str(c.get("execution_timeframe", "5m")),
        extras=dict(c.get("extras") or {}),
    )


def semantic_payload(cfg: GCVWAPStrategyConfig) -> dict[str, Any]:
    """Strategy semantics only — excludes freeze timestamps / paths."""
    return {
        "strategy_family": cfg.strategy_family,
        "candidate_id": cfg.candidate_id,
        "confirmation_mode": cfg.confirmation_mode,
        "entry_mode": cfg.entry_mode,
        "sigma_threshold": float(cfg.sigma_threshold),
        "max_entry_bars": int(cfg.max_entry_bars),
        "min_vwap_bars": int(cfg.min_vwap_bars),
        "volume_filter": bool(cfg.volume_filter),
        "execution_timeframe": cfg.execution_timeframe,
        "session_timezone": OR_TIMEZONE,
        "session_start": SESSION_START_LOCAL,
        "session_end": SESSION_END_LOCAL,
        "no_new_setups_after": NO_NEW_SETUP_AFTER_LOCAL,
        "vwap_price_input": "typical_price=(high+low+close)/3",
        "vwap_formula": "sum(typical_price*volume)/sum(volume)",
        "sigma_methodology": "volume_weighted_std_of_typical_price_vs_running_session_vwap",
        "extension_definition": "close beyond VWAP +/- sigma_threshold * session_std",
        "reclaim_definition": "BAND_RECLAIM: close returns inside +/-2σ band",
        "stop_rule": "extension_sequence_extreme",
        "stop_buffer": 0.0,
        "targets": ["1R", "1.5R", "2R", "3R", "VWAP_TOUCH_diagnostic"],
        "horizon": "expire unresolved by session_end 13:30 America/New_York",
        "frozen_band_semantics": (
            "Phase25 FROZEN_2SIG_RETEST: entry_band_price = 2σ band at first extension bar; "
            "held fixed through reclaim and retest wait (does not follow moving VWAP/σ)"
        ),
    }


def frozen_config_hash(semantic: dict[str, Any]) -> str:
    blob = json.dumps(semantic, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def assert_source_frozen_match(cfg: GCVWAPStrategyConfig, source: dict[str, Any]) -> dict[str, Any]:
    source_hash = str(source.get("config_hash") or "")
    live_hash = config_hash(cfg)
    # Also compare required fields exactly
    c = source.get("candidate") or {}
    mismatches = []
    checks = [
        ("candidate_id", cfg.candidate_id, c.get("candidate_id")),
        ("confirmation_mode", cfg.confirmation_mode, c.get("confirmation_mode")),
        ("entry_mode", cfg.entry_mode, c.get("entry_mode")),
        ("sigma_threshold", float(cfg.sigma_threshold), float(c.get("sigma_threshold", -1))),
        ("max_entry_bars", int(cfg.max_entry_bars), int(c.get("max_entry_bars", -1))),
        ("min_vwap_bars", int(cfg.min_vwap_bars), int(c.get("min_vwap_bars", -1))),
        ("volume_filter", bool(cfg.volume_filter), bool(c.get("volume_filter", True))),
    ]
    for name, a, b in checks:
        if a != b:
            mismatches.append({"field": name, "live": a, "source": b})
    ok = live_hash == source_hash and not mismatches
    # If source hash missing, still require field match
    if not source_hash:
        ok = not mismatches
    return {
        "ok": ok,
        "source_config_hash": source_hash or None,
        "live_config_hash": live_hash,
        "hashes_equal": (live_hash == source_hash) if source_hash else None,
        "mismatches": mismatches,
    }


def load_phase25_v2_benchmark() -> dict[str, Any]:
    if not PHASE25_VALIDATION.exists():
        return {"ok": False, "error": "missing_phase25_validation"}
    p = json.loads(PHASE25_VALIDATION.read_text(encoding="utf-8"))
    train = next(
        (m for m in (p.get("train_metrics") or []) if m.get("candidate_id") == CANDIDATE_NAME),
        None,
    )
    hold = next(
        (m for m in (p.get("holdout_metrics") or []) if m.get("candidate_id") == CANDIDATE_NAME),
        None,
    )
    wf = [r for r in (p.get("walkforward") or []) if r.get("candidate_id") == CANDIDATE_NAME]
    cost = [c for c in (p.get("cost_sensitivity") or []) if c.get("candidate_id") == CANDIDATE_NAME]
    return {
        "ok": True,
        "verdict": p.get("verdict"),
        "train": train,
        "holdout": hold,
        "walkforward": wf,
        "cost_sensitivity": cost,
        "structural_reversion": p.get("structural_reversion"),
        "source_validation": str(PHASE25_VALIDATION),
    }


def build_frozen_document(
    *,
    freeze_timestamp: Optional[str] = None,
) -> dict[str, Any]:
    source = load_phase25_v2_candidate()
    cfg = candidate_to_config(source)
    if cfg.candidate_id != CANDIDATE_NAME:
        raise ValueError(f"unexpected_candidate_id:{cfg.candidate_id}")
    if cfg.confirmation_mode != ConfirmationMode.BAND_RECLAIM.value:
        raise ValueError("confirmation_mode_mismatch")
    if cfg.entry_mode != EntryMode.FROZEN_2SIG_RETEST.value:
        raise ValueError("entry_mode_mismatch")
    if float(cfg.sigma_threshold) != 2.0:
        raise ValueError("sigma_mismatch")

    match = assert_source_frozen_match(cfg, source)
    if not match["ok"]:
        raise RuntimeError(f"FROZEN_CONFIG_MISMATCH:{match}")

    semantic = semantic_payload(cfg)
    fhash = frozen_config_hash(semantic)
    ts = freeze_timestamp or datetime.now(tz=timezone.utc).isoformat()
    benchmark = load_phase25_v2_benchmark()

    return {
        "phase": 26,
        "status": STATUS,
        "strategy_family": STRATEGY_FAMILY,
        "strategy_version": FROZEN_STRATEGY_VERSION,
        "candidate_name": CANDIDATE_NAME,
        "candidate_id": CANDIDATE_NAME,
        "instrument": {
            "root": "GC",
            "name": "COMEX Gold Futures",
            "forbid_xauusd": True,
        },
        "data_assumptions": {
            "historical_provider": "databento",
            "dataset": "GLBX.MDP3",
            "schema": "ohlcv-1m aggregated to 5m",
            "contract_stitch": "aitrade_volume_crossover_unadjusted",
            "canonical_bars": "data/databento/GC/stitched/databento_GC_stitched_5m.jsonl",
        },
        "session": {
            "timezone": OR_TIMEZONE,
            "start": SESSION_START_LOCAL,
            "end": SESSION_END_LOCAL,
            "no_new_setups_after": NO_NEW_SETUP_AFTER_LOCAL,
            "dst_aware": True,
            "note": SESSION_NOTE,
        },
        "vwap": {
            "price_input": semantic["vwap_price_input"],
            "formula": semantic["vwap_formula"],
            "reset": "each trading day at session start",
            "no_overnight_carry": True,
            "no_future_bars": True,
        },
        "sigma": {
            "methodology": semantic["sigma_methodology"],
            "threshold": 2.0,
            "bands": ["1σ", "2σ", "3σ"],
        },
        "warmup": {"minimum_vwap_bars": 6, "timeframe": "5m"},
        "extension": {
            "threshold": "±2σ",
            "upper": "close >= VWAP + 2σ",
            "lower": "close <= VWAP - 2σ",
            "sequence_extreme_tracked": True,
            "no_overlapping_duplicate_setups": True,
        },
        "confirmation": {
            "mode": "BAND_RECLAIM",
            "upper_reclaim": "completed close returns below +2σ",
            "lower_reclaim": "completed close returns above -2σ",
            "forbidden": ["CHoCH", "FVG", "candle_pattern_filter", "volume_filter"],
        },
        "entry": {
            "mode": "FROZEN_2SIG_RETEST",
            "frozen_band_semantics": semantic["frozen_band_semantics"],
            "max_entry_bars": 6,
            "timeout_bars": 6,
        },
        "stop": {
            "rule": "extension_sequence_extreme",
            "short": "extension extreme high",
            "long": "extension extreme low",
            "buffer": 0.0,
            "no_trailing": True,
            "no_breakeven_move": True,
        },
        "targets": {
            "fixed_r": [1.0, 1.5, 2.0, 3.0],
            "vwap_touch_diagnostic": True,
            "no_partials": True,
            "no_trailing": True,
        },
        "horizon": {
            "trade_expire": "13:30 America/New_York",
            "no_overnight": True,
        },
        "cost_model_assumptions": {
            "tick_size_research": 0.1,
            "scenarios_ticks_per_side": [0, 1, 2],
            "primary_paper_fill_assumption": "1_TICK_ADVERSE",
            "overlays": ["IDEAL_TOUCH", "1_TICK_ADVERSE", "2_TICK_ADVERSE"],
            "note": "Illustrative; not brokerage quotes",
        },
        "candidate": cfg.to_dict(),
        "source_candidate_path": str(PHASE25_V2_PATH).replace("\\", "/"),
        "source_paper_candidate_path": str(PHASE25_PAPER_PATH).replace("\\", "/"),
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
            "status_before_n30": "PAPER_VALIDATION_IN_PROGRESS",
            "position_sizing": "normalize_to_1R_reporting_only",
            "broker_execution": False,
        },
        "forbidden_mutations": [
            "sigma_threshold",
            "session_start",
            "session_end",
            "no_new_setups_after",
            "warmup",
            "confirmation_mode",
            "entry_mode",
            "max_entry_bars",
            "stop_rule",
            "targets",
            "direction_filter",
            "time_of_day_filter",
            "volume_filter",
        ],
        "note": "Immutable Phase 26 freeze for paper validation only — NOT production/live execution",
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


def load_frozen_strategy_config(doc: Optional[dict[str, Any]] = None) -> GCVWAPStrategyConfig:
    doc = doc or load_frozen_document()
    return candidate_to_config({"candidate": doc["candidate"]})


def assert_runtime_matches_frozen(
    cfg: GCVWAPStrategyConfig,
    doc: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    doc = doc or load_frozen_document()
    frozen_cfg = load_frozen_strategy_config(doc)
    mismatches = []
    for field in (
        "candidate_id",
        "confirmation_mode",
        "entry_mode",
        "sigma_threshold",
        "max_entry_bars",
        "min_vwap_bars",
        "volume_filter",
    ):
        a = getattr(cfg, field)
        b = getattr(frozen_cfg, field)
        if a != b:
            mismatches.append({"field": field, "runtime": a, "frozen": b})
    # session constants must match frozen doc
    sess = doc.get("session") or {}
    if sess.get("start") != SESSION_START_LOCAL:
        mismatches.append({"field": "session_start", "runtime": SESSION_START_LOCAL, "frozen": sess.get("start")})
    if sess.get("end") != SESSION_END_LOCAL:
        mismatches.append({"field": "session_end", "runtime": SESSION_END_LOCAL, "frozen": sess.get("end")})
    if float(cfg.sigma_threshold) != 2.0:
        mismatches.append({"field": "sigma", "runtime": cfg.sigma_threshold, "frozen": 2.0})
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
    hold = bm.get("holdout") or {}
    train = bm.get("train") or {}
    return f"""# Frozen Strategy — GC VWAP V2 (Phase 26)

## Status

```text
paper_validation
NOT production
NO broker execution
```

## Thesis

After COMEX GC becomes statistically stretched beyond session VWAP ±2σ, objective band reclaim followed by a retest of the **frozen** 2σ band may offer positive fixed-R mean-reversion expectancy.

## Identity

| Field | Value |
|------|-------|
| Strategy family | `{doc['strategy_family']}` |
| Strategy version | `{doc['strategy_version']}` |
| Candidate | `{doc['candidate_id']}` |
| Frozen config hash | `{doc['frozen_config_hash']}` |
| Engine config hash | `{doc['engine_config_hash']}` |
| Source candidate | `{doc['source_candidate_path']}` |
| Freeze timestamp | `{doc['freeze_timestamp']}` |

## Exact rules (immutable)

### Session (America/New_York, DST-aware)

- Start: **08:20**
- End / trade expire: **13:30**
- No new setups after: **12:30**

### VWAP

- typical_price = (high + low + close) / 3
- VWAP = Σ(typical × volume) / Σ(volume) from session start
- Resets each trading day; no overnight carry; no future bars

### Sigma

- Volume-weighted std of typical prices vs **running** session VWAP
- Extension threshold: **±2.0σ**
- Warm-up: **6** completed 5m bars

### Confirmation

- `BAND_RECLAIM` only
- Upper: close returns below +2σ → short
- Lower: close returns above −2σ → long
- Forbidden: CHoCH, FVG, candle patterns, volume filter

### Entry

- `FROZEN_2SIG_RETEST`
- `entry_band_price` = 2σ band from the **first extension bar** (Phase 25 `frozen_2sig`)
- That level is preserved through reclaim confirmation and the retest wait
- Pending entry must **not** move with later VWAP/σ
- Timeout: **6** bars after confirmation

### Stop

- Extension sequence extreme (high for short, low for long)
- Buffer: **0**
- No trail / no break-even move

### Targets (tracked independently)

- 1R, 1.5R, 2R, 3R
- VWAP touch (diagnostic)

## State progression

```text
WAITING_FOR_SESSION → VWAP_WARMUP → WAITING_FOR_EXTENSION
→ EXTENDED → RECLAIM_CONFIRMED → WAITING_FOR_RETEST
→ ENTRY_TRIGGERED → STOP / TARGET_* / VWAP_HIT / EXPIRED / AMBIGUOUS
```

## Historical evidence (Phase 25 source of truth)

TRAIN V2: N={train.get('resolved_n')}, E2R={train.get('theoretical_2r_expectancy')}

HOLDOUT V2:

- N={hold.get('resolved_n')}
- stop={hold.get('stop_rate')}
- 1R={hold.get('r1_rate')} 1.5R={hold.get('r15_rate')} 2R={hold.get('r2_rate')} 3R={hold.get('r3_rate')}
- E1R={hold.get('theoretical_1r_expectancy')} E1.5R={hold.get('theoretical_1_5r_expectancy')}
- E2R={hold.get('theoretical_2r_expectancy')} E3R={hold.get('theoretical_3r_expectancy')}
- median MFE={hold.get('median_mfe_r')} MAE={hold.get('median_mae_r')}

Walk-forward: all 4 blocks E2R > 0 (see `phase25_validation.json`).

Cost: HOLDOUT E2R remains positive after 1–2 ticks/side.

## Paper-validation criteria

- Minimum resolved: **30** (preferred 50, strong 100)
- Before N=30: only `PAPER_VALIDATION_IN_PROGRESS`
- After N≥30: `FORWARD_EDGE_SUPPORTED` / `WEAK` / `NOT_SUPPORTED` / `STILL_INSUFFICIENT`
- Primary paper fill overlay: **1 tick adverse** (also report ideal and 2-tick)

## What is NOT allowed to change

Any change to sigma, session times, reclaim, entry freeze semantics, timeout, stop, or targets creates a **new** strategy version and must not contaminate Phase 26 V2 paper statistics.
"""


if __name__ == "__main__":
    info = write_frozen_files()
    print(json.dumps(info, indent=2))
