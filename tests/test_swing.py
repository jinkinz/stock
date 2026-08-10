"""Swing mode — the multi-day trading horizon.

The switch has to change three things or it is cosmetic: the candle horizon the
indicators are computed from, whether a session can expire (and therefore
whether stop_at_end can flatten a multi-day position), and how an overnight gap
through a stop is reported.

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
    ApprovalMode, Position, Quote, Settings, TradingMode,
)


def setUpModule() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="trading-swing-"))
    app.STATE_DIR = tmp
    app.STATE_FILE = tmp / "paper_state.json"
    app.AUDIT_LOG = tmp / "audit_log.jsonl"
    app.TRADES_CLOSED_LOG = tmp / "trades_closed.jsonl"
    app.TRADE_LOG = tmp / "trade_log.jsonl"


class HorizonSettingTest(unittest.TestCase):
    def test_valid_values(self):
        self.assertTrue(Settings(trading_horizon="swing").normalized().is_swing)
        self.assertFalse(Settings(trading_horizon="intraday").normalized().is_swing)

    def test_case_insensitive(self):
        self.assertTrue(Settings(trading_horizon="SWING").normalized().is_swing)

    def test_garbage_falls_back_to_intraday(self):
        # Never silently trade a horizon nobody asked for.
        for bad in ("bogus", "", None, 42):
            self.assertEqual(Settings(trading_horizon=bad).normalized().trading_horizon,
                             "intraday")

    def test_default_is_intraday(self):
        self.assertEqual(Settings().normalized().trading_horizon, "intraday")

    def test_other_normalisation_still_runs(self):
        # Regression: the horizon logic sits inside normalized(); it must not
        # short-circuit the rest of it.
        s = Settings(target_profit_per_hour=20, duration_minutes=390).normalized()
        self.assertEqual(s.target_profit, 130.0)


class CandleHorizonTest(unittest.TestCase):
    """On 1-min bars EMA9/21 measure minutes; on daily bars they measure days.
    That difference is what makes swing mode actually swing."""

    def test_intraday_uses_minute_bars(self):
        period, count, _, _ = app.TradingEngine.CANDLE_SPEC["intraday"]
        self.assertEqual(period, "Min_1")
        self.assertGreaterEqual(count, 30)

    def test_swing_uses_daily_bars(self):
        period, count, _, _ = app.TradingEngine.CANDLE_SPEC["swing"]
        self.assertEqual(period, "Day")
        self.assertGreaterEqual(count, 60, "need enough days for EMA21 + ATR14")

    def test_swing_refreshes_less_often(self):
        # Daily bars change once a day; re-fetching every minute burns API
        # calls for identical data.
        self.assertGreater(app.TradingEngine.CANDLE_SPEC["swing"][2],
                           app.TradingEngine.CANDLE_SPEC["intraday"][2])


class SessionExpiryTest(unittest.TestCase):
    def setUp(self):
        self._saved = app.STATE.settings
        self.engine = app.TradingEngine()

    def tearDown(self):
        app.STATE.settings = self._saved

    def configure(self, horizon):
        long_ago = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        app.STATE.settings = Settings(trading_horizon=horizon, duration_minutes=1,
                                      started_at=long_ago).normalized()

    def test_intraday_session_expires(self):
        self.configure("intraday")
        self.assertTrue(self.engine._session_expired())

    def test_swing_session_never_expires(self):
        # A duration timer firing would hand stop_at_end a mandate to flatten a
        # multi-day position at an arbitrary moment.
        self.configure("swing")
        self.assertFalse(self.engine._session_expired())

    def test_swing_still_starts_the_clock(self):
        app.STATE.settings = Settings(trading_horizon="swing").normalized()
        app.STATE.settings.started_at = None
        self.assertFalse(self.engine._session_expired())
        self.assertIsNotNone(app.STATE.settings.started_at)


class OvernightGapTest(unittest.TestCase):
    """A stop is a trigger, not a guaranteed fill. Holding overnight means a
    gap can open the position below the stop — the defining swing risk."""

    def setUp(self):
        self._saved_settings = app.STATE.settings
        self._saved_broker = app.STATE.paper_broker
        from broker import PaperBroker
        app.STATE.settings = Settings(trading_horizon="swing", stop_loss_pct=0.0,
                                      lock_profit_pct=0.0, trailing_stop_pct=0.0,
                                      trading_mode=TradingMode.PAPER,
                                      approval_mode=ApprovalMode.AUTO).normalized()
        broker = PaperBroker(starting_cash=100_000.0)
        broker._lb = None
        broker._portfolio.positions["AAPL.US"] = Position(
            symbol="AAPL.US", quantity=10, avg_cost=100.0,
            entry_qty=10, entry_price=100.0, stop_price=95.0)
        app.STATE.paper_broker = broker
        self.engine = app.TradingEngine()

    def tearDown(self):
        app.STATE.settings = self._saved_settings
        app.STATE.paper_broker = self._saved_broker

    def quote(self, price):
        return [Quote(symbol="AAPL.US", price=price, timestamp="", source="longbridge")]

    def test_atr_stop_fires_even_with_every_percentage_off(self):
        # An absolute stop must keep the exit loop alive on its own.
        proposals = self.engine._check_mechanical_exits(self.quote(94.0))
        self.assertEqual(len(proposals), 1)
        self.assertEqual(proposals[0].tag, "stop_loss")

    def test_a_clean_stop_is_not_reported_as_a_gap(self):
        proposals = self.engine._check_mechanical_exits(self.quote(94.9))
        self.assertNotIn("GAPPED", proposals[0].reason)

    def test_a_large_gap_is_named_explicitly(self):
        # Opens 10% below the stop — that loss is a gap, not slippage.
        proposals = self.engine._check_mechanical_exits(self.quote(85.5))
        self.assertIn("GAPPED", proposals[0].reason)

    def test_price_above_the_stop_does_not_exit(self):
        self.assertEqual(self.engine._check_mechanical_exits(self.quote(96.0)), [])


if __name__ == "__main__":
    unittest.main()
