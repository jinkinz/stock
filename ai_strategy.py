"""
AI Strategy — goal-driven multi-provider brain.

The AI receives a clear mission every tick:
  "You have $X, Y minutes left, need $Z more profit. Use strategy: FIFO/Scalp/Swing/etc."

It sees all open positions + ranked market signals, then decides exactly what to
buy, sell, or hold — with fractional quantities — to reach the profit target in time.

Strategies:
  fifo         Lock profit fast. Buy momentum leaders, sell the moment a position
               covers its share of the target. Reinvest. Never let a winner turn loser.
  scalp        Many small wins. Sell at +0.3-0.5% gain per trade, cut at -0.2%.
               High turnover, consistent small profits compound.
  swing        Hold winners longer. Only sell on clear reversal. Fewer, bigger wins.
  conservative Capital first. Only high-confidence signals, tiny positions, tight stops.
  aggressive   Max position sizes, trade every signal, swing for large gains fast.
               High risk, use only if you have time to watch closely.
"""
from __future__ import annotations

import json
import os
import time
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone

from sizing import size_position
from models import OrderProposal, Portfolio, Settings, Side
from strategy import MomentumStrategy

# ─────────────────────────────────────────────────────────────────────────────
# Provider registry
# ─────────────────────────────────────────────────────────────────────────────
PROVIDERS: dict[str, dict] = {
    "anthropic": {
        "label": "Anthropic Claude",
        "env_key": "ANTHROPIC_API_KEY",
        # claude-sonnet-4-20250514 / claude-opus-4-20250514 were RETIRED
        # 2026-06-15 and now 404. Current model IDs below.
        "default_model": "claude-opus-4-8",
        "models": ["claude-opus-4-8", "claude-sonnet-5", "claude-haiku-4-5"],
    },
    "openai": {
        "label": "OpenAI",
        "env_key": "OPENAI_API_KEY",
        "default_model": "gpt-4o",
        "models": ["gpt-4o", "gpt-4o-mini", "gpt-4-turbo"],
    },
    "gemini": {
        "label": "Google Gemini",
        "env_key": "GEMINI_API_KEY",
        "default_model": "gemini-2.5-flash",
        "models": ["gemini-2.5-flash", "gemini-2.5-pro", "gemini-2.0-flash", "gemini-1.5-pro", "gemini-1.5-flash"],
    },
    "openrouter": {
        "label": "OpenRouter",
        "env_key": "OPENROUTER_API_KEY",
        "default_model": "meta-llama/llama-3.3-70b-instruct",
        "models": [
            "meta-llama/llama-3.3-70b-instruct",
            "mistralai/mistral-large",
            "deepseek/deepseek-chat",
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

# ─────────────────────────────────────────────────────────────────────────────
# Strategy definitions — the "rules" you inject into the AI
# ─────────────────────────────────────────────────────────────────────────────
STRATEGIES: dict[str, dict] = {
    "fifo": {
        "label": "FIFO — Lock Profit Fast",
        "description": """Your mission: reach the profit target before time runs out.

RULES:
1. Buy the top 3-5 momentum signals, spread cash across them (never >25% in one position).
2. For each open position: as soon as its unrealized P&L covers (position_value / total_invested * remaining_target), SELL IT immediately to lock in that profit.
3. After selling, immediately reinvest the freed cash into the next best signal.
4. If profit target is already reached: stop buying, protect gains, only sell if a position starts losing.
5. With <20% time left and still below target: increase urgency — sell even small gains, prioritise capital recovery.
6. NEVER let a winning position reverse into a loss. Take profit, don't be greedy.""",
    },
    "scalp": {
        "label": "Scalp — Many Small Wins",
        "description": """Your mission: generate many small profits that compound to reach the target.

RULES:
1. Buy any symbol with positive momentum score >0.55.
2. SELL immediately when a position's unrealized gain reaches +0.3% or more. Don't wait for more.
3. SELL immediately if a position drops -0.2% from entry (stop loss, no exceptions).
4. Maximum 5 positions open at once. Prefer smaller position sizes so you can trade more symbols.
5. High turnover is good — 10 small wins of 0.3% beats waiting for one big win.
6. Don't hold anything overnight or near session end — be flat by the last 10 minutes.""",
    },
    "swing": {
        "label": "Swing — Ride the Trend",
        "description": """Your mission: catch 1-3% moves by holding through minor pullbacks.

RULES:
1. Only buy signals with score >0.70. Be selective — wait for strong setups.
2. Hold positions through small dips (<0.5% from high). Don't panic sell.
3. Only SELL when: momentum score drops below 0.40, OR position drops >1% from its peak, OR session is <15 min from end.
4. Larger position sizes are OK (up to 30% per position) since you expect bigger moves.
5. Aim for 1-2% profit per trade. A few good swings will hit the target.
6. If a position is up >1.5%, consider selling half to lock profit while letting the rest ride.""",
    },
    "conservative": {
        "label": "Conservative — Protect Capital",
        "description": """Your mission: reach the profit target while preserving capital above all else.

RULES:
1. Only buy signals with score >0.75 AND volatility <25%. No exceptions.
2. Maximum 12% of available cash per position (spread risk wide).
3. SELL immediately if any position loses >0.4% from entry (tight stop loss).
4. Take profit at +0.4% gain — small consistent wins.
5. If total P&L goes negative at any point, stop all new buys immediately.
6. Better to end the session flat than lose money. Capital preservation first.""",
    },
    "aggressive": {
        "label": "Aggressive — Swing for the Fences",
        "description": """Your mission: reach the profit target fast using maximum conviction and position sizes.

RULES:
1. Buy the top 2-3 signals regardless of score (even if <0.6) — more exposure = more gain potential.
2. Use up to 40% of cash per position. Concentrate, don't diversify.
3. Hold until: profit target share is met, OR position drops >1.5% (stop loss).
4. With >50% time remaining: be patient, let winners run up to 3%+ gains.
5. With <30% time remaining: sell anything green immediately, cut all losers.
6. High risk — only use if you're watching closely and comfortable with drawdowns.""",
    },
}

# ─────────────────────────────────────────────────────────────────────────────
# Status
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class AIStatus:
    provider: str = "none"
    model: str = ""
    strategy: str = "fifo"
    connected: bool = False
    error: str = ""
    last_call_at: float = 0.0
    call_count: int = 0
    fallback_count: int = 0
    skipped_count: int = 0        # ticks where the AI call was skipped as unnecessary

    def as_dict(self) -> dict:
        return {
            "provider": self.provider,
            "model": self.model,
            "strategy": self.strategy,
            "connected": self.connected,
            "error": self.error,
            "last_call_at": self.last_call_at,
            "call_count": self.call_count,
            "fallback_count": self.fallback_count,
            "skipped_count": self.skipped_count,
        }

AI_STATUS = AIStatus()

# ─────────────────────────────────────────────────────────────────────────────
# .env loader — searches script dir first, then cwd, then home
# ─────────────────────────────────────────────────────────────────────────────
def _load_env() -> None:
    _here = os.path.dirname(os.path.abspath(__file__))
    for env_path in [os.path.join(_here, ".env"), os.path.join(os.getcwd(), ".env"), os.path.expanduser("~/.env")]:
        if os.path.exists(env_path):
            with open(env_path) as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        key, _, value = line.partition("=")
                        key = key.strip()
                        value = value.strip().strip('"').strip("'")
                        if key and value:
                            os.environ.setdefault(key, value)
            break

_load_env()

# ─────────────────────────────────────────────────────────────────────────────
# HTTP call implementations
# ─────────────────────────────────────────────────────────────────────────────
def _call_anthropic(api_key: str, model: str, system: str, user: str) -> str:
    payload = json.dumps({"model": model, "max_tokens": 1500, "system": system,
                          "messages": [{"role": "user", "content": user}]}).encode()
    req = urllib.request.Request("https://api.anthropic.com/v1/messages", data=payload,
        headers={"Content-Type": "application/json", "x-api-key": api_key, "anthropic-version": "2023-06-01"}, method="POST")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())["content"][0]["text"]

def _call_openai_compat(base_url: str, api_key: str, model: str, system: str, user: str, extra_headers: dict | None = None) -> str:
    payload = json.dumps({"model": model, "max_tokens": 1500,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}).encode()
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    if extra_headers:
        headers.update(extra_headers)
    req = urllib.request.Request(f"{base_url.rstrip('/')}/v1/chat/completions", data=payload, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())["choices"][0]["message"]["content"]

def _call_gemini(api_key: str, model: str, system: str, user: str) -> str:
    gen_cfg = {"maxOutputTokens": 2048}
    # Gemini 2.5 models "think" by default, and that reasoning is billed against
    # maxOutputTokens — it can eat the whole budget before the JSON answer is
    # emitted, truncating it mid-string ("Unterminated string" on parse). Flash
    # lets us switch thinking off so the full budget goes to the answer.
    if "flash" in model or "2.0" in model or "1.5" in model:
        gen_cfg["thinkingConfig"] = {"thinkingBudget": 0}
    payload = json.dumps({"contents": [{"parts": [{"text": f"{system}\n\n{user}"}]}],
                          "generationConfig": gen_cfg}).encode()
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
    req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read())
    cand = (data.get("candidates") or [{}])[0]
    parts = (cand.get("content") or {}).get("parts") or []
    text = "".join(p.get("text", "") for p in parts).strip()
    if not text:
        raise ValueError(f"Gemini returned no usable text (finishReason={cand.get('finishReason', '?')})")
    return text

def _salvage_json_objects(text: str) -> list:
    """Recover the complete objects from a truncated JSON array. Walks the text
    tracking brace depth (ignoring braces inside strings) and keeps everything
    up to the last top-level object that closed cleanly."""
    depth = 0
    in_str = False
    escape = False
    last_complete = -1
    for i, ch in enumerate(text):
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                last_complete = i
    if last_complete == -1:
        return []
    try:
        return json.loads(text[: last_complete + 1] + "]")
    except json.JSONDecodeError:
        return []


def _call_ai(provider: str, model: str, system: str, user: str) -> str:
    if provider == "anthropic":
        key = os.environ.get("ANTHROPIC_API_KEY", "")
        if not key: raise RuntimeError("ANTHROPIC_API_KEY not set")
        return _call_anthropic(key, model, system, user)
    if provider == "openai":
        key = os.environ.get("OPENAI_API_KEY", "")
        if not key: raise RuntimeError("OPENAI_API_KEY not set")
        return _call_openai_compat("https://api.openai.com", key, model, system, user)
    if provider == "openrouter":
        key = os.environ.get("OPENROUTER_API_KEY", "")
        if not key: raise RuntimeError("OPENROUTER_API_KEY not set")
        return _call_openai_compat("https://openrouter.ai/api", key, model, system, user,
                                   extra_headers={"HTTP-Referer": "http://localhost:8765", "X-Title": "Trading Tool"})
    if provider == "ollama":
        base = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")
        return _call_openai_compat(base, "", model, system, user)
    if provider == "gemini":
        key = os.environ.get("GEMINI_API_KEY", "")
        if not key: raise RuntimeError("GEMINI_API_KEY not set")
        return _call_gemini(key, model, system, user)
    if provider == "custom":
        base = os.environ.get("CUSTOM_AI_BASE_URL", "")
        if not base: raise RuntimeError("CUSTOM_AI_BASE_URL not set")
        key = os.environ.get("CUSTOM_AI_API_KEY", "")
        model = model or os.environ.get("CUSTOM_AI_MODEL", "")
        return _call_openai_compat(base, key, model, system, user)
    raise RuntimeError(f"Unknown provider: {provider}")

# ─────────────────────────────────────────────────────────────────────────────
# Config resolution
# ─────────────────────────────────────────────────────────────────────────────
def _resolve_config() -> tuple[str, str, str]:
    provider = os.environ.get("AI_PROVIDER", "").lower()
    if not provider:
        for name, meta in PROVIDERS.items():
            if name == "ollama": continue
            env_key = meta.get("env_key")
            if env_key and os.environ.get(env_key):
                provider = name
                break
    if not provider:
        provider = "none"
    meta = PROVIDERS.get(provider, {})
    model = os.environ.get("AI_MODEL", "") or meta.get("default_model", "")
    strategy = os.environ.get("AI_STRATEGY", "fifo")
    return provider, model, strategy

# ─────────────────────────────────────────────────────────────────────────────
# System prompt — the AI's permanent rulebook
# ─────────────────────────────────────────────────────────────────────────────
SYSTEM_PROMPT = """You are an autonomous trading AI. Your job is to make buy/sell/hold decisions to reach a profit target within a session time limit.

ABSOLUTE RULES (never break these, no exceptions):
- Never spend more than the available cash
- Never sell more shares than currently held — no shorting
- Never put more than 35% of available cash into a single position
- If total P&L reaches the max_loss limit, sell ALL positions immediately
- Return ONLY a valid JSON array — no markdown, no explanation, no text outside the array

OUTPUT FORMAT — each item in the array must have exactly:
  "symbol"     : string  (e.g. "AAPL.US")
  "action"     : string  ("buy", "sell", or "hold")
  "quantity"   : number  (exact shares — CAN be fractional e.g. 1.5, 0.25)
  "confidence" : number  (0.0 to 1.0)
  "reason"     : string  (one sentence)

For BUY: quantity = dollars_to_spend / current_price  (you decide how much to spend)
For SELL: quantity = how many shares to sell (usually all held, sometimes partial)
Return [] if no action makes sense right now."""

# ─────────────────────────────────────────────────────────────────────────────
# Prompt builder — gives AI full mission context every tick
# ─────────────────────────────────────────────────────────────────────────────
def _build_prompt(signals: list, portfolio: Portfolio, settings: Settings) -> str:
    # Time tracking
    minutes_elapsed = 0
    if settings.started_at:
        try:
            elapsed = (datetime.now(timezone.utc) - datetime.fromisoformat(settings.started_at)).total_seconds()
            minutes_elapsed = int(elapsed / 60)
        except Exception:
            pass
    minutes_remaining = max(0, settings.duration_minutes - minutes_elapsed)
    pct_time_left = minutes_remaining / max(1, settings.duration_minutes) * 100

    # P&L status
    realized = portfolio.realized_pnl
    unrealized = portfolio.unrealized_pnl()
    total_pnl = round(realized + unrealized, 4)
    target = getattr(settings, "target_profit", 0.0)
    still_needed = max(0.0, round(target - total_pnl, 4))
    target_met = total_pnl >= target > 0

    # Strategy
    strat_name = getattr(settings, "ai_strategy_name", "fifo")
    strat = STRATEGIES.get(strat_name, STRATEGIES["fifo"])

    # Urgency
    if pct_time_left < 10:
        urgency = "🔴 CRITICAL — less than 10% time left. Close positions, protect capital."
    elif pct_time_left < 25:
        urgency = "🟠 HIGH — less than 25% time left. Prioritise locking in any open gains."
    elif still_needed > 0 and pct_time_left < 50:
        urgency = "🟡 MEDIUM — halfway through time, still need profit. Be more decisive."
    else:
        urgency = "🟢 NORMAL — plenty of time remaining."

    lines = [
        "━━━ STRATEGY ━━━",
        f"Active strategy: {strat['label']}",
        "",
        strat["description"],
        "",
        "━━━ SESSION STATUS ━━━",
        f"Total budget:          ${settings.budget:.2f}",
        f"Cash available now:    ${portfolio.cash:.2f}",
        f"Max per trade:         ${settings.max_trade_value:.2f}",
        f"Max loss limit:        ${settings.max_loss:.2f}",
        f"Profit target:         ${target:.2f}" + (
            f" (${getattr(settings, 'target_profit_per_hour', 0):.2f}/hour pace)"
            if getattr(settings, "target_profit_per_hour", 0) > 0
            and not settings.is_swing else ""
        ),
        f"Realized P&L:          ${realized:.4f}",
        f"Unrealized P&L:        ${unrealized:.4f}",
        f"Total P&L so far:      ${total_pnl:.4f}",
        f"Still need:            ${still_needed:.4f}",
        f"Target met?            {'YES — protect gains' if target_met else 'NO — keep working'}",
        f"Time elapsed:          {minutes_elapsed} min",
        # A swing session never expires, so a countdown would be fiction — and
        # the styles that key off "time left" are not offered on that horizon.
        (f"Holding horizon:       SWING — no session deadline"
         + (f", auto-close after {settings.max_hold_days}d" if settings.max_hold_days else "")
         if settings.is_swing else
         f"Time remaining:        {minutes_remaining} min of {settings.duration_minutes} total"),
        f"Urgency:               {urgency}",
    ]

    lock_pct = getattr(settings, "lock_profit_pct", 0.0)
    if lock_pct > 0:
        lines.append(
            f"AUTOMATIC RULE ACTIVE: any position reaching +{lock_pct:.2f}% gain will be "
            f"AUTO-SOLD by the system regardless of your decision. Factor this in — there's "
            f"no need to manually sell purely to lock profit below this threshold, the system "
            f"already guarantees it. You can still sell earlier if your strategy calls for it."
        )
    stop_pct = getattr(settings, "stop_loss_pct", 0.0)
    if stop_pct > 0:
        lines.append(
            f"AUTOMATIC RULE ACTIVE: any position losing {stop_pct:.2f}% from entry is "
            f"AUTO-SOLD by the system (hard stop loss). You may exit losers earlier."
        )
    trail_pct = getattr(settings, "trailing_stop_pct", 0.0)
    if trail_pct > 0:
        lines.append(
            f"AUTOMATIC RULE ACTIVE: a trailing stop sells any profitable position that "
            f"falls {trail_pct:.2f}% below its peak price."
        )

    lines += [
        "",
        "━━━ OPEN POSITIONS ━━━",
    ]

    active_positions = {s: p for s, p in portfolio.positions.items() if p.quantity > 0}
    if active_positions:
        for sym, pos in active_positions.items():
            last = portfolio.last_prices.get(sym, pos.avg_cost)
            pos_pnl = round(pos.quantity * (last - pos.avg_cost), 4)
            pos_pnl_pct = round((last / pos.avg_cost - 1) * 100, 3) if pos.avg_cost > 0 else 0
            pos_value = round(pos.quantity * last, 2)
            lines.append(
                f"  {sym}: {pos.quantity:.6f} shares | avg ${pos.avg_cost:.4f} | "
                f"now ${last:.4f} | value ${pos_value} | P&L ${pos_pnl} ({pos_pnl_pct:+.3f}%)"
            )
    else:
        lines.append("  (no open positions — all cash)")

    lines += ["", "━━━ MARKET SIGNALS (best opportunities, ranked) ━━━"]
    for sig in signals[:12]:
        d = sig.diagnostics
        diag = ""
        if d:
            parts = []
            if d.day_change_pct:
                parts.append(f"day {d.day_change_pct:+.2f}%")
            if d.rsi:
                parts.append(f"RSI {d.rsi:.0f}")
            if d.vwap_dist_pct:
                parts.append(f"VWAP {d.vwap_dist_pct:+.2f}%")
            if d.ema_trend:
                parts.append(f"EMA {d.ema_trend}")
            if d.turnover:
                parts.append(f"turnover ${d.turnover/1e6:.1f}M")
            parts.append(f"vol {d.volatility:.0f}%")
            if d.volume_spike:
                parts.append("⚡SPIKE")
            diag = " | " + " ".join(parts)
        lines.append(
            f"  {sig.symbol}: {sig.action.upper()} | score={sig.score:.3f} | "
            f"price=${sig.price:.4f}{diag}"
        )
        lines.append(f"    → {sig.reason}")

    lines += [
        "",
        "━━━ YOUR TASK ━━━",
        f"Apply the {strat['label']} strategy to reach ${target:.2f} profit in {minutes_remaining} minutes.",
        f"You still need ${still_needed:.4f} more profit.",
        "",
        "Think step by step:",
        "1. Which open positions should be sold now (profit lock / stop loss)?",
        "2. Which signals offer the best risk/reward for new buys?",
        "3. For each BUY: quantity = amount_to_spend / price (fractional shares allowed)",
        "",
        "Return a JSON array only. Return [] if no action is needed.",
    ]
    return "\n".join(lines)

# ─────────────────────────────────────────────────────────────────────────────
# Main AIStrategy class
# ─────────────────────────────────────────────────────────────────────────────
class AIStrategy:
    # Minimum seconds between AI calls (hard rate cap). Beyond this the call is
    # gated further by whether there is actually a decision to make.
    THROTTLE_SECONDS = float(os.environ.get("AI_MIN_INTERVAL_SECONDS", "30"))
    # When holding positions with an unchanged decision picture, still re-consult
    # the AI at least this often so it can re-evaluate as the session ages.
    HEARTBEAT_SECONDS = float(os.environ.get("AI_HEARTBEAT_SECONDS", "180"))

    def __init__(self) -> None:
        self._fallback = MomentumStrategy()
        self._last_call = 0.0
        self._last_fingerprint = None   # decision snapshot at the last AI call
        self._provider, self._model, self._strategy = _resolve_config()
        AI_STATUS.provider = self._provider
        AI_STATUS.model = self._model
        AI_STATUS.strategy = self._strategy
        AI_STATUS.connected = self._provider not in ("none", "")

    def configure(self, provider: str, model: str = "", strategy: str = "") -> None:
        """Hot-swap provider, model, and strategy at runtime from the UI."""
        if provider:
            self._provider = provider
            meta = PROVIDERS.get(provider, {})
            self._model = model or meta.get("default_model", "")
        if strategy:
            self._strategy = strategy
        AI_STATUS.provider = self._provider
        AI_STATUS.model = self._model
        AI_STATUS.strategy = self._strategy
        AI_STATUS.connected = self._provider not in ("none", "")
        AI_STATUS.error = ""

    def ingest_candles(self, symbol: str, candles: list[dict]) -> None:
        self._fallback.ingest_candles(symbol, candles)

    def scan_signals_only(self, settings: Settings, quotes: list, portfolio: Portfolio) -> list:
        """Momentum signals without any AI call. Used by UI refresh paths so
        display updates never burn paid API tokens."""
        signals, _ = self._fallback.scan(settings, quotes, portfolio)
        return signals

    @staticmethod
    def _held_symbols(portfolio: Portfolio) -> set:
        return {s for s, p in portfolio.positions.items() if p.quantity > 0}

    def _actionable(self, signals: list, portfolio: Portfolio) -> bool:
        """Is there any decision worth spending an AI call on? Two cases:
        we hold something (needs managing), or there's a buy candidate we can
        actually afford. Otherwise the AI can only return [] — skip it."""
        if self._held_symbols(portfolio):
            return True
        if portfolio.cash >= 1.0 and any(s.action == "buy" for s in signals):
            return True
        return False

    def _decision_fingerprint(self, signals: list, portfolio: Portfolio, settings: Settings):
        """A coarse snapshot of the state the AI reasons about. If it hasn't
        changed since the last call, re-asking wastes tokens. P&L is bucketed to
        0.5% so ordinary jitter doesn't force a call; a real move does."""
        held = []
        for sym in sorted(self._held_symbols(portfolio)):
            pos = portfolio.positions[sym]
            last = portfolio.last_prices.get(sym, pos.avg_cost)
            pnl_pct = (last / pos.avg_cost - 1.0) * 100 if pos.avg_cost > 0 else 0.0
            held.append((sym, round(pnl_pct * 2) / 2))   # 0.5% buckets
        buys = tuple(sorted(s.symbol for s in signals if s.action == "buy")[:5])
        total_pnl = portfolio.realized_pnl + portfolio.unrealized_pnl()
        target = getattr(settings, "target_profit", 0.0)
        return (tuple(held), buys, total_pnl >= target > 0)

    def scan(self, settings: Settings, quotes: list, portfolio: Portfolio) -> tuple[list, list[OrderProposal]]:
        # Always run momentum for signal generation (cheap, no API call)
        signals, fallback_proposals = self._fallback.scan(settings, quotes, portfolio)

        if self._provider in ("none", ""):
            return signals, fallback_proposals

        now = time.time()
        # Hard rate cap
        if now - self._last_call < self.THROTTLE_SECONDS:
            return signals, fallback_proposals
        # Nothing to decide → don't burn a call the AI would answer with []
        if not self._actionable(signals, portfolio):
            AI_STATUS.skipped_count += 1
            return signals, fallback_proposals
        # Same picture as last call and still within the heartbeat window → skip
        fingerprint = self._decision_fingerprint(signals, portfolio, settings)
        if fingerprint == self._last_fingerprint and now - self._last_call < self.HEARTBEAT_SECONDS:
            AI_STATUS.skipped_count += 1
            return signals, fallback_proposals

        try:
            # Use strategy from settings if set, otherwise use our own
            active_strategy = getattr(settings, "ai_strategy_name", None) or self._strategy
            # Temporarily sync so prompt builder sees the right strategy
            original = self._strategy
            self._strategy = active_strategy

            # Send only what's actionable: buy candidates + the signals for held
            # names. Sub-threshold WATCH rows can't be traded, so they'd only
            # inflate the prompt.
            held = self._held_symbols(portfolio)
            decision_signals = [s for s in signals if s.action == "buy" or s.symbol in held][:12]
            user_prompt = _build_prompt(decision_signals, portfolio, settings)
            self._strategy = original

            raw = _call_ai(self._provider, self._model, SYSTEM_PROMPT, user_prompt)
            proposals = self._parse_proposals(raw, quotes, portfolio, settings, signals)
            self._last_call = now
            self._last_fingerprint = fingerprint
            AI_STATUS.connected = True
            AI_STATUS.error = ""
            AI_STATUS.last_call_at = now
            AI_STATUS.call_count += 1
            return signals, proposals
        except Exception as exc:
            AI_STATUS.error = str(exc)
            AI_STATUS.fallback_count += 1
            return signals, fallback_proposals

    def _parse_proposals(self, raw: str, quotes: list, portfolio: Portfolio,
                         settings: Settings, signals: list | None = None) -> list[OrderProposal]:
        text = raw.strip()
        # Strip markdown fences
        if "```" in text:
            for part in text.split("```"):
                part = part.strip().lstrip("json").strip()
                if part.startswith("["):
                    text = part
                    break
        # Extract JSON array
        start, end = text.find("["), text.rfind("]")
        if start != -1 and end != -1:
            text = text[start:end + 1]

        try:
            decisions = json.loads(text)
        except json.JSONDecodeError:
            # A truncated response (e.g. the model ran out of output tokens
            # mid-array) leaves an unterminated string / dangling object.
            # Salvage every complete object up to the last balanced brace so
            # one cut-off decision doesn't discard the whole batch.
            decisions = _salvage_json_objects(text)
            if not decisions:
                raise ValueError("AI response was not valid JSON (using momentum fallback)")
        quote_map = {q.symbol: q for q in quotes}
        proposals: list[OrderProposal] = []

        for item in decisions:
            symbol = item.get("symbol", "")
            action = item.get("action", "hold")
            confidence = float(item.get("confidence", 0.5))
            reason = item.get("reason", "AI decision.")
            raw_qty = float(item.get("quantity", 0) or 0)

            if action == "hold" or symbol not in quote_map:
                continue

            quote = quote_map[symbol]
            position = portfolio.positions.get(symbol)
            held = position.quantity if position else 0.0

            if action == "sell" and held > 0:
                sell_qty = round(min(raw_qty if raw_qty > 0 else held, held), 6)
                if sell_qty > 0:
                    proposals.append(OrderProposal(
                        symbol=symbol, side=Side.SELL, quantity=sell_qty,
                        price=quote.price, confidence=confidence, reason=reason,
                        tag="ai_sell",
                    ))

            elif action == "buy" and held == 0:
                # Safety cap: never exceed 35% of cash or max_trade_value.
                max_spend = min(settings.max_trade_value, portfolio.cash * 0.35)
                max_qty = max_spend / quote.price if quote.price > 0 else 0
                qty = round(min(raw_qty, max_qty), 6)
                # The AI picks WHAT to buy; risk sizing decides HOW MUCH is
                # survivable. Without this clamp `risk_per_trade_pct` silently
                # applied to rule-based trades only, so the same setting meant
                # different things depending on which brain was driving.
                if settings.use_atr_sizing:
                    diag = next((s.diagnostics for s in (signals or [])
                                 if s.symbol == symbol and s.diagnostics), None)
                    sized = size_position(
                        price=quote.price, atr=getattr(diag, "atr", 0.0) or 0.0,
                        equity=portfolio.equity(), spendable=portfolio.cash,
                        max_trade_value=settings.max_trade_value,
                        risk_per_trade_pct=settings.risk_per_trade_pct,
                        atr_stop_multiple=settings.atr_stop_multiple,
                        use_atr_sizing=True,
                        max_concurrent_positions=settings.max_concurrent_positions)
                    if sized.method == "atr" and sized.quantity > 0:
                        qty = round(min(qty, sized.quantity), 6)
                if qty > 0 and qty * quote.price <= portfolio.cash:
                    proposals.append(OrderProposal(
                        symbol=symbol, side=Side.BUY, quantity=qty,
                        price=quote.price, confidence=confidence, reason=reason,
                    ))

        return proposals[:5]
