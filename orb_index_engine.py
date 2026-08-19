"""Phase 38 — leak-safe ES/NQ RTH opening-range breakout engine (research-only).

OR is valid only after the window closes. Same-bar stop+target = AMBIGUOUS.
No VWAP, DOM, sweep, or indicator filters.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Optional, Sequence
from zoneinfo import ZoneInfo

from models import Bar
from nq_pdh_pdl import local_ts, ny_date, rth_bars

NY = ZoneInfo("America/New_York")
RTH_START = "09:30"
RTH_END = "16:00"
FLATTEN_LOCAL = "15:55"
TICK = 0.25

INSTRUMENTS = {
    "NQ": {"tick": 0.25, "point_usd": 20.0, "commission_points": 0.20, "max_stop_points": 80.0},
    "ES": {"tick": 0.25, "point_usd": 50.0, "commission_points": 0.08, "max_stop_points": 40.0},
}

US_RTH_HOLIDAYS = {
    "2020-01-01", "2020-01-20", "2020-02-17", "2020-04-10", "2020-05-25",
    "2020-07-03", "2020-09-07", "2020-11-26", "2020-12-25",
    "2021-01-01", "2021-01-18", "2021-02-15", "2021-04-02", "2021-05-31",
    "2021-07-05", "2021-09-06", "2021-11-25", "2021-12-24",
    "2022-01-17", "2022-02-21", "2022-04-15", "2022-05-30", "2022-06-20",
    "2022-07-04", "2022-09-05", "2022-11-24", "2022-12-26",
    "2023-01-02", "2023-01-16", "2023-02-20", "2023-04-07", "2023-05-29",
    "2023-06-19", "2023-07-04", "2023-09-04", "2023-11-23", "2023-12-25",
    "2024-01-01", "2024-01-15", "2024-02-19", "2024-03-29", "2024-05-27",
    "2024-06-19", "2024-07-04", "2024-09-02", "2024-11-28", "2024-12-25",
    "2025-01-01", "2025-01-20", "2025-02-17", "2025-04-18", "2025-05-26",
    "2025-06-19", "2025-07-04", "2025-09-01", "2025-11-27", "2025-12-25",
    "2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03", "2026-05-25",
    "2026-06-19", "2026-07-03",
}


@dataclass
class OpeningRange:
    trading_date: str
    or_minutes: int
    start_ts: int
    end_ts: int
    high: float
    low: float
    mid: float
    width: float
    bar_count: int
    complete: bool
    total_volume: float = 0.0


@dataclass
class OrbTrade:
    instrument: str
    trading_date: str
    or_minutes: int
    entry_family: str
    stop_mode: str
    direction: str
    or_high: float
    or_low: float
    or_width: float
    status: str
    outcome: Optional[str] = None
    break_ts: Optional[int] = None
    break_lag_sec: Optional[int] = None
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
    false_break: Optional[bool] = None
    crossed_opposite: Optional[bool] = None
    break_distance_points: Optional[float] = None
    breakout_bar_volume: Optional[float] = None
    breakout_bar_range: Optional[float] = None
    rel_volume: Optional[float] = None
    or_width_over_atr: Optional[float] = None
    gap_points: Optional[float] = None
    overnight_broke: Optional[bool] = None
    prior_day_return_pts: Optional[float] = None
    year: Optional[int] = None
    extras: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _close_ts(bar: Bar) -> int:
    return int(bar.time) + 60


def flatten_ts(trading_date: str) -> int:
    return local_ts(trading_date, FLATTEN_LOCAL)


def build_opening_range(rth: Sequence[Bar], trading_date: str, or_minutes: int) -> Optional[OpeningRange]:
    start = local_ts(trading_date, RTH_START)
    end = start + int(or_minutes) * 60
    window = [b for b in rth if start <= int(b.time) < end]
    need = int(or_minutes)
    if len(window) < need:
        return OpeningRange(trading_date, or_minutes, start, end, 0, 0, 0, 0, len(window), False)
    hi = max(float(b.high) for b in window)
    lo = min(float(b.low) for b in window)
    if hi <= lo:
        return OpeningRange(trading_date, or_minutes, start, end, hi, lo, hi, 0, len(window), False)
    vol = sum(float(b.volume or 0) for b in window)
    return OpeningRange(
        trading_date, or_minutes, start, end, hi, lo, (hi + lo) / 2.0, hi - lo, len(window), True, vol
    )


def five_minute_bars(rth: Sequence[Bar], rth_open: int) -> list[Bar]:
    buckets: dict[int, list[Bar]] = {}
    for b in rth:
        off = int(b.time) - int(rth_open)
        if off < 0:
            continue
        start = int(rth_open) + (off // 300) * 300
        buckets.setdefault(start, []).append(b)
    out: list[Bar] = []
    for start in sorted(buckets):
        g = buckets[start]
        out.append(
            Bar(
                time=start,
                open=float(g[0].open),
                high=max(float(x.high) for x in g),
                low=min(float(x.low) for x in g),
                close=float(g[-1].close),
                volume=sum(float(x.volume or 0) for x in g),
            )
        )
    return out


def _next_bar(bars: Sequence[Bar], after_ts: int) -> Optional[Bar]:
    for b in bars:
        if int(b.time) >= int(after_ts):
            return b
    return None


def detect_first_break(
    rth: Sequence[Bar],
    orng: OpeningRange,
    *,
    family: str,
) -> Optional[dict[str, Any]]:
    """Return first break after OR completion. family: range_1m | close_1m | close_5m."""
    if not orng.complete:
        return None
    rth_open = orng.start_ts
    if family == "close_5m":
        bars = five_minute_bars(rth, rth_open)
        bar_len = 300
        mode = "close"
    else:
        bars = list(rth)
        bar_len = 60
        mode = "range" if family == "range_1m" else "close"
    for b in bars:
        close_t = int(b.time) + bar_len
        if close_t <= orng.end_ts and mode == "close":
            continue
        if int(b.time) < orng.end_ts and mode == "range":
            continue
        if int(b.time) >= flatten_ts(orng.trading_date):
            break
        long_hit = float(b.high) > orng.high if mode == "range" else float(b.close) > orng.high
        short_hit = float(b.low) < orng.low if mode == "range" else float(b.close) < orng.low
        if long_hit and short_hit:
            return {"direction": None, "bar": b, "ambiguous_both": True, "confirm_ts": close_t if mode == "close" else int(b.time)}
        bar_range = float(b.high) - float(b.low)
        if long_hit:
            dist = (float(b.high) - orng.high) if mode == "range" else (float(b.close) - orng.high)
            return {
                "direction": "LONG",
                "bar": b,
                "ambiguous_both": False,
                "confirm_ts": close_t if mode == "close" else int(b.time),
                "break_distance": dist,
                "volume": float(b.volume or 0),
                "bar_range": bar_range,
            }
        if short_hit:
            dist = (orng.low - float(b.low)) if mode == "range" else (orng.low - float(b.close))
            return {
                "direction": "SHORT",
                "bar": b,
                "ambiguous_both": False,
                "confirm_ts": close_t if mode == "close" else int(b.time),
                "break_distance": dist,
                "volume": float(b.volume or 0),
                "bar_range": bar_range,
            }
    return None


def resolve_path(
    path: Sequence[Bar],
    *,
    is_long: bool,
    sl: float,
    tp: float,
    flatten: int,
) -> tuple[str, Optional[int], Optional[float], Optional[float], Optional[float]]:
    mfe = 0.0
    mae = 0.0
    entry_ref = None
    for b in path:
        if int(b.time) >= flatten:
            break
        if entry_ref is None:
            entry_ref = float(b.open)
        if is_long:
            mfe = max(mfe, float(b.high) - entry_ref)
            mae = min(mae, float(b.low) - entry_ref)
            hit_sl = float(b.low) <= sl
            hit_tp = float(b.high) >= tp
        else:
            mfe = max(mfe, entry_ref - float(b.low))
            mae = min(mae, entry_ref - float(b.high))
            hit_sl = float(b.high) >= sl
            hit_tp = float(b.low) <= tp
        if hit_sl and hit_tp:
            return "AMBIGUOUS", int(b.time), None, mfe, mae
        if hit_sl:
            return "STOP_HIT", int(b.time), sl, mfe, mae
        if hit_tp:
            return "TARGET_HIT", int(b.time), tp, mfe, mae
    last = None
    for b in path:
        if int(b.time) < flatten:
            last = b
        else:
            break
    if last is None:
        return "NO_PATH", None, None, mfe, mae
    px = float(last.close)
    return "TIME_EXIT", min(_close_ts(last), flatten), px, mfe, mae


def _false_break_and_opposite(path: Sequence[Bar], orng: OpeningRange, is_long: bool, stop_ts: Optional[int], flatten: int) -> tuple[bool, bool]:
    inside = False
    opposite = False
    for b in path:
        if int(b.time) >= flatten:
            break
        if stop_ts is not None and int(b.time) > int(stop_ts):
            break
        if is_long:
            if float(b.low) <= orng.high:
                inside = True
            if float(b.low) < orng.low:
                opposite = True
        else:
            if float(b.high) >= orng.low:
                inside = True
            if float(b.high) > orng.high:
                opposite = True
    return inside, opposite


def simulate(
    *,
    instrument: str,
    rth: Sequence[Bar],
    orng: OpeningRange,
    family: str,
    stop_mode: str,
    target_r: float,
    adverse_ticks: float,
    atr_daily: Optional[float] = None,
    rel_volume: Optional[float] = None,
    gap_points: Optional[float] = None,
    overnight_broke: Optional[bool] = None,
    prior_day_return_pts: Optional[float] = None,
) -> OrbTrade:
    spec = INSTRUMENTS[instrument]
    tick = float(spec["tick"])
    comm = float(spec["commission_points"])
    year = int(orng.trading_date[:4])
    base = OrbTrade(
        instrument=instrument,
        trading_date=orng.trading_date,
        or_minutes=orng.or_minutes,
        entry_family=family,
        stop_mode=stop_mode,
        direction="",
        or_high=orng.high,
        or_low=orng.low,
        or_width=orng.width,
        status="NO_BREAK",
        target_r=float(target_r),
        rel_volume=rel_volume,
        or_width_over_atr=None if not atr_daily else orng.width / atr_daily,
        gap_points=gap_points,
        overnight_broke=overnight_broke,
        prior_day_return_pts=prior_day_return_pts,
        year=year,
    )
    br = detect_first_break(rth, orng, family=family)
    if br is None:
        return base
    if br.get("ambiguous_both"):
        base.status = "BOTH_SIDES_SAME_BAR"
        return base
    direction = br["direction"]
    is_long = direction == "LONG"
    bar: Bar = br["bar"]
    confirm_ts = int(br["confirm_ts"])
    if family == "range_1m":
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
            base.direction = direction
            return base
        theo = float(nxt.open)
        fill = theo + adverse_ticks * tick if is_long else theo - adverse_ticks * tick
        entry_ts = int(nxt.time)
        path_start = int(nxt.time)
    if stop_mode == "A_opposite":
        sl = orng.low if is_long else orng.high
    elif stop_mode == "B_mid":
        sl = orng.mid
    else:
        atr = float(atr_daily or 0)
        if atr <= 0:
            base.status = "NO_ATR"
            return base
        sl = fill - atr if is_long else fill + atr
    risk = abs(fill - sl)
    if risk < tick:
        base.status = "REJECT_TIGHT_STOP"
        base.direction = direction
        return base
    if risk > float(spec["max_stop_points"]):
        base.status = "REJECT_WIDE_STOP"
        base.direction = direction
        base.risk_points = risk
        return base
    tp = fill + float(target_r) * risk if is_long else fill - float(target_r) * risk
    path = [b for b in rth if int(b.time) >= path_start]
    flat = flatten_ts(orng.trading_date)
    outcome, exit_ts, exit_px, mfe, mae = resolve_path(path, is_long=is_long, sl=sl, tp=tp, flatten=flat)
    inside, opposite = _false_break_and_opposite(path, orng, is_long, exit_ts, flat)
    if outcome == "AMBIGUOUS":
        return OrbTrade(
            **{**base.to_dict(), "direction": direction, "status": "ENTERED", "outcome": "AMBIGUOUS",
               "break_ts": confirm_ts, "break_lag_sec": confirm_ts - orng.end_ts,
               "entry_ts": entry_ts, "entry_theo": theo, "entry_fill": fill, "stop": sl, "target": tp,
               "risk_points": risk, "false_break": inside, "crossed_opposite": opposite,
               "break_distance_points": br.get("break_distance"),
               "breakout_bar_volume": br.get("volume"),
               "breakout_bar_range": br.get("bar_range"),
               "mfe_points": mfe, "mae_points": mae},
        )
    if outcome in ("NO_PATH",) or exit_px is None:
        base.status = "NO_PATH"
        base.direction = direction
        return base
    pts = (exit_px - fill) if is_long else (fill - exit_px)
    r_mult = pts / risk
    pts_c = pts - comm
    r_c = pts_c / risk
    hold = None if exit_ts is None else int(exit_ts) - int(entry_ts)
    return OrbTrade(
        instrument=instrument,
        trading_date=orng.trading_date,
        or_minutes=orng.or_minutes,
        entry_family=family,
        stop_mode=stop_mode,
        direction=direction,
        or_high=orng.high,
        or_low=orng.low,
        or_width=orng.width,
        status="ENTERED",
        outcome=outcome,
        break_ts=confirm_ts,
        break_lag_sec=confirm_ts - orng.end_ts,
        entry_ts=entry_ts,
        entry_theo=theo,
        entry_fill=fill,
        stop=sl,
        target=tp,
        risk_points=risk,
        target_r=float(target_r),
        exit_ts=exit_ts,
        exit_px=exit_px,
        points=pts,
        r_multiple=r_mult,
        points_after_cost=pts_c,
        r_after_cost=r_c,
        mfe_points=mfe,
        mae_points=mae,
        hold_sec=hold,
        false_break=inside,
        crossed_opposite=opposite,
        break_distance_points=br.get("break_distance"),
        breakout_bar_volume=br.get("volume"),
        breakout_bar_range=br.get("bar_range"),
        rel_volume=rel_volume,
        or_width_over_atr=None if not atr_daily else orng.width / atr_daily,
        gap_points=gap_points,
        overnight_broke=overnight_broke,
        prior_day_return_pts=prior_day_return_pts,
        year=year,
    )


def overnight_hl(bars_by_date: dict[str, list[Bar]], trading_date: str) -> Optional[tuple[float, float]]:
    rth0 = local_ts(trading_date, RTH_START)
    d = date.fromisoformat(trading_date)
    prev = d - timedelta(days=1)
    while prev.weekday() > 4:
        prev = prev - timedelta(days=1)
    globex = int(datetime(prev.year, prev.month, prev.day, 18, 0, tzinfo=NY).timestamp())
    window = []
    for iso, rows in bars_by_date.items():
        for b in rows:
            if globex <= int(b.time) < rth0:
                window.append(b)
    if len(window) < 10:
        return None
    return max(float(b.high) for b in window), min(float(b.low) for b in window)
