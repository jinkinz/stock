from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any
from uuid import uuid4


class TradingMode(str, Enum):
    PAPER = "paper"
    LIVE = "live"


class ApprovalMode(str, Enum):
    MANUAL = "manual"
    AUTO = "auto"


class Side(str, Enum):
    BUY = "buy"
    SELL = "sell"


class OrderStatus(str, Enum):
    PROPOSED = "proposed"
    APPROVED = "approved"
    REJECTED = "rejected"
    FILLED = "filled"
    FAILED = "failed"


@dataclass
class Settings:
    symbol: str = "AAPL.US"
    markets: list[str] = field(default_factory=lambda: ["US"])
    universe: list[str] = field(default_factory=list)
    max_scan_symbols: int = 50
    trading_mode: TradingMode = TradingMode.PAPER
    approval_mode: ApprovalMode = ApprovalMode.MANUAL
    budget: float = 1000.0
    duration_minutes: int = 390
    max_loss: float = 25.0
    max_trade_value: float = 250.0
    auto_tick_enabled: bool = False
    tick_interval_seconds: int = 60
    allow_live_trading: bool = False
    stop_at_end: bool = True
    strategy_enabled: bool = False
    started_at: str | None = None

    def normalized(self) -> "Settings":
        self.symbol = self.symbol.strip().upper()
        self.markets = [market.strip().upper() for market in self.markets if market.strip()]
        self.universe = [symbol.strip().upper() for symbol in self.universe if symbol.strip()]
        if not self.markets:
            self.markets = ["US"]
        self.budget = max(0.0, float(self.budget))
        self.duration_minutes = max(1, int(self.duration_minutes))
        self.max_scan_symbols = max(1, min(500, int(self.max_scan_symbols)))
        self.max_loss = max(0.0, float(self.max_loss))
        self.max_trade_value = max(0.0, float(self.max_trade_value))
        self.tick_interval_seconds = max(5, int(self.tick_interval_seconds))
        return self

    def active_universe(self) -> list[str]:
        if self.universe:
            return self.universe[: self.max_scan_symbols]
        symbols: list[str] = []
        for market in self.markets:
            symbols.extend(DEFAULT_UNIVERSES.get(market, []))
        return list(dict.fromkeys(symbols))[: self.max_scan_symbols]


@dataclass
class Position:
    symbol: str
    quantity: int = 0
    avg_cost: float = 0.0


@dataclass
class Quote:
    symbol: str
    price: float
    timestamp: str
    source: str


@dataclass
class Signal:
    symbol: str
    price: float
    score: float
    action: str
    reason: str


@dataclass
class OrderProposal:
    symbol: str
    side: Side
    quantity: int
    price: float
    reason: str
    confidence: float
    id: str = field(default_factory=lambda: uuid4().hex)
    status: OrderStatus = OrderStatus.PROPOSED
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    error: str | None = None

    @property
    def notional(self) -> float:
        return round(self.quantity * self.price, 2)


@dataclass
class Portfolio:
    cash: float = 10000.0
    realized_pnl: float = 0.0
    positions: dict[str, Position] = field(default_factory=dict)
    last_prices: dict[str, float] = field(default_factory=dict)

    def equity(self) -> float:
        holdings = sum(pos.quantity * self.last_prices.get(symbol, pos.avg_cost) for symbol, pos in self.positions.items())
        return round(self.cash + holdings, 2)

    def unrealized_pnl(self) -> float:
        pnl = 0.0
        for symbol, pos in self.positions.items():
            pnl += pos.quantity * (self.last_prices.get(symbol, pos.avg_cost) - pos.avg_cost)
        return round(pnl, 2)


DEFAULT_UNIVERSES = {
    "US": ["AAPL.US", "MSFT.US", "NVDA.US", "TSLA.US", "AMD.US", "META.US", "GOOGL.US", "AMZN.US"],
    "HK": ["700.HK", "9988.HK", "3690.HK", "1810.HK", "1299.HK", "388.HK"],
    "SG": ["D05.SG", "O39.SG", "U11.SG", "C38U.SG", "Z74.SG", "S68.SG"],
}


def to_json(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if hasattr(value, "__dataclass_fields__"):
        return {key: to_json(item) for key, item in asdict(value).items()}
    if isinstance(value, dict):
        return {key: to_json(item) for key, item in value.items()}
    if isinstance(value, list):
        return [to_json(item) for item in value]
    return value
