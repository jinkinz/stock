"""Boot smoke tests.

A unit suite can be entirely green while the app cannot start: nothing was
exercising `_startup_banner`, so widening the `CANDLE_SPEC` tuples from 3 to 4
elements passed 262 tests and then crashed on launch with
`ValueError: too many values to unpack`.

These tests cover the paths that only run at startup, plus the shape contracts
that several call sites unpack positionally.

Run from the repo root:  python3 -m unittest discover tests
"""
from __future__ import annotations

import io
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app  # noqa: E402
from models import Settings  # noqa: E402


def setUpModule() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="trading-smoke-"))
    app.STATE_DIR = tmp
    app.STATE_FILE = tmp / "paper_state.json"
    app.AUDIT_LOG = tmp / "audit_log.jsonl"
    app.TRADES_CLOSED_LOG = tmp / "trades_closed.jsonl"


class StartupBannerTest(unittest.TestCase):
    """The exact regression: the banner unpacked CANDLE_SPEC positionally."""

    def banner(self, horizon: str) -> str:
        saved = app.STATE.settings
        try:
            app.STATE.settings = Settings(trading_horizon=horizon).normalized()
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                app._startup_banner()
            return buffer.getvalue()
        finally:
            app.STATE.settings = saved

    def test_banner_runs_on_intraday(self):
        self.assertIn("Horizon", self.banner("intraday"))

    def test_banner_runs_on_swing(self):
        self.assertIn("SWING", self.banner("swing"))

    def test_banner_reports_viability(self):
        self.assertIn("Viability", self.banner("intraday"))

    def test_banner_reports_fee_verification_state(self):
        self.assertIn("fees", self.banner("intraday").lower())


class SpecShapeTest(unittest.TestCase):
    """Several call sites unpack these positionally, so the arity is a contract."""

    def test_candle_spec_entries_have_four_elements(self):
        for horizon, spec in app.TradingEngine.CANDLE_SPEC.items():
            self.assertEqual(len(spec), 4, f"{horizon} spec changed shape")

    def test_candle_spec_field_types(self):
        for horizon, (period, count, refresh, budget) in app.TradingEngine.CANDLE_SPEC.items():
            self.assertIsInstance(period, str, horizon)
            self.assertIsInstance(count, int, horizon)
            self.assertIsInstance(refresh, float, horizon)
            self.assertIsInstance(budget, int, horizon)

    def test_every_horizon_has_a_spec(self):
        from models import HORIZON_DEFAULTS
        for horizon in HORIZON_DEFAULTS:
            self.assertIn(horizon, app.TradingEngine.CANDLE_SPEC)


class StatusPayloadTest(unittest.TestCase):
    """`/api/status` is built on every SSE frame; a KeyError there kills the UI."""

    def test_status_builds_for_both_horizons(self):
        saved = app.STATE.settings
        try:
            for horizon in ("intraday", "swing"):
                app.STATE.settings = Settings(trading_horizon=horizon).normalized()
                payload = app.ENGINE.status()
                for key in ("settings", "portfolio", "viability", "coverage", "premarket"):
                    self.assertIn(key, payload, f"{horizon}: missing {key}")
        finally:
            app.STATE.settings = saved

    def test_status_is_json_serialisable(self):
        import json
        json.dumps(app.ENGINE.status())


if __name__ == "__main__":
    unittest.main()
