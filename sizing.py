"""
Position sizing.

The flat sizer spends the same dollars on every symbol regardless of how far
that symbol typically moves. Two positions of $1,000 in a sleepy utility and in
a biotech carry completely different risk, and a flat 2% stop is a normal
morning's noise for one and a catastrophe for the other. The result is that
losses are unequal and uncontrolled even when every rule "worked".

Risk-based sizing inverts the question. Instead of "how much do I spend?", ask
"how much am I willing to lose if the stop fires?" — then let the symbol's own
volatility (ATR) decide the quantity:

    stop_distance = atr × atr_stop_multiple
    risk_dollars  = equity × (risk_per_trade_pct / 100)
    quantity      = risk_dollars / stop_distance

A volatile symbol gets a wider stop and therefore fewer shares; a quiet one
gets more. Each position then risks the same fraction of equity.

Pure functions, no I/O and no app imports, so this is testable in isolation —
see `tests/test_sizing.py`.

Never silently size on a missing input. When ATR is unavailable the result
falls back to flat sizing and says so in `reason`; the caller can surface that
rather than quietly taking a differently-shaped risk.
"""
from __future__ import annotations

from dataclasses import dataclass

# Never put more than this share of available cash into one position, however
# the sizing maths comes out. A single conviction trade should not be able to
# consume the account.
MAX_CASH_FRACTION_PER_TRADE = 0.25

# Hard ceiling even when very few positions are allowed — one trade must never
# be able to take the whole account.
ABSOLUTE_CASH_FRACTION_CEILING = 0.50


def cash_fraction_for(max_concurrent_positions: int) -> float:
    """Share of cash a single position may take, given how many are allowed.

    A fixed 25% assumed you always wanted four-plus positions. If you only
    allow two, that fixed cap strands half the account and makes each position
    too small to clear its own costs — the concentration limit and the
    viability floor end up pulling against each other. Spread the account over
    the number of positions actually permitted instead.
    """
    if max_concurrent_positions <= 0:
        return MAX_CASH_FRACTION_PER_TRADE
    share = 1.0 / max_concurrent_positions
    return min(ABSOLUTE_CASH_FRACTION_CEILING, max(MAX_CASH_FRACTION_PER_TRADE, share))


@dataclass
class SizingResult:
    quantity: float
    notional: float
    method: str          # "atr" | "flat"
    reason: str
    stop_price: float    # absolute price; 0.0 when ATR was unavailable
    stop_distance: float

    @property
    def ok(self) -> bool:
        return self.quantity > 0


def _clamp_to_budget(quantity: float, price: float, max_trade_value: float,
                     spendable: float, cash_fraction: float = MAX_CASH_FRACTION_PER_TRADE) -> tuple[float, str]:
    """Apply the hard dollar ceilings. Returns (quantity, note)."""
    note = ""
    if max_trade_value > 0 and quantity * price > max_trade_value:
        quantity = max_trade_value / price
        note = "capped by max trade value"
    cash_cap = spendable * cash_fraction
    if quantity * price > cash_cap:
        quantity = cash_cap / price
        note = f"capped at {cash_fraction:.0%} of available cash"
    if quantity * price > spendable:
        quantity = spendable / price
        note = "capped by available cash"
    return max(0.0, quantity), note


def size_position(price: float, atr: float, equity: float, spendable: float,
                  max_trade_value: float, risk_per_trade_pct: float,
                  atr_stop_multiple: float, use_atr_sizing: bool = True,
                  max_concurrent_positions: int = 0) -> SizingResult:
    """How many shares to buy, and where the stop belongs.

    `spendable` is cash actually available after any reservations already made
    this cycle — not total equity.
    """
    if price <= 0 or spendable <= 0:
        return SizingResult(0.0, 0.0, "flat", "No price or no cash available.", 0.0, 0.0)

    use_atr = use_atr_sizing and atr > 0 and equity > 0 and risk_per_trade_pct > 0
    if use_atr:
        stop_distance = atr * atr_stop_multiple
        if stop_distance <= 0:
            use_atr = False

    if use_atr:
        risk_dollars = equity * (risk_per_trade_pct / 100.0)
        quantity = risk_dollars / stop_distance
        method = "atr"
        detail = (f"risking {risk_per_trade_pct:.2f}% of ${equity:,.0f} "
                  f"(${risk_dollars:,.2f}) over a {atr_stop_multiple:.1f}×ATR "
                  f"stop of ${stop_distance:.2f}")
        stop_price = max(0.0, price - stop_distance)
    else:
        # Fall back to the flat cap, and say so — an unstated fallback is how a
        # missing input silently becomes a differently-shaped risk.
        stop_distance = 0.0
        stop_price = 0.0
        quantity = (max_trade_value / price) if max_trade_value > 0 else (spendable / price)
        method = "flat"
        detail = ("no ATR available (candles not fetched yet) — flat sizing"
                  if use_atr_sizing else "ATR sizing disabled — flat sizing")

    quantity, cap_note = _clamp_to_budget(
        quantity, price, max_trade_value, spendable,
        cash_fraction_for(max_concurrent_positions))
    quantity = round(quantity, 6)
    reason = detail + (f"; {cap_note}" if cap_note else "")
    return SizingResult(quantity=quantity, notional=round(quantity * price, 2),
                        method=method, reason=reason,
                        stop_price=round(stop_price, 6) if quantity > 0 else 0.0,
                        stop_distance=round(stop_distance, 6))
