"""Phase 47 — append-only paper journal for locked ES DVP (not frozen)."""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from es_dvp_lock import (
    CANDIDATE_ID,
    INSTRUMENT,
    LOCKED_CFG,
    LOCKED_VERSION,
    STRATEGY_FAMILY,
    locked_config_hash,
)

JOURNAL_DIR = Path("journal") / "phase47_es_dvp_paper"
PAPER_TRADES_PATH = JOURNAL_DIR / "paper_trades.jsonl"
SETUPS_PATH = JOURNAL_DIR / "setups.jsonl"
DAILY_STATE_PATH = JOURNAL_DIR / "daily_state.jsonl"
RUNNER_STATE_PATH = JOURNAL_DIR / "runner_state.json"

TICK = 0.25
POINT_USD = 50.0
MES_POINT_USD = 5.0
COMMISSION_POINTS = 0.08
PRIMARY_FILL = "1_TICK_ADVERSE"
PRIMARY_ADVERSE_TICKS = 1.0

RESOLVED_ENTERED = ("TARGET", "STOP", "FORCE_CLOSE", "TARGET_HIT", "STOP_HIT", "TIME_EXIT")

STATES = (
    "NO_SETUP",
    "SETUP_ARMED",
    "ENTRY_PENDING",
    "OPEN_POSITION",
    "TARGET",
    "STOP",
    "FORCE_CLOSE",
    "SESSION_CANCEL",
    "INVALIDATED_BEFORE_ENTRY",
)


@dataclass
class ESDVPForwardTrade:
    paper_trade_id: str
    strategy_family: str
    strategy_version: str
    config_hash: str
    instrument: str
    contract: str
    session_date: str
    timezone: str
    direction: str
    setup_timestamp: Optional[int]
    signal_timestamp: Optional[int]
    entry_timestamp: Optional[int]
    entry_price: Optional[float]
    theoretical_entry_price: Optional[float]
    stop_price: Optional[float]
    target_price: Optional[float]
    exit_timestamp: Optional[int]
    exit_price: Optional[float]
    exit_reason: Optional[str]
    raw_pnl_points: Optional[float]
    net_pnl_points: Optional[float]
    pnl_dollars_es: Optional[float]
    pnl_dollars_mes: Optional[float]
    r_result: Optional[float]
    slippage_assumption: str
    news_blackout: bool
    daily_trade_number: int
    daily_prior_losses: int
    mfe_points: Optional[float]
    mae_points: Optional[float]
    state: str
    notes: str = ""
    created_at: str = ""
    updated_at: str = ""
    extras: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def setup_key(session_date: str, setup_timestamp: int, direction: str) -> str:
    return f"{STRATEGY_FAMILY}|{INSTRUMENT}|{session_date}|{setup_timestamp}|{direction}"


def paper_trade_id(session_date: str, direction: str, setup_timestamp: int) -> str:
    return f"ES|DVP|{session_date}|{direction}|{setup_timestamp}"


def ensure_journal_dir() -> None:
    JOURNAL_DIR.mkdir(parents=True, exist_ok=True)
    for path in (PAPER_TRADES_PATH, SETUPS_PATH, DAILY_STATE_PATH):
        if not path.exists():
            path.write_text("", encoding="utf-8")
    if not RUNNER_STATE_PATH.exists():
        save_runner_state(default_runner_state())


def default_runner_state() -> dict[str, Any]:
    return {
        "mode": "DRY_RUN",
        "broker_execution": False,
        "locked_config_hash": locked_config_hash(),
        "locked_version": LOCKED_VERSION,
        "session_date": None,
        "state": "NO_SETUP",
        "armed_setup": None,
        "open_position": None,
        "daily_trades": 0,
        "daily_losses": 0,
        "seen_setup_keys": [],
        "seen_triggers": [],
        "seen_trade_ids": [],
        "last_event": None,
    }


def load_runner_state() -> dict[str, Any]:
    ensure_journal_dir()
    if not RUNNER_STATE_PATH.exists():
        return default_runner_state()
    st = json.loads(RUNNER_STATE_PATH.read_text(encoding="utf-8"))
    base = default_runner_state()
    base.update(st or {})
    return base


def save_runner_state(state: dict[str, Any]) -> None:
    JOURNAL_DIR.mkdir(parents=True, exist_ok=True)
    RUNNER_STATE_PATH.write_text(json.dumps(state, indent=2, default=str), encoding="utf-8")


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            out.append(json.loads(line))
    return out


def load_paper_trades(path: Path = PAPER_TRADES_PATH) -> list[dict[str, Any]]:
    return load_jsonl(path)


def existing_ids(path: Path = PAPER_TRADES_PATH) -> set[str]:
    return {str(r.get("paper_trade_id")) for r in load_paper_trades(path) if r.get("paper_trade_id")}


def existing_setup_keys(path: Path = SETUPS_PATH) -> set[str]:
    return {str(r.get("setup_key")) for r in load_jsonl(path) if r.get("setup_key")}


def append_jsonl(path: Path, row: dict[str, Any]) -> None:
    ensure_journal_dir()
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, default=str) + "\n")


def append_paper_trade(trade: ESDVPForwardTrade, path: Path = PAPER_TRADES_PATH) -> bool:
    """Idempotent: same paper_trade_id is a no-op."""
    ensure_journal_dir()
    if trade.paper_trade_id in existing_ids(path):
        return False
    append_jsonl(path, trade.to_dict())
    return True


def append_setup_diagnostic(row: dict[str, Any], path: Path = SETUPS_PATH) -> bool:
    ensure_journal_dir()
    key = str(row.get("setup_key") or "")
    if not key or key in existing_setup_keys(path):
        return False
    append_jsonl(path, row)
    return True


def is_short(direction: str) -> bool:
    return str(direction).lower() in ("bearish", "short")


def parse_lock_ts(iso: str) -> float:
    raw = iso.replace("Z", "+00:00")
    dt = datetime.fromisoformat(raw)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def counts_toward_forward(setup_timestamp: Optional[int], lock_timestamp: str) -> bool:
    if setup_timestamp is None:
        return False
    return int(setup_timestamp) >= parse_lock_ts(lock_timestamp)


def cost_points(adverse_ticks: float) -> float:
    return 2.0 * float(adverse_ticks) * TICK + COMMISSION_POINTS


def overlay_net(raw_points: Optional[float], adverse_ticks: float) -> Optional[float]:
    if raw_points is None:
        return None
    return float(raw_points) - cost_points(adverse_ticks)


def fill_overlays(theoretical: float, direction: str) -> list[dict[str, Any]]:
    rows = []
    for name, ticks in (("IDEAL_TOUCH", 0.0), ("1_TICK_ADVERSE", 1.0), ("2_TICK_ADVERSE", 2.0)):
        delta = ticks * TICK
        fill = theoretical - delta if is_short(direction) else theoretical + delta
        rows.append(
            {
                "scenario": name,
                "ticks_adverse": ticks,
                "paper_fill_price": fill,
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
    return "STRONG_FORWARD_SAMPLE"


def campaign_status(resolved_n: int, *, defect: bool = False, data_blocked: bool = False) -> str:
    if defect:
        return "ES_DVP_IMPLEMENTATION_DEFECT_FOUND"
    if data_blocked and resolved_n == 0:
        return "ES_DVP_FORWARD_VALIDATION_BLOCKED"
    if resolved_n <= 0:
        return "ES_DVP_FORWARD_VALIDATION_READY"
    if resolved_n < 30:
        return "ES_DVP_FORWARD_VALIDATION_IN_PROGRESS"
    return "ES_DVP_FORWARD_VALIDATION_IN_PROGRESS"


def refuse_custom_strategy_params(**kwargs: Any) -> None:
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
    }
    bad = {k: v for k, v in kwargs.items() if k in banned and v is not None}
    if bad:
        raise ValueError(f"LOCKED_PARAM_REJECTED:{sorted(bad)}")


def recover_daily_state_from_journal(
    session_date: str,
    path: Path = PAPER_TRADES_PATH,
) -> dict[str, Any]:
    rows = [r for r in load_paper_trades(path) if r.get("session_date") == session_date]
    losses = 0
    for r in rows:
        pts = r.get("net_pnl_points")
        if r.get("exit_reason") in ("STOP", "STOP_HIT") or (pts is not None and float(pts) < 0):
            losses += 1
    return {
        "session_date": session_date,
        "daily_trade_count": len(rows),
        "daily_loss_count": losses,
        "hit_trade_cap": len(rows) >= LOCKED_CFG.max_trades_per_day,
        "hit_loss_cap": losses >= LOCKED_CFG.max_losses_per_day,
    }


def restore_runner_from_journal(lock_hash: str) -> dict[str, Any]:
    """Restart recovery: journal + runner_state, never duplicate a trade."""
    st = load_runner_state()
    if st.get("locked_config_hash") and st.get("locked_config_hash") != lock_hash:
        raise ValueError("RUNNER_HASH_MISMATCH")
    st["locked_config_hash"] = lock_hash
    st["broker_execution"] = False
    st["mode"] = "DRY_RUN"
    ids = existing_ids()
    seen = set(st.get("seen_trade_ids") or []) | ids
    st["seen_trade_ids"] = sorted(seen)
    setup_keys = existing_setup_keys()
    seen_setups = set(st.get("seen_setup_keys") or []) | setup_keys
    st["seen_setup_keys"] = sorted(seen_setups)
    if st.get("session_date"):
        daily = recover_daily_state_from_journal(str(st["session_date"]))
        st["daily_trades"] = max(int(st.get("daily_trades") or 0), int(daily["daily_trade_count"]))
        st["daily_losses"] = max(int(st.get("daily_losses") or 0), int(daily["daily_loss_count"]))
    save_runner_state(st)
    return st


def summarize_paper_journal(path: Path = PAPER_TRADES_PATH) -> dict[str, Any]:
    rows = load_paper_trades(path)
    resolved = [
        r
        for r in rows
        if r.get("exit_reason") in RESOLVED_ENTERED or r.get("state") in ("TARGET", "STOP", "FORCE_CLOSE")
    ]
    n = len(resolved)

    def _mean(xs):
        return None if not xs else sum(xs) / len(xs)

    nets = [float(r["net_pnl_points"]) for r in resolved if r.get("net_pnl_points") is not None]
    rs = [float(r["r_result"]) for r in resolved if r.get("r_result") is not None]
    wins = [x for x in nets if x > 0]
    losses = [abs(x) for x in nets if x <= 0]
    equity = peak = max_dd = 0.0
    streak = max_streak = 0
    holds = []
    for r in resolved:
        p = float(r.get("net_pnl_points") or 0.0)
        equity += p
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
        if p <= 0:
            streak += 1
            max_streak = max(max_streak, streak)
        else:
            streak = 0
        if r.get("entry_timestamp") and r.get("exit_timestamp"):
            holds.append(int(r["exit_timestamp"]) - int(r["entry_timestamp"]))
    stop_n = sum(1 for r in resolved if r.get("exit_reason") in ("STOP", "STOP_HIT"))
    tgt_n = sum(1 for r in resolved if r.get("exit_reason") in ("TARGET", "TARGET_HIT"))
    long_n = sum(1 for r in resolved if not is_short(str(r.get("direction") or "")))
    short_n = n - long_n
    pf = (sum(wins) / sum(losses)) if losses and sum(losses) > 0 else None
    return {
        "paper_trades": len(rows),
        "resolved": n,
        "forward_n": n,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": (len(wins) / n) if n else None,
        "expectancy_points": _mean(nets),
        "expectancy_r": _mean(rs),
        "profit_factor": pf,
        "max_drawdown_points": max_dd if n else None,
        "longest_losing_streak": max_streak if n else None,
        "stop_frequency": (stop_n / n) if n else None,
        "target_frequency": (tgt_n / n) if n else None,
        "avg_hold_sec": _mean(holds),
        "long_n": long_n,
        "short_n": short_n,
        "sample_label": sample_label(n),
        "progress": f"{n} / 30",
        "candidate_id": CANDIDATE_ID,
        "rows": rows,
    }
