"""Phase 48 — prop rule engine V1 tests. Does not modify frozen strategies."""
from __future__ import annotations

import json
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from account_state_engine import classify_account_state
from phase34_validate import GC_FILE_SHA, NQ_FILE_SHA, file_sha256
from prop_rule_engine import PropRuleEngine, evaluate_trade, is_benchmark_day
from prop_rules_v1 import (
    CHICAGO,
    AccountMetrics,
    MarketState,
    adjusted_required_profit,
    consistency_ratio,
    instrument_family,
    load_profile,
    load_rules_document,
    mffu_payout_unlocked,
    trail_eod_mll_equity,
    trail_eod_mll_pnl,
)
from risk_manager import propose_size

ROOT = Path(__file__).resolve().parent
MFFU = "MFFU_RAPID_EOD_50K"
FN = "FUNDEDNEXT_FLEX_50K"
ENGINE = PropRuleEngine()


def _ct(y, m, d, hh, mm=0) -> datetime:
    return datetime(y, m, d, hh, mm, tzinfo=CHICAGO)


class FrozenIsolationTests(unittest.TestCase):
    def test_frozen_hashes_unchanged(self):
        gc = json.loads((ROOT / "strategy_frozen" / "gc_vwap_v2_phase26.json").read_text(encoding="utf-8"))
        nq = json.loads((ROOT / "strategy_frozen" / "nq_dvp_phase30.json").read_text(encoding="utf-8"))
        self.assertEqual(gc["frozen_config_hash"], "0695d7a881b6247033426bceb077b6895b96405b826c21f32a109a02f44b2d43")
        self.assertEqual(nq["frozen_config_hash"], "935e3a616351b09dbfa8d2d0e2b5d6850be803fb385ec5e5f8e3593856a1212a")
        self.assertEqual(file_sha256(ROOT / "strategy_frozen" / "gc_vwap_v2_phase26.json"), GC_FILE_SHA)
        self.assertEqual(file_sha256(ROOT / "strategy_frozen" / "nq_dvp_phase30.json"), NQ_FILE_SHA)
        self.assertFalse((ROOT / "strategy_frozen" / "es_dvp_phase47.json").exists())


class NewsPolicyTests(unittest.TestCase):
    def test_mffu_eval_allows_t1_news(self):
        d = ENGINE.evaluate_trade(
            firm_profile=MFFU,
            account_stage="EVALUATION",
            instrument="MNQ",
            proposed_quantity=1,
            timestamp=_ct(2026, 8, 19, 10, 0),
            market_state=MarketState(is_tier1_news=True),
        )
        self.assertEqual(d.verdict, "ALLOW")

    def test_mffu_funded_rejects_t1_news(self):
        d = ENGINE.evaluate_trade(
            firm_profile=MFFU,
            account_stage="FUNDED",
            instrument="MNQ",
            proposed_quantity=1,
            timestamp=_ct(2026, 8, 19, 10, 0),
            market_state=MarketState(is_tier1_news=True),
        )
        self.assertEqual(d.verdict, "BLOCK")
        self.assertEqual(d.code, "BLOCK_NEWS")

    def test_fundednext_allows_news_challenge_and_funded(self):
        ts = _ct(2026, 8, 19, 10, 0)
        for stage in ("EVALUATION", "FUNDED"):
            d = ENGINE.evaluate_trade(
                firm_profile=FN,
                account_stage=stage,
                instrument="MES",
                proposed_quantity=1,
                timestamp=ts,
                market_state=MarketState(is_tier1_news=True),
            )
            self.assertEqual(d.verdict, "ALLOW", stage)


class FundedNextHoursTests(unittest.TestCase):
    def test_rejects_open_after_1510_ct(self):
        d = ENGINE.evaluate_trade(
            firm_profile=FN,
            account_stage="FUNDED",
            instrument="MNQ",
            proposed_quantity=1,
            timestamp=_ct(2026, 8, 19, 15, 30),
            action="OPEN",
        )
        self.assertEqual(d.verdict, "BLOCK")
        self.assertEqual(d.code, "BLOCK_TRADING_HOURS")

    def test_allows_close_after_1510_ct(self):
        d = ENGINE.evaluate_trade(
            firm_profile=FN,
            account_stage="FUNDED",
            instrument="MNQ",
            proposed_quantity=1,
            timestamp=_ct(2026, 8, 19, 15, 30),
            action="CLOSE",
        )
        self.assertEqual(d.verdict, "ALLOW")

    def test_resumes_after_1700_ct(self):
        d = ENGINE.evaluate_trade(
            firm_profile=FN,
            account_stage="CHALLENGE",
            instrument="MNQ",
            proposed_quantity=1,
            timestamp=_ct(2026, 8, 19, 17, 0),
            action="OPEN",
        )
        self.assertEqual(d.verdict, "ALLOW")


class InactivityTests(unittest.TestCase):
    def test_fundednext_lockout_after_30_calendar_days(self):
        last = _ct(2026, 7, 20, 10, 0)
        now = last + timedelta(days=30)
        metrics = AccountMetrics(last_trade_timestamp=last, remaining_drawdown=1000)
        snap = classify_account_state(firm_profile=FN, account_stage="FUNDED", metrics=metrics, now=now)
        self.assertEqual(snap.state, "FUNDED_LOCKOUT")
        self.assertEqual(snap.lockout_reason, "BLOCK_INACTIVITY")
        d = ENGINE.evaluate_trade(
            firm_profile=FN,
            account_stage="FUNDED",
            instrument="MNQ",
            proposed_quantity=1,
            timestamp=now,
            account_metrics=metrics,
        )
        self.assertEqual(d.code, "BLOCK_INACTIVITY")

    def test_mffu_inactivity_represented_as_7_days(self):
        ev = load_profile(MFFU).stage("EVALUATION")
        fd = load_profile(MFFU).stage("FUNDED")
        self.assertEqual(ev.get("inactivity_days"), 7)
        self.assertEqual(fd.get("inactivity_days"), 7)


class MllTests(unittest.TestCase):
    def test_mffu_funded_mll_trails_eod_only_and_never_down(self):
        mll, locked = trail_eod_mll_pnl(eod_pnl_high=0, previous_mll=-2000, locked=False)
        self.assertEqual(mll, -2000)
        self.assertFalse(locked)
        mll, locked = trail_eod_mll_pnl(eod_pnl_high=500, previous_mll=mll, locked=locked)
        self.assertEqual(mll, -1500)
        mll_down, _ = trail_eod_mll_pnl(eod_pnl_high=100, previous_mll=mll, locked=False)
        self.assertEqual(mll_down, -1500)

    def test_mffu_mll_stops_at_plus_100(self):
        mll, locked = trail_eod_mll_pnl(eod_pnl_high=2500, previous_mll=-2000, locked=False)
        self.assertEqual(mll, 100.0)
        self.assertTrue(locked)
        mll2, locked2 = trail_eod_mll_pnl(eod_pnl_high=5000, previous_mll=mll, locked=True)
        self.assertEqual(mll2, 100.0)
        self.assertTrue(locked2)

    def test_fundednext_mll_stops_at_50100(self):
        mll, locked = trail_eod_mll_equity(eod_equity=48500, previous_mll=48500, locked=False)
        self.assertEqual(mll, 48500)
        mll, locked = trail_eod_mll_equity(eod_equity=52000, previous_mll=mll, locked=False)
        self.assertEqual(mll, 50100.0)
        self.assertTrue(locked)
        mll2, _ = trail_eod_mll_equity(eod_equity=60000, previous_mll=mll, locked=True)
        self.assertEqual(mll2, 50100.0)


class ConsistencyTests(unittest.TestCase):
    def test_fundednext_40_percent_target_adjustment(self):
        self.assertEqual(adjusted_required_profit(base_target=2500, highest_profitable_day=1500, ratio_max=0.40), 3750)
        self.assertEqual(adjusted_required_profit(base_target=2500, highest_profitable_day=1000, ratio_max=0.40), 2500)

    def test_mffu_30_percent_consistency(self):
        self.assertAlmostEqual(consistency_ratio(900, 3000), 0.30)
        self.assertEqual(adjusted_required_profit(base_target=3000, highest_profitable_day=900, ratio_max=0.30), 3000)
        self.assertEqual(adjusted_required_profit(base_target=3000, highest_profitable_day=1200, ratio_max=0.30), 4000)


class ContractLimitTests(unittest.TestCase):
    def test_rejects_more_than_3_minis(self):
        d = ENGINE.evaluate_trade(
            firm_profile=MFFU,
            account_stage="EVALUATION",
            instrument="NQ",
            proposed_quantity=4,
            timestamp=_ct(2026, 8, 19, 10, 0),
        )
        self.assertEqual(d.code, "BLOCK_CONTRACT_LIMIT")

    def test_rejects_more_than_30_micros(self):
        d = ENGINE.evaluate_trade(
            firm_profile=FN,
            account_stage="FUNDED",
            instrument="MES",
            proposed_quantity=31,
            timestamp=_ct(2026, 8, 19, 10, 0),
        )
        self.assertEqual(d.code, "BLOCK_CONTRACT_LIMIT")

    def test_allows_3_minis_or_30_micros(self):
        a = ENGINE.evaluate_trade(
            firm_profile=MFFU,
            account_stage="EVALUATION",
            instrument="ES",
            proposed_quantity=3,
            timestamp=_ct(2026, 8, 19, 10, 0),
        )
        b = ENGINE.evaluate_trade(
            firm_profile=MFFU,
            account_stage="EVALUATION",
            instrument="MGC",
            proposed_quantity=30,
            timestamp=_ct(2026, 8, 19, 10, 0),
        )
        self.assertEqual(a.verdict, "ALLOW")
        self.assertEqual(b.verdict, "ALLOW")

    def test_instrument_families(self):
        self.assertEqual(instrument_family("MNQ"), "NQ")
        self.assertEqual(instrument_family("MES"), "ES")
        self.assertEqual(instrument_family("MGC"), "GC")
        self.assertEqual(instrument_family("MCL"), "CL")


class BenchmarkAndPayoutTests(unittest.TestCase):
    def test_fundednext_200_benchmark_day(self):
        self.assertTrue(is_benchmark_day(200))
        self.assertTrue(is_benchmark_day(201))
        self.assertFalse(is_benchmark_day(199.99))

    def test_mffu_first_payout_not_below_2100(self):
        self.assertFalse(mffu_payout_unlocked(realized_pnl=2099.99, first_payout_completed=False, net_profit_since_last_payout=0))
        self.assertTrue(mffu_payout_unlocked(realized_pnl=2100, first_payout_completed=False, net_profit_since_last_payout=0))

    def test_mffu_later_payout_not_below_500(self):
        self.assertFalse(mffu_payout_unlocked(realized_pnl=5000, first_payout_completed=True, net_profit_since_last_payout=499.99))
        self.assertTrue(mffu_payout_unlocked(realized_pnl=5000, first_payout_completed=True, net_profit_since_last_payout=500))


class UnknownRuleAndPriceLimitTests(unittest.TestCase):
    def test_unknown_profile_fails_safe(self):
        d = ENGINE.evaluate_trade(
            firm_profile="NOT_A_FIRM",
            account_stage="EVALUATION",
            instrument="MNQ",
            proposed_quantity=1,
            timestamp=_ct(2026, 8, 19, 10, 0),
        )
        self.assertEqual(d.code, "BLOCK_UNKNOWN_RULE")

    def test_alternative_rapid_standard_fails_safe(self):
        d = ENGINE.evaluate_trade(
            firm_profile="MFFU_RAPID_STANDARD_50K",
            account_stage="EVALUATION",
            instrument="MNQ",
            proposed_quantity=1,
            timestamp=_ct(2026, 8, 19, 10, 0),
        )
        self.assertEqual(d.code, "BLOCK_UNKNOWN_RULE")

    def test_fundednext_price_limit_zone_blocks(self):
        d = ENGINE.evaluate_trade(
            firm_profile=FN,
            account_stage="FUNDED",
            instrument="NQ",
            proposed_quantity=1,
            timestamp=_ct(2026, 8, 19, 10, 0),
            market_state=MarketState(in_cme_price_limit_zone=True),
        )
        self.assertEqual(d.code, "BLOCK_PRICE_LIMIT_ZONE")

    def test_mffu_price_limit_unstated_fails_safe_when_in_zone(self):
        d = ENGINE.evaluate_trade(
            firm_profile=MFFU,
            account_stage="EVALUATION",
            instrument="NQ",
            proposed_quantity=1,
            timestamp=_ct(2026, 8, 19, 10, 0),
            market_state=MarketState(in_cme_price_limit_zone=True),
        )
        self.assertEqual(d.code, "BLOCK_UNKNOWN_RULE")

    def test_risk_manager_prop_qty_locked_dry_run(self):
        out = propose_size(instrument="MNQ")
        self.assertEqual(out["status"], "PROP_QTY_LOCKED")
        self.assertEqual(out["quantity"], 2)
        self.assertFalse(out["broker_execution"])
        self.assertEqual(out["execution_default"], "DRY_RUN")

    def test_rules_document_primary_profiles(self):
        doc = load_rules_document()
        self.assertEqual(doc["schema_version"], "PROP_RULES_V1")
        self.assertEqual(doc["execution_default"], "DRY_RUN")
        self.assertFalse(doc["broker_execution"])
        self.assertIn(MFFU, doc["primary_profiles"])
        self.assertIn(FN, doc["primary_profiles"])
        alt = doc["profiles"]["MFFU_RAPID_STANDARD_50K"]
        self.assertEqual(alt["status"], "ALTERNATIVE_RESEARCH_PROFILE")
        self.assertFalse(alt["primary"])


if __name__ == "__main__":
    unittest.main()
