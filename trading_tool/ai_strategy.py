"""
AI-powered trading strategy using Claude as the decision brain.

Architecture:
─────────────
1. MomentumStrategy computes raw per-symbol metrics cheaply on every tick.
2. AIStrategy takes the top-ranked signals + full portfolio context and sends
   them to Claude (claude-sonnet-4-20250514) via the Anthropic API.
3. Claude reasons about:
     - Which signals are actually worth acting on
     - How to spread the budget across multiple stocks (no all-in)
     - Whether to hold, buy more, or exit existing positions
     - Risk: volatility, concentration, max-loss proximity
     - Confidence threshold — skip trades it's unsure about
4. Claude returns a structured JSON decision list.
5. Falls back to MomentumStrategy rule-based logic if API key missing or call fails.

Setup:
    Add to your .env file:
        ANTHROPIC_API_KEY=sk-ant-...

Constraints enforced regardless of AI decision:
    - Only BUY with available cash (no margin, no borrowing)
    - Only SELL what is held (no shorting)
    - Max trade value per position respected
    - Max loss circuit-breaker respected
"""
from __future__ import annotations

import json
import math
import os
import time
from collections import deque
from typing import Any

from .broker import affordable_quantity
from .models import (
    Diagnostics, OrderProposal, Portfolio, Quote, Settings, Side, Signal,
)
from .strategy import MomentumStrategy, PROPOSAL_TTL_SECONDS

# Re-export so app.py can import from one place
__all__ = ["AIStrategy", "PROPOSAL_TTL_SECONDS"]

# ---------------------------------------------------------------------------
# AI connection status — surfaced to UI
# ---------------------------------------------------------------------------
AI_STATUS: dict[str, Any] = {
    "enabled": False,
    "model": "claude-sonnet-4-20250514",
    "last_call_at": None,
    "last_error": None,
    "calls_this_session": 0,
}


def _get_api_key() -> str | None:
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    return key if key else None


# ---------------------------------------------------------------------------
# Claude API call — direct HTTP, no SDK dependency
# ---------------------------------------------------------------------------

def _call_claude(prompt: str, max_tokens: int = 1024) -> str:
    """Call Claude API and return the text response. Raises on failure."""
    import urllib.request
    import urllib.error

    api_key = _get_api_key()
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY not set")

    body = json.dumps({
        "model": AI_STATUS["model"],
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
        "system": (
            "You are an expert intraday trading assistant. You receive market signals "
            "and portfolio data, and return ONLY a JSON array of trading decisions. "
            "You reason carefully about risk, diversification, and position sizing. "
            "You never recommend margin, options, or shorting. "
            "You always spread risk — never put more than 30% of available cash into one position. "
            "You are conservative: when unsure, you watch rather than trade."
        ),
    }).encode("utf-8")

    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=body,
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        method="POST",
    )

    with urllib.request.urlopen(req, timeout=20) as resp:
        result = json.loads(resp.read().decode("utf-8"))
        return result["content"][0]["text"]


# ---------------------------------------------------------------------------
# Build the prompt
# ---------------------------------------------------------------------------

def _build_prompt(
    signals: list[Signal],
    portfolio: Portfolio,
    settings: Settings,
) -> str:
    # Summarise portfolio
    positions_summary = []
    for sym, pos in portfolio.positions.items():
        if pos.quantity > 0:
            last = portfolio.last_prices.get(sym, pos.avg_cost)
            pnl  = pos.quantity * (last - pos.avg_cost)
            positions_summary.append({
                "symbol": sym,
                "quantity": round(pos.quantity, 6),
                "avg_cost": pos.avg_cost,
                "last_price": last,
                "unrealized_pnl": round(pnl, 2),
                "value": round(pos.quantity * last, 2),
            })

    total_pnl = portfolio.realized_pnl + portfolio.unrealized_pnl()

    # Top signals with their diagnostics
    signal_data = []
    for s in signals[:15]:  # send top 15 signals max to keep prompt tight
        entry: dict[str, Any] = {
            "symbol": s.symbol,
            "price": s.price,
            "action": s.action,
            "score": round(s.score, 3),
            "reason": s.reason,
        }
        if s.diagnostics:
            d = s.diagnostics
            entry["volatility_pct"] = d.volatility
            entry["trend_strength_pct"] = d.trend_strength
            entry["volume_spike"] = d.volume_spike
            entry["spread_pct"] = round(d.spread_pct * 100, 3)
        signal_data.append(entry)

    context = {
        "portfolio": {
            "cash_available": round(portfolio.cash, 2),
            "realized_pnl": round(portfolio.realized_pnl, 2),
            "unrealized_pnl": round(portfolio.unrealized_pnl(), 2),
            "total_pnl": round(total_pnl, 2),
            "open_positions": positions_summary,
        },
        "settings": {
            "budget": settings.budget,
            "max_trade_value": settings.max_trade_value,
            "max_loss": settings.max_loss,
            "max_loss_remaining": round(settings.max_loss + total_pnl, 2),
        },
        "signals": signal_data,
    }

    return f"""Here is the current market state for an intraday paper trading session:

{json.dumps(context, indent=2)}

Analyse the signals and portfolio above. Return ONLY a JSON array of decisions.
Each decision must have exactly these fields:
  - "symbol": string
  - "action": "buy" | "sell" | "watch"
  - "quantity_pct": number (0-100) — percentage of available cash to spend on a buy,
                    or percentage of held position to sell (use 100 for full exit)
  - "confidence": number (0-1)
  - "reason": string (1-2 sentences explaining the decision)

Rules you must follow:
1. Never recommend more than 25% of available cash on a single buy.
2. If cash < $5, do not recommend any buys.
3. Only recommend selling a symbol if it is in open_positions.
4. If total_pnl <= -max_loss, recommend selling all positions immediately, no new buys.
5. Skip symbols with volatility > 80% — too risky.
6. Diversify — prefer spreading buys across 3-5 symbols over concentrating in one.
7. If a signal score < 0.6, set action to "watch" unless you have strong conviction.
8. Return an empty array [] if no action is warranted.

Return ONLY the JSON array, no explanation, no markdown fences."""


# ---------------------------------------------------------------------------
# Parse Claude's response
# ---------------------------------------------------------------------------

def _parse_decisions(raw: str) -> list[dict]:
    """Extract JSON array from Claude's response robustly."""
    raw = raw.strip()
    # Strip markdown fences if present
    if raw.startswith("```"):
        lines = raw.split("\n")
        raw = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])
    try:
        decisions = json.loads(raw)
        if isinstance(decisions, list):
            return decisions
    except json.JSONDecodeError:
        # Try to find JSON array within the text
        start = raw.find("[")
        end   = raw.rfind("]") + 1
        if start != -1 and end > start:
            try:
                return json.loads(raw[start:end])
            except Exception:
                pass
    return []


# ---------------------------------------------------------------------------
# Convert AI decisions → OrderProposals
# ---------------------------------------------------------------------------

def _decisions_to_proposals(
    decisions: list[dict],
    signals: list[Signal],
    portfolio: Portfolio,
    settings: Settings,
) -> list[OrderProposal]:
    price_map  = {s.symbol: s.price for s in signals}
    price_map.update(portfolio.last_prices)

    proposals: list[OrderProposal] = []

    for dec in decisions:
        try:
            symbol     = dec["symbol"]
            action     = dec["action"]
            qty_pct    = float(dec.get("quantity_pct", 0))
            confidence = float(dec.get("confidence", 0.5))
            reason     = str(dec.get("reason", "AI decision."))

            if action == "watch":
                continue

            price = price_map.get(symbol)
            if not price or price <= 0:
                continue

            if action == "buy":
                # qty_pct = % of available cash to spend
                spend    = portfolio.cash * (qty_pct / 100.0)
                spend    = min(spend, settings.max_trade_value, portfolio.cash)
                quantity = affordable_quantity(price, spend, spend)
                if quantity <= 0:
                    continue
                proposals.append(OrderProposal(
                    symbol=symbol, side=Side.BUY,
                    quantity=quantity, price=price,
                    confidence=confidence, reason=f"[AI] {reason}",
                ))

            elif action == "sell":
                pos = portfolio.positions.get(symbol)
                if not pos or pos.quantity <= 0:
                    continue
                # qty_pct = % of held position to sell
                quantity = round(pos.quantity * (qty_pct / 100.0), 6)
                quantity = min(quantity, pos.quantity)
                if quantity <= 0:
                    continue
                proposals.append(OrderProposal(
                    symbol=symbol, side=Side.SELL,
                    quantity=quantity, price=price,
                    confidence=confidence, reason=f"[AI] {reason}",
                ))
        except Exception:
            continue

    return proposals[:6]  # max 6 proposals per tick


# ---------------------------------------------------------------------------
# AIStrategy — drop-in replacement for MomentumStrategy
# ---------------------------------------------------------------------------

class AIStrategy:
    """
    Uses Claude to make trading decisions based on momentum signals.
    Falls back to MomentumStrategy if API key is not set or call fails.
    """

    def __init__(self) -> None:
        self._momentum = MomentumStrategy()
        # Throttle AI calls — max 1 per 30s to avoid burning API quota
        self._last_ai_call: float = 0.0
        self._ai_call_interval: float = 30.0

    def scan(
        self,
        settings: Settings,
        quotes: list[Quote],
        portfolio: Portfolio,
    ) -> tuple[list[Signal], list[OrderProposal]]:
        # Always compute momentum signals — fast, no API cost
        signals, fallback_proposals = self._momentum.scan(settings, quotes, portfolio)

        api_key = _get_api_key()
        AI_STATUS["enabled"] = bool(api_key)

        if not api_key:
            AI_STATUS["last_error"] = "ANTHROPIC_API_KEY not set — using rule-based fallback"
            return signals, fallback_proposals

        # Throttle: only call Claude every 30s
        now = time.monotonic()
        if now - self._last_ai_call < self._ai_call_interval:
            # Return cached signals but no new proposals until next AI call window
            return signals, []

        # Circuit-breaker: no new buys if max loss hit
        total_pnl = portfolio.realized_pnl + portfolio.unrealized_pnl()
        if total_pnl <= -settings.max_loss:
            return signals, fallback_proposals  # fallback handles emergency sells

        try:
            prompt    = _build_prompt(signals, portfolio, settings)
            raw       = _call_claude(prompt)
            decisions = _parse_decisions(raw)

            self._last_ai_call = now
            AI_STATUS["last_call_at"]        = time.strftime("%H:%M:%S")
            AI_STATUS["last_error"]          = None
            AI_STATUS["calls_this_session"] += 1

            proposals = _decisions_to_proposals(decisions, signals, portfolio, settings)
            return signals, proposals

        except Exception as exc:
            AI_STATUS["last_error"] = str(exc)
            # Fall back to rule-based proposals
            return signals, fallback_proposals
