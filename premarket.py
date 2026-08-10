"""
Pre-session watchlist — decide what to watch before trading starts.

TWO screens, because the right metric depends on the holding horizon:

  intraday -> pre-market GAPPERS. A stock gapping 6% on real pre-open volume
              has something happening to it today, which is the whole
              timeframe an intraday position lives in.
  swing    -> 20-day LEADERS. A one-session gap is noise at a multi-day scale
              and gaps frequently fade; holding one for ten days means eating
              the fade. What matters is which names have been strongest over
              the window the strategy actually measures on.

Selecting candidates on a one-day event and handing them to a strategy whose
structure and momentum factors span twenty days is a horizon mismatch — the
same class of error as computing EMA9 on 1-minute bars and holding for a week.

The default universe is ranked by *yesterday's* turnover, which answers "what
was liquid" rather than "what is likely to move today". Pre-market trading
answers the second question directly: a stock gapping 6% on real pre-open
volume has something happening to it.

So before the open, rank the universe by its pre-market gap and pre-market
turnover, and scan that instead. This is the "pre-market architect" half of the
common two-phase design: think while the market is shut, then execute
mechanically once it opens.

What this deliberately does NOT do
──────────────────────────────────
The usual version of this idea asks an LLM to pick the "catalyst-driven"
gappers from the list. Longbridge exposes no news, earnings or calendar API
(see NEXT_SPEC), so a model shown only price and volume cannot distinguish a
catalyst from noise — it can only produce a confident-sounding guess. The
ranking below uses the two things actually measurable: how far it gapped, and
whether real money traded into that gap.

Pure functions, no I/O — `tests/test_premarket.py`.
"""
from __future__ import annotations

from dataclasses import dataclass

# A gap on almost no volume is a quote artefact, not a move. Pre-market books
# are thin, so this floor is far below the regular-session liquidity gate.
MIN_PRE_MARKET_TURNOVER = 50_000.0

# Ignore noise-level gaps; they rank above genuine movers only by accident.
MIN_ABS_GAP_PCT = 1.0


@dataclass
class Gapper:
    symbol: str
    gap_pct: float
    turnover: float
    price: float
    score: float

    def as_dict(self) -> dict:
        return {"symbol": self.symbol, "gap_pct": round(self.gap_pct, 3),
                "turnover": round(self.turnover, 2), "price": round(self.price, 4),
                "score": round(self.score, 4)}


def rank_gappers(quotes, limit: int = 20, min_turnover: float = MIN_PRE_MARKET_TURNOVER,
                 min_gap_pct: float = MIN_ABS_GAP_PCT,
                 longs_only: bool = True) -> list[Gapper]:
    """Rank symbols by pre-market activity, strongest first.

    `longs_only` keeps gap-UPS only, because the engine cannot short — ranking
    a symbol that has collapsed 9% would fill the watchlist with names it is
    structurally unable to act on.

    Score is the gap size weighted by how much money actually traded into it,
    so a 3% gap on real volume outranks an 8% gap on a handful of shares.
    """
    candidates: list[Gapper] = []
    for quote in quotes:
        gap = getattr(quote, "pre_market_change_pct", 0.0) or 0.0
        turnover = getattr(quote, "pre_market_turnover", 0.0) or 0.0
        price = getattr(quote, "pre_market_price", 0.0) or 0.0
        if price <= 0 or turnover < min_turnover:
            continue
        if abs(gap) < min_gap_pct:
            continue
        if longs_only and gap <= 0:
            continue
        # log-scaled turnover so a mega-cap's volume does not simply dominate
        # every ranking regardless of whether it actually gapped.
        weight = 1.0 + (turnover / min_turnover) ** 0.25
        candidates.append(Gapper(symbol=quote.symbol, gap_pct=gap, turnover=turnover,
                                 price=price, score=abs(gap) * weight))
    candidates.sort(key=lambda g: g.score, reverse=True)
    return candidates[:limit] if limit > 0 else candidates


def watchlist_symbols(quotes, limit: int = 20, **kwargs) -> list[str]:
    return [g.symbol for g in rank_gappers(quotes, limit=limit, **kwargs)]


# A leader screen needs enough history to have a 20-day view at all.
MIN_LEADER_BARS = 21
# Ignore names that are up over the window but sitting in the bottom half of
# their own range — that is a fading move, not leadership.
MIN_RANGE_POSITION = 0.5


@dataclass
class Leader:
    symbol: str
    change_pct: float
    range_pos: float
    price: float
    score: float

    def as_dict(self) -> dict:
        return {"symbol": self.symbol, "gap_pct": round(self.change_pct, 3),
                "range_pos": round(self.range_pos, 3), "price": round(self.price, 4),
                "turnover": 0.0, "score": round(self.score, 4)}


def rank_momentum_leaders(indicators_by_symbol: dict, limit: int = 20,
                          min_change_pct: float = 1.0) -> list[Leader]:
    """Rank symbols by multi-day strength — the swing counterpart to gappers.

    Input is {symbol: (price, indicators)} where indicators come from
    `strategy.compute_indicators` on DAILY candles, so `change_lookback_pct`
    and the range are already measured over the same window the swing signal
    engine uses.

    Score rewards being both up over the window AND near the top of its own
    range: a name up 12% but sitting at the bottom of its range has already
    given the move back.
    """
    leaders: list[Leader] = []
    for symbol, (price, ind) in indicators_by_symbol.items():
        if not ind or price <= 0:
            continue
        change = ind.get("change_lookback_pct", 0.0) or 0.0
        high, low = ind.get("range_high", 0.0), ind.get("range_low", 0.0)
        if change < min_change_pct or high <= low:
            continue
        range_pos = max(0.0, min(1.0, (price - low) / (high - low)))
        if range_pos < MIN_RANGE_POSITION:
            continue
        leaders.append(Leader(symbol=symbol, change_pct=change, range_pos=range_pos,
                              price=price, score=change * (0.5 + range_pos)))
    leaders.sort(key=lambda l: l.score, reverse=True)
    return leaders[:limit] if limit > 0 else leaders


def has_premarket_data(quotes) -> bool:
    """True when any quote carries a usable pre-market print.

    Outside a pre-open session — and in markets without one — every field is
    0.0, and the caller must fall back to the normal universe rather than
    scanning an empty watchlist.
    """
    return any((getattr(q, "pre_market_price", 0.0) or 0.0) > 0 for q in quotes)
