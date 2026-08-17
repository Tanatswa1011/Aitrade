"""Phase 10 CLI: historical replay + journal + stats."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import Any, Optional

from historical_structure_config import DEFAULT_HISTORICAL_STRUCTURE_CONFIG
from models import RiskConfig
from replay_engine import replay_historical_setups
from replay_stats import compare_stop_modes, compute_replay_statistics
from setup_journal import append_journal_records
from strategy_config import DEFAULT_STRATEGY_CONFIG, StrategyConfig


def _parse_sessions(raw: str) -> tuple[str, ...]:
    parts = [p.strip().capitalize() for p in (raw or "asia,london").split(",") if p.strip()]
    out = []
    for p in parts:
        if p.lower() == "asia":
            out.append("Asia")
        elif p.lower() == "london":
            out.append("London")
    return tuple(out) or ("Asia", "London")


def _with_stop_mode(stop_mode: str) -> StrategyConfig:
    base = DEFAULT_STRATEGY_CONFIG
    risk = RiskConfig(
        stop_mode=stop_mode,
        stop_buffer_price=base.risk.stop_buffer_price,
        stop_buffer_points=base.risk.stop_buffer_points,
        point_size=base.risk.point_size,
        invalidate_before_entry=base.risk.invalidate_before_entry,
    )
    return StrategyConfig(
        sweep_rule=base.sweep_rule,
        entry_modes=base.entry_modes,
        fvg=base.fvg,
        entry=base.entry,
        risk=risk,
        target=base.target,
        expiry=base.expiry,
        prefer_completed_sessions_only=base.prefer_completed_sessions_only,
        session_confidence=dict(base.session_confidence),
        dst_uncertainty=base.dst_uncertainty,
    )


async def _load_bars_from_cdp() -> dict[str, Any]:
    from bars import fetch_bars

    return await fetch_bars()


def run_fixture_replay() -> dict[str, Any]:
    """Deterministic offline replay used when CDP history is insufficient."""
    from datetime import date, timedelta

    from confirmation_provider import HistoricalStructureProvider
    from models import Bar
    from replay_fixtures import build_multi_day_fixture_bars

    bars = build_multi_day_fixture_bars()
    result = replay_historical_setups(
        bars,
        symbol="OANDA:XAUUSD",
        timeframe="5",
        confirmation_provider=HistoricalStructureProvider(
            DEFAULT_HISTORICAL_STRUCTURE_CONFIG
        ),
        session_names=("Asia", "London"),
    )
    stats = compute_replay_statistics(result.journal_records)
    path = append_journal_records(
        result.journal_records, root=Path("journal") / "phase10_fixture"
    )
    return {
        "mode": "fixture",
        "replay": {
            "total_sessions": result.total_sessions,
            "total_sweeps": result.total_sweeps,
            "total_setups": result.total_setups,
            "coverage": result.coverage.to_dict(),
            "warnings": result.warnings,
            "metadata": result.metadata,
        },
        "stats": stats,
        "journal_path": str(path),
    }


async def run_live_or_fixture(
    *,
    symbol: str,
    timeframe: str,
    sessions: tuple[str, ...],
    compare_stops: bool,
    use_fixture: bool,
) -> dict[str, Any]:
    if use_fixture:
        return run_fixture_replay()

    try:
        payload = await _load_bars_from_cdp()
    except Exception as exc:  # noqa: BLE001
        out = run_fixture_replay()
        out["live_error"] = str(exc)
        out["note"] = "CDP unavailable — fixture replay used"
        return out

    if not payload.get("ok"):
        out = run_fixture_replay()
        out["live_error"] = payload.get("error")
        out["note"] = "Bar fetch failed — fixture replay used"
        return out

    bars = payload["bars"]
    sym = payload.get("symbol") or symbol
    tf = str(payload.get("resolution") or timeframe)

    # Primary replay
    primary = replay_historical_setups(
        bars,
        symbol=sym,
        timeframe=tf,
        session_names=sessions,
    )
    stats = compute_replay_statistics(primary.journal_records)
    jpath = append_journal_records(primary.journal_records, root="journal")

    stop_cmp = None
    if compare_stops:
        by_mode = {}
        for mode in ("beyond_sweep", "beyond_fvg"):
            r = replay_historical_setups(
                bars,
                symbol=sym,
                timeframe=tf,
                strategy_config=_with_stop_mode(mode),
                session_names=sessions,
            )
            by_mode[mode] = r.journal_records
        stop_cmp = compare_stop_modes(by_mode)

    # LuxAlgo overlap if available
    overlap = None
    try:
        from confirmation_provider import HistoricalStructureProvider
        from luxalgo_overlap import compare_choch_overlap
        from luxalgo_structure import fetch_luxalgo_choch

        lux = await fetch_luxalgo_choch()
        internal = HistoricalStructureProvider().get_confirmations(bars)
        overlap = compare_choch_overlap(internal, lux.get("events") or [])
    except Exception as exc:  # noqa: BLE001
        overlap = {"ok": False, "error": str(exc)}

    return {
        "mode": "live_cdp_bars",
        "symbol": sym,
        "timeframe": tf,
        "bar_count": len(bars),
        "replay": {
            "total_sessions": primary.total_sessions,
            "total_sweeps": primary.total_sweeps,
            "total_setups": primary.total_setups,
            "coverage": primary.coverage.to_dict(),
            "warnings": primary.warnings,
            "metadata": primary.metadata,
            "errors": primary.errors,
        },
        "stats": stats,
        "stop_mode_comparison": stop_cmp,
        "luxalgo_overlap": overlap,
        "journal_path": str(jpath),
        "sample_setup_ids": [r.setup_id for r in primary.journal_records[:10]],
    }


def main(argv: Optional[list[str]] = None) -> None:
    p = argparse.ArgumentParser(description="Phase 10 historical setup replay")
    p.add_argument("--symbol", default="OANDA:XAUUSD")
    p.add_argument("--timeframe", default="5")
    p.add_argument("--session", default="asia,london")
    p.add_argument("--start", default=None, help="Unused placeholder (filter via bars)")
    p.add_argument("--end", default=None)
    p.add_argument("--compare-stops", action="store_true")
    p.add_argument("--fixture", action="store_true", help="Force offline fixture replay")
    args = p.parse_args(argv)

    report = asyncio.run(
        run_live_or_fixture(
            symbol=args.symbol,
            timeframe=args.timeframe,
            sessions=_parse_sessions(args.session),
            compare_stops=args.compare_stops,
            use_fixture=bool(args.fixture),
        )
    )
    Path("phase10_validation.json").write_text(
        json.dumps(report, indent=2, default=str), encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "mode": report.get("mode"),
                "total_setups": (report.get("replay") or {}).get("total_setups"),
                "total_sweeps": (report.get("replay") or {}).get("total_sweeps"),
                "coverage": (report.get("replay") or {}).get("coverage"),
                "journal_path": report.get("journal_path"),
                "luxalgo_overlap_matched": (
                    (report.get("luxalgo_overlap") or {}).get("matched_count")
                ),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
