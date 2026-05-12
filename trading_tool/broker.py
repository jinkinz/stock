from __future__ import annotations

import math
import random
from datetime import datetime, timezone
from typing import Protocol

from .models import OrderProposal, Portfolio, Position, Quote, Side


class Broker(Protocol):
    def quote(self, symbol: str) -> Quote:
        ...

    def quotes(self, symbols: list[str]) -> list[Quote]:
        ...

    def discover_symbols(self, markets: list[str]) -> list[str]:
        ...

    def submit_order(self, proposal: OrderProposal) -> OrderProposal:
        ...

    def portfolio(self) -> Portfolio:
        ...


class PaperBroker:
    def __init__(self, starting_cash: float = 10000.0, portfolio: Portfolio | None = None, prices: dict[str, float] | None = None) -> None:
        self._portfolio = portfolio or Portfolio(cash=starting_cash)
        self._prices: dict[str, float] = prices or {}

    def quote(self, symbol: str) -> Quote:
        previous = self._prices.get(symbol)
        if previous is None:
            previous = self._seed_price(symbol)
        drift = random.uniform(-0.008, 0.008)
        price = max(1.0, previous * (1.0 + drift))
        self._prices[symbol] = round(price, 2)
        self._portfolio.last_prices[symbol] = self._prices[symbol]
        return Quote(
            symbol=symbol,
            price=self._prices[symbol],
            timestamp=datetime.now(timezone.utc).isoformat(),
            source="paper",
        )

    def quotes(self, symbols: list[str]) -> list[Quote]:
        return [self.quote(symbol) for symbol in symbols]

    def discover_symbols(self, markets: list[str]) -> list[str]:
        return []

    def submit_order(self, proposal: OrderProposal) -> OrderProposal:
        symbol = proposal.symbol
        price = self._prices.get(symbol, proposal.price)
        notional = price * proposal.quantity
        position = self._portfolio.positions.setdefault(symbol, Position(symbol=symbol))

        if proposal.side is Side.BUY:
            if notional > self._portfolio.cash:
                proposal.error = "Insufficient paper cash."
                return proposal
            new_quantity = position.quantity + proposal.quantity
            position.avg_cost = ((position.quantity * position.avg_cost) + notional) / new_quantity
            position.quantity = new_quantity
            self._portfolio.cash -= notional
        else:
            if proposal.quantity > position.quantity:
                proposal.error = "Cannot sell more than the current paper position."
                return proposal
            self._portfolio.cash += notional
            self._portfolio.realized_pnl += proposal.quantity * (price - position.avg_cost)
            position.quantity -= proposal.quantity
            if position.quantity == 0:
                position.avg_cost = 0.0

        proposal.price = round(price, 2)
        self._portfolio.cash = round(self._portfolio.cash, 2)
        self._portfolio.realized_pnl = round(self._portfolio.realized_pnl, 2)
        return proposal

    def portfolio(self) -> Portfolio:
        return self._portfolio

    def snapshot(self) -> dict:
        return {
            "portfolio": {
                "cash": self._portfolio.cash,
                "realized_pnl": self._portfolio.realized_pnl,
                "positions": {symbol: {"symbol": pos.symbol, "quantity": pos.quantity, "avg_cost": pos.avg_cost} for symbol, pos in self._portfolio.positions.items()},
                "last_prices": self._portfolio.last_prices,
            },
            "prices": self._prices,
        }

    def _seed_price(self, symbol: str) -> float:
        if symbol.endswith(".HK"):
            return 20.0 + random.random() * 400.0
        if symbol.endswith(".SG"):
            return 1.0 + random.random() * 40.0
        return 50.0 + random.random() * 250.0


class LongbridgeBroker:
    def __init__(self) -> None:
        try:
            from decimal import Decimal
            from longbridge.openapi import Config, Market, OrderSide, OrderType, QuoteContext, SecurityListCategory, TimeInForceType, TradeContext
        except ImportError as exc:
            raise RuntimeError("Install the Longbridge SDK with `pip install longbridge` before using live mode.") from exc

        self.Decimal = Decimal
        self.Market = Market
        self.OrderSide = OrderSide
        self.OrderType = OrderType
        self.SecurityListCategory = SecurityListCategory
        self.TimeInForceType = TimeInForceType
        config = Config.from_apikey_env()
        self.quote_ctx = QuoteContext(config)
        self.trade_ctx = TradeContext(config)
        self._portfolio = Portfolio(cash=0.0)

    def quote(self, symbol: str) -> Quote:
        quotes = self.quote_ctx.quote([symbol])
        first = quotes[0]
        price = float(first.last_done)
        self._portfolio.last_prices[symbol] = price
        return Quote(symbol=symbol, price=price, timestamp=datetime.now(timezone.utc).isoformat(), source="longbridge")

    def quotes(self, symbols: list[str]) -> list[Quote]:
        if not symbols:
            return []
        responses = self.quote_ctx.quote(symbols)
        quotes: list[Quote] = []
        for item in responses:
            price = float(item.last_done)
            symbol = item.symbol
            self._portfolio.last_prices[symbol] = price
            quotes.append(Quote(symbol=symbol, price=price, timestamp=datetime.now(timezone.utc).isoformat(), source="longbridge"))
        return quotes

    def discover_symbols(self, markets: list[str]) -> list[str]:
        symbols: list[str] = []
        if "US" not in markets:
            return symbols
        try:
            responses = self.quote_ctx.security_list(self.Market.US, self.SecurityListCategory.Overnight)
        except TypeError:
            responses = self.quote_ctx.security_list(self.SecurityListCategory.Overnight)
        for item in responses:
            symbol = getattr(item, "symbol", None)
            if symbol:
                symbols.append(symbol)
        return list(dict.fromkeys(symbols))

    def submit_order(self, proposal: OrderProposal) -> OrderProposal:
        side = self.OrderSide.Buy if proposal.side is Side.BUY else self.OrderSide.Sell
        self.trade_ctx.submit_order(
            proposal.symbol,
            self.OrderType.LO,
            side,
            self.Decimal(proposal.quantity),
            self.TimeInForceType.Day,
            submitted_price=self.Decimal(str(proposal.price)),
            remark=f"trading-tool:{proposal.id}",
        )
        return proposal

    def portfolio(self) -> Portfolio:
        balances = self.trade_ctx.account_balance()
        cash = 0.0
        for balance in balances:
            for info in getattr(balance, "cash_infos", []) or []:
                if getattr(info, "currency", "") == "USD":
                    cash += float(getattr(info, "available_cash", 0) or 0)
        self._portfolio.cash = round(cash, 2)
        return self._portfolio


def affordable_quantity(price: float, max_trade_value: float, available_cash: float) -> int:
    if price <= 0:
        return 0
    return max(0, math.floor(min(max_trade_value, available_cash) / price))
