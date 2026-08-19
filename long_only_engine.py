"""Phase 44 — leak-safe long-only bullish-state engine (research-only).

BULLISH -> LONG. Else FLAT. Never short. No VWAP.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Optional, Sequence

from models import Bar
from nq_pdh_pdl import local_ts
from orb_index_engine import INSTRUMENTS, five_minute_bars, flatten_ts, resolve_path
from tsmom_engine import SessionDay, TsmomTrade, cum_clean, signal_at

NO_NEW = "15:00"
FLATTEN = "15:55"


@dataclass
class DayState:
    date: str
    bull_10: Optional[bool]
    bull_20: Optional[bool]
    bull_60: Optional[bool]
    bull_ema: Optional[bool]
    bull_20_and_5: Optional[bool]
    ret_5: Optional[float]
    ret_10: Optional[float]
    ret_20: Optional[float]
    ret_60: Optional[float]
    prior_1d: Optional[float]
    prior_2d: Optional[float]
    prior_3d: Optional[float]
    pct_below_20h: Optional[float]
    close_loc: Optional[float]
    rv20: Optional[float]
    dip_bucket: Optional[str]


@dataclass
class LongTrade:
    instrument: str
    trading_date: str
    candidate: str
    direction: str
    status: str
    outcome: Optional[str] = None
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
    reach_1r: Optional[bool] = None
    reach_2r: Optional[bool] = None
    hold_sec: Optional[int] = None
    year: Optional[int] = None
    signal_hhmm: Optional[str] = None
    extras: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def ema20_series(days: Sequence[SessionDay]) -> list[Optional[float]]:
    k = 2.0 / 21.0
    out: list[Optional[float]] = [None] * len(days)
    ema = None
    for i, d in enumerate(days):
        c = d.close
        if ema is None:
            if i >= 19:
                ema = sum(days[j].close for j in range(i - 19, i + 1)) / 20.0
                out[i] = ema
            continue
        ema = c * k + ema * (1.0 - k)
        out[i] = ema
    return out


def rv20_at(days: Sequence[SessionDay], i: int) -> Optional[float]:
    xs = [days[j].cc_clean for j in range(max(0, i - 19), i + 1) if days[j].cc_clean is not None]
    if len(xs) < 15:
        return None
    m = sum(xs) / len(xs)
    var = sum((x - m) ** 2 for x in xs) / (len(xs) - 1)
    return var ** 0.5


def pct_below_20h(days: Sequence[SessionDay], i: int) -> Optional[float]:
    if i < 19:
        return None
    hh = max(days[j].high for j in range(i - 19, i + 1))
    if hh <= 0:
        return None
    return (hh - days[i].close) / hh


def dip_bucket(pct: Optional[float]) -> Optional[str]:
    if pct is None:
        return None
    if pct < 0.02:
        return "near_high"
    if pct < 0.06:
        return "modest_dip"
    return "deep_dip"


def close_loc(d: SessionDay) -> Optional[float]:
    rng = d.high - d.low
    if rng <= 0:
        return None
    return (d.close - d.low) / rng


def build_states(days: list[SessionDay]) -> list[DayState]:
    emas = ema20_series(days)
    out: list[DayState] = []
    for i, d in enumerate(days):
        r5 = signal_at(days, i, 5)
        r10 = signal_at(days, i, 10)
        r20 = signal_at(days, i, 20)
        r60 = signal_at(days, i, 60)
        ema = emas[i]
        ema_prev = emas[i - 1] if i else None
        bull_ema = None
        if ema is not None and ema_prev is not None:
            bull_ema = bool(d.close > ema and ema > ema_prev)
        p20 = pct_below_20h(days, i)
        out.append(
            DayState(
                date=d.date,
                bull_10=None if r10 is None else r10 > 0,
                bull_20=None if r20 is None else r20 > 0,
                bull_60=None if r60 is None else r60 > 0,
                bull_ema=bull_ema,
                bull_20_and_5=None if r20 is None or r5 is None else (r20 > 0 and r5 > 0),
                ret_5=r5,
                ret_10=r10,
                ret_20=r20,
                ret_60=r60,
                prior_1d=d.cc_clean,
                prior_2d=cum_clean(days, i - 1, i) if i >= 1 else None,
                prior_3d=cum_clean(days, i - 2, i) if i >= 2 else None,
                pct_below_20h=p20,
                close_loc=close_loc(d),
                rv20=rv20_at(days, i),
                dip_bucket=dip_bucket(p20),
            )
        )
    return out


def last_completed_state(states: Sequence[DayState], td: str) -> Optional[DayState]:
    """Last daily state with date strictly before RTH date td (no same-day leak)."""
    lo, hi = 0, len(states) - 1
    best = None
    while lo <= hi:
        mid = (lo + hi) // 2
        if states[mid].date < td:
            best = states[mid]
            lo = mid + 1
        else:
            hi = mid - 1
    return best


def _hhmm(ts: int) -> str:
    from datetime import datetime
    from zoneinfo import ZoneInfo

    return datetime.fromtimestamp(int(ts), tz=ZoneInfo("America/New_York")).strftime("%H:%M")


def rth_exit_bar(rth: Sequence[Bar], td: str) -> Optional[Bar]:
    flatten = flatten_ts(td)
    last = None
    for b in rth:
        if int(b.time) >= flatten:
            break
        last = b
    return last


def simulate_open_long(
    *,
    instrument: str,
    td: str,
    rth: Sequence[Bar],
    adverse_ticks: float = 1.0,
    flatten_hhmm: str = FLATTEN,
    candidate: str = "BULL_STATE_RTH_OPEN_LONG",
) -> LongTrade:
    spec = INSTRUMENTS[instrument]
    tick = float(spec["tick"])
    comm = float(spec["commission_points"])
    if not rth:
        return LongTrade(instrument=instrument, trading_date=td, candidate=candidate, direction="LONG", status="NO_BARS", year=int(td[:4]))
    entry = rth[0]
    cut = local_ts(td, flatten_hhmm)
    last = None
    for b in rth:
        if int(b.time) >= cut:
            break
        last = b
    if last is None:
        return LongTrade(instrument=instrument, trading_date=td, candidate=candidate, direction="LONG", status="NO_PATH", year=int(td[:4]))
    theo = float(entry.open)
    fill = theo + adverse_ticks * tick
    exit_theo = float(last.close)
    exit_fill = exit_theo - adverse_ticks * tick
    pts = exit_fill - fill
    pts_c = pts - comm
    path = [b for b in rth if int(b.time) < cut]
    mfe = max((float(b.high) - fill for b in path), default=0.0)
    mae = max((fill - float(b.low) for b in path), default=0.0)
    return LongTrade(
        instrument=instrument,
        trading_date=td,
        candidate=candidate,
        direction="LONG",
        status="ENTERED",
        outcome="TIME_EXIT",
        entry_ts=int(entry.time),
        entry_theo=theo,
        entry_fill=fill,
        exit_ts=int(last.time),
        exit_px=exit_fill,
        points=pts,
        points_after_cost=pts_c,
        mfe_points=mfe,
        mae_points=mae,
        hold_sec=int(last.time) - int(entry.time),
        year=int(td[:4]),
        signal_hhmm="09:30",
        extras={"adverse_ticks": adverse_ticks, "exit_theo": exit_theo, "flatten": flatten_hhmm},
    )


def find_first_red_green(
    rth: Sequence[Bar],
    td: str,
) -> Optional[dict[str, Any]]:
    rth0 = local_ts(td, "09:30")
    no_new = local_ts(td, NO_NEW)
    bars5 = five_minute_bars(rth, rth0)
    if len(bars5) < 3:
        return None
    red_i = None
    red_low = None
    for i, b in enumerate(bars5):
        if float(b.close) < float(b.open):
            red_i = i
            red_low = float(b.low)
            break
    if red_i is None:
        return None
    for i in range(red_i + 1, len(bars5)):
        b = bars5[i]
        red_low = min(red_low, float(b.low))
        if float(b.close) <= float(b.open):
            continue
        if i + 1 >= len(bars5):
            return None
        entry = bars5[i + 1]
        if int(entry.time) > no_new:
            return None
        return {
            "red_ts": int(bars5[red_i].time),
            "confirm_ts": int(b.time),
            "entry_ts": int(entry.time),
            "entry_theo": float(entry.open),
            "pullback_low": float(red_low),
        }
    return None


def simulate_red_green(
    *,
    instrument: str,
    td: str,
    rth: Sequence[Bar],
    setup: dict[str, Any],
    target_r: float = 1.0,
    adverse_ticks: float = 1.0,
    stop_buffer_ticks: float = 1.0,
    candidate: str = "LONG20_FIRST_RED_GREEN_5M",
) -> LongTrade:
    spec = INSTRUMENTS[instrument]
    tick = float(spec["tick"])
    comm = float(spec["commission_points"])
    theo = float(setup["entry_theo"])
    fill = theo + adverse_ticks * tick
    sl = float(setup["pullback_low"]) - stop_buffer_ticks * tick
    risk = fill - sl
    if risk <= tick * 0.5:
        return LongTrade(
            instrument=instrument,
            trading_date=td,
            candidate=candidate,
            direction="LONG",
            status="SKIP_TINY_RISK",
            year=int(td[:4]),
            extras={"risk": risk},
        )
    tp = fill + target_r * risk
    flatten = flatten_ts(td)
    path = [b for b in rth if int(b.time) >= int(setup["entry_ts"])]
    outcome, exit_ts, exit_px, mfe, mae = resolve_path(path, is_long=True, sl=sl, tp=tp, flatten=flatten)
    pts = r_mult = pts_c = r_c = hold = None
    status = "ENTERED"
    if outcome == "NO_PATH":
        status = "NO_PATH"
        outcome = None
    if exit_px is not None and outcome in ("TARGET_HIT", "STOP_HIT", "TIME_EXIT"):
        if outcome == "STOP_HIT":
            pts = -risk
            exit_px = sl
        elif outcome == "TARGET_HIT":
            pts = target_r * risk
            exit_px = tp
        else:
            pts = exit_px - fill
        pts_c = pts - comm
        r_mult = pts / risk
        r_c = pts_c / risk
        hold = None if exit_ts is None else int(exit_ts) - int(setup["entry_ts"])
    mfe_abs = 0.0 if mfe is None else float(mfe)
    mae_abs = 0.0 if mae is None else abs(float(mae))
    return LongTrade(
        instrument=instrument,
        trading_date=td,
        candidate=candidate,
        direction="LONG",
        status=status,
        outcome=outcome,
        entry_ts=int(setup["entry_ts"]),
        entry_theo=theo,
        entry_fill=fill,
        stop=sl,
        target=tp,
        risk_points=risk,
        target_r=target_r,
        exit_ts=exit_ts,
        exit_px=exit_px,
        points=pts,
        r_multiple=r_mult,
        points_after_cost=pts_c,
        r_after_cost=r_c,
        mfe_points=mfe_abs,
        mae_points=mae_abs,
        reach_1r=mfe_abs >= risk if risk > 0 else None,
        reach_2r=mfe_abs >= 2 * risk if risk > 0 else None,
        hold_sec=hold,
        year=int(td[:4]),
        signal_hhmm=_hhmm(int(setup["entry_ts"])),
        extras={"adverse_ticks": adverse_ticks, "stop_buffer_ticks": stop_buffer_ticks},
    )


def find_atr_pullback(rth: Sequence[Bar], td: str, k: float = 0.5) -> Optional[dict[str, Any]]:
    rth0 = local_ts(td, "09:30")
    no_new = local_ts(td, NO_NEW)
    bars5 = five_minute_bars(rth, rth0)
    if len(bars5) < 16:
        return None
    trs = []
    for i in range(1, 15):
        prev = float(bars5[i - 1].close)
        h, l = float(bars5[i].high), float(bars5[i].low)
        trs.append(max(h - l, abs(h - prev), abs(l - prev)))
    atr = sum(trs) / len(trs) if trs else None
    if not atr or atr <= 0:
        return None
    sess_high = float(bars5[0].high)
    armed = False
    extreme = None
    for i, b in enumerate(bars5):
        sess_high = max(sess_high, float(b.high))
        if not armed:
            if sess_high - float(b.low) >= k * atr:
                armed = True
                extreme = float(b.low)
            continue
        extreme = min(extreme, float(b.low))
        if float(b.close) > float(b.open) and i + 1 < len(bars5):
            entry = bars5[i + 1]
            if int(entry.time) > no_new:
                return None
            return {
                "entry_ts": int(entry.time),
                "entry_theo": float(entry.open),
                "pullback_low": float(extreme),
            }
    return None


def simulate_mode2_long(
    *,
    instrument: str,
    days: Sequence[SessionDay],
    bull: Sequence[Optional[bool]],
    hold: int,
    adverse_ticks: float = 1.0,
    candidate: str = "LONG20_HOLD_N_SESSIONS",
) -> list[TsmomTrade]:
    """Long-only: if day i is bullish, buy day i+1 open, exit day i+hold close.

    Hold=1 is next-session open-to-close. Never short. Roll overnight is
    neutralized the same way as Phase 40 `simulate_fixed_hold`.
    """
    spec = INSTRUMENTS[instrument]
    tick = float(spec["tick"])
    comm = float(spec["commission_points"])
    trades: list[TsmomTrade] = []
    n = len(days)
    for i in range(n - 1):
        if not bull[i]:
            continue
        entry_i = i + 1
        exit_i = i + hold
        if exit_i >= n:
            break
        entry_theo = days[entry_i].open
        exit_theo = days[exit_i].close
        entry_fill = entry_theo + adverse_ticks * tick
        exit_fill = exit_theo - adverse_ticks * tick
        on_pts = 0.0
        sess_pts = days[entry_i].close - days[entry_i].open
        wknd = 0.0
        rolls = 0
        exec_pts = days[entry_i].close - entry_fill
        for j in range(entry_i + 1, exit_i + 1):
            d = days[j]
            prev = days[j - 1]
            if d.is_roll:
                rolls += 1
                chunk_on = 0.0
            else:
                chunk_on = d.open - prev.close
            chunk_sess = d.close - d.open
            on_pts += chunk_on
            sess_pts += chunk_sess
            exec_pts += chunk_on + chunk_sess
            if d.is_weekend_gap and not d.is_roll:
                wknd += chunk_on
        exec_pts += exit_fill - days[exit_i].close
        trades.append(
            TsmomTrade(
                instrument=instrument,
                signal_date=days[i].date,
                entry_date=days[entry_i].date,
                exit_date=days[exit_i].date,
                direction="LONG",
                lookback=20,
                hold=hold,
                mode="long_only_hold",
                status="ENTERED",
                entry_theo=entry_theo,
                entry_fill=entry_fill,
                exit_theo=exit_theo,
                exit_fill=exit_fill,
                points=exec_pts,
                points_after_cost=exec_pts - comm,
                overnight_points=on_pts,
                session_points=sess_pts,
                weekend_points=wknd,
                n_roll_nights=rolls,
                hold_days=hold,
                year=int(days[entry_i].date[:4]),
                extras={"candidate": candidate, "adverse_ticks": adverse_ticks},
            )
        )
    return trades

