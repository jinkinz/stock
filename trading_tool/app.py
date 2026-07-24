from __future__ import annotations

import json
import queue as _queue
import threading
import time
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from .broker import LongbridgeBroker, PaperBroker
from .models import (
    ApprovalMode,
    AuditEntry,
    AuditEventType,
    OrderProposal,
    OrderStatus,
    Portfolio,
    Position,
    Settings,
    Side,
    TradingMode,
    to_json,
)
from .ai_strategy import AI_STATUS, AIStrategy
from .market_hours import market_of, markets_status, open_markets
from .strategy import MomentumStrategy, PROPOSAL_TTL_SECONDS

ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "static"
STATE_DIR = ROOT / "state"
STATE_FILE = STATE_DIR / "paper_state.json"
TRADE_LOG = STATE_DIR / "trade_log.jsonl"
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
        STATE_DIR.mkdir(exist_ok=True)
        payload = {
            "settings": to_json(self.settings),
            "paper": self.paper_broker.snapshot(),
            "proposals": to_json(self.proposals[-200:]),
            "last_tick_at": self.last_tick_at,
            "tick_paused": self.tick_paused,
            "session_start_at": self.session_start_at,
            "session_start_equity": self.session_start_equity,
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

    def _proposal_from_json(self, item: dict) -> OrderProposal:
        return OrderProposal(
            symbol=item["symbol"],
            side=Side(item["side"]),
            quantity=float(item["quantity"]),
            price=float(item["price"]),
            reason=item.get("reason", "Restored proposal."),
            confidence=float(item.get("confidence", 0.0)),
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
    from .broker import PaperBroker as _PB
    from .strategy import MomentumStrategy as _MS
    from .models import Quote as _Quote

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
        from .broker import LB_STATUS
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
        }

    def tick(self) -> dict:
        from .broker import LB_STATUS
        with STATE.lock:
            broker = STATE.broker()
            # Market-hours gate — only with real data. Sim prices move 24/7,
            # so the simulator stays testable at any hour.
            gate = LB_STATUS["connected"]
            open_mkts = open_markets(STATE.settings.markets) if gate else list(STATE.settings.markets)

            if gate and not open_mkts:
                # Every selected market is closed: prices are frozen, so no
                # scanning, no AI calls, no proposals. Only the session clock
                # and proposal expiry keep running.
                STATE.last_tick_at = datetime.now(timezone.utc).isoformat()
                STATE.universe_source = "all selected markets closed — waiting for open"
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

                signals, proposals = STATE.strategy.scan(STATE.settings, quotes, broker.portfolio())
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
                STATE.signals = STATE.strategy.scan_signals_only(STATE.settings, quotes, broker.portfolio())
            STATE.save()
            return self.status()

    def update_settings(self, payload: dict) -> dict:
        with STATE.lock:
            settings = STATE.settings
            was_enabled = settings.strategy_enabled
            for key in ("symbol", "markets", "universe", "budget", "duration_minutes", "max_scan_symbols",
                        "max_loss", "max_trade_value", "auto_tick_enabled", "tick_interval_seconds",
                        "allow_live_trading", "stop_at_end", "strategy_enabled",
                        "target_profit", "target_profit_per_hour", "lock_profit_pct",
                        "stop_loss_pct", "trailing_stop_pct", "ai_strategy_name"):
                if key in payload:
                    setattr(settings, key, payload[key])
            if "trading_mode" in payload:
                settings.trading_mode = TradingMode(payload["trading_mode"])
            if "approval_mode" in payload:
                settings.approval_mode = ApprovalMode(payload["approval_mode"])
            settings.normalized()
            if settings.strategy_enabled and not was_enabled:
                settings.started_at = datetime.now(timezone.utc).isoformat()
                STATE.begin_session()
            if not settings.strategy_enabled and was_enabled:
                settings.started_at = None
                STATE.close_session()
            STATE.save()
            return self.status()

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

    def execute(self, proposal: OrderProposal) -> None:
        # Hard guard: never trade options or on margin.
        # We only allow plain BUY (cash) or SELL (held position).
        # This cannot be overridden by settings.
        if proposal.side not in (Side.BUY, Side.SELL):
            proposal.status = OrderStatus.FAILED
            proposal.error = "Rejected: only plain BUY/SELL of owned shares is permitted. No margin, no options."
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
        if proposal.status is OrderStatus.FILLED:
            event = AuditEventType.FILL
        elif proposal.status is OrderStatus.APPROVED:
            event = AuditEventType.PROPOSAL   # live order pending on exchange
        else:
            event = AuditEventType.FAIL
        STATE.audit(event, symbol=proposal.symbol, side=proposal.side.value, quantity=round(proposal.quantity, 6), price=proposal.price, error=proposal.error)
        if STATE.settings.trading_mode is TradingMode.PAPER:
            STATE.log_trade(proposal)

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
            if age > PROPOSAL_TTL_SECONDS:
                proposal.status = OrderStatus.REJECTED
                proposal.error = f"Auto-expired after {int(age)}s (manual approval timeout)."
                STATE.audit(AuditEventType.REJECT, symbol=proposal.symbol,
                            proposal_id=proposal.id, reason="TTL expired")

    def _session_expired(self) -> bool:
        if STATE.settings.started_at is None:
            STATE.settings.started_at = datetime.now(timezone.utc).isoformat()
            return False
        started = datetime.fromisoformat(STATE.settings.started_at)
        return datetime.now(timezone.utc) >= started + timedelta(minutes=STATE.settings.duration_minutes)

    def _resolve_universe(self, broker) -> list[str]:
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
            cap = STATE.settings.max_scan_symbols
            if cap == 0:
                # "Unlimited" still needs a feasibility ceiling: Longbridge
                # discovery returns 30k+ US symbols, but quoting them takes
                # 150+ HTTP calls per tick (rate limit: ~10 req/s), so a full
                # scan can't finish inside one tick interval.
                cap = self.MAX_UNLIMITED_SCAN
            scanned = STATE._symbol_cache[:cap]
            STATE.universe_source = f"Longbridge discovery cache: {len(STATE._symbol_cache)} found, scanning {len(scanned)}"
            return scanned

        # 3. Fall back to expanded DEFAULT_UNIVERSES sample list
        STATE.universe_source = "sample fallback (set Longbridge credentials for full scan)"
        return STATE.settings.active_universe()

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
        for symbol, position in portfolio.positions.items():
            if position.quantity <= 0 or symbol not in latest:
                continue
            quote = latest[symbol]
            proposal = OrderProposal(symbol=quote.symbol, side=Side.SELL, quantity=position.quantity,
                                     price=quote.price, confidence=1.0, reason="Trading duration ended — stop-at-end enabled.")
            STATE.proposals.append(proposal)
            if STATE.settings.approval_mode is ApprovalMode.AUTO:
                self.execute(proposal)

    # How many top movers get real candle indicators each tick (plus all
    # held positions). Kept small — each one costs a quote-API call.
    CANDLE_CANDIDATES = 15
    _candle_fetched_at: dict[str, float] = {}
    CANDLE_REFRESH_SECONDS = 55.0

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
        targets = list(dict.fromkeys(held + [q.symbol for q in movers[: self.CANDLE_CANDIDATES]]))
        now = time.monotonic()
        for symbol in targets:
            if now - self._candle_fetched_at.get(symbol, 0.0) < self.CANDLE_REFRESH_SECONDS:
                continue
            try:
                candles = broker.candles(symbol, period="Min_1", count=120)
            except Exception:
                continue
            self._candle_fetched_at[symbol] = now
            if candles and hasattr(STATE.strategy, "ingest_candles"):
                STATE.strategy.ingest_candles(symbol, candles)

    def _check_mechanical_exits(self, quotes) -> list[OrderProposal]:
        """Mechanical, AI-independent exit rules that run every tick BEFORE
        the AI gets a chance to act. Three independent triggers per position:

          profit lock    — unrealized gain ≥ lock_profit_pct  → sell all
          stop loss      — loss from entry ≥ stop_loss_pct    → sell all
          trailing stop  — while in profit, price fell trailing_stop_pct
                           below its peak since entry         → sell all

        These are hard guarantees; the AI can exit earlier on its own
        judgment but can never hold past these thresholds."""
        settings = STATE.settings
        lock_pct = settings.lock_profit_pct
        stop_pct = settings.stop_loss_pct
        trail_pct = settings.trailing_stop_pct
        if lock_pct <= 0 and stop_pct <= 0 and trail_pct <= 0:
            return []
        portfolio = STATE.broker().portfolio()
        latest = {q.symbol: q for q in quotes}
        proposals: list[OrderProposal] = []
        for symbol, position in portfolio.positions.items():
            if position.quantity <= 0 or position.avg_cost <= 0:
                continue
            quote = latest.get(symbol)
            if quote is None:
                continue
            gain_pct = (quote.price / position.avg_cost - 1.0) * 100
            reason = None
            if lock_pct > 0 and gain_pct >= lock_pct:
                reason = f"Profit lock: +{gain_pct:.2f}% ≥ {lock_pct:.2f}% threshold."
            elif stop_pct > 0 and gain_pct <= -stop_pct:
                reason = f"STOP LOSS: {gain_pct:.2f}% ≤ -{stop_pct:.2f}% threshold."
            elif trail_pct > 0 and position.peak_price > position.avg_cost:
                drop_from_peak = (1.0 - quote.price / position.peak_price) * 100
                if drop_from_peak >= trail_pct:
                    reason = (f"Trailing stop: {drop_from_peak:.2f}% below peak "
                              f"${position.peak_price:.2f} (limit {trail_pct:.2f}%).")
            if reason:
                proposals.append(OrderProposal(
                    symbol=symbol, side=Side.SELL, quantity=position.quantity,
                    price=quote.price, confidence=1.0, reason=reason,
                ))
        return proposals

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
            time.sleep(10)
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
                from .broker import LB_STATUS as _LBS
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
                STATE.signals = STATE.strategy.scan_signals_only(STATE.settings, quotes, broker.portfolio())
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
        path = urlparse(self.path).path
        routes = {
            "/": lambda: self._send_file(STATIC / "index.html", "text/html"),
            "/api/stream": self._sse_stream,
            "/api/status": lambda: self._json(ENGINE.status()),
            "/api/tick": lambda: self._json(ENGINE.tick()),
            "/api/tick/pause": lambda: self._json(ENGINE.pause_tick()),
            "/api/tick/resume": lambda: self._json(ENGINE.resume_tick()),
            "/api/audit": lambda: self._serve_jsonl_tail(AUDIT_LOG, 100),
            "/api/sessions": lambda: self._serve_jsonl_tail(SESSIONS_LOG, 50),
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
    from .ai_strategy import PROVIDERS
    from .broker import LB_STATUS

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