"""
Unit tests for PortfolioIntelligence (Phase 3).
Uses temp DB, mocked Alpaca calls for correlation.
"""
import json
import os
import sys
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from trading_floor.strategies.orb.portfolio_intel import PortfolioIntelligence


def create_test_db(db_path: str) -> None:
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


def insert_position(db_path, symbol, strategy, side, sector, price, qty, status="open"):
    conn = sqlite3.connect(db_path)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    conn.execute(
        "INSERT INTO position_meta (symbol, strategy, side, sector, entry_price, entry_qty, status, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (symbol, strategy, side, sector, price, qty, status, now),
    )
    conn.commit()
    conn.close()


class TestCrossDesk(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, "test.db")
        create_test_db(self.db_path)
        self.pi = PortfolioIntelligence(db_path=self.db_path)

    def test_same_symbol_same_direction_blocked(self):
        insert_position(self.db_path, "TSLA", "swing", "buy", "EV/Auto", 200, 5)
        ok, reason = self.pi.check_cross_desk("TSLA", "buy", "EV/Auto")
        self.assertFalse(ok)
        self.assertEqual(reason, "cross_desk_same_symbol_same_direction")

    def test_same_symbol_opposite_direction_allowed(self):
        insert_position(self.db_path, "TSLA", "swing", "buy", "EV/Auto", 200, 5)
        ok, reason = self.pi.check_cross_desk("TSLA", "sell", "EV/Auto")
        self.assertTrue(ok)
        self.assertEqual(reason, "cross_desk_hedge_ok")

    def test_same_sector_both_long_flagged(self):
        insert_position(self.db_path, "AAPL", "swing", "buy", "Technology", 150, 10)
        ok, reason = self.pi.check_cross_desk("MSFT", "buy", "Technology")
        self.assertTrue(ok)  # Advisory, not blocked
        self.assertEqual(reason, "cross_desk_same_sector_long_flag")

    def test_no_conflict_clear(self):
        insert_position(self.db_path, "AAPL", "swing", "buy", "Technology", 150, 10)
        ok, reason = self.pi.check_cross_desk("CVX", "buy", "Energy")
        self.assertTrue(ok)
        self.assertEqual(reason, "clear")

    def test_empty_positions_clear(self):
        ok, reason = self.pi.check_cross_desk("AAPL", "buy", "Technology")
        self.assertTrue(ok)
        self.assertEqual(reason, "clear")

    def test_orb_positions_ignored(self):
        # Only swing desk matters for cross-desk check
        insert_position(self.db_path, "TSLA", "intraday", "buy", "EV/Auto", 200, 5)
        ok, reason = self.pi.check_cross_desk("TSLA", "buy", "EV/Auto")
        self.assertTrue(ok)
        self.assertEqual(reason, "clear")


class TestSectorExposure(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, "test.db")
        create_test_db(self.db_path)
        self.pi = PortfolioIntelligence(db_path=self.db_path, orb_capital=3000, swing_capital=2000)

    def test_under_limit_allowed(self):
        ok, pct, reason = self.pi.check_sector_exposure("Technology", 500, "orb")
        self.assertTrue(ok)
        self.assertEqual(reason, "ok")

    def test_orb_sector_limit_exceeded(self):
        # ORB limit: 40% of $3K = $1,200
        insert_position(self.db_path, "AAPL", "intraday", "buy", "Technology", 100, 10)  # $1,000
        ok, pct, reason = self.pi.check_sector_exposure("Technology", 300, "orb")  # $1,300 total
        self.assertFalse(ok)
        self.assertEqual(reason, "orb_sector_limit_exceeded")

    def test_total_sector_limit_exceeded(self):
        # Total limit: 60% of $5K = $3,000
        insert_position(self.db_path, "AAPL", "swing", "buy", "Technology", 200, 12)  # $2,400 swing
        insert_position(self.db_path, "MSFT", "intraday", "buy", "Technology", 100, 4)  # $400 ORB
        ok, pct, reason = self.pi.check_sector_exposure("Technology", 300, "orb")  # $3,100 total
        self.assertFalse(ok)
        self.assertEqual(reason, "total_sector_limit_exceeded")

    def test_unknown_sector_skipped(self):
        ok, pct, reason = self.pi.check_sector_exposure("", 500, "orb")
        self.assertTrue(ok)
        self.assertEqual(reason, "unknown_sector_skipped")


class TestCorrelation(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, "test.db")
        create_test_db(self.db_path)
        self.pi = PortfolioIntelligence(db_path=self.db_path)

    def test_no_positions_returns_1(self):
        mult = self.pi.get_correlation_adjustment("AAPL", [])
        self.assertEqual(mult, 1.0)

    def test_high_correlation_returns_half(self):
        # Mock _fetch_close_prices to return highly correlated series
        correlated = [100 + i for i in range(20)]
        self.pi._fetch_close_prices = lambda syms: {s: list(correlated) for s in syms}
        mult = self.pi.get_correlation_adjustment("AAPL", ["MSFT"])
        self.assertEqual(mult, 0.5)

    def test_low_correlation_returns_1(self):
        # Mock with uncorrelated series (random-ish, low correlation)
        import random
        random.seed(42)
        a = [100 + random.uniform(-5, 5) for _ in range(20)]
        b = [100 + random.uniform(-5, 5) for _ in range(20)]
        self.pi._fetch_close_prices = lambda syms: {"AAPL": a, "GLD": b}
        mult = self.pi.get_correlation_adjustment("AAPL", ["GLD"])
        self.assertEqual(mult, 1.0)

    def test_cache_used_on_second_call(self):
        correlated = [100 + i for i in range(20)]
        call_count = [0]
        orig_fetch = self.pi._fetch_close_prices

        def counting_fetch(syms):
            call_count[0] += 1
            return {s: list(correlated) for s in syms}

        self.pi._fetch_close_prices = counting_fetch
        self.pi.get_correlation_adjustment("AAPL", ["MSFT"])
        self.pi.get_correlation_adjustment("AAPL", ["MSFT"])
        self.assertEqual(call_count[0], 1)  # Only fetched once


class TestNetExposure(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, "test.db")
        create_test_db(self.db_path)
        self.pi = PortfolioIntelligence(db_path=self.db_path)
        self.pi.exposure_path = Path(self.tmpdir) / "orb_exposure.json"

    def test_all_long_flagged(self):
        insert_position(self.db_path, "AAPL", "intraday", "buy", "Tech", 150, 10)
        insert_position(self.db_path, "MSFT", "intraday", "buy", "Tech", 200, 5)
        result = self.pi.get_net_exposure()
        self.assertTrue(result["flagged"])
        self.assertEqual(result["bias"], "long")
        self.assertAlmostEqual(result["net_pct"], 1.0)

    def test_balanced_not_flagged(self):
        insert_position(self.db_path, "AAPL", "intraday", "buy", "Tech", 150, 10)  # $1500 long
        insert_position(self.db_path, "SPY", "intraday", "sell", "ETF", 150, 10)   # $1500 short
        result = self.pi.get_net_exposure()
        self.assertFalse(result["flagged"])
        self.assertEqual(result["bias"], "flat")

    def test_empty_not_flagged(self):
        result = self.pi.get_net_exposure()
        self.assertFalse(result["flagged"])
        self.assertEqual(result["bias"], "flat")

    def test_writes_json_file(self):
        insert_position(self.db_path, "AAPL", "intraday", "buy", "Tech", 150, 10)
        self.pi.get_net_exposure()
        self.assertTrue(self.pi.exposure_path.exists())
        data = json.loads(self.pi.exposure_path.read_text())
        self.assertIn("net_pct", data)


class TestPreEntryCheck(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, "test.db")
        create_test_db(self.db_path)
        self.pi = PortfolioIntelligence(db_path=self.db_path)
        self.pi.exposure_path = Path(self.tmpdir) / "orb_exposure.json"
        # Disable Alpaca calls
        self.pi._client = None

    def test_all_clear(self):
        result = self.pi.pre_entry_check("AAPL", "buy", "Technology", 500)
        self.assertTrue(result["allowed"])
        self.assertEqual(result["sizing_mult"], 1.0)
        self.assertEqual(len(result["checks"]), 4)

    def test_cross_desk_blocks(self):
        insert_position(self.db_path, "TSLA", "swing", "buy", "EV/Auto", 200, 5)
        result = self.pi.pre_entry_check("TSLA", "buy", "EV/Auto", 500)
        self.assertFalse(result["allowed"])
        cross = next(c for c in result["checks"] if c["name"] == "cross_desk")
        self.assertFalse(cross["allowed"])


if __name__ == "__main__":
    unittest.main()
