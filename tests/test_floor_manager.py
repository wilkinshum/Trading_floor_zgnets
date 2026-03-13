"""
Unit tests for FloorPositionManager (Phase 2).
Uses temp DB + mocked lock file. Tests position limits, sector caps,
stale cleanup, confirm/release flow.
"""
import os
import sys
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch, MagicMock
from contextlib import contextmanager

# Add project root to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))


def create_test_db(db_path: str) -> None:
    """Create position_meta table matching production schema."""
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS position_meta (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL,
            strategy TEXT NOT NULL,
            side TEXT NOT NULL DEFAULT 'buy',
            entry_price REAL,
            entry_time TIMESTAMP,
            entry_qty REAL,
            sector TEXT,
            status TEXT DEFAULT 'open',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            pnl REAL,
            signals_json TEXT
        )
    """)
    conn.commit()
    conn.close()


@contextmanager
def _noop_lock():
    """No-op lock for testing (skip msvcrt)."""
    yield


class TestFloorPositionManager(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, "test.db")
        self.lock_file = os.path.join(self.tmpdir, ".floor_lock")
        create_test_db(self.db_path)

        # Patch the file lock to no-op for unit tests
        from trading_floor.strategies.orb import floor_manager as fm_module
        self._orig_file_lock = fm_module.FloorPositionManager._file_lock
        fm_module.FloorPositionManager._file_lock = lambda self: _noop_lock()

        self.mgr = fm_module.FloorPositionManager(
            db_path=self.db_path,
            lock_file=self.lock_file,
        )
        # Override config-loaded values to known test values
        self.mgr.max_orb_positions = 2
        self.mgr.max_swing_positions = 3
        self.mgr.max_total_positions = 5
        self.mgr.max_per_sector = 1

    def tearDown(self):
        from trading_floor.strategies.orb import floor_manager as fm_module
        fm_module.FloorPositionManager._file_lock = self._orig_file_lock
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _insert_open(self, symbol, strategy, sector=None):
        """Helper: insert an open position directly."""
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "INSERT INTO position_meta (symbol, strategy, side, status, sector, created_at) "
            "VALUES (?, ?, 'buy', 'open', ?, ?)",
            (symbol, strategy, sector, datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")),
        )
        conn.commit()
        conn.close()

    # ── Basic ORB limits ──────────────────────────────────────

    def test_orb_can_open_two(self):
        ok1, _ = self.mgr.can_open_position("orb", "AAPL", "Technology")
        self.assertTrue(ok1)
        ok2, _ = self.mgr.can_open_position("orb", "MSFT", "Software")
        self.assertTrue(ok2)

    def test_orb_rejected_on_third(self):
        self._insert_open("AAPL", "intraday", "Technology")
        self._insert_open("MSFT", "intraday", "Software")
        ok, reason = self.mgr.can_open_position("orb", "GOOGL", "Internet")
        self.assertFalse(ok)
        self.assertEqual(reason, "orb_limit_reached")

    # ── Basic Swing limits ────────────────────────────────────

    def test_swing_can_open_three(self):
        ok1, _ = self.mgr.can_open_position("swing", "CVX", "Energy")
        self.assertTrue(ok1)
        ok2, _ = self.mgr.can_open_position("swing", "NFLX", "Media")
        self.assertTrue(ok2)
        ok3, _ = self.mgr.can_open_position("swing", "JPM", "Finance")
        self.assertTrue(ok3)

    def test_swing_rejected_on_fourth(self):
        self._insert_open("CVX", "swing", "Energy")
        self._insert_open("NFLX", "swing", "Media")
        self._insert_open("JPM", "swing", "Finance")
        ok, reason = self.mgr.can_open_position("swing", "GS", "Finance2")
        self.assertFalse(ok)
        self.assertEqual(reason, "swing_limit_reached")

    # ── Total cap ─────────────────────────────────────────────

    def test_total_cap_enforced(self):
        self._insert_open("AAPL", "intraday", "Tech")
        self._insert_open("MSFT", "intraday", "Software")
        self._insert_open("CVX", "swing", "Energy")
        self._insert_open("NFLX", "swing", "Media")
        self._insert_open("JPM", "swing", "Finance")
        # 5 total — ORB hits desk cap first (2), swing hits desk cap first (3)
        ok1, reason1 = self.mgr.can_open_position("orb", "GOOGL", "Internet")
        self.assertFalse(ok1)
        self.assertIn(reason1, ("orb_limit_reached", "total_limit_reached"))
        ok2, reason2 = self.mgr.can_open_position("swing", "GS", "Banking")
        self.assertFalse(ok2)
        self.assertIn(reason2, ("swing_limit_reached", "total_limit_reached"))

    # ── Sector limits ─────────────────────────────────────────

    def test_sector_limit_same_desk(self):
        self._insert_open("AAPL", "intraday", "Technology")
        ok, reason = self.mgr.can_open_position("orb", "MSFT", "Technology")
        self.assertFalse(ok)
        self.assertEqual(reason, "sector_limit_reached")

    def test_sector_different_desk_allowed(self):
        self._insert_open("AAPL", "intraday", "Technology")
        ok, _ = self.mgr.can_open_position("swing", "MSFT", "Technology")
        self.assertTrue(ok)  # Different desk, same sector = OK

    def test_no_sector_allowed_with_warning(self):
        ok, _ = self.mgr.can_open_position("orb", "XYZ", "")
        self.assertTrue(ok)  # Unknown sector = allowed (architect rec)

    # ── Stale cleanup ─────────────────────────────────────────

    def test_stale_pending_cleaned(self):
        # Insert a stale pending (10 min ago)
        conn = sqlite3.connect(self.db_path)
        old_time = (datetime.utcnow() - timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M:%S")
        conn.execute(
            "INSERT INTO position_meta (symbol, strategy, side, status, sector, created_at) "
            "VALUES ('OLD', 'intraday', 'buy', 'pending', 'Tech', ?)", (old_time,)
        )
        conn.commit()
        conn.close()

        count = self.mgr.cleanup_stale_pendings(max_age_minutes=5)
        self.assertEqual(count, 1)

    def test_fresh_pending_not_cleaned(self):
        self.mgr.can_open_position("orb", "AAPL", "Tech")
        count = self.mgr.cleanup_stale_pendings(max_age_minutes=5)
        self.assertEqual(count, 0)  # Just created, not stale

    # ── Confirm / Release ─────────────────────────────────────

    def test_confirm_converts_pending_to_open(self):
        ok, msg = self.mgr.can_open_position("orb", "AAPL", "Tech")
        self.assertTrue(ok)
        pid = self.mgr.last_pending_id

        result = self.mgr.confirm_position(pid, "AAPL", 150.00, 10)
        self.assertTrue(result)

        # Verify it's open now
        conn = sqlite3.connect(self.db_path)
        row = conn.execute("SELECT status, entry_price, entry_qty FROM position_meta WHERE id=?", (pid,)).fetchone()
        conn.close()
        self.assertEqual(row[0], "open")
        self.assertEqual(row[1], 150.00)
        self.assertEqual(row[2], 10)

    def test_release_removes_pending(self):
        ok, _ = self.mgr.can_open_position("orb", "AAPL", "Tech")
        self.assertTrue(ok)
        pid = self.mgr.last_pending_id

        self.mgr.release_slot(pid)

        conn = sqlite3.connect(self.db_path)
        row = conn.execute("SELECT COUNT(*) FROM position_meta WHERE id=?", (pid,)).fetchone()
        conn.close()
        self.assertEqual(row[0], 0)

    def test_release_frees_slot_for_new_position(self):
        # Fill 2 ORB slots
        ok1, _ = self.mgr.can_open_position("orb", "AAPL", "Tech")
        pid1 = self.mgr.last_pending_id
        ok2, _ = self.mgr.can_open_position("orb", "MSFT", "Software")
        self.assertTrue(ok1 and ok2)

        # 3rd rejected
        ok3, reason = self.mgr.can_open_position("orb", "GOOGL", "Internet")
        self.assertFalse(ok3)

        # Release one
        self.mgr.release_slot(pid1)

        # Now 3rd should work
        ok4, _ = self.mgr.can_open_position("orb", "GOOGL", "Internet")
        self.assertTrue(ok4)

    # ── Floor status ──────────────────────────────────────────

    def test_floor_status_correct(self):
        self._insert_open("AAPL", "intraday", "Tech")
        self._insert_open("CVX", "swing", "Energy")
        self.mgr.can_open_position("orb", "MSFT", "Software")  # pending

        status = self.mgr.get_floor_status()
        self.assertEqual(status["orb"]["open"], 1)
        self.assertEqual(status["orb"]["pending"], 1)
        self.assertEqual(status["swing"]["open"], 1)
        self.assertEqual(status["total"]["open"], 2)
        self.assertEqual(status["total"]["pending"], 1)
        self.assertEqual(status["limits"]["orb"], 2)
        self.assertEqual(status["limits"]["swing"], 3)
        self.assertEqual(status["limits"]["total"], 5)

    # ── Edge cases ────────────────────────────────────────────

    def test_invalid_symbol_rejected(self):
        ok, reason = self.mgr.can_open_position("orb", "", "Tech")
        self.assertFalse(ok)
        self.assertEqual(reason, "invalid_symbol")

    def test_invalid_strategy_raises(self):
        with self.assertRaises(ValueError):
            self.mgr.can_open_position("unknown", "AAPL", "Tech")

    def test_confirm_nonexistent_returns_false(self):
        result = self.mgr.confirm_position(99999, "AAPL", 100.0, 10)
        self.assertFalse(result)

    def test_confirm_none_returns_false(self):
        result = self.mgr.confirm_position(None, "AAPL", 100.0, 10)
        self.assertFalse(result)

    def test_release_none_is_noop(self):
        self.mgr.release_slot(None)  # Should not raise


if __name__ == "__main__":
    unittest.main()
