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
from .strategy import MomentumStrategy

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
        self.strategy = MomentumStrategy()
        self.proposals: list[OrderProposal] = []
        self.last_quote = None
        self.last_quotes: list = []
        self.signals: list = []
        self.universe_source = "sample"
        self.last_tick_at: str | None = None
        self.tick_paused: bool = False
        self.session_start_at: str | None = None
        self.session_start_equity: float = 0.0
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

    def save(self) -> None:
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

    def audit(self, event: AuditEventType, symbol: str | None = None, **detail) -> None:
        STATE_DIR.mkdir(exist_ok=True)
        entry = AuditEntry(event=event, symbol=symbol, detail=detail)
        serialised = to_json(entry)
        with AUDIT_LOG.open("a") as handle:
            handle.write(json.dumps(serialised) + "\n")
        sse_broadcast_audit(serialised)

    def log_trade(self, proposal: OrderProposal) -> None:
        STATE_DIR.mkdir(exist_ok=True)
        record = {
            "logged_at": datetime.now(timezone.utc).isoformat(),
            "order": to_json(proposal),
            "portfolio": to_json(self.paper_broker.portfolio()),
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


def run_backtest(symbols: list[str], ticks: int = 60, starting_cash: float | None = None) -> dict:
    if starting_cash is None:
        starting_cash = STATE.settings.budget
    from .broker import PaperBroker as _PB
    from .strategy import MomentumStrategy as _MS
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
    for tick_i in range(ticks):
        quotes = bt_broker.quotes(symbols)
        signals, proposals = bt_strategy.scan(settings, quotes, bt_broker.portfolio())
        for p in proposals:
            bt_broker.submit_order(p)
            trades.append({"tick": tick_i, "trade": to_json(p)})
        equity_curve.append({"tick": tick_i, "equity": bt_broker.portfolio().equity()})
    final = bt_broker.portfolio()
    result = {
        "symbols": symbols, "ticks": ticks, "starting_cash": starting_cash,
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
    def status(self) -> dict:
        broker = STATE.paper_broker if STATE.settings.trading_mode is TradingMode.PAPER else (STATE.live_broker or STATE.paper_broker)
        portfolio = broker.portfolio()
        now = datetime.now(timezone.utc).isoformat()
        return {
            "settings": to_json(STATE.settings),
            "portfolio": to_json(portfolio),
            "last_quote": to_json(STATE.last_quote),
            "last_quotes": to_json(STATE.last_quotes),
            "universe_source": STATE.universe_source,
            "signals": to_json(STATE.signals),
            "signals_updated_at": STATE.last_tick_at,
            "proposals": to_json(STATE.proposals[-20:]),
            "last_tick_at": STATE.last_tick_at,
            "tick_paused": STATE.tick_paused,
            "session_pnl": STATE.session_pnl(),
            "session_start_at": STATE.session_start_at,
        }

    def tick(self) -> dict:
        with STATE.lock:
            broker = STATE.broker()
            symbols = self._resolve_universe(broker)
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
            if STATE.settings.strategy_enabled:
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
                signals, _ = STATE.strategy.scan(STATE.settings, quotes, broker.portfolio())
                STATE.signals = signals
            STATE.save()
            return self.status()

    def update_settings(self, payload: dict) -> dict:
        with STATE.lock:
            settings = STATE.settings
            was_enabled = settings.strategy_enabled
            for key in ("symbol", "markets", "universe", "budget", "duration_minutes", "max_scan_symbols",
                        "max_loss", "max_trade_value", "auto_tick_enabled", "tick_interval_seconds",
                        "allow_live_trading", "stop_at_end", "strategy_enabled"):
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
            STATE.strategy = MomentumStrategy()
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
            proposal.status = OrderStatus.FILLED if proposal.error is None else OrderStatus.FAILED
        except Exception as exc:
            proposal.status = OrderStatus.FAILED
            proposal.error = str(exc)
        event = AuditEventType.FILL if proposal.status is OrderStatus.FILLED else AuditEventType.FAIL
        STATE.audit(event, symbol=proposal.symbol, side=proposal.side.value, quantity=round(proposal.quantity, 6), price=proposal.price, error=proposal.error)
        if STATE.settings.trading_mode is TradingMode.PAPER:
            STATE.log_trade(proposal)

    def _expire_stale_proposals(self) -> None:
        """In manual mode, auto-expire proposals older than PROPOSAL_TTL_SECONDS."""
        from .strategy import PROPOSAL_TTL_SECONDS
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
        if STATE.settings.universe:
            STATE.universe_source = "custom"
            return STATE.settings.active_universe()
        if STATE.settings.trading_mode is TradingMode.LIVE:
            discovered = broker.discover_symbols(STATE.settings.markets)
            if discovered:
                unsupported = [m for m in STATE.settings.markets if m != "US"]
                source = "Longbridge security list"
                if unsupported:
                    source += f" + sample fallback for {', '.join(unsupported)}"
                    from .models import DEFAULT_UNIVERSES
                    for market in unsupported:
                        discovered.extend(DEFAULT_UNIVERSES.get(market, []))
                STATE.universe_source = source
                cap = STATE.settings.max_scan_symbols
                return list(dict.fromkeys(discovered)) if cap == 0 else list(dict.fromkeys(discovered))[:cap]
        STATE.universe_source = "sample fallback"
        return STATE.settings.active_universe()

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
                symbols = [q.symbol for q in STATE.last_quotes]
                quotes = broker.quotes(symbols)
                STATE.last_quotes = quotes
                STATE.last_quote = quotes[0] if quotes else None
                # Re-run signals (read-only — no proposals) so rankings stay fresh
                signals, _ = STATE.strategy.scan(STATE.settings, quotes, broker.portfolio())
                STATE.signals = signals
                STATE.last_tick_at = datetime.now(timezone.utc).isoformat()
                sse_broadcast(ENGINE.status())
        except Exception:
            pass


class Handler(BaseHTTPRequestHandler):
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
            if AUDIT_LOG.exists():
                for line in AUDIT_LOG.read_text().strip().splitlines()[-50:]:
                    if line.strip():
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
        if not path.exists():
            self._json({"entries": []})
            return
        lines = path.read_text().strip().splitlines()
        self._json({"entries": [json.loads(l) for l in lines[-n:] if l.strip()]})

    def _send_file(self, path: Path, content_type: str) -> None:
        if not path.exists() or not path.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        data = path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("content-type", content_type)
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def main() -> None:
    threading.Thread(target=auto_tick_loop, daemon=True).start()
    threading.Thread(target=quote_refresh_loop, daemon=True).start()
    server = ThreadingHTTPServer(("127.0.0.1", 8765), Handler)
    server.daemon_threads = True
    print("Trading tool running at http://127.0.0.1:8765")
    server.serve_forever()


if __name__ == "__main__":
    main()
