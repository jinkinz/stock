from __future__ import annotations

import json
import queue as _queue
import threading
import time
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from broker import LongbridgeBroker, PaperBroker
from models import (
    ApprovalMode,
    AuditEntry,
    AuditEventType,
    OrderProposal,
    OrderStatus,
    Portfolio,
    Position,
    Settings,
    Side,
    THESIS_FACTORS,
    TradingMode,
    to_json,
)
from ai_strategy import AI_STATUS, AIStrategy
from market_hours import (currency_of, market_of, markets_status,
                          minutes_until_open, open_markets)
from premarket import has_premarket_data, rank_gappers, rank_momentum_leaders
from metrics import compute_metrics, equal_weight_return
from risk import RiskState, check_limits
from strategy import MomentumStrategy, proposal_ttl_seconds

ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "static"
STATE_DIR = ROOT / "state"
STATE_FILE = STATE_DIR / "paper_state.json"
TRADE_LOG = STATE_DIR / "trade_log.jsonl"
# One line per completed round trip (entry fill(s) → exit fill(s)), unlike
# TRADE_LOG which records individual fills. This is what /api/metrics reads.
TRADES_CLOSED_LOG = STATE_DIR / "trades_closed.jsonl"
AUDIT_LOG = STATE_DIR / "audit_log.jsonl"
BACKTEST_LOG = STATE_DIR / "backtest_log.jsonl"
SESSIONS_LOG = STATE_DIR / "sessions_log.jsonl"


class TradingRateLimiter:
    MAX_CALLS = 30
    WINDOW_SECONDS = 30.0
    MIN_GAP = 0.02

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._call_times: list[float] = []
        self._last_call: float = 0.0

    def acquire(self, timeout: float = 5.0) -> bool:
        deadline = time.monotonic() + timeout
        while True:
            with self._lock:
                now = time.monotonic()
                self._call_times = [t for t in self._call_times if now - t < self.WINDOW_SECONDS]
                if (now - self._last_call) >= self.MIN_GAP and len(self._call_times) < self.MAX_CALLS:
                    self._call_times.append(now)
                    self._last_call = now
                    return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(self.MIN_GAP)


TRADE_RATE_LIMITER = TradingRateLimiter()


class AppState:
    def __init__(self) -> None:
        self.settings = Settings()
        self.paper_broker = PaperBroker(starting_cash=self.settings.budget)
        self.live_broker: LongbridgeBroker | None = None
        self.strategy = AIStrategy()
        self.proposals: list[OrderProposal] = []
        self.last_quote = None
        self.last_quotes: list = []
        self.signals: list = []
        self.universe_source = "sample"
        self.last_tick_at: str | None = None
        self.tick_paused: bool = False
        self.session_start_at: str | None = None
        self.session_start_equity: float = 0.0
        self.risk_state = RiskState()
        self.premarket_watchlist: list[dict] = []
        self.premarket_built_at: str = ""
        self.watchlist_kind: str = ""
        # Symbol discovery cache — refreshed every 30 min to avoid rate limit hammering
        self._symbol_cache: list[str] = []
        self._symbol_cache_markets: list[str] = []
        self._symbol_cache_at: float = 0.0
        self.lock = threading.RLock()
        self.load()

    def broker(self):
        if self.settings.trading_mode is TradingMode.LIVE:
            if self.live_broker is None:
                self.live_broker = LongbridgeBroker()
            return self.live_broker
        return self.paper_broker

    def load(self) -> None:
        if not STATE_FILE.exists():
            return
        try:
            data = json.loads(STATE_FILE.read_text())
            settings_data = data.get("settings", {})
            for key, value in settings_data.items():
                if hasattr(self.settings, key):
                    setattr(self.settings, key, value)
            if "trading_mode" in settings_data:
                self.settings.trading_mode = TradingMode(settings_data["trading_mode"])
            if "approval_mode" in settings_data:
                self.settings.approval_mode = ApprovalMode(settings_data["approval_mode"])
            self.settings.normalized()
            paper = data.get("paper", {})
            portfolio_data = paper.get("portfolio", {})
            positions = {
                symbol: Position(
                    symbol=pos.get("symbol", symbol),
                    quantity=float(pos.get("quantity", 0)),
                    avg_cost=float(pos.get("avg_cost", 0)),
                    peak_price=float(pos.get("peak_price", 0)),
                    stop_price=float(pos.get("stop_price", 0)),
                    # Round-trip context — absent in states written before the
                    # trade ledger existed, hence the defaults.
                    opened_at=pos.get("opened_at", ""),
                    entry_price=float(pos.get("entry_price", 0)),
                    entry_qty=float(pos.get("entry_qty", 0)),
                    entry_score=float(pos.get("entry_score", 0)),
                    entry_strategy=pos.get("entry_strategy", ""),
                    entry_mode=pos.get("entry_mode", ""),
                    entry_diagnostics=pos.get("entry_diagnostics") or {},
                    entry_config=pos.get("entry_config") or {},
                    # Absent in states written before thesis exits existed. An
                    # empty list disables the thesis check for that position
                    # rather than inventing a thesis it was never opened on.
                    entry_confirmations=pos.get("entry_confirmations") or [],
                    # Must survive a restart: re-deriving it from the current
                    # gain would silently disarm protection a trade had already
                    # earned, exactly when the position is underwater.
                    breakeven_armed=bool(pos.get("breakeven_armed", False)),
                    fees_paid=float(pos.get("fees_paid", 0)),
                    exit_qty=float(pos.get("exit_qty", 0)),
                    exit_proceeds=float(pos.get("exit_proceeds", 0)),
                )
                for symbol, pos in portfolio_data.get("positions", {}).items()
            }
            portfolio = Portfolio(
                cash=float(portfolio_data.get("cash", STATE.settings.budget)),
                realized_pnl=float(portfolio_data.get("realized_pnl", 0.0)),
                positions=positions,
                last_prices={s: float(p) for s, p in portfolio_data.get("last_prices", {}).items()},
            )
            prices = {s: float(p) for s, p in paper.get("prices", {}).items()}
            self.paper_broker = PaperBroker(portfolio=portfolio, prices=prices)
            self.last_tick_at = data.get("last_tick_at")
            self.tick_paused = bool(data.get("tick_paused", False))
            self.session_start_at = data.get("session_start_at")
            self.session_start_equity = float(data.get("session_start_equity", 0.0))
            self.risk_state = RiskState.from_json(data.get("risk_state") or {})
            premarket = data.get("premarket") or {}
            self.premarket_watchlist = list(premarket.get("watchlist") or [])
            self.premarket_built_at = str(premarket.get("built_at") or "")
            self.watchlist_kind = str(premarket.get("kind") or "")
            self.proposals = [self._proposal_from_json(item) for item in data.get("proposals", [])]
        except Exception:
            return

    def prune_proposals(self) -> None:
        """Bound the in-memory proposal list. Without this it grows forever
        during long auto-trading runs (the save() slice only trimmed the copy
        written to disk, not the list held in RAM)."""
        if len(self.proposals) <= 300:
            return
        pending = [p for p in self.proposals if p.status is OrderStatus.PROPOSED]
        settled = [p for p in self.proposals if p.status is not OrderStatus.PROPOSED]
        self.proposals = settled[-200:] + pending

    def save(self) -> None:
        self.prune_proposals()
        self.risk_state.prune()
        STATE_DIR.mkdir(exist_ok=True)
        payload = {
            "settings": to_json(self.settings),
            "paper": self.paper_broker.snapshot(),
            "proposals": to_json(self.proposals[-200:]),
            "last_tick_at": self.last_tick_at,
            "tick_paused": self.tick_paused,
            "session_start_at": self.session_start_at,
            "session_start_equity": self.session_start_equity,
            "risk_state": self.risk_state.to_json(),
            # In-memory only until now: a restart mid-session lost the day's
            # watchlist for good, because it can only be rebuilt in the window
            # BEFORE an open. Its own TTL still decides when it goes stale.
            "premarket": {
                "watchlist": self.premarket_watchlist,
                "built_at": self.premarket_built_at,
                "kind": self.watchlist_kind,
            },
        }
        STATE_FILE.write_text(json.dumps(payload, indent=2))

    def begin_session(self) -> None:
        portfolio = self.paper_broker.portfolio()
        self.session_start_at = datetime.now(timezone.utc).isoformat()
        self.session_start_equity = portfolio.equity()

    def close_session(self) -> None:
        if self.session_start_at is None:
            return
        portfolio = self.paper_broker.portfolio()
        equity_now = portfolio.equity()
        record = {
            "session_start": self.session_start_at,
            "session_end": datetime.now(timezone.utc).isoformat(),
            "start_equity": self.session_start_equity,
            "end_equity": equity_now,
            "session_pnl": round(equity_now - self.session_start_equity, 6),
            "realized_pnl": portfolio.realized_pnl,
            "settings": to_json(self.settings),
        }
        STATE_DIR.mkdir(exist_ok=True)
        with SESSIONS_LOG.open("a") as f:
            f.write(json.dumps(record) + "\n")
        self.session_start_at = None
        self.session_start_equity = 0.0

    def session_pnl(self) -> float:
        if self.session_start_at is None:
            return 0.0
        return round(self.paper_broker.portfolio().equity() - self.session_start_equity, 6)

    _audit_writes = 0

    def audit(self, event: AuditEventType, symbol: str | None = None, **detail) -> None:
        STATE_DIR.mkdir(exist_ok=True)
        # Rotate at ~5MB so the audit log can't grow without bound on long runs.
        AppState._audit_writes += 1
        if AppState._audit_writes % 200 == 0 and AUDIT_LOG.exists() and AUDIT_LOG.stat().st_size > 5_000_000:
            AUDIT_LOG.replace(AUDIT_LOG.with_suffix(".jsonl.old"))
        entry = AuditEntry(event=event, symbol=symbol, detail=detail)
        serialised = to_json(entry)
        with AUDIT_LOG.open("a") as handle:
            handle.write(json.dumps(serialised) + "\n")
        sse_broadcast_audit(serialised)

    def log_trade(self, proposal: OrderProposal) -> None:
        STATE_DIR.mkdir(exist_ok=True)
        # Slim snapshot only — a full portfolio dump includes last_prices for
        # every scanned symbol (32k+ with Longbridge discovery), which balloons
        # each record to ~600KB and fills the disk on long runs.
        portfolio = self.paper_broker.portfolio()
        record = {
            "logged_at": datetime.now(timezone.utc).isoformat(),
            "order": to_json(proposal),
            "cash": portfolio.cash,
            "realized_pnl": portfolio.realized_pnl,
            "equity": portfolio.equity(),
            "open_positions": {
                s: {"quantity": p.quantity, "avg_cost": p.avg_cost}
                for s, p in portfolio.positions.items() if p.quantity > 0
            },
        }
        with TRADE_LOG.open("a") as handle:
            handle.write(json.dumps(record) + "\n")

    def log_closed_trade(self, record: dict) -> None:
        STATE_DIR.mkdir(exist_ok=True)
        with TRADES_CLOSED_LOG.open("a") as handle:
            handle.write(json.dumps(record) + "\n")

    def _proposal_from_json(self, item: dict) -> OrderProposal:
        return OrderProposal(
            symbol=item["symbol"],
            side=Side(item["side"]),
            quantity=float(item["quantity"]),
            price=float(item["price"]),
            reason=item.get("reason", "Restored proposal."),
            confidence=float(item.get("confidence", 0.0)),
            tag=item.get("tag", ""),
            id=item.get("id"),
            status=OrderStatus(item.get("status", "proposed")),
            created_at=item.get("created_at", datetime.now(timezone.utc).isoformat()),
            error=item.get("error"),
        )


STATE = AppState()


def _fetch_backtest_history(symbols: list[str], ticks: int) -> dict[str, list[dict]]:
    """Real 5-min candles per symbol from Longbridge; {} when unavailable."""
    history: dict[str, list[dict]] = {}
    for symbol in symbols[:10]:   # keep the API cost bounded
        try:
            candles = STATE.paper_broker.candles(symbol, period="Min_5", count=min(1000, ticks + 30))
        except Exception:
            candles = []
        if len(candles) >= 20:
            history[symbol] = candles
    return history


def run_backtest(symbols: list[str], ticks: int = 60, starting_cash: float | None = None) -> dict:
    """Backtest on REAL historical 5-minute candles when Longbridge is
    connected. Falls back to a random-walk simulation otherwise — clearly
    labeled, because random-walk results say nothing about real performance."""
    if starting_cash is None:
        starting_cash = STATE.settings.budget
    from broker import PaperBroker as _PB
    from strategy import MomentumStrategy as _MS
    from models import Quote as _Quote

    history = _fetch_backtest_history(symbols, ticks)
    if history:
        symbols = list(history.keys())
        replay_len = min(min(len(c) for c in history.values()), ticks + 30)
        data_source = f"longbridge-history ({replay_len} real 5-min bars/symbol)"
    else:
        replay_len = ticks
        data_source = "SIMULATED random walk — not indicative of real performance"

    bt_broker = _PB(starting_cash=starting_cash)
    bt_strategy = _MS()
    settings = Settings(
        budget=starting_cash,
        max_trade_value=starting_cash / max(1, len(symbols)),
        max_loss=starting_cash * 0.10,
        approval_mode=ApprovalMode.AUTO,
        strategy_enabled=True,
    )
    equity_curve: list[dict] = []
    trades: list[dict] = []
    for tick_i in range(replay_len):
        if history:
            quotes = []
            for symbol, candles in history.items():
                bar = candles[len(candles) - replay_len + tick_i]
                if bar["close"] <= 0:
                    continue
                bt_broker._prices[symbol] = bar["close"]
                bt_broker.portfolio().last_prices[symbol] = bar["close"]
                quotes.append(_Quote(
                    symbol=symbol, price=bar["close"],
                    timestamp=datetime.now(timezone.utc).isoformat(),
                    source="longbridge-history",
                    high=bar.get("high", 0.0), low=bar.get("low", 0.0),
                    open=bar.get("open", 0.0), volume=bar.get("volume", 0.0),
                    turnover=bar.get("turnover", 0.0),
                    # first replayed bar's close stands in for prev_close so
                    # "day change" reads as change since the replay window start
                    prev_close=candles[len(candles) - replay_len]["close"],
                ))
        else:
            quotes = bt_broker.quotes(symbols)
        signals, proposals = bt_strategy.scan(settings, quotes, bt_broker.portfolio())
        for p in proposals:
            bt_broker.submit_order(p)
            trades.append({"tick": tick_i, "trade": to_json(p)})
        equity_curve.append({"tick": tick_i, "equity": bt_broker.portfolio().equity()})
    final = bt_broker.portfolio()
    result = {
        "symbols": symbols, "ticks": replay_len, "starting_cash": starting_cash,
        "data_source": data_source,
        "final_equity": final.equity(), "realized_pnl": final.realized_pnl,
        "unrealized_pnl": final.unrealized_pnl(), "total_trades": len(trades),
        "equity_curve": equity_curve, "trades": trades[-50:],
        "ran_at": datetime.now(timezone.utc).isoformat(),
    }
    STATE_DIR.mkdir(exist_ok=True)
    with BACKTEST_LOG.open("a") as f:
        f.write(json.dumps(result) + "\n")
    return result


# ---------------------------------------------------------------------------
# Metrics + buy-and-hold benchmark
# ---------------------------------------------------------------------------

# How many closed trades /api/metrics reads at most. Bounded on purpose — the
# ledger is append-only and grows for the life of the account.
MAX_LEDGER_READ = 5000
# Symbols priced for the benchmark. Each costs one candle API call, so this is
# capped exactly like _fetch_backtest_history.
BENCHMARK_MAX_SYMBOLS = 10
_benchmark_cache: dict[tuple, tuple[float, dict]] = {}
BENCHMARK_CACHE_SECONDS = 300.0


def _config_key_of(config: dict) -> str:
    """Grouping key from a stored fingerprint (trades outlive settings)."""
    if not config:
        return "unknown"
    return (f"{config.get('horizon', '?')}/{config.get('strategy', '?')}"
            f"/gate{config.get('min_confirmations', '?')}"
            f"/+{config.get('lock_profit_pct', 0):g}-{config.get('stop_loss_pct', 0):g}"
            f"/{config.get('sizing', '?')}/{config.get('universe', '?')}")


def _metric_float(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _window_start(window: str) -> datetime | None:
    """Start of the requested metrics window, or None for 'all'."""
    now = datetime.now(timezone.utc)
    if window == "session":
        if STATE.session_start_at is None:
            return now      # no session running → empty window
        try:
            return datetime.fromisoformat(STATE.session_start_at)
        except ValueError:
            return None
    if window == "day":
        return now - timedelta(days=1)
    if window == "week":
        return now - timedelta(days=7)
    return None


def _closed_trades(window: str) -> list[dict]:
    """Closed trades within the window, oldest first."""
    start = _window_start(window)
    trades: list[dict] = []
    for line in _tail_lines(TRADES_CLOSED_LOG, MAX_LEDGER_READ):
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if start is not None:
            try:
                if datetime.fromisoformat(record.get("closed_at", "")) < start:
                    continue
            except ValueError:
                continue
        trades.append(record)
    return trades


def _benchmark(window: str, trades: list[dict]) -> dict:
    """Equal-weight buy-and-hold return (%) for the symbols actually traded in
    the window, over the same period. Cached — the UI polls this endpoint."""
    symbols = list(dict.fromkeys(t.get("symbol", "") for t in trades if t.get("symbol")))
    if not symbols:
        return {"return_pct": 0.0, "symbols": [], "source": "no trades in window"}
    symbols = symbols[:BENCHMARK_MAX_SYMBOLS]

    key = (window, tuple(symbols))
    cached = _benchmark_cache.get(key)
    if cached and (time.monotonic() - cached[0]) < BENCHMARK_CACHE_SECONDS:
        return cached[1]

    start = _window_start(window)
    if start is None:
        # "all" — anchor on the earliest entry we have a record of
        stamps = [t.get("opened_at", "") for t in trades if t.get("opened_at")]
        if stamps:
            try:
                start = datetime.fromisoformat(min(stamps))
            except ValueError:
                start = None
    span_days = (datetime.now(timezone.utc) - start).total_seconds() / 86400 if start else 1.0
    period = "Day" if span_days > 5 else "Min_5"
    bars_needed = int(span_days * 24 * 12) + 5 if period == "Min_5" else int(span_days) + 5

    returns: dict[str, float] = {}
    for symbol in symbols:
        try:
            candles = STATE.paper_broker.candles(symbol, period=period,
                                                 count=max(20, min(1000, bars_needed)))
        except Exception:
            candles = []
        bars = [c for c in candles if c.get("close", 0) > 0]
        if len(bars) < 2:
            continue
        first = _first_bar_in_window(bars, start)
        if first["close"] > 0:
            returns[symbol] = (bars[-1]["close"] / first["close"] - 1.0) * 100

    if returns:
        result = {
            "return_pct": equal_weight_return(returns),
            "symbols": sorted(returns.keys()),
            "source": f"longbridge {period} candles",
        }
    else:
        # No candle data (sim mode, or Longbridge disconnected). Fall back to
        # what the ledger already knows: hold each symbol from its first entry
        # price to the latest price we have seen.
        result = _benchmark_from_ledger(trades, symbols)

    _benchmark_cache[key] = (time.monotonic(), result)
    return result


def _first_bar_in_window(bars: list[dict], start: datetime | None) -> dict:
    """First bar at/after `start`. Falls back to the oldest bar when the SDK
    gave us no timestamps — count-based selection already bounded the fetch."""
    if start is None:
        return bars[0]
    for bar in bars:
        stamp = bar.get("timestamp") or ""
        if not stamp:
            break
        try:
            when = datetime.fromisoformat(stamp)
        except ValueError:
            break
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        if when >= start:
            return bar
    return bars[0]


def _benchmark_from_ledger(trades: list[dict], symbols: list[str]) -> dict:
    """Candle-free fallback: equal-weight hold from each symbol's first entry
    price in the window to its latest known price."""
    last_prices = STATE.paper_broker.portfolio().last_prices
    returns: dict[str, float] = {}
    for symbol in symbols:
        entries = [t for t in trades if t.get("symbol") == symbol]
        entry_price = float(entries[0].get("entry_price", 0) or 0) if entries else 0.0
        current = float(last_prices.get(symbol, 0) or 0) or float(entries[-1].get("exit_price", 0) or 0)
        if entry_price > 0 and current > 0:
            returns[symbol] = (current / entry_price - 1.0) * 100
    return {
        "return_pct": equal_weight_return(returns),
        "symbols": sorted(returns.keys()),
        "source": "ledger prices (no candle data available)",
    }


# Hard ceiling on how many individual trades an API response may carry. The
# ledger is append-only and grows for the life of the account.
MAX_TRADES_RESPONSE = 200


def closed_trades_report(window: str = "all", limit: str = "50") -> dict:
    """Individual closed round trips, newest first.

    Aggregates answer "is this working"; this answers "which trades, and why
    did each one end" — the question you would otherwise open a JSONL for.
    """
    if window not in ("session", "day", "week", "all"):
        window = "all"
    try:
        count = max(1, min(MAX_TRADES_RESPONSE, int(limit)))
    except (TypeError, ValueError):
        count = 50
    trades = _closed_trades(window)
    return {
        "window": window,
        "total_in_window": len(trades),
        "returned": min(count, len(trades)),
        "trades": list(reversed(trades))[:count],
    }


def performance_report(window: str = "all", min_trades: int = 1) -> dict:
    """Configuration leaderboard: which setups produced which outcomes.

    Groups every closed trade by the configuration it was opened under, so the
    question "what works" is answered from recorded evidence rather than
    memory. Ranked by expectancy, but a config with three trades is noise —
    `sample_warning` is carried through per row and the UI must show it.
    """
    if window not in ("session", "day", "week", "all"):
        window = "all"
    trades = _closed_trades(window)
    rows = []
    for key, group in compute_metrics(trades).get("by_config", {}).items():
        if group["total_trades"] < max(1, min_trades):
            continue
        sample = next((t for t in trades if t.get("config_key") == key), {})
        rows.append({
            "config_key": key,
            "config": sample.get("config", {}),
            "trades": group["total_trades"],
            "expectancy": group["expectancy_per_trade"],
            "net_pnl": group["net_pnl"],
            "win_rate": group["win_rate"],
            "profit_factor": group["profit_factor"],
            "max_drawdown_pct": group["max_drawdown_pct"],
            "fees_as_pct_of_gross": group["fees_as_pct_of_gross"],
            "avg_hold_seconds": group["avg_hold_seconds"],
            "sample_warning": group["sample_warning"],
            "first_trade": min((t.get("opened_at", "") for t in trades
                                if t.get("config_key") == key), default=""),
            "last_trade": max((t.get("closed_at", "") for t in trades
                               if t.get("config_key") == key), default=""),
            "by_exit_reason": group.get("by_exit_reason", {}),
        })
    rows.sort(key=lambda r: r["expectancy"], reverse=True)
    return {
        "window": window,
        "configs": rows,
        "total_trades": len(trades),
        "distinct_configs": len(rows),
        "current_config_key": STATE.settings.config_key(),
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


def metrics_report(window: str = "session") -> dict:
    """Everything /api/metrics returns: metrics, benchmark, strategy return."""
    if window not in ("session", "day", "week", "all"):
        window = "session"
    trades = _closed_trades(window)
    # Starting equity is needed BEFORE the metrics: it is the basis for both
    # the strategy return and the drawdown percentage.
    net = sum(_metric_float(t.get("net_pnl")) for t in trades)
    if window == "session" and STATE.session_start_equity > 0:
        starting_equity = STATE.session_start_equity
        basis = "session start equity"
    else:
        # Best available proxy: back the window's realised P&L out of current
        # equity. Not exact when cash was added mid-window.
        starting_equity = STATE.paper_broker.portfolio().equity() - net
        basis = "current equity minus window P&L"

    report = compute_metrics(trades, max(0.0, starting_equity))
    benchmark = _benchmark(window, trades)
    strategy_return_pct = round(net / starting_equity * 100, 4) if starting_equity > 0 else 0.0

    start = _window_start(window)
    return {
        "window": window,
        "window_start": start.isoformat() if start else None,
        "metrics": report,
        "benchmark": benchmark,
        "strategy_return_pct": strategy_return_pct,
        "vs_benchmark_pct": round(strategy_return_pct - benchmark["return_pct"], 4),
        "return_basis": basis,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


# ---------------------------------------------------------------------------
# Live-mode hard limits
# ---------------------------------------------------------------------------
# Currencies whose balance this build can actually read and check an order
# against (see LongbridgeBroker.cash_by_currency). An order settling in any
# other currency is BLOCKED live: we cannot prove it is covered by cash, and an
# uncovered foreign-currency order is how a cash account quietly ends up
# borrowing. Adding a currency here means adding real balance handling for it.
LIVE_ENFORCED_CURRENCIES = {"USD", "SGD"}

# How long a synced live portfolio is reused before re-hitting the broker API.
# The guard runs per proposal; without this, one tick of 5 proposals would cost
# 10+ account calls and run into Longbridge's trade-API rate limit.
LIVE_SYNC_TTL_SECONDS = 3.0


class TradingEngine:
    # Quotes shown in the UI Market Scan panel. With full Longbridge discovery
    # the universe can exceed 30,000 symbols — serializing every quote into
    # every SSE broadcast made each payload several MB and was the main RAM/CPU
    # hog. Positions and ranked signals are always included; the rest fill up
    # to this cap.
    MAX_STATUS_QUOTES = 60
    # Ceiling applied when max_scan_symbols = 0 ("unlimited") — see
    # _resolve_universe for why a truly unlimited scan is not feasible.
    MAX_UNLIMITED_SCAN = 2000

    def _display_quotes(self, portfolio) -> list:
        quotes = STATE.last_quotes
        if len(quotes) <= self.MAX_STATUS_QUOTES:
            return quotes
        priority = {s for s, p in portfolio.positions.items() if p.quantity > 0}
        priority.update(sig.symbol for sig in STATE.signals)
        selected = [q for q in quotes if q.symbol in priority]
        for q in quotes:
            if len(selected) >= self.MAX_STATUS_QUOTES:
                break
            if q.symbol not in priority:
                selected.append(q)
        return selected

    def status(self) -> dict:
        from broker import LB_STATUS
        broker = STATE.paper_broker if STATE.settings.trading_mode is TradingMode.PAPER else (STATE.live_broker or STATE.paper_broker)
        portfolio = broker.portfolio()
        display_quotes = self._display_quotes(portfolio)
        # Trim portfolio.last_prices the same way — it mirrors the full scan
        # universe and would otherwise re-inflate every status payload.
        portfolio_json = to_json(portfolio)
        keep = {s for s, p in portfolio.positions.items() if p.quantity > 0}
        keep.update(q.symbol for q in display_quotes)
        portfolio_json["last_prices"] = {
            s: p for s, p in portfolio_json["last_prices"].items() if s in keep
        }
        return {
            "settings": to_json(STATE.settings),
            "portfolio": portfolio_json,
            "last_quote": to_json(STATE.last_quote),
            "last_quotes": to_json(display_quotes),
            "universe_source": STATE.universe_source,
            "signals": to_json(STATE.signals),
            "signals_updated_at": STATE.last_tick_at,
            "proposals": to_json(STATE.proposals[-20:]),
            "last_tick_at": STATE.last_tick_at,
            "tick_paused": STATE.tick_paused,
            "session_pnl": STATE.session_pnl(),
            "session_start_at": STATE.session_start_at,
            "lb_connected": LB_STATUS["connected"],
            "lb_error": LB_STATUS["error"],
            # None = gate inactive (sim mode trades 24/7); dict = per-market open flag
            "markets_open": markets_status(STATE.settings.markets) if LB_STATUS["connected"] else None,
            "ai_status": AI_STATUS.as_dict(),
            "viability": self.viability_summary(),
            "coverage": self._coverage_summary(),
            "premarket": {"watchlist": STATE.premarket_watchlist[:20],
                          "built_at": STATE.premarket_built_at,
                          "kind": STATE.watchlist_kind},
        }

    def tick(self) -> dict:
        from broker import LB_STATUS
        with STATE.lock:
            broker = STATE.broker()
            # Fresh tick: the exchange positions are about to be re-synced, so
            # last tick's in-flight reservations are either reflected there now
            # or were never filled.
            TradingEngine._live_pending_notional = 0.0
            TradingEngine._live_sync_at = 0.0
            # Market-hours gate — only with real data. Sim prices move 24/7,
            # so the simulator stays testable at any hour.
            gate = LB_STATUS["connected"]
            open_mkts = open_markets(STATE.settings.markets) if gate else list(STATE.settings.markets)

            if gate and not open_mkts:
                # Every selected market is closed: prices are frozen, so no
                # scanning, no AI calls, no proposals. Only the session clock
                # and proposal expiry keep running.
                STATE.last_tick_at = datetime.now(timezone.utc).isoformat()
                # Markets are shut, so no trading — but this is exactly when a
                # pre-market screen is useful: think while it is closed, act
                # mechanically once it opens.
                built = self._build_watchlist(broker)
                STATE.universe_source = built or "all selected markets closed — waiting for open"
                self._expire_stale_proposals()
                if STATE.settings.strategy_enabled and self._session_expired():
                    STATE.settings.strategy_enabled = False
                    STATE.close_session()   # can't flatten a closed market — positions stay
                STATE.audit(AuditEventType.TICK, detail={"markets_closed": True})
                STATE.save()
                return self.status()

            symbols = self._resolve_universe(broker)
            if gate:
                closed = {m for m in STATE.settings.markets if m not in open_mkts}
                if closed:
                    symbols = [s for s in symbols if market_of(s) not in closed]
            quotes = broker.quotes(symbols)
            STATE.last_quotes = quotes
            STATE.last_quote = quotes[0] if quotes else None
            STATE.last_tick_at = datetime.now(timezone.utc).isoformat()
            STATE.audit(AuditEventType.TICK, detail={"symbols_count": len(symbols), "quotes": len(quotes)})

            # Expire stale proposed entries in manual mode
            self._expire_stale_proposals()

            if STATE.settings.strategy_enabled and self._session_expired():
                STATE.settings.strategy_enabled = False
                STATE.close_session()
                if STATE.settings.stop_at_end:
                    self._close_positions_at_end(quotes)
            # Feed real 1-min candles for held positions + top candidates so
            # the strategy can use VWAP / EMA / RSI (no-op in pure sim mode)
            self._enrich_with_candles(broker, quotes)

            if STATE.settings.strategy_enabled:
                # ── Mechanical exits (profit lock / stop loss / trailing) ─────
                # Hard guarantees independent of AI judgment — run BEFORE the
                # AI scan so a breached threshold always exits this tick.
                lock_proposals = self._check_mechanical_exits(quotes)
                # Runs after the mechanical exits so a position already being
                # closed for a real reason is never also proposed for rotation
                # (the dedupe below drops the second proposal on the same
                # symbol+side, and the mechanical reason is the truthful one).
                lock_proposals += self._check_rotation(quotes)
                pending_locks = {(i.symbol, i.side) for i in STATE.proposals if i.status is OrderStatus.PROPOSED}
                for proposal in lock_proposals:
                    if (proposal.symbol, proposal.side) in pending_locks:
                        continue
                    STATE.proposals.append(proposal)
                    pending_locks.add((proposal.symbol, proposal.side))
                    STATE.audit(AuditEventType.PROPOSAL, symbol=proposal.symbol, side=proposal.side.value,
                                quantity=round(proposal.quantity, 6), price=proposal.price,
                                confidence=round(proposal.confidence, 3), source="profit_lock")
                    if STATE.settings.approval_mode is ApprovalMode.AUTO:
                        self.execute(proposal)

                signals, proposals = STATE.strategy.scan(
                    STATE.settings, quotes, self._tradable_view(broker.portfolio()))
                STATE.signals = signals
                for sig in signals:
                    STATE.audit(AuditEventType.SIGNAL, symbol=sig.symbol, action=sig.action, score=round(sig.score, 3), price=sig.price)
                pending = {(i.symbol, i.side) for i in STATE.proposals if i.status is OrderStatus.PROPOSED}
                for proposal in proposals:
                    if (proposal.symbol, proposal.side) in pending:
                        continue
                    STATE.proposals.append(proposal)
                    pending.add((proposal.symbol, proposal.side))
                    STATE.audit(AuditEventType.PROPOSAL, symbol=proposal.symbol, side=proposal.side.value, quantity=round(proposal.quantity, 6), price=proposal.price, confidence=round(proposal.confidence, 3))
                    if STATE.settings.approval_mode is ApprovalMode.AUTO:
                        self.execute(proposal)
            else:
                # Session not running — refresh signals for display only.
                # Never call the paid AI here; its proposals would be discarded.
                STATE.signals = STATE.strategy.scan_signals_only(
                    STATE.settings, quotes, ENGINE._tradable_view(broker.portfolio()))
            STATE.save()
            return self.status()

    def update_settings(self, payload: dict) -> dict:
        with STATE.lock:
            settings = STATE.settings
            was_enabled = settings.strategy_enabled
            # A horizon change swaps in that horizon's saved tuning. Values the
            # client sent alongside it belong to the OUTGOING horizon, so they
            # are ignored rather than written over the incoming profile.
            switching = ("trading_horizon" in payload
                         and payload["trading_horizon"] != settings.trading_horizon)
            if switching:
                settings.switch_horizon(payload["trading_horizon"])
            from models import HORIZON_FIELDS
            for key in ("symbol", "markets", "universe", "budget", "duration_minutes", "max_scan_symbols",
                        "max_loss", "max_trade_value", "auto_tick_enabled", "tick_interval_seconds",
                        "allow_live_trading", "stop_at_end", "strategy_enabled",
                        "target_profit", "target_profit_per_hour", "lock_profit_pct",
                        "stop_loss_pct", "trailing_stop_pct", "ai_strategy_name",
                        "enforce_trade_viability", "min_confirmations",
                        "risk_per_trade_pct", "atr_stop_multiple", "use_atr_sizing",
                        "max_hold_days", "max_hold_minutes", "breakeven_trigger_pct",
                        "exit_on_thesis_break", "allow_rotation", "rotation_score_gap",
                        "use_premarket_watchlist",
                        "premarket_watchlist_size",
                        "max_concurrent_positions", "daily_turnover_multiple",
                        "daily_loss_limit", "cooldown_after_losses",
                        "trading_horizon"):
                if switching and key in HORIZON_FIELDS:
                    continue
                if key in payload:
                    setattr(settings, key, payload[key])
            if "trading_mode" in payload:
                settings.trading_mode = TradingMode(payload["trading_mode"])
            if "approval_mode" in payload:
                settings.approval_mode = ApprovalMode(payload["approval_mode"])
            settings.normalized().sync_horizon_profile()
            # "Budget" reads as "the money I am trading with", but it is only
            # the STARTING cash used on reset — so changing it left a $0 paper
            # account that could never trade. Fund an untouched account
            # automatically; one that has traded is left alone, because
            # rewriting its cash would corrupt its P&L history.
            portfolio = STATE.paper_broker.portfolio()
            pristine = (portfolio.cash == 0 and portfolio.realized_pnl == 0
                        and not any(p.quantity > 0 for p in portfolio.positions.values()))
            if pristine and settings.budget > 0:
                portfolio.cash = settings.budget
                STATE.audit(AuditEventType.TICK, detail={
                    "action": "funded_paper_account_from_budget",
                    "cash": settings.budget})
            if settings.strategy_enabled and not was_enabled:
                settings.started_at = datetime.now(timezone.utc).isoformat()
                STATE.begin_session()
            if not settings.strategy_enabled and was_enabled:
                settings.started_at = None
                STATE.close_session()
            STATE.save()
            return self.status()

    def apply_recommended_defaults(self) -> dict:
        """Adopt the recommended profile for the CURRENT horizon.

        Defaults only apply to a fresh install, so a state file written before
        a default changed keeps the old value forever — which is how a saved
        `lock_profit_pct = 0` silently disabled the viability guard long after
        the shipped default became 0.8. This is the explicit opt-in to catch up.

        Deliberately does NOT touch budget, markets, universe, trading mode or
        approval mode: those are the user's own decisions, not tuning.
        """
        from models import HORIZON_DEFAULTS
        with STATE.lock:
            settings = STATE.settings
            recommended = HORIZON_DEFAULTS.get(settings.trading_horizon,
                                               HORIZON_DEFAULTS["intraday"])
            changed = {}
            # A profit goal is the user's own ambition, not tuning — leave it.
            preserve = {"target_profit", "target_profit_per_hour"}
            for field, value in recommended.items():
                if field in preserve:
                    continue
                if getattr(settings, field, None) != value:
                    changed[field] = (getattr(settings, field, None), value)
                    setattr(settings, field, value)
            # Not a horizon field, but the shipped default moved with them and
            # a stale small value is what makes trades unprofitable by
            # construction.
            fresh = Settings()
            for field in ("max_trade_value", "min_confirmations", "use_atr_sizing",
                          "max_concurrent_positions", "cooldown_after_losses",
                          "daily_turnover_multiple",
                          # Rotation resets to OFF: it is the one exit here
                          # whose benefit is unmeasured, so "recommended"
                          # must not quietly switch it on.
                          "exit_on_thesis_break", "allow_rotation", "rotation_score_gap",
                          "enforce_trade_viability", "use_premarket_watchlist"):
                value = getattr(fresh, field)
                if getattr(settings, field, None) != value:
                    changed[field] = (getattr(settings, field, None), value)
                    setattr(settings, field, value)
            settings.normalized().sync_horizon_profile()
            STATE.audit(AuditEventType.TICK, detail={
                "action": "apply_recommended_defaults",
                "horizon": settings.trading_horizon,
                "changed": {k: f"{a} -> {b}" for k, (a, b) in changed.items()},
            })
            STATE.save()
            result = self.status()
            result["defaults_applied"] = {k: {"from": a, "to": b}
                                          for k, (a, b) in changed.items()}
            return result

    def reset_paper(self, starting_cash: float | None = None) -> dict:
        with STATE.lock:
            cash = starting_cash if starting_cash is not None else STATE.settings.budget
            STATE.close_session()
            STATE.paper_broker = PaperBroker(starting_cash=cash)
            STATE.proposals = []
            STATE.signals = []
            STATE.last_quotes = []
            STATE.last_quote = None
            STATE.strategy = AIStrategy()
            STATE.settings.strategy_enabled = False
            STATE.settings.started_at = None
            STATE.save()
            STATE.audit(AuditEventType.TICK, detail={"action": "paper_reset", "starting_cash": cash})
            return self.status()

    def pause_tick(self) -> dict:
        with STATE.lock:
            STATE.tick_paused = True
            STATE.save()
            return self.status()

    def resume_tick(self) -> dict:
        with STATE.lock:
            STATE.tick_paused = False
            STATE.save()
            return self.status()

    def approve(self, proposal_id: str) -> bool:
        with STATE.lock:
            proposal = self._find_proposal(proposal_id)
            if proposal is None:
                return False
            STATE.audit(AuditEventType.APPROVE, symbol=proposal.symbol, proposal_id=proposal_id)
            self.execute(proposal)
            STATE.save()
            return True

    def reject(self, proposal_id: str) -> bool:
        with STATE.lock:
            proposal = self._find_proposal(proposal_id)
            if proposal is None:
                return False
            proposal.status = OrderStatus.REJECTED
            STATE.audit(AuditEventType.REJECT, symbol=proposal.symbol, proposal_id=proposal_id)
            STATE.save()
            return True

    # ── Live-mode ownership + budget ceiling ─────────────────────────────
    # In LIVE mode the portfolio is synced from the exchange, so it contains
    # whatever else is in the account. Two rules follow, and neither can be
    # overridden by settings:
    #   1. The tool only ever sells what the tool itself bought.
    #   2. The tool never deploys more than `settings.budget` at cost.
    # Both are enforced at execute(), the single chokepoint every order passes
    # through, so mechanical exits, the strategy, the AI and manual approval
    # are all covered by the same code.
    #
    # PAPER mode is deliberately exempt: the paper account is entirely the
    # tool's, its starting cash already IS the budget, and applying a second
    # ceiling there would change paper behaviour and invalidate a baseline
    # measured before this change.

    # Time source for the day-and-cooldown logic in risk.py. Normally wall
    # clock; the replay harness overrides it with the current bar's timestamp
    # so a year of history rolls days properly and a 30-minute cooldown does
    # not swallow an entire run that completes in seconds.
    simulated_now: "datetime | None" = None

    @classmethod
    def _now(cls) -> datetime:
        return cls.simulated_now or datetime.now(timezone.utc)

    _live_sync_at: float = 0.0
    # Notional submitted live this tick but not yet visible in the synced
    # exchange positions. Without it, several buys in one tick each see the
    # same "deployed" figure and can collectively overshoot the budget.
    _live_pending_notional: float = 0.0

    def _live_portfolio(self):
        """Exchange-synced portfolio, re-fetched at most every few seconds."""
        broker = STATE.broker()
        now = time.monotonic()
        if now - self._live_sync_at >= LIVE_SYNC_TTL_SECONDS:
            self._live_sync_at = now
            return broker.portfolio()
        return getattr(broker, "_portfolio", None) or broker.portfolio()

    def _tool_positions(self, portfolio) -> dict:
        """Positions the tool itself opened.

        `entry_qty` is only ever set by one of our own buy fills, so a synced
        exchange position the tool never touched has entry_qty == 0.
        """
        if STATE.settings.trading_mode is TradingMode.PAPER:
            return dict(portfolio.positions)
        return {s: p for s, p in portfolio.positions.items() if p.entry_qty > 0}

    def _deployed_cost_basis(self, portfolio) -> float:
        """What the tool currently has deployed, at cost."""
        total = 0.0
        for position in self._tool_positions(portfolio).values():
            if position.quantity <= 0:
                continue
            basis = position.entry_price if position.entry_price > 0 else position.avg_cost
            total += basis * position.quantity
        return round(total, 2)

    def _budget_room(self, portfolio) -> float:
        """Budget still available to deploy, at cost, including in-flight buys.

        Order notionals are counted at face value in their own currency. With
        SGD worth less than USD that is conservative — it exhausts the budget
        sooner than a true FX conversion would, never later.
        """
        budget = STATE.settings.budget
        if budget <= 0:
            return 0.0
        used = self._deployed_cost_basis(portfolio) + self._live_pending_notional
        return round(max(0.0, budget - used), 2)

    # ── Trade viability ──────────────────────────────────────────────────
    # A position too small to clear its own brokerage costs loses money even
    # when it reaches its profit target. That is arithmetic, not strategy, and
    # it is checked before the order is sent — in BOTH paper and live, because
    # a structurally-losing paper trade corrupts the baseline being measured.

    def _slippage_bps(self) -> float:
        from broker import PAPER_SLIPPAGE_BPS
        return PAPER_SLIPPAGE_BPS

    def _reference_price(self) -> float:
        """A representative price for settings-level estimates, taken from live
        quotes when there are any so the figure reflects what is actually being
        scanned rather than an invented number."""
        from fees import REFERENCE_PRICE
        prices = sorted(q.price for q in STATE.last_quotes if q.price > 0)
        if not prices:
            return REFERENCE_PRICE
        return prices[len(prices) // 2]

    def viability_summary(self) -> dict:
        """Are the CURRENT settings capable of making money? Surfaced in the
        status payload and the startup banner — the question should be
        answerable before trading, not after."""
        from fees import assess_trade
        settings = STATE.settings
        # The size that will actually be traded, not the configured ceiling:
        # sizing clamps every position to a fraction of available cash, so a
        # $2,500 cap on a $250 account buys $60 of stock. Judging viability on
        # the ceiling would report "fine" for trades that can never happen.
        from sizing import cash_fraction_for
        cash = STATE.paper_broker.portfolio().equity() or settings.budget
        affordable = cash * cash_fraction_for(settings.max_concurrent_positions)
        notional = min(settings.max_trade_value, affordable) if affordable > 0 \
            else settings.max_trade_value
        price = self._reference_price()
        markets = settings.markets or ["US"]
        per_market = {}
        for market in markets:
            verdict = assess_trade(market, notional, price,
                                   settings.lock_profit_pct, self._slippage_bps())
            per_market[market] = verdict.as_dict()
        # "Raise trade size to $574" is useless advice if 25% of your cash is
        # $250 — the real fix is more capital or a bigger target. Flag which.
        for verdict in per_market.values():
            floor = verdict.get("min_viable_notional", 0.0)
            verdict["reachable"] = bool(floor and affordable > 0 and floor <= affordable)
            verdict["affordable_notional"] = round(affordable, 2)
        assessable = [v for v in per_market.values() if v["assessable"]]
        return {
            "per_market": per_market,
            "reference_price": round(price, 2),
            "trade_value": round(notional, 2),
            "trade_value_capped_by_cash": notional < settings.max_trade_value,
            # So the figure is traceable rather than looking invented.
            "sizing_basis": {
                "equity": round(cash, 2),
                "cash_fraction": cash_fraction_for(settings.max_concurrent_positions),
                "max_positions": settings.max_concurrent_positions,
                "max_trade_value": settings.max_trade_value,
                "account_cash": round(STATE.paper_broker.portfolio().cash, 2),
            },
            "target_pct": settings.lock_profit_pct,
            "enforced": settings.enforce_trade_viability,
            # Any market that cannot pay for itself is worth shouting about.
            "any_unviable": any(not v["viable"] for v in assessable),
        }

    def _viability_denial(self, proposal: OrderProposal) -> str | None:
        """Reason to refuse this buy on cost grounds, or None."""
        if not STATE.settings.enforce_trade_viability:
            return None
        from fees import assess_trade
        notional = proposal.quantity * proposal.price
        if notional <= 0 or proposal.price <= 0:
            return None
        verdict = assess_trade(market_of(proposal.symbol), notional, proposal.price,
                               STATE.settings.lock_profit_pct, self._slippage_bps())
        if not verdict.assessable or verdict.viable:
            return None
        return (f"BLOCKED (unprofitable by construction): {proposal.symbol} at "
                f"${notional:,.2f} — {verdict.reason}")

    def _tradable_view(self, portfolio):
        """What the strategy and the AI are allowed to see and act on.

        In live mode this hides positions the tool did not open and caps cash
        at the remaining budget, so no sell proposal is ever generated for
        someone else's shares and no buy is sized against money the tool is
        not allowed to spend. execute() re-checks both — this just stops the
        proposals being created in the first place.
        """
        if STATE.settings.trading_mode is TradingMode.PAPER:
            return portfolio
        return Portfolio(
            cash=min(portfolio.cash, self._budget_room(portfolio)),
            realized_pnl=portfolio.realized_pnl,
            positions=self._tool_positions(portfolio),
            last_prices=portfolio.last_prices,
        )

    def _live_guard(self, proposal: OrderProposal) -> str | None:
        """Reason to refuse this live order, or None to let it through.
        May shrink the quantity to fit the remaining budget."""
        settings = STATE.settings
        currency = currency_of(proposal.symbol)
        if currency not in LIVE_ENFORCED_CURRENCIES:
            return (f"BLOCKED: {proposal.symbol} settles in "
                    f"{currency or 'an unrecognised currency'}. Live orders are limited to "
                    f"{'/'.join(sorted(LIVE_ENFORCED_CURRENCIES))} — the balance for anything "
                    "else cannot be verified, so the order cannot be proven cash-covered.")

        portfolio = self._live_portfolio()
        position = portfolio.positions.get(proposal.symbol)

        if proposal.side is Side.SELL:
            if position is None or position.entry_qty <= 0:
                return ("BLOCKED: this position was not opened by the tool. It never sells "
                        "shares it did not buy.")
            if proposal.quantity > position.quantity + 1e-9:
                return (f"BLOCKED: tried to sell {proposal.quantity:g} but the tool only holds "
                        f"{position.quantity:g}.")
            return None

        # ── BUY ──────────────────────────────────────────────────────────
        if settings.budget <= 0:
            return "BLOCKED: budget is 0. Set a budget before enabling live trading."
        if proposal.price <= 0:
            return "BLOCKED: no valid price for this symbol."

        room = self._budget_room(portfolio)
        if room <= 0:
            return (f"BLOCKED: budget ${settings.budget:,.2f} is fully deployed "
                    f"(${self._deployed_cost_basis(portfolio):,.2f} at cost). "
                    "No new buys until something is sold.")

        notional = proposal.quantity * proposal.price
        if notional > room:
            fitted = int(room / proposal.price)     # whole shares, always rounds down
            if fitted <= 0:
                return (f"BLOCKED: only ${room:,.2f} of budget left — not enough for one share "
                        f"of {proposal.symbol} at ${proposal.price:,.2f}.")
            proposal.quantity = float(fitted)
            notional = fitted * proposal.price
            proposal.reason += f" [trimmed to ${notional:,.2f} to stay inside the budget]"

        # Second, independent bound: real balance in the order's own currency.
        broker = STATE.broker()
        available = 0.0
        if hasattr(broker, "cash_by_currency"):
            available = broker.cash_by_currency().get(currency, 0.0)
        if notional > available:
            return (f"BLOCKED: order needs {currency} {notional:,.2f} but only "
                    f"{currency} {available:,.2f} is available. No borrowing.")
        return None

    def execute(self, proposal: OrderProposal) -> None:
        # Hard guard: never trade options or on margin.
        # We only allow plain BUY (cash) or SELL (held position).
        # This cannot be overridden by settings.
        if proposal.side not in (Side.BUY, Side.SELL):
            proposal.status = OrderStatus.FAILED
            proposal.error = "Rejected: only plain BUY/SELL of owned shares is permitted. No margin, no options."
            return

        # Portfolio-level limits: concentration, daily budget, daily loss,
        # loss-streak cooldown. Buys only — an exit must always be possible.
        if proposal.side is Side.BUY:
            portfolio = STATE.broker().portfolio()
            open_count = sum(1 for p in self._tool_positions(portfolio).values()
                             if p.quantity > 0)
            denial = check_limits(STATE.settings, open_count, STATE.risk_state,
                                  proposal.symbol, proposal.quantity * proposal.price,
                                  self._now())
            if denial is not None:
                proposal.status = OrderStatus.FAILED
                proposal.error = denial
                STATE.audit(AuditEventType.FAIL, symbol=proposal.symbol,
                            side=proposal.side.value, guard="portfolio_limits",
                            error=denial)
                return

        # Cost floor: never open a position that loses money when it wins.
        # Applies in paper and live alike.
        if proposal.side is Side.BUY:
            denial = self._viability_denial(proposal)
            if denial is not None:
                proposal.status = OrderStatus.FAILED
                proposal.error = denial
                STATE.audit(AuditEventType.FAIL, symbol=proposal.symbol,
                            side=proposal.side.value, guard="trade_viability",
                            error=denial)
                return

        # Live-only ownership + budget ceiling. Runs before anything is sent.
        if STATE.settings.trading_mode is TradingMode.LIVE:
            denial = self._live_guard(proposal)
            if denial is not None:
                proposal.status = OrderStatus.FAILED
                proposal.error = denial
                STATE.audit(AuditEventType.FAIL, symbol=proposal.symbol,
                            side=proposal.side.value, guard="live_limits", error=denial)
                return

        proposal.status = OrderStatus.APPROVED
        try:
            if STATE.settings.trading_mode is TradingMode.LIVE:
                if not STATE.settings.allow_live_trading:
                    raise RuntimeError("Live order submission is disabled.")
                if not TRADE_RATE_LIMITER.acquire(timeout=5.0):
                    raise RuntimeError("Trade rate limit exceeded — try again shortly.")
            STATE.broker().submit_order(proposal)
            if STATE.settings.trading_mode is TradingMode.LIVE:
                # The live broker sets the real outcome itself after polling
                # the exchange: FILLED (confirmed), FAILED (rejected), or
                # APPROVED with an explanatory error (accepted, fill pending).
                pass
            else:
                proposal.status = OrderStatus.FILLED if proposal.error is None else OrderStatus.FAILED
        except Exception as exc:
            proposal.status = OrderStatus.FAILED
            proposal.error = str(exc)
        # Anything that reached the exchange consumes budget from this moment,
        # even if the fill is still pending — otherwise the next proposal in
        # the same tick sees stale "deployed" and the pair overshoots.
        if (STATE.settings.trading_mode is TradingMode.LIVE
                and proposal.side is Side.BUY
                and proposal.status in (OrderStatus.FILLED, OrderStatus.APPROVED)):
            TradingEngine._live_pending_notional += proposal.quantity * proposal.price

        if proposal.status is OrderStatus.FILLED:
            if proposal.side is Side.BUY:
                # Counts toward the daily budget at fill time. Cumulative, so
                # recycling the same capital consumes the allowance again.
                STATE.risk_state.record_buy(proposal.symbol,
                                            proposal.quantity * proposal.price,
                                            self._now())
            self._record_round_trip(proposal)
            event = AuditEventType.FILL
        elif proposal.status is OrderStatus.APPROVED:
            event = AuditEventType.PROPOSAL   # live order pending on exchange
        else:
            event = AuditEventType.FAIL
        STATE.audit(event, symbol=proposal.symbol, side=proposal.side.value, quantity=round(proposal.quantity, 6), price=proposal.price, error=proposal.error)
        if STATE.settings.trading_mode is TradingMode.PAPER:
            STATE.log_trade(proposal)

    # ── Round-trip ledger ────────────────────────────────────────────────
    # A "trade" is a round trip: entry fill(s) → exit fill(s) for one symbol.
    # trade_log.jsonl records individual fills and cannot answer "did that
    # trade make money"; trades_closed.jsonl can.

    def _entry_diagnostics(self, symbol: str) -> tuple[float, dict]:
        """(score, diagnostics subset) from the current scan for this symbol.
        Only the five fields worth correlating against outcomes — never the
        whole Diagnostics object, and never anything universe-sized."""
        signal = next((s for s in STATE.signals if s.symbol == symbol), None)
        if signal is None:
            return 0.0, {}
        diag = signal.diagnostics
        if diag is None:
            return signal.score, {}
        return signal.score, {
            "rsi": diag.rsi,
            "vwap_dist_pct": diag.vwap_dist_pct,
            "ema_trend": diag.ema_trend,
            "vol_surge": diag.vol_surge,
            "day_change_pct": diag.day_change_pct,
            "atr": diag.atr,
            "atr_pct": diag.atr_pct,
        }

    def _record_round_trip(self, proposal: OrderProposal) -> None:
        """Called after every confirmed fill. Stamps entry context on a BUY,
        and on the SELL that takes the position flat emits one closed-trade
        record. Partial exits just accumulate on the Position."""
        try:
            portfolio = STATE.broker().portfolio()
            position = portfolio.positions.get(proposal.symbol)
            mode = STATE.settings.trading_mode.value
            if position is None:
                if proposal.side is not Side.BUY:
                    return
                # Live buy the exchange sync hasn't caught up with yet. Create
                # the local record now: it is what marks the position as
                # tool-owned, and without it the position would be invisible to
                # both the ownership rule and the budget ceiling.
                position = Position(symbol=proposal.symbol)
                portfolio.positions[proposal.symbol] = position

            if proposal.side is Side.BUY:
                if not position.entry_strategy:
                    score, diag = self._entry_diagnostics(proposal.symbol)
                    position.entry_score = score if score else proposal.confidence
                    position.entry_diagnostics = diag
                    position.entry_strategy = STATE.settings.ai_strategy_name
                    position.entry_mode = mode
                    position.entry_config = STATE.settings.config_fingerprint()
                    # The stated reason for owning this, captured once on the
                    # opening fill. Adding to a position must not rewrite the
                    # thesis it was opened on — otherwise a top-up during a
                    # deteriorating setup would quietly relabel the trade as
                    # justified by whatever happens to be true at that moment.
                    signal = next((s for s in STATE.signals
                                   if s.symbol == proposal.symbol), None)
                    position.entry_confirmations = list(
                        getattr(signal, "confirmations", []) or [])
                # Fix the stop at entry from the symbol's own volatility, so a
                # quiet name and a violent one don't get the same 2% leash.
                # Only on the opening fill — adding to a position must not
                # loosen the stop that is already protecting it.
                if position.stop_price <= 0:
                    entry_atr = float(position.entry_diagnostics.get("atr", 0) or 0)
                    if entry_atr > 0:
                        distance = entry_atr * STATE.settings.atr_stop_multiple
                        position.stop_price = round(max(0.0, proposal.price - distance), 6)
                if not position.opened_at:
                    # Engine-owned so the replay's simulated clock applies;
                    # wall clock in production.
                    position.opened_at = self._now().isoformat()
                if position.entry_qty <= 0:
                    position.entry_qty = proposal.quantity
                    position.entry_price = proposal.price
                if mode != TradingMode.PAPER.value:
                    # PaperBroker accumulates its own fee; live has to take the
                    # real charge off the fill.
                    position.fees_paid = round(position.fees_paid + proposal.fee, 6)
                return

            # ── SELL ──────────────────────────────────────────────────────
            if mode != TradingMode.PAPER.value:
                # Live sells never touched PaperBroker's accumulators.
                position.exit_qty = round(position.exit_qty + proposal.quantity, 6)
                position.exit_proceeds = round(
                    position.exit_proceeds + proposal.quantity * proposal.price, 6)
                position.fees_paid = round(position.fees_paid + proposal.fee, 6)
            if position.quantity > 1e-9 or position.entry_qty <= 0:
                return   # partial exit, or nothing was ever recorded as opened

            entry_notional = position.entry_price * position.entry_qty
            gross_pnl = position.exit_proceeds - entry_notional
            fees = position.fees_paid
            hold_seconds = 0.0
            if position.opened_at:
                try:
                    opened = datetime.fromisoformat(position.opened_at)
                    hold_seconds = round((self._now() - opened).total_seconds(), 2)
                except ValueError:
                    hold_seconds = 0.0
            exit_price = (position.exit_proceeds / position.exit_qty
                          if position.exit_qty > 0 else proposal.price)
            record = {
                "symbol": proposal.symbol,
                "opened_at": position.opened_at,
                "closed_at": self._now().isoformat(),
                "hold_seconds": hold_seconds,
                "entry_price": round(position.entry_price, 6),
                "exit_price": round(exit_price, 6),
                "quantity": round(position.entry_qty, 6),
                "gross_pnl": round(gross_pnl, 6),
                "fees": round(fees, 6),
                "net_pnl": round(gross_pnl - fees, 6),
                "return_pct": round(gross_pnl / entry_notional * 100, 6) if entry_notional else 0.0,
                "exit_reason": proposal.tag or "manual",
                "strategy": position.entry_strategy or STATE.settings.ai_strategy_name,
                "mode": position.entry_mode or mode,
                "entry_score": round(position.entry_score, 4),
                "entry_diagnostics": position.entry_diagnostics,
                # What configuration produced this trade — the basis for
                # comparing setups against each other later.
                "config": position.entry_config,
                "config_key": _config_key_of(position.entry_config),
                # Where the fee figure came from, so net_pnl is never read with
                # more confidence than it deserves:
                #   actual   — billed by the broker (charge_detail)
                #   modelled — computed from fees.py (paper)
                #   unknown  — live fill the broker reported no charges for
                "fees_source": (
                    "modelled" if position.entry_mode == TradingMode.PAPER.value
                    else ("actual" if fees > 0 else "unknown")
                ),
            }
            STATE.log_closed_trade(record)
            # Feeds the daily loss limit and the consecutive-loss cooldown.
            STATE.risk_state.record_close(proposal.symbol, record["net_pnl"],
                                          STATE.settings.cooldown_after_losses,
                                          self._now())
            position.reset_round_trip()
        except Exception as exc:
            # The ledger must never be able to break order execution.
            STATE.audit(AuditEventType.FAIL, symbol=proposal.symbol,
                        error=f"closed-trade record failed: {exc}")

    def _expire_stale_proposals(self) -> None:
        """In manual mode, auto-expire proposals older than PROPOSAL_TTL_SECONDS."""
        if STATE.settings.approval_mode is ApprovalMode.AUTO:
            return
        now = datetime.now(timezone.utc)
        for proposal in STATE.proposals:
            if proposal.status is not OrderStatus.PROPOSED:
                continue
            try:
                age = (now - datetime.fromisoformat(proposal.created_at)).total_seconds()
            except Exception:
                continue
            if age > proposal_ttl_seconds(STATE.settings.tick_interval_seconds):
                proposal.status = OrderStatus.REJECTED
                proposal.error = f"Auto-expired after {int(age)}s (manual approval timeout)."
                STATE.audit(AuditEventType.REJECT, symbol=proposal.symbol,
                            proposal_id=proposal.id, reason="TTL expired")

    def _session_expired(self) -> bool:
        if STATE.settings.started_at is None:
            STATE.settings.started_at = datetime.now(timezone.utc).isoformat()
            return False
        if STATE.settings.is_swing:
            # Swing sessions span days by definition, so a duration timer makes
            # no sense — and letting it fire would hand stop_at_end a mandate to
            # flatten multi-day positions at an arbitrary moment. End a swing
            # session manually.
            return False
        started = datetime.fromisoformat(STATE.settings.started_at)
        return datetime.now(timezone.utc) >= started + timedelta(minutes=STATE.settings.duration_minutes)

    # ── Pre-market watchlist ─────────────────────────────────────────────
    # How close to the bell the screen becomes worth running. Earlier than this
    # the pre-market book is empty or stale; the point is to read TODAY's
    # prints, not last night's.
    PREMARKET_WINDOW_MINUTES = 150
    # Screening the full working set would cost hundreds of quote calls for
    # data most of which has no pre-market print at all.
    PREMARKET_SCAN_CAP = 400

    # Daily candles for a leader screen cost one call each, so the swing
    # screen looks at a shortlist rather than the whole working set.
    LEADER_SCAN_CAP = 120

    def _build_watchlist(self, broker) -> str:
        """Narrow the universe before trading starts, using the metric that
        matches the horizon. Returns a status string, or "" to leave the
        caller's message alone."""
        settings = STATE.settings
        if not settings.use_premarket_watchlist or settings.premarket_watchlist_size <= 0:
            return ""
        if self._watchlist_is_fresh():
            return ""                      # already built for this session
        if settings.is_swing:
            return self._build_leader_watchlist(broker)
        return self._build_gapper_watchlist(broker)

    def _build_leader_watchlist(self, broker) -> str:
        """Swing: rank by strength over the SAME window the signal engine
        measures on. A one-session gap is noise at a multi-day scale, and gaps
        fade — selecting on one and then holding for days is a horizon
        mismatch."""
        from strategy import compute_indicators
        settings = STATE.settings
        candidates = self._resolve_universe(broker)[: self.LEADER_SCAN_CAP]
        if not candidates:
            return ""
        period, count, _, _ = self.CANDLE_SPEC["swing"]
        indicators: dict = {}
        for symbol in candidates:
            try:
                candles = broker.candles(symbol, period=period, count=min(count, 60))
            except Exception:
                continue
            if len(candles) < 21:
                continue
            ind = compute_indicators(candles)
            if ind:
                indicators[symbol] = (candles[-1].get("close", 0.0), ind)
        leaders = rank_momentum_leaders(indicators, limit=settings.premarket_watchlist_size)
        if not leaders:
            return ""
        STATE.premarket_watchlist = [l.as_dict() for l in leaders]
        STATE.premarket_built_at = self._now().isoformat()
        STATE.watchlist_kind = "leaders"
        top = ", ".join(f"{l.symbol} {l.change_pct:+.0f}%" for l in leaders[:3])
        return (f"swing leaders watchlist: {len(leaders)} names by 20-day strength "
                f"— top: {top}")

    def _build_gapper_watchlist(self, broker) -> str:
        """Intraday: rank tomorrow's candidates by pre-market gap."""
        settings = STATE.settings
        soon = [m for m in settings.markets
                if 0 < minutes_until_open(m) <= self.PREMARKET_WINDOW_MINUTES]
        if not soon:
            return ""
        candidates = [s for s in self._resolve_universe(broker)
                      if market_of(s) in soon][: self.PREMARKET_SCAN_CAP]
        if not candidates:
            return ""
        try:
            quotes = broker.quotes(candidates)
        except Exception:
            return ""
        if not has_premarket_data(quotes):
            # No pre-open session, or nothing has traded yet. Falling back is
            # correct; an empty watchlist would mean scanning nothing at all.
            return ""
        gappers = rank_gappers(quotes, limit=settings.premarket_watchlist_size)
        if not gappers:
            return ""
        STATE.premarket_watchlist = [g.as_dict() for g in gappers]
        STATE.premarket_built_at = self._now().isoformat()
        STATE.watchlist_kind = "gappers"
        top = ", ".join(f"{g.symbol} {g.gap_pct:+.1f}%" for g in gappers[:3])
        return (f"pre-market watchlist: {len(gappers)} gappers for "
                f"{'/'.join(soon)} — top: {top}")

    def _resolve_universe(self, broker) -> list[str]:
        # 0. The pre-market watchlist SEEDS the pool, it does not replace it.
        #    Replacing froze the universe at 20 names chosen before the bell, so
        #    a stock that broke out at 11am with no pre-market gap could never
        #    be seen — while the ranking that picks candidates re-runs every
        #    tick and had nothing new to look at. Widening costs nothing: 20
        #    and 200 symbols are the same single quote call.
        #    Watchlist names go first so they survive the cap.
        watchlist_seed: list[str] = []
        if (STATE.settings.use_premarket_watchlist and STATE.premarket_watchlist
                and not STATE.settings.universe and self._watchlist_is_fresh()):
            watchlist_seed = [g["symbol"] for g in STATE.premarket_watchlist]

        # 1. User-defined custom universe always wins
        if STATE.settings.universe:
            STATE.universe_source = "custom"
            return STATE.settings.active_universe()

        # 2. Try to discover from Longbridge — cached for 30 min to avoid
        #    hammering the API on every tick
        markets = STATE.settings.markets
        cache_age = time.monotonic() - STATE._symbol_cache_at
        cache_stale = cache_age > 1800 or STATE._symbol_cache_markets != markets

        if cache_stale:
            try:
                discovered = broker.discover_symbols(markets)
            except Exception:
                discovered = []
            if discovered:
                if len(discovered) > self.MAX_UNLIMITED_SCAN:
                    # Rank the whole market by traded value so the working
                    # set is the LIQUID, ACTIVE slice — not an alphabetical
                    # truncation full of illiquid names. One bulk pass per
                    # 30-min cache refresh.
                    discovered = self._rank_by_liquidity(broker, discovered)
                STATE._symbol_cache = discovered
                STATE._symbol_cache_markets = list(markets)
                STATE._symbol_cache_at = time.monotonic()

        if STATE._symbol_cache:
            # normalized() guarantees a positive, bounded value — the old
            # "0 = unlimited" sentinel is gone.
            cap = min(STATE.settings.max_scan_symbols, self.MAX_UNLIMITED_SCAN)
            scanned = STATE._symbol_cache[:cap]
            if watchlist_seed:
                merged = list(dict.fromkeys(watchlist_seed + scanned))[:cap]
                STATE.universe_source = (
                    f"{len(watchlist_seed)} {STATE.watchlist_kind} (built "
                    f"{STATE.premarket_built_at[11:16]} UTC) + {len(merged) - len(watchlist_seed)} "
                    f"ranked \u2014 re-ranked every scan")
                return merged
            STATE.universe_source = f"Longbridge discovery cache: {len(STATE._symbol_cache)} found, scanning {len(scanned)}"
            return scanned

        # 3. Fall back to expanded DEFAULT_UNIVERSES sample list
        fallback = STATE.settings.active_universe()
        if watchlist_seed:
            merged = list(dict.fromkeys(watchlist_seed + fallback))[:STATE.settings.max_scan_symbols]
            STATE.universe_source = (f"{len(watchlist_seed)} {STATE.watchlist_kind} + "
                                     f"{len(merged) - len(watchlist_seed)} sample \u2014 re-ranked every scan")
            return merged
        STATE.universe_source = "sample fallback (set Longbridge credentials for full scan)"
        return fallback

    # A watchlist is only good for the session it was built for.
    WATCHLIST_TTL_HOURS = 12

    def _watchlist_is_fresh(self) -> bool:
        if not STATE.premarket_built_at:
            return False
        try:
            built = datetime.fromisoformat(STATE.premarket_built_at)
        except ValueError:
            return False
        return (self._now() - built).total_seconds() < self.WATCHLIST_TTL_HOURS * 3600

    # Bulk-rank at most this many discovered symbols (50 batches of 200);
    # beyond that the one-off ranking pass itself would take too long.
    MAX_RANK_PASS = 10_000

    def _rank_by_liquidity(self, broker, symbols: list[str]) -> list[str]:
        """One bulk quote pass over the discovered universe, ranked by today's
        turnover (traded value), but bucketed PER MARKET so every selected
        market keeps a slice of the working set.

        A single global turnover sort is dominated by US names (their traded
        value is orders of magnitude larger than HK/SG), which starves the
        Asia markets entirely. The tool would then scan NOTHING whenever US is
        closed but HK/SG are open — exactly the "no ticks, no scan" symptom.
        Returns at most MAX_UNLIMITED_SCAN symbols, allocated across markets."""
        buckets: dict[str, list[str]] = {}
        for s in symbols:
            buckets.setdefault(market_of(s), []).append(s)

        ranked_by_market: dict[str, list[str]] = {}
        for mkt, syms in buckets.items():
            try:
                quotes = broker.quotes(syms[: self.MAX_RANK_PASS])
            except Exception:
                quotes = []
            if quotes and any(q.turnover > 0 for q in quotes):
                quotes.sort(key=lambda q: q.turnover, reverse=True)
                ranked_by_market[mkt] = [q.symbol for q in quotes]
            else:
                ranked_by_market[mkt] = list(syms)  # no turnover data — keep order

        # Give each market an equal share of the cap first (guarantees Asia
        # names survive), then let the remaining capacity fill from whatever is
        # left — in practice the big US bucket absorbs it.
        cap = self.MAX_UNLIMITED_SCAN
        markets = list(ranked_by_market.keys())
        if not markets:
            return symbols[:cap]
        share = max(1, cap // len(markets))
        result: list[str] = []
        leftovers: list[str] = []
        for mkt in markets:
            lst = ranked_by_market[mkt]
            result.extend(lst[:share])
            leftovers.extend(lst[share:])
        remaining = cap - len(result)
        if remaining > 0:
            result.extend(leftovers[:remaining])
        return result[:cap]

    def _close_positions_at_end(self, quotes) -> None:
        portfolio = STATE.broker().portfolio()
        latest = {q.symbol: q for q in quotes}
        # Stop-at-end flattens only what the tool opened.
        for symbol, position in self._tool_positions(portfolio).items():
            if position.quantity <= 0 or symbol not in latest:
                continue
            quote = latest[symbol]
            proposal = OrderProposal(symbol=quote.symbol, side=Side.SELL, quantity=position.quantity,
                                     price=quote.price, confidence=1.0, tag="session_end",
                                     reason="Trading duration ended — stop-at-end enabled.")
            STATE.proposals.append(proposal)
            if STATE.settings.approval_mode is ApprovalMode.AUTO:
                self.execute(proposal)

    # How many top movers get real candle indicators each tick (plus all
    # held positions). Kept small — each one costs a quote-API call.
    # Candle horizon per trading mode. This is what actually makes swing mode
    # swing: on 1-min bars EMA9/EMA21 measure 9 and 21 MINUTES, so holding for
    # days off those signals is a horizon mismatch. On daily bars the same
    # indicators measure 9 and 21 days.
    #   (period, count, refresh seconds)
    #   (period, count, refresh seconds, candle budget)
    # The budget is how many symbols get indicators per tick — and since the
    # convergence gate treats missing indicators as NOT confirmed, it is the
    # real ceiling on how many symbols can be TRADED. It sat at 15 while the
    # universe ran to 2000, so scanning wider bought nothing at all.
    # Sized against the rate limit: sequential calls self-pace at ~150ms, so
    # 40 calls is ~6s of a 60s tick, well inside ~10 req/s alongside quotes.
    CANDLE_SPEC = {
        "intraday": ("Min_1", 120, 55.0, 40),
        # Daily bars only change once a day; re-fetching every minute would
        # burn API calls for identical data.
        # 15 minutes between ticks and daily bars that change once a day, so a
        # far larger budget costs almost nothing.
        "swing": ("Day", 250, 900.0, 150),
    }

    # Floor only; the per-horizon budget above is what actually applies.
    CANDLE_CANDIDATES = 15
    _candle_fetched_at: dict[str, float] = {}
    CANDLE_REFRESH_SECONDS = 55.0
    # None = use the horizon's own interval. Replay sets 0.0.
    CANDLE_REFRESH_OVERRIDE: 'float | None' = None

    def _coverage_summary(self) -> dict:
        """Scanned vs actually tradable.

        Scanning more symbols does not mean trading more of them: only the
        candle budget gets indicators, and the convergence gate treats missing
        indicators as not-confirmed. Surfacing this stops "Max Symbols" from
        implying an opportunity it cannot deliver.
        """
        _, _, _, budget = self.CANDLE_SPEC.get(
            STATE.settings.trading_horizon, self.CANDLE_SPEC["intraday"])
        strategy = STATE.strategy
        target = getattr(strategy, "_fallback", strategy)
        with_indicators = sum(
            1 for sym in getattr(target, "_indicators", {})
            if target._fresh_indicators(sym)) if hasattr(target, "_fresh_indicators") else 0
        return {
            "scanned": len(STATE.last_quotes),
            "candle_budget": budget,
            "with_indicators": with_indicators,
            "gate": STATE.settings.min_confirmations,
        }

    def _enrich_with_candles(self, broker, quotes) -> None:
        """Fetch 1-min candles for held positions + the biggest day movers and
        feed them to the strategy for VWAP/EMA/RSI. Silent no-op when the
        broker has no candle data (sim mode)."""
        if not hasattr(broker, "candles"):
            return
        portfolio = broker.portfolio()
        held = [s for s, p in portfolio.positions.items() if p.quantity > 0]
        movers = sorted(
            (q for q in quotes if q.prev_close > 0),
            key=lambda q: abs(q.price / q.prev_close - 1.0),
            reverse=True,
        )
        period, count, refresh, budget = self.CANDLE_SPEC.get(
            STATE.settings.trading_horizon, self.CANDLE_SPEC["intraday"])
        targets = list(dict.fromkeys(held + [q.symbol for q in movers[:budget]]))
        now = time.monotonic()
        # Replay sets this to 0 so indicators move bar to bar. It is an
        # OVERRIDE, not a cap: using min() here silently pinned swing's 900s
        # refresh to the intraday 55s, refetching daily candles every minute
        # for data that changes once a day.
        if self.CANDLE_REFRESH_OVERRIDE is not None:
            refresh = self.CANDLE_REFRESH_OVERRIDE
        # Indicators must stay fresh across a whole refresh cycle, or every
        # candle-derived factor reads as unknown for most of the interval.
        strategy = STATE.strategy
        target = getattr(strategy, "_fallback", strategy)
        if hasattr(target, "indicator_ttl"):
            target.indicator_ttl = max(target.INDICATOR_TTL, refresh * 3)
        for symbol in targets:
            if now - self._candle_fetched_at.get(symbol, 0.0) < refresh:
                continue
            try:
                candles = broker.candles(symbol, period=period, count=count)
            except Exception:
                continue
            self._candle_fetched_at[symbol] = now
            if candles and hasattr(STATE.strategy, "ingest_candles"):
                STATE.strategy.ingest_candles(symbol, candles)

    def _check_mechanical_exits(self, quotes) -> list[OrderProposal]:
        """Mechanical, AI-independent exit rules that run every tick BEFORE
        the AI gets a chance to act. Checked in priority order, first match
        wins, one proposal per position:

          max hold (days)  — swing time stop                    → sell all
          stall (minutes)  — intraday time stop: the trade has   → sell all
                             had its chance and is holding a slot
          profit lock      — unrealized gain ≥ lock_profit_pct   → sell all
          stop loss        — ATR stop, else stop_loss_pct        → sell all
          breakeven        — armed after breakeven_trigger_pct,  → sell all
                             then price fell back to entry
          trailing stop    — while in profit, price fell         → sell all
                             trailing_stop_pct below its peak
          thesis break     — a structural confirmation that was  → sell all
                             true at entry no longer is

        PRICE-BASED exits (lock/stop/trailing) ask "what is it worth?".
        The other three ask "is this still worth a slot?" — they exist because
        price alone cannot tell you that a trade has stopped working, only
        that it has not yet hit a number.

        These are hard guarantees; the AI can exit earlier on its own
        judgment but can never hold past these thresholds."""
        settings = STATE.settings
        lock_pct = settings.lock_profit_pct
        stop_pct = settings.stop_loss_pct
        trail_pct = settings.trailing_stop_pct
        breakeven_pct = settings.breakeven_trigger_pct
        # A position can carry an absolute ATR stop even when every percentage
        # setting is 0, so the presence of one has to keep this loop alive.
        portfolio_positions = STATE.broker().portfolio().positions
        has_atr_stop = any(p.stop_price > 0 and p.quantity > 0
                           for p in portfolio_positions.values())
        max_hold_days = settings.max_hold_days
        # Horizon-guarded, not just profile-defaulted. Swing's profile sets
        # this to 0, but a Settings built directly (tests, an API payload, a
        # state file written before the field existed) inherits the intraday
        # dataclass default — and a 120-minute clock would then close a
        # multi-day thesis inside its first session. The horizon is the fact;
        # the value is only a setting.
        max_hold_minutes = 0 if settings.is_swing else settings.max_hold_minutes
        thesis_exit = settings.exit_on_thesis_break
        if (lock_pct <= 0 and stop_pct <= 0 and trail_pct <= 0 and breakeven_pct <= 0
                and not has_atr_stop and max_hold_days <= 0 and max_hold_minutes <= 0
                and not thesis_exit):
            return []
        # Current confirmations per symbol, for the thesis check. Read from the
        # scan this tick already performed — never recomputed here, so the exit
        # and the entry can never disagree about what is true right now.
        live_confirmations = {s.symbol: set(getattr(s, "confirmations", []) or [])
                              for s in STATE.signals}
        # Tool-owned positions only: a stop loss must never reach into shares
        # the user bought themselves.
        portfolio = STATE.broker().portfolio()
        latest = {q.symbol: q for q in quotes}
        proposals: list[OrderProposal] = []
        for symbol, position in self._tool_positions(portfolio).items():
            if position.quantity <= 0 or position.avg_cost <= 0:
                continue
            quote = latest.get(symbol)
            if quote is None:
                continue
            gain_pct = (quote.price / position.avg_cost - 1.0) * 100
            reason = None
            tag = ""
            # Latch the breakeven guarantee the first time the trade earns it.
            # Re-deriving it from the CURRENT gain each tick would hand the
            # protection back the moment price retraced — which is precisely
            # the moment it is needed.
            if breakeven_pct > 0 and gain_pct >= breakeven_pct:
                position.breakeven_armed = True
            held_days = held_minutes = 0.0
            if (max_hold_days > 0 or max_hold_minutes > 0) and position.opened_at:
                try:
                    opened = datetime.fromisoformat(position.opened_at)
                    held_seconds = (self._now() - opened).total_seconds()
                    held_days = held_seconds / 86400
                    held_minutes = held_seconds / 60
                except ValueError:
                    held_days = held_minutes = 0.0
            if max_hold_days > 0 and held_days >= max_hold_days:
                # The swing equivalent of a session timer: capital sitting in a
                # position that has not resolved is capital doing nothing.
                reason = (f"Max hold reached: held {held_days:.1f} days "
                          f"(limit {max_hold_days}), P&L {gain_pct:+.2f}%.")
                tag = "max_hold"
            elif max_hold_minutes > 0 and held_minutes >= max_hold_minutes:
                # Intraday's per-position clock. The trade had its window and
                # did not resolve, so the slot goes back into service. Fires on
                # winners and losers alike — it is a statement about time, not
                # about P&L, and a position at +0.3% for two hours is the exact
                # case it exists for.
                reason = (f"Stalled out: held {held_minutes:.0f} min "
                          f"(limit {max_hold_minutes}) without reaching "
                          f"+{lock_pct:.2f}% or -{stop_pct:.2f}%; "
                          f"P&L {gain_pct:+.2f}%. Freeing the slot.")
                tag = "stall"
            elif lock_pct > 0 and gain_pct >= lock_pct:
                reason = f"Profit lock: +{gain_pct:.2f}% ≥ {lock_pct:.2f}% threshold."
                tag = "profit_lock"
            elif position.stop_price > 0 and quote.price <= position.stop_price:
                # ATR stop fixed at entry — takes precedence over the flat
                # percentage, which stays as the fallback below.
                # A stop is a trigger, not a guaranteed fill price: exits are
                # only evaluated on a tick, so an overnight gap opens the
                # position below the stop and it sells there. Swing mode holds
                # overnight, so this is its defining risk — name it in the
                # record rather than letting the loss look like slippage.
                slip = (position.stop_price - quote.price) / position.stop_price * 100
                gapped = (f" GAPPED {slip:.2f}% THROUGH the stop — "
                          f"filled below it, not at it." if slip > 0.5 else "")
                reason = (f"STOP LOSS: ${quote.price:.2f} at or below the "
                          f"${position.stop_price:.2f} ATR stop set at entry "
                          f"({gain_pct:.2f}%).{gapped}")
                tag = "stop_loss"
            elif position.stop_price <= 0 and stop_pct > 0 and gain_pct <= -stop_pct:
                reason = f"STOP LOSS: {gain_pct:.2f}% ≤ -{stop_pct:.2f}% threshold."
                tag = "stop_loss"
            elif position.breakeven_armed and gain_pct <= 0:
                # It got far enough to prove itself, then gave all of it back.
                # Ranked below the real stops so a genuine stop-out is still
                # reported as one, and above trailing because it is the tighter
                # promise: this trade is no longer allowed to lose money.
                reason = (f"Breakeven stop: ran to +{breakeven_pct:.2f}% and gave "
                          f"it back ({gain_pct:+.2f}%). Closed flat rather than "
                          f"letting a proven winner turn into a loser.")
                tag = "breakeven"
            elif trail_pct > 0 and position.peak_price > position.avg_cost:
                drop_from_peak = (1.0 - quote.price / position.peak_price) * 100
                if drop_from_peak >= trail_pct:
                    reason = (f"Trailing stop: {drop_from_peak:.2f}% below peak "
                              f"${position.peak_price:.2f} (limit {trail_pct:.2f}%).")
                    tag = "trailing_stop"
            elif thesis_exit and position.entry_confirmations:
                # The reason for owning this expired. Checked LAST so every
                # price-based guarantee keeps precedence, and restricted to
                # THESIS_FACTORS — the noisy confirmations flip on chop and
                # would spend a round trip each time.
                entry_thesis = {c for c in position.entry_confirmations
                                if c in THESIS_FACTORS}
                if entry_thesis and symbol in live_confirmations:
                    broken = sorted(entry_thesis - live_confirmations[symbol])
                    if broken:
                        reason = (f"Thesis broken: bought on {', '.join(sorted(entry_thesis))} "
                                  f"but {', '.join(broken)} no longer holds "
                                  f"(P&L {gain_pct:+.2f}%). Exiting on the reason, "
                                  f"not the price.")
                        tag = "thesis_break"
            if reason:
                proposals.append(OrderProposal(
                    symbol=symbol, side=Side.SELL, quantity=position.quantity,
                    price=quote.price, confidence=1.0, reason=reason, tag=tag,
                ))
        return proposals

    def _check_rotation(self, quotes) -> list[OrderProposal]:
        """Free a slot when a clearly stronger signal is being turned away.

        The engine ranks candidates competitively at entry and then never
        ranks them again — a position is only ever compared against its own
        entry price. This is the one place a holding has to re-justify its
        slot against the alternatives.

        OFF by default, and deliberately hard to trigger, because the cost is
        certain and the benefit is not: a swap pays a full round trip (~1.0%
        of a $500 position, ~1.8% of a $250 one) to buy a score DIFFERENCE
        whose predictive power has never been measured on this account. Ship
        it enabled only once replay shows entry score actually predicts return
        by more than the swap costs.

        Emits only the SELL. The freed slot is filled by the next tick's
        ranking pass, so the replacement is chosen by the same competitive
        process as any other entry rather than being hard-wired to the
        challenger that happened to trigger the swap.
        """
        settings = STATE.settings
        cap = settings.max_concurrent_positions
        if not settings.allow_rotation or cap <= 0:
            return []
        portfolio = STATE.broker().portfolio()
        held = {s: p for s, p in self._tool_positions(portfolio).items() if p.quantity > 0}
        # Only bites when the cap is what is actually turning trades away. With
        # a free slot the challenger can simply be bought.
        if len(held) < cap:
            return []
        scores = {s.symbol: s.score for s in STATE.signals}
        challenger = max((s for s in STATE.signals
                          if s.action == "buy" and s.symbol not in held),
                         key=lambda s: s.score, default=None)
        if challenger is None:
            return []
        latest = {q.symbol: q for q in quotes}
        weakest = None
        for symbol, position in held.items():
            quote = latest.get(symbol)
            if quote is None or position.avg_cost <= 0:
                continue
            gain_pct = (quote.price / position.avg_cost - 1.0) * 100
            # Never displace a trade that is working. Rotation exists to
            # recycle dead capital, not to chase a fresh signal with money
            # already earning — and a winner cut short still pays the full
            # round trip on the way out.
            if gain_pct > 0 or position.breakeven_armed:
                continue
            score = scores.get(symbol, 0.0)
            if weakest is None or score < weakest[1]:
                weakest = (symbol, score, quote, gain_pct)
        if weakest is None:
            return []
        symbol, score, quote, gain_pct = weakest
        gap = challenger.score - score
        if gap < settings.rotation_score_gap:
            return []
        return [OrderProposal(
            symbol=symbol, side=Side.SELL, quantity=held[symbol].quantity,
            price=quote.price, confidence=1.0, tag="rotation",
            reason=(f"Rotation: {symbol} now scores {score:.2f} at {gain_pct:+.2f}% "
                    f"while {challenger.symbol} scores {challenger.score:.2f} "
                    f"(gap {gap:.2f} ≥ {settings.rotation_score_gap:.2f}) and is "
                    f"blocked for want of a slot."),
        )]

    def _find_proposal(self, proposal_id: str):
        return next((p for p in STATE.proposals if p.id == proposal_id), None)


ENGINE = TradingEngine()


# SSE — two named event types per connection: "state" and "audit"
_sse_subs_state: dict[int, _queue.Queue] = {}
_sse_subs_audit: dict[int, _queue.Queue] = {}
_sse_lock = threading.Lock()
_sse_counter = 0


def _sse_subscribe() -> tuple[int, _queue.Queue, _queue.Queue]:
    global _sse_counter
    with _sse_lock:
        _sse_counter += 1
        sid = _sse_counter
        sq: _queue.Queue = _queue.Queue(maxsize=20)
        aq: _queue.Queue = _queue.Queue(maxsize=50)
        _sse_subs_state[sid] = sq
        _sse_subs_audit[sid] = aq
    return sid, sq, aq


def _sse_unsubscribe(sid: int) -> None:
    with _sse_lock:
        _sse_subs_state.pop(sid, None)
        _sse_subs_audit.pop(sid, None)


def _sse_push(subs: dict, data: dict, event_type: str) -> None:
    payload = f"event: {event_type}\ndata: {json.dumps(data)}\n\n"
    with _sse_lock:
        sids = list(subs.keys())
    for sid in sids:
        with _sse_lock:
            q = subs.get(sid)
        if q is not None:
            try:
                q.put_nowait(payload)
            except _queue.Full:
                pass


def sse_broadcast(data: dict) -> None:
    _sse_push(_sse_subs_state, data, "state")


def sse_broadcast_audit(entry: dict) -> None:
    _sse_push(_sse_subs_audit, entry, "audit")


# Patch ENGINE methods to auto-broadcast
_o = {k: getattr(TradingEngine, k) for k in ("tick", "execute", "pause_tick", "resume_tick", "update_settings", "approve", "reject", "reset_paper")}

def _p_tick(self):
    r = _o["tick"](self); sse_broadcast(r); return r
def _p_execute(self, p):
    _o["execute"](self, p); sse_broadcast(self.status())
def _p_pause(self):
    r = _o["pause_tick"](self); sse_broadcast(r); return r
def _p_resume(self):
    r = _o["resume_tick"](self); sse_broadcast(r); return r
def _p_update(self, payload):
    r = _o["update_settings"](self, payload); sse_broadcast(r); return r
def _p_approve(self, pid):
    r = _o["approve"](self, pid)
    if r: sse_broadcast(self.status())
    return r
def _p_reject(self, pid):
    r = _o["reject"](self, pid)
    if r: sse_broadcast(self.status())
    return r
def _p_reset(self, cash=None):
    r = _o["reset_paper"](self, cash); sse_broadcast(r); return r

TradingEngine.tick = _p_tick
TradingEngine.execute = _p_execute
TradingEngine.pause_tick = _p_pause
TradingEngine.resume_tick = _p_resume
TradingEngine.update_settings = _p_update
TradingEngine.approve = _p_approve
TradingEngine.reject = _p_reject
TradingEngine.reset_paper = _p_reset


def auto_tick_loop() -> None:
    while True:
        try:
            if STATE.settings.auto_tick_enabled and STATE.settings.strategy_enabled and not STATE.tick_paused:
                ENGINE.tick()
            time.sleep(max(5, STATE.settings.tick_interval_seconds))
        except Exception:
            time.sleep(10)


def quote_refresh_loop() -> None:
    """Lightweight loop that refreshes prices and signals every 10 s without
    running the full strategy tick. Keeps AI Ranking / Diagnostics live even
    when auto_tick is off or between strategy ticks."""
    while True:
        try:
            # A 10s display refresh is right for intraday. For a multi-day hold
            # it is churn — real quote calls and a full re-scan for a position
            # that will not be touched for days.
            time.sleep(60 if STATE.settings.is_swing else 10)
            with STATE.lock:
                if not STATE.last_quotes:
                    continue  # nothing to refresh yet
                broker = STATE.broker()
                # Refresh only what the UI actually shows: held positions,
                # ranked signals, then the head of the universe — capped at
                # 200. Refreshing the full discovery universe (32k+ symbols)
                # every 10s blew straight through Longbridge's HTTP rate
                # limits and burned CPU serializing the results. The full
                # universe is still scanned by the strategy tick.
                portfolio = broker.portfolio()
                priority = [s for s, p in portfolio.positions.items() if p.quantity > 0]
                priority += [sig.symbol for sig in STATE.signals]
                priority += [q.symbol for q in STATE.last_quotes]
                symbols = list(dict.fromkeys(priority))[:200]
                from broker import LB_STATUS as _LBS
                if _LBS["connected"]:
                    # Frozen markets don't need refreshing
                    open_mkts = set(open_markets(["US", "HK", "SG"]))
                    symbols = [s for s in symbols if market_of(s) in open_mkts or market_of(s) not in ("US", "HK", "SG")]
                    if not symbols:
                        continue
                fresh = {q.symbol: q for q in broker.quotes(symbols)}
                quotes = [fresh.get(q.symbol, q) for q in STATE.last_quotes]
                STATE.last_quotes = quotes
                STATE.last_quote = quotes[0] if quotes else None
                # Re-run signals (read-only — no proposals, no AI call) so
                # rankings stay fresh without burning API tokens
                STATE.signals = STATE.strategy.scan_signals_only(
                    STATE.settings, quotes, ENGINE._tradable_view(broker.portfolio()))
                STATE.last_tick_at = datetime.now(timezone.utc).isoformat()
                sse_broadcast(ENGINE.status())
        except Exception:
            pass


def _tail_lines(path: Path, n: int) -> list[str]:
    """Last n non-empty lines of a file without loading the whole file into
    memory (the logs can reach tens of MB on long runs)."""
    if not path.exists():
        return []
    with path.open("rb") as f:
        f.seek(0, 2)
        size = f.tell()
        block = 65536
        data = b""
        while size > 0 and data.count(b"\n") <= n:
            step = min(block, size)
            size -= step
            f.seek(size)
            data = f.read(step) + data
            if size == 0:
                break
    lines = [l for l in data.decode("utf-8", errors="replace").splitlines() if l.strip()]
    return lines[-n:]


class Handler(BaseHTTPRequestHandler):
    def handle_one_request(self) -> None:
        # Suppress noisy tracebacks for normal client disconnects (browser tab
        # closed, page navigated away, refresh mid-request). These are not bugs.
        try:
            super().handle_one_request()
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        routes = {
            "/": lambda: self._send_file(STATIC / "index.html", "text/html"),
            "/api/stream": self._sse_stream,
            "/api/status": lambda: self._json(ENGINE.status()),
            "/api/tick": lambda: self._json(ENGINE.tick()),
            "/api/tick/pause": lambda: self._json(ENGINE.pause_tick()),
            "/api/tick/resume": lambda: self._json(ENGINE.resume_tick()),
            "/api/audit": lambda: self._serve_jsonl_tail(AUDIT_LOG, 100),
            "/api/sessions": lambda: self._serve_jsonl_tail(SESSIONS_LOG, 50),
            "/api/metrics": lambda: self._json(
                metrics_report(parse_qs(parsed.query).get("window", ["session"])[0])
            ),
            "/api/performance": lambda: self._json(performance_report(
                parse_qs(parsed.query).get("window", ["all"])[0],
                int(parse_qs(parsed.query).get("min_trades", ["1"])[0] or 1),
            )),
            "/api/trades": lambda: self._json(closed_trades_report(
                parse_qs(parsed.query).get("window", ["all"])[0],
                parse_qs(parsed.query).get("limit", ["50"])[0],
            )),
            "/api/backtest/last": lambda: self._serve_jsonl_tail(BACKTEST_LOG, 1),
            "/api/ai/status": lambda: self._json({"ai": AI_STATUS.as_dict()}),
        }
        if path in routes:
            routes[path]()
            return
        if path.startswith("/static/"):
            target = STATIC / path.removeprefix("/static/")
            ct = "text/css" if target.suffix == ".css" else "application/javascript"
            self._send_file(target, ct)
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path == "/api/settings":
            self._json(ENGINE.update_settings(self._read_json()))
        elif path == "/api/tick/pause":
            self._json(ENGINE.pause_tick())
        elif path == "/api/tick/resume":
            self._json(ENGINE.resume_tick())
        elif path == "/api/settings/defaults":
            self._json(ENGINE.apply_recommended_defaults())
        elif path == "/api/paper/reset":
            payload = self._read_json()
            # If caller passes starting_cash explicitly use it, otherwise fall back to budget
            cash = float(payload["starting_cash"]) if "starting_cash" in payload else None
            self._json(ENGINE.reset_paper(cash))
        elif path.startswith("/api/proposals/") and path.endswith("/approve"):
            pid = path.split("/")[3]
            if not ENGINE.approve(pid):
                self.send_error(HTTPStatus.NOT_FOUND)
            else:
                self._json(ENGINE.status())
        elif path.startswith("/api/proposals/") and path.endswith("/reject"):
            pid = path.split("/")[3]
            if not ENGINE.reject(pid):
                self.send_error(HTTPStatus.NOT_FOUND)
            else:
                self._json(ENGINE.status())
        elif path == "/api/backtest":
            p = self._read_json()
            symbols = p.get("symbols") or STATE.settings.active_universe()
            ticks = max(10, min(500, int(p.get("ticks", 60))))
            cash = float(p.get("starting_cash", STATE.settings.budget))
            self._json(run_backtest(symbols, ticks=ticks, starting_cash=cash))
        elif path == "/api/ai/config":
            payload = self._read_json()
            provider = payload.get("provider", "")
            model = payload.get("model", "").strip()
            strategy = payload.get("strategy", "").strip()
            if hasattr(STATE.strategy, "configure"):
                STATE.strategy.configure(provider, model, strategy)
            # Also sync strategy to settings so prompt builder sees it
            if strategy:
                STATE.settings.ai_strategy_name = strategy
                STATE.save()
            self._json({"ai": AI_STATUS.as_dict()})
        else:
            self.send_error(HTTPStatus.NOT_FOUND)

    def log_message(self, *args) -> None:
        return

    def _read_json(self) -> dict:
        n = int(self.headers.get("content-length", "0"))
        return json.loads(self.rfile.read(n).decode("utf-8")) if n else {}

    def _json(self, payload: dict) -> None:
        data = json.dumps(payload).encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _sse_stream(self) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("content-type", "text/event-stream")
        self.send_header("cache-control", "no-cache")
        self.send_header("x-accel-buffering", "no")
        self.end_headers()

        sid, sq, aq = _sse_subscribe()
        try:
            # Immediately send current state
            self.wfile.write(f"event: state\ndata: {json.dumps(ENGINE.status())}\n\n".encode())
            # Immediately send last 50 audit entries
            for line in _tail_lines(AUDIT_LOG, 50):
                self.wfile.write(f"event: audit\ndata: {line}\n\n".encode())
            self.wfile.flush()
        except Exception:
            _sse_unsubscribe(sid)
            return

        try:
            while True:
                # Drain both queues without blocking first
                sent = False
                for q in (sq, aq):
                    while True:
                        try:
                            self.wfile.write(q.get_nowait().encode())
                            sent = True
                        except _queue.Empty:
                            break
                if sent:
                    self.wfile.flush()
                    continue
                # Nothing pending — block on state queue up to 20 s
                try:
                    self.wfile.write(sq.get(timeout=20).encode())
                    self.wfile.flush()
                except _queue.Empty:
                    # Heartbeat to keep connection alive
                    self.wfile.write(b": heartbeat\n\n")
                    self.wfile.flush()
        except Exception:
            pass
        finally:
            _sse_unsubscribe(sid)

    def _serve_jsonl_tail(self, path: Path, n: int) -> None:
        self._json({"entries": [json.loads(l) for l in _tail_lines(path, n)]})

    def _send_file(self, path: Path, content_type: str) -> None:
        if not path.exists() or not path.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        data = path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("content-type", content_type)
        self.send_header("content-length", str(len(data)))
        # Without this, browsers cache index.html/app.js indefinitely and the
        # UI silently stays stale after code updates.
        self.send_header("cache-control", "no-cache")
        self.end_headers()
        self.wfile.write(data)


class QuietServer(ThreadingHTTPServer):
    """Suppresses the default traceback printing for client disconnects
    (BrokenPipeError, ConnectionResetError) — these happen any time a
    browser tab is closed or a request is interrupted, and are not bugs."""

    def handle_error(self, request, client_address) -> None:
        import sys
        exc_type = sys.exc_info()[0]
        if exc_type in (BrokenPipeError, ConnectionResetError):
            return  # expected on client disconnect — don't log
        super().handle_error(request, client_address)


def _startup_banner() -> None:
    """Print exactly what is connected so nobody has to guess whether the app
    is using real Longbridge data or the local simulator."""
    import os
    from ai_strategy import PROVIDERS
    from broker import LB_STATUS

    def key_state(name: str) -> str:
        v = os.environ.get(name, "")
        return f"set ({len(v)} chars)" if v else "MISSING"

    print("─" * 62)
    print("Credentials:")
    for name in ("LONGBRIDGE_APP_KEY", "LONGBRIDGE_APP_SECRET", "LONGBRIDGE_ACCESS_TOKEN"):
        print(f"  {name:<26} {key_state(name)}")
    if LB_STATUS["connected"]:
        print("Longbridge: CONNECTED — real market quotes in use")
    else:
        print(f"Longbridge: NOT CONNECTED — using SIMULATED random-walk prices")
        print(f"  reason: {LB_STATUS['error']}")
    ai = AI_STATUS.as_dict()
    provider, model = ai["provider"], ai["model"]
    if provider in ("none", ""):
        print("AI brain:   OFF — rule-based momentum strategy only")
    else:
        env_key = PROVIDERS.get(provider, {}).get("env_key")
        key_ok = (env_key is None) or bool(os.environ.get(env_key, ""))
        if key_ok:
            print(f"AI brain:   {provider} / {model}")
        else:
            print(f"AI brain:   {provider} selected but {env_key} is MISSING —")
            print("            every AI call will fail and fall back to momentum rules")
    from broker import PAPER_FEE_PER_TRADE, PAPER_SLIPPAGE_BPS
    from fees import unverified_markets
    if PAPER_FEE_PER_TRADE is not None:
        print(f"Paper fees: FLAT ${PAPER_FEE_PER_TRADE:.2f}/order override "
              f"(PAPER_FEE_PER_TRADE) — will not match a real bill")
    else:
        unverified = unverified_markets()
        print(f"Paper fees: real per-market schedules + {PAPER_SLIPPAGE_BPS:.0f}bps slippage")
        if unverified:
            print(f"  {', '.join(unverified)} fees are UNVERIFIED estimates — "
                  f"run `python3 calibrate_fees.py`")

    horizon = STATE.settings.trading_horizon
    period, count, _, _ = TradingEngine.CANDLE_SPEC.get(horizon, TradingEngine.CANDLE_SPEC["intraday"])
    if horizon == "swing":
        hold = STATE.settings.max_hold_days
        print(f"Horizon:    SWING — {period} candles, held overnight, session does "
              f"not expire, max hold {str(hold) + 'd' if hold else 'unlimited'}")
    else:
        print(f"Horizon:    INTRADAY — {period} candles, session ends after "
              f"{STATE.settings.duration_minutes / 60:.1f}h")

    # Can the current settings make money at all? Answer it up front.
    summary = ENGINE.viability_summary()
    target = summary["target_pct"]
    if target <= 0:
        costs = ", ".join(f"{m} {v['breakeven_pct']:.2f}%"
                          for m, v in summary["per_market"].items())
        print(f"Viability:  no profit target set (lock_profit_pct = 0)")
        print(f"  round-trip cost to beat at ${summary['trade_value']:,.0f}/trade: {costs}")
    elif summary["any_unviable"]:
        print(f"Viability:  ⚠ UNPROFITABLE BY CONSTRUCTION at "
              f"${summary['trade_value']:,.0f}/trade, target {target:.2f}%")
        for market, v in summary["per_market"].items():
            if not v["viable"]:
                floor = v["min_viable_notional"]
                fix = (f"raise trade value to ~${floor:,.0f}"
                       if floor > 0 else "no trade size works — raise the target")
                print(f"  {market}: costs {v['breakeven_pct']:.2f}% > target "
                      f"{target:.2f}% → a win nets {v['net_edge_pct']:+.2f}%  ({fix})")
        print("  Buys are BLOCKED while this holds."
              if summary["enforced"] else
              "  Enforcement is OFF — these trades will be placed anyway.")
    else:
        edges = ", ".join(f"{m} {v['net_edge_pct']:+.2f}%"
                          for m, v in summary["per_market"].items())
        print(f"Viability:  ok — a winning trade nets {edges} after costs")
    print("─" * 62, flush=True)


def main() -> None:
    _startup_banner()
    threading.Thread(target=auto_tick_loop, daemon=True).start()
    threading.Thread(target=quote_refresh_loop, daemon=True).start()
    server = QuietServer(("127.0.0.1", 8765), Handler)
    server.daemon_threads = True
    print("Trading tool running at http://127.0.0.1:8765", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()