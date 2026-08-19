"""PDH/PDL from prior RTH 1m bars + first-touch sweep detection (no look-ahead)."""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Optional, Sequence
from zoneinfo import ZoneInfo

from closed_candles import bar_close_ts, filter_closed_bars
from models import Bar
from nq_microstructure_models import (
    AUX_HORIZONS_SEC,
    CONTINUATION_EXT_POINTS,
    NO_NEW_SWEEP_AFTER,
    NQ_TICK,
    OR_TIMEZONE,
    PRIMARY_HORIZON_SEC,
    REVERSAL_TARGET_POINTS,
    RTH_END,
    RTH_START,
    SweepEvent,
    SweepOutcome,
)

NY = ZoneInfo(OR_TIMEZONE)


def local_ts(trading_date: str, hhmm: str) -> int:
    d = date.fromisoformat(trading_date)
    hh, mm = map(int, hhmm.split(":"))
    return int(datetime(d.year, d.month, d.day, hh, mm, tzinfo=NY).timestamp())


def ny_date(ts: int) -> str:
    return datetime.fromtimestamp(int(ts), tz=NY).date().isoformat()


def index_by_ny_date(bars: Sequence[Bar]) -> dict[str, list[Bar]]:
    out: dict[str, list[Bar]] = defaultdict(list)
    for b in sorted(bars, key=lambda x: int(x.time)):
        out[ny_date(int(b.time))].append(b)
    return dict(out)


def rth_bars(bars: Sequence[Bar], trading_date: str) -> list[Bar]:
    start = local_ts(trading_date, RTH_START)
    end = local_ts(trading_date, RTH_END)
    return [b for b in bars if start <= int(b.time) < end]


def prior_rth_high_low(
    bars_by_date: dict[str, list[Bar]],
    trading_date: str,
    *,
    skip_dates: Optional[set[str]] = None,
) -> Optional[tuple[float, float, str]]:
    """PDH/PDL = high/low of the previous NY date that has RTH 1m bars (skip weekends/holidays)."""
    d = date.fromisoformat(trading_date)
    skip = skip_dates or set()
    for i in range(1, 8):
        prev = (d - timedelta(days=i)).isoformat()
        if prev in skip:
            continue
        day = bars_by_date.get(prev) or []
        rth = rth_bars(day, prev)
        if len(rth) < 30:
            continue
        return max(float(b.high) for b in rth), min(float(b.low) for b in rth), prev
    return None


def atr_1m(bars: Sequence[Bar], as_of_ts: int, period: int = 14) -> Optional[float]:
    closed = filter_closed_bars(bars, as_of_ts=as_of_ts, timeframe="1m")
    if len(closed) < period + 1:
        return None
    window = closed[-(period + 1) :]
    trs = []
    for i in range(1, len(window)):
        prev_c = float(window[i - 1].close)
        h, l = float(window[i].high), float(window[i].low)
        trs.append(max(h - l, abs(h - prev_c), abs(l - prev_c)))
    return sum(trs[-period:]) / float(period)


def detect_pdh_pdl_sweeps(
    bars: Sequence[Bar],
    trading_dates: Sequence[str],
    *,
    skip_source_dates: Optional[set[str]] = None,
    contract: Optional[str] = None,
) -> list[SweepEvent]:
    by_date = index_by_ny_date(bars)
    events: list[SweepEvent] = []
    for td in trading_dates:
        hl = prior_rth_high_low(by_date, td, skip_dates=skip_source_dates)
        if hl is None:
            continue
        pdh, pdl, src = hl
        day = by_date.get(td) or []
        rth = rth_bars(day, td)
        rth_open = local_ts(td, RTH_START)
        cutoff = local_ts(td, NO_NEW_SWEEP_AFTER)
        seen_high = seen_low = False
        for b in rth:
            t = int(b.time)
            if t >= cutoff:
                break
            close_ok = bar_close_ts(b, "1m")
            if close_ok is None:
                continue
            if (not seen_high) and float(b.high) > pdh:
                seen_high = True
                atr = atr_1m(day, t, 14)
                events.append(
                    SweepEvent(
                        event_id=f"NQ|{contract}|PDH|{td}|{t}" if contract else f"NQ|PDH|{td}|{t}",
                        trading_date=td,
                        side="pdh_sweep",
                        level=float(pdh),
                        sweep_bar_time=t,
                        sweep_ts=t,
                        extreme=float(b.high),
                        penetration_points=float(b.high) - float(pdh),
                        rth_open_ts=rth_open,
                        seconds_from_rth_open=t - rth_open,
                        atr_1m_14=atr,
                        volume_sweep_bar=None if b.volume is None else float(b.volume),
                        prior_rth_high=float(pdh),
                        prior_rth_low=float(pdl),
                        extras={
                            "pdh_pdl_source_date": src,
                            "session": "RTH_0930_1600",
                            **({"contract": contract} if contract else {}),
                        },
                    )
                )
            if (not seen_low) and float(b.low) < pdl:
                seen_low = True
                atr = atr_1m(day, t, 14)
                events.append(
                    SweepEvent(
                        event_id=f"NQ|{contract}|PDL|{td}|{t}" if contract else f"NQ|PDL|{td}|{t}",
                        trading_date=td,
                        side="pdl_sweep",
                        level=float(pdl),
                        sweep_bar_time=t,
                        sweep_ts=t,
                        extreme=float(b.low),
                        penetration_points=float(pdl) - float(b.low),
                        rth_open_ts=rth_open,
                        seconds_from_rth_open=t - rth_open,
                        atr_1m_14=atr,
                        volume_sweep_bar=None if b.volume is None else float(b.volume),
                        prior_rth_high=float(pdh),
                        prior_rth_low=float(pdl),
                        extras={
                            "pdh_pdl_source_date": src,
                            "session": "RTH_0930_1600",
                            **({"contract": contract} if contract else {}),
                        },
                    )
                )
    return events


def label_outcome_1m(
    event: SweepEvent,
    bars: Sequence[Bar],
    *,
    horizon_sec: int = PRIMARY_HORIZON_SEC,
    target_pts: float = REVERSAL_TARGET_POINTS,
    ext_pts: float = CONTINUATION_EXT_POINTS,
) -> SweepOutcome:
    """Label using 1m OHLC after the sweep bar close. Same-bar stop+target => AMBIGUOUS."""
    start = int(event.sweep_bar_time) + 60  # first bar strictly after sweep bar
    end = int(event.sweep_ts) + int(horizon_sec)
    path = [b for b in bars if start <= int(b.time) < end]
    reclaim_ts = None
    mfe = 0.0
    mae = 0.0
    further = 0.0
    if event.side == "pdl_sweep":
        for b in path:
            further = max(further, float(event.extreme) - float(b.low) if float(b.low) < event.extreme else further)
            # continuation: more than ext_pts below sweep extreme
            hit_cont = float(b.low) <= float(event.extreme) - ext_pts
            # reclaim through PDL
            hit_reclaim = float(b.high) >= float(event.level)
            if hit_reclaim and reclaim_ts is None:
                reclaim_ts = int(b.time)
            if reclaim_ts is not None:
                mfe = max(mfe, float(b.high) - float(event.level))
            mae = min(mae, float(b.low) - float(event.level))
            if hit_cont and hit_reclaim:
                return SweepOutcome(event.event_id, horizon_sec, "AMBIGUOUS", reclaim_ts, None, mfe, mae, further)
            if hit_cont:
                return SweepOutcome(event.event_id, horizon_sec, "CONTINUATION", reclaim_ts, None if reclaim_ts is None else float(reclaim_ts - event.sweep_ts), mfe, mae, further)
            if hit_reclaim and mfe >= target_pts:
                return SweepOutcome(
                    event.event_id,
                    horizon_sec,
                    "REVERSAL",
                    reclaim_ts,
                    float(reclaim_ts - event.sweep_ts) if reclaim_ts else None,
                    mfe,
                    mae,
                    further,
                )
    else:
        for b in path:
            further = max(further, float(b.high) - float(event.extreme) if float(b.high) > event.extreme else further)
            hit_cont = float(b.high) >= float(event.extreme) + ext_pts
            hit_reclaim = float(b.low) <= float(event.level)
            if hit_reclaim and reclaim_ts is None:
                reclaim_ts = int(b.time)
            if reclaim_ts is not None:
                mfe = max(mfe, float(event.level) - float(b.low))
            mae = max(mae, float(b.high) - float(event.level))
            if hit_cont and hit_reclaim:
                return SweepOutcome(event.event_id, horizon_sec, "AMBIGUOUS", reclaim_ts, None, mfe, mae, further)
            if hit_cont:
                return SweepOutcome(event.event_id, horizon_sec, "CONTINUATION", reclaim_ts, None if reclaim_ts is None else float(reclaim_ts - event.sweep_ts), mfe, mae, further)
            if hit_reclaim and mfe >= target_pts:
                return SweepOutcome(
                    event.event_id,
                    horizon_sec,
                    "REVERSAL",
                    reclaim_ts,
                    float(reclaim_ts - event.sweep_ts) if reclaim_ts else None,
                    mfe,
                    mae,
                    further,
                )
    label = "NEITHER"
    if reclaim_ts is not None and mfe >= target_pts:
        label = "REVERSAL"
    return SweepOutcome(
        event.event_id,
        horizon_sec,
        label,
        reclaim_ts,
        None if reclaim_ts is None else float(reclaim_ts - event.sweep_ts),
        mfe,
        mae,
        further,
    )


def label_all_horizons(event: SweepEvent, bars: Sequence[Bar]) -> list[SweepOutcome]:
    return [label_outcome_1m(event, bars, horizon_sec=h) for h in AUX_HORIZONS_SEC]
