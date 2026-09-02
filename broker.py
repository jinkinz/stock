from __future__ import annotations

import dataclasses
import os
import random
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

from models import OrderProposal, OrderStatus, Portfolio, Position, Quote, Side


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
# especially for high-turnover scalping.
#
# Fees come from the per-market schedules in fees.py (SG measured from real
# contract notes; US/HK are flagged estimates). Setting PAPER_FEE_PER_TRADE in
# .env overrides the whole model with a flat per-order charge — useful for a
# quick what-if, but it will not resemble a real bill.
PAPER_FEE_OVERRIDE = os.environ.get("PAPER_FEE_PER_TRADE", "").strip()
PAPER_FEE_PER_TRADE = float(PAPER_FEE_OVERRIDE) if PAPER_FEE_OVERRIDE else None
PAPER_SLIPPAGE_BPS = float(os.environ.get("PAPER_SLIPPAGE_BPS", "5.0"))


def paper_fee(symbol: str, side: str, quantity: float, price: float) -> float:
    """Modelled brokerage cost of one paper fill, in the symbol's currency."""
    if PAPER_FEE_PER_TRADE is not None:
        return PAPER_FEE_PER_TRADE
    from fees import estimate_fee
    from market_hours import market_of
    return estimate_fee(market_of(symbol), side, quantity, price)


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
        # Fee depends on side, size and price, so it is computed per fill from
        # the market's real schedule rather than being a flat constant.
        fee = paper_fee(symbol, proposal.side.value, quantity,
                        last * (1.0 + slip if proposal.side is Side.BUY else 1.0 - slip))
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
            # Round-trip accounting: entry_price mirrors avg_cost but is keyed
            # off entry_qty so it survives the position going flat.
            new_entry_qty = round(position.entry_qty + quantity, 6)
            position.entry_price = ((position.entry_qty * position.entry_price) + notional) / new_entry_qty
            position.entry_qty = new_entry_qty
            position.fees_paid = round(position.fees_paid + fee, 6)
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
            position.exit_qty = round(position.exit_qty + quantity, 6)
            position.exit_proceeds = round(position.exit_proceeds + notional, 6)
            position.fees_paid = round(position.fees_paid + fee, 6)
            position.quantity = round(position.quantity - quantity, 6)
            if position.quantity < 1e-9:
                position.quantity = 0.0
                position.avg_cost = 0.0
                position.peak_price = 0.0
                # The next entry sets its own stop from fresh ATR; carrying a
                # stale one over would protect the new position at the old
                # position's price.
                position.stop_price = 0.0
                # Entry context is deliberately NOT cleared here — the engine
                # reads it right after this call to emit the round-trip record,
                # then calls Position.reset_round_trip().

        proposal.price = round(price, 2)
        proposal.quantity = quantity
        proposal.fee = round(fee, 4)
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
                    symbol: {
                        "symbol": pos.symbol, "quantity": pos.quantity,
                        "avg_cost": pos.avg_cost, "peak_price": pos.peak_price,
                        "stop_price": pos.stop_price,
                        # Entry context must round-trip through disk, or a
                        # restart mid-position loses the trade record.
                        "opened_at": pos.opened_at, "entry_price": pos.entry_price,
                        "entry_qty": pos.entry_qty, "entry_score": pos.entry_score,
                        "entry_strategy": pos.entry_strategy, "entry_mode": pos.entry_mode,
                        "entry_diagnostics": pos.entry_diagnostics,
                        "entry_config": pos.entry_config,
                        "entry_confirmations": list(pos.entry_confirmations),
                        "breakeven_armed": pos.breakeven_armed,
                        "fees_paid": pos.fees_paid, "exit_qty": pos.exit_qty,
                        "exit_proceeds": pos.exit_proceeds,
                    }
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


def _pre_market_fields(item) -> dict:
    """Pre-market price/gap/turnover from a SecurityQuote, or zeros.

    Longbridge exposes `pre_market_quote` as a PrePostQuote. Not every market
    has a pre-open session and it is empty outside those hours, so every field
    degrades to 0.0 rather than raising — callers must treat 0.0 as "no
    pre-market data", never as "unchanged".
    """
    pre = getattr(item, "pre_market_quote", None)
    if pre is None:
        return {}
    def num(attr: str) -> float:
        try:
            return float(getattr(pre, attr, 0) or 0)
        except (TypeError, ValueError):
            return 0.0
    price, prev = num("last_done"), num("prev_close")
    if price <= 0:
        return {}
    return {
        "pre_market_price": price,
        "pre_market_change_pct": round((price / prev - 1.0) * 100, 3) if prev > 0 else 0.0,
        "pre_market_turnover": num("turnover"),
    }


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
        # currency -> available cash, refreshed by portfolio(). The live budget
        # guard checks an order against the balance in ITS OWN currency; a
        # USD-only reading cannot cover an SGD order.
        self._cash_by_currency: dict[str, float] = {}

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
            # e.g. "TradeStatus.Normal" -> "normal". Anything else is a symbol
            # the exchange is not trading normally right now.
            trade_status=str(getattr(item, "trade_status", "") or "normal").rsplit(".", 1)[-1].lower(),
            **_pre_market_fields(item),
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
        """Recent candlesticks as [{close, high, low, volume, turnover, timestamp}, ...],
        oldest first. Returns [] when the SDK version has no candlestick API.

        `timestamp` is an ISO string (or "" when the SDK omits it) — the
        benchmark needs it to align bars to a metrics window."""
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
            raw_ts = getattr(b, "timestamp", None)
            try:
                stamp = raw_ts.isoformat() if hasattr(raw_ts, "isoformat") else (str(raw_ts) if raw_ts else "")
            except Exception:
                stamp = ""
            out.append({
                "close": num("close"), "open": num("open"),
                "high": num("high"), "low": num("low"),
                "volume": num("volume"), "turnover": num("turnover"),
                "timestamp": stamp,
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

    def cash_max_quantity(self, symbol: str, price: float) -> float | None:
        """Largest quantity of `symbol` buyable at `price` out of SETTLED CASH,
        per the broker's own estimate. Returns None when the figure cannot be
        obtained — callers must read that as "unproven", never as "fine".

        This is the ONLY reading that separates cash from financing.
        `account_balance().available_cash` does not: on a financing-enabled
        account it already includes the loan, so an order can clear a balance
        check and still settle on borrowed money.

        Deliberately reads `cash_max_qty` and never `margin_max_qty`. The
        response carries both, and the difference between them IS the debt.

        Costs one trade-context call per check (TradingRateLimiter, 30/30s).
        Not cached on purpose: the number moves with every fill, and acting on
        a stale cash bound is the exact failure this exists to prevent.
        """
        try:
            estimate = self.trade_ctx.estimate_max_purchase_quantity(
                symbol=symbol,
                order_type=self.OrderType.LO,
                side=self.OrderSide.Buy,
                price=self.Decimal(str(price)),
            )
        except Exception:
            return None
        raw = getattr(estimate, "cash_max_qty", None)
        if raw is None:
            return None
        try:
            return max(0.0, float(raw))
        except (TypeError, ValueError):
            return None

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

    @staticmethod
    def _charged_fee(order) -> float:
        """Total fee actually billed on a filled order, from charge_detail.
        Returns 0.0 when the broker didn't supply one — the caller must not
        assume 0 means free."""
        detail = getattr(order, "charge_detail", None)
        if detail is None:
            return 0.0
        try:
            total = float(getattr(detail, "total_amount", 0) or 0)
        except (TypeError, ValueError):
            return 0.0
        if total > 0:
            return round(total, 4)
        # Some responses itemise without a total.
        summed = 0.0
        for item in getattr(detail, "items", []) or []:
            for fee in getattr(item, "fees", []) or []:
                try:
                    summed += float(getattr(fee, "amount", 0) or 0)
                except (TypeError, ValueError):
                    continue
        return round(summed, 4)

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
                # Real charges, straight from the contract note — no modelling
                # needed once the broker has billed it.
                proposal.fee = self._charged_fee(order)
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

    def cash_by_currency(self) -> dict[str, float]:
        """Available cash per currency as of the last portfolio() sync.

        NOTE: this is the broker's `available_cash`. On a margin account that
        figure can include borrowing power rather than settled cash, so it is
        NOT proof that an order is cash-covered. The budget ceiling in
        TradingEngine is the binding constraint; this is a second bound.
        """
        return dict(self._cash_by_currency)

    def portfolio(self) -> Portfolio:
        """Sync cash and stock positions from Longbridge account."""
        # --- cash ---
        try:
            balances = self.trade_ctx.account_balance()
            by_currency: dict[str, float] = {}
            for balance in balances:
                for info in getattr(balance, "cash_infos", []) or []:
                    currency = str(getattr(info, "currency", "") or "").upper()
                    if not currency:
                        continue
                    amount = float(getattr(info, "available_cash", 0) or 0)
                    by_currency[currency] = by_currency.get(currency, 0.0) + amount
            self._cash_by_currency = {c: round(v, 2) for c, v in by_currency.items()}
            # portfolio.cash stays USD — it feeds equity() and the UI, both of
            # which are single-currency. Order sizing must use
            # cash_by_currency(), never this field.
            self._portfolio.cash = round(by_currency.get("USD", 0.0), 2)
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
                        # Merge, don't replace: a plain overwrite would wipe the
                        # round-trip entry context (opened_at, entry_price,
                        # score, fees) on every sync, so live trades could never
                        # be closed out into the ledger.
                        existing = self._portfolio.positions.get(symbol)
                        if existing is not None:
                            existing.quantity = qty
                            existing.avg_cost = cost_price
                            synced[symbol] = existing
                        else:
                            synced[symbol] = Position(symbol=symbol, quantity=qty, avg_cost=cost_price)
            if synced:
                # Keep flat-but-unlogged positions around so the engine can
                # still emit their closed-trade record on the next pass.
                for symbol, pos in self._portfolio.positions.items():
                    if symbol not in synced and pos.entry_qty > 0:
                        pos.quantity = 0.0
                        synced[symbol] = pos
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