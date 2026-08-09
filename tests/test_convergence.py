"""Convergence gate: require independent factors to agree before buying.

The point is not "a higher score" — it is that a blended score can hide
disagreement, and disagreement is what the expensive trades look like. These
tests pin the counting rules and the gate behaviour, not any tuned threshold.

Run from the repo root:  python3 -m unittest discover tests
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from models import Diagnostics, Portfolio, Quote, Settings  # noqa: E402
from strategy import MomentumStrategy  # noqa: E402


def diagnostics(**kwargs) -> Diagnostics:
    """A fully-confirming diagnostic set; override one field to break it."""
    base = dict(symbol="AAPL.US", price=100.0, ema_trend="bull",
                vwap_dist_pct=0.5, vol_surge=1.4, day_change_pct=1.2)
    base.update(kwargs)
    return Diagnostics(**base)


class ConfirmationCountingTest(unittest.TestCase):
    def confirm(self, range_pos=0.9, momentum=0.01, **diag_kwargs):
        return MomentumStrategy._confirmations(diagnostics(**diag_kwargs), range_pos, momentum)

    def test_all_five_can_be_met(self):
        met, missing = self.confirm()
        self.assertEqual(len(met), 5)
        self.assertEqual(missing, [])

    def test_each_factor_can_independently_fail(self):
        cases = {
            "trend": dict(ema_trend="bear"),
            "vwap": dict(vwap_dist_pct=-0.3),
            "volume": dict(vol_surge=0.4),
        }
        for name, override in cases.items():
            with self.subTest(factor=name):
                met, missing = self.confirm(**override)
                self.assertIn(name, missing)
                self.assertEqual(len(met), 4)

    def test_structure_depends_on_range_position(self):
        self.assertIn("structure", self.confirm(range_pos=0.2)[1])
        self.assertIn("structure", self.confirm(range_pos=0.9)[0])

    def test_momentum_depends_on_tick_push(self):
        self.assertIn("momentum", self.confirm(momentum=-0.01)[1])
        self.assertIn("momentum", self.confirm(momentum=0.01)[0])

    def test_missing_data_is_never_a_confirmation(self):
        # No candles fetched yet: ema_trend "", vwap 0.0, vol_surge 0.0.
        # Absence of evidence must not count as evidence.
        met, missing = self.confirm(ema_trend="", vwap_dist_pct=0.0, vol_surge=0.0)
        for factor in ("trend", "vwap", "volume"):
            self.assertIn(factor, missing)
        self.assertNotIn(factor, met)

    def test_met_and_missing_partition_every_factor(self):
        met, missing = self.confirm(ema_trend="bear", vol_surge=0.1)
        self.assertEqual(sorted(met + missing),
                         sorted(MomentumStrategy.CONFIRMATION_NAMES))


class GateBehaviourTest(unittest.TestCase):
    """End-to-end through _signal, using a quote engineered to score a buy."""

    def setUp(self):
        self.strategy = MomentumStrategy()
        self.portfolio = Portfolio(cash=100_000.0)

        # A realistic uptrend: rising but with pullbacks. A monotonic series
        # pins RSI at 100, which trips the overbought veto before the
        # convergence gate is ever consulted — the fixture has to clear every
        # earlier gate for this test to be testing what it claims.
        closes, price = [], 100.0
        for i in range(40):
            price += 0.30 if i % 2 == 0 else -0.20      # net +0.05/bar, RSI ~60
            closes.append(round(price, 4))
        candles = [{"close": c, "high": c + 0.15, "low": c - 0.15,
                    "volume": 1000 + i * 50}                 # rising -> vol_surge > 1
                   for i, c in enumerate(closes)]
        self.strategy.ingest_candles("AAPL.US", candles)

        last = closes[-1]
        self.quote = Quote(symbol="AAPL.US", price=last + 0.5, timestamp="",
                           source="longbridge",
                           prev_close=last - 1.0,               # ~ +1.4% on the day
                           open=last - 0.8,
                           high=last + 0.7, low=last - 1.5,     # price high in range
                           volume=1e6, turnover=5e7)
        # Rising observations so tick momentum is positive.
        for step in range(12):
            self.strategy.observe(
                Quote(symbol="AAPL.US", price=last - 1.0 + step * 0.125,
                      timestamp="", source="longbridge"))

    def signal(self, min_confirmations):
        return self.strategy._signal(self.quote, self.portfolio, min_confirmations)

    def test_gate_off_preserves_the_original_behaviour(self):
        self.assertEqual(self.signal(0).action, "buy")

    def test_a_fully_confirmed_setup_still_buys_at_max_gate(self):
        self.assertEqual(self.signal(5).action, "buy")

    def _break_volume_confirmation(self):
        """Drain volume so `volume` fails to confirm.

        Chosen deliberately: it is the factor with the smallest weight in the
        composite score, so the setup still clears the 0.55 score gate and the
        convergence gate is genuinely what rejects it. Breaking EMA instead
        would cost 0.30 of score and the earlier gate would reject it first —
        which would test nothing.
        """
        indicators = dict(self.strategy._indicators["AAPL.US"][1])
        indicators["vol_surge"] = 0.5
        self.strategy._indicators["AAPL.US"] = (
            self.strategy._indicators["AAPL.US"][0], indicators)

    def test_gate_downgrades_to_watch_when_factors_disagree(self):
        self._break_volume_confirmation()
        self.assertEqual(self.signal(0).action, "buy", "gate off: still a buy")
        signal = self.signal(5)
        self.assertEqual(signal.action, "watch")
        self.assertIn("Not converged", signal.reason)

    def test_watch_reason_names_the_missing_factors(self):
        self._break_volume_confirmation()
        self.assertIn("volume", self.signal(5).reason)

    def test_four_of_five_is_still_rejected_at_full_convergence(self):
        # The "almost converged" band measured worst in replay — it must not
        # slip through.
        self._break_volume_confirmation()
        self.assertEqual(self.signal(4).action, "buy")
        self.assertEqual(self.signal(5).action, "watch")

    def test_blocked_signal_scores_below_the_buy_threshold(self):
        # A downgraded signal must not still look like a buy candidate to
        # anything ranking on score.
        self._break_volume_confirmation()
        self.assertLess(self.signal(5).score, 0.55)

    def test_buy_reason_reports_the_confirmation_count(self):
        self.assertIn("confirm", self.signal(5).reason)

    def test_gate_never_blocks_an_exit(self):
        # Hold a position and make the setup bearish: the sell path must be
        # reachable regardless of the gate.
        self.portfolio.positions["AAPL.US"] = __import__(
            "models", fromlist=["Position"]).Position(
                symbol="AAPL.US", quantity=10, avg_cost=120.0)
        falling = Quote(symbol="AAPL.US", price=95.0, timestamp="", source="longbridge",
                        prev_close=100.0, open=100.0, high=101.0, low=94.0,
                        volume=1e6, turnover=5e7)
        signal = self.strategy._signal(falling, self.portfolio, 5)
        self.assertIn(signal.action, ("sell", "watch"))
        self.assertNotIn("Not converged", signal.reason)


class SettingsTest(unittest.TestCase):
    def test_clamped_to_the_factor_count(self):
        self.assertEqual(Settings(min_confirmations=99).normalized().min_confirmations, 5)
        self.assertEqual(Settings(min_confirmations=-3).normalized().min_confirmations, 0)

    def test_defaults_to_full_convergence(self):
        self.assertEqual(Settings().normalized().min_confirmations, 5)


if __name__ == "__main__":
    unittest.main()
