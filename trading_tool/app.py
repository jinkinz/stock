from __future__ import annotations

import json
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


class AppState:
    def __init__(self) -> None:
        self.settings = Settings()
        self.paper_broker = PaperBroker()
        self.live_broker: LongbridgeBroker | None = None
        self.strategy = MomentumStrategy()
        self.proposals: list[OrderProposal] = []
        self.last_quote = None
        self.last_quotes = []
        self.signals = []
        self.universe_source = "sample"
        self.last_tick_at: str | None = None
        # Kill switch: when True the auto-tick loop skips ticking even if
        # auto_tick_enabled is True.  The Run Tick button bypasses this.
        self.tick_paused: bool = False
        self.lock = threading.RLock()
        self.load()

    def broker(self):
        if self.settings.trading_mode is TradingMode.LIVE:
            if self.live_broker is None:
                self.live_broker = LongbridgeBroker()
            return self.live_broker
        return self.paper_broker

    # ------------------------------------------------------------------
    # Persist / load
    # ------------------------------------------------------------------

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
                    quantity=int(pos.get("quantity", 0)),
                    avg_cost=float(pos.get("avg_cost", 0)),
                )
                for symbol, pos in portfolio_data.get("positions", {}).items()
            }
            portfolio = Portfolio(
                cash=float(portfolio_data.get("cash", 10000.0)),
                realized_pnl=float(portfolio_data.get("realized_pnl", 0.0)),
                positions=positions,
                last_prices={
                    symbol: float(price)
                    for symbol, price in portfolio_data.get("last_prices", {}).items()
                },
            )
            prices = {symbol: float(price) for symbol, price in paper.get("prices", {}).items()}
            self.paper_broker = PaperBroker(portfolio=portfolio, prices=prices)
            self.last_tick_at = data.get("last_tick_at")
            self.tick_paused = bool(data.get("tick_paused", False))
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
        }
        STATE_FILE.write_text(json.dumps(payload, indent=2))

    # ------------------------------------------------------------------
    # Audit logging
    # ------------------------------------------------------------------

    def audit(self, event: AuditEventType, symbol: str | None = None, **detail) -> None:
        STATE_DIR.mkdir(exist_ok=True)
        entry = AuditEntry(event=event, symbol=symbol, detail=detail)
        with AUDIT_LOG.open("a") as handle:
            handle.write(json.dumps(to_json(entry)) + "\n")

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
            quantity=int(item["quantity"]),
            price=float(item["price"]),
            reason=item.get("reason", "Restored proposal."),
            confidence=float(item.get("confidence", 0.0)),
            id=item.get("id"),
            status=OrderStatus(item.get("status", "proposed")),
            created_at=item.get("created_at", datetime.now(timezone.utc).isoformat()),
            error=item.get("error"),
        )


STATE = AppState()


# ---------------------------------------------------------------------------
# Backtest helper
# ---------------------------------------------------------------------------

def run_backtest(symbols: list[str], ticks: int = 60, starting_cash: float = 10000.0) -> dict:
    """Run a fast deterministic backtest using the paper price simulator.

    Simulates `ticks` price ticks per symbol using the random-walk model,
    then replays the MomentumStrategy and records equity at each step.
    Returns a summary and per-tick equity curve.
    """
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
        portfolio = bt_broker.portfolio()
        signals, proposals = bt_strategy.scan(settings, quotes, portfolio)

        for proposal in proposals:
            pending = {(p.symbol, p.side) for p in [] if p.status is OrderStatus.PROPOSED}
            if (proposal.symbol, proposal.side) in pending:
                continue
            bt_broker.submit_order(proposal)
            trades.append({"tick": tick_i, "trade": to_json(proposal)})

        equity_curve.append({"tick": tick_i, "equity": bt_broker.portfolio().equity()})

    final_portfolio = bt_broker.portfolio()
    result = {
        "symbols": symbols,
        "ticks": ticks,
        "starting_cash": starting_cash,
        "final_equity": final_portfolio.equity(),
        "realized_pnl": final_portfolio.realized_pnl,
        "unrealized_pnl": final_portfolio.unrealized_pnl(),
        "total_trades": len(trades),
        "equity_curve": equity_curve,
        "trades": trades[-50:],
        "ran_at": datetime.now(timezone.utc).isoformat(),
    }

    # persist
    STATE_DIR.mkdir(exist_ok=True)
    with BACKTEST_LOG.open("a") as f:
        f.write(json.dumps(result) + "\n")

    return result


# ---------------------------------------------------------------------------
# Trading engine
# ---------------------------------------------------------------------------

class TradingEngine:
    def status(self) -> dict:
        broker = (
            STATE.paper_broker
            if STATE.settings.trading_mode is TradingMode.PAPER
            else (STATE.live_broker or STATE.paper_broker)
        )
        return {
            "settings": to_json(STATE.settings),
            "portfolio": to_json(broker.portfolio()),
            "last_quote": to_json(STATE.last_quote),
            "last_quotes": to_json(STATE.last_quotes),
            "universe_source": STATE.universe_source,
            "signals": to_json(STATE.signals),
            "proposals": to_json(STATE.proposals[-20:]),
            "last_tick_at": STATE.last_tick_at,
            "tick_paused": STATE.tick_paused,
        }

    def tick(self) -> dict:
        with STATE.lock:
            broker = STATE.broker()
            symbols = self._resolve_universe(broker)
            quotes = broker.quotes(symbols)
            STATE.last_quotes = quotes
            STATE.last_quote = quotes[0] if quotes else None
            STATE.last_tick_at = datetime.now(timezone.utc).isoformat()

            STATE.audit(AuditEventType.TICK, detail={"symbols": symbols, "count": len(quotes)})

            if STATE.settings.strategy_enabled and self._session_expired():
                STATE.settings.strategy_enabled = False
                if STATE.settings.stop_at_end:
                    self._close_positions_at_end(quotes)

            if STATE.settings.strategy_enabled:
                signals, proposals = STATE.strategy.scan(STATE.settings, quotes, broker.portfolio())
                STATE.signals = signals

                # Audit every signal
                for sig in signals:
                    STATE.audit(
                        AuditEventType.SIGNAL,
                        symbol=sig.symbol,
                        action=sig.action,
                        score=sig.score,
                        price=sig.price,
                        reason=sig.reason,
                    )

                pending = {
                    (item.symbol, item.side)
                    for item in STATE.proposals
                    if item.status is OrderStatus.PROPOSED
                }
                for proposal in proposals:
                    if (proposal.symbol, proposal.side) in pending:
                        continue
                    STATE.proposals.append(proposal)
                    pending.add((proposal.symbol, proposal.side))
                    STATE.audit(
                        AuditEventType.PROPOSAL,
                        symbol=proposal.symbol,
                        side=proposal.side.value,
                        quantity=proposal.quantity,
                        price=proposal.price,
                        confidence=proposal.confidence,
                    )
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
            for key in (
                "symbol", "markets", "universe", "budget", "duration_minutes",
                "max_scan_symbols", "max_loss", "max_trade_value", "auto_tick_enabled",
                "tick_interval_seconds", "allow_live_trading", "stop_at_end", "strategy_enabled",
            ):
                if key in payload:
                    setattr(settings, key, payload[key])
            if "trading_mode" in payload:
                settings.trading_mode = TradingMode(payload["trading_mode"])
            if "approval_mode" in payload:
                settings.approval_mode = ApprovalMode(payload["approval_mode"])
            settings.normalized()
            if settings.strategy_enabled and settings.started_at is None:
                settings.started_at = datetime.now(timezone.utc).isoformat()
            if not settings.strategy_enabled:
                settings.started_at = None
            STATE.save()
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
        proposal.status = OrderStatus.APPROVED
        try:
            if STATE.settings.trading_mode is TradingMode.LIVE and not STATE.settings.allow_live_trading:
                raise RuntimeError("Live order submission is disabled. Enable it only when you want real orders sent.")
            STATE.broker().submit_order(proposal)
            proposal.status = OrderStatus.FILLED if proposal.error is None else OrderStatus.FAILED
        except Exception as exc:
            proposal.status = OrderStatus.FAILED
            proposal.error = str(exc)

        event = AuditEventType.FILL if proposal.status is OrderStatus.FILLED else AuditEventType.FAIL
        STATE.audit(
            event,
            symbol=proposal.symbol,
            side=proposal.side.value,
            quantity=proposal.quantity,
            price=proposal.price,
            error=proposal.error,
        )

        if STATE.settings.trading_mode is TradingMode.PAPER:
            STATE.log_trade(proposal)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

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
                return list(dict.fromkeys(discovered))[: STATE.settings.max_scan_symbols]

        STATE.universe_source = "sample fallback"
        return STATE.settings.active_universe()

    def _close_positions_at_end(self, quotes) -> None:
        portfolio = STATE.broker().portfolio()
        latest = {quote.symbol: quote for quote in quotes}
        for symbol, position in portfolio.positions.items():
            if position.quantity <= 0 or symbol not in latest:
                continue
            quote = latest[symbol]
            proposal = OrderProposal(
                symbol=quote.symbol,
                side=Side.SELL,
                quantity=position.quantity,
                price=quote.price,
                confidence=1.0,
                reason="Trading duration ended and stop-at-end is enabled.",
            )
            STATE.proposals.append(proposal)
            if STATE.settings.approval_mode is ApprovalMode.AUTO:
                self.execute(proposal)

    def _find_proposal(self, proposal_id: str):
        return next((p for p in STATE.proposals if p.id == proposal_id), None)


ENGINE = TradingEngine()


# ---------------------------------------------------------------------------
# Auto-tick loop
# ---------------------------------------------------------------------------

def auto_tick_loop() -> None:
    while True:
        try:
            should_tick = (
                STATE.settings.auto_tick_enabled
                and STATE.settings.strategy_enabled
                and not STATE.tick_paused
            )
            if should_tick:
                ENGINE.tick()
            time.sleep(max(5, STATE.settings.tick_interval_seconds))
        except Exception:
            time.sleep(10)


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/":
            self._send_file(STATIC / "index.html", "text/html")
            return
        if path == "/api/status":
            self._json(ENGINE.status())
            return
        if path == "/api/tick":
            self._json(ENGINE.tick())
            return
        if path == "/api/tick/pause":
            self._json(ENGINE.pause_tick())
            return
        if path == "/api/tick/resume":
            self._json(ENGINE.resume_tick())
            return
        if path == "/api/audit":
            self._serve_jsonl_tail(AUDIT_LOG, 100)
            return
        if path == "/api/backtest/last":
            self._serve_jsonl_tail(BACKTEST_LOG, 1)
            return
        if path.startswith("/static/"):
            target = STATIC / path.removeprefix("/static/")
            content_type = "text/css" if target.suffix == ".css" else "application/javascript"
            self._send_file(target, content_type)
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path == "/api/settings":
            self._json(ENGINE.update_settings(self._read_json()))
            return
        if path == "/api/tick/pause":
            self._json(ENGINE.pause_tick())
            return
        if path == "/api/tick/resume":
            self._json(ENGINE.resume_tick())
            return
        if path.startswith("/api/proposals/") and path.endswith("/approve"):
            proposal_id = path.split("/")[3]
            if not ENGINE.approve(proposal_id):
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            self._json(ENGINE.status())
            return
        if path.startswith("/api/proposals/") and path.endswith("/reject"):
            proposal_id = path.split("/")[3]
            if not ENGINE.reject(proposal_id):
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            self._json(ENGINE.status())
            return
        if path == "/api/backtest":
            payload = self._read_json()
            symbols = payload.get("symbols") or STATE.settings.active_universe()
            ticks = max(10, min(500, int(payload.get("ticks", 60))))
            starting_cash = float(payload.get("starting_cash", 10000.0))
            result = run_backtest(symbols, ticks=ticks, starting_cash=starting_cash)
            self._json(result)
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def log_message(self, format: str, *args) -> None:
        return

    def _read_json(self) -> dict:
        length = int(self.headers.get("content-length", "0"))
        if length == 0:
            return {}
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def _json(self, payload: dict) -> None:
        data = json.dumps(payload).encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _serve_jsonl_tail(self, path: Path, n: int) -> None:
        if not path.exists():
            self._json({"entries": []})
            return
        lines = path.read_text().strip().splitlines()
        entries = [json.loads(line) for line in lines[-n:] if line.strip()]
        self._json({"entries": entries})

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
    server = ThreadingHTTPServer(("127.0.0.1", 8765), Handler)
    print("Trading tool running at http://127.0.0.1:8765")
    server.serve_forever()


if __name__ == "__main__":
    main()
