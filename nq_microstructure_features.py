"""Leak-safe microstructure features for Phase 34 (research-only).

Predictive features use only events with timestamp <= sweep-bar close.
Nothing in the outcome window may enter a classification feature.
"""

from __future__ import annotations

import math
import statistics
from typing import Any, Iterable, Optional, Sequence

from nq_microstructure_models import SweepEvent

FEATURE_LOOKBACK_SEC = 60
# Sweep is detected on a 1m OHLC bar; the event is known at bar close.
FEATURE_CUTOFF_OFFSET_SEC = 60


def unix_ts(raw: Any) -> int:
    ts = int(raw or 0)
    if ts > 10_000_000_000_000:
        return ts // 1_000_000_000
    if ts > 10_000_000_000:
        return ts // 1_000_000
    return ts


def px_to_float(raw: Any) -> Optional[float]:
    if raw is None:
        return None
    v = float(raw)
    if math.isnan(v) or math.isinf(v):
        return None
    return v / 1e9 if abs(v) > 1e6 else v


def rec_attr(rec: Any, name: str, default: Any = None) -> Any:
    if isinstance(rec, dict):
        return rec.get(name, default)
    return getattr(rec, name, default)


def depth(rec: Any, nlev: int, side: str) -> float:
    total = 0.0
    for i in range(nlev):
        sz = rec_attr(rec, f"{side}_sz_{i:02d}")
        if sz is None:
            continue
        total += float(sz)
    return total


def slope_lots_per_point(rec: Any, side: str) -> Optional[float]:
    px0 = px_to_float(rec_attr(rec, f"pretty_{side}_px_00") or rec_attr(rec, f"{side}_px_00"))
    px9 = px_to_float(rec_attr(rec, f"pretty_{side}_px_09") or rec_attr(rec, f"{side}_px_09"))
    if px0 is None or px9 is None:
        return None
    dist = abs(px9 - px0)
    if dist < 1e-9:
        return None
    return depth(rec, 10, side) / dist


def book_imbalance(rec: Any, nlev: int) -> Optional[float]:
    if rec is None:
        return None
    b = depth(rec, nlev, "bid")
    a = depth(rec, nlev, "ask")
    s = b + a
    if s <= 0:
        return None
    return b / s


def _ch(val: Any) -> str:
    if val is None:
        return ""
    if hasattr(val, "value"):
        val = val.value
    if isinstance(val, (bytes, bytearray)):
        val = val.decode("ascii", "ignore")
    if isinstance(val, int):
        return chr(val) if 0 <= val < 256 else ""
    s = str(val)
    if "." in s:
        s = s.split(".")[-1]
    return s[:1].upper()


def is_trade(rec: Any) -> bool:
    action = _ch(rec_attr(rec, "action"))
    if action == "T":
        return True
    if rec_attr(rec, "bid_sz_00") is not None:
        return False
    price = rec_attr(rec, "price")
    size = rec_attr(rec, "size")
    return price is not None and size is not None


def aggressor_side(rec: Any) -> str:
    return _ch(rec_attr(rec, "side"))


def trade_px_sz(rec: Any) -> tuple[Optional[float], float]:
    raw_px = rec_attr(rec, "pretty_price")
    if raw_px is None:
        raw_px = rec_attr(rec, "price")
    return px_to_float(raw_px), float(rec_attr(rec, "size", 0) or 0)


def merge_spans(spans: Sequence[tuple[int, int]]) -> list[tuple[int, int]]:
    ordered = sorted((int(a), int(b)) for a, b in spans if b > a)
    out: list[list[int]] = []
    for a, b in ordered:
        if not out or a > out[-1][1]:
            out.append([a, b])
        else:
            out[-1][1] = max(out[-1][1], b)
    return [(a, b) for a, b in out]


def features_from_records(
    records: Iterable[Any],
    event: SweepEvent,
    *,
    lookback_sec: int = FEATURE_LOOKBACK_SEC,
    cutoff_offset_sec: int = FEATURE_CUTOFF_OFFSET_SEC,
) -> dict[str, Any]:
    """Features from MBP-10 / trades with a hard cutoff at sweep-bar close."""
    t_open = int(event.sweep_bar_time)
    t_pre = t_open - int(lookback_sec)
    t_cut = t_open + int(cutoff_offset_sec)
    book_pre = None
    book_t = None
    trades_buy = 0.0
    trades_sell = 0.0
    n_trades = 0
    px_min = None
    px_max = None
    n_book = 0
    n_future_ignored = 0
    first_through = None
    level = float(event.level)

    for rec in records:
        ts = unix_ts(rec_attr(rec, "ts_event") or rec_attr(rec, "ts"))
        if ts <= 0:
            continue
        if ts > t_cut:
            n_future_ignored += 1
            continue
        if ts < t_pre:
            continue
        if rec_attr(rec, "bid_sz_00") is not None:
            n_book += 1
            if ts <= t_pre + 1:
                book_pre = rec
            book_t = rec
        if is_trade(rec):
            px, sz = trade_px_sz(rec)
            if px is None or sz <= 0:
                continue
            n_trades += 1
            px_min = px if px_min is None else min(px_min, px)
            px_max = px if px_max is None else max(px_max, px)
            side = aggressor_side(rec)
            if side == "A":
                trades_sell += sz
            elif side == "B":
                trades_buy += sz
            if event.side == "pdl_sweep" and px < level and first_through is None:
                first_through = ts
            if event.side == "pdh_sweep" and px > level and first_through is None:
                first_through = ts

    disp = None if px_min is None or px_max is None else (px_max - px_min)
    agg = trades_buy + trades_sell
    absorption = None if not agg or not disp or disp <= 0 else agg / disp
    impact = None if not agg or disp is None else disp / agg
    ofi_proxy = None
    if book_pre is not None and book_t is not None:
        ofi_proxy = (depth(book_t, 1, "bid") - depth(book_pre, 1, "bid")) - (
            depth(book_t, 1, "ask") - depth(book_pre, 1, "ask")
        )
    bid1 = depth(book_t, 1, "bid") if book_t is not None else None
    ask1 = depth(book_t, 1, "ask") if book_t is not None else None
    displayed = None
    aggressive_at_level = trades_sell if event.side == "pdl_sweep" else trades_buy
    if event.side == "pdl_sweep" and bid1 is not None:
        displayed = bid1
    if event.side == "pdh_sweep" and ask1 is not None:
        displayed = ask1
    exec_to_disp = None if not displayed or displayed <= 0 else aggressive_at_level / displayed
    bid_slope = None if book_t is None else slope_lots_per_point(book_t, "bid")
    ask_slope = None if book_t is None else slope_lots_per_point(book_t, "ask")
    slope_ratio = None
    if bid_slope and ask_slope and ask_slope > 0:
        slope_ratio = bid_slope / ask_slope
    signed_flow = trades_buy - trades_sell
    signed_flow_for_reversal = signed_flow if event.side == "pdl_sweep" else -signed_flow
    ofi_for_reversal = None if ofi_proxy is None else (
        ofi_proxy if event.side == "pdl_sweep" else -ofi_proxy
    )
    imb1 = book_imbalance(book_t, 1)
    imb3 = book_imbalance(book_t, 3)
    imb5 = book_imbalance(book_t, 5)
    imb10 = book_imbalance(book_t, 10)

    def _rev_imb(imb: Optional[float]) -> Optional[float]:
        if imb is None:
            return None
        return imb if event.side == "pdl_sweep" else 1.0 - imb

    bid_pre = depth(book_pre, 1, "bid") if book_pre is not None else None
    ask_pre = depth(book_pre, 1, "ask") if book_pre is not None else None
    swept_pre = bid_pre if event.side == "pdl_sweep" else ask_pre
    swept_t = bid1 if event.side == "pdl_sweep" else ask1
    persistence = None
    if swept_pre is not None and swept_pre > 0 and swept_t is not None:
        persistence = swept_t / swept_pre
    withdrawal = None if persistence is None else max(0.0, 1.0 - persistence)
    slope_for_reversal = None
    if bid_slope is not None and ask_slope is not None and (bid_slope + ask_slope) > 0:
        # PDL reversal wants thick bids (high bid slope vs ask).
        slope_for_reversal = bid_slope / (bid_slope + ask_slope)
        if event.side == "pdh_sweep":
            slope_for_reversal = ask_slope / (bid_slope + ask_slope)
    return {
        "has_book": book_t is not None,
        "feature_cutoff_ts": t_cut,
        "n_book_updates_le_cutoff": n_book,
        "n_future_records_ignored": n_future_ignored,
        "trade_imbalance": signed_flow,
        "signed_flow_for_reversal": signed_flow_for_reversal,
        "aggressive_buy": trades_buy,
        "aggressive_sell": trades_sell,
        "n_trades_le_cutoff": n_trades,
        "book_imbalance_top1": imb1,
        "book_imbalance_top3": imb3,
        "book_imbalance_top5": imb5,
        "book_imbalance_top10": imb10,
        "imb_for_reversal_top1": _rev_imb(imb1),
        "imb_for_reversal_top3": _rev_imb(imb3),
        "imb_for_reversal_top5": _rev_imb(imb5),
        "imb_for_reversal_top10": _rev_imb(imb10),
        "ofi_top1_proxy": ofi_proxy,
        "ofi_for_reversal": ofi_for_reversal,
        "absorption_proxy": absorption,
        "price_impact_per_lot": impact,
        "price_range_feature_window": disp,
        "executed_to_displayed": exec_to_disp,
        "bid_slope_lots_per_pt": bid_slope,
        "ask_slope_lots_per_pt": ask_slope,
        "bid_ask_slope_ratio": slope_ratio,
        "slope_for_reversal": slope_for_reversal,
        "persistence_top1_swept_side": persistence,
        "withdrawal_proxy": withdrawal,
        "first_through_ts": first_through,
    }


def spearman_rho(pairs: Sequence[tuple[float, float]]) -> Optional[float]:
    if len(pairs) < 5:
        return None

    def _rank(vals: Sequence[float]) -> list[float]:
        indexed = sorted(enumerate(vals), key=lambda kv: kv[1])
        ranks = [0.0] * len(vals)
        i = 0
        while i < len(indexed):
            j = i
            while j + 1 < len(indexed) and indexed[j + 1][1] == indexed[i][1]:
                j += 1
            avg = (i + j) / 2.0 + 1.0
            for k in range(i, j + 1):
                ranks[indexed[k][0]] = avg
            i = j + 1
        return ranks

    rx = _rank([p[0] for p in pairs])
    ry = _rank([p[1] for p in pairs])
    mx = statistics.mean(rx)
    my = statistics.mean(ry)
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    denx = math.sqrt(sum((a - mx) ** 2 for a in rx))
    deny = math.sqrt(sum((b - my) ** 2 for b in ry))
    if denx <= 0 or deny <= 0:
        return None
    return num / (denx * deny)


def median_split(pairs: Sequence[tuple[float, bool]]) -> dict[str, Any]:
    if len(pairs) < 6:
        return {"n": len(pairs), "ok": False}
    xs = sorted(x for x, _ in pairs)
    med = statistics.median(xs)
    lo = [int(y) for x, y in pairs if x <= med]
    hi = [int(y) for x, y in pairs if x > med]
    p_lo = None if not lo else sum(lo) / len(lo)
    p_hi = None if not hi else sum(hi) / len(hi)
    lift = None if p_lo is None or p_hi is None else p_hi - p_lo
    return {
        "ok": True,
        "n": len(pairs),
        "median": med,
        "n_lo": len(lo),
        "n_hi": len(hi),
        "p_reversal_lo": p_lo,
        "p_reversal_hi": p_hi,
        "lift_hi_minus_lo": lift,
    }


def quartile_rows(pairs: Sequence[tuple[float, bool]]) -> list[dict[str, Any]]:
    if len(pairs) < 8:
        return []
    ordered = sorted(pairs, key=lambda x: x[0])
    n = len(ordered)
    rows = []
    for q in range(4):
        a = int(q * n / 4)
        b = int((q + 1) * n / 4)
        chunk = ordered[a:b]
        if not chunk:
            continue
        ys = [int(y) for _, y in chunk]
        rows.append(
            {
                "quartile": q + 1,
                "n": len(chunk),
                "mean_feature": statistics.mean(x for x, _ in chunk),
                "p_reversal": sum(ys) / len(ys),
            }
        )
    return rows


def neighboring_threshold_stable(
    pairs: Sequence[tuple[float, bool]],
    min_abs_lift: float = 0.08,
    percentiles: Sequence[float] = (0.5, 0.6, 0.7),
) -> dict[str, Any]:
    """True if listed percentile splits share a lift sign and |median lift| >= min."""
    if len(pairs) < 10:
        return {"ok": False, "stable": False, "reason": "n<10"}
    xs = sorted(x for x, _ in pairs)

    def _at(p: float) -> Optional[float]:
        thr = xs[min(len(xs) - 1, int(round((len(xs) - 1) * p)))]
        lo = [int(y) for x, y in pairs if x <= thr]
        hi = [int(y) for x, y in pairs if x > thr]
        if not lo or not hi:
            return None
        return (sum(hi) / len(hi)) - (sum(lo) / len(lo))

    lifts = {f"p{int(p * 100)}": _at(p) for p in percentiles}
    vals = [v for v in lifts.values() if v is not None]
    if len(vals) < len(percentiles):
        return {"ok": False, "stable": False, "lifts": lifts}
    signs = {1 if v > 0 else -1 if v < 0 else 0 for v in vals}
    p50 = lifts.get("p50")
    stable = len(signs) == 1 and 0 not in signs and abs(p50 or 0) >= min_abs_lift
    return {"ok": True, "stable": stable, "lifts": lifts, "sign": next(iter(signs))}


def wilson_ci(k: int, n: int, z: float = 1.96) -> tuple[Optional[float], Optional[float]]:
    if n <= 0:
        return None, None
    p = k / n
    den = 1.0 + z * z / n
    centre = (p + z * z / (2.0 * n)) / den
    half = z * math.sqrt((p * (1.0 - p) + z * z / (4.0 * n)) / n) / den
    return centre - half, centre + half


def quantile_rows(pairs: Sequence[tuple[float, bool]], q: int = 5) -> list[dict[str, Any]]:
    if len(pairs) < q * 2:
        return []
    ordered = sorted(pairs, key=lambda x: x[0])
    n = len(ordered)
    rows = []
    for i in range(q):
        a = int(i * n / q)
        b = int((i + 1) * n / q)
        chunk = ordered[a:b]
        if not chunk:
            continue
        ys = [int(y) for _, y in chunk]
        k = sum(ys)
        lo, hi = wilson_ci(k, len(ys))
        rows.append(
            {
                "bucket": i + 1,
                "n": len(chunk),
                "mean_feature": statistics.mean(x for x, _ in chunk),
                "min_feature": chunk[0][0],
                "max_feature": chunk[-1][0],
                "p_reversal": k / len(ys),
                "wilson_lo": lo,
                "wilson_hi": hi,
            }
        )
    return rows
