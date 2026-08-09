"""Risk-based position sizing and the ATR indicator.

The point of ATR sizing is that every position risks the SAME fraction of
equity regardless of how violent the symbol is. That invariant is what these
tests pin down — not any particular share count.

Run from the repo root:  python3 -m unittest discover tests
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sizing import (  # noqa: E402
    MAX_CASH_FRACTION_PER_TRADE, size_position,
)
from strategy import atr, compute_indicators  # noqa: E402


def bars(closes, spread=1.0):
    """Candles with a fixed high-low spread around each close."""
    return [{"close": c, "high": c + spread / 2, "low": c - spread / 2,
             "open": c, "volume": 1000} for c in closes]


class AtrTest(unittest.TestCase):
    def test_constant_spread_gives_that_spread(self):
        # Flat prices, high-low of 2.0 every bar -> ATR is 2.0.
        self.assertAlmostEqual(atr(bars([100.0] * 30, spread=2.0)), 2.0, places=6)

    def test_wider_ranges_give_larger_atr(self):
        quiet = atr(bars([100.0] * 30, spread=0.5))
        wild = atr(bars([100.0] * 30, spread=5.0))
        self.assertGreater(wild, quiet * 5)

    def test_gaps_are_counted(self):
        # A gap between bars is invisible to high-low but is real risk, so
        # true range must pick it up.
        no_gap = atr(bars([100.0] * 30, spread=1.0))
        gapped = bars([100.0] * 30, spread=1.0)
        for i in range(15, 30):
            for key in ("close", "high", "low", "open"):
                gapped[i][key] += 20.0          # one large gap up
        self.assertGreater(atr(gapped), no_gap)

    def test_insufficient_history_returns_zero_not_a_guess(self):
        self.assertEqual(atr(bars([100.0] * 5)), 0.0)
        self.assertEqual(atr([]), 0.0)

    def test_zero_means_unknown_and_is_distinguishable(self):
        # A caller must be able to tell "no data" from "no volatility".
        self.assertEqual(atr(bars([100.0] * 5)), 0.0)
        self.assertGreater(atr(bars([100.0] * 30, spread=0.01)), 0.0)

    def test_exposed_through_compute_indicators(self):
        indicators = compute_indicators(bars([100 + i * 0.1 for i in range(40)]))
        self.assertIn("atr", indicators)
        self.assertGreater(indicators["atr"], 0)


class RiskInvariantTest(unittest.TestCase):
    """The core promise: same risk per trade, whatever the symbol."""

    def size(self, atr_value, price=100.0, equity=100_000.0, spendable=100_000.0,
             max_trade_value=1_000_000.0, risk_pct=0.5, multiple=2.0):
        return size_position(price=price, atr=atr_value, equity=equity,
                             spendable=spendable, max_trade_value=max_trade_value,
                             risk_per_trade_pct=risk_pct, atr_stop_multiple=multiple)

    def test_risk_is_equal_across_wildly_different_symbols(self):
        # Headroom so no dollar clamp binds — the invariant is about the risk
        # maths, and a clamp is a separate, deliberate override of it.
        quiet = self.size(0.5, spendable=1_000_000.0)
        wild = self.size(5.0, spendable=1_000_000.0)
        risk_quiet = quiet.quantity * quiet.stop_distance
        risk_wild = wild.quantity * wild.stop_distance
        self.assertAlmostEqual(risk_quiet, risk_wild, places=4)
        self.assertAlmostEqual(risk_quiet, 100_000 * 0.005, places=4)

    def test_clamps_reduce_risk_below_target_never_above(self):
        # A quiet symbol needs many shares to reach the risk target, so the
        # cash cap can bind first. The position then risks LESS than
        # configured — acceptable, because the failure is toward safety — but
        # it does mean risk is not equalised once a clamp is active.
        clamped = self.size(0.5, spendable=100_000.0)
        realised_risk = clamped.quantity * clamped.stop_distance
        target_risk = 100_000 * 0.005
        self.assertLess(realised_risk, target_risk)
        self.assertIn("capped", clamped.reason)

    def test_volatile_symbols_get_fewer_shares(self):
        self.assertLess(self.size(5.0).quantity, self.size(0.5).quantity)

    def test_risk_scales_with_the_configured_percentage(self):
        half = self.size(2.0, risk_pct=0.5)
        double = self.size(2.0, risk_pct=1.0)
        self.assertAlmostEqual(double.quantity, half.quantity * 2, places=4)

    def test_wider_stop_multiple_means_fewer_shares(self):
        self.assertLess(self.size(2.0, multiple=4.0).quantity,
                        self.size(2.0, multiple=2.0).quantity)

    def test_stop_price_sits_one_stop_distance_below_entry(self):
        result = self.size(2.0, price=100.0, multiple=2.0)
        self.assertAlmostEqual(result.stop_distance, 4.0, places=6)
        self.assertAlmostEqual(result.stop_price, 96.0, places=6)

    def test_method_is_reported_as_atr(self):
        self.assertEqual(self.size(2.0).method, "atr")


class FallbackTest(unittest.TestCase):
    def test_missing_atr_falls_back_to_flat_and_says_so(self):
        result = size_position(price=100.0, atr=0.0, equity=100_000.0, spendable=50_000.0,
                               max_trade_value=1_000.0, risk_per_trade_pct=0.5,
                               atr_stop_multiple=2.0)
        self.assertEqual(result.method, "flat")
        self.assertIn("no ATR", result.reason)
        self.assertEqual(result.quantity, 10.0)          # $1,000 / $100

    def test_fallback_sets_no_stop_price(self):
        # Never imply a stop that was not actually derived.
        result = size_position(price=100.0, atr=0.0, equity=100_000.0, spendable=50_000.0,
                               max_trade_value=1_000.0, risk_per_trade_pct=0.5,
                               atr_stop_multiple=2.0)
        self.assertEqual(result.stop_price, 0.0)
        self.assertEqual(result.stop_distance, 0.0)

    def test_disabling_atr_sizing_uses_flat(self):
        result = size_position(price=100.0, atr=2.0, equity=100_000.0, spendable=50_000.0,
                               max_trade_value=1_000.0, risk_per_trade_pct=0.5,
                               atr_stop_multiple=2.0, use_atr_sizing=False)
        self.assertEqual(result.method, "flat")
        self.assertIn("disabled", result.reason)

    def test_zero_risk_percentage_falls_back(self):
        result = size_position(price=100.0, atr=2.0, equity=100_000.0, spendable=50_000.0,
                               max_trade_value=1_000.0, risk_per_trade_pct=0.0,
                               atr_stop_multiple=2.0)
        self.assertEqual(result.method, "flat")


class ClampTest(unittest.TestCase):
    def base(self, **kwargs):
        args = dict(price=100.0, atr=0.1, equity=1_000_000.0, spendable=100_000.0,
                    max_trade_value=1_000_000.0, risk_per_trade_pct=5.0,
                    atr_stop_multiple=2.0)
        args.update(kwargs)
        return size_position(**args)

    def test_max_trade_value_caps_the_position(self):
        result = self.base(max_trade_value=2_000.0)
        self.assertLessEqual(result.notional, 2_000.0)
        self.assertIn("max trade value", result.reason)

    def test_never_exceeds_the_cash_fraction(self):
        result = self.base(spendable=10_000.0)
        self.assertLessEqual(result.notional,
                             10_000.0 * MAX_CASH_FRACTION_PER_TRADE + 0.01)

    def test_never_exceeds_available_cash(self):
        result = self.base(spendable=50.0, max_trade_value=1_000_000.0)
        self.assertLessEqual(result.notional, 50.0)

    def test_no_cash_means_no_position(self):
        self.assertFalse(self.base(spendable=0.0).ok)

    def test_no_price_means_no_position(self):
        self.assertFalse(self.base(price=0.0).ok)

    def test_clamping_is_reported_in_the_reason(self):
        self.assertTrue(self.base(max_trade_value=2_000.0).reason)


if __name__ == "__main__":
    unittest.main()
