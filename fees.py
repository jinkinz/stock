"""
Brokerage fee model.

Paper fills used to cost a flat $1, which is a made-up round number. Real
Longbridge charges are a stack of components with different bases — a flat
platform fee, percentage-of-notional exchange fees, per-share regulatory
levies, some charged only on sells — and they do not resemble $1. Since the
whole point of the metrics layer is to answer "how much of the gross is going
to fees", modelling them wrong makes the answer worthless.

Where the numbers come from
───────────────────────────
`SG` is MEASURED. It was derived from real `charge_detail` on filled orders in
this account and reproduces every one of them to the cent (see
`tests/test_fees.py`, which asserts against the actual figures).

`US` and `HK` are UNVERIFIED ESTIMATES — no filled orders existed in those
markets to measure. They are deliberately set on the expensive side, because
understating fees overstates P&L, which is the dangerous direction. Anything
carrying `verified=False` should be treated as a placeholder.

Fixing that is a command, not a guess:

    python3 calibrate_fees.py

It reads real charges from your own order history and prints the schedule that
actually applies, so these tables can be corrected with evidence.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class FeeComponent:
    """One line item on a contract note.

    The charge is `flat + rate × notional + per_share × quantity`, clamped to
    [minimum, maximum], then rounded to cents — brokers bill per component, so
    rounding per component (not on the total) is what reproduces real notes.
    """
    name: str
    code: str = ""
    flat: float = 0.0
    rate: float = 0.0            # fraction of notional
    per_share: float = 0.0
    minimum: float = 0.0
    maximum: float = 0.0         # 0 = uncapped
    sell_only: bool = False
    taxable: bool = False        # feeds the sales-tax base (GST/VAT)

    def applies_to(self, side: str) -> bool:
        return not self.sell_only or side == "sell"

    def charge(self, quantity: float, price: float) -> float:
        notional = quantity * price
        amount = self.flat + self.rate * notional + self.per_share * quantity
        if amount <= 0 and self.minimum <= 0:
            return 0.0
        if self.minimum:
            amount = max(amount, self.minimum)
        if self.maximum:
            amount = min(amount, self.maximum)
        return round(amount, 2)


@dataclass
class FeeSchedule:
    market: str
    currency: str
    components: list[FeeComponent] = field(default_factory=list)
    tax_rate: float = 0.0        # applied to components marked taxable
    tax_name: str = "Tax"
    verified: bool = False       # True only when measured from real charges
    note: str = ""

    def compute(self, side: str, quantity: float, price: float) -> tuple[float, dict[str, float]]:
        """(total fee, {component name: amount}) for one fill."""
        if quantity <= 0 or price <= 0:
            return 0.0, {}
        breakdown: dict[str, float] = {}
        taxable_base = 0.0
        for component in self.components:
            if not component.applies_to(side):
                continue
            amount = component.charge(quantity, price)
            if amount <= 0:
                continue
            breakdown[component.name] = amount
            if component.taxable:
                taxable_base += amount
        if self.tax_rate and taxable_base:
            tax = round(taxable_base * self.tax_rate, 2)
            if tax > 0:
                breakdown[self.tax_name] = tax
        return round(sum(breakdown.values()), 2), breakdown


# ---------------------------------------------------------------------------
# Schedules
# ---------------------------------------------------------------------------

# MEASURED from real filled orders in this account. Verified against four SGX
# buys; the model reproduces each contract note exactly.
#   Commission     0.00       (commission-free)
#   Platform fee   0.99 flat  (charged on some orders, not others — modelled as
#                              always charged, which is the safe direction)
#   Clearing fee   0.0325% of notional   (SGX CDP)
#   Trading fee    0.0075% of notional   (SGX)
#   GST            9% of (platform + clearing)
SG_SCHEDULE = FeeSchedule(
    market="SG", currency="SGD", verified=True,
    tax_rate=0.09, tax_name="GST",
    note="Measured from real charge_detail on 4 filled SGX orders.",
    components=[
        FeeComponent("Commission", code="Commission", rate=0.0, taxable=True),
        FeeComponent("Platform Fee", code="PlatformFee", flat=0.99, taxable=True),
        FeeComponent("Clearing Fee", code="ClearingFee", rate=0.000325, taxable=True),
        FeeComponent("Trading Fee", code="TradingFee", rate=0.000075),
    ],
)

# UNVERIFIED — no US fills existed to measure. Set deliberately on the
# expensive side so paper P&L is pessimistic rather than flattering. Run
# calibrate_fees after the first real US trade to replace this with truth.
US_SCHEDULE = FeeSchedule(
    market="US", currency="USD", verified=False,
    note="ESTIMATE — not measured. Run calibrate_fees after a real US fill.",
    components=[
        FeeComponent("Commission", code="Commission", per_share=0.0099, minimum=1.00),
        FeeComponent("Platform Fee", code="PlatformFee", per_share=0.005, minimum=1.00),
        # Regulatory, sell side only, small but real.
        FeeComponent("SEC Fee", code="SecFee", rate=0.0000278, sell_only=True),
        FeeComponent("Trading Activity Fee", code="TafFee",
                     per_share=0.000166, maximum=8.30, sell_only=True),
    ],
)

# UNVERIFIED — and HK live orders are blocked by the live guard anyway. Present
# so paper-mode HK scanning does not silently trade for free.
HK_SCHEDULE = FeeSchedule(
    market="HK", currency="HKD", verified=False,
    note="ESTIMATE — not measured. HK live orders are blocked; paper only.",
    components=[
        FeeComponent("Commission", code="Commission", rate=0.0003, minimum=3.00),
        FeeComponent("Platform Fee", code="PlatformFee", flat=15.0),
        FeeComponent("Stamp Duty", code="StampDuty", rate=0.001),
        FeeComponent("SFC Levy", code="SfcLevy", rate=0.000027),
        FeeComponent("FRC Levy", code="FrcLevy", rate=0.0000015),
        FeeComponent("Exchange Fee", code="ExchangeFee", rate=0.0000565),
        FeeComponent("Settlement Fee", code="SettlementFee",
                     rate=0.00002, minimum=2.0, maximum=100.0),
    ],
)

SCHEDULES: dict[str, FeeSchedule] = {
    "SG": SG_SCHEDULE,
    "US": US_SCHEDULE,
    "HK": HK_SCHEDULE,
}

# Markets with no schedule at all fall back to this, so an unknown market can
# never trade for free in paper and quietly overstate returns.
FALLBACK_FLAT_FEE = 2.0


def schedule_for(market: str) -> FeeSchedule | None:
    return SCHEDULES.get((market or "").upper())


def estimate_fee(market: str, side: str, quantity: float, price: float) -> float:
    """Total fee for one fill, in the market's own currency."""
    return estimate_fee_detail(market, side, quantity, price)[0]


def estimate_fee_detail(market: str, side: str, quantity: float,
                        price: float) -> tuple[float, dict[str, float]]:
    schedule = schedule_for(market)
    if schedule is None:
        return (FALLBACK_FLAT_FEE, {"Estimated fee": FALLBACK_FLAT_FEE}) if quantity > 0 and price > 0 else (0.0, {})
    return schedule.compute((side or "").lower(), quantity, price)


def unverified_markets() -> list[str]:
    """Markets whose fees are guesses. Surfaced in the startup banner."""
    return sorted(m for m, s in SCHEDULES.items() if not s.verified)


# ---------------------------------------------------------------------------
# Trade viability — can this trade clear its own costs?
# ---------------------------------------------------------------------------
#
# Brokerage cost is dominated by FLAT minimums, so cost as a percentage of the
# position falls as the position grows. Below a certain size a trade cannot pay
# for itself: you hit your profit target, you sell, and you are still down. No
# strategy, signal or model fixes that — it is arithmetic, and it has to be
# checked before the order goes out rather than discovered in the P&L.
#
# Nothing here is hard-coded to any particular budget. Every figure is derived
# from the caller's own position size, price, target and fee schedule.

# Only used to illustrate cost at the SETTINGS level, when no specific symbol
# is in hand. Per-trade checks always use the real fill price.
REFERENCE_PRICE = 25.0

# Upper bound for the "what size would work?" search.
_MAX_SEARCH_NOTIONAL = 10_000_000.0


@dataclass
class TradeViability:
    """Whether a position of this size can profitably reach its target."""
    market: str
    notional: float
    price: float
    fee_pct: float               # round-trip brokerage, % of notional
    slippage_pct: float          # round-trip slippage, % of notional
    breakeven_pct: float         # gross move needed just to break even
    target_pct: float            # configured profit target (0 = none set)
    net_edge_pct: float          # what a winning trade actually nets
    viable: bool
    assessable: bool             # False when no profit target is configured
    min_viable_notional: float   # smallest position that would work; 0 = none
    reason: str

    def as_dict(self) -> dict:
        return {
            "market": self.market,
            "notional": round(self.notional, 2),
            "fee_pct": round(self.fee_pct, 4),
            "slippage_pct": round(self.slippage_pct, 4),
            "breakeven_pct": round(self.breakeven_pct, 4),
            "target_pct": round(self.target_pct, 4),
            "net_edge_pct": round(self.net_edge_pct, 4),
            "viable": self.viable,
            "assessable": self.assessable,
            "min_viable_notional": round(self.min_viable_notional, 2),
            "reason": self.reason,
        }


def round_trip_fee_pct(market: str, notional: float, price: float) -> float:
    """Buy + sell brokerage cost as a percentage of the position."""
    if notional <= 0 or price <= 0:
        return 0.0
    quantity = notional / price
    total = (estimate_fee(market, "buy", quantity, price)
             + estimate_fee(market, "sell", quantity, price))
    return total / notional * 100.0


def breakeven_pct(market: str, notional: float, price: float,
                  slippage_bps: float = 0.0) -> float:
    """Gross move required just to get the position back to flat.

    Slippage is counted on both legs — you buy above the quote and sell below
    it — so the round-trip cost is twice the one-way figure.
    """
    return round_trip_fee_pct(market, notional, price) + (slippage_bps * 2.0) / 100.0


def min_viable_notional(market: str, price: float, target_pct: float,
                        slippage_bps: float = 0.0) -> float:
    """Smallest position whose target clears its own costs. 0.0 if none does.

    Cost falls as the position grows (flat fees amortise), so a binary search
    converges. The fall is monotonic only up to cent-rounding — once the flat
    minimums are cleared the curve flattens onto the per-share and percentage
    components and wobbles by ~0.0001pp — which is far below any target worth
    trading, so the search result is exact for practical purposes.

    When the purely percentage-based components already exceed the target, no
    size works and this returns 0.0.
    """
    if target_pct <= 0 or price <= 0:
        return 0.0
    if breakeven_pct(market, _MAX_SEARCH_NOTIONAL, price, slippage_bps) >= target_pct:
        return 0.0      # even an enormous position cannot clear the target
    low, high = price, _MAX_SEARCH_NOTIONAL
    if breakeven_pct(market, low, price, slippage_bps) < target_pct:
        return low      # even a single share works
    for _ in range(60):
        mid = (low + high) / 2
        if breakeven_pct(market, mid, price, slippage_bps) < target_pct:
            high = mid
        else:
            low = mid
    return round(high, 2)


def assess_trade(market: str, notional: float, price: float, target_pct: float,
                 slippage_bps: float = 0.0) -> TradeViability:
    """Full viability verdict for one position."""
    fee_pct = round_trip_fee_pct(market, notional, price)
    slip_pct = (slippage_bps * 2.0) / 100.0
    breakeven = fee_pct + slip_pct
    net_edge = target_pct - breakeven

    if target_pct <= 0:
        return TradeViability(
            market=market, notional=notional, price=price, fee_pct=fee_pct,
            slippage_pct=slip_pct, breakeven_pct=breakeven, target_pct=0.0,
            net_edge_pct=0.0, viable=True, assessable=False,
            min_viable_notional=0.0,
            reason=(f"No profit target set, so viability cannot be judged. This "
                    f"position must still clear {breakeven:.2f}% just to break even."),
        )

    floor = min_viable_notional(market, price, target_pct, slippage_bps)
    viable = net_edge > 0
    if viable:
        reason = (f"Target {target_pct:.2f}% clears {breakeven:.2f}% of costs — "
                  f"a winning trade nets {net_edge:+.2f}%.")
    elif floor > 0:
        reason = (f"Costs {breakeven:.2f}% exceed the {target_pct:.2f}% target — a "
                  f"winning trade still loses {net_edge:.2f}%. Raise the position to "
                  f"~{floor:,.0f} or lift the target above {breakeven:.2f}%.")
    else:
        reason = (f"Costs {breakeven:.2f}% exceed the {target_pct:.2f}% target at every "
                  f"position size. Raise the target above {breakeven:.2f}% — no amount "
                  f"of capital fixes this one.")

    return TradeViability(
        market=market, notional=notional, price=price, fee_pct=fee_pct,
        slippage_pct=slip_pct, breakeven_pct=breakeven, target_pct=target_pct,
        net_edge_pct=net_edge, viable=viable, assessable=True,
        min_viable_notional=floor, reason=reason,
    )
