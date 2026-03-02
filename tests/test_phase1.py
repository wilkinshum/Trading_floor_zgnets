"""Phase 1 unit tests for Trading Floor v4.0 broker modules.

Mocks all Alpaca API calls — no real API hits.
"""

import os
import sys
import time
import sqlite3
import tempfile
import unittest
from unittest.mock import MagicMock, patch, PropertyMock
from datetime import datetime, timezone
from types import SimpleNamespace

# Ensure src is on path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from trading_floor.db import Database
from trading_floor.broker.alpaca_broker import AlpacaBroker, RateLimiter, _retry_with_backoff
from trading_floor.broker.portfolio_state import PortfolioState
from trading_floor.broker.order_ledger import OrderLedger
from trading_floor.broker.strategy_budgeter import StrategyBudgeter
from trading_floor.broker.execution_service import ExecutionService


def _make_db(tmp_dir):
    """Create a Database pointing at a temp file."""
    path = os.path.join(tmp_dir, "test.db")
    return Database(db_path=path)


def _mock_account(cash=5000, equity=5000, buying_power=10000, last_equity=5000):
    acct = SimpleNamespace(
        cash=str(cash), equity=str(equity),
        buying_power=str(buying_power), last_equity=str(last_equity),
    )
    return acct


def _mock_position(symbol, qty=10, market_value=1000, avg_entry=100,
                    unrealized_pl=50, unrealized_plpc=0.05, current_price=105):
    return SimpleNamespace(
        symbol=symbol, qty=str(qty), side="long",
        market_value=str(market_value), avg_entry_price=str(avg_entry),
        unrealized_pl=str(unrealized_pl), unrealized_plpc=str(unrealized_plpc),
        current_price=str(current_price),
    )


def _mock_alpaca_order(order_id="abc-123", status="new", filled_qty=0,
                        filled_avg_price=None):
    return SimpleNamespace(
        id=order_id, status=status,
        filled_qty=str(filled_qty) if filled_qty else None,
        filled_avg_price=str(filled_avg_price) if filled_avg_price else None,
    )


# ═══════════════════════════════════════════════════════════
# DB Schema Tests
# ═══════════════════════════════════════════════════════════

class TestDBSchema(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = _make_db(self.tmp)

    def test_v4_tables_created(self):
        """All v4 tables should exist after init."""
        conn = self.db._get_conn()
        cursor = conn.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = {row[0] for row in cursor.fetchall()}
        conn.close()

        expected = {
            "trades", "signals", "events", "agent_memory", "shadow_predictions",
            "position_meta", "orders", "fills", "budget_reservations",
            "signal_accuracy", "reviews", "config_history",
        }
        self.assertTrue(expected.issubset(tables), f"Missing: {expected - tables}")

    def test_v4_indexes_created(self):
        """All v4 indexes should exist."""
        conn = self.db._get_conn()
        cursor = conn.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type='index'")
        indexes = {row[0] for row in cursor.fetchall()}
        conn.close()

        expected_indexes = {
            "idx_position_meta_strategy_status",
            "idx_position_meta_symbol",
            "idx_orders_status",
            "idx_orders_symbol",
            "idx_signal_accuracy_type",
            "idx_budget_reservations_strategy",
            "idx_config_history_field",
        }
        self.assertTrue(expected_indexes.issubset(indexes), f"Missing: {expected_indexes - indexes}")

    def test_existing_tables_preserved(self):
        """Existing tables (trades, signals, etc.) must still work."""
        self.db.log_trade({
            "timestamp": "2026-01-01T00:00:00",
            "symbol": "AAPL", "side": "buy", "quantity": 10, "price": 150.0,
        })
        conn = self.db._get_conn()
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM trades")
        self.assertEqual(cursor.fetchone()[0], 1)
        conn.close()


# ═══════════════════════════════════════════════════════════
# Rate Limiter Tests
# ═══════════════════════════════════════════════════════════

class TestRateLimiter(unittest.TestCase):
    def test_no_delay_under_limit(self):
        rl = RateLimiter(max_calls=10, window=60)
        start = time.monotonic()
        for _ in range(10):
            rl.wait_if_needed()
        elapsed = time.monotonic() - start
        self.assertLess(elapsed, 1.0)

    def test_retry_with_backoff_success(self):
        calls = []
        def flaky():
            calls.append(1)
            if len(calls) < 3:
                raise RuntimeError("fail")
            return "ok"
        result = _retry_with_backoff(flaky, max_retries=3, base_delay=0.01)
        self.assertEqual(result, "ok")
        self.assertEqual(len(calls), 3)

    def test_retry_exhausted(self):
        def always_fail():
            raise RuntimeError("permanent")
        with self.assertRaises(RuntimeError):
            _retry_with_backoff(always_fail, max_retries=2, base_delay=0.01)


# ═══════════════════════════════════════════════════════════
# AlpacaBroker Tests
# ═══════════════════════════════════════════════════════════

class TestAlpacaBroker(unittest.TestCase):
    def test_client_order_id_format(self):
        cid = AlpacaBroker.make_client_order_id("intraday", "AAPL")
        parts = cid.split("_")
        self.assertEqual(parts[0], "intraday")
        self.assertEqual(parts[1], "AAPL")
        self.assertTrue(parts[2].isdigit())

    @patch("trading_floor.broker.alpaca_broker.TradingClient")
    @patch("trading_floor.broker.alpaca_broker.StockHistoricalDataClient")
    def test_get_account(self, mock_data, mock_trading):
        mock_trading.return_value.get_account.return_value = _mock_account()
        broker = AlpacaBroker("key", "secret", paper=True)
        acct = broker.get_account()
        self.assertEqual(acct.cash, "5000")

    @patch("trading_floor.broker.alpaca_broker.TradingClient")
    @patch("trading_floor.broker.alpaca_broker.StockHistoricalDataClient")
    def test_submit_market_order(self, mock_data, mock_trading):
        mock_order = _mock_alpaca_order()
        mock_trading.return_value.submit_order.return_value = mock_order
        broker = AlpacaBroker("key", "secret")
        result = broker.submit_order("AAPL", 10, "buy")
        self.assertEqual(result.id, "abc-123")


# ═══════════════════════════════════════════════════════════
# PortfolioState Tests
# ═══════════════════════════════════════════════════════════

class TestPortfolioState(unittest.TestCase):
    def setUp(self):
        self.broker = MagicMock()
        self.broker.get_account.return_value = _mock_account()
        self.broker.get_positions.return_value = [
            _mock_position("AAPL"), _mock_position("MSFT", market_value=2000),
        ]
        self.ps = PortfolioState(self.broker)

    def test_properties(self):
        self.assertEqual(self.ps.cash, 5000.0)
        self.assertEqual(self.ps.equity, 5000.0)
        self.assertEqual(self.ps.buying_power, 10000.0)
        self.assertEqual(self.ps.daily_pnl, 0.0)

    def test_positions(self):
        positions = self.ps.positions
        self.assertEqual(len(positions), 2)
        self.assertEqual(positions[0]["symbol"], "AAPL")

    def test_caching(self):
        """Account should be fetched only once within TTL."""
        _ = self.ps.cash
        _ = self.ps.equity
        _ = self.ps.buying_power
        self.assertEqual(self.broker.get_account.call_count, 1)

    def test_invalidate(self):
        _ = self.ps.cash
        self.ps.invalidate()
        _ = self.ps.cash
        self.assertEqual(self.broker.get_account.call_count, 2)

    def test_get_position_value(self):
        self.assertEqual(self.ps.get_position_value("AAPL"), 1000.0)
        self.assertEqual(self.ps.get_position_value("TSLA"), 0.0)


# ═══════════════════════════════════════════════════════════
# OrderLedger Tests
# ═══════════════════════════════════════════════════════════

class TestOrderLedger(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = _make_db(self.tmp)
        self.ledger = OrderLedger(self.db)

    def test_record_order(self):
        oid = self.ledger.record_order(
            alpaca_order_id="alp-1", client_order_id="intraday_AAPL_123",
            symbol="AAPL", strategy="intraday", side="buy",
            order_type="market", qty=10,
        )
        self.assertIsInstance(oid, int)
        order = self.ledger.get_order(oid)
        self.assertEqual(order["symbol"], "AAPL")
        self.assertEqual(order["status"], "pending")

    def test_partial_fills(self):
        """Two partial fills should accumulate qty and compute weighted avg price."""
        oid = self.ledger.record_order(
            alpaca_order_id="alp-2", client_order_id="intraday_MSFT_456",
            symbol="MSFT", strategy="intraday", side="buy",
            order_type="limit", qty=20, limit_price=100.0,
        )
        self.ledger.record_fill(oid, "alp-2", 99.0, 10)
        self.ledger.record_fill(oid, "alp-2", 101.0, 10)

        order = self.ledger.get_order(oid)
        self.assertEqual(order["filled_qty"], 20.0)
        self.assertAlmostEqual(order["avg_fill_price"], 100.0)

    def test_status_update(self):
        oid = self.ledger.record_order(
            alpaca_order_id="alp-3", client_order_id="swing_TSLA_789",
            symbol="TSLA", strategy="swing", side="buy",
            order_type="market", qty=5,
        )
        self.ledger.update_status(oid, "filled")
        order = self.ledger.get_order(oid)
        self.assertEqual(order["status"], "filled")
        self.assertIsNotNone(order["filled_at"])

    def test_get_by_alpaca_id(self):
        self.ledger.record_order(
            alpaca_order_id="alp-4", client_order_id="intraday_GME_111",
            symbol="GME", strategy="intraday", side="buy",
            order_type="market", qty=50,
        )
        order = self.ledger.get_order_by_alpaca_id("alp-4")
        self.assertIsNotNone(order)
        self.assertEqual(order["symbol"], "GME")
        self.assertIsNone(self.ledger.get_order_by_alpaca_id("nonexistent"))


# ═══════════════════════════════════════════════════════════
# StrategyBudgeter Tests
# ═══════════════════════════════════════════════════════════

class TestStrategyBudgeter(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = _make_db(self.tmp)
        self.portfolio = MagicMock()
        self.portfolio.get_position_value.return_value = 0.0
        self.budgeter = StrategyBudgeter(
            self.db, self.portfolio,
            strategy_budgets={"intraday": 2000, "swing": 3000},
        )

    def test_reserve_and_available(self):
        avail = self.budgeter.get_available("intraday")
        self.assertEqual(avail, 2000.0)

        rid = self.budgeter.reserve("intraday", "AAPL", 500)
        self.assertIsInstance(rid, int)
        self.assertAlmostEqual(self.budgeter.get_available("intraday"), 1500.0)

    def test_release(self):
        rid = self.budgeter.reserve("intraday", "AAPL", 500)
        self.budgeter.release(rid)
        self.assertAlmostEqual(self.budgeter.get_available("intraday"), 2000.0)

    def test_double_spend_prevention(self):
        """Reserving more than available should raise ValueError."""
        self.budgeter.reserve("intraday", "AAPL", 1500)
        with self.assertRaises(ValueError):
            self.budgeter.reserve("intraday", "MSFT", 600)

    def test_positions_reduce_available(self):
        """Open positions should reduce available budget."""
        # Simulate an open position worth $1000
        self.portfolio.get_position_value.return_value = 1000.0
        # Insert a position_meta row so get_open_position_value finds it
        conn = self.db._get_conn()
        cursor = conn.cursor()
        cursor.execute(
            """INSERT INTO position_meta (symbol, strategy, side, status)
               VALUES ('AAPL', 'intraday', 'buy', 'open')"""
        )
        conn.commit()
        conn.close()

        avail = self.budgeter.get_available("intraday")
        self.assertAlmostEqual(avail, 1000.0)


# ═══════════════════════════════════════════════════════════
# ExecutionService Tests
# ═══════════════════════════════════════════════════════════

class TestExecutionService(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = _make_db(self.tmp)

        self.broker = MagicMock()
        self.broker.make_client_order_id = AlpacaBroker.make_client_order_id
        self.broker.submit_order.return_value = _mock_alpaca_order("order-001")

        self.portfolio = MagicMock()
        self.portfolio.get_position_value.return_value = 0.0
        self.portfolio.invalidate = MagicMock()

        self.ledger = OrderLedger(self.db, self.broker)
        self.budgeter = StrategyBudgeter(
            self.db, self.portfolio, {"intraday": 2000, "swing": 3000},
        )
        self.svc = ExecutionService(
            self.broker, self.ledger, self.budgeter, self.portfolio,
        )

    def test_submit_success(self):
        result = self.svc.submit(
            symbol="AAPL", qty=10, side="buy", strategy="intraday",
            estimated_cost=500,
        )
        self.assertEqual(result["status"], "submitted")
        self.assertIn("order_id", result)
        self.assertIn("alpaca_order_id", result)

    def test_deduplication(self):
        """Second identical order within 60s should be rejected."""
        self.svc.submit(symbol="AAPL", qty=10, side="buy", strategy="intraday",
                        estimated_cost=500)
        result = self.svc.submit(symbol="AAPL", qty=10, side="buy",
                                  strategy="intraday", estimated_cost=500)
        self.assertEqual(result["status"], "rejected")
        self.assertIn("Duplicate", result["reason"])

    def test_different_strategy_not_duplicate(self):
        """Same symbol but different strategy should not be deduplicated."""
        self.broker.submit_order.side_effect = [
            _mock_alpaca_order("order-001"),
            _mock_alpaca_order("order-002"),
        ]
        self.svc.submit(symbol="AAPL", qty=10, side="buy", strategy="intraday",
                        estimated_cost=500)
        result = self.svc.submit(symbol="AAPL", qty=10, side="buy",
                                  strategy="swing", estimated_cost=500)
        self.assertEqual(result["status"], "submitted")

    def test_budget_rejection(self):
        """Order exceeding budget should be rejected."""
        result = self.svc.submit(
            symbol="AAPL", qty=100, side="buy", strategy="intraday",
            estimated_cost=5000,
        )
        self.assertEqual(result["status"], "rejected")
        self.assertIn("Insufficient", result["reason"])

    def test_broker_failure_releases_reservation(self):
        """If Alpaca rejects, budget reservation should be released."""
        self.broker.submit_order.side_effect = RuntimeError("API error")
        result = self.svc.submit(
            symbol="AAPL", qty=10, side="buy", strategy="intraday",
            estimated_cost=500,
        )
        self.assertEqual(result["status"], "rejected")
        # Budget should be fully available again
        self.assertAlmostEqual(self.budgeter.get_available("intraday"), 2000.0)

    def test_sell_no_budget_check(self):
        """Sell orders should not require budget reservation."""
        result = self.svc.submit(
            symbol="AAPL", qty=10, side="sell", strategy="intraday",
        )
        self.assertEqual(result["status"], "submitted")


if __name__ == "__main__":
    unittest.main()
