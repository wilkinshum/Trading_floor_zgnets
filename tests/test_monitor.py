"""Tests for ORB Monitor — Phase 7."""

import os
import sys
import json
import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch
from dataclasses import asdict

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from trading_floor.strategies.orb.monitor import (
    ORBState, CandidateState, ORBMonitor,
    is_consolidating, is_breakout, is_retest,
)


class MockCandle:
    """Mock 1-min bar."""
    def __init__(self, o, c, h, l, v, ts=None):
        self.open = o
        self.close = c
        self.high = h
        self.low = l
        self.volume = v
        self.timestamp = ts or datetime.now(timezone.utc)


def _make_config():
    return {
        "entry": {
            "consolidation": {
                "min_candles": 3,
                "range_contraction_threshold": 0.9,
                "band_max_pct_of_mm": 0.35,
                "band_max_pct_of_price": 0.006,
                "location_pct": 0.3,
            },
            "breakout": {
                "min_extension_pct": 0.1,
                "min_vol_multiple": 1.2,
            },
            "retest": {
                "proximity_pct_price": 0.005,
                "proximity_pct_mm_min": 0.20,
                "proximity_pct_mm_max": 0.25,
                "body_ratio_min": 0.6,
            },
            "max_retries": 1,
            "spread_max_pct": 0.003,
            "data_freshness_sec": 15,
            "max_wave1_time": "10:30",
            "max_wave23_time": "11:00",
        },
        "exit": {
            "partial_pct": 0.5,
            "partial_target_pct_of_mm": 0.5,
            "time_stop": "11:30",
        },
        "risk": {
            "max_positions": 2,
            "max_total_positions": 5,
            "max_waves_per_stock": 3,
            "daily_loss_cap": 120,
            "flash_crash_cap": 60,
        },
        "timing": {"force_close_time": "11:30"},
        "execution": {"entry_slip_cents": 3},
    }


def _make_monitor(**overrides):
    broker = overrides.get("broker", MagicMock())
    executor = overrides.get("executor", MagicMock())
    exit_manager = overrides.get("exit_manager", MagicMock())
    portfolio_intel = overrides.get("portfolio_intel", MagicMock())
    floor_manager = overrides.get("floor_manager", MagicMock())
    config = overrides.get("config", _make_config())
    mon = ORBMonitor(broker, executor, exit_manager, portfolio_intel,
                     floor_manager, config, ":memory:")
    return mon, broker, executor, exit_manager, portfolio_intel, floor_manager


def _make_bars_consolidating(n=12):
    """Generate bars that show contraction near $100 ORB high."""
    bars = []
    base_vol = 5000
    for i in range(n):
        # Contracting range, declining volume, near $100
        rng = max(0.05, 0.5 - i * 0.04)
        mid = 99.8
        vol = base_vol - i * 300
        bars.append(MockCandle(mid - rng/4, mid + rng/4, mid + rng/2,
                               mid - rng/2, max(100, vol)))
    return bars


def _make_candidate(sym="AAPL", state=ORBState.WATCHING_FOR_CONSOLIDATION.value,
                    **kw):
    defaults = dict(
        symbol=sym, direction="long", state=state,
        orb_high=100.0, orb_low=98.0, measured_move=2.0,
        sector="Technology",
    )
    defaults.update(kw)
    return CandidateState(**defaults)


# ── Detection: is_consolidating ──────────────────────────────

class TestIsConsolidating(unittest.TestCase):

    def test_happy_path_long(self):
        """Contracting candles near ORB high → True."""
        bars = _make_bars_consolidating(12)
        ok, bh, bl = is_consolidating(
            bars, 100.0, 98.0, 2.0, "long",
            _make_config()["entry"]
        )
        self.assertTrue(ok)
        self.assertIsNotNone(bh)
        self.assertIsNotNone(bl)
        self.assertGreater(bh, bl)

    def test_too_few_candles(self):
        """Fewer than min required → False."""
        bars = [MockCandle(100, 100.1, 100.2, 99.9, 1000) for _ in range(5)]
        ok, _, _ = is_consolidating(bars, 100.0, 98.0, 2.0, "long",
                                     _make_config()["entry"])
        self.assertFalse(ok)

    def test_no_contraction(self):
        """Expanding ranges → False."""
        bars = []
        for i in range(12):
            rng = 0.1 + i * 0.1  # expanding
            bars.append(MockCandle(100, 100, 100 + rng, 100 - rng, 5000 + i * 100))
        ok, _, _ = is_consolidating(bars, 100.0, 98.0, 2.0, "long",
                                     _make_config()["entry"])
        self.assertFalse(ok)

    def test_too_wide_band(self):
        """Band > 0.35*mm → False."""
        bars = []
        for i in range(12):
            # Wide band: 1.0 > 0.35 * 2.0 = 0.70
            bars.append(MockCandle(100, 100.2, 100.8, 99.8, 5000 - i * 300))
        ok, _, _ = is_consolidating(bars, 100.0, 98.0, 2.0, "long",
                                     _make_config()["entry"])
        self.assertFalse(ok)

    def test_wrong_location_long(self):
        """Long but band far below ORB high → False."""
        bars = _make_bars_consolidating(12)
        # Set ORB high much higher than where bars are
        ok, _, _ = is_consolidating(bars, 110.0, 98.0, 12.0, "long",
                                     _make_config()["entry"])
        self.assertFalse(ok)

    def test_short_near_orb_low(self):
        """Short direction: consolidation near ORB low → True."""
        bars = []
        base_vol = 5000
        for i in range(12):
            rng = max(0.05, 0.5 - i * 0.04)
            mid = 98.2  # near orb_low of 98.0
            vol = base_vol - i * 300
            bars.append(MockCandle(mid - rng/4, mid + rng/4, mid + rng/2,
                                   mid - rng/2, max(100, vol)))
        ok, _, _ = is_consolidating(bars, 100.0, 98.0, 2.0, "short",
                                     _make_config()["entry"])
        self.assertTrue(ok)


# ── Detection: is_breakout ───────────────────────────────────

class TestIsBreakout(unittest.TestCase):

    def test_long_success(self):
        """Close above band + extension + volume → True."""
        candle = MockCandle(100.0, 100.7, 100.8, 100.0, 3000)
        result = is_breakout(candle, 100.5, 100.0, 3000, 2000, "long",
                             _make_config()["entry"])
        self.assertTrue(result)

    def test_long_no_volume(self):
        """Close above band but volume < 1.2x SMA → False."""
        candle = MockCandle(100.0, 100.7, 100.8, 100.0, 1000)
        result = is_breakout(candle, 100.5, 100.0, 1000, 2000, "long",
                             _make_config()["entry"])
        self.assertFalse(result)

    def test_short_success(self):
        """Close below band - extension + volume → True."""
        # band_lo=100.0, band_width=0.5, extension=0.1*0.5=0.05
        # Need close < 100.0 - 0.05 = 99.95
        candle = MockCandle(100.0, 99.3, 100.0, 99.2, 3000)
        result = is_breakout(candle, 100.5, 100.0, 3000, 2000, "short",
                             _make_config()["entry"])
        self.assertTrue(result)  # 99.3 < 99.95 ✓

    def test_inside_band(self):
        """Close inside band → False."""
        candle = MockCandle(100.0, 100.3, 100.4, 100.0, 3000)
        result = is_breakout(candle, 100.5, 100.0, 3000, 2000, "long",
                             _make_config()["entry"])
        self.assertFalse(result)


# ── Detection: is_retest ─────────────────────────────────────

class TestIsRetest(unittest.TestCase):

    def test_long_success(self):
        """Pullback to breakout level + strong PA → True."""
        # breakout at 100.5, candle pulls back to touch ~100.5
        candle = MockCandle(100.6, 100.8, 100.9, 100.5, 1500)
        # body=0.2, range=0.4, ratio=0.5... need higher
        candle2 = MockCandle(100.45, 100.7, 100.75, 100.4, 1500)
        # body=0.25, range=0.35, ratio=0.71 ✓; low=100.4 near 100.5
        result = is_retest(candle2, 100.5, 2.0, 100.7, "long",
                           _make_config()["entry"])
        self.assertTrue(result)

    def test_weak_pa(self):
        """Body ratio < 0.6 → False."""
        # Doji candle: tiny body, big range
        candle = MockCandle(100.5, 100.51, 100.8, 100.3, 1500)
        # body=0.01, range=0.5, ratio=0.02
        result = is_retest(candle, 100.5, 2.0, 100.51, "long",
                           _make_config()["entry"])
        self.assertFalse(result)

    def test_too_far(self):
        """Candle doesn't reach proximity of breakout → False."""
        candle = MockCandle(102.0, 102.3, 102.5, 101.8, 1500)
        result = is_retest(candle, 100.5, 2.0, 102.3, "long",
                           _make_config()["entry"])
        self.assertFalse(result)

    def test_short_success(self):
        """Short retest: high touches breakout, close below → True."""
        candle = MockCandle(99.55, 99.3, 99.6, 99.25, 1500)
        # body=0.25, range=0.35, ratio=0.71 ✓; high=99.6 near 99.5
        result = is_retest(candle, 99.5, 2.0, 99.3, "short",
                           _make_config()["entry"])
        self.assertTrue(result)


# ── Monitor State Transitions ────────────────────────────────

class TestMonitorTick(unittest.TestCase):

    def test_tick_consolidation_detected(self):
        """WATCHING_FOR_CONSOLIDATION → WATCHING_FOR_BREAKOUT."""
        mon, broker, *_ = _make_monitor()
        cs = _make_candidate()
        mon.candidates["AAPL"] = cs
        broker.get_bars.return_value = _make_bars_consolidating(12)

        mon._tick("AAPL")

        self.assertEqual(cs.state, ORBState.WATCHING_FOR_BREAKOUT.value)
        self.assertIsNotNone(cs.band_high)
        self.assertIsNotNone(cs.band_low)

    def test_tick_consolidation_not_detected(self):
        """Stays in WATCHING_FOR_CONSOLIDATION if bars don't consolidate."""
        mon, broker, *_ = _make_monitor()
        cs = _make_candidate()
        mon.candidates["AAPL"] = cs
        # Expanding bars
        bars = []
        for i in range(12):
            rng = 0.1 + i * 0.1
            bars.append(MockCandle(100, 100, 100 + rng, 100 - rng, 5000 + i * 100))
        broker.get_bars.return_value = bars

        mon._tick("AAPL")

        self.assertEqual(cs.state, ORBState.WATCHING_FOR_CONSOLIDATION.value)

    def test_tick_breakout_detected(self):
        """WATCHING_FOR_BREAKOUT → WATCHING_FOR_RETEST."""
        mon, broker, *_ = _make_monitor()
        cs = _make_candidate(state=ORBState.WATCHING_FOR_BREAKOUT.value,
                             band_high=100.5, band_low=100.0)
        mon.candidates["AAPL"] = cs

        # Strong breakout bar with high volume
        bars = [MockCandle(100, 100.1, 100.2, 99.9, 2000) for _ in range(9)]
        bars.append(MockCandle(100.3, 100.7, 100.8, 100.2, 4000))  # breakout
        broker.get_bars.return_value = bars

        mon._tick("AAPL")

        self.assertEqual(cs.state, ORBState.WATCHING_FOR_RETEST.value)
        self.assertIsNotNone(cs.breakout_level)

    def test_tick_breakout_fails_inside_band(self):
        """2 bars back inside band → FAILED."""
        mon, broker, *_ = _make_monitor()
        cs = _make_candidate(state=ORBState.WATCHING_FOR_BREAKOUT.value,
                             band_high=100.5, band_low=100.0, inside_band_count=1)
        mon.candidates["AAPL"] = cs

        # Bar back inside band
        bars = [MockCandle(100, 100.3, 100.4, 100.1, 1500) for _ in range(10)]
        broker.get_bars.return_value = bars

        mon._tick("AAPL")

        self.assertEqual(cs.state, ORBState.FAILED.value)

    def test_tick_retest_triggers_entry(self):
        """Retest + checklist → executor called, IN_POSITION."""
        mon, broker, executor, _, pi, fm = _make_monitor()
        cs = _make_candidate(state=ORBState.WATCHING_FOR_RETEST.value,
                             breakout_level=100.5)
        mon.candidates["AAPL"] = cs

        # Retest bar: pullback to breakout, strong body
        bars = [MockCandle(100.45, 100.7, 100.75, 100.4, 1500) for _ in range(5)]
        broker.get_bars.return_value = bars

        # Checklist passes
        quote = MagicMock()
        quote.ask_price = 100.72
        quote.bid_price = 100.70
        broker.get_latest_quote.return_value = quote
        fm.can_open_position.return_value = (True, "ok")
        fm.last_pending_id = 42
        pi.pre_entry_check.return_value = {"hard_block": False}
        executor.enter_position.return_value = {
            "status": "submitted", "alpaca_order_id": "alp-1", "pending_id": 42
        }

        mon._tick("AAPL")

        self.assertEqual(cs.state, ORBState.IN_POSITION.value)
        executor.enter_position.assert_called_once()

    def test_tick_position_exit(self):
        """Exit signal → executor called, CLOSED."""
        mon, broker, executor, exit_mgr, *_ = _make_monitor()
        cs = _make_candidate(state=ORBState.IN_POSITION.value,
                             entry_price=100.5, entry_qty=50)
        mon.candidates["AAPL"] = cs

        bars = [MockCandle(100, 100.1, 100.2, 99.9, 2000) for _ in range(20)]
        broker.get_bars.return_value = bars
        exit_mgr.check_exit.return_value = {
            "action": "full_exit", "order_type": "market"
        }
        executor.execute_exit.return_value = {"status": "submitted"}

        mon._tick("AAPL")

        self.assertEqual(cs.state, ORBState.CLOSED.value)
        executor.execute_exit.assert_called_once()

    def test_tick_position_partial_exit(self):
        """Partial exit signal → partial done, stays IN_POSITION."""
        mon, broker, executor, exit_mgr, *_ = _make_monitor()
        cs = _make_candidate(state=ORBState.IN_POSITION.value,
                             entry_price=100.5, entry_qty=50)
        mon.candidates["AAPL"] = cs

        bars = [MockCandle(100, 101.0, 101.1, 100, 2000) for _ in range(20)]
        broker.get_bars.return_value = bars
        exit_mgr.check_exit.return_value = {
            "action": "partial_exit", "trail_stop": 100.3
        }
        executor.execute_partial_exit.return_value = {"status": "filled"}

        mon._tick("AAPL")

        self.assertTrue(cs.partial_done)
        self.assertEqual(cs.state, ORBState.IN_POSITION.value)

    def test_tick_position_no_exit(self):
        """No exit signal → stays IN_POSITION."""
        mon, broker, executor, exit_mgr, *_ = _make_monitor()
        cs = _make_candidate(state=ORBState.IN_POSITION.value,
                             entry_price=100.5, entry_qty=50)
        mon.candidates["AAPL"] = cs

        bars = [MockCandle(100, 100.1, 100.2, 99.9, 2000) for _ in range(20)]
        broker.get_bars.return_value = bars
        exit_mgr.check_exit.return_value = None

        mon._tick("AAPL")

        self.assertEqual(cs.state, ORBState.IN_POSITION.value)

    def test_tick_closed_wave_reset(self):
        """CLOSED with wave < max → back to WATCHING_FOR_CONSOLIDATION."""
        mon, *_ = _make_monitor()
        cs = _make_candidate(state=ORBState.CLOSED.value, wave=1,
                             entry_price=100.5, entry_qty=50,
                             band_high=100.5, band_low=100.0)
        mon.candidates["AAPL"] = cs

        mon._tick("AAPL")

        self.assertEqual(cs.state, ORBState.WATCHING_FOR_CONSOLIDATION.value)
        self.assertEqual(cs.wave, 2)
        self.assertIsNone(cs.band_high)  # reset
        self.assertIsNone(cs.entry_price)  # reset

    def test_tick_closed_done(self):
        """CLOSED at max wave → DONE."""
        mon, *_ = _make_monitor()
        cs = _make_candidate(state=ORBState.CLOSED.value, wave=3)
        mon.candidates["AAPL"] = cs

        mon._tick("AAPL")

        self.assertEqual(cs.state, ORBState.DONE.value)

    def test_tick_failed_retry(self):
        """FAILED with retries < max → back to WATCHING_FOR_CONSOLIDATION."""
        mon, *_ = _make_monitor()
        cs = _make_candidate(state=ORBState.FAILED.value, retries=0)
        mon.candidates["AAPL"] = cs

        mon._tick("AAPL")

        self.assertEqual(cs.state, ORBState.WATCHING_FOR_CONSOLIDATION.value)
        self.assertEqual(cs.retries, 1)

    def test_tick_failed_no_retry(self):
        """FAILED with retries exhausted → SKIPPED."""
        mon, *_ = _make_monitor()
        cs = _make_candidate(state=ORBState.FAILED.value, retries=1)
        mon.candidates["AAPL"] = cs

        mon._tick("AAPL")

        self.assertEqual(cs.state, ORBState.SKIPPED.value)


# ── State Persistence ────────────────────────────────────────

class TestStatePersistence(unittest.TestCase):

    def test_save_and_load_roundtrip(self):
        """Save state → load state preserves candidates."""
        mon, *_ = _make_monitor()
        mon.state_file = os.path.join(tempfile.mkdtemp(), "orb_state.json")
        cs = _make_candidate(state=ORBState.WATCHING_FOR_BREAKOUT.value,
                             band_high=100.5, band_low=100.0)
        mon.candidates["AAPL"] = cs
        mon.daily_pnl = -45.0

        mon.save_state()

        # New monitor, load state
        mon2, *_ = _make_monitor()
        mon2.state_file = mon.state_file
        recovered = mon2.load_state()

        self.assertTrue(recovered)
        self.assertIn("AAPL", mon2.candidates)
        self.assertEqual(mon2.candidates["AAPL"].state,
                         ORBState.WATCHING_FOR_BREAKOUT.value)
        self.assertAlmostEqual(mon2.candidates["AAPL"].band_high, 100.5)
        self.assertAlmostEqual(mon2.daily_pnl, -45.0)

    def test_load_state_no_file(self):
        """No state file → returns False."""
        mon, *_ = _make_monitor()
        mon.state_file = "/nonexistent/orb_state.json"
        self.assertFalse(mon.load_state())


# ── Entry Checklist ──────────────────────────────────────────

class TestEntryChecklist(unittest.TestCase):

    def test_checklist_passes(self):
        """All conditions met → True."""
        mon, broker, _, _, pi, fm = _make_monitor()
        cs = _make_candidate()
        candle = MockCandle(100, 100.5, 100.6, 99.9, 2000)

        quote = MagicMock()
        quote.ask_price = 100.52
        quote.bid_price = 100.50
        broker.get_latest_quote.return_value = quote
        fm.can_open_position.return_value = (True, "ok")
        fm.last_pending_id = 1
        pi.pre_entry_check.return_value = {"hard_block": False}

        result = mon._validate_entry_checklist("AAPL", cs, candle)
        self.assertTrue(result)

    def test_checklist_spread_too_wide(self):
        """Spread > 0.3% → False."""
        mon, broker, _, _, pi, fm = _make_monitor()
        cs = _make_candidate()
        candle = MockCandle(100, 100.5, 100.6, 99.9, 2000)

        quote = MagicMock()
        quote.ask_price = 101.0
        quote.bid_price = 100.0  # 1% spread
        broker.get_latest_quote.return_value = quote

        result = mon._validate_entry_checklist("AAPL", cs, candle)
        self.assertFalse(result)

    def test_checklist_daily_loss_cap(self):
        """Daily PnL at cap → False."""
        mon, broker, _, _, pi, fm = _make_monitor()
        cs = _make_candidate()
        candle = MockCandle(100, 100.5, 100.6, 99.9, 2000)
        mon.daily_pnl = -120.0  # At cap

        quote = MagicMock()
        quote.ask_price = 100.01
        quote.bid_price = 100.00
        broker.get_latest_quote.return_value = quote

        result = mon._validate_entry_checklist("AAPL", cs, candle)
        self.assertFalse(result)

    def test_checklist_portfolio_hard_block(self):
        """Portfolio intel hard block → False."""
        mon, broker, _, _, pi, fm = _make_monitor()
        cs = _make_candidate()
        candle = MockCandle(100, 100.5, 100.6, 99.9, 2000)

        quote = MagicMock()
        quote.ask_price = 100.01
        quote.bid_price = 100.00
        broker.get_latest_quote.return_value = quote
        fm.can_open_position.return_value = (True, "ok")
        fm.last_pending_id = 1
        pi.pre_entry_check.return_value = {"hard_block": True}

        result = mon._validate_entry_checklist("AAPL", cs, candle)
        self.assertFalse(result)


# ── Poll Interval ────────────────────────────────────────────

class TestPollInterval(unittest.TestCase):

    def test_returns_shortest(self):
        """Mixed states → shortest interval."""
        mon, *_ = _make_monitor()
        mon.candidates = {
            "AAPL": _make_candidate("AAPL", ORBState.WATCHING_FOR_CONSOLIDATION.value),
            "MSFT": _make_candidate("MSFT", ORBState.IN_POSITION.value),
            "GOOG": _make_candidate("GOOG", ORBState.DONE.value),
        }

        interval = mon._get_poll_interval()
        self.assertEqual(interval, 10)  # IN_POSITION = 10s

    def test_all_terminal(self):
        """All done → default 30."""
        mon, *_ = _make_monitor()
        mon.candidates = {
            "AAPL": _make_candidate("AAPL", ORBState.DONE.value),
        }
        self.assertEqual(mon._get_poll_interval(), 30.0)


# ── Helpers ──────────────────────────────────────────────────

class TestHelpers(unittest.TestCase):

    def test_calc_atr(self):
        """ATR calculates correctly from bars."""
        mon, *_ = _make_monitor()
        bars = [MockCandle(100, 100 + i * 0.1, 100 + i * 0.2,
                           100 - i * 0.1, 1000) for i in range(15)]
        atr = mon._calc_atr(bars)
        self.assertGreater(atr, 0)

    def test_fetch_bars_error_returns_empty(self):
        """Broker error → empty list."""
        mon, broker, *_ = _make_monitor()
        broker.get_bars.side_effect = RuntimeError("API down")
        self.assertEqual(mon._fetch_bars("AAPL"), [])


if __name__ == "__main__":
    unittest.main()
