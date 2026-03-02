"""Generate nightly markdown review reports."""

from __future__ import annotations

from datetime import datetime, date, timedelta, timezone
from pathlib import Path
from typing import Dict, Optional


class Reporter:
    """Produces nightly markdown review reports."""

    def __init__(self, cfg: dict, db, adaptive_weights, safety):
        self.cfg = cfg
        self.sl = cfg["self_learning"]
        self.db = db
        self.aw = adaptive_weights
        self.safety = safety

    def generate_nightly_report(self, date_: Optional[date] = None) -> str:
        """Generate full nightly markdown report."""
        if date_ is None:
            date_ = date.today()

        lines = [f"# Self-Learning Review — {date_}", ""]

        for strategy in ("intraday", "swing"):
            trades, pnl = self._get_trades_today(strategy, date_)
            window = self.sl[strategy]["review_window_days"]
            total_trades = self._get_trade_count(strategy, window)
            tier = self.safety.get_confidence_tier(total_trades)
            should_revert, revert_reason = self.safety.check_revert_triggers(strategy)
            cum_pnl = self.safety.get_cumulative_pnl_since_adjustment(strategy)
            can_apply = self.safety.can_auto_apply(strategy, total_trades)

            lines.append(f"## {strategy.title()}")
            lines.append(f"- Trades today: {trades}")
            lines.append(f"- PnL today: ${pnl:.2f}")
            lines.append(f"- Trades in window ({window}d): {total_trades}")
            lines.append(f"- Confidence tier: {tier.upper()}")
            lines.append(f"- Cumulative PnL since last adjustment: ${cum_pnl:.2f}")
            lines.append(f"- Projected weekly apply: {'YES' if can_apply else 'NO'}")
            if should_revert:
                lines.append(f"- ⚠️ REVERT TRIGGER: {revert_reason}")
            lines.append("")

            # Weights per regime
            for regime in ("directional", "non_directional"):
                w = self.aw.get_weights(strategy, regime)
                b = self.aw.get_baseline(strategy, regime)
                drift = self.aw.get_drift(strategy, regime)
                lines.append(f"### {regime.replace('_', ' ').title()} Weights")
                for sig in ["momentum", "meanrev", "breakout", "news"]:
                    d = drift.get(sig, 0)
                    sign = "+" if d >= 0 else ""
                    lines.append(f"  - {sig}: {w.get(sig, 0):.3f} (baseline {b.get(sig, 0):.3f}, drift {sign}{d:.3f})")
                lines.append(f"  - reserve: {w.get('reserve', 0):.3f}")
                lines.append("")

            # Signal accuracy
            for regime in ("directional", "non_directional"):
                acc = self._get_signal_accuracy(strategy, regime, window)
                if acc:
                    lines.append(f"### Signal Accuracy ({regime.replace('_', ' ').title()})")
                    for sig, data in acc.items():
                        lines.append(f"  - {sig}: {data['accuracy']:.0f}% ({data['correct']}/{data['total']})")
                    lines.append("")

            # Symbol performance
            perf = self._get_symbol_performance(strategy, window)
            if perf:
                lines.append("### Symbol Performance")
                for sym, data in sorted(perf.items(), key=lambda x: x[1].get("pnl", 0), reverse=True)[:10]:
                    lines.append(f"  - {sym}: {data['wins']}/{data['total']} wins, PnL ${data['pnl']:.2f}")
                lines.append("")

        # Kill switch status
        lines.append("## Kill Switch Status")
        lines.append(f"- auto_apply: {self.sl.get('auto_apply', False)}")
        lines.append("")

        return "\n".join(lines)

    def save_report(self, report: str, date_: Optional[date] = None):
        """Save report to memory/reviews/YYYY-MM-DD.md."""
        if date_ is None:
            date_ = date.today()
        path = Path("memory/reviews") / f"{date_}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(report, encoding="utf-8")

    def _get_trades_today(self, strategy: str, date_: date):
        """Count trades and sum PnL for a date."""
        conn = self.db._get_conn()
        try:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT COUNT(*), COALESCE(SUM(pnl), 0)
                FROM position_meta
                WHERE strategy=? AND status='closed' AND DATE(exit_time)=?
            """, (strategy, date_.isoformat()))
            row = cursor.fetchone()
            return row[0], row[1]
        finally:
            conn.close()

    def _get_trade_count(self, strategy: str, window_days: int) -> int:
        """Count closed trades in the review window."""
        cutoff = (datetime.now(timezone.utc) - timedelta(days=window_days)).isoformat()
        conn = self.db._get_conn()
        try:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT COUNT(*) FROM position_meta
                WHERE strategy=? AND status='closed' AND exit_time >= ?
            """, (strategy, cutoff))
            return cursor.fetchone()[0]
        finally:
            conn.close()

    def _get_signal_accuracy(self, strategy: str, regime: str, window_days: int) -> dict:
        """Query signal_accuracy table for per-signal stats."""
        cutoff = (datetime.now(timezone.utc) - timedelta(days=window_days)).isoformat()
        conn = self.db._get_conn()
        try:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT signal_type, 
                       SUM(CASE WHEN was_correct THEN 1 ELSE 0 END),
                       COUNT(*)
                FROM signal_accuracy
                WHERE strategy=? AND market_regime=? AND created_at >= ?
                GROUP BY signal_type
            """, (strategy, regime, cutoff))
            result = {}
            for sig, correct, total in cursor.fetchall():
                result[sig] = {
                    "correct": correct,
                    "total": total,
                    "accuracy": (correct / total * 100) if total > 0 else 0,
                }
            return result
        finally:
            conn.close()

    def _get_symbol_performance(self, strategy: str, window_days: int) -> dict:
        """Query position_meta for per-symbol win/loss counts."""
        cutoff = (datetime.now(timezone.utc) - timedelta(days=window_days)).isoformat()
        conn = self.db._get_conn()
        try:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT symbol,
                       SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END),
                       COUNT(*),
                       COALESCE(SUM(pnl), 0)
                FROM position_meta
                WHERE strategy=? AND status='closed' AND exit_time >= ?
                GROUP BY symbol
            """, (strategy, cutoff))
            result = {}
            for sym, wins, total, pnl in cursor.fetchall():
                result[sym] = {"wins": wins, "total": total, "pnl": pnl}
            return result
        finally:
            conn.close()
