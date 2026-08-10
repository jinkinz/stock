"""Swing's replacement for the session timer, and horizon-filtered styles.

`duration_minutes` does nothing in swing mode — sessions never expire — so a
position could sit unresolved forever. `max_hold_days` is the swing equivalent:
capital tied up in a trade that has not resolved is capital doing nothing.

Run from the repo root:  python3 -m unittest discover tests
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app  # noqa: E402
from models import (  # noqa: E402
    STRATEGIES_BY_HORIZON, ApprovalMode, Position, Quote, Settings, TradingMode,
)


def setUpModule() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="trading-maxhold-"))
    for attr, name in (("STATE_DIR", ""), ("STATE_FILE", "paper_state.json"),
                       ("AUDIT_LOG", "audit_log.jsonl"),
                       ("TRADES_CLOSED_LOG", "trades_closed.jsonl"),
                       ("TRADE_LOG", "trade_log.jsonl")):
        setattr(app, attr, tmp / name if name else tmp)


class MaxHoldExitTest(unittest.TestCase):
    def setUp(self):
        self._settings = app.STATE.settings
        self._broker = app.STATE.paper_broker
        from broker import PaperBroker
        app.STATE.settings = Settings(
            trading_horizon="swing", max_hold_days=10, lock_profit_pct=0.0,
            stop_loss_pct=0.0, trailing_stop_pct=0.0,
            trading_mode=TradingMode.PAPER, approval_mode=ApprovalMode.AUTO,
        ).normalized()
        broker = PaperBroker(starting_cash=100_000.0)
        broker._lb = None
        app.STATE.paper_broker = broker
        self.broker = broker
        self.engine = app.TradingEngine()

    def tearDown(self):
        app.STATE.settings = self._settings
        app.STATE.paper_broker = self._broker
        app.TradingEngine.simulated_now = None

    def hold_for(self, days: float):
        opened = datetime.now(timezone.utc) - timedelta(days=days)
        self.broker._portfolio.positions["AAPL.US"] = Position(
            symbol="AAPL.US", quantity=10, avg_cost=100.0,
            entry_qty=10, entry_price=100.0, opened_at=opened.isoformat())
        return [Quote(symbol="AAPL.US", price=101.0, timestamp="", source="longbridge")]

    def test_position_under_the_limit_is_left_alone(self):
        self.assertEqual(self.engine._check_mechanical_exits(self.hold_for(3)), [])

    def test_position_past_the_limit_is_closed(self):
        proposals = self.engine._check_mechanical_exits(self.hold_for(11))
        self.assertEqual(len(proposals), 1)
        self.assertEqual(proposals[0].tag, "max_hold")
        self.assertIn("Max hold reached", proposals[0].reason)

    def test_exactly_at_the_limit_closes(self):
        self.assertEqual(len(self.engine._check_mechanical_exits(self.hold_for(10))), 1)

    def test_it_closes_winners_and_losers_alike(self):
        # Not a stop and not a target — it is about time, not P&L.
        quotes = self.hold_for(11)
        quotes[0] = Quote(symbol="AAPL.US", price=140.0, timestamp="", source="longbridge")
        self.assertEqual(self.engine._check_mechanical_exits(quotes)[0].tag, "max_hold")

    def test_zero_disables_the_rule(self):
        app.STATE.settings.max_hold_days = 0
        self.assertEqual(self.engine._check_mechanical_exits(self.hold_for(999)), [])

    def test_rule_keeps_the_exit_loop_alive_on_its_own(self):
        # Every percentage setting is 0; only max_hold is configured.
        self.assertEqual(app.STATE.settings.lock_profit_pct, 0.0)
        self.assertEqual(app.STATE.settings.stop_loss_pct, 0.0)
        self.assertEqual(len(self.engine._check_mechanical_exits(self.hold_for(11))), 1)

    def test_uses_the_engine_clock_so_replays_measure_simulated_time(self):
        quotes = self.hold_for(3)
        app.TradingEngine.simulated_now = datetime.now(timezone.utc) + timedelta(days=30)
        self.assertEqual(len(self.engine._check_mechanical_exits(quotes)), 1)


class HorizonStrategyTest(unittest.TestCase):
    def test_intraday_offers_session_based_styles(self):
        self.assertIn("fifo", Settings().normalized().horizon_strategies())

    def test_swing_hides_styles_built_on_a_session_countdown(self):
        swing = Settings(trading_horizon="swing").normalized()
        # fifo's prompt is "reach the target before time runs out"; scalp
        # targets +0.3%, below round-trip cost at every size measured.
        self.assertNotIn("fifo", swing.horizon_strategies())
        self.assertNotIn("scalp", swing.horizon_strategies())

    def test_swing_offers_the_swing_style(self):
        self.assertIn("swing", Settings(trading_horizon="swing").normalized().horizon_strategies())

    def test_intraday_does_not_offer_the_swing_style(self):
        self.assertNotIn("swing", Settings().normalized().horizon_strategies())

    def test_risk_postures_are_available_on_both(self):
        for posture in ("conservative", "aggressive"):
            for horizon in ("intraday", "swing"):
                self.assertIn(posture, STRATEGIES_BY_HORIZON[horizon])

    def test_max_hold_is_part_of_the_horizon_profile(self):
        s = Settings().normalized()
        s.max_hold_days = 4
        s.switch_horizon("swing")
        self.assertEqual(s.max_hold_days, 10)      # swing's own saved value
        s.switch_horizon("intraday")
        self.assertEqual(s.max_hold_days, 4)       # intraday's preserved


if __name__ == "__main__":
    unittest.main()
