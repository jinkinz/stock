"""
Check the modelled fee schedule against what Longbridge actually billed.

`fees.py` carries a schedule per market. SG was derived from real contract
notes; US and HK are estimates carrying `verified=False`. Rather than trusting
either, this reads the real `charge_detail` from your own filled orders and
compares it, line by line, with what the model predicts for the same fill.

    python3 calibrate_fees.py
    python3 calibrate_fees.py --days 90

Read-only: it places no orders and writes no files. Its output is evidence for
correcting `fees.py` by hand — deliberately not automatic, because a schedule
silently rewritten from three orders is how you end up with a confident wrong
number.
"""
from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from fees import estimate_fee_detail, schedule_for
from market_hours import market_of


def _num(value, default: float = 0.0) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return default


def collect(days: int) -> list[dict]:
    """Filled orders with their real charges."""
    from longbridge.openapi import Config, TradeContext
    import broker  # noqa: F401  — imported for its .env loading

    ctx = TradeContext(Config.from_apikey_env())
    orders = ctx.history_orders(
        start_at=datetime.now(timezone.utc) - timedelta(days=days),
        end_at=datetime.now(timezone.utc),
    ) or []

    fills: list[dict] = []
    for order in orders:
        if "Filled" not in str(getattr(order, "status", "")) or "Partial" in str(getattr(order, "status", "")):
            continue
        try:
            detail = ctx.order_detail(order.order_id)
        except Exception as exc:
            print(f"  could not read {getattr(order, 'order_id', '?')}: {exc}")
            continue
        quantity = _num(getattr(detail, "executed_quantity", 0))
        price = _num(getattr(detail, "executed_price", 0))
        if quantity <= 0 or price <= 0:
            continue
        charge = getattr(detail, "charge_detail", None)
        items: dict[str, float] = {}
        for item in getattr(charge, "items", []) or []:
            for fee in getattr(item, "fees", []) or []:
                name = str(getattr(fee, "name", "") or getattr(fee, "code", ""))
                items[name] = items.get(name, 0.0) + _num(getattr(fee, "amount", 0))
        fills.append({
            "symbol": detail.symbol,
            "side": "sell" if "Sell" in str(getattr(detail, "side", "")) else "buy",
            "quantity": quantity,
            "price": price,
            "notional": quantity * price,
            "currency": str(getattr(charge, "currency", "") or getattr(detail, "currency", "")),
            "total": _num(getattr(charge, "total_amount", 0)) or round(sum(items.values()), 4),
            "items": items,
        })
    return fills


def report(fills: list[dict]) -> int:
    if not fills:
        print("\nNo filled orders in this window — nothing to calibrate against.")
        print("The estimates in fees.py stay unverified until you trade for real.")
        return 1

    by_market: dict[str, list[dict]] = defaultdict(list)
    for fill in fills:
        by_market[market_of(fill["symbol"])].append(fill)

    worst_gap = 0.0
    under_charged = False
    for market, market_fills in sorted(by_market.items()):
        schedule = schedule_for(market)
        label = "MEASURED" if schedule and schedule.verified else "ESTIMATE"
        print(f"\n{'─' * 70}\n{market}  ({len(market_fills)} fills)   model: {label}\n{'─' * 70}")
        for fill in market_fills:
            modelled, breakdown = estimate_fee_detail(
                market, fill["side"], fill["quantity"], fill["price"])
            actual = fill["total"]
            gap = modelled - actual
            worst_gap = max(worst_gap, abs(gap))
            if gap < -0.01:
                under_charged = True
            flag = "ok" if abs(gap) < 0.01 else ("over (safe)" if gap > 0 else "UNDER-CHARGING")
            print(f"\n  {fill['symbol']:<10} {fill['side']:<4} "
                  f"{fill['quantity']:g} @ {fill['price']:g} = {fill['notional']:.2f} {fill['currency']}")
            print(f"    actual   {actual:>8.4f}   ({actual / fill['notional'] * 100:.4f}% of notional)")
            print(f"    modelled {modelled:>8.4f}   diff {gap:+.4f}  [{flag}]")
            names = sorted(set(fill["items"]) | set(breakdown))
            for name in names:
                a, m = fill["items"].get(name, 0.0), breakdown.get(name, 0.0)
                mark = " " if abs(a - m) < 0.005 else "*"
                print(f"      {mark} {name:<24} actual {a:>8.4f}   modelled {m:>8.4f}")

    print(f"\n{'─' * 70}")
    print(f"Largest single-order gap: {worst_gap:.4f}")
    if under_charged:
        print("UNDER-CHARGING on some orders — the model bills less than the broker did.")
        print("This is the dangerous direction: it makes paper P&L look better than")
        print("reality. Fix the starred lines in fees.py before trusting")
        print("any metrics.")
    elif worst_gap >= 0.01:
        print("Over-charging only, which is the safe direction — paper P&L is")
        print("pessimistic rather than flattering. Usually a fee the broker waived")
        print("on some orders (a promo or free quota) that the model always applies.")
        print("Leave it unless you know the waiver rule.")
    else:
        print("Model matches every real charge to the cent.")
    unverified = [m for m in by_market if not (schedule_for(m) and schedule_for(m).verified)]
    if unverified:
        print(f"\nStill unverified (no confirmation from real fills): {', '.join(sorted(unverified))}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    parser.add_argument("--days", type=int, default=365,
                        help="how far back to read order history (default 365)")
    args = parser.parse_args()
    print(f"Reading filled orders from the last {args.days} days…")
    try:
        fills = collect(args.days)
    except Exception as exc:
        print(f"Could not read order history: {exc}")
        print("Check .env and whether the access token has expired.")
        return 1
    return report(fills)


if __name__ == "__main__":
    sys.exit(main())
