"""Phase 53 — FundedNext Flex 50K DRY_RUN shadow pipeline. Never transmits orders."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from aitrade_operating_policy import load_operating_policy
from execution_status import BLOCKED_MODES
from macro_calendar import EVENTS_PATH, load_events
from nq_microstructure_models import FROZEN_GC_HASH, FROZEN_NQ_HASH
from bar_dataset import dataset_path, row_to_bar, validate_bars
from phase34_validate import GC_FILE_SHA, NQ_FILE_SHA, assert_frozen, file_sha256
from phase52_degradation import DegradationMonitor
from phase52_policy import (
    CONSISTENCY_RATIO,
    DAILY_STOP_FRAC,
    FAST_QTY,
    FROZEN_AVG_LOSS_R,
    FROZEN_AVG_WIN_R,
    FROZEN_E_R,
    FROZEN_WR,
    MAX_LOSS,
    MLL_LOCK_AT,
    NY,
    PROFIT_TARGET,
    SAFE_QTY,
    START_EQUITY,
    STOP_POINTS,
    TICK,
    chicago_session_id,
    evaluate_intent,
    remaining_drawdown,
    session_daily_stop_threshold,
)

ROOT = Path(__file__).resolve().parent
PROP_POLICY_PATH = ROOT / "config" / "aitrade_prop_execution_policy_v1.json"
AUTOMATION_CONFIRM_PATH = ROOT / "config" / "aitrade_phase53_fn_automation_confirmation.json"
JOURNAL_DIR = ROOT / "journal" / "phase53_fn_flex_shadow"
AUDIT_PATH = JOURNAL_DIR / "audit.jsonl"

UTC = timezone.utc
POINT_USD_MICRO = 2.0
COMM_RT_PER_MNQ = 0.40
ENTRY_SLIP_TICKS = 1.0
EXIT_SLIP_TICKS = 1.0
CALENDAR_STALE_DAYS = 45
EXPECTED_STRATEGY_ID = "NQ_DRIFT_VWAP_PULLBACK"
ACK_DELAY_LIMIT_SEC = 5.0
BALANCE_JUMP_USD = 2500.0

PHASE52_LOCK = {
    "fast_qty": 2,
    "safe_qty": 1,
    "max_qty": 2,
    "daily_stop_frac": 0.35,
    "near_rule": "PCT_95",
    "execution_mode": "DRY_RUN",
    "strategy_hash": FROZEN_NQ_HASH,
}


def _now_utc() -> datetime:
    return datetime.now(tz=UTC)


def policy_sha256() -> str:
    return file_sha256(PROP_POLICY_PATH)


def integrity_snapshot() -> dict[str, Any]:
    frozen = assert_frozen()
    pol = load_operating_policy()
    doc = json.loads(PROP_POLICY_PATH.read_text(encoding="utf-8"))
    auto = json.loads(AUTOMATION_CONFIRM_PATH.read_text(encoding="utf-8")) if AUTOMATION_CONFIRM_PATH.exists() else {}
    field_ok = (
        int(doc["position_sizing"]["fast_qty_mnq"]) == PHASE52_LOCK["fast_qty"]
        and int(doc["position_sizing"]["safe_qty_mnq"]) == PHASE52_LOCK["safe_qty"]
        and int(doc["position_sizing"]["max_qty_mnq"]) == PHASE52_LOCK["max_qty"]
        and float(doc["daily_governor"]["fraction"]) == PHASE52_LOCK["daily_stop_frac"]
        and str(doc["near_target"]["rule"]) == PHASE52_LOCK["near_rule"]
        and str(doc["execution_mode"]) == "DRY_RUN"
        and doc.get("broker_execution") is False
        and str(doc["strategy_hash"]) == FROZEN_NQ_HASH
    )
    journals = []
    for rel in (
        "journal/phase26_gc_vwap_v2_paper/paper_trades.jsonl",
        "journal/phase30_nq_dvp_paper/paper_trades.jsonl",
        "journal/phase47_es_dvp_paper/paper_trades.jsonl",
        "journal/phase31_nq_dvp_sim/executions.jsonl",
    ):
        p = ROOT / rel
        journals.append({"path": rel, "exists": p.exists(), "bytes": p.stat().st_size if p.exists() else None})
    return {
        "frozen": frozen,
        "gc": frozen.get("gc"),
        "nq": frozen.get("nq"),
        "gc_file_sha": file_sha256(ROOT / "strategy_frozen" / "gc_vwap_v2_phase26.json"),
        "nq_file_sha": file_sha256(ROOT / "strategy_frozen" / "nq_dvp_phase30.json"),
        "gc_file_sha_expected": GC_FILE_SHA,
        "nq_file_sha_expected": NQ_FILE_SHA,
        "policy_sha256": policy_sha256(),
        "policy_fields_match_phase52_lock": field_ok,
        "execution_default": pol.execution_default,
        "broker_execution": pol.broker_execution,
        "blocked_live_modes": sorted(BLOCKED_MODES),
        "automation_confirmation": {
            "path": str(AUTOMATION_CONFIRM_PATH.as_posix()),
            "automation_allowed": auto.get("automation_allowed"),
            "snapshot_date": auto.get("snapshot_date"),
            "source": auto.get("source"),
            "broker_execution": auto.get("broker_execution"),
        },
        "journals": journals,
        "live_eval_configured": False,
        "es_frozen_exists": (ROOT / "strategy_frozen" / "es_dvp_phase47.json").exists(),
    }


def freeze_verdict(snap: dict[str, Any]) -> Optional[str]:
    if not snap["frozen"].get("ok"):
        return "STOP_PHASE53_FREEZE_INTEGRITY_FAILURE"
    if snap["gc"] != FROZEN_GC_HASH or snap["nq"] != FROZEN_NQ_HASH:
        return "STOP_PHASE53_FREEZE_INTEGRITY_FAILURE"
    if snap["gc_file_sha"] != GC_FILE_SHA or snap["nq_file_sha"] != NQ_FILE_SHA:
        return "STOP_PHASE53_FREEZE_INTEGRITY_FAILURE"
    if snap["es_frozen_exists"]:
        return "STOP_PHASE53_FREEZE_INTEGRITY_FAILURE"
    return None


def policy_verdict(snap: dict[str, Any]) -> Optional[str]:
    if not snap.get("policy_fields_match_phase52_lock"):
        return "STOP_PHASE53_POLICY_INTEGRITY_FAILURE"
    if snap.get("execution_default") != "DRY_RUN" or snap.get("broker_execution"):
        return "STOP_PHASE53_POLICY_INTEGRITY_FAILURE"
    return None


def event_datetime(publication_date: str, release_local: str = "08:30") -> datetime:
    hh, mm = (release_local or "08:30").split(":")
    return datetime(
        int(publication_date[:4]),
        int(publication_date[5:7]),
        int(publication_date[8:10]),
        int(hh),
        int(mm),
        tzinfo=NY,
    )


def calendar_status_for(now: datetime, events: Optional[list] = None) -> tuple[str, Optional[datetime]]:
    if not EVENTS_PATH.exists() or EVENTS_PATH.stat().st_size == 0:
        return "MISSING", None
    evs = events if events is not None else load_events()
    if not evs:
        return "MISSING", None
    nearest = None
    best = None
    for e in evs:
        ts = event_datetime(e.publication_date, e.release_local or "08:30")
        d = abs((ts - now).total_seconds())
        if best is None or d < best:
            best = d
            nearest = ts
    last = event_datetime(evs[-1].publication_date, evs[-1].release_local or "08:30")
    if (now.date() - last.date()).days > CALENDAR_STALE_DAYS:
        return "STALE", nearest
    return "OK", nearest


def _slip_px(px: float, direction: str, ticks: float, *, worse_for: str) -> float:
    delta = float(ticks) * TICK
    long = direction.upper() in ("LONG", "BULLISH")
    if worse_for == "entry":
        return px + delta if long else px - delta
    return px - delta if long else px + delta


def simulate_fill(
    *,
    direction: str,
    theoretical_entry: float,
    theoretical_exit: Optional[float],
    outcome: str,
    qty: int,
    miss_entry: bool = False,
    extra_entry_ticks: float = 0.0,
    extra_exit_ticks: float = 0.0,
    partial_frac: float = 1.0,
) -> dict[str, Any]:
    if miss_entry or theoretical_exit is None:
        return {
            "filled": False,
            "reason": "MISSED_ENTRY" if miss_entry else "NO_EXIT",
            "qty_filled": 0,
            "entry_fill": None,
            "exit_fill": None,
            "entry_slippage_ticks": 0.0,
            "exit_slippage_ticks": 0.0,
            "round_trip_cost_usd": 0.0,
            "realized_points": None,
            "realized_R": None,
            "pnl_usd": 0.0,
        }
    q = max(0, int(round(int(qty) * float(partial_frac))))
    if q <= 0:
        return simulate_fill(
            direction=direction,
            theoretical_entry=theoretical_entry,
            theoretical_exit=theoretical_exit,
            outcome=outcome,
            qty=qty,
            miss_entry=True,
        )
    e_ticks = ENTRY_SLIP_TICKS + extra_entry_ticks
    x_ticks = EXIT_SLIP_TICKS + extra_exit_ticks
    entry = _slip_px(theoretical_entry, direction, e_ticks, worse_for="entry")
    exit_px = _slip_px(float(theoretical_exit), direction, x_ticks, worse_for="exit")
    long = direction.upper() in ("LONG", "BULLISH")
    pts = (exit_px - entry) if long else (entry - exit_px)
    comm = COMM_RT_PER_MNQ * q
    pnl = pts * POINT_USD_MICRO * q - comm
    return {
        "filled": True,
        "reason": outcome,
        "qty_filled": q,
        "entry_fill": entry,
        "exit_fill": exit_px,
        "entry_slippage_ticks": e_ticks,
        "exit_slippage_ticks": x_ticks,
        "round_trip_cost_usd": comm + (e_ticks + x_ticks) * TICK * POINT_USD_MICRO * q,
        "realized_points": pts,
        "realized_R": pts / STOP_POINTS,
        "pnl_usd": pnl,
        "partial_frac": partial_frac,
    }


@dataclass
class ShadowAccount:
    equity: float = START_EQUITY
    mll: float = START_EQUITY - MAX_LOSS
    mll_locked: bool = False
    realized_pnl: float = 0.0
    unrealized_pnl: float = 0.0
    state: str = "EVAL_FAST"
    session_id: str = ""
    session_open_equity: float = START_EQUITY
    remaining_dd_open: float = MAX_LOSS
    daily_stopped: bool = False
    demoted: bool = False
    consecutive_losses: int = 0
    last_qty: int = FAST_QTY
    open_qty: int = 0
    trading_days: int = 0
    traded_dates: set = field(default_factory=set)
    best_day: float = 0.0
    day_pnl: float = 0.0
    daily_stop_count: int = 0
    demote_count: int = 0
    near_count: int = 0
    accepted: int = 0
    rejected: int = 0
    trades_filled: int = 0
    breach_attempts_prevented: int = 0
    orders_transmitted: int = 0
    last_client_order_id: Optional[str] = None
    monitor: DegradationMonitor = field(default_factory=DegradationMonitor)
    peak_equity: float = START_EQUITY
    max_dd: float = 0.0
    lowest_remaining_dd: float = MAX_LOSS
    last_trade_ts: Optional[datetime] = None
    overnight_violation: bool = False
    kill_log: list = field(default_factory=list)

    def remaining_dd(self) -> float:
        return remaining_drawdown(self.equity, self.mll)

    def remaining_profit(self) -> float:
        return PROFIT_TARGET - (self.equity - START_EQUITY)

    def maybe_new_session(self, now: datetime) -> None:
        sid = chicago_session_id(now)
        if sid != self.session_id:
            if self.open_qty > 0:
                # FundedNext forbids overnight/weekend. Fail closed; never transmit.
                self.open_qty = 0
                self.unrealized_pnl = 0.0
                self.overnight_violation = True
                self.state = "PAUSED"
                self.kill_log.append("OVERNIGHT_POSITION_AT_SESSION_RESET")
            if self.session_id:
                self._eod_trail()
            self.session_id = sid
            self.session_open_equity = self.equity
            self.remaining_dd_open = max(0.0, self.remaining_dd())
            self.daily_stopped = False
            self.day_pnl = 0.0
            if self.state == "EVAL_DAILY_STOPPED":
                self.state = "EVAL_PROTECTED" if self.demoted else "EVAL_FAST"
            if self.overnight_violation:
                self.state = "PAUSED"

    def _eod_trail(self) -> None:
        if self.day_pnl:
            self.best_day = max(self.best_day, self.day_pnl)
        if not self.mll_locked:
            cand = self.equity - MAX_LOSS
            if cand > self.mll:
                self.mll = cand
            if self.mll >= MLL_LOCK_AT - 1e-12:
                self.mll = MLL_LOCK_AT
                self.mll_locked = True
        rem = self.remaining_dd()
        self.lowest_remaining_dd = min(self.lowest_remaining_dd, rem)
        dd = self.peak_equity - self.equity
        if dd > self.max_dd:
            self.max_dd = dd

    def apply_fill(self, fill: dict[str, Any], trading_date: str, now: datetime) -> None:
        pnl = float(fill["pnl_usd"])
        self.equity += pnl
        self.realized_pnl += pnl
        self.day_pnl += pnl
        self.open_qty = 0
        self.unrealized_pnl = 0.0
        self.last_qty = int(fill["qty_filled"])
        self.last_trade_ts = now
        self.traded_dates.add(trading_date)
        self.trading_days = len(self.traded_dates)
        self.trades_filled += 1
        if pnl < 0:
            self.consecutive_losses += 1
        else:
            self.consecutive_losses = 0
        if self.equity > self.peak_equity:
            self.peak_equity = self.equity
        rem = self.remaining_dd()
        self.lowest_remaining_dd = min(self.lowest_remaining_dd, rem)
        if rem <= 0:
            self.state = "EVAL_BREACHED"
        thr = session_daily_stop_threshold(self.remaining_dd_open)
        daily_loss = self.session_open_equity - self.equity
        if thr > 0 and daily_loss + 1e-9 >= thr:
            self.daily_stopped = True
            self.daily_stop_count += 1
            if self.state not in ("EVAL_BREACHED", "EVAL_PASSED", "PAUSED"):
                self.state = "EVAL_DAILY_STOPPED"
        r = fill.get("realized_R")
        if r is not None:
            info = self.monitor.observe(float(r))
            if info["demoted"] and not self.demoted:
                self.demoted = True
                self.demote_count += 1
                if self.state == "EVAL_FAST":
                    self.state = "EVAL_PROTECTED"
        if self.remaining_profit() <= 0.05 * PROFIT_TARGET + 1e-9 and self.state not in (
            "EVAL_BREACHED",
            "EVAL_PASSED",
            "PAUSED",
            "EVAL_DAILY_STOPPED",
        ):
            if self.state != "EVAL_NEAR_TARGET":
                self.near_count += 1
            self.state = "EVAL_NEAR_TARGET"
        adj = max(PROFIT_TARGET, (self.best_day / CONSISTENCY_RATIO) if self.best_day > 0 else PROFIT_TARGET)
        if (self.equity - START_EQUITY) + 1e-9 >= adj:
            self.state = "EVAL_PASSED"

    def snapshot(self) -> dict[str, Any]:
        return {
            "equity": self.equity,
            "mll": self.mll,
            "remaining_dd": self.remaining_dd(),
            "remaining_profit": self.remaining_profit(),
            "state": self.state,
            "daily_stopped": self.daily_stopped,
            "demoted": self.demoted,
            "trading_days": self.trading_days,
            "accepted": self.accepted,
            "rejected": self.rejected,
            "trades_filled": self.trades_filled,
            "orders_transmitted": self.orders_transmitted,
            "max_dd": self.max_dd,
            "lowest_remaining_dd": self.lowest_remaining_dd,
        }


def reset_audit() -> None:
    JOURNAL_DIR.mkdir(parents=True, exist_ok=True)
    AUDIT_PATH.write_text("", encoding="utf-8")


def append_audit(row: dict[str, Any]) -> None:
    JOURNAL_DIR.mkdir(parents=True, exist_ok=True)
    if not AUDIT_PATH.exists():
        AUDIT_PATH.write_text("", encoding="utf-8")
    with AUDIT_PATH.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, default=str) + "\n")


def inspect_bar_stream(bars, *, symbol: str = "NQ", timeframe: str = "1m", expected_period_sec: int = 60) -> dict[str, Any]:
    return validate_bars(list(bars), symbol=symbol, timeframe=timeframe, expected_period_sec=expected_period_sec)


def _read_jsonl_since(path: Path, min_ts: int) -> list:
    if not path.exists() or path.stat().st_size == 0:
        return []
    size = path.stat().st_size
    chunk = 32 * 1024 * 1024
    offset = max(0, size - chunk)
    while True:
        rows = []
        first_t = None
        with path.open("r", encoding="utf-8") as fh:
            fh.seek(offset)
            if offset > 0:
                fh.readline()
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                obj = json.loads(line)
                t = int(obj["time"])
                if first_t is None:
                    first_t = t
                if t >= min_ts:
                    rows.append(row_to_bar(obj))
        if offset == 0 or (first_t is not None and first_t <= min_ts):
            return rows
        offset = max(0, offset - chunk)


def load_recent_stitched_nq(*, min_ts: int) -> dict[str, Any]:
    """Same Databento stitched NQ files as production; last window only."""
    root = ROOT / "data" / "databento" / "NQ" / "stitched"
    p1 = dataset_path("databento_NQ_stitched", "1m", root=root)
    if not p1.exists():
        return {"ok": False, "error_code": "SIGNAL_DATA_UNAVAILABLE", "source": None}
    bars_1m = _read_jsonl_since(p1, min_ts)
    bars_5m = _read_jsonl_since(dataset_path("databento_NQ_stitched", "5m", root=root), min_ts)
    bars_15m = _read_jsonl_since(dataset_path("databento_NQ_stitched", "15m", root=root), min_ts)
    if not bars_1m:
        return {"ok": False, "error_code": "SIGNAL_DATA_UNAVAILABLE", "source": str(p1)}
    if not bars_5m:
        from nq_databento import aggregate_1m_to_ny

        bars_5m = aggregate_1m_to_ny(bars_1m, 5)
    if not bars_15m:
        from nq_databento import aggregate_1m_to_ny

        bars_15m = aggregate_1m_to_ny(bars_1m, 15)
    integ = inspect_bar_stream(bars_1m, expected_period_sec=60)
    return {
        "ok": True,
        "source": "databento:GLBX.MDP3:NQ_stitched",
        "bars_1m": bars_1m,
        "bars_5m": bars_5m,
        "bars_15m": bars_15m,
        "integrity": {
            "duplicate_count": integ.get("duplicate_count"),
            "ohlc_invalid_count": integ.get("ohlc_invalid_count"),
            "sorted": integ.get("sorted"),
            "gap_count": integ.get("gap_count"),
            "ok": integ.get("ok"),
        },
        "note": "Most-recent stitched NQ window — same files as nq_dvp_live_runner.load_nq_signal_bars",
    }


def process_signal(
    acct: ShadowAccount,
    *,
    signal: dict[str, Any],
    now: datetime,
    calendar_status: str,
    event_ts: Optional[datetime],
    data_age_sec: float = 1.0,
    broker_ok: bool = True,
    requested_qty: Optional[int] = None,
    strategy_hash: str = FROZEN_NQ_HASH,
    duplicate: bool = False,
    position_known: bool = True,
    order_known: bool = True,
    equity_override: Any = "USE_ACCOUNT",
    miss_entry: bool = False,
    extra_entry_ticks: float = 0.0,
    extra_exit_ticks: float = 0.0,
    partial_frac: float = 1.0,
    ingest_ts: Optional[datetime] = None,
    mll_override: Any = "USE_ACCOUNT",
    strategy_id: str = EXPECTED_STRATEGY_ID,
    data_fault: Optional[str] = None,
    ack_fault: Optional[str] = None,
    position_fault: Optional[str] = None,
    signal_corrupt: bool = False,
    balance_jump: bool = False,
    pnl_mismatch: bool = False,
    impossible_mll: bool = False,
    ack_delay_sec: float = 0.0,
) -> dict[str, Any]:
    acct.maybe_new_session(now)

    def _fail(code: str, *, pause: bool = True, frozen: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        acct.rejected += 1
        acct.kill_log.append(code)
        if pause:
            acct.state = "PAUSED"
        row = {
            "market_timestamp": now.isoformat(),
            "ingestion_timestamp": (ingest_ts or _now_utc()).isoformat(),
            "strategy_id": strategy_id,
            "strategy_hash": strategy_hash,
            "account_state": acct.state,
            "policy_state": acct.state,
            "signal": frozen or {"signal_mutated": False},
            "quantity_request": 0,
            "quantity_allowed": 0,
            "accepted": False,
            "rejection_reason": code,
            "rule_checks": [code],
            "news_state": calendar_status,
            "kill_switch": code,
            "orders_transmitted": 0,
            "client_order_id": None,
        }
        append_audit(row)
        return {**row, "decision": None, "fill": None, "account": acct.snapshot()}

    if acct.overnight_violation or acct.state == "PAUSED":
        return _fail("OVERNIGHT_POSITION_AT_SESSION_RESET" if acct.overnight_violation else "STRATEGY_HASH_MISMATCH")
    if strategy_id != EXPECTED_STRATEGY_ID:
        return _fail("STRATEGY_HASH_MISMATCH")
    if signal_corrupt:
        return _fail("SIGNAL_PAYLOAD_CORRUPT")
    if data_fault in ("MISSING_CANDLE", "DUPLICATE_CANDLE", "OOO_TIMESTAMP"):
        return _fail("LIVE_DATA_STALE")
    if ack_fault == "DELAYED_ACK" or float(ack_delay_sec) > ACK_DELAY_LIMIT_SEC:
        return _fail("ORDER_STATE_MISMATCH")
    if ack_fault == "ORDER_REJECTED":
        return _fail("BROKER_CONNECTION_UNSTABLE")
    if ack_fault == "DUPLICATE_ACK":
        return _fail("DUPLICATE_ORDER_DETECTED", pause=False)
    if position_fault in ("UNEXPECTED_OPEN", "EXPECTED_MISSING"):
        return _fail("POSITION_STATE_MISMATCH")
    if balance_jump:
        return _fail("ACCOUNT_EQUITY_UNKNOWN")
    if pnl_mismatch:
        return _fail("DRAW_DOWN_CALCULATION_INVALID")
    if impossible_mll:
        return _fail("DRAW_DOWN_CALCULATION_INVALID")

    try:
        direction = str(signal.get("direction") or "")
        theo_entry = float(signal["entry_price"])
        theo_stop = float(signal["stop_price"])
        theo_tgt = float(signal["target_price"])
        if direction not in ("LONG", "SHORT", "bullish", "bearish"):
            return _fail("SIGNAL_PAYLOAD_CORRUPT")
    except (TypeError, ValueError, KeyError):
        return _fail("SIGNAL_PAYLOAD_CORRUPT")

    expected_r = signal.get("r_multiple")
    req = FAST_QTY if requested_qty is None else int(requested_qty)
    if acct.demoted or acct.state in ("EVAL_SAFE", "EVAL_PROTECTED", "EVAL_NEAR_TARGET"):
        req = min(req, SAFE_QTY)
    cid = f"{signal.get('trading_date')}|{signal.get('entry_timestamp')}|{direction}"
    dup = duplicate or (acct.last_client_order_id == cid)
    eq = acct.equity if equity_override == "USE_ACCOUNT" else equity_override
    mll = acct.mll if mll_override == "USE_ACCOUNT" else mll_override
    if mll is not None and mll != "USE_ACCOUNT":
        try:
            if float(mll) > MLL_LOCK_AT + 1e-6:
                return _fail("DRAW_DOWN_CALCULATION_INVALID")
            if float(mll) < 0:
                return _fail("DRAW_DOWN_CALCULATION_INVALID")
        except (TypeError, ValueError):
            return _fail("DRAW_DOWN_CALCULATION_INVALID")
    if (
        equity_override != "USE_ACCOUNT"
        and equity_override is not None
        and abs(float(equity_override) - acct.equity) > BALANCE_JUMP_USD
        and mll_override == "USE_ACCOUNT"
    ):
        return _fail("ACCOUNT_EQUITY_UNKNOWN")

    decision = evaluate_intent(
        state=acct.state,
        intent_qty=req,
        action="NEW_ENTRY",
        now=now,
        equity=eq,
        mll=mll,
        session_open_equity=acct.session_open_equity,
        remaining_dd_open=acct.remaining_dd_open,
        realized_pnl=acct.realized_pnl,
        open_pnl=acct.unrealized_pnl,
        open_qty=acct.open_qty,
        last_qty=acct.last_qty,
        consecutive_losses=acct.consecutive_losses,
        demoted=acct.demoted,
        strategy_hash=strategy_hash,
        calendar_status=calendar_status,
        event_ts=event_ts,
        data_age_sec=data_age_sec,
        broker_ok=broker_ok,
        position_known=position_known,
        order_known=order_known,
        duplicate=dup,
        daily_already_stopped=acct.daily_stopped,
        near_rule="PCT_95",
    )
    frozen_body = {
        "trading_date": signal.get("trading_date"),
        "direction": direction,
        "entry_timestamp": signal.get("entry_timestamp"),
        "intended_entry": theo_entry,
        "stop": theo_stop,
        "target": theo_tgt,
        "expected_R": expected_r,
        "signal_mutated": False,
    }
    row: dict[str, Any] = {
        "market_timestamp": now.isoformat(),
        "ingestion_timestamp": (ingest_ts or _now_utc()).isoformat(),
        "strategy_id": strategy_id,
        "strategy_hash": strategy_hash,
        "account_state": acct.state,
        "policy_state": decision.state,
        "signal": frozen_body,
        "quantity_request": req,
        "quantity_allowed": decision.allowed_qty,
        "accepted": decision.verdict == "ALLOW",
        "rejection_reason": None if decision.verdict == "ALLOW" else decision.code,
        "rule_checks": decision.reasons,
        "news_state": calendar_status,
        "kill_switch": None if decision.verdict == "ALLOW" else decision.code,
        "orders_transmitted": 0,
        "client_order_id": cid,
    }
    if decision.verdict != "ALLOW":
        acct.rejected += 1
        acct.kill_log.append(decision.code)
        if decision.code in ("ACCOUNT_BREACH_IMMINENT", "BLOCK_QTY_3MNQ_REJECTED", "DAILY_STOP_TRIGGERED", "MAX_POSITION_EXCEEDED"):
            acct.breach_attempts_prevented += 1
        if decision.state == "PAUSED":
            acct.state = "PAUSED"
        append_audit(row)
        return {**row, "decision": decision.to_dict(), "fill": None, "account": acct.snapshot()}

    fill = simulate_fill(
        direction=direction,
        theoretical_entry=theo_entry,
        theoretical_exit=signal.get("exit_price"),
        outcome=str(signal.get("outcome") or "UNKNOWN"),
        qty=decision.allowed_qty,
        miss_entry=miss_entry,
        extra_entry_ticks=extra_entry_ticks,
        extra_exit_ticks=extra_exit_ticks,
        partial_frac=partial_frac,
    )
    fill["expected_R"] = expected_r
    acct.accepted += 1
    acct.last_client_order_id = cid
    if fill["filled"]:
        acct.apply_fill(fill, str(signal.get("trading_date")), now)
    else:
        row["rejection_reason"] = fill["reason"]
        row["accepted"] = False
        acct.accepted -= 1
        acct.rejected += 1
    row["simulated_execution"] = fill
    row["resulting_pnl"] = fill.get("pnl_usd")
    row["account_equity"] = acct.equity
    row["remaining_dd"] = acct.remaining_dd()
    row["state_after"] = acct.state
    append_audit(row)
    return {**row, "decision": decision.to_dict(), "fill": fill, "account": acct.snapshot()}


def classify_health(rs: list[float], *, flip_pct: Optional[float], flip_n: int) -> dict[str, Any]:
    n = len(rs)
    wr = (sum(1 for x in rs if x > 0) / n) if n else None
    er = (sum(rs) / n) if n else None
    wins = [x for x in rs if x > 0]
    losses = [x for x in rs if x <= 0]
    avg_w = (sum(wins) / len(wins)) if wins else None
    avg_l = (sum(losses) / len(losses)) if losses else None
    demoted = False
    if n >= 20:
        mon = DegradationMonitor()
        info: dict[str, Any] = {}
        for r in rs:
            info = mon.observe(r)
        demoted = bool(info.get("demoted"))
        destructive_flip = flip_pct is not None and flip_n >= 20 and float(flip_pct) >= 0.10
        wr_collapse = wr is not None and wr <= FROZEN_WR - 0.18
        if wr_collapse or destructive_flip:
            cls = "DEGRADED"
        elif demoted or (wr is not None and wr < FROZEN_WR - 0.08) or (er is not None and er < 0.5 * FROZEN_E_R) or (
            flip_pct is not None and flip_n >= 15 and float(flip_pct) >= 0.08
        ):
            cls = "WATCH"
        else:
            cls = "HEALTHY"
    else:
        cls = "INSUFFICIENT_SAMPLE"
    return {
        "class": cls,
        "n": n,
        "wr": wr,
        "er": er,
        "avg_win": avg_w,
        "avg_loss": avg_l,
        "flip_pct": flip_pct,
        "flip_n": flip_n,
        "frozen_wr": FROZEN_WR,
        "frozen_er": FROZEN_E_R,
        "frozen_avg_win": FROZEN_AVG_WIN_R,
        "frozen_avg_loss": FROZEN_AVG_LOSS_R,
        "monitor_demoted": demoted,
    }
