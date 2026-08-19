"""Phase 29 — Drift VWAP Pullback engine (exact DVP_ORIGINAL replication)."""

from __future__ import annotations

import hashlib
from collections import defaultdict
from datetime import date, datetime
from typing import Any, Optional, Sequence
from zoneinfo import ZoneInfo

from models import Bar
from nq_drift_vwap_models import (
    FORCE_CLOSE_LOCAL,
    NO_NEW_TRADES_AFTER_LOCAL,
    OR_TIMEZONE,
    TRADE_START_LOCAL,
    VWAP_BASIS_STATUS,
    VWAP_PRICE_BASIS,
    VWAP_RESET_LOCAL,
    DVPStrategyConfig,
    DVPTrade,
    DVP_ORIGINAL,
)

NY = ZoneInfo(OR_TIMEZONE)


def config_hash(cfg: DVPStrategyConfig = DVP_ORIGINAL) -> str:
    raw = "|".join(
        [
            cfg.strategy_family,
            cfg.candidate_id,
            str(cfg.hour_return_threshold),
            str(cfg.long_stop_points),
            str(cfg.long_target_points),
            str(cfg.short_stop_points),
            str(cfg.short_target_points),
            str(cfg.max_trades_per_day),
            str(cfg.max_losses_per_day),
        ]
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _local_ts(trading_date: str, hhmm: str) -> int:
    d = date.fromisoformat(trading_date)
    hh, mm = map(int, hhmm.split(":"))
    return int(datetime(d.year, d.month, d.day, hh, mm, tzinfo=NY).timestamp())


def session_anchors(
    trading_date: str,
    *,
    vwap_reset: Optional[str] = None,
    trade_start: Optional[str] = None,
    no_new: Optional[str] = None,
    force_close: Optional[str] = None,
) -> dict[str, int]:
    return {
        "vwap_reset": _local_ts(trading_date, vwap_reset or VWAP_RESET_LOCAL),
        "trade_start": _local_ts(trading_date, trade_start or TRADE_START_LOCAL),
        "no_new": _local_ts(trading_date, no_new or NO_NEW_TRADES_AFTER_LOCAL),
        "force_close": _local_ts(trading_date, force_close or FORCE_CLOSE_LOCAL),
    }


def typical_price(bar: Bar) -> float:
    return (float(bar.high) + float(bar.low) + float(bar.close)) / 3.0


def trading_dates_ny(bars: Sequence[Bar]) -> list[str]:
    return sorted({datetime.fromtimestamp(int(b.time), tz=NY).date().isoformat() for b in bars})


def index_bars_by_ny_date(bars: Sequence[Bar]) -> dict[str, list[Bar]]:
    out: dict[str, list[Bar]] = defaultdict(list)
    for b in sorted(bars, key=lambda x: int(x.time)):
        td = datetime.fromtimestamp(int(b.time), tz=NY).date().isoformat()
        out[td].append(b)
    return dict(out)


def compute_session_vwap_by_ts(
    bars_1m: Sequence[Bar],
    trading_date: str,
    *,
    clock: Optional[dict[str, str]] = None,
) -> dict[int, float]:
    """Running VWAP from session reset using 1m bars; key = bar timestamp."""
    clock = clock or {}
    anchors = session_anchors(
        trading_date,
        vwap_reset=clock.get("vwap_reset"),
        trade_start=clock.get("trade_start"),
        no_new=clock.get("no_new"),
        force_close=clock.get("force_close"),
    )
    start = anchors["vwap_reset"]
    end = anchors["force_close"] + 3600
    session = [b for b in bars_1m if start <= int(b.time) < end]
    out: dict[int, float] = {}
    sum_pv = 0.0
    sum_v = 0.0
    for b in session:
        tp = typical_price(b)
        vol = max(0.0, float(b.volume or 0.0))
        sum_pv += tp * vol
        sum_v += vol
        if sum_v > 0:
            out[int(b.time)] = sum_pv / sum_v
    return out


def vwap_at_or_before(vwap_map: dict[int, float], ts: int, sorted_keys: Optional[list[int]] = None) -> Optional[float]:
    if not vwap_map:
        return None
    if ts in vwap_map:
        return vwap_map[ts]
    keys = sorted_keys if sorted_keys is not None else sorted(vwap_map)
    # binary search last key <= ts
    lo, hi = 0, len(keys) - 1
    best = None
    while lo <= hi:
        mid = (lo + hi) // 2
        if keys[mid] <= ts:
            best = keys[mid]
            lo = mid + 1
        else:
            hi = mid - 1
    return None if best is None else vwap_map[best]


def classify_drift_15m(
    bars_15m: Sequence[Bar],
    idx: int,
    vwap_map: dict[int, float],
    *,
    threshold: float,
    vwap_keys: Optional[list[int]] = None,
) -> Optional[str]:
    if idx < 4:
        return None
    b = bars_15m[idx]
    prev = bars_15m[idx - 1]
    b4 = bars_15m[idx - 4]
    v_now = vwap_at_or_before(vwap_map, int(b.time) + 14 * 60, vwap_keys)
    v_prev = vwap_at_or_before(vwap_map, int(prev.time) + 14 * 60, vwap_keys)
    if v_now is None or v_prev is None or float(b4.close) == 0:
        return None
    close = float(b.close)
    hour_ret = close / float(b4.close) - 1.0
    if close > v_now and v_now > v_prev and hour_ret >= threshold:
        return "POSITIVE_DRIFT"
    if close < v_now and v_now < v_prev and hour_ret <= -threshold:
        return "NEGATIVE_DRIFT"
    return None


def resolve_exit_1m(
    *,
    bars_1m: Sequence[Bar],
    entry_ts: int,
    direction: str,
    stop: float,
    target: float,
    force_close_ts: int,
) -> dict[str, Any]:
    ordered = [b for b in bars_1m if int(b.time) >= int(entry_ts)]
    for b in ordered:
        t = int(b.time)
        if t > force_close_ts + 60:
            break
        hit_stop = float(b.low) <= stop if direction == "bullish" else float(b.high) >= stop
        hit_tgt = float(b.high) >= target if direction == "bullish" else float(b.low) <= target
        if force_close_ts <= t < force_close_ts + 60:
            if hit_stop and hit_tgt:
                return {"outcome": "AMBIGUOUS", "exit_timestamp": t, "exit_price": None}
            if hit_stop:
                return {"outcome": "STOP_HIT", "exit_timestamp": t, "exit_price": stop}
            if hit_tgt:
                return {"outcome": "TARGET_HIT", "exit_timestamp": t, "exit_price": target}
            return {"outcome": "FORCE_CLOSE", "exit_timestamp": t, "exit_price": float(b.open)}
        if hit_stop and hit_tgt:
            return {"outcome": "AMBIGUOUS", "exit_timestamp": t, "exit_price": None}
        if hit_stop:
            return {"outcome": "STOP_HIT", "exit_timestamp": t, "exit_price": stop}
        if hit_tgt:
            return {"outcome": "TARGET_HIT", "exit_timestamp": t, "exit_price": target}
    force_bars = [b for b in ordered if int(b.time) <= force_close_ts]
    if force_bars:
        b = force_bars[-1]
        return {"outcome": "FORCE_CLOSE", "exit_timestamp": int(b.time), "exit_price": float(b.close)}
    return {"outcome": "FORCE_CLOSE", "exit_timestamp": force_close_ts, "exit_price": None}


def _points_and_r(direction: str, entry: float, exit_px: Optional[float], risk: float) -> tuple[Optional[float], Optional[float]]:
    if exit_px is None:
        return None, None
    pts = (float(exit_px) - float(entry)) if direction == "bullish" else (float(entry) - float(exit_px))
    r = pts / float(risk) if risk else None
    return pts, r


def replay_dvp_day(
    *,
    trading_date: str,
    bars_1m: Sequence[Bar],
    bars_5m: Sequence[Bar],
    bars_15m: Sequence[Bar],
    cfg: DVPStrategyConfig = DVP_ORIGINAL,
    clock: Optional[dict[str, str]] = None,
    instrument: str = "NQ",
) -> dict[str, Any]:
    """
    Exact DVP_ORIGINAL day simulation.
    Loss-limit interpretation: stop after TWO losing trades in the day (any two, not only consecutive).
    Optional `clock` remaps session times for portability research; defaults remain frozen NQ.
    """
    clock = clock or {}
    anchors = session_anchors(
        trading_date,
        vwap_reset=clock.get("vwap_reset"),
        trade_start=clock.get("trade_start"),
        no_new=clock.get("no_new"),
        force_close=clock.get("force_close"),
    )
    vwap_map = compute_session_vwap_by_ts(bars_1m, trading_date, clock=clock)
    vwap_keys = sorted(vwap_map)
    b15 = [b for b in bars_15m if int(b.time) >= anchors["vwap_reset"]]
    b5 = [b for b in bars_5m if int(b.time) >= anchors["vwap_reset"]]
    b5_by_ts = {int(b.time): i for i, b in enumerate(b5)}

    trades: list[DVPTrade] = []
    suppressed = 0
    daily_losses = 0
    daily_trades = 0

    drift_by_15_end: dict[int, Optional[str]] = {}
    for i, bar in enumerate(b15):
        state = classify_drift_15m(b15, i, vwap_map, threshold=cfg.hour_return_threshold, vwap_keys=vwap_keys)
        drift_by_15_end[int(bar.time) + 15 * 60] = state
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

        drift = latest_drift_at(bar_end)
        red = float(bar.close) < float(bar.open)
        green = float(bar.close) > float(bar.open)
        setup = (drift == "POSITIVE_DRIFT" and red) or (drift == "NEGATIVE_DRIFT" and green)

        if daily_trades >= cfg.max_trades_per_day or daily_losses >= cfg.max_losses_per_day:
            if setup:
                suppressed += 1
            i += 1
            continue
        if not setup:
            i += 1
            continue

        nxt = b5[i + 1]
        entry_ts = int(nxt.time)
        if entry_ts >= anchors["no_new"] or entry_ts >= anchors["force_close"]:
            suppressed += 1
            i += 1
            continue

        direction = "bullish" if drift == "POSITIVE_DRIFT" else "bearish"
        entry = float(nxt.open)
        if direction == "bullish":
            stop = entry - cfg.long_stop_points
            target = entry + cfg.long_target_points
            risk = cfg.long_stop_points
        else:
            stop = entry + cfg.short_stop_points
            target = entry - cfg.short_target_points
            risk = cfg.short_stop_points

        daily_trades += 1
        exit_info = resolve_exit_1m(
            bars_1m=bars_1m,
            entry_ts=entry_ts,
            direction=direction,
            stop=stop,
            target=target,
            force_close_ts=anchors["force_close"],
        )
        pts, r_mult = _points_and_r(direction, entry, exit_info.get("exit_price"), risk)
        outcome = exit_info["outcome"]
        if outcome == "STOP_HIT" or (outcome == "FORCE_CLOSE" and pts is not None and pts < 0):
            daily_losses += 1

        trades.append(
            DVPTrade(
                trade_id=f"{instrument}|DVP|{trading_date}|{direction}|{entry_ts}",
                trading_date=trading_date,
                direction=direction,
                entry_timestamp=entry_ts,
                entry_price=entry,
                stop_price=stop,
                target_price=target,
                exit_timestamp=exit_info.get("exit_timestamp"),
                exit_price=exit_info.get("exit_price"),
                outcome=outcome,
                points=pts,
                r_multiple=r_mult,
                extras={
                    "drift": drift,
                    "pullback_5m_ts": t,
                    "vwap_basis": VWAP_PRICE_BASIS,
                    "vwap_basis_status": VWAP_BASIS_STATUS,
                    "risk_points": risk,
                },
            )
        )
        i = b5_by_ts.get(entry_ts, i) + 1

    return {
        "trading_date": trading_date,
        "trades": trades,
        "suppressed_setups": suppressed,
        "daily_trades": daily_trades,
        "daily_losses": daily_losses,
        "stopped_for_losses": daily_losses >= cfg.max_losses_per_day,
        "hit_trade_cap": daily_trades >= cfg.max_trades_per_day,
    }


def replay_all_days(
    bars_1m: Sequence[Bar],
    bars_5m: Sequence[Bar],
    bars_15m: Sequence[Bar],
    cfg: DVPStrategyConfig = DVP_ORIGINAL,
    *,
    clock: Optional[dict[str, str]] = None,
    instrument: str = "NQ",
) -> tuple[list[DVPTrade], dict[str, Any]]:
    """Returns trades + guardrail aggregate (single pass)."""
    by1 = index_bars_by_ny_date(bars_1m)
    by5 = index_bars_by_ny_date(bars_5m)
    by15 = index_bars_by_ny_date(bars_15m)
    dates = sorted(set(by5) | set(by15))
    all_trades: list[DVPTrade] = []
    suppressed = 0
    hit_loss = 0
    hit_cap = 0
    for td in dates:
        day = replay_dvp_day(
            trading_date=td,
            bars_1m=by1.get(td, []),
            bars_5m=by5.get(td, []),
            bars_15m=by15.get(td, []),
            cfg=cfg,
            clock=clock,
            instrument=instrument,
        )
        all_trades.extend(day["trades"])
        suppressed += int(day.get("suppressed_setups") or 0)
        if day.get("stopped_for_losses"):
            hit_loss += 1
        if day.get("hit_trade_cap"):
            hit_cap += 1
    guard = {
        "suppressed_setups_total": suppressed,
        "days_hit_two_loss_stop": hit_loss,
        "days_hit_four_trade_cap": hit_cap,
        "loss_stop_interpretation": "STOP AFTER TWO LOSING TRADES IN THE DAY (any two; not consecutive-only)",
    }
    return all_trades, guard
