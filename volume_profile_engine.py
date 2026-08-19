"""Phase 41 — leak-safe prior-RTH volume profile (research-only).

DEGRADED: 1m bar volume is spread uniformly across the bar's tick range.
Not a trade-print profile.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Optional, Sequence

from models import Bar
from nq_pdh_pdl import local_ts
from orb_index_engine import FLATTEN_LOCAL, INSTRUMENTS, flatten_ts, resolve_path

TICK = 0.25
VALUE_FRAC = 0.70


@dataclass
class VolumeProfile:
    trading_date: str
    poc: float
    vah: float
    val: float
    width: float
    total_volume: float
    vwap: float
    poc_in_range: float
    vol_above_poc: float
    vol_below_poc: float
    close: float
    high: float
    low: float
    close_vs_value: str
    n_ticks: int
    degraded: bool = True


@dataclass
class VpTrade:
    instrument: str
    trading_date: str
    candidate: str
    direction: str
    open_class: str
    status: str
    outcome: Optional[str] = None
    poc: Optional[float] = None
    vah: Optional[float] = None
    val: Optional[float] = None
    entry_ts: Optional[int] = None
    entry_theo: Optional[float] = None
    entry_fill: Optional[float] = None
    stop: Optional[float] = None
    target: Optional[float] = None
    risk_points: Optional[float] = None
    target_name: Optional[str] = None
    exit_ts: Optional[int] = None
    exit_px: Optional[float] = None
    points: Optional[float] = None
    r_multiple: Optional[float] = None
    points_after_cost: Optional[float] = None
    r_after_cost: Optional[float] = None
    mfe_points: Optional[float] = None
    mae_points: Optional[float] = None
    hold_sec: Optional[int] = None
    year: Optional[int] = None
    signal_hhmm: Optional[str] = None
    extras: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _tick_i(px: float, tick: float = TICK) -> int:
    return int(round(float(px) / tick))


def _px(i: int, tick: float = TICK) -> float:
    return round(i * tick, 10)


def build_profile(rth: Sequence[Bar], trading_date: str, tick: float = TICK) -> Optional[VolumeProfile]:
    if not rth:
        return None
    hist: dict[int, float] = {}
    typ_num = 0.0
    vol_sum = 0.0
    for b in rth:
        v = float(b.volume or 0.0)
        if v <= 0:
            continue
        lo = _tick_i(float(b.low), tick)
        hi = _tick_i(float(b.high), tick)
        if hi < lo:
            lo, hi = hi, lo
        n = hi - lo + 1
        share = v / n
        for t in range(lo, hi + 1):
            hist[t] = hist.get(t, 0.0) + share
        typ = (float(b.high) + float(b.low) + float(b.close)) / 3.0
        typ_num += typ * v
        vol_sum += v
    if not hist or vol_sum <= 0:
        return None
    vwap = typ_num / vol_sum
    max_v = max(hist.values())
    poc_cands = [t for t, x in hist.items() if x == max_v]
    vwap_i = _tick_i(vwap, tick)
    poc_cands.sort(key=lambda t: (abs(t - vwap_i), t))
    poc_i = poc_cands[0]
    total = sum(hist.values())
    need = VALUE_FRAC * total
    included = {poc_i}
    acc = hist[poc_i]
    lo_i = hi_i = poc_i
    tmin, tmax = min(hist), max(hist)
    while acc < need - 1e-12:
        up = hi_i + 1
        dn = lo_i - 1
        vu = hist.get(up, 0.0) if up <= tmax else -1.0
        vd = hist.get(dn, 0.0) if dn >= tmin else -1.0
        if vu < 0 and vd < 0:
            break
        if vu > vd:
            included.add(up)
            acc += max(vu, 0.0)
            hi_i = up
        else:
            included.add(dn)
            acc += max(vd, 0.0)
            lo_i = dn
    vah_i = max(included)
    val_i = min(included)
    hi = max(float(b.high) for b in rth)
    lo = min(float(b.low) for b in rth)
    cl = float(rth[-1].close)
    rng = hi - lo
    vol_above = sum(v for t, v in hist.items() if t > poc_i)
    vol_below = sum(v for t, v in hist.items() if t < poc_i)
    poc = _px(poc_i, tick)
    vah = _px(vah_i, tick)
    val = _px(val_i, tick)
    if cl > vah:
        loc = "CLOSE_ABOVE_VAH"
    elif cl < val:
        loc = "CLOSE_BELOW_VAL"
    else:
        loc = "CLOSE_INSIDE_VALUE"
    return VolumeProfile(
        trading_date=trading_date,
        poc=poc,
        vah=vah,
        val=val,
        width=vah - val,
        total_volume=total,
        vwap=vwap,
        poc_in_range=None if rng <= 0 else (poc - lo) / rng,
        vol_above_poc=vol_above / total,
        vol_below_poc=vol_below / total,
        close=cl,
        high=hi,
        low=lo,
        close_vs_value=loc,
        n_ticks=len(hist),
    )


def open_class(open_px: float, prof: VolumeProfile) -> str:
    if open_px > prof.vah:
        return "OPEN_ABOVE_VAH"
    if open_px < prof.val:
        return "OPEN_BELOW_VAL"
    return "OPEN_INSIDE_VALUE"


def _hhmm(ts: int) -> str:
    from datetime import datetime
    from zoneinfo import ZoneInfo
    return datetime.fromtimestamp(int(ts), tz=ZoneInfo("America/New_York")).strftime("%H:%M")


def _tod_bucket(ts: int) -> str:
    hm = _hhmm(ts)
    if hm < "10:00":
        return "0930_1000"
    if hm < "11:30":
        return "1000_1130"
    if hm < "13:30":
        return "1130_1330"
    return "1330_1530"


def first_touch(rth: Sequence[Bar], level: float) -> Optional[Bar]:
    for b in rth:
        if float(b.low) <= level <= float(b.high):
            return b
    return None


def structural_day(rth: Sequence[Bar], prof: VolumeProfile, trading_date: str) -> dict[str, Any]:
    op = float(rth[0].open)
    cls = open_class(op, prof)
    flatten = flatten_ts(trading_date)
    path = [b for b in rth if int(b.time) < flatten]
    if not path:
        path = list(rth)
    t_vah = first_touch(path, prof.vah)
    t_val = first_touch(path, prof.val)
    t_poc = first_touch(path, prof.poc)
    entered = False
    traversed = False
    rejected = False
    continued_away = False
    first_inside_close = None
    two_inside = False
    prev_inside = False
    accept5 = False
    if cls == "OPEN_ABOVE_VAH":
        entered = any(float(b.low) < prof.vah for b in path)
        traversed = any(float(b.low) <= prof.val for b in path)
        continued_away = not any(float(b.low) <= prof.vah for b in path)
    elif cls == "OPEN_BELOW_VAL":
        entered = any(float(b.high) > prof.val for b in path)
        traversed = any(float(b.high) >= prof.vah for b in path)
        continued_away = not any(float(b.high) >= prof.val for b in path)
    else:
        entered = True
        continued_away = False
        traversed = (t_vah is not None) and (t_val is not None)

    inside_closes = 0
    for b in path:
        inside = prof.val <= float(b.close) <= prof.vah
        if inside:
            inside_closes += 1
            if first_inside_close is None:
                first_inside_close = b
            if prev_inside:
                two_inside = True
        prev_inside = inside

    # 5m close diagnostic: aligned 09:30, 09:35, ...
    by5: dict[int, list[Bar]] = {}
    if path:
        t0 = int(path[0].time)
        for b in path:
            k = (int(b.time) - t0) // 300
            by5.setdefault(k, []).append(b)
        for k in sorted(by5):
            chunk = by5[k]
            if len(chunk) < 5:
                continue
            c5 = float(chunk[-1].close)
            if prof.val <= c5 <= prof.vah:
                accept5 = True
                break

    # 1m rejection: test then close back outside
    if cls == "OPEN_ABOVE_VAH":
        tested = False
        for b in path:
            if float(b.low) <= prof.vah:
                tested = True
            if tested and float(b.close) > prof.vah:
                rejected = True
                break
            if tested and float(b.close) < prof.vah:
                break
    elif cls == "OPEN_BELOW_VAL":
        tested = False
        for b in path:
            if float(b.high) >= prof.val:
                tested = True
            if tested and float(b.close) < prof.val:
                rejected = True
                break
            if tested and float(b.close) > prof.val:
                break

    mfe_away = 0.0
    mae_into = 0.0
    if cls == "OPEN_ABOVE_VAH":
        mfe_away = max((float(b.high) - op) for b in path)
        mae_into = min((float(b.low) - op) for b in path)
    elif cls == "OPEN_BELOW_VAL":
        mfe_away = max((op - float(b.low)) for b in path)
        mae_into = min((op - float(b.high)) for b in path)

    first_exit_side = None
    if cls == "OPEN_INSIDE_VALUE":
        for b in path:
            hit_h = float(b.high) >= prof.vah
            hit_l = float(b.low) <= prof.val
            if hit_h and hit_l:
                first_exit_side = "BOTH_SAME_BAR"
                break
            if hit_h:
                first_exit_side = "VAH"
                break
            if hit_l:
                first_exit_side = "VAL"
                break

    def _t(bar: Optional[Bar]) -> Optional[int]:
        return None if bar is None else int(bar.time)

    return {
        "open_class": cls,
        "open": op,
        "return_vah": t_vah is not None,
        "return_val": t_val is not None,
        "touch_poc": t_poc is not None,
        "enter_value": entered,
        "traverse_full_value": traversed,
        "continue_away": continued_away,
        "reject_1m": rejected,
        "accept_1m": first_inside_close is not None,
        "accept_two_1m": two_inside,
        "accept_5m": accept5,
        "ts_vah": _t(t_vah),
        "ts_val": _t(t_val),
        "ts_poc": _t(t_poc),
        "ts_accept_1m": None if first_inside_close is None else int(first_inside_close.time),
        "tod_first_value_int": _tod_bucket(_t(t_vah) or _t(t_val) or int(path[0].time)),
        "mfe_away": mfe_away,
        "mae_into": mae_into,
        "first_exit_side": first_exit_side,
        "inside_closes": inside_closes,
        "open_above_poc": op > prof.poc,
        "open_below_poc": op < prof.poc,
    }


def _session_date(bar: Bar) -> str:
    from datetime import datetime
    from zoneinfo import ZoneInfo
    return datetime.fromtimestamp(int(bar.time), tz=ZoneInfo("America/New_York")).date().isoformat()


def _next_bar(rth: Sequence[Bar], after_ts: int) -> Optional[Bar]:
    for b in rth:
        if int(b.time) > int(after_ts):
            return b
    return None


def _finish_trade(
    *,
    instrument: str,
    td: str,
    candidate: str,
    direction: str,
    open_cls: str,
    prof: VolumeProfile,
    rth: Sequence[Bar],
    entry_bar: Bar,
    theo: float,
    fill: float,
    sl: float,
    tp: float,
    target_name: str,
    adverse_ticks: float,
    signal_ts: int,
) -> VpTrade:
    spec = INSTRUMENTS[instrument]
    tick = float(spec["tick"])
    comm = float(spec["commission_points"])
    is_long = direction == "LONG"
    flatten = flatten_ts(td)
    risk = abs(fill - sl)
    path = [b for b in rth if int(b.time) >= int(entry_bar.time)]
    outcome, exit_ts, exit_px, mfe, mae = resolve_path(path, is_long=is_long, sl=sl, tp=tp, flatten=flatten)
    pts = r_mult = pts_c = r_c = hold = None
    status = "ENTERED"
    if outcome == "AMBIGUOUS":
        status = "ENTERED"
    elif outcome == "NO_PATH":
        status = "NO_PATH"
        outcome = None
    if exit_px is not None and outcome in ("TARGET_HIT", "STOP_HIT", "TIME_EXIT"):
        pts = (exit_px - fill) if is_long else (fill - exit_px)
        if outcome == "STOP_HIT":
            pts = -risk
            exit_px = sl
        if outcome == "TARGET_HIT":
            pts = (tp - fill) if is_long else (fill - tp)
            exit_px = tp
        pts_c = pts - comm
        r_mult = None if risk <= 0 else pts / risk
        r_c = None if risk <= 0 else pts_c / risk
        hold = None if exit_ts is None else int(exit_ts) - int(entry_bar.time)
    return VpTrade(
        instrument=instrument,
        trading_date=td,
        candidate=candidate,
        direction=direction,
        open_class=open_cls,
        status=status,
        outcome=outcome,
        poc=prof.poc,
        vah=prof.vah,
        val=prof.val,
        entry_ts=int(entry_bar.time),
        entry_theo=theo,
        entry_fill=fill,
        stop=sl,
        target=tp,
        risk_points=risk,
        target_name=target_name,
        exit_ts=exit_ts,
        exit_px=exit_px,
        points=pts,
        r_multiple=r_mult,
        points_after_cost=pts_c,
        r_after_cost=r_c,
        mfe_points=mfe,
        mae_points=None if mae is None else abs(mae) if mae <= 0 else mae,
        hold_sec=hold,
        year=int(td[:4]),
        signal_hhmm=_hhmm(signal_ts),
        extras={"adverse_ticks": adverse_ticks, "tick": tick},
    )


def simulate_accept_poc(
    *,
    instrument: str,
    td: str,
    rth: Sequence[Bar],
    prof: VolumeProfile,
    adverse_ticks: float,
    target_name: str = "POC",
) -> VpTrade:
    spec = INSTRUMENTS[instrument]
    tick = float(spec["tick"])
    flatten = flatten_ts(td)
    op = float(rth[0].open)
    cls = open_class(op, prof)
    base = VpTrade(instrument=instrument, trading_date=td, candidate="VP_OUTSIDE_ACCEPT_POC", direction="", open_class=cls, status="NO_SETUP", poc=prof.poc, vah=prof.vah, val=prof.val, year=int(td[:4]))
    if cls == "OPEN_INSIDE_VALUE":
        return base
    if cls == "OPEN_ABOVE_VAH":
        direction = "SHORT"
        signal = None
        for b in rth:
            if int(b.time) >= flatten:
                break
            if prof.val <= float(b.close) <= prof.vah:
                signal = b
                break
        if signal is None:
            return base
        extreme = max(float(x.high) for x in rth if int(x.time) <= int(signal.time))
        sl = extreme + tick
        if target_name == "POC":
            tp = prof.poc
        elif target_name == "opposite_value":
            tp = prof.val
        else:
            tp = None
    else:
        direction = "LONG"
        signal = None
        for b in rth:
            if int(b.time) >= flatten:
                break
            if prof.val <= float(b.close) <= prof.vah:
                signal = b
                break
        if signal is None:
            return base
        extreme = min(float(x.low) for x in rth if int(x.time) <= int(signal.time))
        sl = extreme - tick
        if target_name == "POC":
            tp = prof.poc
        elif target_name == "opposite_value":
            tp = prof.vah
        else:
            tp = None
    nxt = _next_bar(rth, int(signal.time))
    if nxt is None or int(nxt.time) >= flatten:
        base.status = "NO_ENTRY_BAR"
        base.direction = direction
        return base
    theo = float(nxt.open)
    fill = theo + adverse_ticks * tick if direction == "LONG" else theo - adverse_ticks * tick
    if target_name == "1R":
        risk = abs(fill - sl)
        tp = fill + risk if direction == "LONG" else fill - risk
    if tp is None:
        base.status = "NO_SETUP"
        return base
    if direction == "LONG" and tp <= fill:
        base.status = "REJECT_TARGET"
        return base
    if direction == "SHORT" and tp >= fill:
        base.status = "REJECT_TARGET"
        return base
    risk = abs(fill - sl)
    if risk < tick:
        base.status = "REJECT_TIGHT_STOP"
        return base
    if risk > float(spec["max_stop_points"]):
        base.status = "REJECT_WIDE_STOP"
        base.direction = direction
        base.risk_points = risk
        return base
    return _finish_trade(
        instrument=instrument, td=td, candidate="VP_OUTSIDE_ACCEPT_POC", direction=direction,
        open_cls=cls, prof=prof, rth=rth, entry_bar=nxt, theo=theo, fill=fill, sl=sl, tp=tp,
        target_name=target_name, adverse_ticks=adverse_ticks, signal_ts=int(signal.time),
    )


def simulate_reject_1r(
    *,
    instrument: str,
    td: str,
    rth: Sequence[Bar],
    prof: VolumeProfile,
    adverse_ticks: float,
) -> VpTrade:
    spec = INSTRUMENTS[instrument]
    tick = float(spec["tick"])
    flatten = flatten_ts(td)
    op = float(rth[0].open)
    cls = open_class(op, prof)
    base = VpTrade(instrument=instrument, trading_date=td, candidate="VP_OUTSIDE_REJECT_1R", direction="", open_class=cls, status="NO_SETUP", poc=prof.poc, vah=prof.vah, val=prof.val, year=int(td[:4]))
    if cls == "OPEN_INSIDE_VALUE":
        return base
    signal = None
    tested = False
    if cls == "OPEN_ABOVE_VAH":
        direction = "SHORT"
        for b in rth:
            if int(b.time) >= flatten:
                break
            if float(b.low) <= prof.vah:
                tested = True
            if tested and float(b.close) > prof.vah:
                signal = b
                break
            if tested and float(b.close) < prof.vah:
                base.status = "ACCEPTED_INSTEAD"
                return base
        if signal is None:
            return base
        extreme = max(float(x.high) for x in rth if int(x.time) <= int(signal.time))
        sl = extreme + tick
    else:
        direction = "LONG"
        for b in rth:
            if int(b.time) >= flatten:
                break
            if float(b.high) >= prof.val:
                tested = True
            if tested and float(b.close) < prof.val:
                signal = b
                break
            if tested and float(b.close) > prof.val:
                base.status = "ACCEPTED_INSTEAD"
                return base
        if signal is None:
            return base
        extreme = min(float(x.low) for x in rth if int(x.time) <= int(signal.time))
        sl = extreme - tick
    nxt = _next_bar(rth, int(signal.time))
    if nxt is None or int(nxt.time) >= flatten:
        base.status = "NO_ENTRY_BAR"
        base.direction = direction
        return base
    theo = float(nxt.open)
    fill = theo + adverse_ticks * tick if direction == "LONG" else theo - adverse_ticks * tick
    risk = abs(fill - sl)
    if risk < tick:
        base.status = "REJECT_TIGHT_STOP"
        return base
    if risk > float(spec["max_stop_points"]):
        base.status = "REJECT_WIDE_STOP"
        base.direction = direction
        base.risk_points = risk
        return base
    tp = fill + risk if direction == "LONG" else fill - risk
    return _finish_trade(
        instrument=instrument, td=td, candidate="VP_OUTSIDE_REJECT_1R", direction=direction,
        open_cls=cls, prof=prof, rth=rth, entry_bar=nxt, theo=theo, fill=fill, sl=sl, tp=tp,
        target_name="1R", adverse_ticks=adverse_ticks, signal_ts=int(signal.time),
    )


def simulate_inside_poc(
    *,
    instrument: str,
    td: str,
    rth: Sequence[Bar],
    prof: VolumeProfile,
    adverse_ticks: float,
) -> VpTrade:
    spec = INSTRUMENTS[instrument]
    tick = float(spec["tick"])
    flatten = flatten_ts(td)
    op = float(rth[0].open)
    cls = open_class(op, prof)
    base = VpTrade(instrument=instrument, trading_date=td, candidate="VP_INSIDE_ROTATE_POC", direction="", open_class=cls, status="NO_SETUP", poc=prof.poc, vah=prof.vah, val=prof.val, year=int(td[:4]))
    if cls != "OPEN_INSIDE_VALUE":
        return base
    if abs(op - prof.poc) < tick:
        base.status = "OPEN_ON_POC"
        return base
    if op > prof.poc:
        direction = "SHORT"
        sl = prof.vah + tick
        tp = prof.poc
    else:
        direction = "LONG"
        sl = prof.val - tick
        tp = prof.poc
    entry = rth[0]
    if int(entry.time) >= flatten:
        return base
    theo = float(entry.open)
    fill = theo + adverse_ticks * tick if direction == "LONG" else theo - adverse_ticks * tick
    risk = abs(fill - sl)
    if risk < tick:
        base.status = "REJECT_TIGHT_STOP"
        return base
    if risk > float(spec["max_stop_points"]):
        base.status = "REJECT_WIDE_STOP"
        base.direction = direction
        base.risk_points = risk
        return base
    if direction == "LONG" and tp <= fill:
        base.status = "REJECT_TARGET"
        return base
    if direction == "SHORT" and tp >= fill:
        base.status = "REJECT_TARGET"
        return base
    return _finish_trade(
        instrument=instrument, td=td, candidate="VP_INSIDE_ROTATE_POC", direction=direction,
        open_cls=cls, prof=prof, rth=rth, entry_bar=entry, theo=theo, fill=fill, sl=sl, tp=tp,
        target_name="POC", adverse_ticks=adverse_ticks, signal_ts=int(entry.time),
    )
