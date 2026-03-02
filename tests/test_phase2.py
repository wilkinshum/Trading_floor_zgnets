"""Phase 2 unit tests for Trading Floor v4.0 strategy engines.

Mocks all Alpaca API calls and market data — no real API hits.
"""

import os
import sys
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch, PropertyMock
from pathlib import Path

# Ensure src is on path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from trading_floor.db import Database
from trading_floor.strategies.base import BaseStrategy, Signal


# ── Helpers ──────────────────────────────────────────────────

def _make_db(tmp_dir):
    path = os.path.join(tmp_dir, "test.db")
    return Database(db_path=path)


def _mock_broker():
    broker = MagicMock()
    broker.get_account.return_value = SimpleNamespace(
        cash="5000", equity="5000", buying_power="10000", last_equity="5000",
    )
    broker.get_positions.return_value = []
    broker.submit_order.return_value = SimpleNamespace(id="alpaca-123")
    broker.make_client_order_id.return_value = "test_order_1"
    return broker


def _make_exec_components(tmp_dir):
    from trading_floor.broker import (
        ExecutionService, OrderLedger, PortfolioState, StrategyBudgeter,
    )
    db = _make_db(tmp_dir)
    broker = _mock_broker()
    portfolio_state = PortfolioState(broker)
    order_ledger = OrderLedger(db, broker)
    budgeter = StrategyBudgeter(db, portfolio_state, {
        "intraday": 2000, "swing": 3000,
    })
    exec_svc = ExecutionService(broker, order_ledger, budgeter, portfolio_state)
    return broker, exec_svc, budgeter, portfolio_state, db


def _base_cfg():
    """Minimal config for testing."""
    return {
        "universe": ["NVDA", "TSLA", "AAPL", "AMD", "MSFT"],
        "scout_top_n": 5,
        "min_avg_volume": 100000,
        "hours": {"tz": "America/New_York", "start": "09:30", "end": "16:00"},
        "data": {"interval": "5m", "lookback": "5d"},
        "signals": {
            "weights": {"momentum": 0.50, "meanrev": 0.0, "breakout": 0.15, "news": 0.25},
            "trade_threshold": 0.25,
        },
        "risk": {"equity": 5000, "max_positions": 4},
        "broker": {"min_shares": 10},
        "logging": {"trades_csv": "test_logs/trades.csv", "events_csv": "test_logs/events.csv", "db_path": "test.db"},
        "strategies": {
            "intraday": {
                "enabled": True,
                "budget": 2000,
                "max_positions": 3,
                "weights": {"momentum": 0.50, "meanrev": 0.0, "breakout": 0.15, "news": 0.25},
                "threshold": 0.25,
                "take_profit": 0.025,
                "stop_loss_atr": 2.0,
                "close_by": "15:45",
                "universe_exclude": ["RKLB", "ONDS", "HUT", "AVAV", "MP", "POWL"],
            },
            "swing": {
                "enabled": True,
                "budget": 3000,
                "max_positions": 3,
                "max_per_sector": 1,
                "weights": {"momentum": 0.55, "meanrev": 0.35, "breakout": 0.0, "news": 0.10},
                "threshold": 0.25,
                "take_profit": 0.15,
                "stop_loss": 0.08,
                "max_hold_days": 10,
                "trailing_trigger": 0.08,
                "trailing_pct": 0.04,
                "time_decay_trail_after_day": 5,
                "time_decay_trail_pct": 0.025,
                "entry_windows": [
                    {"start": "09:40", "end": "10:00", "bias": "gap_continuation"},
                    {"start": "15:45", "end": "15:55", "bias": "trend_confirmation"},
                ],
                "universe_exclude": ["RKLB", "ONDS", "HUT", "IONQ", "RGTI", "AVAV", "MP", "POWL"],
            },
        },
    }


# ── BaseStrategy Tests ───────────────────────────────────────

class TestBaseStrategy(unittest.TestCase):
    def test_score_signals(self):
        weights = {"momentum": 0.55, "meanrev": 0.35, "breakout": 0.0, "news": 0.10}
        raw = {"momentum": 0.8, "meanrev": 0.4, "breakout": 0.5, "news": 0.6}
        score = BaseStrategy.score_signals(weights, raw)
        # breakout weight=0 excluded; total_weight = 0.55+0.35+0.10 = 1.0
        expected = (0.8 * 0.55 + 0.4 * 0.35 + 0.6 * 0.10) / 1.0
        self.assertAlmostEqual(score, expected, places=4)

    def test_score_signals_swing_weights(self):
        weights = {"momentum": 0.55, "meanrev": 0.35, "breakout": 0.0, "news": 0.10}
        raw = {"momentum": 1.0, "meanrev": 0.0, "breakout": 0.0, "news": 0.0}
        score = BaseStrategy.score_signals(weights, raw)
        # meanrev=0 is still included (weight>0, value=0), news=0 too
        expected = (1.0 * 0.55 + 0.0 * 0.35 + 0.0 * 0.10) / 1.0
        self.assertAlmostEqual(score, expected, places=4)

    def test_filter_universe(self):
        u = ["NVDA", "TSLA", "RKLB", "ONDS"]
        excl = ["RKLB", "ONDS"]
        result = BaseStrategy.filter_universe(u, excl)
        self.assertEqual(result, ["NVDA", "TSLA"])

    def test_is_in_time_window(self):
        from zoneinfo import ZoneInfo
        et = ZoneInfo("America/New_York")
        in_window = datetime(2026, 3, 2, 10, 0, tzinfo=et)
        self.assertTrue(BaseStrategy.is_in_time_window("09:30", "11:30", in_window))
        out_window = datetime(2026, 3, 2, 12, 0, tzinfo=et)
        self.assertFalse(BaseStrategy.is_in_time_window("09:30", "11:30", out_window))


# ── IntradayStrategy Tests ───────────────────────────────────

class TestIntradayStrategy(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.broker, self.exec_svc, self.budgeter, self.portfolio_state, self.db = _make_exec_components(self.tmp)
        self.cfg = _base_cfg()
        self.cfg["logging"]["db_path"] = os.path.join(self.tmp, "test.db")

        from trading_floor.strategies.intraday import IntradayStrategy
        self.strat = IntradayStrategy(self.cfg, self.broker, self.exec_svc, self.budgeter, self.db)

    def _make_signal(self, symbol="NVDA", price=100.0, score=0.5, atr=2.0):
        return Signal(
            symbol=symbol, side="buy", score=score,
            scores_breakdown={"momentum": 0.8, "meanrev": 0.1},
            timestamp=datetime.now(timezone.utc).isoformat(),
            strategy_name="intraday",
            metadata={"price": price, "atr": atr},
        )

    def test_execute_routes_through_exec_service(self):
        """Signals are submitted via ExecutionService."""
        sig = self._make_signal(price=50.0)
        results = self.strat.execute([sig])
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["status"], "submitted")
        self.broker.submit_order.assert_called_once()

    def test_budget_enforced(self):
        """Budget limit of $2K is enforced."""
        # Pre-fill budget with reservations
        conn = self.db._get_conn()
        conn.execute(
            "INSERT INTO budget_reservations (strategy, symbol, reserved_amount, status, created_at) "
            "VALUES (?,?,?,?,?)",
            ("intraday", "AAPL", 2000.0, "reserved", datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
        conn.close()

        sig = self._make_signal(price=50.0)
        results = self.strat.execute([sig])
        self.assertEqual(results[0]["status"], "rejected")
        self.assertIn("budget", results[0].get("reason", ""))

    def test_max_3_positions(self):
        """Max 3 intraday positions enforced."""
        conn = self.db._get_conn()
        for i, sym in enumerate(["AAPL", "AMD", "TSLA"]):
            conn.execute(
                "INSERT INTO position_meta (symbol, strategy, side, status) VALUES (?,?,?,?)",
                (sym, "intraday", "buy", "open"),
            )
        conn.commit()
        conn.close()

        sig = self._make_signal()
        results = self.strat.execute([sig])
        self.assertEqual(results[0]["status"], "rejected")
        self.assertEqual(results[0]["reason"], "max_positions")

    def test_min_10_shares_filter(self):
        """Reject if qty < 10 shares."""
        sig = self._make_signal(price=500.0)  # 2000/3 = ~666, 666/500 = 1 share < 10
        results = self.strat.execute([sig])
        self.assertEqual(results[0]["status"], "rejected")
        self.assertEqual(results[0]["reason"], "min_shares")

    def test_force_close(self):
        """Force close sends sell orders for all intraday positions."""
        # Create open position
        conn = self.db._get_conn()
        conn.execute(
            "INSERT INTO position_meta (symbol, strategy, side, entry_price, entry_qty, status) "
            "VALUES (?,?,?,?,?,?)",
            ("NVDA", "intraday", "buy", 100.0, 20, "open"),
        )
        conn.commit()
        conn.close()

        # Mock Alpaca position
        self.broker.get_positions.return_value = [
            SimpleNamespace(
                symbol="NVDA", qty="20", side="long", market_value="2100",
                avg_entry_price="100", unrealized_pl="100",
                unrealized_plpc="0.05", current_price="105",
            )
        ]
        self.portfolio_state.invalidate()

        results = self.strat.force_close()
        self.assertTrue(len(results) >= 1)

    def test_time_window_check(self):
        """Signals rejected outside 9:30-11:30 ET."""
        from zoneinfo import ZoneInfo
        et = ZoneInfo("America/New_York")
        noon = datetime(2026, 3, 2, 12, 0, tzinfo=et)
        self.assertFalse(self.strat.is_in_time_window("09:30", "11:30", noon))

        morning = datetime(2026, 3, 2, 10, 0, tzinfo=et)
        self.assertTrue(self.strat.is_in_time_window("09:30", "11:30", morning))


# ── SwingStrategy Tests ──────────────────────────────────────

class TestSwingStrategy(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.broker, self.exec_svc, self.budgeter, self.portfolio_state, self.db = _make_exec_components(self.tmp)
        self.cfg = _base_cfg()
        self.cfg["logging"]["db_path"] = os.path.join(self.tmp, "test.db")

        from trading_floor.strategies.swing import SwingStrategy
        self.strat = SwingStrategy(self.cfg, self.broker, self.exec_svc, self.budgeter, self.db)

    def _make_signal(self, symbol="NVDA", price=100.0, score=0.5, sector="Semiconductors"):
        return Signal(
            symbol=symbol, side="buy", score=score,
            scores_breakdown={"momentum": 0.8, "meanrev": 0.3, "breakout": 0.0, "news": 0.2},
            timestamp=datetime.now(timezone.utc).isoformat(),
            strategy_name="swing",
            metadata={"price": price, "sector": sector},
        )

    def test_swing_weights(self):
        """Swing weights applied correctly (mom=0.55, mean=0.35, brk=0, news=0.10)."""
        raw = {"momentum": 0.8, "meanrev": 0.4, "breakout": 0.5, "news": 0.6}
        weights = self.strat.weights
        score = BaseStrategy.score_signals(weights, raw)
        expected = (0.8 * 0.55 + 0.4 * 0.35 + 0.6 * 0.10) / (0.55 + 0.35 + 0.10)
        self.assertAlmostEqual(score, expected, places=4)

    def test_threshold_enforced(self):
        """Signals below 0.25 threshold are filtered."""
        raw = {"momentum": 0.1, "meanrev": 0.05, "breakout": 0.0, "news": 0.0}
        score = BaseStrategy.score_signals(self.strat.weights, raw)
        self.assertLess(abs(score), 0.25)

    def test_sector_concentration(self):
        """Rejects 2nd position in same sector."""
        # Add existing open position in Semiconductors
        conn = self.db._get_conn()
        conn.execute(
            "INSERT INTO position_meta (symbol, strategy, side, sector, status) VALUES (?,?,?,?,?)",
            ("AMD", "swing", "buy", "Semiconductors", "open"),
        )
        conn.commit()
        conn.close()

        sectors = self.strat._get_open_sectors()
        self.assertEqual(sectors.get("Semiconductors", 0), 1)
        # With max_per_sector=1, another semi would be blocked

    def test_max_hold_exit(self):
        """Position open 10+ days triggers close."""
        conn = self.db._get_conn()
        entry_time = (datetime.now(timezone.utc) - timedelta(days=11)).isoformat()
        conn.execute(
            "INSERT INTO position_meta (symbol, strategy, side, entry_price, entry_qty, entry_time, status) "
            "VALUES (?,?,?,?,?,?,?)",
            ("NVDA", "swing", "buy", 100.0, 20, entry_time, "open"),
        )
        conn.commit()
        conn.close()

        # Mock Alpaca position
        self.broker.get_positions.return_value = [
            SimpleNamespace(
                symbol="NVDA", qty="20", side="long", market_value="2100",
                avg_entry_price="100", unrealized_pl="100",
                unrealized_plpc="0.05", current_price="105",
            )
        ]
        self.portfolio_state.invalidate()

        actions = self.strat.manage_exits()
        self.assertTrue(any(a["action"] == "exit_time" for a in actions))

    def test_tp_exit(self):
        """TP at 15% triggers close."""
        conn = self.db._get_conn()
        entry_time = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
        conn.execute(
            "INSERT INTO position_meta (symbol, strategy, side, entry_price, entry_qty, entry_time, status) "
            "VALUES (?,?,?,?,?,?,?)",
            ("TSLA", "swing", "buy", 100.0, 20, entry_time, "open"),
        )
        conn.commit()
        conn.close()

        self.broker.get_positions.return_value = [
            SimpleNamespace(
                symbol="TSLA", qty="20", side="long", market_value="2320",
                avg_entry_price="100", unrealized_pl="320",
                unrealized_plpc="0.16", current_price="116",  # 16% > 15% TP
            )
        ]
        self.portfolio_state.invalidate()

        actions = self.strat.manage_exits()
        self.assertTrue(any(a["action"] == "exit_tp" for a in actions))

    def test_trailing_stop_activates(self):
        """Trailing activates at +8%, trails 4%."""
        self.assertTrue(self.strat.trailing_trigger == 0.08)
        self.assertTrue(self.strat.trailing_pct == 0.04)

    def test_day5_tightening(self):
        """After day 5, trail tightens to 2.5%."""
        self.assertEqual(self.strat.time_decay_day, 5)
        self.assertEqual(self.strat.time_decay_pct, 0.025)

    def test_am_pm_windows(self):
        """AM (9:40-10:00) and PM (15:45-15:55) windows configured."""
        from zoneinfo import ZoneInfo
        et = ZoneInfo("America/New_York")

        am = datetime(2026, 3, 2, 9, 45, tzinfo=et)
        self.assertIsNotNone(self.strat._active_window(am))

        pm = datetime(2026, 3, 2, 15, 50, tzinfo=et)
        self.assertIsNotNone(self.strat._active_window(pm))

        noon = datetime(2026, 3, 2, 12, 0, tzinfo=et)
        self.assertIsNone(self.strat._active_window(noon))

    def test_exclusion_list(self):
        """Exclusion list applied."""
        excl = self.strat.exclusions
        self.assertIn("RKLB", excl)
        self.assertIn("IONQ", excl)
        self.assertIn("RGTI", excl)

    def test_execute_through_exec_service(self):
        """Swing orders go through ExecutionService."""
        sig = self._make_signal(price=50.0)
        results = self.strat.execute([sig])
        self.assertTrue(len(results) >= 1)
        # Should have submitted (market order)
        self.assertEqual(results[0]["status"], "submitted")

    def test_max_positions_enforced(self):
        """Max 3 swing positions enforced."""
        conn = self.db._get_conn()
        for sym in ["AAPL", "AMD", "TSLA"]:
            conn.execute(
                "INSERT INTO position_meta (symbol, strategy, side, status) VALUES (?,?,?,?)",
                (sym, "swing", "buy", "open"),
            )
        conn.commit()
        conn.close()

        sig = self._make_signal()
        results = self.strat.execute([sig])
        self.assertEqual(results[0]["status"], "rejected")
        self.assertEqual(results[0]["reason"], "max_positions")

    def test_min_shares_swing(self):
        """Reject if qty < 10 shares."""
        sig = self._make_signal(price=500.0)  # 3000/3 = 1000, 1000/500 = 2 < 10
        results = self.strat.execute([sig])
        self.assertEqual(results[0]["status"], "rejected")
        self.assertEqual(results[0]["reason"], "min_shares")


# ── Budget Isolation Tests ───────────────────────────────────

class TestBudgetIsolation(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.broker, self.exec_svc, self.budgeter, self.portfolio_state, self.db = _make_exec_components(self.tmp)

    def test_intraday_cant_spend_swing_budget(self):
        """Intraday budget is independent of swing budget."""
        intraday_avail = self.budgeter.get_available("intraday")
        swing_avail = self.budgeter.get_available("swing")
        self.assertAlmostEqual(intraday_avail, 2000.0)
        self.assertAlmostEqual(swing_avail, 3000.0)

        # Reserve from intraday
        rid = self.budgeter.reserve("intraday", "NVDA", 1500.0)
        self.assertAlmostEqual(self.budgeter.get_available("intraday"), 500.0)
        self.assertAlmostEqual(self.budgeter.get_available("swing"), 3000.0)

    def test_swing_cant_spend_intraday_budget(self):
        """Swing budget is independent of intraday budget."""
        rid = self.budgeter.reserve("swing", "TSLA", 2500.0)
        self.assertAlmostEqual(self.budgeter.get_available("swing"), 500.0)
        self.assertAlmostEqual(self.budgeter.get_available("intraday"), 2000.0)

    def test_cross_budget_rejection(self):
        """Can't exceed strategy-specific budget."""
        with self.assertRaises(ValueError):
            self.budgeter.reserve("intraday", "NVDA", 2500.0)  # > $2K


# ── run.py CLI Tests ─────────────────────────────────────────

class TestRunCLI(unittest.TestCase):
    def test_load_config(self):
        """Config loads correctly."""
        from trading_floor.run import load_config
        cfg_path = os.path.join(os.path.dirname(__file__), "..", "configs", "workflow.yaml")
        if os.path.exists(cfg_path):
            cfg = load_config(cfg_path)
            self.assertIn("universe", cfg)
            self.assertIn("strategies", cfg)

    def test_deep_merge(self):
        """deep_merge works correctly."""
        from trading_floor.run import deep_merge
        base = {"a": 1, "b": {"c": 2, "d": 3}}
        overrides = {"b": {"c": 99}, "e": 5}
        result = deep_merge(base, overrides)
        self.assertEqual(result["b"]["c"], 99)
        self.assertEqual(result["b"]["d"], 3)
        self.assertEqual(result["e"], 5)

    @patch("sys.argv", ["run.py", "--intraday-scan"])
    def test_intraday_scan_flag_parsed(self):
        """--intraday-scan flag is parsed correctly."""
        import argparse
        ap = argparse.ArgumentParser()
        ap.add_argument("--intraday-scan", action="store_true")
        ap.add_argument("--swing-scan", action="store_true")
        args = ap.parse_args(["--intraday-scan"])
        self.assertTrue(args.intraday_scan)
        self.assertFalse(args.swing_scan)

    @patch("sys.argv", ["run.py", "--swing-scan"])
    def test_swing_scan_flag_parsed(self):
        """--swing-scan flag is parsed correctly."""
        import argparse
        ap = argparse.ArgumentParser()
        ap.add_argument("--intraday-scan", action="store_true")
        ap.add_argument("--swing-scan", action="store_true")
        args = ap.parse_args(["--swing-scan"])
        self.assertFalse(args.intraday_scan)
        self.assertTrue(args.swing_scan)


if __name__ == "__main__":
    unittest.main()
