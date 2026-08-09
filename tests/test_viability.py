"""Trade-viability guard: never open a position that loses money when it wins.

Every figure here is derived from the fee schedules and the caller's own
settings. No test asserts a hard-coded budget — the guard has to work for any
position size, target and market.

Run from the repo root:  python3 -m unittest discover tests
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app  # noqa: E402
from fees import (  # noqa: E402
    assess_trade, breakeven_pct, min_viable_notional, round_trip_fee_pct,
)
from models import (  # noqa: E402
    ApprovalMode, OrderProposal, OrderStatus, Settings, Side, TradingMode,
)


def setUpModule() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="trading-viabtest-"))
    app.STATE_DIR = tmp
    app.STATE_FILE = tmp / "paper_state.json"
    app.TRADE_LOG = tmp / "trade_log.jsonl"
    app.AUDIT_LOG = tmp / "audit_log.jsonl"
    app.TRADES_CLOSED_LOG = tmp / "trades_closed.jsonl"
    app.SESSIONS_LOG = tmp / "sessions_log.jsonl"


class CostCurveTest(unittest.TestCase):
    """Flat minimums mean cost% falls as the position grows. The guard depends
    on that shape, so pin it down."""

    def test_cost_pct_falls_as_position_grows(self):
        # Non-increasing, but only up to cent-rounding: once the flat minimums
        # are exceeded the curve flattens onto the per-share/percentage
        # components and rounding makes it wobble by ~0.0001pp. Allow that.
        tolerance = 0.001
        for market in ("US", "SG"):
            costs = [round_trip_fee_pct(market, n, 25.0)
                     for n in (250, 500, 1000, 5000, 25000)]
            for earlier, later in zip(costs, costs[1:]):
                self.assertLessEqual(later, earlier + tolerance,
                                     f"{market} cost% should not rise with size: {costs}")

    def test_small_positions_are_expensive(self):
        # Not asserting a specific budget — just that tiny positions are
        # materially worse than large ones.
        small = round_trip_fee_pct("US", 250, 25.0)
        large = round_trip_fee_pct("US", 25_000, 25.0)
        self.assertGreater(small, large * 5)

    def test_slippage_is_counted_on_both_legs(self):
        without = breakeven_pct("US", 1000, 25.0, slippage_bps=0)
        with_slip = breakeven_pct("US", 1000, 25.0, slippage_bps=5)
        self.assertAlmostEqual(with_slip - without, 0.10, places=6)

    def test_price_affects_per_share_fees(self):
        # Same notional, different share counts — US fees are per-share.
        cheap = round_trip_fee_pct("US", 1000, 1.0)     # 1000 shares
        pricey = round_trip_fee_pct("US", 1000, 100.0)  # 10 shares
        self.assertGreater(cheap, pricey)


class MinViableNotionalTest(unittest.TestCase):
    def test_returns_a_size_that_actually_works(self):
        target = 0.8
        floor = min_viable_notional("US", 25.0, target, slippage_bps=5)
        self.assertGreater(floor, 0)
        self.assertLess(breakeven_pct("US", floor, 25.0, 5), target)

    def test_just_below_the_floor_does_not_work(self):
        target = 0.8
        floor = min_viable_notional("US", 25.0, target, slippage_bps=5)
        self.assertGreaterEqual(breakeven_pct("US", floor * 0.9, 25.0, 5), target)

    def test_impossible_target_returns_zero(self):
        # A target below the purely percentage-based fees can never be met,
        # at any size.
        self.assertEqual(min_viable_notional("HK", 25.0, 0.01, slippage_bps=5), 0.0)

    def test_generous_target_is_viable_even_tiny(self):
        self.assertGreater(min_viable_notional("SG", 25.0, 50.0, slippage_bps=5), 0.0)

    def test_no_target_returns_zero(self):
        self.assertEqual(min_viable_notional("US", 25.0, 0.0), 0.0)


class AssessTradeTest(unittest.TestCase):
    def test_a_winning_trade_that_loses_is_flagged(self):
        # The core failure: target smaller than costs.
        v = assess_trade("US", 250, 25.0, target_pct=0.8, slippage_bps=5)
        self.assertFalse(v.viable)
        self.assertLess(v.net_edge_pct, 0)
        self.assertIn("still loses", v.reason)

    def test_the_same_target_is_viable_at_a_larger_size(self):
        small = assess_trade("US", 250, 25.0, 0.8, 5)
        large = assess_trade("US", 25_000, 25.0, 0.8, 5)
        self.assertFalse(small.viable)
        self.assertTrue(large.viable)
        self.assertGreater(large.net_edge_pct, 0)

    def test_reason_tells_you_what_to_change(self):
        v = assess_trade("US", 250, 25.0, 0.8, 5)
        self.assertGreater(v.min_viable_notional, 250)
        self.assertIn(f"{v.min_viable_notional:,.0f}", v.reason)

    def test_no_target_is_unassessable_not_a_failure(self):
        v = assess_trade("US", 250, 25.0, target_pct=0.0, slippage_bps=5)
        self.assertFalse(v.assessable)
        self.assertTrue(v.viable, "no target must not block trading")
        self.assertGreater(v.breakeven_pct, 0)

    def test_swing_sized_target_is_viable_where_scalping_is_not(self):
        scalp = assess_trade("SG", 250, 25.0, 0.8, 5)
        swing = assess_trade("SG", 250, 25.0, 3.0, 5)
        self.assertFalse(scalp.viable)
        self.assertTrue(swing.viable)

    def test_serialises_cleanly(self):
        payload = assess_trade("US", 1000, 25.0, 2.0, 5).as_dict()
        self.assertEqual(payload["market"], "US")
        self.assertIsInstance(payload["viable"], bool)


class EngineGuardTest(unittest.TestCase):
    def setUp(self):
        self._saved = app.STATE.settings
        app.STATE.settings = Settings(
            budget=10_000.0, max_trade_value=250.0, lock_profit_pct=0.8,
            trading_mode=TradingMode.PAPER, approval_mode=ApprovalMode.AUTO,
            enforce_trade_viability=True,
        ).normalized()
        app.STATE.last_quotes = []
        self.engine = app.TradingEngine()

    def tearDown(self):
        app.STATE.settings = self._saved

    @staticmethod
    def buy(qty, price, symbol="AAPL.US") -> OrderProposal:
        return OrderProposal(symbol=symbol, side=Side.BUY, quantity=qty,
                             price=price, reason="test", confidence=0.9)

    def test_unprofitable_buy_is_blocked(self):
        denial = self.engine._viability_denial(self.buy(10, 25.0))   # $250
        self.assertIsNotNone(denial)
        self.assertIn("unprofitable by construction", denial.lower())

    def test_large_enough_buy_passes(self):
        self.assertIsNone(self.engine._viability_denial(self.buy(1000, 25.0)))

    def test_sells_are_never_blocked_on_cost_grounds(self):
        # Exits must always be possible — trapping a position in a falling
        # market would be far worse than any fee. Open a viable position, then
        # exit it in a size the guard would refuse as an ENTRY.
        from broker import PaperBroker
        app.STATE.paper_broker = PaperBroker(starting_cash=100_000.0)
        app.STATE.paper_broker._lb = None
        app.STATE.paper_broker._prices["AAPL.US"] = 25.0

        entry = self.buy(1000, 25.0)                 # $25k — clears the guard
        self.engine.execute(entry)
        self.assertIs(entry.status, OrderStatus.FILLED, entry.error)

        exit_proposal = OrderProposal(symbol="AAPL.US", side=Side.SELL, quantity=10,
                                      price=25.0, reason="exit", confidence=1.0,
                                      tag="stop_loss")   # $250 — unviable as an entry
        self.engine.execute(exit_proposal)
        self.assertIs(exit_proposal.status, OrderStatus.FILLED, exit_proposal.error)

    def test_disabling_enforcement_lets_it_through(self):
        app.STATE.settings.enforce_trade_viability = False
        self.assertIsNone(self.engine._viability_denial(self.buy(10, 25.0)))

    def test_no_target_does_not_block(self):
        app.STATE.settings.lock_profit_pct = 0.0
        self.assertIsNone(self.engine._viability_denial(self.buy(10, 25.0)))

    def test_blocked_order_never_reaches_the_broker(self):
        proposal = self.buy(10, 25.0)
        before = app.STATE.paper_broker.portfolio().cash
        self.engine.execute(proposal)
        self.assertIs(proposal.status, OrderStatus.FAILED)
        self.assertEqual(app.STATE.paper_broker.portfolio().cash, before,
                         "a blocked buy must not move cash")

    def test_raising_the_target_makes_the_same_trade_viable(self):
        # Nothing about the position changed — only the target.
        self.assertIsNotNone(self.engine._viability_denial(self.buy(10, 25.0)))
        app.STATE.settings.lock_profit_pct = 5.0
        self.assertIsNone(self.engine._viability_denial(self.buy(10, 25.0)))


class ViabilitySummaryTest(unittest.TestCase):
    def setUp(self):
        self._saved = app.STATE.settings
        app.STATE.last_quotes = []
        self.engine = app.TradingEngine()

    def tearDown(self):
        app.STATE.settings = self._saved

    def configure(self, **kwargs):
        defaults = dict(budget=10_000.0, max_trade_value=250.0, lock_profit_pct=0.8,
                        markets=["US", "SG"], trading_mode=TradingMode.PAPER)
        defaults.update(kwargs)
        app.STATE.settings = Settings(**defaults).normalized()

    def test_flags_unviable_settings(self):
        self.configure()
        summary = self.engine.viability_summary()
        self.assertTrue(summary["any_unviable"])
        self.assertEqual(sorted(summary["per_market"]), ["SG", "US"])

    def test_larger_trade_value_clears_it(self):
        self.configure(max_trade_value=25_000.0)
        self.assertFalse(self.engine.viability_summary()["any_unviable"])

    def test_works_for_any_budget_not_just_the_example(self):
        # The guard must be size-agnostic: for each trade value, "unviable"
        # must agree with the underlying arithmetic.
        for trade_value in (100, 250, 1_000, 5_000, 50_000):
            self.configure(max_trade_value=float(trade_value), markets=["US"])
            summary = self.engine.viability_summary()
            expected = breakeven_pct("US", trade_value,
                                     summary["reference_price"], 5.0) >= 0.8
            self.assertEqual(summary["any_unviable"], expected,
                             f"disagreement at trade value {trade_value}")

    def test_no_target_is_reported_as_unassessable(self):
        self.configure(lock_profit_pct=0.0)
        summary = self.engine.viability_summary()
        self.assertFalse(summary["any_unviable"])
        self.assertFalse(summary["per_market"]["US"]["assessable"])


if __name__ == "__main__":
    unittest.main()
