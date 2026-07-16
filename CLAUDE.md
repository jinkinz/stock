# AI Trading Tool

Local AI-assisted day-trading dashboard. Python 3.9+ stdlib backend (no
frameworks), vanilla JS frontend, Longbridge brokerage for market data and
live orders, multi-provider LLM "brain" for trade decisions.

## Run / verify

```bash
python3 -m trading_tool.app        # from repo root — serves http://127.0.0.1:8765
```

- Startup banner prints credential + connection status (Longbridge, AI provider).
- No test suite. Verify by booting and hitting `/api/status`, `/api/tick`.
- System python3 is 3.9 (code uses `from __future__ import annotations`).
- macOS box: no `timeout` command; use background `&` + `sleep` + `pkill`.
- Check for an already-running instance before starting one (port 8765);
  `pkill -f trading_tool.app` kills the user's instance too — be careful.

## Architecture (trading_tool/)

| File | Role |
|---|---|
| `app.py` | HTTP server, SSE, `TradingEngine` (tick pipeline, mechanical exits, candle enrichment, universe ranking, backtest), `AppState` persistence |
| `strategy.py` | `MomentumStrategy` — signal engine: multi-factor scoring (day change, range position, VWAP, EMA9/21, RSI(14), turnover liquidity gate) + `compute_indicators()` |
| `ai_strategy.py` | `AIStrategy` — multi-provider LLM wrapper (anthropic/openai/gemini/openrouter/ollama/custom), 30s throttle, prompt builder, response sanitizer, falls back to MomentumStrategy on any failure |
| `broker.py` | `PaperBroker` (local fills + fee/slippage model, real LB quotes when connected), `LongbridgeBroker` (quotes, candles, discovery, live orders), `.env` loader |
| `models.py` | Dataclasses: Settings, Portfolio, Position (has `peak_price`), Quote (enriched: prev_close/high/low/volume/turnover), Signal, Diagnostics, OrderProposal |
| `market_hours.py` | US/HK/SG regular-session times (zoneinfo); `is_market_open`, `market_of`, `markets_status` |
| `state/` | JSON persistence: paper_state.json, trade_log.jsonl, audit_log.jsonl (rotates at 5MB), sessions_log.jsonl |
| `static/` | index.html + app.js (SSE client, render functions) + styles.css |

## Decision layers (order matters)

1. **Signal engine** scores every symbol; BUY needs score ≥0.55, positive day
   change, RSI <75, turnover ≥$500k.
2. **Mechanical exits** run BEFORE the AI each tick: profit lock → stop loss →
   trailing stop (uses `Position.peak_price`), plus max-loss circuit breaker.
   The AI can exit earlier but never hold past these.
3. **AI brain** (≤1 call/30s) returns JSON decisions; sanitizer caps SELL at
   held qty, BUY at min(max_trade_value, 35% cash), max 5 proposals.

## Invariants — do not break

- Paper mode is default; live orders need BOTH `trading_mode=live` AND
  `allow_live_trading=true`.
- No options, margin, or shorting — hard-coded in `TradingEngine.execute()`.
- Live orders round to whole shares AND board-lot multiples
  (`LongbridgeBroker.lot_size()` from static_info, cached). Live fills are
  confirmed by polling order status after submit — never assume a fill.
- Market-hours gate (`market_hours.py`): with Longbridge connected, only
  markets in session are scanned/traded; closed markets are filtered out of
  the universe and quote refresh. Gate is OFF in sim mode (source
  "paper-sim") so the simulator stays testable 24/7. Holidays not handled.
- Strategy real-data path only applies when `quote.source != "paper-sim"`;
  sim keeps the legacy tick-momentum path so paper-sim still trades.
- Never serialize full-universe dicts (`last_prices`, `last_quotes`) into
  status payloads, trade logs, or state files — the working set can be 2000
  symbols and this previously caused 27MB logs and multi-MB SSE payloads.
- The 10s quote-refresh loop and any display-only path must use
  `scan_signals_only()` — never the AI (burns paid tokens for nothing).

## Environment

- `.env` lives at `trading_tool/.env` (loader: script dir → cwd → `~/.env`);
  git-ignored; contains real keys. Loader skips empty values silently.
- Longbridge access tokens expire (~90 days) — "suddenly disconnected" usually
  means regenerate the token.
- Longbridge rate limits: ~10 quote req/s, 30 trade calls/30s
  (`TradingRateLimiter`), quote batches capped at 200 symbols.
- Universe: full-market discovery is ranked by turnover once per 30 min into a
  working set (cap 2000). "Max Symbols = 0" means this cap, not truly unlimited.

## Docs

- `USER_GUIDE.md` — full user documentation incl. deep-dive on strategy/AI
  internals (section 9). Keep it in sync when changing strategy logic,
  settings, or safety rules.
