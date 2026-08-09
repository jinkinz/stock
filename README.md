# Longbridge Trading Tool

A small local trading dashboard for testing an AI-assisted trading workflow.

The app starts in paper mode. Live Longbridge order submission is disabled unless you switch to live mode and enable the live-order checkbox in the dashboard.

Instead of trading one fixed ticker, the dashboard scans a selected market universe across US, HK, and SG symbols. You can leave Custom Universe blank to use symbol discovery where Longbridge exposes it, then sample fallback for unsupported markets, or enter your own symbols such as `AAPL.US, 700.HK, D05.SG`.

## Run

```bash
python3 app.py
```

Then open:

```text
http://127.0.0.1:8765
```

The startup banner prints exactly what is connected — Longbridge credentials, whether real quotes or the simulator are in use, and which AI provider is active.

Unit tests (metrics only; the rest is verified by running the app):

```bash
python3 -m unittest discover tests
```

To check that the trade-recording path still works without waiting for a market to open, replay real historical candles through the real engine:

```bash
python3 replay.py
```

This places no orders and writes to a throwaway temp directory. It is not a backtest — its P&L means nothing; it exists to prove that fills, exit reasons, the round-trip ledger and the metrics all still line up. Worth running after any change to order execution or the exit rules.

For full documentation — settings reference, safety ladders, how the strategy and AI brain actually decide — see [`USER_GUIDE.md`](USER_GUIDE.md).

## Live Trading Setup

Install the Longbridge SDK only when you are ready to test live connectivity:

```bash
pip install longbridge
```

Set credentials in your shell, a local secret manager, or a git-ignored `.env` file. Do not commit them.

```bash
export LONGBRIDGE_APP_KEY="your-app-key"
export LONGBRIDGE_APP_SECRET="your-app-secret"
export LONGBRIDGE_ACCESS_TOKEN="your-access-token"
```

Longbridge documents these exact environment variables for legacy API-key access. Access tokens expire roughly every 90 days — a working setup that "suddenly disconnects" usually just needs a regenerated token.

The AI brain is optional and reads its own key from the same place (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GEMINI_API_KEY`, …). Without one, the tool runs on its rule-based momentum strategy alone.

## Two-Month Paper Run

For a local paper run, use `Trading Mode = Paper`, keep `Allow live order submission` unchecked, set `Approval = Auto` if you want simulated orders to execute without manual clicks, enable `Strategy running`, and enable `Auto paper scan`. For roughly two months, set Duration to `86400` minutes.

Paper state is persisted to `state/paper_state.json`. Simulated order fills are appended to `state/trade_log.jsonl`, and each completed round trip — a position opened and then fully closed — is appended to `state/trades_closed.jsonl`. Keep the server running for continuous scans.

## Measuring Whether It Works

Session P&L cannot tell you whether a strategy is any good; one lucky trade hides a losing system. The **Performance** card, at the top of the main column above Positions, measures closed round trips over a window you pick (session, 24 hours, 7 days, all time):

- **Expectancy per trade** — the average dollars a trade is worth. This is the headline number. If it is negative, nothing else matters.
- **Win rate**, **profit factor**, and **max drawdown**.
- **Fees as % of gross** — how much of the edge the fee and slippage model ate.
- **Strategy vs buy-and-hold** — the same symbols, the same window, equal-weighted from real candles. If this is negative you would have made more money doing nothing.

Under 20 closed trades the card says so in a warning banner rather than presenting noise as a result. The same data is available at `GET /api/metrics?window=session|day|week|all`, which also breaks the metrics down by exit reason (`stop_loss`, `profit_lock`, `trailing_stop`, `ai_sell`, …) and by AI strategy style.

Only *closed* round trips count, so a window where you bought and held shows nothing.

Paper fills are charged **real per-market brokerage fees**, not a token amount. On a real SGX order of ~SGD 500 the charges come to about 0.26%, so a round trip costs ~0.5% — enough to swallow most of a 0.8% profit target. The SG schedule was derived from actual contract notes on this account; US and HK are deliberately conservative estimates, and the startup banner flags them as unverified on every boot. To replace a guess with measured truth:

```bash
python3 calibrate_fees.py
```

That compares the model against the real charges Longbridge billed on your own filled orders. It is read-only. In live mode nothing is modelled — the actual charge is read back off each filled order.

## Safety Model

- Paper mode is the default and uses a local simulator, not Longbridge live/demo order routing.
- Manual approval is the default.
- Live mode cannot submit orders unless `Allow live order submission` is checked.
- The scanner ranks a multi-symbol universe on day change, VWAP, EMA9/21, RSI and turnover. That is a ranking heuristic, not a profit guarantee.
- Budget, max trade value, max loss, and duration are enforced before proposals.
- Mechanical exits (stop loss, profit lock, trailing stop) run *before* the AI on every tick. The AI can exit earlier, but it can never hold a position past these thresholds.
- With Longbridge connected, only markets currently in session are scanned and traded; when everything is closed the engine idles rather than trading frozen prices.
- No options, no margin, no short selling — hard-coded, not a setting.
- In live mode the tool can never deploy more than the Budget you set, measured as the cost of what it holds plus anything in flight. Your account balance is not the limit; the Budget is.
- In live mode it only sells positions it opened itself. Shares you bought yourself are invisible to it — and correspondingly not protected by its stop loss.
- Live orders are restricted to US and SG symbols, the currencies whose balance the app can verify, and each order is checked against real cash in its own currency.
- Live orders are submitted as day limit orders.
- The app suppresses duplicate pending proposals for the same symbol and side.
- Paper trades and state are persisted under `state/`.
- `Allow live order submission` only gates real order placement. Live market data can be used without enabling that final order switch.

## Market Notes

Longbridge documentation shows symbol formats for US (`AAPL.US`), HK (`700.HK`), CN (`600519.SH` / `000568.SZ`), and SG (`D05.SG`). It also documents a `security_list` endpoint, but that endpoint currently says it only supports US Overnight securities. The main OpenAPI overview emphasizes HK and US for supported trading functions, so test SG in paper mode and confirm your account/API permission before enabling live orders.

## Roadmap

Shipped since the first version:

- Longbridge positions sync for live mode.
- Backtesting on real Longbridge 5-minute candles (falls back to a clearly-labeled random walk when disconnected).
- Richer diagnostics: volatility, spread, volume spikes, trend strength, VWAP, EMA9/21, RSI(14), volume surge.
- Audit log of every tick, signal, proposal, approval, rejection, and order result.
- Round-trip trade ledger with per-trade exit reasons, plus the Performance card and `/api/metrics` described above.

Next, in order (see [`NEXT_SPEC.md`](NEXT_SPEC.md) for the reasoning):

- **Make the arithmetic work.** Measuring real fees showed the strategy is structurally unprofitable at small position sizes: round-trip friction is 1.60% (US) / 0.95% (SG) on a $250 position against a 0.8% profit target, so a *winning* trade loses money. First a guard that refuses trades which cannot clear their own costs, then a convergence gate to cut trade frequency (fee drag is cost × frequency), then swing mode for multi-day holds where the numbers work.
- **Risk-based position sizing** — size each trade off ATR so one loss costs a fixed % of equity, replacing the flat dollar cap that ignores how volatile a symbol is. Per-position stops from the same ATR.
- **Portfolio protections** — max concurrent positions, per-sector caps, a daily budget and loss limit that survive restarts, and a cooldown after consecutive losses. Every block writes an audit entry; a silent block is worse than no block.

Still missing, and worth knowing about: no news or fundamentals (the news gate is a stub), no exchange-holiday calendar, and no train/test split on the backtest. `NEXT_SPEC.md` also records what was tried and rejected, and why — including why removing the AI from the live loop does *not* fix the losses.
