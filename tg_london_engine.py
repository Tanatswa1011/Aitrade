"""Phase 45 — leak-safe TG Capital London 30m FVG engine (research-only).

Mechanized approximations are labeled in phase45_spec.json. Completed bars only.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from typing import Any, Optional, Sequence
from zoneinfo import ZoneInfo

from models import Bar
from orb_index_engine import flatten_ts, resolve_path
from htf_pullback_engine import HtfBar, aggregate_bars, last_completed_index

LONDON = ZoneInfo("Europe/London")
NY = ZoneInfo("America/New_York")
PERIOD_30M = 1800
DOJI_RATIO = 0.25
TRIDENT_WICK_BODY = 1.0

INSTRUMENTS = {
    "GC": {"tick": 0.10, "point_usd": 100.0, "commission_points": 0.04, "mgc_point_usd": 10.0},
    "NQ": {"tick": 0.25, "point_usd": 20.0, "commission_points": 0.20, "mnq_point_usd": 2.0},
}


@dataclass
class TfBar:
    time: int
    close_ts: int
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


@dataclass
class FvgEvent:
    instrument: str
    direction: str
    london_date: str
    ny_date: str
    c1_ts: int
    c3_ts: int
    close_ts: int
    zone_low: float
    zone_high: float
    mid: float
    width: float
    impulse: float
    in_window: bool
    window: str
    htf_side: Optional[str]
    ema200_side: Optional[str]
    stack_side: Optional[str]
    alignment: str
    dist_ema200_atr: Optional[float]
    width_atr: Optional[float]
    bar_index: int
    extras: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class TgTrade:
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
    reach_3r: Optional[bool] = None
    hold_sec: Optional[int] = None
    year: Optional[int] = None
    signal_hhmm: Optional[str] = None
    stop_family: Optional[str] = None
    reaction: Optional[str] = None
    extras: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def london_dt(ts: int) -> datetime:
    return datetime.fromtimestamp(int(ts), tz=LONDON)


def london_date(ts: int) -> str:
    return london_dt(ts).date().isoformat()


def london_hhmm(ts: int) -> str:
    return london_dt(ts).strftime("%H:%M")


def ny_date_of(ts: int) -> str:
    return datetime.fromtimestamp(int(ts), tz=NY).date().isoformat()


def in_window(ts: int, start: str = "07:00", end: str = "11:00") -> bool:
    hh = london_hhmm(ts)
    return start <= hh < end


def _bucket_start_london_30m(ts: int) -> int:
    dt = london_dt(ts)
    minute = (dt.minute // 30) * 30
    start = dt.replace(minute=minute, second=0, microsecond=0)
    return int(start.timestamp())


def aggregate_30m_london(bars: Sequence[Bar]) -> list[TfBar]:
    buckets: dict[int, list[Bar]] = {}
    for b in bars:
        start = _bucket_start_london_30m(int(b.time))
        buckets.setdefault(start, []).append(b)
    out: list[TfBar] = []
    for start in sorted(buckets):
        g = buckets[start]
        out.append(
            TfBar(
                time=start,
                close_ts=start + PERIOD_30M,
                open=float(g[0].open),
                high=max(float(x.high) for x in g),
                low=min(float(x.low) for x in g),
                close=float(g[-1].close),
                volume=sum(float(x.volume or 0) for x in g),
            )
        )
    return out


def ema_series(closes: Sequence[float], span: int) -> list[Optional[float]]:
    out: list[Optional[float]] = [None] * len(closes)
    if len(closes) < span:
        return out
    k = 2.0 / (span + 1.0)
    sma = sum(closes[:span]) / span
    out[span - 1] = sma
    ema = sma
    for i in range(span, len(closes)):
        ema = closes[i] * k + ema * (1.0 - k)
        out[i] = ema
    return out


def atr_series(bars: Sequence[TfBar], period: int = 14) -> list[Optional[float]]:
    out: list[Optional[float]] = [None] * len(bars)
    trs: list[float] = []
    prev_c: Optional[float] = None
    for i, b in enumerate(bars):
        h, l, c = float(b.high), float(b.low), float(b.close)
        if prev_c is None:
            tr = h - l
        else:
            tr = max(h - l, abs(h - prev_c), abs(l - prev_c))
        trs.append(tr)
        prev_c = c
        if i + 1 >= period:
            out[i] = sum(trs[i + 1 - period : i + 1]) / period
    return out


def side_vs_ema(close: float, ema: Optional[float]) -> Optional[str]:
    if ema is None:
        return None
    if close > ema:
        return "BULLISH"
    if close < ema:
        return "BEARISH"
    return "NEUTRAL"


def stack_side(e20: Optional[float], e50: Optional[float], e200: Optional[float]) -> Optional[str]:
    if e20 is None or e50 is None or e200 is None:
        return None
    if e20 > e50 > e200:
        return "BULLISH"
    if e20 < e50 < e200:
        return "BEARISH"
    return "NEUTRAL"


def alignment_of(htf: Optional[str], e200: Optional[str], stack: Optional[str]) -> str:
    sides = [htf, e200, stack]
    if all(s == "BULLISH" for s in sides):
        return "FULL_BULL"
    if all(s == "BEARISH" for s in sides):
        return "FULL_BEAR"
    bulls = sum(1 for s in sides if s == "BULLISH")
    bears = sum(1 for s in sides if s == "BEARISH")
    if bulls >= 2 and bears == 0:
        return "PART_BULL"
    if bears >= 2 and bulls == 0:
        return "PART_BEAR"
    return "CONFLICT"


def candle_geom(b: TfBar) -> dict[str, float]:
    o, h, l, c = float(b.open), float(b.high), float(b.low), float(b.close)
    body = abs(c - o)
    rng = h - l
    upper = h - max(o, c)
    lower = min(o, c) - l
    loc = None if rng <= 0 else (c - l) / rng
    return {"body": body, "range": rng, "upper_wick": upper, "lower_wick": lower, "close_loc": loc or 0.0}


def is_doji(b: TfBar, ratio: float = DOJI_RATIO) -> bool:
    g = candle_geom(b)
    return g["range"] > 0 and (g["body"] / g["range"]) <= ratio


def is_trident(b: TfBar, direction: str, wick_body: float = TRIDENT_WICK_BODY) -> bool:
    g = candle_geom(b)
    if g["range"] <= 0:
        return False
    if direction == "BULLISH":
        return g["lower_wick"] >= wick_body * g["body"] and g["close_loc"] >= 0.5
    return g["upper_wick"] >= wick_body * g["body"] and g["close_loc"] <= 0.5


def directional_close(b: TfBar, direction: str) -> bool:
    if direction == "BULLISH":
        return float(b.close) > float(b.open)
    return float(b.close) < float(b.open)


def intersects_mid(b: TfBar, mid: float) -> bool:
    return float(b.low) <= mid <= float(b.high)


def detect_fvgs(
    *,
    instrument: str,
    bars30: Sequence[TfBar],
    h4: Sequence[HtfBar],
    ema20: Sequence[Optional[float]],
    ema50: Sequence[Optional[float]],
    ema200: Sequence[Optional[float]],
    atr: Sequence[Optional[float]],
    ema200_4h: Sequence[Optional[float]],
    window: tuple[str, str] = ("07:00", "11:00"),
) -> list[FvgEvent]:
    out: list[FvgEvent] = []
    w0, w1 = window
    for i in range(2, len(bars30)):
        c1, c2, c3 = bars30[i - 2], bars30[i - 1], bars30[i]
        bull = float(c1.high) < float(c3.low)
        bear = float(c1.low) > float(c3.high)
        if not bull and not bear:
            continue
        if bull:
            direction = "BULLISH"
            zlo, zhi = float(c1.high), float(c3.low)
            impulse = float(c3.high) - float(c1.low)
        else:
            direction = "BEARISH"
            zlo, zhi = float(c3.high), float(c1.low)
            impulse = float(c1.high) - float(c3.low)
        mid = (zlo + zhi) / 2.0
        width = zhi - zlo
        hi = last_completed_index(h4, int(c3.close_ts))
        htf = None
        if hi >= 0 and ema200_4h and hi < len(ema200_4h) and ema200_4h[hi] is not None:
            htf = side_vs_ema(h4[hi].close, ema200_4h[hi])
        e200 = side_vs_ema(float(c3.close), ema200[i])
        stack = stack_side(ema20[i], ema50[i], ema200[i])
        a = None if atr[i] in (None, 0) else atr[i]
        dist = None if a is None or ema200[i] is None else (float(c3.close) - float(ema200[i])) / a
        w_atr = None if a is None else width / a
        ld = london_date(int(c3.time))
        out.append(
            FvgEvent(
                instrument=instrument,
                direction=direction,
                london_date=ld,
                ny_date=ny_date_of(int(c3.time)),
                c1_ts=int(c1.time),
                c3_ts=int(c3.time),
                close_ts=int(c3.close_ts),
                zone_low=zlo,
                zone_high=zhi,
                mid=mid,
                width=width,
                impulse=impulse,
                in_window=in_window(int(c3.time), w0, w1),
                window=f"{w0}-{w1}",
                htf_side=htf,
                ema200_side=e200,
                stack_side=stack,
                alignment=alignment_of(htf, e200, stack),
                dist_ema200_atr=dist,
                width_atr=w_atr,
                bar_index=i,
                extras={"c2_ts": int(c2.time)},
            )
        )
    return out


def trend_aligned(ev: FvgEvent) -> bool:
    if ev.direction == "BULLISH":
        return ev.alignment == "FULL_BULL"
    return ev.alignment == "FULL_BEAR"


def scan_fvg_forward(
    bars30: Sequence[TfBar],
    ev: FvgEvent,
    *,
    horizon_bars: int = 24,
) -> dict[str, Any]:
    """Structural path after FVG completes. No trade."""
    start = ev.bar_index + 1
    end = min(len(bars30), start + horizon_bars)
    mid_i = fill_i = through_i = None
    edge_i = None
    for j in range(start, end):
        b = bars30[j]
        lo, hi = float(b.low), float(b.high)
        if ev.direction == "BULLISH":
            if edge_i is None and lo <= ev.zone_high:
                edge_i = j
            if mid_i is None and lo <= ev.mid:
                mid_i = j
            if fill_i is None and lo <= ev.zone_low:
                fill_i = j
            if through_i is None and lo < ev.zone_low:
                through_i = j
        else:
            if edge_i is None and hi >= ev.zone_low:
                edge_i = j
            if mid_i is None and hi >= ev.mid:
                mid_i = j
            if fill_i is None and hi >= ev.zone_high:
                fill_i = j
            if through_i is None and hi > ev.zone_high:
                through_i = j
    resume = ext = None
    mfe = mae = None
    if mid_i is not None:
        px = ev.mid
        mfe = mae = 0.0
        for j in range(mid_i, end):
            b = bars30[j]
            if ev.direction == "BULLISH":
                mfe = max(mfe, float(b.high) - px)
                mae = max(mae, px - float(b.low))
                if resume is None and float(b.close) > ev.mid:
                    resume = j
                if ext is None and float(b.high) > float(bars30[ev.bar_index].high):
                    ext = j
            else:
                mfe = max(mfe, px - float(b.low))
                mae = max(mae, float(b.high) - px)
                if resume is None and float(b.close) < ev.mid:
                    resume = j
                if ext is None and float(b.low) < float(bars30[ev.bar_index].low):
                    ext = j
    def _reac(kind: str) -> Optional[int]:
        if mid_i is None:
            return None
        b = bars30[mid_i]
        if not intersects_mid(b, ev.mid):
            return None
        if kind == "doji" and is_doji(b):
            return mid_i
        if kind == "trident" and is_trident(b, ev.direction):
            return mid_i
        if kind == "close" and directional_close(b, ev.direction):
            return mid_i
        for j in range(mid_i + 1, min(end, mid_i + 6)):
            bb = bars30[j]
            if not intersects_mid(bb, ev.mid):
                continue
            if kind == "doji" and is_doji(bb):
                return j
            if kind == "trident" and is_trident(bb, ev.direction):
                return j
            if kind == "close" and directional_close(bb, ev.direction):
                return j
        return None

    return {
        "touched_edge": edge_i is not None,
        "touched_mid": mid_i is not None,
        "full_fill": fill_i is not None,
        "through": through_i is not None,
        "mid_index": mid_i,
        "bars_to_mid": None if mid_i is None else mid_i - ev.bar_index,
        "resume_after_mid": resume is not None,
        "new_extreme_after_mid": ext is not None,
        "mfe_after_mid": mfe,
        "mae_after_mid": mae,
        "doji_i": _reac("doji"),
        "trident_i": _reac("trident"),
        "close_i": _reac("close"),
    }


def find_reaction_index(
    bars30: Sequence[TfBar],
    ev: FvgEvent,
    *,
    kind: str,
    doji_ratio: float = DOJI_RATIO,
    wick_body: float = TRIDENT_WICK_BODY,
    max_wait: int = 8,
    window_end: str = "11:00",
) -> Optional[int]:
    start = ev.bar_index + 1
    end = min(len(bars30), start + max_wait)
    seen_mid = False
    for j in range(start, end):
        b = bars30[j]
        if london_date(int(b.time)) != ev.london_date:
            break
        if not intersects_mid(b, ev.mid):
            if seen_mid:
                continue
            continue
        seen_mid = True
        ok = False
        if kind == "doji":
            ok = is_doji(b, doji_ratio)
        elif kind == "trident":
            ok = is_trident(b, ev.direction, wick_body)
        elif kind == "close":
            ok = directional_close(b, ev.direction)
        if not ok:
            continue
        if j + 1 >= len(bars30):
            return None
        entry = bars30[j + 1]
        if not in_window(int(entry.time), "00:00", window_end):
            return None
        if london_date(int(entry.time)) != ev.london_date:
            return None
        return j
    return None


def simulate_setup(
    *,
    instrument: str,
    bars_1m: Sequence[Bar],
    bars30: Sequence[TfBar],
    ev: FvgEvent,
    reaction_i: int,
    stop_family: str,
    target_r: float,
    adverse_ticks: float,
    candidate: str,
    reaction: str,
) -> TgTrade:
    spec = INSTRUMENTS[instrument]
    tick = float(spec["tick"])
    comm = float(spec["commission_points"])
    entry_bar = bars30[reaction_i + 1]
    reac = bars30[reaction_i]
    theo = float(entry_bar.open)
    is_long = ev.direction == "BULLISH"
    fill = theo + adverse_ticks * tick if is_long else theo - adverse_ticks * tick
    if stop_family == "reaction_extreme":
        raw_stop = float(reac.low) - tick if is_long else float(reac.high) + tick
    else:
        raw_stop = ev.zone_low - tick if is_long else ev.zone_high + tick
    risk = (fill - raw_stop) if is_long else (raw_stop - fill)
    td = ev.ny_date
    year = int(td[:4])
    if risk <= tick * 0.5:
        return TgTrade(
            instrument=instrument, trading_date=td, candidate=candidate, direction=ev.direction,
            status="SKIP_TINY_RISK", year=year, extras={"risk": risk},
        )
    tp = fill + target_r * risk if is_long else fill - target_r * risk
    flatten = flatten_ts(td)
    path = [b for b in bars_1m if int(b.time) >= int(entry_bar.time)]
    if not path:
        path = [Bar(time=int(entry_bar.time), open=theo, high=float(entry_bar.high), low=float(entry_bar.low), close=float(entry_bar.close), volume=0)]
        extra_path = [b for b in bars30 if int(b.time) >= int(entry_bar.time)]
        path = [Bar(time=int(b.time), open=float(b.open), high=float(b.high), low=float(b.low), close=float(b.close), volume=b.volume) for b in extra_path]
    outcome, exit_ts, exit_px, mfe, mae = resolve_path(path, is_long=is_long, sl=raw_stop, tp=tp, flatten=flatten)
    status = "ENTERED"
    pts = r_mult = pts_c = r_c = hold = None
    if outcome == "NO_PATH":
        status = "NO_PATH"
        outcome = None
    if outcome == "AMBIGUOUS":
        pass
    elif exit_px is not None and outcome in ("TARGET_HIT", "STOP_HIT", "TIME_EXIT"):
        if outcome == "STOP_HIT":
            pts = -risk
            exit_px = raw_stop
        elif outcome == "TARGET_HIT":
            pts = target_r * risk
            exit_px = tp
        else:
            pts = (exit_px - fill) if is_long else (fill - exit_px)
        pts_c = pts - comm
        r_mult = pts / risk
        r_c = pts_c / risk
        hold = None if exit_ts is None else int(exit_ts) - int(entry_bar.time)
    mfe_abs = 0.0 if mfe is None else abs(float(mfe))
    mae_abs = 0.0 if mae is None else abs(float(mae))
    return TgTrade(
        instrument=instrument,
        trading_date=td,
        candidate=candidate,
        direction=ev.direction,
        status=status,
        outcome=outcome,
        entry_ts=int(entry_bar.time),
        entry_theo=theo,
        entry_fill=fill,
        stop=raw_stop,
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
        reach_3r=mfe_abs >= 3 * risk if risk > 0 else None,
        hold_sec=hold,
        year=year,
        signal_hhmm=london_hhmm(int(entry_bar.time)),
        stop_family=stop_family,
        reaction=reaction,
        extras={"adverse_ticks": adverse_ticks, "fvg_mid": ev.mid, "london_date": ev.london_date},
    )


def collect_session_setups(
    bars30: Sequence[TfBar],
    events: Sequence[FvgEvent],
    *,
    kind: str,
    require_full_align: bool = True,
    window_end: str = "11:00",
    doji_ratio: float = DOJI_RATIO,
    wick_body: float = TRIDENT_WICK_BODY,
) -> list[tuple[FvgEvent, int]]:
    """First valid aligned setup per London date."""
    by_day: dict[str, list[FvgEvent]] = {}
    for ev in events:
        if not ev.in_window:
            continue
        if require_full_align and not trend_aligned(ev):
            continue
        by_day.setdefault(ev.london_date, []).append(ev)
    out: list[tuple[FvgEvent, int]] = []
    for _day, rows in sorted(by_day.items()):
        rows.sort(key=lambda e: e.close_ts)
        for ev in rows:
            ri = find_reaction_index(
                bars30, ev, kind=kind, doji_ratio=doji_ratio, wick_body=wick_body, window_end=window_end
            )
            if ri is None:
                continue
            out.append((ev, ri))
            break
    return out
