"""Portfolio protections: concentration, daily budget, daily loss, cooldown.

These bound how much damage a bad day can do, so the tests care most about the
edges — the boundary values, the day rolling over in the EXCHANGE's timezone,
and the state surviving a restart.

Run from the repo root:  python3 -m unittest discover tests
"""
from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from market_hours import local_date  # noqa: E402
from models import Settings  # noqa: E402
from risk import COOLDOWN_MINUTES, RiskState, check_limits  # noqa: E402


def settings(**kwargs) -> Settings:
    # Every limit is switched off so each test exercises one rule in isolation
    # — except the concentration cap, which no longer HAS an off switch (see
    # MaxPositionsHasNoSentinelTest). The highest legal value is the closest
    # thing available, so tests for other rules must stay under it.
    base = dict(max_concurrent_positions=20, budget=0.0, daily_turnover_multiple=0.0,
                daily_loss_limit=0.0, cooldown_after_losses=0)
    base.update(kwargs)
    return Settings(**base).normalized()


UTC_NOON = datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)


class ExchangeLocalDayTest(unittest.TestCase):
    def test_us_and_sg_can_be_on_different_dates(self):
        # 02:00 UTC — already the 11th in Singapore, still the 10th in New York.
        moment = datetime(2026, 8, 11, 2, 0, tzinfo=timezone.utc)
        self.assertEqual(local_date("US", moment), "2026-08-10")
        self.assertEqual(local_date("SG", moment), "2026-08-11")

    def test_unknown_market_falls_back_to_utc(self):
        self.assertEqual(local_date("ZZ", UTC_NOON), "2026-08-10")

    def test_daily_counters_are_kept_per_market(self):
        state = RiskState()
        state.record_buy("AAPL.US", 100.0, UTC_NOON)
        state.record_buy("D05.SG", 250.0, UTC_NOON)
        self.assertEqual(state.deployed_today("US", UTC_NOON), 100.0)
        self.assertEqual(state.deployed_today("SG", UTC_NOON), 250.0)


class DailyBudgetTest(unittest.TestCase):
    def setUp(self):
        self.state = RiskState()
        self.settings = settings(budget=1000.0, daily_turnover_multiple=1.0)

    def check(self, notional, now=UTC_NOON):
        return check_limits(self.settings, 0, self.state, "AAPL.US", notional, now)

    def test_within_budget_is_allowed(self):
        self.assertIsNone(self.check(400.0))

    def test_exceeding_budget_is_blocked(self):
        self.state.record_buy("AAPL.US", 900.0, UTC_NOON)
        denial = self.check(200.0)
        self.assertIsNotNone(denial)
        self.assertIn("deployment", denial)

    def test_exactly_at_the_budget_is_allowed(self):
        self.state.record_buy("AAPL.US", 600.0, UTC_NOON)
        self.assertIsNone(self.check(400.0))

    def test_budget_counts_cumulatively_not_currently_held(self):
        # Buy, sell, buy again: recycling consumes the allowance twice, which
        # is the churn a daily cap is meant to stop.
        for _ in range(4):
            self.state.record_buy("AAPL.US", 250.0, UTC_NOON)
        self.assertEqual(self.state.deployed_today("US", UTC_NOON), 1000.0)
        self.assertIsNotNone(self.check(1.0))

    def test_budget_resets_on_the_next_exchange_day(self):
        self.state.record_buy("AAPL.US", 1000.0, UTC_NOON)
        self.assertIsNotNone(self.check(100.0))
        tomorrow = UTC_NOON + timedelta(days=1)
        self.assertIsNone(self.check(100.0, now=tomorrow))

    def test_zero_multiple_disables_the_rule(self):
        self.settings = settings(budget=1000.0, daily_turnover_multiple=0.0)
        self.state.record_buy("AAPL.US", 1_000_000.0, UTC_NOON)
        self.assertIsNone(self.check(999_999.0))

    def test_markets_have_independent_budgets(self):
        self.state.record_buy("AAPL.US", 1000.0, UTC_NOON)
        self.assertIsNotNone(self.check(50.0))
        self.assertIsNone(check_limits(self.settings, 0, self.state, "D05.SG", 50.0, UTC_NOON))


class ConcurrentPositionsTest(unittest.TestCase):
    def test_blocked_at_the_cap(self):
        denial = check_limits(settings(max_concurrent_positions=5), 5, RiskState(),
                              "AAPL.US", 100.0, UTC_NOON)
        self.assertIsNotNone(denial)
        self.assertIn("Concentration", denial)

    def test_allowed_below_the_cap(self):
        self.assertIsNone(check_limits(settings(max_concurrent_positions=5), 4,
                                       RiskState(), "AAPL.US", 100.0, UTC_NOON))

    def test_zero_no_longer_disables_the_rule(self):
        # It used to. But 0 meant "no cap" here while sizing.cash_fraction_for
        # read the same 0 as "assume four positions" and funded each at 25% of
        # cash — so typing 0 to mean "let the engine decide" removed the
        # concentration limit AND shrank every position below the viability
        # floor. 0 now falls back to the default instead.
        denial = check_limits(settings(max_concurrent_positions=0), 500,
                              RiskState(), "AAPL.US", 100.0, UTC_NOON)
        self.assertIsNotNone(denial)
        self.assertIn("max 5", denial)


class DailyLossLimitTest(unittest.TestCase):
    def setUp(self):
        self.state = RiskState()
        self.settings = settings(daily_loss_limit=100.0)

    def check(self, now=UTC_NOON):
        return check_limits(self.settings, 0, self.state, "AAPL.US", 50.0, now)

    def test_losses_below_the_limit_allow_trading(self):
        self.state.record_close("AAPL.US", -50.0, 0, UTC_NOON)
        self.assertIsNone(self.check())

    def test_reaching_the_limit_blocks_buying(self):
        self.state.record_close("AAPL.US", -100.0, 0, UTC_NOON)
        denial = self.check()
        self.assertIsNotNone(denial)
        self.assertIn("daily loss limit", denial)

    def test_profits_offset_losses_within_the_day(self):
        self.state.record_close("AAPL.US", -120.0, 0, UTC_NOON)
        self.state.record_close("AAPL.US", 60.0, 0, UTC_NOON)
        self.assertIsNone(self.check())

    def test_limit_resets_the_next_day(self):
        self.state.record_close("AAPL.US", -500.0, 0, UTC_NOON)
        self.assertIsNotNone(self.check())
        self.assertIsNone(self.check(now=UTC_NOON + timedelta(days=1)))


class CooldownTest(unittest.TestCase):
    def setUp(self):
        self.state = RiskState()
        self.settings = settings(cooldown_after_losses=3)

    def lose(self, times, now=UTC_NOON):
        for _ in range(times):
            self.state.record_close("AAPL.US", -10.0, 3, now)

    def check(self, now=UTC_NOON):
        return check_limits(self.settings, 0, self.state, "AAPL.US", 50.0, now)

    def test_two_losses_do_not_trigger(self):
        self.lose(2)
        self.assertIsNone(self.check())

    def test_third_consecutive_loss_triggers(self):
        self.lose(3)
        denial = self.check()
        self.assertIsNotNone(denial)
        self.assertIn("cooling off", denial)

    def test_a_win_resets_the_streak(self):
        self.lose(2)
        self.state.record_close("AAPL.US", 5.0, 3, UTC_NOON)
        self.lose(2)
        self.assertIsNone(self.check())

    def test_cooldown_expires(self):
        self.lose(3)
        later = UTC_NOON + timedelta(minutes=COOLDOWN_MINUTES + 1)
        self.assertIsNone(self.check(now=later))

    def test_cooldown_applies_across_markets(self):
        # A losing streak is about judgment, not about one exchange.
        self.lose(3)
        self.assertIsNotNone(check_limits(self.settings, 0, self.state,
                                          "D05.SG", 50.0, UTC_NOON))

    def test_zero_disables_the_rule(self):
        state = RiskState()
        for _ in range(20):
            state.record_close("AAPL.US", -10.0, 0, UTC_NOON)
        self.assertIsNone(check_limits(settings(cooldown_after_losses=0), 0, state,
                                       "AAPL.US", 50.0, UTC_NOON))


class PersistenceTest(unittest.TestCase):
    def test_round_trips_through_json(self):
        state = RiskState()
        state.record_buy("AAPL.US", 250.0, UTC_NOON)
        state.record_close("AAPL.US", -30.0, 3, UTC_NOON)
        restored = RiskState.from_json(state.to_json())
        self.assertEqual(restored.deployed_today("US", UTC_NOON), 250.0)
        self.assertEqual(restored.realized_today("US", UTC_NOON), -30.0)
        self.assertEqual(restored.consecutive_losses, state.consecutive_losses)

    def test_a_daily_cap_survives_a_restart(self):
        # A limit that resets when the process does is not a limit.
        state = RiskState()
        state.record_buy("AAPL.US", 1000.0, UTC_NOON)
        restored = RiskState.from_json(state.to_json())
        denial = check_limits(settings(budget=1000.0, daily_turnover_multiple=1.0), 0, restored,
                              "AAPL.US", 100.0, UTC_NOON)
        self.assertIsNotNone(denial)

    def test_missing_or_corrupt_state_is_not_fatal(self):
        self.assertEqual(RiskState.from_json({}).consecutive_losses, 0)
        self.assertEqual(RiskState.from_json(None).consecutive_losses, 0)

    def test_pruning_bounds_the_state(self):
        state = RiskState()
        for day in range(120):
            state.record_buy("AAPL.US", 1.0, UTC_NOON + timedelta(days=day))
        state.prune()
        self.assertLessEqual(len(state.daily_deployed), 40)

    def test_pruning_keeps_the_most_recent_days(self):
        state = RiskState()
        for day in range(120):
            state.record_buy("AAPL.US", 1.0, UTC_NOON + timedelta(days=day))
        state.prune()
        latest = local_date("US", UTC_NOON + timedelta(days=119))
        self.assertIn(f"US:{latest}", state.daily_deployed)


class RuleIndependenceTest(unittest.TestCase):
    def test_all_rules_off_never_blocks(self):
        # 99 open positions would now trip the concentration cap on its own —
        # it is the one rule that cannot be switched off — so this stays under
        # it and still proves the other three are independent.
        state = RiskState()
        state.record_buy("AAPL.US", 10_000.0, UTC_NOON)
        state.record_close("AAPL.US", -9_000.0, 0, UTC_NOON)
        self.assertIsNone(check_limits(settings(), 19, state, "AAPL.US", 5_000.0, UTC_NOON))


if __name__ == "__main__":
    unittest.main()
