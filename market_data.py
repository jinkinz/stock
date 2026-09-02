"""Per-market routing of quotes and candles.

NOT WIRED IN. This is the seam, built and reasoned; no source sits behind it
yet (see the Tiger findings below). Nothing imports this module.

WHY THIS EXISTS
---------------
Longbridge has no SG market-data entitlement, and there is no package to buy.
Both halves of the feed are blocked, which was measured, not assumed:

    quote(["D05.SG"])        -> []                      (silently empty)
    candlesticks("D05.SG")   -> OpenApiException 301604 "no quote access"

The second one is what actually decides whether SG is tradable. A symbol with
no candles gets no indicators, the convergence gate counts a missing factor as
NOT confirmed, and the engine can therefore never buy it. So a quotes-only
second source would leave SG symbols scoring in the table and unbuyable in
practice — the worst of both worlds, because it looks like it works.

This module is the seam where another vendor supplies the data WITHOUT touching
execution. Orders keep going to the broker that proved it accepts them
(Longbridge takes SG orders — `estimate_max_purchase_quantity` succeeds for
every SG symbol tested). Keeping the two apart also protects `fees.SG_SCHEDULE`,
which is `verified=True` against real LONGBRIDGE contract notes and would be
silently wrong for anyone else's fills.

MEASURED 2026-09-03 — TIGER IS NOT (YET) THAT VENDOR
----------------------------------------------------
Tiger Brokers SG (license TBSG) was probed as the second source. It INVERTS
Longbridge rather than completing it, and at the entry tier it cannot feed
this engine. Recorded here because re-deriving it costs real quota:

    get_symbols(Market.SG)   -> 1616 symbols, ".SI" format, NOT ".SG"
    get_market_status(SG)    -> works (session times)
    get_bars("D05.SI")       -> WORKS. Day and 1min, and the columns carry
                                `amount`, which is the turnover this repo
                                needs for the liquidity gate
    get_stock_briefs         -> permission denied
    get_stock_delay_briefs   -> "only support us market symbols"
    get_timeline/trade_ticks -> permission denied

Entitlements on the account are a single entry, `aStockQuoteLv1` (China A
shares). So Tiger has SG CANDLES but no SG QUOTES; Longbridge has neither.

The wall is the K-line quota, and it is not a rate limit: it is a cap on
DISTINCT SYMBOLS per rolling 30 days, tied to account tier — 20 at "API
access activation", 200 at assets > $10K, 500 at > $50K. First request for a
symbol consumes a slot; all frequencies share it; tiers refresh Tuesdays
08:00 GMT+8. `CANDLE_SPEC` is 40 symbols/tick intraday and 150 swing, so 20
per MONTH cannot complete one scan. The 200 tier would clear swing; nothing
clears intraday.

Two consequences if this is ever revisited:
  * Symbol translation is real work, not cosmetic — ".SG" (Longbridge, and
    what this repo speaks) vs ".SI" (Tiger). Nine quota slots were burned on
    that mistake alone during the probe.
  * A candles-only feed can synthesize a price from the latest 1min close
    (fine for swing, ~1min stale), but it CANNOT supply `trade_status`. That
    collides head-on with "halted symbols are never bought" — a quote with an
    unknown status must not silently default to "normal".

Routing is PER SYMBOL, by market — never global. A universe can legitimately
hold US symbols the default source serves and SG symbols it does not, and a
global switch would either break US or leave SG dark.
"""
from __future__ import annotations

from market_hours import market_of
from models import Quote


class MarketDataRouter:
    """Dispatches market-data calls to whichever source is entitled to that
    market, then merges the results.

    Presents the same surface as a broker's data side (`quote`, `quotes`,
    `candles`, `discover_symbols`) so it can be dropped in wherever a raw
    source was used, without changing a single call site upstream.

    A source that fails is contained: its symbols are recorded as missing and
    the other sources' results still come back. One vendor's outage must not
    blank the whole scan.
    """

    def __init__(self, default, overrides: dict | None = None) -> None:
        self._default = default
        # market (upper) -> source. Only markets the default cannot serve.
        self._overrides = {str(m).upper(): s for m, s in (overrides or {}).items() if s is not None}
        # Requested-but-not-returned, from the last quotes() call. This is the
        # ONLY evidence a caller gets that symbols went missing: the underlying
        # APIs do not raise for an unentitled market, they return nothing, and
        # a silently shorter list reads as "the market is quiet".
        self.last_missing: list[str] = []
        self.last_errors: dict[str, str] = {}

    # ------------------------------------------------------------------
    # Routing
    # ------------------------------------------------------------------

    def source_for(self, symbol: str):
        return self._overrides.get(market_of(symbol).upper(), self._default)

    def overrides(self) -> dict:
        return dict(self._overrides)

    def set_override(self, market: str, source) -> None:
        """Route one market elsewhere. `source=None` removes the override and
        hands the market back to the default."""
        key = str(market).upper()
        if source is None:
            self._overrides.pop(key, None)
        else:
            self._overrides[key] = source

    def _group(self, symbols: list[str]) -> list[tuple[object, list[str]]]:
        """Split symbols by the source that serves them, preserving order.
        Grouped by identity because a source need not be hashable."""
        groups: list[tuple[object, list[str]]] = []
        for symbol in symbols:
            source = self.source_for(symbol)
            for existing, bucket in groups:
                if existing is source:
                    bucket.append(symbol)
                    break
            else:
                groups.append((source, [symbol]))
        return groups

    # ------------------------------------------------------------------
    # Data surface
    # ------------------------------------------------------------------

    def quote(self, symbol: str) -> Quote:
        return self.source_for(symbol).quote(symbol)

    def quotes(self, symbols: list[str]) -> list[Quote]:
        if not symbols:
            self.last_missing = []
            self.last_errors = {}
            return []
        collected: list[Quote] = []
        errors: dict[str, str] = {}
        for source, group in self._group(symbols):
            try:
                collected.extend(source.quotes(group) or [])
            except Exception as exc:
                # Contained on purpose. The alternative — letting it propagate —
                # means one unentitled market blanks the entire tick.
                errors[getattr(source, "name", type(source).__name__)] = str(exc)
        returned = {q.symbol for q in collected}
        self.last_missing = [s for s in symbols if s not in returned]
        self.last_errors = errors
        return collected

    def candles(self, symbol: str, period: str = "Min_1", count: int = 120) -> list[dict]:
        return self.source_for(symbol).candles(symbol, period=period, count=count)

    def discover_symbols(self, markets: list[str]) -> list[str]:
        """Discovery per market, from whichever source owns that market."""
        found: list[str] = []
        seen: set[str] = set()
        for source, group in self._group_markets(markets):
            try:
                for symbol in source.discover_symbols(group) or []:
                    if symbol not in seen:
                        seen.add(symbol)
                        found.append(symbol)
            except Exception:
                continue
        return found

    def _group_markets(self, markets: list[str]) -> list[tuple[object, list[str]]]:
        groups: list[tuple[object, list[str]]] = []
        for market in markets:
            source = self._overrides.get(str(market).upper(), self._default)
            for existing, bucket in groups:
                if existing is source:
                    bucket.append(market)
                    break
            else:
                groups.append((source, [market]))
        return groups

    # ------------------------------------------------------------------
    # Reporting
    # ------------------------------------------------------------------

    def coverage(self) -> dict:
        """What the last quote request actually returned, for the UI.

        Symbols vanishing from a scan is the failure mode this whole module
        exists around, so it is reported explicitly rather than left to be
        inferred from a smaller number.
        """
        by_market: dict[str, int] = {}
        for symbol in self.last_missing:
            key = market_of(symbol).upper() or "?"
            by_market[key] = by_market.get(key, 0) + 1
        return {
            "missing_count": len(self.last_missing),
            "missing_by_market": by_market,
            "missing_sample": self.last_missing[:10],
            "errors": dict(self.last_errors),
            "routes": {m: getattr(s, "name", type(s).__name__)
                       for m, s in self._overrides.items()},
        }
