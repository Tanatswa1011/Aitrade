"""Phase 26 paper-trade model + append-only journal for frozen V2."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, Sequence

from gc_orb_engine import detect_roll_gap_timestamps, trading_dates_in_bars
from gc_vwap_engine import (
    analyze_candidate,
    collect_extension_sequences,
    compute_session_vwap_series,
    config_hash,
    evaluate_vwap_touch_after_entry,
    session_window,
    setup_to_entry_analysis,
)
from gc_vwap_freeze import (
    FROZEN_JSON,
    assert_runtime_matches_frozen,
    load_frozen_document,
    load_frozen_strategy_config,
)
from gc_vwap_models import GCVWAPStrategyConfig
from models import Bar
from outcome_engine import evaluate_entry_outcome

JOURNAL_DIR = Path("journal") / "phase26_gc_vwap_v2_paper"
PAPER_TRADES_PATH = JOURNAL_DIR / "paper_trades.jsonl"
DAILY_STATE_PATH = JOURNAL_DIR / "daily_state.jsonl"
TICK_SIZE = 0.1


@dataclass
class GCVWAPPaperTrade:
    paper_trade_id: str
    frozen_config_hash: str
    trading_date: str
    contract: str
    direction: str
    extension_event_id: str
    first_extension_timestamp: int
    reclaim_timestamp: Optional[int]
    entry_band_price: Optional[float]
    entry_trigger_timestamp: Optional[int]
    entry_price: Optional[float]
    theoretical_entry_price: Optional[float]
    paper_fill_price: Optional[float]
    fill_delta_points: Optional[float]
    fill_delta_ticks: Optional[float]
    stop_price: Optional[float]
    risk_points: Optional[float]
    target_1r: Optional[float]
    target_1_5r: Optional[float]
    target_2r: Optional[float]
    target_3r: Optional[float]
    vwap_at_entry: Optional[float]
    sigma_at_entry: Optional[float]
    z_at_extension: Optional[float]
    status: str
    outcome: Optional[str] = None
    mfe_points: Optional[float] = None
    mae_points: Optional[float] = None
    mfe_r: Optional[float] = None
    mae_r: Optional[float] = None
    vwap_hit: Optional[bool] = None
    vwap_hit_timestamp: Optional[int] = None
    hit_1r: Optional[bool] = None
    hit_1_5r: Optional[bool] = None
    hit_2r: Optional[bool] = None
    hit_3r: Optional[bool] = None
    stop_hit: Optional[bool] = None
    slippage_assumption_ticks: float = 1.0
    cost_assumption: str = "1_TICK_ADVERSE"
    created_at: str = ""
    updated_at: str = ""
    extras: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def paper_trade_id(trading_date: str, direction: str, extension_ts: int) -> str:
    return f"GC|V2|{trading_date}|{direction}|{extension_ts}"


def ensure_journal_dir() -> None:
    JOURNAL_DIR.mkdir(parents=True, exist_ok=True)
    if not PAPER_TRADES_PATH.exists():
        PAPER_TRADES_PATH.write_text("", encoding="utf-8")


def load_paper_trades(path: Path = PAPER_TRADES_PATH) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            out.append(json.loads(line))
    return out


def existing_ids(path: Path = PAPER_TRADES_PATH) -> set[str]:
    return {str(r.get("paper_trade_id")) for r in load_paper_trades(path) if r.get("paper_trade_id")}


def append_paper_trade(trade: GCVWAPPaperTrade, path: Path = PAPER_TRADES_PATH) -> bool:
    """Append if id not present. Returns True if written."""
    ensure_journal_dir()
    if trade.paper_trade_id in existing_ids(path):
        return False
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(trade.to_dict(), default=str) + "\n")
    return True


def is_short(direction: str) -> bool:
    return direction in ("bearish", "short")


def fill_price(
    theoretical: float,
    direction: str,
    *,
    ticks_adverse: float,
    tick: float = TICK_SIZE,
) -> float:
    """Adverse fill overlay — does not change setup selection."""
    delta = float(ticks_adverse) * float(tick)
    if is_short(direction):
        return float(theoretical) - delta  # short fills lower (worse)
    return float(theoretical) + delta  # long fills higher (worse)


def fill_sensitivity_overlay(
    theoretical: float,
    direction: str,
    risk: float,
) -> list[dict[str, Any]]:
    rows = []
    for name, ticks in (("IDEAL_TOUCH", 0.0), ("1_TICK_ADVERSE", 1.0), ("2_TICK_ADVERSE", 2.0)):
        fp = fill_price(theoretical, direction, ticks_adverse=ticks)
        friction_r = (2 * ticks * TICK_SIZE) / max(float(risk), TICK_SIZE) if risk else None
        rows.append(
            {
                "scenario": name,
                "ticks_adverse_entry": ticks,
                "paper_fill_price": fp,
                "fill_delta_points": abs(fp - theoretical),
                "illustrative_roundturn_friction_r": friction_r,
            }
        )
    return rows


def sample_label(resolved_n: int) -> str:
    if resolved_n < 20:
        return "VERY_EARLY"
    if resolved_n < 30:
        return "EARLY"
    if resolved_n < 50:
        return "MINIMUM_FORWARD_SAMPLE"
    if resolved_n < 100:
        return "MEANINGFUL_FORWARD_SAMPLE"
    return "LARGER_FORWARD_SAMPLE"


def paper_campaign_status(resolved_n: int, *, data_quality_issue: bool = False) -> str:
    if data_quality_issue:
        return "PAPER_DATA_QUALITY_ISSUE"
    if resolved_n < 30:
        return "PAPER_VALIDATION_IN_PROGRESS"
    return "FORWARD_SAMPLE_STILL_INSUFFICIENT"


def _status_from_setup(setup, seq: dict[str, Any]) -> str:
    if seq.get("roll_artifact"):
        return "INVALID"
    if setup.entry_triggered:
        return "ENTRY_TRIGGERED"
    if setup.reason == "no_confirmation":
        return "EXTENSION_FOUND"
    if setup.reason == "entry_timeout":
        return "WAITING_FOR_RETEST"
    if setup.state == "EXPIRED":
        return "EXPIRED"
    if setup.state == "INVALIDATED":
        return "INVALID"
    return str(setup.state or "OBSERVING")


def run_frozen_v2_on_bars(
    bars: Sequence[Bar],
    *,
    contract: str = "GC",
    fill_ticks_adverse: float = 1.0,
    persist: bool = False,
    cfg_override: Optional[GCVWAPStrategyConfig] = None,
) -> dict[str, Any]:
    """
    Replay frozen V2 semantics on bars (historical equivalence / paper seed).
    Refuses to run if runtime config ≠ frozen file.
    """
    if not FROZEN_JSON.exists():
        return {"ok": False, "error_code": "MISSING_FROZEN_FILE"}

    doc = load_frozen_document()
    cfg = cfg_override or load_frozen_strategy_config(doc)
    check = assert_runtime_matches_frozen(cfg, doc)
    if not check.get("ok"):
        return {"ok": False, "error_code": "FROZEN_CONFIG_MISMATCH", "check": check}

    if float(cfg.sigma_threshold) != 2.0 or cfg.entry_mode != "FROZEN_2SIG_RETEST":
        return {"ok": False, "error_code": "FROZEN_CONFIG_MISMATCH", "reason": "param_guard"}

    ordered = sorted(bars, key=lambda b: int(b.time))
    roll = detect_roll_gap_timestamps(ordered)
    trades: list[GCVWAPPaperTrade] = []
    written = 0
    now = datetime.now(tz=timezone.utc).isoformat()

    for td in trading_dates_in_bars(ordered):
        seqs = collect_extension_sequences(ordered, td, roll_flags=roll)
        states = {int(s.timestamp): s for s in compute_session_vwap_series(ordered, td)}
        _, session_end, _ = session_window(td)
        for seq in seqs:
            setup = analyze_candidate(seq, cfg)
            eid = setup.vwap_extension_event_id
            pid = paper_trade_id(td, setup.direction, int(seq["first_ts"]))
            status = _status_from_setup(setup, seq)

            theoretical = setup.entry_price
            pfill = fdelta = fticks = None
            if theoretical is not None and setup.entry_triggered:
                pfill = fill_price(float(theoretical), setup.direction, ticks_adverse=fill_ticks_adverse)
                fdelta = abs(float(pfill) - float(theoretical))
                fticks = fdelta / TICK_SIZE

            outcome = None
            mfe_r = mae_r = mfe_p = mae_p = None
            hit_1 = hit_15 = hit_2 = hit_3 = stop_hit = None
            vwap_hit = vwap_ts = None
            targets: dict[float, Optional[float]] = {1.0: None, 1.5: None, 2.0: None, 3.0: None}

            if setup.entry_triggered and setup.risk_valid:
                analysis = setup_to_entry_analysis(setup)
                er = evaluate_entry_outcome(
                    analysis, ordered, direction=setup.direction, horizon_end_ts=session_end
                )
                outcome = er.outcome
                mfe_r, mae_r = er.mfe_r, er.mae_r
                mfe_p, mae_p = er.max_favorable_excursion, er.max_adverse_excursion
                if mfe_r is not None:
                    hit_1 = float(mfe_r) >= 1.0
                    hit_15 = float(mfe_r) >= 1.5
                    hit_2 = float(mfe_r) >= 2.0
                    hit_3 = float(mfe_r) >= 3.0
                stop_hit = outcome == "STOP_HIT"
                if outcome == "AMBIGUOUS_INTRABAR":
                    status = "AMBIGUOUS"
                elif stop_hit:
                    status = "STOP_HIT"
                elif hit_3:
                    status = "TARGET_3R_HIT"
                elif hit_2:
                    status = "TARGET_2R_HIT"
                elif hit_15:
                    status = "TARGET_1_5R_HIT"
                elif hit_1:
                    status = "TARGET_1R_HIT"
                elif outcome == "EXPIRED_WITHOUT_EXIT":
                    status = "EXPIRED"
                vt = evaluate_vwap_touch_after_entry(
                    bars=ordered,
                    trading_date=td,
                    entry_ts=int(setup.entry_timestamp or 0),
                    direction=setup.direction,
                    stop_price=float(setup.stop_price or 0),
                    session_end=session_end,
                )
                vwap_hit = bool(vt.get("vwap_hit"))
                vwap_ts = vt.get("timestamp")
                if vwap_hit and status == "ENTRY_TRIGGERED":
                    status = "VWAP_HIT"
                for t in setup.targets or []:
                    targets[float(t.get("rr"))] = float(t.get("price"))

            st_entry = states.get(int(setup.entry_timestamp)) if setup.entry_timestamp else None
            trade = GCVWAPPaperTrade(
                paper_trade_id=pid,
                frozen_config_hash=str(doc["frozen_config_hash"]),
                trading_date=td,
                contract=contract,
                direction=setup.direction,
                extension_event_id=eid,
                first_extension_timestamp=int(seq["first_ts"]),
                reclaim_timestamp=(setup.extras or {}).get("confirmation_timestamp"),
                entry_band_price=seq.get("frozen_2sig"),
                entry_trigger_timestamp=setup.entry_timestamp,
                entry_price=setup.entry_price,
                theoretical_entry_price=theoretical,
                paper_fill_price=pfill,
                fill_delta_points=fdelta,
                fill_delta_ticks=fticks,
                stop_price=setup.stop_price,
                risk_points=setup.risk_distance,
                target_1r=targets.get(1.0),
                target_1_5r=targets.get(1.5),
                target_2r=targets.get(2.0),
                target_3r=targets.get(3.0),
                vwap_at_entry=None if st_entry is None else st_entry.vwap,
                sigma_at_entry=None if st_entry is None else st_entry.session_std,
                z_at_extension=float(seq.get("first_z") or 0.0),
                status=status,
                outcome=outcome,
                mfe_points=mfe_p,
                mae_points=mae_p,
                mfe_r=mfe_r,
                mae_r=mae_r,
                vwap_hit=vwap_hit,
                vwap_hit_timestamp=vwap_ts,
                hit_1r=hit_1,
                hit_1_5r=hit_15,
                hit_2r=hit_2,
                hit_3r=hit_3,
                stop_hit=stop_hit,
                slippage_assumption_ticks=float(fill_ticks_adverse),
                cost_assumption=(
                    "IDEAL_TOUCH"
                    if fill_ticks_adverse == 0
                    else ("1_TICK_ADVERSE" if fill_ticks_adverse == 1 else f"{fill_ticks_adverse}_TICK_ADVERSE")
                ),
                created_at=now,
                updated_at=now,
                extras={
                    "setup_state": setup.state,
                    "setup_reason": setup.reason,
                    "engine_config_hash": config_hash(cfg),
                    "max_abs_z": seq.get("max_abs_z"),
                },
            )
            trades.append(trade)
            if persist and setup.entry_triggered and setup.risk_valid:
                if append_paper_trade(trade):
                    written += 1

    resolved = [
        t
        for t in trades
        if t.status
        in (
            "STOP_HIT",
            "TARGET_1R_HIT",
            "TARGET_1_5R_HIT",
            "TARGET_2R_HIT",
            "TARGET_3R_HIT",
            "VWAP_HIT",
        )
        or (t.outcome and t.outcome not in ("AMBIGUOUS_INTRABAR", "EXPIRED_WITHOUT_EXIT"))
    ]
    # Prefer explicit resolved via stop/target outcomes excluding ambiguous
    resolved_n = sum(
        1
        for t in trades
        if t.outcome in ("STOP_HIT", "TARGET_HIT")
        or (t.stop_hit is True)
        or (t.hit_1r is True and t.outcome and t.outcome != "AMBIGUOUS_INTRABAR")
        or t.status
        in (
            "STOP_HIT",
            "TARGET_1R_HIT",
            "TARGET_1_5R_HIT",
            "TARGET_2R_HIT",
            "TARGET_3R_HIT",
        )
    )

    return {
        "ok": True,
        "frozen_config_hash": doc["frozen_config_hash"],
        "trades": trades,
        "trade_count": len(trades),
        "triggered": sum(1 for t in trades if t.entry_price is not None),
        "resolved_n": resolved_n,
        "written": written,
        "journal": str(PAPER_TRADES_PATH).replace("\\", "/"),
        "sample_label": sample_label(0),
        "campaign_status": paper_campaign_status(0),
        "engine_config_hash": config_hash(cfg),
    }


def summarize_paper_journal(path: Path = PAPER_TRADES_PATH) -> dict[str, Any]:
    rows = load_paper_trades(path)
    triggered = [r for r in rows if r.get("entry_price") is not None]
    ambiguous = [r for r in rows if r.get("status") == "AMBIGUOUS" or r.get("outcome") == "AMBIGUOUS_INTRABAR"]
    expired = [r for r in rows if r.get("status") == "EXPIRED"]
    invalid = [r for r in rows if r.get("status") == "INVALID"]
    resolved = [
        r
        for r in triggered
        if r.get("status")
        in (
            "STOP_HIT",
            "TARGET_1R_HIT",
            "TARGET_1_5R_HIT",
            "TARGET_2R_HIT",
            "TARGET_3R_HIT",
            "VWAP_HIT",
        )
        or r.get("outcome") in ("STOP_HIT", "TARGET_HIT")
    ]
    # Exclude ambiguous from expectancy denominator
    resolved_clean = [r for r in resolved if r not in ambiguous]
    n = len(resolved_clean)
    return {
        "paper_trades": len(rows),
        "triggered": len(triggered),
        "resolved": n,
        "ambiguous": len(ambiguous),
        "expired": len(expired),
        "invalid": len(invalid),
        "sample_label": sample_label(n),
        "campaign_status": paper_campaign_status(n),
        "rows": rows,
    }
