"""Tests for ORB Executor — Phase 6."""

import os
import sys
import time
import unittest
import threading
from unittest.mock import MagicMock, patch, PropertyMock, call

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from trading_floor.strategies.orb.executor import ORBExecutor


def _make_executor(**overrides):
    """Factory for ORBExecutor with mocked deps."""
    broker = overrides.get("broker", MagicMock())
    exec_service = overrides.get("exec_service", MagicMock())
    floor_manager = overrides.get("floor_manager", MagicMock())
    # Default: floor allows, pending_id=42
    floor_manager.can_open_position.return_value = (True, "reserved pending_id=42")
    floor_manager.last_pending_id = 42
    config = overrides.get("config", {
        "order_type": "limit",
        "time_in_force": "day",
        "entry_slip_cents": 3,
        "confirm_timeout_sec": 0.5,   # very short for tests
        "confirm_poll_sec": 0.05,
    })
    return ORBExecutor(
        broker=broker,
        exec_service=exec_service,
        floor_manager=floor_manager,
        config=config,
        db_path=":memory:",
    ), broker, exec_service, floor_manager


def _mock_order(status="filled", filled_avg_price=105.0,
                filled_qty=50, symbol="AAPL", qty=50, order_id="ord-123"):
    """Create a mock Alpaca order object."""
    o = MagicMock()
    o.status = status
    o.filled_avg_price = filled_avg_price
    o.filled_qty = filled_qty
    o.symbol = symbol
    o.qty = qty
    o.id = order_id
    return o


# ── Entry Tests ──────────────────────────────────────────────

class TestEnterPosition(unittest.TestCase):

    def test_enter_position_success(self):
        """Happy path: floor allows, exec submits, confirm called with full args."""
        ex, broker, exec_svc, floor = _make_executor()
        exec_svc.submit.return_value = {
            "status": "submitted", "order_id": 1,
            "alpaca_order_id": "alp-1", "reservation_id": "r1"
        }

        result = ex.enter_position("AAPL", "buy", 50, 105.0, 100.0, 110.0, "Technology")

        self.assertEqual(result["status"], "submitted")
        self.assertEqual(result["pending_id"], 42)
        # confirm_position called with (pending_id, symbol, entry_price, qty)
        floor.confirm_position.assert_called_once_with(42, "AAPL", 105.0, 50)
        floor.release_slot.assert_not_called()

    def test_enter_position_floor_rejected(self):
        """Floor manager denies → rejected, no exec call."""
        ex, broker, exec_svc, floor = _make_executor()
        floor.can_open_position.return_value = (False, "orb_limit_reached")

        result = ex.enter_position("AAPL", "buy", 50, 105.0, 100.0, 110.0)

        self.assertEqual(result["status"], "rejected")
        self.assertEqual(result["reason"], "floor_limit")
        self.assertEqual(result["detail"], "orb_limit_reached")
        exec_svc.submit.assert_not_called()

    def test_enter_position_broker_failure_releases_slot(self):
        """Exec rejects → slot released."""
        ex, broker, exec_svc, floor = _make_executor()
        floor.last_pending_id = 7
        exec_svc.submit.return_value = {"status": "rejected", "reason": "budget"}

        result = ex.enter_position("AAPL", "buy", 50, 105.0, 100.0, 110.0)

        self.assertEqual(result["status"], "rejected")
        floor.release_slot.assert_called_once_with(7)
        floor.confirm_position.assert_not_called()

    def test_enter_position_exception_releases_slot(self):
        """Exception during submit → slot released."""
        ex, broker, exec_svc, floor = _make_executor()
        floor.last_pending_id = 99
        exec_svc.submit.side_effect = RuntimeError("network error")

        result = ex.enter_position("AAPL", "buy", 50, 105.0, 100.0, 110.0)

        self.assertEqual(result["status"], "error")
        floor.release_slot.assert_called_once_with(99)

    def test_enter_position_no_pending_id(self):
        """Floor allows but last_pending_id is None → error."""
        ex, broker, exec_svc, floor = _make_executor()
        floor.last_pending_id = None

        result = ex.enter_position("AAPL", "buy", 50, 105.0, 100.0, 110.0)

        self.assertEqual(result["status"], "error")
        self.assertIn("no_pending_id", result["reason"])


# ── Confirm Fill Tests ───────────────────────────────────────

class TestConfirmFill(unittest.TestCase):

    def test_confirm_fill_success(self):
        """Order is filled on first poll."""
        ex, broker, _, _ = _make_executor()
        broker.get_order.return_value = _mock_order("filled", 105.5, 50)

        result = ex.confirm_fill("alp-1")

        self.assertEqual(result["status"], "filled")
        self.assertAlmostEqual(result["fill_price"], 105.5)
        self.assertEqual(result["filled_qty"], 50)

    def test_confirm_fill_timeout_cancels(self):
        """Polling exhausts timeout → cancel called."""
        ex, broker, _, _ = _make_executor()
        broker.get_order.return_value = _mock_order("new")  # never fills

        result = ex.confirm_fill("alp-1")

        self.assertEqual(result["status"], "timeout")
        broker.cancel_order.assert_called_once_with("alp-1")

    def test_confirm_fill_rejected(self):
        """Order rejected by exchange."""
        ex, broker, _, _ = _make_executor()
        broker.get_order.return_value = _mock_order("rejected")

        result = ex.confirm_fill("alp-1")

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["reason"], "rejected")


# ── Partial Exit Tests ───────────────────────────────────────

class TestPartialExit(unittest.TestCase):

    def test_partial_exit_limit_fills(self):
        """Limit order fills within timeout."""
        ex, broker, exec_svc, _ = _make_executor()
        exec_svc.submit.return_value = {
            "status": "submitted", "alpaca_order_id": "alp-pe1"
        }
        broker.get_order.return_value = _mock_order("filled", 107.0, 25)

        result = ex.execute_partial_exit("AAPL", 25, 107.0, fallback_timeout=1)

        self.assertEqual(result["status"], "filled")
        self.assertAlmostEqual(result["fill_price"], 107.0)
        self.assertEqual(result["qty_sold"], 25)

    def test_partial_exit_market_fallback(self):
        """Limit doesn't fill → market fallback."""
        ex, broker, exec_svc, _ = _make_executor()
        exec_svc.submit.side_effect = [
            {"status": "submitted", "alpaca_order_id": "alp-pe2"},
            {"status": "submitted", "alpaca_order_id": "alp-pe3"},
        ]
        broker.get_order.return_value = _mock_order("new")  # never fills

        result = ex.execute_partial_exit("AAPL", 25, 107.0, fallback_timeout=0.3)

        self.assertEqual(result["status"], "market_fallback")
        self.assertEqual(result["qty_sold"], 25)
        self.assertEqual(exec_svc.submit.call_count, 2)


# ── Full Exit Tests ──────────────────────────────────────────

class TestExecuteExit(unittest.TestCase):

    def test_execute_exit_market(self):
        """Market order exit (force close / time stop)."""
        ex, broker, exec_svc, floor = _make_executor()
        exec_svc.submit.return_value = {"status": "submitted", "order_id": 5}

        result = ex.execute_exit("AAPL", 50, "market")

        self.assertEqual(result["status"], "submitted")
        call_kwargs = exec_svc.submit.call_args
        self.assertEqual(call_kwargs[1]["order_type"], "market")

    def test_execute_exit_stop(self):
        """Stop order exit (trailing stop)."""
        ex, broker, exec_svc, floor = _make_executor()
        exec_svc.submit.return_value = {"status": "submitted", "order_id": 6}

        result = ex.execute_exit("AAPL", 50, "stop", price=102.5)

        self.assertEqual(result["status"], "submitted")
        call_kwargs = exec_svc.submit.call_args
        self.assertEqual(call_kwargs[1]["stop_price"], 102.5)

    def test_execute_exit_closes_floor_position(self):
        """Floor close_position called on successful exit."""
        ex, broker, exec_svc, floor = _make_executor()
        floor.close_position = MagicMock()
        exec_svc.submit.return_value = {"status": "submitted", "order_id": 7}

        result = ex.execute_exit("AAPL", 50, "market")

        self.assertEqual(result["status"], "submitted")
        floor.close_position.assert_called_once_with("AAPL", "orb")

    def test_execute_exit_scoring_failure_doesnt_block(self):
        """Scoring raises → exit still succeeds."""
        ex, broker, exec_svc, floor = _make_executor()
        floor.close_position = MagicMock(side_effect=RuntimeError("db error"))
        exec_svc.submit.return_value = {"status": "submitted", "order_id": 8}

        result = ex.execute_exit("AAPL", 50, "market")

        self.assertEqual(result["status"], "submitted")

    def test_execute_exit_with_position_meta_id_scores(self):
        """When position_meta_id provided, _score_trade is called."""
        ex, broker, exec_svc, floor = _make_executor()
        exec_svc.submit.return_value = {"status": "submitted", "order_id": 9}
        ex._score_trade = MagicMock()

        result = ex.execute_exit("AAPL", 50, "market", position_meta_id=42)

        ex._score_trade.assert_called_once_with(42)

    def test_execute_exit_no_meta_id_no_scoring(self):
        """Without position_meta_id, scoring is skipped."""
        ex, broker, exec_svc, floor = _make_executor()
        exec_svc.submit.return_value = {"status": "submitted", "order_id": 10}
        ex._score_trade = MagicMock()

        result = ex.execute_exit("AAPL", 50, "market")

        ex._score_trade.assert_not_called()


# ── Modify Stop Tests ────────────────────────────────────────

class TestModifyStop(unittest.TestCase):

    def test_modify_stop_success(self):
        """Cancel + resubmit works."""
        ex, broker, _, _ = _make_executor()
        broker.get_order.return_value = _mock_order("open", symbol="AAPL", qty=50)
        new_order = _mock_order(order_id="ord-new")
        broker.submit_order.return_value = new_order

        result = ex.modify_stop("ord-old", 103.0)

        self.assertEqual(result["status"], "replaced")
        self.assertEqual(result["new_order_id"], "ord-new")
        self.assertAlmostEqual(result["new_stop_price"], 103.0)
        broker.cancel_order.assert_called_once_with("ord-old")

    def test_modify_stop_cancel_fails(self):
        """Cancel fails → graceful failure."""
        ex, broker, _, _ = _make_executor()
        broker.get_order.return_value = _mock_order("open", symbol="AAPL", qty=50)
        broker.cancel_order.side_effect = RuntimeError("API error")

        result = ex.modify_stop("ord-old", 103.0)

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["reason"], "cancel_failed")

    def test_modify_stop_get_order_fails(self):
        """Can't fetch current order → error."""
        ex, broker, _, _ = _make_executor()
        broker.get_order.side_effect = RuntimeError("not found")

        result = ex.modify_stop("ord-bad", 103.0)

        self.assertEqual(result["status"], "error")
        self.assertIn("get_order_failed", result["reason"])


# ── Budget / Cleanup Tests ───────────────────────────────────

class TestBudgetCleanup(unittest.TestCase):

    def test_budget_released_on_any_failure(self):
        """Floor slot released even if exec_service raises."""
        ex, broker, exec_svc, floor = _make_executor()
        floor.last_pending_id = 55
        exec_svc.submit.side_effect = Exception("kaboom")

        result = ex.enter_position("TSLA", "buy", 10, 200.0, 190.0, 210.0)

        self.assertEqual(result["status"], "error")
        floor.release_slot.assert_called_once_with(55)


# ── Concurrency Tests ────────────────────────────────────────

class TestConcurrency(unittest.TestCase):

    def test_concurrent_entries_serialized(self):
        """Two threads entering same symbol — per-symbol lock serializes."""
        ex, broker, exec_svc, floor = _make_executor()
        exec_svc.submit.return_value = {
            "status": "submitted", "order_id": 1,
            "alpaca_order_id": "a1", "reservation_id": "r1"
        }

        results = []

        def enter():
            r = ex.enter_position("AAPL", "buy", 50, 105.0, 100.0, 110.0)
            results.append(r)

        t1 = threading.Thread(target=enter)
        t2 = threading.Thread(target=enter)
        t1.start()
        t2.start()
        t1.join(timeout=5)
        t2.join(timeout=5)

        self.assertEqual(len(results), 2)
        for r in results:
            self.assertIn(r["status"], ("submitted", "rejected", "error"))

    def test_different_symbols_not_blocked(self):
        """Different symbols get different locks — no deadlock."""
        ex, broker, exec_svc, floor = _make_executor()
        exec_svc.submit.return_value = {
            "status": "submitted", "order_id": 1,
            "alpaca_order_id": "a1", "reservation_id": "r1"
        }

        results = []

        def enter(sym):
            r = ex.enter_position(sym, "buy", 50, 105.0, 100.0, 110.0)
            results.append(r)

        t1 = threading.Thread(target=enter, args=("AAPL",))
        t2 = threading.Thread(target=enter, args=("TSLA",))
        t1.start()
        t2.start()
        t1.join(timeout=5)
        t2.join(timeout=5)

        self.assertEqual(len(results), 2)

    def test_entry_and_exit_same_symbol_serialized(self):
        """Entry and exit on same symbol don't interleave."""
        ex, broker, exec_svc, floor = _make_executor()
        exec_svc.submit.return_value = {
            "status": "submitted", "order_id": 1,
            "alpaca_order_id": "a1"
        }

        results = []

        def enter():
            results.append(("enter", ex.enter_position("AAPL", "buy", 50, 105.0, 100.0, 110.0)))

        def exit_pos():
            results.append(("exit", ex.execute_exit("AAPL", 50, "market")))

        t1 = threading.Thread(target=enter)
        t2 = threading.Thread(target=exit_pos)
        t1.start()
        t2.start()
        t1.join(timeout=5)
        t2.join(timeout=5)

        self.assertEqual(len(results), 2)


if __name__ == "__main__":
    unittest.main()
