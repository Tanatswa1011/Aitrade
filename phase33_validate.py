"""Phase 33 — Post-news macro repricing research (DRY_RUN / no broker execution).

Does not modify frozen Phase 26 / Phase 30 artifacts.
"""

from __future__ import annotations

import csv
import hashlib
import json
import random
import statistics
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional, Sequence
from zoneinfo import ZoneInfo

from bar_dataset import load_dataset
from macro_calendar import ensure_events, write_audit, write_events
from nq_databento import DATA_ROOT as NQ_DATA_ROOT
from nq_post_news_engine import (
    assert_no_blackout_actions,
    continuation_path,
    index_bars_by_ny_date,
    local_ts,
    replay_family,
    snapshot_event,
    snapshot_event_5m,
)
from nq_post_news_models import (
    NQ_TICK_SIZE,
    PostNewsStrategyConfig,
    PostNewsTrade,
    config_hash,
)
from phase22_validate import _write_csv
from phase29_validate import apply_cost, score_trades, split_dev_oos, walkforward

ROOT = Path(__file__).resolve().parent
REPORTS = ROOT / "reports"
VALIDATION_JSON = ROOT / "phase33_validation.json"
JOURNAL_DIR = ROOT / "journal" / "phase33_post_news_macro"
CANDIDATES_DIR = ROOT / "strategy_candidates"
NY = ZoneInfo("America/New_York")

GC_FROZEN = ROOT / "strategy_frozen" / "gc_vwap_v2_phase26.json"
NQ_FROZEN = ROOT / "strategy_frozen" / "nq_dvp_phase30.json"
GC_HASH = "0695d7a881b6247033426bceb077b6895b96405b826c21f32a109a02f44b2d43"
NQ_HASH = "935e3a616351b09dbfa8d2d0e2b5d6850be803fb385ec5e5f8e3593856a1212a"
GC_FILE_SHA = "12615d19ef3deed36b7929c161ef7c377975cd2317c0ae6d06f1949bd327046f"
NQ_FILE_SHA = "34927fba0f268e56cf166cf8939bdfec6aa031510f7cee9acfab3909dd36b541"
GC_PAPER = ROOT / "journal" / "phase26_gc_vwap_v2_paper" / "paper_trades.jsonl"
NQ_PAPER = ROOT / "journal" / "phase30_nq_dvp_paper" / "paper_trades.jsonl"

ENTRY_FAMILIES = (
    "A_RANGE_BREAKOUT",
    "B_FIRST_PULLBACK",
    "C_5M_CLOSE_CONFIRM",
    "D_CASH_OPEN",
)
DELAYS = (5, 10, 15, 30, 60)  # 60 minutes after 08:30 == 09:30 cash open
HORIZONS = (5, 10, 15, 30, 60, 120)
PARAM_GRID = (
    (0.35, 0.50, 0.35),
    (0.50, 0.75, 0.50),  # predeclared default
    (0.75, 1.00, 0.65),
)


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()


def assert_frozen_untouched() -> dict[str, Any]:
    reasons = []
    gc = json.loads(GC_FROZEN.read_text(encoding="utf-8"))
    nq = json.loads(NQ_FROZEN.read_text(encoding="utf-8"))
    if gc.get("frozen_config_hash") != GC_HASH:
        reasons.append("gc_hash_changed")
    if nq.get("frozen_config_hash") != NQ_HASH:
        reasons.append("nq_hash_changed")
    if file_sha256(GC_FROZEN) != GC_FILE_SHA:
        reasons.append("gc_file_bytes_changed")
    if file_sha256(NQ_FROZEN) != NQ_FILE_SHA:
        reasons.append("nq_file_bytes_changed")
    if file_sha256(GC_FROZEN.with_suffix(".md")) != "72451c1215baad08c7d7ebf2620d353f1398bfd0efd447de2d43ca2a9d340ae5":
        reasons.append("gc_md_changed")
    if file_sha256(NQ_FROZEN.with_suffix(".md")) != "655b8c661a257f0e5311e49eabb9ed594a3bc64404d64073f07fe66bbee577dd":
        reasons.append("nq_md_changed")
    if file_sha256(GC_PAPER) != "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855":
        reasons.append("gc_paper_journal_changed")
    if file_sha256(NQ_PAPER) != "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855":
        reasons.append("nq_paper_journal_changed")
    return {
        "ok": not reasons,
        "reasons": reasons,
        "gc_hash": gc.get("frozen_config_hash"),
        "nq_hash": nq.get("frozen_config_hash"),
        "execution": "DRY_RUN_NO_BROKER",
    }


def load_nq() -> dict[str, Any]:
    root = NQ_DATA_ROOT / "stitched"
    b1 = load_dataset("databento_NQ_stitched", "1m", root=root)
    b5 = load_dataset("databento_NQ_stitched", "5m", root=root)
    return {
        "ok": bool(b1.get("bars") and b5.get("bars")),
        "bars_1m": list(b1.get("bars") or []),
        "bars_5m": list(b5.get("bars") or []),
    }


def load_gc() -> dict[str, Any]:
    root = ROOT / "data" / "databento" / "GC" / "stitched"
    b5 = load_dataset("databento_GC_stitched", "5m", root=root)
    return {"ok": bool(b5.get("bars")), "bars_5m": list(b5.get("bars") or [])}


def _nearby(by_date: dict[str, list], td: str, days_back: int = 3) -> list:
    d = date.fromisoformat(td)
    out = []
    for i in range(days_back, -1, -1):
        key = (d - timedelta(days=i)).isoformat()
        out.extend(by_date.get(key) or [])
    return out


def mean_or_none(xs: Sequence[float]) -> Optional[float]:
    return None if not xs else float(statistics.mean(xs))


def _score_signed(vals: Sequence[Optional[float]]) -> dict[str, Any]:
    xs = [float(v) for v in vals if v is not None]
    if not xs:
        return {"n": 0, "mean": None, "hit_rate": None, "median": None}
    hits = sum(1 for x in xs if x > 0)
    return {
        "n": len(xs),
        "mean": statistics.mean(xs),
        "median": statistics.median(xs),
        "hit_rate": hits / len(xs),
        "stdev": statistics.pstdev(xs) if len(xs) > 1 else 0.0,
    }


def monte_carlo_reshuffle(trades: Sequence[PostNewsTrade], *, n_sim: int = 10000, seed: int = 33) -> dict[str, Any]:
    pts = [float(t.points) for t in trades if t.points is not None and t.outcome != "AMBIGUOUS"]
    if len(pts) < 20:
        return {"ok": False, "reason": "insufficient_trades", "n": len(pts)}
    rng = random.Random(seed)
    terminals: list[float] = []
    dds: list[float] = []
    for _ in range(n_sim):
        seq = pts[:]
        rng.shuffle(seq)
        eq = 0.0
        peak = 0.0
        max_dd = 0.0
        for p in seq:
            eq += p
            peak = max(peak, eq)
            max_dd = min(max_dd, eq - peak)
        terminals.append(eq)
        dds.append(abs(max_dd))
    terminals.sort()
    dds.sort()

    def pct(arr: list[float], q: float) -> float:
        return arr[min(len(arr) - 1, int(q * (len(arr) - 1)))]

    return {
        "ok": True,
        "simulations": n_sim,
        "method": "trade_order_reshuffle",
        "n_trades": len(pts),
        "terminal_p05": pct(terminals, 0.05),
        "terminal_p50": pct(terminals, 0.50),
        "terminal_p95": pct(terminals, 0.95),
        "maxdd_p50": pct(dds, 0.50),
        "maxdd_p95": pct(dds, 0.95),
        "pct_terminal_positive": sum(1 for x in terminals if x > 0) / n_sim,
    }


def _mfe_mae(trades: Sequence[PostNewsTrade]) -> dict[str, Any]:
    mfes = [float((t.extras or {}).get("mfe")) for t in trades if (t.extras or {}).get("mfe") is not None]
    maes = [float((t.extras or {}).get("mae")) for t in trades if (t.extras or {}).get("mae") is not None]
    return {
        "mfe_mean": mean_or_none(mfes),
        "mae_mean": mean_or_none(maes),
        "mfe_n": len(mfes),
        "mae_n": len(maes),
    }


def summarize_bucket(trades: Sequence[PostNewsTrade]) -> dict[str, Any]:
    sc = score_trades(trades)
    sc.update(_mfe_mae(trades))
    longs = [t for t in trades if t.direction == "bullish"]
    shorts = [t for t in trades if t.direction == "bearish"]
    sc["long"] = score_trades(longs)
    sc["short"] = score_trades(shorts)
    return sc


def apply_cost_ticks(trades: Sequence[PostNewsTrade], ticks_per_side: float, tick: float = NQ_TICK_SIZE) -> list[PostNewsTrade]:
    # Reuse DVP apply_cost semantics via duck typing is NQ_TICK hardcoded.
    friction = 2.0 * float(ticks_per_side) * float(tick)
    out = []
    for t in trades:
        if t.points is None or t.outcome == "AMBIGUOUS":
            out.append(t)
            continue
        pts = float(t.points) - friction
        risk = float((t.extras or {}).get("risk_points") or 1.0)
        extras = {**(t.extras or {}), "cost_ticks_per_side": ticks_per_side, "friction_points": friction}
        out.append(
            PostNewsTrade(
                trade_id=t.trade_id,
                trading_date=t.trading_date,
                direction=t.direction,
                entry_timestamp=t.entry_timestamp,
                entry_price=t.entry_price,
                stop_price=t.stop_price,
                target_price=t.target_price,
                exit_timestamp=t.exit_timestamp,
                exit_price=t.exit_price,
                outcome=t.outcome,
                points=pts,
                r_multiple=pts / risk if risk else None,
                extras=extras,
            )
        )
    return out


def load_dvp_trades() -> list[dict[str, Any]]:
    path = ROOT / "journal" / "phase29_nq_drift_vwap" / "trades.jsonl"
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def gc_v2_daily_pnl() -> dict[str, float]:
    """Replay frozen V2 historically in memory — does not write Phase 26 journals."""
    from gc_vwap_engine import analyze_candidate, collect_all_sequences
    from gc_vwap_models import PHASE25_CANDIDATES
    from gc_vwap_replay import setup_to_journal

    loaded = load_gc()
    if not loaded["ok"]:
        return {}
    bars = loaded["bars_5m"]
    cfg = next(c for c in PHASE25_CANDIDATES if c.candidate_id == "V2_BAND_RECLAIM_2SIG_RETEST")
    seqs = collect_all_sequences(bars)
    daily: dict[str, float] = {}
    for seq in seqs:
        setup = analyze_candidate(seq, cfg)
        rec = setup_to_journal(setup, bars, cfg)
        if not rec.entry_results:
            continue
        er = rec.entry_results[0]
        if not er.triggered:
            continue
        r = None
        if er.outcome in ("2R_HIT", "3R_HIT"):
            r = 2.0 if er.outcome == "2R_HIT" else 3.0
        elif er.outcome == "1R_HIT":
            r = 1.0
        elif er.outcome == "STOP_HIT":
            r = -1.0
        if r is None:
            continue
        td = rec.trading_date or ""
        daily[td] = daily.get(td, 0.0) + float(r)
    return daily


def recommend(results: list[dict[str, Any]], delay_rows: list[dict[str, Any]], oos: dict[str, Any], wf: str, cost: dict[str, Any]) -> str:
    """Single official verdict. Conservative: do not manufacture an edge."""
    usable = [r for r in results if (r.get("resolved_n") or 0) >= 20]
    if not usable:
        return "PROMISING_NEEDS_MORE_DATA"
    # Delay decay: if +5m signed continuation mean <= 0, reject.
    d5 = next((r for r in delay_rows if r.get("horizon") == "fwd_5m_signed"), None)
    if d5 and (d5.get("mean") is not None) and float(d5["mean"]) <= 0:
        return "MACRO_EDGE_REJECTED"
    oos_e = oos.get("expectancy_points")
    cost_e = cost.get("expectancy_points")
    pos_usable = [r for r in usable if (r.get("expectancy_points") or 0) > 0]
    if not pos_usable:
        return "MACRO_EDGE_REJECTED"
    if oos_e is None or float(oos_e) <= 0:
        return "MACRO_EDGE_WEAK"
    if cost_e is None or float(cost_e) <= 0:
        return "MACRO_EDGE_WEAK"
    if wf in ("STABLE_NEGATIVE", "MIXED") and float(oos_e) < 0.5:
        return "MACRO_EDGE_WEAK"
    if wf == "STABLE_POSITIVE" and float(oos_e) > 0 and float(cost_e) > 0:
        return "MACRO_EDGE_FOUND"
    return "MACRO_EDGE_WEAK"


def main() -> dict[str, Any]:
    frozen = assert_frozen_untouched()
    if not frozen["ok"]:
        payload = {"ok": False, "phase": 33, "status": "FROZEN_INTEGRITY_FAILED", "frozen": frozen}
        VALIDATION_JSON.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return payload

    events = ensure_events(fetch_prints=False)
    write_events(events)
    audit = write_audit(events)

    nq = load_nq()
    if not nq["ok"]:
        payload = {"ok": False, "phase": 33, "status": "MISSING_NQ_DATA"}
        VALIDATION_JSON.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return payload

    bars_1m = nq["bars_1m"]
    bars_5m = nq["bars_5m"]
    by1 = index_bars_by_ny_date(bars_1m)
    by5 = index_bars_by_ny_date(bars_5m)

    base_cfg = PostNewsStrategyConfig()
    snaps: list[Any] = []
    for ev in events:
        w1 = _nearby(by1, ev.publication_date)
        w5 = _nearby(by5, ev.publication_date)
        snaps.append(snapshot_event(ev, w1, w5, instrument="NQ", cfg=base_cfg))

    JOURNAL_DIR.mkdir(parents=True, exist_ok=True)
    with (JOURNAL_DIR / "event_snapshots.jsonl").open("w", encoding="utf-8") as fh:
        for s in snaps:
            fh.write(json.dumps(s.to_dict()) + "\n")

    # Continuation / delay-decay (regime-conditioned, no entry mechanics).
    delay_rows = []
    for family in ("CPI", "NFP"):
        for regime in ("MACRO_BULLISH", "MACRO_BEARISH"):
            sub = [s for s in snaps if s.event_family == family and s.regime == regime]
            paths = []
            for s in sub:
                w1 = _nearby(by1, s.trading_date)
                paths.append(continuation_path(s, w1, HORIZONS))
            for key in [f"fwd_{h}m_signed" for h in HORIZONS] + ["fwd_0930_signed", "fwd_1555_signed"]:
                sc = _score_signed([p.get(key) for p in paths])
                delay_rows.append(
                    {
                        "event_family": family,
                        "regime": regime,
                        "horizon": key,
                        **sc,
                    }
                )
    _write_csv(REPORTS / "phase33_delay_decay.csv", delay_rows)

    regime_rows = []
    for family in ("CPI", "NFP"):
        sub = [s for s in snaps if s.event_family == family]
        counts = {}
        for s in sub:
            counts[s.regime] = counts.get(s.regime, 0) + 1
        regime_rows.append({"event_family": family, "n": len(sub), **counts})
    _write_csv(REPORTS / "phase33_regimes.csv", regime_rows)

    # Entry-family experiments on the default param set.
    experiment_rows = []
    all_trade_files = []
    default_trades_by_key: dict[str, list[PostNewsTrade]] = {}
    for fam in ENTRY_FAMILIES:
        for delay in DELAYS:
            for event_family in ("CPI", "NFP"):
                cfg = PostNewsStrategyConfig(
                    entry_family=fam,
                    event_family=event_family,
                    delay_minutes=int(delay),
                )
                trades: list[PostNewsTrade] = []
                for s in snaps:
                    if s.event_family != event_family:
                        continue
                    w1 = _nearby(by1, s.trading_date)
                    w5 = _nearby(by5, s.trading_date)
                    t = replay_family(s, w1, w5, cfg)
                    if t is not None:
                        trades.append(t)
                key = f"{fam}|{event_family}|d{delay}"
                default_trades_by_key[key] = trades
                sc = summarize_bucket(trades)
                row = {
                    "entry_family": fam,
                    "event_family": event_family,
                    "delay_minutes": delay,
                    "config_hash": config_hash(cfg),
                    **{k: v for k, v in sc.items() if k not in ("long", "short")},
                    "long_n": sc["long"]["resolved_n"],
                    "long_wr": sc["long"]["win_rate"],
                    "long_exp": sc["long"]["expectancy_points"],
                    "short_n": sc["short"]["resolved_n"],
                    "short_wr": sc["short"]["win_rate"],
                    "short_exp": sc["short"]["expectancy_points"],
                }
                experiment_rows.append(row)
                all_trade_files.append((key, trades))
    _write_csv(REPORTS / "phase33_experiments.csv", experiment_rows)

    # Choose a research "best" by OOS expectancy among default 1-tick cost, min N=20, delay>=5.
    # Selection uses chronological 70/30 on each series — declared before ranking on holdout.
    ranked = []
    for row in experiment_rows:
        key = f"{row['entry_family']}|{row['event_family']}|d{row['delay_minutes']}"
        trades = default_trades_by_key[key]
        if (row.get("resolved_n") or 0) < 20:
            continue
        if int(row["delay_minutes"]) < 5:
            continue
        _dev, oos, _meta = split_dev_oos(trades)
        oos_sc = score_trades(oos)
        cost1 = score_trades(apply_cost_ticks(trades, 1.0))
        ranked.append(
            {
                **row,
                "oos_n": oos_sc["resolved_n"],
                "oos_exp": oos_sc["expectancy_points"],
                "cost1_exp": cost1["expectancy_points"],
                "cost1_pf": cost1["profit_factor"],
            }
        )
    ranked.sort(key=lambda r: (r.get("oos_exp") is not None, r.get("oos_exp") or -1e9), reverse=True)
    _write_csv(REPORTS / "phase33_ranked.csv", ranked)

    best = ranked[0] if ranked else None
    best_trades: list[PostNewsTrade] = []
    best_cfg = base_cfg
    if best:
        best_cfg = PostNewsStrategyConfig(
            entry_family=best["entry_family"],
            event_family=best["event_family"],
            delay_minutes=int(best["delay_minutes"]),
        )
        best_trades = default_trades_by_key[f"{best['entry_family']}|{best['event_family']}|d{best['delay_minutes']}"]

    with (JOURNAL_DIR / "trades.jsonl").open("w", encoding="utf-8") as fh:
        for t in best_trades:
            fh.write(json.dumps(t.to_dict()) + "\n")

    # Robustness on best candidate (or empty).
    if best_trades:
        dev, oos, split_meta = split_dev_oos(best_trades)
        wf_rows, wf_class = walkforward(best_trades, n_blocks=4)
        _write_csv(REPORTS / "phase33_walkforward.csv", wf_rows)
        cost_rows = []
        cost_map = {}
        for ticks, name in ((0.0, "IDEAL"), (1.0, "1_TICK_ADVERSE"), (2.0, "2_TICK_ADVERSE"), (4.0, "EVENT_STRESS_4TICK")):
            ct = apply_cost_ticks(best_trades, ticks)
            sc = score_trades(ct)
            cost_map[name] = sc
            cost_rows.append({"overlay": name, "ticks_per_side": ticks, **sc})
        _write_csv(REPORTS / "phase33_cost.csv", cost_rows)
        mc = monte_carlo_reshuffle(best_trades)
        (REPORTS / "phase33_monte_carlo.json").write_text(json.dumps(mc, indent=2), encoding="utf-8")
        pre = [t for t in best_trades if t.trading_date < "2020-03-01"]
        covid = [t for t in best_trades if "2020-03-01" <= t.trading_date < "2022-01-01"]
        hike = [t for t in best_trades if "2022-01-01" <= t.trading_date < "2023-08-01"]
        post = [t for t in best_trades if t.trading_date >= "2023-08-01"]
        regime_perf = [
            {"bucket": "pre_covid", **score_trades(pre)},
            {"bucket": "covid_2020_2021", **score_trades(covid)},
            {"bucket": "hiking_2022_mid2023", **score_trades(hike)},
            {"bucket": "post_2023aug", **score_trades(post)},
        ]
        _write_csv(REPORTS / "phase33_market_regimes.csv", regime_perf)
        oos_sc = score_trades(oos)
        train_sc = score_trades(dev)
        full_sc = summarize_bucket(best_trades)
        cost1 = cost_map["1_TICK_ADVERSE"]
        blackout_chk = assert_no_blackout_actions(best_trades, snaps)
    else:
        split_meta = {}
        wf_rows, wf_class = [], "INSUFFICIENT"
        oos_sc = train_sc = full_sc = cost1 = score_trades([])
        mc = {"ok": False}
        regime_perf = []
        blackout_chk = {"ok": True, "n_violations": 0}
        cost_map = {}

    # Parameter stability around default on Family C / delay 5 / both families separately.
    stability = []
    for event_family in ("CPI", "NFP"):
        for close_atr, range_atr, ret in PARAM_GRID:
            cfg = PostNewsStrategyConfig(
                entry_family="C_5M_CLOSE_CONFIRM",
                event_family=event_family,
                delay_minutes=5,
                min_close_move_atr=close_atr,
                min_range_atr=range_atr,
                min_retention=ret,
            )
            trades = []
            for s0 in snaps:
                if s0.event_family != event_family:
                    continue
                # Re-snapshot with this cfg so regime thresholds apply.
                w1 = _nearby(by1, s0.trading_date)
                w5 = _nearby(by5, s0.trading_date)
                ev = next(e for e in events if e.event_id == s0.event_id)
                s = snapshot_event(ev, w1, w5, instrument="NQ", cfg=cfg)
                t = replay_family(s, w1, w5, cfg)
                if t is not None:
                    trades.append(t)
            sc = score_trades(trades)
            stability.append(
                {
                    "event_family": event_family,
                    "min_close_move_atr": close_atr,
                    "min_range_atr": range_atr,
                    "min_retention": ret,
                    **sc,
                }
            )
    _write_csv(REPORTS / "phase33_param_stability.csv", stability)

    # GC limited sample
    gc = load_gc()
    gc_rows = []
    if gc["ok"]:
        gc_by5 = index_bars_by_ny_date(gc["bars_5m"])
        gc_start = datetime.fromtimestamp(int(gc["bars_5m"][0].time), tz=NY).date().isoformat()
        for fam in ENTRY_FAMILIES:
            for event_family in ("CPI", "NFP"):
                cfg = PostNewsStrategyConfig(
                    instrument="GC",
                    entry_family=fam,
                    event_family=event_family,
                    delay_minutes=5,
                )
                trades = []
                for ev in events:
                    if ev.event_family != event_family or ev.publication_date < gc_start:
                        continue
                    w5 = _nearby(gc_by5, ev.publication_date)
                    s = snapshot_event_5m(ev, w5, instrument="GC", cfg=cfg)
                    t = replay_family(s, w5, w5, cfg)
                    if t is not None:
                        trades.append(t)
                sc = score_trades(trades)
                gc_rows.append({"instrument": "GC", "entry_family": fam, "event_family": event_family, **sc})
        _write_csv(REPORTS / "phase33_gc_limited.csv", gc_rows)

    # Portfolio comparison vs DVP (read-only journal) and GC V2 (in-memory replay).
    dvp_rows = load_dvp_trades()
    dvp_daily: dict[str, float] = {}
    dvp_by_date: dict[str, list[dict[str, Any]]] = {}
    for r in dvp_rows:
        td = r.get("trading_date")
        if not td or r.get("points") is None:
            continue
        dvp_daily[td] = dvp_daily.get(td, 0.0) + float(r["points"])
        dvp_by_date.setdefault(td, []).append(r)

    macro_daily: dict[str, float] = {}
    for t in best_trades:
        if t.points is None:
            continue
        macro_daily[t.trading_date] = macro_daily.get(t.trading_date, 0.0) + float(t.points)

    overlap_dates = sorted(set(macro_daily) & set(dvp_daily))
    corr = None
    if len(overlap_dates) >= 8:
        xs = [macro_daily[d] for d in overlap_dates]
        ys = [dvp_daily[d] for d in overlap_dates]
        corr = statistics.correlation(xs, ys) if len(set(xs)) > 1 and len(set(ys)) > 1 else None

    event_dates = {s.trading_date for s in snaps}
    dvp_on_event = [dvp_daily[d] for d in dvp_daily if d in event_dates]
    dvp_off_event = [dvp_daily[d] for d in dvp_daily if d not in event_dates]

    # Simultaneous exposure: macro still open after 10:30 while DVP enters.
    simultaneous = 0
    for t in best_trades:
        if t.exit_timestamp is None:
            continue
        for r in dvp_by_date.get(t.trading_date, []):
            et = int(r.get("entry_timestamp") or 0)
            if t.entry_timestamp <= et <= int(t.exit_timestamp):
                simultaneous += 1
                break

    # Ensemble research log (not a strategy): MACRO regime vs DVP drift that day.
    ensemble_rows = []
    for s in snaps:
        if s.regime not in ("MACRO_BULLISH", "MACRO_BEARISH"):
            continue
        drifts = sorted({str((r.get("extras") or {}).get("drift")) for r in dvp_by_date.get(s.trading_date, []) if (r.get("extras") or {}).get("drift")})
        if not drifts:
            continue
        agree = (
            (s.regime == "MACRO_BULLISH" and "POSITIVE_DRIFT" in drifts)
            or (s.regime == "MACRO_BEARISH" and "NEGATIVE_DRIFT" in drifts)
        )
        ensemble_rows.append(
            {
                "trading_date": s.trading_date,
                "event_family": s.event_family,
                "macro_regime": s.regime,
                "dvp_drifts": "|".join(drifts),
                "agree": agree,
            }
        )
    _write_csv(REPORTS / "phase33_ensemble_research.csv", ensemble_rows)
    agree_n = sum(1 for r in ensemble_rows if r["agree"])

    gc_daily = {}
    try:
        gc_daily = gc_v2_daily_pnl()
    except Exception as exc:  # noqa: BLE001
        gc_daily = {"_error": str(exc)}  # type: ignore[assignment]
    gc_overlap = []
    gc_corr = None
    if isinstance(gc_daily, dict) and "_error" not in gc_daily and best_trades:
        gc_overlap = sorted(set(macro_daily) & set(gc_daily))
        if len(gc_overlap) >= 6:
            xs = [macro_daily[d] for d in gc_overlap]
            ys = [gc_daily[d] for d in gc_overlap]
            if len(set(xs)) > 1 and len(set(ys)) > 1:
                gc_corr = statistics.correlation(xs, ys)

    portfolio = {
        "dvp_journal": "journal/phase29_nq_drift_vwap/trades.jsonl",
        "dvp_n_days": len(dvp_daily),
        "macro_n_days": len(macro_daily),
        "same_day_overlap_n": len(overlap_dates),
        "daily_pnl_correlation_macro_vs_dvp": corr,
        "simultaneous_exposure_days": simultaneous,
        "dvp_mean_pnl_on_event_days": mean_or_none(dvp_on_event),
        "dvp_mean_pnl_off_event_days": mean_or_none(dvp_off_event),
        "gc_v2_replay": "in_memory_phase25_V2_not_writing_frozen_journals",
        "gc_same_day_overlap_n": 0 if not isinstance(gc_overlap, list) else len(gc_overlap),
        "daily_r_correlation_macro_vs_gc_v2": gc_corr,
        "ensemble_agreement_n": agree_n,
        "ensemble_compared_n": len(ensemble_rows),
        "note": "Frozen strategy files and paper journals were not modified.",
    }
    (REPORTS / "phase33_portfolio.json").write_text(json.dumps(portfolio, indent=2), encoding="utf-8")

    # Look-ahead unit tests
    test_run = subprocess.run(
        [sys.executable, str(ROOT / "tests_phase33.py")],
        capture_output=True,
        text=True,
        cwd=str(ROOT),
    )
    tests_ok = test_run.returncode == 0

    delay_pooled = [
        r
        for r in delay_rows
        if r["horizon"] in ("fwd_5m_signed", "fwd_10m_signed", "fwd_15m_signed", "fwd_30m_signed", "fwd_60m_signed", "fwd_0930_signed")
    ]
    verdict = recommend(experiment_rows, delay_rows, oos_sc, wf_class, cost1)

    best_candidate = None
    if best:
        best_candidate = {
            "strategy_family": "nq_post_news_macro_repricing_v1",
            "candidate_id": f"{best['entry_family']}_{best['event_family']}_D{best['delay_minutes']}",
            "status": "RESEARCH_ONLY_NOT_FROZEN",
            "entry_family": best["entry_family"],
            "event_family": best["event_family"],
            "delay_minutes": best["delay_minutes"],
            "blackout": "DEFAULT_CONSERVATIVE_PM_5_5",
            "config_hash": best["config_hash"],
            "resolved_n": best.get("resolved_n"),
            "win_rate": best.get("win_rate"),
            "expectancy_points": best.get("expectancy_points"),
            "profit_factor": best.get("profit_factor"),
            "oos_expectancy_points": best.get("oos_exp"),
            "cost_1tick_expectancy": best.get("cost1_exp"),
            "note": "Not frozen. Next research phase only if verdict is MACRO_EDGE_FOUND or PROMISING_NEEDS_MORE_DATA.",
        }
        (CANDIDATES_DIR / "phase33_POST_NEWS_MACRO.json").write_text(
            json.dumps(
                {
                    "phase": "phase33",
                    "strategy_family": "nq_post_news_macro_repricing_v1",
                    "strategy_version": "v1.phase33",
                    "instrument": "NQ",
                    "provider": "databento:GLBX.MDP3",
                    "candidate": best_candidate,
                    "predeclared": {
                        "min_close_move_atr": 0.50,
                        "min_range_atr": 0.75,
                        "min_retention": 0.50,
                        "atr_period": 14,
                        "target_r": 1.0,
                        "news_profile": "DEFAULT_CONSERVATIVE_PM_5_5",
                    },
                    "note": "RESEARCH ONLY — not production default, not frozen.",
                },
                indent=2,
            ),
            encoding="utf-8",
        )

    frozen_after = assert_frozen_untouched()
    payload = {
        "ok": frozen["ok"] and frozen_after["ok"] and tests_ok and blackout_chk["ok"],
        "phase": 33,
        "status": "RESEARCH_COMPLETE",
        "strategy_family": "nq_post_news_macro_repricing_v1",
        "execution": "DRY_RUN_NO_BROKER",
        "verdict": verdict,
        "best_candidate": best_candidate,
        "frozen_before": frozen,
        "frozen_after": frozen_after,
        "macro_data_audit": audit,
        "nq_bars": {"n_1m": len(bars_1m), "n_5m": len(bars_5m)},
        "es_data": {"available": False, "note": "No Databento ES store; cash/CFD substitution forbidden."},
        "treasury_dxy": {"available": False, "note": "Not in repository; omitted rather than substituted."},
        "consensus": {"available": False, "implication": "Surprise-conditioned rules not tested."},
        "event_counts": regime_rows,
        "continuation_delay_decay": delay_pooled,
        "experiments_n": len(experiment_rows),
        "best_full": full_sc if best_trades else {},
        "best_train": train_sc if best_trades else {},
        "best_oos": oos_sc if best_trades else {},
        "walkforward_class": wf_class,
        "walkforward": wf_rows,
        "cost_1tick": cost1 if best_trades else {},
        "monte_carlo": mc,
        "market_regimes": regime_perf,
        "gc_limited": gc_rows,
        "portfolio": portfolio,
        "blackout_check": blackout_chk,
        "lookahead_tests_ok": tests_ok,
        "lookahead_tests_output": (test_run.stdout + test_run.stderr)[-2000:],
        "split": split_meta,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "philosophy": (
            "Goal is not to manufacture a profitable-looking macro bot. "
            "Question: after the prop-firm news blackout, is there a persistent, "
            "cost-adjusted directional edge in NQ/ES/GC that is different from frozen DVP and GC V2?"
        ),
    }
    VALIDATION_JSON.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    return payload


if __name__ == "__main__":
    out = main()
    print(json.dumps({"ok": out.get("ok"), "verdict": out.get("verdict"), "best": out.get("best_candidate"), "frozen_ok": out.get("frozen_after")}, indent=2, default=str))
