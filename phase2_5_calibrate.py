"""Phase 2.5 — calibrate DST-aware session definitions against ICT levels."""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from bars import fetch_bars
from ict_sessions import fetch_ict_session_ranges
from models import Bar, PRIMARY_SESSIONS
from ohlc_sessions import compute_session_ranges
from session_time import SessionDefinition, resolve_session_window
from sessions_config import SESSION_DEFINITIONS


CANDIDATES: dict[str, dict[str, SessionDefinition]] = {
    "ny_ict_classic": {
        "Asia": SessionDefinition(
            "Asia", "America/New_York", "20:00", "03:00", source="candidate"
        ),
        "London": SessionDefinition(
            "London", "America/New_York", "03:00", "08:30", source="candidate"
        ),
    },
    "gmt_labeled": {
        "Asia": SessionDefinition("Asia", "UTC", "00:00", "07:00", source="candidate"),
        "London": SessionDefinition(
            "London", "UTC", "08:00", "13:30", source="candidate"
        ),
    },
    "gmt_london_shifted": {
        "Asia": SessionDefinition("Asia", "UTC", "00:00", "07:00", source="candidate"),
        "London": SessionDefinition(
            "London", "UTC", "07:00", "12:30", source="candidate"
        ),
    },
    "europe_london_local": {
        "Asia": SessionDefinition(
            "Asia", "Europe/London", "00:00", "07:00", source="candidate"
        ),
        "London": SessionDefinition(
            "London", "Europe/London", "08:00", "13:30", source="candidate"
        ),
    },
    "hybrid_asia_utc_london_eu": {
        "Asia": SessionDefinition("Asia", "UTC", "00:00", "07:00", source="candidate"),
        "London": SessionDefinition(
            "London", "Europe/London", "08:00", "13:30", source="candidate"
        ),
    },
}


def _fmt(ts):
    if ts is None:
        return None
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).isoformat()


def _pair_key(session: str, start: int | None) -> str:
    return f"{session}:{start}"


def compare_candidate(
    name: str,
    definitions: dict[str, SessionDefinition],
    ict_ranges: list,
    bars: list[Bar],
    resolution: int,
) -> dict:
    ohlc = compute_session_ranges(
        bars, definitions=definitions, resolution_minutes=resolution
    )
    ohlc_full = [
        r
        for r in ohlc
        if r.name in PRIMARY_SESSIONS
        and r.high is not None
        and r.coverage_status == "full"
    ]
    ohlc_by_id = {_pair_key(r.name, r.start): r for r in ohlc_full}
    # Price fingerprint index for ICT drawings lacking time anchors.
    ohlc_by_price: dict[tuple, list] = {}
    for r in ohlc_full:
        ohlc_by_price.setdefault((r.name, float(r.high), float(r.low)), []).append(r)

    rows = []
    matched = 0
    compared = 0
    for ict in ict_ranges:
        if ict.name not in PRIMARY_SESSIONS or ict.high is None:
            continue

        ohlc_r = None
        match_method = None
        if ict.start is not None:
            ohlc_r = ohlc_by_id.get(_pair_key(ict.name, ict.start))
            if ohlc_r is not None:
                match_method = "start_identity"
            else:
                same = [r for r in ohlc_full if r.name == ict.name]
                if same:
                    ohlc_r = min(same, key=lambda r: abs((r.start or 0) - ict.start))
                    match_method = "nearest_start"

        if ohlc_r is None:
            hits = ohlc_by_price.get((ict.name, float(ict.high), float(ict.low))) or []
            if len(hits) == 1:
                ohlc_r = hits[0]
                match_method = "unique_price_fingerprint"
            elif len(hits) > 1:
                # Ambiguous identical ranges — skip scoring
                rows.append(
                    {
                        "session": ict.name,
                        "ict_high": ict.high,
                        "ict_low": ict.low,
                        "match": False,
                        "comparable_full_coverage": False,
                        "reason": "ambiguous_price_fingerprint",
                        "candidates": len(hits),
                    }
                )
                continue

        if ohlc_r is None or ohlc_r.high is None:
            rows.append(
                {
                    "session": ict.name,
                    "ict_identity": ict.identity,
                    "ict_start": _fmt(ict.start),
                    "ict_high": ict.high,
                    "ict_low": ict.low,
                    "match": False,
                    "comparable_full_coverage": False,
                    "reason": "no_internal_range",
                }
            )
            continue

        comparable = ohlc_r.coverage_status == "full"
        high_ok = abs(float(ohlc_r.high) - float(ict.high)) < 1e-6
        low_ok = abs(float(ohlc_r.low) - float(ict.low)) < 1e-6
        ok = high_ok and low_ok
        if comparable:
            compared += 1
            if ok:
                matched += 1
        rw = (ohlc_r.extras or {}).get("resolved_window") or {}
        rows.append(
            {
                "date": rw.get("trading_date"),
                "session": ict.name,
                "reference_timezone": rw.get("reference_timezone"),
                "local_start": rw.get("local_start_datetime"),
                "local_end": rw.get("local_end_datetime"),
                "utc_start": _fmt(ohlc_r.start),
                "utc_end": _fmt(ohlc_r.end),
                "dst_active": rw.get("dst_active"),
                "utc_offset_start": rw.get("utc_offset_start"),
                "ICT_High": ict.high,
                "ICT_Low": ict.low,
                "Internal_High": ohlc_r.high,
                "Internal_Low": ohlc_r.low,
                "match": ok,
                "comparable_full_coverage": comparable,
                "coverage": ohlc_r.coverage_status,
                "match_method": match_method,
                "high_diff": round(float(ict.high) - float(ohlc_r.high), 6),
                "low_diff": round(float(ict.low) - float(ohlc_r.low), 6),
            }
        )

    by_session = {}
    for sess in PRIMARY_SESSIONS:
        srows = [
            r
            for r in rows
            if r.get("session") == sess and r.get("comparable_full_coverage")
        ]
        s_ok = sum(1 for r in srows if r.get("match"))
        by_session[sess] = {
            "compared": len(srows),
            "matched": s_ok,
            "rate": None if not srows else round(s_ok / len(srows), 4),
        }

    return {
        "candidate": name,
        "definitions": {k: v.to_dict() for k, v in definitions.items()},
        "compared_full": compared,
        "matched_full": matched,
        "rate_full": None if compared == 0 else round(matched / compared, 4),
        "by_session": by_session,
        "rows": rows,
        "mismatches": [
            r
            for r in rows
            if r.get("comparable_full_coverage") and not r.get("match")
        ],
    }


def dst_transition_table() -> list[dict]:
    """Show UTC conversion shifts around US and EU DST transitions (no bar data needed)."""
    defs = SESSION_DEFINITIONS
    # 2026: US DST starts Mar 8; EU starts Mar 29; EU ends Oct 25; US ends Nov 1.
    probe_dates = [
        date(2026, 3, 6),   # before US DST
        date(2026, 3, 9),   # after US DST, before EU
        date(2026, 3, 28),  # still before EU DST
        date(2026, 3, 30),  # after EU DST
        date(2026, 10, 24), # before EU ends
        date(2026, 10, 26), # after EU ends, US still DST
        date(2026, 11, 2),  # after US ends
        date(2026, 8, 14),  # summer reference (validated live)
    ]
    out = []
    for d in probe_dates:
        for name, definition in defs.items():
            # For Asia, trading_date is the evening local start date.
            w = resolve_session_window(definition, d)
            out.append(
                {
                    "trading_date": d.isoformat(),
                    "session": name,
                    "local_start": w.local_start_datetime.isoformat(),
                    "local_end": w.local_end_datetime.isoformat(),
                    "utc_start": _fmt(w.utc_start),
                    "utc_end": _fmt(w.utc_end),
                    "utc_offset_start": w.utc_offset_start,
                    "utc_offset_end": w.utc_offset_end,
                    "dst_active": w.dst_active,
                }
            )
    return out


async def main():
    bars_payload = await fetch_bars()
    if not bars_payload.get("ok"):
        print(json.dumps(bars_payload, indent=2))
        return
    bars = bars_payload["bars"]
    resolution = int(bars_payload.get("resolution") or 5)
    by_index = bars_payload.get("bars_by_series_index") or {}

    ict = await fetch_ict_session_ranges(bars_by_series_index=by_index)
    if not ict.get("ok"):
        print(json.dumps(ict, indent=2))
        return

    # Use all ICT Asia/London price levels; time anchors are sparse.
    ict_ranges = [
        r
        for r in ict.get("ranges") or []
        if r.name in PRIMARY_SESSIONS and r.high is not None and r.low is not None
    ]

    results = []
    for cname, defs in CANDIDATES.items():
        results.append(
            compare_candidate(cname, defs, ict_ranges, bars, resolution)
        )
    results.sort(key=lambda r: (r["rate_full"] is None, -(r["rate_full"] or -1)))

    locked = compare_candidate(
        "locked_sessions_config",
        SESSION_DEFINITIONS,
        ict_ranges,
        bars,
        resolution,
    )

    report = {
        "symbol": bars_payload.get("symbol"),
        "resolution": resolution,
        "bar_count": len(bars),
        "bar_span": {
            "first": _fmt(bars[0].time) if bars else None,
            "last": _fmt(bars[-1].time) if bars else None,
        },
        "ict_indicator": {
            "timezone_input": ict.get("timezone"),
            "study_id": ict.get("study_id"),
            "asia_count": sum(1 for r in ict_ranges if r.name == "Asia"),
            "london_count": sum(1 for r in ict_ranges if r.name == "London"),
            "session_strings": {
                "Asia": "0000-0700",
                "London": "0800-1330",
                "New York": "1330-2100",
            },
            "timezone_options_are_fixed_offsets": True,
        },
        "candidate_ranking": [
            {
                "candidate": r["candidate"],
                "rate_full": r["rate_full"],
                "matched_full": r["matched_full"],
                "compared_full": r["compared_full"],
                "by_session": r["by_session"],
            }
            for r in results
        ],
        "best_candidate": results[0]["candidate"] if results else None,
        "locked_config_validation": {
            "rate_full": locked["rate_full"],
            "by_session": locked["by_session"],
            "mismatches": locked["mismatches"],
            "sample_rows": [
                r
                for r in locked["rows"]
                if r.get("comparable_full_coverage")
            ][-10:],
        },
        "dst_transition_conversions": dst_transition_table(),
        "note": (
            "Loaded chart history may not span US/EU DST transition weeks; "
            "dst_transition_conversions still prove ZoneInfo shifts UTC bounds "
            "by calendar date without frozen offsets."
        ),
    }

    out = Path(__file__).resolve().parent / "phase2_5_calibration.json"
    # Include full best candidate rows for evidence
    report["best_candidate_detail"] = results[0] if results else None
    out.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    print(json.dumps(report, indent=2, default=str))
    print(f"\nWrote {out}")


if __name__ == "__main__":
    asyncio.run(main())
