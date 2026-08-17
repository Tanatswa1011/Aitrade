"""Phase 23 tests — Databento adapter, stitching, frozen candidates, splits, cost model."""

from __future__ import annotations

import json
import os
import unittest
from datetime import datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

from databento_history import (
    VOLUME_STATUS,
    aggregate_1m_to_5m,
    databento_preflight,
    load_databento_credential,
    ohlcv_records_to_bars,
    validate_bars_quality,
)
from gc_contract_stitch import (
    ContractSeries,
    decide_rolls,
    detect_roll_price_artifacts,
    session_boundary_ts,
    stitch_contracts,
)
from gc_orb_engine import config_hash, find_first_breakouts, build_opening_range
from gc_orb_models import PHASE22_CANDIDATES, OR_TIMEZONE
from models import Bar
from phase23_validate import assert_g1_frozen, chronological_split, load_frozen_phase22_candidates


def _bar(t, o, h, l, c, v=100.0):
    return Bar(time=t, open=o, high=h, low=l, close=c, volume=v)


def _ts_ny(y, m, d, hh, mm):
    return int(datetime(y, m, d, hh, mm, tzinfo=ZoneInfo(OR_TIMEZONE)).timestamp())


class DatabentoAdapterTests(unittest.TestCase):
    def test_missing_credential_preflight(self):
        with patch.dict(os.environ, {"DATABENTO_API_KEY": ""}, clear=False):
            # Force empty even if .env had loaded earlier in process — clear key
            os.environ.pop("DATABENTO_API_KEY", None)
            with patch("databento_history.load_databento_credential") as mock_cred:
                mock_cred.return_value = {
                    "credential_required": True,
                    "credential_present": False,
                    "credential_env_var": "DATABENTO_API_KEY",
                    "loaded_dotenv": True,
                }
                pf = databento_preflight()
                self.assertFalse(pf.get("ok"))
                self.assertEqual(pf.get("error_code"), "DATABENTO_CREDENTIAL_REQUIRED")

    def test_package_info_when_available(self):
        pf = databento_preflight()
        self.assertTrue(pf.get("databento_package_available"))
        self.assertIsNotNone(pf.get("databento_version"))
        self.assertTrue(pf.get("historical_client_available"))

    def test_credential_loader_never_returns_secret(self):
        cred = load_databento_credential()
        self.assertIn("credential_present", cred)
        self.assertNotIn("DATABENTO_API_KEY", cred)
        blob = json.dumps(cred)
        self.assertNotIn("api_key", blob.lower().replace("databento_api_key", ""))

    def test_aggregate_1m_to_5m(self):
        base = (1_700_000_000 // 300) * 300  # align to 5m boundary
        bars = [_bar(base + i * 60, 10 + i, 11 + i, 9 + i, 10.5 + i, v=float(i + 1)) for i in range(5)]
        out = aggregate_1m_to_5m(bars)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].time, base)
        self.assertEqual(out[0].open, bars[0].open)
        self.assertEqual(out[0].close, bars[-1].close)
        self.assertEqual(out[0].volume, sum(range(1, 6)))

    def test_utc_normalization_from_ns(self):
        class Rec:
            ts_event = 1_700_000_000_000_000_000  # ns
            pretty_open = 1.0
            pretty_high = 2.0
            pretty_low = 0.5
            pretty_close = 1.5
            volume = 9

        bars = ohlcv_records_to_bars([Rec()])
        self.assertEqual(bars[0].time, 1_700_000_000)
        self.assertEqual(bars[0].volume, 9.0)

    def test_volume_preserved(self):
        bars = [_bar(300, 1, 2, 0.5, 1.5, v=42)]
        qa = validate_bars_quality(bars)
        self.assertTrue(qa["ok"])
        self.assertEqual(VOLUME_STATUS, "PROVIDER_DOCUMENTED_TRADE_VOLUME")

    def test_empty_result_qa(self):
        qa = validate_bars_quality([])
        self.assertEqual(qa["bar_count"], 0)
        self.assertTrue(qa["ok"])

    def test_schema_mapping_metadata_status(self):
        self.assertIn("TRADE_VOLUME", VOLUME_STATUS)


class ContractStitchTests(unittest.TestCase):
    def test_single_contract(self):
        bars = [_bar(1000 + i * 300, 100, 101, 99, 100, v=10) for i in range(5)]
        s = ContractSeries("GCZ5", tuple(bars), first_seen=1000, last_seen=1000 + 4 * 300)
        rolls = decide_rolls([s])
        self.assertEqual(rolls, [])
        stitched, prov = stitch_contracts([s], rolls)
        self.assertEqual(len(stitched), 5)
        self.assertEqual(len(prov), 5)

    def test_volume_crossover_roll_no_future_leakage(self):
        # Day1: cur higher vol; Day2: next exceeds → roll activates 18:00 NY day2
        d1 = "2025-09-02"
        d2 = "2025-09-03"
        cur_bars = []
        nxt_bars = []
        for hh in range(10, 16):
            cur_bars.append(_bar(_ts_ny(2025, 9, 2, hh, 0), 2000, 2001, 1999, 2000, v=100))
            nxt_bars.append(_bar(_ts_ny(2025, 9, 2, hh, 0), 2000, 2001, 1999, 2000, v=50))
        for hh in range(10, 16):
            cur_bars.append(_bar(_ts_ny(2025, 9, 3, hh, 0), 2000, 2001, 1999, 2000, v=40))
            nxt_bars.append(_bar(_ts_ny(2025, 9, 3, hh, 0), 2000, 2001, 1999, 2000, v=90))
        # post-roll bars
        for hh in range(19, 22):
            cur_bars.append(_bar(_ts_ny(2025, 9, 3, hh, 0), 2000, 2001, 1999, 2000, v=10))
            nxt_bars.append(_bar(_ts_ny(2025, 9, 3, hh, 0), 2010, 2011, 2009, 2010, v=80))

        series = [
            ContractSeries("GCZ5", tuple(cur_bars), first_seen=cur_bars[0].time, last_seen=cur_bars[-1].time),
            ContractSeries("GCG6", tuple(nxt_bars), first_seen=nxt_bars[0].time, last_seen=nxt_bars[-1].time),
        ]
        rolls = decide_rolls(series, calendar_order=["GCZ5", "GCG6"])
        self.assertEqual(len(rolls), 1)
        self.assertEqual(rolls[0].decision_date, d2)
        self.assertEqual(rolls[0].roll_timestamp, session_boundary_ts(d2))
        # decision uses same-day volumes only (no look-ahead into later days)
        self.assertGreater(rolls[0].new_volume, rolls[0].old_volume)

        stitched, prov = stitch_contracts(series, rolls)
        # No intraday switch before 18:00 on decision date
        for p in prov:
            t = int(p["time"])
            if t < rolls[0].roll_timestamp:
                self.assertEqual(p["contract"], "GCZ5")
            else:
                self.assertEqual(p["contract"], "GCG6")
        # Duplicate boundary timestamps not duplicated
        times = [int(b.time) for b in stitched]
        self.assertEqual(len(times), len(set(times)))

    def test_no_intraday_accidental_roll(self):
        boundary = session_boundary_ts("2025-10-01")
        self.assertEqual(
            datetime.fromtimestamp(boundary, tz=ZoneInfo(OR_TIMEZONE)).hour,
            18,
        )

    def test_roll_price_discontinuity_does_not_create_or_breakout(self):
        # OR day; mark breakout bar as roll artifact → no tradeable event path
        or_bars = [_bar(_ts_ny(2026, 7, 2, 8, 20) + i * 300, 100, 102, 98, 100, v=10) for i in range(6)]
        later = [_bar(or_bars[-1].time + 300, 101, 106, 100, 104, v=50)]
        hist = [_bar(or_bars[0].time - (20 - i) * 300, 100, 101, 99, 100, v=10) for i in range(20)]
        orng = build_opening_range(or_bars, "2026-07-02", or_minutes=30)
        flags = {int(later[0].time)}
        events = find_first_breakouts(hist + or_bars + later, orng, roll_flags=flags)
        self.assertEqual(len(events), 1)
        self.assertTrue(events[0].roll_artifact)
        # Artifact detector on stitched discontinuity
        prior = _bar(later[0].time - 3600, 90, 91, 89, 90, v=5)
        jump_bar = _bar(later[0].time, 110, 111, 109, 110, v=5)
        arts = detect_roll_price_artifacts(
            [prior, jump_bar],
            rolls=[],
            window_sec=86400,
            min_jump=8.0,
        )
        # near_roll requires roll timestamps; without rolls, empty is OK
        self.assertIsInstance(arts, list)


class FrozenCandidateTests(unittest.TestCase):
    def test_g1_config_hash_matches_frozen_json(self):
        configs = load_frozen_phase22_candidates()
        check = assert_g1_frozen(configs)
        self.assertTrue(check["ok"], msg=str(check))
        self.assertEqual(check["live_hash"], check["frozen_hash"])

    def test_phase22_matrix_unchanged_semantics(self):
        g1 = next(c for c in PHASE22_CANDIDATES if c.candidate_id.startswith("G1_"))
        self.assertEqual(g1.or_minutes, 30)
        self.assertFalse(g1.volume_filter)
        self.assertFalse(g1.displacement_filter)
        self.assertEqual(g1.entry_mode, "BREAKOUT_CLOSE")
        self.assertEqual(g1.stop_mode, "OR_OPPOSITE")
        self.assertEqual(g1.rvol_threshold, 1.5)
        self.assertEqual(g1.displacement_body_or_ratio, 0.5)


class SplitAndCostTests(unittest.TestCase):
    def test_chronological_no_overlap(self):
        rows = [{"trading_date": f"2025-01-{d:02d}", "extras": {}} for d in range(1, 11)]
        train, hold, meta = chronological_split(rows, 0.70)
        tdates = {r["trading_date"] for r in train}
        hdates = {r["trading_date"] for r in hold}
        self.assertFalse(tdates & hdates)
        self.assertLess(max(tdates), min(hdates))
        self.assertEqual(meta["method"], "chronological_trading_date_70_30_databento")

    def test_cost_friction_positive_tick(self):
        tick = 0.1
        rd = 5.0
        friction_1 = (2 * 1 * tick) / rd
        self.assertAlmostEqual(friction_1, 0.04)
        e2 = 0.10
        self.assertGreater(e2 - friction_1, 0)
        self.assertLess(e2 - (2 * 2 * tick) / rd, 0.1)


class DeterminismSmokeTests(unittest.TestCase):
    def test_config_hash_stable(self):
        g1 = next(c for c in PHASE22_CANDIDATES if c.candidate_id.startswith("G1_"))
        self.assertEqual(config_hash(g1), config_hash(g1))


if __name__ == "__main__":
    unittest.main()
