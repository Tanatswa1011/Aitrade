"""Indicator-free liquidity sweep → reclaim → confirmation → entry (Phase 21)."""

from __future__ import annotations

import hashlib
from typing import Any, Optional, Sequence

from models import (
    Bar,
    EntryAnalysis,
    EntryCandidate,
    EntryStatus,
    LiquiditySweep,
    RiskPlan,
    SessionRange,
    StructureDirection,
    SweepSide,
    TargetConfig,
    TargetPlan,
)
from target_plan import build_target_plan
from liquidity_reclaim_models import (
    BreakMode,
    ConfirmationMode,
    EntryMode,
    LiquidityReclaimEvent,
    LiquidityReclaimSetup,
    ReclaimState,
    ReclaimStrategyConfig,
    STRATEGY_FAMILY,
)


def make_liquidity_event_id(
    symbol: str, session: str, trading_date: Optional[str], side: str, sweep_ts: int
) -> str:
    return f"{symbol}|{session}|{trading_date or 'unknown'}|{side}|{sweep_ts}"


def make_setup_id(event_id: str, cfg: ReclaimStrategyConfig) -> str:
    return (
        f"{event_id}|family:{STRATEGY_FAMILY}|cand:{cfg.candidate_id}"
        f"|tf:{cfg.execution_timeframe}|conf:{cfg.confirmation_mode}"
        f"|entry:{cfg.entry_mode}|brk:{cfg.break_mode}"
    )


def config_hash(cfg: ReclaimStrategyConfig) -> str:
    raw = "|".join(
        [
            cfg.strategy_family,
            cfg.candidate_id,
            cfg.confirmation_mode,
            cfg.break_mode,
            cfg.entry_mode,
            cfg.execution_timeframe,
            str(cfg.max_reclaim_bars),
            str(cfg.max_confirmation_bars),
            str(cfg.max_entry_bars),
            cfg.stop_mode,
            str(cfg.stop_buffer),
        ]
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _direction_for_side(side: str) -> str:
    if side in (SweepSide.LOW.value, "low"):
        return StructureDirection.BULLISH.value
    return StructureDirection.BEARISH.value


def is_bullish_sweep(bar: Bar, level: float) -> bool:
    """Strict penetration: low < L. Touch at equality is not a sweep."""
    return float(bar.low) < float(level)


def is_bearish_sweep(bar: Bar, level: float) -> bool:
    """Strict penetration: high > H."""
    return float(bar.high) > float(level)


def is_bullish_reclaim(bar: Bar, level: float) -> bool:
    """low < L AND close > L. Close exactly at L is not a reclaim."""
    return float(bar.low) < float(level) and float(bar.close) > float(level)


def is_bearish_reclaim(bar: Bar, level: float) -> bool:
    return float(bar.high) > float(level) and float(bar.close) < float(level)


def detect_first_penetration_sweep(
    session: SessionRange,
    bars: Sequence[Bar],
    *,
    side: str,
    search_from_ts: Optional[int] = None,
    search_to_ts: Optional[int] = None,
) -> Optional[dict[str, Any]]:
    """First bar that strictly penetrates session H/L after search_from."""
    if session.high is None or session.low is None:
        return None
    level = float(session.high if side in ("high", SweepSide.HIGH.value) else session.low)
    start = search_from_ts
    if start is None:
        start = session.end if session.end is not None else session.start
    if start is None:
        return None
    ordered = sorted(bars, key=lambda b: int(b.time))
    for bar in ordered:
        t = int(bar.time)
        if t < int(start):
            continue
        if search_to_ts is not None and t > int(search_to_ts):
            break
        if side in ("low", SweepSide.LOW.value):
            if not is_bullish_sweep(bar, level):
                continue
            extreme = float(bar.low)
            pen = float(level) - extreme
        else:
            if not is_bearish_sweep(bar, level):
                continue
            extreme = float(bar.high)
            pen = extreme - float(level)
        return {
            "side": "low" if side in ("low", SweepSide.LOW.value) else "high",
            "level": level,
            "sweep_timestamp": t,
            "sweep_extreme": extreme,
            "penetration": pen,
            "bar": bar,
        }
    return None


def find_reclaim(
    *,
    side: str,
    level: float,
    sweep_bar: Bar,
    bars_after_including_sweep: Sequence[Bar],
    max_reclaim_bars: int,
) -> Optional[dict[str, Any]]:
    """
    Reclaim on sweep candle or within next max_reclaim_bars completed bars.

    max_reclaim_bars=0 → sweep candle only.
    max_reclaim_bars=3 → sweep + up to 3 subsequent bars.
    """
    ordered = sorted(bars_after_including_sweep, key=lambda b: int(b.time))
    # Ensure sweep bar is first considered
    sweep_ts = int(sweep_bar.time)
    window = [b for b in ordered if int(b.time) >= sweep_ts]
    limit = 1 + max(0, int(max_reclaim_bars))
    for i, bar in enumerate(window[:limit]):
        ok = (
            is_bullish_reclaim(bar, level)
            if side in ("low", SweepSide.LOW.value)
            else is_bearish_reclaim(bar, level)
        )
        if not ok:
            continue
        return {
            "reclaim_timestamp": int(bar.time),
            "reclaim_close": float(bar.close),
            "reclaim_bars_after_sweep": i,
            "bar": bar,
        }
    return None


def _sweep_candle_break_level(side: str, sweep_bar: Bar) -> float:
    if side in ("low", SweepSide.LOW.value):
        return float(sweep_bar.high)
    return float(sweep_bar.low)


def find_confirmation(
    *,
    cfg: ReclaimStrategyConfig,
    side: str,
    level: float,
    sweep_bar: Bar,
    reclaim_bar: Bar,
    bars: Sequence[Bar],
) -> Optional[dict[str, Any]]:
    mode = cfg.confirmation_mode
    reclaim_ts = int(reclaim_bar.time)
    ordered = sorted(bars, key=lambda b: int(b.time))

    if mode == ConfirmationMode.IMMEDIATE_RECLAIM.value:
        return {
            "confirmation_timestamp": reclaim_ts,
            "confirmation_level": float(level),
            "confirmation_break_mode": None,
            "bar": reclaim_bar,
            "bars_from_reclaim": 0,
        }

    if mode == ConfirmationMode.CONFIRMATION_CANDLE.value:
        # Next completed candle after reclaim
        after = [b for b in ordered if int(b.time) > reclaim_ts]
        if not after:
            return None
        nxt = after[0]
        if len(after) > cfg.max_confirmation_bars:
            # still only need the immediate next candle; timeout if missing
            pass
        if side in ("low", SweepSide.LOW.value):
            bullish_close = float(nxt.close) > float(nxt.open)
            holds = float(nxt.close) > float(reclaim_bar.close) or float(nxt.close) > float(
                level
            )
            ok = bullish_close and holds
        else:
            bearish_close = float(nxt.close) < float(nxt.open)
            holds = float(nxt.close) < float(reclaim_bar.close) or float(nxt.close) < float(
                level
            )
            ok = bearish_close and holds
        if not ok:
            return None
        return {
            "confirmation_timestamp": int(nxt.time),
            "confirmation_level": float(level),
            "confirmation_break_mode": None,
            "bar": nxt,
            "bars_from_reclaim": 1,
        }

    if mode == ConfirmationMode.SWEEP_CANDLE_BREAK.value:
        conf_level = _sweep_candle_break_level(side, sweep_bar)
        after = [b for b in ordered if int(b.time) > reclaim_ts]
        break_mode = cfg.break_mode or BreakMode.CLOSE_BREAK.value
        for i, bar in enumerate(after[: max(1, cfg.max_confirmation_bars)]):
            if side in ("low", SweepSide.LOW.value):
                if break_mode == BreakMode.WICK_BREAK.value:
                    ok = float(bar.high) > conf_level
                else:
                    ok = float(bar.close) > conf_level
            else:
                if break_mode == BreakMode.WICK_BREAK.value:
                    ok = float(bar.low) < conf_level
                else:
                    ok = float(bar.close) < conf_level
            if ok:
                return {
                    "confirmation_timestamp": int(bar.time),
                    "confirmation_level": conf_level,
                    "confirmation_break_mode": break_mode,
                    "bar": bar,
                    "bars_from_reclaim": i + 1,
                }
        return None

    return None


def find_entry(
    *,
    cfg: ReclaimStrategyConfig,
    side: str,
    level: float,
    sweep_bar: Bar,
    confirmation_bar: Bar,
    bars: Sequence[Bar],
) -> Optional[dict[str, Any]]:
    conf_ts = int(confirmation_bar.time)
    ordered = sorted(bars, key=lambda b: int(b.time))
    mode = cfg.entry_mode

    if mode == EntryMode.CONFIRMATION_CLOSE.value:
        return {
            "price": float(confirmation_bar.close),
            "timestamp": conf_ts,
            "bar": confirmation_bar,
            "triggered": True,
        }

    after = [b for b in ordered if int(b.time) > conf_ts]
    window = after[: max(1, cfg.max_entry_bars)]

    if mode == EntryMode.LIQUIDITY_RETEST.value:
        for bar in window:
            # Retest: price trades back to liquidity level (wick touch inclusive)
            if side in ("low", SweepSide.LOW.value):
                touched = float(bar.low) <= float(level) <= float(bar.high)
            else:
                touched = float(bar.low) <= float(level) <= float(bar.high)
            if touched:
                return {
                    "price": float(level),
                    "timestamp": int(bar.time),
                    "bar": bar,
                    "triggered": True,
                }
        return {"price": None, "timestamp": None, "bar": None, "triggered": False}

    if mode == EntryMode.SWEEP_MIDPOINT.value:
        mid = (float(sweep_bar.high) + float(sweep_bar.low)) / 2.0
        for bar in window:
            touched = float(bar.low) <= mid <= float(bar.high)
            if touched:
                return {
                    "price": mid,
                    "timestamp": int(bar.time),
                    "bar": bar,
                    "triggered": True,
                }
        return {"price": None, "timestamp": None, "bar": None, "triggered": False}

    return None


def pre_entry_close_invalidation(
    *,
    direction: str,
    sweep_extreme: float,
    from_ts: int,
    entry_ts: int,
    bars: Sequence[Bar],
) -> tuple[bool, Optional[int]]:
    """Invalidate if a bar closes beyond sweep extreme before entry."""
    for bar in sorted(bars, key=lambda b: int(b.time)):
        t = int(bar.time)
        if t <= int(from_ts) or t >= int(entry_ts):
            continue
        if direction == StructureDirection.BULLISH.value:
            if float(bar.close) < float(sweep_extreme):
                return True, t
        else:
            if float(bar.close) > float(sweep_extreme):
                return True, t
    return False, None


def build_reclaim_risk(
    *,
    direction: str,
    entry_price: float,
    sweep_extreme: float,
    stop_buffer: float = 0.0,
) -> RiskPlan:
    if direction == StructureDirection.BULLISH.value:
        stop = float(sweep_extreme) - float(stop_buffer)
        if entry_price <= sweep_extreme:
            return RiskPlan(
                direction=direction,
                stop_mode="beyond_sweep",
                entry_price=entry_price,
                stop_price=stop,
                risk_distance=None,
                risk_points=None,
                buffer=stop_buffer,
                valid=False,
                invalidation_reason="entry_already_beyond_sweep_extreme",
                setup_reference={},
                extras={"sweep_extreme": sweep_extreme},
            )
        if not (stop < entry_price):
            return RiskPlan(
                direction=direction,
                stop_mode="beyond_sweep",
                entry_price=entry_price,
                stop_price=stop,
                risk_distance=None,
                risk_points=None,
                buffer=stop_buffer,
                valid=False,
                invalidation_reason="stop_not_directional",
                setup_reference={},
                extras={},
            )
    else:
        stop = float(sweep_extreme) + float(stop_buffer)
        if entry_price >= sweep_extreme:
            return RiskPlan(
                direction=direction,
                stop_mode="beyond_sweep",
                entry_price=entry_price,
                stop_price=stop,
                risk_distance=None,
                risk_points=None,
                buffer=stop_buffer,
                valid=False,
                invalidation_reason="entry_already_beyond_sweep_extreme",
                setup_reference={},
                extras={"sweep_extreme": sweep_extreme},
            )
        if not (stop > entry_price):
            return RiskPlan(
                direction=direction,
                stop_mode="beyond_sweep",
                entry_price=entry_price,
                stop_price=stop,
                risk_distance=None,
                risk_points=None,
                buffer=stop_buffer,
                valid=False,
                invalidation_reason="stop_not_directional",
                setup_reference={},
                extras={},
            )

    risk = abs(entry_price - stop)
    if risk <= 0:
        return RiskPlan(
            direction=direction,
            stop_mode="beyond_sweep",
            entry_price=entry_price,
            stop_price=stop,
            risk_distance=None,
            risk_points=None,
            buffer=stop_buffer,
            valid=False,
            invalidation_reason="non_positive_risk",
            setup_reference={},
            extras={},
        )
    return RiskPlan(
        direction=direction,
        stop_mode="beyond_sweep",
        entry_price=entry_price,
        stop_price=stop,
        risk_distance=risk,
        risk_points=risk,
        buffer=stop_buffer,
        valid=True,
        invalidation_reason=None,
        setup_reference={},
        extras={"sweep_extreme": sweep_extreme},
    )


def to_liquidity_sweep(session: SessionRange, sweep_info: dict[str, Any], reclaimed: bool) -> LiquiditySweep:
    bar: Bar = sweep_info["bar"]
    return LiquiditySweep(
        session=session.name,
        side=sweep_info["side"],
        level=float(sweep_info["level"]),
        sweep_timestamp=int(sweep_info["sweep_timestamp"]),
        sweep_price=float(sweep_info["sweep_extreme"]),
        maximum_excursion=float(sweep_info["penetration"]),
        reclaim_status=reclaimed,
        rule="phase21_penetration",
        sweep_candle=bar,
        session_range=session.to_dict() if hasattr(session, "to_dict") else None,
    )


def analyze_session_liquidity_reclaim(
    session: SessionRange,
    bars: Sequence[Bar],
    *,
    symbol: str,
    cfg: ReclaimStrategyConfig,
    side: str,
) -> LiquidityReclaimSetup:
    """Full deterministic chain for one session side + candidate config."""
    td = None
    extras = session.extras or {}
    rw = extras.get("resolved_window") or {}
    if rw.get("trading_date"):
        td = str(rw["trading_date"])[:10]

    empty_event = LiquidityReclaimEvent(
        liquidity_event_id=f"{symbol}|{session.name}|{td or 'unknown'}|{side}|none",
        symbol=symbol,
        session=session.name,
        trading_date=td,
        side=side,
        direction=_direction_for_side(side),
        liquidity_level=float(
            session.low if side == "low" else session.high or 0.0
        )
        if (session.low is not None and session.high is not None)
        else 0.0,
        sweep_timestamp=0,
        sweep_extreme=0.0,
        sweep_penetration=0.0,
        reclaim_timestamp=None,
        reclaim_close=None,
        reclaim_bars_after_sweep=None,
        confirmation_mode=cfg.confirmation_mode,
        confirmation_timestamp=None,
        confirmation_level=None,
        confirmation_break_mode=None,
        execution_timeframe=cfg.execution_timeframe,
        state=ReclaimState.NO_SETUP.value,
        reason="no_sweep",
        session_range_high=session.high,
        session_range_low=session.low,
    )

    def _setup_from_event(ev: LiquidityReclaimEvent, **kw) -> LiquidityReclaimSetup:
        return LiquidityReclaimSetup(
            strategy_family=STRATEGY_FAMILY,
            setup_id=make_setup_id(ev.liquidity_event_id, cfg),
            liquidity_event_id=ev.liquidity_event_id,
            symbol=symbol,
            session=session.name,
            trading_date=td,
            direction=ev.direction,
            execution_timeframe=cfg.execution_timeframe,
            event=ev,
            entry_mode=cfg.entry_mode,
            entry_price=kw.get("entry_price"),
            entry_timestamp=kw.get("entry_timestamp"),
            entry_triggered=bool(kw.get("entry_triggered")),
            stop_price=kw.get("stop_price"),
            risk_distance=kw.get("risk_distance"),
            risk_valid=bool(kw.get("risk_valid")),
            risk_invalidation_reason=kw.get("risk_invalidation_reason"),
            targets=kw.get("targets") or [],
            opposite_liquidity_price=kw.get("opposite_liquidity_price"),
            state=ev.state,
            reason=ev.reason,
            candidate_id=cfg.candidate_id,
            extras=kw.get("extras") or {},
        )

    if not session.complete or session.high is None or session.low is None:
        return _setup_from_event(
            LiquidityReclaimEvent(
                liquidity_event_id=empty_event.liquidity_event_id,
                symbol=symbol,
                session=session.name,
                trading_date=td,
                side=side,
                direction=_direction_for_side(side),
                liquidity_level=empty_event.liquidity_level,
                sweep_timestamp=0,
                sweep_extreme=0.0,
                sweep_penetration=0.0,
                reclaim_timestamp=None,
                reclaim_close=None,
                reclaim_bars_after_sweep=None,
                confirmation_mode=cfg.confirmation_mode,
                confirmation_timestamp=None,
                confirmation_level=None,
                confirmation_break_mode=None,
                execution_timeframe=cfg.execution_timeframe,
                state=ReclaimState.WAITING_FOR_SESSION.value,
                reason="session_incomplete",
                session_range_high=session.high,
                session_range_low=session.low,
            )
        )

    sweep_info = detect_first_penetration_sweep(session, bars, side=side)
    if sweep_info is None:
        return _setup_from_event(empty_event)

    event_id = make_liquidity_event_id(symbol, session.name, td, side, int(sweep_info["sweep_timestamp"]))
    sweep_bar: Bar = sweep_info["bar"]
    level = float(sweep_info["level"])
    direction = _direction_for_side(side)

    reclaim = find_reclaim(
        side=side,
        level=level,
        sweep_bar=sweep_bar,
        bars_after_including_sweep=bars,
        max_reclaim_bars=cfg.max_reclaim_bars,
    )
    if reclaim is None:
        ev = LiquidityReclaimEvent(
            liquidity_event_id=event_id,
            symbol=symbol,
            session=session.name,
            trading_date=td,
            side=side,
            direction=direction,
            liquidity_level=level,
            sweep_timestamp=int(sweep_info["sweep_timestamp"]),
            sweep_extreme=float(sweep_info["sweep_extreme"]),
            sweep_penetration=float(sweep_info["penetration"]),
            reclaim_timestamp=None,
            reclaim_close=None,
            reclaim_bars_after_sweep=None,
            confirmation_mode=cfg.confirmation_mode,
            confirmation_timestamp=None,
            confirmation_level=None,
            confirmation_break_mode=None,
            execution_timeframe=cfg.execution_timeframe,
            sweep_candle=sweep_bar.to_dict(),
            state=ReclaimState.EXPIRED.value,
            reason="reclaim_timeout",
            session_range_high=session.high,
            session_range_low=session.low,
            extras={"max_reclaim_bars": cfg.max_reclaim_bars},
        )
        return _setup_from_event(ev)

    reclaim_bar: Bar = reclaim["bar"]
    conf = find_confirmation(
        cfg=cfg,
        side=side,
        level=level,
        sweep_bar=sweep_bar,
        reclaim_bar=reclaim_bar,
        bars=bars,
    )
    if conf is None:
        ev = LiquidityReclaimEvent(
            liquidity_event_id=event_id,
            symbol=symbol,
            session=session.name,
            trading_date=td,
            side=side,
            direction=direction,
            liquidity_level=level,
            sweep_timestamp=int(sweep_info["sweep_timestamp"]),
            sweep_extreme=float(sweep_info["sweep_extreme"]),
            sweep_penetration=float(sweep_info["penetration"]),
            reclaim_timestamp=int(reclaim["reclaim_timestamp"]),
            reclaim_close=float(reclaim["reclaim_close"]),
            reclaim_bars_after_sweep=int(reclaim["reclaim_bars_after_sweep"]),
            confirmation_mode=cfg.confirmation_mode,
            confirmation_timestamp=None,
            confirmation_level=None,
            confirmation_break_mode=cfg.break_mode
            if cfg.confirmation_mode == ConfirmationMode.SWEEP_CANDLE_BREAK.value
            else None,
            execution_timeframe=cfg.execution_timeframe,
            sweep_candle=sweep_bar.to_dict(),
            reclaim_candle=reclaim_bar.to_dict(),
            state=ReclaimState.EXPIRED.value,
            reason="confirmation_timeout",
            session_range_high=session.high,
            session_range_low=session.low,
            extras={"bars_from_reclaim_attempted": True},
        )
        return _setup_from_event(ev)

    conf_bar: Bar = conf["bar"]
    entry = find_entry(
        cfg=cfg,
        side=side,
        level=level,
        sweep_bar=sweep_bar,
        confirmation_bar=conf_bar,
        bars=bars,
    )
    if entry is None or not entry.get("triggered"):
        ev = LiquidityReclaimEvent(
            liquidity_event_id=event_id,
            symbol=symbol,
            session=session.name,
            trading_date=td,
            side=side,
            direction=direction,
            liquidity_level=level,
            sweep_timestamp=int(sweep_info["sweep_timestamp"]),
            sweep_extreme=float(sweep_info["sweep_extreme"]),
            sweep_penetration=float(sweep_info["penetration"]),
            reclaim_timestamp=int(reclaim["reclaim_timestamp"]),
            reclaim_close=float(reclaim["reclaim_close"]),
            reclaim_bars_after_sweep=int(reclaim["reclaim_bars_after_sweep"]),
            confirmation_mode=cfg.confirmation_mode,
            confirmation_timestamp=int(conf["confirmation_timestamp"]),
            confirmation_level=float(conf["confirmation_level"]),
            confirmation_break_mode=conf.get("confirmation_break_mode"),
            execution_timeframe=cfg.execution_timeframe,
            sweep_candle=sweep_bar.to_dict(),
            reclaim_candle=reclaim_bar.to_dict(),
            confirmation_candle=conf_bar.to_dict(),
            state=ReclaimState.EXPIRED.value,
            reason="entry_not_triggered",
            session_range_high=session.high,
            session_range_low=session.low,
            extras={"bars_from_reclaim": conf.get("bars_from_reclaim")},
        )
        return _setup_from_event(ev, entry_triggered=False)

    entry_ts = int(entry["timestamp"])
    invalidated, inv_ts = pre_entry_close_invalidation(
        direction=direction,
        sweep_extreme=float(sweep_info["sweep_extreme"]),
        from_ts=int(reclaim["reclaim_timestamp"]),
        entry_ts=entry_ts,
        bars=bars,
    )
    if invalidated:
        ev = LiquidityReclaimEvent(
            liquidity_event_id=event_id,
            symbol=symbol,
            session=session.name,
            trading_date=td,
            side=side,
            direction=direction,
            liquidity_level=level,
            sweep_timestamp=int(sweep_info["sweep_timestamp"]),
            sweep_extreme=float(sweep_info["sweep_extreme"]),
            sweep_penetration=float(sweep_info["penetration"]),
            reclaim_timestamp=int(reclaim["reclaim_timestamp"]),
            reclaim_close=float(reclaim["reclaim_close"]),
            reclaim_bars_after_sweep=int(reclaim["reclaim_bars_after_sweep"]),
            confirmation_mode=cfg.confirmation_mode,
            confirmation_timestamp=int(conf["confirmation_timestamp"]),
            confirmation_level=float(conf["confirmation_level"]),
            confirmation_break_mode=conf.get("confirmation_break_mode"),
            execution_timeframe=cfg.execution_timeframe,
            sweep_candle=sweep_bar.to_dict(),
            reclaim_candle=reclaim_bar.to_dict(),
            confirmation_candle=conf_bar.to_dict(),
            state=ReclaimState.INVALIDATED.value,
            reason="pre_entry_close_beyond_sweep_extreme",
            session_range_high=session.high,
            session_range_low=session.low,
            extras={"invalidation_timestamp": inv_ts},
        )
        return _setup_from_event(ev, entry_triggered=False, extras={"invalidation_timestamp": inv_ts})

    risk = build_reclaim_risk(
        direction=direction,
        entry_price=float(entry["price"]),
        sweep_extreme=float(sweep_info["sweep_extreme"]),
        stop_buffer=cfg.stop_buffer,
    )
    ls = to_liquidity_sweep(session, sweep_info, True)
    ec = EntryCandidate(
        mode=cfg.entry_mode,
        direction=direction,
        price=float(entry["price"]),
        triggered=True,
        trigger_timestamp=entry_ts,
        trigger_bar_index=None,
        fvg_reference={},
        setup_reference={"liquidity_event_id": event_id},
        entry_depth=None,
        max_retrace_depth=None,
        bars_after_fvg=None,
        status=EntryStatus.TRIGGERED.value,
        extras={},
    )
    targets = build_target_plan(session, ls, ec, risk, TargetConfig())
    ev = LiquidityReclaimEvent(
        liquidity_event_id=event_id,
        symbol=symbol,
        session=session.name,
        trading_date=td,
        side=side,
        direction=direction,
        liquidity_level=level,
        sweep_timestamp=int(sweep_info["sweep_timestamp"]),
        sweep_extreme=float(sweep_info["sweep_extreme"]),
        sweep_penetration=float(sweep_info["penetration"]),
        reclaim_timestamp=int(reclaim["reclaim_timestamp"]),
        reclaim_close=float(reclaim["reclaim_close"]),
        reclaim_bars_after_sweep=int(reclaim["reclaim_bars_after_sweep"]),
        confirmation_mode=cfg.confirmation_mode,
        confirmation_timestamp=int(conf["confirmation_timestamp"]),
        confirmation_level=float(conf["confirmation_level"]),
        confirmation_break_mode=conf.get("confirmation_break_mode"),
        execution_timeframe=cfg.execution_timeframe,
        sweep_candle=sweep_bar.to_dict(),
        reclaim_candle=reclaim_bar.to_dict(),
        confirmation_candle=conf_bar.to_dict(),
        state=ReclaimState.ENTRY_READY.value if risk.valid else ReclaimState.INVALIDATED.value,
        reason=None if risk.valid else risk.invalidation_reason,
        session_range_high=session.high,
        session_range_low=session.low,
        extras={"bars_from_reclaim": conf.get("bars_from_reclaim")},
    )
    return _setup_from_event(
        ev,
        entry_price=float(entry["price"]),
        entry_timestamp=entry_ts,
        entry_triggered=True,
        stop_price=risk.stop_price,
        risk_distance=risk.risk_distance,
        risk_valid=risk.valid,
        risk_invalidation_reason=risk.invalidation_reason,
        targets=[t.to_dict() for t in (targets.fixed_rr_targets or [])],
        opposite_liquidity_price=targets.opposite_liquidity_price,
        extras={"target_plan_valid": targets.valid, "bars_from_reclaim": conf.get("bars_from_reclaim")},
    )


def setup_to_entry_analysis(setup: LiquidityReclaimSetup) -> EntryAnalysis:
    """Adapt LiquidityReclaimSetup into EntryAnalysis for outcome_engine."""
    entry = EntryCandidate(
        mode=setup.entry_mode,
        direction=setup.direction or "",
        price=setup.entry_price,
        triggered=setup.entry_triggered,
        trigger_timestamp=setup.entry_timestamp,
        trigger_bar_index=None,
        fvg_reference={},
        setup_reference={"setup_id": setup.setup_id},
        entry_depth=None,
        max_retrace_depth=None,
        bars_after_fvg=None,
        status=EntryStatus.TRIGGERED.value if setup.entry_triggered else EntryStatus.WAITING.value,
        extras={},
    )
    risk = RiskPlan(
        direction=setup.direction or "",
        stop_mode="beyond_sweep",
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
    from models import FixedRRTarget

    fixed = [
        FixedRRTarget(rr=float(t["rr"]), price=float(t["price"]), distance=float(t.get("distance") or 0))
        for t in (setup.targets or [])
        if t.get("price") is not None
    ]
    target = TargetPlan(
        fixed_rr_targets=fixed,
        opposite_liquidity=setup.opposite_liquidity_price is not None,
        opposite_liquidity_label=None,
        opposite_liquidity_price=setup.opposite_liquidity_price,
        rr_to_opposite=None,
        opposite_target_valid=setup.opposite_liquidity_price is not None,
        valid=bool(fixed),
        setup_reference={},
        extras={},
    )
    return EntryAnalysis(entry=entry, risk=risk, target=target)
