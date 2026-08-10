# NEXT_SPEC.md — Roadmap

Written for a Claude Code session working in this repo. Read `CLAUDE.md` first
— its invariants are binding and nothing here overrides them.

Last updated 2026-08-10, after Phase 1 shipped and the economics were measured.

---

## The finding that reorders everything

Phase 1 built the measurement layer, and the first thing it measured was that
the strategy is **structurally unprofitable at the intended position size**.
This is arithmetic, not strategy, and it decides the priority of everything
below.

Real round-trip brokerage cost, by position size:

| Position | US | SG |
|---|---|---|
| **$250** | **1.60%** | **0.95%** |
| $500 | 0.80% | 0.52% |
| $1,000 | 0.40% | 0.30% |
| $2,500 | 0.16% | 0.17% |
| $10,000 | 0.12% | 0.11% |

Brokerage fees are dominated by **flat minimums** (SGD 0.99 platform fee; US
commission and platform minimums), so cost as a percentage scales inversely
with size. ~$2,500 is the knee of the curve.

Against a 0.8% profit lock at a $250 position:

```
US: a winning trade nets -0.90%   -> required win rate: undefined
SG: a winning trade nets -0.25%   -> required win rate: undefined
```

A *winning* trade loses money. No AI, sentiment layer, indicator or execution
speed changes that.

Two things fix it, and only two: **bigger positions** or **longer holds**.
At a 3% swing target / 2% stop the required win rate becomes finite — 74% (US),
61% (SG) — demanding but achievable.

### The strategy is defensive, not alpha — this was measured

Under a swing-like configuration over ~1 year of daily bars, the strategy
returned **+7.64%** while equal-weight buy-and-hold of the same symbols returned
**+34.32%**. On the subset that declined, it returned **−2.25%** against
buy-and-hold's **−17.55%**.

It captures roughly a fifth of the upside and an eighth of the downside. Any
work premised on "this beats the market" is premised on something untrue. See
Phase 2.3 for the full matrix and the hypotheses already tested and rejected.

### Latency is NOT the problem — this was tested

A widely-suggested fix is to remove the AI from the live path because LLM
latency causes late entries. The replay harness disproves it for this codebase:
`replay.py` uses `MomentumStrategy` directly — **zero AI calls, zero network
latency** — and still lost money.

| Fee model | Net P&L | Expectancy |
|---|---|---|
| Flat $1 (the old fake model) | −$6.88 | −0.06 |
| Real measured fees | −$298.67 | −2.45 |

Same strategy, same bars, no AI either time. The delta is entirely fees. The
app also already avoids the AI on fast paths (`scan_signals_only()`), so that
part is done.

---

## Phase 1 — Trade ledger + metrics ✅ DONE

Shipped. `state/trades_closed.jsonl` (one record per round trip, with
`exit_reason` and entry context), `metrics.py` (expectancy, win rate, profit
factor, drawdown, fees, breakdowns), `GET /api/metrics?window=…` with a
buy-and-hold benchmark, and the Performance card above Positions.

Also shipped alongside it, not in the original spec:

- `replay.py` — replays real candles through the real engine to verify the
  fill → ledger → metrics path without a market open. Run after ANY change to
  execution or exits.
- `fees.py` + `calibrate_fees.py` — real per-market fee schedules. SG measured
  from actual contract notes and reproduces them to the cent; US/HK are
  flagged estimates.
- Live hard limits in `TradingEngine._live_guard()` — budget ceiling,
  tool-owned-positions-only, currency allowlist (USD/SGD).

---

## Phase 2 — Make the arithmetic work

**Nothing else on this roadmap matters until this phase lands.**

### 2.1 Minimum-viable-trade guard ✅ DONE

Shipped. `fees.assess_trade()` / `min_viable_notional()` compute breakeven from
the real schedule; `TradingEngine._viability_denial()` refuses unprofitable buys
in `execute()` (paper and live); `viability_summary()` feeds the startup banner,
`/api/status` and a dashboard banner. `settings.enforce_trade_viability`
(default on) governs blocking. Sells are never blocked. Covered by
`tests/test_viability.py`, mutation-tested.

**It confirmed the thesis.** Same strategy, same bars, same 0.8% target — only
the position size changed:

| Trade size | Buys | Expectancy | Net P&L |
|---|---|---|---|
| $250 | 0 (all blocked) | — | — |
| $25,000 | 125 | **+14.92** | **+$1,864.84** |

The strategy was never the problem; the size was. Caveats: one window, no
train/test split, and US fees are still an estimate — so treat the magnitude as
indicative, not a forecast.

<details>
<summary>Original spec (for reference)</summary>


Compute breakeven from the real fee model and compare it against the configured
profit target. Refuse — or loudly warn at startup and in the UI — when a trade
size cannot clear its own costs.

```
breakeven_pct = round_trip_fee_pct(market, notional) + slippage_pct
if profit_target_pct <= breakeven_pct:  # the trade loses money when it WINS
```

Surface it in the startup banner, on the Performance card, and as an audit
entry when a proposal is blocked. This is the guard that would have caught
"$250 × 0.8%" on day one instead of after a losing paper run.

Reuse `fees.estimate_fee()`; do not re-derive costs.

</details>

### 2.2 Convergence gate ✅ DONE

Shipped. `MomentumStrategy._confirmations()` counts five independent factors —
trend (EMA9>EMA21), vwap (above session VWAP), volume (surge ≥1.0), structure
(top third of day range), momentum (positive tick push). A factor with no data
counts as NOT confirmed. `settings.min_confirmations` (default **5**) gates the
buy; blocked signals become `watch` with score capped below 0.55. Exposed in
the UI and as `--min-confirmations` in the replay. Mutation-tested,
`tests/test_convergence.py`.

**Measured across three datasets** (cash 100k so the viability guard doesn't
interfere):

| Dataset | gate 0 | gate 4 | gate 5 |
|---|---|---|---|
| Min_15, 12 volatile tech, 400 bars | +14.92 (125 trades) | −1.33 (120) | **+64.12 (75)** |
| Min_30, same symbols, 600 bars | −66.62 (160) | — | **+21.41 (136)** |
| Min_15, 10 defensive large-caps | −6.31 (65) | — | **+21.57 (45)** |

Improves expectancy on all three and flips two from negative to positive, while
cutting trade count ~40%. Decomposing dataset A by band:

| Confirmations | Trades | Net | Avg/trade |
|---|---|---|---|
| exactly 3 | 5 | +$2,024 | +$405 |
| exactly 4 | 45 | −$4,969 | **−$110** |
| all 5 | 75 | +$4,809 | +$64 |

The "almost converged" band is reliably the worst — 45 trades, consistently
negative. That is the real effect. The `exactly 3` band is 5 trades and is
noise; note it also means the **+14.92 expectancy reported for Phase 2.1 was
carried entirely by those 5 outliers** — gate 0 was negative without them.

**⚠ Later train/test split qualifies this result.** With `--split 0.6` on the
intraday dataset, gate 5 scored **−52.54 expectancy over the first 60%** of the
window (34 trades, −$1,786) and +130.01 over the last 40% (41 trades, +$5,330).
The headline +64.12 is therefore **regime-concentrated, not a consistent edge**.
The cross-dataset comparison below still stands — gate 5 beat gate 0 on all
three — but its absolute profitability does not generalise across the window.

**Default is 5 deliberately as an endpoint, not a tuned optimum.** Picking the
best-scoring interior value would be curve-fitting. Caveats that remain: three
US datasets over an overlapping period and market regime, no train/test split,
and gate 4 scoring worse than both its neighbours in dataset A shows the
response surface is noisy. This needs forward validation, not more backtesting.

<details>
<summary>Original spec (for reference)</summary>


Fee drag is **cost × frequency**. At $250, cutting 10 trades/day to 2 saves
~$642/month in pure friction — more than the daily budget itself.

| Trades/day | Fees/day | % of $250 | Fees/month |
|---|---|---|---|
| 1 | $4.01 | 1.6% | $80 |
| 5 | $20.05 | 8.0% | $401 |
| 10 | $40.10 | 16.0% | $802 |

Require multiple independent conditions to agree before entering, rather than
one composite score clearing 0.55. Mostly a tightening of the existing gate in
`strategy.py`, not a new module.

Measure the result in the replay: trade count and `fees_as_pct_of_gross` must
both fall, and expectancy must rise. If expectancy does not improve, the gate
is filtering the wrong thing — revert rather than tune further.

</details>

### 2.3 Swing mode ✅ DONE — built despite the evidence below, and here is why

`settings.trading_horizon` (`intraday` | `swing`) drives `CANDLE_SPEC`
(Min_1/120 vs **Day**/250) and `_session_expired()` (swing sessions never
auto-expire, so `stop_at_end` cannot flatten a multi-day position). Overnight
gaps through a stop are named explicitly in the exit reason rather than looking
like slippage. `tests/test_swing.py`.

**Why build it despite the measurement:** at a small daily budget, intraday is
arithmetically impossible — the viability guard blocks every trade, because a
0.8% target cannot clear 1.7% of round-trip cost. A 3% swing target clears it
with a finite required win rate (74% US / 61% SG). Swing is not merely an
alternative at that budget; it is the only configuration that can trade at all.

Measured after Phase 3 + 4 (daily bars, 3%/2%, simulated clock):
50 trades, expectancy **+53.85**, net **+2,692** (+2.69%), max drawdown 1.3%.
Better than the pre-Phase-4 run (58 trades, +34.41, +1,996) — fewer, better
trades. Still far below buy-and-hold's +34.32% over the same window.

**The evidence against remains true and is not superseded:**

### ⚠ It still loses to buy-and-hold in a rising market

Before building it, the strategy was measured under a swing-like configuration
using **daily** bars (which makes EMA9/21 mean 9 and 21 *days* rather than
minutes — genuinely swing-scale) over ~1 year, 12 US symbols.

**It loses heavily to buy-and-hold in a rising market:**

| Regime | Buy-and-hold | Strategy (gate 5) | Verdict |
|---|---|---|---|
| All 12 names (bull year) | **+34.32%** | +7.64% | **loses by 27 pts** |
| The 5 names that declined | **−17.55%** | **−2.25%** | **beats by 15 pts** |

That is a coherent **defensive, low-beta profile**: roughly a fifth of the
upside, about an eighth of the downside. It is not an alpha generator, and it
will lose to holding in any strong bull market. The convergence gate does most
of the work on the downside (−7.46% → −2.25%, largely by declining to trade).

**Hypotheses tested and rejected** — do not re-litigate without new evidence:

- *"The 3% profit lock clips winners (AMD ran +180%)."* Widening exits made it
  **worse**: 7.64% → 3.70% (3% trail) / 4.84% (8% and 15% trails). The 8/12 and
  15/20 configs are identical, meaning those stops never bind at all — exits
  come from the reversal logic, so the exit rules are not the lever.

**Implication.** Building multi-day plumbing for a strategy that underperforms
buy-and-hold by 27 points in a bull market is a large investment for a known-weak
payoff. Swing mode would make it *cheaper to run* (fewer trades, less fee drag)
but would not make it *better*.

**Decision point — this is a goals question, not a code question:**

- *Want to beat the market?* This strategy does not. Reconsider the signal logic
  rather than adding horizon plumbing.
- *Want defensive participation* (low drawdown, capital protection)? It roughly
  does that already, and swing mode reduces the cost of running it.

**Recommended before either: run a forward paper baseline.** Everything in Phase
2 is validated only on overlapping historical windows with no train/test split.
The Performance card already shows strategy vs buy-and-hold. One week of real
sessions is the cheapest possible validation.

Caveats on all of the above: one year, US only, 12 symbols chosen for the replay
defaults, no train/test split, and the strategy holds cash much of the time while
the benchmark is fully invested — which flatters the benchmark on return and the
strategy on risk.

<details>
<summary>Original spec, if the decision is to build it anyway</summary>


The real fix if $250/day is a hard limit. Needs:

- Daily (or 60-min) candles instead of `Min_1`
- Multi-day position persistence; `stop_at_end` off by default
- Wider targets and stops (3% / 2% rather than 0.8% / 1%)
- Overnight gap handling — a gap through the stop is the main new risk
- Session/duration semantics reworked: `duration_minutes` and the
  market-hours gate assume intraday

Makes the latency question moot: a 30-second decision delay is irrelevant on a
multi-day hold, so the AI can stay in the loop where it is genuinely useful.

**Decision required before building:** swing mode and bigger positions are
different products. If $250/day is immovable, swing is the only path. If sizing
to ~$2,500 is possible, day trading becomes arguable again.

</details>

---

## Phase 3 — Risk-based position sizing ✅ DONE (with a caveat)

Shipped: `strategy.atr()` (Wilder-smoothed, counts gaps), `atr`/`atr_pct` on
`Diagnostics`, `sizing.size_position()` (pure, `tests/test_sizing.py`), and an
absolute `Position.stop_price` fixed at entry that takes precedence over the
flat `stop_loss_pct` (which stays as the fallback). Settings:
`risk_per_trade_pct` (0.5), `atr_stop_multiple` (2.0), `use_atr_sizing` (on).

**Measured on daily bars, same 58-trade signal set — sizing is the only difference:**

| Config | Net | Max DD | Return/DD |
|---|---|---|---|
| Flat sizing + flat stop (pre-Phase-3) | +7,639 | — | — |
| Flat sizing + **ATR stop** | **+8,858** | 4,336 (4.0%) | **2.04** |
| **ATR sizing** + ATR stop | +1,996 | 1,311 (1.3%) | 1.52 |

**The ATR stop earns its place** (+7,639 → +8,858). **ATR sizing cuts both
return and drawdown** and scores slightly worse risk-adjusted here.

Two things to understand before judging that:

1. **It is not exposure-matched.** At `risk_per_trade_pct = 0.5` with a 2×ATR
   stop, daily-bar ATR implies ~10% of equity per position where flat sizing
   took 25%. Most of the return gap is simply smaller positions, not worse
   selection. Raising `risk_per_trade_pct` to ~1.25 would match exposure.
2. **It is timeframe-sensitive.** Intraday ATR is tiny (~0.33% of price on
   15-min bars), so the same 0.5% risk implies a **76% of equity** position and
   the clamps do all the work — ATR sizing degenerates to "always max out".
   The 0.5% default assumes daily-scale ATR.

Left **on** by default because it delivers the specced guarantee (a stop-out
costs a bounded share of equity) and because the return comparison above is not
a clean test of sizing quality. Turn it off in settings for the old behaviour;
ATR stops still apply either way.

Also found: the risk invariant only holds while no clamp binds. For a
low-volatility symbol the 25%-of-cash cap binds before the risk target is
reached, so those positions risk **less** than configured — the safe direction,
but risk is not truly equalised. Pinned by a test.

<details>
<summary>Original spec (for reference)</summary>


### 3.1 ATR

Add `atr(candles, period=14)` to `strategy.py` next to `compute_indicators()`.
True range = max(high−low, |high−prev_close|, |low−prev_close|). Wilder
smoothing, consistent with the existing RSI implementation. Add `atr` and
`atr_pct` to `Diagnostics`.

Candles are already fetched for top candidates and held positions — reuse that
path, do not add new API calls. ~20 lines by hand; do NOT add the `ta` library
(it pulls in pandas/numpy and breaks the stdlib-only design).

### 3.2 Replace the flat sizer

New settings fields:

- `risk_per_trade_pct: float = 0.5`   # % of equity risked per trade
- `atr_stop_multiple: float = 2.0`    # stop distance = N × ATR
- `use_atr_sizing: bool = True`

```
stop_distance = atr * atr_stop_multiple
risk_dollars  = portfolio.equity() * (risk_per_trade_pct / 100)
qty           = risk_dollars / stop_distance
```

Then clamp by, in order: `max_trade_value`, 25% of cash, available cash after
reservations, **and the 2.1 minimum-viable-trade floor**. Live-mode lot
rounding and `_live_guard()` apply unchanged and happen last.

If ATR is unavailable (no candles yet), fall back to flat sizing and set a
reason string saying so. Never silently size on a missing input.

### 3.3 Per-position stop from the same ATR

`stop_loss_pct` is a flat 2% for every symbol. Store an absolute stop price on
`Position` at entry (`entry − atr × multiple`); `_check_mechanical_exits()`
compares against that stored price. Keep flat `stop_loss_pct` as the fallback.

</details>

---

## Phase 4 — Portfolio protections ✅ DONE (4 of 5)

Shipped in `risk.py` (`RiskState` + `check_limits()`), enforced in `execute()`
on BUYs only, persisted in `paper_state.json`, audited on every block.
`tests/test_risk.py`, mutation-tested five ways.

| Rule | Setting | Default |
|---|---|---|
| Concentration cap | `max_concurrent_positions` | 5 |
| Daily deployment cap | `daily_budget` | 0 (off) |
| Daily loss halt | `daily_loss_limit` | 0 (off) |
| Loss-streak cooldown | `cooldown_after_losses` (30 min) | 3 |

Decisions worth knowing:

- **`daily_budget` counts capital deployed CUMULATIVELY today**, not currently
  held. Buying $250, selling, and buying again is $500 of deployment and two
  sets of fees. Treating that as "still $250" would miss exactly the churn that
  costs the most.
- **Days are exchange-local**, so US and SG roll at different moments and each
  market carries its own daily bucket. A UTC reset would clear a US limit
  mid-session. Consequence: with both markets enabled the budget applies *per
  market per day*.
- **The cooldown is global, not per market** — a losing streak is about
  judgment, not about one exchange.
- State survives restarts. A daily cap that resets when the process does is not
  a cap.

### ❌ `max_positions_per_sector` — NOT BUILT, and cannot be

Longbridge `static_info` exposes no sector or industry field. The full attribute
list is `board, bps, circulating_shares, currency, dividend_yield, eps, eps_ttm,
exchange, hk_shares, lot_size, name_cn, name_en, name_hk, stock_derivatives,
symbol, total_shares` — `board` is the exchange board (`SecurityBoard.USMain`),
not a sector.

Shipping the setting anyway would put a control in the UI that can never fire,
which is worse than its absence: it would read as protection that does not
exist. Needs a third-party sector source before it can be built.

<details>
<summary>Original spec (for reference)</summary>


Add to `_check_mechanical_exits()` / the pre-trade gate in `tick()`:

- `max_concurrent_positions: int = 5`
- `max_positions_per_sector: int = 2` — needs sector data from Longbridge
  `static_info`; cache it. Unknown sector = its own sector, never a block.
- `daily_budget: float` — total capital deployable per calendar day
  (**exchange-local**, not UTC — US and SG days roll at different times),
  persisted in `paper_state.json` so it survives a restart. Decide explicitly
  whether "deployed" means currently-held or cumulatively-bought-today; they
  differ by 5× with recycling.
- `daily_loss_limit: float` — halts new buys for the rest of the local day.
- `cooldown_after_losses: int = 3` — N consecutive losers pauses buys 30 min.

Each of these, when it blocks a trade, must write an audit entry with the
reason. A silent block is worse than no block.

</details>

---

## Phase 5 — Edge features (only after Phase 2 shows positive expectancy)

- **News as a veto** — ⚠ PARTIALLY BUILT, and partially unbuildable.
  Longbridge exposes **no** news, earnings or calendar API
  (`[m for m in dir(QuoteContext) if 'news'/'calendar'/'earning' in m]` is
  empty), so headline and earnings vetoes have no data source and `news_gate`
  stays an honest stub. What IS available is `SecurityQuote.trade_status`, and
  that veto is **built**: a symbol the exchange is not trading normally is
  never bought (`Diagnostics.tradable`, `tests/test_tradability.py`). Buying
  into a halt is how you end up holding something you cannot exit.
- ~~**Pre-market AI watchlist.**~~ ✅ **DONE** (quantitative half). `premarket.py`
  ranks gappers by gap × pre-market turnover; the engine builds the watchlist in
  the markets-closed branch of `tick()` and prefers it over turnover discovery.
  **Two screens, dispatched by horizon**: gappers (intraday) vs 20-day leaders
  (swing) — selecting on a one-session gap for a multi-day hold repeats the
  horizon mismatch that Phase 2.3 fixed in the signal engine.
  The **AI catalyst filter is deliberately not built** — no news API means a
  model shown price+volume alone would be guessing. Verified on live data.
- ~~**Closed-trade UI.**~~ ✅ **DONE.** `GET /api/trades?window=&limit=`
  (`closed_trades_report()`, bounded by `MAX_TRADES_RESPONSE`), a "How trades
  ended" breakdown inside the Performance card, and a collapsible **Closed
  Trades** table. Shares the window selector with the metrics so the two views
  of the ledger cannot disagree. `tests/test_trades_api.py`.
- ~~**Backtest train/test split.**~~ ✅ **DONE.** `python3 replay.py
  --split 0.6` reports the first 60% of the window separately from the rest and
  classifies the outcome (overfit / regime-concentrated / consistent), with a
  sample warning under 20 trades per segment. This is the check every earlier
  measurement in this file lacked.

---

## Explicitly recommended against

| | Why |
|---|---|
| Multimodal / visual chart reading | Slow, costly, non-deterministic answer to a question with an exact numerical solution |
| LLM sentiment as a scored signal | Uncalibrated output, lagging free feeds. Fine as a veto (Phase 5) |
| `ta` library | pandas/numpy dependency, breaks stdlib-only design, for indicators mostly already present |
| Pre-market/live split *for speed* | Losses are fees, not latency — proven above. A Mac polling REST is ~10,000× slower than co-located institutions; that gap does not close |
| Using an LLM to compute pivots / support levels | Fixed formulas. Compute them — never ask a language model for arithmetic |
| US options | Needs a paid Longbridge data package (account lacks `USOption` access), and leverages a currently-negative expectancy. Long-only would be the sole cash-compatible structure |
| Shorting | Requires margin; contradicts the cash-only guarantee hard-coded in `execute()` |

---

## Known gaps and debt

- **US/HK fee schedules are unverified estimates.** One real US fill plus
  `python3 calibrate_fees.py` replaces the guess with truth. Until
  then treat US replay P&L as directionally right, magnitude uncertain.
- **`MomentumStrategy` has no `scan_signals_only()`** — latent `AttributeError`
  if it is ever set as the live strategy. Not currently reachable.
- No exchange-holiday calendar (quotes freeze; safe but looks broken).
- Benchmark starting equity is approximated outside session windows.
- Live round-trip tracking is best-effort against exchange sync lag.
- Longbridge exposes real push streaming (`QuoteContext.subscribe` +
  `set_on_quote`) which the app does not use — the right mechanism if a faster
  loop is ever genuinely needed.

---

## Testing

`python3 -m unittest discover tests` — currently 54 tests across
`test_metrics.py`, `test_fees.py`, `test_live_guards.py`. Add `test_sizing.py`
with Phase 3.

Sizing and fees are pure functions; there is no excuse for them being untested.
Include zero, single and all-losses cases. Mutation-test anything guarding real
money: break the guard deliberately and confirm a test fails.

After ANY change to execution, exits or the ledger, run
`python3 replay.py`. Verify end to end by booting the server and
hitting `/api/metrics` and `/api/tick` per `CLAUDE.md`. Check port 8765 for a
running instance first, and shut down anything you start.

---

## Definition of done

**Phase 2:** the app refuses to place a trade that cannot clear its own costs,
and the replay shows positive expectancy at a realistic position size. Until
that is true, every other feature is decoration on a losing system.

**Phases 3–4:** no single trade can lose more than `risk_per_trade_pct` of
equity when the stop fires as intended, and no day can lose more than
`daily_loss_limit`.
