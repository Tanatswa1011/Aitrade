"""Phase 28 — GC NY momentum / continuation engine."""

from __future__ import annotations

import hashlib
import statistics
from datetime import date, datetime
from typing import Any, Optional, Sequence
from zoneinfo import ZoneInfo

from gc_momentum_models import (
    CLOSE_LOCATION_PCT,
    MAX_ENTRY_BARS,
    NO_NEW_SETUP_AFTER_LOCAL,
    OR_TIMEZONE,
    RANGE_MULTIPLIER,
    RVOL_LOOKBACK,
    RVOL_THRESHOLD,
    SESSION_END_LOCAL,
    SESSION_NOTE,
    SESSION_START_LOCAL,
    STRATEGY_FAMILY,
    EntryMode,
    GCMomentumImpulse,
    GCMomentumSetup,
    GCMomentumStrategyConfig,
    PullbackMode,
)
from gc_orb_engine import build_risk, build_targets, compute_rvol, detect_roll_gap_timestamps, trading_dates_in_bars
from gc_vwap_engine import compute_session_vwap_series, session_window
from gc_vwap_models import DEFAULT_NY_SESSION
from models import (
    Bar,
    EntryAnalysis,
    EntryCandidate,
    EntryStatus,
    FixedRRTarget,
    RiskPlan,
    TargetPlan,
)

NY = ZoneInfo(OR_TIMEZONE)


def config_hash(cfg: GCMomentumStrategyConfig) -> str:
    raw = "|".join(
        [
            cfg.strategy_family,
            cfg.candidate_id,
            cfg.pullback_mode,
            cfg.entry_mode,
            str(cfg.volume_filter),
            str(cfg.rvol_threshold),
            str(cfg.range_multiplier),
            str(cfg.max_entry_bars),
        ]
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _local_ts(trading_date: str, hhmm: str) -> int:
    d = date.fromisoformat(trading_date)
    hh, mm = map(int, hhmm.split(":"))
    return int(datetime(d.year, d.month, d.day, hh, mm, tzinfo=NY).timestamp())


def momentum_session_window(trading_date: str) -> tuple[int, int, int]:
    return (
        _local_ts(trading_date, SESSION_START_LOCAL),
        _local_ts(trading_date, SESSION_END_LOCAL),
        _local_ts(trading_date, NO_NEW_SETUP_AFTER_LOCAL),
    )


def bar_range(bar: Bar) -> float:
    return max(0.0, float(bar.high) - float(bar.low))


def close_location(bar: Bar) -> Optional[float]:
    r = bar_range(bar)
    if r <= 0:
        return None
    return (float(bar.close) - float(bar.low)) / r


def median_prior_range(ordered: Sequence[Bar], idx: int, lookback: int = RVOL_LOOKBACK) -> Optional[float]:
    if idx <= 0:
        return None
    start = max(0, idx - lookback)
    ranges = [bar_range(b) for b in ordered[start:idx] if bar_range(b) > 0]
    if not ranges:
        return None
    return float(statistics.median(ranges))


def median_prior_volume(ordered: Sequence[Bar], idx: int, lookback: int = RVOL_LOOKBACK) -> Optional[float]:
    if idx <= 0:
        return None
    start = max(0, idx - lookback)
    vols = [float(b.volume) for b in ordered[start:idx] if b.volume is not None]
    if not vols:
        return None
    return float(statistics.median(vols))


def make_impulse_id(trading_date: str, direction: str, ts: int) -> str:
    return f"GC|{trading_date}|MOM_IMP|{direction}|{ts}"


def detect_impulses(
    bars: Sequence[Bar],
    trading_date: str,
    *,
    roll_flags: Optional[set[int]] = None,
    range_mult: float = RANGE_MULTIPLIER,
) -> list[dict[str, Any]]:
    """
    Non-overlapping IMPULSE_BREAK events inside the NY research session.
    After an impulse is emitted, scanning resumes on the next bar (no overlap).
    """
    flags = roll_flags or set()
    start, end, no_new = momentum_session_window(trading_date)
    ordered = sorted(bars, key=lambda b: int(b.time))
    index_by_ts = {int(b.time): i for i, b in enumerate(ordered)}
    session = [b for b in ordered if start <= int(b.time) < end]
    out: list[dict[str, Any]] = []
    session_high = None
    session_low = None
    i = 0
    while i < len(session):
        b = session[i]
        t = int(b.time)
        if t > no_new:
            break
        # prior session extremes exclude current bar
        prior_high = session_high
        prior_low = session_low
        # update extremes after checks
        hi, lo, cl = float(b.high), float(b.low), float(b.close)
        rng = bar_range(b)
        loc = close_location(b)
        glob_i = index_by_ts[t]
        med_r = median_prior_range(ordered, glob_i)
        med_v = median_prior_volume(ordered, glob_i)
        rvol = compute_rvol(None if b.volume is None else float(b.volume), med_v)

        bull = (
            prior_high is not None
            and cl > float(prior_high)
            and med_r is not None
            and med_r > 0
            and rng >= float(range_mult) * float(med_r)
            and loc is not None
            and loc >= (1.0 - CLOSE_LOCATION_PCT)
        )
        bear = (
            prior_low is not None
            and cl < float(prior_low)
            and med_r is not None
            and med_r > 0
            and rng >= float(range_mult) * float(med_r)
            and loc is not None
            and loc <= CLOSE_LOCATION_PCT
        )

        if bull or bear:
            direction = "bullish" if bull else "bearish"
            breakout_level = float(prior_high if bull else prior_low)
            impulse = GCMomentumImpulse(
                impulse_id=make_impulse_id(trading_date, direction, t),
                trading_date=trading_date,
                direction=direction,
                timestamp=t,
                open=float(b.open),
                high=hi,
                low=lo,
                close=cl,
                range_size=rng,
                median_range_20=float(med_r),
                range_ratio=rng / float(med_r),
                rvol=rvol,
                breakout_level=breakout_level,
                session_high_before=float(prior_high),
                session_low_before=float(prior_low),
                roll_artifact=t in flags,
                extras={"session_note": SESSION_NOTE, "bar_index_in_session": i},
            )
            out.append(
                {
                    **impulse.to_dict(),
                    "impulse_bar": b,
                    "impulse_idx_session": i,
                    "session_bars": session,
                    "ordered": ordered,
                    "no_new": no_new,
                    "session_end": end,
                    "session_start": start,
                }
            )
            # advance past impulse bar to avoid overlapping duplicate from same sequence
            session_high = hi if session_high is None else max(session_high, hi)
            session_low = lo if session_low is None else min(session_low, lo)
            i += 1
            continue

        session_high = hi if session_high is None else max(session_high, hi)
        session_low = lo if session_low is None else min(session_low, lo)
        i += 1
    return out


def collect_all_impulses(bars: Sequence[Bar]) -> list[dict[str, Any]]:
    ordered = sorted(bars, key=lambda b: int(b.time))
    roll = detect_roll_gap_timestamps(ordered)
    out: list[dict[str, Any]] = []
    for td in trading_dates_in_bars(ordered):
        out.extend(detect_impulses(ordered, td, roll_flags=roll))
    return out


def _pullback_extreme_and_done(
    seq: dict[str, Any],
    mode: str,
) -> tuple[Optional[int], Optional[float], Optional[float], Optional[str]]:
    """
    Scan bars after impulse for pullback completion.
    Returns (pullback_done_idx, pullback_extreme, pullback_touch_price, fail_reason).
    Extreme tracks adverse excursion during pullback window until confirmation starts.
    """
    session_bars = seq["session_bars"]
    i0 = int(seq["impulse_idx_session"])
    direction = seq["direction"]
    impulse_high = float(seq["high"])
    impulse_low = float(seq["low"])
    impulse_open = float(seq["open"])
    impulse_range = float(seq["range_size"])
    breakout = float(seq["breakout_level"])
    no_new = int(seq["no_new"])
    session_end = int(seq["session_end"])

    if mode == PullbackMode.NONE.value:
        return i0, (impulse_low if direction == "bullish" else impulse_high), None, None

    half = impulse_high - 0.5 * impulse_range if direction == "bullish" else impulse_low + 0.5 * impulse_range
    extreme = impulse_low if direction == "bullish" else impulse_high

    for k in range(i0 + 1, len(session_bars)):
        b = session_bars[k]
        t = int(b.time)
        if t >= session_end:
            break
        # invalidation: break impulse origin before pullback complete
        if direction == "bullish" and float(b.low) < impulse_low:
            return None, extreme, None, "invalid_broke_impulse_low"
        if direction == "bearish" and float(b.high) > impulse_high:
            return None, extreme, None, "invalid_broke_impulse_high"

        if direction == "bullish":
            extreme = min(extreme, float(b.low))
        else:
            extreme = max(extreme, float(b.high))

        touched = False
        if mode == PullbackMode.P1_HALF_RETRACE.value:
            touched = float(b.low) <= half if direction == "bullish" else float(b.high) >= half
        elif mode == PullbackMode.P2_IMPULSE_OPEN.value:
            touched = float(b.low) <= impulse_open <= float(b.high)
        elif mode == PullbackMode.P3_BREAKOUT_RETEST.value:
            touched = float(b.low) <= breakout <= float(b.high)
        else:
            return None, extreme, None, "unknown_pullback_mode"

        if touched:
            # pullback may complete after no_new for entry window, but setups should start pullback before end
            return k, extreme, half if mode == PullbackMode.P1_HALF_RETRACE.value else (
                impulse_open if mode == PullbackMode.P2_IMPULSE_OPEN.value else breakout
            ), None

        if t > no_new and k > i0 + 1:
            # allow pullback search a bit past no_new only if already started; else expire
            pass

    return None, extreme, None, "pullback_timeout"


def _find_continuation(
    seq: dict[str, Any],
    after_idx: int,
) -> Optional[dict[str, Any]]:
    """PULLBACK_CONTINUATION_BREAK after pullback (or after impulse for C0 handled separately)."""
    session_bars = seq["session_bars"]
    direction = seq["direction"]
    impulse_high = float(seq["high"])
    impulse_low = float(seq["low"])
    session_end = int(seq["session_end"])
    extreme = impulse_low if direction == "bullish" else impulse_high

    for k in range(after_idx + 1, len(session_bars)):
        b = session_bars[k]
        t = int(b.time)
        if t >= session_end:
            break
        prev = session_bars[k - 1]
        if direction == "bullish":
            if float(b.low) < impulse_low:
                return {"failed": "invalid_broke_impulse_low", "extreme": min(extreme, float(b.low))}
            extreme = min(extreme, float(b.low))
            if float(b.close) > float(prev.high):
                return {
                    "confirmation_idx": k,
                    "confirmation_bar": b,
                    "confirmation_timestamp": t,
                    "pullback_extreme": extreme,
                }
        else:
            if float(b.high) > impulse_high:
                return {"failed": "invalid_broke_impulse_high", "extreme": max(extreme, float(b.high))}
            extreme = max(extreme, float(b.high))
            if float(b.close) < float(prev.low):
                return {
                    "confirmation_idx": k,
                    "confirmation_bar": b,
                    "confirmation_timestamp": t,
                    "pullback_extreme": extreme,
                }
    return None


def _find_entry(
    seq: dict[str, Any],
    conf: dict[str, Any],
    cfg: GCMomentumStrategyConfig,
    stop_seed: float,
) -> Optional[dict[str, Any]]:
    session_bars = seq["session_bars"]
    direction = seq["direction"]
    conf_idx = int(conf["confirmation_idx"])
    conf_bar = conf["confirmation_bar"]
    max_bars = int(cfg.max_entry_bars)
    impulse_high = float(seq["high"])
    impulse_low = float(seq["low"])
    breakout = float(seq["breakout_level"])
    extreme = float(conf.get("pullback_extreme", stop_seed))

    if cfg.entry_mode == EntryMode.CONFIRMATION_CLOSE.value:
        return {
            "entry_price": float(conf_bar.close),
            "entry_timestamp": int(conf_bar.time),
            "entry_bar": conf_bar,
            "stop_extreme": extreme,
        }

    if cfg.entry_mode == EntryMode.CONFIRMATION_MIDPOINT_RETEST.value:
        mid = (float(conf_bar.high) + float(conf_bar.low)) / 2.0
        for k in range(conf_idx + 1, min(len(session_bars), conf_idx + 1 + max_bars)):
            b = session_bars[k]
            if direction == "bullish" and float(b.low) < impulse_low:
                return None
            if direction == "bearish" and float(b.high) > impulse_high:
                return None
            if direction == "bullish":
                extreme = min(extreme, float(b.low))
            else:
                extreme = max(extreme, float(b.high))
            if float(b.low) <= mid <= float(b.high):
                return {
                    "entry_price": mid,
                    "entry_timestamp": int(b.time),
                    "entry_bar": b,
                    "stop_extreme": extreme,
                    "frozen_level": mid,
                }
        return None

    if cfg.entry_mode == EntryMode.BREAKOUT_LEVEL_RETEST.value:
        level = breakout
        for k in range(conf_idx + 1, min(len(session_bars), conf_idx + 1 + max_bars)):
            b = session_bars[k]
            if direction == "bullish" and float(b.low) < impulse_low:
                return None
            if direction == "bearish" and float(b.high) > impulse_high:
                return None
            if direction == "bullish":
                extreme = min(extreme, float(b.low))
            else:
                extreme = max(extreme, float(b.high))
            if float(b.low) <= level <= float(b.high):
                return {
                    "entry_price": level,
                    "entry_timestamp": int(b.time),
                    "entry_bar": b,
                    "stop_extreme": extreme,
                    "frozen_level": level,
                }
        return None

    return None


def analyze_candidate(seq: dict[str, Any], cfg: GCMomentumStrategyConfig) -> GCMomentumSetup:
    eid = seq["impulse_id"]
    setup_id = f"{eid}|cand:{cfg.candidate_id}"

    def _empty(reason: str, state: str = "EXPIRED", **extra) -> GCMomentumSetup:
        return GCMomentumSetup(
            strategy_family=STRATEGY_FAMILY,
            setup_id=setup_id,
            impulse_id=eid,
            candidate_id=cfg.candidate_id,
            trading_date=seq["trading_date"],
            direction=seq["direction"],
            pullback_mode=cfg.pullback_mode,
            entry_mode=cfg.entry_mode,
            entry_price=None,
            entry_timestamp=None,
            entry_triggered=False,
            stop_price=None,
            risk_distance=None,
            risk_valid=False,
            risk_invalidation_reason=None,
            impulse={k: v for k, v in seq.items() if k not in ("impulse_bar", "session_bars", "ordered")},
            state=state,
            reason=reason,
            extras=extra,
        )

    if seq.get("roll_artifact"):
        return _empty("ROLL_ARTIFACT", state="INVALIDATED")
    if int(seq["timestamp"]) > int(seq["no_new"]):
        return _empty("past_no_new_setup_cutoff")

    if cfg.volume_filter:
        rvol = seq.get("rvol")
        if rvol is None or float(rvol) < float(cfg.rvol_threshold):
            return _empty("rvol_filter_failed", state="INVALIDATED")

    direction = seq["direction"]

    # C0 control: entry at impulse close
    if cfg.pullback_mode == PullbackMode.NONE.value or cfg.entry_mode == EntryMode.IMPULSE_CLOSE.value:
        entry_price = float(seq["close"])
        entry_ts = int(seq["timestamp"])
        stop = float(seq["low"] if direction == "bullish" else seq["high"])
        risk = build_risk(direction=direction, entry_price=entry_price, stop_price=stop)
        targets, _ = build_targets(entry_price, stop, direction, float(seq["range_size"])) if risk.valid else ([], {})
        return GCMomentumSetup(
            strategy_family=STRATEGY_FAMILY,
            setup_id=setup_id,
            impulse_id=eid,
            candidate_id=cfg.candidate_id,
            trading_date=seq["trading_date"],
            direction=direction,
            pullback_mode=cfg.pullback_mode,
            entry_mode=cfg.entry_mode,
            entry_price=entry_price,
            entry_timestamp=entry_ts,
            entry_triggered=True,
            stop_price=risk.stop_price,
            risk_distance=risk.risk_distance,
            risk_valid=risk.valid,
            risk_invalidation_reason=risk.invalidation_reason,
            targets=targets,
            impulse={k: v for k, v in seq.items() if k not in ("impulse_bar", "session_bars", "ordered")},
            state="ENTRY_READY" if risk.valid else "INVALIDATED",
            reason=None if risk.valid else risk.invalidation_reason,
            extras={"control": True},
        )

    pb_idx, pb_extreme, touch_px, pb_fail = _pullback_extreme_and_done(seq, cfg.pullback_mode)
    if pb_fail and pb_fail.startswith("invalid"):
        return _empty(pb_fail, state="INVALIDATED", pullback_extreme=pb_extreme)
    if pb_idx is None:
        return _empty(pb_fail or "pullback_timeout", pullback_extreme=pb_extreme)

    conf = _find_continuation(seq, pb_idx)
    if conf is None:
        return _empty("no_continuation", pullback_extreme=pb_extreme)
    if conf.get("failed"):
        return _empty(conf["failed"], state="INVALIDATED", pullback_extreme=conf.get("extreme"))

    # Keep tracking extreme through confirmation bar
    conf_bar = conf["confirmation_bar"]
    extreme = float(conf["pullback_extreme"])
    if direction == "bullish":
        extreme = min(extreme, float(conf_bar.low), float(pb_extreme or extreme))
    else:
        extreme = max(extreme, float(conf_bar.high), float(pb_extreme or extreme))
    conf["pullback_extreme"] = extreme

    if int(conf["confirmation_timestamp"]) >= int(seq["session_end"]):
        return _empty("confirmation_after_session_end")

    entry = _find_entry(seq, conf, cfg, extreme)
    if entry is None:
        return _empty("entry_timeout", confirmation_timestamp=conf["confirmation_timestamp"])

    entry_price = float(entry["entry_price"])
    entry_ts = int(entry["entry_timestamp"])
    stop = float(entry["stop_extreme"])
    if entry_ts >= int(seq["session_end"]):
        return _empty("entry_after_session_end")

    risk = build_risk(direction=direction, entry_price=entry_price, stop_price=stop)
    targets, _ = build_targets(entry_price, stop, direction, float(seq["range_size"])) if risk.valid else ([], {})

    return GCMomentumSetup(
        strategy_family=STRATEGY_FAMILY,
        setup_id=setup_id,
        impulse_id=eid,
        candidate_id=cfg.candidate_id,
        trading_date=seq["trading_date"],
        direction=direction,
        pullback_mode=cfg.pullback_mode,
        entry_mode=cfg.entry_mode,
        entry_price=entry_price,
        entry_timestamp=entry_ts,
        entry_triggered=True,
        stop_price=risk.stop_price,
        risk_distance=risk.risk_distance,
        risk_valid=risk.valid,
        risk_invalidation_reason=risk.invalidation_reason,
        targets=targets,
        impulse={k: v for k, v in seq.items() if k not in ("impulse_bar", "session_bars", "ordered")},
        state="ENTRY_READY" if risk.valid else "INVALIDATED",
        reason=None if risk.valid else risk.invalidation_reason,
        extras={
            "confirmation_timestamp": conf["confirmation_timestamp"],
            "pullback_idx": pb_idx,
            "pullback_touch": touch_px,
            "frozen_level": entry.get("frozen_level"),
            "rvol": seq.get("rvol"),
        },
    )


def setup_to_entry_analysis(setup: GCMomentumSetup) -> EntryAnalysis:
    entry = EntryCandidate(
        mode=setup.entry_mode,
        direction=setup.direction,
        price=setup.entry_price,
        triggered=setup.entry_triggered,
        trigger_timestamp=setup.entry_timestamp,
        trigger_bar_index=None,
        fvg_reference={},
        setup_reference={"setup_id": setup.setup_id, "impulse_id": setup.impulse_id},
        entry_depth=None,
        max_retrace_depth=None,
        bars_after_fvg=None,
        status=EntryStatus.TRIGGERED.value if setup.entry_triggered else EntryStatus.WAITING.value,
        extras={},
    )
    risk = RiskPlan(
        direction=setup.direction,
        stop_mode="pullback_extreme",
        entry_price=float(setup.entry_price or 0.0),
        stop_price=setup.stop_price,
        risk_distance=setup.risk_distance,
        risk_points=setup.risk_distance,
        buffer=0.0,
        valid=setup.risk_valid,
        invalidation_reason=setup.risk_invalidation_reason,
        setup_reference={},
        extras={},
    )
    fixed = [
        FixedRRTarget(rr=float(t["rr"]), price=float(t["price"]), distance=float(t["distance"]))
        for t in (setup.targets or [])
    ]
    target = TargetPlan(
        fixed_rr_targets=fixed,
        opposite_liquidity=False,
        opposite_liquidity_label=None,
        opposite_liquidity_price=None,
        rr_to_opposite=None,
        opposite_target_valid=False,
        valid=bool(fixed),
        setup_reference={},
        extras={},
    )
    return EntryAnalysis(entry=entry, risk=risk, target=target)


def vwap_context_at_impulse(bars: Sequence[Bar], seq: dict[str, Any]) -> dict[str, Any]:
    """Descriptive VWAP relationship at impulse (no filter)."""
    td = seq["trading_date"]
    states = compute_session_vwap_series(bars, td, session=DEFAULT_NY_SESSION)
    by_ts = {int(s.timestamp): s for s in states}
    st = by_ts.get(int(seq["timestamp"]))
    if st is None or st.vwap is None:
        return {"vwap_side": "unknown", "z": None, "vwap": None}
    close = float(seq["close"])
    vwap = float(st.vwap)
    if close > vwap:
        side = "above_vwap"
    elif close < vwap:
        side = "below_vwap"
    else:
        side = "at_vwap"
    # crossing: impulse open and close straddle VWAP
    o = float(seq["open"])
    if (o < vwap <= close) or (o > vwap >= close):
        side = "crossing_vwap"
    return {
        "vwap_side": side,
        "z": st.z_vwap,
        "vwap": vwap,
        "sigma": st.session_std,
    }


def session_directional_efficiency(bars: Sequence[Bar], trading_date: str) -> Optional[float]:
    start, end, _ = momentum_session_window(trading_date)
    session = [b for b in bars if start <= int(b.time) < end]
    if len(session) < 2:
        return None
    hi = max(float(b.high) for b in session)
    lo = min(float(b.low) for b in session)
    total = hi - lo
    if total <= 0:
        return None
    directional = abs(float(session[-1].close) - float(session[0].open))
    return directional / total
