"""Phase 29 validation — independent DVP_ORIGINAL replication on NQ."""

from __future__ import annotations

import json
import random
import statistics
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, Sequence
from zoneinfo import ZoneInfo

from bar_dataset import load_dataset
from nq_databento import DATA_ROOT, fetch_and_stitch_nq
from nq_drift_vwap_engine import config_hash, replay_all_days, trading_dates_ny
from nq_drift_vwap_models import (
    DVP_ORIGINAL,
    FORCE_CLOSE_LOCAL,
    NO_NEW_TRADES_AFTER_LOCAL,
    SOURCE_CLAIMED_WIN_RATE,
    STRATEGY_FAMILY,
    STRATEGY_VERSION,
    TRADE_START_LOCAL,
    VWAP_BASIS_STATUS,
    VWAP_PRICE_BASIS,
    VWAP_RESET_LOCAL,
    DVPTrade,
)
from phase22_validate import _write_csv

REPORTS = Path("reports")
VALIDATION_JSON = Path("phase29_validation.json")
JOURNAL_DIR = Path("journal") / "phase29_nq_drift_vwap"
PHASE26_FROZEN = Path("strategy_frozen") / "gc_vwap_v2_phase26.json"
PHASE26_HASH = "0695d7a881b6247033426bceb077b6895b96405b826c21f32a109a02f44b2d43"
PHASE26_PAPER = Path("journal") / "phase26_gc_vwap_v2_paper" / "paper_trades.jsonl"
NQ_TICK = 0.25
NY = ZoneInfo("America/New_York")


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
    return {"ok": ok, "reasons": reasons, "expected_hash": PHASE26_HASH}


def load_nq_bars() -> dict[str, Any]:
    root = DATA_ROOT / "stitched"
    b1 = load_dataset("databento_NQ_stitched", "1m", root=root)
    b5 = load_dataset("databento_NQ_stitched", "5m", root=root)
    b15 = load_dataset("databento_NQ_stitched", "15m", root=root)
    return {
        "ok": bool(b1.get("bars") and b5.get("bars") and b15.get("bars")),
        "bars_1m": list(b1.get("bars") or []),
        "bars_5m": list(b5.get("bars") or []),
        "bars_15m": list(b15.get("bars") or []),
    }


def score_trades(trades: Sequence[DVPTrade]) -> dict[str, Any]:
    resolved = [t for t in trades if t.outcome in ("TARGET_HIT", "STOP_HIT", "FORCE_CLOSE")]
    amb = [t for t in trades if t.outcome == "AMBIGUOUS"]
    wins = [t for t in resolved if t.points is not None and float(t.points) > 0]
    losses = [t for t in resolved if t.points is not None and float(t.points) <= 0]
    timed = [t for t in resolved if t.outcome == "FORCE_CLOSE"]
    pts = [float(t.points) for t in resolved if t.points is not None]
    rs = [float(t.r_multiple) for t in resolved if t.r_multiple is not None]
    win_pts = [float(t.points) for t in wins if t.points is not None]
    loss_pts = [abs(float(t.points)) for t in losses if t.points is not None]

    # equity curve in points
    equity = 0.0
    peak = 0.0
    max_dd = 0.0
    streak = max_streak = 0
    for t in resolved:
        if t.points is None:
            continue
        p = float(t.points)
        equity += p
        peak = max(peak, equity)
        max_dd = min(max_dd, equity - peak)
        if p <= 0:
            streak += 1
            max_streak = max(max_streak, streak)
        else:
            streak = 0

    days = sorted({t.trading_date for t in resolved})
    day_pnl: dict[str, float] = {}
    for t in resolved:
        if t.points is None:
            continue
        day_pnl[t.trading_date] = day_pnl.get(t.trading_date, 0.0) + float(t.points)
    profitable_days = sum(1 for v in day_pnl.values() if v > 0)

    gross_win = sum(win_pts)
    gross_loss = sum(loss_pts)
    pf = None if gross_loss <= 0 else gross_win / gross_loss

    long_r = [t for t in resolved if t.direction == "bullish"]
    short_r = [t for t in resolved if t.direction == "bearish"]

    def _side(rows):
        if not rows:
            return {"n": 0, "win_rate": None, "expectancy_points": None}
        w = sum(1 for t in rows if t.points is not None and float(t.points) > 0)
        pp = [float(t.points) for t in rows if t.points is not None]
        return {
            "n": len(rows),
            "win_rate": w / len(rows),
            "expectancy_points": None if not pp else statistics.mean(pp),
        }

    return {
        "trades": len(trades),
        "resolved_n": len(resolved),
        "wins": len(wins),
        "losses": len(losses),
        "timed_exits": len(timed),
        "ambiguous": len(amb),
        "win_rate": None if not resolved else len(wins) / len(resolved),
        "average_win_points": None if not win_pts else statistics.mean(win_pts),
        "average_loss_points": None if not loss_pts else statistics.mean(loss_pts),
        "expectancy_points": None if not pts else statistics.mean(pts),
        "expectancy_r": None if not rs else statistics.mean(rs),
        "profit_factor": pf,
        "max_drawdown_points": abs(max_dd),
        "longest_losing_streak": max_streak,
        "trading_days": len(days),
        "trades_per_day": None if not days else len(resolved) / len(days),
        "pct_profitable_days": None if not day_pnl else profitable_days / len(day_pnl),
        "long": _side(long_r),
        "short": _side(short_r),
    }


def apply_cost(trades: Sequence[DVPTrade], ticks_per_side: float) -> list[DVPTrade]:
    """Round-turn friction in points = 2 * ticks * tick_size, adverse to PnL."""
    friction = 2.0 * float(ticks_per_side) * NQ_TICK
    out = []
    for t in trades:
        if t.points is None or t.outcome == "AMBIGUOUS":
            out.append(t)
            continue
        pts = float(t.points) - friction
        risk = float((t.extras or {}).get("risk_points") or 80.0)
        out.append(
            DVPTrade(
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
                extras={**(t.extras or {}), "cost_ticks_per_side": ticks_per_side, "friction_points": friction},
            )
        )
    return out


def split_dev_oos(trades: Sequence[DVPTrade]) -> tuple[list[DVPTrade], list[DVPTrade], dict]:
    """2020–2024 development / 2025+ OOS when available; else chronological 70/30."""
    years = sorted({int(t.trading_date[:4]) for t in trades if t.trading_date})
    has_2020 = any(y <= 2024 for y in years)
    has_2025 = any(y >= 2025 for y in years)
    if has_2020 and has_2025 and min(years) <= 2024:
        dev = [t for t in trades if t.trading_date < "2025-01-01"]
        oos = [t for t in trades if t.trading_date >= "2025-01-01"]
        meta = {
            "method": "calendar_2020_2024_dev_2025plus_oos",
            "dev_start": None if not dev else min(t.trading_date for t in dev),
            "dev_end": None if not dev else max(t.trading_date for t in dev),
            "oos_start": None if not oos else min(t.trading_date for t in oos),
            "oos_end": None if not oos else max(t.trading_date for t in oos),
        }
        return dev, oos, meta
    # chronological 70/30 by trading date
    dates = sorted({t.trading_date for t in trades})
    cut = max(1, min(len(dates) - 1, int(round(len(dates) * 0.7)))) if dates else 0
    train_d, hold_d = set(dates[:cut]), set(dates[cut:])
    return (
        [t for t in trades if t.trading_date in train_d],
        [t for t in trades if t.trading_date in hold_d],
        {
            "method": "chronological_70_30_fallback",
            "note": "Full 2020+ calendar split unavailable or insufficient year coverage",
            "dev_dates": len(train_d),
            "oos_dates": len(hold_d),
        },
    )


def walkforward(trades: Sequence[DVPTrade], n_blocks: int = 4) -> tuple[list[dict], str]:
    dates = sorted({t.trading_date for t in trades})
    if len(dates) < n_blocks:
        return [], "INSUFFICIENT"
    size = max(1, len(dates) // n_blocks)
    rows = []
    for i in range(n_blocks):
        s = i * size
        e = (i + 1) * size if i < n_blocks - 1 else len(dates)
        dset = set(dates[s:e])
        block = [t for t in trades if t.trading_date in dset]
        sc = score_trades(block)
        rows.append(
            {
                "block": i + 1,
                "resolved_n": sc["resolved_n"],
                "win_rate": sc["win_rate"],
                "expectancy_points": sc["expectancy_points"],
                "profit_factor": sc["profit_factor"],
                "max_dd_points": sc["max_drawdown_points"],
            }
        )
    usable = [r for r in rows if (r.get("resolved_n") or 0) >= 20]
    if len(usable) < 2:
        return rows, "INSUFFICIENT"
    pos = [r for r in usable if (r.get("expectancy_points") or -1) > 0]
    if len(pos) == len(usable):
        return rows, "STABLE_POSITIVE"
    if len(pos) == 0:
        return rows, "STABLE_NEGATIVE"
    return rows, "MIXED"


def time_of_day(trades: Sequence[DVPTrade]) -> list[dict]:
    buckets = {
        "10:30-11:30": (1030, 1130),
        "11:30-12:30": (1130, 1230),
        "12:30-13:30": (1230, 1330),
        "13:30-14:30": (1330, 1430),
        "14:30-15:30": (1430, 1530),
    }
    rows = []
    for name, (a, b) in buckets.items():
        sub = []
        for t in trades:
            dt = datetime.fromtimestamp(int(t.entry_timestamp), tz=NY)
            hm = dt.hour * 100 + dt.minute
            if a <= hm < b:
                sub.append(t)
        sc = score_trades(sub)
        rows.append({"bucket": name, "n": sc["resolved_n"], "win_rate": sc["win_rate"], "expectancy_points": sc["expectancy_points"]})
    return rows


def decide_verdict(full: dict, oos: dict, wf_class: str, cost_1tick: dict) -> str:
    n = oos.get("resolved_n") or 0
    e = oos.get("expectancy_points")
    e1 = cost_1tick.get("expectancy_points")
    if n < 30:
        return "INSUFFICIENT_SAMPLE"
    if e is None:
        return "INSUFFICIENT_SAMPLE"
    if float(e) <= 0:
        return "NO_EDGE_OBSERVED"
    if wf_class == "STABLE_NEGATIVE":
        return "NO_EDGE_OBSERVED"
    survives = e1 is not None and float(e1) > 0
    if float(e) > 0 and n >= 50 and wf_class in ("STABLE_POSITIVE", "MIXED") and survives:
        if wf_class == "STABLE_POSITIVE" and survives:
            return "EDGE_OBSERVED"
        return "WEAK_EDGE_OBSERVED"
    if float(e) > 0:
        return "WEAK_EDGE_OBSERVED"
    return "NO_EDGE_OBSERVED"


def monte_carlo(trades: Sequence[DVPTrade], *, n_sim: int = 20000, seed: int = 29) -> dict[str, Any]:
    """Bootstrap trade points; descriptive challenge-style pass/fail under generic params."""
    pts = [float(t.points) for t in trades if t.points is not None and t.outcome != "AMBIGUOUS"]
    if len(pts) < 30:
        return {"ok": False, "reason": "insufficient_trades"}
    rng = random.Random(seed)
    # Generic challenge: +$3000 target, -$2000 max loss, $50/point approx for 1 NQ ($20/pt * 2.5? actually NQ=$20/point)
    # Use points space: target +150 pts ($3000), max loss -100 pts ($2000) at $20/pt
    target_pts = 150.0
    max_loss_pts = -100.0
    passes = fails = 0
    days_to_pass = []
    days_to_fail = []
    for _ in range(n_sim):
        eq = 0.0
        day = 0
        while day < 500:
            day += 1
            # sample ~trades_per_day from empirical mean capped 1-4
            n_day = rng.randint(1, 3)
            for _j in range(n_day):
                eq += rng.choice(pts)
            if eq >= target_pts:
                passes += 1
                days_to_pass.append(day)
                break
            if eq <= max_loss_pts:
                fails += 1
                days_to_fail.append(day)
                break
        else:
            fails += 1
            days_to_fail.append(500)
    return {
        "ok": True,
        "simulations": n_sim,
        "challenge_assumptions": {
            "point_value_usd": 20,
            "profit_target_usd": 3000,
            "max_loss_usd": 2000,
            "profit_target_points": target_pts,
            "max_loss_points": max_loss_pts,
            "note": "Generic parameterized challenge — not a named prop firm",
        },
        "pass_probability": passes / n_sim,
        "fail_probability": fails / n_sim,
        "median_days_to_pass": None if not days_to_pass else statistics.median(days_to_pass),
        "mean_days_to_pass": None if not days_to_pass else statistics.mean(days_to_pass),
        "median_days_to_fail": None if not days_to_fail else statistics.median(days_to_fail),
    }


def run_phase29(*, force_fetch: bool = False) -> dict[str, Any]:
    REPORTS.mkdir(parents=True, exist_ok=True)
    JOURNAL_DIR.mkdir(parents=True, exist_ok=True)
    paper_before = PHASE26_PAPER.stat().st_size if PHASE26_PAPER.exists() else 0
    p26 = assert_phase26_untouched()

    fetch_info = fetch_and_stitch_nq(force=force_fetch)
    if not fetch_info.get("ok"):
        payload = {"ok": False, "phase": 29, "verdict": "INSUFFICIENT_SAMPLE", "fetch": fetch_info, "phase26": p26}
        VALIDATION_JSON.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
        return payload

    data = load_nq_bars()
    if not data.get("ok"):
        payload = {"ok": False, "phase": 29, "verdict": "INSUFFICIENT_SAMPLE", "error": "missing_nq_bars", "fetch": fetch_info}
        VALIDATION_JSON.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
        return payload

    bars_1m, bars_5m, bars_15m = data["bars_1m"], data["bars_5m"], data["bars_15m"]
    print(f"replaying DVP on {len(bars_5m)} 5m bars...", flush=True)
    trades, guard = replay_all_days(bars_1m, bars_5m, bars_15m, DVP_ORIGINAL)
    print(f"trades={len(trades)}", flush=True)
    # persist journal
    jpath = JOURNAL_DIR / "trades.jsonl"
    with jpath.open("w", encoding="utf-8") as fh:
        for t in trades:
            fh.write(json.dumps(t.to_dict(), default=str) + "\n")

    full = score_trades(trades)
    dev, oos, split_meta = split_dev_oos(trades)
    sc_dev, sc_oos = score_trades(dev), score_trades(oos)
    wf_rows, wf_class = walkforward(oos if (sc_oos.get("resolved_n") or 0) >= 40 else trades)
    cost_rows = []
    cost_scores = {}
    for ticks in (0, 1, 2):
        ct = apply_cost(oos if oos else trades, ticks)
        sc = score_trades(ct)
        cost_scores[ticks] = sc
        cost_rows.append(
            {
                "ticks_per_side": ticks,
                "tick_size": NQ_TICK,
                "expectancy_points": sc["expectancy_points"],
                "win_rate": sc["win_rate"],
                "profit_factor": sc["profit_factor"],
                "resolved_n": sc["resolved_n"],
            }
        )

    tod = time_of_day(trades)
    verdict = decide_verdict(full, sc_oos, wf_class, cost_scores.get(1, {}))

    mc = {"ok": False, "skipped": True}
    if verdict in ("EDGE_OBSERVED", "WEAK_EDGE_OBSERVED") and (sc_oos.get("expectancy_points") or 0) > 0:
        mc = monte_carlo(oos if oos else trades)
        _write_csv(REPORTS / "phase29_monte_carlo.csv", [mc])
    else:
        _write_csv(REPORTS / "phase29_monte_carlo.csv", [{"status": "SKIPPED", "reason": "no_independent_positive_edge"}])

    paper_after = PHASE26_PAPER.stat().st_size if PHASE26_PAPER.exists() else 0
    indep_wr = full.get("win_rate")
    similar = None if indep_wr is None else abs(float(indep_wr) - SOURCE_CLAIMED_WIN_RATE) < 0.05

    _write_csv(REPORTS / "phase29_overall.csv", [full])
    _write_csv(REPORTS / "phase29_in_sample.csv", [sc_dev])
    _write_csv(REPORTS / "phase29_out_of_sample.csv", [sc_oos])
    _write_csv(
        REPORTS / "phase29_long_short.csv",
        [
            {"side": "long", **(full.get("long") or {})},
            {"side": "short", **(full.get("short") or {})},
        ],
    )
    _write_csv(REPORTS / "phase29_time_of_day.csv", tod)
    _write_csv(REPORTS / "phase29_cost_sensitivity.csv", cost_rows)
    _write_csv(REPORTS / "phase29_walkforward.csv", wf_rows)
    _write_csv(REPORTS / "phase29_daily_guardrails.csv", [guard])

    payload = {
        "ok": True,
        "phase": 29,
        "verdict": verdict,
        "strategy_family": STRATEGY_FAMILY,
        "strategy_version": STRATEGY_VERSION,
        "candidate_id": DVP_ORIGINAL.candidate_id,
        "config_hash": config_hash(DVP_ORIGINAL),
        "phase26_untouched": p26,
        "phase26_paper_unchanged": paper_before == paper_after,
        "fetch": {k: fetch_info.get(k) for k in ("ok", "reused", "bars_1m", "bars_5m", "bars_15m", "start", "end", "continuous_choice", "roll_rule", "cost_estimate", "qa_1m")},
        "dataset": {
            "provider": "databento:GLBX.MDP3",
            "bars_1m": len(bars_1m),
            "bars_5m": len(bars_5m),
            "bars_15m": len(bars_15m),
            "trading_days": len(trading_dates_ny(bars_5m)),
            "period": f"{fetch_info.get('start')} → {fetch_info.get('end')}",
            "contracts": len(fetch_info.get("contracts") or []),
        },
        "exact_strategy": {
            "vwap_reset": VWAP_RESET_LOCAL,
            "vwap_basis": VWAP_PRICE_BASIS,
            "vwap_basis_status": VWAP_BASIS_STATUS,
            "trend_tf": "15m",
            "execution_tf": "5m",
            "trade_start": TRADE_START_LOCAL,
            "no_new_after": NO_NEW_TRADES_AFTER_LOCAL,
            "force_close": FORCE_CLOSE_LOCAL,
            "long_sl_tp": [80, 40],
            "short_sl_tp": [80, 50],
            "max_trades_day": 4,
            "max_losses_day": 2,
            "loss_stop_interpretation": guard["loss_stop_interpretation"],
        },
        "full_sample": full,
        "split": split_meta,
        "in_sample": sc_dev,
        "out_of_sample": sc_oos,
        "walkforward": wf_rows,
        "walkforward_class": wf_class,
        "cost_sensitivity": cost_rows,
        "time_of_day": tod,
        "guardrails": guard,
        "source_comparison": {
            "source_claimed_win_rate": SOURCE_CLAIMED_WIN_RATE,
            "aitrade_win_rate": indep_wr,
            "similar_within_5pp": similar,
            "source_claims_reproduced": bool(verdict in ("EDGE_OBSERVED", "WEAK_EDGE_OBSERVED") and similar),
        },
        "monte_carlo": mc,
        "independently_positive": verdict in ("EDGE_OBSERVED", "WEAK_EDGE_OBSERVED"),
        "survives_1tick": (cost_scores.get(1) or {}).get("expectancy_points") is not None
        and ((cost_scores.get(1) or {}).get("expectancy_points") or 0) > 0,
        "stable_oos": (sc_oos.get("expectancy_points") or 0) > 0 and wf_class in ("STABLE_POSITIVE", "MIXED"),
        "ready_for_paper": verdict in ("EDGE_OBSERVED", "WEAK_EDGE_OBSERVED"),
        "run_gc_portability_29b": False,  # only if NQ edge; set below
        "generated_at": datetime.now(tz=timezone.utc).isoformat(),
        "note": "Independent falsification of published Drift VWAP Pullback on NQ — does not modify Phase 26",
    }
    payload["run_gc_portability_29b"] = bool(payload["independently_positive"])
    VALIDATION_JSON.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    return payload


if __name__ == "__main__":
    out = run_phase29(force_fetch=False)
    print(
        json.dumps(
            {
                "ok": out.get("ok"),
                "verdict": out.get("verdict"),
                "full_n": (out.get("full_sample") or {}).get("resolved_n"),
                "oos_n": (out.get("out_of_sample") or {}).get("resolved_n"),
                "oos_wr": (out.get("out_of_sample") or {}).get("win_rate"),
                "oos_exp": (out.get("out_of_sample") or {}).get("expectancy_points"),
                "p26": out.get("phase26_untouched"),
            },
            indent=2,
        )
    )
