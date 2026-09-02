"""Exits that free a slot on something other than price.

Before these, the only things that could release an intraday slot were the
profit target, the stop, and the closing bell. A position drifting at +0.3%
therefore held capital for the whole session while stronger setups were refused
for want of a slot — 52 blocked buys in one session, every one of them the
concentration cap.

Four rules answer that, and they are NOT free: three of them end a trade
early, the freed slot gets refilled, and the replacement pays a full round
trip. What the tests below pin is that each one fires on the condition it
claims and no other — an exit that fires on chop would burn fees for nothing.

Run from the repo root:  python3 -m unittest discover tests
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app  # noqa: E402
from models import (  # noqa: E402
    ApprovalMode, HORIZON_DEFAULTS, Position, Quote, Settings, Signal,
    THESIS_FACTORS, TradingMode,
)


def setUpModule() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="trading-slots-"))
    for attr, name in (("STATE_DIR", ""), ("STATE_FILE", "paper_state.json"),
                       ("AUDIT_LOG", "audit_log.jsonl"),
                       ("TRADES_CLOSED_LOG", "trades_closed.jsonl"),
                       ("TRADE_LOG", "trade_log.jsonl")):
        setattr(app, attr, tmp / name if name else tmp)


class ExitHarness(unittest.TestCase):
    """Shared fixture: one tool-owned position, every exit off unless enabled."""

    settings_kwargs: dict = {}

    def setUp(self):
        from broker import PaperBroker
        self._saved = (app.STATE.settings, app.STATE.paper_broker, app.STATE.signals)
        base = dict(trading_horizon="intraday", lock_profit_pct=0.0, stop_loss_pct=0.0,
                    trailing_stop_pct=0.0, breakeven_trigger_pct=0.0,
                    max_hold_minutes=0, max_hold_days=0, exit_on_thesis_break=False,
                    trading_mode=TradingMode.PAPER, approval_mode=ApprovalMode.AUTO)
        base.update(self.settings_kwargs)
        app.STATE.settings = Settings(**base).normalized()
        broker = PaperBroker(starting_cash=100_000.0)
        broker._lb = None
        app.STATE.paper_broker = broker
        app.STATE.signals = []
        self.broker = broker
        self.engine = app.TradingEngine()

    def tearDown(self):
        (app.STATE.settings, app.STATE.paper_broker, app.STATE.signals) = self._saved
        app.TradingEngine.simulated_now = None

    def position(self, *, price=101.0, minutes_held=1.0, entry=100.0,
                 confirmations=("trend", "vwap"), armed=False, symbol="AAPL.US"):
        opened = datetime.now(timezone.utc) - timedelta(minutes=minutes_held)
        self.broker._portfolio.positions[symbol] = Position(
            symbol=symbol, quantity=10, avg_cost=entry, entry_qty=10,
            entry_price=entry, opened_at=opened.isoformat(),
            entry_confirmations=list(confirmations), breakeven_armed=armed)
        return [Quote(symbol=symbol, price=price, timestamp="", source="longbridge")]

    def signal(self, symbol="AAPL.US", *, confirmations=(), score=0.6, action="watch"):
        app.STATE.signals = [Signal(symbol=symbol, price=100.0, score=score,
                                    action=action, reason="",
                                    confirmations=list(confirmations))]

    def exits(self, quotes):
        return self.engine._check_mechanical_exits(quotes)

    def tags(self, quotes):
        return [p.tag for p in self.exits(quotes)]


class StallExitTest(ExitHarness):
    """The intraday per-position clock — capital has a time cost."""

    settings_kwargs = {"max_hold_minutes": 120}

    def test_a_fresh_position_is_left_alone(self):
        self.assertEqual(self.exits(self.position(minutes_held=10)), [])

    def test_a_position_past_the_limit_is_closed(self):
        self.assertEqual(self.tags(self.position(minutes_held=121)), ["stall"])

    def test_exactly_at_the_limit_closes(self):
        self.assertEqual(self.tags(self.position(minutes_held=120)), ["stall"])

    def test_it_closes_winners_too(self):
        # Time, not P&L. A position up 0.5% after two hours has not reached
        # its target and is still occupying the slot.
        self.assertEqual(self.tags(self.position(minutes_held=200, price=100.5)),
                         ["stall"])

    def test_zero_disables_it(self):
        app.STATE.settings.max_hold_minutes = 0
        self.assertEqual(self.exits(self.position(minutes_held=9999)), [])

    def test_it_keeps_the_exit_loop_alive_on_its_own(self):
        # Every percentage setting is 0; only the clock is configured. The
        # early-return guard must not short-circuit past it.
        s = app.STATE.settings
        self.assertEqual((s.lock_profit_pct, s.stop_loss_pct, s.trailing_stop_pct), (0.0, 0.0, 0.0))
        self.assertEqual(self.tags(self.position(minutes_held=121)), ["stall"])

    def test_it_uses_the_engine_clock_so_replays_measure_simulated_time(self):
        quotes = self.position(minutes_held=1)
        app.TradingEngine.simulated_now = datetime.now(timezone.utc) + timedelta(hours=9)
        self.assertEqual(self.tags(quotes), ["stall"])

    def test_a_position_with_no_open_time_is_not_closed_on_a_guess(self):
        quotes = self.position(minutes_held=5)
        self.broker._portfolio.positions["AAPL.US"].opened_at = ""
        self.assertEqual(self.exits(quotes), [])


class StallExitIsIntradayOnlyTest(ExitHarness):
    """A minutes clock must never reach a multi-day thesis.

    Swing's profile sets max_hold_minutes to 0, but a Settings built directly
    — a test, an API payload, a state file predating the field — inherits the
    intraday dataclass default. The horizon is the fact; the value is only a
    setting, so the guard is on the horizon.
    """

    settings_kwargs = {"trading_horizon": "swing", "max_hold_minutes": 120,
                       "max_hold_days": 10}

    def test_swing_ignores_the_minute_clock_even_when_set(self):
        self.assertEqual(self.exits(self.position(minutes_held=5000)), [])

    def test_swing_still_honours_its_own_day_clock(self):
        quotes = self.position(minutes_held=11 * 24 * 60)
        self.assertEqual(self.tags(quotes), ["max_hold"])

    def test_the_swing_profile_zeroes_it_anyway(self):
        self.assertEqual(HORIZON_DEFAULTS["swing"]["max_hold_minutes"], 0)


class BreakevenTest(ExitHarness):
    """Once a trade has proved itself, stop letting it become a loser."""

    settings_kwargs = {"breakeven_trigger_pct": 1.0, "lock_profit_pct": 3.0}

    def test_it_arms_once_the_trigger_is_reached(self):
        self.exits(self.position(price=101.0))
        self.assertTrue(self.broker._portfolio.positions["AAPL.US"].breakeven_armed)

    def test_it_does_not_arm_below_the_trigger(self):
        self.exits(self.position(price=100.5))
        self.assertFalse(self.broker._portfolio.positions["AAPL.US"].breakeven_armed)

    def test_arming_alone_does_not_exit(self):
        # Reaching the trigger is not a sell signal; it is a promise about
        # what happens if the gain is given back.
        self.assertEqual(self.exits(self.position(price=101.0)), [])

    def test_it_exits_when_a_proven_winner_returns_to_entry(self):
        self.assertEqual(self.tags(self.position(price=100.0, armed=True)), ["breakeven"])

    def test_it_exits_below_entry_too(self):
        self.assertEqual(self.tags(self.position(price=99.5, armed=True)), ["breakeven"])

    def test_an_unarmed_position_at_entry_is_left_alone(self):
        self.assertEqual(self.exits(self.position(price=100.0, armed=False)), [])

    def test_arming_latches_across_a_retrace(self):
        # Re-deriving from the CURRENT gain would hand the protection back at
        # exactly the moment it is needed.
        quotes = self.position(price=101.5)
        self.exits(quotes)
        self.assertEqual(self.tags([Quote(symbol="AAPL.US", price=99.9,
                                          timestamp="", source="longbridge")]),
                         ["breakeven"])

    def test_a_real_stop_out_is_still_reported_as_a_stop(self):
        # Ranking matters: a breakeven exit must not mask a genuine stop.
        app.STATE.settings.stop_loss_pct = 1.0
        self.assertEqual(self.tags(self.position(price=98.0, armed=True)), ["stop_loss"])

    def test_zero_disables_it(self):
        app.STATE.settings.breakeven_trigger_pct = 0.0
        quotes = self.position(price=101.0)
        self.exits(quotes)
        self.assertFalse(self.broker._portfolio.positions["AAPL.US"].breakeven_armed)


class BreakevenCannotBeInertTest(unittest.TestCase):
    """A trigger at or above the target could never fire — the profit lock
    would close the trade first — leaving a control in the UI that looks like
    protection while being incapable of acting."""

    def test_a_trigger_above_the_target_is_pulled_below_it(self):
        s = Settings(lock_profit_pct=2.0, breakeven_trigger_pct=5.0).normalized()
        self.assertLess(s.breakeven_trigger_pct, s.lock_profit_pct)
        self.assertEqual(s.breakeven_trigger_pct, 1.0)

    def test_equal_to_the_target_is_also_corrected(self):
        s = Settings(lock_profit_pct=2.0, breakeven_trigger_pct=2.0).normalized()
        self.assertEqual(s.breakeven_trigger_pct, 1.0)

    def test_a_sensible_trigger_is_left_alone(self):
        s = Settings(lock_profit_pct=2.0, breakeven_trigger_pct=0.7).normalized()
        self.assertEqual(s.breakeven_trigger_pct, 0.7)

    def test_no_target_means_nothing_to_clamp_against(self):
        s = Settings(lock_profit_pct=0.0, breakeven_trigger_pct=5.0).normalized()
        self.assertEqual(s.breakeven_trigger_pct, 5.0)

    def test_shipped_defaults_are_internally_consistent(self):
        for horizon, defaults in HORIZON_DEFAULTS.items():
            self.assertLess(defaults["breakeven_trigger_pct"], defaults["lock_profit_pct"],
                            f"{horizon}: breakeven could never arm")


class ThesisBreakTest(ExitHarness):
    """Exit on the reason, not the price."""

    settings_kwargs = {"exit_on_thesis_break": True}

    def test_an_intact_thesis_holds(self):
        quotes = self.position(confirmations=("trend", "vwap"))
        self.signal(confirmations=("trend", "vwap"))
        self.assertEqual(self.exits(quotes), [])

    def test_losing_a_structural_factor_exits(self):
        quotes = self.position(confirmations=("trend", "vwap"))
        self.signal(confirmations=("trend",))       # vwap lost
        proposals = self.exits(quotes)
        self.assertEqual([p.tag for p in proposals], ["thesis_break"])
        self.assertIn("vwap", proposals[0].reason)

    def test_losing_a_noisy_factor_does_not_exit(self):
        # momentum/volume/structure flip on chop inside an intact uptrend, and
        # every firing would cost a full round trip.
        quotes = self.position(confirmations=("trend", "vwap", "momentum", "volume"))
        self.signal(confirmations=("trend", "vwap"))
        self.assertEqual(self.exits(quotes), [])

    def test_only_structural_factors_count_as_the_thesis(self):
        self.assertEqual(set(THESIS_FACTORS), {"trend", "vwap"})

    def test_a_position_with_no_recorded_thesis_is_never_exited(self):
        # Positions opened before this existed must not be closed on an
        # invented thesis.
        quotes = self.position(confirmations=())
        self.signal(confirmations=())
        self.assertEqual(self.exits(quotes), [])

    def test_a_missing_signal_is_not_treated_as_a_broken_thesis(self):
        # No scan data for the symbol this tick means UNKNOWN, not broken —
        # absence of evidence must never become evidence.
        quotes = self.position(confirmations=("trend", "vwap"))
        app.STATE.signals = []
        self.assertEqual(self.exits(quotes), [])

    def test_a_thesis_of_only_noisy_factors_is_never_actionable(self):
        quotes = self.position(confirmations=("momentum", "volume"))
        self.signal(confirmations=())
        self.assertEqual(self.exits(quotes), [])

    def test_the_flag_disables_it(self):
        app.STATE.settings.exit_on_thesis_break = False
        quotes = self.position(confirmations=("trend", "vwap"))
        self.signal(confirmations=())
        self.assertEqual(self.exits(quotes), [])

    def test_price_exits_outrank_it(self):
        app.STATE.settings.stop_loss_pct = 1.0
        quotes = self.position(price=98.0, confirmations=("trend", "vwap"))
        self.signal(confirmations=())
        self.assertEqual(self.tags(quotes), ["stop_loss"])


class ThesisIsCapturedOnEveryScanTest(unittest.TestCase):
    """The thesis has to be readable when a symbol has STOPPED qualifying —
    which is exactly when a held position needs re-testing. Computing
    confirmations only on the buy branch would blind the exit."""

    def scan(self, price, prev_close):
        from models import Portfolio
        from strategy import MomentumStrategy
        quote = Quote(symbol="AAPL.US", price=price, timestamp="", source="longbridge",
                      prev_close=prev_close, open=prev_close, high=price * 1.01,
                      low=prev_close * 0.99, volume=1e6, turnover=5e7)
        return MomentumStrategy().scan_signals_only(
            Settings(min_confirmations=3).normalized(), [quote], Portfolio(cash=10_000.0))

    def test_a_watch_signal_still_reports_its_confirmations(self):
        signals = self.scan(price=95.0, prev_close=100.0)      # falling → watch
        self.assertTrue(signals)
        self.assertEqual(signals[0].action, "watch")
        self.assertIsInstance(signals[0].confirmations, list)

    def test_confirmations_are_a_subset_of_the_known_names(self):
        from strategy import MomentumStrategy
        for price, prev in ((105.0, 100.0), (95.0, 100.0)):
            for signal in self.scan(price, prev):
                self.assertLessEqual(set(signal.confirmations),
                                     set(MomentumStrategy.CONFIRMATION_NAMES))


class RotationTest(ExitHarness):
    """A holding re-justifying its slot against the alternatives."""

    settings_kwargs = {"allow_rotation": True, "rotation_score_gap": 0.15,
                       "max_concurrent_positions": 1}

    def challenger(self, score, held_score, *, held_price=99.0):
        quotes = self.position(price=held_price)
        app.STATE.signals = [
            Signal(symbol="AAPL.US", price=held_price, score=held_score,
                   action="watch", reason=""),
            Signal(symbol="DDOG.US", price=50.0, score=score, action="buy", reason=""),
        ]
        return quotes

    def test_a_much_stronger_signal_frees_the_slot(self):
        proposals = self.engine._check_rotation(self.challenger(0.90, 0.40))
        self.assertEqual([p.tag for p in proposals], ["rotation"])
        self.assertEqual(proposals[0].symbol, "AAPL.US")

    def test_a_marginal_improvement_does_not(self):
        # The gap must clear the round-trip cost of acting on it.
        self.assertEqual(self.engine._check_rotation(self.challenger(0.60, 0.55)), [])

    def test_it_is_off_by_default(self):
        self.assertFalse(Settings().normalized().allow_rotation)

    def test_the_flag_disables_it(self):
        app.STATE.settings.allow_rotation = False
        self.assertEqual(self.engine._check_rotation(self.challenger(0.95, 0.10)), [])

    def test_a_winning_position_is_never_displaced(self):
        # Rotation recycles dead capital; it does not cut winners short to
        # chase a signal, which would pay a round trip to abandon one.
        self.assertEqual(
            self.engine._check_rotation(self.challenger(0.95, 0.10, held_price=105.0)), [])

    def test_a_breakeven_armed_position_is_never_displaced(self):
        quotes = self.challenger(0.95, 0.10)
        self.broker._portfolio.positions["AAPL.US"].breakeven_armed = True
        self.assertEqual(self.engine._check_rotation(quotes), [])

    def test_it_does_nothing_while_a_slot_is_free(self):
        # With room to spare the challenger can simply be bought; selling
        # something first would be a round trip for nothing.
        app.STATE.settings.max_concurrent_positions = 5
        self.assertEqual(self.engine._check_rotation(self.challenger(0.95, 0.10)), [])

    def test_it_needs_an_actual_challenger(self):
        quotes = self.position(price=99.0)
        app.STATE.signals = [Signal(symbol="AAPL.US", price=99.0, score=0.1,
                                    action="watch", reason="")]
        self.assertEqual(self.engine._check_rotation(quotes), [])

    def test_a_symbol_already_held_is_not_its_own_challenger(self):
        quotes = self.position(price=99.0)
        app.STATE.signals = [Signal(symbol="AAPL.US", price=99.0, score=0.95,
                                    action="buy", reason="")]
        self.assertEqual(self.engine._check_rotation(quotes), [])

    def test_it_sells_only_and_lets_the_next_scan_choose(self):
        # Hard-wiring the buy to the challenger would bypass the ranking pass
        # that every other entry goes through.
        proposals = self.engine._check_rotation(self.challenger(0.90, 0.40))
        self.assertTrue(all(p.side.value == "sell" for p in proposals))


class FingerprintTest(unittest.TestCase):
    """Each new exit changes WHERE a trade closes, so trades taken with and
    without them must not pool in the same performance bucket."""

    def fingerprint(self, **kwargs):
        return Settings(**kwargs).normalized().config_fingerprint()

    def test_the_stall_clock_is_recorded(self):
        self.assertNotEqual(self.fingerprint(max_hold_minutes=120),
                            self.fingerprint(max_hold_minutes=30))

    def test_the_breakeven_trigger_is_recorded(self):
        self.assertNotEqual(self.fingerprint(breakeven_trigger_pct=0.4),
                            self.fingerprint(breakeven_trigger_pct=0.0))

    def test_the_thesis_exit_is_recorded(self):
        self.assertNotEqual(self.fingerprint(exit_on_thesis_break=True),
                            self.fingerprint(exit_on_thesis_break=False))

    def test_rotation_is_recorded(self):
        self.assertNotEqual(self.fingerprint(allow_rotation=True),
                            self.fingerprint(allow_rotation=False))

    def test_the_gap_only_matters_when_rotation_is_on(self):
        # An inert setting must not fragment the comparison buckets.
        self.assertEqual(self.fingerprint(allow_rotation=False, rotation_score_gap=0.1),
                         self.fingerprint(allow_rotation=False, rotation_score_gap=0.9))


class ShippedDefaultsTest(unittest.TestCase):
    """What ships ON, and why — pinned so it cannot drift back silently.

    Three of these four rules were MEASURED at a loss against the price-only
    baseline over 60 replayed round trips:

        baseline (price exits only)   exp -0.45   win 51.7%   net  -26.82
        + breakeven 0.4%              exp -0.81   win 43.3%   net  -48.47
        + stall 120min                exp -1.10   win 30.2%   net  -69.17
        + thesis break                exp -4.88   win 16.0%   net -365.77

    They stay implemented and configurable because the reasoning behind them
    is sound and the measurement is one window — but they do not ship on.
    """

    def setUp(self):
        self.fresh = Settings().normalized()

    def test_breakeven_ships_off(self):
        self.assertEqual(self.fresh.breakeven_trigger_pct, 0.0)

    def test_thesis_exit_ships_off(self):
        self.assertFalse(self.fresh.exit_on_thesis_break)

    def test_rotation_ships_off(self):
        self.assertFalse(self.fresh.allow_rotation)

    def test_the_stall_clock_ships_on_but_long(self):
        # The one exception. It is the only rule that answers "how long will
        # this hold my slot?" with a bounded number, and at 240 it is inert in
        # a faithful one-session replay (identical 16 trades with it on or
        # off) while still capping the pathological case.
        self.assertEqual(self.fresh.max_hold_minutes, 240)

    def test_the_stall_is_long_enough_to_let_winners_run(self):
        # 120 minutes cut 74% of winning trades. The threshold must sit well
        # past the point where a trade is merely slow.
        self.assertGreaterEqual(self.fresh.max_hold_minutes, 240)

    def test_it_cannot_fire_before_most_of_the_session_is_gone(self):
        # A time stop that fires early is just a worse stop loss.
        self.assertGreater(self.fresh.max_hold_minutes,
                           self.fresh.duration_minutes * 0.5)


class MaxPositionsHasNoSentinelTest(unittest.TestCase):
    """`0` used to mean two contradictory things at once.

    risk.check_limits read `cap > 0` as "no cap at all", while
    sizing.cash_fraction_for read `<= 0` as "assume four positions" and funded
    each at 25% of cash. Typing 0 to mean "let the engine decide" therefore
    removed the concentration limit AND shrank every position to the size that
    fails the viability floor on a small account — the two halves of the
    setting pulling in opposite directions.
    """

    def test_zero_falls_back_to_the_default_not_to_unlimited(self):
        self.assertEqual(Settings(max_concurrent_positions=0).normalized()
                         .max_concurrent_positions, 5)

    def test_negative_does_too(self):
        self.assertEqual(Settings(max_concurrent_positions=-3).normalized()
                         .max_concurrent_positions, 5)

    def test_a_real_value_is_left_alone(self):
        self.assertEqual(Settings(max_concurrent_positions=2).normalized()
                         .max_concurrent_positions, 2)

    def test_absurd_values_are_capped(self):
        self.assertEqual(Settings(max_concurrent_positions=500).normalized()
                         .max_concurrent_positions, 20)

    def test_the_cap_can_never_be_switched_off(self):
        # A concentration limit that can be disabled is protection that does
        # not exist — the same reasoning that removed max_positions_per_sector.
        for value in (0, -1, -999):
            self.assertGreaterEqual(
                Settings(max_concurrent_positions=value).normalized()
                .max_concurrent_positions, 1)

    def test_sizing_and_the_risk_cap_now_agree(self):
        # The bug was the two layers disagreeing about what the number meant.
        from sizing import cash_fraction_for
        settings = Settings(max_concurrent_positions=0).normalized()
        self.assertEqual(cash_fraction_for(settings.max_concurrent_positions),
                         cash_fraction_for(5))

    def test_holding_fewer_funds_each_position_more(self):
        # The lever the banner now names explicitly.
        from sizing import cash_fraction_for
        self.assertGreater(cash_fraction_for(2), cash_fraction_for(5))


class EqualWeightSlotsTest(unittest.TestCase):
    """"5 positions" must mean five equal ones, not five shrinking ones.

    The cash cap used to be a share of REMAINING cash, so the same percentage
    was re-applied to a pool that each purchase made smaller. On $1,000 over 5
    slots that produced $250, $187, $141, $105, $79: only the first cleared its
    own round-trip cost, every later one was unprofitable purely because it was
    opened later, and $237 was never deployed at all.
    """

    def slot_sizes(self, budget, n):
        """Sizes the engine would authorise, filling slots one at a time."""
        from sizing import cash_fraction_for
        fraction = cash_fraction_for(n)
        cash, sizes = budget, []
        for _ in range(n):
            size = min(budget * fraction, cash)   # equity base, cash ceiling
            if size <= 0:
                break
            sizes.append(round(size, 2))
            cash -= size
        return sizes, round(cash, 2)

    def test_every_slot_gets_the_same_size(self):
        sizes, _ = self.slot_sizes(1000.0, 5)
        self.assertEqual(len(set(sizes)), 1, f"positions decayed: {sizes}")

    def test_the_account_is_fully_deployed(self):
        _, idle = self.slot_sizes(1000.0, 5)
        self.assertAlmostEqual(idle, 0.0, places=2)

    def test_the_last_slot_is_as_viable_as_the_first(self):
        from fees import assess_trade
        sizes, _ = self.slot_sizes(1000.0, 3)
        first = assess_trade("US", sizes[0], 100.0, 2.0, 10.0)
        last = assess_trade("US", sizes[-1], 100.0, 2.0, 10.0)
        self.assertEqual(first.viable, last.viable)
        self.assertAlmostEqual(first.breakeven_pct, last.breakeven_pct, places=4)

    def test_n_means_n(self):
        # The old 25% FLOOR meant >4 slots could never be equal-weighted:
        # five positions each asking 25% wanted 125% of the account.
        from sizing import cash_fraction_for
        for n in (2, 3, 4, 5, 8, 10):
            self.assertAlmostEqual(cash_fraction_for(n) * n, 1.0, places=6,
                                   msg=f"{n} slots do not add up to one account")

    def test_one_position_can_never_take_the_whole_account(self):
        from sizing import ABSOLUTE_CASH_FRACTION_CEILING, cash_fraction_for
        self.assertEqual(cash_fraction_for(1), ABSOLUTE_CASH_FRACTION_CEILING)


class HorizonProfileTest(unittest.TestCase):
    """Switching horizon and back must not silently change these."""

    def test_both_new_fields_are_horizon_scoped(self):
        from models import HORIZON_FIELDS
        for field in ("max_hold_minutes", "breakeven_trigger_pct"):
            self.assertIn(field, HORIZON_FIELDS)

    def test_dataclass_defaults_match_the_intraday_profile(self):
        fresh = Settings()
        for field in ("max_hold_minutes", "breakeven_trigger_pct"):
            self.assertEqual(getattr(fresh, field), HORIZON_DEFAULTS["intraday"][field],
                             f"{field} would change on a horizon round trip")

    def test_each_horizon_keeps_its_own_value(self):
        s = Settings().normalized()
        s.max_hold_minutes = 45
        s.switch_horizon("swing")
        self.assertEqual(s.max_hold_minutes, 0)        # swing's own
        s.switch_horizon("intraday")
        self.assertEqual(s.max_hold_minutes, 45)       # intraday's preserved


class PersistenceTest(ExitHarness):
    """A restart mid-position must not lose the thesis or hand back protection
    the trade already earned."""

    settings_kwargs = {"breakeven_trigger_pct": 1.0}

    def test_both_fields_round_trip_through_the_snapshot(self):
        self.position(price=101.5, confirmations=("trend", "vwap"))
        self.exits([Quote(symbol="AAPL.US", price=101.5, timestamp="", source="longbridge")])
        snap = self.broker.snapshot()["portfolio"]["positions"]["AAPL.US"]
        self.assertEqual(snap["entry_confirmations"], ["trend", "vwap"])
        self.assertTrue(snap["breakeven_armed"])

    def test_reset_round_trip_clears_them(self):
        quotes = self.position(price=101.5, confirmations=("trend", "vwap"))
        self.exits(quotes)
        position = self.broker._portfolio.positions["AAPL.US"]
        position.reset_round_trip()
        self.assertEqual(position.entry_confirmations, [])
        self.assertFalse(position.breakeven_armed)


if __name__ == "__main__":
    unittest.main()
