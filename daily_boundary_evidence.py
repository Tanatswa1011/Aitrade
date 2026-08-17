"""Native Daily-bar boundary evidence (confirm or keep provisional)."""

from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional, Sequence
from zoneinfo import ZoneInfo

from models import Bar
from trading_day_config import (
    DEFAULT_DAY_ROLL_TIME,
    DEFAULT_REFERENCE_TIMEZONE,
    DEFAULT_TRADING_DAY_CONFIG,
    TradingDayConfig,
    infer_trading_day_config_from_native_bars,
)


@dataclass(frozen=True)
class DailyBoundaryEvidence:
    symbol: str
    observed_bar_count: int
    observed_local_open_times: list[str]
    inferred_timezone: str
    inferred_roll_time: Optional[str]
    dst_consistent: Optional[bool]
    weekend_consistent: Optional[bool]
    status: str  # provisional | confirmed | conflicting | insufficient_data
    notes: list[str] = field(default_factory=list)
    durations_sec: list[int] = field(default_factory=list)
    rows: list[dict[str, Any]] = field(default_factory=list)
    config: dict[str, Any] = field(default_factory=dict)
    extras: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _local_open_rows(
    bars: Sequence[Bar], *, tz_name: str = DEFAULT_REFERENCE_TIMEZONE
) -> list[dict[str, Any]]:
    tz = ZoneInfo(tz_name)
    ordered = sorted(bars, key=lambda b: int(b.time))
    rows = []
    for i, b in enumerate(ordered):
        local = datetime.fromtimestamp(int(b.time), tz=timezone.utc).astimezone(tz)
        nxt = int(ordered[i + 1].time) if i + 1 < len(ordered) else None
        dur = None if nxt is None else int(nxt) - int(b.time)
        rows.append(
            {
                "bar_timestamp": int(b.time),
                "utc_open_iso": datetime.fromtimestamp(
                    int(b.time), tz=timezone.utc
                ).isoformat(),
                "ny_local_open": local.isoformat(),
                "ny_hhmm": f"{local.hour:02d}:{local.minute:02d}",
                "weekday": local.strftime("%A"),
                "next_bar_timestamp": nxt,
                "utc_duration_to_next": dur,
                "utc_offset_hours": (
                    None
                    if local.utcoffset() is None
                    else local.utcoffset().total_seconds() / 3600.0
                ),
            }
        )
    return rows


def build_daily_boundary_evidence(
    bars: Sequence[Bar],
    *,
    symbol: str = "OANDA:XAUUSD",
    reference_timezone: str = DEFAULT_REFERENCE_TIMEZONE,
    expected_roll: str = DEFAULT_DAY_ROLL_TIME,
    min_bars: int = 10,
) -> DailyBoundaryEvidence:
    """
    Inspect native Daily opens and classify confirmation status.

    Does not force confirmation when evidence is thin or conflicting.
    """
    ordered = sorted(bars, key=lambda b: int(b.time))
    rows = _local_open_rows(ordered, tz_name=reference_timezone)
    notes: list[str] = []
    if len(ordered) < min_bars:
        notes.append(f"insufficient_bars:{len(ordered)}<{min_bars}")
        return DailyBoundaryEvidence(
            symbol=symbol,
            observed_bar_count=len(ordered),
            observed_local_open_times=[r["ny_hhmm"] for r in rows],
            inferred_timezone=reference_timezone,
            inferred_roll_time=None,
            dst_consistent=None,
            weekend_consistent=None,
            status="insufficient_data",
            notes=notes,
            durations_sec=[r["utc_duration_to_next"] for r in rows if r["utc_duration_to_next"]],
            rows=rows,
            config=DEFAULT_TRADING_DAY_CONFIG.to_dict(),
        )

    hhmm_counts = Counter(r["ny_hhmm"] for r in rows)
    mode_hhmm, mode_n = hhmm_counts.most_common(1)[0]
    mode_share = mode_n / max(1, len(rows))

    # Weekend: Saturday Daily opens are unexpected for FX; Sunday 17:00 week-open is normal.
    saturday_opens = [r for r in rows if r["weekday"] == "Saturday"]
    sunday_opens = [r for r in rows if r["weekday"] == "Sunday"]
    weekend_consistent = len(saturday_opens) == 0
    if saturday_opens:
        notes.append(f"saturday_opens={len(saturday_opens)}")
    if sunday_opens:
        notes.append(f"sunday_week_opens={len(sunday_opens)} (expected for FX)")

    # DST: mode local HH:MM should appear under both UTC offsets when history spans DST
    offsets = {
        r["utc_offset_hours"]
        for r in rows
        if r["ny_hhmm"] == mode_hhmm and r["utc_offset_hours"] is not None
    }
    durations = [r["utc_duration_to_next"] for r in rows if r["utc_duration_to_next"]]
    dst_durations = sorted({d for d in durations if d in (23 * 3600, 25 * 3600, 24 * 3600)})
    dst_consistent = True
    if len(offsets) > 1:
        notes.append(f"dst_offsets_at_mode={sorted(offsets)}")
        # Local clock roll stable across DST → consistent
        dst_consistent = mode_share >= 0.8
    if 23 * 3600 in durations or 25 * 3600 in durations:
        notes.append(f"dst_span_durations={dst_durations}")

    inferred = infer_trading_day_config_from_native_bars(
        ordered, reference_timezone=reference_timezone, min_samples=min_bars
    )

    status = "provisional"
    if mode_share < 0.6:
        status = "conflicting"
        notes.append(f"mode_share_low={mode_share:.2f} histogram={dict(hhmm_counts)}")
    elif mode_hhmm == expected_roll and mode_share >= 0.8 and weekend_consistent:
        status = "confirmed"
        notes.append("matches_17:00_America/New_York")
    elif mode_hhmm != expected_roll and mode_share >= 0.8:
        status = "confirmed"
        notes.append(f"alternate_roll_confirmed:{mode_hhmm}")
    else:
        status = "provisional"
        notes.append(f"mode={mode_hhmm} share={mode_share:.2f}")

    cfg = TradingDayConfig(
        reference_timezone=reference_timezone,
        day_roll_time=mode_hhmm or expected_roll,
        weekend_policy="skip_fabricate",
        source=(
            "native_tv_daily_opens_confirmed"
            if status == "confirmed"
            else "native_tv_daily_opens_provisional"
        ),
        extras={
            "evidence_status": status,
            "mode_share": mode_share,
            "hhmm_histogram": dict(hhmm_counts),
            "inferred_from_helper": inferred.to_dict(),
        },
    )

    return DailyBoundaryEvidence(
        symbol=symbol,
        observed_bar_count=len(ordered),
        observed_local_open_times=[r["ny_hhmm"] for r in rows],
        inferred_timezone=reference_timezone,
        inferred_roll_time=mode_hhmm,
        dst_consistent=dst_consistent,
        weekend_consistent=weekend_consistent,
        status=status,
        notes=notes,
        durations_sec=durations,
        rows=rows,
        config=cfg.to_dict(),
        extras={"mode_share": mode_share, "hhmm_histogram": dict(hhmm_counts)},
    )


def apply_evidence_to_default(evidence: DailyBoundaryEvidence) -> TradingDayConfig:
    """
    Return an updated TradingDayConfig from evidence without mutating globals.

    Caller may persist / use for closed-bar math when status == confirmed.
    """
    if evidence.status == "confirmed" and evidence.inferred_roll_time:
        return TradingDayConfig(
            reference_timezone=evidence.inferred_timezone,
            day_roll_time=evidence.inferred_roll_time,
            weekend_policy="skip_fabricate",
            source="native_tv_daily_opens_confirmed",
            extras={"evidence_status": "confirmed", "notes": list(evidence.notes)},
        )
    return TradingDayConfig(
        reference_timezone=DEFAULT_REFERENCE_TIMEZONE,
        day_roll_time=evidence.inferred_roll_time or DEFAULT_DAY_ROLL_TIME,
        weekend_policy="skip_fabricate",
        source="fx_session_hypothesis_pending_native_confirm",
        extras={
            "evidence_status": evidence.status,
            "notes": list(evidence.notes),
        },
    )
