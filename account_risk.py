"""Account-level risk safeguards — fail-closed checks, no live execution enablement."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from execution_status import GC_FROZEN_HASH, NQ_FROZEN_HASH, is_execution_paused


@dataclass
class AccountRiskLimits:
    max_total_open_risk_usd: float = 500.0
    max_daily_account_loss_usd: float = 500.0
    max_simultaneous_positions: int = 1
    allowed_account: str = "Sim101"
    allowed_execution_instruments: tuple[str, ...] = ("MNQ SEP26",)
    nq_max_trades_per_day: int = 4
    nq_max_losses_per_day: int = 2
    nq_no_new_trades_after: str = "15:30"
    nq_force_flatten: str = "15:55"


@dataclass
class RiskCheckContext:
    account: str
    instrument: str
    quantity: int
    strategy: str
    strategy_hash: str
    open_positions: int = 0
    open_risk_usd: float = 0.0
    proposed_risk_usd: float = 0.0
    daily_loss_usd: float = 0.0
    daily_trades: int = 0
    daily_losses: int = 0
    et_time: str = ""
    data_stale: bool = False
    bridge_connected: bool = True
    halted: bool = False
    duplicate_position: bool = False


@dataclass
class RiskCheckResult:
    ok: bool
    blocks: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "blocks": self.blocks}


def expected_hash_for_strategy(strategy: str) -> Optional[str]:
    s = (strategy or "").upper()
    if "GC" in s or "V2" in s or "VWAP" in s and "NQ" not in s:
        return GC_FROZEN_HASH
    if "NQ" in s or "DVP" in s or "DRIFT" in s:
        return NQ_FROZEN_HASH
    return None


def run_account_risk_checks(ctx: RiskCheckContext, limits: Optional[AccountRiskLimits] = None) -> RiskCheckResult:
    lim = limits or AccountRiskLimits()
    blocks: list[str] = []

    if is_execution_paused():
        blocks.append("PROJECT_PAUSED")
    if ctx.halted:
        blocks.append("HALT_ACTIVE")
    if ctx.account != lim.allowed_account:
        blocks.append(f"LIVE_ACCOUNT_BLOCKED:{ctx.account}")
    if ctx.instrument not in lim.allowed_execution_instruments:
        blocks.append(f"INSTRUMENT_MISMATCH:{ctx.instrument}")
    if int(ctx.quantity) != 1:
        blocks.append(f"QUANTITY_BLOCKED:{ctx.quantity}")
    if ctx.data_stale:
        blocks.append("STALE_DATA_BLOCK")
    if not ctx.bridge_connected:
        blocks.append("CONNECTION_STATE_BLOCK")
    if ctx.duplicate_position:
        blocks.append("DUPLICATE_POSITION_BLOCK")
    if ctx.open_positions >= lim.max_simultaneous_positions:
        blocks.append("MAX_SIMULTANEOUS_POSITIONS")
    if ctx.open_risk_usd + ctx.proposed_risk_usd > lim.max_total_open_risk_usd + 1e-9:
        blocks.append("MAX_TOTAL_OPEN_RISK")
    if ctx.daily_loss_usd >= lim.max_daily_account_loss_usd:
        blocks.append("MAX_DAILY_ACCOUNT_LOSS")

    expected = expected_hash_for_strategy(ctx.strategy)
    if expected and ctx.strategy_hash != expected:
        blocks.append("STRATEGY_HASH_MISMATCH")

    # NQ DVP frozen daily guardrails
    if "DVP" in (ctx.strategy or "").upper() or "NQ" in (ctx.strategy or "").upper():
        if ctx.daily_trades >= lim.nq_max_trades_per_day:
            blocks.append("NQ_MAX_TRADES_PER_DAY")
        if ctx.daily_losses >= lim.nq_max_losses_per_day:
            blocks.append("NQ_MAX_LOSSES_PER_DAY")
        if ctx.et_time and ctx.et_time[11:16] >= lim.nq_no_new_trades_after:
            blocks.append("NQ_TRADE_WINDOW_CLOSED")

    return RiskCheckResult(ok=len(blocks) == 0, blocks=blocks)


def assert_account_risk_ok(ctx: RiskCheckContext, limits: Optional[AccountRiskLimits] = None) -> RiskCheckResult:
    result = run_account_risk_checks(ctx, limits)
    if not result.ok:
        raise PermissionError("|".join(result.blocks))
    return result
