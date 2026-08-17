"""Phase 9 unit tests: setup expiry lifecycle + EXPIRED annotation (no CDP)."""

from __future__ import annotations

import unittest

from annotation_plan import plan_annotations
from expiry_config import (
    EXPIRY_REASON_CONFIRMATION_TIMEOUT,
    EXPIRY_REASON_FVG_TIMEOUT,
    EXPIRY_REASON_NEW_SESSION_STARTED,
    EXPIRY_REASON_RETRACE_TIMEOUT,
    ExpiryConfig,
)
from models import (
    Bar,
    LiquiditySweep,
    SessionRange,
    SetupStatus,
    StructureConfirmation,
)
from session_time import resolve_session_window
from sessions_config import SESSION_DEFINITIONS
from setup_engine import analyze_session_setup
from setup_expiry import (
    SessionContext,
    build_session_context,
    evaluate_setup_expiry,
)
from strategy_config import DEFAULT_STRATEGY_CONFIG, StrategyConfig
from datetime import date


def _bar(ts: int, o: float, h: float, l: float, c: float) -> Bar:
    return Bar(time=ts, open=o, high=h, low=l, close=c)


def _asia_session(
    *,
    trading_date: str = "2026-08-14",
    complete: bool = True,
) -> SessionRange:
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
        complete=complete,
        source="ict_sessions",
        coverage_status="full",
        identity=f"Asia:{trading_date}",
        extras={"resolved_window": w.to_dict()},
    )


def _choch(direction: str, ts: int, level: float = 4320.0, timing: str = "exact"):
    return StructureConfirmation(
        kind="CHoCH",
        direction=direction,
        level=level,
        event_timestamp=ts,
        event_bar_index=None,
        source="luxalgo",
        study_id="smUEv2",
        raw_id="t",
        timing_confidence=timing,
    )


def _cfg(**expiry_kwargs) -> StrategyConfig:
    return StrategyConfig(
        sweep_rule=DEFAULT_STRATEGY_CONFIG.sweep_rule,
        entry_modes=DEFAULT_STRATEGY_CONFIG.entry_modes,
        fvg=DEFAULT_STRATEGY_CONFIG.fvg,
        entry=DEFAULT_STRATEGY_CONFIG.entry,
        risk=DEFAULT_STRATEGY_CONFIG.risk,
        target=DEFAULT_STRATEGY_CONFIG.target,
        expiry=ExpiryConfig(**expiry_kwargs),
        prefer_completed_sessions_only=True,
        session_confidence=dict(DEFAULT_STRATEGY_CONFIG.session_confidence),
        dst_uncertainty=DEFAULT_STRATEGY_CONFIG.dst_uncertainty,
    )


class NewSessionExpiryTests(unittest.TestCase):
    def test_asia_waiting_expires_when_london_active(self):
        s = _asia_session()
        asia_w = resolve_session_window(SESSION_DEFINITIONS["Asia"], date(2026, 8, 14))
        london_w = resolve_session_window(
            SESSION_DEFINITIONS["London"], date(2026, 8, 15)
        )
        # Sweep shortly after Asia ends window start; still waiting for CHoCH.
        sweep_ts = asia_w.utc_end + 60
        bars = [_bar(sweep_ts, 4312, 4313, 4310, 4312)]
        now_ts = london_w.utc_start + 300
        ctx = SessionContext(
            setup_session_name="Asia",
            setup_trading_date="2026-08-14",
            setup_window=asia_w,
            active_session_name=london_w.session,
            active_trading_date=london_w.trading_date,
            active_window=london_w,
            now_ts=now_ts,
        )
        setup = analyze_session_setup(
            s,
            bars,
            [],
            _cfg(expire_on_new_session=True),
            symbol="OANDA:XAUUSD",
            timeframe="15",
            now_ts=now_ts,
            session_context=ctx,
        )
        self.assertEqual(setup.status, SetupStatus.EXPIRED.value)
        self.assertEqual(setup.expiry_reason, EXPIRY_REASON_NEW_SESSION_STARTED)
        self.assertIn("Asia|2026-08-14|low|", setup.id)

    def test_no_expiry_while_still_in_asia_context(self):
        s = _asia_session()
        asia_w = resolve_session_window(SESSION_DEFINITIONS["Asia"], date(2026, 8, 14))
        sweep_ts = asia_w.utc_end + 60
        bars = [_bar(sweep_ts, 4312, 4313, 4310, 4312)]
        now_ts = asia_w.utc_start + 3600  # still inside Asia window
        ctx = build_session_context(
            setup_session_name="Asia",
            setup_trading_date="2026-08-14",
            session_range=s,
            now_ts=now_ts,
        )
        setup = analyze_session_setup(
            s,
            bars,
            [],
            _cfg(expire_on_new_session=True),
            symbol="OANDA:XAUUSD",
            timeframe="15",
            now_ts=now_ts,
            session_context=ctx,
        )
        self.assertEqual(setup.status, SetupStatus.WAITING_FOR_CONFIRMATION.value)
        self.assertIsNone(setup.expiry_reason)

    def test_setup_id_stable_across_expiry(self):
        s = _asia_session()
        asia_w = resolve_session_window(SESSION_DEFINITIONS["Asia"], date(2026, 8, 14))
        london_w = resolve_session_window(
            SESSION_DEFINITIONS["London"], date(2026, 8, 15)
        )
        sweep_ts = asia_w.utc_end + 60
        bars = [_bar(sweep_ts, 4312, 4313, 4310, 4312)]
        mid = analyze_session_setup(
            s,
            bars,
            [],
            _cfg(enabled=False),
            symbol="OANDA:XAUUSD",
            timeframe="15",
            now_ts=asia_w.utc_start + 100,
        )
        expired = analyze_session_setup(
            s,
            bars,
            [],
            _cfg(expire_on_new_session=True),
            symbol="OANDA:XAUUSD",
            timeframe="15",
            now_ts=london_w.utc_start + 100,
            session_context=SessionContext(
                setup_session_name="Asia",
                setup_trading_date="2026-08-14",
                setup_window=asia_w,
                active_session_name=london_w.session,
                active_trading_date=london_w.trading_date,
                active_window=london_w,
                now_ts=london_w.utc_start + 100,
            ),
        )
        self.assertEqual(mid.id, expired.id)
        self.assertEqual(expired.status, SetupStatus.EXPIRED.value)


class BarTimeoutExpiryTests(unittest.TestCase):
    def test_confirmation_timeout(self):
        s = _asia_session()
        asia_w = resolve_session_window(SESSION_DEFINITIONS["Asia"], date(2026, 8, 14))
        sweep_ts = asia_w.utc_end + 60
        bars = [
            _bar(sweep_ts, 4312, 4313, 4310, 4312),
            _bar(sweep_ts + 60, 4312, 4314, 4311, 4313),
            _bar(sweep_ts + 120, 4313, 4315, 4312, 4314),
            _bar(sweep_ts + 180, 4314, 4316, 4313, 4315),
        ]
        # 3 bars after sweep; threshold 3 → expire. Disable new-session.
        setup = analyze_session_setup(
            s,
            bars,
            [],
            _cfg(
                expire_on_new_session=False,
                max_bars_to_confirmation=3,
            ),
            symbol="XAU",
            timeframe="15",
            now_ts=asia_w.utc_start + 100,
            session_context=build_session_context(
                setup_session_name="Asia",
                setup_trading_date="2026-08-14",
                session_range=s,
                now_ts=asia_w.utc_start + 100,
            ),
        )
        self.assertEqual(setup.status, SetupStatus.EXPIRED.value)
        self.assertEqual(setup.expiry_reason, EXPIRY_REASON_CONFIRMATION_TIMEOUT)

    def test_confirmation_timeout_not_before_threshold(self):
        s = _asia_session()
        asia_w = resolve_session_window(SESSION_DEFINITIONS["Asia"], date(2026, 8, 14))
        sweep_ts = asia_w.utc_end + 60
        bars = [
            _bar(sweep_ts, 4312, 4313, 4310, 4312),
            _bar(sweep_ts + 60, 4312, 4314, 4311, 4313),
        ]
        setup = analyze_session_setup(
            s,
            bars,
            [],
            _cfg(expire_on_new_session=False, max_bars_to_confirmation=3),
            symbol="XAU",
            timeframe="15",
            now_ts=asia_w.utc_start + 100,
            session_context=build_session_context(
                setup_session_name="Asia",
                setup_trading_date="2026-08-14",
                session_range=s,
                now_ts=asia_w.utc_start + 100,
            ),
        )
        self.assertEqual(setup.status, SetupStatus.WAITING_FOR_CONFIRMATION.value)

    def test_fvg_timeout(self):
        s = _asia_session()
        asia_w = resolve_session_window(SESSION_DEFINITIONS["Asia"], date(2026, 8, 14))
        sweep_ts = asia_w.utc_end + 60
        choch_ts = sweep_ts + 60
        bars = [
            _bar(sweep_ts, 4312, 4313, 4310, 4312),
            _bar(choch_ts, 4315, 4322, 4314, 4320),
            _bar(choch_ts + 60, 4320, 4321, 4319, 4320),
            _bar(choch_ts + 120, 4320, 4322, 4319, 4321),
            _bar(choch_ts + 180, 4321, 4323, 4320, 4322),
        ]
        # No FVG forms (gaps filled). Threshold 3 bars after CHoCH.
        setup = analyze_session_setup(
            s,
            bars,
            [_choch("bullish", choch_ts)],
            _cfg(expire_on_new_session=False, max_bars_to_fvg=3),
            symbol="XAU",
            timeframe="15",
            now_ts=asia_w.utc_start + 100,
            session_context=build_session_context(
                setup_session_name="Asia",
                setup_trading_date="2026-08-14",
                session_range=s,
                now_ts=asia_w.utc_start + 100,
            ),
        )
        self.assertEqual(setup.status, SetupStatus.EXPIRED.value)
        self.assertEqual(setup.expiry_reason, EXPIRY_REASON_FVG_TIMEOUT)

    def test_retrace_timeout(self):
        s = _asia_session()
        asia_w = resolve_session_window(SESSION_DEFINITIONS["Asia"], date(2026, 8, 14))
        t0 = asia_w.utc_end + 60
        # FVG forms; price stays above zone (no retrace) for >= 3 bars after FVG.
        bars = [
            _bar(t0, 4312, 4313, 4310, 4312),
            _bar(t0 + 60, 4315, 4322, 4314, 4320),
            _bar(t0 + 120, 4321, 4325, 4320, 4324),
            _bar(t0 + 180, 4324, 4340, 4323, 4338),
            _bar(t0 + 240, 4338, 4345, 4330, 4342),  # FVG create
            _bar(t0 + 300, 4342, 4348, 4340, 4345),
            _bar(t0 + 360, 4345, 4350, 4343, 4348),
            _bar(t0 + 420, 4348, 4352, 4346, 4350),
        ]
        setup = analyze_session_setup(
            s,
            bars,
            [_choch("bullish", t0 + 60)],
            _cfg(expire_on_new_session=False, max_bars_to_retrace=3),
            symbol="XAU",
            timeframe="15",
            now_ts=asia_w.utc_start + 100,
            session_context=build_session_context(
                setup_session_name="Asia",
                setup_trading_date="2026-08-14",
                session_range=s,
                now_ts=asia_w.utc_start + 100,
            ),
        )
        self.assertEqual(setup.status, SetupStatus.EXPIRED.value)
        self.assertEqual(setup.expiry_reason, EXPIRY_REASON_RETRACE_TIMEOUT)

    def test_none_threshold_means_no_timeout(self):
        s = _asia_session()
        asia_w = resolve_session_window(SESSION_DEFINITIONS["Asia"], date(2026, 8, 14))
        sweep_ts = asia_w.utc_end + 60
        bars = [
            _bar(sweep_ts, 4312, 4313, 4310, 4312),
            *[_bar(sweep_ts + 60 * i, 4312, 4314, 4311, 4313) for i in range(1, 20)],
        ]
        setup = analyze_session_setup(
            s,
            bars,
            [],
            _cfg(
                expire_on_new_session=False,
                max_bars_to_confirmation=None,
            ),
            symbol="XAU",
            timeframe="15",
            now_ts=asia_w.utc_start + 100,
            session_context=build_session_context(
                setup_session_name="Asia",
                setup_trading_date="2026-08-14",
                session_range=s,
                now_ts=asia_w.utc_start + 100,
            ),
        )
        self.assertEqual(setup.status, SetupStatus.WAITING_FOR_CONFIRMATION.value)
        self.assertIsNone(setup.expiry_reason)

    def test_disabled_expiry_engine(self):
        s = _asia_session()
        asia_w = resolve_session_window(SESSION_DEFINITIONS["Asia"], date(2026, 8, 14))
        london_w = resolve_session_window(
            SESSION_DEFINITIONS["London"], date(2026, 8, 15)
        )
        sweep_ts = asia_w.utc_end + 60
        bars = [_bar(sweep_ts, 4312, 4313, 4310, 4312)]
        setup = analyze_session_setup(
            s,
            bars,
            [],
            _cfg(enabled=False, expire_on_new_session=True, max_bars_to_confirmation=1),
            symbol="XAU",
            timeframe="15",
            now_ts=london_w.utc_start + 100,
            session_context=SessionContext(
                setup_session_name="Asia",
                setup_trading_date="2026-08-14",
                setup_window=asia_w,
                active_session_name=london_w.session,
                active_trading_date=london_w.trading_date,
                active_window=london_w,
                now_ts=london_w.utc_start + 100,
            ),
        )
        self.assertEqual(setup.status, SetupStatus.WAITING_FOR_CONFIRMATION.value)
        self.assertIsNone(setup.expiry_reason)


class OppositeSweepExpiryTests(unittest.TestCase):
    def test_opposite_liquidity_event_when_enabled(self):
        s = _asia_session()
        asia_w = resolve_session_window(SESSION_DEFINITIONS["Asia"], date(2026, 8, 14))
        sweep_ts = asia_w.utc_end + 60
        bars = [_bar(sweep_ts, 4312, 4313, 4310, 4312)]
        candle = _bar(asia_w.utc_end + 500, 4399, 4401, 4398, 4399)
        opp = LiquiditySweep(
            session="London",
            side="high",
            level=4400.0,
            sweep_timestamp=asia_w.utc_end + 500,
            sweep_price=4401.0,
            maximum_excursion=1.0,
            reclaim_status=True,
            rule="wick_only",
            sweep_candle=candle,
        )
        setup = analyze_session_setup(
            s,
            bars,
            [],
            _cfg(
                expire_on_new_session=False,
                expire_on_opposite_session_sweep=True,
            ),
            symbol="XAU",
            timeframe="15",
            now_ts=asia_w.utc_start + 100,
            session_context=build_session_context(
                setup_session_name="Asia",
                setup_trading_date="2026-08-14",
                session_range=s,
                now_ts=asia_w.utc_start + 100,
                opposite_session_sweeps=[opp],
            ),
            opposite_session_sweeps=[opp],
        )
        self.assertEqual(setup.status, SetupStatus.EXPIRED.value)
        self.assertEqual(setup.expiry_reason, "OPPOSITE_LIQUIDITY_EVENT")


class ExpiredAnnotationTests(unittest.TestCase):
    def test_expired_status_annotation_includes_reason(self):
        s = _asia_session()
        asia_w = resolve_session_window(SESSION_DEFINITIONS["Asia"], date(2026, 8, 14))
        london_w = resolve_session_window(
            SESSION_DEFINITIONS["London"], date(2026, 8, 15)
        )
        sweep_ts = asia_w.utc_end + 60
        bars = [_bar(sweep_ts, 4312, 4313, 4310, 4312)]
        setup = analyze_session_setup(
            s,
            bars,
            [],
            _cfg(expire_on_new_session=True),
            symbol="XAU",
            timeframe="15",
            now_ts=london_w.utc_start + 100,
            session_context=SessionContext(
                setup_session_name="Asia",
                setup_trading_date="2026-08-14",
                setup_window=asia_w,
                active_session_name=london_w.session,
                active_trading_date=london_w.trading_date,
                active_window=london_w,
                now_ts=london_w.utc_start + 100,
            ),
        )
        plan = plan_annotations(setup)
        status_items = [i for i in plan.items if i.role == "status"]
        self.assertEqual(len(status_items), 1)
        self.assertTrue(
            status_items[0].label.startswith("AITRADE · EXPIRED · NEW_SESSION_STARTED")
        )


class EvaluateExpiryPureTests(unittest.TestCase):
    def test_evaluate_returns_none_reason_when_no_rule(self):
        s = _asia_session()
        asia_w = resolve_session_window(SESSION_DEFINITIONS["Asia"], date(2026, 8, 14))
        draft = analyze_session_setup(
            s,
            [_bar(asia_w.utc_end + 60, 4312, 4313, 4310, 4312)],
            [],
            _cfg(enabled=False),
            symbol="XAU",
            timeframe="15",
        )
        decision = evaluate_setup_expiry(
            draft,
            [],
            build_session_context(
                setup_session_name="Asia",
                setup_trading_date="2026-08-14",
                session_range=s,
                now_ts=asia_w.utc_start + 100,
            ),
            ExpiryConfig(enabled=True, expire_on_new_session=True),
        )
        self.assertFalse(decision.expired)
        self.assertIsNone(decision.reason)


if __name__ == "__main__":
    unittest.main()
