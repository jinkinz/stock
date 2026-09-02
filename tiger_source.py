"""Tiger Brokers market data for SG, behind `market_data.MarketDataRouter`.

READ ONLY. This module never places an order and never imports a TradeClient.
SG orders go to Longbridge, which accepts them, whose SG fee schedule is
`verified=True` against real contract notes, and whose
`estimate_max_purchase_quantity` backs the cash-cover guard. Tiger is eyes,
Longbridge is hands. See market_data.py for why the two are split.

WHAT TIGER ACTUALLY SERVES (measured — see market_data.py for the full probe)
---------------------------------------------------------------------------
    get_bars(...)            -> WORKS for SG, and carries `amount` (turnover)
    get_stock_briefs(...)    -> permission denied
    get_stock_delay_briefs   -> "only support us market symbols"

So there is NO quote endpoint for SG on this account, and a Quote must be
SYNTHESISED FROM BARS. That is the whole design of this module, and it is why
`quotes()` costs two calls rather than one: the daily bar supplies the session
aggregates and the previous close, the minute bar supplies the live price.

THE QUOTA IS A SYMBOL ALLOWLIST, NOT A RATE LIMIT
-------------------------------------------------
Tiger's K-line quota caps DISTINCT SYMBOLS per rolling 30 days (20 at the
entry tier, 200 above $10K assets). A symbol is consumed on FIRST REQUEST —
including a request that returns nothing — and re-requesting a symbol already
inside the window is FREE (measured: quota held at 10/10 across a re-request).

Two consequences shape this class:

  * Polling is free, so a small watchlist can be refreshed every tick forever.
  * Touching a NEW symbol is permanently expensive for 30 days. So this source
    refuses any symbol outside `self.symbols`, and `discover_symbols()` returns
    that allowlist rather than the 1616 names Tiger will happily list. Without
    that, ONE discovery pass would exhaust a month of quota in a single tick.

The allowlist is therefore a safety device, not a convenience. Do not widen it
to "whatever was asked for".

TRADE STATUS IS DERIVED, NOT ASSUMED
------------------------------------
Bars carry no halt flag, and `models.Quote.trade_status` defaults to "normal",
which would silently claim every SG counter is tradable — including a
suspended one, whose stale bars look exactly like a quiet one's. CLAUDE.md is
explicit that halted symbols are never bought, so this module never writes
"normal" unless it has evidence: status is derived from BAR FRESHNESS against
the exchange session, and anything else is a non-"normal" string, which
`strategy.py` already turns into `tradable=False` and a refusal to buy.

Absence of evidence is not evidence of tradability.
"""
from __future__ import annotations

import os
import time
from datetime import datetime, timezone

from market_hours import is_market_open, market_of
from models import Quote

# The repo speaks ".SG" everywhere — market_of(), MARKET_CURRENCY, the live
# guard, the universe. Tiger speaks ".SI". Nine quota slots were burned
# discovering that, so the translation lives in exactly one place.
REPO_SUFFIX = ".SG"
TIGER_SUFFIX = ".SI"

# Longbridge period string -> Tiger BarPeriod value. Keyed by the strings
# CANDLE_SPEC already uses so callers pass what they always passed.
PERIOD_MAP = {
    "Day": "day", "Week": "week", "Month": "month",
    "Min_1": "1min", "Min_3": "3min", "Min_5": "5min", "Min_15": "15min",
    "Min_30": "30min", "Min_60": "60min",
}

# A liquid, boring default: STI large caps. Deliberately small — every name
# here costs a quota slot the first time it is touched, held for 30 days.
DEFAULT_SG_SYMBOLS = [
    "D05.SG",   # DBS
    "O39.SG",   # OCBC
    "U11.SG",   # UOB
    "Z74.SG",   # Singtel
    "C6L.SG",   # Singapore Airlines
    "S68.SG",   # SGX
    "C38U.SG",  # CapitaLand Integrated Commercial Trust
    "A17U.SG",  # CapitaLand Ascendas REIT
    "F34.SG",   # Wilmar
    "BN4.SG",   # Keppel
]


def configured_symbols() -> list[str]:
    """SG watchlist from .env (`TIGER_SG_SYMBOLS`), else the default list.

    Accepts either suffix so a paste from Tiger's own UI works.
    """
    raw = os.environ.get("TIGER_SG_SYMBOLS", "").strip()
    if not raw:
        return list(DEFAULT_SG_SYMBOLS)
    out = []
    for chunk in raw.replace(";", ",").split(","):
        sym = chunk.strip().upper()
        if not sym:
            continue
        if sym.endswith(TIGER_SUFFIX):
            sym = sym[: -len(TIGER_SUFFIX)] + REPO_SUFFIX
        elif not sym.endswith(REPO_SUFFIX):
            sym = sym + REPO_SUFFIX
        out.append(sym)
    return list(dict.fromkeys(out))


def to_tiger(symbol: str) -> str:
    """'D05.SG' -> 'D05.SI'."""
    base, _, _ = symbol.rpartition(".")
    return (base or symbol) + TIGER_SUFFIX


def to_repo(symbol: str) -> str:
    """'D05.SI' -> 'D05.SG'."""
    base, _, _ = symbol.rpartition(".")
    return (base or symbol) + REPO_SUFFIX


class TigerSource:
    """Market data for SG counters, synthesised from Tiger K-lines.

    Presents the data half of a broker (`quote`, `quotes`, `candles`,
    `discover_symbols`) so `MarketDataRouter` can drop it in without any call
    site upstream knowing Tiger exists.
    """

    name = "tiger"

    # Hard ceiling on the allowlist. The entry tier is 20 symbols / 30 days;
    # this stops a fat-fingered TIGER_SG_SYMBOLS from spending a month of
    # quota in one tick. Raise it only alongside an actual tier upgrade.
    MAX_SYMBOLS = 20

    # A symbol whose most recent bar is older than this during an OPEN session
    # is not treated as tradable. Halted counters keep serving stale bars that
    # are indistinguishable from a quiet counter's, so this is the only halt
    # signal available without a quote feed. Generous on purpose: SG small caps
    # genuinely trade in bursts, and the cost of being wrong is a missed buy.
    STALE_MINUTES = 15

    # Quotes are rebuilt from bars at most this often. The engine's quote loop
    # can tick every 10s; two API calls per tick per market is needless when
    # the underlying bar is one minute wide.
    CACHE_SECONDS = 20.0

    def __init__(self, symbols: list[str] | None = None, client=None) -> None:
        allow = symbols if symbols is not None else configured_symbols()
        self.symbols = list(dict.fromkeys(
            s.upper() for s in allow if s))[: self.MAX_SYMBOLS]
        self._client = client
        self._cache: dict[str, Quote] = {}
        self._cache_at: float = 0.0
        self.last_error: str = ""

    # ------------------------------------------------------------------
    # Client
    # ------------------------------------------------------------------

    def client(self):
        """Lazily built QuoteClient. Import is deferred so the app still boots
        with tigeropen absent — SG is an optional market, not a requirement."""
        if self._client is not None:
            return self._client
        from tigeropen.common.consts import Language
        from tigeropen.quote.quote_client import QuoteClient
        from tigeropen.tiger_open_config import TigerOpenClientConfig

        config = TigerOpenClientConfig()
        config.tiger_id = os.environ["TIGER_ID"]
        config.account = os.environ["TIGER_ACCOUNT"]
        config.private_key = os.environ["TIGER_PRIVATE_KEY"].strip()
        config.license = os.environ.get("TIGER_LICENSE", "TBSG")
        config.language = Language.en_US
        # is_grab_permission=False: grabbing SEIZES the market-data device slot
        # and kicks the user's own Tiger app off the live feed. Bars do not
        # need it (measured), and nothing here reads a realtime quote.
        self._client = QuoteClient(config, is_grab_permission=False)
        return self._client

    # ------------------------------------------------------------------
    # Allowlist
    # ------------------------------------------------------------------

    def serves(self, symbol: str) -> bool:
        return symbol.upper() in self.symbols

    def _permitted(self, symbols: list[str]) -> list[str]:
        """Filter to the allowlist. Anything else is DROPPED, not fetched —
        an unknown symbol costs a 30-day quota slot on first touch."""
        return [s for s in dict.fromkeys(s.upper() for s in symbols) if s in self.symbols]

    # ------------------------------------------------------------------
    # Data surface
    # ------------------------------------------------------------------

    def discover_symbols(self, markets: list[str]) -> list[str]:
        """The allowlist, NOT Tiger's 1616 SG names.

        Returning the real list would be honest and ruinous: the engine would
        rank it, quote it, and spend every remaining quota slot inside one
        tick, permanently, for 30 days.
        """
        if not any(str(m).upper() == "SG" for m in markets):
            return []
        return list(self.symbols)

    def candles(self, symbol: str, period: str = "Min_1", count: int = 120) -> list[dict]:
        """Bars in the repo's shape: [{close, open, high, low, volume, turnover,
        timestamp}, ...] oldest-first. Empty for a symbol off the allowlist."""
        if not self.serves(symbol):
            return []
        rows = self._bars([symbol.upper()], period=period, limit=count)
        return [self._row_to_candle(r) for r in rows.get(symbol.upper(), [])]

    def quote(self, symbol: str) -> Quote:
        found = self.quotes([symbol])
        if found:
            return found[0]
        raise KeyError(f"{symbol} is not served by {self.name}")

    def quotes(self, symbols: list[str]) -> list[Quote]:
        """Synthesise quotes from bars — there is no SG quote endpoint.

        Two bulk calls regardless of symbol count: daily bars for the session
        aggregates and the previous close, minute bars for the live price and
        the freshness that `trade_status` is derived from.
        """
        wanted = self._permitted(symbols)
        if not wanted:
            return []

        now = time.monotonic()
        if self._cache and (now - self._cache_at) < self.CACHE_SECONDS:
            cached = [self._cache[s] for s in wanted if s in self._cache]
            if len(cached) == len(wanted):
                return cached

        try:
            daily = self._bars(wanted, period="Day", limit=2)
            minute = self._bars(wanted, period="Min_1", limit=3)
            self.last_error = ""
        except Exception as exc:
            # Contained: an outage here must not blank the whole scan, and the
            # router reports it. Serve the cache if we have one.
            self.last_error = str(exc)
            return [self._cache[s] for s in wanted if s in self._cache]

        built = []
        for symbol in wanted:
            quote = self._synthesise(symbol, daily.get(symbol, []), minute.get(symbol, []))
            if quote is not None:
                self._cache[symbol] = quote
                built.append(quote)
        self._cache_at = now
        return built

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _bars(self, symbols: list[str], period: str, limit: int) -> dict[str, list[dict]]:
        """One bulk get_bars call -> {repo symbol: [row, ...]} oldest-first."""
        tiger_period = PERIOD_MAP.get(period, PERIOD_MAP["Day"])
        frame = self.client().get_bars(
            [to_tiger(s) for s in symbols], period=tiger_period, limit=limit)
        out: dict[str, list[dict]] = {}
        if frame is None or getattr(frame, "empty", True):
            return out
        for row in frame.to_dict("records"):
            symbol = to_repo(str(row.get("symbol", "")))
            out.setdefault(symbol, []).append(row)
        for rows in out.values():
            rows.sort(key=lambda r: r.get("time") or 0)
        return out

    @staticmethod
    def _row_to_candle(row: dict) -> dict:
        raw_ts = row.get("time") or 0
        stamp = ""
        if raw_ts:
            stamp = datetime.fromtimestamp(raw_ts / 1000.0, timezone.utc).isoformat()
        return {
            "close": float(row.get("close") or 0.0),
            "open": float(row.get("open") or 0.0),
            "high": float(row.get("high") or 0.0),
            "low": float(row.get("low") or 0.0),
            "volume": float(row.get("volume") or 0.0),
            # Tiger calls turnover `amount`. This is the field strategy.py's
            # liquidity gate reads; without it the gate silently goes dark,
            # because it tests `0 < turnover < MIN` and a zero is not "illiquid".
            "turnover": float(row.get("amount") or 0.0),
            "timestamp": stamp,
        }

    def _synthesise(self, symbol: str, daily: list[dict], minute: list[dict]) -> Quote | None:
        if not daily:
            return None
        today = daily[-1]
        prev_close = float(daily[-2].get("close") or 0.0) if len(daily) > 1 else 0.0

        # The live price is the most recent MINUTE close when there is one; the
        # daily bar's close is the same figure at lower resolution and is the
        # fallback when minute bars are unavailable (or the session is shut).
        last_bar = minute[-1] if minute else today
        price = float(last_bar.get("close") or 0.0)
        if price <= 0:
            return None

        status, bar_age_s = self._trade_status(symbol, last_bar)
        return Quote(
            symbol=symbol,
            price=price,
            timestamp=datetime.now(timezone.utc).isoformat(),
            # Names the vendor AND the fact that this is a derived quote, so it
            # is never mistaken for an exchange top-of-book in a log or the UI.
            source="tiger-bars",
            prev_close=prev_close,
            open=float(today.get("open") or 0.0),
            high=float(today.get("high") or 0.0),
            low=float(today.get("low") or 0.0),
            volume=float(today.get("volume") or 0.0),
            turnover=float(today.get("amount") or 0.0),
            trade_status=status,
        )

    def _trade_status(self, symbol: str, last_bar: dict) -> tuple[str, float]:
        """Derive tradability from bar freshness. Never returns "normal"
        without evidence — see the module docstring.

        `strategy.py` turns anything but "normal" into `tradable=False` and a
        refusal to buy, so each branch here is a real trading decision.
        """
        if not is_market_open(market_of(symbol)):
            # Outside the session nothing is buyable anyway (the market-hours
            # gate sees to that); saying so plainly beats implying a live book.
            return "closed", 0.0
        raw_ts = last_bar.get("time") or 0
        if not raw_ts:
            return "unknown", 0.0
        age = (datetime.now(timezone.utc)
               - datetime.fromtimestamp(raw_ts / 1000.0, timezone.utc)).total_seconds()
        if age > self.STALE_MINUTES * 60:
            # Could be a halt, could be a counter nobody is trading. We cannot
            # tell them apart from bars, and both are reasons not to buy.
            return "stale", age
        return "normal", age

    # ------------------------------------------------------------------
    # Reporting
    # ------------------------------------------------------------------

    def coverage(self) -> dict:
        return {
            "source": self.name,
            "symbols": list(self.symbols),
            "cached": len(self._cache),
            "last_error": self.last_error,
        }
