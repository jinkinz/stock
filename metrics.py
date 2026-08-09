"""
Performance metrics over closed round trips.

Pure functions only — no I/O, no global state, no imports from the rest of the
app. Input is a list of closed-trade dicts as written to
`state/trades_closed.jsonl`; output is a plain dict of numbers, safe to
serialise straight into an API response.

Expectancy is the headline number: the average dollars a trade is worth. If it
is negative the strategy loses money by construction and nothing else here
matters.

Every division is guarded — an empty or degenerate trade list returns the full
shape with zeros rather than raising or returning None, so callers never need
to special-case "no data yet".
"""
from __future__ import annotations

import statistics

# Below this many trades the numbers are noise, not signal. Callers must
# surface `sample_warning` rather than presenting the metrics as conclusive.
MIN_MEANINGFUL_SAMPLE = 20


def _safe_div(numerator: float, denominator: float, default: float = 0.0) -> float:
    return numerator / denominator if denominator else default


def _num(value, default: float = 0.0) -> float:
    """Tolerant float coercion — a malformed ledger line degrades one field
    rather than blowing up the whole endpoint."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def compute_metrics(trades: list[dict], starting_equity: float = 0.0) -> dict:
    """Full metric set for a list of closed trades, plus per-exit_reason and
    per-strategy breakdowns.

    `starting_equity` is only used for the drawdown percentage — see
    `_max_drawdown`. Pass it whenever it is known; the numbers are far more
    meaningful with it.
    """
    result = _core_metrics(trades, starting_equity)
    result["by_exit_reason"] = _grouped(trades, "exit_reason", "unknown", starting_equity)
    result["by_strategy"] = _grouped(trades, "strategy", "unknown", starting_equity)
    # The comparison that answers "which setup actually works" — every trade
    # carries the configuration it was opened under.
    result["by_config"] = _grouped(trades, "config_key", "unknown", starting_equity)
    return result


def _grouped(trades: list[dict], key: str, fallback: str, starting_equity: float) -> dict:
    groups: dict[str, list[dict]] = {}
    for trade in trades:
        groups.setdefault(str(trade.get(key) or fallback), []).append(trade)
    return {name: _core_metrics(items, starting_equity) for name, items in sorted(groups.items())}


def _core_metrics(trades: list[dict], starting_equity: float = 0.0) -> dict:
    total = len(trades)
    net_pnls = [_num(t.get("net_pnl")) for t in trades]
    fees = [_num(t.get("fees")) for t in trades]
    gross_pnls = [_num(t.get("gross_pnl")) for t in trades]
    holds = [_num(t.get("hold_seconds")) for t in trades]

    wins = [p for p in net_pnls if p > 0]
    losses = [p for p in net_pnls if p < 0]
    # Scratches (exactly 0.0) count toward total_trades but toward neither
    # side, so win_rate + loss_rate need not sum to 1.
    win_rate = _safe_div(len(wins), total)
    loss_rate = _safe_div(len(losses), total)
    avg_win = _safe_div(sum(wins), len(wins))
    avg_loss = _safe_div(sum(losses), len(losses))     # negative or 0.0

    gross_wins = sum(wins)
    gross_losses = abs(sum(losses))
    # No losses at all: profit_factor is mathematically infinite. Report 0.0
    # when there is nothing at all, and the raw win total otherwise, with a
    # flag rather than a fake "inf" the UI would have to special-case.
    profit_factor = _safe_div(gross_wins, gross_losses)

    total_fees = sum(fees)
    total_gross = sum(gross_pnls)
    total_net = sum(net_pnls)

    max_dd_dollars, max_dd_pct = _max_drawdown(net_pnls, starting_equity)

    return {
        "total_trades": total,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(win_rate, 4),
        "avg_win": round(avg_win, 4),
        "avg_loss": round(avg_loss, 4),
        "largest_win": round(max(wins), 4) if wins else 0.0,
        "largest_loss": round(min(losses), 4) if losses else 0.0,
        "profit_factor": round(profit_factor, 4),
        "profit_factor_undefined": bool(wins) and not losses,
        "expectancy_per_trade": round(win_rate * avg_win - loss_rate * abs(avg_loss), 4),
        "gross_pnl": round(total_gross, 4),
        "net_pnl": round(total_net, 4),
        "total_fees": round(total_fees, 4),
        # Against gross MAGNITUDE, not signed gross: a near-zero signed gross
        # would otherwise produce a meaningless ratio in the thousands.
        "fees_as_pct_of_gross": round(
            _safe_div(total_fees, sum(abs(g) for g in gross_pnls)) * 100, 4
        ),
        "max_drawdown_dollars": round(max_dd_dollars, 4),
        "max_drawdown_pct": round(max_dd_pct, 4),
        "avg_hold_seconds": round(_safe_div(sum(holds), len(holds)), 2),
        "median_hold_seconds": round(statistics.median(holds), 2) if holds else 0.0,
        "sample_warning": total < MIN_MEANINGFUL_SAMPLE,
    }


def _max_drawdown(net_pnls: list[float], starting_equity: float = 0.0) -> tuple[float, float]:
    """Largest peak-to-trough decline, in dollars and percent.

    With `starting_equity` known, this is drawdown in the conventional sense:
    the decline measured against the running peak of the EQUITY curve
    (`starting_equity + cumulative P&L`), which is bounded by 100%.

    Without it, there is no equity basis, so the percentage falls back to the
    decline against peak cumulative *profit*. That figure is unbounded and can
    read above 100% — giving back $800 against a peak profit of $700 is
    "114%" — so callers that can supply the equity should always do so.
    """
    cumulative = 0.0
    peak = starting_equity          # 0.0 in the no-basis fallback
    max_dd = 0.0
    max_dd_pct = 0.0
    for pnl in net_pnls:
        cumulative += pnl
        equity = starting_equity + cumulative
        peak = max(peak, equity)
        drawdown = peak - equity
        if drawdown > max_dd:
            max_dd = drawdown
        if peak > 0:
            max_dd_pct = max(max_dd_pct, drawdown / peak * 100)
    return max_dd, max_dd_pct


def equal_weight_return(per_symbol_returns: dict[str, float]) -> float:
    """Equal-weight buy-and-hold return (%) across symbols. Empty input → 0.0."""
    if not per_symbol_returns:
        return 0.0
    return round(sum(per_symbol_returns.values()) / len(per_symbol_returns), 4)
