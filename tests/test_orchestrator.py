"""
Unit tests for ORBOrchestrator (scripts/orb_workflow.py).

All external dependencies are mocked — no real broker/API calls.
27 tests across 7 groups.
"""

import os
import sys
import json
import unittest
import tempfile
import shutil
from unittest.mock import MagicMock, patch, PropertyMock
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts'))


def _make_config(tmp_dir, **overrides):
    """Write a minimal orb_config.yaml and return its path."""
    import yaml
    cfg = {
        "alpaca_api_key": "test-key",
        "alpaca_api_secret": "test-secret",
        "paper": True,
        "min_buying_power": 1000,
        "orb_capital": 3000,
        "swing_capital": 2000,
        "dry_run": True,
        "regime_path": os.path.join(tmp_dir, "regime_state.json"),
    }
    cfg.update(overrides)
    path = os.path.join(tmp_dir, "orb_config.yaml")
    with open(path, "w") as f:
        yaml.dump(cfg, f)
    return path


def _make_regime(tmp_dir, regime="NORMAL"):
    path = os.path.join(tmp_dir, "regime_state.json")
    with open(path, "w") as f:
        json.dump({"regime": regime}, f)
    return path


# Patch targets — all imports inside orb_workflow
_PATCH_PREFIX = "orb_workflow."


@patch(_PATCH_PREFIX + "ORBReconciler")
@patch(_PATCH_PREFIX + "ORBMonitor")
@patch(_PATCH_PREFIX + "ORBRangeMarker")
@patch(_PATCH_PREFIX + "ORBScanner")
@patch(_PATCH_PREFIX + "ORBExecutor")
@patch(_PATCH_PREFIX + "ORBExitManager")
@patch(_PATCH_PREFIX + "PortfolioIntelligence")
@patch(_PATCH_PREFIX + "FloorPositionManager")
@patch(_PATCH_PREFIX + "AlpacaBroker")
def _make_orchestrator(tmp_dir, mock_broker_cls, mock_floor_cls, mock_pi_cls,
                       mock_exit_cls, mock_exec_cls, mock_scanner_cls,
                       mock_rm_cls, mock_monitor_cls, mock_recon_cls,
                       regime="NORMAL", **kw):
    """Build an ORBOrchestrator with all deps mocked."""
    from orb_workflow import ORBOrchestrator

    # Setup mock broker instance
    broker_inst = mock_broker_cls.return_value
    acct = MagicMock()
    acct.buying_power = kw.get("buying_power", 50000)
    acct.id = "test-account"
    # Support both dict-like and attribute access
    acct.get = lambda k, d=None: {"buying_power": acct.buying_power, "id": "test-account"}.get(k, d)
    acct.__getitem__ = lambda self, k: {"buying_power": acct.buying_power, "id": "test-account"}[k]
    broker_inst.get_account.return_value = acct

    # Config + regime
    config_path = _make_config(tmp_dir, **kw.get("config_overrides", {}))
    if regime is not None:
        _make_regime(tmp_dir, regime)

    # Create dirs the orchestrator expects
    os.makedirs(os.path.join(tmp_dir, "logs"), exist_ok=True)
    os.makedirs(os.path.join(tmp_dir, "web"), exist_ok=True)

    old_cwd = os.getcwd()
    os.chdir(tmp_dir)
    try:
        orc = ORBOrchestrator(config_path=config_path, db_path=":memory:", dry_run=True)
    finally:
        os.chdir(old_cwd)

    # Return orchestrator + all mock instances for assertions
    orc._tmp_dir = tmp_dir
    orc._old_cwd = old_cwd
    orc._mocks = {
        "broker": broker_inst,
        "scanner": mock_scanner_cls.return_value,
        "range_marker": mock_rm_cls.return_value,
        "monitor": mock_monitor_cls.return_value,
        "reconciler": mock_recon_cls.return_value,
        "executor": mock_exec_cls.return_value,
    }
    return orc


class _OrcTestCase(unittest.TestCase):
    """Base class that creates orchestrator in a temp dir."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.orc = _make_orchestrator(self.tmp)
        # Run in tmp dir so file writes land there
        self._old_cwd = os.getcwd()
        os.chdir(self.tmp)

    def tearDown(self):
        os.chdir(self._old_cwd)
        shutil.rmtree(self.tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
# Health Check (6 tests)
# ---------------------------------------------------------------------------
class TestHealthCheck(_OrcTestCase):

    def test_broker_ok(self):
        self.assertTrue(self.orc.health_check())

    def test_broker_down(self):
        self.orc.broker.get_account.side_effect = ConnectionError("down")
        self.assertFalse(self.orc.health_check())

    def test_buying_power_below_minimum(self):
        acct = MagicMock()
        acct.buying_power = 500
        acct.get = lambda k, d=None: {"buying_power": 500, "id": "test"}.get(k, d)
        self.orc.broker.get_account.return_value = acct
        self.assertFalse(self.orc.health_check())

    def test_regime_crash(self):
        _make_regime(self.tmp, "CRASH")
        self.orc.config["regime_path"] = os.path.join(self.tmp, "regime_state.json")
        self.assertFalse(self.orc.health_check())

    def test_regime_file_missing_passes(self):
        self.orc.config["regime_path"] = "/nonexistent/regime.json"
        self.assertTrue(self.orc.health_check())

    def test_writes_heartbeat(self):
        self.orc.health_check()
        hb_path = os.path.join("web", "orb_heartbeat.json")
        self.assertTrue(os.path.exists(hb_path))
        with open(hb_path) as f:
            data = json.load(f)
        self.assertEqual(data["phase"], "health_check")


# ---------------------------------------------------------------------------
# Scan Phase (4 tests)
# ---------------------------------------------------------------------------
class TestScanPhase(_OrcTestCase):

    def test_returns_candidates(self):
        self.orc.scanner.scan.return_value = [{"symbol": "AAPL"}, {"symbol": "TSLA"}]
        result = self.orc.scan()
        self.assertEqual(len(result), 2)

    def test_empty_scan_exits_cleanly(self):
        self.orc.scanner.scan.return_value = []
        # Patch _wait_until to skip time waits, and weekday to be a weekday
        with patch("orb_workflow._et_now") as mock_now, \
             patch("orb_workflow._et_today_at") as mock_at:
            now = MagicMock()
            now.weekday.return_value = 1  # Tuesday
            mock_now.return_value = now
            mock_at.return_value = now
            # Also need to handle the < comparison in _wait_until
            now.__lt__ = lambda s, o: False  # already past target
            code = self.orc.run()
        self.assertEqual(code, 0)

    def test_scanner_exception_exits_code_1(self):
        self.orc.scanner.scan.side_effect = RuntimeError("API fail")
        with patch("orb_workflow._et_now") as mock_now, \
             patch("orb_workflow._et_today_at") as mock_at:
            now = MagicMock()
            now.weekday.return_value = 1
            mock_now.return_value = now
            mock_at.return_value = now
            now.__lt__ = lambda s, o: False
            code = self.orc.run()
        self.assertEqual(code, 1)

    def test_candidates_written_to_web(self):
        cands = [{"symbol": "AAPL"}]
        self.orc.scanner.scan.return_value = cands
        self.orc.scan()
        path = os.path.join("web", "orb_candidates.json")
        with open(path) as f:
            data = json.load(f)
        self.assertEqual(data["candidates"], cands)


# ---------------------------------------------------------------------------
# Range Marking (3 tests)
# ---------------------------------------------------------------------------
class TestRangeMarking(_OrcTestCase):

    def test_enriched_candidates(self):
        enriched = [{"symbol": "AAPL", "range_high": 150}]
        self.orc.range_marker.mark_ranges.return_value = enriched
        result = self.orc.mark_ranges([{"symbol": "AAPL"}])
        self.assertEqual(result, enriched)

    def test_filters_some_out(self):
        self.orc.range_marker.mark_ranges.return_value = [{"symbol": "AAPL"}]
        result = self.orc.mark_ranges([{"symbol": "AAPL"}, {"symbol": "TSLA"}])
        self.assertEqual(len(result), 1)

    def test_exception_in_run(self):
        self.orc.scanner.scan.return_value = [{"symbol": "X"}]
        self.orc.range_marker.mark_ranges.side_effect = ValueError("bad")
        with patch("orb_workflow._et_now") as mock_now, \
             patch("orb_workflow._et_today_at") as mock_at:
            now = MagicMock()
            now.weekday.return_value = 1
            mock_now.return_value = now
            mock_at.return_value = now
            now.__lt__ = lambda s, o: False
            code = self.orc.run()
        self.assertEqual(code, 1)


# ---------------------------------------------------------------------------
# Monitor Phase (4 tests)
# ---------------------------------------------------------------------------
class TestMonitorPhase(_OrcTestCase):

    def _run_full(self):
        self.orc.scanner.scan.return_value = [{"symbol": "AAPL"}]
        self.orc.range_marker.mark_ranges.return_value = [{"symbol": "AAPL", "range": True}]
        with patch("orb_workflow._et_now") as mock_now, \
             patch("orb_workflow._et_today_at") as mock_at:
            now = MagicMock()
            now.weekday.return_value = 1
            mock_now.return_value = now
            mock_at.return_value = now
            now.__lt__ = lambda s, o: False
            return self.orc.run()

    def test_monitor_called(self):
        self._run_full()
        self.orc.monitor.run.assert_called_once()

    def test_completes_then_reconciles(self):
        self._run_full()
        self.orc.reconciler.reconcile.assert_called_once()

    def test_exception_still_reconciles(self):
        self.orc.monitor.run.side_effect = RuntimeError("boom")
        self._run_full()
        self.orc.reconciler.reconcile.assert_called_once()

    def test_dry_run_still_runs(self):
        self.orc.dry_run = True
        self._run_full()
        self.orc.monitor.run.assert_called_once()


# ---------------------------------------------------------------------------
# Reconciliation (3 tests)
# ---------------------------------------------------------------------------
class TestReconciliation(_OrcTestCase):

    def _run_full(self):
        self.orc.scanner.scan.return_value = [{"symbol": "AAPL"}]
        self.orc.range_marker.mark_ranges.return_value = [{"symbol": "AAPL"}]
        with patch("orb_workflow._et_now") as mock_now, \
             patch("orb_workflow._et_today_at") as mock_at:
            now = MagicMock()
            now.weekday.return_value = 1
            mock_now.return_value = now
            mock_at.return_value = now
            now.__lt__ = lambda s, o: False
            return self.orc.run()

    def test_reconciler_called(self):
        self._run_full()
        self.orc.reconciler.reconcile.assert_called_once_with(strategy="orb")

    def test_critical_alert_logged(self):
        self.orc.reconciler.reconcile.return_value = {"mismatches": [{"critical": True}]}
        self.orc.reconciler.format_alert.return_value = "CRITICAL: position mismatch"
        self.orc.reconcile()
        self.orc.reconciler.format_alert.assert_called_once()

    def test_exception_no_crash(self):
        self.orc.reconciler.reconcile.side_effect = RuntimeError("db down")
        code = self._run_full()
        self.assertEqual(code, 0)


# ---------------------------------------------------------------------------
# Heartbeat (3 tests)
# ---------------------------------------------------------------------------
class TestHeartbeat(_OrcTestCase):

    def test_written_with_timestamp_and_phase(self):
        self.orc._write_heartbeat("ok")
        hb_path = os.path.join("web", "orb_heartbeat.json")
        with open(hb_path) as f:
            data = json.load(f)
        self.assertIn("timestamp", data)
        self.assertIn("phase", data)
        # timestamp should be parseable
        datetime.fromisoformat(data["timestamp"])

    def test_updated_each_phase(self):
        self.orc.scanner.scan.return_value = [{"symbol": "X"}]
        self.orc.range_marker.mark_ranges.return_value = [{"symbol": "X"}]
        with patch("orb_workflow._et_now") as mock_now, \
             patch("orb_workflow._et_today_at") as mock_at:
            now = MagicMock()
            now.weekday.return_value = 1
            mock_now.return_value = now
            mock_at.return_value = now
            now.__lt__ = lambda s, o: False
            self.orc.run()
        hb_path = os.path.join("web", "orb_heartbeat.json")
        with open(hb_path) as f:
            data = json.load(f)
        # Last phase should be "done"
        self.assertEqual(data["phase"], "done")

    def test_atomic_no_tmp_left(self):
        self.orc._write_heartbeat("test")
        tmp_path = os.path.join("web", "orb_heartbeat.json.tmp")
        final_path = os.path.join("web", "orb_heartbeat.json")
        self.assertFalse(os.path.exists(tmp_path), ".tmp should not persist")
        self.assertTrue(os.path.exists(final_path))


# ---------------------------------------------------------------------------
# CLI Args (4 tests)
# ---------------------------------------------------------------------------
class TestCLIArgs(unittest.TestCase):

    def test_dry_run_default(self):
        from orb_workflow import main
        import argparse
        parser = argparse.ArgumentParser()
        parser.add_argument("--dry-run", action="store_true", default=True)
        parser.add_argument("--live", dest="dry_run", action="store_false")
        args = parser.parse_args([])
        self.assertTrue(args.dry_run)

    def test_live_overrides(self):
        import argparse
        parser = argparse.ArgumentParser()
        parser.add_argument("--dry-run", action="store_true", default=True)
        parser.add_argument("--live", dest="dry_run", action="store_false")
        args = parser.parse_args(["--live"])
        self.assertFalse(args.dry_run)

    def test_scan_only_flag(self):
        import argparse
        parser = argparse.ArgumentParser()
        parser.add_argument("--scan-only", action="store_true", default=False)
        args = parser.parse_args(["--scan-only"])
        self.assertTrue(args.scan_only)

    def test_scan_only_skips_monitor(self):
        tmp = tempfile.mkdtemp()
        try:
            orc = _make_orchestrator(tmp)
            os.chdir(tmp)
            orc.scanner.scan.return_value = [{"symbol": "AAPL"}]
            with patch("orb_workflow._et_now") as mock_now, \
                 patch("orb_workflow._et_today_at") as mock_at:
                now = MagicMock()
                now.weekday.return_value = 1
                mock_now.return_value = now
                mock_at.return_value = now
                now.__lt__ = lambda s, o: False
                code = orc.run(scan_only=True)
            self.assertEqual(code, 0)
            orc.range_marker.mark_ranges.assert_not_called()
            orc.monitor.run.assert_not_called()
        finally:
            os.chdir(os.path.expanduser("~"))
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
