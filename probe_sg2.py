"""
probe_sg2.py — READ-ONLY follow-up probe.

Confirms the Longbridge ORDER path accepts SG symbols, using the correct
estimate_max_purchase_quantity signature (order_type, side, price).

Submits NO orders. Safe with the market closed.

Usage (from inside trading_tool/, same place you ran the first probe):
    python3 probe_sg2.py
"""
from __future__ import annotations

import os
import sys
from decimal import Decimal

# ── .env load (same fallback as before) ───────────────────────────────
try:
    from trading_tool.broker import _load_dotenv
    _load_dotenv()
    print("✓ .env loaded via trading_tool.broker")
except Exception:
    for path in (".env", "trading_tool/.env"):
        if os.path.exists(path):
            for line in open(path):
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, _, v = line.partition("=")
                    os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
            print(f"✓ .env loaded from {path}")
            break

try:
    from longbridge.openapi import Config, OrderSide, OrderType, QuoteContext, TradeContext
except ImportError:
    sys.exit("✗ Longbridge SDK not installed. Run: pip install longbridge")

config = Config.from_apikey_env()
qc = QuoteContext(config)
tc = TradeContext(config)
print("✓ Contexts created\n")


def attempt(label, fn):
    try:
        print(f"  ✓ {label}\n    → {fn()!r}"[:700])
        return True
    except Exception as exc:
        print(f"  ✗ {label}\n    → {type(exc).__name__}: {exc}")
        return False


# ── 1. Pull real lot sizes + a sane reference price per symbol ────────
# We need a price for the estimate call. Longbridge gives no live SG quote,
# so derive one from static info where possible, else use a placeholder well
# away from market. The exact number does not matter for this test.
print("─" * 66)
print("1. LOT SIZES (authoritative — use these, don't hardcode 100)")
print("─" * 66)

SG = ["D05.SG", "O39.SG", "U11.SG", "OV8.SG", "AJBU.SG"]
lot_sizes: dict[str, int] = {}
try:
    for info in qc.static_info(SG):
        lot_sizes[info.symbol] = info.lot_size
        print(f"  {info.symbol:10s} {info.name_en:22s} lot_size={info.lot_size}  {info.currency}")
except Exception as exc:
    print(f"  ✗ static_info failed: {exc}")


# ── 2. THE TEST — does the order path accept SG? ──────────────────────
print("\n" + "─" * 66)
print("2. ORDER PATH — estimate_max_purchase_quantity (READ-ONLY)")
print("─" * 66)

# Placeholder prices — deliberately rough. This call only asks "how much
# COULD I buy at this price", it places nothing.
PROBE_PRICES = {
    "D05.SG":  "45.00",
    "O39.SG":  "17.00",
    "U11.SG":  "35.00",
    "OV8.SG":   "2.90",
    "AJBU.SG":  "2.30",
    "AAPL.US": "200.00",   # control
}

ok_any = False
for symbol, price in PROBE_PRICES.items():
    ok = attempt(
        f"estimate_max_purchase_quantity({symbol} @ {price})",
        lambda s=symbol, p=price: tc.estimate_max_purchase_quantity(
            symbol=s,
            order_type=OrderType.LO,
            side=OrderSide.Buy,
            price=Decimal(p),
        ),
    )
    if ok and symbol.endswith(".SG"):
        ok_any = True

print("\n" + "─" * 66)
print("VERDICT")
print("─" * 66)
print(f"SG accepted by the ORDER path: {'YES' if ok_any else 'NO / INCONCLUSIVE'}")
print("""
  YES → Longbridge will take SG orders. You only need an external quote feed.
  TypeError / signature error → wrong args again, not a market rejection.
  "unsupported market" / "no permission" → genuinely blocked.

No orders were submitted by this script.
""")