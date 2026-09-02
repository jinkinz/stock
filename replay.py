"""
Replay harness — drive the REAL TradingEngine over historical candles.

Why this exists
───────────────
The round-trip ledger (`trades_closed.jsonl`) is written by
`TradingEngine._record_round_trip()`, which only runs on a confirmed fill from
`TradingEngine.execute()`. That whole path — real quotes → signal engine →
proposal → auto-approve → fill → close-out — can only be exercised while a
market is open, which means a bug in it is invisible until a live session is
already underway and a day of measurement has been lost.

This harness replays real historical bars through the actual engine so that
path runs on demand, at any hour, with no market open and no orders placed.

What it does NOT do
───────────────────
This is not a backtest and its P&L means nothing. `run_backtest()` in app.py
answers "would this strategy have made money"; it builds its own broker and
strategy and never calls `execute()`, so it proves nothing about the ledger.
This harness answers the opposite question: "does the trade-recording
machinery actually work end to end". Judge it on the checks it prints, not on
the returns.

It also uses `MomentumStrategy` directly rather than `AIStrategy`: the AI is
throttled to one call per 30 s and costs money per call, neither of which
survives a loop over hundreds of bars. The AI's own sell path is one tagged
`OrderProposal`; everything downstream of it is what this exercises.

Usage
─────
    python3 replay.py
    python3 replay.py --symbols AAPL.US,NVDA.US --bars 400
    python3 replay.py --period Min_15 --lock-profit 1.0 -v

Requires a working Longbridge connection to fetch the history. All state is
written to a throwaway temp directory — your real state/ files are never
touched.
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from broker import PaperBroker
from models import ApprovalMode, Quote, Settings, TradingMode
from strategy import MomentumStrategy

# Exit reasons a closed trade is allowed to carry. A record outside this set
# means a sell site was added without tagging it.
VALID_EXIT_REASONS = {
    "profit_lock", "stop_loss", "trailing_stop",
    "ai_sell", "strategy_sell", "session_end", "max_hold", "manual",
    # Slot-releasing exits: they close on time, on a given-back gain, on the
    # entry reason expiring, and on a stronger signal — not on price.
    "stall", "breakeven", "thesis_break", "rotation",
}

# Deliberately broad and volatile. The signal engine is selective — it needs
# score ≥0.55 AND a positive day change AND RSI <75 AND price above VWAP all at
# once — so a handful of mega-caps over a quiet window can produce zero entries
# and the replay then proves nothing. Breadth is what makes entries fire.
DEFAULT_SYMBOLS = [
    "AAPL.US", "NVDA.US", "TSLA.US", "AMD.US", "MSFT.US", "META.US",
    "AMZN.US", "GOOGL.US", "COIN.US", "PLTR.US", "MRNA.US", "F.US",
]


# ---------------------------------------------------------------------------
# Timeline
# ---------------------------------------------------------------------------

def _enrich_timeline(bars: list[dict]) -> list[dict]:
    """Turn raw candles into per-bar snapshots that look like a live Quote.

    A real quote's high/low/volume/turnover are DAY-to-date, not per-bar, and
    the signal engine leans on that: range position is
    `(price - day_low) / (day_high - day_low)`. Feeding it per-bar highs would
    make every bar look like it sits mid-range and the replay would exercise a
    scoring path that never happens in production. So day context is
    accumulated here, and reset when the calendar date rolls over.
    """
    out: list[dict] = []
    day_key: str | None = None
    day_open = day_high = day_low = 0.0
    day_volume = day_turnover = 0.0
    prev_close = 0.0
    prior_day_close = 0.0

    for bar in bars:
        close = float(bar.get("close", 0) or 0)
        if close <= 0:
            continue
        high = float(bar.get("high", 0) or 0) or close
        low = float(bar.get("low", 0) or 0) or close
        stamp = bar.get("timestamp") or ""
        key = stamp[:10]

        if key != day_key:
            if out:
                prior_day_close = out[-1]["close"]
            day_key = key
            day_open = float(bar.get("open", 0) or 0) or close
            day_high, day_low = high, low
            day_volume = day_turnover = 0.0
            # No prior day in the window (or no timestamps at all): fall back
            # to the window's opening price, so day change reads as change
            # since replay start rather than being undefined.
            prev_close = prior_day_close or day_open

        day_high = max(day_high, high)
        day_low = min(day_low, low) if day_low > 0 else low
        day_volume += float(bar.get("volume", 0) or 0)
        day_turnover += float(bar.get("turnover", 0) or 0)

        out.append({
            "close": close, "open": float(bar.get("open", 0) or 0) or close,
            "high": high, "low": low,
            "volume": float(bar.get("volume", 0) or 0),
            "turnover": float(bar.get("turnover", 0) or 0),
            "timestamp": stamp,
            "prev_close": prev_close, "day_open": day_open,
            "day_high": day_high, "day_low": day_low,
            "day_volume": day_volume, "day_turnover": day_turnover,
        })
    return out


class ReplayBroker(PaperBroker):
    """PaperBroker with its quote feed swapped for a replayed timeline.

    Fill accounting, fees, slippage and the round-trip accumulators are all
    inherited unchanged — that is the point, they are what we are testing.
    """

    def __init__(self, timeline: dict[str, list[dict]], starting_cash: float) -> None:
        super().__init__(starting_cash=starting_cash)
        self._lb = None            # never touch the network mid-replay
        self._timeline = timeline
        self._index = 0

    def set_index(self, index: int) -> None:
        self._index = index

    def _bar(self, symbol: str) -> dict | None:
        bars = self._timeline.get(symbol)
        if not bars or self._index >= len(bars):
            return None
        return bars[self._index]

    def quotes(self, symbols: list[str]) -> list[Quote]:
        result: list[Quote] = []
        for symbol in symbols:
            bar = self._bar(symbol)
            if bar is None or bar["close"] <= 0:
                continue
            price = bar["close"]
            self._prices[symbol] = price
            self._portfolio.last_prices[symbol] = price
            result.append(Quote(
                symbol=symbol, price=price,
                timestamp=bar["timestamp"] or datetime.now(timezone.utc).isoformat(),
                # Anything but "paper-sim" so the strategy takes its real-data
                # scoring path (VWAP/EMA/RSI) rather than the sim fallback.
                source="replay",
                prev_close=bar["prev_close"], open=bar["day_open"],
                high=bar["day_high"], low=bar["day_low"],
                volume=bar["day_volume"], turnover=bar["day_turnover"],
            ))
        self._update_peaks()
        return result

    def quote(self, symbol: str) -> Quote:
        found = self.quotes([symbol])
        return found[0] if found else self._simulated_quote(symbol)

    def candles(self, symbol: str, period: str = "Min_1", count: int = 120) -> list[dict]:
        """History up to (and including) the current bar — never the future."""
        bars = self._timeline.get(symbol) or []
        window = bars[max(0, self._index + 1 - count): self._index + 1]
        keys = ("close", "open", "high", "low", "volume", "turnover", "timestamp")
        return [{k: bar[k] for k in keys} for bar in window]

    def discover_symbols(self, markets: list[str]) -> list[str]:
        return list(self._timeline.keys())


# ---------------------------------------------------------------------------
# Replay
# ---------------------------------------------------------------------------

def fetch_history(symbols: list[str], period: str, count: int) -> dict[str, list[dict]]:
    """Real candles per symbol, keyed by symbol. Needs Longbridge connected."""
    from broker import LB_STATUS

    source = PaperBroker(starting_cash=0.0)
    if not LB_STATUS["connected"]:
        raise SystemExit(
            f"Longbridge is not connected ({LB_STATUS['error']}).\n"
            "The replay harness needs real history — check .env "
            "and whether the access token has expired."
        )
    history: dict[str, list[dict]] = {}
    for symbol in symbols:
        try:
            candles = source.candles(symbol, period=period, count=count)
        except Exception as exc:
            print(f"  {symbol}: fetch failed ({exc})")
            continue
        timeline = _enrich_timeline(candles)
        if len(timeline) < 30:
            print(f"  {symbol}: only {len(timeline)} usable bars — skipped")
            continue
        history[symbol] = timeline
        stamps = [b["timestamp"] for b in timeline if b["timestamp"]]
        span = f"{stamps[0]} → {stamps[-1]}" if stamps else "no timestamps"
        print(f"  {symbol}: {len(timeline)} bars  ({span})")
    return history


def _split_report(app, trades: list[dict], split_at: str, fraction: float) -> None:
    """Metrics for the first `fraction` of the window vs the rest.

    Every measurement in this project so far has been in-sample: settings were
    chosen by looking at the same bars they were then scored on, which is how a
    backtest produces a beautiful number that evaporates live. Splitting the
    window is the cheapest available check on whether a result generalises.
    A large train/test gap means the settings are fitted to noise.
    """
    from metrics import compute_metrics
    train = [t for t in trades if t.get("closed_at", "") < split_at]
    test = [t for t in trades if t.get("closed_at", "") >= split_at]
    print(f"\n{'─' * 64}\nTrain / test split at {fraction:.0%} of the window\n{'─' * 64}")
    print(f"  boundary: {split_at}")
    rows = [("TRAIN (in-sample)", compute_metrics(train)),
            ("TEST (out-of-sample)", compute_metrics(test))]
    print(f"\n  {'segment':<22} {'trades':>7} {'expectancy':>12} {'net P&L':>11} {'win rate':>9}")
    for label, m in rows:
        print(f"  {label:<22} {m['total_trades']:>7} {m['expectancy_per_trade']:>12.2f} "
              f"{m['net_pnl']:>11.2f} {m['win_rate'] * 100:>8.1f}%")

    train_exp = rows[0][1]["expectancy_per_trade"]
    test_exp = rows[1][1]["expectancy_per_trade"]
    if not train or not test:
        print("\n  One segment is empty — nothing to compare. Try a different --split.")
        return

    if test_exp <= 0 < train_exp:
        print("\n  ⚠ OVERFIT: profitable in-sample, unprofitable out-of-sample. That is")
        print("    the signature of settings fitted to this particular window. Do not")
        print("    trust the in-sample number.")
    elif train_exp <= 0 < test_exp:
        # Not overfitting, but not reassuring either: the whole result rests on
        # the later, smaller segment.
        print("\n  Flat or negative in-sample, profitable out-of-sample — the opposite")
        print("    of overfitting, but it means the entire result rests on the test")
        print("    segment rather than on a consistent edge across the window.")
    elif train_exp > 0 and test_exp > 0:
        ratio = test_exp / train_exp
        verdict = ("consistent" if 0.5 <= ratio <= 2.0
                   else "very uneven between halves")
        print(f"\n  Profitable in both segments, {verdict} "
              f"(test is {ratio:.1f}× the in-sample expectancy).")
    else:
        print("\n  Unprofitable in both segments; the split adds nothing.")

    thin = [label for label, m in rows if m["total_trades"] < 20]
    if thin:
        print(f"    Sample warning: {', '.join(thin)} has under 20 trades. At that size")
        print("    a handful of outliers sets the result — this is a sanity check, not proof.")


def run_replay(symbols: list[str], period: str = "Min_5", bars: int = 300,
               cash: float = 10_000.0, warmup: int = 30, lock_profit: float = 0.8,
               stop_loss: float = 1.0, trailing: float = 0.0,
               min_confirmations: int = -1, use_atr_sizing: bool = True,
               split: float = 0.0, verbose: bool = False,
               stall_minutes: int = -1, breakeven_pct: float = -1.0,
               thesis_exit: bool | None = None) -> int:
    """Replay the engine over history. Returns a process exit code."""
    import app

    print(f"Fetching {period} history for {len(symbols)} symbols…")
    timeline = fetch_history(symbols, period, min(1000, bars + warmup))
    if not timeline:
        print("\nNo usable history — nothing to replay.")
        return 1

    length = min(len(v) for v in timeline.values())
    if length <= warmup:
        print(f"\nOnly {length} bars, need more than the {warmup}-bar warmup.")
        return 1

    # ── Isolate every state path. The real state/ directory is never written.
    tmp = Path(tempfile.mkdtemp(prefix="trading-replay-"))
    app.STATE_DIR = tmp
    app.STATE_FILE = tmp / "paper_state.json"
    app.TRADE_LOG = tmp / "trade_log.jsonl"
    app.AUDIT_LOG = tmp / "audit_log.jsonl"
    app.TRADES_CLOSED_LOG = tmp / "trades_closed.jsonl"
    app.SESSIONS_LOG = tmp / "sessions_log.jsonl"
    app.BACKTEST_LOG = tmp / "backtest_log.jsonl"

    state = app.STATE
    if min_confirmations < 0:                 # -1 = "use the app default"
        min_confirmations = Settings().normalized().min_confirmations
    replay_symbols = list(timeline.keys())
    broker = ReplayBroker(timeline, starting_cash=cash)

    # Fetching history connects to Longbridge, which flips LB_STATUS to
    # "connected" — and that is what arms the market-hours gate in tick().
    # Replaying outside a live session would then hit the "all selected markets
    # closed" branch and return before scanning anything at all. Replayed bars
    # are historical, so the gate must be off exactly as it is in sim mode.
    # This must happen AFTER the broker is built: its constructor reconnects.
    from broker import LB_STATUS
    LB_STATUS["connected"] = False
    LB_STATUS["error"] = "replay mode — market-hours gate disabled"

    state.paper_broker = broker
    # A custom universe short-circuits discovery, so no API calls per tick.
    state.settings = Settings(
        universe=replay_symbols, max_scan_symbols=0, budget=cash,
        trading_mode=TradingMode.PAPER, approval_mode=ApprovalMode.AUTO,
        strategy_enabled=True, max_trade_value=cash / 4,
        max_loss=cash * 0.10, duration_minutes=100_000,
        lock_profit_pct=lock_profit, stop_loss_pct=stop_loss,
        trailing_stop_pct=trailing, ai_strategy_name="replay",
        min_confirmations=min_confirmations, use_atr_sizing=use_atr_sizing,
        # Daily bars mean a multi-day horizon; match the engine's semantics.
        trading_horizon=("swing" if period.lower().startswith("day") else "intraday"),
    )
    # Slot-releasing exits, overridable so they can be A/B'd against a run
    # without them. A sentinel (-1 / None) means "leave the profile alone" —
    # a plain `python3 replay.py` must measure what the app actually ships,
    # not a variant the harness quietly forced on.
    if stall_minutes >= 0:
        state.settings.max_hold_minutes = stall_minutes
    if breakeven_pct >= 0:
        state.settings.breakeven_trigger_pct = breakeven_pct
    if thesis_exit is not None:
        state.settings.exit_on_thesis_break = thesis_exit
    state.settings = state.settings.normalized()
    state.proposals = []
    state.signals = []
    state.begin_session()
    # Momentum, not AI — see the module docstring.
    state.strategy = MomentumStrategy()

    engine = app.TradingEngine()
    # Candle re-fetch is throttled by WALL-CLOCK seconds, which would freeze
    # indicators at bar 1 in a loop that finishes in seconds.
    engine.CANDLE_REFRESH_OVERRIDE = 0.0

    ledger_before = 0
    print(f"\nReplaying {length - warmup} bars across {len(replay_symbols)} symbols "
          f"(warmup {warmup})…")
    for index in range(warmup, length):
        broker.set_index(index)
        # Advance the engine's clock to this bar, so daily budgets roll over
        # and loss cooldowns expire on simulated time rather than the few
        # seconds of wall clock this loop actually takes.
        stamp = next((b[index].get("timestamp") for b in timeline.values()
                      if index < len(b) and b[index].get("timestamp")), "")
        if stamp:
            try:
                moment = datetime.fromisoformat(stamp)
                app.TradingEngine.simulated_now = (
                    moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc))
            except ValueError:
                pass
        engine.tick()
        if verbose:
            closed = _read_ledger(app.TRADES_CLOSED_LOG)
            for record in closed[ledger_before:]:
                print(f"  bar {index:>4}  CLOSED {record['symbol']:<9} "
                      f"{record['exit_reason']:<14} net {record['net_pnl']:+.2f}")
            ledger_before = len(closed)

    # Flatten anything still open so every round trip completes and the
    # session_end path gets exercised too.
    #
    # simulated_now stays pinned to the LAST replayed bar for this. Clearing it
    # first stamped `closed_at` from the wall clock onto positions whose
    # `opened_at` is a bar timestamp, so every session_end round trip recorded a
    # nonsense hold time — negative whenever the replayed window ends later than
    # the moment the replay runs. It is cleared after the flatten instead.
    split_at = ""
    if 0 < split < 1:
        boundary = warmup + int((length - warmup) * split)
        split_at = next((b[boundary].get("timestamp") for b in timeline.values()
                         if boundary < len(b) and b[boundary].get("timestamp")), "")
    broker.set_index(length - 1)
    open_before = sum(1 for p in broker.portfolio().positions.values() if p.quantity > 0)
    if open_before:
        print(f"\nFlattening {open_before} open position(s) via stop-at-end…")
        engine._close_positions_at_end(broker.quotes(replay_symbols))
    # Every round trip is now recorded; the class attribute must not leak into
    # anything that runs after this in the same process.
    app.TradingEngine.simulated_now = None

    code = _report(app, tmp, broker, engine)
    if split_at:
        _split_report(app, _read_ledger(app.TRADES_CLOSED_LOG), split_at, split)
    return code


def _read_ledger(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _report(app, tmp: Path, broker, engine) -> int:
    """Validate what the replay produced. This is the actual output."""
    ledger = _read_ledger(app.TRADES_CLOSED_LOG)
    fills = _read_ledger(app.TRADE_LOG)
    portfolio = broker.portfolio()
    failures: list[str] = []

    def check(label: str, ok: bool, detail: str = "") -> None:
        print(f"  {'PASS' if ok else 'FAIL'}  {label}{('  — ' + detail) if detail else ''}")
        if not ok:
            failures.append(label)

    print("\n" + "─" * 64)
    print("Trade path")
    print("─" * 64)
    buys = sum(1 for f in fills if f["order"]["side"] == "buy" and f["order"]["status"] == "filled")
    sells = sum(1 for f in fills if f["order"]["side"] == "sell" and f["order"]["status"] == "filled")
    check("entries filled", buys > 0, f"{buys} buys")
    check("exits filled", sells > 0, f"{sells} sells")
    check("round trips recorded", len(ledger) > 0, f"{len(ledger)} closed trades")

    if not buys:
        print("\n  No entry ever filled, so the ledger path was never reached.")
        summary = engine.viability_summary()
        blocked = [m for m, v in summary["per_market"].items()
                   if v["assessable"] and not v["viable"]]
        if summary["enforced"] and blocked:
            # Not a signal problem — the trades were refused as unprofitable.
            print(f"  CAUSE: the viability guard blocked every buy. At "
                  f"${summary['trade_value']:,.0f}/trade with a "
                  f"{summary['target_pct']:.2f}% target:")
            for market in blocked:
                v = summary["per_market"][market]
                floor = v["min_viable_notional"]
                print(f"    {market}: costs {v['breakeven_pct']:.2f}% — a win nets "
                      f"{v['net_edge_pct']:+.2f}%"
                      + (f" (needs ~${floor:,.0f}/trade)" if floor > 0 else ""))
            print("  Re-run with a larger --cash, or a --lock-profit above those costs.")
        else:
            print("  This is a replay-setup result, not necessarily a bug: the signal")
            print("  engine needs score ≥0.55, a positive day change, RSI <75 and")
            print("  turnover ≥$500k. Try more bars, more symbols, or a wider period.")

    print("\n" + "─" * 64)
    print("Ledger integrity")
    print("─" * 64)
    if ledger:
        bad_math = [r for r in ledger
                    if abs(r["net_pnl"] - (r["gross_pnl"] - r["fees"])) > 1e-6]
        bad_reason = [r for r in ledger if r.get("exit_reason") not in VALID_EXIT_REASONS]
        bad_qty = [r for r in ledger if not r.get("quantity", 0) > 0]
        bad_price = [r for r in ledger
                     if not (r.get("entry_price", 0) > 0 and r.get("exit_price", 0) > 0)]
        bad_hold = [r for r in ledger if r.get("hold_seconds", -1) < 0]
        no_ctx = [r for r in ledger if not r.get("opened_at")]
        check("net_pnl == gross_pnl - fees", not bad_math, f"{len(bad_math)} bad")
        check("every exit_reason is tagged", not bad_reason,
              f"{sorted({r.get('exit_reason') for r in bad_reason})}" if bad_reason else "")
        check("quantity > 0", not bad_qty, f"{len(bad_qty)} bad")
        check("entry and exit prices set", not bad_price, f"{len(bad_price)} bad")
        check("hold_seconds >= 0", not bad_hold, f"{len(bad_hold)} bad")
        check("entry context captured", not no_ctx, f"{len(no_ctx)} missing opened_at")
        scored = [r for r in ledger if r.get("entry_score", 0) > 0]
        check("entry score captured", len(scored) == len(ledger),
              f"{len(scored)}/{len(ledger)} have a score")
    else:
        check("ledger integrity", False, "no closed trades to validate")

    # An orphan is a position that went flat without its record being written —
    # the exact failure this harness exists to catch.
    orphans = [s for s, p in portfolio.positions.items()
               if p.quantity <= 0 and p.entry_qty > 0]
    check("no orphaned round trips", not orphans, ", ".join(orphans) if orphans else "")
    still_open = [s for s, p in portfolio.positions.items() if p.quantity > 0]
    check("all positions flattened", not still_open, ", ".join(still_open) if still_open else "")

    print("\n" + "─" * 64)
    print("Metrics")
    print("─" * 64)
    report = app.metrics_report("all")
    metrics = report["metrics"]
    check("metrics agree with ledger", metrics["total_trades"] == len(ledger),
          f"{metrics['total_trades']} vs {len(ledger)}")
    if ledger:
        print(f"\n  expectancy/trade   {metrics['expectancy_per_trade']:+.2f}")
        print(f"  win rate           {metrics['win_rate'] * 100:.1f}%  "
              f"({metrics['wins']}W / {metrics['losses']}L)")
        print(f"  profit factor      "
              f"{'∞' if metrics['profit_factor_undefined'] else metrics['profit_factor']}")
        print(f"  net P&L            {metrics['net_pnl']:+.2f}")
        print(f"  fees               {metrics['total_fees']:.2f} "
              f"({metrics['fees_as_pct_of_gross']:.1f}% of gross)")
        print(f"  max drawdown       {metrics['max_drawdown_dollars']:.2f} "
              f"({metrics['max_drawdown_pct']:.1f}%)")
        print("\n  exit reasons:")
        for reason, group in sorted(metrics["by_exit_reason"].items(),
                                    key=lambda kv: -kv[1]["total_trades"]):
            print(f"    {reason:<15} {group['total_trades']:>3} trades  "
                  f"net {group['net_pnl']:+9.2f}  win rate {group['win_rate'] * 100:>5.1f}%")
        print("\n  P&L above is replayed history, not a forecast — judge the checks,")
        print("  not the returns.")

    print("\n" + "─" * 64)
    print(f"State written to: {tmp}")
    if failures:
        print(f"REPLAY FAILED — {len(failures)} check(s): {', '.join(failures)}")
        return 1
    print("REPLAY PASSED — the live trade-recording path works end to end.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Replay real historical candles through the real TradingEngine "
                    "to verify the trade-recording path.")
    parser.add_argument("--symbols", default=",".join(DEFAULT_SYMBOLS),
                        help="comma-separated, e.g. AAPL.US,700.HK")
    parser.add_argument("--period", default="Min_15",
                        help="Longbridge candle period (Min_1, Min_5, Min_15, Day…). "
                             "Min_5 skews toward thin overnight bars where entries "
                             "rarely trigger")
    parser.add_argument("--bars", type=int, default=400, help="bars to replay")
    parser.add_argument("--warmup", type=int, default=30,
                        help="bars fed in before trading starts (indicators need ~15)")
    parser.add_argument("--cash", type=float, default=10_000.0, help="starting cash")
    parser.add_argument("--lock-profit", type=float, default=0.8,
                        help="profit-lock %% (0 = off)")
    parser.add_argument("--stop-loss", type=float, default=1.0, help="stop-loss %% (0 = off)")
    parser.add_argument("--trailing", type=float, default=0.0,
                        help="trailing-stop %% (0 = off)")
    # Defaults to whatever the app ships, so a bare run reflects real
    # behaviour rather than a harness-only configuration.
    parser.add_argument("--min-confirmations", type=int,
                        default=Settings().normalized().min_confirmations,
                        help="convergence gate: independent factors that must agree "
                             "before a buy (0-5, 0 = off). Defaults to the app default")
    parser.add_argument("--split", type=float, default=0.0,
                        help="train/test split fraction, e.g. 0.6 — reports the first "
                             "60%% of the window separately from the rest")
    parser.add_argument("--flat-sizing", action="store_true",
                        help="disable ATR position sizing (A/B against the default)")
    parser.add_argument("--stall-minutes", type=int, default=-1,
                        help="per-position time stop in minutes (0 = off). "
                             "Defaults to the horizon profile")
    parser.add_argument("--breakeven-pct", type=float, default=-1.0,
                        help="gain that arms the breakeven stop (0 = off). "
                             "Defaults to the horizon profile")
    parser.add_argument("--thesis-exit", dest="thesis_exit", action="store_true",
                        default=None,
                        help="exit when the entry thesis breaks (ships OFF — it "
                             "measured badly; see models.THESIS_FACTORS)")
    parser.add_argument("--no-thesis-exit", dest="thesis_exit", action="store_false",
                        help="do not exit when the entry thesis breaks")
    parser.add_argument("--legacy-exits", action="store_true",
                        help="price-based exits ONLY — the pre-slot-exit baseline. "
                             "Use this to A/B whether the slot-releasing exits earn "
                             "the extra round trips they cause")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="print each round trip as it closes")
    args = parser.parse_args()

    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    return run_replay(symbols=symbols, period=args.period, bars=args.bars,
                      cash=args.cash, warmup=args.warmup,
                      lock_profit=args.lock_profit, stop_loss=args.stop_loss,
                      trailing=args.trailing,
                      min_confirmations=args.min_confirmations,
                      use_atr_sizing=not args.flat_sizing, split=args.split,
                      verbose=args.verbose,
                      stall_minutes=0 if args.legacy_exits else args.stall_minutes,
                      breakeven_pct=0.0 if args.legacy_exits else args.breakeven_pct,
                      thesis_exit=False if args.legacy_exits else args.thesis_exit)


if __name__ == "__main__":
    sys.exit(main())
