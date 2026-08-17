"""Resolve OHLC same-bar ambiguities using lower-timeframe constituent bars."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional, Sequence

from models import Bar


# Resolution outcomes for 15m → 5m (and similar hierarchical) resolution.
ENTRY_THEN_STOP = "ENTRY_THEN_STOP"
STOP_BEFORE_ENTRY = "STOP_BEFORE_ENTRY"
TARGET_THEN_STOP = "TARGET_THEN_STOP"
STOP_THEN_TARGET = "STOP_THEN_TARGET"
RESOLVED_NO_STOP = "RESOLVED_NO_STOP"
STILL_AMBIGUOUS = "STILL_AMBIGUOUS"
INSUFFICIENT_DATA = "INSUFFICIENT_DATA"


@dataclass(frozen=True)
class IntrabarResolution:
    result: str
    parent_timeframe: str
    child_timeframe: str
    parent_bar_time: int
    entry_price: float
    stop_price: float
    direction: str
    child_bars_used: int
    entry_first_ts: Optional[int] = None
    stop_first_ts: Optional[int] = None
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "result": self.result,
            "parent_timeframe": self.parent_timeframe,
            "child_timeframe": self.child_timeframe,
            "parent_bar_time": self.parent_bar_time,
            "entry_price": self.entry_price,
            "stop_price": self.stop_price,
            "direction": self.direction,
            "child_bars_used": self.child_bars_used,
            "entry_first_ts": self.entry_first_ts,
            "stop_first_ts": self.stop_first_ts,
            "note": self.note,
        }


class IntrabarResolver:
    """
    Hierarchical intrabar resolver.

    15m ambiguous → inspect chronological constituent 5m bars.
    Does NOT invent ordering inside a single 5m candle.
    Does NOT fetch or invent 1m data for 5m ambiguity.
    """

    def __init__(
        self,
        *,
        parent_period_sec: int = 900,
        child_period_sec: int = 300,
        parent_timeframe: str = "15m",
        child_timeframe: str = "5m",
    ) -> None:
        self.parent_period_sec = parent_period_sec
        self.child_period_sec = child_period_sec
        self.parent_timeframe = parent_timeframe
        self.child_timeframe = child_timeframe

    def constituent_bars(
        self,
        child_bars: Sequence[Bar],
        parent_bar_time: int,
    ) -> list[Bar]:
        start = int(parent_bar_time)
        end = start + self.parent_period_sec
        return sorted(
            [b for b in child_bars if start <= int(b.time) < end],
            key=lambda b: int(b.time),
        )

    @staticmethod
    def _hit_entry(direction: str, bar: Bar, entry: float) -> bool:
        if direction == "bullish":
            return float(bar.low) <= float(entry) <= float(bar.high)
        return float(bar.low) <= float(entry) <= float(bar.high)

    @staticmethod
    def _hit_stop(direction: str, bar: Bar, stop: float) -> bool:
        if direction == "bullish":
            return float(bar.low) <= float(stop)
        return float(bar.high) >= float(stop)

    @staticmethod
    def _hit_target(direction: str, bar: Bar, price: float) -> bool:
        if direction == "bullish":
            return float(bar.high) >= float(price)
        return float(bar.low) <= float(price)

    def resolve_entry_stop(
        self,
        *,
        direction: str,
        entry_price: float,
        stop_price: float,
        parent_bar_time: int,
        child_bars: Sequence[Bar],
    ) -> IntrabarResolution:
        kids = self.constituent_bars(child_bars, parent_bar_time)
        expected = self.parent_period_sec // self.child_period_sec
        if len(kids) < 1:
            return IntrabarResolution(
                result=INSUFFICIENT_DATA,
                parent_timeframe=self.parent_timeframe,
                child_timeframe=self.child_timeframe,
                parent_bar_time=int(parent_bar_time),
                entry_price=float(entry_price),
                stop_price=float(stop_price),
                direction=direction,
                child_bars_used=0,
                note=f"expected_up_to_{expected}_child_bars",
            )

        entry_ts: Optional[int] = None
        stop_ts: Optional[int] = None
        for b in kids:
            e_hit = self._hit_entry(direction, b, entry_price)
            s_hit = self._hit_stop(direction, b, stop_price)
            if e_hit and s_hit:
                # Same 5m candle — do not invent intrabar order.
                return IntrabarResolution(
                    result=STILL_AMBIGUOUS,
                    parent_timeframe=self.parent_timeframe,
                    child_timeframe=self.child_timeframe,
                    parent_bar_time=int(parent_bar_time),
                    entry_price=float(entry_price),
                    stop_price=float(stop_price),
                    direction=direction,
                    child_bars_used=len(kids),
                    entry_first_ts=int(b.time),
                    stop_first_ts=int(b.time),
                    note="entry_and_stop_inside_same_5m_candle;_no_1m_ordering",
                )
            if e_hit and entry_ts is None:
                entry_ts = int(b.time)
            if s_hit and stop_ts is None:
                stop_ts = int(b.time)
            # Early decisive ordering once both seen on different bars
            if entry_ts is not None and stop_ts is not None:
                break

        if entry_ts is not None and stop_ts is None:
            result = RESOLVED_NO_STOP
        elif entry_ts is not None and stop_ts is not None:
            if entry_ts < stop_ts:
                result = ENTRY_THEN_STOP
            elif stop_ts < entry_ts:
                result = STOP_BEFORE_ENTRY
            else:
                result = STILL_AMBIGUOUS
        elif entry_ts is None and stop_ts is not None:
            result = STOP_BEFORE_ENTRY
        else:
            result = INSUFFICIENT_DATA

        return IntrabarResolution(
            result=result,
            parent_timeframe=self.parent_timeframe,
            child_timeframe=self.child_timeframe,
            parent_bar_time=int(parent_bar_time),
            entry_price=float(entry_price),
            stop_price=float(stop_price),
            direction=direction,
            child_bars_used=len(kids),
            entry_first_ts=entry_ts,
            stop_first_ts=stop_ts,
        )

    def resolve_target_stop(
        self,
        *,
        direction: str,
        target_price: float,
        stop_price: float,
        parent_bar_time: int,
        child_bars: Sequence[Bar],
    ) -> IntrabarResolution:
        """Same hierarchical principle for target/stop same-bar ambiguity."""
        kids = self.constituent_bars(child_bars, parent_bar_time)
        if not kids:
            return IntrabarResolution(
                result=INSUFFICIENT_DATA,
                parent_timeframe=self.parent_timeframe,
                child_timeframe=self.child_timeframe,
                parent_bar_time=int(parent_bar_time),
                entry_price=float(target_price),
                stop_price=float(stop_price),
                direction=direction,
                child_bars_used=0,
                note="target_stop_resolution",
            )
        t_ts = s_ts = None
        for b in kids:
            t_hit = self._hit_target(direction, b, target_price)
            s_hit = self._hit_stop(direction, b, stop_price)
            if t_hit and s_hit:
                return IntrabarResolution(
                    result=STILL_AMBIGUOUS,
                    parent_timeframe=self.parent_timeframe,
                    child_timeframe=self.child_timeframe,
                    parent_bar_time=int(parent_bar_time),
                    entry_price=float(target_price),
                    stop_price=float(stop_price),
                    direction=direction,
                    child_bars_used=len(kids),
                    entry_first_ts=int(b.time),
                    stop_first_ts=int(b.time),
                    note="target_and_stop_inside_same_5m_candle",
                )
            if t_hit and t_ts is None:
                t_ts = int(b.time)
            if s_hit and s_ts is None:
                s_ts = int(b.time)
            if t_ts is not None and s_ts is not None:
                break
        if t_ts is not None and s_ts is None:
            result = RESOLVED_NO_STOP
        elif t_ts is not None and s_ts is not None:
            if t_ts < s_ts:
                result = TARGET_THEN_STOP
            elif s_ts < t_ts:
                result = STOP_THEN_TARGET
            else:
                result = STILL_AMBIGUOUS
        elif t_ts is None and s_ts is not None:
            result = STOP_THEN_TARGET
        else:
            result = INSUFFICIENT_DATA
        return IntrabarResolution(
            result=result,
            parent_timeframe=self.parent_timeframe,
            child_timeframe=self.child_timeframe,
            parent_bar_time=int(parent_bar_time),
            entry_price=float(target_price),
            stop_price=float(stop_price),
            direction=direction,
            child_bars_used=len(kids),
            entry_first_ts=t_ts,
            stop_first_ts=s_ts,
            note="target_stop_resolution",
        )


def resolver_5m_from_1m() -> IntrabarResolver:
    """5m ambiguity → chronological 1m evidence only (not a strategy TF)."""
    return IntrabarResolver(
        parent_period_sec=300,
        child_period_sec=60,
        parent_timeframe="5m",
        child_timeframe="1m",
    )


def resolve_15m_ambiguities_from_journal(
    records: Sequence[Any],
    bars_5m: Sequence[Bar],
) -> dict[str, Any]:
    """Apply IntrabarResolver to TRIGGER_BAR_STOP_AMBIGUITY / AMBIGUOUS on 15m rows."""
    resolver = IntrabarResolver()
    rows = []
    for r in records:
        tf = str(getattr(r, "execution_timeframe", None) or getattr(r, "timeframe", "") or "")
        if tf not in ("15m", "15"):
            continue
        direction = getattr(r, "direction", None) or ""
        for e in getattr(r, "entry_results", []) or []:
            flags = list(getattr(e, "ambiguity_flags", []) or [])
            rel = list(getattr(r, "reliability_flags", []) or [])
            amb = (
                "TRIGGER_BAR_STOP_AMBIGUITY" in flags
                or "TRIGGER_BAR_STOP_AMBIGUITY" in rel
                or getattr(e, "outcome", "") == "AMBIGUOUS_INTRABAR"
            )
            if not amb:
                continue
            if e.entry_price is None or e.stop_price is None or e.entry_timestamp is None:
                rows.append(
                    {
                        "setup_id": getattr(r, "setup_id", None),
                        "entry_mode": e.mode,
                        "result": INSUFFICIENT_DATA,
                    }
                )
                continue
            res = resolver.resolve_entry_stop(
                direction=direction,
                entry_price=float(e.entry_price),
                stop_price=float(e.stop_price),
                parent_bar_time=int(e.entry_timestamp),
                child_bars=bars_5m,
            )
            rows.append(
                {
                    "setup_id": getattr(r, "setup_id", None),
                    "liquidity_event_id": getattr(r, "liquidity_event_id", None),
                    "entry_mode": e.mode,
                    "execution_timeframe": tf,
                    **res.to_dict(),
                }
            )

    by_result: dict[str, int] = {}
    for row in rows:
        by_result[row["result"]] = by_result.get(row["result"], 0) + 1
    return {
        "resolved_count": len(rows),
        "by_result": by_result,
        "rows": rows,
        "five_m_ambiguity_note": (
            "Ambiguous 5m events require a future 1m dataset to resolve ordering; "
            "1m is not fetched or invented in Phase 15."
        ),
    }
