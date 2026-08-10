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

from models import Diagnostics, OrderProposal, Portfolio, Quote, Settings, Side, Signal
from sizing import size_position

# How many ticks a proposal stays valid in manual-approval mode before
# it is considered stale and should be ignored by the UI / auto-expiry.
PROPOSAL_TTL_SECONDS = 300   # 5 minutes — floor, scaled per horizon below


def proposal_ttl_seconds(tick_interval_seconds: int) -> float:
    """How long a manual proposal stays valid.

    A fixed 5 minutes expires a swing proposal before the next 15-minute scan
    even happens, so it could never be approved. Give the user at least a few
    scan cycles to decide, whatever the cadence.
    """
    return max(PROPOSAL_TTL_SECONDS, tick_interval_seconds * 3)

# Liquidity gate: never buy a symbol whose traded value today is below this.
# Illiquid names have wide spreads and unreliable fills.
MIN_TURNOVER = 500_000.0

# Bars used for the horizon-scale range and momentum measures. On daily candles
# this is roughly a trading month — the swing equivalent of "today's range".
RANGE_LOOKBACK_BARS = 20

# Typical move size per horizon, used to normalise the momentum score component.
# A +2% day is strong intraday; over 20 days it is ordinary.
TREND_SCALE_PCT = {"intraday": 5.0, "swing": 15.0}


def atr(candles: list[dict], period: int = 14) -> float:
    """Average True Range — how far this symbol typically moves per bar.

    True range is max(high−low, |high−prev_close|, |low−prev_close|), which
    counts overnight gaps that a plain high−low misses. Smoothed the Wilder
    way, consistent with the RSI implementation below.

    A flat percentage stop treats a sleepy utility and a biotech the same; ATR
    is what lets the stop and the position size adapt to the symbol. Returns
    0.0 when there aren't enough bars — callers must treat 0.0 as "unknown"
    and fall back, never as "no volatility".
    """
    if period < 1 or len(candles) < period + 1:
        return 0.0
    true_ranges: list[float] = []
    for i in range(1, len(candles)):
        current, previous = candles[i], candles[i - 1]
        close = current.get("close", 0.0) or 0.0
        high = current.get("high") or close
        low = current.get("low") or close
        prev_close = previous.get("close", 0.0) or 0.0
        if close <= 0 or prev_close <= 0:
            continue
        true_ranges.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
    if len(true_ranges) < period:
        return 0.0
    smoothed = sum(true_ranges[:period]) / period
    for tr in true_ranges[period:]:
        smoothed = (smoothed * (period - 1) + tr) / period
    return round(smoothed, 6)


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

    # Volume surge (RVOL proxy): recent-minute volume vs the session's average
    # per-minute volume. A genuine momentum move is confirmed by expanding
    # volume; a breakout on thin volume is usually a fakeout. >1.0 = heavier
    # than the session average, <1.0 = drying up.
    vols = [c.get("volume", 0) or 0 for c in candles]
    vol_surge = 0.0
    if len(vols) >= 10:
        base = sum(vols) / len(vols)
        if base > 0:
            recent = sum(vols[-3:]) / 3
            vol_surge = round(recent / base, 2)

    # Horizon-scale structure and momentum, measured in BARS rather than in
    # today's session or in the last half-hour of ticks. On daily candles these
    # are the multi-day equivalents of "near the day high" and "pushing up";
    # a multi-day position confirmed by a 30-minute tick push is a horizon
    # mismatch, not a confirmation.
    lookback = closes[-RANGE_LOOKBACK_BARS:]
    highs = [c.get("high") or c["close"] for c in candles[-RANGE_LOOKBACK_BARS:]
             if c.get("close", 0) > 0]
    lows = [c.get("low") or c["close"] for c in candles[-RANGE_LOOKBACK_BARS:]
            if c.get("close", 0) > 0]
    range_high = max(highs) if highs else 0.0
    range_low = min(lows) if lows else 0.0

    bar_momentum = 0.0
    if len(lookback) >= 5:
        recent = sum(lookback[-3:]) / 3
        baseline = sum(lookback) / len(lookback)
        if baseline > 0:
            bar_momentum = recent / baseline - 1.0
    change_lookback_pct = 0.0
    if len(lookback) >= 2 and lookback[0] > 0:
        change_lookback_pct = (lookback[-1] / lookback[0] - 1.0) * 100

    return {
        "ema9": ema(closes, 9),
        "ema21": ema(closes, 21),
        "rsi": round(rsi, 1),
        # NOTE: volume-weighted average over WHATEVER candles were supplied.
        # On 1-min bars that approximates session VWAP; on daily bars it is a
        # long-run fair-value anchor, NOT session VWAP. Same maths, different
        # meaning — do not describe it as session VWAP in swing mode.
        "vwap": vwap,
        "vol_surge": vol_surge,
        "atr": atr(candles),
        "range_high": range_high,
        "range_low": range_low,
        "bar_momentum": bar_momentum,
        "change_lookback_pct": round(change_lookback_pct, 3),
    }


class MomentumStrategy:
    # Candle indicators are considered fresh for this long
    # Default freshness window. The engine raises this to outlive the candle
    # refresh interval of the active horizon — if indicators expire before the
    # next fetch, every candle-derived factor silently reads as "unknown",
    # which the convergence gate treats as NOT confirmed.
    INDICATOR_TTL = 180.0

    def __init__(self) -> None:
        self.indicator_ttl = self.INDICATOR_TTL
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
        if entry and (_time.monotonic() - entry[0]) < self.indicator_ttl:
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
        vol_surge = ind.get("vol_surge", 0.0)
        atr_value = ind.get("atr", 0.0)
        atr_pct = round(atr_value / quote.price * 100, 3) if quote.price > 0 and atr_value > 0 else 0.0

        return Diagnostics(
            symbol=quote.symbol,
            price=quote.price,
            volatility=volatility,
            spread_pct=spread_pct,
            volume_spike=volume_spike,
            trend_strength=trend_strength,
            news_gate=True,   # stub — no news API available from Longbridge
            # Real, data-backed veto (unlike news_gate): the exchange itself
            # says whether this symbol is trading normally.
            tradable=(quote.trade_status or "normal") == "normal",
            day_change_pct=day_change_pct,
            from_high_pct=from_high_pct,
            turnover=quote.turnover,
            rsi=rsi,
            vwap_dist_pct=vwap_dist_pct,
            ema_trend=ema_trend,
            vol_surge=vol_surge,
            atr=atr_value,
            atr_pct=atr_pct,
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

        signals = [s for quote in quotes
                   if (s := self._signal(quote, portfolio, settings.min_confirmations,
                                         settings.trading_horizon)) is not None]
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
                    tag="strategy_sell",
                ))
                continue

            # ── BUY ───────────────────────────────────────────────────
            # Only buy with cash on hand. Never borrow, never options.
            if signal.action != "buy" or held_quantity > 0:
                continue

            spendable = max(0.0, available_cash - reserved_cash)
            if spendable <= 0:
                break   # no cash left this cycle

            # Risk-based sizing: the symbol's own volatility decides the
            # quantity, so every position risks the same slice of equity.
            sized = size_position(
                price=signal.price,
                atr=signal.diagnostics.atr if signal.diagnostics else 0.0,
                equity=portfolio.equity(),
                spendable=spendable,
                max_trade_value=settings.max_trade_value,
                risk_per_trade_pct=settings.risk_per_trade_pct,
                atr_stop_multiple=settings.atr_stop_multiple,
                use_atr_sizing=settings.use_atr_sizing,
                max_concurrent_positions=settings.max_concurrent_positions,
            )
            if not sized.ok:
                continue

            reserved_cash += sized.notional
            proposals.append(OrderProposal(
                symbol=signal.symbol,
                side=Side.BUY,
                quantity=sized.quantity,
                price=signal.price,
                confidence=signal.score,
                reason=f"{signal.reason} Size: {sized.reason}.",
            ))

        return signals[:12], proposals[:5]

    def scan_signals_only(self, settings: Settings, quotes: list, portfolio: Portfolio) -> list:
        """Signals without proposals. AIStrategy exposes the same method; the
        display paths call it on whichever strategy is active, so both must
        have it or swapping them crashes the refresh loop."""
        signals, _ = self.scan(settings, quotes, portfolio)
        return signals

    # ------------------------------------------------------------------
    # Per-symbol signal scoring
    # ------------------------------------------------------------------

    # ── Convergence gate ─────────────────────────────────────────────────
    # The composite score is a weighted blend, which means one strong factor
    # can carry an entry over the line while every other factor disagrees — a
    # big day move with no trend, no VWAP support and drying volume still
    # scores well. Those are the trades that lose.
    #
    # This counts INDEPENDENT confirmations instead. Each looks at a different
    # thing: direction, institutional value, participation, structure, and
    # short-term push. Requiring several to agree cuts trade count sharply,
    # which matters because fee drag is cost × frequency.
    #
    # A factor with no data (no candles fetched yet) counts as NOT confirmed —
    # never as confirmed. Absence of evidence is not confirmation.
    CONFIRMATION_NAMES = ("trend", "vwap", "volume", "structure", "momentum")

    @staticmethod
    def _confirmations(diag, range_pos: float, momentum: float) -> tuple[list[str], list[str]]:
        """(met, missing) independent confirmations for a long entry."""
        checks = {
            # EMA9 above EMA21 — short-term direction
            "trend": diag.ema_trend == "bull",
            # Above session VWAP — institutions defend it
            "vwap": diag.vwap_dist_pct > 0,
            # Heavier tape than the session average confirms a real move
            "volume": diag.vol_surge >= 1.0,
            # Sitting in the top third of the day's range — breakout structure
            "structure": range_pos >= 0.66,
            # Short-term tick push still positive
            "momentum": momentum > 0,
        }
        met = [name for name in MomentumStrategy.CONFIRMATION_NAMES if checks[name]]
        missing = [name for name in MomentumStrategy.CONFIRMATION_NAMES if not checks[name]]
        return met, missing

    def _signal(self, quote: Quote, portfolio: Portfolio,
                min_confirmations: int = 0, horizon: str = "intraday") -> Signal | None:
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

        # ── Horizon-appropriate structure, momentum and trend ────────────
        # Intraday: today's session range and the rolling tick window are the
        # right measures. Swing: they are not — "top third of TODAY's range"
        # and "pushed up over the last 30 minutes" say nothing about a
        # multi-day position, so the same concepts are measured in bars.
        indicators = self._fresh_indicators(quote.symbol)
        swing = horizon == "swing"
        range_high = indicators.get("range_high", 0.0)
        range_low = indicators.get("range_low", 0.0)

        if swing and range_high > range_low > 0:
            range_pos = clamp((quote.price - range_low) / (range_high - range_low), 0.0, 1.0)
            structure_label = f"{RANGE_LOOKBACK_BARS}-bar range"
        else:
            range_pos = 0.5
            if quote.high > quote.low:
                range_pos = (quote.price - quote.low) / (quote.high - quote.low)
            structure_label = "day range"

        if swing and indicators:
            momentum = indicators.get("bar_momentum", 0.0)
            # Multi-bar change, not a single session's move.
            trend_pct = indicators.get("change_lookback_pct", day_chg)
            trend_label = f"{RANGE_LOOKBACK_BARS}-bar"
        else:
            trend_pct = day_chg
            trend_label = "day"

        reasons: list[str] = []
        score = 0.0

        # Trend momentum, normalised by what counts as a big move on this
        # horizon: +5% is a strong day but an ordinary month.
        scale = TREND_SCALE_PCT.get(horizon, TREND_SCALE_PCT["intraday"])
        score += clamp(trend_pct / scale, -0.5, 1.0) * 0.30
        if trend_pct != 0:
            reasons.append(f"{trend_label} {trend_pct:+.2f}%")

        # Buying strength near the day high = breakout behaviour
        score += range_pos * 0.15
        if range_pos > 0.8:
            reasons.append(f"near {structure_label} high")

        # Tick momentum confirmation
        score += clamp(momentum * 400, -1.0, 1.0) * 0.10

        # VWAP: institutions defend it — above is long territory
        if diag.vwap_dist_pct != 0.0:
            anchor = "long-run VWAP" if swing else "VWAP"
            if diag.vwap_dist_pct > 0:
                score += 0.15
                reasons.append(f"above {anchor}")
            else:
                score -= 0.15
                reasons.append(f"below {anchor}")

        # EMA9/21 trend
        if diag.ema_trend == "bull":
            score += 0.15
            reasons.append("EMA9>21")
        elif diag.ema_trend == "bear":
            score -= 0.15
            reasons.append("EMA9<21")

        # Volume surge: a real momentum move is confirmed by expanding volume.
        # Reward heavier-than-average tape (surge>1), penalise volume drying up
        # on the breakout (surge<0.7) — the classic low-volume fakeout.
        vsurge = diag.vol_surge
        if vsurge > 0:
            score += clamp((vsurge - 1.0) / 1.5, -0.5, 1.0) * 0.15
            if vsurge >= 1.5:
                reasons.append(f"vol {vsurge:.1f}x surge")
            elif vsurge < 0.7:
                reasons.append(f"vol {vsurge:.1f}x thin")

        overbought = diag.rsi >= 75
        exhausted = diag.rsi >= 80
        if overbought:
            reasons.append(f"RSI {diag.rsi:.0f} overbought")
        illiquid = 0 < quote.turnover < MIN_TURNOVER
        # Don't initiate a momentum long below institutional fair value. VWAP of
        # 0 means "unknown" (no candles fetched yet) and does NOT block.
        below_vwap = diag.vwap_dist_pct < 0

        score = clamp(score, 0.0, 0.99)
        reason_txt = ", ".join(reasons) if reasons else "no strong factors"

        # ── SELL: held position showing reversal ─────────────────────────
        # Require confirmation (a VWAP loss) for momentum-based exits so tick
        # noise alone doesn't shake us out of a position still trending above
        # VWAP. Hard risk (stop/trailing) is enforced separately in the engine.
        if held_quantity > 0:
            reversal = (
                exhausted
                or (below_vwap and diag.ema_trend == "bear")
                or (below_vwap and momentum < -0.003)
                or trend_pct < -1.0
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

        # ── BUY: strong composite, liquid, not overbought, above VWAP ────
        if not diag.tradable:
            # Halted or suspended. Buying into one is how you end up holding
            # something you cannot exit.
            return Signal(
                symbol=quote.symbol, price=quote.price, score=0.0, action="watch",
                reason=f"Not tradable — exchange status '{quote.trade_status}'.",
                diagnostics=diag,
            )

        # `trend_pct > 0` is the horizon-appropriate form of "it is going up":
        # today's change intraday, the multi-bar change for swing. Requiring
        # TODAY to be green before opening a multi-day position is an intraday
        # constraint that has no business gating a swing entry.
        if score >= 0.55 and trend_pct > 0 and not overbought and not illiquid and not below_vwap:
            met, missing = self._confirmations(diag, range_pos, momentum)
            if len(met) >= min_confirmations:
                confirm_txt = (f" [{len(met)}/{len(self.CONFIRMATION_NAMES)} confirm: "
                               f"{', '.join(met)}]" if min_confirmations else "")
                return Signal(
                    symbol=quote.symbol, price=quote.price, score=score, action="buy",
                    reason=f"Uptrend: {reason_txt}.{confirm_txt}",
                    diagnostics=diag,
                )
            # Scored well but the factors disagree — the expensive kind of trade.
            return Signal(
                symbol=quote.symbol, price=quote.price,
                score=clamp(score, 0.0, 0.54), action="watch",
                reason=(f"Not converged — only {len(met)}/{min_confirmations} confirmations "
                        f"({', '.join(met) or 'none'}); missing {', '.join(missing)}."),
                diagnostics=diag,
            )

        if below_vwap and score >= 0.55:
            watch_note = f"below VWAP — waiting for reclaim ({reason_txt})"
        elif illiquid:
            watch_note = "illiquid — skipped"
        else:
            watch_note = reason_txt
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