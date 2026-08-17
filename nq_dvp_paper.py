"""Phase 30 paper-trade model + append-only journal for frozen NQ DVP."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, Sequence

from nq_databento import aggregate_1m_to_ny
from nq_drift_vwap_engine import (
    config_hash,
    replay_all_days,
    replay_dvp_day,
)
from nq_drift_vwap_models import NQ_TICK_SIZE, DVPStrategyConfig, DVPTrade
from nq_dvp_freeze import (
    FROZEN_JSON,
    assert_runtime_matches_frozen,
    load_frozen_document,
    load_frozen_strategy_config,
)
from models import Bar

JOURNAL_DIR = Path("journal") / "phase30_nq_dvp_paper"
PAPER_TRADES_PATH = JOURNAL_DIR / "paper_trades.jsonl"
DAILY_STATE_PATH = JOURNAL_DIR / "daily_state.jsonl"

PRIMARY_FILL = "1_TICK_ADVERSE"
RESOLVED_OUTCOMES = ("TARGET_HIT", "STOP_HIT", "TIME_EXIT", "FORCE_CLOSE")


@dataclass
class NQDVPForwardTrade:
    paper_trade_id: str
    frozen_config_hash: str
    trading_date: str
    contract: str
    direction: str
    drift_timestamp: Optional[int]
    trigger_timestamp: Optional[int]
    entry_timestamp: Optional[int]
    entry_price: Optional[float]
    stop_price: Optional[float]
    target_price: Optional[float]
    exit_timestamp: Optional[int]
    exit_price: Optional[float]
    outcome: Optional[str]
    gross_points: Optional[float]
    net_points: Optional[float]
    mfe_points: Optional[float]
    mae_points: Optional[float]
    fill_slippage_ticks: float
    cost_ticks: float
    daily_trade_number: int
    daily_loss_count_before: int
    status: str = "INVALID"
    created_at: str = ""
    updated_at: str = ""
    extras: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def paper_trade_id(trading_date: str, direction: str, trigger_ts: int) -> str:
    return f"NQ|DVP|{trading_date}|{direction}|{trigger_ts}"


def ensure_journal_dir() -> None:
    JOURNAL_DIR.mkdir(parents=True, exist_ok=True)
    if not PAPER_TRADES_PATH.exists():
        PAPER_TRADES_PATH.write_text("", encoding="utf-8")
    if not DAILY_STATE_PATH.exists():
        DAILY_STATE_PATH.write_text("", encoding="utf-8")


def load_paper_trades(path: Path = PAPER_TRADES_PATH) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            out.append(json.loads(line))
    return out


def existing_ids(path: Path = PAPER_TRADES_PATH) -> set[str]:
    return {str(r.get("paper_trade_id")) for r in load_paper_trades(path) if r.get("paper_trade_id")}


def append_paper_trade(trade: NQDVPForwardTrade, path: Path = PAPER_TRADES_PATH) -> bool:
    ensure_journal_dir()
    if trade.paper_trade_id in existing_ids(path):
        return False
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(trade.to_dict(), default=str) + "\n")
    return True


def append_daily_state(row: dict[str, Any], path: Path = DAILY_STATE_PATH) -> None:
    ensure_journal_dir()
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, default=str) + "\n")


def is_short(direction: str) -> bool:
    return direction in ("bearish", "short", "SHORT")


def fill_price(
    theoretical: float,
    direction: str,
    *,
    ticks_adverse: float,
    tick: float = NQ_TICK_SIZE,
) -> float:
    delta = float(ticks_adverse) * float(tick)
    if is_short(direction):
        return float(theoretical) - delta
    return float(theoretical) + delta


def fill_sensitivity_overlay(theoretical: float, direction: str) -> list[dict[str, Any]]:
    rows = []
    for name, ticks in (("IDEAL_TOUCH", 0.0), ("1_TICK_ADVERSE", 1.0), ("2_TICK_ADVERSE", 2.0)):
        fp = fill_price(theoretical, direction, ticks_adverse=ticks)
        rows.append(
            {
                "scenario": name,
                "ticks_adverse_entry": ticks,
                "paper_fill_price": fp,
                "fill_delta_points": abs(fp - theoretical),
                "primary": name == PRIMARY_FILL,
            }
        )
    return rows


def sample_label(resolved_n: int) -> str:
    if resolved_n < 20:
        return "VERY_EARLY"
    if resolved_n < 30:
        return "EARLY"
    if resolved_n < 50:
        return "MINIMUM_FORWARD_SAMPLE"
    if resolved_n < 100:
        return "MEANINGFUL_FORWARD_SAMPLE"
    if resolved_n < 250:
        return "STRONG_FORWARD_SAMPLE"
    return "LARGE_FORWARD_SAMPLE"


def paper_campaign_status(resolved_n: int, *, data_quality_issue: bool = False) -> str:
    if data_quality_issue:
        return "PAPER_DATA_QUALITY_ISSUE"
    if resolved_n < 30:
        return "PAPER_VALIDATION_IN_PROGRESS"
    return "FORWARD_SAMPLE_INSUFFICIENT"


def outcome_to_status(outcome: Optional[str]) -> str:
    if outcome == "TARGET_HIT":
        return "TARGET_HIT"
    if outcome == "STOP_HIT":
        return "STOP_HIT"
    if outcome in ("FORCE_CLOSE", "TIME_EXIT"):
        return "TIME_EXIT"
    if outcome == "AMBIGUOUS":
        return "AMBIGUOUS"
    if outcome == "OPEN":
        return "POSITION_OPEN"
    return "INVALID"


def _direction_label(direction: str) -> str:
    return "SHORT" if is_short(direction) else "LONG"


def dvp_trade_to_paper(
    trade: DVPTrade,
    *,
    frozen_hash: str,
    contract: str,
    daily_trade_number: int,
    daily_loss_count_before: int,
    fill_ticks_adverse: float = 1.0,
    tick: float = NQ_TICK_SIZE,
    now: Optional[str] = None,
) -> NQDVPForwardTrade:
    ts_now = now or datetime.now(tz=timezone.utc).isoformat()
    extras = dict(trade.extras or {})
    trigger_ts = int(extras.get("pullback_5m_ts") or trade.entry_timestamp)
    direction = trade.direction
    theoretical = float(trade.entry_price)
    pfill = fill_price(theoretical, direction, ticks_adverse=fill_ticks_adverse, tick=tick)
    # Recompute points from paper fill vs historical exit at theoretical levels
    gross = trade.points
    net = None
    if trade.exit_price is not None and trade.points is not None:
        # Adverse entry worsens net by fill_ticks * tick (one-way entry assumption)
        friction = float(fill_ticks_adverse) * float(tick)
        net = float(trade.points) - friction
        # Round-turn cost overlay reported separately via cost_ticks
    status = outcome_to_status(trade.outcome)
    return NQDVPForwardTrade(
        paper_trade_id=paper_trade_id(trade.trading_date, direction, trigger_ts),
        frozen_config_hash=frozen_hash,
        trading_date=trade.trading_date,
        contract=contract,
        direction=direction,
        drift_timestamp=None,
        trigger_timestamp=trigger_ts,
        entry_timestamp=trade.entry_timestamp,
        entry_price=pfill,
        stop_price=trade.stop_price,
        target_price=trade.target_price,
        exit_timestamp=trade.exit_timestamp,
        exit_price=trade.exit_price,
        outcome=trade.outcome if trade.outcome != "FORCE_CLOSE" else "TIME_EXIT",
        gross_points=gross,
        net_points=net,
        mfe_points=None,
        mae_points=None,
        fill_slippage_ticks=float(fill_ticks_adverse),
        cost_ticks=float(fill_ticks_adverse),
        daily_trade_number=daily_trade_number,
        daily_loss_count_before=daily_loss_count_before,
        status=status,
        created_at=ts_now,
        updated_at=ts_now,
        extras={
            **extras,
            "theoretical_entry_price": theoretical,
            "fill_overlays": fill_sensitivity_overlay(theoretical, direction),
            "direction_label": _direction_label(direction),
            "primary_fill": PRIMARY_FILL,
        },
    )


def refuse_custom_strategy_params(**kwargs: Any) -> None:
    """Paper runner must not accept tunable strategy parameters."""
    banned = {
        "hour_return_threshold",
        "long_stop_points",
        "long_target_points",
        "short_stop_points",
        "short_target_points",
        "max_trades_per_day",
        "max_losses_per_day",
        "vwap_reset",
        "trade_start",
        "no_new_trades_after",
        "force_close",
        "threshold",
        "stop",
        "target",
        "sigma_threshold",
    }
    bad = {k: v for k, v in kwargs.items() if k in banned and v is not None}
    if bad:
        raise ValueError(f"RUNTIME_PARAM_REJECTED:{sorted(bad)}")


def run_frozen_dvp_on_bars(
    bars_1m: Sequence[Bar],
    bars_5m: Optional[Sequence[Bar]] = None,
    bars_15m: Optional[Sequence[Bar]] = None,
    *,
    contract: str = "NQ",
    fill_ticks_adverse: float = 1.0,
    persist: bool = False,
    cfg_override: Optional[DVPStrategyConfig] = None,
    tick_size: Optional[float] = None,
    **forbidden_kwargs: Any,
) -> dict[str, Any]:
    """
    Replay frozen DVP semantics. Loads config only from strategy_frozen/nq_dvp_phase30.json.
    Refuses custom strategy parameters.
    """
    refuse_custom_strategy_params(**forbidden_kwargs)

    if not FROZEN_JSON.exists():
        return {"ok": False, "error_code": "MISSING_FROZEN_FILE"}

    doc = load_frozen_document()
    cfg = cfg_override or load_frozen_strategy_config(doc)
    check = assert_runtime_matches_frozen(cfg, doc)
    if not check.get("ok"):
        return {"ok": False, "error_code": "FROZEN_CONFIG_MISMATCH", "check": check}

    tick = float(tick_size) if tick_size is not None else float(
        (doc.get("cost_model_assumptions") or {}).get("tick_size_research") or NQ_TICK_SIZE
    )
    b1 = list(bars_1m)
    b5 = list(bars_5m) if bars_5m is not None else aggregate_1m_to_ny(b1, 5)
    b15 = list(bars_15m) if bars_15m is not None else aggregate_1m_to_ny(b1, 15)

    hist_trades, guard = replay_all_days(b1, b5, b15, cfg=cfg)
    frozen_hash = str(doc.get("frozen_config_hash") or "")
    now = datetime.now(tz=timezone.utc).isoformat()

    # Reconstruct daily counters for paper fields
    paper_trades: list[NQDVPForwardTrade] = []
    written = 0
    by_day: dict[str, list[DVPTrade]] = {}
    for t in hist_trades:
        by_day.setdefault(t.trading_date, []).append(t)

    for td, day_trades in by_day.items():
        losses_before = 0
        for i, t in enumerate(day_trades, start=1):
            pt = dvp_trade_to_paper(
                t,
                frozen_hash=frozen_hash,
                contract=contract,
                daily_trade_number=i,
                daily_loss_count_before=losses_before,
                fill_ticks_adverse=fill_ticks_adverse,
                tick=tick,
                now=now,
            )
            paper_trades.append(pt)
            if persist and append_paper_trade(pt):
                written += 1
            if t.outcome == "STOP_HIT" or (t.points is not None and float(t.points) < 0):
                losses_before += 1

    return {
        "ok": True,
        "frozen_config_hash": frozen_hash,
        "engine_config_hash": config_hash(cfg),
        "tick_size": tick,
        "primary_fill": PRIMARY_FILL,
        "historical_trades": hist_trades,
        "paper_trades": paper_trades,
        "guardrails": guard,
        "persisted": written,
        "n_historical": len(hist_trades),
        "n_paper": len(paper_trades),
    }


def summarize_paper_journal(path: Path = PAPER_TRADES_PATH) -> dict[str, Any]:
    rows = load_paper_trades(path)
    resolved = [
        r
        for r in rows
        if r.get("outcome") in ("TARGET_HIT", "STOP_HIT", "TIME_EXIT", "FORCE_CLOSE")
        or r.get("status") in ("TARGET_HIT", "STOP_HIT", "TIME_EXIT")
    ]
    wins = [r for r in resolved if r.get("gross_points") is not None and float(r["gross_points"]) > 0]
    losses = [r for r in resolved if r.get("gross_points") is not None and float(r["gross_points"]) <= 0]
    timed = [r for r in resolved if r.get("outcome") in ("TIME_EXIT", "FORCE_CLOSE") or r.get("status") == "TIME_EXIT"]
    amb = [r for r in rows if r.get("outcome") == "AMBIGUOUS" or r.get("status") == "AMBIGUOUS"]
    n = len(resolved)
    pts = [float(r["gross_points"]) for r in resolved if r.get("gross_points") is not None]
    win_pts = [float(r["gross_points"]) for r in wins]
    loss_pts = [abs(float(r["gross_points"])) for r in losses]

    def _mean(xs):
        return None if not xs else sum(xs) / len(xs)

    expectancy = _mean(pts)
    gross_wins = sum(win_pts)
    gross_losses = sum(loss_pts)
    pf = (gross_wins / gross_losses) if gross_losses > 0 else None

    # max DD / streak on journal order
    equity = peak = max_dd = 0.0
    streak = max_streak = 0
    for r in resolved:
        p = float(r.get("gross_points") or 0.0)
        equity += p
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
        if p <= 0:
            streak += 1
            max_streak = max(max_streak, streak)
        else:
            streak = 0

    long_r = [r for r in resolved if not is_short(str(r.get("direction") or ""))]
    short_r = [r for r in resolved if is_short(str(r.get("direction") or ""))]

    def _side(rs):
        if not rs:
            return {"n": 0, "win_rate": None, "expectancy_points": None}
        w = [r for r in rs if r.get("gross_points") is not None and float(r["gross_points"]) > 0]
        ep = [float(r["gross_points"]) for r in rs if r.get("gross_points") is not None]
        return {
            "n": len(rs),
            "win_rate": len(w) / len(rs),
            "expectancy_points": _mean(ep),
        }

    return {
        "paper_trades": len(rows),
        "resolved": n,
        "wins": len(wins),
        "losses": len(losses),
        "timed_exits": len(timed),
        "ambiguous": len(amb),
        "win_rate": (len(wins) / n) if n else None,
        "average_win_points": _mean(win_pts),
        "average_loss_points": _mean(loss_pts),
        "expectancy_points": expectancy,
        "profit_factor": pf,
        "max_drawdown_points": max_dd if n else None,
        "longest_losing_streak": max_streak if n else None,
        "long": _side(long_r),
        "short": _side(short_r),
        "sample_label": sample_label(n),
        "campaign_status": paper_campaign_status(n),
        "rows": rows,
    }


def recover_daily_state_from_journal(
    trading_date: str,
    path: Path = PAPER_TRADES_PATH,
) -> dict[str, Any]:
    """Resume helper: reconstruct current-day counters from append-only journal."""
    rows = [r for r in load_paper_trades(path) if r.get("trading_date") == trading_date]
    losses = 0
    for r in rows:
        pts = r.get("gross_points")
        if r.get("outcome") == "STOP_HIT" or (pts is not None and float(pts) < 0):
            losses += 1
    return {
        "trading_date": trading_date,
        "daily_trade_count": len(rows),
        "daily_loss_count": losses,
        "hit_trade_cap": len(rows) >= 4,
        "hit_loss_cap": losses >= 2,
    }
