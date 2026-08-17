"""Offline Phase 3 narrative from prior Phase 1/2 captures (no live CDP)."""

from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from luxalgo_structure import normalize_choch_events
from models import Bar, LiquiditySweep
from sessions_config import SESSION_DST_UNCERTAINTY
from structure_confirm import confirm_after_sweep


def _fmt(ts):
    if ts is None:
        return None
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).isoformat()


def main() -> None:
    deep = json.loads(Path("phase1_v1_lux_deep.json").read_text(encoding="utf-8"))
    p2 = json.loads(Path("phase2_validation.json").read_text(encoding="utf-8"))

    events = normalize_choch_events(
        {
            "ok": True,
            "studyId": deep.get("studyId") or "smUEv2",
            "bullColor": 4286683400,
            "bearColor": 4283585279,
            "labels": deep.get("labels") or [],
            "lines": deep.get("lines") or [],
        },
        bars_by_series_index=None,
    )

    fh = p2["sweeps"]["London"]["first_high"]
    sweep = LiquiditySweep(
        session=fh["session"],
        side=fh["side"],
        level=fh["level"],
        sweep_timestamp=fh["sweep_timestamp"],
        sweep_price=fh["sweep_price"],
        maximum_excursion=fh["maximum_excursion"],
        reclaim_status=fh["reclaim_status"],
        rule=fh["rule"],
        sweep_candle=Bar(**fh["sweep_candle"]),
        session_range=fh.get("session_range"),
    )

    decision = confirm_after_sweep(sweep, events)
    candidates = [
        {
            "direction": e.direction,
            "level": e.level,
            "timestamp": _fmt(e.event_timestamp),
            "bar_index": e.event_bar_index,
            "timing_confidence": e.timing_confidence,
        }
        for e in events
    ]

    report = {
        "ok": True,
        "mode": "offline_captures",
        "note": (
            "Live TradingView CDP (9222) was unavailable during Phase 3 validation. "
            "This report uses the last saved LuxAlgo deep probe + Phase 2 sweep capture. "
            "It is not a forced confirmation."
        ),
        "session_dst_uncertainty": SESSION_DST_UNCERTAINTY,
        "live_example": {
            "session": sweep.session,
            "swept_side": sweep.side,
            "sweep_level": sweep.level,
            "sweep_time": _fmt(sweep.sweep_timestamp),
            "candidate_choch_events": candidates,
            "timing_summary": dict(Counter(e.timing_confidence for e in events)),
            "direction_summary": dict(Counter(e.direction for e in events)),
            "chosen_confirmation": None,
            "status": decision.reason,
            "decision": decision.to_dict(),
            "interpretation": (
                "London High was swept (wick_only). Required confirmation is bearish CHoCH "
                "after the sweep. Of 17 CHoCH labels, 8 are bearish but all have "
                "timing_confidence=unavailable (placeholder bar indexes). The only "
                "usable bar-indexed CHoCH is bullish (wrong direction for a high sweep). "
                "Fail-closed: no confirmation."
            ),
        },
    }
    Path("phase3_validation.json").write_text(
        json.dumps(report, indent=2, default=str), encoding="utf-8"
    )
    print(json.dumps(report, indent=2, default=str))


if __name__ == "__main__":
    main()
