"""Phase 6 validation: RiskPlan + TargetPlan for triggered entries."""

from __future__ import annotations

import json
from pathlib import Path

from entry_detect import evaluate_entry_modes
from fvg_config import DEFAULT_FVG_CONFIG
from fvg_detect import detect_fvg
from models import Bar, LiquiditySweep, SessionRange, StructureConfirmation
from risk_config import DEFAULT_RISK_CONFIG, DEFAULT_TARGET_CONFIG
from risk_plan import build_risk_plan, sweep_extreme
from sessions_config import SESSION_DST_UNCERTAINTY
from target_plan import build_target_plan


def _fmt_plan(risk, targets) -> dict:
    return {
        "risk": {
            "valid": risk.valid,
            "stop_mode": risk.stop_mode,
            "entry_price": risk.entry_price,
            "stop_price": risk.stop_price,
            "risk_distance": risk.risk_distance,
            "risk_points": risk.risk_points,
            "invalidation_reason": risk.invalidation_reason,
            "sweep_extreme": (risk.extras or {}).get("sweep_extreme"),
            "entry_depth": (risk.extras or {}).get("entry_depth"),
        },
        "targets": {
            "valid": targets.valid,
            "fixed_rr": [t.to_dict() for t in targets.fixed_rr_targets],
            "opposite_liquidity_label": targets.opposite_liquidity_label,
            "opposite_liquidity_price": targets.opposite_liquidity_price,
            "rr_to_opposite": targets.rr_to_opposite,
            "opposite_target_valid": targets.opposite_target_valid,
        },
    }


def offline_fixture() -> dict:
    sweep = LiquiditySweep(
        session="Asia",
        side="low",
        level=4311.04,
        sweep_timestamp=1_000,
        sweep_price=4310.0,
        maximum_excursion=1.04,
        reclaim_status=True,
        rule="wick_only",
        sweep_candle=Bar(time=1_000, open=4312, high=4313, low=4310, close=4312),
    )
    conf = StructureConfirmation(
        kind="CHoCH",
        direction="bullish",
        level=4320.0,
        event_timestamp=2_000,
        event_bar_index=10,
        source="luxalgo",
        study_id="smUEv2",
        raw_id="fixture",
        timing_confidence="exact",
    )
    bars = [
        Bar(time=1_000, open=4312, high=4313, low=4310, close=4312),
        Bar(time=2_000, open=4315, high=4322, low=4314, close=4320),
        Bar(time=3_000, open=4321, high=4325, low=4320, close=4324),
        Bar(time=4_000, open=4324, high=4340, low=4323, close=4338),
        Bar(time=5_000, open=4338, high=4345, low=4330, close=4342),  # FVG 4325-4330
        Bar(time=6_000, open=4342, high=4343, low=4328, close=4329),  # boundary/first touch
        Bar(time=7_000, open=4329, high=4330, low=4326, close=4327),  # CE reach
    ]
    fvg = detect_fvg(sweep, conf, bars, DEFAULT_FVG_CONFIG).zones[0]
    modes = evaluate_entry_modes(fvg, bars, ("boundary", "ce"))

    session = SessionRange(
        name="Asia",
        timezone="America/New_York",
        start=0,
        end=1_000,
        high=4360.0,
        low=4311.04,
        high_timestamp=None,
        low_timestamp=None,
        complete=True,
        source="ict_sessions",
        coverage_status="full",
        identity="Asia:fixture",
    )

    comparison = {}
    for mode, entry in modes.items():
        risk = build_risk_plan(sweep, fvg, entry, bars, DEFAULT_RISK_CONFIG)
        targets = build_target_plan(
            session, sweep, entry, risk, DEFAULT_TARGET_CONFIG
        )
        comparison[mode] = {
            "entry": {
                "mode": entry.mode,
                "price": entry.price,
                "triggered": entry.triggered,
                "entry_depth": entry.entry_depth,
                "max_retrace_depth": entry.max_retrace_depth,
            },
            **_fmt_plan(risk, targets),
        }

    ce = comparison["ce"]
    return {
        "mode": "offline_fixture",
        "note": "Deterministic fixture (not live CDP). Compares boundary vs CE risk/RR.",
        "session": "Asia",
        "sweep": {
            "side": "low",
            "level": sweep.level,
            "sweep_extreme": sweep_extreme(sweep),
            "time": sweep.sweep_timestamp,
        },
        "choch": {"direction": "bullish", "level": conf.level},
        "fvg": {"low": fvg.low, "high": fvg.high, "ce": fvg.midpoint},
        "primary_example_ce": ce,
        "boundary_vs_ce": comparison,
        "risk_config": DEFAULT_RISK_CONFIG.to_dict(),
        "target_config": DEFAULT_TARGET_CONFIG.to_dict(),
    }


def main():
    fixture = offline_fixture()
    report = {
        "ok": True,
        "phase": 6,
        "session_dst_uncertainty": SESSION_DST_UNCERTAINTY,
        "example": fixture,
    }
    Path("phase6_validation.json").write_text(
        json.dumps(report, indent=2, default=str), encoding="utf-8"
    )
    print(json.dumps(report, indent=2, default=str))


if __name__ == "__main__":
    main()
