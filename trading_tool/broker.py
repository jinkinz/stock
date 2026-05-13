from __future__ import annotations

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
    """Local paper broker. Uses Longbridge real quotes when credentials are
    available; falls back to a random-walk simulator when they are not."""

    def __init__(
        self,
        starting_cash: float = 0.0,
        portfolio: Portfolio | None = None,
        prices: dict[str, float] | None = None,
    ) -> None:
        self._portfolio = portfolio or Portfolio(cash=starting_cash)
        self._prices: dict[str, float] = prices or {}
        self._lb: LongbridgeBroker | None = None
        # Try to attach a Longbridge quote context so paper trades use real prices.
        try:
            self._lb = LongbridgeBroker()
        except Exception:
            self._lb = None

    # ------------------------------------------------------------------
    # Quote helpers
    # ------------------------------------------------------------------

    def quote(self, symbol: str) -> Quote:
        if self._lb is not None:
            try:
                q = self._lb.quote(symbol)
                self._prices[symbol] = q.price
                self._portfolio.last_prices[symbol] = q.price
                return Quote(symbol=symbol, price=q.price, timestamp=q.timestamp, source="longbridge-paper")
            except Exception:
                pass
        return self._simulated_quote(symbol)

    def quotes(self, symbols: list[str]) -> list[Quote]:
        if self._lb is not None:
            try:
                qs = self._lb.quotes(symbols)
                for q in qs:
                    self._prices[q.symbol] = q.price
                    self._portfolio.last_prices[q.symbol] = q.price
                return [Quote(symbol=q.symbol, price=q.price, timestamp=q.timestamp, source="longbridge-paper") for q in qs]
            except Exception:
                pass
        return [self._simulated_quote(s) for s in symbols]

    def discover_symbols(self, markets: list[str]) -> list[str]:
        if self._lb is not None:
            try:
                return self._lb.discover_symbols(markets)
            except Exception:
                pass
        return []

    def submit_order(self, proposal: OrderProposal) -> OrderProposal:
        symbol = proposal.symbol
        price = self._prices.get(symbol, proposal.price)
        quantity = round(float(proposal.quantity), 6)
        notional = round(price * quantity, 6)
        position = self._portfolio.positions.setdefault(symbol, Position(symbol=symbol))

        if proposal.side is Side.BUY:
            if notional > self._portfolio.cash:
                proposal.error = "Insufficient paper cash."
                return proposal
            new_quantity = round(position.quantity + quantity, 6)
            position.avg_cost = ((position.quantity * position.avg_cost) + notional) / new_quantity
            position.quantity = new_quantity
            self._portfolio.cash -= notional
        else:
            if quantity > position.quantity + 1e-9:
                proposal.error = "Cannot sell more than the current paper position."
                return proposal
            quantity = min(quantity, position.quantity)
            notional = round(price * quantity, 6)
            self._portfolio.cash += notional
            self._portfolio.realized_pnl += quantity * (price - position.avg_cost)
            position.quantity = round(position.quantity - quantity, 6)
            if position.quantity < 1e-9:
                position.quantity = 0.0
                position.avg_cost = 0.0

        proposal.price = round(price, 2)
        proposal.quantity = quantity
        self._portfolio.cash = round(self._portfolio.cash, 2)
        self._portfolio.realized_pnl = round(self._portfolio.realized_pnl, 6)
        return proposal

    def portfolio(self) -> Portfolio:
        return self._portfolio

    def snapshot(self) -> dict:
        return {
            "portfolio": {
                "cash": self._portfolio.cash,
                "realized_pnl": self._portfolio.realized_pnl,
                "positions": {
                    symbol: {"symbol": pos.symbol, "quantity": pos.quantity, "avg_cost": pos.avg_cost}
                    for symbol, pos in self._portfolio.positions.items()
                },
                "last_prices": self._portfolio.last_prices,
            },
            "prices": self._prices,
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _simulated_quote(self, symbol: str) -> Quote:
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
            source="paper-sim",
        )

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
            from longbridge.openapi import (
                Config,
                Market,
                OrderSide,
                OrderType,
                QuoteContext,
                SecurityListCategory,
                TimeInForceType,
                TradeContext,
            )
        except ImportError as exc:
            raise RuntimeError(
                "Install the Longbridge SDK with `pip install longbridge` before using live mode."
            ) from exc

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

    # ------------------------------------------------------------------
    # Quotes
    # ------------------------------------------------------------------

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
        result: list[Quote] = []
        for item in responses:
            price = float(item.last_done)
            symbol = item.symbol
            self._portfolio.last_prices[symbol] = price
            result.append(Quote(symbol=symbol, price=price, timestamp=datetime.now(timezone.utc).isoformat(), source="longbridge"))
        return result

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

    # ------------------------------------------------------------------
    # Orders
    # ------------------------------------------------------------------

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

    # ------------------------------------------------------------------
    # Portfolio — full sync from Longbridge
    # ------------------------------------------------------------------

    def portfolio(self) -> Portfolio:
        """Sync cash and stock positions from Longbridge account."""
        # --- cash ---
        try:
            balances = self.trade_ctx.account_balance()
            cash = 0.0
            for balance in balances:
                for info in getattr(balance, "cash_infos", []) or []:
                    if getattr(info, "currency", "") == "USD":
                        cash += float(getattr(info, "available_cash", 0) or 0)
            self._portfolio.cash = round(cash, 2)
        except Exception:
            pass

        # --- stock positions ---
        try:
            resp = self.trade_ctx.stock_positions()
            channels = getattr(resp, "channels", None) or []
            synced: dict[str, Position] = {}
            for channel in channels:
                for pos in getattr(channel, "positions", []) or []:
                    symbol = getattr(pos, "symbol", None)
                    qty = float(getattr(pos, "quantity", 0) or 0)
                    cost_price = float(getattr(pos, "cost_price", 0) or 0)
                    if symbol and qty > 0:
                        synced[symbol] = Position(symbol=symbol, quantity=qty, avg_cost=cost_price)
            if synced:
                self._portfolio.positions = synced
        except Exception:
            pass

        return self._portfolio


def affordable_quantity(price: float, max_trade_value: float, available_cash: float) -> float:
    """Return the fractional quantity affordable within budget and trade-value limits.
    Result is rounded to 6 decimal places (sub-cent precision)."""
    if price <= 0:
        return 0.0
    return round(min(max_trade_value, available_cash) / price, 6)
