"""Phase 39 — leak-safe OR breakout → boundary retest → continuation (research-only).

Breakout confirms a pending state only. Entry is after a shallow retest hold.
Same-bar stop+target = AMBIGUOUS. No VWAP, DOM, sweep, or indicator filters.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Optional, Sequence

from models import Bar
from orb_index_engine import (
    INSTRUMENTS,
    OpeningRange,
    _next_bar,
    detect_first_break,
    five_minute_bars,
    flatten_ts,
    resolve_path,
)

TICK = 0.25
EXPIRY_SEC = 1800
MIN_STOP_TICKS = 2


@dataclass
class RetestTrade:
    instrument: str
    trading_date: str
    or_minutes: int
    direction: str
    status: str
    trigger: str
    fail_frac: float
    confirm: str
    stop_mode: str
    or_high: float
    or_low: float
    or_width: float
    outcome: Optional[str] = None
    break_ts: Optional[int] = None
    retest_ts: Optional[int] = None
    confirm_ts: Optional[int] = None
    entry_ts: Optional[int] = None
    entry_theo: Optional[float] = None
    entry_fill: Optional[float] = None
    stop: Optional[float] = None
    target: Optional[float] = None
    risk_points: Optional[float] = None
    target_r: Optional[float] = None
    exit_ts: Optional[int] = None
    exit_px: Optional[float] = None
    points: Optional[float] = None
    r_multiple: Optional[float] = None
    points_after_cost: Optional[float] = None
    r_after_cost: Optional[float] = None
    mfe_points: Optional[float] = None
    mae_points: Optional[float] = None
    hold_sec: Optional[int] = None
    retest_lag_sec: Optional[int] = None
    confirm_lag_sec: Optional[int] = None
    penetration: Optional[float] = None
    penetration_frac: Optional[float] = None
    retest_extreme: Optional[float] = None
    max_extension: Optional[float] = None
    extension_over_width: Optional[float] = None
    bars_inside: Optional[int] = None
    year: Optional[int] = None
    gap_points: Optional[float] = None
    or_width_over_atr: Optional[float] = None
    prior_day_return_pts: Optional[float] = None
    false_break: Optional[bool] = None
    crossed_opposite: Optional[bool] = None
    extras: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def trigger_level(orng: OpeningRange, is_long: bool, trigger: str, tick: float) -> float:
    if trigger == "T0_exact":
        return orng.high if is_long else orng.low
    if trigger == "T1_two_ticks":
        return orng.high + 2 * tick if is_long else orng.low - 2 * tick
    tol = min(0.05 * orng.width, 8 * tick)
    return orng.high + tol if is_long else orng.low - tol


def _hit_trigger(bar: Bar, is_long: bool, level: float) -> bool:
    return float(bar.low) <= level if is_long else float(bar.high) >= level


def _penetration(orng: OpeningRange, is_long: bool, extreme: float) -> float:
    return (orng.high - extreme) if is_long else (extreme - orng.low)


def _opposite(bar: Bar, orng: OpeningRange, is_long: bool) -> bool:
    return float(bar.low) < orng.low if is_long else float(bar.high) > orng.high


def _hold_close(bar: Bar, orng: OpeningRange, is_long: bool) -> bool:
    return float(bar.close) > orng.high if is_long else float(bar.close) < orng.low


def _hold_range(bar: Bar, orng: OpeningRange, is_long: bool) -> bool:
    return float(bar.high) > orng.high if is_long else float(bar.low) < orng.low


def scan_retest(
    rth: Sequence[Bar],
    orng: OpeningRange,
    *,
    trigger: str,
    fail_frac: float,
    confirm: str,
    tick: float,
    expiry_sec: int = EXPIRY_SEC,
) -> dict[str, Any]:
    br = detect_first_break(rth, orng, family="close_1m")
    if br is None:
        return {"status": "NO_BREAK"}
    if br.get("ambiguous_both"):
        return {"status": "BOTH_SIDES_SAME_BAR"}
    direction = br["direction"]
    is_long = direction == "LONG"
    confirm_break = int(br["confirm_ts"])
    level = trigger_level(orng, is_long, trigger, tick)
    fail_pts = float(fail_frac) * orng.width
    flat = flatten_ts(orng.trading_date)
    expiry = confirm_break + int(expiry_sec)
    if confirm == "C_close_5m":
        bars = five_minute_bars(rth, orng.start_ts)
        bar_len = 300
        close_mode = True
        range_mode = False
    else:
        bars = list(rth)
        bar_len = 60
        close_mode = confirm == "B_close_1m"
        range_mode = confirm == "A_range"

    retested = False
    retest_ts = None
    extreme = None
    max_ext = 0.0
    bars_inside = 0
    for b in bars:
        t = int(b.time)
        close_t = t + bar_len
        if close_t <= confirm_break and close_mode:
            continue
        if t < confirm_break and not close_mode:
            continue
        if t >= flat or t >= expiry:
            return {
                "status": "EXPIRED",
                "direction": direction,
                "break_ts": confirm_break,
                "retested": retested,
                "retest_ts": retest_ts,
                "max_extension": max_ext,
                "break_bar": br["bar"],
            }
        if _opposite(b, orng, is_long):
            return {
                "status": "BREAKOUT_INVALIDATED",
                "direction": direction,
                "break_ts": confirm_break,
                "retest_ts": retest_ts,
                "penetration": None if extreme is None else _penetration(orng, is_long, extreme),
                "max_extension": max_ext,
                "break_bar": br["bar"],
            }
        if not retested:
            if is_long:
                max_ext = max(max_ext, float(b.high) - orng.high)
            else:
                max_ext = max(max_ext, orng.low - float(b.low))
            if _hit_trigger(b, is_long, level):
                retested = True
                retest_ts = t
                extreme = float(b.low) if is_long else float(b.high)
            else:
                continue
        # retest active
        if is_long:
            extreme = min(extreme, float(b.low))
            if float(b.low) < orng.high:
                bars_inside += 1
        else:
            extreme = max(extreme, float(b.high))
            if float(b.high) > orng.low:
                bars_inside += 1
        pen = _penetration(orng, is_long, extreme)
        if pen > fail_pts + 1e-12:
            return {
                "status": "RETEST_FAILED",
                "direction": direction,
                "break_ts": confirm_break,
                "retest_ts": retest_ts,
                "penetration": pen,
                "penetration_frac": pen / orng.width if orng.width else None,
                "retest_extreme": extreme,
                "max_extension": max_ext,
                "bars_inside": bars_inside,
                "break_bar": br["bar"],
            }
        held = _hold_close(b, orng, is_long) if close_mode else _hold_range(b, orng, is_long)
        if held:
            return {
                "status": "RETEST_CONFIRMED",
                "direction": direction,
                "break_ts": confirm_break,
                "retest_ts": retest_ts,
                "confirm_ts": close_t if close_mode else t,
                "confirm_bar": b,
                "penetration": pen,
                "penetration_frac": pen / orng.width if orng.width else None,
                "retest_extreme": extreme,
                "max_extension": max_ext,
                "bars_inside": bars_inside,
                "break_bar": br["bar"],
                "is_long": is_long,
            }
    return {
        "status": "EXPIRED",
        "direction": direction,
        "break_ts": confirm_break,
        "retested": retested,
        "retest_ts": retest_ts,
        "max_extension": max_ext,
        "break_bar": br["bar"],
    }


def simulate_retest(
    *,
    instrument: str,
    rth: Sequence[Bar],
    orng: OpeningRange,
    trigger: str,
    fail_frac: float,
    confirm: str,
    stop_mode: str,
    target_r: float,
    adverse_ticks: float,
    atr_daily: Optional[float] = None,
    gap_points: Optional[float] = None,
    prior_day_return_pts: Optional[float] = None,
) -> RetestTrade:
    spec = INSTRUMENTS[instrument]
    tick = float(spec["tick"])
    comm = float(spec["commission_points"])
    year = int(orng.trading_date[:4])
    base = RetestTrade(
        instrument=instrument,
        trading_date=orng.trading_date,
        or_minutes=orng.or_minutes,
        direction="",
        status="NO_BREAK",
        trigger=trigger,
        fail_frac=float(fail_frac),
        confirm=confirm,
        stop_mode=stop_mode,
        or_high=orng.high,
        or_low=orng.low,
        or_width=orng.width,
        target_r=float(target_r),
        year=year,
        gap_points=gap_points,
        or_width_over_atr=None if not atr_daily else orng.width / atr_daily,
        prior_day_return_pts=prior_day_return_pts,
    )
    ev = scan_retest(rth, orng, trigger=trigger, fail_frac=fail_frac, confirm=confirm, tick=tick)
    base.status = ev["status"]
    base.direction = ev.get("direction") or ""
    base.break_ts = ev.get("break_ts")
    base.retest_ts = ev.get("retest_ts")
    base.penetration = ev.get("penetration")
    base.penetration_frac = ev.get("penetration_frac")
    base.retest_extreme = ev.get("retest_extreme")
    base.max_extension = ev.get("max_extension")
    base.bars_inside = ev.get("bars_inside")
    if ev.get("break_ts") and ev.get("retest_ts"):
        base.retest_lag_sec = int(ev["retest_ts"]) - int(ev["break_ts"])
    if ev.get("max_extension") is not None and orng.width:
        base.extension_over_width = float(ev["max_extension"]) / orng.width
    if ev["status"] != "RETEST_CONFIRMED":
        return base
    is_long = bool(ev["is_long"])
    confirm_ts = int(ev["confirm_ts"])
    extreme = float(ev["retest_extreme"])
    if confirm == "A_range":
        bar: Bar = ev["confirm_bar"]
        if is_long:
            theo = orng.high if float(bar.open) <= orng.high else float(bar.open)
            fill = theo + adverse_ticks * tick
        else:
            theo = orng.low if float(bar.open) >= orng.low else float(bar.open)
            fill = theo - adverse_ticks * tick
        entry_ts = int(bar.time)
        path_start = int(bar.time)
    else:
        nxt = _next_bar(rth, confirm_ts)
        if nxt is None or int(nxt.time) >= flatten_ts(orng.trading_date):
            base.status = "NO_ENTRY_BAR"
            return base
        theo = float(nxt.open)
        fill = theo + adverse_ticks * tick if is_long else theo - adverse_ticks * tick
        entry_ts = int(nxt.time)
        path_start = int(nxt.time)
    if stop_mode == "A_retest_extreme":
        sl = extreme - tick if is_long else extreme + tick
    elif stop_mode == "B_boundary_2ticks":
        sl = orng.high - 2 * tick if is_long else orng.low + 2 * tick
    elif stop_mode == "C_mid":
        sl = orng.mid
    else:
        sl = orng.low if is_long else orng.high
    risk = abs(fill - sl)
    if (is_long and fill <= sl) or ((not is_long) and fill >= sl):
        base.status = "REJECT_TIGHT_STOP"
        base.risk_points = risk
        return base
    if risk < MIN_STOP_TICKS * tick:
        base.status = "REJECT_TIGHT_STOP"
        base.risk_points = risk
        return base
    if risk > float(spec["max_stop_points"]):
        base.status = "REJECT_WIDE_STOP"
        base.risk_points = risk
        return base
    tp = fill + float(target_r) * risk if is_long else fill - float(target_r) * risk
    path = [b for b in rth if int(b.time) >= path_start]
    flat = flatten_ts(orng.trading_date)
    outcome, exit_ts, exit_px, mfe, mae = resolve_path(path, is_long=is_long, sl=sl, tp=tp, flatten=flat)
    base.confirm_ts = confirm_ts
    base.confirm_lag_sec = None if ev.get("retest_ts") is None else confirm_ts - int(ev["retest_ts"])
    base.entry_ts = entry_ts
    base.entry_theo = theo
    base.entry_fill = fill
    base.stop = sl
    base.target = tp
    base.risk_points = risk
    base.mfe_points = mfe
    base.mae_points = mae
    if outcome == "AMBIGUOUS":
        base.status = "ENTERED"
        base.outcome = "AMBIGUOUS"
        return base
    if outcome == "NO_PATH" or exit_px is None:
        base.status = "NO_PATH"
        return base
    pts = (exit_px - fill) if is_long else (fill - exit_px)
    pts_c = pts - comm
    base.status = "ENTERED"
    base.outcome = outcome
    base.exit_ts = exit_ts
    base.exit_px = exit_px
    base.points = pts
    base.r_multiple = pts / risk
    base.points_after_cost = pts_c
    base.r_after_cost = pts_c / risk
    base.hold_sec = None if exit_ts is None else int(exit_ts) - int(entry_ts)
    return base
