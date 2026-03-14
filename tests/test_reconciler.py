"""Tests for ORB Reconciler — Phase 8."""

import json
import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock, PropertyMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from trading_floor.strategies.orb.reconciler import ORBReconciler, MismatchType


def _make_db(tmp_dir):
    """Create a fresh DB with position_meta + orders + budget_reservations tables."""
    db_path = os.path.join(tmp_dir, "trading.db")
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript("""
        CREATE TABLE position_meta (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT, strategy TEXT, side TEXT,
            entry_order_id TEXT, entry_price REAL, entry_time TIMESTAMP,
            entry_qty REAL, exit_order_id TEXT, exit_price REAL,
            exit_time TIMESTAMP, stop_price REAL, tp_price REAL,
            max_hold_days INTEGER, signals_json TEXT, market_regime TEXT,
            sector TEXT, exit_reason TEXT, pnl REAL, pnl_pct REAL,
            status TEXT, created_at TIMESTAMP
        );
        CREATE TABLE orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            alpaca_order_id TEXT, client_order_id TEXT,
            position_meta_id INTEGER, symbol TEXT, strategy TEXT,
            side TEXT, order_type TEXT, qty REAL, filled_qty REAL,
            limit_price REAL, stop_price REAL, avg_fill_price REAL,
            status TEXT, submitted_at TIMESTAMP, filled_at TIMESTAMP,
            cancelled_at TIMESTAMP, error_message TEXT, created_at TIMESTAMP
        );
        CREATE TABLE budget_reservations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            strategy TEXT, symbol TEXT, reserved_amount REAL,
            order_id INTEGER, status TEXT,
            created_at TIMESTAMP, released_at TIMESTAMP
        );
    """)
    conn.close()
    return db_path


def _insert_position(db_path, symbol, strategy="orb", side="long",
                     qty=100, price=50.0, status="open"):
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO position_meta (symbol, strategy, side, entry_qty, entry_price, status, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (symbol, strategy, side, qty, price, status,
         datetime.now(timezone.utc).isoformat())
    )
    conn.commit()
    conn.close()


def _insert_order(db_path, symbol, strategy="orb", status="new",
                  position_meta_id=None, alpaca_id="alp-1"):
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO orders (alpaca_order_id, symbol, strategy, status, position_meta_id, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (alpaca_id, symbol, strategy, status, position_meta_id,
         datetime.now(timezone.utc).isoformat())
    )
    conn.commit()
    conn.close()


def _insert_budget(db_path, strategy="orb", symbol="AAPL", amount=500, status="reserved"):
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO budget_reservations (strategy, symbol, reserved_amount, status, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (strategy, symbol, amount, status, datetime.now(timezone.utc).isoformat())
    )
    conn.commit()
    conn.close()


class MockPosition:
    """Mock Alpaca position object."""
    def __init__(self, symbol, qty, side="long"):
        self.symbol = symbol
        self.qty = str(qty)
        self.side = side


# ── Happy Path ───────────────────────────────────────────────

class TestReconcileHappyPath(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = _make_db(self.tmp)
        self.broker = MagicMock()
        self.rec = ORBReconciler(self.broker, self.db, report_dir=self.tmp)

    def test_all_match(self):
        """Alpaca and DB agree — no mismatches."""
        _insert_position(self.db, "AAPL", qty=100, side="long")
        self.broker.get_positions.return_value = [MockPosition("AAPL", 100, "long")]

        report = self.rec.reconcile()

        self.assertEqual(report["status"], "ok")
        self.assertEqual(len(report["mismatches"]), 0)

    def test_both_empty(self):
        """No positions anywhere — clean."""
        self.broker.get_positions.return_value = []
        report = self.rec.reconcile()
        self.assertEqual(report["status"], "ok")
        self.assertEqual(len(report["mismatches"]), 0)

    def test_multiple_match(self):
        """Multiple positions all matching."""
        _insert_position(self.db, "AAPL", qty=100, side="long")
        _insert_position(self.db, "MSFT", qty=50, side="long")
        self.broker.get_positions.return_value = [
            MockPosition("AAPL", 100, "long"),
            MockPosition("MSFT", 50, "long"),
        ]
        report = self.rec.reconcile()
        self.assertEqual(report["status"], "ok")
        self.assertEqual(len(report["mismatches"]), 0)


# ── Mismatch Detection ──────────────────────────────────────

class TestMismatchDetection(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = _make_db(self.tmp)
        self.broker = MagicMock()
        self.rec = ORBReconciler(self.broker, self.db, report_dir=self.tmp)

    def test_ghost_position(self):
        """Alpaca has position, DB doesn't → ghost (info severity)."""
        self.broker.get_positions.return_value = [MockPosition("TSLA", 50, "long")]
        report = self.rec.reconcile()
        ghosts = [m for m in report["mismatches"] if m["type"] == MismatchType.GHOST]
        self.assertEqual(len(ghosts), 1)
        self.assertEqual(ghosts[0]["symbol"], "TSLA")
        self.assertEqual(ghosts[0]["severity"], "info")

    def test_phantom_position(self):
        """DB has open position, Alpaca doesn't → phantom (critical)."""
        _insert_position(self.db, "NVDA", qty=30, side="long")
        self.broker.get_positions.return_value = []

        report = self.rec.reconcile()

        phantoms = [m for m in report["mismatches"] if m["type"] == MismatchType.PHANTOM]
        self.assertEqual(len(phantoms), 1)
        self.assertEqual(phantoms[0]["symbol"], "NVDA")
        self.assertEqual(phantoms[0]["severity"], "critical")
        self.assertEqual(report["status"], "critical")

    def test_phantom_auto_closes_db(self):
        """Phantom position gets auto-closed in DB."""
        _insert_position(self.db, "NVDA", qty=30, side="long")
        self.broker.get_positions.return_value = []
        self.rec.reconcile()

        # Check DB was updated
        conn = sqlite3.connect(self.db)
        row = conn.execute("SELECT status, exit_reason FROM position_meta WHERE symbol='NVDA'").fetchone()
        conn.close()
        self.assertEqual(row[0], "closed")
        self.assertEqual(row[1], "reconciler_phantom")

    def test_qty_mismatch(self):
        """Same symbol but different quantities."""
        _insert_position(self.db, "AAPL", qty=100, side="long")
        self.broker.get_positions.return_value = [MockPosition("AAPL", 75, "long")]

        report = self.rec.reconcile()

        qty_mm = [m for m in report["mismatches"] if m["type"] == MismatchType.QTY]
        self.assertEqual(len(qty_mm), 1)
        self.assertEqual(qty_mm[0]["severity"], "warning")
        self.assertEqual(report["status"], "warning")

    def test_side_mismatch(self):
        """Same symbol but different sides → critical."""
        _insert_position(self.db, "AAPL", qty=100, side="long")
        self.broker.get_positions.return_value = [MockPosition("AAPL", 100, "short")]

        report = self.rec.reconcile()

        side_mm = [m for m in report["mismatches"] if m["type"] == MismatchType.SIDE]
        self.assertEqual(len(side_mm), 1)
        self.assertEqual(side_mm[0]["severity"], "critical")

    def test_multiple_mismatches(self):
        """Multiple issues at once."""
        _insert_position(self.db, "AAPL", qty=100, side="long")  # phantom
        _insert_position(self.db, "MSFT", qty=50, side="long")   # qty mismatch
        self.broker.get_positions.return_value = [
            MockPosition("MSFT", 30, "long"),   # qty mismatch
            MockPosition("TSLA", 20, "long"),   # ghost
        ]

        report = self.rec.reconcile()

        self.assertEqual(report["status"], "critical")  # phantom = critical
        types = [m["type"] for m in report["mismatches"]]
        self.assertIn(MismatchType.PHANTOM, types)
        self.assertIn(MismatchType.QTY, types)
        self.assertIn(MismatchType.GHOST, types)

    def test_ignores_non_strategy_positions(self):
        """Swing positions in DB don't trigger phantom for ORB reconcile."""
        _insert_position(self.db, "CVX", strategy="swing", qty=5, side="long")
        self.broker.get_positions.return_value = [MockPosition("CVX", 5, "long")]

        report = self.rec.reconcile(strategy="orb")
        # CVX is swing, not orb — should see ghost (Alpaca has it, ORB DB doesn't)
        ghosts = [m for m in report["mismatches"] if m["type"] == MismatchType.GHOST]
        self.assertEqual(len(ghosts), 1)

    def test_closed_db_positions_ignored(self):
        """Closed positions in DB don't count."""
        _insert_position(self.db, "AAPL", qty=100, status="closed")
        self.broker.get_positions.return_value = []
        report = self.rec.reconcile()
        self.assertEqual(report["status"], "ok")

    def test_qty_tolerance(self):
        """Tiny qty difference (< 0.01) ignored."""
        _insert_position(self.db, "AAPL", qty=100.005, side="long")
        self.broker.get_positions.return_value = [MockPosition("AAPL", 100.001, "long")]
        report = self.rec.reconcile()
        qty_mm = [m for m in report["mismatches"] if m["type"] == MismatchType.QTY]
        self.assertEqual(len(qty_mm), 0)


# ── Orphaned Orders ─────────────────────────────────────────

class TestOrphanedOrders(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = _make_db(self.tmp)
        self.broker = MagicMock()
        self.broker.get_positions.return_value = []
        self.rec = ORBReconciler(self.broker, self.db, report_dir=self.tmp)

    def test_orphaned_order_detected(self):
        """Order with no matching open position → orphan."""
        _insert_order(self.db, "AAPL", status="new", position_meta_id=None)
        report = self.rec.reconcile()
        self.assertEqual(len(report["orphaned_orders"]), 1)

    def test_filled_orders_not_orphaned(self):
        """Filled/cancelled orders are not flagged."""
        _insert_order(self.db, "AAPL", status="filled", position_meta_id=None)
        _insert_order(self.db, "MSFT", status="cancelled", position_meta_id=None)
        report = self.rec.reconcile()
        self.assertEqual(len(report["orphaned_orders"]), 0)

    def test_order_with_valid_position_not_orphaned(self):
        """Order linked to open position → not orphaned."""
        _insert_position(self.db, "AAPL", qty=100)
        conn = sqlite3.connect(self.db)
        pm_id = conn.execute("SELECT id FROM position_meta WHERE symbol='AAPL'").fetchone()[0]
        conn.close()
        _insert_order(self.db, "AAPL", status="new", position_meta_id=pm_id)
        self.broker.get_positions.return_value = [MockPosition("AAPL", 100, "long")]
        report = self.rec.reconcile()
        self.assertEqual(len(report["orphaned_orders"]), 0)


# ── Budget Reservations ──────────────────────────────────────

class TestBudgetCleanup(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = _make_db(self.tmp)
        self.broker = MagicMock()
        self.broker.get_positions.return_value = []
        self.rec = ORBReconciler(self.broker, self.db, report_dir=self.tmp)

    def test_stale_budget_released(self):
        """Reserved budgets get released."""
        _insert_budget(self.db, status="reserved")
        report = self.rec.reconcile()
        self.assertEqual(report["stale_budgets_released"], 1)

        # Verify DB
        conn = sqlite3.connect(self.db)
        row = conn.execute("SELECT status FROM budget_reservations").fetchone()
        conn.close()
        self.assertEqual(row[0], "released")

    def test_already_released_not_counted(self):
        """Already-released budgets not double-counted."""
        _insert_budget(self.db, status="released")
        report = self.rec.reconcile()
        self.assertEqual(report["stale_budgets_released"], 0)

    def test_multiple_budgets(self):
        """Multiple stale budgets released."""
        _insert_budget(self.db, symbol="AAPL")
        _insert_budget(self.db, symbol="MSFT")
        report = self.rec.reconcile()
        self.assertEqual(report["stale_budgets_released"], 2)


# ── Stale Pendings ───────────────────────────────────────────

class TestStalePendings(unittest.TestCase):

    def test_delegates_to_floor_manager(self):
        tmp = tempfile.mkdtemp()
        db = _make_db(tmp)
        broker = MagicMock()
        broker.get_positions.return_value = []
        fm = MagicMock()
        fm.cleanup_stale_pendings.return_value = 3
        rec = ORBReconciler(broker, db, floor_manager=fm, report_dir=tmp)
        report = rec.reconcile()
        self.assertEqual(report["stale_pendings_cleaned"], 3)
        fm.cleanup_stale_pendings.assert_called_once()

    def test_no_floor_manager(self):
        tmp = tempfile.mkdtemp()
        db = _make_db(tmp)
        broker = MagicMock()
        broker.get_positions.return_value = []
        rec = ORBReconciler(broker, db, floor_manager=None, report_dir=tmp)
        report = rec.reconcile()
        self.assertEqual(report["stale_pendings_cleaned"], 0)


# ── Report & Alert ───────────────────────────────────────────

class TestReportAndAlert(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = _make_db(self.tmp)
        self.broker = MagicMock()
        self.broker.get_positions.return_value = []
        self.rec = ORBReconciler(self.broker, self.db, report_dir=self.tmp)

    def test_report_written(self):
        """JSON report written to disk."""
        self.rec.reconcile()
        report_path = os.path.join(self.tmp, "orb_reconciliation.json")
        self.assertTrue(os.path.exists(report_path))
        with open(report_path) as f:
            data = json.load(f)
        self.assertIn("timestamp", data)
        self.assertIn("status", data)

    def test_alert_none_when_clean(self):
        """No alert when everything is clean."""
        report = self.rec.reconcile()
        alert = self.rec.format_alert(report)
        self.assertIsNone(alert)

    def test_alert_on_mismatch(self):
        """Alert generated on mismatch."""
        _insert_position(self.db, "NVDA", qty=30)
        report = self.rec.reconcile()
        alert = self.rec.format_alert(report)
        self.assertIsNotNone(alert)
        self.assertIn("PHANTOM", alert)
        self.assertIn("NVDA", alert)

    def test_alert_includes_orphans(self):
        _insert_order(self.db, "AAPL", status="new")
        report = self.rec.reconcile()
        alert = self.rec.format_alert(report)
        self.assertIsNotNone(alert)
        self.assertIn("orphaned", alert)


# ── Error Handling ───────────────────────────────────────────

class TestErrorHandling(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = _make_db(self.tmp)
        self.broker = MagicMock()
        self.rec = ORBReconciler(self.broker, self.db, report_dir=self.tmp)

    def test_broker_failure(self):
        """Broker API error → error status, not crash."""
        self.broker.get_positions.side_effect = RuntimeError("API down")
        report = self.rec.reconcile()
        self.assertEqual(report["status"], "error")
        self.assertTrue(any("error" in m.get("type", "") for m in report["mismatches"]))

    def test_floor_manager_failure(self):
        """Floor manager error → graceful (count=0), not crash."""
        self.broker.get_positions.return_value = []
        fm = MagicMock()
        fm.cleanup_stale_pendings.side_effect = RuntimeError("DB locked")
        rec = ORBReconciler(self.broker, self.db, floor_manager=fm, report_dir=self.tmp)
        report = rec.reconcile()
        self.assertEqual(report["stale_pendings_cleaned"], 0)

    def test_dict_positions(self):
        """Broker returns dicts instead of objects → still works."""
        _insert_position(self.db, "AAPL", qty=100, side="long")
        self.broker.get_positions.return_value = [
            {"symbol": "AAPL", "qty": "100", "side": "long"}
        ]
        report = self.rec.reconcile()
        self.assertEqual(report["status"], "ok")


# ── Strategy Filtering ───────────────────────────────────────

class TestStrategyFiltering(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = _make_db(self.tmp)
        self.broker = MagicMock()
        self.broker.get_positions.return_value = []
        self.rec = ORBReconciler(self.broker, self.db, report_dir=self.tmp)

    def test_reconcile_swing(self):
        """Can reconcile swing strategy too."""
        _insert_position(self.db, "CVX", strategy="swing", qty=5)
        report = self.rec.reconcile(strategy="swing")
        phantoms = [m for m in report["mismatches"] if m["type"] == MismatchType.PHANTOM]
        self.assertEqual(len(phantoms), 1)

    def test_default_strategy_is_orb(self):
        """Default reconciles ORB only."""
        _insert_position(self.db, "AAPL", strategy="orb", qty=100)
        _insert_position(self.db, "CVX", strategy="swing", qty=5)
        self.broker.get_positions.return_value = [MockPosition("AAPL", 100, "long")]
        report = self.rec.reconcile()
        # AAPL matches, CVX is swing so ignored, broker has AAPL matched
        self.assertEqual(report["status"], "ok")


if __name__ == "__main__":
    unittest.main()
