from __future__ import annotations

import dataclasses
import os
import random
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

from .models import OrderProposal, OrderStatus, Portfolio, Position, Quote, Side


def _load_dotenv() -> None:
    """Load ALL recognised vars from a .env file if present.
    Searches: script dir first, then cwd, then home. Does NOT override existing env vars."""
    _here = Path(__file__).resolve().parent
    candidates = [_here / ".env", Path.cwd() / ".env", Path.home() / ".env"]
    for candidate in candidates:
        if not candidate.exists():
            continue
        try:
            for line in candidate.read_text().splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                key = key.strip()
                val = val.strip().strip('"').strip("'")
                if key and val and key not in os.environ:
                    os.environ[key] = val
        except Exception:
            pass
        break   # stop after first found


# Load .env on import so credentials are available before any broker is created
_load_dotenv()


# Module-level connection status — surfaced to the UI
LB_STATUS: dict = {"connected": False, "error": None}

# Paper-trading friction model. Without these, paper fills execute at the
# exact last price with zero fees, which systematically overstates P&L —
# especially for high-turnover scalping. Override in .env if needed.
PAPER_FEE_PER_TRADE = float(os.environ.get("PAPER_FEE_PER_TRADE", "1.0"))
PAPER_SLIPPAGE_BPS = float(os.environ.get("PAPER_SLIPPAGE_BPS", "5.0"))


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
    available; falls back to a random-walk simulator when they are not.

    Connection to Longbridge is retried automatically (every RETRY_SECONDS)
    if the initial attempt — or any later attempt — fails, instead of
    permanently giving up after one failure."""

    RETRY_SECONDS = 20.0

    def __init__(
        self,
        starting_cash: float = 0.0,
        portfolio: Portfolio | None = None,
        prices: dict[str, float] | None = None,
    ) -> None:
        self._portfolio = portfolio or Portfolio(cash=starting_cash)
        self._prices: dict[str, float] = prices or {}
        self._sim_day: dict[str, dict] = {}
        self._lb: LongbridgeBroker | None = None
        self._lb_last_attempt: float = 0.0
        self._try_connect_lb(force=True)

    def _try_connect_lb(self, force: bool = False) -> None:
        """(Re)try connecting to Longbridge. Safe to call repeatedly — only
        actually attempts a new connection every RETRY_SECONDS unless forced."""
        now = time.monotonic()
        if not force and (now - self._lb_last_attempt) < self.RETRY_SECONDS:
            return
        self._lb_last_attempt = now
        try:
            self._lb = LongbridgeBroker()
            LB_STATUS["connected"] = True
            LB_STATUS["error"] = None
        except Exception as e:
            self._lb = None
            LB_STATUS["connected"] = False
            LB_STATUS["error"] = str(e)

    # ------------------------------------------------------------------
    # Quote helpers
    # ------------------------------------------------------------------

    def _update_peaks(self) -> None:
        """Track each held position's high-water mark — drives trailing stops."""
        for symbol, pos in self._portfolio.positions.items():
            if pos.quantity <= 0:
                continue
            last = self._portfolio.last_prices.get(symbol, 0.0)
            if last > pos.peak_price:
                pos.peak_price = last

    def quote(self, symbol: str) -> Quote:
        if self._lb is None:
            self._try_connect_lb()   # retry — connection may have been transient
        if self._lb is not None:
            try:
                q = self._lb.quote(symbol)
                self._prices[symbol] = q.price
                self._portfolio.last_prices[symbol] = q.price
                LB_STATUS["connected"] = True
                LB_STATUS["error"] = None
                self._update_peaks()
                return dataclasses.replace(q, source="longbridge-paper")
            except Exception as e:
                # A previously-working connection just failed — drop it so
                # _try_connect_lb() will attempt a fresh reconnect next time.
                self._lb = None
                LB_STATUS["connected"] = False
                LB_STATUS["error"] = f"Quote fetch failed: {e}"
        return self._simulated_quote(symbol)

    def quotes(self, symbols: list[str]) -> list[Quote]:
        if self._lb is None:
            self._try_connect_lb()   # retry — connection may have been transient
        if self._lb is not None:
            try:
                qs = self._lb.quotes(symbols)
                for q in qs:
                    self._prices[q.symbol] = q.price
                    self._portfolio.last_prices[q.symbol] = q.price
                LB_STATUS["connected"] = True
                LB_STATUS["error"] = None
                self._update_peaks()
                return [dataclasses.replace(q, source="longbridge-paper") for q in qs]
            except Exception as e:
                # A previously-working connection just failed — drop it so
                # _try_connect_lb() will attempt a fresh reconnect next time.
                self._lb = None
                LB_STATUS["connected"] = False
                LB_STATUS["error"] = f"Quote fetch failed: {e}"
        # Fall back: use last known real price as seed so sim starts from reality
        result = [self._simulated_quote(s) for s in symbols]
        self._update_peaks()
        return result

    def candles(self, symbol: str, period: str = "Min_1", count: int = 120) -> list[dict]:
        """Real candlesticks when Longbridge is connected; [] otherwise."""
        if self._lb is None:
            self._try_connect_lb()
        if self._lb is not None:
            try:
                return self._lb.candles(symbol, period=period, count=count)
            except Exception:
                return []
        return []

    def discover_symbols(self, markets: list[str]) -> list[str]:
        if self._lb is None:
            self._try_connect_lb()
        if self._lb is not None:
            try:
                return self._lb.discover_symbols(markets)
            except Exception:
                self._lb = None
        return []

    def submit_order(self, proposal: OrderProposal) -> OrderProposal:
        symbol = proposal.symbol
        last = self._prices.get(symbol, proposal.price)
        quantity = round(float(proposal.quantity), 6)
        slip = PAPER_SLIPPAGE_BPS / 10_000.0
        fee = PAPER_FEE_PER_TRADE
        position = self._portfolio.positions.setdefault(symbol, Position(symbol=symbol))

        if proposal.side is Side.BUY:
            price = last * (1.0 + slip)   # buys fill slightly above last
            notional = round(price * quantity, 6)
            if notional + fee > self._portfolio.cash:
                proposal.error = "Insufficient paper cash (incl. fee)."
                return proposal
            new_quantity = round(position.quantity + quantity, 6)
            position.avg_cost = ((position.quantity * position.avg_cost) + notional) / new_quantity
            position.quantity = new_quantity
            position.peak_price = max(position.peak_price, price)
            self._portfolio.cash -= notional + fee
            self._portfolio.realized_pnl -= fee
        else:
            price = last * (1.0 - slip)   # sells fill slightly below last
            if quantity > position.quantity + 1e-9:
                proposal.error = "Cannot sell more than the current paper position."
                return proposal
            quantity = min(quantity, position.quantity)
            notional = round(price * quantity, 6)
            self._portfolio.cash += notional - fee
            self._portfolio.realized_pnl += quantity * (price - position.avg_cost) - fee
            position.quantity = round(position.quantity - quantity, 6)
            if position.quantity < 1e-9:
                position.quantity = 0.0
                position.avg_cost = 0.0
                position.peak_price = 0.0

        proposal.price = round(price, 2)
        proposal.quantity = quantity
        self._portfolio.cash = round(self._portfolio.cash, 2)
        self._portfolio.realized_pnl = round(self._portfolio.realized_pnl, 6)
        return proposal

    def portfolio(self) -> Portfolio:
        return self._portfolio

    def snapshot(self) -> dict:
        # Persist prices only for symbols we actually hold — the full price
        # maps mirror the scan universe (32k+ symbols under Longbridge
        # discovery) and rewriting them to disk on every tick churns storage.
        held = {s for s, p in self._portfolio.positions.items() if p.quantity > 0}
        return {
            "portfolio": {
                "cash": self._portfolio.cash,
                "realized_pnl": self._portfolio.realized_pnl,
                "positions": {
                    symbol: {"symbol": pos.symbol, "quantity": pos.quantity,
                             "avg_cost": pos.avg_cost, "peak_price": pos.peak_price}
                    for symbol, pos in self._portfolio.positions.items()
                },
                "last_prices": {s: p for s, p in self._portfolio.last_prices.items() if s in held},
            },
            "prices": {s: p for s, p in self._prices.items() if s in held},
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _simulated_quote(self, symbol: str) -> Quote:
        previous = self._prices.get(symbol)
        if previous is None:
            # No cached price at all — use a random seed
            # NOTE: this only happens when Longbridge is not connected AND
            # we have never fetched a real price for this symbol.
            # If LB was connected before and then failed, self._prices will
            # still have the last real price, so we won't hit this branch.
            previous = self._seed_price(symbol)
        drift = random.uniform(-0.008, 0.008)
        price = max(0.01, previous * (1.0 + drift))
        self._prices[symbol] = round(price, 4)
        self._portfolio.last_prices[symbol] = self._prices[symbol]
        # Synthetic day context so the strategy exercises the same code path
        # it uses with real data (still clearly labeled paper-sim).
        day = self._sim_day.setdefault(symbol, {
            "open": self._prices[symbol],
            "prev_close": round(self._prices[symbol] * (1.0 + random.uniform(-0.01, 0.01)), 4),
            "high": self._prices[symbol],
            "low": self._prices[symbol],
            "volume": 0.0,
        })
        day["high"] = max(day["high"], self._prices[symbol])
        day["low"] = min(day["low"], self._prices[symbol])
        day["volume"] += random.uniform(1_000, 50_000)
        return Quote(
            symbol=symbol,
            price=self._prices[symbol],
            timestamp=datetime.now(timezone.utc).isoformat(),
            source="paper-sim",
            prev_close=day["prev_close"],
            open=day["open"],
            high=day["high"],
            low=day["low"],
            volume=day["volume"],
            turnover=day["volume"] * self._prices[symbol],
        )

    def _seed_price(self, symbol: str) -> float:
        """Only used when no real price has ever been fetched for this symbol.
        These are intentionally realistic ranges — but you will see wrong prices
        if Longbridge is not connected. Check the LB status indicator in the UI."""
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
        # Optional — candlestick support varies by SDK version
        try:
            from longbridge.openapi import AdjustType, Period
            self.Period = Period
            self.AdjustType = AdjustType
        except ImportError:
            self.Period = None
            self.AdjustType = None
        config = Config.from_apikey_env()
        self.quote_ctx = QuoteContext(config)
        self.trade_ctx = TradeContext(config)
        self._portfolio = Portfolio(cash=0.0)
        self._lot_sizes: dict[str, int] = {}

    # ------------------------------------------------------------------
    # Quotes
    # ------------------------------------------------------------------

    # Longbridge enforces a hard cap on symbols per quote() call. Sending more
    # than this in one request fails or is silently truncated/rejected by the
    # API, which previously caused the broker to look "disconnected" even
    # though the credentials and connection were perfectly fine.
    MAX_QUOTE_BATCH = 200

    @staticmethod
    def _to_quote(item, source: str = "longbridge") -> Quote:
        """Convert an SDK SecurityQuote into our enriched Quote. Every extra
        field is optional — missing attributes simply stay 0.0."""
        def num(attr: str) -> float:
            try:
                return float(getattr(item, attr, 0) or 0)
            except (TypeError, ValueError):
                return 0.0
        return Quote(
            symbol=item.symbol,
            price=num("last_done"),
            timestamp=datetime.now(timezone.utc).isoformat(),
            source=source,
            prev_close=num("prev_close"),
            open=num("open"),
            high=num("high"),
            low=num("low"),
            volume=num("volume"),
            turnover=num("turnover"),
        )

    def quote(self, symbol: str) -> Quote:
        q = self._to_quote(self.quote_ctx.quote([symbol])[0])
        self._portfolio.last_prices[symbol] = q.price
        return q

    def quotes(self, symbols: list[str]) -> list[Quote]:
        if not symbols:
            return []
        result: list[Quote] = []
        # Batch into chunks — never send more than MAX_QUOTE_BATCH symbols at once
        for i in range(0, len(symbols), self.MAX_QUOTE_BATCH):
            chunk = symbols[i:i + self.MAX_QUOTE_BATCH]
            for item in self.quote_ctx.quote(chunk):
                q = self._to_quote(item)
                self._portfolio.last_prices[q.symbol] = q.price
                result.append(q)
        return result

    def candles(self, symbol: str, period: str = "Min_1", count: int = 120) -> list[dict]:
        """Recent candlesticks as [{close, high, low, volume, turnover}, ...],
        oldest first. Returns [] when the SDK version has no candlestick API."""
        if self.Period is None:
            return []
        p = getattr(self.Period, period, None)
        adjust = getattr(self.AdjustType, "NoAdjust", None) if self.AdjustType else None
        if p is None or adjust is None:
            return []
        bars = self.quote_ctx.candlesticks(symbol, p, count, adjust)
        out = []
        for b in bars or []:
            def num(attr: str) -> float:
                try:
                    return float(getattr(b, attr, 0) or 0)
                except (TypeError, ValueError):
                    return 0.0
            out.append({
                "close": num("close"), "open": num("open"),
                "high": num("high"), "low": num("low"),
                "volume": num("volume"), "turnover": num("turnover"),
            })
        return out

    def discover_symbols(self, markets: list[str]) -> list[str]:
        """Discover as many tradeable symbols as possible from Longbridge.

        Strategy per market:
          US  — security_list(Overnight) gives the full Longbridge US universe
          HK  — index constituents: HSI (^HSI), HSCEI (^HSCEI), HSTECH (^HSTECH)
          SG  — index constituents: STI (^STI)

        All results are deduplicated and capped by max_scan_symbols (0 = unlimited).
        Falls back to empty list so caller can use DEFAULT_UNIVERSES.
        """
        symbols: list[str] = []
        seen: set[str] = set()

        def add(sym: str) -> None:
            if sym and sym not in seen:
                seen.add(sym)
                symbols.append(sym)

        market_map = {
            "US": getattr(self.Market, "US", None),
            "HK": getattr(self.Market, "HK", None),
            "SG": getattr(self.Market, "SG", None),
        }

        # ── US: security_list gives a large universe directly ──────────
        if "US" in markets:
            lb_market = market_map.get("US")
            if lb_market:
                for cat_name in ("Overnight", "All", "Normal"):
                    cat = getattr(self.SecurityListCategory, cat_name, None)
                    if cat is None:
                        continue
                    try:
                        responses = self.quote_ctx.security_list(lb_market, cat)
                        for item in responses or []:
                            add(getattr(item, "symbol", None))
                        if responses:
                            break
                    except Exception:
                        continue

        # ── HK: pull HSI + HSCEI + HSTECH index constituents ──────────
        if "HK" in markets:
            hk_indices = ["^HSI", "^HSCEI", "^HSTECH", "^HCCI"]
            for idx in hk_indices:
                try:
                    resp = self.quote_ctx.index_constituents(idx)
                    for sym in getattr(resp, "constituents", []) or []:
                        s = getattr(sym, "symbol", sym) if not isinstance(sym, str) else sym
                        if s and s.endswith(".HK"):
                            add(s)
                except Exception:
                    pass
            # Also try security_list for HK if available
            lb_market = market_map.get("HK")
            if lb_market:
                for cat_name in ("Overnight", "All", "Normal"):
                    cat = getattr(self.SecurityListCategory, cat_name, None)
                    if cat is None:
                        continue
                    try:
                        responses = self.quote_ctx.security_list(lb_market, cat)
                        for item in responses or []:
                            sym = getattr(item, "symbol", None)
                            if sym and sym.endswith(".HK"):
                                add(sym)
                        if responses:
                            break
                    except Exception:
                        continue

        # ── SG: pull STI index constituents ────────────────────────────
        if "SG" in markets:
            sg_indices = ["^STI"]
            for idx in sg_indices:
                try:
                    resp = self.quote_ctx.index_constituents(idx)
                    for sym in getattr(resp, "constituents", []) or []:
                        s = getattr(sym, "symbol", sym) if not isinstance(sym, str) else sym
                        if s and s.endswith(".SG"):
                            add(s)
                except Exception:
                    pass
            lb_market = market_map.get("SG")
            if lb_market:
                for cat_name in ("Overnight", "All", "Normal"):
                    cat = getattr(self.SecurityListCategory, cat_name, None)
                    if cat is None:
                        continue
                    try:
                        responses = self.quote_ctx.security_list(lb_market, cat)
                        for item in responses or []:
                            sym = getattr(item, "symbol", None)
                            if sym and sym.endswith(".SG"):
                                add(sym)
                        if responses:
                            break
                    except Exception:
                        continue

        return symbols

    # ------------------------------------------------------------------
    # Orders
    # ------------------------------------------------------------------

    def lot_size(self, symbol: str) -> int:
        """Board-lot size from exchange static info, cached. US stocks are 1;
        HK stocks trade in lots of 100/500/2000 etc. — orders that aren't a
        lot multiple are rejected by the exchange."""
        cached = self._lot_sizes.get(symbol)
        if cached:
            return cached
        lot = 1
        try:
            infos = self.quote_ctx.static_info([symbol])
            if infos:
                lot = int(getattr(infos[0], "lot_size", 0) or 0) or 1
        except Exception:
            lot = 1
        self._lot_sizes[symbol] = lot
        return lot

    # Fill confirmation: poll this many times, this far apart, before giving
    # up and reporting the order as "accepted but not confirmed filled".
    FILL_POLLS = 3
    FILL_POLL_GAP = 1.5

    def _find_order(self, order_id) -> object | None:
        detail = getattr(self.trade_ctx, "order_detail", None)
        if detail is not None:
            try:
                return detail(order_id)
            except Exception:
                pass
        try:
            for order in self.trade_ctx.today_orders() or []:
                if str(getattr(order, "order_id", "")) == str(order_id):
                    return order
        except Exception:
            pass
        return None

    def submit_order(self, proposal: OrderProposal) -> OrderProposal:
        side = self.OrderSide.Buy if proposal.side is Side.BUY else self.OrderSide.Sell
        # Longbridge takes whole shares only, in board-lot multiples.
        lot = self.lot_size(proposal.symbol)
        whole = (int(proposal.quantity) // lot) * lot
        if whole <= 0:
            proposal.status = OrderStatus.FAILED
            proposal.error = (
                f"Quantity {proposal.quantity:g} is below one tradable unit "
                f"(lot size {lot}) — increase max trade value or pick a cheaper symbol."
            )
            return proposal
        proposal.quantity = float(whole)
        response = self.trade_ctx.submit_order(
            proposal.symbol,
            self.OrderType.LO,
            side,
            self.Decimal(whole),
            self.TimeInForceType.Day,
            submitted_price=self.Decimal(str(proposal.price)),
            remark=f"trading-tool:{proposal.id}",
        )

        # ── Fill confirmation ─────────────────────────────────────────────
        # Never assume a limit order filled: poll its status and report what
        # actually happened so P&L tracks reality.
        order_id = getattr(response, "order_id", None)
        proposal.status = OrderStatus.APPROVED
        if order_id is None:
            proposal.error = "Order submitted — broker returned no order id, fill unconfirmed."
            return proposal
        for _ in range(self.FILL_POLLS):
            time.sleep(self.FILL_POLL_GAP)
            order = self._find_order(order_id)
            if order is None:
                continue
            status_name = str(getattr(order, "status", ""))
            if "Filled" in status_name and "Partial" not in status_name:
                proposal.status = OrderStatus.FILLED
                executed = getattr(order, "executed_price", None)
                try:
                    if executed:
                        proposal.price = float(executed)
                except (TypeError, ValueError):
                    pass
                return proposal
            if any(k in status_name for k in ("Rejected", "Canceled", "Cancelled", "Expired")):
                proposal.status = OrderStatus.FAILED
                msg = getattr(order, "msg", "") or status_name
                proposal.error = f"Order {status_name.rsplit('.', 1)[-1]} by exchange: {msg}"
                return proposal
        proposal.error = (
            f"Order {order_id} accepted by exchange — fill not confirmed within "
            f"{self.FILL_POLLS * self.FILL_POLL_GAP:.0f}s (limit order may still fill; "
            "check the Longbridge app)."
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