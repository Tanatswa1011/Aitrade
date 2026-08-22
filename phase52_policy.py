"""Phase 52 — FundedNext Flex 50K / NQ prop execution policy. Machine-enforceable. DRY_RUN."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from typing import Any, Literal, Optional
from zoneinfo import ZoneInfo

from nq_microstructure_models import FROZEN_NQ_HASH
from nq_post_news_models import DEFAULT_BLACKOUT_AFTER_MIN, DEFAULT_BLACKOUT_BEFORE_MIN
from prop_rules_v1 import (
    REQUIRES_CONFIRMATION,
    in_fundednext_flat_window,
    load_profile,
)

CHICAGO = ZoneInfo("America/Chicago")
NY = ZoneInfo("America/New_York")

PROFILE_ID = "FUNDEDNEXT_FLEX_50K"
STRATEGY_ID = "NQ_DRIFT_VWAP_PULLBACK"
INSTRUMENT = "MNQ"
STOP_POINTS = 80.0
POINT_USD_MICRO = 2.0
UNIT_RISK_USD = STOP_POINTS * POINT_USD_MICRO  # 160
TICK = 0.25

FAST_QTY = 2
SAFE_QTY = 1
MAX_QTY = 2
REJECT_QTY = 3

DAILY_STOP_FRAC = 0.35
PROFIT_TARGET = 2500.0
MAX_LOSS = 1500.0
START_EQUITY = 50000.0
MLL_LOCK_AT = 50100.0
CONSISTENCY_RATIO = 0.40
MAX_MICROS = 30
MAX_MINIS = 3
INACTIVITY_DAYS = 30
FLAT_TIME = time(15, 10)
RESUME_TIME = time(17, 0)

# Frozen NQ DVP research distribution (Phase 49 audit).
FROZEN_WR = 0.663
FROZEN_E_R = 0.06463687499999997
FROZEN_AVG_WIN_R = 0.5164347662141779
FROZEN_AVG_LOSS_R = -0.8242117952522255

POLICY_STATES = (
    "EVAL_SAFE",
    "EVAL_FAST",
    "EVAL_PROTECTED",
    "EVAL_NEAR_TARGET",
    "EVAL_DAILY_STOPPED",
    "EVAL_BREACHED",
    "EVAL_PASSED",
    "FUNDED_SAFE",
    "FUNDED_PROTECTED",
    "PAUSED",
)

ACTIVE_EVAL = ("EVAL_SAFE", "EVAL_FAST", "EVAL_PROTECTED", "EVAL_NEAR_TARGET", "EVAL_DAILY_STOPPED")

KillClass = Literal[
    "block_new_orders",
    "cancel_entries",
    "flatten_positions",
    "pause_strategy",
    "pause_account",
    "operator_review",
]


# ---------------------------------------------------------------------------
# Daily governor
# ---------------------------------------------------------------------------
def remaining_drawdown(equity: float, mll: float) -> float:
    if equity is None or mll is None:
        raise ValueError("DRAW_DOWN_CALCULATION_INVALID")
    return float(equity) - float(mll)


def session_daily_stop_threshold(remaining_dd_at_session_open: float, frac: float = DAILY_STOP_FRAC) -> float:
    """Fixed for the Chicago session. MLL trails EOD-only, so open remaining DD is the reference."""
    return max(0.0, float(frac) * float(remaining_dd_at_session_open))


def daily_loss_usd(*, session_open_equity: float, current_equity: float) -> float:
    """Positive number = money lost. Uses marked equity (realized + unrealized + fees already in fills)."""
    return float(session_open_equity) - float(current_equity)


def daily_governor_triggered(
    *,
    session_open_equity: float,
    current_equity: float,
    remaining_dd_at_session_open: float,
    frac: float = DAILY_STOP_FRAC,
    eps: float = 1e-9,
) -> bool:
    thr = session_daily_stop_threshold(remaining_dd_at_session_open, frac)
    loss = daily_loss_usd(session_open_equity=session_open_equity, current_equity=current_equity)
    return loss + eps >= thr and thr > 0


def chicago_session_id(ts: datetime) -> str:
    """Globex day: 17:00 CT → next 16:59 CT. Friday 15:10 starts weekend flat until Sunday 17:00."""
    local = ts.astimezone(CHICAGO) if ts.tzinfo else ts.replace(tzinfo=CHICAGO)
    if local.time() >= RESUME_TIME:
        d = local.date()
    else:
        d = (local - timedelta(days=1)).date()
    return d.isoformat()


def governor_resets_at(ts: datetime) -> bool:
    """New entries allowed again at 17:00 CT if not weekend-flat."""
    return not in_fundednext_flat_window(ts)


# ---------------------------------------------------------------------------
# Position sizing
# ---------------------------------------------------------------------------
def allowed_qty(
    *,
    state: str,
    requested: int,
    remaining_dd: float,
    daily_capacity: float,
    demoted: bool,
    consecutive_losses: int,
    last_qty: int,
) -> tuple[int, str]:
    """Never rounds up. Never increases after losses or because the eval is slow."""
    if state in ("EVAL_BREACHED", "PAUSED", "EVAL_DAILY_STOPPED"):
        return 0, "BLOCK_STATE"
    if requested >= REJECT_QTY:
        return 0, "BLOCK_QTY_3MNQ_REJECTED"
    state_cap = SAFE_QTY if state in ("EVAL_SAFE", "EVAL_PROTECTED", "EVAL_NEAR_TARGET", "FUNDED_SAFE", "FUNDED_PROTECTED") or demoted else FAST_QTY
    if state == "EVAL_FAST" and not demoted:
        state_cap = FAST_QTY
    dd_cap = int(remaining_dd // UNIT_RISK_USD) if remaining_dd > 0 else 0
    # daily governor capacity: do not enter if one full stop would exceed remaining daily room
    day_cap = int(daily_capacity // UNIT_RISK_USD) if daily_capacity > 0 else 0
    q = min(int(requested), MAX_QTY, MAX_MICROS, dd_cap, max(0, day_cap), state_cap)
    if consecutive_losses > 0:
        q = min(q, last_qty if last_qty > 0 else SAFE_QTY, SAFE_QTY if demoted else q)
        q = min(q, last_qty if last_qty > 0 else q)
    if q < requested:
        return q, "SIZE_REDUCED" if q > 0 else "BLOCK_INSUFFICIENT_RISK_CAPACITY"
    return q, "OK"


# ---------------------------------------------------------------------------
# Near-target
# ---------------------------------------------------------------------------
def near_target(remaining_profit: float, *, rule: str = "ONE_FAST_R") -> bool:
    if remaining_profit <= 0:
        return True
    if rule == "ONE_FAST_R":
        return remaining_profit <= UNIT_RISK_USD * FAST_QTY + 1e-9  # $320
    if rule == "ONE_SAFE_R":
        return remaining_profit <= UNIT_RISK_USD * SAFE_QTY + 1e-9  # $160
    if rule == "PCT_90":
        return remaining_profit <= 0.10 * PROFIT_TARGET
    if rule == "PCT_95":
        return remaining_profit <= 0.05 * PROFIT_TARGET
    if rule == "PCT_80":
        return remaining_profit <= 0.20 * PROFIT_TARGET
    return False


# ---------------------------------------------------------------------------
# News
# ---------------------------------------------------------------------------
def news_blackout_window(event_ts: datetime) -> tuple[datetime, datetime]:
    pre = timedelta(minutes=int(DEFAULT_BLACKOUT_BEFORE_MIN))
    post = timedelta(minutes=int(DEFAULT_BLACKOUT_AFTER_MIN))
    return event_ts - pre, event_ts + post


def in_clock_news_window(now: datetime) -> bool:
    """family_port_engine 08:25–08:35 ET clock lock (in addition to event-relative ±5m)."""
    ny = now.astimezone(NY) if now.tzinfo else now.replace(tzinfo=NY)
    hh = ny.strftime("%H:%M")
    return "08:25" <= hh <= "08:35"


def in_internal_news_lock(*, now: datetime, event_ts: Optional[datetime], calendar_status: str) -> tuple[bool, str]:
    """FundedNext allows news trading. AITRADE still enforces ±5m internal lock. Missing calendar → fail closed."""
    if calendar_status in ("MISSING", "STALE", "UNKNOWN"):
        return True, "NEWS_CALENDAR_FAIL_SAFE"
    if in_clock_news_window(now):
        return True, "NEWS_LOCK_CLOCK_0825_0835_ET"
    if event_ts is None:
        return False, ""
    start, end = news_blackout_window(event_ts)
    if now < start:
        return False, "NEWS_LOCK_PRE"
    if start <= now <= end:
        return True, "NEWS_LOCK"
    return False, "NEWS_LOCK_POST"


# ---------------------------------------------------------------------------
# Kill switches
# ---------------------------------------------------------------------------
KILL_SWITCHES: dict[str, dict[str, Any]] = {
    "PROP_RULE_DATA_MISSING": {"actions": ("block_new_orders", "pause_account", "operator_review"), "flatten": False},
    "ACCOUNT_EQUITY_UNKNOWN": {"actions": ("block_new_orders", "cancel_entries", "pause_account", "operator_review"), "flatten": True},
    "DRAW_DOWN_CALCULATION_INVALID": {"actions": ("block_new_orders", "cancel_entries", "pause_account"), "flatten": True},
    "POSITION_STATE_MISMATCH": {"actions": ("block_new_orders", "cancel_entries", "pause_account", "operator_review"), "flatten": True},
    "ORDER_STATE_MISMATCH": {"actions": ("block_new_orders", "cancel_entries", "pause_strategy", "operator_review"), "flatten": False},
    "STRATEGY_HASH_MISMATCH": {"actions": ("block_new_orders", "cancel_entries", "pause_account", "operator_review"), "flatten": True},
    "LIVE_DATA_STALE": {"actions": ("block_new_orders", "cancel_entries", "pause_strategy"), "flatten": False},
    "BROKER_CONNECTION_UNSTABLE": {"actions": ("block_new_orders", "cancel_entries", "pause_account", "operator_review"), "flatten": False},
    "DUPLICATE_ORDER_DETECTED": {"actions": ("block_new_orders", "cancel_entries"), "flatten": False},
    "MAX_POSITION_EXCEEDED": {"actions": ("block_new_orders", "cancel_entries"), "flatten": True},
    "NEWS_BLACKOUT_VIOLATION_RISK": {"actions": ("block_new_orders", "cancel_entries"), "flatten": False},
    "DAILY_STOP_TRIGGERED": {"actions": ("block_new_orders", "cancel_entries"), "flatten": False},
    "ACCOUNT_BREACH_IMMINENT": {"actions": ("block_new_orders", "cancel_entries", "flatten_positions", "pause_account"), "flatten": True},
}


@dataclass
class PolicyDecision:
    verdict: Literal["ALLOW", "BLOCK"]
    code: str
    state: str
    allowed_qty: int = 0
    actions: tuple[str, ...] = ()
    reasons: list[str] = field(default_factory=list)
    flatten: bool = False
    extras: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "code": self.code,
            "state": self.state,
            "allowed_qty": self.allowed_qty,
            "actions": list(self.actions),
            "reasons": self.reasons,
            "flatten": self.flatten,
            "extras": self.extras,
        }


def _block(code: str, state: str, *reasons: str, flatten: bool = False, actions: tuple[str, ...] = ()) -> PolicyDecision:
    spec = KILL_SWITCHES.get(code, {})
    acts = actions or tuple(spec.get("actions") or ("block_new_orders",))
    flat = flatten or bool(spec.get("flatten"))
    return PolicyDecision("BLOCK", code, state, 0, acts, list(reasons), flat)


def next_state(
    state: str,
    *,
    passed: bool = False,
    breached: bool = False,
    daily_stopped: bool = False,
    near: bool = False,
    demoted: bool = False,
    integrity_fail: bool = False,
    new_session: bool = False,
) -> str:
    if integrity_fail:
        return "PAUSED"
    if breached:
        return "EVAL_BREACHED"
    if passed:
        return "EVAL_PASSED"
    if state == "PAUSED":
        return "PAUSED"
    if state == "EVAL_BREACHED":
        return "EVAL_BREACHED"
    if state == "EVAL_PASSED":
        return "EVAL_PASSED"
    if daily_stopped:
        return "EVAL_DAILY_STOPPED"
    if state == "EVAL_DAILY_STOPPED" and new_session:
        state = "EVAL_SAFE" if demoted else "EVAL_FAST"
    if near:
        return "EVAL_NEAR_TARGET"
    if demoted:
        return "EVAL_PROTECTED" if state in ("EVAL_FAST", "EVAL_PROTECTED", "EVAL_NEAR_TARGET", "EVAL_SAFE", "EVAL_DAILY_STOPPED") else state
    if state in ("EVAL_NEAR_TARGET",) and not near:
        return "EVAL_PROTECTED" if demoted else "EVAL_FAST"
    return state


def evaluate_intent(
    *,
    state: str,
    intent_qty: int,
    action: str,
    now: datetime,
    equity: Optional[float],
    mll: Optional[float],
    session_open_equity: Optional[float],
    remaining_dd_open: Optional[float],
    realized_pnl: Optional[float],
    open_pnl: float = 0.0,
    open_qty: int = 0,
    last_qty: int = 2,
    consecutive_losses: int = 0,
    demoted: bool = False,
    strategy_hash: str = FROZEN_NQ_HASH,
    calendar_status: str = "OK",
    event_ts: Optional[datetime] = None,
    data_age_sec: Optional[float] = None,
    broker_ok: bool = True,
    position_known: bool = True,
    order_known: bool = True,
    duplicate: bool = False,
    in_price_limit: Optional[bool] = False,
    prop_rules_ok: bool = True,
    last_trade_ts: Optional[datetime] = None,
    daily_already_stopped: bool = False,
    near_rule: str = "PCT_95",
) -> PolicyDecision:
    st = state
    if not prop_rules_ok:
        return _block("PROP_RULE_DATA_MISSING", "PAUSED", "prop_rule_blob_missing")
    if strategy_hash != FROZEN_NQ_HASH:
        return _block("STRATEGY_HASH_MISMATCH", "PAUSED", "frozen_nq_hash_mismatch")
    if not broker_ok:
        return _block("BROKER_CONNECTION_UNSTABLE", "PAUSED", "broker_unstable")
    if equity is None:
        return _block("ACCOUNT_EQUITY_UNKNOWN", "PAUSED", "equity_unknown")
    if mll is None or session_open_equity is None or remaining_dd_open is None:
        return _block("DRAW_DOWN_CALCULATION_INVALID", "PAUSED", "dd_inputs_missing")
    if not position_known:
        return _block("POSITION_STATE_MISMATCH", "PAUSED", "position_unknown")
    if not order_known:
        return _block("ORDER_STATE_MISMATCH", st, "order_unknown")
    if data_age_sec is not None and data_age_sec > 30:
        return _block("LIVE_DATA_STALE", st, f"data_age_sec={data_age_sec}")
    if duplicate:
        return _block("DUPLICATE_ORDER_DETECTED", st, "duplicate_client_order")

    rem = remaining_drawdown(equity, mll)
    if rem <= 0:
        return _block("ACCOUNT_BREACH_IMMINENT", "EVAL_BREACHED", "remaining_dd<=0", flatten=True)

    lock, news_code = in_internal_news_lock(now=now, event_ts=event_ts, calendar_status=calendar_status)
    if lock and action == "NEW_ENTRY":
        return _block("NEWS_BLACKOUT_VIOLATION_RISK", st, news_code or "news_lock")

    if in_fundednext_flat_window(now) and action in ("NEW_ENTRY",):
        return _block("BLOCK_TRADING_HOURS", st, "fundednext_mandatory_flat", actions=("block_new_orders", "cancel_entries", "flatten_positions"), flatten=True)

    if in_price_limit:
        return _block("BLOCK_PRICE_LIMIT_ZONE", st, "within_2pct_cme_limit")

    if last_trade_ts is not None:
        elapsed = abs((now.astimezone(CHICAGO).date() - last_trade_ts.astimezone(CHICAGO).date()).days)
        if elapsed >= INACTIVITY_DAYS:
            return _block("BLOCK_INACTIVITY", "PAUSED", f"inactive_{elapsed}_days")

    marked = float(equity)  # already marked
    stopped = daily_already_stopped or daily_governor_triggered(
        session_open_equity=float(session_open_equity),
        current_equity=marked,
        remaining_dd_at_session_open=float(remaining_dd_open),
    )
    if stopped and action == "NEW_ENTRY":
        return _block("DAILY_STOP_TRIGGERED", "EVAL_DAILY_STOPPED", "daily_loss>=0.35*remaining_dd_open", actions=("block_new_orders", "cancel_entries"))

    remaining_profit = PROFIT_TARGET - (float(equity) - START_EQUITY)
    near = near_target(remaining_profit, rule=near_rule)
    passed = (float(equity) - START_EQUITY) >= PROFIT_TARGET - 1e-9
    new_st = next_state(
        st,
        passed=passed,
        daily_stopped=stopped,
        near=near,
        demoted=demoted,
        new_session=governor_resets_at(now) and not stopped,
    )
    if action != "NEW_ENTRY":
        return PolicyDecision("ALLOW", "ALLOW", new_st, 0, (), [], extras={"remaining_dd": rem})
    if passed:
        return PolicyDecision("BLOCK", "ALREADY_PASSED", "EVAL_PASSED", 0, ("block_new_orders",), ["profit_target_met"])

    if open_qty + max(0, intent_qty) > MAX_QTY:
        return _block("MAX_POSITION_EXCEEDED", st, f"open={open_qty}+{intent_qty}>{MAX_QTY}")

    # imminent: one full SAFE stop would breach MLL
    if rem < UNIT_RISK_USD - 1e-9:
        return _block("ACCOUNT_BREACH_IMMINENT", st, "full_stop_exceeds_remaining_dd", flatten=False)

    daily_cap = session_daily_stop_threshold(float(remaining_dd_open)) - daily_loss_usd(
        session_open_equity=float(session_open_equity), current_equity=marked
    )
    q, why = allowed_qty(
        state="EVAL_NEAR_TARGET" if near else ("EVAL_PROTECTED" if demoted else st),
        requested=int(intent_qty),
        remaining_dd=rem,
        daily_capacity=max(0.0, daily_cap),
        demoted=demoted or near,
        consecutive_losses=consecutive_losses,
        last_qty=last_qty,
    )
    new_st = next_state(st, passed=passed, daily_stopped=stopped, near=near, demoted=demoted, new_session=governor_resets_at(now) and not stopped)
    if q <= 0:
        return PolicyDecision("BLOCK", why, new_st, 0, ("block_new_orders",), [why])
    return PolicyDecision("ALLOW", "ALLOW", new_st, q, (), [why] if why != "OK" else [], extras={"daily_capacity": daily_cap, "remaining_dd": rem})


def fn_eval_rules_catalog() -> list[dict[str, Any]]:
    prof = load_profile(PROFILE_ID)
    ev = prof.stage("EVALUATION").raw
    gen = prof.general()
    pay = prof.payout()
    rows = []

    def add(name, source, value, stage, event, calc, action, reset, material_survival, status="CONFIRMED"):
        rows.append(
            {
                "canonical_rule": name,
                "source": source,
                "threshold": value,
                "applies": stage,
                "trigger_event": event,
                "machine_calculation": calc,
                "enforcement": action,
                "reset": reset,
                "material_eval_survival": material_survival,
                "status": status,
            }
        )

    add("NOMINAL_BALANCE", "PROP_RULES_V1.evaluation.nominal_account_size", ev["nominal_account_size"], "EVALUATION", "account_open", "equity_start=50000", "informational", "n/a", False)
    add("PROFIT_TARGET", "PROP_RULES_V1.evaluation.profit_target", ev["profit_target"], "EVALUATION", "eod_and_trade", "equity-50000 >= 2500, then 40% consistency adj", "EVAL_PASSED", "n/a", True)
    add("MAX_LOSS", "PROP_RULES_V1.evaluation.max_loss", ev["max_loss"], "EVALUATION", "equity_vs_mll", "equity <= mll", "EVAL_BREACHED flatten", "n/a", True)
    add("EOD_TRAILING_MLL", "PROP_RULES_V1.evaluation.drawdown_type/mll_*", "EOD_TRAILING lock 50100 distance 1500", "EVALUATION", "eod_high", "trail_eod_mll_equity", "update mll never down", "locks at 50100", True)
    add("FIRM_DAILY_LOSS_LIMIT", "PROP_RULES_V1.evaluation.daily_loss_limit", ev["daily_loss_limit"], "EVALUATION", "n/a", "NONE", "none (AITRADE internal 35% governor instead)", "n/a", False)
    add("AITRADE_DAILY_GOVERNOR", "Phase 49B / phase52_policy.DAILY_STOP_FRAC", 0.35, "EVALUATION", "marked_equity vs session-open remaining DD", "daily_loss >= 0.35 * remaining_dd_at_session_open", "EVAL_DAILY_STOPPED block new+cancel entries; do not flatten unless firm flat window", "17:00 CT session reset", True)
    add("CONSISTENCY_40", "PROP_RULES_V1.evaluation.consistency_ratio_max", ev["consistency_ratio_max"], "EVALUATION", "eod best day", "best_day/total_profit<=0.40 else target=best_day/0.40", "expand target, not auto-fail", "pass", True)
    add("CONTRACT_CAP", "PROP_RULES_V1.evaluation.max_minis/max_micros", f"{ev['max_minis']} minis / {ev['max_micros']} micros", "EVALUATION", "order", "to_micro_equivalent <= 30; policy max 2 MNQ", "BLOCK_CONTRACT / reject 3 MNQ", "n/a", True)
    add("MIN_TRADING_DAYS", "PROP_RULES_V1.evaluation.minimum_trading_days", ev["minimum_trading_days"], "EVALUATION", "pass_check", "NONE", "no min-day gate", "n/a", False)
    add("INACTIVITY_30D", "PROP_RULES_V1.evaluation.inactivity_days", ev["inactivity_days"], "EVALUATION", "calendar", "calendar_days_inactive>=30", "PAUSED", "trade", True)
    add("NO_OVERNIGHT", "PROP_RULES_V1.evaluation.overnight_holding", ev["overnight_holding"], "EVALUATION", "15:10 CT", "in_fundednext_flat_window", "flatten + block entries", "17:00 CT", True)
    add("NO_WEEKEND", "PROP_RULES_V1.evaluation.weekend_holding", ev["weekend_holding"], "EVALUATION", "Fri 15:10–Sun 17:00 CT", "in_fundednext_flat_window", "flatten + block", "Sun 17:00 CT", True)
    add("MANDATORY_FLAT", "PROP_RULES_V1.evaluation.mandatory_flat_time", f"{ev['mandatory_flat_time']} {ev['mandatory_flat_timezone']}", "EVALUATION", "clock", "time>=15:10 CT", "flatten", "17:00 CT", True)
    add("FIRM_NEWS_TRADING", "PROP_RULES_V1.evaluation.tier_1_news_trading", ev["tier_1_news_trading"], "EVALUATION", "news", "ALLOWED by firm", "firm does not block; AITRADE still ±5m internal", "n/a", False)
    add("AITRADE_NEWS_BLACKOUT", "nq_post_news_models DEFAULT ±5m + family_port 08:25-08:35", "±5 minutes", "INTERNAL", "tier1 event or missing calendar", "in_internal_news_lock", "block new + cancel entries; fail closed if calendar missing", "event_end+5m", True)
    add("CME_PRICE_LIMIT_2PCT", "PROP_RULES_V1.general_compliance.prohibit_trading_within_2_percent_of_CME_price_limit", True, "EVALUATION", "market_state", "in_cme_price_limit_zone", "BLOCK_PRICE_LIMIT_ZONE", "n/a", True)
    add("CME_PRODUCT_LIMIT_PCT", "PROP_RULES_V1.general_compliance.cme_product_limit_pct", gen.get("cme_product_limit_pct"), "EVALUATION", "limit_pct", "unknown exact product limit %", "fail closed if in zone already", "n/a", False, REQUIRES_CONFIRMATION)
    add("AUTOMATION_ALLOWED", "PROP_RULES_V1.general_compliance.automation_allowed", gen.get("automation_allowed"), "LIVE_ENABLEMENT", "go_live", "unconfirmed", "DRY_RUN only until confirmed", "n/a", False, REQUIRES_CONFIRMATION)
    add("COPY_TRADING", "PROP_RULES_V1.general_compliance.copy_trading", gen.get("copy_trading"), "MULTI_ACCOUNT", "replication", "independent accounts", "not used in single-eval policy", "n/a", False, REQUIRES_CONFIRMATION)
    add("PAYOUT_FREQUENCY", "PROP_RULES_V1.payout.payout_frequency", pay.get("payout_frequency"), "FUNDED", "payout", "unconfirmed", "not eval survival", "n/a", False, REQUIRES_CONFIRMATION)
    add("FIRST_PAYOUT_BUFFER", "PROP_RULES_V1.payout.first_payout_required_buffer", pay.get("first_payout_required_buffer"), "FUNDED", "payout", "unconfirmed", "not eval survival", "n/a", False, REQUIRES_CONFIRMATION)
    add("MAX_FUNDED_ACCOUNTS", "PROP_RULES_V1.funded.max_funded_accounts", "not set on FN funded", "FUNDED", "purchase", "REQUIRES_CONFIRMATION from Phase 51", "not eval survival", "n/a", False, REQUIRES_CONFIRMATION)
    add("NO_MARTINGALE", "aitrade_operating_policy flags + Phase 49B", True, "EVALUATION", "size", "qty never increases after loss", "SIZE_REDUCED / BLOCK", "win resets consec", True)
    return rows
