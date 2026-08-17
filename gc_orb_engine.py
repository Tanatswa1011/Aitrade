"""GC opening-range breakout engine (OHLC + futures volume only)."""

from __future__ import annotations

import hashlib
import statistics
from datetime import date, datetime, timedelta
from typing import Any, Optional, Sequence
from zoneinfo import ZoneInfo

from models import (
    Bar,
    EntryAnalysis,
    EntryCandidate,
    EntryStatus,
    FixedRRTarget,
    RiskPlan,
    TargetConfig,
    TargetPlan,
)
from gc_orb_models import (
    DISPLACEMENT_BODY_OR_RATIO,
    EntryMode,
    GCORBEvent,
    GCORBSetup,
    GCORBStrategyConfig,
    INSTRUMENT,
    MAX_RETEST_BARS,
    OR_ANCHOR_LOCAL,
    OR_ANCHOR_NOTE,
    OR_TIMEZONE,
    OpeningRange,
    RVOL_LOOKBACK,
    STRATEGY_FAMILY,
    StopMode,
    VOLUME_RVOL_THRESHOLD,
)


def config_hash(cfg: GCORBStrategyConfig) -> str:
    raw = "|".join(
        [
            cfg.strategy_family,
            cfg.candidate_id,
            str(cfg.or_minutes),
            str(cfg.volume_filter),
            str(cfg.displacement_filter),
            str(cfg.rvol_threshold),
            str(cfg.displacement_body_or_ratio),
            cfg.entry_mode,
            cfg.stop_mode,
            str(cfg.max_retest_bars),
        ]
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _ny_dt(ts: int) -> datetime:
    return datetime.fromtimestamp(int(ts), tz=ZoneInfo(OR_TIMEZONE))


def trading_dates_in_bars(bars: Sequence[Bar]) -> list[str]:
    dates = sorted({_ny_dt(b.time).date().isoformat() for b in bars})
    return dates


def _or_window_utc(trading_date: str, or_minutes: int) -> tuple[int, int]:
    """OR from 08:20 NY for or_minutes."""
    d = date.fromisoformat(trading_date)
    hh, mm = map(int, OR_ANCHOR_LOCAL.split(":"))
    start_local = datetime(d.year, d.month, d.day, hh, mm, tzinfo=ZoneInfo(OR_TIMEZONE))
    end_local = start_local + timedelta(minutes=int(or_minutes))
    return int(start_local.timestamp()), int(end_local.timestamp())


def build_opening_range(
    bars: Sequence[Bar],
    trading_date: str,
    *,
    or_minutes: int = 30,
) -> OpeningRange:
    start_ts, end_ts = _or_window_utc(trading_date, or_minutes)
    window = [b for b in bars if start_ts <= int(b.time) < end_ts]
    if not window:
        return OpeningRange(
            trading_date=trading_date,
            start_timestamp=start_ts,
            end_timestamp=end_ts,
            high=0.0,
            low=0.0,
            midpoint=0.0,
            range_size=0.0,
            bar_count=0,
            complete=False,
            or_minutes=or_minutes,
            extras={"reason": "no_bars", "anchor_note": OR_ANCHOR_NOTE},
        )
    # Expect ~ or_minutes/5 bars for 5m data
    expected = max(1, int(or_minutes) // 5)
    complete = len(window) >= expected
    high = max(float(b.high) for b in window)
    low = min(float(b.low) for b in window)
    vols = [float(b.volume) for b in window if b.volume is not None]
    return OpeningRange(
        trading_date=trading_date,
        start_timestamp=start_ts,
        end_timestamp=end_ts,
        high=high,
        low=low,
        midpoint=(high + low) / 2.0,
        range_size=high - low,
        bar_count=len(window),
        complete=complete and (high > low),
        or_minutes=or_minutes,
        total_volume=sum(vols) if vols else None,
        median_bar_volume=statistics.median(vols) if vols else None,
        extras={
            "anchor_local": OR_ANCHOR_LOCAL,
            "anchor_timezone": OR_TIMEZONE,
            "anchor_note": OR_ANCHOR_NOTE,
            "expected_bars": expected,
        },
    )


def detect_roll_gap_timestamps(
    bars: Sequence[Bar],
    *,
    min_gap_sec: int = 3 * 3600,
    min_price_jump: float = 8.0,
    min_pct: float = 0.002,
) -> set[int]:
    """Flag bars after large time+price discontinuities (possible continuous-roll artifacts)."""
    ordered = sorted(bars, key=lambda b: int(b.time))
    flagged: set[int] = set()
    for a, b in zip(ordered, ordered[1:]):
        dt = int(b.time) - int(a.time)
        if dt < min_gap_sec:
            continue
        jump = abs(float(b.open) - float(a.close))
        thresh = max(min_price_jump, abs(float(a.close)) * min_pct)
        if jump >= thresh:
            flagged.add(int(b.time))
    return flagged


def rolling_median_volume(
    bars: Sequence[Bar],
    breakout_index: int,
    *,
    lookback: int = RVOL_LOOKBACK,
) -> Optional[float]:
    """Median of previous `lookback` completed bars; excludes breakout bar. No future leakage."""
    if breakout_index <= 0:
        return None
    start = max(0, breakout_index - lookback)
    window = bars[start:breakout_index]
    vols = [float(b.volume) for b in window if b.volume is not None]
    if not vols:
        return None
    return float(statistics.median(vols))


def compute_rvol(breakout_volume: Optional[float], reference: Optional[float]) -> Optional[float]:
    if breakout_volume is None or reference is None:
        return None
    if reference <= 0:
        return None
    return float(breakout_volume) / float(reference)


def make_breakout_id(trading_date: str, side: str, breakout_ts: int, or_minutes: int) -> str:
    return f"GC|{trading_date}|OR{or_minutes}|{side}|{breakout_ts}"


def find_first_breakouts(
    bars: Sequence[Bar],
    orng: OpeningRange,
    *,
    roll_flags: Optional[set[int]] = None,
    lookback: int = RVOL_LOOKBACK,
    contract: str = "GC=F",
) -> list[GCORBEvent]:
    """First bullish and first bearish close breakout after OR end."""
    if not orng.complete or orng.range_size <= 0:
        return []
    ordered = sorted(bars, key=lambda b: int(b.time))
    index_by_ts = {int(b.time): i for i, b in enumerate(ordered)}
    events: list[GCORBEvent] = []
    found_bull = False
    found_bear = False
    for bar in ordered:
        t = int(bar.time)
        if t < int(orng.end_timestamp):
            continue
        if found_bull and found_bear:
            break
        side = None
        if (not found_bull) and float(bar.close) > float(orng.high):
            side = "bullish"
            found_bull = True
        elif (not found_bear) and float(bar.close) < float(orng.low):
            side = "bearish"
            found_bear = True
        else:
            continue

        body = abs(float(bar.close) - float(bar.open))
        cr = float(bar.high) - float(bar.low)
        rs = float(orng.range_size)
        body_ratio = body / rs if rs > 0 else 0.0
        range_ratio = cr / rs if rs > 0 else 0.0
        dist = (
            float(bar.close) - float(orng.high)
            if side == "bullish"
            else float(orng.low) - float(bar.close)
        )
        i = index_by_ts[t]
        ref = rolling_median_volume(ordered, i, lookback=lookback)
        rvol = compute_rvol(None if bar.volume is None else float(bar.volume), ref)
        roll = bool(roll_flags and t in roll_flags)
        if roll_flags and any(
            orng.start_timestamp <= r < orng.end_timestamp for r in roll_flags
        ):
            roll = True

        events.append(
            GCORBEvent(
                breakout_id=make_breakout_id(orng.trading_date, side, t, orng.or_minutes),
                instrument=INSTRUMENT,
                contract=contract,
                trading_date=orng.trading_date,
                side=side,
                or_minutes=orng.or_minutes,
                or_high=orng.high,
                or_low=orng.low,
                or_midpoint=orng.midpoint,
                or_range_size=orng.range_size,
                breakout_timestamp=t,
                breakout_close=float(bar.close),
                breakout_high=float(bar.high),
                breakout_low=float(bar.low),
                breakout_open=float(bar.open),
                distance_beyond_range=dist,
                body=body,
                candle_range=cr,
                body_or_ratio=body_ratio,
                range_or_ratio=range_ratio,
                volume=None if bar.volume is None else float(bar.volume),
                reference_volume=ref,
                rvol=rvol,
                displacement_ok=body_ratio >= DISPLACEMENT_BODY_OR_RATIO,
                volume_ok=rvol is not None and rvol >= VOLUME_RVOL_THRESHOLD,
                roll_artifact=roll,
                extras={
                    "bars_from_or_end": max(0, (t - orng.end_timestamp) // 300),
                    "or_end": orng.end_timestamp,
                },
            )
        )
    return events


def find_retest(
    bars: Sequence[Bar],
    event: GCORBEvent,
    *,
    max_retest_bars: int = MAX_RETEST_BARS,
) -> Optional[dict[str, Any]]:
    ordered = sorted(bars, key=lambda b: int(b.time))
    after = [b for b in ordered if int(b.time) > int(event.breakout_timestamp)]
    for i, bar in enumerate(after[: max(1, max_retest_bars)]):
        if event.side == "bullish":
            touched = float(bar.low) <= float(event.or_high)
            held = float(bar.close) >= float(event.or_high)
        else:
            touched = float(bar.high) >= float(event.or_low)
            held = float(bar.close) <= float(event.or_low)
        if touched and held:
            return {
                "retest_timestamp": int(bar.time),
                "retest_low": float(bar.low),
                "retest_high": float(bar.high),
                "retest_close": float(bar.close),
                "bars_after_breakout": i + 1,
                "bar": bar,
            }
    return None


def build_risk(
    *,
    direction: str,
    entry_price: float,
    stop_price: float,
) -> RiskPlan:
    if direction == "bullish":
        if not (stop_price < entry_price):
            return RiskPlan(
                direction=direction,
                stop_mode="gc_orb",
                entry_price=entry_price,
                stop_price=stop_price,
                risk_distance=None,
                risk_points=None,
                buffer=0.0,
                valid=False,
                invalidation_reason="stop_not_directional",
                setup_reference={},
                extras={},
            )
    else:
        if not (stop_price > entry_price):
            return RiskPlan(
                direction=direction,
                stop_mode="gc_orb",
                entry_price=entry_price,
                stop_price=stop_price,
                risk_distance=None,
                risk_points=None,
                buffer=0.0,
                valid=False,
                invalidation_reason="stop_not_directional",
                setup_reference={},
                extras={},
            )
    risk = abs(entry_price - stop_price)
    if risk <= 0:
        return RiskPlan(
            direction=direction,
            stop_mode="gc_orb",
            entry_price=entry_price,
            stop_price=stop_price,
            risk_distance=None,
            risk_points=None,
            buffer=0.0,
            valid=False,
            invalidation_reason="non_positive_risk",
            setup_reference={},
            extras={},
        )
    return RiskPlan(
        direction=direction,
        stop_mode="gc_orb",
        entry_price=entry_price,
        stop_price=stop_price,
        risk_distance=risk,
        risk_points=risk,
        buffer=0.0,
        valid=True,
        invalidation_reason=None,
        setup_reference={},
        extras={},
    )


def build_targets(entry: float, stop: float, direction: str, or_size: float) -> tuple[list[dict], dict]:
    risk = abs(entry - stop)
    fixed = []
    for rr in (1.0, 1.5, 2.0, 3.0):
        dist = risk * rr
        price = entry + dist if direction == "bullish" else entry - dist
        fixed.append({"rr": rr, "price": price, "distance": dist})
    boundary = entry  # descriptive from breakout boundary tracked separately
    # range extension from OR boundary is descriptive in extras
    return fixed, {
        "plus_1x_or": (entry + or_size) if direction == "bullish" else (entry - or_size),
        "plus_2x_or": (entry + 2 * or_size) if direction == "bullish" else (entry - 2 * or_size),
    }


def analyze_breakout_for_candidate(
    event: GCORBEvent,
    bars: Sequence[Bar],
    cfg: GCORBStrategyConfig,
) -> GCORBSetup:
    setup_id = f"{event.breakout_id}|cand:{cfg.candidate_id}|entry:{cfg.entry_mode}"

    def _empty(reason: str, state: str = "EXPIRED") -> GCORBSetup:
        return GCORBSetup(
            strategy_family=STRATEGY_FAMILY,
            setup_id=setup_id,
            breakout_id=event.breakout_id,
            candidate_id=cfg.candidate_id,
            trading_date=event.trading_date,
            direction=event.side,
            entry_mode=cfg.entry_mode,
            entry_price=None,
            entry_timestamp=None,
            entry_triggered=False,
            stop_price=None,
            risk_distance=None,
            risk_valid=False,
            risk_invalidation_reason=None,
            event=event.to_dict(),
            state=state,
            reason=reason,
            extras={},
        )

    if event.roll_artifact:
        return _empty("ROLL_ARTIFACT", state="INVALIDATED")

    if cfg.volume_filter and not event.volume_ok:
        return _empty("volume_filter_fail")
    if cfg.displacement_filter and not event.displacement_ok:
        return _empty("displacement_filter_fail")

    retest = None
    if cfg.entry_mode in (EntryMode.RETEST_CLOSE.value, EntryMode.RETEST_BOUNDARY.value):
        retest = find_retest(bars, event, max_retest_bars=cfg.max_retest_bars)
        if retest is None:
            return _empty("retest_timeout")

    direction = event.side
    if cfg.entry_mode == EntryMode.BREAKOUT_CLOSE.value:
        entry_price = float(event.breakout_close)
        entry_ts = int(event.breakout_timestamp)
        # Range-invalidation stop (not breakout-bar extreme): same-bar low/high of the
        # breakout candle almost always tags TRIGGER_BAR_STOP_AMBIGUITY and yields zero
        # resolved trades. OR opposite boundary is the predeclared non-retest stop.
        stop = float(event.or_low) if direction == "bullish" else float(event.or_high)
    elif cfg.entry_mode == EntryMode.RETEST_CLOSE.value:
        assert retest is not None
        entry_price = float(retest["retest_close"])
        entry_ts = int(retest["retest_timestamp"])
        stop = float(retest["retest_low"]) if direction == "bullish" else float(retest["retest_high"])
    else:  # RETEST_BOUNDARY
        assert retest is not None
        entry_price = float(event.or_high if direction == "bullish" else event.or_low)
        entry_ts = int(retest["retest_timestamp"])
        stop = float(retest["retest_low"]) if direction == "bullish" else float(retest["retest_high"])

    if cfg.stop_mode == StopMode.OR_MIDPOINT.value:
        stop = float(event.or_midpoint)
    elif cfg.stop_mode == StopMode.OR_OPPOSITE.value:
        stop = float(event.or_low) if direction == "bullish" else float(event.or_high)

    risk = build_risk(direction=direction, entry_price=entry_price, stop_price=stop)
    targets, ext = build_targets(entry_price, stop, direction, float(event.or_range_size)) if risk.valid else ([], {})

    return GCORBSetup(
        strategy_family=STRATEGY_FAMILY,
        setup_id=setup_id,
        breakout_id=event.breakout_id,
        candidate_id=cfg.candidate_id,
        trading_date=event.trading_date,
        direction=direction,
        entry_mode=cfg.entry_mode,
        entry_price=entry_price,
        entry_timestamp=entry_ts,
        entry_triggered=True,
        stop_price=risk.stop_price,
        risk_distance=risk.risk_distance,
        risk_valid=risk.valid,
        risk_invalidation_reason=risk.invalidation_reason,
        targets=targets,
        event=event.to_dict(),
        retest_timestamp=None if retest is None else int(retest["retest_timestamp"]),
        state="ENTRY_READY" if risk.valid else "INVALIDATED",
        reason=None if risk.valid else risk.invalidation_reason,
        extras={"range_extension_targets": ext, "rvol": event.rvol, "body_or_ratio": event.body_or_ratio},
    )


def setup_to_entry_analysis(setup: GCORBSetup) -> EntryAnalysis:
    entry = EntryCandidate(
        mode=setup.entry_mode,
        direction=setup.direction,
        price=setup.entry_price,
        triggered=setup.entry_triggered,
        trigger_timestamp=setup.entry_timestamp,
        trigger_bar_index=None,
        fvg_reference={},
        setup_reference={"setup_id": setup.setup_id, "breakout_id": setup.breakout_id},
        entry_depth=None,
        max_retrace_depth=None,
        bars_after_fvg=None,
        status=EntryStatus.TRIGGERED.value if setup.entry_triggered else EntryStatus.WAITING.value,
        extras={},
    )
    risk = RiskPlan(
        direction=setup.direction,
        stop_mode="gc_orb",
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
