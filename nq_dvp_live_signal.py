"""Phase 31 — pure NQ DVP live signal state (frozen rules; no broker I/O)."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Optional, Sequence
from zoneinfo import ZoneInfo

from models import Bar
from nq_drift_vwap_engine import (
    classify_drift_15m,
    compute_session_vwap_by_ts,
    session_anchors,
    vwap_at_or_before,
)
from nq_drift_vwap_models import (
    FORCE_CLOSE_LOCAL,
    NO_NEW_TRADES_AFTER_LOCAL,
    OR_TIMEZONE,
    TRADE_START_LOCAL,
    VWAP_RESET_LOCAL,
    DVPStrategyConfig,
)
from nq_dvp_freeze import load_frozen_strategy_config

NY = ZoneInfo(OR_TIMEZONE)

# Max age for a completed 5m bar before STALE_DATA_BLOCK (2.5 bars)
STALE_5M_SECONDS = 5 * 60 * 2 + 30


@dataclass
class DVPSignalSnapshot:
    trading_date: str
    state: str
    et_time: str
    vwap: Optional[float]
    previous_15m_vwap: Optional[float]
    current_15m_close: Optional[float]
    hour_return: Optional[float]
    drift: Optional[str]
    pullback_ready: bool
    pending_entry: Optional[dict[str, Any]]
    intended_order: Optional[dict[str, Any]]
    daily_trades: int
    daily_losses: int
    blocked_reason: Optional[str] = None
    data_freshness_sec: Optional[float] = None
    last_5m_ts: Optional[int] = None
    last_15m_ts: Optional[int] = None
    extras: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def load_frozen_cfg() -> DVPStrategyConfig:
    return load_frozen_strategy_config()


def _et_now_iso() -> str:
    return datetime.now(tz=NY).isoformat()


def evaluate_completed_bars(
    *,
    bars_1m: Sequence[Bar],
    bars_5m: Sequence[Bar],
    bars_15m: Sequence[Bar],
    trading_date: str,
    cfg: Optional[DVPStrategyConfig] = None,
    daily_trades: int = 0,
    daily_losses: int = 0,
    seen_triggers: Optional[set[str]] = None,
    now_ts: Optional[int] = None,
) -> DVPSignalSnapshot:
    """
    Evaluate frozen DVP signal from completed bars only.
    Emits at most one intended next-bar entry (does not place orders).
    """
    cfg = cfg or load_frozen_cfg()
    seen = seen_triggers or set()
    anchors = session_anchors(trading_date)
    now = int(now_ts if now_ts is not None else datetime.now(tz=NY).timestamp())

    vwap_map = compute_session_vwap_by_ts(bars_1m, trading_date)
    vwap_keys = sorted(vwap_map)
    b15 = [b for b in bars_15m if int(b.time) >= anchors["vwap_reset"]]
    b5 = [b for b in bars_5m if int(b.time) >= anchors["vwap_reset"]]

    state = "WAITING_FOR_SESSION"
    if now < anchors["vwap_reset"]:
        state = "WAITING_FOR_SESSION"
    elif now < anchors["trade_start"]:
        state = "WAITING_FOR_1030" if vwap_map else "VWAP_BUILDING"
    elif now >= anchors["force_close"]:
        state = "SESSION_CLOSED"
    elif daily_losses >= cfg.max_losses_per_day:
        state = "DAILY_LOSS_CAP"
    elif daily_trades >= cfg.max_trades_per_day:
        state = "DAILY_TRADE_CAP"
    else:
        state = "WAITING_FOR_DRIFT"

    drift = None
    vwap_now = vwap_prev = close_15 = hour_ret = None
    last_15 = None
    if b15:
        idx = len(b15) - 1
        last_15 = b15[idx]
        drift = classify_drift_15m(
            b15, idx, vwap_map, threshold=cfg.hour_return_threshold, vwap_keys=vwap_keys
        )
        close_15 = float(last_15.close)
        vwap_now = vwap_at_or_before(vwap_map, int(last_15.time) + 14 * 60, vwap_keys)
        if idx >= 1:
            prev = b15[idx - 1]
            vwap_prev = vwap_at_or_before(vwap_map, int(prev.time) + 14 * 60, vwap_keys)
        if idx >= 4 and float(b15[idx - 4].close) != 0:
            hour_ret = close_15 / float(b15[idx - 4].close) - 1.0
        if drift == "POSITIVE_DRIFT" and state == "WAITING_FOR_DRIFT":
            state = "POSITIVE_DRIFT"
        elif drift == "NEGATIVE_DRIFT" and state == "WAITING_FOR_DRIFT":
            state = "NEGATIVE_DRIFT"

    pullback_ready = False
    pending = None
    intended = None
    blocked = None
    last_5_ts = int(b5[-1].time) if b5 else None
    freshness = None if last_5_ts is None else max(0, now - (last_5_ts + 300))

    if state in ("POSITIVE_DRIFT", "NEGATIVE_DRIFT", "WAITING_FOR_PULLBACK") and b5:
        # Scan for first eligible pullback after trade_start with room for next bar
        for i, bar in enumerate(b5[:-1] if len(b5) >= 1 else []):
            t = int(bar.time)
            bar_end = t + 300
            if bar_end < anchors["trade_start"]:
                continue
            if t >= anchors["no_new"]:
                break
            # drift at bar end: use latest 15m completed at or before bar_end
            local_drift = drift
            # Prefer classifying from 15m bars whose end <= bar_end
            for j in range(len(b15) - 1, -1, -1):
                if int(b15[j].time) + 15 * 60 <= bar_end:
                    local_drift = classify_drift_15m(
                        b15, j, vwap_map, threshold=cfg.hour_return_threshold, vwap_keys=vwap_keys
                    )
                    break
            red = float(bar.close) < float(bar.open)
            green = float(bar.close) > float(bar.open)
            setup = (local_drift == "POSITIVE_DRIFT" and red) or (
                local_drift == "NEGATIVE_DRIFT" and green
            )
            if not setup:
                continue
            trigger_key = f"{trading_date}|{t}|{local_drift}"
            if trigger_key in seen:
                continue
            nxt = b5[i + 1] if i + 1 < len(b5) else None
            direction = "LONG" if local_drift == "POSITIVE_DRIFT" else "SHORT"
            pullback_ready = True
            state = "WAITING_FOR_PULLBACK"
            pending = {
                "direction": direction,
                "drift": local_drift,
                "trigger_ts": t,
                "trigger_key": trigger_key,
                "entry_bar_open_ts": t + 300,
            }
            if nxt is not None and int(nxt.time) == t + 300:
                entry_ts = int(nxt.time)
                if entry_ts >= anchors["no_new"]:
                    blocked = "PAST_CUTOFF"
                    break
                if freshness is not None and freshness > STALE_5M_SECONDS and entry_ts + 300 < now - STALE_5M_SECONDS:
                    # only stale-block if we're trying to act on a late signal relative to now
                    pass
                action = "BUY" if direction == "LONG" else "SELL"
                stop_pts = cfg.long_stop_points if direction == "LONG" else cfg.short_stop_points
                tgt_pts = cfg.long_target_points if direction == "LONG" else cfg.short_target_points
                # Theoretical levels from next-bar open (signal price); execution fill may differ
                entry_px = float(nxt.open)
                if direction == "LONG":
                    stop = entry_px - stop_pts
                    target = entry_px + tgt_pts
                else:
                    stop = entry_px + stop_pts
                    target = entry_px - tgt_pts
                intended = {
                    "direction": direction,
                    "action": action,
                    "entry_timestamp": entry_ts,
                    "signal_entry_price": entry_px,
                    "stop_points": stop_pts,
                    "target_points": tgt_pts,
                    "theoretical_stop": stop,
                    "theoretical_target": target,
                    "trigger_ts": t,
                    "trigger_key": trigger_key,
                    "trade_id": f"AITRADE_DVP_{trading_date}_{direction}_{t}",
                }
                state = "ENTRY_PENDING_NEXT_BAR"
            break

    if state == "WAITING_FOR_DRIFT" and drift in ("POSITIVE_DRIFT", "NEGATIVE_DRIFT"):
        state = drift

    # Stale data block for actionable pending entries near "now"
    if intended and freshness is not None and freshness > STALE_5M_SECONDS:
        # If the entry bar is already far in the past relative to wall clock, block
        if now - (int(intended["entry_timestamp"]) + 60) > STALE_5M_SECONDS:
            blocked = "STALE_DATA_BLOCK"
            intended = None
            state = "ERROR_SAFE_HALT" if state == "ENTRY_PENDING_NEXT_BAR" else state

    if now >= anchors["no_new"] and state not in (
        "SESSION_CLOSED",
        "POSITION_OPEN",
        "DAILY_LOSS_CAP",
        "DAILY_TRADE_CAP",
    ):
        if state not in ("TARGET_HIT", "STOP_HIT", "TIME_EXIT"):
            if now >= anchors["force_close"]:
                state = "SESSION_CLOSED"

    return DVPSignalSnapshot(
        trading_date=trading_date,
        state=state,
        et_time=_et_now_iso(),
        vwap=vwap_now,
        previous_15m_vwap=vwap_prev,
        current_15m_close=close_15,
        hour_return=hour_ret,
        drift=drift,
        pullback_ready=pullback_ready,
        pending_entry=pending,
        intended_order=intended,
        daily_trades=daily_trades,
        daily_losses=daily_losses,
        blocked_reason=blocked,
        data_freshness_sec=freshness,
        last_5m_ts=last_5_ts,
        last_15m_ts=None if last_15 is None else int(last_15.time),
        extras={
            "vwap_reset": VWAP_RESET_LOCAL,
            "trade_start": TRADE_START_LOCAL,
            "no_new_after": NO_NEW_TRADES_AFTER_LOCAL,
            "force_close": FORCE_CLOSE_LOCAL,
            "anchors": anchors,
        },
    )


def extract_signal_entries_for_day(
    *,
    bars_1m: Sequence[Bar],
    bars_5m: Sequence[Bar],
    bars_15m: Sequence[Bar],
    trading_date: str,
    cfg: Optional[DVPStrategyConfig] = None,
) -> list[dict[str, Any]]:
    """Walk a day identically to replay_dvp_day entry selection (incl. loss/trade caps)."""
    from nq_drift_vwap_engine import resolve_exit_1m

    cfg = cfg or load_frozen_cfg()
    anchors = session_anchors(trading_date)
    vwap_map = compute_session_vwap_by_ts(bars_1m, trading_date)
    vwap_keys = sorted(vwap_map)
    b15 = [b for b in bars_15m if int(b.time) >= anchors["vwap_reset"]]
    b5 = [b for b in bars_5m if int(b.time) >= anchors["vwap_reset"]]
    b5_by_ts = {int(b.time): i for i, b in enumerate(b5)}

    drift_by_15_end: dict[int, Optional[str]] = {}
    for i, bar in enumerate(b15):
        drift_by_15_end[int(bar.time) + 15 * 60] = classify_drift_15m(
            b15, i, vwap_map, threshold=cfg.hour_return_threshold, vwap_keys=vwap_keys
        )
    drift_ends = sorted(drift_by_15_end)

    def latest_drift_at(ts: int) -> Optional[str]:
        lo, hi = 0, len(drift_ends) - 1
        best = None
        while lo <= hi:
            mid = (lo + hi) // 2
            if drift_ends[mid] <= ts:
                best = drift_ends[mid]
                lo = mid + 1
            else:
                hi = mid - 1
        return None if best is None else drift_by_15_end[best]

    out: list[dict[str, Any]] = []
    daily_trades = 0
    daily_losses = 0
    i = 0
    while i < len(b5) - 1:
        bar = b5[i]
        t = int(bar.time)
        bar_end = t + 300
        if bar_end < anchors["trade_start"]:
            i += 1
            continue
        if t >= anchors["no_new"]:
            break
        if daily_trades >= cfg.max_trades_per_day or daily_losses >= cfg.max_losses_per_day:
            i += 1
            continue
        drift = latest_drift_at(bar_end)
        red = float(bar.close) < float(bar.open)
        green = float(bar.close) > float(bar.open)
        setup = (drift == "POSITIVE_DRIFT" and red) or (drift == "NEGATIVE_DRIFT" and green)
        if not setup:
            i += 1
            continue
        nxt = b5[i + 1]
        entry_ts = int(nxt.time)
        if entry_ts >= anchors["no_new"] or entry_ts >= anchors["force_close"]:
            i += 1
            continue
        direction = "LONG" if drift == "POSITIVE_DRIFT" else "SHORT"
        hist_dir = "bullish" if direction == "LONG" else "bearish"
        entry = float(nxt.open)
        if direction == "LONG":
            stop = entry - cfg.long_stop_points
            target = entry + cfg.long_target_points
        else:
            stop = entry + cfg.short_stop_points
            target = entry - cfg.short_target_points
        daily_trades += 1
        exit_info = resolve_exit_1m(
            bars_1m=bars_1m,
            entry_ts=entry_ts,
            direction=hist_dir,
            stop=stop,
            target=target,
            force_close_ts=anchors["force_close"],
        )
        pts = None
        if exit_info.get("exit_price") is not None:
            pts = (
                float(exit_info["exit_price"]) - entry
                if direction == "LONG"
                else entry - float(exit_info["exit_price"])
            )
        outcome = exit_info["outcome"]
        if outcome == "STOP_HIT" or (outcome == "FORCE_CLOSE" and pts is not None and pts < 0):
            daily_losses += 1
        out.append(
            {
                "trading_date": trading_date,
                "direction": direction,
                "drift": drift,
                "trigger_ts": t,
                "entry_timestamp": entry_ts,
                "entry_price": entry,
                "stop_price": stop,
                "target_price": target,
            }
        )
        i = b5_by_ts.get(entry_ts, i) + 1
    return out
