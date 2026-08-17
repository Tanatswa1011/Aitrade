"""Pure FVG entry-candidate evaluation (no TradingView / broker I/O)."""

from __future__ import annotations

from typing import Optional, Sequence

from models import (
    Bar,
    EntryCandidate,
    EntryConfig,
    EntryMode,
    EntryStatus,
    FVGZone,
    StructureDirection,
)

ENTRY_MODES = (
    EntryMode.FIRST_TOUCH.value,
    EntryMode.CE.value,
    EntryMode.BOUNDARY.value,
)


def _fvg_ref(fvg: FVGZone) -> dict:
    return {
        "direction": fvg.direction,
        "low": fvg.low,
        "high": fvg.high,
        "midpoint": fvg.midpoint,
        "created_timestamp": fvg.created_timestamp,
        "gap_size": fvg.gap_size,
        "candle1_timestamp": fvg.candle1_timestamp,
        "candle2_timestamp": fvg.candle2_timestamp,
        "candle3_timestamp": fvg.candle3_timestamp,
    }


def _validate_ordering(fvg: FVGZone) -> Optional[str]:
    """Fail closed if FVG / setup timeline is inconsistent."""
    if fvg.created_timestamp is None:
        return "missing_created_timestamp"
    if int(fvg.candle3_timestamp) != int(fvg.created_timestamp):
        return "candle3_created_mismatch"
    if not (
        int(fvg.candle1_timestamp)
        < int(fvg.candle2_timestamp)
        < int(fvg.candle3_timestamp)
    ):
        return "fvg_candle_order_invalid"

    setup = fvg.setup_reference or {}
    sweep_ts = setup.get("sweep_timestamp")
    choch_ts = setup.get("confirmation_timestamp")
    if sweep_ts is not None and choch_ts is not None:
        if int(choch_ts) <= int(sweep_ts):
            return "choch_not_after_sweep"
        if int(fvg.created_timestamp) <= int(choch_ts):
            return "fvg_not_after_choch"
    elif sweep_ts is not None and int(fvg.created_timestamp) <= int(sweep_ts):
        return "fvg_not_after_sweep"

    if fvg.direction not in (
        StructureDirection.BULLISH.value,
        StructureDirection.BEARISH.value,
    ):
        return "invalid_direction"
    if float(fvg.high) <= float(fvg.low):
        return "invalid_zone_bounds"
    return None


def boundary_price(fvg: FVGZone) -> float:
    """
    First edge encountered on retracement.

    Bullish: FVG high (price falling into zone from above).
    Bearish: FVG low (price rising into zone from below).
    """
    if fvg.direction == StructureDirection.BULLISH.value:
        return float(fvg.high)
    return float(fvg.low)


def ce_price(fvg: FVGZone) -> float:
    return float(fvg.midpoint)


def _gap(fvg: FVGZone) -> float:
    return max(float(fvg.high) - float(fvg.low), 0.0)


def entry_depth_at_price(fvg: FVGZone, price: float) -> float:
    """
    Depth of the configured entry price in the FVG.

    Bullish: (FVG_high - entry_price) / gap
    Bearish: (entry_price - FVG_low) / gap
    Clamped to [0, 1]. Boundary→0.0, CE→0.5, opposite edge→1.0.
    """
    gap = _gap(fvg)
    if gap <= 0:
        return 0.0
    p = float(price)
    if fvg.direction == StructureDirection.BULLISH.value:
        raw = (float(fvg.high) - p) / gap
    else:
        raw = (p - float(fvg.low)) / gap
    return max(0.0, min(1.0, raw))


# Back-compat alias used by older tests/callers.
retracement_depth_at_price = entry_depth_at_price


def bar_penetration_depth(fvg: FVGZone, bar: Bar) -> float:
    """Deepest market penetration into the zone on a single bar."""
    if fvg.direction == StructureDirection.BULLISH.value:
        deepest = float(bar.low)
        if deepest > float(fvg.high):
            return 0.0
        if deepest <= float(fvg.low):
            return 1.0
        return entry_depth_at_price(fvg, deepest)
    deepest = float(bar.high)
    if deepest < float(fvg.low):
        return 0.0
    if deepest >= float(fvg.high):
        return 1.0
    return entry_depth_at_price(fvg, deepest)


def max_retrace_depth_through(
    fvg: FVGZone, bars_through_trigger: Sequence[Bar]
) -> float:
    """Max penetration over bars from after FVG creation through the trigger bar."""
    if not bars_through_trigger:
        return 0.0
    return max(bar_penetration_depth(fvg, b) for b in bars_through_trigger)


# Deprecated name — penetration on one bar only.
retracement_depth_on_bar = bar_penetration_depth


def _touches_zone(fvg: FVGZone, bar: Bar) -> bool:
    if fvg.direction == StructureDirection.BULLISH.value:
        return float(bar.low) <= float(fvg.high)
    return float(bar.high) >= float(fvg.low)


def _touches_boundary(fvg: FVGZone, bar: Bar) -> bool:
    # Same geometric condition as first zone contact for v1.
    return _touches_zone(fvg, bar)


def _touches_ce(fvg: FVGZone, bar: Bar) -> bool:
    ce = ce_price(fvg)
    if fvg.direction == StructureDirection.BULLISH.value:
        return float(bar.low) <= ce
    return float(bar.high) >= ce


def _full_fill_on_bar(fvg: FVGZone, bar: Bar) -> bool:
    if fvg.direction == StructureDirection.BULLISH.value:
        return float(bar.low) <= float(fvg.low)
    return float(bar.high) >= float(fvg.high)


def _first_touch_price(fvg: FVGZone, bar: Bar) -> float:
    """Recorded contact price clipped into the zone."""
    if fvg.direction == StructureDirection.BULLISH.value:
        # First contact from above: clip wick to [low, high].
        return min(float(fvg.high), max(float(bar.low), float(fvg.low)))
    return max(float(fvg.low), min(float(bar.high), float(fvg.high)))


def _candidate_price(mode: str, fvg: FVGZone, bar: Optional[Bar]) -> Optional[float]:
    if mode == EntryMode.BOUNDARY.value:
        return boundary_price(fvg)
    if mode == EntryMode.CE.value:
        return ce_price(fvg)
    if mode == EntryMode.FIRST_TOUCH.value:
        if bar is None:
            return None
        return _first_touch_price(fvg, bar)
    return None


def _mode_triggered(mode: str, fvg: FVGZone, bar: Bar) -> bool:
    if mode == EntryMode.FIRST_TOUCH.value:
        return _touches_zone(fvg, bar)
    if mode == EntryMode.BOUNDARY.value:
        return _touches_boundary(fvg, bar)
    if mode == EntryMode.CE.value:
        return _touches_ce(fvg, bar)
    return False


def _waiting_price(mode: str, fvg: FVGZone) -> Optional[float]:
    """Theoretical entry price while waiting (known for boundary/CE)."""
    if mode == EntryMode.BOUNDARY.value:
        return boundary_price(fvg)
    if mode == EntryMode.CE.value:
        return ce_price(fvg)
    # first_touch price unknown until contact
    return None


def evaluate_entry(
    fvg: FVGZone,
    bars: Sequence[Bar],
    config: Optional[EntryConfig] = None,
) -> EntryCandidate:
    """
    Evaluate one entry mode against bars after FVG creation.

    Does not place orders. Fail closed on invalid ordering.
    """
    cfg = config or EntryConfig()
    mode = str(cfg.mode)
    setup_ref = dict(fvg.setup_reference or {})
    fvg_ref = _fvg_ref(fvg)

    def _invalid(reason: str) -> EntryCandidate:
        return EntryCandidate(
            mode=mode,
            direction=fvg.direction,
            price=_waiting_price(mode, fvg),
            triggered=False,
            trigger_timestamp=None,
            trigger_bar_index=None,
            fvg_reference=fvg_ref,
            setup_reference=setup_ref,
            entry_depth=None,
            max_retrace_depth=None,
            bars_after_fvg=None,
            status=EntryStatus.INVALID.value,
            extras={"reason": reason, "config": cfg.to_dict()},
        )

    if mode not in ENTRY_MODES:
        return _invalid(f"unsupported_mode:{mode}")

    order_err = _validate_ordering(fvg)
    if order_err:
        return _invalid(order_err)

    created = int(fvg.created_timestamp)
    # Exclude FVG creation candles (and anything at/before created).
    creation_times = {
        int(fvg.candle1_timestamp),
        int(fvg.candle2_timestamp),
        int(fvg.candle3_timestamp),
        created,
    }
    sorted_bars = sorted(bars, key=lambda b: b.time)
    post = [
        (idx, b)
        for idx, b in enumerate(sorted_bars)
        if int(b.time) > created and int(b.time) not in creation_times
    ]

    if cfg.max_bars_after_fvg is not None:
        post = post[: int(cfg.max_bars_after_fvg)]

    filled_before_entry = False
    first_fill_ts: Optional[int] = None
    for _, bar in post:
        if _full_fill_on_bar(fvg, bar):
            filled_before_entry = True
            first_fill_ts = int(bar.time)
            break

    for offset, (bar_index, bar) in enumerate(post):
        if not _mode_triggered(mode, fvg, bar):
            continue

        # Full fill before this bar (strictly earlier) recorded in metadata.
        prior_fill = False
        prior_fill_ts: Optional[int] = None
        for _, earlier in post[:offset]:
            if _full_fill_on_bar(fvg, earlier):
                prior_fill = True
                prior_fill_ts = int(earlier.time)
                break
        fill_on_trigger = _full_fill_on_bar(fvg, bar)
        price = _candidate_price(mode, fvg, bar)
        through = [b for _, b in post[: offset + 1]]
        e_depth = None if price is None else entry_depth_at_price(fvg, price)
        m_depth = max_retrace_depth_through(fvg, through)

        if not cfg.allow_full_fill and (prior_fill or fill_on_trigger):
            # Still record trigger info but mark invalid per config.
            return EntryCandidate(
                mode=mode,
                direction=fvg.direction,
                price=price,
                triggered=True,
                trigger_timestamp=int(bar.time),
                trigger_bar_index=bar_index,
                fvg_reference=fvg_ref,
                setup_reference=setup_ref,
                entry_depth=e_depth,
                max_retrace_depth=m_depth,
                bars_after_fvg=offset + 1,
                status=EntryStatus.INVALID.value,
                extras={
                    "reason": "full_fill_not_allowed",
                    "fully_filled_before_or_at_entry": True,
                    "first_full_fill_timestamp": prior_fill_ts or int(bar.time),
                    "config": cfg.to_dict(),
                },
            )

        return EntryCandidate(
            mode=mode,
            direction=fvg.direction,
            price=price,
            triggered=True,
            trigger_timestamp=int(bar.time),
            trigger_bar_index=bar_index,
            fvg_reference=fvg_ref,
            setup_reference=setup_ref,
            entry_depth=e_depth,
            max_retrace_depth=m_depth,
            bars_after_fvg=offset + 1,
            status=EntryStatus.TRIGGERED.value,
            extras={
                "fully_filled_before_or_at_entry": bool(prior_fill or fill_on_trigger),
                "first_full_fill_timestamp": prior_fill_ts
                if prior_fill
                else (int(bar.time) if fill_on_trigger else None),
                "fill_on_trigger_bar": fill_on_trigger,
                "config": cfg.to_dict(),
            },
        )

    # No trigger in available / windowed bars.
    status = EntryStatus.WAITING.value
    if cfg.max_bars_after_fvg is not None and len(post) >= int(cfg.max_bars_after_fvg):
        status = EntryStatus.MISSED.value

    waiting_price = _waiting_price(mode, fvg)
    return EntryCandidate(
        mode=mode,
        direction=fvg.direction,
        price=waiting_price,
        triggered=False,
        trigger_timestamp=None,
        trigger_bar_index=None,
        fvg_reference=fvg_ref,
        setup_reference=setup_ref,
        entry_depth=None
        if waiting_price is None
        else entry_depth_at_price(fvg, waiting_price),
        max_retrace_depth=None,
        bars_after_fvg=None,
        status=status,
        extras={
            "fully_filled_before_or_at_entry": filled_before_entry,
            "first_full_fill_timestamp": first_fill_ts,
            "bars_scanned_after_fvg": len(post),
            "config": cfg.to_dict(),
        },
    )


def evaluate_entry_modes(
    fvg: FVGZone,
    bars: Sequence[Bar],
    modes: Sequence[str] = ENTRY_MODES,
    *,
    allow_full_fill: bool = True,
    max_bars_after_fvg: Optional[int] = None,
) -> dict[str, EntryCandidate]:
    """Evaluate the same FVG under multiple entry modes (for later comparison)."""
    out: dict[str, EntryCandidate] = {}
    for mode in modes:
        out[str(mode)] = evaluate_entry(
            fvg,
            bars,
            EntryConfig(
                mode=str(mode),
                allow_full_fill=allow_full_fill,
                max_bars_after_fvg=max_bars_after_fvg,
            ),
        )
    return out
