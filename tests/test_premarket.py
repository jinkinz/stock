"""Pre-market watchlist — what to watch before the bell.

The default universe ranks by yesterday's turnover, which answers "what was
liquid", not "what is moving today". These tests pin the screen's judgement:
volume-backed gaps outrank bigger gaps on nothing, gap-downs are excluded
because the engine cannot short, and no pre-market data means falling back
rather than scanning an empty list.

Run from the repo root:  python3 -m unittest discover tests
"""
from __future__ import annotations

import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from market_hours import minutes_until_open  # noqa: E402
from models import Quote  # noqa: E402
from premarket import (  # noqa: E402
    MIN_ABS_GAP_PCT, MIN_PRE_MARKET_TURNOVER,
    has_premarket_data, rank_gappers, watchlist_symbols,
)


def quote(symbol, gap=0.0, turnover=0.0, price=100.0) -> Quote:
    return Quote(symbol=symbol, price=price, timestamp="", source="longbridge",
                 pre_market_price=price if turnover or gap else 0.0,
                 pre_market_change_pct=gap, pre_market_turnover=turnover)


class RankingTest(unittest.TestCase):
    def test_ranks_by_gap_when_volume_is_equal(self):
        ranked = rank_gappers([quote("A.US", 2.0, 1e6), quote("B.US", 6.0, 1e6)])
        self.assertEqual([g.symbol for g in ranked], ["B.US", "A.US"])

    def test_volume_backed_gap_beats_a_bigger_gap_on_nothing(self):
        # An 8% move on a handful of shares is a quote artefact; a 3% move on
        # real money is a position someone actually took.
        ranked = rank_gappers([quote("THIN.US", 8.0, MIN_PRE_MARKET_TURNOVER),
                               quote("REAL.US", 3.0, 5e8)])
        self.assertEqual(ranked[0].symbol, "REAL.US")

    def test_limit_is_respected(self):
        quotes = [quote(f"S{i}.US", 2.0 + i, 1e6) for i in range(30)]
        self.assertEqual(len(rank_gappers(quotes, limit=5)), 5)

    def test_zero_limit_returns_everything(self):
        quotes = [quote(f"S{i}.US", 2.0 + i, 1e6) for i in range(7)]
        self.assertEqual(len(rank_gappers(quotes, limit=0)), 7)


class ExclusionTest(unittest.TestCase):
    def test_gap_downs_are_excluded_because_shorting_is_impossible(self):
        # Ranking a name that collapsed 9% fills the watchlist with symbols the
        # engine is structurally unable to act on.
        self.assertEqual(rank_gappers([quote("DOWN.US", -9.0, 1e7)]), [])

    def test_gap_downs_can_be_included_explicitly(self):
        ranked = rank_gappers([quote("DOWN.US", -9.0, 1e7)], longs_only=False)
        self.assertEqual(len(ranked), 1)

    def test_thin_volume_is_excluded(self):
        self.assertEqual(rank_gappers([quote("X.US", 12.0, MIN_PRE_MARKET_TURNOVER / 2)]), [])

    def test_noise_level_gaps_are_excluded(self):
        self.assertEqual(rank_gappers([quote("X.US", MIN_ABS_GAP_PCT / 2, 1e7)]), [])

    def test_quotes_without_a_premarket_print_are_skipped(self):
        plain = Quote(symbol="X.US", price=50.0, timestamp="", source="longbridge")
        self.assertEqual(rank_gappers([plain]), [])


class DataAvailabilityTest(unittest.TestCase):
    def test_detects_usable_premarket_data(self):
        self.assertTrue(has_premarket_data([quote("A.US", 3.0, 1e6)]))

    def test_no_premarket_session_is_detectable(self):
        # HK/SG have no pre-open print here, and outside pre-open every field
        # is 0.0. The caller must fall back, not scan an empty watchlist.
        plain = [Quote(symbol="700.HK", price=300.0, timestamp="", source="longbridge")]
        self.assertFalse(has_premarket_data(plain))

    def test_zero_means_no_data_not_unchanged(self):
        self.assertFalse(has_premarket_data([quote("A.US", 0.0, 0.0)]))


class WatchlistTest(unittest.TestCase):
    def test_returns_plain_symbols(self):
        symbols = watchlist_symbols([quote("A.US", 4.0, 1e7), quote("B.US", 2.0, 1e7)], limit=2)
        self.assertEqual(symbols, ["A.US", "B.US"])

    def test_serialises_for_the_api(self):
        payload = rank_gappers([quote("A.US", 4.0, 1e7)])[0].as_dict()
        self.assertEqual(sorted(payload), ["gap_pct", "price", "score", "symbol", "turnover"])


class TimingTest(unittest.TestCase):
    def test_open_market_returns_zero(self):
        # Monday 14:00 UTC = 10:00 New York, mid-session.
        self.assertEqual(minutes_until_open("US", datetime(2026, 8, 10, 14, 0, tzinfo=timezone.utc)), 0.0)

    def test_counts_down_to_the_bell(self):
        # 13:00 UTC Monday = 09:00 New York, 30 minutes before the open.
        self.assertEqual(minutes_until_open("US", datetime(2026, 8, 10, 13, 0, tzinfo=timezone.utc)), 30.0)

    def test_skips_the_weekend(self):
        # Saturday: the next open is Monday, well over a day away.
        self.assertGreater(minutes_until_open("US", datetime(2026, 8, 8, 14, 0, tzinfo=timezone.utc)), 24 * 60)

    def test_markets_have_independent_clocks(self):
        moment = datetime(2026, 8, 9, 19, 0, tzinfo=timezone.utc)
        self.assertNotEqual(minutes_until_open("US", moment), minutes_until_open("SG", moment))

    def test_unknown_market_is_not_treated_as_pending(self):
        self.assertEqual(minutes_until_open("ZZ", datetime(2026, 8, 10, 14, 0, tzinfo=timezone.utc)), 0.0)



class HorizonMatchTest(unittest.TestCase):
    """The screen's metric must match the horizon it feeds.

    Selecting on a one-session gap and handing the result to a strategy whose
    structure and momentum span twenty days is the same class of error as
    computing EMA9 on 1-minute bars and holding for a week.
    """

    def leaders(self, **overrides):
        from premarket import rank_momentum_leaders
        book = {
            "STRONG.US": (120.0, {"change_lookback_pct": 18.0,
                                  "range_high": 121.0, "range_low": 100.0}),
            "FADED.US": (102.0, {"change_lookback_pct": 22.0,
                                 "range_high": 125.0, "range_low": 100.0}),
            "FLAT.US": (100.5, {"change_lookback_pct": 0.4,
                                "range_high": 101.0, "range_low": 100.0}),
            "FALLING.US": (90.0, {"change_lookback_pct": -8.0,
                                  "range_high": 110.0, "range_low": 89.0}),
        }
        book.update(overrides)
        return rank_momentum_leaders(book)

    def test_leader_near_its_highs_ranks(self):
        self.assertEqual(self.leaders()[0].symbol, "STRONG.US")

    def test_a_bigger_move_already_given_back_is_excluded(self):
        # Up 22% over the window but sitting at the bottom of its own range:
        # the move happened and then reversed. That is not leadership.
        self.assertNotIn("FADED.US", [l.symbol for l in self.leaders()])

    def test_flat_and_falling_names_are_excluded(self):
        symbols = [l.symbol for l in self.leaders()]
        self.assertNotIn("FLAT.US", symbols)
        self.assertNotIn("FALLING.US", symbols)

    def test_missing_indicators_are_skipped_not_guessed(self):
        self.assertEqual(self.leaders(**{"NODATA.US": (50.0, {})})[0].symbol, "STRONG.US")

    def test_score_rewards_both_strength_and_position(self):
        from premarket import rank_momentum_leaders
        same_move = {
            "HIGH.US": (120.0, {"change_lookback_pct": 15.0, "range_high": 121.0, "range_low": 100.0}),
            "MID.US": (110.0, {"change_lookback_pct": 15.0, "range_high": 121.0, "range_low": 100.0}),
        }
        self.assertEqual(rank_momentum_leaders(same_move)[0].symbol, "HIGH.US")

    def test_config_key_distinguishes_the_two_screens(self):
        from models import Settings
        intraday = Settings(trading_horizon="intraday").normalized().config_key()
        swing = Settings(trading_horizon="swing").normalized().config_key()
        self.assertIn("gappers", intraday)
        self.assertIn("leaders", swing)

    def test_disabling_the_watchlist_is_also_distinguishable(self):
        from models import Settings
        off = Settings(trading_horizon="swing", use_premarket_watchlist=False).normalized()
        self.assertIn("turnover", off.config_key())


class WatchlistSeedsPoolTest(unittest.TestCase):
    """The watchlist must SEED the scan pool, not replace it.

    Replacing froze the universe at names chosen before the bell, so a stock
    that broke out mid-session with no pre-market gap could never be seen —
    while the ranking that picks candidates re-runs every tick and had nothing
    new to look at. Widening is free: 20 and 200 symbols are one quote call.
    """

    def setUp(self):
        import app
        from models import Settings
        self._saved = (app.STATE.settings, app.STATE.premarket_watchlist,
                       app.STATE.premarket_built_at)
        from datetime import datetime, timezone
        app.STATE.settings = Settings(use_premarket_watchlist=True,
                                      max_scan_symbols=200).normalized()
        app.STATE.premarket_watchlist = [{"symbol": f"GAP{i}.US"} for i in range(5)]
        app.STATE.premarket_built_at = datetime.now(timezone.utc).isoformat()
        app.STATE.watchlist_kind = "gappers"
        app.STATE._symbol_cache = [f"POOL{i}.US" for i in range(50)]
        app.STATE._symbol_cache_markets = list(app.STATE.settings.markets)
        import time as _t
        app.STATE._symbol_cache_at = _t.monotonic()
        self.engine = app.TradingEngine()

    def tearDown(self):
        import app
        (app.STATE.settings, app.STATE.premarket_watchlist,
         app.STATE.premarket_built_at) = self._saved

    def universe(self):
        class NoDiscovery:
            def discover_symbols(self, markets): return []
        return self.engine._resolve_universe(NoDiscovery())

    def test_watchlist_names_are_present(self):
        symbols = self.universe()
        for i in range(5):
            self.assertIn(f"GAP{i}.US", symbols)

    def test_the_broader_pool_is_present_too(self):
        # The whole point: a mid-session breakout outside the watchlist must
        # still be reachable.
        symbols = self.universe()
        self.assertIn("POOL0.US", symbols)
        self.assertGreater(len(symbols), 5, "pool was replaced, not seeded")

    def test_watchlist_names_come_first_so_they_survive_the_cap(self):
        self.assertEqual(self.universe()[:5], [f"GAP{i}.US" for i in range(5)])

    def test_no_duplicates_when_a_gapper_is_also_in_the_pool(self):
        import app
        app.STATE.premarket_watchlist = [{"symbol": "POOL0.US"}]
        symbols = self.universe()
        self.assertEqual(symbols.count("POOL0.US"), 1)

    def test_pool_respects_the_scan_cap(self):
        import app
        app.STATE.settings.max_scan_symbols = 10
        app.STATE.settings.normalized()
        self.assertLessEqual(len(self.universe()), 25)

if __name__ == "__main__":
    unittest.main()
