"""Fetch multi-timeframe OHLC via chart resolution switching + restore."""

from __future__ import annotations

import asyncio
from typing import Any, Optional, Sequence

from bars import fetch_bars
from chart_timeframe import get_chart_resolution, set_chart_resolution
from multi_tf_bars import MultiTimeframeBars
from study_discovery import (
    KNOWN_STUDIES,
    StudyIdentity,
    compare_study_snapshots,
    rediscover_studies,
)
from timeframe import normalize_timeframe


async def _study_snapshot() -> dict[str, Any]:
    from cdp import evaluate_js

    js = """
(() => {
  const c = window.TradingViewApi?.activeChart?.();
  if (!c || typeof c.getAllStudies !== "function") {
    return { ok: false, error: "getAllStudies unavailable" };
  }
  try {
    const studies = c.getAllStudies() || [];
    return {
      ok: true,
      count: studies.length,
      studies: studies.map(s => ({
        id: s.id ?? s.entityId ?? null,
        name: s.name ?? s.title ?? null,
      })),
    };
  } catch (err) {
    return { ok: false, error: String(err) };
  }
})()
""".strip()
    raw = await evaluate_js(js)
    return raw if isinstance(raw, dict) else {"ok": False, "error": "bad study snapshot"}


def _identities_from_rediscover(payload: dict[str, Any]) -> list[StudyIdentity]:
    found = payload.get("found") or {}
    out: list[StudyIdentity] = []
    for key, row in found.items():
        out.append(
            StudyIdentity(
                semantic_key=key,
                name=str(row.get("name") or ""),
                study_id=row.get("study_id"),
                name_pattern=row.get("name_pattern"),
            )
        )
    return out


async def fetch_mtf_bar_bundle(
    timeframes: Sequence[str] = ("1D", "4H", "5m", "15m"),
    *,
    settle_ms: int = 1500,
) -> dict[str, Any]:
    """
    Switch chart TF for each requested timeframe, fetch bars, restore original.

    Returns MultiTimeframeBars payload + restore/study diagnostics.
    Tracks semantic study stability across each switch (IDs may churn).
    """
    original = await get_chart_resolution()
    orig_raw = original.get("resolution")
    studies_before_raw = await _study_snapshot()
    studies_before = rediscover_studies(
        studies_before_raw.get("studies") or [],
        known=KNOWN_STUDIES,
    )
    cached_ids = _identities_from_rediscover(studies_before)
    mtf = MultiTimeframeBars()
    fetched: dict[str, Any] = {}
    errors: list[dict[str, Any]] = []
    switch_log: list[dict[str, Any]] = []
    study_trail: list[dict[str, Any]] = [
        {"phase": "before", "rediscover": studies_before, "raw": studies_before_raw}
    ]

    try:
        for tf in timeframes:
            canon = normalize_timeframe(tf) or tf
            set_res = await set_chart_resolution(canon)
            if not set_res.get("ok"):
                errors.append({"timeframe": canon, "error": set_res.get("error")})
                switch_log.append(
                    {
                        "requested_timeframe": canon,
                        "success": False,
                        "error": set_res.get("error"),
                    }
                )
                continue
            await asyncio.sleep(settle_ms / 1000.0)
            after_tf = await get_chart_resolution()
            actual = after_tf.get("resolution")
            payload = await fetch_bars()
            snap_raw = await _study_snapshot()
            snap = rediscover_studies(
                snap_raw.get("studies") or [],
                known=KNOWN_STUDIES,
                previous=cached_ids,
            )
            cached_ids = _identities_from_rediscover(snap) or cached_ids
            study_trail.append(
                {
                    "phase": f"after_{canon}",
                    "rediscover": snap,
                    "compare_to_before": compare_study_snapshots(studies_before, snap),
                    "raw": snap_raw,
                }
            )
            bars = payload.get("bars") or []
            ok = bool(payload.get("ok"))
            if not ok:
                errors.append(
                    {"timeframe": canon, "error": payload.get("error") or "bars failed"}
                )
            else:
                mtf = mtf.with_series(
                    canon,
                    bars,
                    source="native",
                    extras={"resolution": payload.get("resolution")},
                )
                fetched[canon] = {
                    "bar_count": len(bars),
                    "resolution": payload.get("resolution"),
                    "first_time": None if not bars else int(bars[0].time),
                    "last_time": None if not bars else int(bars[-1].time),
                }
            switch_log.append(
                {
                    "requested_timeframe": canon,
                    "actual_timeframe_after_switch": actual,
                    "bars_returned": len(bars) if ok else 0,
                    "earliest_bar": None if not bars else int(bars[0].time),
                    "latest_bar": None if not bars else int(bars[-1].time),
                    "success": ok,
                    "studies": snap,
                }
            )
    finally:
        restore_ok = True
        if orig_raw is not None:
            restore = await set_chart_resolution(str(orig_raw))
            restore_ok = bool(restore.get("ok"))
            await asyncio.sleep(settle_ms / 1000.0)
        after = await get_chart_resolution()
        studies_after_raw = await _study_snapshot()
        studies_after = rediscover_studies(
            studies_after_raw.get("studies") or [],
            known=KNOWN_STUDIES,
            previous=cached_ids,
        )
        study_trail.append(
            {
                "phase": "after_restore",
                "rediscover": studies_after,
                "compare_to_before": compare_study_snapshots(studies_before, studies_after),
                "raw": studies_after_raw,
            }
        )

    return {
        "ok": len(errors) == 0 or bool(fetched),
        "mtf": mtf,
        "fetched": fetched,
        "errors": errors,
        "original_chart_timeframe": orig_raw,
        "chart_timeframe_after": after.get("resolution"),
        "restore_ok": restore_ok
        and str(after.get("resolution")) == str(orig_raw),
        "studies_before": studies_before,
        "studies_after": studies_after,
        "study_trail": study_trail,
        "switch_log": switch_log,
        "study_ids_changed": (
            studies_before.get("ok")
            and studies_after.get("ok")
            and any(
                (studies_before.get("found") or {}).get(k, {}).get("study_id")
                != (studies_after.get("found") or {}).get(k, {}).get("study_id")
                for k in set(studies_before.get("found") or {})
                | set(studies_after.get("found") or {})
            )
        ),
        "study_semantic_stable": compare_study_snapshots(
            studies_before, studies_after
        ).get("all_tracked_present_after"),
    }
