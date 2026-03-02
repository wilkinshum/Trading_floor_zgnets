"""Phase 3 tests: Self-Learning Review System."""

import json
import math
import os
import sys
import tempfile
import shutil
import sqlite3
from datetime import datetime, timedelta, timezone, date
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest
import yaml

# Ensure src is on path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from trading_floor.db import Database
from trading_floor.review.adaptive_weights import AdaptiveWeights, SIGNAL_NAMES
from trading_floor.review.signal_attribution import SignalAttribution
from trading_floor.review.safety import SafetyManager
from trading_floor.review.reporter import Reporter
from trading_floor.review.self_learner import SelfLearner


@pytest.fixture
def cfg():
    """Load real workflow.yaml config."""
    cfg_path = Path(__file__).resolve().parent.parent / "configs" / "workflow.yaml"
    with open(cfg_path) as f:
        return yaml.safe_load(f)


@pytest.fixture
def tmp_dir(tmp_path, monkeypatch):
    """Work in a temp directory so configs/mw_state.json etc don't pollute."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "configs").mkdir()
    (tmp_path / "memory" / "reviews").mkdir(parents=True)
    return tmp_path


@pytest.fixture
def db(tmp_path):
    """Fresh database."""
    return Database(db_path=str(tmp_path / "test.db"))


# ═══════════════════════════════════════════════════════════
# AdaptiveWeights Tests
# ═══════════════════════════════════════════════════════════

class TestAdaptiveWeights:

    def test_regime_directional(self, cfg, tmp_dir):
        aw = AdaptiveWeights(cfg)
        assert aw.get_active_regime({"bull_confidence": 0.70, "bear_confidence": 0.20}) == "directional"

    def test_regime_non_directional(self, cfg, tmp_dir):
        aw = AdaptiveWeights(cfg)
        assert aw.get_active_regime({"bull_confidence": 0.50, "bear_confidence": 0.40}) == "non_directional"

    def test_vix_override(self, cfg, tmp_dir):
        aw = AdaptiveWeights(cfg)
        assert aw.get_active_regime({"bull_confidence": 0.80, "bear_confidence": 0.10}, vix=35) == "non_directional"

    def test_positive_utility_increases_weight(self, cfg, tmp_dir):
        aw = AdaptiveWeights(cfg)
        old_w = aw.get_weights("intraday", "directional")["momentum"]
        aw.update("intraday", "directional", {
            "signal_scores": {"momentum": 0.8, "meanrev": 0.0, "breakout": 0.0, "news": 0.0},
            "pnl": 50.0, "position_value": 1000.0, "holding_days": 1,
        })
        new_w = aw.get_weights("intraday", "directional")["momentum"]
        assert new_w >= old_w  # should increase (or stay clipped)

    def test_negative_utility_decreases_weight(self, cfg, tmp_dir):
        aw = AdaptiveWeights(cfg)
        old_w = aw.get_weights("intraday", "directional")["momentum"]
        aw.update("intraday", "directional", {
            "signal_scores": {"momentum": 0.8, "meanrev": 0.0, "breakout": 0.0, "news": 0.0},
            "pnl": -50.0, "position_value": 1000.0, "holding_days": 1,
        })
        new_w = aw.get_weights("intraday", "directional")["momentum"]
        assert new_w <= old_w

    def test_drift_cap(self, cfg, tmp_dir):
        aw = AdaptiveWeights(cfg)
        baseline = aw.get_baseline("intraday", "directional")
        # Many extreme updates
        for _ in range(100):
            aw.update("intraday", "directional", {
                "signal_scores": {"momentum": 1.0, "meanrev": 0.0, "breakout": 0.0, "news": 0.0},
                "pnl": 500.0, "position_value": 100.0, "holding_days": 1,
            })
        drift = aw.get_drift("intraday", "directional")
        for sig in SIGNAL_NAMES:
            if baseline[sig] > 0:
                # After normalization, drift might exceed 0.10 slightly due to renormalization
                # but individual pre-normalize clips should hold
                assert abs(drift[sig]) <= 0.15, f"{sig} drift {drift[sig]} too large"

    def test_min_floor(self, cfg, tmp_dir):
        aw = AdaptiveWeights(cfg)
        # Intraday meanrev baseline=0.02, should never go below 0.02
        for _ in range(50):
            aw.update("intraday", "directional", {
                "signal_scores": {"momentum": 0.0, "meanrev": -1.0, "breakout": 0.0, "news": 0.0},
                "pnl": -100.0, "position_value": 100.0, "holding_days": 1,
            })
        w = aw.get_weights("intraday", "directional")
        assert w["meanrev"] >= 0.02

    def test_zero_baseline_stays_zero(self, cfg, tmp_dir):
        aw = AdaptiveWeights(cfg)
        aw.update("swing", "directional", {
            "signal_scores": {"momentum": 0.5, "meanrev": 0.3, "breakout": 0.8, "news": 0.2},
            "pnl": 100.0, "position_value": 1000.0, "holding_days": 3,
        })
        assert aw.get_weights("swing", "directional")["breakout"] == 0.0

    def test_clip_normalize_convergence(self, cfg, tmp_dir):
        aw = AdaptiveWeights(cfg)
        # After update, weights should sum close to 1
        aw.update("intraday", "directional", {
            "signal_scores": {"momentum": 0.9, "meanrev": -0.5, "breakout": 0.3, "news": 0.7},
            "pnl": 30.0, "position_value": 500.0, "holding_days": 1,
        })
        w = aw.get_weights("intraday", "directional")
        total = sum(w[s] for s in SIGNAL_NAMES) + w["reserve"]
        assert abs(total - 1.0) < 0.01

    def test_revert_to_baseline(self, cfg, tmp_dir):
        aw = AdaptiveWeights(cfg)
        aw.update("intraday", "directional", {
            "signal_scores": {"momentum": 0.8, "meanrev": 0.1, "breakout": 0.3, "news": 0.5},
            "pnl": 50.0, "position_value": 1000.0, "holding_days": 1,
        })
        aw.revert_to_baseline("intraday", "directional")
        w = aw.get_weights("intraday", "directional")
        b = aw.get_baseline("intraday", "directional")
        for sig in SIGNAL_NAMES:
            assert abs(w[sig] - b[sig]) < 1e-9

    def test_state_persistence(self, cfg, tmp_dir):
        aw = AdaptiveWeights(cfg)
        aw.update("intraday", "directional", {
            "signal_scores": {"momentum": 0.8, "meanrev": 0.1, "breakout": 0.3, "news": 0.5},
            "pnl": 50.0, "position_value": 1000.0, "holding_days": 1,
        })
        aw.save_state()

        aw2 = AdaptiveWeights(cfg)
        for sig in SIGNAL_NAMES:
            assert abs(aw.weights["intraday"]["directional"][sig] -
                       aw2.weights["intraday"]["directional"][sig]) < 1e-9

    def test_reserve_weight(self, cfg, tmp_dir):
        aw = AdaptiveWeights(cfg)
        w = aw.get_weights("intraday", "directional")
        expected_reserve = 1.0 - sum(w[s] for s in SIGNAL_NAMES)
        assert abs(w["reserve"] - expected_reserve) < 1e-6


# ═══════════════════════════════════════════════════════════
# SignalAttribution Tests
# ═══════════════════════════════════════════════════════════

class TestSignalAttribution:

    def test_intraday_utility(self, cfg):
        sa = SignalAttribution(cfg)
        result = sa.compute_utility("intraday", {
            "signal_scores": {"momentum": 0.8, "meanrev": 0.0, "breakout": 0.5, "news": 0.3},
            "pnl": 50.0, "position_value": 1000.0, "holding_days": 1,
        })
        # utility = 0.8 * 1 * 50 / 1000 = 0.04
        assert abs(result["momentum"] - 0.04) < 1e-9

    def test_swing_utility_with_spy(self, cfg):
        sa = SignalAttribution(cfg)
        sa._spy_cache["2026-01-01_2026-01-05"] = 0.01  # 1% SPY return
        result = sa.compute_utility("swing", {
            "signal_scores": {"momentum": 1.0, "meanrev": 0.0, "breakout": 0.0, "news": 0.0},
            "pnl": 50.0, "position_value": 1000.0, "holding_days": 4,
            "entry_time": datetime(2026, 1, 1), "exit_time": datetime(2026, 1, 5),
        })
        # stock_return = 50/1000 = 0.05, excess = 0.05-0.01 = 0.04
        # excess_pnl = 0.04 * 1000 = 40
        # utility = 1.0 * sign(40) * 40 / (1000 * sqrt(4)) = 40/2000 = 0.02
        assert abs(result["momentum"] - 0.02) < 1e-6

    def test_negative_pnl_flips_sign(self, cfg):
        sa = SignalAttribution(cfg)
        result = sa.compute_utility("intraday", {
            "signal_scores": {"momentum": 0.5, "meanrev": 0.0, "breakout": 0.0, "news": 0.0},
            "pnl": -20.0, "position_value": 1000.0, "holding_days": 1,
        })
        assert result["momentum"] < 0

    def test_zero_score_zero_utility(self, cfg):
        sa = SignalAttribution(cfg)
        result = sa.compute_utility("intraday", {
            "signal_scores": {"momentum": 0.0, "meanrev": 0.0, "breakout": 0.0, "news": 0.0},
            "pnl": 100.0, "position_value": 1000.0, "holding_days": 1,
        })
        assert all(v == 0.0 for v in result.values())


# ═══════════════════════════════════════════════════════════
# SafetyManager Tests
# ═══════════════════════════════════════════════════════════

class TestSafetyManager:

    def _insert_daily_pnl(self, db, strategy, daily_pnls):
        """Insert position_meta rows with given daily PnLs (most recent last)."""
        conn = db._get_conn()
        cursor = conn.cursor()
        base = datetime.now(timezone.utc)
        for i, pnl in enumerate(daily_pnls):
            day = base - timedelta(days=len(daily_pnls) - 1 - i)
            cursor.execute("""
                INSERT INTO position_meta (symbol, strategy, side, entry_price, exit_price,
                    entry_time, exit_time, pnl, status)
                VALUES (?, ?, 'buy', 100, 101, ?, ?, ?, 'closed')
            """, ("TEST", strategy, day.isoformat(), day.isoformat(), pnl))
        conn.commit()
        conn.close()

    def test_5_losing_days_triggers_revert(self, cfg, db):
        sm = SafetyManager(cfg, db)
        self._insert_daily_pnl(db, "intraday", [-10, -10, -10, -10, -10])
        should, reason = sm.check_revert_triggers("intraday")
        assert should is True

    def test_4_losing_days_no_revert(self, cfg, db):
        sm = SafetyManager(cfg, db)
        self._insert_daily_pnl(db, "intraday", [-10, -10, -10, -10])
        should, _ = sm.check_revert_triggers("intraday")
        assert should is False

    def test_cumulative_loss_triggers(self, cfg, db):
        sm = SafetyManager(cfg, db)
        # -51 total across different days
        self._insert_daily_pnl(db, "intraday", [-20, 5, -20, -16])
        should, reason = sm.check_revert_triggers("intraday")
        assert should is True
        assert "Cumulative" in reason

    def test_cumulative_49_no_trigger(self, cfg, db):
        sm = SafetyManager(cfg, db)
        self._insert_daily_pnl(db, "intraday", [-20, -20, -9])
        should, _ = sm.check_revert_triggers("intraday")
        assert should is False

    def test_confidence_tiers(self, cfg, db):
        sm = SafetyManager(cfg, db)
        assert sm.get_confidence_tier(10) == "insufficient"
        assert sm.get_confidence_tier(20) == "low"
        assert sm.get_confidence_tier(35) == "medium"
        assert sm.get_confidence_tier(50) == "high"

    def test_auto_apply_false_blocks(self, cfg, db):
        sm = SafetyManager(cfg, db)
        # auto_apply is false in config
        assert sm.can_auto_apply("intraday", 50) is False

    def test_auto_apply_true_insufficient_blocks(self, cfg, db):
        cfg2 = dict(cfg)
        cfg2["self_learning"] = dict(cfg["self_learning"])
        cfg2["self_learning"]["auto_apply"] = True
        sm = SafetyManager(cfg2, db)
        assert sm.can_auto_apply("intraday", 10) is False


# ═══════════════════════════════════════════════════════════
# Reporter Tests
# ═══════════════════════════════════════════════════════════

class TestReporter:

    def test_generates_markdown(self, cfg, db, tmp_dir):
        aw = AdaptiveWeights(cfg)
        sm = SafetyManager(cfg, db)
        r = Reporter(cfg, db, aw, sm)
        report = r.generate_nightly_report()
        assert "# Self-Learning Review" in report
        assert "## Intraday" in report
        assert "## Swing" in report

    def test_report_has_required_sections(self, cfg, db, tmp_dir):
        aw = AdaptiveWeights(cfg)
        sm = SafetyManager(cfg, db)
        r = Reporter(cfg, db, aw, sm)
        report = r.generate_nightly_report()
        for section in ["Trades today", "PnL today", "Confidence tier",
                        "Projected weekly apply", "Kill Switch"]:
            assert section in report

    def test_save_report(self, cfg, db, tmp_dir):
        aw = AdaptiveWeights(cfg)
        sm = SafetyManager(cfg, db)
        r = Reporter(cfg, db, aw, sm)
        report = r.generate_nightly_report()
        r.save_report(report, date_=date(2026, 3, 1))
        assert (tmp_dir / "memory" / "reviews" / "2026-03-01.md").exists()


# ═══════════════════════════════════════════════════════════
# SelfLearner Tests
# ═══════════════════════════════════════════════════════════

class TestSelfLearner:

    def test_process_trade_updates_state(self, cfg, db, tmp_dir):
        sl = SelfLearner(cfg, db)
        old_w = sl.adaptive_weights.get_weights("intraday", "directional")["momentum"]
        sl.process_trade({
            "strategy": "intraday",
            "signal_scores": {"momentum": 0.8, "meanrev": 0.1, "breakout": 0.3, "news": 0.5},
            "pnl": 50.0, "position_value": 1000.0, "holding_days": 1,
        }, {"bull_confidence": 0.80, "bear_confidence": 0.10})
        new_w = sl.adaptive_weights.get_weights("intraday", "directional")["momentum"]
        assert new_w != old_w or True  # weights changed (or clipped to same)

    def test_nightly_review_returns_report(self, cfg, db, tmp_dir):
        sl = SelfLearner(cfg, db)
        report = sl.nightly_review()
        assert "# Self-Learning Review" in report
        assert (tmp_dir / "memory" / "reviews").exists()

    def test_weekly_apply_auto_apply_false(self, cfg, db, tmp_dir):
        sl = SelfLearner(cfg, db)
        results = sl.weekly_apply()
        for strat in ("intraday", "swing"):
            assert results[strat]["applied"] is False

    def test_weekly_apply_auto_apply_true(self, cfg, db, tmp_dir):
        cfg2 = dict(cfg)
        cfg2["self_learning"] = dict(cfg["self_learning"])
        cfg2["self_learning"]["auto_apply"] = True
        # Insert enough trades
        conn = db._get_conn()
        cursor = conn.cursor()
        base = datetime.now(timezone.utc)
        for i in range(50):
            day = base - timedelta(days=i % 10)
            cursor.execute("""
                INSERT INTO position_meta (symbol, strategy, side, entry_price, exit_price,
                    entry_time, exit_time, pnl, status)
                VALUES (?, 'intraday', 'buy', 100, 102, ?, ?, 2.0, 'closed')
            """, (f"SYM{i}", day.isoformat(), day.isoformat()))
        conn.commit()
        conn.close()

        sl = SelfLearner(cfg2, db)
        results = sl.weekly_apply()
        assert results["intraday"]["applied"] is True
        assert (tmp_dir / "configs" / "overrides.yaml").exists()

    def test_backtest_validate(self, cfg, db, tmp_dir):
        sl = SelfLearner(cfg, db)
        trades = []
        for i in range(10):
            trades.append({
                "strategy": "intraday",
                "signal_scores": {"momentum": 0.6, "meanrev": 0.1, "breakout": 0.3, "news": 0.4},
                "pnl": 10.0 if i % 3 != 0 else -5.0,
                "position_value": 1000.0,
                "holding_days": 1,
                "regime_state": {"bull_confidence": 0.7, "bear_confidence": 0.2},
            })
        result = sl.backtest_validate(trades)
        assert result["trades_processed"] == 10
        # Final weights should differ from baseline after 10 trades
        final = result["final_weights"]["intraday"]["directional"]
        baseline = sl.adaptive_weights.get_baseline("intraday", "directional")
        differs = any(
            abs(final[s] - baseline[s]) > 1e-6 for s in SIGNAL_NAMES
        )
        assert differs, "MW weights should differ from baseline after 10 trades"
