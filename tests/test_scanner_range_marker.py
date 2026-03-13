"""
Unit tests for ORBScanner + ORBRangeMarker (Phase 4).
Mocks all Alpaca API calls.
"""
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))


# ── Mock helpers ─────────────────────────────────────────────

def make_bar(high, low, close, volume, timestamp=None):
    return SimpleNamespace(high=high, low=low, close=close, open=close, volume=volume,
                           timestamp=timestamp or datetime.now())

def make_snapshot(price, volume, prev_close, bid=None, ask=None):
    lt = SimpleNamespace(price=price, size=100) if price else None
    db = SimpleNamespace(volume=volume) if volume else None
    pdb = SimpleNamespace(close=prev_close) if prev_close else None
    mb = SimpleNamespace(close=price, volume=volume) if price else None
    lq = None
    if bid and ask:
        lq = SimpleNamespace(bid_price=bid, ask_price=ask)
    return SimpleNamespace(latest_trade=lt, daily_bar=db, previous_daily_bar=pdb,
                           minute_bar=mb, latest_quote=lq)


class MockBarsResponse:
    """Dict-like bars response."""
    def __init__(self, data: dict):
        self._data = data
    def get(self, key, default=None):
        return self._data.get(key, default)


class MockSnapshotResponse:
    def __init__(self, data: dict):
        self._data = data
    def get(self, key, default=None):
        return self._data.get(key, default)


# ── Scanner Tests ────────────────────────────────────────────

class TestORBScanner(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.orig_cwd = os.getcwd()
        os.chdir(self.tmpdir)
        Path("web").mkdir(exist_ok=True)

        self.config = {
            "gap_min_pct": 2.0,
            "gap_max_pct": 5.0,
            "premarket_vol_min": 300_000,
            "atr_min": 1.50,
            "price_min": 10,
            "price_max": 500,
            "avg_daily_vol_min": 1_000_000,
            "max_candidates": 8,
        }
        self.mock_client = MagicMock()

    def tearDown(self):
        os.chdir(self.orig_cwd)
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _make_scanner(self):
        from trading_floor.strategies.orb.scanner import ORBScanner
        return ORBScanner(config=self.config, data_client=self.mock_client)

    def _setup_daily_bars(self, symbol, closes, volumes=None, highs=None, lows=None):
        """Create mock daily bars for a symbol."""
        n = len(closes)
        if not volumes:
            volumes = [2_000_000] * n
        if not highs:
            highs = [c + 1.0 for c in closes]
        if not lows:
            lows = [c - 1.0 for c in closes]
        bars = [make_bar(highs[i], lows[i], closes[i], volumes[i]) for i in range(n)]
        return bars

    def test_gap_within_range_passes(self):
        scanner = self._make_scanner()
        # Mock snapshot: price=103, prev_close=100 → 3% gap
        self.mock_client.get_stock_snapshot.return_value = MockSnapshotResponse({
            "AAPL": make_snapshot(103, 500_000, 100),
        })
        # Mock daily bars with 20 bars for ATR + avg vol
        bars = self._setup_daily_bars("AAPL", [100 + i * 0.5 for i in range(20)])
        self.mock_client.get_stock_bars.return_value = MockBarsResponse({"AAPL": bars})

        with patch.object(scanner, '_get_universe', return_value=["AAPL"]):
            results = scanner.scan()

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["symbol"], "AAPL")
        self.assertAlmostEqual(results[0]["gap_pct"], 3.0, places=0)

    def test_gap_outside_range_filtered(self):
        scanner = self._make_scanner()
        # 10% gap — exceeds max 5%
        self.mock_client.get_stock_snapshot.return_value = MockSnapshotResponse({
            "AAPL": make_snapshot(110, 500_000, 100),
        })
        bars = self._setup_daily_bars("AAPL", [100] * 20)
        self.mock_client.get_stock_bars.return_value = MockBarsResponse({"AAPL": bars})

        with patch.object(scanner, '_get_universe', return_value=["AAPL"]):
            results = scanner.scan()
        self.assertEqual(len(results), 0)

    def test_premarket_vol_too_low_filtered(self):
        scanner = self._make_scanner()
        # Volume 100K < 300K minimum
        self.mock_client.get_stock_snapshot.return_value = MockSnapshotResponse({
            "AAPL": make_snapshot(103, 100_000, 100),
        })
        bars = self._setup_daily_bars("AAPL", [100] * 20)
        self.mock_client.get_stock_bars.return_value = MockBarsResponse({"AAPL": bars})

        with patch.object(scanner, '_get_universe', return_value=["AAPL"]):
            results = scanner.scan()
        self.assertEqual(len(results), 0)

    def test_price_outside_range_filtered(self):
        scanner = self._make_scanner()
        # Price $5 — below $10 minimum
        self.mock_client.get_stock_snapshot.return_value = MockSnapshotResponse({
            "AAPL": make_snapshot(5.15, 500_000, 5.0),
        })
        bars = self._setup_daily_bars("AAPL", [5.0] * 20)
        self.mock_client.get_stock_bars.return_value = MockBarsResponse({"AAPL": bars})

        with patch.object(scanner, '_get_universe', return_value=["AAPL"]):
            results = scanner.scan()
        self.assertEqual(len(results), 0)

    def test_ranking_higher_score_first(self):
        scanner = self._make_scanner()
        # Two symbols: MSFT 4% gap (higher score), AAPL 2.5% gap
        self.mock_client.get_stock_snapshot.return_value = MockSnapshotResponse({
            "AAPL": make_snapshot(102.5, 500_000, 100),
            "MSFT": make_snapshot(104, 600_000, 100),
        })
        bars_a = self._setup_daily_bars("AAPL", [100 + i * 0.5 for i in range(20)])
        bars_m = self._setup_daily_bars("MSFT", [100 + i * 0.5 for i in range(20)])
        self.mock_client.get_stock_bars.return_value = MockBarsResponse({
            "AAPL": bars_a, "MSFT": bars_m,
        })

        with patch.object(scanner, '_get_universe', return_value=["AAPL", "MSFT"]):
            results = scanner.scan()

        self.assertEqual(len(results), 2)
        self.assertEqual(results[0]["symbol"], "MSFT")  # Higher gap = higher score

    def test_empty_snapshot_returns_empty(self):
        scanner = self._make_scanner()
        self.mock_client.get_stock_snapshot.return_value = MockSnapshotResponse({})
        self.mock_client.get_stock_bars.return_value = MockBarsResponse({})

        with patch.object(scanner, '_get_universe', return_value=["AAPL"]):
            results = scanner.scan()
        self.assertEqual(len(results), 0)

    def test_output_json_written(self):
        scanner = self._make_scanner()
        self.mock_client.get_stock_snapshot.return_value = MockSnapshotResponse({
            "AAPL": make_snapshot(103, 500_000, 100),
        })
        bars = self._setup_daily_bars("AAPL", [100 + i * 0.5 for i in range(20)])
        self.mock_client.get_stock_bars.return_value = MockBarsResponse({"AAPL": bars})

        with patch.object(scanner, '_get_universe', return_value=["AAPL"]):
            scanner.scan()

        out = Path("web/orb_candidates.json")
        self.assertTrue(out.exists())
        data = json.loads(out.read_text())
        self.assertIn("candidates", data)
        self.assertIn("timestamp", data)


# ── Range Marker Tests ───────────────────────────────────────

class TestORBRangeMarker(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.orig_cwd = os.getcwd()
        os.chdir(self.tmpdir)
        Path("web").mkdir(exist_ok=True)

        self.config = {
            "entry": {"spread_max_pct": 0.003},
        }
        self.mock_client = MagicMock()

    def tearDown(self):
        os.chdir(self.orig_cwd)
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _make_marker(self):
        from trading_floor.strategies.orb.range_marker import ORBRangeMarker
        return ORBRangeMarker(config=self.config, data_client=self.mock_client)

    def _candidate(self, symbol="AAPL", **kw):
        base = {"symbol": symbol, "gap_pct": 3.0, "gap_dir": "up", "premarket_vol": 500_000,
                "atr14": 2.5, "prev_close": 100, "sector": "Technology",
                "sector_alignment": 1.5, "score": 10.0, "reason": "passed_filters"}
        base.update(kw)
        return base

    def test_correct_range_from_bars(self):
        marker = self._make_marker()
        # 15 bars: high ~152, low ~150 → MM ~$2, ~1.3% of $151 midprice
        bars = [make_bar(152, 150, 151, 10000) for i in range(15)]
        self.mock_client.get_stock_bars.return_value = MockBarsResponse({"AAPL": bars})
        self.mock_client.get_stock_snapshot.return_value = MockSnapshotResponse({
            "AAPL": make_snapshot(151, 500_000, 148, bid=150.9, ask=151.0),
        })

        results = marker.mark_ranges([self._candidate()])
        self.assertEqual(len(results), 1)
        self.assertGreater(results[0]["range_high"], results[0]["range_low"])
        self.assertEqual(results[0]["bar_count"], 15)

    def test_measured_move_calculation(self):
        marker = self._make_marker()
        # Fixed bars: high=152, low=150 → MM = 2.0
        bars = [make_bar(152, 150, 151, 10000) for _ in range(15)]
        self.mock_client.get_stock_bars.return_value = MockBarsResponse({"AAPL": bars})
        self.mock_client.get_stock_snapshot.return_value = MockSnapshotResponse({
            "AAPL": make_snapshot(151, 500_000, 148, bid=150.9, ask=151.0),
        })

        results = marker.mark_ranges([self._candidate()])
        self.assertEqual(len(results), 1)
        self.assertAlmostEqual(results[0]["measured_move"], 2.0, places=2)

    def test_mm_too_small_filtered(self):
        marker = self._make_marker()
        # Narrow range: high=100.5, low=100.0 → MM = 0.5 < $1.50
        bars = [make_bar(100.5, 100.0, 100.2, 10000) for _ in range(15)]
        self.mock_client.get_stock_bars.return_value = MockBarsResponse({"AAPL": bars})
        self.mock_client.get_stock_snapshot.return_value = MockSnapshotResponse({
            "AAPL": make_snapshot(100.3, 500_000, 100, bid=100.2, ask=100.3),
        })

        results = marker.mark_ranges([self._candidate()])
        self.assertEqual(len(results), 0)

    def test_mm_too_large_filtered(self):
        marker = self._make_marker()
        # Wide range: high=115, low=100 on $107.50 midprice → 14% > 3%
        bars = [make_bar(115, 100, 107, 10000) for _ in range(15)]
        self.mock_client.get_stock_bars.return_value = MockBarsResponse({"AAPL": bars})
        self.mock_client.get_stock_snapshot.return_value = MockSnapshotResponse({
            "AAPL": make_snapshot(107, 500_000, 100, bid=106.9, ask=107.1),
        })

        results = marker.mark_ranges([self._candidate()])
        self.assertEqual(len(results), 0)

    def test_range_too_narrow_filtered(self):
        marker = self._make_marker()
        # Range: 100.1 - 100.0 = 0.1 → 0.1% < 0.3%
        # But also < $1.50 so will be caught by mm_too_small first
        bars = [make_bar(100.1, 100.0, 100.05, 10000) for _ in range(15)]
        self.mock_client.get_stock_bars.return_value = MockBarsResponse({"AAPL": bars})
        self.mock_client.get_stock_snapshot.return_value = MockSnapshotResponse({
            "AAPL": make_snapshot(100.05, 500_000, 100, bid=100.0, ask=100.1),
        })

        results = marker.mark_ranges([self._candidate()])
        self.assertEqual(len(results), 0)  # Filtered (too small)

    def test_all_filters_pass(self):
        marker = self._make_marker()
        # Good range: 102-100 = $2.0, ~2% of $101 midprice, spread $0.10 = 0.1%
        bars = [make_bar(102, 100, 101, 10000) for _ in range(15)]
        self.mock_client.get_stock_bars.return_value = MockBarsResponse({"AAPL": bars})
        self.mock_client.get_stock_snapshot.return_value = MockSnapshotResponse({
            "AAPL": make_snapshot(101, 500_000, 100, bid=100.95, ask=101.05),
        })

        results = marker.mark_ranges([self._candidate()])
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0]["meets_post_range_filters"])

    def test_missing_bars_excluded(self):
        marker = self._make_marker()
        self.mock_client.get_stock_bars.return_value = MockBarsResponse({})
        self.mock_client.get_stock_snapshot.return_value = MockSnapshotResponse({})

        # Disable retry delay for test speed
        marker.bar_retry_delay = 0

        results = marker.mark_ranges([self._candidate()])
        self.assertEqual(len(results), 0)

    def test_output_json_written(self):
        marker = self._make_marker()
        bars = [make_bar(102, 100, 101, 10000) for _ in range(15)]
        self.mock_client.get_stock_bars.return_value = MockBarsResponse({"AAPL": bars})
        self.mock_client.get_stock_snapshot.return_value = MockSnapshotResponse({
            "AAPL": make_snapshot(101, 500_000, 100, bid=100.95, ask=101.05),
        })

        marker.mark_ranges([self._candidate()])
        out = Path("web/orb_ranges.json")
        self.assertTrue(out.exists())
        data = json.loads(out.read_text())
        self.assertIn("candidates", data)


if __name__ == "__main__":
    unittest.main()
