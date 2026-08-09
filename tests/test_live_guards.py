"""Live-mode hard limits: the tool must never spend past the budget, and must
never sell shares it did not buy.

These guard real money, so they are tested at the chokepoint every order goes
through — TradingEngine.execute() / _live_guard() — not at the strategy layer
that merely proposes.

Run from the repo root:  python3 -m unittest discover tests
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app  # noqa: E402
from models import (  # noqa: E402
    ApprovalMode, OrderProposal, OrderStatus, Portfolio, Position, Settings, Side, TradingMode,
)


def setUpModule() -> None:
    """Never let a test touch the real state/ directory."""
    tmp = Path(tempfile.mkdtemp(prefix="trading-guardtest-"))
    app.STATE_DIR = tmp
    app.STATE_FILE = tmp / "paper_state.json"
    app.TRADE_LOG = tmp / "trade_log.jsonl"
    app.AUDIT_LOG = tmp / "audit_log.jsonl"
    app.TRADES_CLOSED_LOG = tmp / "trades_closed.jsonl"
    app.SESSIONS_LOG = tmp / "sessions_log.jsonl"


class FakeLiveBroker:
    """Stands in for LongbridgeBroker. Records what it was asked to submit so a
    test can prove a blocked order never reached the exchange."""

    def __init__(self, positions=None, cash=None):
        self._portfolio = Portfolio(cash=(cash or {}).get("USD", 0.0),
                                    positions=positions or {})
        self._cash = cash or {}
        self.submitted: list[OrderProposal] = []

    def portfolio(self) -> Portfolio:
        return self._portfolio

    def cash_by_currency(self) -> dict:
        return dict(self._cash)

    def submit_order(self, proposal: OrderProposal) -> OrderProposal:
        self.submitted.append(proposal)
        proposal.status = OrderStatus.FILLED
        return proposal


def tool_position(symbol: str, qty: float, entry: float) -> Position:
    """A position the tool opened — entry_qty is what marks it as ours."""
    return Position(symbol=symbol, quantity=qty, avg_cost=entry,
                    entry_qty=qty, entry_price=entry, opened_at="2026-08-09T00:00:00+00:00")


def foreign_position(symbol: str, qty: float, cost: float) -> Position:
    """A position synced from the exchange that the tool never opened."""
    return Position(symbol=symbol, quantity=qty, avg_cost=cost)


class LiveGuardTestCase(unittest.TestCase):
    def setUp(self):
        self._saved = (app.STATE.settings, app.STATE.live_broker,
                       app.TradingEngine._live_pending_notional)
        app.STATE.settings = Settings(
            budget=1000.0, max_trade_value=500.0, trading_mode=TradingMode.LIVE,
            approval_mode=ApprovalMode.AUTO, allow_live_trading=True,
        ).normalized()
        app.TradingEngine._live_pending_notional = 0.0
        app.TradingEngine._live_sync_at = 0.0
        self.engine = app.TradingEngine()

    def tearDown(self):
        (app.STATE.settings, app.STATE.live_broker,
         app.TradingEngine._live_pending_notional) = self._saved

    def use_broker(self, **kwargs) -> FakeLiveBroker:
        broker = FakeLiveBroker(**kwargs)
        app.STATE.live_broker = broker
        return broker

    @staticmethod
    def buy(symbol="AAPL.US", qty=1.0, price=100.0) -> OrderProposal:
        return OrderProposal(symbol=symbol, side=Side.BUY, quantity=qty,
                             price=price, reason="test", confidence=0.9)

    @staticmethod
    def sell(symbol="AAPL.US", qty=1.0, price=100.0) -> OrderProposal:
        return OrderProposal(symbol=symbol, side=Side.SELL, quantity=qty,
                             price=price, reason="test", confidence=0.9, tag="stop_loss")


class CurrencyAllowlistTest(LiveGuardTestCase):
    def test_hk_order_is_blocked(self):
        self.use_broker(cash={"USD": 100_000.0, "HKD": 100_000.0})
        denial = self.engine._live_guard(self.buy(symbol="700.HK"))
        self.assertIsNotNone(denial)
        self.assertIn("BLOCKED", denial)
        self.assertIn("HKD", denial)

    def test_unknown_suffix_is_blocked(self):
        self.use_broker(cash={"USD": 100_000.0})
        self.assertIsNotNone(self.engine._live_guard(self.buy(symbol="ABC.XYZ")))

    def test_us_and_sg_are_allowed(self):
        self.use_broker(cash={"USD": 10_000.0, "SGD": 10_000.0})
        self.assertIsNone(self.engine._live_guard(self.buy(symbol="AAPL.US", qty=1, price=100)))
        self.assertIsNone(self.engine._live_guard(self.buy(symbol="D05.SG", qty=1, price=30)))


class OwnershipTest(LiveGuardTestCase):
    def test_cannot_sell_a_position_the_tool_did_not_open(self):
        self.use_broker(positions={"AAPL.US": foreign_position("AAPL.US", 500, 90.0)},
                        cash={"USD": 10_000.0})
        denial = self.engine._live_guard(self.sell(qty=500))
        self.assertIsNotNone(denial)
        self.assertIn("did not buy", denial)

    def test_can_sell_what_the_tool_opened(self):
        self.use_broker(positions={"AAPL.US": tool_position("AAPL.US", 5, 100.0)},
                        cash={"USD": 10_000.0})
        self.assertIsNone(self.engine._live_guard(self.sell(qty=5)))

    def test_cannot_oversell_beyond_the_tool_holding(self):
        self.use_broker(positions={"AAPL.US": tool_position("AAPL.US", 5, 100.0)},
                        cash={"USD": 10_000.0})
        self.assertIsNotNone(self.engine._live_guard(self.sell(qty=6)))

    def test_foreign_position_never_reaches_the_exchange(self):
        broker = self.use_broker(
            positions={"AAPL.US": foreign_position("AAPL.US", 500, 90.0)},
            cash={"USD": 10_000.0})
        proposal = self.sell(qty=500)
        self.engine.execute(proposal)
        self.assertIs(proposal.status, OrderStatus.FAILED)
        self.assertEqual(broker.submitted, [], "a blocked sell must never be submitted")

    def test_foreign_positions_are_hidden_from_the_strategy(self):
        broker = self.use_broker(
            positions={"AAPL.US": foreign_position("AAPL.US", 500, 90.0),
                       "NVDA.US": tool_position("NVDA.US", 2, 100.0)},
            cash={"USD": 10_000.0})
        view = self.engine._tradable_view(broker.portfolio())
        self.assertEqual(sorted(view.positions), ["NVDA.US"])

    def test_mechanical_exits_ignore_foreign_positions(self):
        # A foreign position far below its cost basis would trip the stop loss
        # if the tool could see it.
        self.use_broker(positions={"AAPL.US": foreign_position("AAPL.US", 500, 200.0)},
                        cash={"USD": 10_000.0})
        app.STATE.settings.stop_loss_pct = 2.0
        from models import Quote
        quotes = [Quote(symbol="AAPL.US", price=100.0, timestamp="", source="longbridge")]
        self.assertEqual(self.engine._check_mechanical_exits(quotes), [])


class BudgetCeilingTest(LiveGuardTestCase):
    def test_buy_within_budget_passes(self):
        self.use_broker(cash={"USD": 100_000.0})
        self.assertIsNone(self.engine._live_guard(self.buy(qty=5, price=100.0)))

    def test_budget_not_account_balance_is_the_limit(self):
        # $100k in the account, $1k budget: a $5k order must not go through.
        self.use_broker(cash={"USD": 100_000.0})
        proposal = self.buy(qty=50, price=100.0)
        self.engine._live_guard(proposal)
        self.assertLessEqual(proposal.quantity * proposal.price, 1000.0)

    def test_oversized_buy_is_trimmed_to_the_remaining_budget(self):
        self.use_broker(cash={"USD": 100_000.0})
        proposal = self.buy(qty=50, price=100.0)
        self.assertIsNone(self.engine._live_guard(proposal))
        self.assertEqual(proposal.quantity, 10.0)          # $1000 / $100
        self.assertIn("trimmed", proposal.reason)

    def test_already_deployed_budget_reduces_the_room(self):
        self.use_broker(positions={"NVDA.US": tool_position("NVDA.US", 8, 100.0)},
                        cash={"USD": 100_000.0})
        proposal = self.buy(qty=10, price=100.0)
        self.assertIsNone(self.engine._live_guard(proposal))
        self.assertEqual(proposal.quantity, 2.0)           # only $200 of room left

    def test_fully_deployed_budget_blocks_new_buys(self):
        self.use_broker(positions={"NVDA.US": tool_position("NVDA.US", 10, 100.0)},
                        cash={"USD": 100_000.0})
        denial = self.engine._live_guard(self.buy(qty=1, price=100.0))
        self.assertIsNotNone(denial)
        self.assertIn("fully deployed", denial)

    def test_room_too_small_for_one_share_blocks(self):
        self.use_broker(positions={"NVDA.US": tool_position("NVDA.US", 9.9, 100.0)},
                        cash={"USD": 100_000.0})
        denial = self.engine._live_guard(self.buy(qty=1, price=100.0))
        self.assertIsNotNone(denial)
        self.assertIn("not enough for one share", denial)

    def test_zero_budget_blocks_everything(self):
        self.use_broker(cash={"USD": 100_000.0})
        app.STATE.settings.budget = 0.0
        denial = self.engine._live_guard(self.buy())
        self.assertIsNotNone(denial)
        self.assertIn("budget is 0", denial)

    def test_several_buys_in_one_tick_cannot_collectively_overshoot(self):
        # The regression this exists for: each order individually fits the
        # budget, but the exchange has not reported the earlier fills yet.
        broker = self.use_broker(cash={"USD": 100_000.0})
        total = 0.0
        for symbol in ("AAPL.US", "NVDA.US", "MSFT.US", "AMZN.US", "META.US"):
            proposal = self.buy(symbol=symbol, qty=4, price=100.0)   # $400 each
            self.engine.execute(proposal)
            if proposal.status is not OrderStatus.FAILED:
                total += proposal.quantity * proposal.price
        self.assertLessEqual(total, 1000.0,
                             f"deployed {total} against a 1000 budget")
        self.assertGreater(total, 0.0, "the guard must not block everything")


class CashCoverTest(LiveGuardTestCase):
    def test_order_beyond_available_currency_cash_is_blocked(self):
        self.use_broker(cash={"USD": 50.0})
        denial = self.engine._live_guard(self.buy(qty=1, price=100.0))
        self.assertIsNotNone(denial)
        self.assertIn("No borrowing", denial)

    def test_sgd_order_checks_sgd_not_usd(self):
        # Plenty of USD, no SGD: an SG order must still be refused.
        self.use_broker(cash={"USD": 100_000.0, "SGD": 10.0})
        denial = self.engine._live_guard(self.buy(symbol="D05.SG", qty=1, price=30.0))
        self.assertIsNotNone(denial)
        self.assertIn("SGD", denial)


class PaperModeUnaffectedTest(LiveGuardTestCase):
    """The budget ceiling is live-only; paper behaviour must be untouched so a
    baseline measured before this change stays comparable."""

    def test_paper_positions_are_all_tool_owned(self):
        app.STATE.settings.trading_mode = TradingMode.PAPER
        portfolio = Portfolio(positions={"AAPL.US": foreign_position("AAPL.US", 10, 90.0)})
        self.assertEqual(sorted(self.engine._tool_positions(portfolio)), ["AAPL.US"])

    def test_paper_view_is_the_portfolio_itself(self):
        app.STATE.settings.trading_mode = TradingMode.PAPER
        portfolio = Portfolio(cash=99.0, positions={})
        self.assertIs(self.engine._tradable_view(portfolio), portfolio)


if __name__ == "__main__":
    unittest.main()
