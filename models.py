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


# Settings whose sensible value depends entirely on the holding horizon. A 1%
# stop is a normal morning's noise on a multi-day hold; a 390-minute session
# timer is meaningless when positions are held for a week. These are kept as
# SEPARATE saved profiles so switching horizon does not destroy the other one's
# tuning — and so the two can be compared honestly.
HORIZON_FIELDS = (
    "duration_minutes", "stop_at_end", "lock_profit_pct", "stop_loss_pct",
    "trailing_stop_pct", "tick_interval_seconds", "risk_per_trade_pct",
    "atr_stop_multiple",
)

HORIZON_DEFAULTS: dict[str, dict] = {
    "intraday": {
        # Mirrors the dataclass defaults exactly, so switching away and back
        # restores what a fresh install actually had rather than a second,
        # subtly different set of "defaults".
        "duration_minutes": 390, "stop_at_end": True,
        "lock_profit_pct": 0.0, "stop_loss_pct": 2.0, "trailing_stop_pct": 0.0,
        "tick_interval_seconds": 60,
        # Intraday ATR is a fraction of a percent, so a small risk % implies a
        # huge position — see sizing.py. Kept low deliberately.
        "risk_per_trade_pct": 0.5, "atr_stop_multiple": 2.0,
    },
    "swing": {
        # Sessions do not expire and nothing is force-flattened, so duration is
        # inert here — kept at a valid value rather than 0, which normalized()
        # would clamp to a misleading 1 minute.
        "duration_minutes": 390, "stop_at_end": False,
        # Wide enough to clear round-trip costs, which intraday targets cannot.
        "lock_profit_pct": 3.0, "stop_loss_pct": 2.0, "trailing_stop_pct": 0.0,
        # Daily bars change once a day; polling every minute is waste.
        "tick_interval_seconds": 900,
        "risk_per_trade_pct": 1.0, "atr_stop_multiple": 2.0,
    },
}


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
    # "intraday" — 1-min candles, session expires after duration_minutes,
    #              stop_at_end can flatten. Indicators measure minutes.
    # "swing"    — daily candles, sessions do NOT auto-expire, positions are
    #              meant to be held overnight. Indicators measure days.
    # This is the single switch that changes the holding horizon; see
    # TradingEngine.CANDLE_SPEC and _session_expired().
    trading_horizon: str = "intraday"
    # Saved values per horizon. The flat fields above are the ACTIVE ones for
    # the current horizon; this holds the other horizon's tuning so switching
    # back restores it instead of silently reusing numbers meant for the other.
    horizon_profiles: dict = field(
        default_factory=lambda: {k: dict(v) for k, v in HORIZON_DEFAULTS.items()})
    # Refuse buys whose profit target cannot clear their own round-trip costs.
    # A trade that loses money when it WINS is never worth placing; leave this
    # on unless you are deliberately measuring the damage.
    enforce_trade_viability: bool = True
    # Convergence gate: how many INDEPENDENT factors must agree before a buy.
    # A single blended score can hide disagreement — a big day move alone can
    # drag the total over the line while trend, VWAP and volume all say no.
    # Higher = fewer, higher-conviction trades = less fee drag. 0 disables it.
    #
    # Defaults to full convergence (5). Replay across three datasets showed the
    # "almost converged" band (exactly 4 of 5) is reliably the WORST cohort,
    # while requiring all five improved expectancy every time and cut trade
    # count ~40%. 5 is deliberately an endpoint, not a tuned interior optimum —
    # picking the best-scoring middle value would be curve-fitting.
    min_confirmations: int = 5
    # ── Risk-based position sizing (see sizing.py) ───────────────────────
    risk_per_trade_pct: float = 0.5   # % of equity risked if the stop fires
    atr_stop_multiple: float = 2.0    # stop distance = N × ATR
    use_atr_sizing: bool = True       # off = flat max_trade_value sizing
    # ── Portfolio protections (see risk.py). 0 disables each one. ────────
    max_concurrent_positions: int = 5
    daily_budget: float = 0.0         # capital deployable per exchange-local day
    daily_loss_limit: float = 0.0     # realised loss that halts buying for the day
    cooldown_after_losses: int = 3    # N consecutive losers pauses buying

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
        self.enforce_trade_viability = bool(self.enforce_trade_viability)
        self.min_confirmations = max(0, min(5, int(self.min_confirmations)))
        self.risk_per_trade_pct = max(0.0, min(100.0, float(self.risk_per_trade_pct)))
        self.atr_stop_multiple = max(0.1, float(self.atr_stop_multiple))
        self.use_atr_sizing = bool(self.use_atr_sizing)
        self.max_concurrent_positions = max(0, int(self.max_concurrent_positions))
        self.daily_budget = max(0.0, float(self.daily_budget))
        self.daily_loss_limit = max(0.0, float(self.daily_loss_limit))
        self.cooldown_after_losses = max(0, int(self.cooldown_after_losses))
        horizon = str(self.trading_horizon or "").strip().lower()
        self.trading_horizon = horizon if horizon in ("intraday", "swing") else "intraday"
        # If an hourly rate is set, it drives the session target automatically —
        # e.g. $20/hr over a 390-minute (6.5hr) session = $130 target.
        if self.target_profit_per_hour > 0:
            self.target_profit = round(self.target_profit_per_hour * (self.duration_minutes / 60.0), 2)
        return self

    @property
    def is_swing(self) -> bool:
        return self.trading_horizon == "swing"

    # ── Horizon profiles ─────────────────────────────────────────────────

    def _horizon_snapshot(self) -> dict:
        return {field: getattr(self, field) for field in HORIZON_FIELDS}

    def sync_horizon_profile(self) -> "Settings":
        """Persist the active values into the current horizon's profile."""
        if not isinstance(self.horizon_profiles, dict):
            self.horizon_profiles = {}
        self.horizon_profiles[self.trading_horizon] = self._horizon_snapshot()
        return self

    def switch_horizon(self, new_horizon: str) -> "Settings":
        """Stash the current horizon's tuning and load the other one's.

        Without this, switching to swing would carry a 1% stop and a 390-minute
        session timer across — numbers that mean something completely different
        on a multi-day hold.
        """
        new_horizon = (new_horizon or "").strip().lower()
        if new_horizon not in HORIZON_DEFAULTS or new_horizon == self.trading_horizon:
            return self
        self.sync_horizon_profile()
        saved = self.horizon_profiles.get(new_horizon) or HORIZON_DEFAULTS[new_horizon]
        for field_name in HORIZON_FIELDS:
            if field_name in saved:
                setattr(self, field_name, saved[field_name])
        self.trading_horizon = new_horizon
        return self

    def config_fingerprint(self) -> dict:
        """The settings that materially shape a trade, for grouping outcomes.

        Deliberately small: parameters that change WHICH trades are taken and
        WHERE they exit. Not budget or universe, which affect size and
        candidates rather than the decision rule.
        """
        return {
            "horizon": self.trading_horizon,
            "strategy": self.ai_strategy_name,
            "min_confirmations": self.min_confirmations,
            "lock_profit_pct": self.lock_profit_pct,
            "stop_loss_pct": self.stop_loss_pct,
            "trailing_stop_pct": self.trailing_stop_pct,
            "sizing": "atr" if self.use_atr_sizing else "flat",
            "risk_per_trade_pct": self.risk_per_trade_pct,
            "atr_stop_multiple": self.atr_stop_multiple,
        }

    def config_key(self) -> str:
        """Short human-readable grouping key, e.g.
        `swing/fifo/gate5/+3.0-2.0/atr`."""
        f = self.config_fingerprint()
        return (f"{f['horizon']}/{f['strategy']}/gate{f['min_confirmations']}"
                f"/+{f['lock_profit_pct']:g}-{f['stop_loss_pct']:g}/{f['sizing']}")

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
    # Absolute stop price fixed at entry from the symbol's own ATR. 0.0 means
    # none was set, and the flat stop_loss_pct applies instead.
    stop_price: float = 0.0
    # ── Round-trip accounting ────────────────────────────────────────────
    # Everything below survives the position going flat: the broker zeroes
    # quantity/avg_cost/peak_price on the closing fill, and the engine reads
    # these fields one call later to emit the closed-trade record. Only
    # reset_round_trip() clears them.
    opened_at: str = ""              # ISO time of the first entry fill
    entry_price: float = 0.0         # weighted avg entry cost for this round trip
    entry_qty: float = 0.0           # total shares bought this round trip
    entry_score: float = 0.0         # signal score at entry
    entry_strategy: str = ""         # settings.ai_strategy_name at entry
    entry_mode: str = ""             # paper | live at entry
    entry_diagnostics: dict = field(default_factory=dict)
    # The configuration this trade was opened under. Without it the ledger can
    # say a trade lost money but not what settings produced it — which makes
    # "what actually works" unanswerable after the fact.
    entry_config: dict = field(default_factory=dict)
    fees_paid: float = 0.0           # fees across entry + all (partial) exits
    exit_qty: float = 0.0            # shares sold so far this round trip
    exit_proceeds: float = 0.0       # gross proceeds so far (before fees)

    def reset_round_trip(self) -> None:
        """Clear entry context after a completed round trip has been logged."""
        self.opened_at = ""
        self.entry_price = 0.0
        self.entry_qty = 0.0
        self.entry_score = 0.0
        self.entry_strategy = ""
        self.entry_mode = ""
        self.entry_diagnostics = {}
        self.entry_config = {}
        self.fees_paid = 0.0
        self.exit_qty = 0.0
        self.exit_proceeds = 0.0


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
    # Exchange trading status. "normal" = tradable; anything else (halted,
    # suspended, delisted) must never be bought into.
    trade_status: str = "normal"


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
    tradable: bool = True             # False = exchange says halted/suspended
    # Real-market metrics (0.0 / "" when data unavailable)
    day_change_pct: float = 0.0      # % vs previous close
    from_high_pct: float = 0.0       # % below the day high (≤ 0)
    turnover: float = 0.0            # traded value today — liquidity gate
    rsi: float = 0.0                 # RSI(14) from 1-min candles
    vwap_dist_pct: float = 0.0       # % above (+) / below (−) session VWAP
    ema_trend: str = ""              # "bull" (EMA9>EMA21), "bear", or "" unknown
    vol_surge: float = 0.0           # recent vs session-avg minute volume (RVOL proxy)
    atr: float = 0.0                 # Average True Range — 0.0 = unknown, NOT "no volatility"
    atr_pct: float = 0.0             # ATR as % of price — comparable across symbols


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
    # Machine-readable origin of this proposal. On a SELL it becomes the closed
    # trade's exit_reason — never parse the free-text `reason` for this.
    #   profit_lock | stop_loss | trailing_stop | ai_sell | strategy_sell
    #   | session_end | "" (→ recorded as "manual")
    tag: str = ""
    # Brokerage cost of this fill. Modelled from fees.py in paper mode; read
    # from the broker's real charge_detail in live mode. 0.0 means "not known",
    # not "free".
    fee: float = 0.0
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
