"""
AI Strategy — multi-provider brain for the trading tool.

Supported providers (set AI_PROVIDER in .env or environment):
  anthropic   — Claude via https://api.anthropic.com  (ANTHROPIC_API_KEY)
  openai      — GPT-4o via https://api.openai.com     (OPENAI_API_KEY)
  gemini      — Gemini via Google AI Studio            (GEMINI_API_KEY)
  openrouter  — Any model via https://openrouter.ai   (OPENROUTER_API_KEY)
  ollama      — Local Ollama server (no key needed)    (OLLAMA_BASE_URL, default http://localhost:11434)
  custom      — Any OpenAI-compatible endpoint         (CUSTOM_AI_BASE_URL, CUSTOM_AI_API_KEY, CUSTOM_AI_MODEL)

The provider + model can also be changed live from the UI via POST /api/ai/config.
Falls back to MomentumStrategy if the selected provider is unavailable.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any

from .models import OrderProposal, Portfolio, Settings, Side
from .strategy import MomentumStrategy

# ---------------------------------------------------------------------------
# Provider registry
# ---------------------------------------------------------------------------

PROVIDERS: dict[str, dict] = {
    "anthropic": {
        "label": "Anthropic Claude",
        "env_key": "ANTHROPIC_API_KEY",
        "default_model": "claude-sonnet-4-20250514",
        "models": [
            "claude-opus-4-20250514",
            "claude-sonnet-4-20250514",
            "claude-haiku-4-5-20251001",
        ],
    },
    "openai": {
        "label": "OpenAI",
        "env_key": "OPENAI_API_KEY",
        "default_model": "gpt-4o",
        "models": ["gpt-4o", "gpt-4o-mini", "gpt-4-turbo", "o1-mini"],
    },
    "gemini": {
        "label": "Google Gemini",
        "env_key": "GEMINI_API_KEY",
        "default_model": "gemini-1.5-flash",
        "models": ["gemini-1.5-pro", "gemini-1.5-flash", "gemini-2.0-flash"],
    },
    "openrouter": {
        "label": "OpenRouter",
        "env_key": "OPENROUTER_API_KEY",
        "default_model": "meta-llama/llama-3.3-70b-instruct",
        "models": [
            "meta-llama/llama-3.3-70b-instruct",
            "mistralai/mistral-large",
            "deepseek/deepseek-chat",
            "google/gemma-3-27b-it",
            "qwen/qwen-2.5-72b-instruct",
        ],
    },
    "ollama": {
        "label": "Ollama (local)",
        "env_key": None,
        "default_model": "llama3.2",
        "models": ["llama3.2", "llama3.1", "mistral", "gemma3", "qwen2.5"],
    },
    "custom": {
        "label": "Custom / Compatible",
        "env_key": "CUSTOM_AI_API_KEY",
        "default_model": "",
        "models": [],
    },
}


# ---------------------------------------------------------------------------
# Status object — readable from app.py
# ---------------------------------------------------------------------------

@dataclass
class AIStatus:
    provider: str = "none"
    model: str = ""
    connected: bool = False
    error: str = ""
    last_call_at: float = 0.0
    call_count: int = 0
    fallback_count: int = 0

    def as_dict(self) -> dict:
        return {
            "provider": self.provider,
            "model": self.model,
            "connected": self.connected,
            "error": self.error,
            "last_call_at": self.last_call_at,
            "call_count": self.call_count,
            "fallback_count": self.fallback_count,
        }


AI_STATUS = AIStatus()


# ---------------------------------------------------------------------------
# .env loader (loads ANTHROPIC_API_KEY, OPENAI_API_KEY, etc.)
# ---------------------------------------------------------------------------

def _load_env() -> None:
    for path in [".env", os.path.expanduser("~/.env")]:
        if os.path.exists(path):
            with open(path) as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        key, _, value = line.partition("=")
                        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))
            break


_load_env()


# ---------------------------------------------------------------------------
# Per-provider HTTP call implementations
# ---------------------------------------------------------------------------

def _call_anthropic(api_key: str, model: str, system: str, user: str) -> str:
    payload = json.dumps({
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "max_tokens": 1000,
    }).encode()
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/chat/completions",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": "2024-12-19",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read())

    if isinstance(data, dict):
        if "choices" in data and data["choices"]:
            choice = data["choices"][0]
            message = choice.get("message", {})
            if isinstance(message, dict):
                content = message.get("content")
                if isinstance(content, list):
                    text_parts = [part.get("text", "") for part in content if isinstance(part, dict)]
                    if text_parts:
                        return "".join(text_parts).strip()
                if isinstance(content, str):
                    return content.strip()
        if "completion" in data and isinstance(data["completion"], str):
            return data["completion"].strip()
        if "output" in data:
            output = data["output"]
            if isinstance(output, list) and output:
                first = output[0]
                if isinstance(first, dict) and "content" in first:
                    content = first["content"]
                    if isinstance(content, list):
                        text_parts = [part.get("text", "") for part in content if isinstance(part, dict)]
                        if text_parts:
                            return "".join(text_parts).strip()
    raise RuntimeError("Unable to decode Anthropic response")


def _call_openai_compat(base_url: str, api_key: str, model: str, system: str, user: str, extra_headers: dict | None = None) -> str:
    """Handles OpenAI, OpenRouter, Ollama, and custom OpenAI-compatible endpoints."""
    payload = json.dumps({
        "model": model,
        "max_tokens": 1000,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }).encode()
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    if extra_headers:
        headers.update(extra_headers)
    req = urllib.request.Request(
        f"{base_url.rstrip('/')}/v1/chat/completions",
        data=payload,
        headers=headers,
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read())
    return data["choices"][0]["message"]["content"]


def _call_gemini(api_key: str, model: str, system: str, user: str) -> str:
    combined = f"{system}\n\n{user}"
    payload = json.dumps({
        "contents": [{"parts": [{"text": combined}]}],
        "generationConfig": {"maxOutputTokens": 1000},
    }).encode()
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
    req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read())
    return data["candidates"][0]["content"]["parts"][0]["text"]


# ---------------------------------------------------------------------------
# Unified call dispatcher
# ---------------------------------------------------------------------------

def _call_ai(provider: str, model: str, system: str, user: str) -> str:
    if provider == "anthropic":
        key = os.environ.get("ANTHROPIC_API_KEY", "")
        if not key:
            raise RuntimeError("ANTHROPIC_API_KEY not set")
        return _call_anthropic(key, model, system, user)

    if provider == "openai":
        key = os.environ.get("OPENAI_API_KEY", "")
        if not key:
            raise RuntimeError("OPENAI_API_KEY not set")
        return _call_openai_compat("https://api.openai.com", key, model, system, user)

    if provider == "openrouter":
        key = os.environ.get("OPENROUTER_API_KEY", "")
        if not key:
            raise RuntimeError("OPENROUTER_API_KEY not set")
        return _call_openai_compat(
            "https://openrouter.ai/api",
            key,
            model,
            system,
            user,
            extra_headers={"HTTP-Referer": "http://localhost:8765", "X-Title": "Trading Tool"},
        )

    if provider == "ollama":
        base = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")
        return _call_openai_compat(base, "", model, system, user)

    if provider == "gemini":
        key = os.environ.get("GEMINI_API_KEY", "")
        if not key:
            raise RuntimeError("GEMINI_API_KEY not set")
        return _call_gemini(key, model, system, user)

    if provider == "custom":
        base = os.environ.get("CUSTOM_AI_BASE_URL", "")
        if not base:
            raise RuntimeError("CUSTOM_AI_BASE_URL not set")
        key = os.environ.get("CUSTOM_AI_API_KEY", "")
        model = model or os.environ.get("CUSTOM_AI_MODEL", "")
        return _call_openai_compat(base, key, model, system, user)

    raise RuntimeError(f"Unknown provider: {provider}")


# ---------------------------------------------------------------------------
# Config resolution
# ---------------------------------------------------------------------------

def _resolve_config() -> tuple[str, str]:
    """Returns (provider, model) from environment or defaults."""
    provider = os.environ.get("AI_PROVIDER", "").lower()

    # Auto-detect if not set: pick first provider with a key present
    if not provider:
        for name, meta in PROVIDERS.items():
            if name == "ollama":
                continue  # skip auto-detect for local
            env_key = meta.get("env_key")
            if env_key and os.environ.get(env_key):
                provider = name
                break

    if not provider:
        provider = "none"

    meta = PROVIDERS.get(provider, {})
    model = os.environ.get("AI_MODEL", "") or meta.get("default_model", "")
    return provider, model


# ---------------------------------------------------------------------------
# Prompt builder
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """You are an autonomous trading AI assistant. Your job is to evaluate market signals
and propose buy/sell decisions within strict risk parameters.

Rules you must follow:
- Never put more than 25% of available cash into one position
- Spread buys across 3-5 symbols if possible
- Skip signals with score < 0.6
- Skip symbols with volatility > 80%
- Emergency sell all held positions if total P&L hits the max loss limit
- Only sell what is held — never short
- Return ONLY a JSON array, no markdown, no explanation

Each item in the array must have:
  symbol     (string, e.g. "AAPL.US")
  action     ("buy", "sell", or "hold")
  quantity_pct (float 0.0-1.0, fraction of max_trade_value to spend)
  confidence (float 0.0-1.0)
  reason     (string, one sentence)

Return [] if no trades are warranted."""


def _build_user_prompt(signals: list, portfolio: Portfolio, settings: Settings) -> str:
    lines = [
        f"Available cash: ${portfolio.cash:.2f}",
        f"Budget: ${settings.budget:.2f}",
        f"Max trade value: ${settings.max_trade_value:.2f}",
        f"Max loss remaining: ${settings.max_loss + portfolio.realized_pnl + portfolio.unrealized_pnl():.2f}",
        f"Realized P&L: ${portfolio.realized_pnl:.2f}",
        f"Unrealized P&L: ${portfolio.unrealized_pnl():.2f}",
        "",
        "Open positions:",
    ]
    if portfolio.positions:
        for sym, pos in portfolio.positions.items():
            last = portfolio.last_prices.get(sym, pos.avg_cost)
            pnl = pos.quantity * (last - pos.avg_cost)
            lines.append(f"  {sym}: {pos.quantity} shares, avg ${pos.avg_cost:.2f}, now ${last:.2f}, P&L ${pnl:.2f}")
    else:
        lines.append("  (none)")

    lines += ["", "Top signals (sorted by score desc):"]
    for sig in signals[:15]:
        lines.append(
            f"  {sig.symbol}: action={sig.action} score={sig.score:.2f} price=${sig.price:.2f} reason={sig.reason}"
        )

    lines += ["", "Respond with a JSON array only."]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main AIStrategy class
# ---------------------------------------------------------------------------

class AIStrategy:
    THROTTLE_SECONDS = 30

    def __init__(self) -> None:
        self._fallback = MomentumStrategy()
        self._last_call = 0.0
        self._provider, self._model = _resolve_config()
        AI_STATUS.provider = self._provider
        AI_STATUS.model = self._model
        AI_STATUS.connected = self._provider not in ("none", "")

    def configure(self, provider: str, model: str = "") -> None:
        """Hot-swap provider and model at runtime (called from /api/ai/config)."""
        self._provider = provider
        meta = PROVIDERS.get(provider, {})
        self._model = model or meta.get("default_model", "")
        AI_STATUS.provider = self._provider
        AI_STATUS.model = self._model
        AI_STATUS.connected = self._provider not in ("none", "")
        AI_STATUS.error = ""

    def scan(self, settings: Settings, quotes: list, portfolio: Portfolio) -> tuple[list, list[OrderProposal]]:
        # Always run momentum for signals
        signals, fallback_proposals = self._fallback.scan(settings, quotes, portfolio)

        if self._provider in ("none", ""):
            return signals, fallback_proposals

        now = time.time()
        if now - self._last_call < self.THROTTLE_SECONDS:
            return signals, fallback_proposals

        try:
            user_prompt = _build_user_prompt(signals, portfolio, settings)
            raw = _call_ai(self._provider, self._model, SYSTEM_PROMPT, user_prompt)
            proposals = self._parse_proposals(raw, quotes, portfolio, settings)
            self._last_call = now
            AI_STATUS.connected = True
            AI_STATUS.error = ""
            AI_STATUS.last_call_at = now
            AI_STATUS.call_count += 1
            return signals, proposals
        except Exception as exc:
            AI_STATUS.error = str(exc)
            AI_STATUS.fallback_count += 1
            return signals, fallback_proposals

    def _parse_proposals(self, raw: str, quotes: list, portfolio: Portfolio, settings: Settings) -> list[OrderProposal]:
        # Strip markdown fences if present
        text = raw.strip()
        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
        text = text.strip().strip("`")

        decisions = json.loads(text)
        quote_map = {q.symbol: q for q in quotes}
        proposals: list[OrderProposal] = []

        for item in decisions:
            symbol = item.get("symbol", "")
            action = item.get("action", "hold")
            confidence = float(item.get("confidence", 0.5))
            reason = item.get("reason", "AI decision.")
            quantity_pct = float(item.get("quantity_pct", 0.5))

            if action == "hold" or symbol not in quote_map:
                continue

            quote = quote_map[symbol]
            position = portfolio.positions.get(symbol)
            held = position.quantity if position else 0

            if action == "sell" and held > 0:
                proposals.append(OrderProposal(
                    symbol=symbol, side=Side.SELL, quantity=held,
                    price=quote.price, confidence=confidence, reason=reason,
                ))

            elif action == "buy" and held == 0:
                budget = min(settings.max_trade_value * quantity_pct, portfolio.cash)
                quantity = max(0, int(budget // quote.price))
                if quantity > 0:
                    proposals.append(OrderProposal(
                        symbol=symbol, side=Side.BUY, quantity=quantity,
                        price=quote.price, confidence=confidence, reason=reason,
                    ))

        return proposals[:5]
