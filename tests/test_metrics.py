"""Unit tests for metrics — stdlib unittest, no dependencies.

Run from the repo root:  python3 -m unittest discover tests
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from metrics import (  # noqa: E402
    MIN_MEANINGFUL_SAMPLE,
    compute_metrics,
    equal_weight_return,
)


def trade(net_pnl: float, *, fees: float = 1.0, hold: float = 60.0,
          exit_reason: str = "stop_loss", strategy: str = "fifo",
          symbol: str = "AAPL.US") -> dict:
    """A closed-trade record shaped exactly like trades_closed.jsonl."""
    return {
        "symbol": symbol,
        "opened_at": "2026-08-08T14:30:00+00:00",
        "closed_at": "2026-08-08T14:31:00+00:00",
        "hold_seconds": hold,
        "entry_price": 100.0,
        "exit_price": 100.0 + net_pnl,
        "quantity": 1.0,
        "gross_pnl": net_pnl + fees,
        "fees": fees,
        "net_pnl": net_pnl,
        "return_pct": net_pnl,
        "exit_reason": exit_reason,
        "strategy": strategy,
        "mode": "paper",
        "entry_score": 0.7,
        "entry_diagnostics": {},
    }


class ZeroTradesTest(unittest.TestCase):
    def test_returns_full_shape_with_zeros(self):
        m = compute_metrics([])
        self.assertEqual(m["total_trades"], 0)
        self.assertEqual(m["wins"], 0)
        self.assertEqual(m["losses"], 0)
        for key in ("win_rate", "avg_win", "avg_loss", "largest_win", "largest_loss",
                    "profit_factor", "expectancy_per_trade", "total_fees",
                    "fees_as_pct_of_gross", "max_drawdown_pct", "max_drawdown_dollars",
                    "avg_hold_seconds", "median_hold_seconds"):
            self.assertEqual(m[key], 0.0, f"{key} should be 0.0 with no trades")

    def test_no_division_by_zero_and_warns(self):
        m = compute_metrics([])
        self.assertTrue(m["sample_warning"])
        self.assertEqual(m["by_exit_reason"], {})
        self.assertEqual(m["by_strategy"], {})


class SingleTradeTest(unittest.TestCase):
    def test_single_win(self):
        m = compute_metrics([trade(10.0, fees=1.0)])
        self.assertEqual(m["total_trades"], 1)
        self.assertEqual(m["wins"], 1)
        self.assertEqual(m["losses"], 0)
        self.assertEqual(m["win_rate"], 1.0)
        self.assertEqual(m["avg_win"], 10.0)
        self.assertEqual(m["avg_loss"], 0.0)
        self.assertEqual(m["largest_win"], 10.0)
        self.assertEqual(m["expectancy_per_trade"], 10.0)
        # No losing trades at all — profit factor has no denominator.
        self.assertEqual(m["profit_factor"], 0.0)
        self.assertTrue(m["profit_factor_undefined"])
        self.assertEqual(m["max_drawdown_dollars"], 0.0)
        self.assertTrue(m["sample_warning"])

    def test_fees_as_pct_of_gross(self):
        # gross 11.0, fee 1.0 → 9.0909%
        m = compute_metrics([trade(10.0, fees=1.0)])
        self.assertEqual(m["total_fees"], 1.0)
        self.assertAlmostEqual(m["fees_as_pct_of_gross"], 9.0909, places=3)


class AllLossesTest(unittest.TestCase):
    def setUp(self):
        self.trades = [trade(-5.0), trade(-10.0), trade(-3.0)]

    def test_metrics(self):
        m = compute_metrics(self.trades)
        self.assertEqual(m["wins"], 0)
        self.assertEqual(m["losses"], 3)
        self.assertEqual(m["win_rate"], 0.0)
        self.assertEqual(m["avg_win"], 0.0)
        self.assertEqual(m["avg_loss"], -6.0)
        self.assertEqual(m["largest_loss"], -10.0)
        self.assertEqual(m["profit_factor"], 0.0)
        self.assertFalse(m["profit_factor_undefined"])
        self.assertEqual(m["expectancy_per_trade"], -6.0)

    def test_drawdown_is_the_whole_run(self):
        m = compute_metrics(self.trades)
        self.assertEqual(m["max_drawdown_dollars"], 18.0)
        # Cumulative P&L never rose above 0, so there is no peak to measure
        # the decline against — percentage stays 0 while dollars are reported.
        self.assertEqual(m["max_drawdown_pct"], 0.0)


class MixedResultsTest(unittest.TestCase):
    def setUp(self):
        # cumulative: +20, +10, +40, +25, +45
        self.trades = [
            trade(20.0, exit_reason="profit_lock", hold=100.0),
            trade(-10.0, exit_reason="stop_loss", hold=50.0),
            trade(30.0, exit_reason="profit_lock", hold=200.0),
            trade(-15.0, exit_reason="stop_loss", hold=10.0),
            trade(20.0, exit_reason="ai_sell", hold=40.0),
        ]

    def test_win_loss_split(self):
        m = compute_metrics(self.trades)
        self.assertEqual(m["total_trades"], 5)
        self.assertEqual(m["wins"], 3)
        self.assertEqual(m["losses"], 2)
        self.assertEqual(m["win_rate"], 0.6)

    def test_profit_factor_and_expectancy(self):
        m = compute_metrics(self.trades)
        # wins 70, losses 25 → 2.8
        self.assertEqual(m["profit_factor"], 2.8)
        # 0.6 * 23.3333 - 0.4 * 12.5 = 9.0
        self.assertAlmostEqual(m["expectancy_per_trade"], 9.0, places=3)
        self.assertEqual(m["net_pnl"], 45.0)

    def test_max_drawdown_without_equity_basis(self):
        m = compute_metrics(self.trades)
        # No starting equity → percentage falls back to peak cumulative PROFIT.
        # The worst dollar decline and the worst percentage decline are then
        # different events on this curve, and both are reported as-is:
        #   peak 20 → trough 10 = $10, but 50% of the peak
        #   peak 40 → trough 25 = $15, but only 37.5% of the peak
        self.assertEqual(m["max_drawdown_dollars"], 15.0)
        self.assertEqual(m["max_drawdown_pct"], 50.0)

    def test_max_drawdown_against_equity(self):
        # Equity curve from 1000: 1020, 1010, 1040, 1025, 1045.
        # Worst decline is 1040 → 1025 = $15 = 1.44% of the peak.
        m = compute_metrics(self.trades, starting_equity=1000.0)
        self.assertEqual(m["max_drawdown_dollars"], 15.0)
        self.assertAlmostEqual(m["max_drawdown_pct"], 1.4423, places=3)

    def test_drawdown_pct_cannot_exceed_100_with_equity(self):
        # Regression: dividing by peak PROFIT produced absurd readings — an
        # $835 give-back against a peak profit near zero reported as 115%.
        # Against equity the same run is a sane single-digit percentage.
        wipeout = [trade(-100.0) for _ in range(5)]
        m = compute_metrics([trade(50.0)] + wipeout, starting_equity=10_000.0)
        self.assertLessEqual(m["max_drawdown_pct"], 100.0)
        self.assertAlmostEqual(m["max_drawdown_pct"], 4.9751, places=3)

    def test_hold_times(self):
        m = compute_metrics(self.trades)
        self.assertEqual(m["avg_hold_seconds"], 80.0)
        self.assertEqual(m["median_hold_seconds"], 50.0)

    def test_breakdown_by_exit_reason(self):
        m = compute_metrics(self.trades)
        by_reason = m["by_exit_reason"]
        self.assertEqual(sorted(by_reason), ["ai_sell", "profit_lock", "stop_loss"])
        self.assertEqual(by_reason["stop_loss"]["total_trades"], 2)
        self.assertEqual(by_reason["stop_loss"]["wins"], 0)
        self.assertEqual(by_reason["stop_loss"]["net_pnl"], -25.0)
        self.assertEqual(by_reason["profit_lock"]["net_pnl"], 50.0)

    def test_breakdown_by_strategy(self):
        trades = self.trades + [trade(5.0, strategy="momentum")]
        by_strategy = compute_metrics(trades)["by_strategy"]
        self.assertEqual(sorted(by_strategy), ["fifo", "momentum"])
        self.assertEqual(by_strategy["momentum"]["total_trades"], 1)

    def test_missing_fields_do_not_raise(self):
        m = compute_metrics(self.trades + [{"symbol": "X.US"}])
        self.assertEqual(m["total_trades"], 6)


class SampleWarningTest(unittest.TestCase):
    def test_boundary(self):
        below = compute_metrics([trade(1.0)] * (MIN_MEANINGFUL_SAMPLE - 1))
        at = compute_metrics([trade(1.0)] * MIN_MEANINGFUL_SAMPLE)
        self.assertTrue(below["sample_warning"])
        self.assertFalse(at["sample_warning"])


class EqualWeightReturnTest(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(equal_weight_return({}), 0.0)

    def test_average(self):
        self.assertEqual(equal_weight_return({"A": 10.0, "B": -4.0, "C": 3.0}), 3.0)


if __name__ == "__main__":
    unittest.main()
