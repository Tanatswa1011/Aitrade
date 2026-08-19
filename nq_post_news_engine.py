"""Phase 33 — Post-news macro repricing engine (research-only).

All decisions use bars that are fully closed at the decision timestamp.
The default prop-firm blackout forbids any order action from 5 minutes before
through 5 minutes after the scheduled release. The strategy never attempts to
trade the announcement print itself.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Any, Optional, Sequence
from zoneinfo import ZoneInfo

from closed_candles import bar_close_ts, filter_closed_bars
from models import Bar
from nq_post_news_models import (
    ATR_PERIOD,
    ATR_TIMEFRAME,
    CASH_OPEN_LOCAL,
    FORCE_CLOSE_LOCAL,
    OR_TIMEZONE,
    PRIOR_SESSION_OPEN_LOCAL,
    EventSnapshot,
    MacroEvent,
    PostNewsStrategyConfig,
    PostNewsTrade,
    PropFirmNewsProfile,
    config_hash,
)

NY = ZoneInfo(OR_TIMEZONE)


def local_ts(trading_date: str, hhmm: str) -> int:
    d = date.fromisoformat(trading_date)
    hh, mm = map(int, hhmm.split(":"))
    return int(datetime(d.year, d.month, d.day, hh, mm, tzinfo=NY).timestamp())


def index_bars_by_ny_date(bars: Sequence[Bar]) -> dict[str, list[Bar]]:
    out: dict[str, list[Bar]] = defaultdict(list)
    for b in sorted(bars, key=lambda x: int(x.time)):
        td = datetime.fromtimestamp(int(b.time), tz=NY).date().isoformat()
        out[td].append(b)
    return dict(out)


def _completed(bars: Sequence[Bar], as_of_ts: int, timeframe: str) -> list[Bar]:
    return filter_closed_bars(bars, as_of_ts=as_of_ts, timeframe=timeframe)


def last_completed(bars: Sequence[Bar], as_of_ts: int, timeframe: str) -> Optional[Bar]:
    closed = _completed(bars, as_of_ts, timeframe)
    return None if not closed else closed[-1]


def bar_at_or_before(bars: Sequence[Bar], ts: int) -> Optional[Bar]:
    best = None
    for b in bars:
        if int(b.time) <= ts:
            best = b
        else:
            break
    return best


def typical_price(bar: Bar) -> float:
    return (float(bar.high) + float(bar.low) + float(bar.close)) / 3.0


def simple_atr(bars_5m: Sequence[Bar], as_of_ts: int, period: int = ATR_PERIOD) -> Optional[float]:
    """Mean true range of the last `period` completed 5m bars strictly before as_of_ts."""
    closed = _completed(bars_5m, as_of_ts, ATR_TIMEFRAME)
    if len(closed) < period + 1:
        return None
    window = closed[-(period + 1) :]
    trs: list[float] = []
    for i in range(1, len(window)):
        prev_c = float(window[i - 1].close)
        h = float(window[i].high)
        l = float(window[i].low)
        trs.append(max(h - l, abs(h - prev_c), abs(l - prev_c)))
    if len(trs) < period:
        return None
    return sum(trs[-period:]) / float(period)


def globex_vwap(
    bars_1m: Sequence[Bar],
    trading_date: str,
    as_of_ts: int,
) -> Optional[float]:
    """Running VWAP from prior 18:00 NY through last completed 1m bar at as_of_ts."""
    d = date.fromisoformat(trading_date)
    start_day = d - timedelta(days=1)
    start = int(datetime(start_day.year, start_day.month, start_day.day, 18, 0, tzinfo=NY).timestamp())
    closed = [b for b in _completed(bars_1m, as_of_ts, "1m") if start <= int(b.time)]
    sum_pv = 0.0
    sum_v = 0.0
    for b in closed:
        vol = max(0.0, float(b.volume or 0.0))
        if vol <= 0:
            continue
        sum_pv += typical_price(b) * vol
        sum_v += vol
    if sum_v <= 0:
        return None
    return sum_pv / sum_v


def blackout_window(
    trading_date: str,
    release_local: str,
    profile: PropFirmNewsProfile,
) -> tuple[int, int, int]:
    release = local_ts(trading_date, release_local)
    start = release - int(profile.blackout_before_minutes) * 60
    end = release + int(profile.blackout_after_minutes) * 60
    return start, end, release


def classify_regime(
    signed_move: Optional[float],
    event_range: Optional[float],
    atr: Optional[float],
    retention: Optional[float],
    event_close: Optional[float],
    event_high: Optional[float],
    event_low: Optional[float],
    cfg: PostNewsStrategyConfig,
) -> str:
    if None in (signed_move, event_range, atr, retention, event_close, event_high, event_low):
        return "DATA_INSUFFICIENT"
    if atr is None or atr <= 0:
        return "DATA_INSUFFICIENT"
    if abs(float(signed_move)) < float(cfg.min_close_move_atr) * float(atr):
        return "MACRO_NEUTRAL"
    if float(event_range) < float(cfg.min_range_atr) * float(atr):
        return "MACRO_NEUTRAL"
    if float(retention) < float(cfg.min_retention):
        return "MACRO_NEUTRAL"
    mid = (float(event_high) + float(event_low)) / 2.0
    if float(signed_move) > 0 and float(event_close) >= mid:
        return "MACRO_BULLISH"
    if float(signed_move) < 0 and float(event_close) <= mid:
        return "MACRO_BEARISH"
    return "MACRO_NEUTRAL"


def snapshot_event(
    event: MacroEvent,
    bars_1m: Sequence[Bar],
    bars_5m: Sequence[Bar],
    *,
    instrument: str,
    cfg: PostNewsStrategyConfig,
) -> EventSnapshot:
    td = event.publication_date
    profile = cfg.news_profile
    b_start, b_end, release = blackout_window(td, event.release_local, profile)
    day_1m = [b for b in bars_1m if abs(int(b.time) - release) < 36 * 3600]
    day_5m = [b for b in bars_5m if abs(int(b.time) - release) < 48 * 3600]

    ref_bar = last_completed(day_1m, release, "1m")  # 08:29 1m close at 08:30
    # Event range uses 1m bars whose open is in [release, blackout_end).
    # They are all closed by blackout_end.
    event_1m = [
        b
        for b in _completed(day_1m, b_end, "1m")
        if release <= int(b.time) < b_end
    ]
    atr = simple_atr(day_5m, b_start, cfg.atr_period)
    if ref_bar is None or not event_1m:
        return EventSnapshot(
            event_id=event.event_id,
            event_family=event.event_family,
            instrument=instrument,
            trading_date=td,
            release_ts=release,
            blackout_start_ts=b_start,
            blackout_end_ts=b_end,
            ref_price=None if ref_bar is None else float(ref_bar.close),
            event_open=None,
            event_high=None,
            event_low=None,
            event_close=None,
            event_range=None,
            signed_move=None,
            atr=atr,
            signed_move_atr=None,
            event_range_atr=None,
            retention=None,
            globex_vwap=None,
            close_vs_vwap=None,
            regime="DATA_INSUFFICIENT",
            skip_reason="missing_event_window_bars",
        )

    event_open = float(event_1m[0].open)
    event_high = max(float(b.high) for b in event_1m)
    event_low = min(float(b.low) for b in event_1m)
    event_close = float(event_1m[-1].close)
    event_range = event_high - event_low
    ref = float(ref_bar.close)
    signed = event_close - ref
    if signed >= 0:
        ext = event_high - ref
        retention = None if ext == 0 else signed / ext
    else:
        ext = ref - event_low
        retention = None if ext == 0 else (-signed) / ext
    vwap = globex_vwap(day_1m, td, b_end)
    regime = classify_regime(
        signed, event_range, atr, retention, event_close, event_high, event_low, cfg
    )
    return EventSnapshot(
        event_id=event.event_id,
        event_family=event.event_family,
        instrument=instrument,
        trading_date=td,
        release_ts=release,
        blackout_start_ts=b_start,
        blackout_end_ts=b_end,
        ref_price=ref,
        event_open=event_open,
        event_high=event_high,
        event_low=event_low,
        event_close=event_close,
        event_range=event_range,
        signed_move=signed,
        atr=atr,
        signed_move_atr=None if not atr else signed / atr,
        event_range_atr=None if not atr else event_range / atr,
        retention=retention,
        globex_vwap=vwap,
        close_vs_vwap=None if vwap is None else event_close - vwap,
        regime=regime,
        extras={
            "ref_bar_time": int(ref_bar.time),
            "event_1m_count": len(event_1m),
            "release_local": event.release_local,
            "blackout_profile": profile.profile_id,
            "actuals": event.actuals,
            "surprise_status": (event.surprise or {}).get("status"),
        },
    )


def forward_return(
    bars_1m: Sequence[Bar],
    start_ts: int,
    horizon_minutes: int,
) -> Optional[float]:
    """Close-to-close from last completed 1m at start_ts to last completed 1m at start+horizon."""
    a = last_completed(bars_1m, start_ts, "1m")
    b = last_completed(bars_1m, start_ts + horizon_minutes * 60, "1m")
    if a is None or b is None:
        return None
    return float(b.close) - float(a.close)


def continuation_path(
    snap: EventSnapshot,
    bars_1m: Sequence[Bar],
    horizons_min: Sequence[int],
) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if snap.regime not in ("MACRO_BULLISH", "MACRO_BEARISH") or snap.event_close is None:
        return {"eligible": False}
    sign = 1.0 if snap.regime == "MACRO_BULLISH" else -1.0
    start = snap.blackout_end_ts
    start_px = float(snap.event_close)
    for h in horizons_min:
        px = last_completed(bars_1m, start + h * 60, "1m")
        if px is None:
            out[f"fwd_{h}m"] = None
            continue
        raw = float(px.close) - start_px
        out[f"fwd_{h}m"] = raw
        out[f"fwd_{h}m_signed"] = raw * sign
    cash = last_completed(bars_1m, local_ts(snap.trading_date, CASH_OPEN_LOCAL), "1m")
    if cash is not None:
        raw = float(cash.close) - start_px
        out["fwd_0930"] = raw
        out["fwd_0930_signed"] = raw * sign
    eod = last_completed(bars_1m, local_ts(snap.trading_date, FORCE_CLOSE_LOCAL), "1m")
    if eod is not None:
        raw = float(eod.close) - start_px
        out["fwd_1555"] = raw
        out["fwd_1555_signed"] = raw * sign
    out["eligible"] = True
    out["regime"] = snap.regime
    out["sign"] = sign
    return out


def _in_blackout(ts: int, snap: EventSnapshot) -> bool:
    return snap.blackout_start_ts <= int(ts) < snap.blackout_end_ts


def _resolve_trade(
    *,
    direction: str,
    entry_ts: int,
    entry_px: float,
    stop: float,
    target: float,
    bars_1m: Sequence[Bar],
    flatten_ts: int,
    extra: dict[str, Any],
    exec_tf: str = "1m",
) -> dict[str, Any]:
    """Walk completed exec_tf bars AFTER the entry bar. Fail-closed on stop+target same bar."""
    risk = abs(entry_px - stop)
    if risk <= 0:
        return {"outcome": "INVALID_RISK", "points": None, "r_multiple": None, "exit_timestamp": None, "exit_price": None}
    mfe = 0.0
    mae = 0.0
    for b in bars_1m:
        close_ts = bar_close_ts(b, exec_tf)
        if close_ts is None or close_ts <= entry_ts:
            continue
        if int(b.time) >= flatten_ts:
            break
        hi = float(b.high)
        lo = float(b.low)
        if direction == "bullish":
            mfe = max(mfe, hi - entry_px)
            mae = min(mae, lo - entry_px)
            hit_t = hi >= target
            hit_s = lo <= stop
        else:
            mfe = max(mfe, entry_px - lo)
            mae = min(mae, entry_px - hi)
            hit_t = lo <= target
            hit_s = hi >= stop
        if hit_t and hit_s:
            return {
                "outcome": "AMBIGUOUS",
                "points": None,
                "r_multiple": None,
                "exit_timestamp": int(b.time),
                "exit_price": None,
                "mfe": mfe,
                "mae": mae,
            }
        if hit_t:
            pts = target - entry_px if direction == "bullish" else entry_px - target
            return {
                "outcome": "TARGET_HIT",
                "points": pts,
                "r_multiple": pts / risk,
                "exit_timestamp": int(b.time),
                "exit_price": target,
                "mfe": mfe,
                "mae": mae,
            }
        if hit_s:
            pts = stop - entry_px if direction == "bullish" else entry_px - stop
            return {
                "outcome": "STOP_HIT",
                "points": pts,
                "r_multiple": pts / risk,
                "exit_timestamp": int(b.time),
                "exit_price": stop,
                "mfe": mfe,
                "mae": mae,
            }
    last = last_completed(bars_1m, flatten_ts, exec_tf)
    if last is None:
        return {"outcome": "NO_DATA", "points": None, "r_multiple": None, "exit_timestamp": None, "exit_price": None, "mfe": mfe, "mae": mae}
    exit_px = float(last.close)
    pts = exit_px - entry_px if direction == "bullish" else entry_px - exit_px
    return {
        "outcome": "FORCE_CLOSE",
        "points": pts,
        "r_multiple": pts / risk,
        "exit_timestamp": int(last.time),
        "exit_price": exit_px,
        "mfe": mfe,
        "mae": mae,
    }


def _exec_tf(snap: EventSnapshot) -> str:
    return "5m" if snap.instrument == "GC" else "1m"


def _trade(
    snap: EventSnapshot,
    direction: str,
    entry_ts: int,
    entry_px: float,
    stop: float,
    target: float,
    bars_1m: Sequence[Bar],
    cfg: PostNewsStrategyConfig,
    extras: dict[str, Any],
    exec_tf: Optional[str] = None,
) -> Optional[PostNewsTrade]:
    if _in_blackout(entry_ts, snap):
        return None
    flatten = local_ts(snap.trading_date, cfg.flatten_local)
    if entry_ts >= flatten:
        return None
    tf = exec_tf or _exec_tf(snap)
    resolved = _resolve_trade(
        direction=direction,
        entry_ts=entry_ts,
        entry_px=entry_px,
        stop=stop,
        target=target,
        bars_1m=bars_1m,
        flatten_ts=flatten,
        extra=extras,
        exec_tf=tf,
    )
    risk = abs(entry_px - stop)
    return PostNewsTrade(
        trade_id=f"{snap.instrument}|MACRO|{snap.event_family}|{snap.trading_date}|{direction}|{entry_ts}",
        trading_date=snap.trading_date,
        direction=direction,
        entry_timestamp=entry_ts,
        entry_price=entry_px,
        stop_price=stop,
        target_price=target,
        exit_timestamp=resolved.get("exit_timestamp"),
        exit_price=resolved.get("exit_price"),
        outcome=str(resolved["outcome"]),
        points=resolved.get("points"),
        r_multiple=resolved.get("r_multiple"),
        extras={
            **extras,
            "event_id": snap.event_id,
            "event_family": snap.event_family,
            "regime": snap.regime,
            "entry_family": cfg.entry_family,
            "delay_minutes": cfg.delay_minutes,
            "config_hash": config_hash(cfg),
            "risk_points": risk,
            "mfe": resolved.get("mfe"),
            "mae": resolved.get("mae"),
            "instrument": snap.instrument,
            "blackout_end_ts": snap.blackout_end_ts,
        },
    )


def _direction(regime: str) -> Optional[str]:
    if regime == "MACRO_BULLISH":
        return "bullish"
    if regime == "MACRO_BEARISH":
        return "bearish"
    return None


def _eligible_start(snap: EventSnapshot, cfg: PostNewsStrategyConfig) -> int:
    """First timestamp at which orders are allowed: max(blackout_end, release + delay)."""
    release_plus = snap.release_ts + int(cfg.delay_minutes) * 60
    return max(snap.blackout_end_ts, release_plus)


def entry_range_breakout(
    snap: EventSnapshot,
    bars_1m: Sequence[Bar],
    cfg: PostNewsStrategyConfig,
) -> Optional[PostNewsTrade]:
    direction = _direction(snap.regime)
    if direction is None or snap.event_high is None or snap.event_low is None:
        return None
    start = _eligible_start(snap, cfg)
    flatten = local_ts(snap.trading_date, cfg.flatten_local)
    tf = _exec_tf(snap)
    for b in _completed(bars_1m, flatten, tf):
        cts = bar_close_ts(b, tf)
        if cts is None or cts < start or cts >= flatten:
            continue
        if direction == "bullish" and float(b.close) > float(snap.event_high):
            stop = float(snap.event_low)
            risk = float(b.close) - stop
            if risk <= 0:
                return None
            target = float(b.close) + cfg.target_r * risk
            return _trade(snap, direction, cts, float(b.close), stop, target, bars_1m, cfg, {"signal": "close_above_event_high"})
        if direction == "bearish" and float(b.close) < float(snap.event_low):
            stop = float(snap.event_high)
            risk = stop - float(b.close)
            if risk <= 0:
                return None
            target = float(b.close) - cfg.target_r * risk
            return _trade(snap, direction, cts, float(b.close), stop, target, bars_1m, cfg, {"signal": "close_below_event_low"})
    return None


def entry_5m_close(
    snap: EventSnapshot,
    bars_5m: Sequence[Bar],
    bars_1m: Sequence[Bar],
    cfg: PostNewsStrategyConfig,
) -> Optional[PostNewsTrade]:
    """First eligible completed 5m bar at/after delay that closes in regime direction vs event close."""
    direction = _direction(snap.regime)
    if direction is None or snap.event_close is None or snap.event_high is None or snap.event_low is None:
        return None
    start = _eligible_start(snap, cfg)
    flatten = local_ts(snap.trading_date, cfg.flatten_local)
    for b in _completed(bars_5m, flatten, "5m"):
        cts = bar_close_ts(b, "5m")
        if cts is None or cts < start or cts >= flatten:
            continue
        if direction == "bullish" and float(b.close) > float(snap.event_close):
            stop = float(snap.event_low)
            risk = float(b.close) - stop
            if risk <= 0:
                continue
            target = float(b.close) + cfg.target_r * risk
            return _trade(snap, direction, cts, float(b.close), stop, target, bars_1m, cfg, {"signal": "5m_close_above_event_close", "signal_bar_time": int(b.time)})
        if direction == "bearish" and float(b.close) < float(snap.event_close):
            stop = float(snap.event_high)
            risk = stop - float(b.close)
            if risk <= 0:
                continue
            target = float(b.close) - cfg.target_r * risk
            return _trade(snap, direction, cts, float(b.close), stop, target, bars_1m, cfg, {"signal": "5m_close_below_event_close", "signal_bar_time": int(b.time)})
        # First eligible bar failed confirmation — Family C does not wait further.
        return None
    return None


def entry_first_pullback(
    snap: EventSnapshot,
    bars_5m: Sequence[Bar],
    bars_1m: Sequence[Bar],
    cfg: PostNewsStrategyConfig,
) -> Optional[PostNewsTrade]:
    direction = _direction(snap.regime)
    if direction is None or snap.event_close is None:
        return None
    start = _eligible_start(snap, cfg)
    flatten = local_ts(snap.trading_date, cfg.flatten_local)
    pullback: Optional[Bar] = None
    for b in _completed(bars_5m, flatten, "5m"):
        cts = bar_close_ts(b, "5m")
        if cts is None or cts < start or cts >= flatten:
            continue
        if pullback is None:
            against = (
                float(b.close) < float(snap.event_close)
                if direction == "bullish"
                else float(b.close) > float(snap.event_close)
            )
            if against:
                pullback = b
            continue
        confirm = (
            float(b.close) > float(pullback.high)
            if direction == "bullish"
            else float(b.close) < float(pullback.low)
        )
        if not confirm:
            continue
        if direction == "bullish":
            stop = float(pullback.low)
            risk = float(b.close) - stop
            target = float(b.close) + cfg.target_r * risk
        else:
            stop = float(pullback.high)
            risk = stop - float(b.close)
            target = float(b.close) - cfg.target_r * risk
        if risk <= 0:
            return None
        return _trade(
            snap,
            direction,
            cts,
            float(b.close),
            stop,
            target,
            bars_1m,
            cfg,
            {"signal": "pullback_then_confirm", "pullback_bar_time": int(pullback.time)},
        )
    return None


def entry_cash_open(
    snap: EventSnapshot,
    bars_5m: Sequence[Bar],
    bars_1m: Sequence[Bar],
    cfg: PostNewsStrategyConfig,
) -> Optional[PostNewsTrade]:
    direction = _direction(snap.regime)
    if direction is None or snap.ref_price is None or snap.atr is None:
        return None
    cash = local_ts(snap.trading_date, CASH_OPEN_LOCAL)
    start = max(_eligible_start(snap, cfg), cash)
    # Regime persistence check at 09:30 using last completed 1m.
    px = last_completed(bars_1m, cash, "1m")
    if px is None:
        return None
    if direction == "bullish" and float(px.close) < float(snap.ref_price):
        return None
    if direction == "bearish" and float(px.close) > float(snap.ref_price):
        return None
    flatten = local_ts(snap.trading_date, cfg.flatten_local)
    atr = float(snap.atr)
    for b in _completed(bars_5m, flatten, "5m"):
        cts = bar_close_ts(b, "5m")
        if cts is None or cts < start or cts >= flatten:
            continue
        if direction == "bullish" and float(b.close) > float(px.close):
            stop = float(b.close) - atr
            target = float(b.close) + cfg.target_r * atr
            return _trade(snap, direction, cts, float(b.close), stop, target, bars_1m, cfg, {"signal": "cash_open_5m_confirm"})
        if direction == "bearish" and float(b.close) < float(px.close):
            stop = float(b.close) + atr
            target = float(b.close) - cfg.target_r * atr
            return _trade(snap, direction, cts, float(b.close), stop, target, bars_1m, cfg, {"signal": "cash_open_5m_confirm"})
        return None
    return None


def snapshot_event_5m(
    event: MacroEvent,
    bars_5m: Sequence[Bar],
    *,
    instrument: str,
    cfg: PostNewsStrategyConfig,
) -> EventSnapshot:
    """GC-style 5m-only snapshot. Event range = the 08:30 5m bar, complete at 08:35."""
    td = event.publication_date
    profile = cfg.news_profile
    b_start, b_end, release = blackout_window(td, event.release_local, profile)
    nearby = [b for b in bars_5m if abs(int(b.time) - release) < 48 * 3600]
    ref_bar = last_completed(nearby, release, "5m")
    event_bars = [
        b
        for b in _completed(nearby, b_end, "5m")
        if release <= int(b.time) < b_end
    ]
    atr = simple_atr(nearby, b_start, cfg.atr_period)
    if ref_bar is None or not event_bars:
        return EventSnapshot(
            event_id=event.event_id,
            event_family=event.event_family,
            instrument=instrument,
            trading_date=td,
            release_ts=release,
            blackout_start_ts=b_start,
            blackout_end_ts=b_end,
            ref_price=None if ref_bar is None else float(ref_bar.close),
            event_open=None,
            event_high=None,
            event_low=None,
            event_close=None,
            event_range=None,
            signed_move=None,
            atr=atr,
            signed_move_atr=None,
            event_range_atr=None,
            retention=None,
            globex_vwap=None,
            close_vs_vwap=None,
            regime="DATA_INSUFFICIENT",
            skip_reason="missing_event_window_bars_5m",
        )
    eb = event_bars[0]
    event_open = float(eb.open)
    event_high = max(float(b.high) for b in event_bars)
    event_low = min(float(b.low) for b in event_bars)
    event_close = float(event_bars[-1].close)
    event_range = event_high - event_low
    ref = float(ref_bar.close)
    signed = event_close - ref
    if signed >= 0:
        ext = event_high - ref
        retention = None if ext == 0 else signed / ext
    else:
        ext = ref - event_low
        retention = None if ext == 0 else (-signed) / ext
    regime = classify_regime(
        signed, event_range, atr, retention, event_close, event_high, event_low, cfg
    )
    return EventSnapshot(
        event_id=event.event_id,
        event_family=event.event_family,
        instrument=instrument,
        trading_date=td,
        release_ts=release,
        blackout_start_ts=b_start,
        blackout_end_ts=b_end,
        ref_price=ref,
        event_open=event_open,
        event_high=event_high,
        event_low=event_low,
        event_close=event_close,
        event_range=event_range,
        signed_move=signed,
        atr=atr,
        signed_move_atr=None if not atr else signed / atr,
        event_range_atr=None if not atr else event_range / atr,
        retention=retention,
        globex_vwap=None,
        close_vs_vwap=None,
        regime=regime,
        extras={
            "ref_bar_time": int(ref_bar.time),
            "event_bar_count": len(event_bars),
            "resolution": "5m_only",
            "actuals": event.actuals,
        },
    )


def replay_family(
    snap: EventSnapshot,
    bars_1m: Sequence[Bar],
    bars_5m: Sequence[Bar],
    cfg: PostNewsStrategyConfig,
) -> Optional[PostNewsTrade]:
    if snap.regime not in ("MACRO_BULLISH", "MACRO_BEARISH"):
        return None
    path = bars_5m if snap.instrument == "GC" else bars_1m
    fam = cfg.entry_family
    if fam == "A_RANGE_BREAKOUT":
        return entry_range_breakout(snap, path, cfg)
    if fam == "B_FIRST_PULLBACK":
        return entry_first_pullback(snap, bars_5m, path, cfg)
    if fam == "C_5M_CLOSE_CONFIRM":
        return entry_5m_close(snap, bars_5m, path, cfg)
    if fam == "D_CASH_OPEN":
        return entry_cash_open(snap, bars_5m, path, cfg)
    raise ValueError(f"unknown entry family {fam}")


def assert_no_blackout_actions(trades: Sequence[PostNewsTrade], snaps: Sequence[EventSnapshot]) -> dict[str, Any]:
    by_event = {s.event_id: s for s in snaps}
    violations = []
    for t in trades:
        snap = by_event.get(str((t.extras or {}).get("event_id")))
        if snap is None:
            continue
        if _in_blackout(int(t.entry_timestamp), snap):
            violations.append(t.trade_id)
        if t.exit_timestamp is not None and _in_blackout(int(t.exit_timestamp), snap):
            # Flatten/stop during blackout is also forbidden by the profile.
            # Stops placed before blackout could theoretically be broker-triggered;
            # this engine never *submits* during blackout. Flag intentional exits only.
            if t.outcome in ("TARGET_HIT", "STOP_HIT", "FORCE_CLOSE"):
                # Pre-existing stops that fill in the window are not an engine action;
                # engine entries are after blackout so this should be empty.
                violations.append(f"exit:{t.trade_id}")
    return {"ok": len(violations) == 0, "violations": violations[:20], "n_violations": len(violations)}
