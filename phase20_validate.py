"""Phase 20 — LuxAlgo ↔ internal CHoCH equivalence falsification (no strategy changes)."""

from __future__ import annotations

import csv
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, Sequence

from bar_dataset import load_dataset
from historical_structure import detect_internal_choch
from luxalgo_capture import (
    DEFAULT_CAPTURE_PATH,
    captures_to_confirmations,
    load_luxalgo_captures,
)
from models import Bar, StructureConfirmation
from phase20_capture import summarize_capture_store
from phase20_divergence import classify_divergence, summarize_divergences
from phase20_mapping import apply_mapping_to_confirmation, map_luxalgo_event_to_bars
from phase20_matching import (
    EXACT_MATCH,
    LUXALGO_ONLY,
    NEAR_TIME_MATCH,
    classify_equivalence_status_phase20,
    match_overlap,
)
from timeframe import timeframe_seconds

SYMBOL_TV = "OANDA:XAUUSD"
REPORTS = Path("reports")
PHASE20_JSON = Path("phase20_validation.json")
LEVEL_TOLERANCE = 5.0  # fixed; not tuned to match rate
MIN_RELIABLE_FOR_CLAIM = 50
PHASE19_VERDICT_PRESERVED = "NO_EDGE_OBSERVED"


def _enrich_internal(events: Sequence[StructureConfirmation], bars: Sequence[Bar]) -> list[dict[str, Any]]:
    out = []
    for e in events:
        extras = dict(e.extras or {})
        swing_i = extras.get("swing_index")
        swing_ts = None
        bars_since = None
        if swing_i is not None and 0 <= int(swing_i) < len(bars):
            swing_ts = int(bars[int(swing_i)].time)
            if e.event_bar_index is not None:
                bars_since = int(e.event_bar_index) - int(swing_i)
        out.append(
            {
                "direction": e.direction,
                "break_level": e.level,
                "event_timestamp": e.event_timestamp,
                "bar_index": e.event_bar_index,
                "broken_swing_index": swing_i,
                "swing_timestamp": swing_ts,
                "bars_since_swing": bars_since,
                "source": e.source,
                "algorithm_version": extras.get("algorithm_version"),
                "parameters": extras.get("parameters"),
            }
        )
    return out


def _window_bars(bars: Sequence[Bar], tmin: int, tmax: int, *, pad_bars: int, period_sec: int) -> list[Bar]:
    pad = pad_bars * period_sec
    lo = tmin - pad
    hi = tmax + pad
    return [b for b in bars if lo <= int(b.time) <= hi]


def analyze_timeframe(
    *,
    timeframe: str,
    capture_rows: list[dict[str, Any]],
    tv_bars: list[Bar],
) -> dict[str, Any]:
    period = timeframe_seconds(timeframe) or (300 if timeframe == "5m" else 900)
    reliable_rows = [r for r in capture_rows if r.get("reliable")]
    all_lux = captures_to_confirmations(capture_rows)

    # Map each Lux event; exclude unresolved from strict matching
    mapped_reliable: list[StructureConfirmation] = []
    mapping_dist: dict[str, int] = {}
    mapping_rows: list[dict[str, Any]] = []
    for row in capture_rows:
        confs = captures_to_confirmations([row])
        if not confs:
            continue
        ev = confs[0]
        mapping = map_luxalgo_event_to_bars(
            ev,
            tv_bars,
            period_sec=period,
            allow_nearest=False,
        )
        method = str(mapping.get("mapping_method") or mapping.get("mapping_status"))
        mapping_dist[method] = mapping_dist.get(method, 0) + 1
        mapping_rows.append({**row, **mapping})
        if row.get("reliable") and mapping.get("mapping_status") == "mapped":
            mapped_reliable.append(apply_mapping_to_confirmation(ev, mapping))
        elif row.get("reliable") and mapping.get("mapping_status") != "mapped":
            # Keep as timing unresolved for metrics
            mapped_reliable.append(
                StructureConfirmation(
                    kind=ev.kind,
                    direction=ev.direction,
                    level=ev.level,
                    event_timestamp=None,
                    event_bar_index=ev.event_bar_index,
                    source=ev.source,
                    study_id=ev.study_id,
                    raw_id=ev.raw_id,
                    timing_confidence="unavailable",
                    extras={**(ev.extras or {}), "mapping": mapping},
                )
            )

    # Internal CHoCH on identical TV/OANDA bars (prefer overlap window)
    if mapped_reliable:
        timed = [e for e in mapped_reliable if e.event_timestamp is not None]
        if timed:
            tmin = min(int(e.event_timestamp) for e in timed)
            tmax = max(int(e.event_timestamp) for e in timed)
            window = _window_bars(tv_bars, tmin, tmax, pad_bars=50, period_sec=period)
        else:
            window = tv_bars[-500:] if len(tv_bars) > 500 else list(tv_bars)
    else:
        window = tv_bars[-500:] if len(tv_bars) > 500 else list(tv_bars)

    internal_events = detect_internal_choch(window) if window else []
    internal_rows = _enrich_internal(internal_events, window)

    overlap = match_overlap(
        mapped_reliable if mapped_reliable else all_lux,
        internal_events,
        timeframe=timeframe,
        period_sec=period,
        level_tolerance=LEVEL_TOLERANCE,
    )

    # Attach divergence classifications
    classified = []
    for row in overlap.get("classifications") or []:
        div = classify_divergence(row)
        classified.append({**row, "divergence": div})
    div_summary = summarize_divergences(classified)
    overlap = {**overlap, "classifications": classified, "divergence": div_summary}

    status, conf_note = classify_equivalence_status_phase20(
        reliable_n=len([e for e in mapped_reliable if e.event_timestamp is not None]),
        exact=int(overlap.get("exact_matches") or 0),
        near2=int(overlap.get("within_2_bar_matches") or 0),
        luxalgo_only=int(overlap.get("luxalgo_only") or 0),
        internal_only=int(overlap.get("internal_only_count") or 0),
    )

    bull = sum(1 for r in reliable_rows if r.get("direction") == "bullish")
    bear = sum(1 for r in reliable_rows if r.get("direction") == "bearish")
    ts_list = [
        int(r["event_timestamp"])
        for r in reliable_rows
        if r.get("event_timestamp") is not None
    ]

    return {
        "timeframe": timeframe,
        "tv_bar_count": len(tv_bars),
        "window_bar_count": len(window),
        "luxalgo_rows": len(capture_rows),
        "luxalgo_reliable": len(reliable_rows),
        "luxalgo_reliable_mapped": sum(
            1 for e in mapped_reliable if e.event_timestamp is not None
        ),
        "bullish": bull,
        "bearish": bear,
        "date_coverage_unix": {
            "min": min(ts_list) if ts_list else None,
            "max": max(ts_list) if ts_list else None,
        },
        "mapping_confidence_distribution": mapping_dist,
        "mapping_rows": mapping_rows,
        "internal_event_count": len(internal_events),
        "internal_events": internal_rows,
        "overlap": overlap,
        "equivalence_status": status,
        "equivalence_confidence": conf_note,
        "level_tolerance": LEVEL_TOLERANCE,
        "bar_provider": "TradingView/OANDA local dataset (not Tiingo)",
    }


def _write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: Optional[list[str]] = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    keys = fieldnames or sorted({k for r in rows for k in r.keys()})
    with path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=keys, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            flat = {k: _csv_cell(r.get(k)) for k in keys}
            w.writerow(flat)


def _csv_cell(v: Any) -> Any:
    if isinstance(v, (dict, list)):
        return json.dumps(v, default=str)
    return v


def decide_final(
    *,
    reliable_total: int,
    overall_status: str,
    by_tf: dict[str, Any],
) -> dict[str, Any]:
    """
    Phase 20 decision. Does not mutate Phase 19 NO_EDGE_OBSERVED.
    No PnL-guided v2. Replay only if LOW_EQUIVALENCE with adequate N.
    """
    if reliable_total < MIN_RELIABLE_FOR_CLAIM:
        return {
            "decision": "NEED_MORE_LUXALGO_EVENTS",
            "continue_strategy": "deferred_until_luxalgo_sample",
            "paper_validation_justified": False,
            "v2_required": False,
            "v2_replaced_v1": False,
            "replay_required": False,
            "phase19_verdict_preserved": PHASE19_VERDICT_PRESERVED,
            "phase19_representative": "unknown_insufficient_luxalgo_n",
            "rationale": (
                f"reliable LuxAlgo CHoCH N={reliable_total} < {MIN_RELIABLE_FOR_CLAIM}; "
                "cannot claim internal~LuxAlgo nor HISTORICAL_MODEL_MISMATCH"
            ),
        }

    if overall_status == "HIGH_EQUIVALENCE":
        return {
            "decision": "NO_EDGE_CONFIRMED",
            "continue_strategy": False,
            "paper_validation_justified": False,
            "v2_required": False,
            "v2_replaced_v1": False,
            "replay_required": False,
            "phase19_verdict_preserved": PHASE19_VERDICT_PRESERVED,
            "phase19_representative": True,
            "recommend": "RETIRE_OR_RETHINK_STRATEGY_HYPOTHESIS",
            "rationale": "internal CHoCH sufficiently matches LuxAlgo; Phase 19 negative edge is more trustworthy",
        }

    if overall_status == "LOW_EQUIVALENCE":
        return {
            "decision": "HISTORICAL_MODEL_MISMATCH",
            "continue_strategy": "only_after_luxalgo_equivalent_confirmation_replay",
            "paper_validation_justified": False,
            "v2_required": True,
            "v2_replaced_v1": False,
            "replay_required": True,
            "phase19_verdict_preserved": PHASE19_VERDICT_PRESERVED,
            "phase19_representative": False,
            "rationale": (
                "internal v1 poorly represents LuxAlgo; Phase 19 must be qualified as "
                "HISTORICAL_MODEL_MISMATCH; derive v2 from Lux behavior only then replay C4/C3/C12"
            ),
            "note": "v2 not auto-implemented without documented LuxAlgo behavioral rules from adequate mismatches",
        }

    if overall_status == "PARTIAL_EQUIVALENCE":
        # Materiality: if coverage poor on setup-selection scale, recommend one replay
        cov5 = (by_tf.get("5m") or {}).get("overlap", {}).get("luxalgo_coverage")
        material = cov5 is not None and float(cov5) < 0.55
        return {
            "decision": "EDGE_REQUIRES_REVALIDATION" if material else "NEED_MORE_LUXALGO_EVENTS",
            "continue_strategy": material,
            "paper_validation_justified": False,
            "v2_required": material,
            "v2_replaced_v1": False,
            "replay_required": material,
            "phase19_verdict_preserved": PHASE19_VERDICT_PRESERVED,
            "phase19_representative": not material,
            "rationale": (
                "PARTIAL_EQUIVALENCE: mismatches "
                + ("likely material for setup selection" if material else "not clearly material; need more events")
            ),
        }

    return {
        "decision": "NEED_MORE_LUXALGO_EVENTS",
        "continue_strategy": "deferred",
        "paper_validation_justified": False,
        "v2_required": False,
        "v2_replaced_v1": False,
        "replay_required": False,
        "phase19_verdict_preserved": PHASE19_VERDICT_PRESERVED,
        "phase19_representative": "unknown",
        "rationale": f"overall_status={overall_status}",
    }


def run_phase20(*, attempt_live_capture: bool = False) -> dict[str, Any]:
    REPORTS.mkdir(parents=True, exist_ok=True)
    capture_attempt = None
    if attempt_live_capture:
        try:
            import asyncio
            from phase20_capture import capture_luxalgo_choch_once

            capture_attempt = asyncio.run(capture_luxalgo_choch_once(include_unreliable=True))
        except Exception as exc:  # noqa: BLE001
            capture_attempt = {"ok": False, "error": str(exc)}

    store = summarize_capture_store(path=DEFAULT_CAPTURE_PATH)
    # Also merge legacy store into summary counts via load
    all_rows = load_luxalgo_captures(symbol=SYMBOL_TV)
    by_tf_rows = {
        "5m": [r for r in all_rows if r.get("timeframe") == "5m"],
        "15m": [r for r in all_rows if r.get("timeframe") == "15m"],
    }

    results: dict[str, Any] = {}
    lux_csv_rows = []
    int_csv_rows = []
    div_csv_rows = []
    eq_csv_rows = []

    for tf in ("5m", "15m"):
        tv = load_dataset(SYMBOL_TV, tf)
        bars = tv.get("bars") or []
        analysis = analyze_timeframe(
            timeframe=tf,
            capture_rows=by_tf_rows[tf],
            tv_bars=bars,
        )
        results[tf] = analysis

        for r in by_tf_rows[tf]:
            lux_csv_rows.append(
                {
                    "timeframe": tf,
                    "event_id": r.get("event_id"),
                    "direction": r.get("direction"),
                    "level": r.get("level"),
                    "event_timestamp": r.get("event_timestamp", r.get("timestamp")),
                    "timing_confidence": r.get("timing_confidence"),
                    "reliable": r.get("reliable"),
                    "mapping_status": r.get("mapping_status"),
                    "study_id": r.get("study_id"),
                    "study_name": r.get("study_name"),
                }
            )
        for ie in analysis.get("internal_events") or []:
            int_csv_rows.append({"timeframe": tf, **ie})

        ov = analysis.get("overlap") or {}
        overlap_rows = []
        for c in ov.get("classifications") or []:
            lux = c.get("luxalgo") or {}
            mi = c.get("matched_internal") or {}
            overlap_rows.append(
                {
                    "timeframe": tf,
                    "category": c.get("category"),
                    "lux_direction": lux.get("direction"),
                    "lux_level": lux.get("level"),
                    "lux_ts": lux.get("event_timestamp"),
                    "internal_direction": mi.get("direction"),
                    "internal_level": mi.get("level"),
                    "internal_ts": mi.get("event_timestamp"),
                    "bar_delta": c.get("bar_delta"),
                    "level_delta": c.get("level_delta"),
                    "divergence_cause": (c.get("divergence") or {}).get("cause"),
                }
            )
            if c.get("category") not in (EXACT_MATCH, NEAR_TIME_MATCH):
                div_csv_rows.append(overlap_rows[-1])
        _write_csv(REPORTS / f"phase20_overlap_{tf}.csv", overlap_rows)

        eq_csv_rows.append(
            {
                "timeframe": tf,
                "reliable_n": analysis.get("luxalgo_reliable"),
                "reliable_mapped": analysis.get("luxalgo_reliable_mapped"),
                "internal_n": analysis.get("internal_event_count"),
                "exact": ov.get("exact_matches"),
                "within_1": ov.get("within_1_bar_matches"),
                "within_2": ov.get("within_2_bar_matches"),
                "luxalgo_only": ov.get("luxalgo_only"),
                "internal_only": ov.get("internal_only_count"),
                "luxalgo_coverage": ov.get("luxalgo_coverage"),
                "internal_precision": ov.get("internal_precision"),
                "equivalence_status": analysis.get("equivalence_status"),
                "confidence": analysis.get("equivalence_confidence"),
            }
        )

    _write_csv(REPORTS / "phase20_luxalgo_events.csv", lux_csv_rows)
    _write_csv(REPORTS / "phase20_internal_events.csv", int_csv_rows)
    _write_csv(REPORTS / "phase20_divergence.csv", div_csv_rows)
    _write_csv(REPORTS / "phase20_equivalence.csv", eq_csv_rows)

    reliable_total = sum(int(results[tf]["luxalgo_reliable"] or 0) for tf in ("5m", "15m"))
    statuses = [results[tf]["equivalence_status"] for tf in ("5m", "15m")]
    if all(s == "HIGH_EQUIVALENCE" for s in statuses) and reliable_total >= MIN_RELIABLE_FOR_CLAIM:
        overall = "HIGH_EQUIVALENCE"
    elif any(s == "LOW_EQUIVALENCE" for s in statuses) and reliable_total >= 20:
        overall = "LOW_EQUIVALENCE"
    elif any(s == "PARTIAL_EQUIVALENCE" for s in statuses) and reliable_total >= 20:
        overall = "PARTIAL_EQUIVALENCE"
    else:
        overall = "UNVALIDATED"

    decision = decide_final(
        reliable_total=reliable_total,
        overall_status=overall,
        by_tf=results,
    )

    # Strip bulky mapping_rows from JSON summary (kept in CSVs)
    slim = {}
    for tf, a in results.items():
        slim[tf] = {k: v for k, v in a.items() if k not in ("mapping_rows", "internal_events")}
        ov = dict(slim[tf].get("overlap") or {})
        # keep classification count but truncate list in top-level json
        clas = ov.get("classifications") or []
        ov["classifications_n"] = len(clas)
        ov["classifications_sample"] = clas[:20]
        ov.pop("classifications", None)
        ov.pop("internal_only", None)
        slim[tf]["overlap"] = ov

    payload = {
        "ok": True,
        "phase": 20,
        "strategy_version": "v1.phase20",
        "generated_at": datetime.now(tz=timezone.utc).isoformat(),
        "phase19_verdict_preserved": PHASE19_VERDICT_PRESERVED,
        "objective": "Does internal historical CHoCH represent LuxAlgo CHoCH used live?",
        "live_capture_attempt": capture_attempt,
        "capture_store": store,
        "reliable_luxalgo_total": reliable_total,
        "by_timeframe": slim,
        "overall_equivalence_status": overall,
        "historical_model": {
            "v2_required": decision.get("v2_required"),
            "v2_implemented": False,
            "v2_replaced_v1_for_replay": False,
            "note": "No PnL-guided CHoCH fitting. v2 only from LuxAlgo behavioral evidence.",
        },
        "replay": {
            "required": decision.get("replay_required"),
            "executed": False,
            "note": "Replay deferred until LuxAlgo-equivalent confirmation candidate exists with adequate N",
        },
        "decision": decision,
        "artifacts": {
            "validation_json": str(PHASE20_JSON),
            "luxalgo_events": str(REPORTS / "phase20_luxalgo_events.csv"),
            "internal_events": str(REPORTS / "phase20_internal_events.csv"),
            "overlap_5m": str(REPORTS / "phase20_overlap_5m.csv"),
            "overlap_15m": str(REPORTS / "phase20_overlap_15m.csv"),
            "divergence": str(REPORTS / "phase20_divergence.csv"),
            "equivalence": str(REPORTS / "phase20_equivalence.csv"),
            "capture_store": str(DEFAULT_CAPTURE_PATH),
        },
        "limitations": [
            "LuxAlgo historical drawings often use placeholder bar indexes (-2000000); exact timing unavailable",
            "Reliable events require live-first capture as labels appear",
            f"Target ≥{MIN_RELIABLE_FOR_CLAIM} reliable events not met → no equivalence claim",
            "Primary equivalence uses TradingView/OANDA bars when present",
            "Phase 19 NO_EDGE_OBSERVED unchanged pending equivalence evidence",
        ],
        "recommended_next_action": (
            "Continue live LuxAlgo CHoCH capture on 5m and 15m until ≥50 reliable timed events, "
            "then re-run phase20_validate without changing strategy rules."
            if decision.get("decision") == "NEED_MORE_LUXALGO_EVENTS"
            else decision.get("recommend")
            or decision.get("rationale")
        ),
    }
    PHASE20_JSON.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    return payload


def main() -> None:
    import sys

    live = "--live-capture" in sys.argv
    payload = run_phase20(attempt_live_capture=live)
    print(json.dumps({
        "ok": payload.get("ok"),
        "overall_equivalence_status": payload.get("overall_equivalence_status"),
        "reliable_luxalgo_total": payload.get("reliable_luxalgo_total"),
        "decision": (payload.get("decision") or {}).get("decision"),
        "phase19_verdict_preserved": payload.get("phase19_verdict_preserved"),
        "recommended_next_action": payload.get("recommended_next_action"),
        "by_timeframe_reliable": {
            tf: (payload.get("by_timeframe") or {}).get(tf, {}).get("luxalgo_reliable")
            for tf in ("5m", "15m")
        },
    }, indent=2))


if __name__ == "__main__":
    main()
