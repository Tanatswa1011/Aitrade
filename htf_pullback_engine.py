"""Phase 42 — leak-safe HTF trend + first intraday pullback (research-only).

VWAP is diagnostic only. No DVP logic. Completed 1h/4h bars only.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from typing import Any, Optional, Sequence
from zoneinfo import ZoneInfo

from models import Bar
from nq_pdh_pdl import local_ts, ny_date
from orb_index_engine import INSTRUMENTS, flatten_ts, resolve_path

NY = ZoneInfo("America/New_York")
TICK = 0.25
THRESH = 0.002
MIN_IMPULSE_TICKS = 8
SHALLOW = (0.25, 0.40)
MEDIUM = (0.40, 0.60)
DEEP = (0.60, 0.75)
CANCEL_BEYOND = 0.75
NO_NEW = "15:30"
CONFIRM_A = "candle"
CONFIRM_B = "break"


@dataclass
class HtfBar:
    time: int
    close_ts: int
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


@dataclass
class HtfState:
    side: str  # BULLISH | BEARISH | NEUTRAL
    ret: Optional[float]
    age: int
    strength: str
    last_close_ts: Optional[int]
    last_close: Optional[float]
    ema20: Optional[float]
    ema20_rising: Optional[bool]
    structure: str


@dataclass
class PullbackSetup:
    instrument: str
    trading_date: str
    candidate: str
    direction: str
    horizon: str
    htf_return: float
    htf_age: int
    htf_strength: str
    retracement: float
    depth_bucket: str
    impulse_high: float
    impulse_low: float
    rth_open: float
    tag_ts: int
    confirm_ts: int
    entry_ts: int
    entry_theo: float
    pullback_extreme: float
    confirm_kind: str
    vwap_at_entry: Optional[float] = None
    vwap_aligned: Optional[bool] = None
    trend_1h: Optional[str] = None
    trend_4h: Optional[str] = None
    aligned_1h_4h: Optional[bool] = None
    gap_points: Optional[float] = None
    prior_ret: Optional[float] = None
    atr_5m: Optional[float] = None
    extras: dict[str, Any] = field(default_factory=dict)


@dataclass
class HtfTrade:
    instrument: str
    trading_date: str
    candidate: str
    direction: str
    status: str
    outcome: Optional[str] = None
    horizon: Optional[str] = None
    htf_return: Optional[float] = None
    htf_age: Optional[int] = None
    htf_strength: Optional[str] = None
    retracement: Optional[float] = None
    depth_bucket: Optional[str] = None
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
    vwap_aligned: Optional[bool] = None
    aligned_1h_4h: Optional[bool] = None
    extras: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _hhmm(ts: int) -> str:
    return datetime.fromtimestamp(int(ts), tz=NY).strftime("%H:%M")


def depth_bucket(ret: float) -> Optional[str]:
    if SHALLOW[0] <= ret < SHALLOW[1]:
        return "shallow"
    if MEDIUM[0] <= ret <= MEDIUM[1]:
        return "medium"
    if DEEP[0] < ret <= DEEP[1]:
        return "deep"
    return None


def strength_bucket(abs_ret: float) -> str:
    if abs_ret < 0.004:
        return "weak"
    if abs_ret < 0.008:
        return "medium"
    return "strong"


def age_bucket(age: int) -> str:
    if age <= 1:
        return "new_1"
    if age == 2:
        return "age_2"
    if age <= 5:
        return "age_3_5"
    return "older_6plus"


def trend_side(ret: Optional[float], thresh: float = THRESH) -> str:
    if ret is None:
        return "NEUTRAL"
    if ret >= thresh:
        return "BULLISH"
    if ret <= -thresh:
        return "BEARISH"
    return "NEUTRAL"


def _bucket_start_clock(ts: int, period_sec: int) -> int:
    dt = datetime.fromtimestamp(int(ts), tz=NY)
    if period_sec == 300:
        minute = (dt.minute // 5) * 5
        start = dt.replace(minute=minute, second=0, microsecond=0)
    elif period_sec == 3600:
        start = dt.replace(minute=0, second=0, microsecond=0)
    else:
        raise ValueError(period_sec)
    return int(start.timestamp())


def _h4_globex_start(ts: int) -> int:
    dt = datetime.fromtimestamp(int(ts), tz=NY)
    if dt.hour >= 18:
        open_dt = dt.replace(hour=18, minute=0, second=0, microsecond=0)
    else:
        prev = dt.date() - timedelta(days=1)
        open_dt = datetime(prev.year, prev.month, prev.day, 18, 0, tzinfo=NY)
    open_ts = int(open_dt.timestamp())
    offset = int(ts) - open_ts
    if offset < 0:
        return open_ts
    return open_ts + (offset // 14400) * 14400


def aggregate_bars(bars: Sequence[Bar], period_sec: int, *, globex_4h: bool = False) -> list[HtfBar]:
    buckets: dict[int, list[float]] = {}
    for b in bars:
        ts = int(b.time)
        start = _h4_globex_start(ts) if globex_4h else _bucket_start_clock(ts, period_sec)
        rec = buckets.get(start)
        o, h, l, c, v = float(b.open), float(b.high), float(b.low), float(b.close), float(b.volume or 0.0)
        if rec is None:
            buckets[start] = [o, h, l, c, v]
        else:
            if h > rec[1]:
                rec[1] = h
            if l < rec[2]:
                rec[2] = l
            rec[3] = c
            rec[4] += v
    out: list[HtfBar] = []
    period = 14400 if globex_4h else period_sec
    for start in sorted(buckets):
        o, h, l, c, v = buckets[start]
        out.append(HtfBar(time=start, close_ts=start + period, open=o, high=h, low=l, close=c, volume=v))
    return out


def last_completed_index(series: Sequence[HtfBar], as_of_ts: int) -> int:
    """Last bar whose close_ts <= as_of_ts. -1 if none."""
    lo, hi = 0, len(series) - 1
    best = -1
    t = int(as_of_ts)
    while lo <= hi:
        mid = (lo + hi) // 2
        if series[mid].close_ts <= t:
            best = mid
            lo = mid + 1
        else:
            hi = mid - 1
    return best


def htf_return_at(series: Sequence[HtfBar], as_of_ts: int, n_intervals: int) -> Optional[float]:
    i = last_completed_index(series, as_of_ts)
    if i < n_intervals:
        return None
    older = series[i - n_intervals].close
    if older == 0:
        return None
    return series[i].close / older - 1.0


def _ema_at(series: Sequence[HtfBar], idx: int, span: int = 20) -> Optional[float]:
    if idx < span - 1:
        return None
    k = 2.0 / (span + 1)
    ema = series[idx - span + 1].close
    for j in range(idx - span + 2, idx + 1):
        ema = series[j].close * k + ema * (1.0 - k)
    return ema


def _structure_at(series: Sequence[HtfBar], idx: int) -> str:
    if idx < 3:
        return "INSUFFICIENT"
    a, b, c = series[idx - 2], series[idx - 1], series[idx]
    hh = c.high > b.high > a.high
    hl = c.low > b.low > a.low
    lh = c.high < b.high < a.high
    ll = c.low < b.low < a.low
    if hh and hl:
        return "HH_HL"
    if lh and ll:
        return "LH_LL"
    return "MIXED"


def trend_age_at(series: Sequence[HtfBar], as_of_ts: int, n_intervals: int, thresh: float = THRESH) -> int:
    i = last_completed_index(series, as_of_ts)
    if i < n_intervals:
        return 0
    side = trend_side(htf_return_at(series, series[i].close_ts, n_intervals), thresh)
    if side == "NEUTRAL":
        return 0
    age = 0
    for k in range(i, n_intervals - 1, -1):
        ret = htf_return_at(series, series[k].close_ts, n_intervals)
        if trend_side(ret, thresh) != side:
            break
        age += 1
        if age >= 40:
            break
    return age


_HTF_CACHE: dict[tuple[int, int, float, int], tuple[Sequence[HtfBar], HtfState]] = {}


def htf_state(series: Sequence[HtfBar], as_of_ts: int, n_intervals: int, thresh: float = THRESH) -> HtfState:
    i = last_completed_index(series, as_of_ts)
    key = (id(series), n_intervals, float(thresh), i)
    hit = _HTF_CACHE.get(key)
    if hit is not None and hit[0] is series:
        return hit[1]
    ret = None if i < n_intervals else (
        None if series[i - n_intervals].close == 0 else series[i].close / series[i - n_intervals].close - 1.0
    )
    side = trend_side(ret, thresh)
    ema = _ema_at(series, i) if i >= 0 else None
    ema_prev = _ema_at(series, i - 1) if i >= 1 else None
    rising = None if ema is None or ema_prev is None else ema > ema_prev
    age = 0
    if side != "NEUTRAL" and i >= n_intervals:
        for k in range(i, n_intervals - 1, -1):
            older = series[k - n_intervals].close
            if older == 0:
                break
            r = series[k].close / older - 1.0
            if trend_side(r, thresh) != side:
                break
            age += 1
            if age >= 40:
                break
    st = HtfState(
        side=side,
        ret=ret,
        age=age,
        strength="none" if ret is None else strength_bucket(abs(ret)),
        last_close_ts=None if i < 0 else series[i].close_ts,
        last_close=None if i < 0 else series[i].close,
        ema20=ema,
        ema20_rising=rising,
        structure="INSUFFICIENT" if i < 0 else _structure_at(series, i),
    )
    # Retaining the owner prevents Python from recycling its id while cached and
    # makes an identity collision fail closed instead of returning foreign state.
    if len(_HTF_CACHE) >= 4096:
        _HTF_CACHE.clear()
    _HTF_CACHE[key] = (series, st)
    return st


def atr_from_series(series: Sequence[HtfBar], as_of_ts: int, period: int = 14) -> Optional[float]:
    i = last_completed_index(series, as_of_ts)
    if i < period:
        return None
    trs = []
    for j in range(i - period + 1, i + 1):
        prev = series[j - 1].close
        h, l = series[j].high, series[j].low
        trs.append(max(h - l, abs(h - prev), abs(l - prev)))
    return sum(trs) / len(trs)


def index_htf_by_date(series: Sequence[HtfBar]) -> dict[str, list[HtfBar]]:
    out: dict[str, list[HtfBar]] = {}
    for b in series:
        d = ny_date(b.time)
        out.setdefault(d, []).append(b)
    return out


def _rth_5m(bars5: Sequence[HtfBar], td: str) -> list[HtfBar]:
    start = local_ts(td, "09:30")
    end = local_ts(td, "16:00")
    return [b for b in bars5 if start <= b.time < end]


def _session_vwap_at(rth_1m: Sequence[Bar], ts: int) -> Optional[float]:
    num = den = 0.0
    for b in rth_1m:
        if int(b.time) > int(ts):
            break
        v = float(b.volume or 0.0)
        if v <= 0:
            continue
        tp = (float(b.high) + float(b.low) + float(b.close)) / 3.0
        num += tp * v
        den += v
    if den <= 0:
        return None
    return num / den


def _tod_bucket(hhmm: str) -> str:
    if hhmm < "10:30":
        return "0930_1030"
    if hhmm < "12:00":
        return "1030_1200"
    if hhmm < "14:00":
        return "1200_1400"
    return "1400_1530"


def find_first_pullback_setup(
    *,
    instrument: str,
    td: str,
    rth_1m: Sequence[Bar],
    bars5: Sequence[HtfBar],
    h1: Sequence[HtfBar],
    h4: Sequence[HtfBar],
    horizon: str,
    confirm_kind: str,
    depth: tuple[float, float] = MEDIUM,
    thresh: float = THRESH,
    gap_points: Optional[float] = None,
    prior_ret: Optional[float] = None,
) -> Optional[PullbackSetup]:
    rth5 = _rth_5m(bars5, td)
    if len(rth5) < 8:
        return None
    n_int = 4 if horizon == "1h" else 3
    series = h1 if horizon == "1h" else h4
    no_new = local_ts(td, NO_NEW)
    rth_open = float(rth_1m[0].open) if rth_1m else rth5[0].open
    min_imp = MIN_IMPULSE_TICKS * TICK
    sess_high = rth_open
    sess_low = rth_open
    armed = False
    armed_long = False
    tag_i = None
    tag_ret = None
    extreme = None
    impulse_h = impulse_l = rth_open

    for i, bar in enumerate(rth5):
        decision_ts = bar.close_ts  # completed 5m only
        st = htf_state(series, decision_ts, n_int, thresh)
        if bar.high > sess_high:
            sess_high = bar.high
        if bar.low < sess_low:
            sess_low = bar.low
        if not armed:
            if st.side == "NEUTRAL":
                continue
            if st.side == "BULLISH":
                rng = sess_high - rth_open
                if rng < min_imp:
                    continue
                retr = (sess_high - bar.low) / rng
                if depth[0] <= retr <= depth[1]:
                    armed = True
                    armed_long = True
                    tag_i = i
                    tag_ret = retr
                    extreme = bar.low
                    impulse_h, impulse_l = sess_high, rth_open
            else:
                rng = rth_open - sess_low
                if rng < min_imp:
                    continue
                retr = (bar.high - sess_low) / rng
                if depth[0] <= retr <= depth[1]:
                    armed = True
                    armed_long = False
                    tag_i = i
                    tag_ret = retr
                    extreme = bar.high
                    impulse_h, impulse_l = rth_open, sess_low
            continue

        # armed: update extreme; cancel if HTF dies or retracement > 75%
        is_long = armed_long
        wanted_side = "BULLISH" if is_long else "BEARISH"
        if st.side != wanted_side:
            return None
        if is_long:
            if bar.low < extreme:
                extreme = bar.low
            rng = impulse_h - rth_open
            retr_now = (impulse_h - extreme) / rng if rng > 0 else 0.0
            if retr_now > CANCEL_BEYOND:
                return None
        else:
            if bar.high > extreme:
                extreme = bar.high
            rng = rth_open - impulse_l
            retr_now = (extreme - impulse_l) / rng if rng > 0 else 0.0
            if retr_now > CANCEL_BEYOND:
                return None

        if i <= (tag_i or 0):
            continue
        confirmed = False
        if confirm_kind == CONFIRM_A:
            if is_long and bar.close > bar.open:
                confirmed = True
            if (not is_long) and bar.close < bar.open:
                confirmed = True
        else:
            prev = rth5[i - 1]
            if is_long and bar.high > prev.high:
                confirmed = True
            if (not is_long) and bar.low < prev.low:
                confirmed = True
        if not confirmed:
            continue
        if i + 1 >= len(rth5):
            return None
        entry = rth5[i + 1]
        if entry.time >= no_new:
            return None
        vwap = _session_vwap_at(rth_1m, entry.time)
        direction = "LONG" if is_long else "SHORT"
        aligned = None
        if vwap is not None:
            aligned = (direction == "LONG" and entry.open >= vwap) or (direction == "SHORT" and entry.open <= vwap)
        s1 = htf_state(h1, entry.time, 4, thresh)
        s4 = htf_state(h4, entry.time, 3, thresh)
        both = s1.side == s4.side and s1.side != "NEUTRAL"
        cand = "HTF_1H_TREND_FIRST_PULLBACK_5M_CONFIRM"
        if horizon == "1h" and confirm_kind == CONFIRM_B:
            cand = "HTF_1H_TREND_FIRST_PULLBACK_5M_BREAK"
        if horizon == "4h":
            cand = "HTF_4H_TREND_FIRST_PULLBACK_5M_CONFIRM"
        return PullbackSetup(
            instrument=instrument,
            trading_date=td,
            candidate=cand,
            direction=direction,
            horizon=horizon,
            htf_return=float(st.ret or 0.0),
            htf_age=int(st.age),
            htf_strength=st.strength,
            retracement=float(tag_ret or 0.0),
            depth_bucket=depth_bucket(float(tag_ret or 0.0)) or "medium",
            impulse_high=impulse_h,
            impulse_low=impulse_l,
            rth_open=rth_open,
            tag_ts=rth5[tag_i].time if tag_i is not None else bar.time,
            confirm_ts=bar.time,
            entry_ts=entry.time,
            entry_theo=float(entry.open),
            pullback_extreme=float(extreme),
            confirm_kind=confirm_kind,
            vwap_at_entry=vwap,
            vwap_aligned=aligned,
            trend_1h=s1.side,
            trend_4h=s4.side,
            aligned_1h_4h=both,
            gap_points=gap_points,
            prior_ret=prior_ret,
            atr_5m=atr_from_series(bars5, entry.time),
            extras={"tod": _tod_bucket(_hhmm(entry.time)), "ema20_rising": s1.ema20_rising, "structure_1h": s1.structure},
        )
    return None


def first_depth_event(
    *,
    td: str,
    rth_1m: Sequence[Bar],
    bars5: Sequence[HtfBar],
    h1: Sequence[HtfBar],
    thresh: float = THRESH,
) -> Optional[dict[str, Any]]:
    """First 25-75% pullback after 1h trend is live (completed bars only). Structural."""
    rth5 = _rth_5m(bars5, td)
    if len(rth5) < 4 or not rth_1m:
        return None
    flatten = flatten_ts(td)
    rth_open = float(rth_1m[0].open)
    min_imp = MIN_IMPULSE_TICKS * TICK
    sess_high = sess_low = rth_open
    live_side = None
    live_st = None
    for bar in rth5:
        st = htf_state(h1, bar.close_ts, 4, thresh)
        if bar.high > sess_high:
            sess_high = bar.high
        if bar.low < sess_low:
            sess_low = bar.low
        if live_side is None:
            if st.side == "NEUTRAL":
                continue
            live_side = st.side
            live_st = st
        if st.side != live_side:
            return None
        is_long = live_side == "BULLISH"
        if is_long:
            rng = sess_high - rth_open
            if rng < min_imp:
                continue
            retr = (sess_high - bar.low) / rng
        else:
            rng = rth_open - sess_low
            if rng < min_imp:
                continue
            retr = (bar.high - sess_low) / rng
        buck = depth_bucket(retr)
        if buck is None:
            if retr > CANCEL_BEYOND:
                return None
            continue
        path = [b for b in rth_1m if int(b.time) > bar.close_ts and int(b.time) < flatten]
        if is_long:
            cont_ext = any(float(b.high) > sess_high for b in path)
            close_with = float(rth_1m[-1].close) > rth_open
            mfe = max((float(b.high) - float(bar.close) for b in path), default=0.0)
            mae = max((float(bar.close) - float(b.low) for b in path), default=0.0)
        else:
            cont_ext = any(float(b.low) < sess_low for b in path)
            close_with = float(rth_1m[-1].close) < rth_open
            mfe = max((float(bar.close) - float(b.low) for b in path), default=0.0)
            mae = max((float(b.high) - float(bar.close) for b in path), default=0.0)
        return {
            "trading_date": td,
            "side": live_side,
            "htf_return": None if live_st is None else live_st.ret,
            "htf_age": 0 if live_st is None else live_st.age,
            "htf_strength": None if live_st is None else live_st.strength,
            "retracement": retr,
            "depth_bucket": buck,
            "continue_extreme": cont_ext,
            "close_with_trend": close_with,
            "mfe_after_tag": mfe,
            "mae_after_tag": mae,
            "tag_hhmm": _hhmm(bar.time),
            "year": int(td[:4]),
        }
    return None


def structural_htf_day(
    *,
    td: str,
    rth_1m: Sequence[Bar],
    h1: Sequence[HtfBar],
    h4: Sequence[HtfBar],
    thresh: float = THRESH,
) -> dict[str, Any]:
    t0 = local_ts(td, "09:30")
    s1 = htf_state(h1, t0, 4, thresh)
    s4 = htf_state(h4, t0, 3, thresh)
    op = float(rth_1m[0].open)
    cl = float(rth_1m[-1].close)
    hi = max(float(b.high) for b in rth_1m)
    lo = min(float(b.low) for b in rth_1m)
    close_with_1h = None
    if s1.side == "BULLISH":
        close_with_1h = cl > op
    elif s1.side == "BEARISH":
        close_with_1h = cl < op
    close_with_4h = None
    if s4.side == "BULLISH":
        close_with_4h = cl > op
    elif s4.side == "BEARISH":
        close_with_4h = cl < op
    return {
        "trading_date": td,
        "year": int(td[:4]),
        "trend_1h": s1.side,
        "ret_1h": s1.ret,
        "age_1h": s1.age,
        "strength_1h": s1.strength,
        "ema20_rising": s1.ema20_rising,
        "structure_1h": s1.structure,
        "trend_4h": s4.side,
        "ret_4h": s4.ret,
        "aligned_1h_4h": s1.side == s4.side and s1.side != "NEUTRAL",
        "rth_open": op,
        "rth_close": cl,
        "rth_high": hi,
        "rth_low": lo,
        "close_with_1h": close_with_1h,
        "close_with_4h": close_with_4h,
        "rth_return": None if op == 0 else cl / op - 1.0,
    }


def simulate_setup(
    setup: PullbackSetup,
    rth_1m: Sequence[Bar],
    *,
    target_r: float = 1.0,
    adverse_ticks: float = 1.0,
    stop_buffer_ticks: float = 1.0,
) -> HtfTrade:
    spec = INSTRUMENTS[setup.instrument]
    tick = float(spec["tick"])
    comm = float(spec["commission_points"])
    is_long = setup.direction == "LONG"
    buf = stop_buffer_ticks * tick
    sl = setup.pullback_extreme - buf if is_long else setup.pullback_extreme + buf
    theo = setup.entry_theo
    fill = theo + adverse_ticks * tick if is_long else theo - adverse_ticks * tick
    risk = abs(fill - sl)
    if risk <= tick * 0.5:
        return HtfTrade(
            instrument=setup.instrument,
            trading_date=setup.trading_date,
            candidate=setup.candidate,
            direction=setup.direction,
            status="SKIP_TINY_RISK",
            year=int(setup.trading_date[:4]),
            extras={"risk": risk},
        )
    tp = fill + target_r * risk if is_long else fill - target_r * risk
    flatten = flatten_ts(setup.trading_date)
    path = [b for b in rth_1m if int(b.time) >= setup.entry_ts]
    outcome, exit_ts, exit_px, mfe, mae = resolve_path(path, is_long=is_long, sl=sl, tp=tp, flatten=flatten)
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
            pts = (exit_px - fill) if is_long else (fill - exit_px)
        pts_c = pts - comm
        r_mult = pts / risk
        r_c = pts_c / risk
        hold = None if exit_ts is None else int(exit_ts) - int(setup.entry_ts)
    mfe_abs = 0.0 if mfe is None else float(mfe)
    mae_abs = 0.0 if mae is None else abs(float(mae))
    return HtfTrade(
        instrument=setup.instrument,
        trading_date=setup.trading_date,
        candidate=setup.candidate,
        direction=setup.direction,
        status=status,
        outcome=outcome,
        horizon=setup.horizon,
        htf_return=setup.htf_return,
        htf_age=setup.htf_age,
        htf_strength=setup.htf_strength,
        retracement=setup.retracement,
        depth_bucket=setup.depth_bucket,
        entry_ts=setup.entry_ts,
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
        year=int(setup.trading_date[:4]),
        signal_hhmm=_hhmm(setup.entry_ts),
        vwap_aligned=setup.vwap_aligned,
        aligned_1h_4h=setup.aligned_1h_4h,
        extras={
            "adverse_ticks": adverse_ticks,
            "stop_buffer_ticks": stop_buffer_ticks,
            "tod": setup.extras.get("tod"),
            "gap_points": setup.gap_points,
            "prior_ret": setup.prior_ret,
            "trend_1h": setup.trend_1h,
            "trend_4h": setup.trend_4h,
            "confirm_kind": setup.confirm_kind,
        },
    )


def find_atr_setup(
    *,
    instrument: str,
    td: str,
    rth_1m: Sequence[Bar],
    bars5: Sequence[HtfBar],
    h1: Sequence[HtfBar],
    k_atr: float,
    thresh: float = THRESH,
) -> Optional[PullbackSetup]:
    """Diagnostic: first 5m ATR pullback from session extreme, then 5m candle confirm."""
    rth5 = _rth_5m(bars5, td)
    if len(rth5) < 8 or not rth_1m:
        return None
    no_new = local_ts(td, NO_NEW)
    rth_open = float(rth_1m[0].open)
    sess_high = sess_low = rth_open
    armed = False
    tag_i = None
    extreme = None
    is_long = False
    for i, bar in enumerate(rth5):
        st = htf_state(h1, bar.close_ts, 4, thresh)
        atr = atr_from_series(bars5, bar.close_ts)
        if bar.high > sess_high:
            sess_high = bar.high
        if bar.low < sess_low:
            sess_low = bar.low
        if atr is None or atr <= 0:
            continue
        if not armed:
            if st.side == "BULLISH" and (sess_high - bar.low) >= k_atr * atr and sess_high > rth_open:
                armed = True
                is_long = True
                tag_i = i
                extreme = bar.low
            elif st.side == "BEARISH" and (bar.high - sess_low) >= k_atr * atr and sess_low < rth_open:
                armed = True
                is_long = False
                tag_i = i
                extreme = bar.high
            continue
        if st.side != ("BULLISH" if is_long else "BEARISH"):
            return None
        if is_long:
            extreme = min(extreme, bar.low)
        else:
            extreme = max(extreme, bar.high)
        if i <= (tag_i or 0):
            continue
        ok = (is_long and bar.close > bar.open) or ((not is_long) and bar.close < bar.open)
        if not ok or i + 1 >= len(rth5):
            continue
        entry = rth5[i + 1]
        if entry.time >= no_new:
            return None
        return PullbackSetup(
            instrument=instrument,
            trading_date=td,
            candidate=f"ATR_{k_atr:g}_5M_CONFIRM",
            direction="LONG" if is_long else "SHORT",
            horizon="1h",
            htf_return=float(st.ret or 0.0),
            htf_age=int(st.age),
            htf_strength=st.strength,
            retracement=float((sess_high - extreme) / max(sess_high - rth_open, TICK) if is_long else (extreme - sess_low) / max(rth_open - sess_low, TICK)),
            depth_bucket="atr",
            impulse_high=sess_high,
            impulse_low=sess_low,
            rth_open=rth_open,
            tag_ts=rth5[tag_i].time if tag_i is not None else bar.time,
            confirm_ts=bar.time,
            entry_ts=entry.time,
            entry_theo=float(entry.open),
            pullback_extreme=float(extreme),
            confirm_kind=CONFIRM_A,
            extras={"k_atr": k_atr, "tod": _tod_bucket(_hhmm(entry.time))},
        )
    return None
