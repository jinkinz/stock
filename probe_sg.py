"""
probe_sg.py — READ-ONLY Longbridge capability probe for the Singapore market.

Answers one question: does the Longbridge OpenAPI recognise SG symbols on the
QUOTE side and, more importantly, on the TRADE side?

This script NEVER submits an order. Every call below is read-only. Safe to run
with the market closed — that is in fact the point, since none of these calls
depend on a live session.

Usage:
    cd /path/to/your/repo          # the dir that CONTAINS trading_tool/
    python probe_sg.py

Requires: the same .env that your app already uses (trading_tool/.env).
"""
from __future__ import annotations

import os
import sys
import traceback

# ── Reuse the app's own .env loader so credentials resolve identically ──
try:
    from trading_tool.broker import _load_dotenv
    _load_dotenv()
    print("✓ Loaded .env via trading_tool.broker._load_dotenv()")
except Exception as exc:
    print(f"⚠ Could not import trading_tool.broker ({exc}).")
    print("  Falling back to manual .env load.")
    for path in ("trading_tool/.env", ".env"):
        if os.path.exists(path):
            for line in open(path):
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, _, v = line.partition("=")
                    os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
            print(f"✓ Loaded {path}")
            break

try:
    from longbridge.openapi import Config, QuoteContext, TradeContext
except ImportError:
    sys.exit("✗ Longbridge SDK not installed. Run: pip install longbridge")


SG_SYMBOLS = ["D05.SG", "O39.SG", "U11.SG"]   # DBS, OCBC, UOB
US_CONTROL = ["AAPL.US"]                       # known-good control


def hr(title: str) -> None:
    print("\n" + "─" * 68)
    print(title)
    print("─" * 68)


def attempt(label: str, fn) -> bool:
    """Run fn(), print result or error. Returns True on success."""
    try:
        result = fn()
        print(f"  ✓ {label}")
        print(f"    → {result!r}"[:900])
        return True
    except Exception as exc:
        print(f"  ✗ {label}")
        print(f"    → {type(exc).__name__}: {exc}")
        return False


# ══════════════════════════════════════════════════════════════════════
config = Config.from_apikey_env()
qc = QuoteContext(config)
tc = TradeContext(config)
print("✓ QuoteContext + TradeContext created")


# ── 1. What methods actually exist? (removes my guesswork) ────────────
hr("1. AVAILABLE SDK METHODS")
print("QuoteContext:")
print("  " + ", ".join(m for m in dir(qc) if not m.startswith("_")))
print("\nTradeContext:")
print("  " + ", ".join(m for m in dir(tc) if not m.startswith("_")))


# ── 2. Does the QUOTE side know SG symbols as reference data? ─────────
# static_info is reference data (name, lot size, currency) — not a live
# price — so it should return even with the market closed.
hr("2. QUOTE SIDE — static/reference info")
attempt("static_info(US control)", lambda: qc.static_info(US_CONTROL))
sg_static_ok = attempt("static_info(SG symbols)", lambda: qc.static_info(SG_SYMBOLS))

# Live quote — expected to fail or return stale/zero for SG per Longbridge docs.
# Included to confirm the documented limitation rather than assume it.
attempt("quote(SG symbols)  [expected to fail per docs]",
        lambda: qc.quote(SG_SYMBOLS))


# ── 3. THE DECISIVE TEST — does the TRADE side accept SG? ─────────────
# Try several read-only trade endpoints. Any one succeeding for an SG
# symbol means the trading side recognises the market.
hr("3. TRADE SIDE — does it accept SG symbols? (THE KEY QUESTION)")

trade_probes = [
    ("estimate_max_purchase_quantity", "estimate_max_purchase_quantity"),
    ("margin_ratio",                    "margin_ratio"),
    ("order_detail",                    None),   # skipped, needs an order id
]

sg_trade_ok = False
for label, method_name in trade_probes:
    if method_name is None:
        continue
    fn = getattr(tc, method_name, None)
    if fn is None:
        print(f"  – {label}: not present in this SDK version")
        continue
    # Try the simplest possible signature first, then a fuller one.
    for symbol in ("D05.SG",):
        ok = attempt(f"{label}('{symbol}')", lambda f=fn, s=symbol: f(s))
        if ok:
            sg_trade_ok = True

# Account-level reads (no symbol) — confirms trade auth works at all.
attempt("account_balance()", lambda: tc.account_balance())
attempt("stock_positions()", lambda: tc.stock_positions())


# ── 4. Verdict ────────────────────────────────────────────────────────
hr("4. VERDICT")
print(f"SG recognised by QUOTE side (static info): {'YES' if sg_static_ok else 'NO'}")
print(f"SG recognised by TRADE side:               {'YES' if sg_trade_ok else 'NO / INCONCLUSIVE'}")
print("""
How to read this:
  • TRADE side YES  → Longbridge can likely execute SG. Your plan works:
                      source SG quotes elsewhere, execute via Longbridge.
  • TRADE side NO   → check the error text above. "unsupported market" or
                      similar = blocked. A signature/TypeError just means my
                      guessed arguments were wrong — not that SG is blocked.
                      Look at the method list from section 1 and retry.
  • Anything unclear → treat as UNCONFIRMED and wait for Longbridge support.

NOTE: this script submitted no orders. Nothing was traded.
""")