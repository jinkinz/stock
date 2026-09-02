# AI Trading Tool

Local AI-assisted day-trading dashboard. Python 3.9+ stdlib backend (no
frameworks), vanilla JS frontend, Longbridge brokerage for market data and
live orders, multi-provider LLM "brain" for trade decisions.

## Run / verify

```bash
python3 app.py        # from repo root — serves http://127.0.0.1:8765
```

- Startup banner prints credential + connection status (Longbridge, AI provider).
- `python3 -m unittest discover tests` — covers `metrics.py` only.
- `python3 replay.py` — replays real historical candles through
  the real `TradingEngine` to verify the fill → ledger → metrics path without
  a market being open. **Run this after any change to `execute()`, the
  mechanical exits, proposal tagging, or the ledger.** Writes to a temp dir;
  never touches `state/`.
- Otherwise verify by booting and hitting `/api/status`, `/api/tick`,
  `/api/metrics`.
- System python3 is 3.9 (code uses `from __future__ import annotations`).
- macOS box: no `timeout` command; use background `&` + `sleep` + `pkill`.
- Check for an already-running instance before starting one (port 8765);
  `pkill -f 'python3 app.py'` kills the user's instance too — be careful.

## Architecture (repo root — flat, no package dir)

| File | Role |
|---|---|
| `app.py` | HTTP server, SSE, `TradingEngine` (tick pipeline, mechanical exits, candle enrichment, universe ranking, backtest, round-trip ledger), `metrics_report()` + buy-and-hold benchmark, `AppState` persistence |
| `strategy.py` | `MomentumStrategy` — signal engine: multi-factor scoring (day change, range position, VWAP, EMA9/21, RSI(14), turnover liquidity gate) + `compute_indicators()` |
| `ai_strategy.py` | `AIStrategy` — multi-provider LLM wrapper (anthropic/openai/gemini/openrouter/ollama/custom), 30s throttle, prompt builder, response sanitizer, falls back to MomentumStrategy on any failure |
| `broker.py` | `PaperBroker` (local fills + fee/slippage model, real LB quotes when connected), `LongbridgeBroker` (quotes, candles, discovery, live orders), `.env` loader |
| `models.py` | Dataclasses: Settings, Portfolio, Position (`peak_price` + round-trip entry context, `reset_round_trip()`), Quote (enriched: prev_close/high/low/volume/turnover), Signal, Diagnostics, OrderProposal (has `tag` → exit_reason) |
| `market_hours.py` | US/HK/SG regular-session times (zoneinfo); `is_market_open`, `market_of`, `markets_status` |
| `replay.py` | Dev harness: replays real candles through the real engine to prove the fill → ledger path works. `ReplayBroker` subclasses `PaperBroker` (fills/fees inherited unchanged), serves day-to-date quote context from bars. Not a backtest — judge the checks, not the P&L |
| `fees.py` | Per-market brokerage fee schedules (`FeeComponent`/`FeeSchedule`). SG is MEASURED from real contract notes and `verified=True`; US/HK are flagged estimates. Unknown markets fall back to a flat charge — paper fills must never be free |
| `calibrate_fees.py` | Read-only: compares modelled fees against real `charge_detail` from order history. Run after any real fill to correct `fees.py` with evidence |
| `premarket.py` | Pre-session watchlist, TWO screens by horizon: intraday = pre-market gappers, swing = 20-day leaders (strength AND near its own highs). Selecting on a one-session gap for a multi-day hold is a horizon mismatch. Pure. Ranks by gap × log-scaled pre-market turnover, gap-UPS only (the engine cannot short), with floors on both gap size and volume. Deliberately has NO AI catalyst filter: with no news API a model shown price+volume can only guess |
| `risk.py` | `RiskState` + `check_limits()` — portfolio protections: concentration cap, daily deployment budget, daily loss halt, loss-streak cooldown. Days are EXCHANGE-local; `daily_turnover_multiple` x budget is the daily deployment cap (derived, not entered) and counts CUMULATIVE deployment, not currently-held. Persisted in paper_state.json so limits survive restarts |
| `sizing.py` | `size_position()` — ATR risk-based sizing (pure, no I/O). Every position risks the same % of equity; volatile symbols get fewer shares. Clamped by max_trade_value → 25% of cash → available cash. Falls back to flat sizing when ATR is missing and SAYS SO in `reason`. **Timeframe-sensitive**: intraday ATR is tiny, so the same risk % implies a far larger position than on daily bars |
| `metrics.py` | Pure functions over closed round trips: win rate, expectancy, profit factor, drawdown, fees, breakdowns by exit_reason/strategy. No I/O, no app imports — unit-tested in `tests/test_metrics.py` |
| — | **Candle budget is the real trading ceiling.** `CANDLE_SPEC[horizon][3]` (intraday 40, swing 150) is how many symbols get indicators per tick; the convergence gate treats missing indicators as NOT confirmed, so a symbol without candles can never be bought. It sat at 15 while the universe ran to 2000, making "Max Symbols" imply an opportunity it could not deliver. A wide universe is still useful — it is the pool the top movers are drawn from — but it does NOT raise this ceiling. `_coverage_summary()` surfaces the relationship. |
| `state/` | JSON persistence: paper_state.json, trade_log.jsonl (per-fill), trades_closed.jsonl (per round trip), audit_log.jsonl (rotates at 5MB), sessions_log.jsonl |
| `static/` | index.html + app.js (SSE client, render functions) + styles.css |

## Decision layers (order matters)

1. **Signal engine** scores every symbol; BUY needs score ≥0.55, positive day
   change, RSI <75, turnover ≥$500k, **and** `settings.min_confirmations`
   independent factors agreeing (trend / vwap / volume / structure / momentum —
   `MomentumStrategy._confirmations()`, default 5 of 5). A factor with no data
   counts as NOT confirmed, never as confirmed. Blocked signals are downgraded
   to `watch` with score capped below 0.55 so nothing ranking on score still
   treats them as candidates.
2. **Mechanical exits** run BEFORE the AI each tick, first match wins:
   max_hold (days, swing) → **stall** (minutes, intraday) → profit lock →
   stop loss → **breakeven** → trailing stop → **thesis break**, plus the
   max-loss circuit breaker. Then `_check_rotation()` separately.
   The AI can exit earlier but never hold past these. The stop is
   `Position.stop_price` (absolute, fixed at entry from that symbol's ATR) when
   set, falling back to flat `stop_loss_pct` when ATR was unavailable. A
   position carrying an ATR stop keeps the exit loop alive even when every
   percentage setting is 0.
   The last four are **slot-releasing** exits — they answer "is this still
   worth a slot?" rather than "what is it worth?". See the measured-defaults
   note under Invariants before changing any of them.
3. **AI brain** (≤1 call/30s) returns JSON decisions; sanitizer caps SELL at
   held qty, BUY at min(max_trade_value, 35% cash), max 5 proposals.

## Measurement layer

- A **trade** is a round trip (entry fill(s) → exit fill(s)), not a fill.
  `trade_log.jsonl` records fills; `trades_closed.jsonl` records round trips and
  is the only input to metrics.
- `GET /api/trades?window=…&limit=N` → `closed_trades_report()`: the individual
  round trips, newest first. Bounded twice — `_tail_lines()` on read and
  `MAX_TRADES_RESPONSE` (200) on the response. Malformed ledger lines are
  skipped, never fatal. Shares the window vocabulary with `/api/metrics`; the
  two are views of one ledger and must never disagree (there is a test).
- `GET /api/metrics?window=session|day|week|all` → `metrics_report()`:
  expectancy (the headline number), win rate, profit factor, drawdown, fees as
  % of gross, plus strategy-vs-buy-and-hold. `sample_warning` is set under 20
  trades and the UI must show it, not hide it in a tooltip.
- The benchmark equal-weights buy-and-hold of the traded symbols over the same
  window from real candles — capped at 10 symbols, cached 5 min (each symbol is
  a candle API call). Falls back to ledger prices when Longbridge is down.
- **Trade viability** (`TradingEngine._viability_denial()`, in `execute()`):
  a BUY whose profit target cannot clear its own round-trip costs is refused —
  in PAPER as well as live, because a structurally-losing paper trade corrupts
  the baseline. Governed by `settings.enforce_trade_viability` (default on).
  Skipped when `lock_profit_pct` is 0 (no target = nothing to judge against).
  SELLs are never blocked on cost grounds — trapping a position is worse than
  any fee. `fees.assess_trade()` also returns `min_viable_notional`, so the
  error says what size WOULD work rather than just refusing.
- Fees are never a flat constant. Paper fills price them per market via
  `fees.paper_fee()`; live fills read the broker's real `charge_detail`. Each
  closed trade records `fees_source`: `actual` (billed), `modelled` (paper), or
  `unknown` (live fill with no charge data — do NOT read as free).
- `PAPER_FEE_PER_TRADE` in `.env` overrides the whole model with a flat charge.
  It exists for what-ifs; the startup banner shouts when it's active.
- `max_drawdown_pct` is measured against the **equity** curve
  (`starting_equity + cumulative P&L`), so pass `starting_equity` into
  `compute_metrics()` whenever it's known. Without it the percentage falls back
  to peak cumulative *profit*, which is unbounded and can read >100%.
- Replay gotcha: fetching history connects to Longbridge, which arms the
  market-hours gate. Anything replaying historical bars must force
  `LB_STATUS["connected"] = False` after building its broker or `tick()`
  returns early on the "markets closed" branch and silently does nothing.

## Invariants — do not break

- Paper mode is default; live orders need BOTH `trading_mode=live` AND
  `allow_live_trading=true`.
- No options, margin, or shorting — hard-coded in `TradingEngine.execute()`.
- **LIVE hard limits** (`TradingEngine._live_guard()`, enforced in `execute()`
  — the one chokepoint every order crosses; not overridable by settings):
  1. **Budget ceiling.** Deployed cost basis + in-flight buys can never exceed
     `settings.budget`. Oversized buys are trimmed to fit, then blocked. Note
     `portfolio.cash` in live mode is the REAL account balance, not the budget
     — never size against it directly (`strategy.py` does, which is why the
     engine caps what the strategy sees via `_tradable_view()`).
  2. **Ownership.** The tool only sells positions it opened. `entry_qty > 0`
     is the marker; synced exchange positions the tool never bought have 0 and
     are invisible to mechanical exits, stop-at-end, the strategy and the AI.
  3. **Currency.** Live orders are limited to `LIVE_ENFORCED_CURRENCIES`
     (USD, SGD) — currencies whose balance `cash_by_currency()` can actually
     read. Adding one means adding real balance handling, not just a string.
  4. **Cash cover — never margin** (`_cash_cover_denial()`). The account has
     financing enabled, and funding source is an ACCOUNT-level property: no
     order field can request cash-only, so nothing in `submit_order` can
     express it. Worse, the balance check cannot see it either —
     `available_cash` on a financing-enabled account ALREADY INCLUDES the loan,
     which is why `cash_by_currency()` says in its own docstring that it is not
     proof of cash cover. The only reading that separates the two is
     `LongbridgeBroker.cash_max_quantity()` →
     `estimate_max_purchase_quantity(...).cash_max_qty`. Never `margin_max_qty`:
     the response carries both and the difference between them IS the debt.
     Buys are trimmed to the cash limit and refused when it is 0.
     **FAILS CLOSED** — an order whose cash cover cannot be read is not sent,
     because a missed buy costs nothing and a margin-funded one is leverage the
     user never chose. Costs one trade-context call per live buy and is
     deliberately NOT cached (a stale cash bound is the exact failure it
     prevents). The concrete case: S$2,113 cash, one lot of DBS is S$4,500 —
     `cash_max_qty` 0, `margin_max_qty` 100, and without this the fill lands in
     the ledger as an ordinary cash buy. In-flight buys are subtracted from the
     bound PER CURRENCY (`_live_pending_by_currency`): the broker's estimate is
     a snapshot that cannot see this tick's own orders, so without it two buys
     each clear the same cash and together exceed it. The budget ceiling may
     pool currencies at face value; the cash bound must not, or SGD spending
     would block USD orders. This is the belt; the braces are
     getting financing disabled on the account itself (+65 6321 8888), which is
     still outstanding. Note the guard in `execute()` that reads "no margin" is
     a check on order SIDE only and never was anything more.
  Budget notionals are counted at face value per currency (conservative while
  SGD < USD). PAPER mode is exempt by design: its starting cash already is the
  budget, and adding a second ceiling would invalidate baselines measured
  before this change. Covered by `tests/test_live_guards.py`.
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
- `news_gate` in `Diagnostics` is a STUB and always True — Longbridge has no
  news/earnings/calendar API. `tradable` is the real veto, from
  `SecurityQuote.trade_status`; halted symbols are never bought. Do not present
  news_gate as working.
- `python3 replay.py --split 0.6` splits the window into
  in-sample/out-of-sample. Every measurement without it is in-sample and can be
  regime-concentrated — the shipped gate-5 default is, notably.
- **Defaults are constraint-derived, NOT backtest-tuned.** Each shipped value
  follows from arithmetic that holds regardless of window: the target clears
  round-trip cost at the default trade size; `risk_per_trade_pct` is set so a
  position lands near 15% of equity at that horizon's typical ATR (intraday
  0.10%, swing 0.75% — the scale differs ~8x); `max_scan_symbols` is a multiple
  of the candle budget so selection is real. Tuning these to maximise a replay
  is curve-fitting — the train/test split already showed the corrected swing
  config is overfit. Change them only with forward evidence.
  The dataclass defaults MUST equal `HORIZON_DEFAULTS["intraday"]`, or
  switching horizon and back silently changes settings. HTML `value=` attrs
  must match them too — they are a second copy that drifts silently.
- `daily_turnover_multiple` is a CHURN cap expressed as a multiple of capital,
  not an absolute figure — it scales with the account and spares the user
  dividing capital by trading days. Absolute dollars are derived in
  `Settings.daily_deployment_cap()`. It bites hardest INTRADAY (recycling is
  where fees compound) and is close to inert in swing; the old name "Daily
  Budget" read as a capital cap and confused exactly that.
- `max_scan_symbols` has NO sentinel: 0 or negative falls back to the horizon
  default, values are clamped to 25..2000. "0 = unlimited" made the
  least-recommended value the most tempting one to type.
- `max_concurrent_positions` has NO sentinel either, for a worse reason: 0 used
  to mean two contradictory things at once. `risk.check_limits` read `cap > 0`
  as "no concentration cap at all", while `sizing.cash_fraction_for` read
  `<= 0` as "assume four positions" and funded each at 25% of cash. Typing 0 to
  mean "let the engine decide" therefore removed the protection AND shrank
  every position to the size that fails the viability floor on a small account.
  0/negative now falls back to the default (5), clamped 1..20 — **the cap can
  never be switched off**, because a concentration limit with an off switch is
  protection that does not exist. This setting is also the SIZING DIVISOR
  (`cash_fraction_for` = `min(0.50, 1/N)`), so it is not just a ceiling:
  holding fewer at once makes each position bigger, which is often the only
  lever that clears the viability floor at a small budget.
- **Position size is a share of EQUITY, never of remaining cash**
  (`sizing._clamp_to_budget`). Charging the fraction against `spendable`
  re-applied the same percentage to a pool each purchase shrank, so slots
  decayed geometrically — $1,000 over 5 slots produced $250/$187/$141/$105/$79.
  Only the FIRST cleared its own round-trip cost; every later one was
  unprofitable purely because it was opened later, and $237 was never deployed
  at all. `spendable` stays a hard ceiling (you cannot spend cash you do not
  have) but must never be the BASE. Fixing this moved replay expectancy from
  −0.45 to −0.06 — a bigger gain than any exit rule produced. `cash_fraction_for`
  also lost its old 0.25 FLOOR, which made >4 slots impossible to equal-weight
  (five positions each claiming 25% want 125% of the account). Pinned by
  `EqualWeightSlotsTest`, which asserts `cash_fraction_for(n) * n == 1.0`.
- **Horizon audit checklist.** Swing was retrofitted onto an intraday-shaped
  app, so intraday constants keep surviving where the switch does not reach.
  Before adding ANY time-based constant, check it against both horizons
  (intraday ticks 60s / candles 55s; swing ticks 900s / candles 900s). Known
  couplings, all now fixed and regression-tested in
  `tests/test_horizon_consistency.py`:
    * `CANDLE_REFRESH_OVERRIDE` is an override, NOT a cap — `min()` there
      silently pinned swing's 900s refresh to 55s.
    * `MomentumStrategy.indicator_ttl` must outlive the refresh interval, or
      candle factors read as "unknown", the convergence gate treats that as
      not-confirmed, ATR sizing falls back to flat, and swing's range/momentum
      silently revert to their intraday forms.
    * `proposal_ttl_seconds()` scales with the tick interval — a fixed 300s
      expired swing proposals before the next scan.
    * `quote_refresh_loop` sleeps 60s in swing, 10s intraday.
    * `target_profit_per_hour` is zeroed in swing (no clock to pace against)
      and the AI prompt omits the "/hour pace" line there. Both `target_profit`
      and the rate are horizon profile fields, so the intraday pacing survives
      a round trip.
- The watchlist screen MUST match the horizon (`_build_watchlist` dispatches):
  gappers for intraday, 20-day leaders for swing. `config_fingerprint` records
  which (`gappers`/`leaders`/`turnover`) so the two never pool in the results
  table.
- Watchlist runs in `tick()`'s **markets-closed** branch (that is
  the only time it is useful) when any selected market opens within
  `PREMARKET_WINDOW_MINUTES`. It outranks turnover discovery in
  `_resolve_universe` but never a user-set custom universe, expires after
  `WATCHLIST_TTL_HOURS`, and falls back silently when a market has no pre-open
  session or nothing has traded — an empty watchlist would mean scanning
  nothing at all.
- Horizon is the FIRST setting in the UI and gates the rest: session length
  (hours) vs max hold (days), scan cadence, and which AI styles are offered
  (`Settings.horizon_strategies()` / `STRATEGIES_BY_HORIZON`). `fifo` and
  `scalp` are intraday-only — fifo's prompt is built on a session countdown,
  scalp targets +0.3% which is below round-trip cost at every measured size.
- `max_hold_days` is swing's replacement for `duration_minutes`: closes a
  position held that long regardless of P&L (`exit_reason: max_hold`), because
  a session timer can never fire when sessions never expire.
- **Slot-releasing exits are MEASURED, and three of four ship OFF.** The
  position cap means a stalled trade blocks better setups (52 blocked buys in
  one observed session, all `max 2 positions`). Freeing that slot is NOT free:
  the slot gets refilled and the replacement pays a full round trip, so it only
  pays if the replacement beats what it displaced by more than that cost — a
  67% win rate at $500/position, 93% at $250. Measured over 60 replayed round
  trips against the price-only baseline (`replay.py --legacy-exits`):

  | config | expectancy | win rate | net |
  |---|---|---|---|
  | baseline (price exits only) | **−0.06** | **54.8%** | **−3.96** |
  | + stall 120 min | −0.47 | 32.8% | −30.08 |
  | + breakeven 0.4% | −0.78 | 37.5% | −49.85 |
  | + stall 240 min (shipped) | −0.83 | 39.1% | −52.93 |
  | + thesis break | −4.52 | 17.6% | −334.40 |

  (Re-measured after the equal-weight sizing fix below, which moved the
  baseline from −0.45 to −0.06 on its own — a bigger improvement than any exit
  rule produced. Numbers recorded before that fix are not comparable.)

  So: `breakeven_trigger_pct`, `exit_on_thesis_break` and `allow_rotation` ship
  **off**; only `max_hold_minutes` ships on, at **240** — derived from the
  session (~60% of 390), NOT from the churn budget. The first attempt derived
  120 from `daily_turnover_multiple ÷ cash fraction`, which answers how fast
  capital MAY recycle rather than how long the edge takes to appear: winners
  ran a median of 1080 minutes and 74% lasted past 120, so it cut three
  quarters of them. At 240 it is NEUTRAL in a faithful one-session replay
  (−186.64 vs −186.63 net, 28 trades vs 27) while still bounding the case it
  exists for; it only costs money on windows where positions run for days,
  which is a regime it should never see (swing has `max_hold_days`).
  Note the default Min_15×400-bar replay spans ~15 trading days with
  `duration_minutes=100_000`, so `stop_at_end` never fires and positions run
  for days — it is NOT a faithful intraday test. Use `--period Min_1 --bars 390`
  for that, and mind that it yields ~16 trades, below the sample warning.
  Re-enable any of these only with forward evidence from a train/test split.
- Thesis exits read `Position.entry_confirmations` (captured once on the
  opening fill) against the live scan, restricted to `models.THESIS_FACTORS`
  = trend + vwap. The other three confirmations flip on chop. A position with
  no recorded thesis, or a symbol missing from this tick's signals, is NEVER
  exited — absence of evidence must not become evidence.
- The AI prompt must NOT show a countdown in swing mode — `ai_strategy.py`
  branches on `settings.is_swing`. Feeding a fake deadline to a model that is
  told to act on urgency is how you get invented urgency.
- **Horizon profiles** (`models.HORIZON_FIELDS` / `HORIZON_DEFAULTS`): eight
  settings are horizon-specific (targets, stops, duration, tick interval, ATR
  risk %). `Settings.horizon_profiles` keeps a saved set per horizon;
  `switch_horizon()` stashes the old and loads the new so switching never
  destroys the other's tuning. `update_settings` IGNORES horizon fields in a
  payload that also switches horizon — they belong to the outgoing one.
- **Every closed trade records `config` + `config_key`**
  (`Settings.config_fingerprint()`), so "what works" is answered from evidence.
  `GET /api/performance` ranks configurations by expectancy. Adding a setting
  that changes WHICH trades are taken or WHERE they exit? Add it to the
  fingerprint, or outcomes become uncomparable across a settings change.
- **Trading horizon** (`settings.trading_horizon`, `intraday` | `swing`):
  drives `TradingEngine.CANDLE_SPEC` (Min_1/120/55s vs Day/250/900s) and
  `_session_expired()`. In swing mode sessions never auto-expire, so
  `stop_at_end` can't flatten a multi-day position at an arbitrary moment; end
  swing sessions manually. Indicators measure minutes on Min_1 bars and days on
  Day bars — that IS the horizon change, not a cosmetic flag.
- Time-based risk logic goes through `TradingEngine._now()`, NOT
  `datetime.now()` directly. The replay overrides `simulated_now` with the
  current bar's timestamp; without that a 30-minute cooldown swallows a whole
  replay that finishes in seconds, and daily budget buckets never roll.
- Portfolio limits (`risk.py`) are checked in `execute()` on BUYs only — never
  block an exit. Every block writes an audit entry with `guard=portfolio_limits`;
  a silent block is worse than no block. `max_positions_per_sector` is NOT
  implemented: Longbridge `static_info` has no sector field, and a control that
  can never fire reads as protection that does not exist.
- Round-trip ledger: `Position` carries entry context (`entry_price`,
  `entry_qty`, `opened_at`, `fees_paid`, `exit_*`) that the broker must NOT
  clear when a position goes flat — `TradingEngine._record_round_trip()` reads
  it one call later, writes `trades_closed.jsonl`, then calls
  `reset_round_trip()`. `exit_reason` comes from `OrderProposal.tag`; never
  parse the free-text `reason`. New sell sites must set a tag.

## Environment

- `.env` lives at `.env` (loader: script dir → cwd → `~/.env`);
  git-ignored; contains real keys. Loader skips empty values silently.
- Longbridge access tokens expire (~90 days) — "suddenly disconnected" usually
  means regenerate the token.
- Longbridge rate limits: ~10 quote req/s, 30 trade calls/30s
  (`TradingRateLimiter`), quote batches capped at 200 symbols.
- Universe: full-market discovery is ranked by turnover once per 30 min into a
  working set (cap 2000). "Max Symbols = 0" means this cap, not truly unlimited.

## Docs

- `USER_GUIDE.md` — full user documentation incl. the Performance card
  (section 5.1) and a deep-dive on strategy/AI internals (section 9). Keep it
  in sync when changing strategy logic, settings, metrics, or safety rules.
- `README.md` — short orientation: run, credentials, safety model, roadmap.
  Update the roadmap when a listed item ships.
- `NEXT_SPEC.md` — the roadmap, and the reasoning behind its order. Phase 1
  (trade ledger + metrics) is **done**. Phase 2 is now "make the arithmetic
  work" — a minimum-viable-trade guard, a convergence gate, and swing mode —
  because measurement showed the strategy is structurally unprofitable at $250
  positions (round-trip fees 1.60% US / 0.95% SG against a 0.8% target, so a
  winning trade loses money). ATR sizing moved to Phase 3, portfolio
  protections to Phase 4. Read the top section before proposing features: it
  records what was measured, and what was tested and rejected.
