"""Pure AnnotationPlan: TradeSetup → desired chart annotations (no CDP)."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Optional, Sequence, Union

from models import EntryAnalysis, TradeSetup


@dataclass(frozen=True)
class PlannedAnnotation:
    """One intended drawing derived from canonical TradeSetup fields."""

    kind: str  # horizontal_line | rectangle | status
    role: str
    label: str
    price: Optional[float] = None
    price_secondary: Optional[float] = None  # rectangle opposite price
    time: Optional[int] = None
    time_secondary: Optional[int] = None
    color: str = "#546E7A"
    linewidth: int = 2
    extras: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class AnnotationPlan:
    """Deterministic annotation intent for one TradeSetup."""

    setup_id: str
    status: str
    direction: Optional[str]
    session: str
    items: list[PlannedAnnotation]
    skipped: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "setup_id": self.setup_id,
            "status": self.status,
            "direction": self.direction,
            "session": self.session,
            "items": [i.to_dict() for i in self.items],
            "skipped": list(self.skipped),
            "item_count": len(self.items),
            "skipped_count": len(self.skipped),
        }


# Readable, non-purple palette
COLOR_SESSION = "#607D8B"
COLOR_SWEPT = "#FF6F00"
COLOR_CHOCH_BULL = "#2E7D32"
COLOR_CHOCH_BEAR = "#C62828"
COLOR_FVG_BULL = "#00897B"
COLOR_FVG_BEAR = "#D84315"
COLOR_CE = "#F9A825"
COLOR_ENTRY = "#1565C0"
COLOR_STOP = "#AD1457"
COLOR_TARGET = "#6A1B9A"
COLOR_OPPOSITE = "#00838F"
COLOR_STATUS = "#455A64"

ENTRY_MODE_LABELS = {
    "first_touch": "First Touch",
    "boundary": "Boundary",
    "ce": "CE",
}


def _as_setup(setup: Union[TradeSetup, dict[str, Any]]) -> dict[str, Any]:
    if isinstance(setup, TradeSetup):
        return setup.to_dict()
    return dict(setup)


def _entries(setup_d: dict[str, Any]) -> list[dict[str, Any]]:
    raw = setup_d.get("entries") or []
    out = []
    for item in raw:
        if isinstance(item, EntryAnalysis):
            out.append(item.to_dict())
        elif isinstance(item, dict):
            out.append(item)
    return out


def _filter_modes(
    entries: Sequence[dict[str, Any]], entry_mode: str
) -> list[dict[str, Any]]:
    mode = (entry_mode or "all").strip().lower()
    if mode == "all":
        return list(entries)
    return [e for e in entries if (e.get("entry") or {}).get("mode") == mode]


def plan_annotations(
    setup: Union[TradeSetup, dict[str, Any]],
    *,
    entry_mode: str = "all",
    show_fixed_rr: bool = True,
    show_opposite_liquidity: bool = True,
    fixed_rr_to_show: Optional[Sequence[float]] = None,
) -> AnnotationPlan:
    """
    Convert TradeSetup → AnnotationPlan.

    Skips missing/unreliable fields; never invents prices or times.
    """
    d = _as_setup(setup)
    setup_id = str(d.get("id") or "unknown")
    status = str(d.get("status") or "NO_SETUP")
    direction = d.get("direction")
    session = str(d.get("session") or "")
    items: list[PlannedAnnotation] = []
    skipped: list[dict[str, Any]] = []

    def skip(role: str, reason: str) -> None:
        skipped.append({"role": role, "reason": reason})

    # Status label (price-less — rendered as a line on last known session mid if available)
    sr = d.get("session_range") or {}
    status_price = None
    if sr.get("high") is not None and sr.get("low") is not None:
        status_price = (float(sr["high"]) + float(sr["low"])) / 2.0
    if status_price is not None:
        status_label = f"AITRADE · {status}"
        expiry_reason = d.get("expiry_reason")
        if status == "EXPIRED" and expiry_reason:
            status_label = f"AITRADE · EXPIRED · {expiry_reason}"
        htf = d.get("higher_timeframe_context") or {}
        daily = (htf.get("daily_bias") or {}).get("direction") if isinstance(htf, dict) else None
        h4 = (htf.get("h4_bias") or {}).get("direction") if isinstance(htf, dict) else None
        exec_tf = d.get("execution_timeframe")
        if daily or h4 or exec_tf:
            bits = []
            if daily and str(daily).lower() != "unknown":
                bits.append(
                    "D:"
                    + {
                        "bullish": "Bull",
                        "bearish": "Bear",
                        "neutral": "Neu",
                    }.get(str(daily).lower(), str(daily)[:3])
                )
            if h4 and str(h4).lower() != "unknown":
                bits.append(
                    "4H:"
                    + {
                        "bullish": "Bull",
                        "bearish": "Bear",
                        "neutral": "Neu",
                    }.get(str(h4).lower(), str(h4)[:3])
                )
            if exec_tf:
                bits.append(f"Exec:{exec_tf}")
            if bits:
                status_label = f"{status_label} · {' '.join(bits)}"
        items.append(
            PlannedAnnotation(
                kind="horizontal_line",
                role="status",
                label=status_label,
                price=status_price,
                color=COLOR_STATUS,
                linewidth=1,
                extras={"style": "status", "expiry_reason": expiry_reason},
            )
        )
    else:
        skip("status", "no_session_midpoint_for_status_anchor")

    # Session high/low
    if sr.get("high") is not None:
        items.append(
            PlannedAnnotation(
                kind="horizontal_line",
                role="session_high",
                label=f"{session} High",
                price=float(sr["high"]),
                color=COLOR_SESSION,
            )
        )
    else:
        skip("session_high", "missing")
    if sr.get("low") is not None:
        items.append(
            PlannedAnnotation(
                kind="horizontal_line",
                role="session_low",
                label=f"{session} Low",
                price=float(sr["low"]),
                color=COLOR_SESSION,
            )
        )
    else:
        skip("session_low", "missing")

    # Sweep
    sweep = d.get("sweep")
    if sweep and sweep.get("level") is not None:
        side = str(sweep.get("side") or "").lower()
        side_label = "High" if side == "high" else "Low" if side == "low" else side
        extreme = (d.get("source_metadata") or {}).get("sweep_extreme")
        price = float(extreme) if extreme is not None else float(sweep["level"])
        items.append(
            PlannedAnnotation(
                kind="horizontal_line",
                role="sweep",
                label=f"{session} {side_label} — SWEPT",
                price=price,
                color=COLOR_SWEPT,
                linewidth=3,
                time=sweep.get("sweep_timestamp")
                if isinstance(sweep.get("sweep_timestamp"), int)
                else None,
                extras={"sweep_timestamp": sweep.get("sweep_timestamp")},
            )
        )
        if not isinstance(sweep.get("sweep_timestamp"), int):
            skip("sweep_time_marker", "no_reliable_sweep_timestamp")
    elif status not in (
        "WAITING_FOR_SESSION",
        "WAITING_FOR_SWEEP",
        "NO_SETUP",
        "SESSION_RANGE_COMPLETE",
    ):
        skip("sweep", "missing_sweep_on_advanced_status")

    # CHoCH — only when present on setup
    conf = d.get("confirmation")
    if conf and conf.get("level") is not None:
        cdir = str(conf.get("direction") or direction or "").lower()
        color = COLOR_CHOCH_BULL if cdir == "bullish" else COLOR_CHOCH_BEAR
        items.append(
            PlannedAnnotation(
                kind="horizontal_line",
                role="choch",
                label=f"{cdir.title()} CHoCH" if cdir else "CHoCH",
                price=float(conf["level"]),
                color=color,
                time=conf.get("event_timestamp")
                if conf.get("timing_confidence") in ("exact", "derived")
                and isinstance(conf.get("event_timestamp"), int)
                else None,
            )
        )
        if conf.get("timing_confidence") == "unavailable" or conf.get(
            "event_timestamp"
        ) is None:
            skip("choch_time_marker", "unreliable_or_missing_choch_timing")
    elif status in (
        "WAITING_FOR_FVG",
        "FVG_FOUND",
        "WAITING_FOR_RETRACE",
        "ENTRY_READY",
        "INVALIDATED",
    ):
        skip("choch", "missing_confirmation")

    # FVG
    fvg = d.get("fvg")
    if fvg and fvg.get("high") is not None and fvg.get("low") is not None:
        fdir = str(fvg.get("direction") or direction or "").lower()
        color = COLOR_FVG_BULL if fdir == "bullish" else COLOR_FVG_BEAR
        t1 = fvg.get("candle1_timestamp")
        t2 = fvg.get("created_timestamp") or fvg.get("candle3_timestamp")
        can_rect = isinstance(t1, int) and isinstance(t2, int) and t2 > t1
        if can_rect:
            items.append(
                PlannedAnnotation(
                    kind="rectangle",
                    role="fvg_zone",
                    label=f"{fdir.title()} FVG" if fdir else "FVG",
                    price=float(fvg["high"]),
                    price_secondary=float(fvg["low"]),
                    time=int(t1),
                    time_secondary=int(t2),
                    color=color,
                )
            )
        else:
            skip("fvg_rectangle", "missing_reliable_fvg_time_bounds")
            items.append(
                PlannedAnnotation(
                    kind="horizontal_line",
                    role="fvg_high",
                    label=f"{fdir.title()} FVG High" if fdir else "FVG High",
                    price=float(fvg["high"]),
                    color=color,
                )
            )
            items.append(
                PlannedAnnotation(
                    kind="horizontal_line",
                    role="fvg_low",
                    label=f"{fdir.title()} FVG Low" if fdir else "FVG Low",
                    price=float(fvg["low"]),
                    color=color,
                )
            )
        if fvg.get("midpoint") is not None:
            items.append(
                PlannedAnnotation(
                    kind="horizontal_line",
                    role="fvg_ce",
                    label="CE",
                    price=float(fvg["midpoint"]),
                    color=COLOR_CE,
                    linewidth=1,
                )
            )
    elif status in ("WAITING_FOR_RETRACE", "ENTRY_READY"):
        skip("fvg", "missing_fvg")

    # Entries / stops / targets — only triggered with prices
    entries = _filter_modes(_entries(d), entry_mode)
    rr_filter = None
    if fixed_rr_to_show is not None:
        rr_filter = {float(x) for x in fixed_rr_to_show}

    stop_groups: dict[float, list[str]] = {}
    for block in entries:
        entry = block.get("entry") or {}
        if not entry.get("triggered") or entry.get("status") != "triggered":
            continue
        mode = str(entry.get("mode") or "")
        mode_label = ENTRY_MODE_LABELS.get(mode, mode)
        if entry.get("price") is None:
            skip(f"entry:{mode}", "missing_entry_price")
            continue
        items.append(
            PlannedAnnotation(
                kind="horizontal_line",
                role=f"entry:{mode}",
                label=f"Entry · {mode_label}",
                price=float(entry["price"]),
                color=COLOR_ENTRY,
                time=entry.get("trigger_timestamp")
                if isinstance(entry.get("trigger_timestamp"), int)
                else None,
            )
        )

        risk = block.get("risk") or {}
        if risk.get("valid") and risk.get("stop_price") is not None:
            sp = float(risk["stop_price"])
            stop_groups.setdefault(sp, []).append(mode_label)
        elif entry.get("triggered"):
            skip(f"stop:{mode}", "risk_invalid_or_missing_stop")

        target = block.get("target") or {}
        if show_fixed_rr and target.get("valid"):
            for ft in target.get("fixed_rr_targets") or []:
                rr = float(ft.get("rr"))
                if rr_filter is not None and rr not in rr_filter:
                    continue
                if ft.get("price") is None:
                    continue
                items.append(
                    PlannedAnnotation(
                        kind="horizontal_line",
                        role=f"target_rr:{mode}:{rr:g}",
                        label=f"{mode_label} {rr:g}R",
                        price=float(ft["price"]),
                        color=COLOR_TARGET,
                        linewidth=1,
                    )
                )

        if show_opposite_liquidity and target.get("opposite_target_valid"):
            label = target.get("opposite_liquidity_label") or "Opposing Liquidity"
            price = target.get("opposite_liquidity_price")
            if price is not None:
                # Dedupe opposite by price later
                items.append(
                    PlannedAnnotation(
                        kind="horizontal_line",
                        role="opposite_liquidity",
                        label=f"{label} · Opposing Liquidity",
                        price=float(price),
                        color=COLOR_OPPOSITE,
                        linewidth=2,
                    )
                )

    # Deduplicate shared stops into one line
    for price, modes in stop_groups.items():
        uniq = sorted(set(modes))
        if len(uniq) == 1:
            label = f"SL · {uniq[0]}"
        else:
            label = "SL · " + " / ".join(uniq)
        items.append(
            PlannedAnnotation(
                kind="horizontal_line",
                role="stop",
                label=label,
                price=price,
                color=COLOR_STOP,
                linewidth=2,
            )
        )

    # Deduplicate opposite liquidity by price
    opp_seen: set[float] = set()
    deduped: list[PlannedAnnotation] = []
    for it in items:
        if it.role == "opposite_liquidity":
            if it.price is None or it.price in opp_seen:
                continue
            opp_seen.add(it.price)
        deduped.append(it)

    return AnnotationPlan(
        setup_id=setup_id,
        status=status,
        direction=None if direction is None else str(direction),
        session=session,
        items=deduped,
        skipped=skipped,
    )
