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
    budget: float = 0.0
    duration_minutes: int = 390
    max_loss: float = 25.0
    max_trade_value: float = 250.0
    auto_tick_enabled: bool = False
    tick_interval_seconds: int = 60
    allow_live_trading: bool = False
    stop_at_end: bool = True
    strategy_enabled: bool = False
    started_at: str | None = None
    target_profit: float = 0.0        # how much $ profit to aim for this session
    target_profit_per_hour: float = 0.0  # pacing rate — overrides target_profit if set
    lock_profit_pct: float = 0.0      # auto-sell a position once its unrealized gain hits this % (0 = off)
    stop_loss_pct: float = 2.0        # auto-sell a position once it loses this % from entry (0 = off)
    trailing_stop_pct: float = 0.0    # auto-sell if price falls this % from its peak while in profit (0 = off)
    ai_strategy_name: str = "fifo"    # which AI strategy to use

    def normalized(self) -> "Settings":
        self.symbol = self.symbol.strip().upper()
        self.markets = [m.strip().upper() for m in self.markets if m.strip()]
        self.universe = [s.strip().upper() for s in self.universe if s.strip()]
        if not self.markets:
            self.markets = ["US"]
        self.budget = max(0.0, float(self.budget))
        self.duration_minutes = max(1, int(self.duration_minutes))
        self.max_scan_symbols = max(0, min(2000, int(self.max_scan_symbols)))
        self.max_loss = max(0.0, float(self.max_loss))
        self.max_trade_value = max(0.0, float(self.max_trade_value))
        self.tick_interval_seconds = max(5, int(self.tick_interval_seconds))
        self.target_profit = max(0.0, float(self.target_profit))
        self.target_profit_per_hour = max(0.0, float(self.target_profit_per_hour))
        self.lock_profit_pct = max(0.0, float(self.lock_profit_pct))
        self.stop_loss_pct = max(0.0, float(self.stop_loss_pct))
        self.trailing_stop_pct = max(0.0, float(self.trailing_stop_pct))
        # If an hourly rate is set, it drives the session target automatically —
        # e.g. $20/hr over a 390-minute (6.5hr) session = $130 target.
        if self.target_profit_per_hour > 0:
            self.target_profit = round(self.target_profit_per_hour * (self.duration_minutes / 60.0), 2)
        return self

    def active_universe(self) -> list[str]:
        if self.universe:
            return self.universe if self.max_scan_symbols == 0 else self.universe[: self.max_scan_symbols]
        symbols: list[str] = []
        for market in self.markets:
            symbols.extend(DEFAULT_UNIVERSES.get(market, []))
        unique = list(dict.fromkeys(symbols))
        return unique if self.max_scan_symbols == 0 else unique[: self.max_scan_symbols]


@dataclass
class Position:
    symbol: str
    quantity: float = 0.0
    avg_cost: float = 0.0
    peak_price: float = 0.0          # high-water mark since entry — drives trailing stop


@dataclass
class Quote:
    symbol: str
    price: float
    timestamp: str
    source: str
    # Real intraday context from the exchange (0.0 = not available, e.g. sim mode)
    prev_close: float = 0.0
    open: float = 0.0
    high: float = 0.0
    low: float = 0.0
    volume: float = 0.0
    turnover: float = 0.0


@dataclass
class Diagnostics:
    """Richer per-symbol metrics computed alongside each scan."""
    symbol: str
    price: float
    volatility: float = 0.0          # std-dev of recent returns (annualised approx)
    spread_pct: float = 0.0          # synthetic bid-ask spread estimate
    volume_spike: bool = False        # True when recent tick-count is >2× baseline
    trend_strength: float = 0.0      # abs(short_avg/long_avg - 1) × 100
    news_gate: bool = True            # True = OK to trade; False = news blackout (stub)
    # Real-market metrics (0.0 / "" when data unavailable)
    day_change_pct: float = 0.0      # % vs previous close
    from_high_pct: float = 0.0       # % below the day high (≤ 0)
    turnover: float = 0.0            # traded value today — liquidity gate
    rsi: float = 0.0                 # RSI(14) from 1-min candles
    vwap_dist_pct: float = 0.0       # % above (+) / below (−) session VWAP
    ema_trend: str = ""              # "bull" (EMA9>EMA21), "bear", or "" unknown


@dataclass
class Signal:
    symbol: str
    price: float
    score: float
    action: str
    reason: str
    diagnostics: Diagnostics | None = None


@dataclass
class OrderProposal:
    symbol: str
    side: Side
    quantity: float
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
    cash: float = 0.0
    realized_pnl: float = 0.0
    positions: dict[str, Position] = field(default_factory=dict)
    last_prices: dict[str, float] = field(default_factory=dict)

    def equity(self) -> float:
        holdings = sum(
            pos.quantity * self.last_prices.get(symbol, pos.avg_cost)
            for symbol, pos in self.positions.items()
        )
        return round(self.cash + holdings, 2)

    def unrealized_pnl(self) -> float:
        pnl = 0.0
        for symbol, pos in self.positions.items():
            pnl += pos.quantity * (self.last_prices.get(symbol, pos.avg_cost) - pos.avg_cost)
        return round(pnl, 2)


# ---------------------------------------------------------------------------
# Audit log
# ---------------------------------------------------------------------------

class AuditEventType(str, Enum):
    SIGNAL = "signal"
    PROPOSAL = "proposal"
    APPROVE = "approve"
    REJECT = "reject"
    FILL = "fill"
    FAIL = "fail"
    TICK = "tick"


@dataclass
class AuditEntry:
    event: AuditEventType
    at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    symbol: str | None = None
    detail: dict = field(default_factory=dict)


DEFAULT_UNIVERSES = {
    # 50 most liquid US stocks across tech, finance, consumer, energy, health
    "US": [
        # Mega-cap tech
        "AAPL.US", "MSFT.US", "NVDA.US", "GOOGL.US", "AMZN.US", "META.US",
        "TSLA.US", "AMD.US", "INTC.US", "QCOM.US", "AVGO.US", "TXN.US",
        "CRM.US", "ORCL.US", "ADBE.US", "NOW.US", "SNOW.US", "PLTR.US",
        # Finance
        "JPM.US", "BAC.US", "GS.US", "MS.US", "WFC.US", "C.US",
        "V.US", "MA.US", "PYPL.US", "SQ.US", "COIN.US",
        # Consumer / retail
        "WMT.US", "COST.US", "TGT.US", "HD.US", "MCD.US", "SBUX.US",
        "NKE.US", "DIS.US", "NFLX.US", "SPOT.US",
        # Health / pharma
        "JNJ.US", "PFE.US", "MRNA.US", "ABBV.US", "LLY.US", "UNH.US",
        # Energy / industrial
        "XOM.US", "CVX.US", "CAT.US", "BA.US", "GE.US", "F.US",
    ],
    # 40 most liquid HK stocks — HSI constituents + large caps
    "HK": [
        # Tech / internet
        "700.HK", "9988.HK", "3690.HK", "1810.HK", "9618.HK", "9999.HK",
        "1024.HK", "2015.HK", "6690.HK", "9961.HK", "9626.HK", "2382.HK",
        # Finance / banking
        "1398.HK", "939.HK", "3988.HK", "2318.HK", "1299.HK", "2628.HK",
        "388.HK", "5.HK", "11.HK", "2388.HK", "6881.HK",
        # Property / infrastructure
        "1.HK", "16.HK", "688.HK", "101.HK", "823.HK", "778.HK",
        # Consumer / retail
        "9922.HK", "6862.HK", "1929.HK", "291.HK", "762.HK",
        # Energy / utilities
        "857.HK", "883.HK", "2.HK", "6.HK", "3.HK",
        # Healthcare
        "1177.HK", "2269.HK",
    ],
    # 30 most liquid SG stocks — STI constituents + REITs
    "SG": [
        # Banks (biggest by volume)
        "D05.SG", "O39.SG", "U11.SG",
        # REITs
        "C38U.SG", "A17U.SG", "ME8U.SG", "N2IU.SG", "T82U.SG",
        "BUOU.SG", "K71U.SG", "J91U.SG", "SK6U.SG", "AW9U.SG",
        # Telcos / tech
        "Z74.SG", "T39.SG", "BN4.SG",
        # Industrial / transport
        "S68.SG", "C6L.SG", "U96.SG", "C52.SG", "BS6.SG",
        # Consumer / property
        "F34.SG", "H78.SG", "C09.SG", "S58.SG", "U14.SG",
        # Healthcare / others
        "Q0F.SG", "5E2.SG", "V03.SG", "BVA.SG", "42F.SG",
    ],
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
