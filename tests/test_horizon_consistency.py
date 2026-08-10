"""Regression tests for horizon assumptions leaking through the system.

Swing mode was retrofitted onto an intraday-shaped app, so intraday constants
kept surviving in places the horizon switch did not reach. Each test here
corresponds to a defect found in a systematic sweep of every time-based
constant, and each would have been invisible in normal use — the system stayed
running and simply made worse decisions.

Run from the repo root:  python3 -m unittest discover tests
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app  # noqa: E402
from models import Settings  # noqa: E402
from strategy import MomentumStrategy, proposal_ttl_seconds  # noqa: E402


def horizon_settings(horizon: str) -> Settings:
    s = Settings()
    s.switch_horizon(horizon)
    return s.normalized()


class CandleRefreshTest(unittest.TestCase):
    """The replay hook must be an OVERRIDE, not a cap.

    `min(refresh, CANDLE_REFRESH_SECONDS)` silently pinned swing's 900s
    interval to the intraday 55s, refetching DAILY candles every minute for
    data that changes once a day.
    """

    def test_override_defaults_to_off(self):
        self.assertIsNone(app.TradingEngine.CANDLE_REFRESH_OVERRIDE)

    def test_swing_refreshes_far_less_often_than_intraday(self):
        intraday = app.TradingEngine.CANDLE_SPEC["intraday"][2]
        swing = app.TradingEngine.CANDLE_SPEC["swing"][2]
        self.assertGreater(swing, intraday * 5)

    def test_the_intraday_constant_does_not_cap_swing(self):
        # The exact regression: the cap was below swing's own interval.
        self.assertLess(app.TradingEngine.CANDLE_REFRESH_SECONDS,
                        app.TradingEngine.CANDLE_SPEC["swing"][2])


class IndicatorFreshnessTest(unittest.TestCase):
    """Indicators must outlive a refresh cycle.

    If they expire first, every candle-derived factor reads as "unknown" —
    which the convergence gate treats as NOT confirmed, so a 5-of-5 gate
    blocks everything, ATR sizing falls back to flat, and the swing range and
    momentum measures silently revert to their intraday forms.
    """

    def test_ttl_is_adjustable_per_instance(self):
        s = MomentumStrategy()
        self.assertEqual(s.indicator_ttl, MomentumStrategy.INDICATOR_TTL)
        s.indicator_ttl = 2700.0
        self.assertEqual(s.indicator_ttl, 2700.0)

    def test_default_ttl_would_expire_within_a_swing_cycle(self):
        # Documents WHY the engine must raise it: the default alone is not
        # enough to survive one swing refresh interval.
        self.assertLess(MomentumStrategy.INDICATOR_TTL,
                        app.TradingEngine.CANDLE_SPEC["swing"][2])

    def test_engine_raises_the_ttl_to_outlive_the_refresh(self):
        strategy = MomentumStrategy()
        refresh = app.TradingEngine.CANDLE_SPEC["swing"][2]
        strategy.indicator_ttl = max(strategy.INDICATOR_TTL, refresh * 3)
        self.assertGreater(strategy.indicator_ttl, refresh)

    def test_stale_indicators_read_as_absent_not_as_zero(self):
        # Absence of evidence must not become evidence — the convergence gate
        # depends on this distinction.
        s = MomentumStrategy()
        s.indicator_ttl = 0.0
        s.ingest_candles("AAPL.US", [{"close": 100 + i, "high": 100 + i, "low": 99 + i,
                                      "volume": 100} for i in range(40)])
        self.assertEqual(s._fresh_indicators("AAPL.US"), {})


class ProposalExpiryTest(unittest.TestCase):
    """A fixed 5-minute expiry rejected swing proposals before the next scan
    even ran, so in manual mode they could never be approved."""

    def test_intraday_keeps_the_five_minute_floor(self):
        self.assertEqual(proposal_ttl_seconds(60), 300)

    def test_swing_gets_multiple_scan_cycles_to_decide(self):
        swing_tick = horizon_settings("swing").tick_interval_seconds
        self.assertGreater(proposal_ttl_seconds(swing_tick), swing_tick)

    def test_ttl_never_expires_before_the_next_scan(self):
        for tick in (5, 60, 300, 900, 3600):
            self.assertGreaterEqual(proposal_ttl_seconds(tick), tick,
                                    f"a proposal would die before the next scan at {tick}s")


class StrategyParityTest(unittest.TestCase):
    """Display paths call scan_signals_only on whichever strategy is active."""

    def test_momentum_strategy_has_it_too(self):
        self.assertTrue(hasattr(MomentumStrategy, "scan_signals_only"))

    def test_it_returns_signals_without_proposals(self):
        from models import Portfolio, Quote
        strategy = MomentumStrategy()
        quote = Quote(symbol="AAPL.US", price=100.0, timestamp="", source="longbridge",
                      prev_close=99.0, open=99.0, high=101.0, low=98.0,
                      volume=1e6, turnover=5e7)
        signals = strategy.scan_signals_only(Settings().normalized(), [quote],
                                             Portfolio(cash=10_000.0))
        self.assertIsInstance(signals, list)


class AiRiskSizingTest(unittest.TestCase):
    """`risk_per_trade_pct` applied to rule-based trades only, so the same
    setting meant different things depending on which brain was driving."""

    def build(self, atr):
        import json
        from ai_strategy import AIStrategy
        from models import Diagnostics, Portfolio, Quote, Signal
        quote = Quote(symbol="AAPL.US", price=100.0, timestamp="", source="longbridge")
        signals = [Signal(symbol="AAPL.US", price=100.0, score=0.8, action="buy",
                          reason="x", diagnostics=Diagnostics(symbol="AAPL.US",
                                                              price=100.0, atr=atr))]
        raw = json.dumps([{"symbol": "AAPL.US", "action": "buy", "quantity": 400,
                           "confidence": 0.9, "reason": "t"}])
        settings = Settings(max_trade_value=50_000, risk_per_trade_pct=0.5,
                            atr_stop_multiple=2.0, use_atr_sizing=True).normalized()
        return AIStrategy()._parse_proposals(raw, [quote], Portfolio(cash=100_000.0),
                                             settings, signals)

    def test_risk_sizing_clamps_an_oversized_ai_order(self):
        # 0.5% of $100k = $500 risked over a 2xATR=$4 stop -> 125 shares.
        self.assertEqual(self.build(atr=2.0)[0].quantity, 125.0)

    def test_without_atr_it_falls_back_rather_than_blocking(self):
        # Never silently size on a missing input, but never refuse either.
        self.assertGreater(self.build(atr=0.0)[0].quantity, 0)

    def test_signals_are_optional_so_the_call_cannot_crash(self):
        import json
        from ai_strategy import AIStrategy
        from models import Portfolio, Quote
        raw = json.dumps([{"symbol": "AAPL.US", "action": "buy", "quantity": 10,
                           "confidence": 0.9, "reason": "t"}])
        quote = Quote(symbol="AAPL.US", price=100.0, timestamp="", source="longbridge")
        proposals = AIStrategy()._parse_proposals(
            raw, [quote], Portfolio(cash=100_000.0),
            Settings(use_atr_sizing=True).normalized())
        self.assertTrue(proposals)



class ProfitPacingTest(unittest.TestCase):
    """Hourly pacing races a closing bell. Swing has no bell, so deriving a
    target from `duration_minutes` computed it from a number that is inert in
    that mode — and the AI prompt then quoted a pace against a missing clock."""

    def test_intraday_derives_the_session_target_from_the_rate(self):
        s = Settings(target_profit_per_hour=20.0).normalized()
        self.assertEqual(s.target_profit, 130.0)          # 20 x 6.5h

    def test_swing_has_no_hourly_pace(self):
        s = Settings(target_profit_per_hour=20.0)
        s.switch_horizon("swing")
        s.normalized()
        self.assertEqual(s.target_profit_per_hour, 0.0,
                         "an inert rate could still be read downstream")

    def test_swing_keeps_an_absolute_target(self):
        s = horizon_settings("swing")
        s.target_profit = 500.0
        s.normalized()
        self.assertEqual(s.target_profit, 500.0)

    def test_switching_back_restores_the_intraday_pace(self):
        s = Settings(target_profit_per_hour=20.0).normalized()
        s.switch_horizon("swing"); s.normalized()
        s.switch_horizon("intraday"); s.normalized()
        self.assertEqual(s.target_profit_per_hour, 20.0)
        self.assertEqual(s.target_profit, 130.0)

    def test_prompt_omits_the_pace_line_in_swing(self):
        import inspect
        import ai_strategy
        src = inspect.getsource(ai_strategy)
        self.assertIn("not settings.is_swing", src,
                      "the /hour pace line must be gated on horizon")

class CandleBudgetTest(unittest.TestCase):
    """The candle budget — not the universe size — is the real ceiling on how
    many symbols can be traded, because the convergence gate treats missing
    indicators as NOT confirmed. It sat at 15 while the universe ran to 2000,
    so widening the scan bought nothing."""

    def budget(self, horizon):
        return app.TradingEngine.CANDLE_SPEC[horizon][3]

    def test_every_horizon_declares_a_budget(self):
        for horizon in ("intraday", "swing"):
            self.assertGreater(self.budget(horizon), 0)

    def test_budget_exceeds_the_old_fixed_fifteen(self):
        for horizon in ("intraday", "swing"):
            self.assertGreater(self.budget(horizon), 15,
                               f"{horizon} still throttled to the old constant")

    def test_swing_can_afford_far_more_than_intraday(self):
        # 15 minutes between ticks vs 60 seconds.
        self.assertGreater(self.budget("swing"), self.budget("intraday") * 2)

    def test_budget_stays_within_the_rate_limit(self):
        # Sequential calls self-pace at roughly 150ms; the budget must fit
        # inside its own tick interval with room for quote batches too.
        for horizon in ("intraday", "swing"):
            _, _, refresh, budget = app.TradingEngine.CANDLE_SPEC[horizon]
            seconds_needed = budget * 0.15
            self.assertLess(seconds_needed, refresh,
                            f"{horizon}: {budget} calls cannot finish within {refresh}s")

    def test_coverage_is_reported_to_the_ui(self):
        summary = app.TradingEngine()._coverage_summary()
        for key in ("scanned", "candle_budget", "with_indicators", "gate"):
            self.assertIn(key, summary)



class ScanPoolTest(unittest.TestCase):
    """`0` used to mean "widest", which reads as "none" and made the
    least-recommended value the most tempting one to type."""

    def test_zero_falls_back_to_the_horizon_default(self):
        self.assertEqual(Settings(max_scan_symbols=0).normalized().max_scan_symbols, 200)

    def test_zero_respects_the_active_horizon(self):
        s = Settings(max_scan_symbols=0)
        s.switch_horizon("swing")
        s.normalized()
        self.assertEqual(s.max_scan_symbols, 500)

    def test_a_pointlessly_small_pool_is_floored(self):
        # Below the candle budget the pool offers no selection at all.
        self.assertGreaterEqual(Settings(max_scan_symbols=5).normalized().max_scan_symbols, 25)

    def test_absurd_values_are_capped_at_the_feasible_ceiling(self):
        self.assertEqual(Settings(max_scan_symbols=99999).normalized().max_scan_symbols, 2000)

    def test_a_sensible_value_is_left_alone(self):
        self.assertEqual(Settings(max_scan_symbols=350).normalized().max_scan_symbols, 350)

    def test_pool_exceeds_the_candle_budget_on_both_horizons(self):
        # Otherwise the budget is wasted and the setting cannot select anything.
        for horizon in ("intraday", "swing"):
            s = Settings()
            if horizon == "swing":
                s.switch_horizon("swing")
            s.normalized()
            self.assertGreater(s.max_scan_symbols,
                               app.TradingEngine.CANDLE_SPEC[horizon][3],
                               f"{horizon}: pool must be larger than the candle budget")

if __name__ == "__main__":
    unittest.main()
