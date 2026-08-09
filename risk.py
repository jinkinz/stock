"""
Portfolio-level protections.

The per-trade guards (viability, ATR sizing, the live budget ceiling) each look
at one order in isolation. None of them stops twenty individually-reasonable
trades from concentrating the whole account, or from grinding the same daily
capital through fees over and over, or from doubling down through a losing
streak. These are the limits that look at the account and the day.

Four rules, each independently switchable and each off when set to 0:

  max_concurrent_positions  cap on open positions at once
  daily_budget              total capital deployable per exchange-local day
  daily_loss_limit          realised losses that halt new buys for the day
  cooldown_after_losses     N consecutive losers pauses buying for a while

A "day" is the EXCHANGE's day, not UTC — see market_hours.local_date. Resetting
a US daily limit at UTC midnight would reset it mid-session.

`daily_budget` counts capital **deployed cumulatively today**, not currently
held. Buying $250, selling, and buying again is $500 of deployment and two sets
of fees; treating that as "still $250" would miss exactly the churn that costs
the most.

Every block returns a reason string. A silent block is worse than no block —
the caller is expected to audit it.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from market_hours import local_date, market_of

# How long buying pauses once the consecutive-loss threshold trips.
COOLDOWN_MINUTES = 30
# Daily buckets older than this are dropped so the state file stays small.
KEEP_DAYS = 10


def _bucket(market: str, now: datetime | None = None) -> str:
    return f"{(market or '?').upper()}:{local_date(market, now)}"


@dataclass
class RiskState:
    """Rolling per-day counters. Persisted so limits survive a restart —
    a daily cap that resets when the process does is not a cap."""
    daily_deployed: dict[str, float] = field(default_factory=dict)
    daily_realized: dict[str, float] = field(default_factory=dict)
    consecutive_losses: int = 0
    cooldown_until: str = ""

    # ── recording ────────────────────────────────────────────────────────
    def record_buy(self, symbol: str, notional: float, now: datetime | None = None) -> None:
        key = _bucket(market_of(symbol), now)
        self.daily_deployed[key] = round(self.daily_deployed.get(key, 0.0) + notional, 4)

    def record_close(self, symbol: str, net_pnl: float, loss_streak_limit: int,
                     now: datetime | None = None) -> None:
        """Called when a round trip completes."""
        moment = now or datetime.now(timezone.utc)
        key = _bucket(market_of(symbol), moment)
        self.daily_realized[key] = round(self.daily_realized.get(key, 0.0) + net_pnl, 4)
        if net_pnl < 0:
            self.consecutive_losses += 1
            if loss_streak_limit > 0 and self.consecutive_losses >= loss_streak_limit:
                self.cooldown_until = (moment + timedelta(minutes=COOLDOWN_MINUTES)).isoformat()
                self.consecutive_losses = 0     # streak served its purpose
        else:
            self.consecutive_losses = 0

    # ── reads ────────────────────────────────────────────────────────────
    def deployed_today(self, market: str, now: datetime | None = None) -> float:
        return self.daily_deployed.get(_bucket(market, now), 0.0)

    def realized_today(self, market: str, now: datetime | None = None) -> float:
        return self.daily_realized.get(_bucket(market, now), 0.0)

    def in_cooldown(self, now: datetime | None = None) -> bool:
        if not self.cooldown_until:
            return False
        try:
            return (now or datetime.now(timezone.utc)) < datetime.fromisoformat(self.cooldown_until)
        except ValueError:
            return False

    def cooldown_remaining_minutes(self, now: datetime | None = None) -> float:
        if not self.in_cooldown(now):
            return 0.0
        remaining = datetime.fromisoformat(self.cooldown_until) - (now or datetime.now(timezone.utc))
        return round(remaining.total_seconds() / 60.0, 1)

    # ── housekeeping ─────────────────────────────────────────────────────
    def prune(self, keep_days: int = KEEP_DAYS) -> None:
        for store in (self.daily_deployed, self.daily_realized):
            if len(store) <= keep_days * 3:
                continue
            for key in sorted(store, key=lambda k: k.split(":", 1)[-1])[:-keep_days * 3]:
                store.pop(key, None)

    def to_json(self) -> dict:
        return {
            "daily_deployed": dict(self.daily_deployed),
            "daily_realized": dict(self.daily_realized),
            "consecutive_losses": self.consecutive_losses,
            "cooldown_until": self.cooldown_until,
        }

    @classmethod
    def from_json(cls, data: dict) -> "RiskState":
        if not isinstance(data, dict):
            return cls()
        return cls(
            daily_deployed={str(k): float(v) for k, v in (data.get("daily_deployed") or {}).items()},
            daily_realized={str(k): float(v) for k, v in (data.get("daily_realized") or {}).items()},
            consecutive_losses=int(data.get("consecutive_losses", 0) or 0),
            cooldown_until=str(data.get("cooldown_until", "") or ""),
        )


def check_limits(settings, open_position_count: int, state: RiskState,
                 symbol: str, notional: float,
                 now: datetime | None = None) -> str | None:
    """Reason to refuse this BUY on portfolio grounds, or None to allow it.

    Order matters only for which message the user sees first; every rule is
    independent. Exits are never checked here — trapping a position would be
    worse than any limit.
    """
    market = market_of(symbol)

    if state.in_cooldown(now):
        return (f"BLOCKED: cooling off after {settings.cooldown_after_losses} consecutive "
                f"losing trades — {state.cooldown_remaining_minutes(now):.0f} min remaining.")

    cap = settings.max_concurrent_positions
    if cap > 0 and open_position_count >= cap:
        return (f"BLOCKED: already holding {open_position_count} positions "
                f"(max {cap}). Concentration limit.")

    limit = settings.daily_loss_limit
    if limit > 0:
        realized = state.realized_today(market, now)
        if realized <= -limit:
            return (f"BLOCKED: {market} is down ${abs(realized):,.2f} today, at or past the "
                    f"${limit:,.2f} daily loss limit. No new buys until tomorrow.")

    budget = settings.daily_budget
    if budget > 0:
        deployed = state.deployed_today(market, now)
        if deployed + notional > budget:
            remaining = max(0.0, budget - deployed)
            return (f"BLOCKED: ${deployed:,.2f} of the ${budget:,.2f} {market} daily budget "
                    f"already deployed; this order needs ${notional:,.2f} and only "
                    f"${remaining:,.2f} is left today.")
    return None
