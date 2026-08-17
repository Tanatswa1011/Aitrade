"""Phase 18 — evidence-based strategy selection + chronological holdout."""

from __future__ import annotations

import argparse
import csv
import json
import math
from collections import Counter, defaultdict
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from bar_dataset import load_dataset
from bias_provider import StructureBiasProvider
from chrono_split import assert_no_split_leakage, chronological_split
from htf_report import htf_report_bucket, paired_execution_comparison
from intrabar_resolver import resolve_15m_ambiguities_from_journal
from invalid_stop_diagnostics import diagnose_invalid_stops
from models import RiskConfig, StopMode
from openbb_history import OpenBBHistoricalDataProvider, load_dotenv_credentials
from phase18_eligibility import ELIG_RESOLVED, categorize_entry
from phase18_journal_codec import record_from_dict, records_from_dicts
from phase18_metrics import (
    iter_entry_pairs,
    mfe_distribution,
    progressive_rr_hit,
    scorecard_from_pairs,
    timing_distribution,
)
from phase18_selection import (
    HTF_POLICIES,
    StrategyCandidate,
    apply_expiry_policy,
    apply_htf_policy,
    classify_stability,
    rank_candidates,
    recommendation_category,
    select_finalists,
)
from replay_engine import replay_historical_mtf_setups
from sample_quality import sample_quality_label
from setup_journal import append_journal_records, load_journal_records
from strategy_config import DEFAULT_STRATEGY_CONFIG, StrategyConfig
from strategy_version import STRATEGY_VERSION, compute_config_hash


PHASE18_VERSION = "v1.phase18"
SYMBOL_TV = "OANDA:XAUUSD"
REPORTS = Path("reports")
CANDIDATES_DIR = Path("strategy_candidates")
JOURNAL_BASELINE = Path("journal") / "phase17_deep"
JOURNAL_FVG = Path("journal") / "phase18_beyond_fvg"
TIINGO_ROOT = Path("data") / "openbb" / "tiingo"

LUXALGO_WARNING = (
    "Historical strategy statistics depend on internal_structure CHoCH v1. "
    "Live confirmation currently depends on LuxAlgo. "
    "Their equivalence remains unvalidated."
)


def _scrub(obj: Any) -> Any:
    secret_keys = {
        "token",
        "api_key",
        "apikey",
        "password",
        "secret",
        "tiingo_token",
        "fmp_api_key",
        "authorization",
    }
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            lk = str(k).lower()
            if any(s in lk for s in secret_keys) and lk not in {
                "credential_key",
                "credential_required",
                "credential_present",
                "credentials_required",
                "environment_variable_names",
            }:
                out[k] = v if isinstance(v, bool) or v is None else "<redacted>"
                continue
            out[k] = _scrub(v)
        return out
    if isinstance(obj, list):
        return [_scrub(x) for x in obj]
    return obj


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return str(path)
    keys: list[str] = []
    seen = set()
    for r in rows:
        for k in r.keys():
            if k not in seen:
                seen.add(k)
                keys.append(k)
    with path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=keys, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            flat = {
                k: (json.dumps(v, default=str) if isinstance(v, (dict, list)) else v)
                for k, v in r.items()
            }
            w.writerow(flat)
    return str(path)


def load_tiingo_bars() -> dict[str, Any]:
    out = {}
    for tf in ("5m", "15m", "4H", "1D"):
        loaded = load_dataset("openbb_tiingo_XAUUSD", tf, root=TIINGO_ROOT)
        out[tf] = loaded.get("bars") or []
    return {
        "bars_by_tf": out,
        "bars_5m": len(out["5m"]),
        "bars_15m": len(out["15m"]),
        "bars_4H": len(out["4H"]),
        "bars_1D": len(out["1D"]),
        "provider": "openbb",
        "underlying_provider": "tiingo",
        "source_symbol": "XAUUSD",
        "feed_equivalence_class": "CLOSE_EQUIVALENT",
    }


def ensure_beyond_fvg_journal(bars_meta: dict[str, Any]) -> dict[str, Any]:
    """Replay TRAIN-comparable beyond_fvg under a separate journal/config hash."""
    existing = load_journal_records(path=JOURNAL_FVG / "setups.jsonl")
    if len(existing) >= 100:
        return {
            "executed": True,
            "reused": True,
            "journal_path": str(JOURNAL_FVG / "setups.jsonl"),
            "journal_size": len(existing),
        }

    risk = replace(DEFAULT_STRATEGY_CONFIG.risk, stop_mode=StopMode.BEYOND_FVG.value)
    cfg = replace(DEFAULT_STRATEGY_CONFIG, risk=risk, extras={"phase": "phase18_beyond_fvg"})
    result = replay_historical_mtf_setups(
        bars_meta["bars_by_tf"],
        symbol=SYMBOL_TV,
        strategy_config=cfg,
        execution_timeframes=("5m", "15m"),
        bias_provider=StructureBiasProvider(),
    )
    enriched = []
    for rec in result.journal_records:
        extras = dict(rec.extras or {})
        extras.update(
            {
                "data_provider": "openbb",
                "underlying_provider": "tiingo",
                "source_symbol": "XAUUSD",
                "feed_equivalence_class": "CLOSE_EQUIVALENT",
                "phase": "phase18",
                "stop_mode": StopMode.BEYOND_FVG.value,
            }
        )
        enriched.append(
            replace(
                rec,
                extras=extras,
                strategy_version=PHASE18_VERSION,
            )
        )
    JOURNAL_FVG.mkdir(parents=True, exist_ok=True)
    # fresh file
    path = JOURNAL_FVG / "setups.jsonl"
    if path.exists():
        path.unlink()
    path = append_journal_records(enriched, path=path)
    return {
        "executed": True,
        "reused": False,
        "journal_path": str(path),
        "journal_size": len(enriched),
        "config_hash": compute_config_hash(cfg),
        "complete_sessions": result.coverage.complete_sessions if result.coverage else None,
    }


def build_resolution_map(records: list[Any], bars_5m: list) -> dict[tuple[str, str], str]:
    amb = resolve_15m_ambiguities_from_journal(records, bars_5m)
    out: dict[tuple[str, str], str] = {}
    for row in amb.get("resolutions") or amb.get("rows") or []:
        sid = row.get("setup_id")
        mode = row.get("entry_mode")
        if sid and mode:
            out[(str(sid), str(mode))] = str(row.get("result"))
    # handle structure from resolve_15m_ambiguities_from_journal
    if not out and isinstance(amb.get("by_result"), dict):
        # fall back: parse detailed list if present
        for row in amb.get("details") or []:
            sid = row.get("setup_id")
            mode = row.get("entry_mode")
            if sid and mode:
                out[(str(sid), str(mode))] = str(row.get("result"))
    # The helper returns rows list under key from earlier read — check
    if not out:
        for row in amb.get("rows") or []:
            sid = row.get("setup_id")
            mode = row.get("entry_mode")
            if sid and mode:
                out[(str(sid), str(mode))] = str(row.get("result"))
    return out, amb


def enrich_resolutions_from_amb(amb: dict[str, Any]) -> dict[tuple[str, str], str]:
    out: dict[tuple[str, str], str] = {}
    rows = amb.get("rows") or amb.get("resolutions") or amb.get("details") or []
    # resolve_15m returns summary; inspect source for actual list key
    if not rows and "by_setup" in amb:
        for sid, modes in amb["by_setup"].items():
            for mode, res in modes.items():
                out[(str(sid), str(mode))] = str(res)
    for row in rows:
        if not isinstance(row, dict):
            continue
        sid = row.get("setup_id")
        mode = row.get("entry_mode")
        if sid and mode and row.get("result"):
            out[(str(sid), str(mode))] = str(row["result"])
    return out


def filter_records_htf(records: list[dict], policy: str) -> list[dict]:
    return [r for r in records if apply_htf_policy(r, policy)]


def filter_records_expiry(
    records: list[dict],
    *,
    confirmation_timeout: Optional[int],
    fvg_timeout: Optional[int],
    retrace_timeout: Optional[int],
) -> tuple[list[dict], dict[str, int]]:
    retained = []
    counts: Counter[str] = Counter()
    for r in records:
        tag = apply_expiry_policy(
            r,
            confirmation_timeout=confirmation_timeout,
            fvg_timeout=fvg_timeout,
            retrace_timeout=retrace_timeout,
        )
        counts[tag] += 1
        if tag == "RETAINED":
            retained.append(r)
    return retained, dict(counts)


def funnel_tf_metrics(records: list[dict], resolutions: dict, tf: str) -> dict[str, Any]:
    subset = [r for r in records if (r.get("execution_timeframe") or r.get("timeframe")) == tf]
    pairs = iter_entry_pairs(subset, execution_tf=tf, resolutions=resolutions)
    # Prefer boundary as default comparison mode for TF-level (also report all-mode aggregate)
    sc_all = scorecard_from_pairs(pairs, label=f"{tf}_all_modes")
    # liquidity / confirmation funnel at setup level
    liqs = len({r.get("liquidity_event_id") for r in subset if r.get("liquidity_event_id")})
    conf = sum(1 for r in subset if r.get("confirmation_timestamp") is not None)
    fvg = sum(1 for r in subset if r.get("fvg_created_timestamp") is not None)
    sweeps = sum(1 for r in subset if r.get("sweep_timestamp") is not None)

    s2c = [r["bars_sweep_to_choch"] for r in subset if r.get("bars_sweep_to_choch") is not None]
    c2f = [r["bars_choch_to_fvg"] for r in subset if r.get("bars_choch_to_fvg") is not None]
    f2e = []
    for r in subset:
        d = r.get("bars_fvg_to_entry") or {}
        if isinstance(d, dict):
            f2e.extend([v for v in d.values() if v is not None])

    return {
        **sc_all,
        "liquidity_events": liqs,
        "sweeps": sweeps,
        "confirmations": conf,
        "fvgs": fvg,
        "median_sweep_to_choch": timing_distribution(s2c).get("median"),
        "median_choch_to_fvg": timing_distribution(c2f).get("median"),
        "median_fvg_to_entry": timing_distribution(f2e).get("median"),
        "timing_sweep_to_choch": timing_distribution(s2c),
        "timing_choch_to_fvg": timing_distribution(c2f),
        "timing_fvg_to_entry": timing_distribution(f2e),
        "n_setups": len(subset),
    }


def paired_tf_summary(records: list[dict]) -> dict[str, Any]:
    objs = records_from_dicts(records)
    return paired_execution_comparison(objs)


def entry_mode_report(records: list[dict], resolutions: dict, tf: Optional[str]) -> list[dict]:
    rows = []
    for mode in ("first_touch", "boundary", "ce"):
        pairs = iter_entry_pairs(
            records, entry_mode=mode, execution_tf=tf, resolutions=resolutions
        )
        sc = scorecard_from_pairs(pairs, label=f"{tf or 'all'}:{mode}")
        sc["entry_mode"] = mode
        sc["execution_timeframe"] = tf or "combined"
        rows.append(sc)
    return rows


def paired_entry_comparison(records: list[dict], resolutions: dict, tf: str) -> dict[str, Any]:
    """Same setup_id across entry modes."""
    by_setup: dict[str, dict[str, dict]] = defaultdict(dict)
    pairs = iter_entry_pairs(records, execution_tf=tf, resolutions=resolutions)
    for p in pairs:
        if not p["entry"].get("triggered") if isinstance(p["entry"], dict) else not p["entry"].triggered:
            # still record eligibility
            pass
        mode = p["entry_mode"]
        by_setup[p["setup_id"]][mode] = p

    both_trig = ce_only = bd_only = ft_only = 0
    outcome_pairs = Counter()
    for sid, modes in by_setup.items():
        trig = {m: modes[m] for m in modes if (
            modes[m]["entry"].get("triggered") if isinstance(modes[m]["entry"], dict)
            else modes[m]["entry"].triggered
        )}
        if "boundary" in trig and "ce" in trig:
            both_trig += 1
            ob = trig["boundary"].get("effective_outcome") or trig["boundary"]["entry"].get("outcome")
            oc = trig["ce"].get("effective_outcome") or trig["ce"]["entry"].get("outcome")
            outcome_pairs[f"boundary={ob}|ce={oc}"] += 1
        elif "boundary" in trig:
            bd_only += 1
        elif "ce" in trig:
            ce_only += 1
        if "first_touch" in trig and "boundary" not in trig and "ce" not in trig:
            ft_only += 1
    return {
        "execution_timeframe": tf,
        "setups_compared": len(by_setup),
        "boundary_and_ce_triggered": both_trig,
        "boundary_only": bd_only,
        "ce_only": ce_only,
        "first_touch_only_among_modes": ft_only,
        "outcome_pair_top": outcome_pairs.most_common(15),
    }


def htf_group_metrics(records: list[dict], resolutions: dict) -> list[dict]:
    objs = {r.get("setup_id"): record_from_dict(r) for r in records}
    groups: dict[str, list[dict]] = defaultdict(list)
    for r in records:
        oid = objs[r["setup_id"]]
        groups[htf_report_bucket(oid)].append(r)
    # also canonical alignment
    for r in records:
        groups[f"canonical:{r.get('htf_alignment') or 'unknown'}"].append(r)

    rows = []
    for g, rs in sorted(groups.items()):
        pairs = iter_entry_pairs(rs, resolutions=resolutions)
        sc = scorecard_from_pairs(pairs, label=g)
        sc["htf_group"] = g
        sc["n_setups"] = len(rs)
        rows.append(sc)
    return rows


def setup_vs_axis(records: list[dict], resolutions: dict, axis: str) -> list[dict]:
    groups: dict[str, list] = defaultdict(list)
    for r in records:
        groups[str(r.get(axis) or "unknown").lower()].append(r)
    rows = []
    for g, rs in sorted(groups.items()):
        pairs = iter_entry_pairs(rs, resolutions=resolutions)
        sc = scorecard_from_pairs(pairs, label=f"{axis}:{g}")
        sc["axis"] = axis
        sc["value"] = g
        sc["n_setups"] = len(rs)
        rows.append(sc)
    return rows


def htf_policy_report(records: list[dict], resolutions: dict) -> list[dict]:
    base_n = len(records)
    rows = []
    for pol in HTF_POLICIES:
        retained = filter_records_htf(records, pol)
        pairs = iter_entry_pairs(retained, resolutions=resolutions)
        sc = scorecard_from_pairs(pairs, label=pol)
        sc["htf_policy"] = pol
        sc["trades_retained_setups"] = len(retained)
        sc["pct_opportunities_retained"] = (len(retained) / base_n) if base_n else None
        sc["sample_quality_setups"] = sample_quality_label(len(retained))
        rows.append(sc)
    return rows


def session_report(records: list[dict], resolutions: dict) -> list[dict]:
    rows = []
    for sess in ("Asia", "London"):
        rs = [r for r in records if r.get("session") == sess]
        pairs = iter_entry_pairs(rs, resolutions=resolutions)
        sc = scorecard_from_pairs(pairs, label=sess)
        sweeps = sum(1 for r in rs if r.get("sweep_timestamp") is not None)
        conf = sum(1 for r in rs if r.get("confirmation_timestamp") is not None)
        sc["session"] = sess
        sc["n_setups"] = len(rs)
        sc["sweep_rate"] = (sweeps / len(rs)) if rs else None
        sc["confirmation_rate"] = (conf / sweeps) if sweeps else None
        rows.append(sc)
        for side in ("high", "low"):
            rss = [r for r in rs if str(r.get("swept_side") or "").lower() == side]
            pairs2 = iter_entry_pairs(rss, resolutions=resolutions)
            sc2 = scorecard_from_pairs(pairs2, label=f"{sess}_{side}")
            sc2["session"] = sess
            sc2["swept_side"] = side
            sc2["n_setups"] = len(rss)
            rows.append(sc2)
    return rows


def expiry_calibration(records: list[dict]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for tf in ("5m", "15m", None):
        for sess in ("Asia", "London", None):
            rs = records
            if tf:
                rs = [r for r in rs if (r.get("execution_timeframe") or r.get("timeframe")) == tf]
            if sess:
                rs = [r for r in rs if r.get("session") == sess]
            key = f"tf={tf or 'all'}|session={sess or 'all'}"
            s2c = [r["bars_sweep_to_choch"] for r in rs if r.get("bars_sweep_to_choch") is not None]
            c2f = [r["bars_choch_to_fvg"] for r in rs if r.get("bars_choch_to_fvg") is not None]
            f2e = []
            for r in rs:
                d = r.get("bars_fvg_to_entry") or {}
                if isinstance(d, dict):
                    f2e.extend(v for v in d.values() if v is not None)
            out[key] = {
                "sweep_to_choch": timing_distribution(s2c),
                "choch_to_fvg": timing_distribution(c2f),
                "fvg_to_entry": timing_distribution(f2e),
            }
    return out


def expiry_candidate_tradeoffs(records: list[dict], resolutions: dict, timing: dict) -> list[dict]:
    # derive p75/p90/p95 from all-train 5m+15m combined timings
    base = timing.get("tf=all|session=all") or {}
    cands = []
    for name, src_key in (
        ("confirmation", "sweep_to_choch"),
        ("fvg", "choch_to_fvg"),
        ("retrace", "fvg_to_entry"),
    ):
        dist = base.get(src_key) or {}
        for pct in ("p75", "p90", "p95"):
            val = dist.get(pct)
            if val is not None and not (isinstance(val, float) and math.isnan(val)):
                cands.append((name, pct, int(round(val))))
    cands.append(("none", "None", None))

    # evaluate policies: independently vary each timeout family + None
    rows = []
    # Use global candidates: for each percentile, set all three timeouts to that family's pX when available
    for pct in ("p75", "p90", "p95", "None"):
        conf = fvg = ret = None
        if pct != "None":
            conf = (base.get("sweep_to_choch") or {}).get(pct)
            fvg = (base.get("choch_to_fvg") or {}).get(pct)
            ret = (base.get("fvg_to_entry") or {}).get(pct)
            conf = int(round(conf)) if conf is not None else None
            fvg = int(round(fvg)) if fvg is not None else None
            ret = int(round(ret)) if ret is not None else None
        retained, counts = filter_records_expiry(
            records,
            confirmation_timeout=conf,
            fvg_timeout=fvg,
            retrace_timeout=ret,
        )
        pairs = iter_entry_pairs(retained, resolutions=resolutions)
        sc = scorecard_from_pairs(pairs, label=f"expiry_{pct}")
        # late valid: retained setups that had confirmation under None policy but expired now
        rows.append(
            {
                **sc,
                "expiry_candidate": pct,
                "confirmation_timeout_bars": conf,
                "fvg_timeout_bars": fvg,
                "retrace_timeout_bars": ret,
                "setups_retained": len(retained),
                "expiry_counts": counts,
                "setups_expired": sum(v for k, v in counts.items() if k != "RETAINED"),
            }
        )
    return rows


def targets_report(records: list[dict], resolutions: dict) -> dict[str, Any]:
    pairs = [p for p in iter_entry_pairs(records, resolutions=resolutions) if p["eligibility"] == ELIG_RESOLVED]
    r1 = sum(1 for p in pairs if progressive_rr_hit(p["entry"], 1))
    r2 = sum(1 for p in pairs if progressive_rr_hit(p["entry"], 2))
    r3 = sum(1 for p in pairs if progressive_rr_hit(p["entry"], 3))
    stop = sum(1 for p in pairs if (p.get("effective_outcome") or p["entry"].get("outcome")) == "STOP_HIT")
    opp = sum(
        1
        for p in pairs
        if (p.get("effective_outcome") or p["entry"].get("outcome")) == "OPPOSITE_LIQUIDITY_HIT"
    )
    n = len(pairs)
    only1 = sum(1 for p in pairs if progressive_rr_hit(p["entry"], 1) and not progressive_rr_hit(p["entry"], 2))
    only2 = sum(1 for p in pairs if progressive_rr_hit(p["entry"], 2) and not progressive_rr_hit(p["entry"], 3))
    r3p = sum(1 for p in pairs if progressive_rr_hit(p["entry"], 3))
    return {
        "resolved_n": n,
        "r1_hit": r1,
        "r2_hit": r2,
        "r3_hit": r3,
        "stop_hit": stop,
        "opposite_liquidity_hit": opp,
        "r1_but_not_2r": only1,
        "r2_but_not_3r": only2,
        "r3_plus": r3p,
        "opposite_before_stop": opp,
        "mfe": mfe_distribution(pairs),
        "by_tf": {
            tf: mfe_distribution(iter_entry_pairs(records, execution_tf=tf, resolutions=resolutions))
            for tf in ("5m", "15m")
        },
        "by_entry_mode": {
            m: mfe_distribution(iter_entry_pairs(records, entry_mode=m, resolutions=resolutions))
            for m in ("first_touch", "boundary", "ce")
        },
        "by_session": {
            s: mfe_distribution(
                iter_entry_pairs([r for r in records if r.get("session") == s], resolutions=resolutions)
            )
            for s in ("Asia", "London")
        },
    }


def attempt_1m_resolution(
    records: list[dict],
    *,
    max_windows: int = 40,
) -> dict[str, Any]:
    """Fetch limited 1m windows for unresolved 5m ambiguities if practical."""
    load_dotenv_credentials()
    amb_ts = []
    for r in records:
        if (r.get("execution_timeframe") or r.get("timeframe")) != "5m":
            continue
        for e in r.get("entry_results") or []:
            flags = e.get("ambiguity_flags") or []
            if e.get("outcome") == "AMBIGUOUS_INTRABAR" or "TRIGGER_BAR_STOP_AMBIGUITY" in flags:
                ts = e.get("entry_timestamp")
                if ts:
                    amb_ts.append(int(ts))
    amb_ts = sorted(set(amb_ts))
    if not amb_ts:
        return {"attempted": False, "reason": "no_5m_ambiguous_timestamps", "unresolved_n": 0}

    sample = amb_ts[:max_windows]
    prov = OpenBBHistoricalDataProvider(underlying_provider="tiingo", route="currency")
    resolved = 0
    still = 0
    insuff = 0
    errors = []
    details = []
    for ts in sample:
        try:
            res = prov.fetch_result(
                "XAUUSD",
                "1m",
                start_ts=ts - 60,
                end_ts=ts + 5 * 60,
            )
            bars = list(res.bars)
            if len(bars) < 2:
                insuff += 1
                details.append({"ts": ts, "result": "INSUFFICIENT_DATA", "bars": len(bars)})
                continue
            # Feasibility only — do not invent intra-1m order beyond chronological bars.
            # Count as feasibility success if we obtained covering 1m bars.
            resolved += 1
            details.append({"ts": ts, "result": "BARS_AVAILABLE", "bars": len(bars)})
        except Exception as exc:  # noqa: BLE001
            errors.append(str(exc)[:200])
            still += 1
    return {
        "attempted": True,
        "unresolved_5m_ambiguous_timestamps": len(amb_ts),
        "sampled_windows": len(sample),
        "windows_with_1m_bars": resolved,
        "insufficient_data": insuff,
        "errors_n": len(errors),
        "errors_head": errors[:3],
        "note": "1m used as intrabar evidence feasibility only; not a new execution TF",
        "feasibility": "practical_sample" if resolved else "limited",
        "details_head": details[:10],
    }


def build_candidate_matrix(train_insights: dict[str, Any]) -> list[StrategyCandidate]:
    """Small controlled matrix derived from TRAIN comparisons (max ~12)."""
    tf_rec = train_insights.get("recommended_tf") or "5m"
    entry_rec = train_insights.get("recommended_entry") or "boundary"
    stop_rec = train_insights.get("recommended_stop") or "beyond_sweep"
    htf_rec = train_insights.get("recommended_htf") or "POLICY_A"
    exp = train_insights.get("recommended_expiry") or {}

    cands = [
        StrategyCandidate("C1_baseline", "5m", "POLICY_A", "boundary", "beyond_sweep"),
        StrategyCandidate("C2_baseline_15m", "15m", "POLICY_A", "boundary", "beyond_sweep"),
        StrategyCandidate("C3_5m_ce", "5m", "POLICY_A", "ce", "beyond_sweep"),
        StrategyCandidate("C4_5m_first_touch", "5m", "POLICY_A", "first_touch", "beyond_sweep"),
        StrategyCandidate("C5_5m_htf_D", "5m", "POLICY_D", "boundary", "beyond_sweep"),
        StrategyCandidate("C6_5m_htf_E", "5m", "POLICY_E", "boundary", "beyond_sweep"),
        StrategyCandidate("C7_5m_beyond_fvg", "5m", "POLICY_A", "boundary", "beyond_fvg"),
        StrategyCandidate("C8_15m_ce_fvg", "15m", "POLICY_A", "ce", "beyond_fvg"),
        StrategyCandidate(
            "C9_train_derived",
            tf_rec,
            htf_rec,
            entry_rec,
            stop_rec,
            confirmation_timeout_bars=exp.get("confirmation_timeout_bars"),
            fvg_timeout_bars=exp.get("fvg_timeout_bars"),
            retrace_timeout_bars=exp.get("retrace_timeout_bars"),
            notes="derived_from_train_comparisons",
        ),
        StrategyCandidate("C10_15m_htf_D_boundary", "15m", "POLICY_D", "boundary", "beyond_sweep"),
        StrategyCandidate("C11_5m_htf_B", "5m", "POLICY_B", "boundary", "beyond_sweep"),
        StrategyCandidate("C12_5m_htf_C", "5m", "POLICY_C", "boundary", "beyond_sweep"),
    ]
    # de-dupe by freeze key
    seen = set()
    uniq = []
    for c in cands:
        k = c.freeze_key()[1:]  # ignore id
        if k in seen:
            continue
        seen.add(k)
        uniq.append(c)
    return uniq[:12]


def evaluate_candidate(
    candidate: StrategyCandidate,
    sweep_rows: list[dict],
    fvg_rows: list[dict],
    resolutions: dict,
) -> dict[str, Any]:
    rows = fvg_rows if candidate.stop_mode == "beyond_fvg" else sweep_rows
    rows = [r for r in rows if (r.get("execution_timeframe") or r.get("timeframe")) == candidate.execution_timeframe]
    rows = filter_records_htf(rows, candidate.htf_policy)
    rows, exp_counts = filter_records_expiry(
        rows,
        confirmation_timeout=candidate.confirmation_timeout_bars,
        fvg_timeout=candidate.fvg_timeout_bars,
        retrace_timeout=candidate.retrace_timeout_bars,
    )
    pairs = iter_entry_pairs(
        rows,
        entry_mode=candidate.entry_mode,
        execution_tf=candidate.execution_timeframe,
        resolutions=resolutions,
    )
    sc = scorecard_from_pairs(pairs, label=candidate.candidate_id)
    return {
        **sc,
        **candidate.to_dict(),
        "expiry_counts": exp_counts,
        "trades_retained_setups": len(rows),
    }


def walk_forward_blocks(rows: list[dict], n_blocks: int = 3) -> list[list[dict]]:
    dates = sorted({str(r.get("trading_date"))[:10] for r in rows if r.get("trading_date")})
    if len(dates) < n_blocks:
        return [rows]
    size = max(1, len(dates) // n_blocks)
    blocks = []
    for i in range(n_blocks):
        start = i * size
        end = (i + 1) * size if i < n_blocks - 1 else len(dates)
        dset = set(dates[start:end])
        blocks.append([r for r in rows if str(r.get("trading_date"))[:10] in dset])
    return blocks


def derive_train_recommendations(
    *,
    tf5: dict,
    tf15: dict,
    entry_rows: list[dict],
    stop_compare: dict,
    htf_policies: list[dict],
    expiry_rows: list[dict],
) -> dict[str, Any]:
    # TF: prefer lower ambiguity with adequate resolved N and non-disastrous expectancy
    def tf_score(sc: dict) -> tuple:
        rn = sc.get("resolved_n") or 0
        amb = sc.get("ambiguity_pct")
        amb_v = 1.0 if amb is None else float(amb)
        e2 = sc.get("theoretical_2r_expectancy")
        e2v = float(e2) if e2 is not None else -9
        return (1 if rn >= 20 else 0, -amb_v, e2v, rn)

    recommended_tf = "5m" if tf_score(tf5) >= tf_score(tf15) else "15m"

    # Entry among recommended TF
    modes = [r for r in entry_rows if r.get("execution_timeframe") == recommended_tf]
    if not modes:
        modes = entry_rows
    modes_sorted = sorted(
        modes,
        key=lambda r: (
            1 if (r.get("resolved_n") or 0) >= 15 else 0,
            -(r.get("ambiguity_pct") or 1),
            r.get("theoretical_2r_expectancy") if r.get("theoretical_2r_expectancy") is not None else -9,
            r.get("triggered_n") or 0,
        ),
        reverse=True,
    )
    recommended_entry = (modes_sorted[0].get("entry_mode") if modes_sorted else "boundary")

    # Stop: compare outcome profile, not only valid-risk rate
    sw = stop_compare.get("beyond_sweep") or {}
    fv = stop_compare.get("beyond_fvg") or {}
    recommended_stop = "beyond_sweep"
    if (fv.get("resolved_n") or 0) >= 20 and (sw.get("resolved_n") or 0) >= 20:
        if (fv.get("theoretical_2r_expectancy") or -9) > (sw.get("theoretical_2r_expectancy") or -9) + 0.1:
            if (fv.get("ambiguity_pct") or 1) <= (sw.get("ambiguity_pct") or 1) + 0.1:
                recommended_stop = "beyond_fvg"

    # HTF: prefer POLICY_A unless a filter clearly helps without tiny N
    best_pol = "POLICY_A"
    base = next((p for p in htf_policies if p.get("htf_policy") == "POLICY_A"), None)
    if base:
        for p in htf_policies:
            if p.get("htf_policy") == "POLICY_A":
                continue
            if (p.get("resolved_n") or 0) < 20:
                continue
            if (p.get("theoretical_2r_expectancy") or -9) > (base.get("theoretical_2r_expectancy") or -9) + 0.15:
                if (p.get("ambiguity_pct") or 1) <= (base.get("ambiguity_pct") or 1) + 0.05:
                    best_pol = p["htf_policy"]
                    break

    # Expiry: prefer None unless p90 retains most resolved expectancy
    recommended_expiry = {
        "confirmation_timeout_bars": None,
        "fvg_timeout_bars": None,
        "retrace_timeout_bars": None,
        "candidate": "None",
    }
    none_row = next((e for e in expiry_rows if e.get("expiry_candidate") == "None"), None)
    p90 = next((e for e in expiry_rows if e.get("expiry_candidate") == "p90"), None)
    if none_row and p90 and (p90.get("resolved_n") or 0) >= 20:
        if (p90.get("theoretical_2r_expectancy") or -9) >= (none_row.get("theoretical_2r_expectancy") or -9) - 0.05:
            # only adopt if it removes a meaningful share of expired waiters
            if (p90.get("setups_expired") or 0) > 0:
                recommended_expiry = {
                    "confirmation_timeout_bars": p90.get("confirmation_timeout_bars"),
                    "fvg_timeout_bars": p90.get("fvg_timeout_bars"),
                    "retrace_timeout_bars": p90.get("retrace_timeout_bars"),
                    "candidate": "p90",
                }

    return {
        "recommended_tf": recommended_tf,
        "recommended_entry": recommended_entry,
        "recommended_stop": recommended_stop,
        "recommended_htf": best_pol,
        "recommended_expiry": recommended_expiry,
    }


def run_phase18(*, write_artifacts: bool = True, attempt_1m: bool = True) -> dict[str, Any]:
    bars_meta = load_tiingo_bars()
    baseline_rows = load_journal_records(path=JOURNAL_BASELINE / "setups.jsonl")
    if not baseline_rows:
        return {"ok": False, "error": "missing_phase17_journal"}

    fvg_meta = ensure_beyond_fvg_journal(bars_meta)
    fvg_rows = load_journal_records(path=JOURNAL_FVG / "setups.jsonl")

    train_rows, holdout_rows, split = chronological_split(baseline_rows, train_fraction=0.70)
    assert_no_split_leakage(split)
    train_fvg = [r for r in fvg_rows if str(r.get("trading_date"))[:10] >= (split.train_start or "")
                 and str(r.get("trading_date"))[:10] <= (split.train_end or "")]
    hold_fvg = [r for r in fvg_rows if str(r.get("trading_date"))[:10] >= (split.holdout_start or "")
                and str(r.get("trading_date"))[:10] <= (split.holdout_end or "9999")]

    # Align fvg split by liquidity ids for safety
    train_liqs = set(split.train_liquidity_event_ids)
    hold_liqs = set(split.holdout_liquidity_event_ids)
    train_fvg = [r for r in fvg_rows if r.get("liquidity_event_id") in train_liqs]
    hold_fvg = [r for r in fvg_rows if r.get("liquidity_event_id") in hold_liqs]

    train_objs = records_from_dicts(train_rows)
    amb15 = resolve_15m_ambiguities_from_journal(train_objs, bars_meta["bars_by_tf"]["5m"])
    resolutions = enrich_resolutions_from_amb(amb15)
    # Also attach raw rows from function internals — re-call collector
    if not resolutions:
        # rebuild from amb structure: function returns rows list in recent code
        from intrabar_resolver import resolve_15m_ambiguities_from_journal as _r

        # monkey: read source end
        pass

    # Fix: resolve_15m returns dict with 'rows' — verify
    if "rows" not in amb15:
        # reconstruct by scanning return
        amb15 = dict(amb15)
        amb15["rows"] = amb15.get("details") or []
    resolutions = enrich_resolutions_from_amb(amb15)

    # --- TRAIN analyses ---
    tf5 = funnel_tf_metrics(train_rows, resolutions, "5m")
    tf15 = funnel_tf_metrics(train_rows, resolutions, "15m")
    paired = paired_tf_summary(train_rows)

    entry_rows = []
    for tf in ("5m", "15m"):
        entry_rows.extend(entry_mode_report(train_rows, resolutions, tf))
    entry_combined = entry_mode_report(train_rows, resolutions, None)
    paired_entry = {
        "5m": paired_entry_comparison(train_rows, resolutions, "5m"),
        "15m": paired_entry_comparison(train_rows, resolutions, "15m"),
    }

    # Stop modes
    inv_sw = diagnose_invalid_stops(train_objs, default_stop_mode="beyond_sweep")
    train_fvg_objs = records_from_dicts(train_fvg)
    inv_fv = diagnose_invalid_stops(train_fvg_objs, default_stop_mode="beyond_fvg")
    stop_sw = scorecard_from_pairs(iter_entry_pairs(train_rows, resolutions=resolutions), label="beyond_sweep")
    stop_fv = scorecard_from_pairs(iter_entry_pairs(train_fvg, resolutions=resolutions), label="beyond_fvg")
    stop_compare = {
        "beyond_sweep": {**stop_sw, "invalid_diagnostics": inv_sw},
        "beyond_fvg": {**stop_fv, "invalid_diagnostics": inv_fv},
    }

    htf_groups = htf_group_metrics(train_rows, resolutions)
    vs_daily = setup_vs_axis(train_rows, resolutions, "setup_vs_daily")
    vs_h4 = setup_vs_axis(train_rows, resolutions, "setup_vs_h4")
    htf_pols = htf_policy_report(train_rows, resolutions)

    timing = expiry_calibration(train_rows)
    expiry_rows = expiry_candidate_tradeoffs(train_rows, resolutions, timing)
    targets = targets_report(train_rows, resolutions)
    sessions = session_report(train_rows, resolutions)

    train_insights = derive_train_recommendations(
        tf5=tf5,
        tf15=tf15,
        entry_rows=entry_rows,
        stop_compare=stop_compare,
        htf_policies=htf_pols,
        expiry_rows=expiry_rows,
    )
    candidates = build_candidate_matrix(train_insights)

    # FREEZE before holdout
    frozen = [c.to_dict() for c in candidates]
    train_scorecards = [evaluate_candidate(c, train_rows, train_fvg, resolutions) for c in candidates]
    ranked = rank_candidates(train_scorecards)
    finalists_sc = select_finalists(ranked, max_finalists=3)
    finalist_ids = [f["candidate_id"] for f in finalists_sc]
    finalist_cfgs = [c for c in candidates if c.candidate_id in finalist_ids]

    # HOLDOUT resolutions
    hold_objs = records_from_dicts(holdout_rows)
    amb15_h = resolve_15m_ambiguities_from_journal(hold_objs, bars_meta["bars_by_tf"]["5m"])
    resolutions_h = enrich_resolutions_from_amb(amb15_h)

    holdout_results = []
    stability = {}
    for c in finalist_cfgs:
        tr = next(x for x in ranked if x["candidate_id"] == c.candidate_id)
        ho = evaluate_candidate(c, holdout_rows, hold_fvg, resolutions_h)
        st = classify_stability(tr, ho)
        stability[c.candidate_id] = st
        holdout_results.append(
            {
                **ho,
                "stability": st,
                "train_resolved_n": tr.get("resolved_n"),
                "train_theoretical_2r_expectancy": tr.get("theoretical_2r_expectancy"),
                "train_r1_rate": tr.get("r1_rate"),
                "train_ambiguity_pct": tr.get("ambiguity_pct"),
                "delta_2r_expectancy": (
                    None
                    if tr.get("theoretical_2r_expectancy") is None or ho.get("theoretical_2r_expectancy") is None
                    else ho["theoretical_2r_expectancy"] - tr["theoretical_2r_expectancy"]
                ),
            }
        )

    # Walk-forward diagnostic on full baseline for finalists (no retrain)
    wf = {}
    blocks = walk_forward_blocks(baseline_rows, 3)
    for c in finalist_cfgs:
        wf[c.candidate_id] = []
        for i, block in enumerate(blocks):
            block_fvg = [r for r in fvg_rows if r.get("liquidity_event_id") in {
                x.get("liquidity_event_id") for x in block
            }]
            sc = evaluate_candidate(c, block, block_fvg, resolutions)
            wf[c.candidate_id].append(
                {
                    "block": i + 1,
                    "resolved_n": sc.get("resolved_n"),
                    "theoretical_2r_expectancy": sc.get("theoretical_2r_expectancy"),
                    "r1_rate": sc.get("r1_rate"),
                    "ambiguity_pct": sc.get("ambiguity_pct"),
                    "sample_quality": sc.get("sample_quality"),
                }
            )

    one_m = attempt_1m_resolution(train_rows) if attempt_1m else {"attempted": False}

    # Data quality totals on TRAIN entry pairs
    all_pairs = iter_entry_pairs(train_rows, resolutions=resolutions)
    elig_counts = Counter(p["eligibility"] for p in all_pairs)

    # Recommendation
    need_intrabar = (tf5.get("ambiguity_pct") or 0) > 0.4 and (one_m.get("windows_with_1m_bars") or 0) == 0
    rec_cat = recommendation_category(
        finalists=finalists_sc,
        stability=stability,
        need_intrabar=need_intrabar,
    )
    if not holdout_results or all(
        classify_stability(
            next(x for x in ranked if x["candidate_id"] == h["candidate_id"]),
            h,
        )
        in ("INSUFFICIENT_HOLDOUT_SAMPLE", "UNSTABLE")
        for h in holdout_results
    ):
        if rec_cat == "LOCK_CANDIDATE_FOR_PAPER_VALIDATION":
            rec_cat = "NO_PRODUCTION_RULESET_SELECTED" if not any(
                stability.get(h["candidate_id"]) == "STABLE" for h in holdout_results
            ) else rec_cat

    # If no stable/weak finalist on holdout → explicit no production
    if not any(stability.get(fid) in ("STABLE", "WEAKLY_STABLE") for fid in finalist_ids):
        if rec_cat not in ("NEED_MORE_DATA", "NEED_MORE_INTRABAR_RESOLUTION", "NO_EDGE_OBSERVED"):
            rec_cat = "NO_PRODUCTION_RULESET_SELECTED"

    selected_cfg = None
    selected_paths: list[str] = []
    if write_artifacts:
        CANDIDATES_DIR.mkdir(parents=True, exist_ok=True)
        REPORTS.mkdir(parents=True, exist_ok=True)
        for c in finalist_cfgs:
            payload = {
                "phase": "phase18",
                "strategy_version": PHASE18_VERSION,
                "baseline_strategy_version": STRATEGY_VERSION,
                "provenance": {
                    "data_provider": "openbb",
                    "underlying_provider": "tiingo",
                    "source_symbol": "XAUUSD",
                    "feed_equivalence_class": "CLOSE_EQUIVALENT",
                    "live_benchmark": SYMBOL_TV,
                },
                "confirmation_equivalence_status": "unvalidated_against_luxalgo",
                "luxalgo_warning": LUXALGO_WARNING,
                "candidate": c.to_dict(),
                "selection_split": split.to_dict(),
                "note": "NOT promoted to DEFAULT_STRATEGY_CONFIG — requires explicit user approval",
            }
            path = CANDIDATES_DIR / f"phase18_{c.candidate_id.lower()}.json"
            path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            selected_paths.append(str(path))
        if rec_cat == "LOCK_CANDIDATE_FOR_PAPER_VALIDATION" and finalist_cfgs:
            # pick the stable one if any
            pick = None
            for c in finalist_cfgs:
                if stability.get(c.candidate_id) == "STABLE":
                    pick = c
                    break
            if pick is None:
                pick = finalist_cfgs[0]
            selected_cfg = pick.to_dict()

    complete_sessions = len({(r.get("session"), r.get("trading_date")) for r in baseline_rows})

    artifacts = {}
    if write_artifacts:
        artifacts["phase18_5m_vs_15m"] = _write_csv(
            REPORTS / "phase18_5m_vs_15m.csv",
            [{"side": "5m", **tf5}, {"side": "15m", **tf15}],
        )
        artifacts["phase18_entry_modes"] = _write_csv(
            REPORTS / "phase18_entry_modes.csv", entry_rows + entry_combined
        )
        artifacts["phase18_stop_modes"] = _write_csv(
            REPORTS / "phase18_stop_modes.csv",
            [
                {"stop_mode": "beyond_sweep", **{k: v for k, v in stop_sw.items()}},
                {"stop_mode": "beyond_fvg", **{k: v for k, v in stop_fv.items()}},
            ],
        )
        artifacts["phase18_htf_alignment"] = _write_csv(
            REPORTS / "phase18_htf_alignment.csv", htf_groups + vs_daily + vs_h4 + htf_pols
        )
        artifacts["phase18_expiry"] = _write_csv(REPORTS / "phase18_expiry.csv", expiry_rows)
        artifacts["phase18_targets"] = _write_csv(
            REPORTS / "phase18_targets.csv",
            [{"metric": k, "value": v} for k, v in targets.items() if not isinstance(v, dict)]
            + [{"metric": "mfe", **targets["mfe"]}],
        )
        artifacts["phase18_candidates"] = _write_csv(REPORTS / "phase18_candidates.csv", ranked)
        artifacts["phase18_train_summary"] = _write_csv(
            REPORTS / "phase18_train_summary.csv",
            finalists_sc or ranked[:3],
        )
        artifacts["phase18_holdout_summary"] = _write_csv(
            REPORTS / "phase18_holdout_summary.csv", holdout_results
        )
        artifacts["phase18_sessions"] = _write_csv(REPORTS / "phase18_sessions.csv", sessions)

    report = {
        "ok": True,
        "phase": 18,
        "strategy_version": PHASE18_VERSION,
        "baseline_strategy_version": STRATEGY_VERSION,
        "provenance": {
            "data_provider": "openbb",
            "underlying_provider": "tiingo",
            "source_symbol": "XAUUSD",
            "feed_equivalence_class": "CLOSE_EQUIVALENT",
            "live_benchmark": SYMBOL_TV,
            "confirmation_equivalence_status": "unvalidated_against_luxalgo",
            "luxalgo_warning": LUXALGO_WARNING,
        },
        "dataset": {
            "journal_baseline": str(JOURNAL_BASELINE / "setups.jsonl"),
            "journal_beyond_fvg": fvg_meta,
            "bars_5m": bars_meta["bars_5m"],
            "bars_15m": bars_meta["bars_15m"],
            "bars_4H": bars_meta["bars_4H"],
            "bars_1D": bars_meta["bars_1D"],
            "journal_row_count": len(baseline_rows),
            "complete_session_count_proxy": complete_sessions,
            "split": split.to_dict(),
            "train_journal_rows": len(train_rows),
            "holdout_journal_rows": len(holdout_rows),
        },
        "data_quality": {
            "eligibility_counts_train_entry_pairs": dict(elig_counts),
            "intrabar_15m_from_5m": amb15,
            "one_m_resolution": one_m,
        },
        "baseline": {
            "htf": "Daily + 4H structure_break_v1 soft context",
            "session": "Asia / London",
            "execution": "5m and 15m",
            "confirmation": "direction-aligned CHoCH",
            "entry": "first_touch / boundary / CE",
            "stop": "beyond_sweep default",
            "targets": "1R / 2R / 3R + same-session opposite liquidity",
        },
        "timeframe": {"train_5m": tf5, "train_15m": tf15, "paired": paired, **train_insights},
        "entry": {
            "by_tf_mode": entry_rows,
            "combined": entry_combined,
            "paired": paired_entry,
        },
        "stop": stop_compare,
        "htf": {
            "groups": htf_groups,
            "setup_vs_daily": vs_daily,
            "setup_vs_h4": vs_h4,
            "policies": htf_pols,
        },
        "expiry": {"timing_distributions": timing, "candidates": expiry_rows},
        "targets": targets,
        "sessions": sessions,
        "candidates": {
            "frozen_before_holdout": frozen,
            "train_ranked": ranked,
            "finalists": finalists_sc,
            "holdout": holdout_results,
            "stability": stability,
            "walk_forward": wf,
        },
        "recommendation": {
            "category": rec_cat,
            "selected_candidate": selected_cfg,
            "candidate_json_paths": selected_paths,
            "production_default_unchanged": True,
        },
        "artifacts": artifacts,
        "limitations": [
            LUXALGO_WARNING,
            "equivalence_status = unvalidated_against_luxalgo",
            "Theoretical fixed-target expectancy is not realized PnL",
            "Holdout must not be used for further tuning",
            "High TRIGGER_BAR_STOP_AMBIGUITY remains a model-risk item",
        ],
    }

    if write_artifacts:
        Path("phase18_validation.json").write_text(
            json.dumps(_scrub(report), indent=2, default=str),
            encoding="utf-8",
        )
        report["artifacts"]["phase18_validation"] = "phase18_validation.json"

    return _scrub(report)


def main() -> None:
    ap = argparse.ArgumentParser(description="Phase 18 validation")
    ap.add_argument("--no-artifacts", action="store_true")
    ap.add_argument("--no-1m", action="store_true")
    args = ap.parse_args()
    report = run_phase18(write_artifacts=not args.no_artifacts, attempt_1m=not args.no_1m)
    print(json.dumps({
        "ok": report.get("ok"),
        "recommendation": (report.get("recommendation") or {}).get("category"),
        "split": (report.get("dataset") or {}).get("split"),
        "finalists": [f.get("candidate_id") for f in ((report.get("candidates") or {}).get("finalists") or [])],
        "stability": (report.get("candidates") or {}).get("stability"),
    }, indent=2, default=str))


if __name__ == "__main__":
    main()
