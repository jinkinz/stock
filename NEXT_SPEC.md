# NEXT_SPEC.md — Risk & Measurement Layer

Status: not started. Written for a Claude Code session working in this repo.
Read `CLAUDE.md` first — its invariants are binding and this spec does not
override any of them.

## Why this work exists

The system can currently place trades but cannot tell whether the strategy
works. Session P&L is the only output. There is no win rate, no expectancy,
no drawdown, no benchmark. Position size is a flat dollar cap
(`max_trade_value`) that ignores how volatile the symbol is.

Everything below is measurement and risk control. **No new alpha, no new
signals, no new AI features in this phase.** Do not add strategy ideas while
implementing this.

---

## Phase 1 — Trade ledger + metrics

### 1.1 Close out round trips

Today `trade_log.jsonl` records fills, not trades. A "trade" is a round trip:
entry fill(s) → exit fill(s) for one symbol.

Add `state/trades_closed.jsonl`. Append one record when a position's quantity
returns to 0:

```
{
  "symbol", "opened_at", "closed_at", "hold_seconds",
  "entry_price", "exit_price", "quantity",
  "gross_pnl", "fees", "net_pnl", "return_pct",
  "exit_reason",          # profit_lock | stop_loss | trailing_stop | ai_sell
                          # | strategy_sell | session_end | manual
  "strategy",             # settings.ai_strategy_name at entry
  "mode",                 # paper | live
  "entry_score",          # signal score at entry
  "entry_diagnostics"     # rsi, vwap_dist_pct, ema_trend, vol_surge, day_change_pct
}
```

Requires tracking entry context on `Position`. Add fields to the dataclass
rather than a side dict, so it survives the existing state persistence.
Partial exits: accumulate, emit the record only when quantity hits 0.

`exit_reason` is the highest-value field here. It is the thing that tells you
whether the stop loss is saving you or bleeding you.

### 1.2 Metrics module

New file: `trading_tool/metrics.py`. Pure functions, no I/O, no state — it
reads a list of closed-trade dicts and returns a dict. Keep it importable and
testable in isolation.

Compute:

- `total_trades`, `wins`, `losses`, `win_rate`
- `avg_win`, `avg_loss`, `largest_win`, `largest_loss`
- `profit_factor` = gross wins / gross losses
- `expectancy_per_trade` = (win_rate × avg_win) − (loss_rate × |avg_loss|)
- `total_fees` and `fees_as_pct_of_gross` — surface this loudly
- `max_drawdown_pct` and `max_drawdown_dollars` from the equity curve
- `avg_hold_seconds`, median hold
- breakdown of the same metrics grouped by `exit_reason` and by `strategy`

**Expectancy is the headline number.** If it is negative, nothing else in the
app matters. Treat it as the primary output of this whole phase.

Guard every division. With <20 trades, return the metrics but mark
`"sample_warning": true` — small samples are noise and the UI must say so.

### 1.3 Benchmark

For any window, compute what an equal-weight buy-and-hold of the same symbols
over the same period would have returned. Run it off the same candle data the
backtest already fetches.

Show it next to strategy return everywhere strategy return appears. If the
strategy loses to buy-and-hold, that should be impossible to miss.

### 1.4 Endpoint + UI

- `GET /api/metrics?window=session|day|week|all`
- New "Performance" card in the main column, above Positions.
- Show: expectancy, win rate, profit factor, max drawdown, fees as % of gross,
  strategy vs benchmark. Six numbers, plain language labels (house style —
  "Average Win", not "μ_win").
- Red/green on expectancy and vs-benchmark only. Everything else neutral.
- If `sample_warning`, render a visible "only N trades — not yet meaningful"
  line rather than hiding it in a tooltip.

---

## Phase 2 — Risk-based position sizing

### 2.1 ATR

Add `atr(candles, period=14)` to `strategy.py` next to `compute_indicators()`.
True range = max(high−low, |high−prev_close|, |low−prev_close|). Wilder
smoothing, consistent with the existing RSI implementation. Add `atr` and
`atr_pct` to `Diagnostics`.

Candles are already fetched for top candidates and held positions — reuse that
path, do not add new API calls.

### 2.2 Replace the flat sizer

New settings fields:

- `risk_per_trade_pct: float = 0.5`   # % of equity risked per trade
- `atr_stop_multiple: float = 2.0`    # stop distance = N × ATR
- `use_atr_sizing: bool = True`

Sizing:

```
stop_distance = atr * atr_stop_multiple
risk_dollars  = portfolio.equity() * (risk_per_trade_pct / 100)
qty           = risk_dollars / stop_distance
```

Then clamp by, in order: `max_trade_value`, 25% of cash, available cash after
reservations. Existing live-mode lot rounding applies unchanged and happens
last.

If ATR is unavailable (no candles yet), fall back to the current flat sizing
and set a reason string saying so. Never silently size on a missing input —
that failure mode has bitten this project before with the quote batching bug.

### 2.3 Per-position stop from the same ATR

`stop_loss_pct` is currently a flat 2% for every symbol. Store an absolute
stop price on `Position` at entry, computed as `entry − (atr × multiple)`.
`_check_mechanical_exits()` compares against that stored price.

Keep flat `stop_loss_pct` working as a fallback when ATR is missing. Do not
delete it.

---

## Phase 3 — Portfolio protections

Add to `_check_mechanical_exits()` / the pre-trade gate in `tick()`:

- `max_concurrent_positions: int = 5`
- `max_positions_per_sector: int = 2` — needs sector data from Longbridge
  `static_info`; cache it. If sector is unavailable for a symbol, treat it as
  its own sector rather than blocking.
- `daily_loss_limit: float` — halts new buys for the rest of the calendar day
  (exchange-local), persists across restarts in `paper_state.json`.
- `cooldown_after_losses: int = 3` — N consecutive losing trades pauses new
  buys for 30 minutes.

Each of these, when it blocks a trade, must write an audit entry with the
reason. A silent block is worse than no block.

---

## Testing

There is no test suite. Add `tests/test_metrics.py` and
`tests/test_sizing.py` — plain `unittest`, stdlib only, no new dependencies.
Metrics and sizing are pure functions; there is no excuse for these being
untested. Include a zero-trades case, a single-trade case, and an
all-losses case.

Verify end to end by booting the server and hitting `/api/metrics` and
`/api/tick` per `CLAUDE.md`. Check for a running instance on 8765 first.

---

## Out of scope for this phase

Do not touch, even if it seems easy while you are in the file:

- The AI prompt or provider layer
- New indicators or signal factors
- The news_gate stub
- Backtest train/test splitting
- Swing/daily mode

Those are the next phase. Keep this diff reviewable.

## Definition of done

The dashboard answers, without opening a log file: *is this strategy making
money, is it beating buy-and-hold, and how much of the gross is going to
fees?* And no single trade can lose more than `risk_per_trade_pct` of equity
when the stop fires as intended.
