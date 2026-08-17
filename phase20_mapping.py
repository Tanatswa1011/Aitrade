"""Map LuxAlgo CHoCH events onto canonical TradingView/OANDA bars."""

from __future__ import annotations

from typing import Any, Mapping, Optional, Sequence

from models import Bar, StructureConfirmation


def _bar_times(bars: Sequence[Bar]) -> list[int]:
    return [int(b.time) for b in bars]


def map_luxalgo_event_to_bars(
    event: StructureConfirmation | dict[str, Any],
    bars: Sequence[Bar],
    *,
    bars_by_series_index: Optional[Mapping[Any, int]] = None,
    period_sec: int = 300,
    allow_nearest: bool = False,
    nearest_max_bars: int = 1,
) -> dict[str, Any]:
    """
    Mapping precedence:
      1. exact timestamp present on a bar
      2. exact bar index → timestamp via bars_by_series_index or bars[i]
      3. paired line/label anchor already resolved into event_timestamp
      4. optional derived nearest valid bar (explicit; never silent)
    Never silently snaps when confidence is inadequate.
    """
    if isinstance(event, StructureConfirmation):
        direction = event.direction
        level = event.level
        ts = event.event_timestamp
        bar_idx = event.event_bar_index
        timing = event.timing_confidence
        extras = dict(event.extras or {})
    else:
        direction = event.get("direction")
        level = event.get("level")
        ts = event.get("event_timestamp", event.get("timestamp"))
        bar_idx = event.get("event_bar_index", event.get("bar_index"))
        timing = event.get("timing_confidence") or "unavailable"
        extras = dict(event.get("raw_metadata") or event.get("extras") or {})

    times = _bar_times(bars)
    time_set = set(times)
    result: dict[str, Any] = {
        "direction": direction,
        "level": level,
        "timing_confidence": timing,
        "mapping_status": "unresolved",
        "mapping_method": None,
        "mapped_timestamp": None,
        "mapped_bar_index": None,
    }

    # Reject known placeholder indexes for exact-time claims
    if bar_idx is not None and int(bar_idx) < 0:
        result["mapping_status"] = "unresolved"
        result["mapping_method"] = "placeholder_bar_index_rejected"
        result["note"] = "bogus_or_placeholder_index"
        return result

    if ts is not None and timing in ("exact", "derived") and int(ts) in time_set:
        mi = times.index(int(ts))
        result.update(
            {
                "mapping_status": "mapped",
                "mapping_method": "exact_timestamp",
                "mapped_timestamp": int(ts),
                "mapped_bar_index": mi,
            }
        )
        return result

    # Bar-index → timestamp via series map
    if bar_idx is not None and bars_by_series_index:
        mapped_ts = bars_by_series_index.get(int(bar_idx))
        if mapped_ts is None:
            mapped_ts = bars_by_series_index.get(str(bar_idx))
        if mapped_ts is not None and int(mapped_ts) in time_set:
            mi = times.index(int(mapped_ts))
            result.update(
                {
                    "mapping_status": "mapped",
                    "mapping_method": "bar_index_to_timestamp",
                    "mapped_timestamp": int(mapped_ts),
                    "mapped_bar_index": mi,
                    "timing_confidence": "derived" if timing == "unavailable" else timing,
                }
            )
            return result

    # Direct list index if bar_idx is a positional index into the provided window
    if bar_idx is not None and 0 <= int(bar_idx) < len(bars):
        b = bars[int(bar_idx)]
        result.update(
            {
                "mapping_status": "mapped",
                "mapping_method": "positional_bar_index",
                "mapped_timestamp": int(b.time),
                "mapped_bar_index": int(bar_idx),
                "timing_confidence": "derived" if timing == "unavailable" else timing,
            }
        )
        return result

    # Paired line/label anchor already in timestamp but not on bar list → unresolved
    if ts is not None and timing in ("exact", "derived") and int(ts) not in time_set:
        if allow_nearest and times:
            nearest_i = min(range(len(times)), key=lambda i: abs(times[i] - int(ts)))
            bar_dist = abs(times[nearest_i] - int(ts)) / max(1, period_sec)
            if bar_dist <= nearest_max_bars:
                result.update(
                    {
                        "mapping_status": "mapped",
                        "mapping_method": "derived_nearest_valid_bar",
                        "mapped_timestamp": times[nearest_i],
                        "mapped_bar_index": nearest_i,
                        "timing_confidence": "derived",
                        "nearest_bar_distance": bar_dist,
                        "original_timestamp": int(ts),
                    }
                )
                return result
        result.update(
            {
                "mapping_status": "unresolved",
                "mapping_method": "timestamp_not_in_bar_window",
                "original_timestamp": int(ts),
            }
        )
        return result

    # Optional nearest only when caller opts in AND we have some temporal hint
    if allow_nearest and ts is not None and times:
        nearest_i = min(range(len(times)), key=lambda i: abs(times[i] - int(ts)))
        bar_dist = abs(times[nearest_i] - int(ts)) / max(1, period_sec)
        if bar_dist <= nearest_max_bars:
            result.update(
                {
                    "mapping_status": "mapped",
                    "mapping_method": "derived_nearest_valid_bar",
                    "mapped_timestamp": times[nearest_i],
                    "mapped_bar_index": nearest_i,
                    "timing_confidence": "derived",
                    "nearest_bar_distance": bar_dist,
                    "original_timestamp": int(ts),
                }
            )
            return result

    _ = extras  # retained for future paired-line diagnostics
    return result


def apply_mapping_to_confirmation(
    event: StructureConfirmation,
    mapping: dict[str, Any],
) -> StructureConfirmation:
    """Return a confirmation with mapped timestamp when status=mapped."""
    if mapping.get("mapping_status") != "mapped" or mapping.get("mapped_timestamp") is None:
        return event
    extras = dict(event.extras or {})
    extras["mapping_method"] = mapping.get("mapping_method")
    extras["mapping_status"] = mapping.get("mapping_status")
    return StructureConfirmation(
        kind=event.kind,
        direction=event.direction,
        level=event.level,
        event_timestamp=int(mapping["mapped_timestamp"]),
        event_bar_index=mapping.get("mapped_bar_index", event.event_bar_index),
        source=event.source,
        study_id=event.study_id,
        raw_id=event.raw_id,
        timing_confidence=str(
            mapping.get("timing_confidence") or event.timing_confidence
        ),
        extras=extras,
    )
