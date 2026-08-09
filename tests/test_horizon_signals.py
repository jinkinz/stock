"""The signal engine must measure on the horizon it is trading.

Swing mode originally changed only the candle timeframe, leaving three of the
five convergence factors measuring intraday things: position in TODAY's range,
a 30-minute tick push, and a VWAP labelled "session" that was nothing of the
sort. A multi-day position confirmed by a half-hour of ticks is a horizon
mismatch, not a confirmation. These tests pin the fix.

Run from the repo root:  python3 -m unittest discover tests
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from models import Portfolio, Quote, Settings  # noqa: E402
from strategy import (  # noqa: E402
    RANGE_LOOKBACK_BARS, TREND_SCALE_PCT, MomentumStrategy, compute_indicators,
)


def rising_bars(n=40, start=100.0, step=0.5):
    out, price = [], start
    for _ in range(n):
        price += step
        out.append({"close": round(price, 4), "high": price + 0.2,
                    "low": price - 0.2, "open": price, "volume": 1000})
    return out


class BarScaleIndicatorsTest(unittest.TestCase):
    def setUp(self):
        self.ind = compute_indicators(rising_bars())

    def test_range_high_and_low_are_exposed(self):
        self.assertGreater(self.ind["range_high"], self.ind["range_low"])

    def test_range_uses_the_lookback_window_not_all_history(self):
        # 40 bars supplied, only the last RANGE_LOOKBACK_BARS should count.
        bars = rising_bars(n=40)
        window_low = min(b["low"] for b in bars[-RANGE_LOOKBACK_BARS:])
        self.assertAlmostEqual(compute_indicators(bars)["range_low"], window_low, places=6)

    def test_bar_momentum_is_positive_in_an_uptrend(self):
        self.assertGreater(self.ind["bar_momentum"], 0)

    def test_bar_momentum_is_negative_in_a_downtrend(self):
        falling = compute_indicators(rising_bars(step=-0.5))
        self.assertLess(falling["bar_momentum"], 0)

    def test_change_over_the_lookback_is_reported(self):
        self.assertGreater(self.ind["change_lookback_pct"], 0)

    def test_change_is_measured_over_the_window_not_all_history(self):
        # 40 rising bars. Measured over the last 20 the move is ~8.6%; over all
        # 40 it is ~19.4%. Using the whole history would silently inflate every
        # swing trend reading.
        bars = rising_bars(n=40)
        closes = [b["close"] for b in bars]
        expected = (closes[-1] / closes[-RANGE_LOOKBACK_BARS] - 1) * 100
        self.assertAlmostEqual(compute_indicators(bars)["change_lookback_pct"],
                               round(expected, 3), places=2)

    def test_indicators_absent_without_enough_bars(self):
        self.assertEqual(compute_indicators(rising_bars(n=5)), {})


class HorizonMeasureTest(unittest.TestCase):
    """Same quote, same candles — only the horizon differs."""

    def setUp(self):
        self.strategy = MomentumStrategy()
        self.portfolio = Portfolio(cash=100_000.0)
        self.bars = rising_bars(n=40)
        self.strategy.ingest_candles("AAPL.US", self.bars)
        last = self.bars[-1]["close"]
        # Price sits at the BOTTOM of today's session range but at the TOP of
        # the multi-bar range — the two measures must disagree here, which is
        # exactly what makes this test meaningful.
        self.quote = Quote(symbol="AAPL.US", price=last, timestamp="",
                           source="longbridge", prev_close=last - 0.5,
                           open=last, high=last + 5.0, low=last - 0.2,
                           volume=1e6, turnover=5e7)
        for _ in range(10):
            self.strategy.observe(self.quote)

    def reason(self, horizon):
        return self.strategy._signal(self.quote, self.portfolio, 0, horizon).reason

    def test_intraday_describes_the_day_range(self):
        self.assertIn("day", self.reason("intraday"))

    def test_swing_describes_the_bar_range(self):
        self.assertIn(f"{RANGE_LOOKBACK_BARS}-bar", self.reason("swing"))

    def test_structure_is_measured_on_the_bar_range_not_the_day(self):
        # Price sits near the bottom of today's range and near the top of the
        # multi-bar range. Only a swing measure can call this strong structure.
        self.assertIn(f"near {RANGE_LOOKBACK_BARS}-bar range high", self.reason("swing"))
        self.assertNotIn("near day range high", self.reason("intraday"))

    def test_swing_and_intraday_reach_different_conclusions(self):
        # If the horizon changed nothing, this strategy would be misnamed.
        self.assertNotEqual(self.reason("intraday"), self.reason("swing"))

    def test_swing_reports_the_vwap_anchor_honestly(self):
        # On daily bars it is not session VWAP, and must not claim to be.
        swing = self.reason("swing")
        if "VWAP" in swing:
            self.assertIn("long-run VWAP", swing)


class TrendGateTest(unittest.TestCase):
    """A red day must not veto a swing entry that is up over the window."""

    def setUp(self):
        self.strategy = MomentumStrategy()
        self.portfolio = Portfolio(cash=100_000.0)
        # Strong multi-bar uptrend, with pullbacks so RSI stays under 75.
        bars, price = [], 100.0
        for i in range(40):
            price += 0.9 if i % 2 == 0 else -0.4
            bars.append({"close": round(price, 4), "high": price + 0.2,
                         "low": price - 0.2, "open": price, "volume": 1000 + i * 40})
        self.strategy.ingest_candles("AAPL.US", bars)
        last = bars[-1]["close"]
        # Down slightly on the day, up strongly over the window.
        self.quote = Quote(symbol="AAPL.US", price=last, timestamp="",
                           source="longbridge", prev_close=last + 0.3,
                           open=last + 0.3, high=last + 0.4, low=last - 0.1,
                           volume=1e6, turnover=5e7)
        for _ in range(10):
            self.strategy.observe(self.quote)

    def test_day_change_is_negative(self):
        signal = self.strategy._signal(self.quote, self.portfolio, 0, "intraday")
        self.assertLess(signal.diagnostics.day_change_pct, 0)

    def test_swing_uses_multi_bar_change_not_the_day(self):
        swing = self.strategy._signal(self.quote, self.portfolio, 0, "swing")
        self.assertIn(f"{RANGE_LOOKBACK_BARS}-bar", swing.reason)
        self.assertNotIn("day -", swing.reason)

    def test_a_red_day_does_not_veto_a_swing_buy(self):
        # The gate that mattered: requiring TODAY to be green before opening a
        # multi-day position is an intraday constraint. Same quote, same bars —
        # only the horizon differs.
        swing = self.strategy._signal(self.quote, self.portfolio, 0, "swing")
        intraday = self.strategy._signal(self.quote, self.portfolio, 0, "intraday")
        self.assertEqual(swing.action, "buy")
        self.assertNotEqual(intraday.action, "buy",
                            "a red day should still veto an INTRADAY entry")


class ScaleTest(unittest.TestCase):
    def test_swing_scale_is_wider_than_intraday(self):
        # +5% is a strong day but an ordinary month; scoring them identically
        # would make every swing candidate look maximally strong.
        self.assertGreater(TREND_SCALE_PCT["swing"], TREND_SCALE_PCT["intraday"])

    def test_scan_passes_the_configured_horizon(self):
        strategy = MomentumStrategy()
        strategy.ingest_candles("AAPL.US", rising_bars(n=40))
        quote = Quote(symbol="AAPL.US", price=120.0, timestamp="", source="longbridge",
                      prev_close=119.0, open=119.0, high=120.5, low=118.0,
                      volume=1e6, turnover=5e7)
        settings = Settings(trading_horizon="swing", min_confirmations=0).normalized()
        signals, _ = strategy.scan(settings, [quote], Portfolio(cash=10_000.0))
        self.assertTrue(signals)
        self.assertIn(f"{RANGE_LOOKBACK_BARS}-bar", signals[0].reason)


if __name__ == "__main__":
    unittest.main()
