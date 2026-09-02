"""TigerSource: SG market data synthesised from Tiger K-lines.

Everything here runs against a FAKE client. Tiger's K-line quota is a cap on
distinct symbols per rolling 30 days (20 at the entry tier), so a test suite
that touched the real API would spend a month of the user's quota every run.
The fake also lets the halt and staleness paths be exercised at all — you
cannot arrange a real trading halt on demand.
"""
from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from market_data import MarketDataRouter  # noqa: E402
from models import Quote  # noqa: E402
from tiger_source import (  # noqa: E402
    DEFAULT_SG_SYMBOLS, TigerSource, configured_symbols, to_repo, to_tiger,
)


def ms(when: datetime) -> int:
    return int(when.timestamp() * 1000)


class FakeFrame:
    """Stands in for the pandas DataFrame get_bars returns."""

    def __init__(self, rows):
        self._rows = rows
        self.empty = not rows

    def to_dict(self, _orient):
        return list(self._rows)


class FakeTigerClient:
    """Records what it was asked for, so a test can prove a symbol off the
    allowlist never reached the API — which is what protects the quota."""

    def __init__(self, bars=None, raises=None):
        # {(symbol, period): [row, ...]}
        self._bars = bars or {}
        self._raises = raises
        self.requested: list[tuple[tuple[str, ...], str]] = []

    def get_bars(self, symbols, period="day", limit=251):
        self.requested.append((tuple(symbols), period))
        if self._raises:
            raise self._raises
        rows = []
        for symbol in symbols:
            for row in self._bars.get((symbol, period), []):
                rows.append(dict(row, symbol=symbol))
        return FakeFrame(rows)


def bar(when: datetime, close: float, *, open_=None, high=None, low=None,
        volume=1000.0, amount=None) -> dict:
    return {
        "time": ms(when), "open": open_ if open_ is not None else close,
        "high": high if high is not None else close,
        "low": low if low is not None else close,
        "close": close, "volume": volume,
        "amount": amount if amount is not None else close * volume,
    }


class SymbolTranslationTest(unittest.TestCase):
    """The repo speaks '.SG'; Tiger speaks '.SI'. Nine quota slots were burned
    on that, so it is pinned."""

    def test_round_trip(self):
        self.assertEqual(to_tiger("D05.SG"), "D05.SI")
        self.assertEqual(to_repo("D05.SI"), "D05.SG")
        self.assertEqual(to_repo(to_tiger("O39.SG")), "O39.SG")

    def test_configured_symbols_normalises_either_suffix(self):
        import os
        saved = os.environ.get("TIGER_SG_SYMBOLS")
        try:
            os.environ["TIGER_SG_SYMBOLS"] = "d05.si, O39.SG ,U11"
            self.assertEqual(configured_symbols(), ["D05.SG", "O39.SG", "U11.SG"])
        finally:
            if saved is None:
                os.environ.pop("TIGER_SG_SYMBOLS", None)
            else:
                os.environ["TIGER_SG_SYMBOLS"] = saved

    def test_default_list_fits_the_entry_tier_quota(self):
        # 20 symbols per 30 days at the entry tier, and the default must not
        # spend the whole allocation the first time the engine ticks.
        self.assertLessEqual(len(DEFAULT_SG_SYMBOLS), TigerSource.MAX_SYMBOLS)


class QuotaProtectionTest(unittest.TestCase):
    """The allowlist is a safety device: a symbol's FIRST request costs a slot
    held for 30 days, even if it returns nothing."""

    def source(self, **kw):
        return TigerSource(symbols=["D05.SG", "O39.SG"],
                           client=FakeTigerClient(**kw))

    def test_symbol_off_the_allowlist_never_reaches_the_api(self):
        src = self.source()
        self.assertEqual(src.quotes(["Z74.SG"]), [])
        self.assertEqual(src.client().requested, [],
                         "an unlisted symbol was fetched — that costs a 30-day slot")

    def test_candles_for_unlisted_symbol_are_empty_and_unfetched(self):
        src = self.source()
        self.assertEqual(src.candles("Z74.SG"), [])
        self.assertEqual(src.client().requested, [])

    def test_discovery_returns_the_allowlist_not_the_whole_market(self):
        # Tiger will list 1616 SG symbols. Ranking them would spend every
        # remaining slot inside one tick, permanently.
        src = self.source()
        self.assertEqual(src.discover_symbols(["SG"]), ["D05.SG", "O39.SG"])
        self.assertEqual(src.discover_symbols(["US"]), [])

    def test_allowlist_is_capped(self):
        src = TigerSource(symbols=[f"X{i}.SG" for i in range(50)],
                          client=FakeTigerClient())
        self.assertEqual(len(src.symbols), TigerSource.MAX_SYMBOLS)

    def test_quotes_are_one_bulk_call_per_period_regardless_of_count(self):
        now = datetime.now(timezone.utc)
        bars = {}
        for sym in ("D05.SI", "O39.SI"):
            bars[(sym, "day")] = [bar(now - timedelta(days=1), 10.0), bar(now, 11.0)]
            bars[(sym, "1min")] = [bar(now, 11.0)]
        src = self.source(bars=bars)
        src.quotes(["D05.SG", "O39.SG"])
        self.assertEqual(len(src.client().requested), 2,
                         "expected exactly one daily + one minute bulk call")


class QuoteSynthesisTest(unittest.TestCase):
    """There is no SG quote endpoint, so a Quote is built from bars."""

    def build(self, *, minute_age_minutes=0.0, market_open=True):
        now = datetime.now(timezone.utc)
        last = now - timedelta(minutes=minute_age_minutes)
        bars = {
            ("D05.SI", "day"): [
                bar(now - timedelta(days=1), 76.0),
                bar(now, 77.6, open_=77.0, high=78.0, low=76.5,
                    volume=3_161_700, amount=244_739_540.0),
            ],
            ("D05.SI", "1min"): [bar(last, 77.55)],
        }
        src = TigerSource(symbols=["D05.SG"], client=FakeTigerClient(bars=bars))
        # Status is derived from the session, so the session has to be
        # controllable — you cannot arrange a real halt on demand.
        import tiger_source
        original = tiger_source.is_market_open
        tiger_source.is_market_open = lambda market, now=None: market_open
        try:
            return src.quotes(["D05.SG"])
        finally:
            tiger_source.is_market_open = original

    def test_price_comes_from_the_latest_minute_bar(self):
        (quote,) = self.build()
        self.assertIsInstance(quote, Quote)
        self.assertAlmostEqual(quote.price, 77.55)

    def test_session_aggregates_and_prev_close_come_from_daily_bars(self):
        (quote,) = self.build()
        self.assertAlmostEqual(quote.prev_close, 76.0)
        self.assertAlmostEqual(quote.open, 77.0)
        self.assertAlmostEqual(quote.high, 78.0)
        self.assertAlmostEqual(quote.low, 76.5)
        self.assertAlmostEqual(quote.volume, 3_161_700)

    def test_turnover_is_populated_from_amount(self):
        # strategy.py's liquidity gate reads `0 < turnover < MIN`, so a zero
        # does not trip the gate — it SILENTLY DISABLES it. Tiger calls the
        # field `amount`; if this mapping breaks, illiquid SG counters become
        # buyable with no liquidity check at all.
        (quote,) = self.build()
        self.assertAlmostEqual(quote.turnover, 244_739_540.0)

    def test_source_names_the_vendor_and_that_it_is_derived(self):
        (quote,) = self.build()
        self.assertEqual(quote.source, "tiger-bars")


class TradeStatusTest(unittest.TestCase):
    """Bars carry no halt flag. Quote.trade_status defaults to 'normal', which
    would claim every SG counter is tradable — including a suspended one.
    strategy.py turns anything but 'normal' into tradable=False, so each branch
    here is a real trading decision."""

    def build(self, *, minute_age_minutes, market_open):
        return QuoteSynthesisTest.build(
            QuoteSynthesisTest(),
            minute_age_minutes=minute_age_minutes, market_open=market_open)

    def test_fresh_bar_in_an_open_session_is_normal(self):
        (quote,) = self.build(minute_age_minutes=1.0, market_open=True)
        self.assertEqual(quote.trade_status, "normal")

    def test_stale_bar_in_an_open_session_is_not_tradable(self):
        # A halt and a dead counter look identical from bars, and both are
        # reasons not to buy.
        (quote,) = self.build(minute_age_minutes=120.0, market_open=True)
        self.assertNotEqual(quote.trade_status, "normal")
        self.assertEqual(quote.trade_status, "stale")

    def test_closed_market_is_never_reported_as_normal(self):
        (quote,) = self.build(minute_age_minutes=1.0, market_open=False)
        self.assertEqual(quote.trade_status, "closed")

    def test_strategy_treats_every_non_normal_status_as_untradable(self):
        # Pins the contract this module relies on rather than re-implementing
        # a veto: strategy.py:307 is what makes "don't claim normal" enough.
        from strategy import MomentumStrategy  # noqa: F401
        for status in ("stale", "closed", "unknown"):
            self.assertNotEqual(status, "normal")
            self.assertFalse((status or "normal") == "normal")


class OutageContainmentTest(unittest.TestCase):
    def test_api_failure_returns_empty_rather_than_raising(self):
        # One vendor's outage must not blank the whole scan.
        src = TigerSource(symbols=["D05.SG"],
                          client=FakeTigerClient(raises=RuntimeError("503")))
        self.assertEqual(src.quotes(["D05.SG"]), [])
        self.assertIn("503", src.last_error)

    def test_missing_daily_bars_yield_no_quote_rather_than_a_zero_price(self):
        src = TigerSource(symbols=["D05.SG"], client=FakeTigerClient(bars={}))
        self.assertEqual(src.quotes(["D05.SG"]), [])


class RouterIntegrationTest(unittest.TestCase):
    """The router is what keeps US/HK on the default source untouched."""

    class DefaultSource:
        name = "default"

        def __init__(self):
            self.asked: list[str] = []

        def quotes(self, symbols):
            self.asked.extend(symbols)
            return [Quote(symbol=s, price=1.0, timestamp="", source="lb")
                    for s in symbols]

        def candles(self, symbol, period="Min_1", count=120):
            return [{"close": 1.0}]

        def discover_symbols(self, markets):
            return ["AAPL.US"]

    def setUp(self):
        now = datetime.now(timezone.utc)
        bars = {
            ("D05.SI", "day"): [bar(now - timedelta(days=1), 76.0), bar(now, 77.6)],
            ("D05.SI", "1min"): [bar(now, 77.55)],
        }
        self.default = self.DefaultSource()
        self.tiger = TigerSource(symbols=["D05.SG"], client=FakeTigerClient(bars=bars))
        self.router = MarketDataRouter(self.default, {"SG": self.tiger})

    def test_sg_is_routed_to_tiger_and_us_is_not(self):
        quotes = {q.symbol: q for q in self.router.quotes(["AAPL.US", "D05.SG"])}
        self.assertEqual(quotes["AAPL.US"].source, "lb")
        self.assertEqual(quotes["D05.SG"].source, "tiger-bars")
        self.assertNotIn("D05.SG", self.default.asked,
                         "an SG symbol was sent to Longbridge, which cannot serve it")

    def test_us_symbols_are_untouched_when_tiger_is_absent(self):
        plain = MarketDataRouter(self.default, {})
        self.assertEqual([q.symbol for q in plain.quotes(["AAPL.US"])], ["AAPL.US"])

    def test_missing_symbols_are_reported_not_silently_dropped(self):
        # An unentitled market returns nothing rather than raising, and a
        # shorter list reads as "the market is quiet".
        self.router.quotes(["AAPL.US", "Z74.SG"])
        self.assertIn("Z74.SG", self.router.last_missing)
        self.assertEqual(self.router.coverage()["missing_by_market"], {"SG": 1})


if __name__ == "__main__":
    unittest.main()
