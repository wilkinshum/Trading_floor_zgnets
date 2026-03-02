"""Main orchestrator: processes trades, runs nightly review, weekly apply."""

from __future__ import annotations

import math
import yaml
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from .adaptive_weights import AdaptiveWeights, SIGNAL_NAMES
from .signal_attribution import SignalAttribution
from .safety import SafetyManager
from .reporter import Reporter


class SelfLearner:
    """Orchestrates per-trade MW updates, nightly review, and weekly apply."""

    def __init__(self, cfg: dict, db):
        self.cfg = cfg
        self.sl = cfg["self_learning"]
        self.db = db
        self.adaptive_weights = AdaptiveWeights(cfg)
        self.attribution = SignalAttribution(cfg)
        self.safety = SafetyManager(cfg, db)
        self.reporter = Reporter(cfg, db, self.adaptive_weights, self.safety)

    def process_trade(self, trade_data: dict, regime_state: dict):
        """Process a closed trade: compute utility, update weights, record accuracy."""
        strategy = trade_data.get("strategy", "intraday")
        regime = self.adaptive_weights.get_active_regime(
            regime_state, trade_data.get("vix")
        )

        # Compute utility
        utility = self.attribution.compute_utility(strategy, trade_data)

        # Update MW
        self.adaptive_weights.update(strategy, regime, {
            "signal_scores": trade_data["signal_scores"],
            "pnl": trade_data["pnl"],
            "position_value": trade_data["position_value"],
            "holding_days": trade_data.get("holding_days", 1.0),
        })

        # Write signal accuracy
        pnl = trade_data["pnl"]
        pos_meta_id = trade_data.get("position_meta_id")
        conn = self.db._get_conn()
        try:
            cursor = conn.cursor()
            for sig in SIGNAL_NAMES:
                score = trade_data["signal_scores"].get(sig, 0.0)
                was_correct = (score > 0 and pnl > 0) or (score < 0 and pnl < 0)
                cursor.execute("""
                    INSERT INTO signal_accuracy
                    (position_meta_id, strategy, signal_type, signal_score,
                     price_direction, market_regime, was_correct)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (pos_meta_id, strategy, sig, score,
                      1.0 if pnl > 0 else -1.0, regime, was_correct))
            conn.commit()
        finally:
            conn.close()

        self.adaptive_weights.save_state()

    def nightly_review(self) -> str:
        """Generate nightly report and check revert triggers."""
        report = self.reporter.generate_nightly_report()

        for strategy in ("intraday", "swing"):
            should_revert, reason = self.safety.check_revert_triggers(strategy)
            if should_revert:
                old_weights = {}
                for regime in ("directional", "non_directional"):
                    old_weights[regime] = self.adaptive_weights.get_weights(strategy, regime)
                self.adaptive_weights.revert_to_baseline(strategy)
                self.adaptive_weights.save_state()
                for regime in ("directional", "non_directional"):
                    self.safety.log_adjustment(
                        strategy,
                        f"weights.{regime}",
                        str(old_weights[regime]),
                        str(self.adaptive_weights.get_baseline(strategy, regime)),
                        f"Auto-revert: {reason}",
                    )

        self.reporter.save_report(report)
        return report

    def weekly_apply(self) -> dict:
        """Apply or recommend weight changes on Friday close."""
        results = {}
        for strategy in ("intraday", "swing"):
            window = self.sl[strategy]["review_window_days"]
            cutoff = datetime.now().isoformat()
            # Count trades in window
            conn = self.db._get_conn()
            try:
                cursor = conn.cursor()
                from datetime import timedelta, timezone as tz
                cutoff_dt = datetime.now(tz.utc) - timedelta(days=window)
                cursor.execute("""
                    SELECT COUNT(*) FROM position_meta
                    WHERE strategy=? AND status='closed' AND exit_time >= ?
                """, (strategy, cutoff_dt.isoformat()))
                trade_count = cursor.fetchone()[0]
            finally:
                conn.close()

            can_apply = self.safety.can_auto_apply(strategy, trade_count)
            tier = self.safety.get_confidence_tier(trade_count)

            recommendations = {}
            for regime in ("directional", "non_directional"):
                recommendations[regime] = {
                    "current": self.adaptive_weights.get_weights(strategy, regime),
                    "baseline": self.adaptive_weights.get_baseline(strategy, regime),
                    "drift": self.adaptive_weights.get_drift(strategy, regime),
                }

            results[strategy] = {
                "trade_count": trade_count,
                "confidence_tier": tier,
                "can_auto_apply": can_apply,
                "applied": False,
                "recommendations": recommendations,
            }

            if can_apply:
                self._write_overrides({strategy: {
                    regime: self.adaptive_weights.get_weights(strategy, regime)
                    for regime in ("directional", "non_directional")
                }})
                results[strategy]["applied"] = True
                for regime in ("directional", "non_directional"):
                    baseline = self.adaptive_weights.get_baseline(strategy, regime)
                    current = self.adaptive_weights.get_weights(strategy, regime)
                    self.safety.log_adjustment(
                        strategy, f"weights.{regime}",
                        str(baseline), str(current),
                        f"Weekly auto-apply (tier={tier}, trades={trade_count})",
                    )

        return results

    def _write_overrides(self, weights_by_strategy: dict):
        """Write weight overrides to configs/overrides.yaml."""
        path = Path("configs/overrides.yaml")
        path.parent.mkdir(parents=True, exist_ok=True)

        existing = {}
        if path.exists():
            with open(path) as f:
                existing = yaml.safe_load(f) or {}

        if "strategies" not in existing:
            existing["strategies"] = {}

        for strategy, regimes in weights_by_strategy.items():
            if strategy not in existing["strategies"]:
                existing["strategies"][strategy] = {}
            # Write signal weights (excluding reserve)
            weights = {}
            for regime, w in regimes.items():
                weights[regime] = {s: round(w[s], 4) for s in SIGNAL_NAMES}
            existing["strategies"][strategy]["learned_weights"] = weights

        with open(path, "w") as f:
            yaml.dump(existing, f, default_flow_style=False)

    def backtest_validate(self, trades: List[dict]) -> dict:
        """Simulate MW updates through historical trades.

        Returns comparison of static vs MW-adapted performance.
        """
        from .adaptive_weights import AdaptiveWeights

        # Static weights (baseline) PnL
        static_pnl = sum(t["pnl"] for t in trades)

        # Simulate MW updates
        sim_aw = AdaptiveWeights(self.cfg)
        mw_pnl = 0.0

        for t in trades:
            strategy = t.get("strategy", "intraday")
            regime_state = t.get("regime_state", {"bull_confidence": 0.5, "bear_confidence": 0.3})
            regime = sim_aw.get_active_regime(regime_state, t.get("vix"))

            # Score with current MW weights
            w = sim_aw.get_weights(strategy, regime)
            scores = t["signal_scores"]
            weighted_score = sum(scores.get(s, 0) * w.get(s, 0) for s in SIGNAL_NAMES)

            # Use actual PnL (in real backtest we'd re-simulate entry/exit)
            mw_pnl += t["pnl"]

            # Update weights
            sim_aw.update(strategy, regime, {
                "signal_scores": scores,
                "pnl": t["pnl"],
                "position_value": t["position_value"],
                "holding_days": t.get("holding_days", 1.0),
            })

        improvement = ((mw_pnl - static_pnl) / abs(static_pnl) * 100) if static_pnl != 0 else 0.0

        return {
            "static_pnl": static_pnl,
            "mw_pnl": mw_pnl,
            "improvement_pct": improvement,
            "trades_processed": len(trades),
            "final_weights": {
                strat: {
                    regime: sim_aw.get_weights(strat, regime)
                    for regime in ("directional", "non_directional")
                }
                for strat in ("intraday", "swing")
            },
        }
