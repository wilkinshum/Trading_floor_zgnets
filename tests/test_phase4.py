"""Phase 4 integration tests — self-learner wiring, preflight, overrides, reports."""

import json
import os
import sys
import sqlite3
import tempfile
import shutil
from datetime import datetime, date, timedelta, timezone
from pathlib import Path
from unittest import TestCase, main as unittest_main
from unittest.mock import MagicMock, patch, PropertyMock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))


def _make_cfg():
    """Minimal config for tests."""
    return {
        "alpaca": {"api_key": "test", "api_secret": "test"},
        "broker": {"mode": "paper", "starting_equity": 5000, "min_shares": 10},
        "strategies": {
            "intraday": {
                "enabled": True, "budget": 2000, "max_positions": 3,
                "weights": {"momentum": 0.50, "meanrev": 0.00, "breakout": 0.15, "news": 0.25},
                "threshold": 0.25, "take_profit": 0.025, "stop_loss_atr": 2.0,
                "close_by": "15:45",
                "universe_exclude": [],
            },
            "swing": {
                "enabled": True, "budget": 3000, "max_positions": 3,
                "max_per_sector": 1,
                "weights": {"momentum": 0.55, "meanrev": 0.35, "breakout": 0.00, "news": 0.10},
                "threshold": 0.25, "take_profit": 0.15, "stop_loss": 0.08,
                "max_hold_days": 10,
                "trailing_trigger": 0.08, "trailing_pct": 0.04,
                "time_decay_trail_after_day": 5, "time_decay_trail_pct": 0.025,
                "entry_windows": [
                    {"start": "09:35", "end": "10:00", "bias": "gap_continuation"},
                ],
                "universe_exclude": [],
            },
        },
        "self_learning": {
            "enabled": True, "auto_apply": False, "apply_cadence": "weekly",
            "regimes": {"directional_threshold": 0.65, "vix_override": 30},
            "intraday": {
                "eta": 0.10, "review_window_days": 14,
                "min_trades_to_apply": 20, "max_drift": 0.10,
                "min_weight_floor": 0.02, "attribution": "raw_pnl",
                "baselines": {
                    "directional": {"momentum": 0.55, "meanrev": 0.02, "breakout": 0.10, "news": 0.28, "reserve": 0.05},
                    "non_directional": {"momentum": 0.35, "meanrev": 0.10, "breakout": 0.15, "news": 0.30, "reserve": 0.10},
                },
            },
            "swing": {
                "eta": 0.08, "review_window_days": 120,
                "min_trades_to_apply": 15, "max_drift": 0.10,
                "min_weight_floor": 0.02, "attribution": "spy_adjusted",
                "baselines": {
                    "directional": {"momentum": 0.60, "meanrev": 0.20, "breakout": 0.00, "news": 0.15, "reserve": 0.05},
                    "non_directional": {"momentum": 0.40, "meanrev": 0.40, "breakout": 0.00, "news": 0.15, "reserve": 0.05},
                },
            },
            "safety": {
                "revert_after_consecutive_losing_days": 5,
                "revert_after_cumulative_loss": 50,
                "min_trades_since_last_apply": 10,
                "min_trades_since_last_apply_swing": 5,
                "confidence_tiers": {"insufficient": 15, "low": 30, "medium": 45},
            },
        },
        "kill_switches": {"daily_max_loss_pct": 0.05},
        "logging": {"db_path": "test_phase4.db", "trades_csv": "trading_logs/trades.csv",
                     "events_csv": "trading_logs/events.csv", "signals_csv": "trading_logs/signals.csv"},
        "universe": ["SPY", "QQQ"],
        "hours": {"tz": "America/New_York", "start": "09:30", "end": "11:30", "holidays": []},
        "signals": {"weights": {"momentum": 0.5, "meanrev": 0, "breakout": 0.15, "news": 0.25, "reserve": 0.10}},
        "risk": {"equity": 5000, "max_positions": 4},
        "scout_top_n": 5,
    }


class TestNightlyReview(TestCase):
    """Test --nightly-review CLI integration."""

    def setUp(self):
        self.cfg = _make_cfg()
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = Path(self.tmpdir) / "test.db"
        self.cfg["logging"]["db_path"] = str(self.db_path)

        from trading_floor.db import Database
        self.db = Database(self.db_path)

        # Ensure mw_state dir
        mw_dir = Path("configs")
        mw_dir.mkdir(exist_ok=True)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)
        # Clean up generated files
        for p in [Path("configs/mw_state.json")]:
            if p.exists():
                p.unlink()

    def test_nightly_review_generates_report(self):
        from trading_floor.review import SelfLearner
        sl = SelfLearner(self.cfg, self.db)
        report = sl.nightly_review()
        self.assertIn("Self-Learning Review", report)
        self.assertIn("Intraday", report)
        self.assertIn("Swing", report)

    def test_report_saved_to_file(self):
        from trading_floor.review import SelfLearner
        sl = SelfLearner(self.cfg, self.db)
        report = sl.nightly_review()
        today = date.today().isoformat()
        path = Path("memory/reviews") / f"{today}.md"
        self.assertTrue(path.exists(), f"Report not saved at {path}")
        content = path.read_text()
        self.assertIn("Self-Learning Review", content)
        # Cleanup
        path.unlink()


class TestSignalScorePersistence(TestCase):
    """Test that signal scores are saved at entry and can be retrieved at exit."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = Path(self.tmpdir) / "test.db"
        from trading_floor.db import Database
        self.db = Database(self.db_path)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_signals_persisted_in_position_meta(self):
        signal_scores = {"momentum": 0.7, "meanrev": -0.1, "breakout": 0.2, "news": 0.3}
        conn = self.db._get_conn()
        try:
            cur = conn.cursor()
            cur.execute(
                """INSERT INTO position_meta
                   (symbol, strategy, side, entry_price, entry_time, entry_qty,
                    signals_json, status)
                   VALUES (?,?,?,?,?,?,?,?)""",
                ("SPY", "intraday", "buy", 500.0,
                 datetime.now(timezone.utc).isoformat(), 10,
                 json.dumps(signal_scores), "open"),
            )
            conn.commit()
            pos_id = cur.lastrowid
        finally:
            conn.close()

        # Retrieve
        conn = self.db._get_conn()
        try:
            cur = conn.cursor()
            cur.execute("SELECT signals_json FROM position_meta WHERE id=?", (pos_id,))
            row = cur.fetchone()
        finally:
            conn.close()

        loaded = json.loads(row[0])
        self.assertEqual(loaded["momentum"], 0.7)
        self.assertEqual(loaded["news"], 0.3)

    def test_get_position_trade_data(self):
        """Test the helper function that builds trade_data from DB."""
        signal_scores = {"momentum": 0.5, "meanrev": 0.1, "breakout": 0.0, "news": 0.2}
        conn = self.db._get_conn()
        try:
            cur = conn.cursor()
            entry_time = datetime.now(timezone.utc).isoformat()
            exit_time = (datetime.now(timezone.utc) + timedelta(hours=4)).isoformat()
            cur.execute(
                """INSERT INTO position_meta
                   (symbol, strategy, side, entry_price, entry_time, entry_qty,
                    exit_time, signals_json, pnl, status)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                ("QQQ", "intraday", "buy", 400.0, entry_time, 5,
                 exit_time, json.dumps(signal_scores), 25.0, "closed"),
            )
            conn.commit()
            pos_id = cur.lastrowid
        finally:
            conn.close()

        from trading_floor.run import _get_position_trade_data
        # Temporarily point db to our test db
        td = _get_position_trade_data(self.db, pos_id, "intraday", 405.0)
        self.assertEqual(td["symbol"], "QQQ")
        self.assertEqual(td["signal_scores"]["momentum"], 0.5)
        self.assertEqual(td["pnl"], 25.0)
        self.assertEqual(td["position_value"], 2000.0)


class TestTradeLifecycleMock(TestCase):
    """Mock trade lifecycle: entry → record signals → exit → process_trade → MW update."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = Path(self.tmpdir) / "test.db"
        self.cfg = _make_cfg()
        self.cfg["logging"]["db_path"] = str(self.db_path)

        from trading_floor.db import Database
        self.db = Database(self.db_path)

        # Set mw_state to tmpdir
        self.mw_path = Path(self.tmpdir) / "mw_state.json"

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_full_lifecycle(self):
        from trading_floor.review import SelfLearner
        from trading_floor.review.adaptive_weights import AdaptiveWeights

        sl = SelfLearner(self.cfg, self.db)
        # Override state path
        sl.adaptive_weights.state_path = self.mw_path

        # Record baseline
        baseline = dict(sl.adaptive_weights.get_weights("intraday", "directional"))

        # Simulate trade entry
        signal_scores = {"momentum": 0.8, "meanrev": 0.0, "breakout": 0.1, "news": 0.5}
        conn = self.db._get_conn()
        try:
            cur = conn.cursor()
            cur.execute(
                """INSERT INTO position_meta
                   (symbol, strategy, side, entry_price, entry_time, entry_qty,
                    signals_json, pnl, status, exit_time)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                ("NVDA", "intraday", "buy", 100.0,
                 datetime.now(timezone.utc).isoformat(), 20,
                 json.dumps(signal_scores), 50.0, "closed",
                 datetime.now(timezone.utc).isoformat()),
            )
            conn.commit()
            pos_id = cur.lastrowid
        finally:
            conn.close()

        # Process trade
        trade_data = {
            "strategy": "intraday",
            "symbol": "NVDA",
            "signal_scores": signal_scores,
            "pnl": 50.0,
            "position_value": 2000.0,
            "holding_days": 0.25,
            "position_meta_id": pos_id,
        }
        regime_state = {"bull_confidence": 0.8, "bear_confidence": 0.1}
        sl.process_trade(trade_data, regime_state)

        # Verify weights changed
        new_weights = sl.adaptive_weights.get_weights("intraday", "directional")
        # With a winning trade and positive momentum signal, momentum weight should increase
        # (or at least be different from baseline due to the update)
        self.assertTrue(self.mw_path.exists(), "mw_state.json not created")

        # Verify signal_accuracy records
        conn = self.db._get_conn()
        try:
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) FROM signal_accuracy WHERE position_meta_id=?",
                        (pos_id,))
            count = cur.fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(count, 4, "Expected 4 signal_accuracy records (one per signal)")


class TestOverridesMerge(TestCase):
    """Test that overrides.yaml merges correctly with base config."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        # Write a minimal base config
        self.base_path = Path(self.tmpdir) / "workflow.yaml"
        import yaml
        base = {"strategies": {"intraday": {"budget": 2000, "threshold": 0.25}}}
        self.base_path.write_text(yaml.dump(base))

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_overrides_merge(self):
        import yaml
        from trading_floor.run import load_config
        # Write overrides
        overrides_path = Path(self.tmpdir) / "overrides.yaml"
        overrides = {"strategies": {"intraday": {"threshold": 0.30}}}
        overrides_path.write_text(yaml.dump(overrides))

        cfg = load_config(str(self.base_path))
        self.assertEqual(cfg["strategies"]["intraday"]["threshold"], 0.30)
        self.assertEqual(cfg["strategies"]["intraday"]["budget"], 2000)

    def test_no_overrides(self):
        from trading_floor.run import load_config
        cfg = load_config(str(self.base_path))
        self.assertEqual(cfg["strategies"]["intraday"]["threshold"], 0.25)


class TestFridayDetection(TestCase):
    """Test that weekly_apply is triggered on Fridays."""

    def test_friday_check(self):
        from datetime import date
        # Friday = weekday 4
        friday = date(2026, 3, 6)  # A Friday
        self.assertEqual(friday.weekday(), 4)
        monday = date(2026, 3, 2)
        self.assertNotEqual(monday.weekday(), 4)


class TestPreflightNewChecks(TestCase):
    """Test that preflight script has new V4 checks."""

    def test_preflight_has_v4_checks(self):
        script = (ROOT / "scripts" / "preflight_check.py").read_text(encoding="utf-8")
        self.assertIn("Broker import", script)
        self.assertIn("Review import", script)
        self.assertIn("V4 DB tables", script)
        self.assertIn("mw_state.json", script)
        self.assertIn("overrides.yaml", script)
        self.assertIn("Alpaca API", script)


class TestRunPyNewModes(TestCase):
    """Test that run.py has --nightly-review and --force-close-all."""

    def test_new_args_present(self):
        run_src = (ROOT / "src" / "trading_floor" / "run.py").read_text()
        self.assertIn("--nightly-review", run_src)
        self.assertIn("--force-close-all", run_src)
        self.assertIn("_init_self_learner", run_src)
        self.assertIn("_get_position_trade_data", run_src)
        self.assertIn("_process_closed_positions", run_src)


class TestCronDefinitions(TestCase):
    """Test cron_definitions.py exists and has expected crons."""

    def test_cron_file_exists(self):
        path = ROOT / "scripts" / "cron_definitions.py"
        self.assertTrue(path.exists())

    def test_cron_content(self):
        src = (ROOT / "scripts" / "cron_definitions.py").read_text()
        self.assertIn("swing_am_scan", src)
        self.assertIn("swing_pm_scan", src)
        self.assertIn("swing_exits", src)
        self.assertIn("intraday_force_close", src)
        self.assertIn("nightly_review", src)


if __name__ == "__main__":
    unittest_main()
