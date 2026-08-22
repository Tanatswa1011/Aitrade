"""PROP_RULES_V1 — typed models and helpers. Outside strategy code. DRY_RUN only."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, time
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
RULES_PATH = ROOT / "config" / "PROP_RULES_V1.json"

UNKNOWN = "UNKNOWN"
REQUIRES_CONFIRMATION = "REQUIRES_CONFIRMATION"
NONE = "NONE"

PRIMARY_PROFILES = ("MFFU_RAPID_EOD_50K", "FUNDEDNEXT_FLEX_50K")
ALTERNATIVE_PROFILES = ("MFFU_RAPID_STANDARD_50K",)

INSTRUMENT_FAMILIES = {
    "NQ": ("NQ", "MNQ"),
    "ES": ("ES", "MES"),
    "GC": ("GC", "MGC"),
    "CL": ("CL", "MCL"),
}
MINI_ROOTS = {"NQ", "ES", "GC", "CL"}
MICRO_ROOTS = {"MNQ", "MES", "MGC", "MCL"}
MICRO_PER_MINI = 10

CHICAGO = ZoneInfo("America/Chicago")


def is_unknown(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str) and value.upper() in (UNKNOWN, REQUIRES_CONFIRMATION):
        return True
    return False


def is_none_policy(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str) and value.upper() in (NONE, "NONE_STATED"):
        return True
    return False


def load_rules_document(path: Path = RULES_PATH) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"missing_prop_rules_v1:{path}")
    return json.loads(path.read_text(encoding="utf-8"))


def normalize_symbol(instrument: str) -> str:
    s = (instrument or "").upper().replace(":", "").replace("/", "")
    for tok in ("MNQ", "MES", "MGC", "MCL", "NQ", "ES", "GC", "CL"):
        if tok in s:
            return tok
    return s


def instrument_family(instrument: str) -> Optional[str]:
    root = normalize_symbol(instrument)
    for fam, members in INSTRUMENT_FAMILIES.items():
        if root in members:
            return fam
    return None


def contract_class(instrument: str) -> Optional[str]:
    root = normalize_symbol(instrument)
    if root in MINI_ROOTS:
        return "MINI"
    if root in MICRO_ROOTS:
        return "MICRO"
    return None


def to_micro_equivalent(instrument: str, quantity: int) -> Optional[int]:
    cls = contract_class(instrument)
    if cls == "MICRO":
        return int(quantity)
    if cls == "MINI":
        return int(quantity) * MICRO_PER_MINI
    return None


def family_members(instrument: str) -> tuple[str, ...]:
    fam = instrument_family(instrument)
    if fam is None:
        return (normalize_symbol(instrument),)
    return INSTRUMENT_FAMILIES[fam]


@dataclass(frozen=True)
class StageRules:
    raw: dict[str, Any]

    def get(self, key: str, default: Any = None) -> Any:
        return (self.raw or {}).get(key, default)


@dataclass
class FirmProfile:
    profile_id: str
    raw: dict[str, Any]
    primary: bool

    def stage(self, account_stage: str) -> StageRules:
        key = {
            "EVALUATION": "evaluation",
            "CHALLENGE": "evaluation",
            "FUNDED": "funded",
            "SIM_FUNDED": "funded",
            "LIVE": "live",
        }.get(str(account_stage).upper(), str(account_stage).lower())
        blob = self.raw.get(key)
        if blob is None or isinstance(blob, str):
            return StageRules(raw={"_unresolved": blob if blob is not None else REQUIRES_CONFIRMATION})
        return StageRules(raw=blob)

    def payout(self) -> dict[str, Any]:
        blob = self.raw.get("payout")
        if not isinstance(blob, dict):
            return {"_unresolved": blob if blob is not None else REQUIRES_CONFIRMATION}
        return blob

    def general(self) -> dict[str, Any]:
        blob = self.raw.get("general_compliance")
        if not isinstance(blob, dict):
            return {"_unresolved": blob if blob is not None else REQUIRES_CONFIRMATION}
        return blob


def load_profile(profile_id: str, doc: Optional[dict[str, Any]] = None) -> FirmProfile:
    doc = doc or load_rules_document()
    profiles = doc.get("profiles") or {}
    if profile_id not in profiles:
        raise KeyError(f"unknown_firm_profile:{profile_id}")
    raw = profiles[profile_id]
    return FirmProfile(profile_id=profile_id, raw=raw, primary=bool(raw.get("primary")))


def adjusted_required_profit(
    *,
    base_target: float,
    highest_profitable_day: float,
    ratio_max: float,
) -> float:
    """If the best day exceeds ratio_max of the base target, required profit increases."""
    if ratio_max <= 0:
        raise ValueError("ratio_max_must_be_positive")
    if highest_profitable_day <= 0:
        return float(base_target)
    implied = float(highest_profitable_day) / float(ratio_max)
    return max(float(base_target), implied)


def consistency_ratio(highest_profitable_day: float, total_profit: float) -> Optional[float]:
    if total_profit <= 0:
        return None
    return float(highest_profitable_day) / float(total_profit)


def consistency_within_cap(highest_profitable_day: float, total_profit: float, ratio_max: float) -> bool:
    ratio = consistency_ratio(highest_profitable_day, total_profit)
    if ratio is None:
        return True
    return ratio <= float(ratio_max) + 1e-12


def trail_eod_mll_pnl(
    *,
    eod_pnl_high: float,
    previous_mll: float,
    locked: bool,
    lock_level: float = 100.0,
    distance: float = 2000.0,
) -> tuple[float, bool]:
    """MFFU Rapid EOD funded: PnL-space MLL, EOD highs only, never down, lock at +lock_level."""
    if locked:
        return float(lock_level), True
    candidate = float(eod_pnl_high) - float(distance)
    mll = max(float(previous_mll), candidate)
    if mll >= float(lock_level) - 1e-12:
        return float(lock_level), True
    return mll, False


def trail_eod_mll_equity(
    *,
    eod_equity: float,
    previous_mll: float,
    locked: bool,
    lock_at: float = 50100.0,
    distance: float = 1500.0,
) -> tuple[float, bool]:
    """FundedNext Flex: equity-space MLL, EOD highs only, lock at lock_at."""
    if locked:
        return float(lock_at), True
    candidate = float(eod_equity) - float(distance)
    mll = max(float(previous_mll), candidate)
    if mll >= float(lock_at) - 1e-12:
        return float(lock_at), True
    return mll, False


def mffu_payout_unlocked(
    *,
    realized_pnl: float,
    first_payout_completed: bool,
    net_profit_since_last_payout: float,
    first_buffer: float = 2100.0,
    subsequent: float = 500.0,
) -> bool:
    if not first_payout_completed:
        return float(realized_pnl) >= float(first_buffer) - 1e-12
    return float(net_profit_since_last_payout) >= float(subsequent) - 1e-12


def parse_hhmm(value: Any) -> Optional[time]:
    if is_unknown(value) or is_none_policy(value) or value is None:
        return None
    text = str(value)
    if "_" in text:
        text = text.split("_")[0]
    parts = text.split(":")
    if len(parts) < 2:
        return None
    return time(int(parts[0]), int(parts[1]))


def chicago_now(ts: datetime) -> datetime:
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=CHICAGO)
    return ts.astimezone(CHICAGO)


def in_fundednext_flat_window(ts: datetime) -> bool:
    """True when FundedNext requires flat: 15:10–17:00 CT daily, plus weekend until Sunday 17:00 CT."""
    local = chicago_now(ts)
    t = local.time()
    start = time(15, 10)
    resume = time(17, 0)
    wd = local.weekday()
    if wd == 5:
        return True
    if wd == 6:
        return t < resume
    if wd == 4:
        return t >= start
    return start <= t < resume


def calendar_days_inactive(now: datetime, last_trade: Optional[datetime]) -> Optional[int]:
    if last_trade is None:
        return None
    a = now if now.tzinfo else now.replace(tzinfo=CHICAGO)
    b = last_trade if last_trade.tzinfo else last_trade.replace(tzinfo=CHICAGO)
    return abs((a.date() - b.date()).days)


@dataclass
class AccountMetrics:
    current_equity: Optional[float] = None
    realized_pnl: Optional[float] = None
    current_mll: Optional[float] = None
    remaining_drawdown: Optional[float] = None
    distance_to_target: Optional[float] = None
    highest_profitable_day: Optional[float] = None
    consistency_ratio: Optional[float] = None
    trading_days: Optional[int] = None
    benchmark_days: Optional[int] = None
    net_profit_since_last_payout: Optional[float] = None
    payout_buffer: Optional[float] = None
    consecutive_losses: Optional[int] = None
    daily_realized_pnl: Optional[float] = None
    open_pnl: Optional[float] = None
    last_trade_timestamp: Optional[datetime] = None
    first_payout_completed: bool = False
    mll_locked: bool = False
    current_mini_qty: int = 0
    current_micro_qty: int = 0
    open_position_count: int = 0
    extras: dict[str, Any] = field(default_factory=dict)


@dataclass
class MarketState:
    in_cme_price_limit_zone: Optional[bool] = None
    distance_to_limit_pct: Optional[float] = None
    is_tier1_news: bool = False
    session_price_limit_pct: Optional[float] = None
    extras: dict[str, Any] = field(default_factory=dict)
