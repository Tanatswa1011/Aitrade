"""Phase 47 — lock ES DVP research candidate for prospective paper validation.

Not a production freeze. Rules become immutable for the forward campaign.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from nq_drift_vwap_models import DVPStrategyConfig

ROOT = Path(__file__).resolve().parent
PHASE46_CANDIDATE = ROOT / "strategy_candidates" / "phase46_ES_DVP.json"
LOCKED_PATH = ROOT / "strategy_candidates" / "phase47_ES_DVP_LOCKED_CANDIDATE.json"

CANDIDATE_ID = "ES_DVP_PORT"
LOCKED_VERSION = "es_dvp_v1.PORT.LOCKED_PHASE47"
STRATEGY_FAMILY = "nq_drift_vwap_pullback_v1"
INSTRUMENT = "ES"

# Authoritative numbers from strategy_candidates/phase46_ES_DVP.json — do not retune.
LOCKED_CFG = DVPStrategyConfig(
    strategy_family=STRATEGY_FAMILY,
    candidate_id=CANDIDATE_ID,
    hour_return_threshold=0.001,
    long_stop_points=18.0,
    long_target_points=9.0,
    short_stop_points=18.0,
    short_target_points=11.25,
    max_trades_per_day=4,
    max_losses_per_day=2,
    extras={"scale": 0.22547932918744962, "neighbor": 1.0, "source": "phase46_ES_DVP"},
)

ES_CLOCK = {
    "timezone": "America/New_York",
    "vwap_reset": "09:30",
    "trade_start": "10:30",
    "no_new_trades_after": "15:30",
    "force_close": "15:55",
}


def load_phase46_candidate() -> dict[str, Any]:
    if not PHASE46_CANDIDATE.exists():
        raise FileNotFoundError(f"missing_phase46_candidate:{PHASE46_CANDIDATE}")
    return json.loads(PHASE46_CANDIDATE.read_text(encoding="utf-8"))


def assert_matches_phase46(src: dict[str, Any]) -> None:
    cfg = ((src.get("metrics") or {}).get("cfg") or {})
    mismatches = []
    floats = {
        "long_stop_points": 18.0,
        "long_target_points": 9.0,
        "short_stop_points": 18.0,
        "short_target_points": 11.25,
        "hour_return_threshold": 0.001,
    }
    ints = {"max_trades_per_day": 4, "max_losses_per_day": 2}
    for name, expected in floats.items():
        got = cfg.get(name)
        if got is None or abs(float(got) - float(expected)) > 1e-12:
            mismatches.append({"field": name, "expected": expected, "got": got})
    for name, expected in ints.items():
        got = cfg.get(name)
        if got is None or int(got) != int(expected):
            mismatches.append({"field": name, "expected": expected, "got": got})
    if src.get("instrument") != "ES":
        mismatches.append({"field": "instrument", "expected": "ES", "got": src.get("instrument")})
    if mismatches:
        raise ValueError(f"phase46_candidate_mismatch:{mismatches}")


def semantic_payload() -> dict[str, Any]:
    """Rules only — lock timestamp is stored outside the hash."""
    return {
        "strategy_family": STRATEGY_FAMILY,
        "candidate_id": CANDIDATE_ID,
        "locked_version": LOCKED_VERSION,
        "instrument": INSTRUMENT,
        "hour_return_threshold": float(LOCKED_CFG.hour_return_threshold),
        "long_stop_points": float(LOCKED_CFG.long_stop_points),
        "long_target_points": float(LOCKED_CFG.long_target_points),
        "short_stop_points": float(LOCKED_CFG.short_stop_points),
        "short_target_points": float(LOCKED_CFG.short_target_points),
        "max_trades_per_day": int(LOCKED_CFG.max_trades_per_day),
        "max_losses_per_day": int(LOCKED_CFG.max_losses_per_day),
        "atr_scale_locked": 0.22547932918744962,
        "timezone": ES_CLOCK["timezone"],
        "vwap_reset": ES_CLOCK["vwap_reset"],
        "trade_start": ES_CLOCK["trade_start"],
        "no_new_trades_after": ES_CLOCK["no_new_trades_after"],
        "force_close": ES_CLOCK["force_close"],
        "vwap_price_basis": "typical_price=(H+L+C)/3",
        "vwap_formula": "sum(typical_price*volume)/sum(volume)",
        "trend_timeframe": "15m",
        "execution_timeframe": "5m",
        "completed_bars_only": True,
        "long_drift": "close>VWAP AND VWAP rising AND 1h_return>=+0.10%",
        "short_drift": "close<VWAP AND VWAP falling AND 1h_return<=-0.10%",
        "long_trigger": "first completed red 5m; entry next 5m open",
        "short_trigger": "first completed green 5m; entry next 5m open",
        "loss_cap_interpretation": "any two losing trades in the day",
        "one_position_at_a_time": True,
        "tick_size": 0.25,
        "point_usd": 50.0,
        "mes_point_usd": 5.0,
        "commission_points": 0.08,
        "primary_fill": "1_TICK_ADVERSE",
        "cost_overlays": ["IDEAL_TOUCH", "1_TICK_ADVERSE", "2_TICK_ADVERSE"],
        "news_blackout": "T-5m to T+5m around 08:30 ET; RTH entries start 10:30 so 08:30 never overlaps Phase 46 ES DVP",
        "not_production": True,
        "dry_run_only": True,
        "no_broker_execution": True,
    }


def locked_config_hash(semantic: dict[str, Any] | None = None) -> str:
    blob = json.dumps(semantic or semantic_payload(), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _slim_side(side: dict[str, Any] | None) -> dict[str, Any] | None:
    if not side:
        return None
    return {k: side.get(k) for k in ("n", "win_rate", "expectancy_points", "expectancy_r")}


def _slim_pack(pack: dict[str, Any] | None) -> dict[str, Any]:
    pack = pack or {}
    years = []
    for y in pack.get("years") or []:
        years.append(
            {
                "year": y.get("year"),
                "n_resolved": y.get("n_resolved"),
                "win_rate": y.get("win_rate"),
                "expectancy_points": y.get("expectancy_points"),
                "expectancy_r": y.get("expectancy_r"),
                "profit_factor": y.get("profit_factor"),
            }
        )
    return {
        "n_resolved": pack.get("n_resolved"),
        "expectancy_r": pack.get("expectancy_r"),
        "expectancy_points": pack.get("expectancy_points"),
        "win_rate": pack.get("win_rate"),
        "profit_factor": pack.get("profit_factor"),
        "max_dd_points": pack.get("max_dd_points"),
        "max_consec_losses": pack.get("max_consec_losses"),
        "avg_stop_points": pack.get("avg_stop_points"),
        "trades_per_year": pack.get("trades_per_year"),
        "worst_day_points": pack.get("worst_day_points"),
        "n_days": pack.get("n_days"),
        "long": _slim_side(pack.get("long")),
        "short": _slim_side(pack.get("short")),
        "years": years,
    }


def historical_benchmark(src: dict[str, Any]) -> dict[str, Any]:
    m = src.get("metrics") or {}
    prim = m.get("primary") or {}
    full = _slim_pack(prim.get("full") or {})
    return {
        "source": "strategy_candidates/phase46_ES_DVP.json",
        "phase46_status": m.get("status") or src.get("status"),
        "full": full,
        "train": _slim_pack(prim.get("train") or {}),
        "holdout": _slim_pack(prim.get("holdout") or {}),
        "walk_forward": {
            "method": "Phase 46 year-block holdouts (no additional retune). Not a new walk-forward.",
            "years": full.get("years") or [],
        },
        "cost": {
            "ideal_expectancy_r": (m.get("ideal") or {}).get("expectancy_r")
            or ((m.get("ideal") or {}).get("full") or {}).get("expectancy_r"),
            "one_tick_expectancy_r": full.get("expectancy_r"),
            "two_tick_expectancy_r": (m.get("stress_2tick") or {}).get("expectancy_r")
            or ((m.get("stress_2tick") or {}).get("full") or {}).get("expectancy_r"),
        },
        "corr_nq_dvp": m.get("corr_nq_dvp"),
        "corr_gc_v2_proxy": m.get("corr_gc_v2_proxy"),
        "atr_scale": m.get("atr_scale"),
        "threshold_stable": (m.get("flags") or {}).get("threshold_stable"),
        "n_news_removed": (prim.get("full") or {}).get("n_news_removed"),
    }


def build_locked_document(*, lock_ts: str | None = None) -> dict[str, Any]:
    src = load_phase46_candidate()
    assert_matches_phase46(src)
    semantic = semantic_payload()
    fhash = locked_config_hash(semantic)
    ts = lock_ts or datetime.now(tz=timezone.utc).isoformat()
    return {
        "phase": 47,
        "status": "LOCKED_FORWARD_VALIDATION_CANDIDATE",
        "NOT_PRODUCTION": True,
        "DRY_RUN_ONLY": True,
        "no_broker_execution": True,
        "not_frozen": True,
        "candidate_id": CANDIDATE_ID,
        "locked_version": LOCKED_VERSION,
        "instrument": INSTRUMENT,
        "strategy_family": STRATEGY_FAMILY,
        "source_phase46_candidate": "strategy_candidates/phase46_ES_DVP.json",
        "lock_timestamp": ts,
        "locked_config_hash": fhash,
        "semantic": semantic,
        "cfg": LOCKED_CFG.to_dict(),
        "session": ES_CLOCK,
        "signal_rules": {
            "vwap": "session reset 09:30 ET, typical price volume-weighted, completed bars only",
            "drift_15m": semantic["long_drift"] + " / " + semantic["short_drift"],
            "entry": "first opposing completed 5m; fill next 5m open ±1 tick adverse (overlay, not signal)",
            "one_position": True,
            "max_trades_per_day": 4,
            "max_losses_per_day": 2,
            "force_close": "15:55 ET",
            "no_new_after": "15:30 ET",
        },
        "risk_rules": {
            "long_stop_points": 18.0,
            "long_target_points": 9.0,
            "short_stop_points": 18.0,
            "short_target_points": 11.25,
            "normalization": "Phase 46 TRAIN median session ATR14 scale 0.22547932918744962 vs NQ 80/40/50. Locked. Do not retune.",
            "no_trailing": True,
            "no_breakeven": True,
        },
        "news": semantic["news_blackout"],
        "cost_model": {
            "tick_size": 0.25,
            "primary": "1_TICK_ADVERSE",
            "overlays": ["IDEAL_TOUCH", "1_TICK_ADVERSE", "2_TICK_ADVERSE"],
            "commission_points": 0.08,
        },
        "forward_journal": "journal/phase47_es_dvp_paper/paper_trades.jsonl",
        "forward_policy": "Setups count only if setup timestamp >= lock_timestamp. No historical backfill.",
        "historical_benchmark": historical_benchmark(src),
        "note": "Locked research candidate for prospective paper validation. Not strategy_frozen/. Any rule change requires a new version, hash, and journal.",
    }


def load_locked_document(path: Path = LOCKED_PATH) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"missing_locked_es_dvp:{path}")
    return json.loads(path.read_text(encoding="utf-8"))


def assert_locked_hash(doc: dict[str, Any] | None = None) -> dict[str, Any]:
    doc = doc or load_locked_document()
    expected = locked_config_hash()
    ok = doc.get("locked_config_hash") == expected
    if doc.get("locked_version") != LOCKED_VERSION:
        ok = False
    if doc.get("status") != "LOCKED_FORWARD_VALIDATION_CANDIDATE":
        ok = False
    return {
        "ok": ok,
        "stored": doc.get("locked_config_hash"),
        "recomputed": expected,
        "lock_timestamp": doc.get("lock_timestamp"),
        "status": doc.get("status"),
    }


def write_locked_document(path: Path = LOCKED_PATH) -> dict[str, Any]:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        existing = json.loads(path.read_text(encoding="utf-8"))
        expected = locked_config_hash()
        if existing.get("locked_config_hash") != expected:
            raise ValueError("locked_candidate_hash_drift_do_not_overwrite")
        return existing
    doc = build_locked_document()
    path.write_text(json.dumps(doc, indent=2), encoding="utf-8")
    return doc
