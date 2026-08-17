"""Phase 18 tests — chronological split, denominators, candidate freeze."""

from __future__ import annotations

import unittest
from dataclasses import replace

from chrono_split import assert_no_split_leakage, chronological_split
from journal_models import HistoricalEntryResult, SetupJournalRecord
from phase18_eligibility import (
    ELIG_AMBIGUOUS,
    ELIG_RESOLVED,
    categorize_entry,
)
from phase18_metrics import (
    iter_entry_pairs,
    scorecard_from_pairs,
    theoretical_fixed_target_expectancy,
)
from phase18_selection import (
    StrategyCandidate,
    apply_expiry_policy,
    apply_htf_policy,
    classify_stability,
    rank_candidates,
    select_finalists,
)


def _entry(**kwargs) -> dict:
    base = {
        "mode": "boundary",
        "triggered": True,
        "entry_price": 100.0,
        "entry_timestamp": 1,
        "entry_depth": 0.5,
        "max_retrace_depth": 0.5,
        "stop_price": 90.0,
        "risk_distance": 10.0,
        "outcome": "STOP_HIT",
        "mfe_r": 0.5,
        "mae_r": 1.0,
        "ambiguity_flags": [],
        "event_timestamps": {},
        "fixed_rr_targets": [],
    }
    base.update(kwargs)
    return base


def _rec(trading_date: str, lid: str, **kwargs) -> dict:
    base = {
        "setup_id": f"{lid}|exec:5m",
        "liquidity_event_id": lid,
        "trading_date": trading_date,
        "session": "Asia",
        "execution_timeframe": "5m",
        "timeframe": "5m",
        "status": "ENTRY_READY",
        "setup_vs_daily": "aligned",
        "setup_vs_h4": "aligned",
        "htf_alignment": "aligned_bullish",
        "entry_results": [_entry()],
        "bars_sweep_to_choch": 3,
        "bars_choch_to_fvg": 2,
        "bars_fvg_to_entry": {"boundary": 4},
        "confirmation_timestamp": 10,
        "fvg_created_timestamp": 20,
        "sweep_timestamp": 1,
    }
    base.update(kwargs)
    return base


class TestChronoSplit(unittest.TestCase):
    def test_chronological_no_overlap(self):
        rows = []
        for i, d in enumerate(
            [
                "2026-05-01",
                "2026-05-02",
                "2026-05-03",
                "2026-05-04",
                "2026-05-05",
                "2026-05-06",
                "2026-05-07",
                "2026-05-08",
                "2026-05-09",
                "2026-05-10",
            ]
        ):
            rows.append(_rec(d, f"L{i}"))
        train, hold, split = chronological_split(rows, train_fraction=0.70)
        assert_no_split_leakage(split)
        self.assertTrue(split.train_end < split.holdout_start)
        train_dates = {r["trading_date"] for r in train}
        hold_dates = {r["trading_date"] for r in hold}
        self.assertFalse(train_dates & hold_dates)
        self.assertEqual(set(split.train_liquidity_event_ids) & set(split.holdout_liquidity_event_ids), set())

    def test_no_shuffle_order(self):
        rows = [_rec(f"2026-06-{i:02d}", f"L{i}") for i in range(1, 21)]
        train, hold, split = chronological_split(rows, train_fraction=0.70)
        self.assertEqual(train[0]["trading_date"], "2026-06-01")
        self.assertGreater(hold[0]["trading_date"], train[-1]["trading_date"])


class TestEligibilityDenominators(unittest.TestCase):
    def test_ambiguous_not_in_resolved(self):
        rec = _rec("2026-05-01", "L1")
        amb = _entry(outcome="AMBIGUOUS_INTRABAR", ambiguity_flags=["TRIGGER_BAR_STOP_AMBIGUITY"])
        self.assertEqual(categorize_entry(rec, amb), ELIG_AMBIGUOUS)
        pairs = iter_entry_pairs(
            [{**rec, "entry_results": [amb, _entry(outcome="1R_HIT", mode="ce")]}]
        )
        sc = scorecard_from_pairs(pairs)
        self.assertEqual(sc["ambiguous_n"], 1)
        self.assertEqual(sc["resolved_n"], 1)
        self.assertEqual(sc["r1_n"], 1)
        # ambiguity must not inflate stop/target rates denominator beyond resolved
        self.assertEqual(sc["stop_n"] + sc["r1_n"] <= sc["resolved_n"] + sc["r1_n"], True)

    def test_expectancy_denominator(self):
        e = theoretical_fixed_target_expectancy(
            target_r=2.0, target_hits=5, stop_hits=5, resolved_n=10
        )
        self.assertAlmostEqual(e, 0.5)
        self.assertIsNone(
            theoretical_fixed_target_expectancy(
                target_r=1.0, target_hits=0, stop_hits=0, resolved_n=0
            )
        )


class TestHTFPolicyEvalLayer(unittest.TestCase):
    def test_policies(self):
        r = _rec("2026-05-01", "L1", setup_vs_daily="aligned", setup_vs_h4="opposed")
        self.assertTrue(apply_htf_policy(r, "POLICY_A"))
        self.assertTrue(apply_htf_policy(r, "POLICY_B"))
        self.assertFalse(apply_htf_policy(r, "POLICY_C"))
        self.assertFalse(apply_htf_policy(r, "POLICY_D"))
        self.assertTrue(apply_htf_policy(r, "POLICY_E"))
        opposed = _rec("2026-05-01", "L2", setup_vs_daily="opposed", setup_vs_h4="opposed")
        self.assertFalse(apply_htf_policy(opposed, "POLICY_E"))


class TestExpiryDeterministic(unittest.TestCase):
    def test_timeout_counts(self):
        r = _rec("2026-05-01", "L1", bars_sweep_to_choch=10)
        self.assertEqual(
            apply_expiry_policy(r, confirmation_timeout=5, fvg_timeout=None, retrace_timeout=None),
            "EXPIRED_CONFIRMATION",
        )
        self.assertEqual(
            apply_expiry_policy(r, confirmation_timeout=20, fvg_timeout=None, retrace_timeout=None),
            "RETAINED",
        )


class TestCandidateSelection(unittest.TestCase):
    def test_small_sample_not_auto_selected(self):
        ranked = rank_candidates(
            [
                {
                    "candidate_id": "tiny",
                    "resolved_n": 5,
                    "ambiguity_pct": 0.1,
                    "theoretical_2r_expectancy": 2.0,
                },
                {
                    "candidate_id": "ok",
                    "resolved_n": 40,
                    "ambiguity_pct": 0.2,
                    "theoretical_2r_expectancy": 0.2,
                },
            ]
        )
        finals = select_finalists(ranked)
        ids = [f["candidate_id"] for f in finals]
        self.assertNotIn("tiny", ids)
        self.assertIn("ok", ids)

    def test_high_ambiguity_penalized(self):
        ranked = rank_candidates(
            [
                {
                    "candidate_id": "amb",
                    "resolved_n": 40,
                    "ambiguity_pct": 0.8,
                    "theoretical_2r_expectancy": 1.5,
                },
                {
                    "candidate_id": "clean",
                    "resolved_n": 40,
                    "ambiguity_pct": 0.1,
                    "theoretical_2r_expectancy": 0.3,
                },
            ]
        )
        self.assertEqual(ranked[0]["candidate_id"], "clean")
        self.assertIn("HIGH_AMBIGUITY", ranked[1]["selection_warnings"])

    def test_freeze_key_stable(self):
        a = StrategyCandidate("C1", "5m", "POLICY_A", "boundary", "beyond_sweep")
        b = StrategyCandidate("C1", "5m", "POLICY_A", "boundary", "beyond_sweep")
        self.assertEqual(a.freeze_key(), b.freeze_key())

    def test_stability_classes(self):
        train = {"resolved_n": 40, "theoretical_2r_expectancy": 0.4, "r1_rate": 0.5, "ambiguity_pct": 0.2}
        hold = {"resolved_n": 30, "theoretical_2r_expectancy": 0.3, "r1_rate": 0.45, "ambiguity_pct": 0.22}
        self.assertEqual(classify_stability(train, hold), "STABLE")
        tiny = {"resolved_n": 5, "theoretical_2r_expectancy": 1.0, "r1_rate": 1.0, "ambiguity_pct": 0.1}
        self.assertEqual(classify_stability(train, tiny), "INSUFFICIENT_HOLDOUT_SAMPLE")


class TestHoldoutExcludedFromSelection(unittest.TestCase):
    def test_selection_uses_train_only_contract(self):
        """Documented contract: finalists chosen from TRAIN scorecards only."""
        train_cards = [
            {"candidate_id": "A", "resolved_n": 50, "ambiguity_pct": 0.2, "theoretical_2r_expectancy": 0.4},
            {"candidate_id": "B", "resolved_n": 50, "ambiguity_pct": 0.2, "theoretical_2r_expectancy": -0.2},
        ]
        # Simulate holdout looking better for B — must not affect select_finalists input
        holdout_better_b = {
            "candidate_id": "B",
            "resolved_n": 50,
            "ambiguity_pct": 0.1,
            "theoretical_2r_expectancy": 2.0,
        }
        finals = select_finalists(rank_candidates(train_cards))
        self.assertEqual(finals[0]["candidate_id"], "A")
        self.assertNotEqual(finals[0]["candidate_id"], holdout_better_b["candidate_id"])


if __name__ == "__main__":
    unittest.main()
