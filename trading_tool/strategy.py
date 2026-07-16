"""
MomentumStrategy — the rule-based day-trading signal engine.

Signal inputs, in order of weight:
──────────────────────────────────
1. REAL day context from exchange quotes (when Longbridge is connected):
     day change %   — price vs previous close (the day trader's primary read)
     range position — where price sits between today's low and high
                      (buying strength near the high = breakout logic)
     turnover       — liquidity gate: illiquid symbols are never bought
2. Candle indicators from 1-minute bars (fetched for top candidates only):
     VWAP  — institutional fair value; long bias above, exit bias below
     EMA9 / EMA21 crossover — short-term trend direction
     RSI(14) — overbought (>75 no new buys) / exhaustion (>80 exit)
3. Tick momentum (the original 30-observation rolling window) — used as a
   short-term confirmation signal, and as the ONLY signal when real market
   data is unavailable (paper-sim mode).

Actions:
  BUY  — composite score ≥ 0.55, positive day momentum, liquid, not overbought
  SELL — held position showing reversal: below VWAP with bearish EMA cross,
         RSI exhaustion, or tick momentum breakdown
  (Mechanical stop-loss / trailing-stop / profit-lock exits live in the
   engine — app.py — and fire regardless of what this strategy thinks.)

What it does NOT do:
  - No options, no margin, no short-selling (only buys what cash covers,
    only sells what is already held).
  - No news or fundamentals yet (news_gate is still a stub).

Budget vs cash (important):
  budget       = the maximum STARTING cash for the paper account.
                 It is NOT re-applied as a per-tick cap.
  portfolio.cash = the actual spendable balance at any moment.
  The strategy buys against portfolio.cash directly, divided across
  however many buy signals exist, capped by max_trade_value per trade.
"""
from __future__ import annotations

import math
import statistics
from collections import deque
from datetime import datetime, timezone

from .broker import affordable_quantity
from .models import Diagnostics, OrderProposal, Portfolio, Quote, Settings, Side, Signal

# How many ticks a proposal stays valid in manual-approval mode before
# it is considered stale and should be ignored by the UI / auto-expiry.
PROPOSAL_TTL_SECONDS = 300   # 5 minutes

# Liquidity gate: never buy a symbol whose traded value today is below this.
# Illiquid names have wide spreads and unreliable fills.
MIN_TURNOVER = 500_000.0


def compute_indicators(candles: list[dict]) -> dict:
    """VWAP, EMA9/EMA21, RSI(14) from a list of 1-minute candles
    (oldest first, as returned by broker.candles)."""
    closes = [c["close"] for c in candles if c.get("close", 0) > 0]
    if len(closes) < 15:
        return {}

    def ema(values: list[float], span: int) -> float:
        k = 2.0 / (span + 1)
        e = values[0]
        for v in values[1:]:
            e = v * k + e * (1 - k)
        return e

    # RSI(14) — Wilder smoothing
    gains, losses = 0.0, 0.0
    for i in range(-14, 0):
        change = closes[i] - closes[i - 1]
        if change >= 0:
            gains += change
        else:
            losses -= change
    avg_gain, avg_loss = gains / 14, losses / 14
    rsi = 100.0 if avg_loss == 0 else 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)

    # Session VWAP — typical price weighted by volume (fallback: plain average)
    pv, vol = 0.0, 0.0
    for c in candles:
        v = c.get("volume", 0) or 0
        if v > 0 and c.get("close", 0) > 0:
            typical = (c["close"] + c.get("high", c["close"]) + c.get("low", c["close"])) / 3
            pv += typical * v
            vol += v
    vwap = pv / vol if vol > 0 else sum(closes) / len(closes)

    return {
        "ema9": ema(closes, 9),
        "ema21": ema(closes, 21),
        "rsi": round(rsi, 1),
        "vwap": vwap,
    }


class MomentumStrategy:
    # Candle indicators are considered fresh for this long
    INDICATOR_TTL = 180.0

    def __init__(self) -> None:
        self.history: dict[str, deque[float]] = {}
        self._tick_counts: dict[str, deque[int]] = {}
        self._current_ticks: dict[str, int] = {}
        # symbol -> (computed_at_monotonic, indicators dict)
        self._indicators: dict[str, tuple[float, dict]] = {}

    def ingest_candles(self, symbol: str, candles: list[dict]) -> None:
        """Feed real 1-minute candles for a symbol; computed indicators are
        used by the next scan. Called by the engine for top candidates and
        held positions only (rate-limit friendly)."""
        import time as _time
        ind = compute_indicators(candles)
        if ind:
            self._indicators[symbol] = (_time.monotonic(), ind)

    def _fresh_indicators(self, symbol: str) -> dict:
        import time as _time
        entry = self._indicators.get(symbol)
        if entry and (_time.monotonic() - entry[0]) < self.INDICATOR_TTL:
            return entry[1]
        return {}

    # ------------------------------------------------------------------
    # Observe
    # ------------------------------------------------------------------

    def observe(self, quote: Quote) -> None:
        if quote.price <= 0:
            # Skip invalid quotes (e.g. halted/delisted symbols returning 0)
            # so they never enter the rolling price history and cause
            # divide-by-zero downstream.
            return
        prices = self.history.setdefault(quote.symbol, deque(maxlen=30))
        prices.append(quote.price)
        self._current_ticks[quote.symbol] = self._current_ticks.get(quote.symbol, 0) + 1

    def flush_tick_counts(self) -> None:
        for symbol, count in self._current_ticks.items():
            self._tick_counts.setdefault(symbol, deque(maxlen=10)).append(count)
        self._current_ticks = {}

    # ------------------------------------------------------------------
    # Diagnostics
    # ------------------------------------------------------------------

    def _diagnostics(self, quote: Quote) -> Diagnostics:
        prices = list(self.history.get(quote.symbol, []))

        volatility = 0.0
        if len(prices) >= 4:
            returns = [math.log(prices[i] / prices[i - 1])
                       for i in range(1, len(prices)) if prices[i - 1] > 0]
            if len(returns) >= 2:
                vol_per_tick = statistics.stdev(returns)
                volatility = round(vol_per_tick * math.sqrt(252 * 390) * 100, 2)

        spread_pct = round(0.001 + max(0.0, volatility / 10000), 4)

        tick_windows = list(self._tick_counts.get(quote.symbol, []))
        volume_spike = False
        if tick_windows:
            avg_ticks = sum(tick_windows) / len(tick_windows)
            current = self._current_ticks.get(quote.symbol, 0)
            volume_spike = avg_ticks > 0 and current > avg_ticks * 2

        trend_strength = 0.0
        if len(prices) >= 8:
            short_avg = sum(prices[-3:]) / 3
            long_avg = sum(prices) / len(prices)
            if long_avg > 0:
                trend_strength = round(abs(short_avg / long_avg - 1.0) * 100, 3)

        # Real-market metrics from the enriched quote (all 0.0 in pure sim
        # mode before any day context exists)
        day_change_pct = 0.0
        from_high_pct = 0.0
        if quote.prev_close > 0:
            day_change_pct = round((quote.price / quote.prev_close - 1.0) * 100, 3)
        if quote.high > 0:
            from_high_pct = round((quote.price / quote.high - 1.0) * 100, 3)

        ind = self._fresh_indicators(quote.symbol)
        rsi = ind.get("rsi", 0.0)
        vwap = ind.get("vwap", 0.0)
        vwap_dist_pct = round((quote.price / vwap - 1.0) * 100, 3) if vwap > 0 else 0.0
        ema_trend = ""
        if ind.get("ema9") and ind.get("ema21"):
            ema_trend = "bull" if ind["ema9"] > ind["ema21"] else "bear"

        return Diagnostics(
            symbol=quote.symbol,
            price=quote.price,
            volatility=volatility,
            spread_pct=spread_pct,
            volume_spike=volume_spike,
            trend_strength=trend_strength,
            news_gate=True,   # stub — wire up a real news feed here
            day_change_pct=day_change_pct,
            from_high_pct=from_high_pct,
            turnover=quote.turnover,
            rsi=rsi,
            vwap_dist_pct=vwap_dist_pct,
            ema_trend=ema_trend,
        )

    # ------------------------------------------------------------------
    # Multi-symbol scan  (main entry point called every tick)
    # ------------------------------------------------------------------

    def scan(
        self,
        settings: Settings,
        quotes: list[Quote],
        portfolio: Portfolio,
    ) -> tuple[list[Signal], list[OrderProposal]]:
        for quote in quotes:
            self.observe(quote)
        self.flush_tick_counts()

        signals = [s for quote in quotes if (s := self._signal(quote, portfolio)) is not None]
        signals.sort(key=lambda s: s.score, reverse=True)

        # Circuit-breaker: no new buys if we've lost too much
        total_pnl = portfolio.realized_pnl + portfolio.unrealized_pnl()
        if total_pnl <= -settings.max_loss:
            return signals[:12], []

        proposals: list[OrderProposal] = []
        reserved_cash = 0.0
        available_cash = portfolio.cash      # trade against ACTUAL cash, not budget cap

        for signal in signals:
            if signal.diagnostics and not signal.diagnostics.news_gate:
                continue    # news gate blocked

            position = portfolio.positions.get(signal.symbol)
            held_quantity = position.quantity if position else 0.0

            # ── SELL ──────────────────────────────────────────────────
            # Only sell what we actually hold. Never short, never margin.
            if signal.action == "sell" and held_quantity > 0:
                proposals.append(OrderProposal(
                    symbol=signal.symbol,
                    side=Side.SELL,
                    quantity=held_quantity,          # sell full position
                    price=signal.price,
                    confidence=signal.score,
                    reason=signal.reason,
                ))
                continue

            # ── BUY ───────────────────────────────────────────────────
            # Only buy with cash on hand. Never borrow, never options.
            if signal.action != "buy" or held_quantity > 0:
                continue

            spendable = max(0.0, available_cash - reserved_cash)
            if spendable <= 0:
                break   # no cash left this cycle

            quantity = affordable_quantity(signal.price, settings.max_trade_value, spendable)
            if quantity <= 0:
                continue

            reserved_cash += quantity * signal.price
            proposals.append(OrderProposal(
                symbol=signal.symbol,
                side=Side.BUY,
                quantity=quantity,
                price=signal.price,
                confidence=signal.score,
                reason=signal.reason,
            ))

        return signals[:12], proposals[:5]

    # ------------------------------------------------------------------
    # Per-symbol signal scoring
    # ------------------------------------------------------------------

    def _signal(self, quote: Quote, portfolio: Portfolio) -> Signal | None:
        prices = self.history.setdefault(quote.symbol, deque(maxlen=30))
        diag = self._diagnostics(quote)
        position = portfolio.positions.get(quote.symbol)
        held_quantity = position.quantity if position else 0.0
        # Strict multi-factor scoring only applies to REAL market data. The
        # simulator fabricates day context for UI display, but its random-walk
        # prices can't satisfy VWAP/EMA confirmation — sim keeps the simple
        # tick-momentum logic so paper-sim sessions still generate trades.
        has_day_data = quote.prev_close > 0 and quote.high > 0 and quote.source != "paper-sim"

        # Tick momentum (short-term confirmation; sole signal without day data)
        momentum = 0.0
        if len(prices) >= 8:
            short_avg = sum(list(prices)[-3:]) / 3
            long_avg = sum(prices) / len(prices)
            if long_avg > 0:
                momentum = short_avg / long_avg - 1.0

        if not has_day_data:
            return self._tick_only_signal(quote, diag, momentum, held_quantity, len(prices))

        # ── Real-data scoring ────────────────────────────────────────────
        clamp = lambda v, lo, hi: max(lo, min(hi, v))
        day_chg = diag.day_change_pct
        range_pos = 0.5
        if quote.high > quote.low:
            range_pos = (quote.price - quote.low) / (quote.high - quote.low)

        reasons: list[str] = []
        score = 0.0

        # Day momentum: reward gains up to +5% (beyond that it's usually chased)
        day_component = clamp(day_chg / 5.0, -0.5, 1.0) * 0.35
        score += day_component
        if day_chg != 0:
            reasons.append(f"day {day_chg:+.2f}%")

        # Buying strength near the day high = breakout behaviour
        score += range_pos * 0.20
        if range_pos > 0.8:
            reasons.append("near day high")

        # Tick momentum confirmation
        score += clamp(momentum * 400, -1.0, 1.0) * 0.15

        # VWAP: institutions defend it — above is long territory
        if diag.vwap_dist_pct != 0.0:
            if diag.vwap_dist_pct > 0:
                score += 0.15
                reasons.append("above VWAP")
            else:
                score -= 0.15
                reasons.append("below VWAP")

        # EMA9/21 trend
        if diag.ema_trend == "bull":
            score += 0.15
            reasons.append("EMA9>21")
        elif diag.ema_trend == "bear":
            score -= 0.15
            reasons.append("EMA9<21")

        overbought = diag.rsi >= 75
        exhausted = diag.rsi >= 80
        if overbought:
            reasons.append(f"RSI {diag.rsi:.0f} overbought")
        illiquid = 0 < quote.turnover < MIN_TURNOVER

        score = clamp(score, 0.0, 0.99)
        reason_txt = ", ".join(reasons) if reasons else "no strong factors"

        # ── SELL: held position showing reversal ─────────────────────────
        if held_quantity > 0:
            reversal = (
                exhausted
                or (diag.vwap_dist_pct < 0 and diag.ema_trend == "bear")
                or momentum < -0.002
                or day_chg < -1.0
            )
            if reversal:
                return Signal(
                    symbol=quote.symbol, price=quote.price,
                    score=clamp(0.6 + abs(min(0.0, momentum)) * 20 + (0.2 if exhausted else 0.0), 0.0, 0.95),
                    action="sell",
                    reason=f"Reversal: {reason_txt}. Exit to protect P&L.",
                    diagnostics=diag,
                )
            return Signal(
                symbol=quote.symbol, price=quote.price, score=score, action="watch",
                reason=f"Holding — {reason_txt}.",
                diagnostics=diag,
            )

        # ── BUY: strong composite, liquid, not overbought ────────────────
        if score >= 0.55 and day_chg > 0 and not overbought and not illiquid:
            return Signal(
                symbol=quote.symbol, price=quote.price, score=score, action="buy",
                reason=f"Uptrend: {reason_txt}.",
                diagnostics=diag,
            )

        watch_note = "illiquid — skipped" if illiquid else reason_txt
        return Signal(
            symbol=quote.symbol, price=quote.price,
            score=clamp(score, 0.0, 0.5), action="watch",
            reason=f"Watching — {watch_note}.",
            diagnostics=diag,
        )

    def _tick_only_signal(self, quote: Quote, diag, momentum: float,
                          held_quantity: float, observations: int) -> Signal:
        """Legacy fallback when no real day data exists (pure sim mode)."""
        if observations < 8:
            return Signal(
                symbol=quote.symbol, price=quote.price, score=0.0, action="watch",
                reason="Collecting price history — needs 8 ticks before ranking.",
                diagnostics=diag,
            )
        if held_quantity > 0 and momentum < -0.002:
            return Signal(
                symbol=quote.symbol, price=quote.price,
                score=min(0.95, 0.55 + abs(momentum) * 20), action="sell",
                reason="Momentum fell below intraday baseline — exit to protect P&L.",
                diagnostics=diag,
            )
        if held_quantity == 0 and momentum > 0.002:
            return Signal(
                symbol=quote.symbol, price=quote.price,
                score=min(0.95, 0.55 + momentum * 20), action="buy",
                reason="Short-term momentum above intraday baseline.",
                diagnostics=diag,
            )
        return Signal(
            symbol=quote.symbol, price=quote.price,
            score=max(0.0, min(0.5, 0.25 + momentum * 10)), action="watch",
            reason="Momentum within neutral range — holding off.",
            diagnostics=diag,
        )