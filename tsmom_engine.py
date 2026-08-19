"""Phase 40 — leak-safe daily time-series momentum (research-only).

Signal uses only completed session closes. Entry is the next session open.
Roll-gap overnight moves are neutralized in the signal series.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional, Sequence

from models import Bar

INSTRUMENTS = {
    "ES": {"tick": 0.25, "point_usd": 50.0, "commission_points": 0.08},
    "NQ": {"tick": 0.25, "point_usd": 20.0, "commission_points": 0.20},
    "GC": {"tick": 0.10, "point_usd": 100.0, "commission_points": 0.04},
}


@dataclass
class SessionDay:
    date: str
    ts: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    prev_close: Optional[float] = None
    overnight_ret: Optional[float] = None
    session_ret: Optional[float] = None
    cc_raw: Optional[float] = None
    is_roll: bool = False
    is_weekend_gap: bool = False
    cc_clean: Optional[float] = None
    year: Optional[int] = None


@dataclass
class TsmomTrade:
    instrument: str
    signal_date: str
    entry_date: str
    exit_date: str
    direction: str
    lookback: int
    hold: Any
    mode: str
    status: str
    signal_ret: Optional[float] = None
    entry_theo: Optional[float] = None
    entry_fill: Optional[float] = None
    exit_theo: Optional[float] = None
    exit_fill: Optional[float] = None
    points: Optional[float] = None
    points_after_cost: Optional[float] = None
    overnight_points: Optional[float] = None
    session_points: Optional[float] = None
    weekend_points: Optional[float] = None
    n_roll_nights: int = 0
    hold_days: Optional[int] = None
    year: Optional[int] = None
    rv20: Optional[float] = None
    vol_weight: Optional[float] = None
    extras: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def ny_date(ts: int) -> str:
    """Globex daily bar date = UTC calendar date of Databento ts_event (00:00 UTC).

    That date is Mon–Fri for weekday sessions and Sunday for the week-open
    Globex stub. Synthetic tests stamped at 10:00 ET keep the same calendar date.
    """
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).date().isoformat()


def bars_to_days(bars: Sequence[Bar], *, keep_sunday: bool = False) -> list[SessionDay]:
    rows = []
    prev: Optional[SessionDay] = None
    ordered = sorted(bars, key=lambda b: int(b.time))
    for b in ordered:
        ds = ny_date(int(b.time))
        wd = datetime.fromisoformat(ds).weekday()
        if wd == 6 and not keep_sunday:
            continue
        d = SessionDay(
            date=ds,
            ts=int(b.time),
            open=float(b.open),
            high=float(b.high),
            low=float(b.low),
            close=float(b.close),
            volume=float(b.volume or 0),
            year=int(ds[:4]),
        )
        if prev is not None and prev.close > 0:
            d.prev_close = prev.close
            d.overnight_ret = d.open / prev.close - 1.0
            d.session_ret = d.close / d.open - 1.0 if d.open else None
            d.cc_raw = d.close / prev.close - 1.0
            try:
                prev_wd = datetime.fromisoformat(prev.date).weekday()
                cur_wd = datetime.fromisoformat(d.date).weekday()
                d.is_weekend_gap = prev_wd == 4 and cur_wd == 0
            except ValueError:
                d.is_weekend_gap = False
        rows.append(d)
        prev = d
    return rows


def mark_rolls(days: list[SessionDay], tick: float, roll_ts: Optional[set[int]] = None) -> None:
    """Neutralize contract-switch gaps, not ordinary overnight moves.

    Prefer `roll_ts` from Databento instrument_id changes. Fallback: gap must
    exceed both 8x the trailing-60 median |overnight points| and 80 bps of
    prior close. The original 15-tick floor over-flagged normal ES nights.
    """
    if roll_ts:
        for d in days:
            if d.prev_close is None:
                d.cc_clean = d.cc_raw
                continue
            d.is_roll = int(d.ts) in roll_ts
            d.cc_clean = d.session_ret if d.is_roll else d.cc_raw
        return
    abs_on_pts: list[float] = []
    for d in days:
        if d.prev_close is None:
            continue
        gap_pts = abs(d.open - d.prev_close)
        window = abs_on_pts[-60:]
        med = sorted(window)[len(window) // 2] if len(window) >= 20 else None
        d.is_roll = bool(
            med is not None
            and med > 0
            and gap_pts > 8.0 * med
            and gap_pts > 0.008 * d.prev_close
        )
        d.cc_clean = d.session_ret if d.is_roll else d.cc_raw
        abs_on_pts.append(gap_pts)


def cum_clean(days: list[SessionDay], start: int, end: int) -> Optional[float]:
    """Product of cc_clean over days[start:end+1] inclusive. All must exist."""
    if start < 0 or end >= len(days) or start > end:
        return None
    acc = 1.0
    for i in range(start, end + 1):
        r = days[i].cc_clean
        if r is None:
            return None
        acc *= 1.0 + r
    return acc - 1.0


def signal_at(days: list[SessionDay], i: int, lookback: int) -> Optional[float]:
    """Lookback return ending at completed day i (inclusive)."""
    return cum_clean(days, i - lookback + 1, i)


def rv20(days: list[SessionDay], i: int) -> Optional[float]:
    xs = [days[j].cc_clean for j in range(max(0, i - 19), i + 1) if days[j].cc_clean is not None]
    if len(xs) < 15:
        return None
    m = sum(xs) / len(xs)
    var = sum((x - m) ** 2 for x in xs) / (len(xs) - 1)
    return var ** 0.5


def vol_weights_series(days: list[SessionDay]) -> list[Optional[float]]:
    rvs = [rv20(days, i) for i in range(len(days))]
    past: list[float] = []
    out: list[Optional[float]] = [None] * len(days)
    for i, cur in enumerate(rvs):
        if cur is None or cur <= 0:
            continue
        past.append(cur)
        if len(past) < 20:
            out[i] = 1.0
        else:
            med = sorted(past)[len(past) // 2]
            out[i] = min(4.0, max(0.25, med / cur))
    return out


def ma_signal_at(days: list[SessionDay], i: int, lookback: int) -> Optional[float]:
    if i < lookback - 1:
        return None
    sma = sum(days[j].close for j in range(i - lookback + 1, i + 1)) / lookback
    c = days[i].close
    if c > sma:
        return 1.0
    if c < sma:
        return -1.0
    return 0.0


def donchian_signal_at(days: list[SessionDay], i: int, lookback: int) -> Optional[float]:
    """close vs prior N-day high/low, excluding day i."""
    start = i - lookback
    if start < 0:
        return None
    prior_high = max(days[j].high for j in range(start, i))
    prior_low = min(days[j].low for j in range(start, i))
    c = days[i].close
    if c > prior_high:
        return 1.0
    if c < prior_low:
        return -1.0
    return 0.0


def fwd_exec_points(days: list[SessionDay], start: int, end: int, is_long: bool) -> tuple[float, float, float, float, int]:
    """Points from close[start] to close[end], neutralizing roll overnight. Also split ON vs session."""
    pts = 0.0
    on_pts = 0.0
    sess_pts = 0.0
    wknd = 0.0
    rolls = 0
    sign = 1.0 if is_long else -1.0
    for j in range(start + 1, end + 1):
        d = days[j]
        prev = days[j - 1]
        if d.is_roll:
            rolls += 1
            chunk_on = 0.0
        else:
            chunk_on = (d.open - prev.close) * sign
        chunk_sess = (d.close - d.open) * sign
        on_pts += chunk_on
        sess_pts += chunk_sess
        pts += chunk_on + chunk_sess
        if d.is_weekend_gap and not d.is_roll:
            wknd += chunk_on
    return pts, on_pts, sess_pts, wknd, rolls


def simulate_fixed_hold(
    *,
    instrument: str,
    days: list[SessionDay],
    lookback: int,
    hold: int,
    adverse_ticks: float,
    overlapping: bool = False,
    signal_fn=None,
) -> list[TsmomTrade]:
    spec = INSTRUMENTS[instrument]
    tick = float(spec["tick"])
    comm = float(spec["commission_points"])
    trades: list[TsmomTrade] = []
    weights = vol_weights_series(days)
    i = lookback
    sig_fn = signal_fn or signal_at
    while i < len(days) - 1:
        sig = sig_fn(days, i, lookback)
        if sig is None or sig == 0:
            i += 1
            continue
        direction = "LONG" if sig > 0 else "SHORT"
        is_long = direction == "LONG"
        entry_i = i + 1
        exit_i = i + hold
        if exit_i >= len(days):
            break
        entry_theo = days[entry_i].open
        exit_theo = days[exit_i].close
        entry_fill = entry_theo + adverse_ticks * tick if is_long else entry_theo - adverse_ticks * tick
        exit_fill = exit_theo - adverse_ticks * tick if is_long else exit_theo + adverse_ticks * tick
        sign = 1.0 if is_long else -1.0
        roll_gap = 0.0
        on_pts = 0.0
        sess_pts = (days[entry_i].close - days[entry_i].open) * sign
        wknd = 0.0
        rolls = 0
        for j in range(entry_i + 1, exit_i + 1):
            d = days[j]
            prev = days[j - 1]
            if d.is_roll:
                rolls += 1
                roll_gap += (d.open - prev.close) * sign
                chunk_on = 0.0
            else:
                chunk_on = (d.open - prev.close) * sign
            chunk_sess = (d.close - d.open) * sign
            on_pts += chunk_on
            sess_pts += chunk_sess
            if d.is_weekend_gap and not d.is_roll:
                wknd += chunk_on
        pts = (exit_fill - entry_fill) * sign - roll_gap
        pts_c = pts - comm
        rvol = rv20(days, i)
        w = weights[i] if i < len(weights) else None
        trades.append(
            TsmomTrade(
                instrument=instrument,
                signal_date=days[i].date,
                entry_date=days[entry_i].date,
                exit_date=days[exit_i].date,
                direction=direction,
                lookback=lookback,
                hold=hold,
                mode="fixed_hold",
                status="ENTERED",
                signal_ret=sig,
                entry_theo=entry_theo,
                entry_fill=entry_fill,
                exit_theo=exit_theo,
                exit_fill=exit_fill,
                points=pts,
                points_after_cost=pts_c,
                overnight_points=on_pts,
                session_points=sess_pts,
                weekend_points=wknd,
                n_roll_nights=rolls,
                hold_days=hold,
                year=days[entry_i].year,
                rv20=rvol,
                vol_weight=w,
            )
        )
        i = i + 1 if overlapping else exit_i
    return trades


def simulate_daily_refresh(
    *,
    instrument: str,
    days: list[SessionDay],
    lookback: int,
    adverse_ticks: float,
) -> list[TsmomTrade]:
    spec = INSTRUMENTS[instrument]
    tick = float(spec["tick"])
    comm = float(spec["commission_points"])
    trades: list[TsmomTrade] = []

    def run_points(entry_i: int, last_full_i: int, is_long: bool, entry_fill: float, exit_px: float) -> tuple[float, float, float, float, int]:
        sign = 1.0 if is_long else -1.0
        roll_gap = 0.0
        on_pts = 0.0
        sess_pts = (days[entry_i].close - days[entry_i].open) * sign
        wknd = 0.0
        rolls = 0
        for j in range(entry_i + 1, last_full_i + 1):
            d = days[j]
            prev = days[j - 1]
            if d.is_roll:
                rolls += 1
                roll_gap += (d.open - prev.close) * sign
                chunk_on = 0.0
            else:
                chunk_on = (d.open - prev.close) * sign
            on_pts += chunk_on
            sess_pts += (d.close - d.open) * sign
            if d.is_weekend_gap and not d.is_roll:
                wknd += chunk_on
        pts = (exit_px - entry_fill) * sign - roll_gap
        return pts, on_pts, sess_pts, wknd, rolls

    i = lookback
    while i < len(days) - 1:
        sig = signal_at(days, i, lookback)
        if sig is None or sig == 0.0:
            i += 1
            continue
        is_long = sig > 0
        direction = "LONG" if is_long else "SHORT"
        entry_i = i + 1
        entry_theo = days[entry_i].open
        entry_fill = entry_theo + adverse_ticks * tick if is_long else entry_theo - adverse_ticks * tick
        j = i
        while j + 1 < len(days) - 1:
            nxt = signal_at(days, j + 1, lookback)
            if nxt is None or nxt == 0.0 or (nxt > 0) != is_long:
                break
            j += 1
        # j is last close with same (or zero skipped handled by break). Reverse at open after a different signal.
        flip_sig = signal_at(days, j + 1, lookback) if j + 1 < len(days) - 1 else None
        if flip_sig is not None and flip_sig != 0 and (flip_sig > 0) != is_long and j + 2 < len(days):
            exit_i = j + 2
            exit_theo = days[exit_i].open
            exit_fill = exit_theo - adverse_ticks * tick if is_long else exit_theo + adverse_ticks * tick
            last_full = exit_i - 1
        else:
            exit_i = len(days) - 1
            exit_theo = days[exit_i].close
            exit_fill = exit_theo - adverse_ticks * tick if is_long else exit_theo + adverse_ticks * tick
            last_full = exit_i
        pts, on_pts, sess_pts, wknd, rolls = run_points(entry_i, last_full, is_long, entry_fill, exit_fill)
        trades.append(
            TsmomTrade(
                instrument=instrument,
                signal_date=days[i].date,
                entry_date=days[entry_i].date,
                exit_date=days[exit_i].date,
                direction=direction,
                lookback=lookback,
                hold="daily_refresh",
                mode="daily_refresh",
                status="ENTERED",
                signal_ret=sig,
                entry_theo=entry_theo,
                entry_fill=entry_fill,
                exit_theo=exit_theo,
                exit_fill=exit_fill,
                points=pts,
                points_after_cost=pts - comm,
                overnight_points=on_pts,
                session_points=sess_pts,
                weekend_points=wknd,
                n_roll_nights=rolls,
                hold_days=last_full - entry_i + 1,
                year=days[entry_i].year,
                rv20=rv20(days, i),
            )
        )
        if flip_sig is not None and flip_sig != 0 and (flip_sig > 0) != is_long:
            i = j + 1
        else:
            break
    return trades


def simulate_same_session(
    *,
    instrument: str,
    days: list[SessionDay],
    lookback: int,
    adverse_ticks: float,
) -> list[TsmomTrade]:
    """Mode 2: signal at close t, enter open t+1, exit close t+1. No overnight."""
    spec = INSTRUMENTS[instrument]
    tick = float(spec["tick"])
    comm = float(spec["commission_points"])
    trades = []
    for i in range(lookback, len(days) - 1):
        sig = signal_at(days, i, lookback)
        if sig is None or sig == 0:
            continue
        d = days[i + 1]
        is_long = sig > 0
        entry_theo = d.open
        exit_theo = d.close
        entry_fill = entry_theo + adverse_ticks * tick if is_long else entry_theo - adverse_ticks * tick
        exit_fill = exit_theo - adverse_ticks * tick if is_long else exit_theo + adverse_ticks * tick
        pts = (exit_fill - entry_fill) * (1.0 if is_long else -1.0)
        trades.append(
            TsmomTrade(
                instrument=instrument,
                signal_date=days[i].date,
                entry_date=d.date,
                exit_date=d.date,
                direction="LONG" if is_long else "SHORT",
                lookback=lookback,
                hold=1,
                mode="same_session",
                status="ENTERED",
                signal_ret=sig,
                entry_theo=entry_theo,
                entry_fill=entry_fill,
                exit_theo=exit_theo,
                exit_fill=exit_fill,
                points=pts,
                points_after_cost=pts - comm,
                overnight_points=0.0,
                session_points=(d.close - d.open) * (1.0 if is_long else -1.0),
                weekend_points=0.0,
                hold_days=1,
                year=d.year,
                rv20=rv20(days, i),
            )
        )
    return trades
