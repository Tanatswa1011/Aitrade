"""Phase 49 — strategy distribution statistics. GC / NQ / ES kept separate."""
from __future__ import annotations

import json
import math
import statistics
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Optional, Sequence

from phase18_metrics import percentile
from phase49_trade_audit import UNAVAILABLE, write_csv

REPORTS = Path(__file__).resolve().parent / "reports" / "phase49_strategy_distributions"


def _rs(trades: Sequence[dict[str, Any]]) -> list[float]:
    out = []
    for t in trades:
        r = t.get("r_multiple")
        if r is None or r == UNAVAILABLE:
            continue
        out.append(float(r))
    return out


def _consec(flags: Sequence[bool]) -> int:
    best = cur = 0
    for f in flags:
        if f:
            cur += 1
            best = max(best, cur)
        else:
            cur = 0
    return best


def _loss_streaks(rs: Sequence[float]) -> list[int]:
    streaks = []
    cur = 0
    for r in rs:
        if r < -1e-12:
            cur += 1
        elif cur:
            streaks.append(cur)
            cur = 0
    if cur:
        streaks.append(cur)
    return streaks or [0]


def _equity_dd(rs: Sequence[float]) -> tuple[float, list[float]]:
    eq = peak = 0.0
    max_dd = 0.0
    curve = []
    for r in rs:
        eq += r
        peak = max(peak, eq)
        dd = peak - eq
        max_dd = max(max_dd, dd)
        curve.append(eq)
    return max_dd, curve


def _daily_r(trades: Sequence[dict[str, Any]]) -> dict[str, float]:
    by: dict[str, float] = defaultdict(float)
    for t in trades:
        r = t.get("r_multiple")
        d = t.get("trading_date")
        if r is None or r == UNAVAILABLE or not d or d == UNAVAILABLE:
            continue
        by[str(d)] += float(r)
    return dict(by)


def _months(daily: dict[str, float]) -> dict[str, float]:
    by: dict[str, float] = defaultdict(float)
    for d, r in daily.items():
        by[d[:7]] += r
    return dict(by)


def _rolling_dd(daily_ordered: Sequence[tuple[str, float]], window: int) -> list[dict[str, Any]]:
    out = []
    rs = [r for _, r in daily_ordered]
    for i in range(len(rs)):
        sl = rs[max(0, i - window + 1) : i + 1]
        dd, _ = _equity_dd(sl)
        out.append({"date": daily_ordered[i][0], "window_days": len(sl), "drawdown_R": dd})
    return out


def distribution_report(trades: Sequence[dict[str, Any]], *, book: str) -> dict[str, Any]:
    rs = _rs(trades)
    n = len(rs)
    wins = [r for r in rs if r > 1e-12]
    losses = [r for r in rs if r < -1e-12]
    daily = _daily_r(trades)
    dates = sorted(daily)
    daily_vals = [daily[d] for d in dates]
    total = sum(rs) if rs else 0.0
    abs_losses = sum(-r for r in losses)
    pf = (sum(wins) / abs_losses) if abs_losses > 1e-12 else (math.inf if wins else None)
    dd, curve = _equity_dd(rs)
    loss_streaks = _loss_streaks(rs)
    best_day = max(daily_vals) if daily_vals else None
    worst_day = min(daily_vals) if daily_vals else None
    pos_profit = sum(r for r in rs if r > 0)
    span_days = None
    if dates:
        d0 = date.fromisoformat(dates[0])
        d1 = date.fromisoformat(dates[-1])
        span_days = max((d1 - d0).days, 1)
    n_days = len(dates)
    weeks = span_days / 7.0 if span_days else None
    months = span_days / 30.437 if span_days else None
    rec = {
        "book": book,
        "number_of_trades": n,
        "date_range": [dates[0], dates[-1]] if dates else None,
        "n_trading_days": n_days,
        "win_rate": (len(wins) / n) if n else None,
        "average_win_R": statistics.mean(wins) if wins else None,
        "average_loss_R": statistics.mean(losses) if losses else None,
        "median_win_R": statistics.median(wins) if wins else None,
        "median_loss_R": statistics.median(losses) if losses else None,
        "expectancy_R": statistics.mean(rs) if rs else None,
        "profit_factor": pf if pf != math.inf else "INF",
        "trades_per_day": (n / n_days) if n_days else None,
        "trades_per_week": (n / weeks) if weeks else None,
        "trades_per_month": (n / months) if months else None,
        "max_consecutive_wins": _consec([r > 1e-12 for r in rs]),
        "max_consecutive_losses": max(loss_streaks) if loss_streaks else 0,
        "90th_percentile_losing_streak": percentile(loss_streaks, 90),
        "95th_percentile_losing_streak": percentile(loss_streaks, 95),
        "max_historical_drawdown_R": dd,
        "average_daily_R": statistics.mean(daily_vals) if daily_vals else None,
        "standard_deviation_daily_R": statistics.pstdev(daily_vals) if len(daily_vals) > 1 else (0.0 if daily_vals else None),
        "best_day_R": best_day,
        "worst_day_R": worst_day,
        "best_day_as_percent_of_total_profit": (best_day / pos_profit) if best_day is not None and pos_profit > 1e-12 else None,
        "recovery_factor": (total / dd) if dd > 1e-12 else None,
        "total_R": total,
        "n_wins": len(wins),
        "n_losses": len(losses),
        "n_scratches": n - len(wins) - len(losses),
    }
    monthly = _months(daily)
    rec["monthly_outcome_R"] = monthly
    ordered = [(d, daily[d]) for d in dates]
    rec["rolling_drawdown_20d"] = _rolling_dd(ordered, 20) if ordered else []
    rec["rolling_drawdown_60d"] = _rolling_dd(ordered, 60) if ordered else []
    return rec


def write_distribution_outputs(book: str, trades: Sequence[dict[str, Any]], report: dict[str, Any]) -> None:
    REPORTS.mkdir(parents=True, exist_ok=True)
    (REPORTS / f"{book.lower()}_distribution.json").write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    flat = {k: v for k, v in report.items() if k not in ("monthly_outcome_R", "rolling_drawdown_20d", "rolling_drawdown_60d")}
    write_csv(REPORTS / f"{book.lower()}_distribution.csv", [flat])
    monthly_rows = [{"month": k, "R": v} for k, v in sorted((report.get("monthly_outcome_R") or {}).items())]
    write_csv(REPORTS / f"{book.lower()}_monthly_R.csv", monthly_rows)
    write_csv(REPORTS / f"{book.lower()}_rolling_dd_20d.csv", report.get("rolling_drawdown_20d") or [])
    daily = _daily_r(trades)
    write_csv(
        REPORTS / f"{book.lower()}_daily_R.csv",
        [{"trading_date": d, "R": daily[d]} for d in sorted(daily)],
    )


def build_all(books: dict[str, Any]) -> dict[str, Any]:
    out = {}
    for name, blob in books.items():
        trades = blob["trades"]
        rep = distribution_report(trades, book=name)
        write_distribution_outputs(name, trades, rep)
        out[name] = rep
    (REPORTS / "all_distributions.json").write_text(json.dumps(out, indent=2, default=str), encoding="utf-8")
    return out
