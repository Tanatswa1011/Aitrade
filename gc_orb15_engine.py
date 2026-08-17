"""GC OR15 breakout + boundary retest / FVG entry engine (Phase 24)."""

from __future__ import annotations

import hashlib
from dataclasses import replace
from typing import Any, Optional, Sequence

from fvg_detect import _bearish_fvg, _bullish_fvg
from gc_orb_engine import (
    build_opening_range,
    build_risk,
    build_targets,
    detect_roll_gap_timestamps,
    trading_dates_in_bars,
)
from gc_orb15_models import (
    HORIZON_BARS,
    MAX_FVG_CREATION_BARS,
    MAX_FVG_RETRACE_BARS,
    MAX_RETEST_BARS,
    OR_MINUTES,
    STRATEGY_FAMILY,
    EntryMode,
    ORB15BreakoutEvent,
    ORB15FVGZone,
    ORB15Setup,
    ORB15StrategyConfig,
    StopMode,
)
from models import (
    Bar,
    EntryAnalysis,
    EntryCandidate,
    EntryStatus,
    FixedRRTarget,
    RiskPlan,
    TargetPlan,
)


def config_hash(cfg: ORB15StrategyConfig) -> str:
    raw = "|".join(
        [
            cfg.strategy_family,
            cfg.candidate_id,
            str(cfg.or_minutes),
            cfg.entry_mode,
            cfg.stop_mode,
            str(cfg.max_retest_bars),
            str(cfg.max_fvg_creation_bars),
            str(cfg.max_fvg_retrace_bars),
            str(cfg.volume_filter),
            str(cfg.displacement_filter),
        ]
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def make_event_id(trading_date: str, direction: str, breakout_ts: int) -> str:
    return f"GC|{trading_date}|OR15|{direction}|{breakout_ts}"


def find_first_or15_breakout(
    bars: Sequence[Bar],
    orng,
    *,
    roll_flags: Optional[set[int]] = None,
    contract: str = "GC",
) -> Optional[ORB15BreakoutEvent]:
    """First 5m close breakout after OR15 complete; one canonical event per day."""
    if not orng.complete or orng.range_size <= 0:
        return None
    ordered = sorted(bars, key=lambda b: int(b.time))
    flags = roll_flags or set()
    first: Optional[ORB15BreakoutEvent] = None
    opposite_ts: Optional[int] = None
    for bar in ordered:
        t = int(bar.time)
        if t < int(orng.end_timestamp):
            continue
        bull = float(bar.close) > float(orng.high)
        bear = float(bar.close) < float(orng.low)
        if not bull and not bear:
            continue
        direction = "bullish" if bull else "bearish"
        if first is None:
            body = abs(float(bar.close) - float(bar.open))
            dist = (
                float(bar.close) - float(orng.high)
                if bull
                else float(orng.low) - float(bar.close)
            )
            first = ORB15BreakoutEvent(
                event_id=make_event_id(orng.trading_date, direction, t),
                trading_date=orng.trading_date,
                contract=contract,
                direction=direction,
                or_high=float(orng.high),
                or_low=float(orng.low),
                or_mid=float(orng.midpoint),
                or_size=float(orng.range_size),
                breakout_timestamp=t,
                breakout_open=float(bar.open),
                breakout_high=float(bar.high),
                breakout_low=float(bar.low),
                breakout_close=float(bar.close),
                distance_beyond_or=dist,
                roll_artifact=t in flags,
                body_or_ratio=(body / float(orng.range_size)) if orng.range_size else None,
                extras={"or_minutes": OR_MINUTES},
            )
        elif first is not None and direction != first.direction and opposite_ts is None:
            opposite_ts = t
            break
    if first is None:
        return None
    if opposite_ts is not None:
        return replace(
            first,
            opposite_break_after_first=True,
            opposite_break_timestamp=opposite_ts,
        )
    return first


def find_boundary_retest(
    bars: Sequence[Bar],
    event: ORB15BreakoutEvent,
    *,
    require_hold: bool,
    max_retest_bars: int = MAX_RETEST_BARS,
) -> Optional[dict[str, Any]]:
    ordered = sorted(bars, key=lambda b: int(b.time))
    after = [b for b in ordered if int(b.time) > int(event.breakout_timestamp)]
    for i, bar in enumerate(after[: max(1, max_retest_bars)]):
        if event.direction == "bullish":
            touched = float(bar.low) <= float(event.or_high)
            held = float(bar.close) >= float(event.or_high)
        else:
            touched = float(bar.high) >= float(event.or_low)
            held = float(bar.close) <= float(event.or_low)
        if not touched:
            continue
        if require_hold and not held:
            continue
        if not require_hold and not touched:
            continue
        return {
            "retest_timestamp": int(bar.time),
            "retest_open": float(bar.open),
            "retest_high": float(bar.high),
            "retest_low": float(bar.low),
            "retest_close": float(bar.close),
            "bars_after_breakout": i + 1,
            "held": held,
            "touched": touched,
        }
    return None


def find_first_breakout_fvg(
    bars: Sequence[Bar],
    event: ORB15BreakoutEvent,
    *,
    max_creation_bars: int = MAX_FVG_CREATION_BARS,
) -> Optional[ORB15FVGZone]:
    """First directional 3-candle FVG with c3 at/after breakout, within creation window."""
    ordered = sorted(bars, key=lambda b: int(b.time))
    # Index of first bar at/after breakout
    bo_idx = next((i for i, b in enumerate(ordered) if int(b.time) >= int(event.breakout_timestamp)), None)
    if bo_idx is None:
        return None
    # c3 must be within [bo_idx, bo_idx + max_creation_bars]
    end_c3 = min(len(ordered) - 1, bo_idx + max(0, max_creation_bars))
    for c3_i in range(max(2, bo_idx), end_c3 + 1):
        c1, c2, c3 = ordered[c3_i - 2], ordered[c3_i - 1], ordered[c3_i]
        if int(c3.time) < int(event.breakout_timestamp):
            continue
        if event.direction == "bullish":
            zone = _bullish_fvg(c1, c3)
        else:
            zone = _bearish_fvg(c1, c3)
        if zone is None:
            continue
        low, high = zone
        gap = high - low
        if gap <= 0:
            continue
        bars_after = sum(1 for b in ordered if int(event.breakout_timestamp) < int(b.time) <= int(c3.time))
        return ORB15FVGZone(
            direction=event.direction,
            low=low,
            high=high,
            ce=(low + high) / 2.0,
            created_timestamp=int(c3.time),
            c1_time=int(c1.time),
            c2_time=int(c2.time),
            c3_time=int(c3.time),
            bars_after_breakout=bars_after,
            gap_size=gap,
        )
    return None


def find_fvg_retrace_entry(
    bars: Sequence[Bar],
    fvg: ORB15FVGZone,
    *,
    mode: str,
    max_retrace_bars: int = MAX_FVG_RETRACE_BARS,
) -> Optional[dict[str, Any]]:
    ordered = sorted(bars, key=lambda b: int(b.time))
    after = [b for b in ordered if int(b.time) > int(fvg.created_timestamp)]
    if mode == EntryMode.FVG_TOUCH.value:
        target = float(fvg.high) if fvg.direction == "bullish" else float(fvg.low)
    else:
        target = float(fvg.ce)
    for i, bar in enumerate(after[: max(1, max_retrace_bars)]):
        if fvg.direction == "bullish":
            hit = float(bar.low) <= target
        else:
            hit = float(bar.high) >= target
        if hit:
            return {
                "entry_timestamp": int(bar.time),
                "entry_price": target,
                "bars_after_fvg": i + 1,
                "bar_high": float(bar.high),
                "bar_low": float(bar.low),
                "bar_close": float(bar.close),
            }
    return None


def analyze_candidate(
    event: ORB15BreakoutEvent,
    bars: Sequence[Bar],
    cfg: ORB15StrategyConfig,
) -> ORB15Setup:
    setup_id = f"{event.event_id}|cand:{cfg.candidate_id}|entry:{cfg.entry_mode}"

    def _empty(reason: str, state: str = "EXPIRED", **extra) -> ORB15Setup:
        return ORB15Setup(
            strategy_family=STRATEGY_FAMILY,
            setup_id=setup_id,
            orb_breakout_event_id=event.event_id,
            candidate_id=cfg.candidate_id,
            trading_date=event.trading_date,
            direction=event.direction,
            entry_mode=cfg.entry_mode,
            entry_price=None,
            entry_timestamp=None,
            entry_triggered=False,
            stop_price=None,
            stop_mode=cfg.stop_mode,
            risk_distance=None,
            risk_valid=False,
            risk_invalidation_reason=None,
            event=event.to_dict(),
            state=state,
            reason=reason,
            extras=extra,
        )

    if event.roll_artifact:
        return _empty("ROLL_ARTIFACT", state="INVALIDATED")

    direction = event.direction
    fvg_dict = None
    retest_ts = None
    excursion = None

    if cfg.entry_mode == EntryMode.BREAKOUT_CLOSE.value:
        entry_price = float(event.breakout_close)
        entry_ts = int(event.breakout_timestamp)
        stop = float(event.or_mid)
    elif cfg.entry_mode == EntryMode.RETEST_TOUCH.value:
        rt = find_boundary_retest(
            bars, event, require_hold=False, max_retest_bars=cfg.max_retest_bars
        )
        if rt is None:
            return _empty("retest_timeout")
        entry_price = float(event.or_high if direction == "bullish" else event.or_low)
        entry_ts = int(rt["retest_timestamp"])
        retest_ts = entry_ts
        # Intra-bar touch entry: do not use same-bar extreme (look-ahead) → OR mid
        stop = float(event.or_mid)
        excursion = abs(float(event.breakout_close) - entry_price)
    elif cfg.entry_mode == EntryMode.RETEST_CLOSE.value:
        rt = find_boundary_retest(
            bars, event, require_hold=True, max_retest_bars=cfg.max_retest_bars
        )
        if rt is None:
            return _empty("retest_timeout")
        entry_price = float(rt["retest_close"])
        entry_ts = int(rt["retest_timestamp"])
        retest_ts = entry_ts
        stop = float(rt["retest_low"] if direction == "bullish" else rt["retest_high"])
        excursion = abs(float(event.breakout_close) - entry_price)
    elif cfg.entry_mode in (EntryMode.FVG_TOUCH.value, EntryMode.FVG_CE.value):
        fvg = find_first_breakout_fvg(
            bars, event, max_creation_bars=cfg.max_fvg_creation_bars
        )
        if fvg is None:
            return _empty("no_fvg_within_creation_window")
        fvg_dict = fvg.to_dict()
        hit = find_fvg_retrace_entry(
            bars,
            fvg,
            mode=cfg.entry_mode,
            max_retrace_bars=cfg.max_fvg_retrace_bars,
        )
        if hit is None:
            return _empty("fvg_retrace_timeout", fvg=fvg_dict)
        entry_price = float(hit["entry_price"])
        entry_ts = int(hit["entry_timestamp"])
        stop = float(fvg.low if direction == "bullish" else fvg.high)
        excursion = abs(float(event.breakout_close) - entry_price)
    else:
        return _empty("unknown_entry_mode", state="INVALIDATED")

    risk = build_risk(direction=direction, entry_price=entry_price, stop_price=stop)
    targets, ext = (
        build_targets(entry_price, stop, direction, float(event.or_size))
        if risk.valid
        else ([], {})
    )
    return ORB15Setup(
        strategy_family=STRATEGY_FAMILY,
        setup_id=setup_id,
        orb_breakout_event_id=event.event_id,
        candidate_id=cfg.candidate_id,
        trading_date=event.trading_date,
        direction=direction,
        entry_mode=cfg.entry_mode,
        entry_price=entry_price,
        entry_timestamp=entry_ts,
        entry_triggered=True,
        stop_price=risk.stop_price,
        stop_mode=cfg.stop_mode,
        risk_distance=risk.risk_distance,
        risk_valid=risk.valid,
        risk_invalidation_reason=risk.invalidation_reason,
        targets=targets,
        event=event.to_dict(),
        fvg=fvg_dict,
        retest_timestamp=retest_ts,
        state="ENTRY_READY" if risk.valid else "INVALIDATED",
        reason=None if risk.valid else risk.invalidation_reason,
        extras={
            "range_extension_targets": ext,
            "excursion_before_entry": excursion,
            "body_or_ratio": event.body_or_ratio,
            "or_minutes": OR_MINUTES,
        },
    )


def setup_to_entry_analysis(setup: ORB15Setup) -> EntryAnalysis:
    entry = EntryCandidate(
        mode=setup.entry_mode,
        direction=setup.direction,
        price=setup.entry_price,
        triggered=setup.entry_triggered,
        trigger_timestamp=setup.entry_timestamp,
        trigger_bar_index=None,
        fvg_reference=setup.fvg or {},
        setup_reference={
            "setup_id": setup.setup_id,
            "orb_breakout_event_id": setup.orb_breakout_event_id,
        },
        entry_depth=None,
        max_retrace_depth=None,
        bars_after_fvg=None,
        status=EntryStatus.TRIGGERED.value if setup.entry_triggered else EntryStatus.WAITING.value,
        extras={},
    )
    risk = RiskPlan(
        direction=setup.direction,
        stop_mode=setup.stop_mode,
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


def resolve_intrabar_with_1m(
    *,
    entry_ts: int,
    entry_price: float,
    stop_price: float,
    direction: str,
    target_prices: Sequence[float],
    bars_1m: Sequence[Bar],
) -> dict[str, Any]:
    """
    Chronological 1m path after entry. Returns resolved order or STILL_AMBIGUOUS.
    """
    after = [b for b in sorted(bars_1m, key=lambda x: int(x.time)) if int(b.time) >= int(entry_ts)]
    for b in after:
        hit_stop = (
            float(b.low) <= float(stop_price)
            if direction == "bullish"
            else float(b.high) >= float(stop_price)
        )
        hit_tgts = []
        for tp in target_prices:
            hit = (
                float(b.high) >= float(tp)
                if direction == "bullish"
                else float(b.low) <= float(tp)
            )
            if hit:
                hit_tgts.append(float(tp))
        if hit_stop and hit_tgts:
            return {"status": "STILL_AMBIGUOUS", "bar_time": int(b.time)}
        if hit_stop:
            return {"status": "STOP_FIRST", "bar_time": int(b.time)}
        if hit_tgts:
            return {"status": "TARGET_FIRST", "bar_time": int(b.time), "targets": hit_tgts}
    return {"status": "UNRESOLVED"}


def collect_or15_events(bars: Sequence[Bar]) -> tuple[list, list[ORB15BreakoutEvent], set[int]]:
    ordered = sorted(bars, key=lambda b: int(b.time))
    roll_flags = detect_roll_gap_timestamps(ordered)
    opening_ranges = []
    events: list[ORB15BreakoutEvent] = []
    for td in trading_dates_in_bars(ordered):
        orng = build_opening_range(ordered, td, or_minutes=OR_MINUTES)
        opening_ranges.append(orng)
        if not orng.complete:
            continue
        ev = find_first_or15_breakout(ordered, orng, roll_flags=roll_flags)
        if ev is not None:
            events.append(ev)
    return opening_ranges, events, roll_flags
