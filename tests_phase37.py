"""Phase 37 frozen-isolation, signing, and leak-safety tests."""
from __future__ import annotations

import json
import unittest
from pathlib import Path

from nq_executed_flow_features import flow_from_trades, parse_trade, sign_trade
from nq_microstructure_models import SweepEvent
from nq_pdh_pdl import local_ts
from phase34_validate import GC_FILE_SHA, NQ_FILE_SHA, file_sha256

ROOT = Path(__file__).resolve().parent


def _event(*, side="pdl_sweep", t0=None, extreme=99.0, level=100.0, pen=5.0):
    t0 = int(t0 if t0 is not None else local_ts("2026-01-02", "09:30"))
    rth_open = local_ts("2026-01-02", "09:30")
    return SweepEvent(
        event_id="t",
        trading_date="2026-01-02",
        side=side,
        level=level,
        sweep_bar_time=t0,
        sweep_ts=t0,
        extreme=extreme,
        penetration_points=pen,
        rth_open_ts=rth_open,
        seconds_from_rth_open=t0 - rth_open,
        atr_1m_14=2.0,
        volume_sweep_bar=1000,
        prior_rth_high=101.0,
        prior_rth_low=level,
        extras={"contract": "NQM6"},
    )


def _tr(ts, px, sz, side):
    return {"ts_event": ts, "pretty_price": px, "size": sz, "side": side}


class FrozenIsolationTests(unittest.TestCase):
    def test_frozen_hashes_unchanged(self):
        gc = json.loads((ROOT / "strategy_frozen" / "gc_vwap_v2_phase26.json").read_text(encoding="utf-8"))
        nq = json.loads((ROOT / "strategy_frozen" / "nq_dvp_phase30.json").read_text(encoding="utf-8"))
        self.assertEqual(gc["frozen_config_hash"], "0695d7a881b6247033426bceb077b6895b96405b826c21f32a109a02f44b2d43")
        self.assertEqual(nq["frozen_config_hash"], "935e3a616351b09dbfa8d2d0e2b5d6850be803fb385ec5e5f8e3593856a1212a")
        self.assertEqual(file_sha256(ROOT / "strategy_frozen" / "gc_vwap_v2_phase26.json"), GC_FILE_SHA)
        self.assertEqual(file_sha256(ROOT / "strategy_frozen" / "nq_dvp_phase30.json"), NQ_FILE_SHA)

    def test_spec_reuses_phase35_and_bans_dom(self):
        spec = json.loads((ROOT / "phase37_spec.json").read_text(encoding="utf-8"))
        self.assertEqual(spec["methodology_corrections"], [])
        self.assertEqual(spec["carried_forward"]["shallow_points"], 18.25)
        self.assertIn("NOT IN THE LIVE FEATURE SET", spec["dom"])
        self.assertTrue(spec["windows"]["classification_only_le_cutoff"])
        self.assertEqual(spec["trade_signing"]["ASK_A"], "AGGRESSIVE_SELL")
        self.assertEqual(spec["trade_signing"]["BID_B"], "AGGRESSIVE_BUY")


class SigningTests(unittest.TestCase):
    def test_ask_is_aggressive_sell_bid_is_aggressive_buy(self):
        self.assertEqual(sign_trade({"side": "A"}), ("AGGRESSIVE_SELL", -1))
        self.assertEqual(sign_trade({"side": "ASK"}), ("AGGRESSIVE_SELL", -1))
        self.assertEqual(sign_trade({"side": "B"}), ("AGGRESSIVE_BUY", 1))
        self.assertEqual(sign_trade({"side": "BID"}), ("AGGRESSIVE_BUY", 1))
        self.assertEqual(sign_trade({"side": "N"}), ("UNSIGNED", 0))
        self.assertEqual(sign_trade({"side": None}), ("UNSIGNED", 0))

    def test_unsigned_excluded_from_delta(self):
        t0 = local_ts("2026-01-02", "09:30")
        recs = [
            _tr(t0 + 1, 99.5, 10, "B"),
            _tr(t0 + 2, 99.4, 4, "A"),
            _tr(t0 + 3, 99.4, 50, "N"),
        ]
        feats = flow_from_trades(recs, _event(t0=t0))
        self.assertEqual(feats["sweep_60_abv"], 10)
        self.assertEqual(feats["sweep_60_asv"], 4)
        self.assertEqual(feats["sweep_60_unsigned_vol"], 50)
        self.assertEqual(feats["sweep_60_delta"], 6)
        self.assertEqual(feats["sweep_60_n_unsigned"], 1)


class LeakSafetyTests(unittest.TestCase):
    def test_post_cutoff_trades_do_not_enter_classification_delta(self):
        t0 = local_ts("2026-01-02", "09:30")
        t_cut = t0 + 60
        recs = [
            _tr(t0 + 10, 99.0, 8, "A"),
            _tr(t_cut + 5, 100.5, 100, "B"),
        ]
        feats = flow_from_trades(recs, _event(t0=t0, pen=1.0))
        self.assertEqual(feats["sweep_60_asv"], 8)
        self.assertEqual(feats["sweep_60_abv"], 0)
        self.assertEqual(feats["n_trades_le_cutoff"], 1)
        self.assertEqual(feats["n_trades_post_cutoff_in_cache"], 1)
        self.assertEqual(feats["post_60_abv"], 100)
        self.assertLess(feats["ndelta_rev_sweep60"], 0)  # PDL, net selling during bar

    def test_pdl_buying_is_reversal_aligned(self):
        t0 = local_ts("2026-01-02", "09:30")
        recs = [_tr(t0 + 1, 99.0, 20, "B"), _tr(t0 + 2, 99.0, 5, "A")]
        feats = flow_from_trades(recs, _event(t0=t0, side="pdl_sweep"))
        self.assertGreater(feats["ndelta_rev_sweep60"], 0)
        self.assertEqual(feats["delta_divergence"], 1.0)

    def test_pdh_selling_is_reversal_aligned(self):
        t0 = local_ts("2026-01-02", "09:30")
        recs = [_tr(t0 + 1, 101.0, 20, "A"), _tr(t0 + 2, 101.0, 5, "B")]
        feats = flow_from_trades(recs, _event(t0=t0, side="pdh_sweep", extreme=101.0, level=100.0))
        self.assertGreater(feats["ndelta_rev_sweep60"], 0)

    def test_parse_ignores_future_quotes_not_used(self):
        row = parse_trade({"ts_event": 1_700_000_000, "pretty_price": 1.0, "size": 1, "side": "B"})
        self.assertIsNotNone(row)
        self.assertEqual(row[3], "AGGRESSIVE_BUY")


if __name__ == "__main__":
    unittest.main()
