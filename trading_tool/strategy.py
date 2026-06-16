"""
MomentumStrategy — the only strategy currently running.

What it does (plain English):
──────────────────────────────
1. Every tick, it records the latest price for each symbol into a 30-price
   rolling window.
2. It computes:
     short_avg  = average of the last 3 prices  (recent trend)
     long_avg   = average of all 30 prices       (intraday baseline)
     momentum   = short_avg / long_avg - 1       (deviation %)
3. BUY  when momentum > +0.2% AND no position held AND cash available.
4. SELL when momentum < -0.2% AND a position is held.
5. Signals are ranked by score; top 5 proposals are returned per scan.
6. A max-loss circuit-breaker stops all new buys if total P&L ≤ -max_loss.

What it does NOT do:
  - No options, no margin, no short-selling (only buys what cash covers,
    only sells what is already held).
  - No news, no fundamentals, no earnings calendar, no volume from
    real exchanges (only the diagnostics spike heuristic).
  - Needs at least 8 price observations per symbol before it will rank it.

Budget vs cash (important):
  budget       = the maximum STARTING cash for the paper account.
                 It is NOT re-applied as a per-tick cap.
  portfolio.cash = the actual spendable balance at any moment.
  The strategy buys against portfolio.cash directly, divided across
  however many buy signals exist, capped by max_trade_value per trade.
"""
from __future__ import annotations

import math
import statistics
from collections import deque
from datetime import datetime, timezone

from .broker import affordable_quantity
from .models import Diagnostics, OrderProposal, Portfolio, Quote, Settings, Side, Signal

# How many ticks a proposal stays valid in manual-approval mode before
# it is considered stale and should be ignored by the UI / auto-expiry.
PROPOSAL_TTL_SECONDS = 300   # 5 minutes


class MomentumStrategy:
    def __init__(self) -> None:
        self.history: dict[str, deque[float]] = {}
        self._tick_counts: dict[str, deque[int]] = {}
        self._current_ticks: dict[str, int] = {}

    # ------------------------------------------------------------------
    # Observe
    # ------------------------------------------------------------------

    def observe(self, quote: Quote) -> None:
        if quote.price <= 0:
            # Skip invalid quotes (e.g. halted/delisted symbols returning 0)
            # so they never enter the rolling price history and cause
            # divide-by-zero downstream.
            return
        prices = self.history.setdefault(quote.symbol, deque(maxlen=30))
        prices.append(quote.price)
        self._current_ticks[quote.symbol] = self._current_ticks.get(quote.symbol, 0) + 1

    def flush_tick_counts(self) -> None:
        for symbol, count in self._current_ticks.items():
            self._tick_counts.setdefault(symbol, deque(maxlen=10)).append(count)
        self._current_ticks = {}

    # ------------------------------------------------------------------
    # Diagnostics
    # ------------------------------------------------------------------

    def _diagnostics(self, quote: Quote) -> Diagnostics:
        prices = list(self.history.get(quote.symbol, []))

        volatility = 0.0
        if len(prices) >= 4:
            returns = [math.log(prices[i] / prices[i - 1])
                       for i in range(1, len(prices)) if prices[i - 1] > 0]
            if len(returns) >= 2:
                vol_per_tick = statistics.stdev(returns)
                volatility = round(vol_per_tick * math.sqrt(252 * 390) * 100, 2)

        spread_pct = round(0.001 + max(0.0, volatility / 10000), 4)

        tick_windows = list(self._tick_counts.get(quote.symbol, []))
        volume_spike = False
        if tick_windows:
            avg_ticks = sum(tick_windows) / len(tick_windows)
            current = self._current_ticks.get(quote.symbol, 0)
            volume_spike = avg_ticks > 0 and current > avg_ticks * 2

        trend_strength = 0.0
        if len(prices) >= 8:
            short_avg = sum(prices[-3:]) / 3
            long_avg = sum(prices) / len(prices)
            if long_avg > 0:
                trend_strength = round(abs(short_avg / long_avg - 1.0) * 100, 3)

        return Diagnostics(
            symbol=quote.symbol,
            price=quote.price,
            volatility=volatility,
            spread_pct=spread_pct,
            volume_spike=volume_spike,
            trend_strength=trend_strength,
            news_gate=True,   # stub — wire up a real news feed here
        )

    # ------------------------------------------------------------------
    # Multi-symbol scan  (main entry point called every tick)
    # ------------------------------------------------------------------

    def scan(
        self,
        settings: Settings,
        quotes: list[Quote],
        portfolio: Portfolio,
    ) -> tuple[list[Signal], list[OrderProposal]]:
        for quote in quotes:
            self.observe(quote)
        self.flush_tick_counts()

        signals = [s for quote in quotes if (s := self._signal(quote, portfolio)) is not None]
        signals.sort(key=lambda s: s.score, reverse=True)

        # Circuit-breaker: no new buys if we've lost too much
        total_pnl = portfolio.realized_pnl + portfolio.unrealized_pnl()
        if total_pnl <= -settings.max_loss:
            return signals[:12], []

        proposals: list[OrderProposal] = []
        reserved_cash = 0.0
        available_cash = portfolio.cash      # trade against ACTUAL cash, not budget cap

        for signal in signals:
            if signal.diagnostics and not signal.diagnostics.news_gate:
                continue    # news gate blocked

            position = portfolio.positions.get(signal.symbol)
            held_quantity = position.quantity if position else 0.0

            # ── SELL ──────────────────────────────────────────────────
            # Only sell what we actually hold. Never short, never margin.
            if signal.action == "sell" and held_quantity > 0:
                proposals.append(OrderProposal(
                    symbol=signal.symbol,
                    side=Side.SELL,
                    quantity=held_quantity,          # sell full position
                    price=signal.price,
                    confidence=signal.score,
                    reason=signal.reason,
                ))
                continue

            # ── BUY ───────────────────────────────────────────────────
            # Only buy with cash on hand. Never borrow, never options.
            if signal.action != "buy" or held_quantity > 0:
                continue

            spendable = max(0.0, available_cash - reserved_cash)
            if spendable <= 0:
                break   # no cash left this cycle

            quantity = affordable_quantity(signal.price, settings.max_trade_value, spendable)
            if quantity <= 0:
                continue

            reserved_cash += quantity * signal.price
            proposals.append(OrderProposal(
                symbol=signal.symbol,
                side=Side.BUY,
                quantity=quantity,
                price=signal.price,
                confidence=signal.score,
                reason=signal.reason,
            ))

        return signals[:12], proposals[:5]

    # ------------------------------------------------------------------
    # Per-symbol signal scoring
    # ------------------------------------------------------------------

    def _signal(self, quote: Quote, portfolio: Portfolio) -> Signal | None:
        prices = self.history.setdefault(quote.symbol, deque(maxlen=30))
        diag = self._diagnostics(quote)

        if len(prices) < 8:
            return Signal(
                symbol=quote.symbol, price=quote.price, score=0.0, action="watch",
                reason="Collecting price history — needs 8 ticks before ranking.",
                diagnostics=diag,
            )

        short_avg = sum(list(prices)[-3:]) / 3
        long_avg = sum(prices) / len(prices)
        if long_avg <= 0:
            # Symbol has zero/invalid prices (halted, delisted, no recent trades) —
            # skip ranking it rather than dividing by zero.
            return Signal(
                symbol=quote.symbol, price=quote.price, score=0.0, action="watch",
                reason="Invalid price data (zero average) — skipping.",
                diagnostics=diag,
            )
        momentum = short_avg / long_avg - 1.0
        position = portfolio.positions.get(quote.symbol)
        held_quantity = position.quantity if position else 0.0

        if held_quantity > 0 and momentum < -0.002:
            return Signal(
                symbol=quote.symbol, price=quote.price,
                score=min(0.95, 0.55 + abs(momentum) * 20),
                action="sell",
                reason=(
                    "Momentum fell below intraday baseline — exit to protect P&L."
                    + (f" Volatility {diag.volatility:.1f}%." if diag.volatility else "")
                ),
                diagnostics=diag,
            )

        if held_quantity == 0 and momentum > 0.002:
            return Signal(
                symbol=quote.symbol, price=quote.price,
                score=min(0.95, 0.55 + momentum * 20),
                action="buy",
                reason=(
                    "Short-term momentum above intraday baseline."
                    + (f" Trend {diag.trend_strength:.2f}%." if diag.trend_strength else "")
                    + (" Volume spike." if diag.volume_spike else "")
                ),
                diagnostics=diag,
            )

        return Signal(
            symbol=quote.symbol, price=quote.price,
            score=max(0.0, min(0.5, 0.25 + momentum * 10)),
            action="watch",
            reason="Momentum within neutral range — holding off.",
            diagnostics=diag,
        )