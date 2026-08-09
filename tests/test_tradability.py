"""Exchange trading-status veto.

Longbridge exposes no news, earnings or calendar API, so the `news_gate` stub
stays a stub. It does expose `trade_status` on every quote, which is a real,
data-backed reason to refuse a symbol: never buy into something the exchange
is not trading normally, because you may not be able to exit it.

Run from the repo root:  python3 -m unittest discover tests
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from models import Portfolio, Quote  # noqa: E402
from strategy import MomentumStrategy  # noqa: E402


def strong_quote(status="normal") -> Quote:
    """A quote that would otherwise score a comfortable buy."""
    return Quote(symbol="AAPL.US", price=109.0, timestamp="", source="longbridge",
                 prev_close=100.0, open=101.0, high=109.5, low=100.5,
                 volume=1e6, turnover=5e7, trade_status=status)


class QuoteFieldTest(unittest.TestCase):
    def test_defaults_to_normal(self):
        self.assertEqual(Quote(symbol="X", price=1, timestamp="", source="s").trade_status,
                         "normal")


class VetoTest(unittest.TestCase):
    def setUp(self):
        self.strategy = MomentumStrategy()
        self.portfolio = Portfolio(cash=100_000.0)

    def signal(self, status):
        quote = strong_quote(status)
        for _ in range(10):
            self.strategy.observe(quote)
        return self.strategy._signal(quote, self.portfolio, 0)

    def test_halted_symbol_is_never_a_buy(self):
        signal = self.signal("halted")
        self.assertEqual(signal.action, "watch")
        self.assertIn("Not tradable", signal.reason)

    def test_veto_scores_zero_so_it_cannot_rank(self):
        # Anything ranking on score must not surface a halted symbol.
        self.assertEqual(self.signal("suspend").score, 0.0)

    def test_every_abnormal_status_is_refused(self):
        for status in ("halted", "suspend", "delisted", "fuse", "unknown"):
            with self.subTest(status=status):
                self.assertEqual(self.signal(status).action, "watch")

    def test_normal_status_does_not_veto(self):
        self.assertNotIn("Not tradable", self.signal("normal").reason)

    def test_diagnostics_expose_tradability(self):
        self.assertFalse(self.signal("halted").diagnostics.tradable)
        self.assertTrue(self.signal("normal").diagnostics.tradable)

    def test_news_gate_remains_an_honest_stub(self):
        # It is still hardcoded True — there is no news source. It must not be
        # mistaken for a working veto.
        self.assertTrue(self.signal("normal").diagnostics.news_gate)


if __name__ == "__main__":
    unittest.main()
