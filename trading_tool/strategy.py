from __future__ import annotations

import math
import statistics
from collections import deque

from .broker import affordable_quantity
from .models import Diagnostics, OrderProposal, Portfolio, Quote, Settings, Side, Signal


class MomentumStrategy:
    def __init__(self) -> None:
        self.history: dict[str, deque[float]] = {}
        # Track tick counts per symbol to detect volume spikes
        self._tick_counts: dict[str, deque[int]] = {}
        self._current_ticks: dict[str, int] = {}

    # ------------------------------------------------------------------
    # Observe
    # ------------------------------------------------------------------

    def observe(self, quote: Quote) -> None:
        prices = self.history.setdefault(quote.symbol, deque(maxlen=30))
        prices.append(quote.price)
        self._current_ticks[quote.symbol] = self._current_ticks.get(quote.symbol, 0) + 1

    def flush_tick_counts(self) -> None:
        """Call once per scan cycle to bucket tick counts."""
        for symbol, count in self._current_ticks.items():
            window = self._tick_counts.setdefault(symbol, deque(maxlen=10))
            window.append(count)
        self._current_ticks = {}

    # ------------------------------------------------------------------
    # Diagnostics
    # ------------------------------------------------------------------

    def _diagnostics(self, quote: Quote) -> Diagnostics:
        prices = list(self.history.get(quote.symbol, []))

        # Volatility: annualised std-dev of log-returns (approx 252 trading days,
        # each scan ~1 min → scale by sqrt(252*390) for intraday)
        volatility = 0.0
        if len(prices) >= 4:
            returns = [math.log(prices[i] / prices[i - 1]) for i in range(1, len(prices)) if prices[i - 1] > 0]
            if len(returns) >= 2:
                vol_per_tick = statistics.stdev(returns)
                # annualise roughly (252 days × 390 ticks/day)
                volatility = round(vol_per_tick * math.sqrt(252 * 390) * 100, 2)

        # Spread estimate: 0.1% baseline + extra if high volatility
        spread_pct = round(0.001 + max(0.0, volatility / 10000), 4)

        # Volume spike: current tick count >2× average of past windows
        tick_windows = list(self._tick_counts.get(quote.symbol, []))
        volume_spike = False
        if tick_windows:
            avg_ticks = sum(tick_windows) / len(tick_windows)
            current = self._current_ticks.get(quote.symbol, 0)
            volume_spike = avg_ticks > 0 and current > avg_ticks * 2

        # Trend strength
        trend_strength = 0.0
        if len(prices) >= 8:
            short_avg = sum(prices[-3:]) / 3
            long_avg = sum(prices) / len(prices)
            trend_strength = round(abs(short_avg / long_avg - 1.0) * 100, 3)

        # News gate stub — always open unless you wire up a real news feed
        news_gate = True

        return Diagnostics(
            symbol=quote.symbol,
            price=quote.price,
            volatility=volatility,
            spread_pct=spread_pct,
            volume_spike=volume_spike,
            trend_strength=trend_strength,
            news_gate=news_gate,
        )

    # ------------------------------------------------------------------
    # Single-symbol propose (used in live tick mode)
    # ------------------------------------------------------------------

    def propose(self, settings: Settings, quote: Quote, portfolio: Portfolio) -> OrderProposal | None:
        prices = self.history.setdefault(quote.symbol, deque(maxlen=30))
        if len(prices) < 8:
            return None

        short_avg = sum(list(prices)[-3:]) / 3
        long_avg = sum(prices) / len(prices)
        position = portfolio.positions.get(quote.symbol)
        held_quantity = position.quantity if position else 0

        if portfolio.realized_pnl + portfolio.unrealized_pnl() <= -settings.max_loss:
            return None

        if short_avg > long_avg * 1.002 and held_quantity == 0:
            quantity = affordable_quantity(quote.price, settings.max_trade_value, min(settings.budget, portfolio.cash))
            if quantity <= 0:
                return None
            return OrderProposal(
                symbol=quote.symbol,
                side=Side.BUY,
                quantity=quantity,
                price=quote.price,
                confidence=min(0.85, 0.55 + abs(short_avg / long_avg - 1.0) * 20),
                reason="Short-term momentum is above the intraday baseline while no position is open.",
            )

        if held_quantity > 0 and short_avg < long_avg * 0.998:
            return OrderProposal(
                symbol=quote.symbol,
                side=Side.SELL,
                quantity=held_quantity,
                price=quote.price,
                confidence=min(0.85, 0.55 + abs(short_avg / long_avg - 1.0) * 20),
                reason="Momentum faded below the intraday baseline, so the strategy proposes exiting.",
            )

        return None

    # ------------------------------------------------------------------
    # Multi-symbol scan
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
        signals.sort(key=lambda item: item.score, reverse=True)

        if portfolio.realized_pnl + portfolio.unrealized_pnl() <= -settings.max_loss:
            return signals, []

        proposals: list[OrderProposal] = []
        reserved_cash = 0.0

        for signal in signals:
            position = portfolio.positions.get(signal.symbol)
            held_quantity = position.quantity if position else 0

            # Skip if diagnostics say news gate is closed
            if signal.diagnostics and not signal.diagnostics.news_gate:
                continue

            if signal.action == "sell" and held_quantity > 0:
                proposals.append(
                    OrderProposal(
                        symbol=signal.symbol,
                        side=Side.SELL,
                        quantity=held_quantity,
                        price=signal.price,
                        confidence=signal.score,
                        reason=signal.reason,
                    )
                )
                continue

            if signal.action != "buy" or held_quantity > 0:
                continue

            available_budget = max(0.0, min(settings.budget, portfolio.cash) - reserved_cash)
            quantity = affordable_quantity(signal.price, settings.max_trade_value, available_budget)
            if quantity <= 0:
                continue

            reserved_cash += quantity * signal.price
            proposals.append(
                OrderProposal(
                    symbol=signal.symbol,
                    side=Side.BUY,
                    quantity=quantity,
                    price=signal.price,
                    confidence=signal.score,
                    reason=signal.reason,
                )
            )

            if reserved_cash >= settings.budget:
                break

        return signals[:12], proposals[:5]

    # ------------------------------------------------------------------
    # Per-symbol signal
    # ------------------------------------------------------------------

    def _signal(self, quote: Quote, portfolio: Portfolio) -> Signal | None:
        prices = self.history.setdefault(quote.symbol, deque(maxlen=30))
        diag = self._diagnostics(quote)

        if len(prices) < 8:
            return Signal(
                symbol=quote.symbol,
                price=quote.price,
                score=0.0,
                action="watch",
                reason="Collecting enough intraday observations before ranking this symbol.",
                diagnostics=diag,
            )

        short_avg = sum(list(prices)[-3:]) / 3
        long_avg = sum(prices) / len(prices)
        momentum = short_avg / long_avg - 1.0
        position = portfolio.positions.get(quote.symbol)
        held_quantity = position.quantity if position else 0

        if held_quantity > 0 and momentum < -0.002:
            return Signal(
                symbol=quote.symbol,
                price=quote.price,
                score=min(0.95, 0.55 + abs(momentum) * 20),
                action="sell",
                reason=(
                    "Momentum weakened versus the intraday baseline; exit is prioritized to protect P&L."
                    + (f" Volatility {diag.volatility:.1f}%." if diag.volatility else "")
                ),
                diagnostics=diag,
            )

        if held_quantity == 0 and momentum > 0.002:
            return Signal(
                symbol=quote.symbol,
                price=quote.price,
                score=min(0.95, 0.55 + momentum * 20),
                action="buy",
                reason=(
                    "Positive short-term momentum versus the intraday baseline ranks this as a buy candidate."
                    + (f" Trend strength {diag.trend_strength:.2f}%." if diag.trend_strength else "")
                    + (" Volume spike detected." if diag.volume_spike else "")
                ),
                diagnostics=diag,
            )

        return Signal(
            symbol=quote.symbol,
            price=quote.price,
            score=max(0.0, min(0.5, 0.25 + momentum * 10)),
            action="watch",
            reason="No strong entry or exit edge right now.",
            diagnostics=diag,
        )
