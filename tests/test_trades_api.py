"""`/api/trades` — individual closed round trips.

Aggregates answer "is this working"; this answers "which trades, and why did
each one end". The ledger is append-only and unbounded, so the bounds matter as
much as the content.

Run from the repo root:  python3 -m unittest discover tests
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app  # noqa: E402


def setUpModule() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="trading-tradesapi-"))
    app.STATE_DIR = tmp
    app.STATE_FILE = tmp / "paper_state.json"
    app.TRADES_CLOSED_LOG = tmp / "trades_closed.jsonl"
    app.AUDIT_LOG = tmp / "audit_log.jsonl"


def write_ledger(records: list[dict]) -> None:
    with app.TRADES_CLOSED_LOG.open("w") as handle:
        for record in records:
            handle.write(json.dumps(record) + "\n")


def trade(hours_ago: float, symbol: str = "AAPL.US", net: float = 10.0,
          reason: str = "profit_lock") -> dict:
    closed = datetime.now(timezone.utc) - timedelta(hours=hours_ago)
    return {
        "symbol": symbol, "closed_at": closed.isoformat(),
        "opened_at": (closed - timedelta(minutes=30)).isoformat(),
        "hold_seconds": 1800, "entry_price": 100.0, "exit_price": 100.0 + net,
        "quantity": 1.0, "gross_pnl": net + 1, "fees": 1.0, "net_pnl": net,
        "return_pct": net, "exit_reason": reason, "strategy": "fifo",
        "mode": "paper", "entry_score": 0.7, "entry_diagnostics": {},
        "fees_source": "modelled",
    }


class ClosedTradesReportTest(unittest.TestCase):
    def setUp(self):
        self._session = app.STATE.session_start_at
        app.STATE.session_start_at = None

    def tearDown(self):
        app.STATE.session_start_at = self._session

    def test_empty_ledger_returns_a_valid_shape(self):
        write_ledger([])
        report = app.closed_trades_report("all", "50")
        self.assertEqual(report["trades"], [])
        self.assertEqual(report["total_in_window"], 0)
        self.assertEqual(report["returned"], 0)

    def test_newest_first(self):
        write_ledger([trade(5, "OLD.US"), trade(1, "NEW.US")])
        trades = app.closed_trades_report("all", "50")["trades"]
        self.assertEqual([t["symbol"] for t in trades], ["NEW.US", "OLD.US"])

    def test_limit_is_respected(self):
        write_ledger([trade(i) for i in range(20)])
        report = app.closed_trades_report("all", "5")
        self.assertEqual(len(report["trades"]), 5)
        self.assertEqual(report["total_in_window"], 20, "count reflects the window, not the page")

    def test_limit_is_clamped_to_the_ceiling(self):
        write_ledger([trade(i * 0.01) for i in range(50)])
        report = app.closed_trades_report("all", "100000")
        self.assertLessEqual(report["returned"], app.MAX_TRADES_RESPONSE)

    def test_garbage_limit_falls_back(self):
        write_ledger([trade(1)])
        self.assertEqual(app.closed_trades_report("all", "not-a-number")["returned"], 1)

    def test_negative_limit_still_returns_something(self):
        write_ledger([trade(1)])
        self.assertGreaterEqual(app.closed_trades_report("all", "-5")["returned"], 1)

    def test_unknown_window_falls_back_to_all(self):
        write_ledger([trade(1)])
        self.assertEqual(app.closed_trades_report("nonsense", "5")["window"], "all")

    def test_day_window_excludes_older_trades(self):
        write_ledger([trade(50, "OLD.US"), trade(2, "RECENT.US")])
        symbols = [t["symbol"] for t in app.closed_trades_report("day", "50")["trades"]]
        self.assertEqual(symbols, ["RECENT.US"])

    def test_malformed_lines_are_skipped_not_fatal(self):
        with app.TRADES_CLOSED_LOG.open("w") as handle:
            handle.write("{not json}\n")
            handle.write(json.dumps(trade(1, "GOOD.US")) + "\n")
        trades = app.closed_trades_report("all", "50")["trades"]
        self.assertEqual([t["symbol"] for t in trades], ["GOOD.US"])

    def test_exit_reason_survives_to_the_response(self):
        write_ledger([trade(1, reason="trailing_stop")])
        self.assertEqual(app.closed_trades_report("all", "1")["trades"][0]["exit_reason"],
                         "trailing_stop")

    def test_metrics_and_trades_agree_on_the_same_window(self):
        write_ledger([trade(2, net=5.0), trade(3, net=-2.0), trade(60, net=99.0)])
        trades = app.closed_trades_report("day", "50")
        metrics = app.metrics_report("day")
        self.assertEqual(trades["total_in_window"], metrics["metrics"]["total_trades"],
                         "the two views of one ledger must not disagree")


if __name__ == "__main__":
    unittest.main()
