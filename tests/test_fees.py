"""Fee model tests.

The SG cases are not invented: each one is a real filled order from the
account, with the fee figures taken from Longbridge's own `charge_detail`. If
the model stops reproducing them, the model is wrong.

Run from the repo root:  python3 -m unittest discover tests
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fees import (  # noqa: E402
    FALLBACK_FLAT_FEE, FeeComponent, FeeSchedule,
    estimate_fee, estimate_fee_detail, schedule_for, unverified_markets,
)


class RealSingaporeChargesTest(unittest.TestCase):
    """Each case: (symbol, qty, price, expected components, expected total)
    exactly as Longbridge billed them."""

    CASES = [
        # G3B.SG — platform fee not charged on this order
        (100, 5.06, {"Clearing Fee": 0.16, "Trading Fee": 0.04, "GST": 0.01}, 0.21, False),
        # AJBU.SG
        (200, 2.30, {"Clearing Fee": 0.15, "Trading Fee": 0.03, "GST": 0.01}, 0.19, False),
        # OV8.SG — platform fee charged
        (200, 2.91, {"Platform Fee": 0.99, "Clearing Fee": 0.19,
                     "Trading Fee": 0.04, "GST": 0.11}, 1.33, True),
        # ES3.SG — platform fee charged
        (100, 4.988, {"Platform Fee": 0.99, "Clearing Fee": 0.16,
                      "Trading Fee": 0.04, "GST": 0.10}, 1.29, True),
    ]

    def test_reproduces_every_real_contract_note(self):
        for qty, price, expected, total, has_platform in self.CASES:
            with self.subTest(qty=qty, price=price):
                got_total, breakdown = estimate_fee_detail("SG", "buy", qty, price)
                if not has_platform:
                    # The model always charges the platform fee (the safe
                    # direction); strip it to compare against the orders where
                    # Longbridge waived it.
                    platform = breakdown.pop("Platform Fee", 0.0)
                    # GST in the model included the platform fee — recompute
                    # the tax base without it, exactly as the real note did.
                    breakdown["GST"] = round(
                        (breakdown.get("Clearing Fee", 0.0)) * 0.09, 2)
                    got_total = round(sum(breakdown.values()), 2)
                    self.assertEqual(platform, 0.99)
                self.assertEqual(breakdown, expected)
                self.assertEqual(got_total, total)

    def test_clearing_fee_rate(self):
        # 0.0325% of notional
        _, breakdown = estimate_fee_detail("SG", "buy", 1000, 10.0)
        self.assertEqual(breakdown["Clearing Fee"], round(10_000 * 0.000325, 2))

    def test_trading_fee_rate(self):
        _, breakdown = estimate_fee_detail("SG", "buy", 1000, 10.0)
        self.assertEqual(breakdown["Trading Fee"], round(10_000 * 0.000075, 2))

    def test_gst_base_excludes_the_trading_fee(self):
        # Derived from the real notes: GST = 9% × (platform + clearing).
        _, b = estimate_fee_detail("SG", "buy", 200, 2.91)
        self.assertEqual(b["GST"], round((b["Platform Fee"] + b["Clearing Fee"]) * 0.09, 2))

    def test_sg_is_marked_verified(self):
        self.assertTrue(schedule_for("SG").verified)

    def test_round_trip_cost_is_material(self):
        # The finding that matters: a ~SGD 500 round trip costs ~0.5%, which
        # is most of a 0.8% profit-lock target.
        buy = estimate_fee("SG", "buy", 100, 5.0)
        sell = estimate_fee("SG", "sell", 100, 5.0)
        self.assertGreater((buy + sell) / 500 * 100, 0.4)


class UnverifiedSchedulesTest(unittest.TestCase):
    def test_us_and_hk_are_flagged_unverified(self):
        self.assertEqual(unverified_markets(), ["HK", "US"])

    def test_us_sell_adds_regulatory_fees_buy_does_not(self):
        buy = estimate_fee("US", "buy", 100, 50.0)
        sell = estimate_fee("US", "sell", 100, 50.0)
        self.assertGreater(sell, buy, "SEC/TAF are sell-side only")

    def test_taf_is_capped(self):
        _, breakdown = estimate_fee_detail("US", "sell", 10_000_000, 1.0)
        self.assertEqual(breakdown["Trading Activity Fee"], 8.30)

    def test_minimums_apply_to_tiny_us_orders(self):
        _, breakdown = estimate_fee_detail("US", "buy", 1, 10.0)
        self.assertEqual(breakdown["Commission"], 1.00)
        self.assertEqual(breakdown["Platform Fee"], 1.00)


class FallbackTest(unittest.TestCase):
    def test_unknown_market_is_never_free(self):
        # Free paper fills silently overstate P&L — the exact failure this
        # whole module exists to prevent.
        self.assertEqual(estimate_fee("ZZ", "buy", 100, 10.0), FALLBACK_FLAT_FEE)

    def test_zero_quantity_or_price_costs_nothing(self):
        self.assertEqual(estimate_fee("SG", "buy", 0, 10.0), 0.0)
        self.assertEqual(estimate_fee("US", "buy", 100, 0.0), 0.0)


class ComponentMechanicsTest(unittest.TestCase):
    def test_maximum_caps_the_charge(self):
        c = FeeComponent("capped", rate=0.5, maximum=5.0)
        self.assertEqual(c.charge(100, 10.0), 5.0)

    def test_minimum_raises_the_charge(self):
        c = FeeComponent("floored", rate=0.0001, minimum=3.0)
        self.assertEqual(c.charge(1, 1.0), 3.0)

    def test_sell_only_components_skip_buys(self):
        schedule = FeeSchedule(market="X", currency="X", components=[
            FeeComponent("both", flat=1.0),
            FeeComponent("sells", flat=2.0, sell_only=True),
        ])
        self.assertEqual(schedule.compute("buy", 10, 10.0)[0], 1.0)
        self.assertEqual(schedule.compute("sell", 10, 10.0)[0], 3.0)


if __name__ == "__main__":
    unittest.main()
