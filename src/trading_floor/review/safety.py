"""Drift bounds, reversion triggers, confidence tiers."""

from __future__ import annotations

from datetime import datetime, timezone, timedelta
from typing import Tuple


class SafetyManager:
    """Safety gates for the self-learning system."""

    def __init__(self, cfg: dict, db):
        self.cfg = cfg["self_learning"]["safety"]
        self.full_cfg = cfg["self_learning"]
        self.db = db

    def check_revert_triggers(self, strategy: str) -> Tuple[bool, str]:
        """Check if weights should revert to baseline.

        Returns (should_revert, reason).
        """
        last_adj_time = self._get_last_adjustment_time(strategy)
        since = last_adj_time or (datetime.now(timezone.utc) - timedelta(days=365))

        losing_days = self.get_consecutive_losing_days(strategy, since)
        threshold = self.cfg["revert_after_consecutive_losing_days"]
        if losing_days >= threshold:
            return True, f"{losing_days} consecutive losing days (threshold: {threshold})"

        cum_pnl = self.get_cumulative_pnl_since_adjustment(strategy)
        loss_threshold = self.cfg["revert_after_cumulative_loss"]
        if cum_pnl < -loss_threshold:
            return True, f"Cumulative PnL ${cum_pnl:.2f} < -${loss_threshold}"

        return False, ""

    def get_confidence_tier(self, trade_count: int) -> str:
        """Classify confidence based on trade count."""
        tiers = self.cfg["confidence_tiers"]
        if trade_count < tiers["insufficient"]:
            return "insufficient"
        if trade_count < tiers["low"]:
            return "low"
        if trade_count < tiers["medium"]:
            return "medium"
        return "high"

    def can_auto_apply(self, strategy: str, trade_count: int) -> bool:
        """Check if weights can be auto-applied."""
        if not self.full_cfg.get("auto_apply", False):
            return False

        tier = self.get_confidence_tier(trade_count)
        if tier in ("insufficient", "low"):
            return False

        # Check min trades since last apply
        min_key = "min_trades_since_last_apply_swing" if strategy == "swing" else "min_trades_since_last_apply"
        min_trades = self.cfg.get(min_key, 10)
        trades_since = self._get_trades_since_last_apply(strategy)
        if trades_since < min_trades:
            return False

        should_revert, _ = self.check_revert_triggers(strategy)
        if should_revert:
            return False

        return True

    def get_consecutive_losing_days(self, strategy: str, since: datetime) -> int:
        """Count consecutive losing days from position_meta since a date."""
        conn = self.db._get_conn()
        try:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT DATE(exit_time), SUM(pnl)
                FROM position_meta
                WHERE strategy=? AND status='closed' AND exit_time >= ?
                GROUP BY DATE(exit_time)
                ORDER BY DATE(exit_time) DESC
            """, (strategy, since.isoformat()))
            rows = cursor.fetchall()
        finally:
            conn.close()

        consecutive = 0
        for _, daily_pnl in rows:
            if daily_pnl is not None and daily_pnl < 0:
                consecutive += 1
            else:
                break
        return consecutive

    def get_cumulative_pnl_since_adjustment(self, strategy: str) -> float:
        """Sum PnL from position_meta since last config change."""
        last_adj = self._get_last_adjustment_time(strategy)
        since = last_adj or datetime(2000, 1, 1, tzinfo=timezone.utc)

        conn = self.db._get_conn()
        try:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT COALESCE(SUM(pnl), 0)
                FROM position_meta
                WHERE strategy=? AND status='closed' AND exit_time >= ?
            """, (strategy, since.isoformat()))
            return cursor.fetchone()[0]
        finally:
            conn.close()

    def log_adjustment(self, strategy: str, field: str, old_val, new_val, reason: str):
        """Write to config_history table."""
        conn = self.db._get_conn()
        try:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO config_history (changed_by, strategy, field_path, old_value, new_value, reason)
                VALUES (?, ?, ?, ?, ?, ?)
            """, ("self_learner", strategy, field, str(old_val), str(new_val), reason))
            conn.commit()
        finally:
            conn.close()

    def _get_last_adjustment_time(self, strategy: str):
        """Get timestamp of last config_history entry for strategy."""
        conn = self.db._get_conn()
        try:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT created_at FROM config_history
                WHERE strategy=? AND changed_by='self_learner'
                ORDER BY created_at DESC LIMIT 1
            """, (strategy,))
            row = cursor.fetchone()
            if row and row[0]:
                try:
                    return datetime.fromisoformat(row[0])
                except (ValueError, TypeError):
                    return None
            return None
        finally:
            conn.close()

    def _get_trades_since_last_apply(self, strategy: str) -> int:
        """Count trades since last weight application."""
        last_adj = self._get_last_adjustment_time(strategy)
        since = last_adj or datetime(2000, 1, 1, tzinfo=timezone.utc)

        conn = self.db._get_conn()
        try:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT COUNT(*) FROM position_meta
                WHERE strategy=? AND status='closed' AND exit_time >= ?
            """, (strategy, since.isoformat()))
            return cursor.fetchone()[0]
        finally:
            conn.close()
