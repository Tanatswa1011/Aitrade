"""Compare overlapping OHLC / sessions / HTF bias across providers or series."""

from __future__ import annotations

from typing import Any, Optional, Sequence

from bias_provider import StructureBiasProvider
from liquidity_sweep import detect_sweeps
from models import Bar, PRIMARY_SESSIONS
from ohlc_sessions import compute_session_ranges as compute_ohlc_session_ranges


def _by_time(bars: Sequence[Bar]) -> dict[int, Bar]:
    return {int(b.time): b for b in bars}


def compare_ohlc_overlap(
    left: Sequence[Bar],
    right: Sequence[Bar],
    *,
    left_label: str = "left",
    right_label: str = "right",
    price_tolerance: float = 0.0,
) -> dict[str, Any]:
    """Compare OHLC on shared timestamps."""
    a = _by_time(left)
    b = _by_time(right)
    shared = sorted(set(a) & set(b))
    only_a = sorted(set(a) - set(b))
    only_b = sorted(set(b) - set(a))
    exact_ts = len(shared)
    exact_ohlc = 0
    max_deltas = {"open": 0.0, "high": 0.0, "low": 0.0, "close": 0.0}
    within_tol = 0
    for t in shared:
        x, y = a[t], b[t]
        d_o = abs(float(x.open) - float(y.open))
        d_h = abs(float(x.high) - float(y.high))
        d_l = abs(float(x.low) - float(y.low))
        d_c = abs(float(x.close) - float(y.close))
        max_deltas["open"] = max(max_deltas["open"], d_o)
        max_deltas["high"] = max(max_deltas["high"], d_h)
        max_deltas["low"] = max(max_deltas["low"], d_l)
        max_deltas["close"] = max(max_deltas["close"], d_c)
        if d_o == 0 and d_h == 0 and d_l == 0 and d_c == 0:
            exact_ohlc += 1
        if (
            d_o <= price_tolerance
            and d_h <= price_tolerance
            and d_l <= price_tolerance
            and d_c <= price_tolerance
        ):
            within_tol += 1
    return {
        "left": left_label,
        "right": right_label,
        "bar_count_compared": exact_ts,
        "exact_timestamp_matches": exact_ts,
        "ohlc_exact_matches": exact_ohlc,
        "ohlc_within_tolerance": within_tol,
        "price_tolerance": price_tolerance,
        "max_open_delta": max_deltas["open"],
        "max_high_delta": max_deltas["high"],
        "max_low_delta": max_deltas["low"],
        "max_close_delta": max_deltas["close"],
        "missing_on_left": len(only_b),
        "missing_on_right": len(only_a),
        "missing_left_timestamps_head": only_b[:20],
        "missing_right_timestamps_head": only_a[:20],
    }


def compare_session_ranges_overlap(
    left_bars: Sequence[Bar],
    right_bars: Sequence[Bar],
    *,
    left_label: str = "left",
    right_label: str = "right",
    session_names: Sequence[str] = PRIMARY_SESSIONS,
    price_tolerance: float = 0.0,
    resolution_minutes: int = 5,
) -> dict[str, Any]:
    """Compare Asia/London high/low on overlapping trading dates."""
    left_ranges = compute_ohlc_session_ranges(
        left_bars, names=list(session_names), resolution_minutes=resolution_minutes
    )
    right_ranges = compute_ohlc_session_ranges(
        right_bars, names=list(session_names), resolution_minutes=resolution_minutes
    )

    def _date_key(sr) -> tuple:
        extras = sr.extras or {}
        rw = extras.get("resolved_window") or {}
        return (sr.name, str(rw.get("trading_date") or sr.start))

    left_by_date = {_date_key(s): s for s in left_ranges}
    right_by_date = {_date_key(s): s for s in right_ranges}
    shared_dates = sorted(set(left_by_date) & set(right_by_date))

    hl_exact = 0
    hl_within = 0
    rows = []
    for k in shared_dates:
        a, b = left_by_date[k], right_by_date[k]
        d_h = abs(float(a.high) - float(b.high))
        d_l = abs(float(a.low) - float(b.low))
        exact = d_h == 0 and d_l == 0
        within = d_h <= price_tolerance and d_l <= price_tolerance
        if exact:
            hl_exact += 1
        if within:
            hl_within += 1
        rows.append(
            {
                "session": k[0],
                "trading_date": k[1],
                "left_high": a.high,
                "right_high": b.high,
                "left_low": a.low,
                "right_low": b.low,
                "high_delta": d_h,
                "low_delta": d_l,
                "exact": exact,
            }
        )

    sweep_match = 0
    sweep_compared = 0
    sweep_rows = []
    for k in shared_dates:
        a, b = left_by_date[k], right_by_date[k]
        sa = detect_sweeps(a, left_bars)
        sb = detect_sweeps(b, right_bars)
        sides_a = {(s.side, round(float(s.level), 2)) for s in sa}
        sides_b = {(s.side, round(float(s.level), 2)) for s in sb}
        if sides_a or sides_b:
            sweep_compared += 1
            if sides_a == sides_b:
                sweep_match += 1
            sweep_rows.append(
                {
                    "session": k[0],
                    "trading_date": k[1],
                    "left_sweeps": [
                        {
                            "side": s.side,
                            "level": s.level,
                            "ts": s.sweep_timestamp,
                        }
                        for s in sa
                    ],
                    "right_sweeps": [
                        {
                            "side": s.side,
                            "level": s.level,
                            "ts": s.sweep_timestamp,
                        }
                        for s in sb
                    ],
                    "sides_equal": sides_a == sides_b,
                }
            )

    n = len(shared_dates)
    return {
        "left": left_label,
        "right": right_label,
        "sessions_compared": n,
        "session_hl_exact_matches": hl_exact,
        "session_hl_within_tolerance": hl_within,
        "session_hl_exact_match_rate": (hl_exact / n) if n else None,
        # Primary strategy-level rate: within configured price tolerance.
        "session_hl_match_rate": (hl_within / n) if n else None,
        "price_tolerance": price_tolerance,
        "sweep_events_compared": sweep_compared,
        "sweep_events_exact_side_level": sweep_match,
        "sweep_match_rate": (sweep_match / sweep_compared) if sweep_compared else None,
        "rows_head": rows[:40],
        "sweep_rows_head": sweep_rows[:40],
    }


def compare_htf_bias_overlap(
    left_daily: Sequence[Bar],
    left_h4: Sequence[Bar],
    right_daily: Sequence[Bar],
    right_h4: Sequence[Bar],
    *,
    left_label: str = "left",
    right_label: str = "right",
    sample_timestamps: Optional[Sequence[int]] = None,
) -> dict[str, Any]:
    """Compare Daily/4H structure bias at overlapping evaluation timestamps."""
    provider = StructureBiasProvider()
    if sample_timestamps is None:
        ld = sorted(int(b.time) for b in left_daily)
        rd = sorted(int(b.time) for b in right_daily)
        shared_d = sorted(set(ld) & set(rd))
        sample_timestamps = [t + 6 * 3600 for t in shared_d[1:]]

    daily_agree = 0
    h4_agree = 0
    compared = 0
    changed = []
    for ts in sample_timestamps:
        try:
            L = provider.get_context(
                as_of_ts=int(ts),
                daily_bars=left_daily,
                h4_bars=left_h4,
            )
            R = provider.get_context(
                as_of_ts=int(ts),
                daily_bars=right_daily,
                h4_bars=right_h4,
            )
        except Exception as exc:  # noqa: BLE001
            changed.append({"ts": ts, "error": str(exc)})
            continue
        compared += 1
        ld = (L.daily_bias.direction if L.daily_bias else None) or "unknown"
        rd = (R.daily_bias.direction if R.daily_bias else None) or "unknown"
        lh = (L.h4_bias.direction if L.h4_bias else None) or "unknown"
        rh = (R.h4_bias.direction if R.h4_bias else None) or "unknown"
        if ld == rd:
            daily_agree += 1
        if lh == rh:
            h4_agree += 1
        if ld != rd or lh != rh:
            changed.append(
                {
                    "ts": int(ts),
                    "left_daily": ld,
                    "right_daily": rd,
                    "left_h4": lh,
                    "right_h4": rh,
                }
            )

    return {
        "left": left_label,
        "right": right_label,
        "timestamps_compared": compared,
        "daily_bias_agree": daily_agree,
        "h4_bias_agree": h4_agree,
        "daily_agree_rate": (daily_agree / compared) if compared else None,
        "h4_agree_rate": (h4_agree / compared) if compared else None,
        "disagreements_head": changed[:30],
        "note": (
            "If data-source differences change HTF bias, disagreements are listed; "
            "strategy rules are not altered."
        ),
    }
