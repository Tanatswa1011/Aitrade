"""Phase 37 — leak-safe executed-trade flow features (no book / MBP / MBO).

Classification features use only trades with timestamp <= sweep-bar close.
Post-cutoff windows are diagnostic and must not enter Models 0-6.
"""
from __future__ import annotations

from typing import Any, Iterable, Optional, Sequence

from nq_microstructure_features import aggressor_side, px_to_float, rec_attr, unix_ts
from nq_microstructure_models import SweepEvent

TICK = 0.25
CUTOFF_OFFSET_SEC = 60
PRE_LOOKBACK_SEC = 60


def sign_trade(rec: Any) -> tuple[str, int]:
    """Map Databento trades.side to aggressor label.

    Official schema: Ask = sell aggressor, Bid = buy aggressor, None = unspecified.
    """
    s = aggressor_side(rec)
    if s in ("B",):
        return "AGGRESSIVE_BUY", 1
    if s in ("A",):
        return "AGGRESSIVE_SELL", -1
    return "UNSIGNED", 0


def parse_trade(rec: Any) -> Optional[tuple[int, float, float, str, int]]:
    ts = unix_ts(rec_attr(rec, "ts_event") or rec_attr(rec, "ts"))
    if ts <= 0:
        return None
    px, sz = px_to_float(rec_attr(rec, "pretty_price") or rec_attr(rec, "price")), float(rec_attr(rec, "size", 0) or 0)
    if px is None or sz <= 0:
        return None
    label, sgn = sign_trade(rec)
    return ts, px, sz, label, sgn


def _empty_agg() -> dict[str, float]:
    return {
        "abv": 0.0,
        "asv": 0.0,
        "unsigned_vol": 0.0,
        "n_trades": 0.0,
        "n_buy": 0.0,
        "n_sell": 0.0,
        "n_unsigned": 0.0,
        "max_size": 0.0,
        "sum_size_sq": 0.0,
        "px_min": 0.0,
        "px_max": 0.0,
        "has_px": 0.0,
        "first_px": 0.0,
        "last_px": 0.0,
    }


def _add(agg: dict[str, float], px: float, sz: float, label: str, sgn: int) -> None:
    agg["n_trades"] += 1
    agg["sum_size_sq"] += sz * sz
    agg["max_size"] = max(agg["max_size"], sz)
    if not agg["has_px"]:
        agg["px_min"] = agg["px_max"] = agg["first_px"] = px
        agg["has_px"] = 1.0
    else:
        agg["px_min"] = min(agg["px_min"], px)
        agg["px_max"] = max(agg["px_max"], px)
    agg["last_px"] = px
    if sgn > 0:
        agg["abv"] += sz
        agg["n_buy"] += 1
    elif sgn < 0:
        agg["asv"] += sz
        agg["n_sell"] += 1
    else:
        agg["unsigned_vol"] += sz
        agg["n_unsigned"] += 1


def _finalize(agg: dict[str, float], duration_sec: float) -> dict[str, Any]:
    abv, asv = agg["abv"], agg["asv"]
    signed = abv + asv
    vol = abv + asv + agg["unsigned_vol"]
    delta = abv - asv
    ndelta = 0.0 if signed <= 0 else delta / signed
    n = agg["n_trades"]
    dur = max(float(duration_sec), 1.0)
    herfindahl = 0.0 if vol <= 0 else (agg["sum_size_sq"] / (vol * vol)) * max(n, 1.0)
    return {
        "abv": abv,
        "asv": asv,
        "unsigned_vol": agg["unsigned_vol"],
        "volume": vol,
        "n_trades": int(n),
        "n_buy": int(agg["n_buy"]),
        "n_sell": int(agg["n_sell"]),
        "n_unsigned": int(agg["n_unsigned"]),
        "delta": delta,
        "normalized_delta": ndelta,
        "max_trade_size": agg["max_size"],
        "avg_trade_size": 0.0 if n <= 0 else vol / n,
        "trades_per_sec": n / dur,
        "contracts_per_sec": vol / dur,
        "burstiness_herfindahl": herfindahl,
        "px_min": None if not agg["has_px"] else agg["px_min"],
        "px_max": None if not agg["has_px"] else agg["px_max"],
        "px_range": None if not agg["has_px"] else agg["px_max"] - agg["px_min"],
        "first_px": None if not agg["has_px"] else agg["first_px"],
        "last_px": None if not agg["has_px"] else agg["last_px"],
        "duration_sec": dur,
    }


def _in_window(ts: int, lo: int, hi: int, *, hi_inclusive: bool) -> bool:
    if ts < lo:
        return False
    if hi_inclusive:
        return ts <= hi
    return ts < hi


def flow_from_trades(
    records: Iterable[Any],
    event: SweepEvent,
    *,
    reclaim_ts: Optional[int] = None,
) -> dict[str, Any]:
    t_open = int(event.sweep_bar_time)
    t_cut = t_open + CUTOFF_OFFSET_SEC
    t_pre = t_open - PRE_LOOKBACK_SEC
    t_end = t_open + 120
    is_pdl = event.side == "pdl_sweep"
    pen = max(float(event.penetration_points), 0.0)
    pen_ticks = pen / TICK

    windows = {
        "pre_30": (t_open - 30, t_open, False),
        "pre_60": (t_pre, t_open, False),
        "sweep_30": (t_open, t_open + 30, False),
        "sweep_60": (t_open, t_cut, True),
        "post_30": (t_cut + 1, t_open + 90, True),
        "post_60": (t_cut + 1, t_end, True),
    }
    aggs = {k: _empty_agg() for k in windows}
    n_class = 0
    n_post = 0
    n_before_cache = 0
    n_unsigned_all = 0
    n_signed_all = 0
    cvd = 0.0
    cvd_at_open = None
    cvd_at_cut = None
    cvd_min_pre = None
    cvd_max_pre = None
    cvd_min_sweep = None
    cvd_max_sweep = None
    parsed: list[tuple[int, float, float, str, int]] = []

    for rec in records:
        row = parse_trade(rec)
        if row is None:
            continue
        ts, px, sz, label, sgn = row
        parsed.append(row)
        if ts < t_pre:
            n_before_cache += 1
            continue
        if ts <= t_cut:
            n_class += 1
        elif ts <= t_end:
            n_post += 1
        if sgn == 0:
            n_unsigned_all += 1
        else:
            n_signed_all += 1
        if t_pre <= ts <= t_end and sgn != 0:
            cvd += sgn * sz
            if ts < t_open:
                cvd_min_pre = cvd if cvd_min_pre is None else min(cvd_min_pre, cvd)
                cvd_max_pre = cvd if cvd_max_pre is None else max(cvd_max_pre, cvd)
            if t_open <= ts <= t_cut:
                cvd_min_sweep = cvd if cvd_min_sweep is None else min(cvd_min_sweep, cvd)
                cvd_max_sweep = cvd if cvd_max_sweep is None else max(cvd_max_sweep, cvd)
            if ts < t_open:
                cvd_at_open = cvd
            if ts <= t_cut:
                cvd_at_cut = cvd
        for name, (lo, hi, incl) in windows.items():
            if _in_window(ts, lo, hi, hi_inclusive=incl):
                _add(aggs[name], px, sz, label, sgn)

    feats: dict[str, Any] = {
        "has_trades": n_class > 0,
        "n_trades_le_cutoff": n_class,
        "n_trades_post_cutoff_in_cache": n_post,
        "n_trades_before_pre_window": n_before_cache,
        "n_signed_in_cache_window": n_signed_all,
        "n_unsigned_in_cache_window": n_unsigned_all,
        "feature_cutoff_ts": t_cut,
        "cvd_at_open": cvd_at_open,
        "cvd_at_cut": cvd_at_cut,
        "cvd_change_sweep": None if cvd_at_open is None or cvd_at_cut is None else cvd_at_cut - cvd_at_open,
        "cvd_min_pre": cvd_min_pre,
        "cvd_max_pre": cvd_max_pre,
        "cvd_min_sweep": cvd_min_sweep,
        "cvd_max_sweep": cvd_max_sweep,
    }
    durs = {"pre_30": 30.0, "pre_60": 60.0, "sweep_30": 30.0, "sweep_60": 60.0, "post_30": 30.0, "post_60": 60.0}
    finalized = {k: _finalize(aggs[k], durs[k]) for k in windows}
    for k, row in finalized.items():
        for fk, fv in row.items():
            feats[f"{k}_{fk}"] = fv

    def _align(delta: float, ndelta: float) -> tuple[float, float]:
        if is_pdl:
            return delta, ndelta
        return -delta, -ndelta

    s60 = finalized["sweep_60"]
    p60 = finalized["pre_60"]
    post = finalized["post_60"]
    d_rev, nd_rev = _align(s60["delta"], s60["normalized_delta"])
    pre_d_rev, pre_nd_rev = _align(p60["delta"], p60["normalized_delta"])
    post_d_rev, post_nd_rev = _align(post["delta"], post["normalized_delta"])
    opposing = s60["asv"] if is_pdl else s60["abv"]
    opposing_pre = p60["asv"] if is_pdl else p60["abv"]
    supporting = s60["abv"] if is_pdl else s60["asv"]
    burst = s60["volume"] / max(p60["volume"], 1.0)
    efficiency = opposing / max(pen, 0.25)
    impact = pen / max(opposing, 1.0)
    opp_ratio = opposing / max(opposing_pre, 1.0)
    exhaustion = opp_ratio / (1.0 + pen_ticks)
    divergence = 1.0 if nd_rev > 0 else 0.0
    flow_flip = 1.0 if nd_rev < 0 and post_nd_rev > 0 else 0.0
    cvd_chg = feats["cvd_change_sweep"]
    if cvd_chg is None:
        cvd_div = None
    else:
        cvd_aligned = cvd_chg if is_pdl else -cvd_chg
        cvd_div = 1.0 if cvd_aligned > 0 else 0.0

    feats.update({
        "ndelta_rev_sweep60": nd_rev,
        "delta_rev_sweep60": d_rev,
        "ndelta_rev_pre60": pre_nd_rev,
        "delta_rev_pre60": pre_d_rev,
        "ndelta_rev_post60": post_nd_rev,
        "delta_rev_post60": post_d_rev,
        "opposing_vol_sweep60": opposing,
        "supporting_vol_sweep60": supporting,
        "volume_burst": burst,
        "flow_efficiency": efficiency,
        "price_impact": impact,
        "exhaustion_score": exhaustion,
        "delta_divergence": divergence,
        "cvd_divergence": cvd_div,
        "flow_flip_diagnostic": flow_flip,
        "opening_bar": 1.0 if int(event.seconds_from_rth_open) < 60 else 0.0,
    })

    reclaim_nd = None
    reclaim_in_cache = False
    if reclaim_ts is not None:
        r_lo, r_hi = t_cut + 1, int(reclaim_ts)
        if r_hi <= t_end and r_hi >= r_lo:
            reclaim_in_cache = True
            ragg = _empty_agg()
            for ts, px, sz, label, sgn in parsed:
                if r_lo <= ts <= r_hi:
                    _add(ragg, px, sz, label, sgn)
            rfin = _finalize(ragg, max(1.0, r_hi - r_lo + 1))
            _, reclaim_nd = _align(rfin["delta"], rfin["normalized_delta"])
            feats["reclaim_window_volume"] = rfin["volume"]
            feats["reclaim_window_n_trades"] = rfin["n_trades"]
        elif int(reclaim_ts) <= t_cut:
            reclaim_in_cache = True
            reclaim_nd = nd_rev
    feats["reclaim_in_cache"] = reclaim_in_cache
    feats["ndelta_rev_reclaim_diagnostic"] = reclaim_nd
    return feats


def expected_sign(feature: str) -> int:
    """+1 means higher values should raise P(reversal)."""
    if feature in ("price_impact",):
        return -1
    return 1
