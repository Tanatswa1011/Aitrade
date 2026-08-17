"""Phase 17 tests: credentials, spot gate, chunking, no futures canonical."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from feed_equivalence import (
    CLASS_CLOSE,
    CLASS_EXACT,
    CLASS_INSUFFICIENT,
    CLASS_NOT,
    CLASS_RESEARCH,
    FeedEquivalenceReport,
    evaluate_feed_equivalence,
    replay_gate,
)
from models import Bar
from openbb_history import (
    HistoricalDataResult,
    OpenBBHistoricalDataProvider,
    provider_preflight,
)
from phase17_validate import run_phase17, validate_spot_candidate


def _bar(t: int, o: float, h: float, l: float, c: float) -> Bar:
    return Bar(time=t, open=o, high=h, low=l, close=c)


class CredentialTests(unittest.TestCase):
    def test_tiingo_missing_credential_preflight(self):
        with patch.dict("os.environ", {}, clear=False):
            import os

            os.environ.pop("TIINGO_TOKEN", None)
            os.environ.pop("TIINGO_API_KEY", None)
            with patch(
                "openbb_history.OpenBBHistoricalDataProvider.credential_status",
                return_value={
                    "credential_required": True,
                    "credential_key": "tiingo_token",
                    "environment_variable_names": ["TIINGO_TOKEN", "TIINGO_API_KEY"],
                    "present": False,
                    "present_via": None,
                    "underlying_provider": "tiingo",
                },
            ):
                pre = provider_preflight("tiingo")
        self.assertTrue(pre["credential_required"])
        self.assertFalse(pre["credential_present"])
        self.assertFalse(pre["ok"])
        # Never leak secret material
        blob = str(pre)
        self.assertNotIn("sk_", blob)

    def test_tiingo_present_preflight_ok_flag(self):
        with patch(
            "openbb_history.OpenBBHistoricalDataProvider.credential_status",
            return_value={
                "credential_required": True,
                "credential_key": "tiingo_token",
                "environment_variable_names": ["TIINGO_TOKEN"],
                "present": True,
                "present_via": "env:TIINGO_TOKEN",
                "underlying_provider": "tiingo",
            },
        ):
            with patch("openbb_history.openbb_version", return_value="4.7.2"):
                pre = provider_preflight("tiingo")
        self.assertTrue(pre["credential_present"])
        # route_available depends on openbb import; ok may still be True if route exists
        self.assertIn("route_available", pre)


class SpotVsFuturesTests(unittest.TestCase):
    def test_futures_rejected_as_canonical(self):
        bars = [_bar(1_700_000_000 + i * 300, 10, 11, 9, 10) for i in range(40)]
        report = evaluate_feed_equivalence(
            bars, bars, instrument_type="futures", candidate_symbol="GC"
        )
        gate = replay_gate(report, require_session_events=False)
        self.assertIn(report.classification, {CLASS_RESEARCH, CLASS_NOT})
        self.assertFalse(gate["deep_replay_allowed"])
        self.assertIn("not_futures", gate["failed_checks"])

    def test_wrong_instrument_type_blocks(self):
        # Empty fetch path for futures in validate_spot_candidate
        with patch(
            "phase17_validate.provider_preflight",
            return_value={
                "ok": True,
                "credential_present": True,
                "credential_required": True,
                "route_available": True,
            },
        ):
            with patch.object(
                OpenBBHistoricalDataProvider,
                "fetch_result",
                return_value=HistoricalDataResult(
                    bars=(_bar(1, 1, 2, 0.5, 1),),
                    provider="openbb",
                    underlying_provider="tiingo",
                    requested_symbol="GC",
                    source_symbol="GC",
                    instrument_type="futures",
                    timeframe="5m",
                    requested_start=1,
                    requested_end=2,
                    actual_start=1,
                    actual_end=1,
                    warnings=["FUTURES"],
                ),
            ):
                out = validate_spot_candidate(
                    underlying="tiingo", symbol="GC", start_ts=1, end_ts=2
                )
        self.assertFalse(out["accepted"])
        self.assertEqual(out["reason"], "futures_rejected_as_canonical")


class EquivalenceGateTests(unittest.TestCase):
    def test_same_spot_feed_exact(self):
        bars = [_bar(1_700_000_000 + i * 300, 10 + i, 11 + i, 9 + i, 10.5 + i) for i in range(40)]
        report = evaluate_feed_equivalence(bars, bars, instrument_type="spot_fx_metals")
        self.assertEqual(report.classification, CLASS_EXACT)
        # Without real sessions, strict gate fails session checks — expected
        strict = replay_gate(report, require_session_events=True)
        self.assertFalse(strict["deep_replay_allowed"])
        loose = replay_gate(report, require_session_events=False)
        self.assertTrue(loose["deep_replay_allowed"])

    def test_insufficient_overlap_blocks(self):
        a = [_bar(100 + i * 300, 1, 2, 0.5, 1) for i in range(3)]
        b = [_bar(99999 + i * 300, 1, 2, 0.5, 1) for i in range(3)]
        report = evaluate_feed_equivalence(a, b, instrument_type="spot_fx_metals")
        self.assertEqual(report.classification, CLASS_INSUFFICIENT)
        self.assertFalse(replay_gate(report)["deep_replay_allowed"])

    def test_price_divergence_not_equivalent(self):
        a = [_bar(1_700_000_000 + i * 300, 100, 110, 90, 105) for i in range(40)]
        b = [_bar(1_700_000_000 + i * 300, 500, 510, 490, 505) for i in range(40)]
        report = evaluate_feed_equivalence(a, b, instrument_type="spot_fx_metals")
        self.assertIn(report.classification, {CLASS_NOT, CLASS_RESEARCH, CLASS_INSUFFICIENT})
        self.assertFalse(replay_gate(report)["deep_replay_allowed"])


class ChunkResumeTests(unittest.TestCase):
    def test_chunk_overlap_deduped(self):
        prov = OpenBBHistoricalDataProvider(underlying_provider="tiingo")
        chunk_a = [_bar(1000 + i * 300, 1, 2, 0.5, 1) for i in range(5)]
        chunk_b = [_bar(1000 + i * 300, 1, 2, 0.5, 1) for i in range(3, 8)]

        def fake(symbol, timeframe, **kwargs):
            start = kwargs.get("start_ts") or 0
            bars = chunk_a if start < 5000 else chunk_b
            return HistoricalDataResult(
                bars=tuple(bars),
                provider="openbb",
                underlying_provider="tiingo",
                requested_symbol=symbol,
                source_symbol=symbol,
                instrument_type="spot_fx_metals",
                timeframe="5m",
                requested_start=kwargs.get("start_ts"),
                requested_end=kwargs.get("end_ts"),
                actual_start=bars[0].time,
                actual_end=bars[-1].time,
            )

        with patch.object(prov, "fetch_result", side_effect=fake):
            with patch.object(prov, "persist_result", return_value={"ok": True}):
                res = prov.fetch_chunked(
                    "XAUUSD",
                    "5m",
                    start_ts=1000,
                    end_ts=1000 + 20 * 86400,
                    chunk_days=1,
                    persist=True,
                )
        times = [b.time for b in res.bars]
        self.assertEqual(len(times), len(set(times)))

    def test_partial_failure_records_errors(self):
        prov = OpenBBHistoricalDataProvider(underlying_provider="tiingo")

        def fake(symbol, timeframe, **kwargs):
            return HistoricalDataResult(
                bars=(),
                provider="openbb",
                underlying_provider="tiingo",
                requested_symbol=symbol,
                source_symbol=symbol,
                instrument_type="spot_fx_metals",
                timeframe="5m",
                requested_start=kwargs.get("start_ts"),
                requested_end=kwargs.get("end_ts"),
                actual_start=None,
                actual_end=None,
                errors=["rate_limit_or_empty"],
            )

        with patch.object(prov, "fetch_result", side_effect=fake):
            res = prov.fetch_chunked(
                "XAUUSD",
                "5m",
                start_ts=1000,
                end_ts=1000 + 2 * 86400,
                chunk_days=1,
                persist=False,
            )
        self.assertFalse(res.bars)
        self.assertTrue(res.errors)


class Phase17OrchestrationTests(unittest.TestCase):
    def test_run_phase17_blocks_without_credentials(self):
        with patch(
            "phase17_validate.provider_preflight",
            side_effect=lambda u, **k: {
                "ok": False,
                "provider": u,
                "credential_required": True,
                "credential_present": False,
                "route_available": True,
                "openbb_version": "4.7.2",
            },
        ):
            out = run_phase17(write_artifacts=False, deep_months=3)
        self.assertTrue(out["ok"])
        self.assertIsNone(out["provider_validation"]["accepted_provider"])
        self.assertFalse(out["deep_history"]["downloaded"])
        self.assertFalse(out["historical_replay"]["executed"])
        self.assertIn("credential", out["conclusion"].lower())


if __name__ == "__main__":
    unittest.main()
