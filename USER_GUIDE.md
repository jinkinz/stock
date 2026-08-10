# AI Trading Tool — User Guide

A local web dashboard where an AI scans the market and trades within limits you set.
**Paper mode (fake money) is the default.** Nothing real is bought or sold until you
deliberately switch on live trading (two separate switches).

---

## 1. Quick Start

```bash
# 1. Install the Longbridge SDK (needed for real market data)
pip install longbridge

# 2. Fill in your credentials (see section 2)
open .env

# 3. Run
cd /Users/squallchu/stock
python3 app.py

# 4. Open the dashboard
open http://127.0.0.1:8765
```

When the app starts it prints a **startup banner** telling you exactly what is
connected:

```
Credentials:
  LONGBRIDGE_APP_KEY         set (32 chars)      ← or "MISSING"
  LONGBRIDGE_APP_SECRET      set (64 chars)
  LONGBRIDGE_ACCESS_TOKEN    set (156 chars)
Longbridge: CONNECTED — real market quotes in use
AI brain:   gemini / gemini-2.5-flash
```

If anything says **MISSING** or **NOT CONNECTED**, fix the `.env` before trusting
any numbers you see — without Longbridge the prices are a **random-walk simulation**.

---

## 2. Credentials Setup (`.env`)

The file lives at `.env` (the loader also checks the folder you run
from, and `~/.env`).

### Longbridge (market data + live trading)

1. Go to https://open.longportapp.com and open the **Developer Portal**
2. Create an app → copy the **App Key**, **App Secret**, and **Access Token**
3. Paste them into `.env` — value directly after the `=`, no spaces, no quotes:

```env
LONGBRIDGE_APP_KEY=abc123...
LONGBRIDGE_APP_SECRET=def456...
LONGBRIDGE_ACCESS_TOKEN=ghi789...
```

⚠ **Common mistakes** (all of these silently fail):
- Missing `=` after the key name
- Leaving the value empty (`LONGBRIDGE_APP_KEY=`)
- Spaces around the `=`
- The **access token expires** (typically every 90 days) — regenerate it in the
  portal if a previously-working setup stops connecting.

### AI provider (the "brain")

Pick one provider and set both lines:

```env
AI_PROVIDER=gemini          # anthropic | openai | gemini | openrouter | ollama | none
GEMINI_API_KEY=AIza...      # the key that matches the provider you chose
```

- `AI_PROVIDER=none` → no AI cost; the built-in momentum rules trade instead.
- If the provider is set but its key is missing, **every AI call fails** and the
  app silently falls back to the momentum rules (the sidebar AI status shows the error).
- `ollama` needs no key — it runs a model locally (install from https://ollama.com).

### How to verify everything is connected

| Check | Connected | Not connected |
|---|---|---|
| Startup banner | `Longbridge: CONNECTED` | `NOT CONNECTED` + the exact reason |
| Sidebar → Quote source | `longbridge-paper` | `paper-sim` (simulated!) |
| Sidebar → Universe source | `Longbridge discovery: N found` | `sample fallback` |
| Sidebar → AI Brain | provider + model name | error text |

---

## 3. The Two Safety Ladders

### Ladder 1 — Paper → Live
| Mode | What happens |
|---|---|
| **Paper** (default) | Orders are simulated locally. Real quotes if Longbridge is connected, but **no real money moves**. |
| **Live** | Orders go to your real Longbridge account — but **only** if the separate **Allow Live Orders** toggle is also on. Both switches must be set; either one alone does nothing. |

Recommended path: run paper sessions until you've seen at least a few weeks of
results you trust, then start live with a small budget.

### Ladder 2 — Manual → Auto approval
| Mode | What happens |
|---|---|
| **Manual** (default) | Every trade the AI wants appears as a proposal card with the dollar amount. You click ✔ approve or ✘ reject. Proposals expire after 5 minutes. |
| **Auto** | The AI executes its own proposals immediately. Use only after you've watched its manual proposals for a while and they make sense. |

---

## 4. Settings Reference

| Setting | Meaning | Suggested start |
|---|---|---|
| **Budget ($)** | Starting cash of the paper account / cap for the session | 1000 |
| **Duration (min)** | Session length; trading stops automatically after this | 390 (one US trading day) |
| **Max Loss ($)** | Circuit breaker — all buying stops if total P&L drops below −this | 2–5% of budget |
| **Max Trade Value ($)** | Cap on any single trade | ≤ 25% of budget |
| **Candidate pool** | How many symbols the scan draws candidates from (25–2000). A bigger pool means better candidates, but does **not** raise how many can be *traded* — that ceiling is the candle budget (40 intraday / 150 swing), shown live under the field. Wider is not automatically better: in testing a larger pool produced more trades of lower average quality | 200 intraday / 500 swing |
| **Markets** | US / HK / SG | US |
| **Trading Mode** | Paper / Live | Paper |
| **Approval** | Manual / Auto | Manual |
| **Scan Interval (s)** | How often a full strategy tick runs | 60 |
| **Target Profit ($)** | The AI's mission goal for the session | optional |
| **Lock Profit (%)** | Mechanical rule: any position up this % is sold automatically, regardless of the AI | 1–2 |
| **Stop Loss (%)** | Mechanical rule: any position down this % from entry is sold automatically | 2 (default) |
| **Trailing Stop (%)** | Mechanical rule: a profitable position falling this % below its peak is sold | 1–2 or off |
| **AI Strategy** | fifo / scalp / swing / conservative / aggressive (see below) | conservative |

*(How signals are scored, how the universe is chosen, and how the AI brain
thinks are covered in depth in section 9 — Deep Dive.)*

### AI strategy styles
- **fifo** — lock profit fast, reinvest freed cash, never let a winner turn loser
- **scalp** — many tiny wins (+0.3–0.5%), tight stops, high turnover
- **swing** — fewer, bigger moves; holds through small dips
- **conservative** — capital preservation first, tiny positions, tight stops
- **aggressive** — concentrated positions, maximum exposure (highest risk)

---

## 5. A Typical Session

1. Start the app, confirm the startup banner shows **CONNECTED**
2. Set Budget / Max Loss / Max Trade Value → **Save Settings**
3. Keep **Approval = Manual** for your first sessions
4. Click **▶ Start Session** — scanning begins on the interval you set
5. Watch:
   - **Performance** — is the strategy actually working (see section 5.1)
   - **Positions** — what you own, what you paid, current P&L
   - **Trade Proposals** — what the AI wants to do and why (approve/reject)
   - **AI Ranking** — how each symbol scores right now
   - **Audit Log** — every tick, signal, proposal, and fill as it happens
6. Click **■ End Session** — the session P&L is recorded in Session History
   (positions stay open unless "stop at end" sold them)
7. **Reset Paper Account** wipes the paper portfolio back to your budget

### 5.1 The Performance card — is this strategy making money?

Session P&L alone can't tell you whether a strategy works; a single lucky trade
hides a losing system. The Performance card measures **closed round trips**
(a position opened and then fully closed), not individual fills. A position you
are still holding contributes nothing until you exit it.

Pick a window — this session, last 24 hours, last 7 days, or all time — and you
get six numbers:

| Number | What it means | What to do about it |
|---|---|---|
| **Expectancy per Trade** | The average dollars a trade is worth: `(win rate × avg win) − (loss rate × avg loss)`. **This is the headline number.** | If it is negative, the strategy loses money by design. Nothing else on the card matters until it is positive. |
| **Win Rate** | Share of closed trades that made money after fees. | A low win rate is fine *if* the wins are much bigger than the losses — check it against expectancy, never alone. |
| **Profit Factor** | Gross wins ÷ gross losses. Above 1.0 means the winners outweigh the losers. Shows **∞** when there are no losing trades yet. | Below 1.0 pairs with negative expectancy — stop and re-tune. |
| **Max Drawdown** | Largest peak-to-trough fall of cumulative profit, in dollars and as a % of the peak. | This is the pain you must be willing to sit through. If it exceeds your comfort, cut Max Trade Value. |
| **Fees as % of Gross** | How much of the gross profit the $1 fee + slippage model ate. | High-turnover styles (scalp) can spend most of the edge here. If this is large, trade less often or in bigger size. |
| **Strategy vs Buy-and-Hold** | Strategy return minus an equal-weight buy-and-hold of the *same symbols over the same window*, priced from real Longbridge candles. | **If this is negative you would have made more money doing nothing.** That is the real bar to beat. |

Only expectancy and vs-benchmark are colour-coded, because those are the two
that decide whether the tool is worth running.

**Small samples lie.** Under 20 closed trades the card shows a visible warning
banner. Ten trades tell you almost nothing about a strategy — resist tuning
against them.

Every closed round trip is also appended to `state/trades_closed.jsonl` with its
`exit_reason` (`profit_lock`, `stop_loss`, `trailing_stop`, `ai_sell`,
`strategy_sell`, `session_end`, `manual`), the signal score and indicator
readings at entry. `exit_reason` is the highest-value field there: it is what
tells you whether your stop loss is protecting you or bleeding you.

The same data is available at `GET /api/metrics?window=session|day|week|all`,
which additionally returns the metrics broken down by exit reason and by AI
strategy style.

### 4.1 Pre-session watchlist (differs by horizon)

By default the scanner ranks the universe by **yesterday's** turnover, which
answers "what was liquid" rather than "what is likely to move today".

With the watchlist on, the app narrows the universe before trading starts — but
**what it ranks on depends on your Horizon**, because the right metric is not
the same for both:

| Horizon | Screen | Why |
|---|---|---|
| **Day trading** | Pre-market **gappers** — gap size weighted by pre-market volume | A gap is a one-session event, which is exactly the timeframe an intraday position lives in |
| **Swing** | **20-day leaders** — strongest over the window *and* near their own highs | A one-session gap is noise across a ten-day hold, and gaps frequently fade. Buying one and holding means eating the fade |

A name up 22% over the window but sitting at the bottom of its range is *not* a
leader — the move already happened and reversed — so it is excluded.

A 3% gap on real pre-market volume outranks an 8% gap on a handful of shares —
thin pre-market books produce large percentage moves that are quote artefacts,
not positions anyone took. Only gap-**ups** are listed, because the app cannot
short: ranking a stock that collapsed 9% would fill the watchlist with names it
is structurally unable to act on.

It falls back to the normal universe automatically when a market has no
pre-open session (HK and SG here) or nothing has traded yet. The watchlist is
shown as its own card once built and expires after 12 hours.

**What it does not do:** pick "catalyst-driven" names. Longbridge exposes no
news, earnings or calendar feed, so a model shown only price and volume cannot
tell a real catalyst from noise — it can only produce a confident guess. The
ranking uses the two things that are actually measurable.

### 5.0 Horizon — the first thing to set

It is the first control in the sidebar because it changes eight other settings,
the candles the indicators are built from, and which AI styles are even offered.
Switching swaps in that horizon's saved tuning — your day-trading numbers are
kept, not overwritten.

| | Day Trading | Swing |
|---|---|---|
| Time control | **Session length** in hours (6.5 = one US day) | **Max hold** in days — closes a stale position regardless of P&L |
| Scan cadence | seconds | minutes (900s = 15 min) |
| Flatten at end | available | hidden — sessions never expire |
| AI styles offered | Conservative, FIFO, Scalp, Aggressive | Conservative, Swing, Aggressive |

**Why the AI styles differ.** *FIFO* is written around a session countdown
("reach the target before time runs out") — meaningless when there is no
deadline. *Scalp* targets +0.3% per trade, which is below the round-trip cost
at every position size measured, so it loses money by construction. Neither is
offered in swing mode. *Conservative* and *Aggressive* are risk postures and
work on both.

### 5.0a Horizon — what actually changes under the hood

The **Horizon** setting is the single most consequential choice in the app,
because it decides whether your budget can clear its own trading costs.

| | Intraday | Swing |
|---|---|---|
| Candles | 1-minute | Daily |
| EMA9 / EMA21 measure | 9 and 21 **minutes** | 9 and 21 **days** |
| Session | ends after Duration; `stop at end` can flatten | never auto-expires; you end it |
| Positions | closed same day | held overnight |
| Typical target | 0.5–1% | 3%+ |

**Why this matters more than any strategy setting.** On a small position, a
round trip costs roughly 1.7% (US) or 1.05% (SG). An intraday target of 0.8%
cannot clear that — the trade loses money when it wins, and the viability check
will block it outright. A 3% swing target clears the same cost comfortably. If
you are trading a few hundred dollars a day, swing is not one option among
several; it is the only setting under which the app can trade at all.

**The cost of holding overnight** is gaps. A stop is a *trigger*, not a
guaranteed price: exits are only evaluated when the app looks at the market, so
a stock that opens 8% below your stop is sold at that opening price, not at the
stop. When this happens the trade record says so explicitly — you will see
`GAPPED x% THROUGH the stop` in the exit reason rather than an unexplained
oversized loss. This risk is real and cannot be engineered away; it is the price
of the better economics.

**One honest caveat.** In testing over a rising year, the strategy in swing mode
returned about +2.7% while simply buying and holding the same symbols returned
+34%. It protects capital when prices fall and lags badly when they rise. Swing
mode makes the *costs* work; it does not by itself make the strategy beat the
market.

### 5.1 Closed Trades — which trades, and why each one ended

The Performance card tells you *whether* the strategy works. The **Closed
Trades** card tells you *why*. It lists every completed round trip — entry and
exit price, how long it was held, net P&L after fees, and the reason it closed.

Above it, inside the Performance card, is a **"How trades ended"** breakdown:
every exit reason with its trade count, win rate, total P&L and average per
trade. This is the single most useful table in the app. It answers the question
you cannot get from a P&L number alone: *is my stop loss protecting me, or is it
bleeding me?*

A pattern worth watching for: if `Stop loss` shows many trades and a large
negative total while `Profit lock` shows a similar count and a similar positive
total, your thresholds are simply trading noise back and forth and paying fees
for the privilege. That is exactly what the replay measurements showed at small
position sizes.

Both views follow the same window selector, so the numbers always describe the
same period. The list is capped at the 100 most recent trades in view; the full
history is always in `state/trades_closed.jsonl`, and available at
`GET /api/trades?window=…&limit=N`.

### 5.1a The viability check — "unprofitable by construction"

Before any of the numbers above matter, one question has to be answered: **can
this trade size clear its own costs?**

If your Profit Lock is 0.8% but a round trip costs 1.7% in fees and slippage,
then hitting your target still leaves you down 0.9%. The trade loses money
*when it wins*. No strategy, indicator or AI fixes that — it is arithmetic.

The app now checks this before placing any buy, using your real configured
trade size, profit target and the actual fee schedule for that market. When the
numbers don't work you get a red banner at the top of the dashboard, a warning
in the startup banner, and **new buys are blocked**. The message always tells
you what would fix it — the trade size that *would* work, or the target you'd
need instead.

A worked example, same 0.8% target throughout:

| Trade size | Round-trip cost (US) | A winning trade nets | Result |
|---|---|---|---|
| $250 | 1.70% | **−0.90%** | blocked |
| $574 | 0.80% | ~0.00% | break-even threshold |
| $25,000 | 0.22% | **+0.58%** | fine |

Two ways out, and they're the two strategies open to you: **trade larger** (fees
are mostly flat minimums, so cost as a percentage falls fast with size) or **aim
further** — a 3% swing target clears the same 1.70% cost comfortably even at
$250.

If you set no Profit Lock at all, viability can't be judged and nothing is
blocked — but the banner still tells you what a round trip costs, so you know
what the strategy has to beat.

To turn the block off (it stays visible either way), untick **enforce trade
viability** in settings. Only do that if you're deliberately measuring the
damage.

### 5.1b The convergence gate — fewer, better trades

Trading costs are **cost × frequency**, so the cheapest way to stop bleeding
fees is to trade less often without giving up the good trades.

The signal engine scores each symbol on a blend of factors, but a blend can hide
disagreement: a big day move alone can drag the total over the line while trend,
VWAP and volume all say no. The convergence gate counts five **independent**
confirmations and requires them to agree:

| Factor | Confirms when |
|---|---|
| **Trend** | EMA9 is above EMA21 |
| **VWAP** | price is above the session VWAP |
| **Volume** | recent volume is at or above the session average |
| **Structure** | price sits in the top third of the day's range |
| **Momentum** | short-term tick push is still positive |

A factor with **no data** counts as *not* confirmed — absence of evidence never
counts as evidence.

Default is **5 of 5**. In testing across three different datasets this improved
average profit per trade every time and cut trade count by around 40%. The most
useful finding was that the "almost agreed" band — exactly 4 of 5 — was the
*worst* performing group of all, losing money consistently across 45 trades.
Near-misses are not weak buys; they are the expensive ones.

You can relax it in settings if you want more activity, but expect more trades
and more fees. Set it to **Off** to restore the old score-only behaviour.

One honest caveat: this was measured on replayed history over a single market
period, so treat it as a sensible default rather than a proven edge. The
Performance card is what will tell you whether it holds up on real sessions.

### 5.2 Fees — why paper P&L is not free money

Paper fills are charged the same fee stack a real order pays. This matters more
than it sounds: on a real SGX order of about SGD 500 the charges come to roughly
**0.26%**, so a round trip costs about **0.5%**. Against a 0.8% profit-lock
target, fees eat most of the edge before the strategy has done anything.

Fees are modelled per market, per side:

| Market | Source | Notes |
|---|---|---|
| **SG** | **Measured** | Derived from real contract notes on this account and reproduces them to the cent: commission 0, platform fee SGD 0.99, clearing 0.0325%, trading 0.0075%, GST 9% on (platform + clearing) |
| **US** | *Estimate* | No real US fills existed to measure. Set deliberately on the expensive side — paper P&L is pessimistic rather than flattering |
| **HK** | *Estimate* | Same caveat. HK live orders are blocked anyway |

Anything marked *Estimate* is a placeholder, and the startup banner says so
every time the app boots. To replace a guess with the truth:

```bash
python3 calibrate_fees.py
```

It reads the real charges Longbridge billed on your own filled orders and prints
them line by line against what the model predicted. It is read-only — it places
no orders and edits nothing. Correcting `fees.py` is deliberately a
manual step, because a schedule silently rewritten from three orders is how you
end up with a confident wrong number.

Two directions of error, and they are not equal. **Over-charging is safe** — your
paper results look worse than reality. **Under-charging is dangerous** — it makes
a losing strategy look profitable. The calibration output calls out under-charging
loudly and treats over-charging as acceptable.

In live mode nothing is modelled at all: the real charge is read back off the
filled order, and each closed trade records whether its fee figure was `actual`,
`modelled`, or `unknown`.

`PAPER_FEE_PER_TRADE` in `.env` overrides the whole model with a flat per-order
charge. Useful for a quick what-if; it will not resemble a real bill, and the
banner warns whenever it is active.

---

## 6. What the Safety Model Guarantees

- No options, no margin, no short selling — hard-coded, not a setting
- Sells only shares actually held; buys only with cash on hand
- Max-loss circuit breaker stops all new buying
- **Mechanical exits fire before the AI acts each tick**: stop loss (default 2%),
  optional profit lock and trailing stop — the AI can exit earlier but can
  never hold past these thresholds
- **In live mode, the tool can never deploy more than your Budget.** The limit
  is the cost basis of what it currently holds plus anything in flight — not
  your account balance. If your account holds $50,000 and Budget is $1,000, it
  can have at most $1,000 at cost invested at any moment; an oversized order is
  trimmed to fit and then refused. Selling frees the room up again.
- **In live mode, it only ever sells what it bought.** Positions you opened
  yourself, or that predate the tool, are invisible to it — no stop loss, no
  profit lock, no AI sell, and they don't consume your Budget. The flip side:
  if you open a position by hand, the tool's stop loss does **not** protect it.
- **Live orders are limited to US and SG symbols** (USD and SGD), the
  currencies whose balance the app can verify. An HK order is refused outright
  rather than risk an uncovered position in a currency it cannot check.
- Every live order is also checked against the real available cash in its own
  currency. No borrowing, no FX overdraft.
- Live orders need **two** independent switches (Live mode + Allow Live Orders)
- Live orders are rounded to **whole shares and board-lot multiples** (HK
  stocks trade in lots of 100/500/…, looked up from exchange data automatically)
- Live fills are **confirmed against the exchange** — after submitting, the
  order status is polled; only a confirmed fill is recorded as filled, at its
  actual executed price
- **Market-hours gate** (with Longbridge connected): only markets currently in
  their trading session are scanned and traded — US 9:30–16:00 ET, HK
  9:30–12:00/13:00–16:00, SG 9:00–12:00/13:00–17:00, weekdays. The sidebar
  "Markets" row shows live open/closed status. When everything is closed, the
  engine idles instead of trading frozen prices. (Sim mode ignores this so you
  can test at any hour.)
- Manual proposals auto-expire after 5 minutes so a stale price is never filled
- Rate limiter respects Longbridge's 30 calls / 30 s trade-API limit
- Paper fills are charged **real per-market brokerage fees** plus 0.05%
  slippage, so paper P&L is not flattered by pretending trading is free (see
  section 5.2)

---

## 7. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Quote source says `paper-sim` | Longbridge not connected | Check startup banner; fill `.env`; regenerate expired access token |
| Prices look wrong / impossible | You're on the simulator | Same as above — sim prices are random |
| AI Brain shows an error | Provider key missing/invalid | Set the matching `*_API_KEY` in `.env`, or `AI_PROVIDER=none` |
| No proposals ever appear | Session not started, or every symbol still "collecting history" (needs 8 ticks) | Start the session and wait ~8 scan intervals |
| Trades never fill in live mode | `Allow Live Orders` off, or quantity below one share/board lot | Enable the toggle; raise Max Trade Value |
| Live order shows "fill not confirmed" | Limit order accepted but not yet executed | It may still fill — check the Longbridge app; the order expires at day end |
| Nothing scans at night | All selected markets are closed (see sidebar "Markets" row) | Normal — trading resumes at market open. Holidays are not detected (quotes just stay frozen, which is safe) |
| Disk/RAM growing | Fixed in this version (slim trade log, log rotation, capped payloads) | Delete old `state/trade_log.jsonl` / `state/audit_log.jsonl.old` if still large |
| Port already in use | A previous instance is still running | `pkill -f 'python3 app.py'` then restart |

---

## 8. Honest Limitations (read before going live)

1. **The backtest uses real Longbridge 5-minute candles when connected** (the
   result panel shows the data source in green). Without a connection it falls
   back to random walk and is labeled in red — those results mean nothing.
   Even real-candle backtests replay a limited window (~3 days of 5-min bars);
   treat them as a sanity check, not proof of profitability.
2. **Signals need Longbridge to be meaningful.** Connected, they use real day
   change, VWAP, EMA and RSI. Disconnected, they're simple momentum on random
   simulated prices — fine for learning the UI, useless for judging strategy.
3. **Day trading regulation:** in the US, a margin account under $25,000 is
   limited to 3 day trades per 5 business days (PDT rule). Cash accounts avoid
   PDT but funds from sales settle T+1 before reuse.
4. **No overnight risk handling** — keep "stop at end" on so sessions end flat.
5. **Performance metrics count closed round trips only.** Open positions are
   invisible to the card, so a window where you bought and held shows no
   trades. In **live** mode the broker's real commissions are not returned by
   the order API, so live records model **zero fees** (`fees_modelled: false`
   in the ledger) — live net P&L is therefore optimistic. Paper mode models
   the $1 + 0.05% friction fully.
6. **The benchmark is a fair-but-rough comparison.** It equal-weights buy-and-hold
   of up to 10 symbols you actually traded, priced from real candles over the
   same window. It ignores position sizing and the order you entered in, so
   treat a small gap either way as noise — a large, persistent negative gap is
   the signal that matters.
7. **AI ≠ profit.** Research on LLM trading agents shows most do not reliably
   beat buy-and-hold. Treat this tool as a disciplined execution assistant with
   hard safety rails, not a money printer. Trade only what you can afford to lose.

---

## 9. Deep Dive — How the Strategy & AI Brain Work

The system is three decision layers stacked on top of each other. Lower layers
are mechanical and always win; the AI sits on top and only acts within the
space the lower layers leave open.

```
┌───────────────────────────────────────────────────────────┐
│ Layer 3 — AI BRAIN (optional)                             │
│ LLM picks WHAT to buy/sell and HOW MUCH, following the    │
│ trading style you chose (fifo/scalp/swing/…)              │
├───────────────────────────────────────────────────────────┤
│ Layer 2 — MECHANICAL EXITS (always on if configured)      │
│ stop loss · trailing stop · profit lock · max-loss        │
│ circuit breaker · session end flatten                     │
├───────────────────────────────────────────────────────────┤
│ Layer 1 — SIGNAL ENGINE (always on)                       │
│ Scores every symbol 0–1 from real market data and         │
│ produces the ranked list everything above works from      │
└───────────────────────────────────────────────────────────┘
```

### 9.1 The tick pipeline — what happens every scan interval

Each tick (default every 60s while a session runs) executes in this exact order:

1. **Resolve the universe** — custom list if you typed one; otherwise the
   Longbridge-discovered working set (see 9.2); otherwise the built-in
   123-symbol sample list.
2. **Fetch quotes** for the whole working set (batched 200 per API call).
   Each quote carries price, previous close, day open/high/low, volume, turnover.
3. **Expire stale proposals** — in manual mode, anything older than 5 minutes.
4. **Session clock check** — if the duration is up: stop trading, record the
   session, and (if "stop at end" is on) sell everything at market.
5. **Candle enrichment** — fetch 1-minute candles (120 bars) for every held
   position plus the ~15 biggest day movers, and compute VWAP / EMA9 / EMA21 /
   RSI(14) for them. Cooldown of 55s per symbol keeps API usage sane.
6. **Mechanical exits fire first** (see 9.4) — any triggered stop/lock becomes
   a SELL proposal *before* the AI is even consulted.
7. **Signal engine scores every symbol** (see 9.3) → ranked list.
8. **AI brain decides** (see 9.5) — at most once per 30s. If the AI is off or
   errors, the rule-based proposal builder (9.3) decides instead.
9. **Proposals dispatch** — auto mode executes immediately; manual mode queues
   them for your approval. Duplicate (symbol, side) pairs are suppressed.
10. **State saved, UI pushed** over SSE.

A separate lightweight loop refreshes displayed prices every 10s without
running any of the above (and never calls the paid AI).

### 9.2 Universe selection — "scan all stocks" done right

Once per 30 minutes, with Longbridge connected:

1. `security_list` discovers the full tradable market (30,000+ US symbols;
   HK/SG via index constituents).
2. One bulk quote pass ranks everything by **turnover** (dollars traded today),
   **bucketed per market**. Each selected market gets an equal share of the
   cap first, then the big US bucket absorbs the remainder.
3. The combined slice — your Max Symbols setting, ceiling 2000 — becomes the
   working set that every tick actually scans.

Why turnover? Day trading needs liquidity: tight spreads, instant fills, real
price discovery. The most-liquid slice is where all of that lives; the other
28,000 symbols are mostly untradeable noise for a retail day trader.

**Why per-market bucketing matters:** a single global turnover sort is
dominated by US names (their traded value dwarfs HK/SG), so the top-2000 would
be *entirely US*. Whenever US is closed but HK/SG are open, the market-hours
gate would then strip every symbol and the tool would scan **nothing** — no
ticks, no signals — for the whole Asia session. Bucketing per market
guarantees the open market always has liquid names to trade.

### 9.3 Layer 1 — the signal engine, formula by formula

Every symbol gets a composite score in [0, 1]. The weights:

| Factor | Formula | Weight | Day-trader logic |
|---|---|---|---|
| Day momentum | `clamp(day_change% / 5, −0.5, 1)` | ×0.30 | Stocks up on the day tend to continue intraday; gains past +5% are treated as already-chased |
| Range position | `(price − day_low) / (day_high − day_low)` | ×0.15 | Price near the day high = buyers in control = breakout behaviour |
| Tick momentum | `clamp(3-tick avg / 30-tick avg − 1, ±0.25%) × 400` | ×0.10 | Short-term confirmation that the move is happening *now* |
| VWAP position | above VWAP +0.15, below −0.15 | ±0.15 | VWAP is the institutional fair-value line; longs are defended above it |
| EMA trend | EMA9 > EMA21 +0.15, else −0.15 | ±0.15 | Classic short-term trend filter |
| Volume surge | `clamp((rvol − 1) / 1.5, −0.5, 1)` | ×0.15 | Real momentum expands volume; a breakout on drying volume is a fakeout |

`rvol` (relative volume) = recent-3-minute average volume ÷ session average
per-minute volume, from the 1-minute candles. >1 = heavier than the session
average, <0.7 = drying up.

**Indicator math** (computed from 120 × 1-minute candles):
- **VWAP** = Σ(typical price × volume) / Σ(volume), typical = (close+high+low)/3
- **EMA** with standard smoothing k = 2/(span+1)
- **RSI(14)** with Wilder averaging of gains vs losses
- **RVOL** = recent 3-min avg volume ÷ session avg per-minute volume

**A BUY signal requires all of:**
- composite score ≥ **0.55**
- day change **positive**
- RSI **< 75** (never buy overbought)
- turnover ≥ **$500,000** today (liquidity gate — hard skip below this)
- price **at or above VWAP** (never initiate a long below institutional fair
  value; a VWAP of 0 means "no candles yet" and does not block)
- no position already held in the symbol

**A SELL signal (for a held position) fires on any reversal sign:**
- RSI ≥ 80 (exhaustion), or
- price below VWAP **and** EMA9 < EMA21 (trend broken), or
- price below VWAP **and** tick momentum < −0.3%, or
- day change < −1%

Note the momentum- and trend-based exits now require a **VWAP loss** to
confirm. A position still holding above VWAP is not shaken out by tick noise —
hard risk (stop-loss / trailing-stop) is still enforced independently by the
engine regardless of what the signal engine thinks.

Everything else is a WATCH with a sub-0.5 score. Every signal carries a
human-readable reason built from the factors that actually fired, e.g.
`"Uptrend: day +2.31%, near day high, above VWAP, EMA9>21"`.

**Without Longbridge** (sim mode) none of the real factors exist, so the
engine falls back to pure tick momentum: buy > +0.2% momentum, sell < −0.2%.
This keeps the UI testable but has zero predictive value.

**Rule-based position sizing** (used when the AI is off or fails): walk the
ranked buy signals from the top, spend `min(max_trade_value, remaining_cash)`
on each, stop when cash runs out; max 5 proposals per tick. Sells always close
the full position.

### 9.4 Layer 2 — mechanical exits (the AI cannot override these)

Checked every tick for every held position, in this order of precedence:

| # | Rule | Trigger | Why it exists |
|---|---|---|---|
| 1 | Profit lock | gain ≥ `lock_profit_pct` | Guarantees a winner is banked at your threshold — greed-proof |
| 2 | Stop loss | loss ≥ `stop_loss_pct` from entry | Caps single-position damage — hope-proof |
| 3 | Trailing stop | position was in profit and price fell `trailing_stop_pct` below its **peak** | Lets winners run but refuses to ride them back down |

The position's peak price is tracked continuously from every quote update and
survives app restarts. On top of these per-position rules sit two portfolio-level
guards: the **max-loss circuit breaker** (total P&L ≤ −max_loss → all buying
stops; the AI is instructed to liquidate) and **session-end flatten**.

The AI is told about every active mechanical rule in its prompt, so it doesn't
waste decisions duplicating them — but if it tries to hold a position past a
threshold, the mechanical layer sells anyway. That's the design: judgment on
top, guarantees underneath.

### 9.5 Layer 3 — the AI brain, call by call

**When it runs:** the AI is only consulted when there is an actual decision to
make. Each tick it is gated by three checks, in order:

1. **Hard rate cap** — never more than once per `AI_MIN_INTERVAL_SECONDS`
   (default 30s).
2. **Actionable check** — skip entirely unless you either hold a position
   (needs managing) or there's an affordable buy candidate. A flat account with
   no buy signal gives the AI nothing to do, so no call is made.
3. **Change check** — if the decision picture (held names + their P&L bucketed
   to 0.5%, the top buy candidates, target-met flag) is identical to the last
   call, skip — unless `AI_HEARTBEAT_SECONDS` (default 180s) has elapsed, which
   forces a periodic re-evaluation while holding.

The effect: an idle scan makes **zero** AI calls; an active session calls only
on real state changes. This typically cuts token usage by 80–95% versus calling
every tick. The `ai_status` panel shows `call_count` vs `skipped_count` so you
can see it working. Only actionable signals (buy candidates + held names, ≤12)
are sent in the prompt, not the full top-20 — smaller prompts, fewer tokens.

Tune it in `.env`: raise `AI_MIN_INTERVAL_SECONDS` to call less often, raise
`AI_HEARTBEAT_SECONDS` to re-evaluate held positions less frequently.

**Safety:** skipping an AI call never removes risk protection — the mechanical
exits (stop-loss / trailing-stop / profit-lock, layer 2) run every tick
regardless of whether the AI was consulted.

**What the AI receives** (rebuilt fresh every call):

1. **Its rulebook (system prompt)** — absolute constraints it cannot be talked
   out of: cash only, never sell more than held, ≤35% of cash per position,
   liquidate everything if max-loss is hit, answer in strict JSON only.
2. **Your chosen strategy's rules** — the full rule list for fifo / scalp /
   swing / conservative / aggressive (see 9.6).
3. **Mission status** — budget, cash, max trade value, max loss, profit target,
   realized + unrealized P&L, dollars still needed, minutes elapsed/remaining,
   and an **urgency flag** that escalates as time runs out:
   🟢 normal → 🟡 halfway & below target → 🟠 <25% time left → 🔴 <10% left.
4. **Notices of active mechanical rules** — e.g. "any position −2% is
   auto-sold; you may exit earlier."
5. **Every open position** — shares, avg cost, current price, value, P&L $ and %.
6. **The top 20 ranked signals** with full diagnostics: day change, RSI, VWAP
   distance, EMA trend, turnover, volatility, and the plain-English reason.

**What the AI returns:** a JSON array of decisions —
`{symbol, action: buy|sell|hold, quantity, confidence, reason}`.

**How its answer is sanitized before anything executes** (never trust the
model blindly):
- markdown fences stripped, JSON array extracted
- `hold` and unknown symbols dropped
- SELL quantity capped at shares actually held
- BUY quantity capped at `min(max_trade_value, 35% of cash)`, and skipped
  entirely if the symbol is already held or cash can't cover it
- maximum 5 proposals per call

**When it fails** (bad key, timeout, malformed JSON): the error is shown in
the sidebar AI status, a fallback counter increments, and the rule-based
proposal builder takes over for that tick. Trading never silently stops.

### 9.6 The five AI trading styles — exact rules given to the model

**fifo — Lock Profit Fast** *(default)*
Buy the top 3–5 momentum signals, never >25% in one position. The moment any
position's unrealized gain covers its share of the remaining profit target,
sell it and reinvest the freed cash into the next best signal. Once the target
is met: stop buying, protect gains. Under 20% time left and below target:
sell even small gains. Never let a winner turn into a loser.

**scalp — Many Small Wins**
Buy anything with score > 0.55. Take profit at +0.3–0.5% per trade, hard stop
at −0.2%. Max 5 positions, small sizes, high turnover — ten +0.3% wins beat
waiting for one big move. Flat by the last 10 minutes.

**swing — Ride the Trend**
Only signals with score > 0.70. Hold through dips < 0.5% from the high. Exit
only when score drops below 0.40, the position falls >1% off its peak, or
<15 minutes remain. Positions up to 30%; aim for 1–2% per trade; consider
selling half at +1.5% and letting the rest ride.

**conservative — Protect Capital**
Only score > 0.75 AND volatility < 25%. Max 12% of cash per position. Stop
loss −0.4%, take profit +0.4%. If total P&L ever goes negative, stop all
buying. Ending flat beats losing money.

**aggressive — Swing for the Fences**
Top 2–3 signals regardless of score, up to 40% of cash each — concentrate,
don't diversify. Hold until target share met or −1.5% stop. Early in the
session let winners run to 3%+; under 30% time left, sell everything green
and cut all losers. Highest risk — watch it live.

### 9.7 Do you even need the AI?

Honest answer: the signal engine + mechanical exits already implement a
complete disciplined day-trading system. The AI adds **portfolio-level
judgment** the rules can't express: balancing the profit target against time
remaining, choosing *which* of several good signals to fund, partial exits,
and adapting position sizes to how the session is going. It does **not** add
price prediction — nothing does. If you want zero API cost, set
`AI_PROVIDER=none` and run pure rules; expect similar behaviour, slightly less
adaptive. The best use of your money is Gemini Flash / Claude Haiku tier —
a smarter model can't outthink the information it's given.
