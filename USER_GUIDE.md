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
open trading_tool/.env

# 3. Run
cd /Users/squallchu/stock
python3 -m trading_tool.app

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

## 2. Credentials Setup (`trading_tool/.env`)

The file lives at `trading_tool/.env` (the loader also checks the folder you run
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
| **Max Symbols** | How many discovered symbols to scan (0 = "all", internally capped at 2000 for API-rate reasons) | 100–500 |
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
   - **Positions** — what you own, what you paid, current P&L
   - **Trade Proposals** — what the AI wants to do and why (approve/reject)
   - **AI Ranking** — how each symbol scores right now
   - **Audit Log** — every tick, signal, proposal, and fill as it happens
6. Click **■ End Session** — the session P&L is recorded in Session History
   (positions stay open unless "stop at end" sold them)
7. **Reset Paper Account** wipes the paper portfolio back to your budget

---

## 6. What the Safety Model Guarantees

- No options, no margin, no short selling — hard-coded, not a setting
- Sells only shares actually held; buys only with cash on hand
- Max-loss circuit breaker stops all new buying
- **Mechanical exits fire before the AI acts each tick**: stop loss (default 2%),
  optional profit lock and trailing stop — the AI can exit earlier but can
  never hold past these thresholds
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
- Paper fills include a $1 fee + 0.05% slippage so paper P&L stays honest
  (tune with `PAPER_FEE_PER_TRADE` / `PAPER_SLIPPAGE_BPS` in `.env`)

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
| Port already in use | A previous instance is still running | `pkill -f trading_tool.app` then restart |

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
5. **AI ≠ profit.** Research on LLM trading agents shows most do not reliably
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
