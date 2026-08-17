"""Chronological train/holdout split (no shuffle, no leakage)."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date
from typing import Any, Optional, Sequence


@dataclass(frozen=True)
class ChronoSplit:
    train_start: Optional[str]
    train_end: Optional[str]
    train_sessions: int
    holdout_start: Optional[str]
    holdout_end: Optional[str]
    holdout_sessions: int
    train_fraction: float
    method: str
    train_liquidity_event_ids: tuple[str, ...]
    holdout_liquidity_event_ids: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _trading_date_key(row: dict[str, Any]) -> str:
    td = row.get("trading_date")
    if td:
        return str(td)[:10]
    # fallback: derive from sweep timestamp date UTC
    ts = row.get("sweep_timestamp")
    if ts is None:
        return "unknown"
    from datetime import datetime, timezone

    return datetime.fromtimestamp(int(ts), tz=timezone.utc).date().isoformat()


def chronological_split(
    rows: Sequence[dict[str, Any]],
    *,
    train_fraction: float = 0.70,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], ChronoSplit]:
    """
    Split by unique trading_date chronological order.

    All rows for a trading_date stay on one side (no leakage across same day).
    Liquidity events are assigned by the trading_date of any of their rows.
    """
    if not 0.5 <= train_fraction <= 0.9:
        raise ValueError("train_fraction must be in [0.5, 0.9]")

    dates = sorted({_trading_date_key(r) for r in rows if _trading_date_key(r) != "unknown"})
    if len(dates) < 4:
        # too few dates — still split but flag
        cut = max(1, int(len(dates) * train_fraction))
    else:
        cut = max(1, min(len(dates) - 1, int(round(len(dates) * train_fraction))))
        # ensure holdout has at least ~25% if 70% would leave tiny holdout
        if len(dates) - cut < max(2, int(0.2 * len(dates))):
            cut = int(len(dates) * 0.70)

    train_dates = set(dates[:cut])
    holdout_dates = set(dates[cut:])

    train = [r for r in rows if _trading_date_key(r) in train_dates]
    holdout = [r for r in rows if _trading_date_key(r) in holdout_dates]

    train_liqs = tuple(
        sorted({str(r.get("liquidity_event_id")) for r in train if r.get("liquidity_event_id")})
    )
    hold_liqs = tuple(
        sorted({str(r.get("liquidity_event_id")) for r in holdout if r.get("liquidity_event_id")})
    )
    # leakage check: no shared liquidity ids
    overlap = set(train_liqs) & set(hold_liqs)
    if overlap:
        # Assign contested events by earliest trading date of their rows
        contested = overlap
        train = [
            r
            for r in rows
            if str(r.get("liquidity_event_id")) not in contested
            and _trading_date_key(r) in train_dates
        ]
        holdout = [
            r
            for r in rows
            if str(r.get("liquidity_event_id")) not in contested
            and _trading_date_key(r) in holdout_dates
        ]
        for lid in contested:
            ev_rows = [r for r in rows if str(r.get("liquidity_event_id")) == lid]
            d0 = min(_trading_date_key(r) for r in ev_rows)
            if d0 in train_dates:
                train.extend(ev_rows)
            else:
                holdout.extend(ev_rows)
        train_liqs = tuple(
            sorted({str(r.get("liquidity_event_id")) for r in train if r.get("liquidity_event_id")})
        )
        hold_liqs = tuple(
            sorted({str(r.get("liquidity_event_id")) for r in holdout if r.get("liquidity_event_id")})
        )

    # session counts = unique (session, trading_date) complete-ish rows
    def _sessions(rs: list[dict[str, Any]]) -> int:
        return len({(r.get("session"), _trading_date_key(r)) for r in rs})

    split = ChronoSplit(
        train_start=dates[0] if dates else None,
        train_end=dates[cut - 1] if dates and cut else None,
        train_sessions=_sessions(train),
        holdout_start=dates[cut] if cut < len(dates) else None,
        holdout_end=dates[-1] if dates and cut < len(dates) else None,
        holdout_sessions=_sessions(holdout),
        train_fraction=train_fraction,
        method="chronological_trading_date_70_30",
        train_liquidity_event_ids=train_liqs,
        holdout_liquidity_event_ids=hold_liqs,
    )
    return train, holdout, split


def assert_no_split_leakage(split: ChronoSplit) -> None:
    ov = set(split.train_liquidity_event_ids) & set(split.holdout_liquidity_event_ids)
    if ov:
        raise AssertionError(f"train/holdout liquidity_event_id overlap: {len(ov)}")
