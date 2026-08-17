"""Generic prop-firm rule simulator — descriptive pass/fail only, no optimization."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Literal, Optional

DrawdownType = Literal["EOD", "INTRADAY_TRAILING", "STATIC"]


@dataclass
class PropTrade:
    trading_date: str
    pnl_usd: float
    strategy: str = ""
    contracts: int = 1
    instrument: str = ""


@dataclass
class PropConfig:
    firm_name: Optional[str] = None
    nominal_account_size: float = 50000.0
    profit_target: float = 3000.0
    max_drawdown: float = 2000.0
    drawdown_type: DrawdownType = "EOD"
    daily_loss_limit: Optional[float] = None
    max_contracts: Optional[int] = None
    consistency_rule_pct: Optional[float] = None
    forced_flatten_time: Optional[str] = None

    @classmethod
    def from_json(cls, doc: dict[str, Any]) -> "PropConfig":
        dd = (doc.get("drawdown_type") or "EOD").upper()
        if dd not in ("EOD", "INTRADAY_TRAILING", "STATIC"):
            dd = "EOD"
        return cls(
            firm_name=doc.get("firm_name"),
            nominal_account_size=float(doc.get("nominal_account_size") or 50000),
            profit_target=float(doc.get("profit_target") or 3000),
            max_drawdown=float(doc.get("max_drawdown") or 2000),
            drawdown_type=dd,  # type: ignore[arg-type]
            daily_loss_limit=None if doc.get("daily_loss_limit") is None else float(doc["daily_loss_limit"]),
            max_contracts=None if doc.get("max_contracts") is None else int(doc["max_contracts"]),
            consistency_rule_pct=None if doc.get("consistency_rule") is None else float(doc["consistency_rule"]),
            forced_flatten_time=doc.get("forced_flatten_time"),
        )


@dataclass
class PropSimResult:
    outcome: Literal["PASS", "FAIL", "IN_PROGRESS"]
    fail_reasons: list[str] = field(default_factory=list)
    ending_balance: float = 0.0
    peak_balance: float = 0.0
    max_drawdown_usd: float = 0.0
    trading_days: int = 0
    total_pnl: float = 0.0
    daily_pnls: dict[str, float] = field(default_factory=dict)
    consistency_violation: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome,
            "fail_reasons": self.fail_reasons,
            "ending_balance": self.ending_balance,
            "peak_balance": self.peak_balance,
            "max_drawdown_usd": self.max_drawdown_usd,
            "trading_days": self.trading_days,
            "total_pnl": self.total_pnl,
            "daily_pnls": self.daily_pnls,
            "consistency_violation": self.consistency_violation,
        }


def _aggregate_daily(trades: list[PropTrade]) -> dict[str, float]:
    daily: dict[str, float] = {}
    for t in trades:
        daily[t.trading_date] = daily.get(t.trading_date, 0.0) + float(t.pnl_usd)
    return dict(sorted(daily.items()))


def simulate_prop_account(
    trades: list[PropTrade],
    config: PropConfig,
    *,
    starting_balance: float = 0.0,
) -> PropSimResult:
    """
    Replay chronological trades against prop rules.
    Balance starts at 0 (evaluation P&L framing); drawdown measured from peak equity.
    """
    result = PropSimResult(outcome="IN_PROGRESS", ending_balance=starting_balance)
    if not trades:
        result.outcome = "IN_PROGRESS"
        result.fail_reasons.append("no_trades")
        return result

    if config.max_contracts is not None:
        for t in trades:
            if t.contracts > config.max_contracts:
                result.outcome = "FAIL"
                result.fail_reasons.append(f"contract_cap_exceeded:{t.trading_date}")
                return result

    daily = _aggregate_daily(trades)
    result.daily_pnls = daily
    result.trading_days = len(daily)

    equity = starting_balance
    peak = equity
    floor = equity - config.max_drawdown
    max_dd = 0.0

    for d, pnl in daily.items():
        if config.daily_loss_limit is not None and pnl <= -abs(config.daily_loss_limit):
            result.outcome = "FAIL"
            result.fail_reasons.append(f"daily_loss_limit:{d}")
            result.ending_balance = equity + pnl
            result.total_pnl = result.ending_balance - starting_balance
            return result

        equity += pnl
        if config.drawdown_type in ("EOD", "STATIC"):
            if equity > peak:
                peak = equity
                floor = peak - config.max_drawdown
            dd = peak - equity
            max_dd = max(max_dd, dd)
            if equity < floor:
                result.outcome = "FAIL"
                result.fail_reasons.append(f"max_drawdown_breach:{d}")
                result.peak_balance = peak
                result.max_drawdown_usd = max_dd
                result.ending_balance = equity
                result.total_pnl = equity - starting_balance
                return result
        else:
            # INTRADAY_TRAILING — same daily bucket; trailing floor follows intraday peak
            if equity > peak:
                peak = equity
            floor = peak - config.max_drawdown
            dd = peak - equity
            max_dd = max(max_dd, dd)
            if equity < floor:
                result.outcome = "FAIL"
                result.fail_reasons.append(f"intraday_trailing_drawdown:{d}")
                result.peak_balance = peak
                result.max_drawdown_usd = max_dd
                result.ending_balance = equity
                result.total_pnl = equity - starting_balance
                return result

    result.peak_balance = peak
    result.max_drawdown_usd = max_dd
    result.ending_balance = equity
    result.total_pnl = equity - starting_balance

    if equity >= config.profit_target:
        if config.consistency_rule_pct is not None and result.total_pnl > 0:
            best_day = max(daily.values())
            pct = best_day / result.total_pnl
            if pct > config.consistency_rule_pct:
                result.consistency_violation = True
                result.outcome = "FAIL"
                result.fail_reasons.append("consistency_rule_violation")
                return result
        result.outcome = "PASS"
        return result

    result.outcome = "IN_PROGRESS"
    result.fail_reasons.append("profit_target_not_reached")
    return result


def load_prop_template(path: Path = Path("config") / "prop_risk_template.json") -> PropConfig:
    doc = json.loads(path.read_text(encoding="utf-8"))
    pct = doc.get("consistency_rule")
    cfg = PropConfig.from_json(doc)
    if isinstance(pct, (int, float)):
        cfg.consistency_rule_pct = float(pct)
    elif isinstance(pct, str) and pct.endswith("%"):
        cfg.consistency_rule_pct = float(pct.rstrip("%")) / 100.0
    return cfg


def trades_from_r_multiples(
    *,
    trade_rs: list[float],
    dates: list[str],
    risk_per_trade_usd: float,
    strategy: str = "",
) -> list[PropTrade]:
    out: list[PropTrade] = []
    for r, d in zip(trade_rs, dates):
        out.append(
            PropTrade(
                trading_date=d,
                pnl_usd=float(r) * float(risk_per_trade_usd),
                strategy=strategy,
                contracts=1,
            )
        )
    return out


def simulate_combined(
    trade_lists: dict[str, list[PropTrade]],
    config: PropConfig,
) -> dict[str, Any]:
    """Run per-strategy and combined chronological simulation."""
    combined: list[PropTrade] = []
    for rows in trade_lists.values():
        combined.extend(rows)
    combined.sort(key=lambda t: (t.trading_date, t.strategy))
    per_strategy = {k: simulate_prop_account(v, config) for k, v in trade_lists.items()}
    combined_result = simulate_prop_account(combined, config)
    return {
        "config_firm": config.firm_name,
        "per_strategy": {k: v.to_dict() for k, v in per_strategy.items()},
        "combined": combined_result.to_dict(),
    }
