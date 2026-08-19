"""Phase 36 — leak-safe shallow PDH/PDL sweep reclaim engine (research-only).

No DOM. No fill at the structural level or sweep extreme. Same-bar stop+target = AMBIGUOUS.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Optional, Sequence

from models import Bar
from nq_microstructure_models import NQ_TICK, SweepEvent
from nq_pdh_pdl import local_ts, rth_bars

FLATTEN_LOCAL = "15:55"
MAX_STOP_POINTS = 40.0
COMMISSION_POINTS = 0.2  # $4 RT / $20 per NQ point
POINT_USD = 20.0


@dataclass
class SweepTrade:
    event_id: str
    trading_date: str
    side: str
    direction: str
    contract: str
    candidate: str
    reclaim_mode: str
    penetration_points: float
    first_of_day: bool
    opening_bar: bool
    seconds_from_rth_open: int
    session_bucket: str
    status: str
    outcome: Optional[str] = None
    reclaim_ts: Optional[int] = None
    reclaim_lag_sec: Optional[int] = None
    entry_ts: Optional[int] = None
    entry_theo: Optional[float] = None
    entry_fill: Optional[float] = None
    stop: Optional[float] = None
    target: Optional[float] = None
    risk_points: Optional[float] = None
    risk_usd: Optional[float] = None
    target_r: Optional[float] = None
    exit_ts: Optional[int] = None
    exit_px: Optional[float] = None
    points: Optional[float] = None
    r_multiple: Optional[float] = None
    points_after_cost: Optional[float] = None
    r_after_cost: Optional[float] = None
    entry_adverse_ticks: float = 1.0
    sl_buffer_ticks: int = 1
    expiry_sec: int = 300
    extras: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def session_bucket(seconds_from_open: int) -> str:
    if seconds_from_open < 1800:
        return "0930_1000"
    if seconds_from_open < 7200:
        return "1000_1130"
    if seconds_from_open < 14400:
        return "1130_1330"
    return "1330_1530"


def _close_ts(bar) -> int:
    return int(bar.time) + 60


def _next_bar(bars: Sequence, after_close_ts: int):
    for b in bars:
        if int(b.time) >= int(after_close_ts):
            return b
    return None


def five_minute_bars(rth: Sequence, rth_open: int) -> list[Bar]:
    buckets: dict[int, list] = {}
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


def reclaim_ok(bar, *, side: str, mode: str, level: float, is_sweep_bar: bool) -> bool:
    if side == "pdl_sweep":
        if mode == "range_1m":
            if is_sweep_bar and float(bar.close) < level:
                return False
            return float(bar.high) >= level
        if mode == "close_1m":
            return float(bar.close) >= level
        if mode == "close_5m":
            return float(bar.close) >= level
    else:
        if mode == "range_1m":
            if is_sweep_bar and float(bar.close) > level:
                return False
            return float(bar.low) <= level
        if mode == "close_1m":
            return float(bar.close) <= level
        if mode == "close_5m":
            return float(bar.close) <= level
    return False


def find_reclaim(
    event: SweepEvent,
    rth: Sequence,
    *,
    mode: str,
    expiry_sec: int,
) -> Optional[tuple[Any, int]]:
    confirm = int(event.sweep_bar_time) + 60
    expiry = confirm + int(expiry_sec)
    level = float(event.level)
    side = event.side
    bars = rth
    if mode == "close_5m":
        bars = five_minute_bars(rth, int(event.rth_open_ts))
        bar_len = 300
    else:
        bar_len = 60
    for b in bars:
        close_t = int(b.time) + bar_len
        if close_t < confirm:
            continue
        if close_t > expiry:
            break
        is_sweep = int(b.time) == int(event.sweep_bar_time)
        if mode == "close_5m":
            is_sweep = False
        if reclaim_ok(b, side=side, mode=mode, level=level, is_sweep_bar=is_sweep):
            return b, close_t
    return None


def resolve_path(
    path: Sequence,
    *,
    is_long: bool,
    sl: float,
    tp: float,
    flatten_ts: int,
) -> tuple[str, Optional[int], Optional[float]]:
    for b in path:
        if int(b.time) >= flatten_ts:
            break
        hit_sl = float(b.low) <= sl if is_long else float(b.high) >= sl
        hit_tp = float(b.high) >= tp if is_long else float(b.low) <= tp
        if hit_sl and hit_tp:
            return "AMBIGUOUS", int(b.time), None
        if hit_sl:
            return "STOP_HIT", int(b.time), sl
        if hit_tp:
            return "TARGET_HIT", int(b.time), tp
    last = None
    for b in path:
        if int(b.time) < flatten_ts:
            last = b
        else:
            break
    if last is None:
        return "NO_PATH", None, None
    return "TIME_EXIT", _close_ts(last) if _close_ts(last) <= flatten_ts else flatten_ts, float(last.close)


def simulate_continuation_diagnostic(
    event: SweepEvent,
    bars: Sequence,
    *,
    target_r: float = 1.5,
    entry_adverse_ticks: float = 1.0,
    sl_buffer_ticks: int = 1,
) -> SweepTrade:
    """One frozen continuation probe: enter with the sweep at the next open after confirmation.

    Stop at the structural level. Not a candidate. No parameter search.
    """
    contract = str((event.extras or {}).get("contract") or "")
    base = SweepTrade(
        event_id=event.event_id,
        trading_date=event.trading_date,
        side=event.side,
        direction="LONG" if event.side == "pdh_sweep" else "SHORT",
        contract=contract,
        candidate="DEEP_CONT_DIAG",
        reclaim_mode="immediate_next_open",
        penetration_points=float(event.penetration_points),
        first_of_day=True,
        opening_bar=int(event.seconds_from_rth_open) == 0,
        seconds_from_rth_open=int(event.seconds_from_rth_open),
        session_bucket=session_bucket(int(event.seconds_from_rth_open)),
        status="INIT",
        target_r=float(target_r),
        entry_adverse_ticks=float(entry_adverse_ticks),
        sl_buffer_ticks=int(sl_buffer_ticks),
        extras={"note": "continuation_diagnostic_not_a_candidate"},
    )
    rth = rth_bars(bars, event.trading_date)
    flatten = local_ts(event.trading_date, FLATTEN_LOCAL)
    confirm = int(event.sweep_bar_time) + 60
    entry_bar = _next_bar(rth, confirm)
    if entry_bar is None or int(entry_bar.time) >= flatten:
        base.status = "NO_ENTRY"
        return base
    is_long = event.side == "pdh_sweep"
    tick = NQ_TICK
    theo = float(entry_bar.open)
    adv = float(entry_adverse_ticks) * tick
    fill = theo + adv if is_long else theo - adv
    buf = int(sl_buffer_ticks) * tick
    sl = float(event.level) - buf if is_long else float(event.level) + buf
    risk = (fill - sl) if is_long else (sl - fill)
    if risk <= tick:
        base.status = "REJECT_STOP_TOO_TIGHT"
        return base
    tp = fill + float(target_r) * risk if is_long else fill - float(target_r) * risk
    path = [b for b in rth if int(b.time) >= int(entry_bar.time)]
    outcome, exit_ts, exit_px = resolve_path(path, is_long=is_long, sl=sl, tp=tp, flatten_ts=flatten)
    base.status = "ENTERED"
    base.outcome = outcome
    base.entry_ts = int(entry_bar.time)
    base.entry_theo = theo
    base.entry_fill = fill
    base.stop = sl
    base.target = tp
    base.risk_points = risk
    base.risk_usd = risk * POINT_USD
    base.exit_ts = exit_ts
    if outcome == "AMBIGUOUS" or exit_px is None:
        return base
    if is_long:
        pts = float(exit_px) - fill
    else:
        pts = fill - float(exit_px)
    base.exit_px = float(exit_px)
    base.points = pts
    base.r_multiple = pts / risk
    base.points_after_cost = pts - COMMISSION_POINTS
    base.r_after_cost = (pts - COMMISSION_POINTS) / risk
    return base


def simulate(
    event: SweepEvent,
    bars: Sequence,
    *,
    candidate: str,
    reclaim_mode: str,
    expiry_sec: int = 300,
    sl_buffer_ticks: int = 1,
    target_r: float = 1.5,
    entry_adverse_ticks: float = 1.0,
    exit_adverse_ticks: float = 0.0,
    first_of_day: bool = True,
    max_stop_points: float = MAX_STOP_POINTS,
) -> SweepTrade:
    contract = str((event.extras or {}).get("contract") or "")
    opening = int(event.seconds_from_rth_open) == 0
    base = SweepTrade(
        event_id=event.event_id,
        trading_date=event.trading_date,
        side=event.side,
        direction="LONG" if event.side == "pdl_sweep" else "SHORT",
        contract=contract,
        candidate=candidate,
        reclaim_mode=reclaim_mode,
        penetration_points=float(event.penetration_points),
        first_of_day=first_of_day,
        opening_bar=opening,
        seconds_from_rth_open=int(event.seconds_from_rth_open),
        session_bucket=session_bucket(int(event.seconds_from_rth_open)),
        status="INIT",
        target_r=float(target_r),
        entry_adverse_ticks=float(entry_adverse_ticks),
        sl_buffer_ticks=int(sl_buffer_ticks),
        expiry_sec=int(expiry_sec),
    )
    rth = rth_bars(bars, event.trading_date)
    flatten = local_ts(event.trading_date, FLATTEN_LOCAL)
    found = find_reclaim(event, rth, mode=reclaim_mode, expiry_sec=expiry_sec)
    if found is None:
        base.status = "EXPIRED"
        return base
    _cbar, reclaim_close = found
    base.reclaim_ts = reclaim_close
    base.reclaim_lag_sec = reclaim_close - (int(event.sweep_bar_time) + 60)
    entry_bar = _next_bar(rth, reclaim_close)
    if entry_bar is None or int(entry_bar.time) >= flatten:
        base.status = "NO_ENTRY"
        return base
    is_long = event.side == "pdl_sweep"
    theo = float(entry_bar.open)
    tick = NQ_TICK
    adv = float(entry_adverse_ticks) * tick
    fill = theo + adv if is_long else theo - adv
    buf = int(sl_buffer_ticks) * tick
    sl = float(event.extreme) - buf if is_long else float(event.extreme) + buf
    risk = (fill - sl) if is_long else (sl - fill)
    if risk <= tick:
        base.status = "REJECT_STOP_TOO_TIGHT"
        base.entry_ts = int(entry_bar.time)
        base.entry_theo = theo
        base.entry_fill = fill
        base.stop = sl
        base.risk_points = risk
        return base
    if risk > max_stop_points:
        base.status = "REJECT_STOP_TOO_WIDE"
        base.entry_ts = int(entry_bar.time)
        base.entry_theo = theo
        base.entry_fill = fill
        base.stop = sl
        base.risk_points = risk
        base.risk_usd = risk * POINT_USD
        return base
    tp = fill + float(target_r) * risk if is_long else fill - float(target_r) * risk
    path = [b for b in rth if int(b.time) >= int(entry_bar.time)]
    outcome, exit_ts, exit_px = resolve_path(path, is_long=is_long, sl=sl, tp=tp, flatten_ts=flatten)
    base.status = "ENTERED"
    base.outcome = outcome
    base.entry_ts = int(entry_bar.time)
    base.entry_theo = theo
    base.entry_fill = fill
    base.stop = sl
    base.target = tp
    base.risk_points = risk
    base.risk_usd = risk * POINT_USD
    base.exit_ts = exit_ts
    if outcome == "AMBIGUOUS":
        return base
    if exit_px is None:
        base.outcome = "NO_PATH"
        return base
    x_adv = float(exit_adverse_ticks) * tick
    if is_long:
        fill_exit = exit_px - x_adv
        pts = fill_exit - fill
    else:
        fill_exit = exit_px + x_adv
        pts = fill - fill_exit
    base.exit_px = fill_exit
    base.points = pts
    base.r_multiple = pts / risk
    cost = COMMISSION_POINTS
    base.points_after_cost = pts - cost
    base.r_after_cost = (pts - cost) / risk
    return base
