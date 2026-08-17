"""Feed-equivalence validation vs TradingView OANDA:XAUUSD benchmark."""

from __future__ import annotations

import statistics
from dataclasses import asdict, dataclass, field
from typing import Any, Optional, Sequence

from dataset_overlap import (
    compare_htf_bias_overlap,
    compare_ohlc_overlap,
    compare_session_ranges_overlap,
)
from liquidity_sweep import detect_sweeps
from models import Bar, PRIMARY_SESSIONS
from ohlc_resample import resample_ohlc
from ohlc_sessions import compute_session_ranges
from trading_day_config import load_confirmed_trading_day_from_evidence


CLASS_EXACT = "EXACT_FEED"
CLASS_CLOSE = "CLOSE_EQUIVALENT"
CLASS_RESEARCH = "RESEARCH_ONLY"
CLASS_NOT = "NOT_EQUIVALENT"
CLASS_INSUFFICIENT = "INSUFFICIENT_EVIDENCE"

REPLAY_ELIGIBLE = frozenset({CLASS_EXACT, CLASS_CLOSE})


@dataclass
class FeedEquivalenceReport:
    benchmark_provider: str
    candidate_provider: str
    benchmark_symbol: str
    candidate_symbol: str
    instrument_type: str
    overlap_bars: int
    ohlc_metrics: dict[str, Any] = field(default_factory=dict)
    timestamp_alignment: dict[str, Any] = field(default_factory=dict)
    session_match_metrics: dict[str, Any] = field(default_factory=dict)
    sweep_match_metrics: dict[str, Any] = field(default_factory=dict)
    fvg_match_metrics: dict[str, Any] = field(default_factory=dict)
    entry_sensitivity: dict[str, Any] = field(default_factory=dict)
    htf_bias_match_metrics: dict[str, Any] = field(default_factory=dict)
    classification: str = CLASS_INSUFFICIENT
    confidence: str = "low"
    warnings: list[str] = field(default_factory=list)
    evidence: dict[str, Any] = field(default_factory=dict)
    deep_replay_allowed: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _abs_deltas(a: Sequence[Bar], b: Sequence[Bar]) -> dict[str, list[float]]:
    left = {int(x.time): x for x in a}
    right = {int(x.time): x for x in b}
    shared = sorted(set(left) & set(right))
    out = {"open": [], "high": [], "low": [], "close": []}
    for t in shared:
        x, y = left[t], right[t]
        out["open"].append(abs(float(x.open) - float(y.open)))
        out["high"].append(abs(float(x.high) - float(y.high)))
        out["low"].append(abs(float(x.low) - float(y.low)))
        out["close"].append(abs(float(x.close) - float(y.close)))
    return out


def _dist(vals: list[float]) -> dict[str, Any]:
    if not vals:
        return {
            "n": 0,
            "mean": None,
            "median": None,
            "p95": None,
            "max": None,
        }
    ordered = sorted(vals)
    p95_i = min(len(ordered) - 1, int(round(0.95 * (len(ordered) - 1))))
    return {
        "n": len(vals),
        "mean": statistics.mean(vals),
        "median": statistics.median(vals),
        "p95": ordered[p95_i],
        "max": max(vals),
    }


def ohlc_overlap_metrics(
    tv: Sequence[Bar],
    cand: Sequence[Bar],
    *,
    round_decimals: Optional[int] = 2,
) -> dict[str, Any]:
    raw = compare_ohlc_overlap(tv, cand, left_label="tv", right_label="candidate")
    deltas = _abs_deltas(tv, cand)
    metrics = {
        "bars_tv": len(tv),
        "bars_candidate": len(cand),
        "timestamps_overlapping": raw["exact_timestamp_matches"],
        "timestamps_only_tv": raw["missing_on_right"],
        "timestamps_only_candidate": raw["missing_on_left"],
        "exact_ohlc_match_pct": (
            None
            if not raw["exact_timestamp_matches"]
            else 100.0 * raw["ohlc_exact_matches"] / raw["exact_timestamp_matches"]
        ),
        "raw": {
            "open": _dist(deltas["open"]),
            "high": _dist(deltas["high"]),
            "low": _dist(deltas["low"]),
            "close": _dist(deltas["close"]),
            "max_open_delta": raw["max_open_delta"],
            "max_high_delta": raw["max_high_delta"],
            "max_low_delta": raw["max_low_delta"],
            "max_close_delta": raw["max_close_delta"],
        },
    }
    if round_decimals is not None:
        tv_r = [
            Bar(
                time=b.time,
                open=round(b.open, round_decimals),
                high=round(b.high, round_decimals),
                low=round(b.low, round_decimals),
                close=round(b.close, round_decimals),
            )
            for b in tv
        ]
        cand_r = [
            Bar(
                time=b.time,
                open=round(b.open, round_decimals),
                high=round(b.high, round_decimals),
                low=round(b.low, round_decimals),
                close=round(b.close, round_decimals),
            )
            for b in cand
        ]
        rr = compare_ohlc_overlap(tv_r, cand_r)
        metrics["rounded"] = {
            "decimals": round_decimals,
            "ohlc_exact_matches": rr["ohlc_exact_matches"],
            "exact_ohlc_match_pct": (
                None
                if not rr["exact_timestamp_matches"]
                else 100.0 * rr["ohlc_exact_matches"] / rr["exact_timestamp_matches"]
            ),
            "max_open_delta": rr["max_open_delta"],
            "max_high_delta": rr["max_high_delta"],
            "max_low_delta": rr["max_low_delta"],
            "max_close_delta": rr["max_close_delta"],
        }
    return metrics


def timestamp_alignment_report(tv: Sequence[Bar], cand: Sequence[Bar]) -> dict[str, Any]:
    tv_t = sorted(int(b.time) for b in tv)
    c_t = sorted(int(b.time) for b in cand)
    shared = sorted(set(tv_t) & set(c_t))
    # Detect systematic ±300s shift
    shift_plus = len(set(tv_t) & {t + 300 for t in c_t})
    shift_minus = len(set(tv_t) & {t - 300 for t in c_t})
    aligned = len(shared)
    issue = None
    if aligned == 0 and (shift_plus > 10 or shift_minus > 10):
        issue = "+5m_or_-5m_shift_suspected"
    elif aligned and aligned / max(1, min(len(tv_t), len(c_t))) < 0.2:
        issue = "poor_timestamp_overlap"
    return {
        "overlapping_timestamps": aligned,
        "tv_bar_count": len(tv_t),
        "candidate_bar_count": len(c_t),
        "overlap_ratio_vs_min": aligned / max(1, min(len(tv_t), len(c_t))),
        "shift_plus_5m_matches": shift_plus,
        "shift_minus_5m_matches": shift_minus,
        "alignment_issue": issue,
        "acceptable": issue is None and aligned >= 20,
    }


def sweep_equivalence(
    tv: Sequence[Bar],
    cand: Sequence[Bar],
    *,
    resolution_minutes: int = 5,
) -> dict[str, Any]:
    tv_sessions = [
        s
        for s in compute_session_ranges(tv, resolution_minutes=resolution_minutes)
        if s.complete and s.name in PRIMARY_SESSIONS
    ]
    cand_sessions = [
        s
        for s in compute_session_ranges(cand, resolution_minutes=resolution_minutes)
        if s.complete and s.name in PRIMARY_SESSIONS
    ]

    def _key(s) -> tuple:
        extras = s.extras or {}
        rw = extras.get("resolved_window") or {}
        return (s.name, str(rw.get("trading_date") or ""))

    tv_map = {_key(s): s for s in tv_sessions}
    c_map = {_key(s): s for s in cand_sessions}
    shared = sorted(set(tv_map) & set(c_map))
    rows = []
    match = 0
    for k in shared:
        a, b = tv_map[k], c_map[k]
        sa = detect_sweeps(a, tv)
        sb = detect_sweeps(b, cand)

        def _sig(sweeps):
            return {
                (
                    s.side,
                    round(float(s.level), 2),
                    int(s.sweep_timestamp) if s.sweep_timestamp else None,
                )
                for s in sweeps
            }

        sig_a, sig_b = _sig(sa), _sig(sb)
        sides_a = {s.side for s in sa}
        sides_b = {s.side for s in sb}
        same_sides = sides_a == sides_b and bool(sides_a)
        if same_sides:
            match += 1
        rows.append(
            {
                "session": k[0],
                "date": k[1],
                "tv_sweeps": [
                    {
                        "side": s.side,
                        "level": s.level,
                        "ts": s.sweep_timestamp,
                        "extreme": getattr(s, "sweep_price", None),
                    }
                    for s in sa
                ],
                "candidate_sweeps": [
                    {
                        "side": s.side,
                        "level": s.level,
                        "ts": s.sweep_timestamp,
                        "extreme": getattr(s, "sweep_price", None),
                    }
                    for s in sb
                ],
                "same_sides": same_sides,
                "signature_equal": sig_a == sig_b,
            }
        )
    n = len(shared)
    return {
        "sessions_compared": n,
        "sweep_side_matches": match,
        "sweep_side_match_rate": (match / n) if n else None,
        "rows": rows,
    }


def fvg_equivalence(
    tv: Sequence[Bar],
    cand: Sequence[Bar],
) -> dict[str, Any]:
    """Compare FVG detections on aligned windows (descriptive)."""
    tv_t = {int(b.time) for b in tv}
    c_t = {int(b.time) for b in cand}
    shared = sorted(tv_t & c_t)
    if len(shared) < 10:
        return {"compared": False, "reason": "insufficient_overlap", "match_rate": None}

    return {
        "compared": True,
        "overlap_bars": len(shared),
        "tv_fvg_count": None,
        "candidate_fvg_count": None,
        "note": (
            "Strategy FVGs require sweep→CHoCH sequencing; standalone zone scan "
            "omitted. Entry/FVG sensitivity runs only after CLOSE/EXACT gate."
        ),
        "match_rate": None,
    }


def classify_feed_equivalence(
    *,
    instrument_type: str,
    ohlc: dict[str, Any],
    alignment: dict[str, Any],
    session: dict[str, Any],
    sweeps: dict[str, Any],
    warnings: Optional[Sequence[str]] = None,
) -> tuple[str, str, list[str]]:
    """Return (classification, confidence, warnings)."""
    warns = list(warnings or [])
    overlap = int(ohlc.get("timestamps_overlapping") or 0)

    if instrument_type == "futures":
        warns.append("instrument_type=futures cannot be EXACT/CLOSE vs OANDA spot")
        if overlap < 20 or not alignment.get("acceptable"):
            return CLASS_NOT, "high", warns
        return CLASS_RESEARCH, "high", warns

    if overlap < 20 or not alignment.get("acceptable"):
        return CLASS_INSUFFICIENT, "low", warns

    if alignment.get("alignment_issue"):
        warns.append(str(alignment["alignment_issue"]))
        return CLASS_NOT, "high", warns

    sess_rate = session.get("session_hl_match_rate")
    sweep_rate = sweeps.get("sweep_side_match_rate")
    sess_n = int(session.get("sessions_compared") or 0)
    sweep_n = int(sweeps.get("sessions_compared") or 0)
    exact_pct = ohlc.get("exact_ohlc_match_pct") or 0.0
    rounded_pct = ((ohlc.get("rounded") or {}).get("exact_ohlc_match_pct")) or 0.0

    # Near-identical bars: EXACT even if the short window has no complete sessions yet.
    if exact_pct >= 99.0 and alignment.get("acceptable"):
        if sess_n == 0 and sweep_n == 0:
            warns.append("exact_ohlc_no_complete_sessions_in_overlap_window")
            return CLASS_EXACT, "medium", warns
        if (sess_rate or 0) >= 0.99 and (sweep_rate is None or (sweep_rate or 0) >= 0.99):
            return CLASS_EXACT, "high", warns
        if sess_rate is not None and sess_rate < 0.85:
            return CLASS_RESEARCH, "medium", warns
        return CLASS_EXACT, "high", warns

    if (
        sess_n > 0
        and (sess_rate is not None and sess_rate >= 0.85)
        and (sweep_n == 0 or (sweep_rate is not None and sweep_rate >= 0.80))
        and alignment.get("acceptable")
    ):
        return CLASS_CLOSE, "medium", warns

    if (sess_rate is not None and sess_rate < 0.5) or (
        sweep_rate is not None and sweep_rate < 0.5 and sweep_n > 0
    ):
        return CLASS_NOT, "high", warns

    # Large systematic price divergence without matching sessions
    raw_close = ((ohlc.get("raw") or {}).get("close") or {}).get("median")
    if raw_close is not None and raw_close > 5.0 and (sess_rate is None or sess_rate < 0.85):
        return CLASS_NOT, "high", warns

    if rounded_pct > 50 or (sess_rate or 0) >= 0.5:
        return CLASS_RESEARCH, "medium", warns

    return CLASS_RESEARCH, "low", warns


def evaluate_feed_equivalence(
    tv_bars: Sequence[Bar],
    candidate_bars: Sequence[Bar],
    *,
    benchmark_provider: str = "tradingview_oanda",
    candidate_provider: str = "openbb",
    benchmark_symbol: str = "OANDA:XAUUSD",
    candidate_symbol: str = "XAUUSD",
    instrument_type: str = "unknown",
    tv_daily: Optional[Sequence[Bar]] = None,
    tv_h4: Optional[Sequence[Bar]] = None,
    extra_warnings: Optional[Sequence[str]] = None,
) -> FeedEquivalenceReport:
    ohlc = ohlc_overlap_metrics(tv_bars, candidate_bars)
    align = timestamp_alignment_report(tv_bars, candidate_bars)
    session = compare_session_ranges_overlap(
        tv_bars,
        candidate_bars,
        left_label="tv",
        right_label="candidate",
        price_tolerance=0.5,
    )
    sweeps = sweep_equivalence(tv_bars, candidate_bars)
    fvg = fvg_equivalence(tv_bars, candidate_bars)

    # HTF via resampled candidate 5m when possible
    trading_day = load_confirmed_trading_day_from_evidence()
    htf: dict[str, Any] = {"compared": False}
    if tv_daily is not None and tv_h4 is not None and len(candidate_bars) >= 50:
        res_d = resample_ohlc(
            candidate_bars, "1D", source_timeframe="5m", trading_day=trading_day
        )
        res_h = resample_ohlc(
            candidate_bars, "4H", source_timeframe="5m", trading_day=trading_day
        )
        htf = compare_htf_bias_overlap(
            tv_daily,
            tv_h4,
            list(res_d.bars),
            list(res_h.bars),
            left_label="tv_htf",
            right_label="candidate_resampled_htf",
        )
        htf["compared"] = True
        htf["h4_anchor"] = (res_h.extras or {}).get("h4_anchor")
        htf["daily_boundary"] = (res_d.extras or {}).get("daily_boundary")

    classification, confidence, warns = classify_feed_equivalence(
        instrument_type=instrument_type,
        ohlc=ohlc,
        alignment=align,
        session=session,
        sweeps=sweeps,
        warnings=extra_warnings,
    )
    allowed = classification in REPLAY_ELIGIBLE
    return FeedEquivalenceReport(
        benchmark_provider=benchmark_provider,
        candidate_provider=candidate_provider,
        benchmark_symbol=benchmark_symbol,
        candidate_symbol=candidate_symbol,
        instrument_type=instrument_type,
        overlap_bars=int(ohlc.get("timestamps_overlapping") or 0),
        ohlc_metrics=ohlc,
        timestamp_alignment=align,
        session_match_metrics=session,
        sweep_match_metrics=sweeps,
        fvg_match_metrics=fvg,
        entry_sensitivity={
            "note": "Full entry-mode sensitivity deferred unless CLOSE/EXACT gate passes"
        },
        htf_bias_match_metrics=htf,
        classification=classification,
        confidence=confidence,
        warnings=warns,
        evidence={
            "session_hl_match_rate": session.get("session_hl_match_rate"),
            "sweep_side_match_rate": sweeps.get("sweep_side_match_rate"),
            "exact_ohlc_match_pct": ohlc.get("exact_ohlc_match_pct"),
        },
        deep_replay_allowed=allowed,
    )


def replay_gate(
    report: FeedEquivalenceReport,
    *,
    integrity_ok: bool = True,
    require_session_events: bool = True,
) -> dict[str, Any]:
    """
    Hard gate: deep strategy replay only if equivalence acceptable.

    Phase 17: EXACT_FEED|CLOSE_EQUIVALENT AND timestamp/session/sweep/integrity.
    Does not weaken acceptance for futures or correlated instruments.
    """
    checks = {
        "classification_ok": report.classification in REPLAY_ELIGIBLE,
        "timestamp_alignment_ok": bool(
            (report.timestamp_alignment or {}).get("acceptable")
        ),
        "session_equivalence_ok": True,
        "sweep_equivalence_ok": True,
        "integrity_ok": bool(integrity_ok),
        "not_futures": (report.instrument_type or "").lower() != "futures",
    }
    sess = report.session_match_metrics or {}
    sweeps = report.sweep_match_metrics or {}
    sess_n = int(sess.get("sessions_compared") or 0)
    sweep_n = int(sweeps.get("sessions_compared") or 0)
    sess_rate = sess.get("session_hl_match_rate")
    sweep_rate = sweeps.get("sweep_side_match_rate")

    if require_session_events:
        # Need at least one comparable complete session with strong match rates.
        checks["session_equivalence_ok"] = (
            sess_n >= 1 and sess_rate is not None and float(sess_rate) >= 0.85
        )
        checks["sweep_equivalence_ok"] = (
            sweep_n >= 1 and sweep_rate is not None and float(sweep_rate) >= 0.80
        )
    else:
        # Exact OHLC-only path (tests) still requires classification + alignment.
        checks["session_equivalence_ok"] = True
        checks["sweep_equivalence_ok"] = True

    ok = all(checks.values()) and report.classification in REPLAY_ELIGIBLE
    # Keep report flag consistent with gate
    report.deep_replay_allowed = ok
    failed = [k for k, v in checks.items() if not v]
    return {
        "deep_replay_allowed": ok,
        "classification": report.classification,
        "checks": checks,
        "failed_checks": failed,
        "reason": (
            "accepted"
            if ok
            else (
                f"gate_blocked:{report.classification};failed={failed}"
            )
        ),
    }
