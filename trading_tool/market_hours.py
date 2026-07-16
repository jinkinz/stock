"""
Market trading-hours awareness.

Regular cash sessions, local exchange time (no holidays — on an exchange
holiday quotes simply stay frozen and no signals fire, which is safe):

  US  09:30–16:00  America/New_York        (NYSE / Nasdaq)
  HK  09:30–12:00, 13:00–16:00  Asia/Hong_Kong   (HKEX, lunch break)
  SG  09:00–12:00, 13:00–17:00  Asia/Singapore   (SGX, lunch break)

The gate only applies when Longbridge is connected (real prices). In pure
sim mode prices are fabricated 24/7, so blocking on market hours would just
make the simulator untestable at night.
"""
from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

# market -> (timezone, [((open_h, open_m), (close_h, close_m)), ...])
MARKET_SESSIONS: dict[str, tuple[str, list[tuple[tuple[int, int], tuple[int, int]]]]] = {
    "US": ("America/New_York", [((9, 30), (16, 0))]),
    "HK": ("Asia/Hong_Kong", [((9, 30), (12, 0)), ((13, 0), (16, 0))]),
    "SG": ("Asia/Singapore", [((9, 0), (12, 0)), ((13, 0), (17, 0))]),
}


def is_market_open(market: str, now: datetime | None = None) -> bool:
    """True if the market's regular cash session is in progress right now.
    Unknown markets return True (never block what we don't understand)."""
    info = MARKET_SESSIONS.get(market.upper())
    if info is None:
        return True
    tz_name, sessions = info
    local = (now or datetime.now(timezone.utc)).astimezone(ZoneInfo(tz_name))
    if local.weekday() >= 5:   # Saturday / Sunday
        return False
    t = (local.hour, local.minute)
    return any(start <= t < end for start, end in sessions)


def market_of(symbol: str) -> str:
    """Market code from a symbol suffix: 'AAPL.US' -> 'US'."""
    _, _, suffix = symbol.rpartition(".")
    return suffix.upper()


def open_markets(markets: list[str], now: datetime | None = None) -> list[str]:
    return [m for m in markets if is_market_open(m, now)]


def markets_status(markets: list[str], now: datetime | None = None) -> dict[str, bool]:
    return {m: is_market_open(m, now) for m in markets}
