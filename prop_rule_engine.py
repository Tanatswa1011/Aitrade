"""Prop Rule Engine V1 — ALLOW / machine-readable BLOCK. Strategy-agnostic. DRY_RUN."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal, Optional

from account_state_engine import classify_account_state
from aitrade_operating_policy import load_operating_policy
from prop_rules_v1 import (
    REQUIRES_CONFIRMATION,
    AccountMetrics,
    FirmProfile,
    MarketState,
    calendar_days_inactive,
    chicago_now,
    consistency_ratio,
    contract_class,
    in_fundednext_flat_window,
    is_none_policy,
    is_unknown,
    load_profile,
    load_rules_document,
    to_micro_equivalent,
)

Verdict = Literal["ALLOW", "BLOCK"]

REJECTION_CODES = (
    "ALLOW",
    "BLOCK_NEWS",
    "BLOCK_CONTRACT_LIMIT",
    "BLOCK_DRAWDOWN",
    "BLOCK_DAILY_LOSS",
    "BLOCK_CONSISTENCY_GOVERNOR",
    "BLOCK_TRADING_HOURS",
    "BLOCK_OVERNIGHT",
    "BLOCK_PRICE_LIMIT_ZONE",
    "BLOCK_INACTIVITY",
    "BLOCK_ACCOUNT_LOCKOUT",
    "BLOCK_UNKNOWN_RULE",
)


@dataclass
class ComplianceDecision:
    verdict: Verdict
    code: str
    reasons: list[str] = field(default_factory=list)
    advisory: list[str] = field(default_factory=list)
    account_state: str = ""
    firm_profile: str = ""
    account_stage: str = ""
    unknown_fields_consulted: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "code": self.code,
            "reasons": self.reasons,
            "advisory": self.advisory,
            "account_state": self.account_state,
            "firm_profile": self.firm_profile,
            "account_stage": self.account_stage,
            "unknown_fields_consulted": self.unknown_fields_consulted,
        }


def _block(code: str, *reasons: str, **kwargs: Any) -> ComplianceDecision:
    return ComplianceDecision(verdict="BLOCK", code=code, reasons=list(reasons), **kwargs)


def _news_policy(stage_rules: dict[str, Any]) -> Any:
    if "tier_1_news_trading" in stage_rules:
        return stage_rules.get("tier_1_news_trading")
    return stage_rules.get("news_trading")


class PropRuleEngine:
    def __init__(self, doc: Optional[dict[str, Any]] = None):
        self.doc = doc or load_rules_document()
        self.policy = load_operating_policy()

    def evaluate_trade(
        self,
        *,
        firm_profile: str | FirmProfile,
        account_stage: str,
        account_state: Optional[str] = None,
        instrument: str,
        proposed_quantity: int,
        timestamp: datetime,
        market_state: Optional[MarketState] = None,
        account_metrics: Optional[AccountMetrics] = None,
        action: str = "OPEN",
    ) -> ComplianceDecision:
        market_state = market_state or MarketState()
        metrics = account_metrics or AccountMetrics()
        action_u = str(action).upper()
        unknown: list[str] = []
        advisory: list[str] = []

        try:
            profile = firm_profile if isinstance(firm_profile, FirmProfile) else load_profile(str(firm_profile), self.doc)
        except KeyError:
            return _block("BLOCK_UNKNOWN_RULE", f"unknown_firm_profile:{firm_profile}")

        if isinstance(profile.raw.get("evaluation"), str) and is_unknown(profile.raw.get("evaluation")):
            return _block(
                "BLOCK_UNKNOWN_RULE",
                f"profile_not_fully_specified:{profile.profile_id}",
                firm_profile=profile.profile_id,
                account_stage=account_stage,
            )

        snap = classify_account_state(
            firm_profile=profile,
            account_stage=account_stage,
            metrics=metrics,
            now=timestamp,
            explicit_state=account_state,
        )
        base = {
            "account_state": snap.state,
            "firm_profile": profile.profile_id,
            "account_stage": snap.account_stage,
            "unknown_fields_consulted": unknown,
            "advisory": advisory,
        }

        if snap.state in ("EVAL_LOCKOUT", "FUNDED_LOCKOUT") or (account_state or "").endswith("LOCKOUT"):
            code = snap.lockout_reason or "BLOCK_ACCOUNT_LOCKOUT"
            return _block(code, "account_lockout", **base)

        stage = profile.stage(account_stage)
        rules = stage.raw
        if rules.get("_unresolved") and is_unknown(rules.get("_unresolved")):
            unknown.append(f"{account_stage}.rules")
            return _block("BLOCK_UNKNOWN_RULE", "stage_rules_requires_confirmation", **{**base, "unknown_fields_consulted": unknown})

        # Inactivity (also covered by classify, but keep explicit)
        days_allowed = rules.get("inactivity_days")
        if not is_unknown(days_allowed) and not is_none_policy(days_allowed) and days_allowed is not None:
            elapsed = calendar_days_inactive(timestamp, metrics.last_trade_timestamp)
            if elapsed is not None and elapsed >= int(days_allowed):
                return _block("BLOCK_INACTIVITY", f"inactive_{elapsed}_days_limit_{days_allowed}", **base)
        elif is_unknown(days_allowed):
            unknown.append("inactivity_days")

        # Drawdown / MLL
        if metrics.remaining_drawdown is not None and metrics.remaining_drawdown <= 0:
            return _block("BLOCK_DRAWDOWN", "remaining_drawdown_exhausted", **base)
        if metrics.mll_locked and metrics.realized_pnl is not None:
            lock = rules.get("mll_lock_level")
            if lock is not None and not is_unknown(lock) and metrics.realized_pnl < float(lock):
                return _block("BLOCK_DRAWDOWN", "below_locked_mll", **base)
        if metrics.current_equity is not None and metrics.current_mll is not None:
            if metrics.current_equity < metrics.current_mll:
                return _block("BLOCK_DRAWDOWN", "equity_below_mll", **base)

        # Daily loss
        dll = rules.get("daily_loss_limit")
        if is_none_policy(dll):
            pass
        elif is_unknown(dll):
            unknown.append("daily_loss_limit")
            if metrics.daily_realized_pnl is not None and metrics.daily_realized_pnl < 0:
                return _block("BLOCK_UNKNOWN_RULE", "daily_loss_limit_unstated_while_day_is_red", **{**base, "unknown_fields_consulted": unknown})
        else:
            if metrics.daily_realized_pnl is not None and metrics.daily_realized_pnl <= -abs(float(dll)):
                return _block("BLOCK_DAILY_LOSS", "daily_loss_limit_hit", **base)

        # Flatten / overnight / hours — flatten (CLOSE) remains allowed
        if action_u in ("OPEN", "INCREASE"):
            overnight = rules.get("overnight_holding")
            weekend = rules.get("weekend_holding")
            flat = rules.get("mandatory_flat_time")
            if profile.profile_id == "FUNDEDNEXT_FLEX_50K":
                if in_fundednext_flat_window(timestamp):
                    local = chicago_now(timestamp)
                    code = "BLOCK_OVERNIGHT" if local.weekday() >= 4 else "BLOCK_TRADING_HOURS"
                    return _block(code, "fundednext_mandatory_flat_1510_ct", **base)
            else:
                if is_unknown(flat):
                    unknown.append("mandatory_flat_time")
                if is_unknown(overnight):
                    unknown.append("overnight_holding")
                if is_unknown(weekend):
                    unknown.append("weekend_holding")

        # News
        news_pol = _news_policy(rules)
        if market_state.is_tier1_news and action_u in ("OPEN", "INCREASE"):
            if is_unknown(news_pol):
                unknown.append("tier_1_news_trading")
                return _block("BLOCK_UNKNOWN_RULE", "tier1_news_policy_unstated", **{**base, "unknown_fields_consulted": unknown})
            if str(news_pol).upper() == "PROHIBITED":
                return _block("BLOCK_NEWS", "tier1_news_prohibited_on_this_stage", **base)

        # Contract limits
        max_minis = rules.get("max_minis")
        max_micros = rules.get("max_micros")
        cls = contract_class(instrument)
        if cls is None:
            unknown.append("instrument_class")
            return _block("BLOCK_UNKNOWN_RULE", f"unrecognized_instrument:{instrument}", **{**base, "unknown_fields_consulted": unknown})
        if cls == "MINI":
            if is_unknown(max_minis):
                unknown.append("max_minis")
                return _block("BLOCK_UNKNOWN_RULE", "max_minis_unstated", **{**base, "unknown_fields_consulted": unknown})
            total = int(metrics.current_mini_qty) + int(proposed_quantity)
            if not is_none_policy(max_minis) and total > int(max_minis):
                return _block("BLOCK_CONTRACT_LIMIT", f"minis_{total}_gt_{max_minis}", **base)
        if cls == "MICRO":
            if is_unknown(max_micros):
                unknown.append("max_micros")
                return _block("BLOCK_UNKNOWN_RULE", "max_micros_unstated", **{**base, "unknown_fields_consulted": unknown})
            total = int(metrics.current_micro_qty) + int(proposed_quantity)
            if not is_none_policy(max_micros) and total > int(max_micros):
                return _block("BLOCK_CONTRACT_LIMIT", f"micros_{total}_gt_{max_micros}", **base)
        # Family-equivalent cap when both classes are known
        if not is_unknown(max_micros) and not is_none_policy(max_micros) and max_micros is not None:
            add = to_micro_equivalent(instrument, proposed_quantity) or 0
            existing = int(metrics.current_micro_qty) + int(metrics.current_mini_qty) * 10
            if existing + add > int(max_micros):
                return _block("BLOCK_CONTRACT_LIMIT", "family_micro_equivalent_exceeded", **base)

        # CME price-limit zone (FundedNext explicit; others unknown)
        gen = profile.general()
        zone_rule = gen.get("prohibit_trading_within_2_percent_of_CME_price_limit")
        in_zone = market_state.in_cme_price_limit_zone
        if in_zone is None and market_state.distance_to_limit_pct is not None:
            zone_pct = float(self.doc.get("cme_price_limit_zone_pct") or 2.0)
            in_zone = float(market_state.distance_to_limit_pct) <= zone_pct
        if in_zone and action_u in ("OPEN", "INCREASE"):
            if zone_rule is True:
                return _block("BLOCK_PRICE_LIMIT_ZONE", "within_2pct_of_cme_price_limit", **base)
            if is_unknown(zone_rule):
                unknown.append("prohibit_trading_within_2_percent_of_CME_price_limit")
                return _block("BLOCK_UNKNOWN_RULE", "price_limit_zone_policy_unstated", **{**base, "unknown_fields_consulted": unknown})

        # Consistency governor — advisory by default
        ratio_max = rules.get("consistency_ratio_max")
        applies = str(rules.get("consistency_applies") or "")
        stage_u = str(account_stage).upper()
        applies_here = (
            ("EVAL" in applies or "CHALLENGE" in applies)
            and stage_u in ("EVALUATION", "CHALLENGE")
        ) or (applies.upper() not in ("", "NONE", "EVALUATION_ONLY", "CHALLENGE_ONLY") and not is_none_policy(rules.get("consistency_rule")))
        if stage_u in ("EVALUATION", "CHALLENGE") and ratio_max is not None and not is_unknown(ratio_max) and not is_none_policy(rules.get("consistency_rule")):
            best = metrics.highest_profitable_day
            total = metrics.realized_pnl
            if best is not None and total is not None and total > 0:
                ratio = consistency_ratio(best, total)
                if ratio is not None and ratio > float(ratio_max) + 1e-12:
                    advisory.append(f"consistency_ratio_{ratio:.4f}_gt_{ratio_max}")
                    if self.policy.consistency_governor_blocks:
                        return _block("BLOCK_CONSISTENCY_GOVERNOR", "governor_configured_to_block", **{**base, "advisory": advisory})

        return ComplianceDecision(
            verdict="ALLOW",
            code="ALLOW",
            reasons=["prop_rule_engine_allow"],
            **base,
        )


def evaluate_trade(**kwargs: Any) -> ComplianceDecision:
    return PropRuleEngine().evaluate_trade(**kwargs)


def is_benchmark_day(daily_profit: float, *, min_profit: float = 200.0) -> bool:
    return float(daily_profit) >= float(min_profit) - 1e-12
