"""Phase 10 tests: historical structure, replay, outcomes, journal (no CDP required)."""

from __future__ import annotations

import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from confirmation_provider import (
    HistoricalStructureProvider,
    LuxAlgoLiveProvider,
)
from historical_structure import detect_internal_choch
from historical_structure_config import HistoricalStructureConfig
from journal_models import (
    OUTCOME_1R_HIT,
    OUTCOME_2R_HIT,
    OUTCOME_3R_HIT,
    OUTCOME_AMBIGUOUS_INTRABAR,
    OUTCOME_OPPOSITE_LIQUIDITY_HIT,
    OUTCOME_STOP_HIT,
)
from luxalgo_overlap import compare_choch_overlap
from models import (
    Bar,
    EntryAnalysis,
    EntryCandidate,
    FixedRRTarget,
    RiskPlan,
    SessionRange,
    SetupStatus,
    StructureConfirmation,
    TargetPlan,
)
from outcome_engine import evaluate_entry_outcome
from replay_engine import replay_historical_setups, trade_setup_to_journal_record
from replay_fixtures import build_multi_day_fixture_bars, bullish_chain_bars
from session_time import resolve_session_window
from sessions_config import SESSION_DEFINITIONS
from setup_engine import analyze_session_setup, make_setup_id
from setup_journal import append_journal_records, load_journal_records
from strategy_config import DEFAULT_STRATEGY_CONFIG, StrategyConfig
from strategy_version import compute_config_hash
from expiry_config import ExpiryConfig


def _choch(direction: str, ts: int, level: float = 4320.0):
    return StructureConfirmation(
        kind="CHoCH",
        direction=direction,
        level=level,
        event_timestamp=ts,
        event_bar_index=None,
        source="luxalgo",
        study_id="test",
        raw_id="t",
        timing_confidence="exact",
    )


def _asia_session_for(trading_date: str = "2026-08-14") -> SessionRange:
    w = resolve_session_window(
        SESSION_DEFINITIONS["Asia"], date.fromisoformat(trading_date)
    )
    return SessionRange(
        name="Asia",
        timezone="America/New_York",
        start=w.utc_start,
        end=w.utc_end,
        high=4360.0,
        low=4311.04,
        high_timestamp=None,
        low_timestamp=None,
        complete=True,
        source="ict_sessions",
        coverage_status="full",
        identity=f"Asia:{trading_date}",
        extras={"resolved_window": w.to_dict()},
    )


def _shift_bars_after_session(session: SessionRange, relative: list[Bar]) -> list[Bar]:
    base = int(session.end) + 60
    t0 = relative[0].time
    return [
        Bar(
            time=base + (b.time - t0),
            open=b.open,
            high=b.high,
            low=b.low,
            close=b.close,
        )
        for b in relative
    ]


class StructureProviderTests(unittest.TestCase):
    def test_internal_choch_emits_canonical_model(self):
        # Explicit: form a swing high, pull back, then close-break above it.
        bars: list[Bar] = []
        t = 10_000
        # Climb into swing high
        for i, px in enumerate([90, 92, 94, 96, 98, 100, 98, 96, 94, 92]):
            bars.append(
                Bar(time=t + i * 60, open=px, high=px + 0.5, low=px - 0.5, close=px)
            )
        # Continue lower then reclaim above 100 by close
        for i, px in enumerate([90, 91, 93, 95, 97, 99, 101, 103, 105]):
            bars.append(
                Bar(
                    time=t + (10 + i) * 60,
                    open=px - 1,
                    high=px + 1,
                    low=px - 2,
                    close=px,
                )
            )
        events = detect_internal_choch(bars, HistoricalStructureConfig(swing_left=2, swing_right=2))
        self.assertTrue(
            any(e.direction == "bullish" for e in events),
            msg=f"events={[ (e.direction, e.level, e.event_timestamp) for e in events ]}",
        )
        e = next(x for x in events if x.direction == "bullish")
        self.assertEqual(e.source, "internal_structure")
        self.assertEqual(e.extras.get("equivalence_status"), "unvalidated_against_luxalgo")
        self.assertEqual(e.timing_confidence, "exact")

    def test_provider_interface(self):
        bars = [Bar(time=i * 60, open=1, high=2, low=0.5, close=1.5) for i in range(20)]
        p = HistoricalStructureProvider()
        self.assertEqual(p.source_name, "internal_structure")
        self.assertIsInstance(p.get_confirmations(bars), list)


class ConfigHashTests(unittest.TestCase):
    def test_stable_hash(self):
        a = compute_config_hash()
        b = compute_config_hash()
        self.assertEqual(a, b)

    def test_hash_changes_with_stop_mode(self):
        from models import RiskConfig

        base = DEFAULT_STRATEGY_CONFIG
        other = StrategyConfig(
            sweep_rule=base.sweep_rule,
            entry_modes=base.entry_modes,
            fvg=base.fvg,
            entry=base.entry,
            risk=RiskConfig(stop_mode="beyond_fvg"),
            target=base.target,
            expiry=base.expiry,
        )
        self.assertNotEqual(compute_config_hash(base), compute_config_hash(other))


class OutcomeEngineTests(unittest.TestCase):
    def _analysis(self, *, entry_ts, entry_px, stop, direction="bullish"):
        entry = EntryCandidate(
            mode="boundary",
            direction=direction,
            price=entry_px,
            triggered=True,
            trigger_timestamp=entry_ts,
            trigger_bar_index=0,
            fvg_reference={},
            setup_reference={},
            entry_depth=0.5,
            max_retrace_depth=0.5,
            bars_after_fvg=1,
            status="triggered",
        )
        risk = RiskPlan(
            direction=direction,
            stop_mode="beyond_sweep",
            entry_price=entry_px,
            stop_price=stop,
            risk_distance=abs(entry_px - stop),
            risk_points=abs(entry_px - stop),
            buffer=0.0,
            valid=True,
            invalidation_reason=None,
            setup_reference={},
        )
        rd = abs(entry_px - stop)
        sign = 1 if direction == "bullish" else -1
        target = TargetPlan(
            fixed_rr_targets=[
                FixedRRTarget(1.0, entry_px + sign * rd, rd),
                FixedRRTarget(2.0, entry_px + sign * 2 * rd, 2 * rd),
                FixedRRTarget(3.0, entry_px + sign * 3 * rd, 3 * rd),
            ],
            opposite_liquidity=True,
            opposite_liquidity_label="Asia High · Opposing Liquidity",
            opposite_liquidity_price=entry_px + sign * 5 * rd,
            rr_to_opposite=5.0,
            opposite_target_valid=True,
            valid=True,
            setup_reference={},
        )
        return EntryAnalysis(entry=entry, risk=risk, target=target)

    def test_stop_hit(self):
        a = self._analysis(entry_ts=100, entry_px=100.0, stop=95.0)
        bars = [
            Bar(time=100, open=100, high=101, low=99, close=100),
            Bar(time=200, open=100, high=100.5, low=94, close=95),
        ]
        r = evaluate_entry_outcome(a, bars, direction="bullish", horizon_end_ts=1000)
        self.assertEqual(r.outcome, OUTCOME_STOP_HIT)

    def test_rr_progression(self):
        a = self._analysis(entry_ts=100, entry_px=100.0, stop=90.0)
        bars = [
            Bar(time=100, open=100, high=101, low=99, close=100),
            Bar(time=200, open=100, high=111, low=99, close=110),  # 1R
            Bar(time=300, open=110, high=121, low=109, close=120),  # 2R
            Bar(time=400, open=120, high=131, low=119, close=130),  # 3R
        ]
        r = evaluate_entry_outcome(a, bars, direction="bullish", horizon_end_ts=1000)
        self.assertEqual(r.outcome, OUTCOME_3R_HIT)
        self.assertIn(OUTCOME_1R_HIT, (r.event_timestamps.get("rr_hits") or {}))

    def test_opposite_liquidity(self):
        a = self._analysis(entry_ts=100, entry_px=100.0, stop=90.0)
        bars = [
            Bar(time=100, open=100, high=101, low=99, close=100),
            Bar(time=200, open=100, high=160, low=99, close=150),
        ]
        r = evaluate_entry_outcome(a, bars, direction="bullish", horizon_end_ts=1000)
        self.assertEqual(r.outcome, OUTCOME_OPPOSITE_LIQUIDITY_HIT)

    def test_ambiguous_stop_and_target_same_bar(self):
        a = self._analysis(entry_ts=100, entry_px=100.0, stop=90.0)
        bars = [
            Bar(time=100, open=100, high=101, low=99, close=100),
            Bar(time=200, open=100, high=115, low=85, close=100),  # 1R and stop
        ]
        r = evaluate_entry_outcome(a, bars, direction="bullish", horizon_end_ts=1000)
        self.assertEqual(r.outcome, OUTCOME_AMBIGUOUS_INTRABAR)
        self.assertIn("AMBIGUOUS_INTRABAR_STOP_AND_TARGET", r.ambiguity_flags)

    def test_trigger_bar_stop_ambiguity(self):
        a = self._analysis(entry_ts=100, entry_px=100.0, stop=90.0)
        bars = [Bar(time=100, open=100, high=101, low=85, close=95)]
        r = evaluate_entry_outcome(a, bars, direction="bullish", horizon_end_ts=1000)
        self.assertEqual(r.outcome, OUTCOME_AMBIGUOUS_INTRABAR)
        self.assertIn("TRIGGER_BAR_STOP_AMBIGUITY", r.ambiguity_flags)

    def test_mfe_mae(self):
        a = self._analysis(entry_ts=100, entry_px=100.0, stop=90.0)
        bars = [
            Bar(time=100, open=100, high=101, low=99, close=100),
            Bar(time=200, open=100, high=108, low=97, close=105),
            Bar(time=300, open=105, high=105, low=94, close=95),  # stop
        ]
        r = evaluate_entry_outcome(a, bars, direction="bullish", horizon_end_ts=1000)
        self.assertAlmostEqual(r.max_favorable_excursion or 0, 8.0)
        self.assertAlmostEqual(r.max_adverse_excursion or 0, 6.0)  # 100-94
        self.assertAlmostEqual(r.mfe_r or 0, 0.8)
        self.assertAlmostEqual(r.mae_r or 0, 0.6)


class StrategyChainTests(unittest.TestCase):
    def test_complete_bullish_chain(self):
        s = _asia_session_for()
        rel = bullish_chain_bars()
        bars = _shift_bars_after_session(s, rel)
        choch_ts = bars[2].time
        setup = analyze_session_setup(
            s,
            bars,
            [_choch("bullish", choch_ts)],
            DEFAULT_STRATEGY_CONFIG,
            symbol="OANDA:XAUUSD",
            timeframe="5",
            now_ts=int(s.end) - 100,  # still in session context window-ish
            session_context=None,
        )
        # Disable new-session expiry by pinning now inside asia via expiry disabled
        cfg = StrategyConfig(
            sweep_rule=DEFAULT_STRATEGY_CONFIG.sweep_rule,
            entry_modes=DEFAULT_STRATEGY_CONFIG.entry_modes,
            fvg=DEFAULT_STRATEGY_CONFIG.fvg,
            entry=DEFAULT_STRATEGY_CONFIG.entry,
            risk=DEFAULT_STRATEGY_CONFIG.risk,
            target=DEFAULT_STRATEGY_CONFIG.target,
            expiry=ExpiryConfig(enabled=False),
        )
        setup = analyze_session_setup(
            s,
            bars,
            [_choch("bullish", choch_ts)],
            cfg,
            symbol="OANDA:XAUUSD",
            timeframe="5",
            now_ts=bars[-1].time,
        )
        self.assertEqual(setup.status, SetupStatus.ENTRY_READY.value)
        self.assertEqual(setup.direction, "bullish")
        rec = trade_setup_to_journal_record(
            setup,
            bars,
            s,
            strategy_config=cfg,
            structure_config=HistoricalStructureConfig(),
            config_hash=compute_config_hash(cfg),
        )
        self.assertEqual(rec.setup_id, setup.id)
        self.assertTrue(any(e.triggered for e in rec.entry_results))

    def test_complete_bearish_chain(self):
        s = _asia_session_for()
        w = resolve_session_window(SESSION_DEFINITIONS["Asia"], date(2026, 8, 14))
        # High sweep then bearish CHoCH + bearish FVG + retrace
        t0 = w.utc_end + 60
        bars = [
            Bar(time=t0, open=4355, high=4362, low=4354, close=4356),  # high sweep
            Bar(time=t0 + 60, open=4356, high=4357, low=4348, close=4350),
            Bar(time=t0 + 120, open=4350, high=4351, low=4345, close=4346),  # c1
            Bar(time=t0 + 180, open=4346, high=4347, low=4320, close=4322),  # c2
            Bar(time=t0 + 240, open=4322, high=4325, low=4318, close=4320),  # c3 FVG
            Bar(time=t0 + 300, open=4320, high=4332, low=4319, close=4330),  # retrace
        ]
        # Session high 4360 — wick sweep
        s = SessionRange(
            name="Asia",
            timezone="America/New_York",
            start=w.utc_start,
            end=w.utc_end,
            high=4360.0,
            low=4311.04,
            high_timestamp=None,
            low_timestamp=None,
            complete=True,
            source="ict_sessions",
            coverage_status="full",
            identity="Asia:2026-08-14",
            extras={"resolved_window": w.to_dict()},
        )
        cfg = StrategyConfig(
            sweep_rule=DEFAULT_STRATEGY_CONFIG.sweep_rule,
            entry_modes=DEFAULT_STRATEGY_CONFIG.entry_modes,
            fvg=DEFAULT_STRATEGY_CONFIG.fvg,
            entry=DEFAULT_STRATEGY_CONFIG.entry,
            risk=DEFAULT_STRATEGY_CONFIG.risk,
            target=DEFAULT_STRATEGY_CONFIG.target,
            expiry=ExpiryConfig(enabled=False),
        )
        setup = analyze_session_setup(
            s,
            bars,
            [_choch("bearish", t0 + 60, level=4355.0)],
            cfg,
            symbol="XAU",
            timeframe="5",
            now_ts=bars[-1].time,
        )
        self.assertEqual(setup.direction, "bearish")
        self.assertIn(
            setup.status,
            {
                SetupStatus.ENTRY_READY.value,
                SetupStatus.WAITING_FOR_RETRACE.value,
                SetupStatus.WAITING_FOR_FVG.value,
            },
        )

    def test_no_sweep(self):
        s = _asia_session_for()
        bars = [
            Bar(time=int(s.end) + 60, open=4330, high=4335, low=4325, close=4332)
        ]
        cfg = StrategyConfig(
            sweep_rule=DEFAULT_STRATEGY_CONFIG.sweep_rule,
            entry_modes=DEFAULT_STRATEGY_CONFIG.entry_modes,
            fvg=DEFAULT_STRATEGY_CONFIG.fvg,
            entry=DEFAULT_STRATEGY_CONFIG.entry,
            risk=DEFAULT_STRATEGY_CONFIG.risk,
            target=DEFAULT_STRATEGY_CONFIG.target,
            expiry=ExpiryConfig(enabled=False),
        )
        setup = analyze_session_setup(
            s, bars, [], cfg, symbol="XAU", timeframe="5", now_ts=bars[-1].time
        )
        self.assertEqual(setup.status, SetupStatus.WAITING_FOR_SWEEP.value)

    def test_sweep_no_choch(self):
        s = _asia_session_for()
        bars = [
            Bar(
                time=int(s.end) + 60,
                open=4312,
                high=4313,
                low=4310,
                close=4312,
            )
        ]
        cfg = StrategyConfig(
            sweep_rule=DEFAULT_STRATEGY_CONFIG.sweep_rule,
            entry_modes=DEFAULT_STRATEGY_CONFIG.entry_modes,
            fvg=DEFAULT_STRATEGY_CONFIG.fvg,
            entry=DEFAULT_STRATEGY_CONFIG.entry,
            risk=DEFAULT_STRATEGY_CONFIG.risk,
            target=DEFAULT_STRATEGY_CONFIG.target,
            expiry=ExpiryConfig(enabled=False),
        )
        setup = analyze_session_setup(
            s, bars, [], cfg, symbol="XAU", timeframe="5", now_ts=bars[-1].time
        )
        self.assertEqual(setup.status, SetupStatus.WAITING_FOR_CONFIRMATION.value)

    def test_setup_id_stable(self):
        s = _asia_session_for()
        bars = [
            Bar(
                time=int(s.end) + 60,
                open=4312,
                high=4313,
                low=4310,
                close=4312,
            )
        ]
        cfg = StrategyConfig(
            sweep_rule=DEFAULT_STRATEGY_CONFIG.sweep_rule,
            entry_modes=DEFAULT_STRATEGY_CONFIG.entry_modes,
            fvg=DEFAULT_STRATEGY_CONFIG.fvg,
            entry=DEFAULT_STRATEGY_CONFIG.entry,
            risk=DEFAULT_STRATEGY_CONFIG.risk,
            target=DEFAULT_STRATEGY_CONFIG.target,
            expiry=ExpiryConfig(enabled=False),
        )
        a = analyze_session_setup(
            s, bars, [], cfg, symbol="XAU", timeframe="5", now_ts=bars[-1].time
        )
        b = analyze_session_setup(
            s, bars, [], cfg, symbol="XAU", timeframe="5", now_ts=bars[-1].time
        )
        self.assertEqual(a.id, b.id)


class ReplayCoverageTests(unittest.TestCase):
    def test_multi_day_dst_aware_replay(self):
        bars = build_multi_day_fixture_bars(date(2026, 8, 12), days=3)
        result = replay_historical_setups(
            bars,
            symbol="OANDA:XAUUSD",
            timeframe="5",
            confirmation_provider=HistoricalStructureProvider(),
        )
        self.assertGreater(result.total_sessions, 0)
        self.assertEqual(
            result.coverage.expected_sessions,
            result.coverage.complete_sessions
            + result.coverage.incomplete_sessions
            + result.coverage.missing_bars_sessions,
        )
        # Incomplete/missing are skipped from setups
        self.assertEqual(
            result.coverage.skipped_sessions,
            result.coverage.incomplete_sessions + result.coverage.missing_bars_sessions,
        )

    def test_incomplete_coverage_skipped(self):
        # Only a few bars — windows will be partial/missing
        bars = [Bar(time=1_786_690_800, open=1, high=2, low=0.5, close=1)]
        result = replay_historical_setups(
            bars, symbol="XAU", timeframe="5"
        )
        self.assertEqual(result.total_setups, 0)
        self.assertGreater(result.coverage.skipped_sessions, 0)


class JournalTests(unittest.TestCase):
    def test_jsonl_dedupe_by_setup_and_hash(self):
        s = _asia_session_for()
        bars = [
            Bar(
                time=int(s.end) + 60,
                open=4312,
                high=4313,
                low=4310,
                close=4312,
            )
        ]
        cfg = StrategyConfig(
            sweep_rule=DEFAULT_STRATEGY_CONFIG.sweep_rule,
            entry_modes=DEFAULT_STRATEGY_CONFIG.entry_modes,
            fvg=DEFAULT_STRATEGY_CONFIG.fvg,
            entry=DEFAULT_STRATEGY_CONFIG.entry,
            risk=DEFAULT_STRATEGY_CONFIG.risk,
            target=DEFAULT_STRATEGY_CONFIG.target,
            expiry=ExpiryConfig(enabled=False),
        )
        setup = analyze_session_setup(
            s, bars, [], cfg, symbol="XAU", timeframe="5", now_ts=bars[-1].time
        )
        rec = trade_setup_to_journal_record(
            setup,
            bars,
            s,
            strategy_config=cfg,
            structure_config=HistoricalStructureConfig(),
            config_hash=compute_config_hash(cfg),
        )
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            append_journal_records([rec], root=root)
            append_journal_records([rec], root=root)
            rows = load_journal_records(root=root)
            self.assertEqual(len(rows), 1)


class OverlapTests(unittest.TestCase):
    def test_overlap_matching(self):
        internal = [
            StructureConfirmation(
                kind="CHoCH",
                direction="bullish",
                level=100.0,
                event_timestamp=1000,
                event_bar_index=1,
                source="internal_structure",
                study_id=None,
                raw_id="i",
                timing_confidence="exact",
            )
        ]
        lux = [
            StructureConfirmation(
                kind="CHoCH",
                direction="bullish",
                level=101.0,
                event_timestamp=1100,
                event_bar_index=2,
                source="luxalgo",
                study_id="smUEv2",
                raw_id="l",
                timing_confidence="exact",
            )
        ]
        rep = compare_choch_overlap(
            internal, lux, time_tolerance_sec=200, level_tolerance=2.0
        )
        self.assertEqual(rep["matched_count"], 1)
        self.assertEqual(rep["equivalence_status"], "unvalidated_against_luxalgo")


if __name__ == "__main__":
    unittest.main()
