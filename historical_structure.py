"""Internal historical CHoCH-equivalent structure detector (OHLC only)."""

from __future__ import annotations

from typing import Optional, Sequence

from historical_structure_config import (
    DEFAULT_HISTORICAL_STRUCTURE_CONFIG,
    HistoricalStructureConfig,
)
from models import Bar, StructureConfirmation, TimingConfidence


def _is_swing_high(bars: Sequence[Bar], i: int, left: int, right: int) -> bool:
    h = bars[i].high
    for j in range(i - left, i):
        if bars[j].high >= h:
            return False
    for j in range(i + 1, i + right + 1):
        if bars[j].high > h:
            return False
    return True


def _is_swing_low(bars: Sequence[Bar], i: int, left: int, right: int) -> bool:
    lo = bars[i].low
    for j in range(i - left, i):
        if bars[j].low <= lo:
            return False
    for j in range(i + 1, i + right + 1):
        if bars[j].low < lo:
            return False
    return True


def detect_internal_choch(
    bars: Sequence[Bar],
    config: Optional[HistoricalStructureConfig] = None,
) -> list[StructureConfirmation]:
    """
    Deterministic swing → bias → close-break CHoCH approximation.

    Emits canonical StructureConfirmation with source=internal_structure.
    """
    cfg = config or DEFAULT_HISTORICAL_STRUCTURE_CONFIG
    left = max(1, int(cfg.swing_left))
    right = max(1, int(cfg.swing_right))
    ordered = sorted(bars, key=lambda b: int(b.time))
    n = len(ordered)
    if n < left + right + 1:
        return []

    swing_highs: list[tuple[int, float]] = []  # (index, price)
    swing_lows: list[tuple[int, float]] = []

    # Confirm swings only once the right-side bars exist.
    for i in range(left, n - right):
        if _is_swing_high(ordered, i, left, right):
            swing_highs.append((i, float(ordered[i].high)))
        if _is_swing_low(ordered, i, left, right):
            swing_lows.append((i, float(ordered[i].low)))

    events: list[StructureConfirmation] = []
    bias: Optional[str] = None
    last_sh: Optional[tuple[int, float]] = None
    last_sl: Optional[tuple[int, float]] = None
    sh_i = 0
    sl_i = 0
    min_break = float(cfg.minimum_break)
    use_close = bool(cfg.require_close_break) or cfg.break_mode == "close"

    for i in range(n):
        while sh_i < len(swing_highs) and swing_highs[sh_i][0] + right <= i:
            last_sh = swing_highs[sh_i]
            sh_i += 1
        while sl_i < len(swing_lows) and swing_lows[sl_i][0] + right <= i:
            last_sl = swing_lows[sl_i]
            sl_i += 1

        bar = ordered[i]
        # Bullish CHoCH: break above last swing high while not already bullish.
        if last_sh is not None and bias != "bullish":
            level = last_sh[1]
            broke = (
                bar.close > level + min_break
                if use_close
                else bar.high > level + min_break
            )
            # Only count if break bar is strictly after swing confirmation.
            if broke and i > last_sh[0] + right:
                events.append(
                    StructureConfirmation(
                        kind="CHoCH",
                        direction="bullish",
                        level=level,
                        event_timestamp=int(bar.time),
                        event_bar_index=i,
                        source="internal_structure",
                        study_id=None,
                        raw_id=f"internal_bull_{bar.time}",
                        timing_confidence=TimingConfidence.EXACT.value,
                        extras={
                            "algorithm_version": cfg.algorithm_version,
                            "equivalence_status": cfg.equivalence_status,
                            "swing_index": last_sh[0],
                            "parameters": cfg.to_dict(),
                        },
                    )
                )
                bias = "bullish"

        # Bearish CHoCH: break below last swing low while not already bearish.
        if last_sl is not None and bias != "bearish":
            level = last_sl[1]
            broke = (
                bar.close < level - min_break
                if use_close
                else bar.low < level - min_break
            )
            if broke and i > last_sl[0] + right:
                events.append(
                    StructureConfirmation(
                        kind="CHoCH",
                        direction="bearish",
                        level=level,
                        event_timestamp=int(bar.time),
                        event_bar_index=i,
                        source="internal_structure",
                        study_id=None,
                        raw_id=f"internal_bear_{bar.time}",
                        timing_confidence=TimingConfidence.EXACT.value,
                        extras={
                            "algorithm_version": cfg.algorithm_version,
                            "equivalence_status": cfg.equivalence_status,
                            "swing_index": last_sl[0],
                            "parameters": cfg.to_dict(),
                        },
                    )
                )
                bias = "bearish"

    return events
