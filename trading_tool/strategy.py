from __future__ import annotations

from collections import deque

from .broker import affordable_quantity
from .models import OrderProposal, Portfolio, Quote, Settings, Side, Signal


class MomentumStrategy:
    def __init__(self) -> None:
        self.history: dict[str, deque[float]] = {}

    def observe(self, quote: Quote) -> None:
        prices = self.history.setdefault(quote.symbol, deque(maxlen=30))
        prices.append(quote.price)

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

    def scan(self, settings: Settings, quotes: list[Quote], portfolio: Portfolio) -> tuple[list[Signal], list[OrderProposal]]:
        for quote in quotes:
            self.observe(quote)

        signals = [signal for quote in quotes if (signal := self._signal(quote, portfolio)) is not None]
        signals.sort(key=lambda item: item.score, reverse=True)

        if portfolio.realized_pnl + portfolio.unrealized_pnl() <= -settings.max_loss:
            return signals, []

        proposals: list[OrderProposal] = []
        reserved_cash = 0.0

        for signal in signals:
            position = portfolio.positions.get(signal.symbol)
            held_quantity = position.quantity if position else 0

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

    def _signal(self, quote: Quote, portfolio: Portfolio) -> Signal | None:
        prices = self.history.setdefault(quote.symbol, deque(maxlen=30))
        if len(prices) < 8:
            return Signal(
                symbol=quote.symbol,
                price=quote.price,
                score=0.0,
                action="watch",
                reason="Collecting enough intraday observations before ranking this symbol.",
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
                reason="Momentum weakened versus the intraday baseline; exit is prioritized to protect P&L.",
            )

        if held_quantity == 0 and momentum > 0.002:
            return Signal(
                symbol=quote.symbol,
                price=quote.price,
                score=min(0.95, 0.55 + momentum * 20),
                action="buy",
                reason="Positive short-term momentum versus the intraday baseline ranks this as a buy candidate.",
            )

        return Signal(
            symbol=quote.symbol,
            price=quote.price,
            score=max(0.0, min(0.5, 0.25 + momentum * 10)),
            action="watch",
            reason="No strong entry or exit edge right now.",
        )
