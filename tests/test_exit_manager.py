"""
Unit tests for ORBExitManager (Phase 5).
Pure logic tests — no broker/DB calls.
"""
import os
import sys
import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from trading_floor.strategies.orb.exit_manager import ORBExitManager

ET = ZoneInfo("America/New_York")

DEFAULT_CONFIG = {
    "exit": {
        "partial_pct": 0.50,
        "partial_target_pct": 0.50,
        "trailing_atr_mult": 0.50,
        "trailing_min_pct": 0.0075,
        "stop_mm_pct": 0.30,
        "time_breakeven": "10:45",
        "time_tight": "11:00",
        "time_tight_pct": 0.003,
        "time_force_close": "11:30",
    }
}


def make_position(**overrides):
    """Helper to build position dict with sensible defaults."""
    base = {
        "symbol": "AAPL",
        "side": "long",
        "entry_price": 100.0,
        "qty": 10,
        "remaining_qty": 10,
        "measured_move": 10.0,  # $10 range
        "retest_low": 96.0,
        "entry_time": datetime(2026, 3, 13, 9, 46, tzinfo=ET),
        "partial_done": False,
        "current_price": 102.0,
        "current_time": datetime(2026, 3, 13, 10, 0, tzinfo=ET),
        "atr_1min": 1.0,
        "trailing_stop": None,
    }
    base.update(overrides)
    return base


class TestPartialExit(unittest.TestCase):
    def setUp(self):
        self.mgr = ORBExitManager(config=DEFAULT_CONFIG)

    def test_partial_exit_triggered_at_50pct(self):
        """Price at 105 = entry(100) + 50% of MM(10). Should trigger partial."""
        pos = make_position(current_price=105.0)
        action = self.mgr.check_exit(pos)
        self.assertIsNotNone(action)
        self.assertEqual(action["action"], "partial")
        self.assertEqual(action["qty"], 5)  # 50% of 10
        self.assertEqual(action["order_type"], "limit")
        self.assertAlmostEqual(action["price"], 104.98, places=2)

    def test_partial_exit_not_triggered_below_50pct(self):
        """Price at 103 < 105 target. No partial."""
        pos = make_position(current_price=103.0)
        action = self.mgr.check_exit(pos)
        self.assertIsNone(action)

    def test_partial_exit_already_done_skipped(self):
        """partial_done=True, price at target. Should NOT re-trigger partial."""
        pos = make_position(current_price=105.0, partial_done=True, remaining_qty=5)
        action = self.mgr.check_exit(pos)
        # Should not be a partial action (could be trailing or None)
        if action is not None:
            self.assertNotEqual(action["action"], "partial")

    def test_partial_exit_short_side(self):
        """Short: entry 100, MM 10, target = 95. Price at 94 → partial."""
        pos = make_position(side="short", entry_price=100.0, current_price=94.0,
                            measured_move=10.0, retest_low=104.0)
        action = self.mgr.check_exit(pos)
        self.assertIsNotNone(action)
        self.assertEqual(action["action"], "partial")


class TestTrailingStop(unittest.TestCase):
    def setUp(self):
        self.mgr = ORBExitManager(config=DEFAULT_CONFIG)

    def test_trailing_stop_atr_based(self):
        """Trail = price - 0.5*ATR. ATR=2.0 → trail = 108 - 1.0 = 107.0"""
        pos = make_position(current_price=108.0, atr_1min=2.0)
        stop = self.mgr.compute_trailing_stop(pos)
        self.assertAlmostEqual(stop, 107.0, places=2)

    def test_trailing_stop_minimum_floor(self):
        """If ATR is tiny, floor = 0.75% of entry. Entry 100 → floor = 0.75"""
        pos = make_position(current_price=108.0, atr_1min=0.01)
        stop = self.mgr.compute_trailing_stop(pos)
        # 0.5 * 0.01 = 0.005 < 0.75 (floor). So trail = 108 - 0.75 = 107.25
        self.assertAlmostEqual(stop, 107.25, places=2)

    def test_trailing_stop_only_trails_up(self):
        """With prev_stop=107, new computed=106 → should keep 107 (never widen)."""
        pos = make_position(current_price=106.5, atr_1min=2.0, trailing_stop=107.0)
        stop = self.mgr.compute_trailing_stop(pos)
        self.assertEqual(stop, 107.0)  # prev was higher, keep it

    def test_trailing_stop_trails_up_when_higher(self):
        """Price moves up: new trail > prev → use new."""
        pos = make_position(current_price=112.0, atr_1min=2.0, trailing_stop=107.0)
        stop = self.mgr.compute_trailing_stop(pos)
        # 112 - 1.0 = 111.0 > 107.0 → trail up
        self.assertAlmostEqual(stop, 111.0, places=2)

    def test_trailing_stop_hit_triggers_exit(self):
        """Price drops below trailing stop → exit."""
        pos = make_position(partial_done=True, remaining_qty=5,
                            current_price=106.5, atr_1min=2.0, trailing_stop=107.0)
        action = self.mgr.check_exit(pos)
        self.assertIsNotNone(action)
        self.assertEqual(action["action"], "trailing_stop")
        self.assertEqual(action["order_type"], "market")

    def test_trailing_stop_short_trails_down(self):
        """Short: trail = price + dist. Only goes DOWN."""
        pos = make_position(side="short", entry_price=100.0, current_price=92.0,
                            atr_1min=2.0, trailing_stop=95.0, retest_low=104.0)
        stop = self.mgr.compute_trailing_stop(pos)
        # 92 + 1.0 = 93 < 95 → keep 93 (trails down)
        self.assertAlmostEqual(stop, 93.0, places=2)


class TestInitialStop(unittest.TestCase):
    def setUp(self):
        self.mgr = ORBExitManager(config=DEFAULT_CONFIG)

    def test_initial_stop_30pct_mm(self):
        """MM=10, 30% = 3. Stop = 100 - 3 = 97. Retest_low=95 (looser). Use 97."""
        stop = self.mgr.compute_initial_stop(10.0, 100.0, 95.0, "long")
        self.assertAlmostEqual(stop, 97.0, places=2)

    def test_initial_stop_retest_low_tighter(self):
        """Retest_low=98 > mm_stop=97. Use 98 (tighter for long)."""
        stop = self.mgr.compute_initial_stop(10.0, 100.0, 98.0, "long")
        self.assertAlmostEqual(stop, 98.0, places=2)

    def test_initial_stop_no_retest(self):
        """No retest_low → use mm_stop only."""
        stop = self.mgr.compute_initial_stop(10.0, 100.0, None, "long")
        self.assertAlmostEqual(stop, 97.0, places=2)

    def test_initial_stop_short(self):
        """Short: stop = entry + 30% MM = 100 + 3 = 103."""
        stop = self.mgr.compute_initial_stop(10.0, 100.0, None, "short")
        self.assertAlmostEqual(stop, 103.0, places=2)

    def test_stop_loss_triggers_exit(self):
        """Price drops to stop → exit."""
        pos = make_position(current_price=96.0, retest_low=96.0)
        action = self.mgr.check_exit(pos)
        self.assertIsNotNone(action)
        self.assertEqual(action["action"], "stop_loss")
        self.assertEqual(action["order_type"], "market")


class TestTimeDecay(unittest.TestCase):
    def setUp(self):
        self.mgr = ORBExitManager(config=DEFAULT_CONFIG)

    def test_time_decay_breakeven_at_1045(self):
        """At 10:45, no partial done, below target → breakeven."""
        pos = make_position(current_price=103.0,
                            current_time=datetime(2026, 3, 13, 10, 45, tzinfo=ET))
        action = self.mgr.check_exit(pos)
        self.assertIsNotNone(action)
        self.assertEqual(action["action"], "time_decay_be")
        self.assertAlmostEqual(action["price"], 100.0, places=2)  # breakeven = entry

    def test_time_decay_tight_at_1100(self):
        """At 11:00, no partial done, below target → tight trail."""
        pos = make_position(current_price=103.0,
                            current_time=datetime(2026, 3, 13, 11, 0, tzinfo=ET))
        action = self.mgr.check_exit(pos)
        self.assertIsNotNone(action)
        self.assertEqual(action["action"], "time_decay_tight")

    def test_time_decay_not_triggered_if_partial_done(self):
        """partial_done=True → no time decay (trailing stop takes over)."""
        action = self.mgr.get_time_decay_action(
            make_position(partial_done=True,
                          current_time=datetime(2026, 3, 13, 10, 50, tzinfo=ET)))
        self.assertIsNone(action)

    def test_time_decay_not_triggered_if_at_target(self):
        """Price at partial target → no time decay (partial exit instead)."""
        action = self.mgr.get_time_decay_action(
            make_position(current_price=105.0,
                          current_time=datetime(2026, 3, 13, 10, 50, tzinfo=ET)))
        self.assertIsNone(action)


class TestTimeStop(unittest.TestCase):
    def setUp(self):
        self.mgr = ORBExitManager(config=DEFAULT_CONFIG)

    def test_time_force_close_at_1130(self):
        """At 11:30 → force close."""
        pos = make_position(current_time=datetime(2026, 3, 13, 11, 30, tzinfo=ET))
        action = self.mgr.check_exit(pos)
        self.assertIsNotNone(action)
        self.assertEqual(action["action"], "time_stop")

    def test_time_force_close_is_market_order(self):
        """Force close must be market order."""
        pos = make_position(current_time=datetime(2026, 3, 13, 11, 31, tzinfo=ET))
        action = self.mgr.check_exit(pos)
        self.assertEqual(action["order_type"], "market")

    def test_time_stop_priority_over_partial(self):
        """At 11:30, even if at partial target, time_stop wins."""
        pos = make_position(current_price=105.0,
                            current_time=datetime(2026, 3, 13, 11, 30, tzinfo=ET))
        action = self.mgr.check_exit(pos)
        self.assertEqual(action["action"], "time_stop")


class TestEdgeCases(unittest.TestCase):
    def setUp(self):
        self.mgr = ORBExitManager(config=DEFAULT_CONFIG)

    def test_check_exit_returns_none_when_no_action(self):
        """Normal position, no triggers → None."""
        pos = make_position(current_price=102.0)
        action = self.mgr.check_exit(pos)
        self.assertIsNone(action)

    def test_exception_returns_none_not_crash(self):
        """Bad input → None, no crash."""
        action = self.mgr.check_exit({"entry_price": "bad"})
        self.assertIsNone(action)

    def test_empty_position_returns_none(self):
        action = self.mgr.check_exit({})
        self.assertIsNone(action)

    def test_none_position_returns_none(self):
        action = self.mgr.check_exit(None)
        self.assertIsNone(action)


if __name__ == "__main__":
    unittest.main()
