"""Unit tests for NinjaTrader Sim101 ATI bridge — no live OIF submission."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import nt_ati as nt


class SafetyLockTests(unittest.TestCase):
    def test_sim101_only(self):
        with self.assertRaises(PermissionError) as ctx:
            nt.assert_sim101("Live1")
        self.assertIn("LIVE_ACCOUNT_BLOCKED", str(ctx.exception))
        with self.assertRaises(PermissionError):
            nt.assert_sim101("Sim102")
        nt.assert_sim101("Sim101")

    def test_mnq_only(self):
        with self.assertRaises(PermissionError) as ctx:
            nt.assert_mnq_instrument("NQ SEP26")
        self.assertIn("REFUSED_FULL_SIZE_NQ", str(ctx.exception))
        with self.assertRaises(PermissionError):
            nt.assert_mnq_instrument("ES SEP26")
        nt.assert_mnq_instrument("MNQ SEP26")

    def test_qty_one(self):
        with self.assertRaises(PermissionError):
            nt.assert_qty_one(2)
        nt.assert_qty_one(1)

    def test_build_place_rejects_live(self):
        with self.assertRaises(PermissionError) as ctx:
            nt.build_place_oif(account="Prop123", action="BUY")
        self.assertIn("LIVE_ACCOUNT_BLOCKED", str(ctx.exception))

    def test_no_live_account_path_in_flatten(self):
        line = nt.build_close_position_oif()
        self.assertIn("Sim101", line)
        self.assertNotIn("Live", line)
        with self.assertRaises(PermissionError):
            nt.build_close_position_oif(account="LiveAccount")


class TickAndBracketMathTests(unittest.TestCase):
    def test_tick_rounding(self):
        self.assertEqual(nt.round_to_tick(9000.13), 9000.25)
        self.assertEqual(nt.round_to_tick(9000.12), 9000.00)
        self.assertTrue(nt.validate_tick_aligned(9000.25))
        self.assertFalse(nt.validate_tick_aligned(9000.1))

    def test_long_stop_target(self):
        px = nt.long_bracket_prices(9000.25, 5.0)
        self.assertEqual(px["stop"], 8995.25)
        self.assertEqual(px["target"], 9005.25)

    def test_short_stop_target(self):
        px = nt.short_bracket_prices(9000.25, 5.0)
        self.assertEqual(px["stop"], 9005.25)
        self.assertEqual(px["target"], 8995.25)

    def test_unique_oco_ids(self):
        a = nt.new_oco_id()
        b = nt.new_oco_id()
        self.assertNotEqual(a, b)
        self.assertTrue(a.startswith("AITRADE_TEST_"))


class OifFormatTests(unittest.TestCase):
    def test_buy_sell_still_builds(self):
        buy = nt.build_place_oif(action="BUY", order_id="AITRADE_X")
        sell = nt.build_place_oif(action="SELL", order_id="AITRADE_Y")
        self.assertTrue(buy.startswith("PLACE;Sim101;MNQ SEP26;BUY;1;MARKET;"))
        self.assertTrue(sell.startswith("PLACE;Sim101;MNQ SEP26;SELL;1;MARKET;"))
        self.assertEqual(buy.count(";"), 12)

    def test_child_oco_lines(self):
        kids = nt.build_bracket_child_oifs(
            direction="LONG",
            entry_fill=9000.0,
            oco_id="AITRADE_TEST_abc",
            stop_order_id="STOP1",
            target_order_id="TGT1",
        )
        self.assertIn("STOPMARKET", kids["stop_line"])
        self.assertIn("8995.0", kids["stop_line"])
        self.assertIn("AITRADE_TEST_abc", kids["stop_line"])
        self.assertIn("AITRADE_TEST_abc", kids["target_line"])
        self.assertIn("LIMIT", kids["target_line"])
        self.assertEqual(kids["prices"]["stop"], 8995.0)
        self.assertEqual(kids["prices"]["target"], 9005.0)

    def test_flatten_generation(self):
        out = nt.flatten_sim(submit=False)
        self.assertFalse(out["submitted"])
        self.assertTrue(any(x.startswith("CLOSEPOSITION;Sim101;MNQ SEP26") for x in out["oif_lines"]))
        self.assertEqual(out["account"], "Sim101")


class GuardTests(unittest.TestCase):
    def test_flat_before_entry_guard(self):
        lines = [
            "2026-08-17 00:00:00|1|64|Instrument='MNQ SEP26' Account='Sim101' "
            "Average price=9000 Quantity=1 Market position=Long Operation=Operation_Add"
        ]
        g = nt.guard_ready_for_bracket(log_lines=lines, active_path=Path(tempfile.mktemp()))
        self.assertFalse(g["ok"])
        self.assertEqual(g["error_code"], "NOT_FLAT")

    def test_flat_ok(self):
        lines = [
            "2026-08-17 00:00:00|1|64|Instrument='MNQ SEP26' Account='Sim101' "
            "Average price=0 Quantity=0 Market position=Flat Operation=Remove"
        ]
        with tempfile.TemporaryDirectory() as td:
            ap = Path(td) / "active.json"
            g = nt.guard_ready_for_bracket(log_lines=lines, active_path=ap)
            self.assertTrue(g["ok"])

    def test_duplicate_active_guard(self):
        lines = [
            "2026-08-17 00:00:00|1|64|Instrument='MNQ SEP26' Account='Sim101' "
            "Average price=0 Quantity=0 Market position=Flat Operation=Remove"
        ]
        with tempfile.TemporaryDirectory() as td:
            ap = Path(td) / "active.json"
            nt.save_active_state(
                {
                    "status": "BRACKET_ARMED",
                    "test_id": "t1",
                    "entry_order_id": "e1",
                    "stop_order_id": "s1",
                    "target_order_id": "t1",
                },
                path=ap,
            )
            g = nt.guard_ready_for_bracket(log_lines=lines, active_path=ap)
            self.assertFalse(g["ok"])
            self.assertEqual(g["error_code"], "TEST_ORDER_STATE_UNSAFE")


class DryRunBracketTests(unittest.TestCase):
    def test_bracket_dry_run_does_not_submit(self):
        with mock.patch.object(nt, "drop_oif") as drop:
            with mock.patch.object(nt, "drop_oif_lines") as drop_lines:
                out = nt.run_bracket_test("LONG", submit=False)
                self.assertEqual(out["status"], "DRY_RUN")
                self.assertFalse(out["submitted"])
                self.assertEqual(out["mechanism"], nt.BRACKET_MECHANISM)
                drop.assert_not_called()
                drop_lines.assert_not_called()

    def test_place_sim_dry_run(self):
        with mock.patch.object(nt, "drop_oif") as drop:
            out = nt.place_sim_mnq_test(action="BUY", submit=False)
            self.assertFalse(out["submitted"])
            drop.assert_not_called()

    def test_fill_parser_maps_ati_to_nt_uuid(self):
        ati = "AITRADE_ENTRY_bf28c4802f5d"
        nt_uuid = "e1d39243ae71422195ddee34920e0492"
        lines = [
            f"2026-08-17 00:39:57:133|1|1|OIF, 'PLACE;Sim101;MNQ SEP26;BUY;1;MARKET;0;0;DAY;;{ati};;' processing",
            "2026-08-17 00:39:57:133|1|1|Submitting order without strategy...",
            f"2026-08-17 00:39:57:162|1|32|Order='{nt_uuid}/Sim101' Name='' New state='Submitted' "
            "Instrument='MNQ SEP26' Action='Buy' Limit price=0 Stop price=0 Quantity=1 "
            "Type='Market' Time in force=DAY Oco='' Filled=0 Fill price=0 Error='No error' Native error=''",
            f"2026-08-17 00:39:57:290|1|32|Order='{nt_uuid}/Sim101' Name='' New state='Filled' "
            "Instrument='MNQ SEP26' Action='Buy' Limit price=0 Stop price=0 Quantity=1 "
            "Type='Market' Time in force=DAY Oco='' Filled=1 Fill price=9006 Error='No error' Native error=''",
        ]
        hit = nt.parse_fill_for_order_id(ati, lines=lines)
        self.assertIsNotNone(hit)
        self.assertEqual(hit["fill_price"], 9006.0)
        self.assertEqual(hit["nt_order_id"], nt_uuid)

    def test_stacked_oif_maps_distinct_nt_ids(self):
        stop_ati = "AITRADE_STOP_aaa"
        tgt_ati = "AITRADE_TGT_bbb"
        oco = "AITRADE_TEST_oco"
        stop_nt = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        tgt_nt = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
        # NT often submits Limit before Stop Market even if OIF lists stop first
        lines = [
            f"OIF, 'PLACE;Sim101;MNQ SEP26;SELL;1;STOPMARKET;0;9008.75;DAY;{oco};{stop_ati};;' processing",
            f"OIF, 'PLACE;Sim101;MNQ SEP26;SELL;1;LIMIT;9018.75;0;DAY;{oco};{tgt_ati};;' processing",
            f"Order='{tgt_nt}/Sim101' Name='' New state='Submitted' Instrument='MNQ SEP26' "
            f"Action='Sell' Limit price=9018.75 Stop price=0 Quantity=1 Type='Limit'",
            f"Order='{stop_nt}/Sim101' Name='' New state='Submitted' Instrument='MNQ SEP26' "
            f"Action='Sell' Limit price=0 Stop price=9008.75 Quantity=1 Type='Stop Market'",
        ]
        self.assertEqual(nt.parse_nt_order_id_after_oif(stop_ati, lines=lines), stop_nt)
        self.assertEqual(nt.parse_nt_order_id_after_oif(tgt_ati, lines=lines), tgt_nt)


if __name__ == "__main__":
    unittest.main()
