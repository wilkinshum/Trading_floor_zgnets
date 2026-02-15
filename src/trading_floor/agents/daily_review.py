from __future__ import annotations

import json
import shutil
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

import yaml

from trading_floor.agent_memory import AgentMemory
from trading_floor.shadow import ShadowRunner


class DailyReviewAgent:
    """
    Nightly self-improvement agent. Analyzes trades, evaluates signal quality,
    adjusts weights, and produces a daily report card.
    """

    def __init__(self, cfg: dict, db_path: str = "trading.db"):
        self.cfg = cfg
        self.db_path = Path(db_path)
        self.report_dir = Path("trading_logs/daily_reviews")
        self.report_dir.mkdir(parents=True, exist_ok=True)

    def _conn(self):
        return sqlite3.connect(self.db_path)

    def run(self, date_str: str | None = None) -> dict:
        """Run the daily review for a given date (default: today)."""
        if date_str is None:
            date_str = datetime.now().strftime("%Y-%m-%d")

        # Query trades for the date
        conn = self._conn()
        conn.row_factory = sqlite3.Row

        trades = conn.execute(
            "SELECT * FROM trades WHERE timestamp LIKE ?", (f"{date_str}%",)
        ).fetchall()

        signals = conn.execute(
            "SELECT * FROM signals WHERE timestamp LIKE ?", (f"{date_str}%",)
        ).fetchall()

        # Also get recent trades (last 30 days) for broader analysis
        cutoff = (datetime.strptime(date_str, "%Y-%m-%d") - timedelta(days=30)).strftime("%Y-%m-%d")
        recent_trades = conn.execute(
            "SELECT * FROM trades WHERE timestamp >= ?", (cutoff,)
        ).fetchall()
        conn.close()

        # --- Metrics ---
        metrics = self._calc_metrics(recent_trades)
        today_metrics = self._calc_metrics(trades)

        # --- Signal Attribution ---
        attribution = self._signal_attribution(signals, trades)

        # --- Weight Recommendations ---
        current_weights = self.cfg.get("signals", {}).get("weights", {
            "momentum": 0.25, "meanrev": 0.25, "breakout": 0.25, "news": 0.25
        })
        new_weights, confidence = self._recommend_weights(attribution, current_weights)

        # --- Memory Audit ---
        memory_audit = self._memory_audit()

        # --- Shadow Model Comparison ---
        shadow_eval = self._shadow_comparison(date_str)

        # --- Generate Report ---
        report = self._generate_report(
            date_str, today_metrics, metrics, attribution, current_weights, new_weights, confidence,
            memory_audit=memory_audit,
            shadow_eval=shadow_eval,
        )

        report_path = self.report_dir / f"{date_str}.md"
        report_path.write_text(report, encoding="utf-8")

        # --- Auto-update weights if confidence is high enough ---
        config_updated = False
        if confidence >= 0.7 and len(recent_trades) >= 10:
            self._update_config_weights(new_weights)
            config_updated = True

        return {
            "date": date_str,
            "today_trades": len(trades),
            "recent_trades": len(recent_trades),
            "metrics_30d": metrics,
            "metrics_today": today_metrics,
            "attribution": attribution,
            "new_weights": new_weights,
            "confidence": confidence,
            "config_updated": config_updated,
            "memory_audit": memory_audit,
            "report_path": str(report_path),
        }

    def _calc_metrics(self, trades) -> dict:
        if not trades:
            return {"trades": 0, "win_rate": 0, "profit_factor": 0,
                    "avg_winner": 0, "avg_loser": 0, "total_pnl": 0, "max_drawdown": 0}

        pnls = [t["pnl"] for t in trades if t["pnl"] is not None]
        if not pnls:
            return {"trades": len(trades), "win_rate": 0, "profit_factor": 0,
                    "avg_winner": 0, "avg_loser": 0, "total_pnl": 0, "max_drawdown": 0}

        winners = [p for p in pnls if p > 0]
        losers = [p for p in pnls if p < 0]

        win_rate = len(winners) / max(1, len(winners) + len(losers))
        gross_profit = sum(winners)
        gross_loss = abs(sum(losers))
        profit_factor = gross_profit / max(1e-9, gross_loss)
        avg_winner = sum(winners) / max(1, len(winners))
        avg_loser = sum(losers) / max(1, len(losers))  # negative

        # Max drawdown from cumulative PnL
        cumulative = 0.0
        peak = 0.0
        max_dd = 0.0
        for p in pnls:
            cumulative += p
            if cumulative > peak:
                peak = cumulative
            dd = peak - cumulative
            if dd > max_dd:
                max_dd = dd

        return {
            "trades": len(trades),
            "win_rate": round(win_rate, 3),
            "profit_factor": round(profit_factor, 2),
            "avg_winner": round(avg_winner, 2),
            "avg_loser": round(avg_loser, 2),
            "total_pnl": round(sum(pnls), 2),
            "max_drawdown": round(max_dd, 2),
        }

    def _signal_attribution(self, signals, trades) -> dict:
        """
        For each signal component, check if it correctly predicted the trade direction.
        Returns: {component: {"correct": N, "wrong": N, "accuracy": float}}
        """
        components = ["momentum", "meanrev", "breakout", "news"]
        score_cols = {"momentum": "score_mom", "meanrev": "score_mean",
                      "breakout": "score_break", "news": "score_news"}

        # Build symbol->pnl map from trades
        trade_pnl = {}
        for t in trades:
            sym = t["symbol"]
            pnl = t["pnl"] or 0
            trade_pnl[sym] = trade_pnl.get(sym, 0) + pnl

        # Build symbol->signal map
        sig_map = {}
        for s in signals:
            sym = s["symbol"]
            sig_map[sym] = dict(s)

        result = {}
        for comp in components:
            correct = 0
            wrong = 0
            col = score_cols[comp]
            for sym, pnl in trade_pnl.items():
                if sym not in sig_map:
                    continue
                sig_val = sig_map[sym].get(col, 0) or 0
                if pnl == 0 or sig_val == 0:
                    continue
                # Signal predicted correctly if both same sign
                if (sig_val > 0 and pnl > 0) or (sig_val < 0 and pnl < 0):
                    correct += 1
                else:
                    wrong += 1
            total = correct + wrong
            result[comp] = {
                "correct": correct,
                "wrong": wrong,
                "accuracy": round(correct / max(1, total), 3),
            }

        return result

    def _recommend_weights(self, attribution: dict, current: dict) -> tuple[dict, float]:
        """
        Adjust weights proportional to signal accuracy.
        Returns (new_weights, confidence).
        Confidence = how much data we have (more trades = higher confidence).
        """
        components = ["momentum", "meanrev", "breakout", "news"]
        accuracies = {}
        total_samples = 0

        for comp in components:
            attr = attribution.get(comp, {"accuracy": 0.5, "correct": 0, "wrong": 0})
            accuracies[comp] = attr["accuracy"] if (attr["correct"] + attr["wrong"]) > 0 else 0.5
            total_samples += attr["correct"] + attr["wrong"]

        # Confidence based on sample size
        confidence = min(1.0, total_samples / 50)  # 50 signals = full confidence

        # New weights proportional to accuracy (with floor of 0.05)
        raw = {c: max(0.05, accuracies[c]) for c in components}
        total = sum(raw.values())
        new_weights = {c: round(raw[c] / total, 3) for c in components}

        # Blend with current weights (conservative: 70% old, 30% new)
        blended = {}
        for c in components:
            blended[c] = round(current.get(c, 0.25) * 0.7 + new_weights[c] * 0.3, 3)

        # Renormalize
        total = sum(blended.values())
        blended = {c: round(blended[c] / total, 3) for c in components}

        return blended, round(confidence, 2)

    def _update_config_weights(self, new_weights: dict):
        """Write updated weights to workflow.yaml."""
        config_path = Path("configs/workflow.yaml")
        if not config_path.exists():
            return

        with open(config_path, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f)

        cfg.setdefault("signals", {})["weights"] = new_weights

        with open(config_path, "w", encoding="utf-8") as f:
            yaml.dump(cfg, f, default_flow_style=False, sort_keys=False)

    def _generate_report(self, date_str, today, period, attribution,
                         old_weights, new_weights, confidence, memory_audit=None,
                         shadow_eval=None) -> str:
        lines = [
            f"# Daily Review — {date_str}",
            "",
            "## Today's Performance",
            f"- Trades: {today['trades']}",
            f"- Win Rate: {today['win_rate']:.1%}" if today['trades'] else "- No trades today",
            f"- Total PnL: ${today['total_pnl']:.2f}" if today['trades'] else "",
            "",
            "## 30-Day Performance",
            f"- Trades: {period['trades']}",
            f"- Win Rate: {period['win_rate']:.1%}",
            f"- Profit Factor: {period['profit_factor']:.2f}",
            f"- Avg Winner: ${period['avg_winner']:.2f}",
            f"- Avg Loser: ${period['avg_loser']:.2f}",
            f"- Total PnL: ${period['total_pnl']:.2f}",
            f"- Max Drawdown: ${period['max_drawdown']:.2f}",
            "",
            "## Signal Attribution",
        ]
        for comp, data in attribution.items():
            lines.append(f"- **{comp}**: {data['correct']}/{data['correct']+data['wrong']} correct "
                         f"({data['accuracy']:.1%})")

        lines.extend([
            "",
            "## Weight Adjustments",
            f"- Confidence: {confidence:.0%} (based on sample size)",
            "",
            "| Component | Old Weight | New Weight |",
            "|-----------|-----------|-----------|",
        ])
        for comp in ["momentum", "meanrev", "breakout", "news"]:
            lines.append(f"| {comp} | {old_weights.get(comp, 0.25):.3f} | {new_weights.get(comp, 0.25):.3f} |")

        auto = confidence >= 0.7
        lines.extend([
            "",
            f"**Auto-applied:** {'YES' if auto else 'NO (confidence too low)'}",
        ])

        # --- Shadow Model Performance ---
        lines.extend(self._shadow_section(shadow_eval))

        # --- Disk Usage ---
        lines.extend(self._disk_usage_section())

        # --- Memory Audit ---
        if memory_audit:
            lines.extend(self._memory_audit_section(memory_audit))

        lines.extend([
            "",
            "---",
            f"*Generated at {datetime.now().isoformat()}*",
        ])
        return "\n".join(lines)

    def _disk_usage_section(self) -> list[str]:
        """Generate disk usage stats for the daily report."""
        lines = [
            "",
            "## Disk Usage",
        ]

        # Overall disk
        try:
            usage = shutil.disk_usage(self.db_path.parent)
            total_gb = usage.total / (1024 ** 3)
            used_gb = usage.used / (1024 ** 3)
            free_gb = usage.free / (1024 ** 3)
            pct = (usage.used / usage.total) * 100
            lines.append(f"- **System Disk:** {used_gb:.1f} GB / {total_gb:.1f} GB ({pct:.1f}% used, {free_gb:.1f} GB free)")
        except Exception:
            lines.append("- **System Disk:** unavailable")

        # DB size
        try:
            db_size = self.db_path.stat().st_size
            if db_size > 1024 * 1024:
                lines.append(f"- **trading.db:** {db_size / (1024 * 1024):.2f} MB")
            else:
                lines.append(f"- **trading.db:** {db_size / 1024:.1f} KB")
        except Exception:
            lines.append("- **trading.db:** not found")

        # Portfolio JSON
        portfolio_path = self.db_path.parent / "portfolio.json"
        try:
            pf_size = portfolio_path.stat().st_size
            lines.append(f"- **portfolio.json:** {pf_size / 1024:.1f} KB")
        except Exception:
            pass

        # Trading logs dir
        logs_dir = self.db_path.parent / "trading_logs"
        try:
            total_logs = sum(f.stat().st_size for f in logs_dir.rglob("*") if f.is_file())
            if total_logs > 1024 * 1024:
                lines.append(f"- **trading_logs/:** {total_logs / (1024 * 1024):.2f} MB")
            else:
                lines.append(f"- **trading_logs/:** {total_logs / 1024:.1f} KB")
        except Exception:
            pass

        return lines

    def _shadow_comparison(self, date_str: str) -> dict | None:
        """Query shadow_predictions and evaluate Kalman/HMM vs existing system."""
        shadow_cfg = self.cfg.get("shadow_mode", {})
        if not shadow_cfg.get("enabled", False):
            return None
        try:
            runner = ShadowRunner(db_path=str(self.db_path), config=shadow_cfg)
            return runner.evaluate(date_str)
        except Exception:
            return None

    def _shadow_section(self, shadow_eval: dict) -> list[str]:
        """Generate Shadow Model Performance section for the report."""
        lines = [
            "",
            "## Shadow Model Performance",
        ]
        if shadow_eval is None:
            lines.append("- Shadow mode not enabled")
            return lines

        samples = shadow_eval.get("samples", 0)
        if samples == 0:
            lines.append("- No evaluated shadow predictions yet")
            return lines

        sc = shadow_eval.get("signal_comparisons", 0)
        lines.append(f"- Evaluated predictions: {samples}")
        lines.append(f"- Signal comparisons: {sc}")
        if sc > 0:
            lines.append(f"- Kalman signal accuracy: {shadow_eval.get('kalman_accuracy', 0):.1%}")
            lines.append(f"- Existing signal accuracy: {shadow_eval.get('existing_accuracy', 0):.1%}")
        hmm_s = shadow_eval.get("hmm_regime_samples", 0)
        if hmm_s > 0:
            lines.append(f"- HMM regime samples: {hmm_s}, correct: {shadow_eval.get('hmm_correct', 0)}")
        rec = shadow_eval.get("recommendation", "Need more data")
        lines.append(f"- **Recommendation:** {rec}")
        return lines

    def _memory_audit(self) -> dict | None:
        """Collect memory stats from all agents for auditing."""
        mem_cfg = self.cfg.get("agent_memory", {})
        if not mem_cfg.get("enabled", False):
            return None

        db_path = str(self.db_path)
        agents = ["pm", "news"]
        audit = {"agents": {}, "recommendations": []}

        for agent_name in agents:
            mem = AgentMemory(agent_name, db_path, mem_cfg)
            stats = mem.get_stats()
            audit["agents"][agent_name] = stats

            # Check for disable recommendation
            suggestion = mem.suggest_weight_adjustment(0.25)
            if suggestion and suggestion.get("action") == "disable":
                audit["recommendations"].append(
                    f"⚠️ {agent_name}: memory should be DISABLED (underperforming default)"
                )
            elif stats["total_observations"] > mem_cfg.get("rolling_window", 50) * 1.5:
                audit["recommendations"].append(
                    f"🧹 {agent_name}: consider pruning ({stats['total_observations']} observations)"
                )

        return audit

    def _memory_audit_section(self, audit: dict) -> list[str]:
        """Generate memory audit section for the daily report."""
        lines = [
            "",
            "## Agent Memory Audit",
        ]

        for agent_name, stats in audit.get("agents", {}).items():
            lines.append(f"\n### {agent_name.upper()}")
            lines.append(f"- Total observations: {stats['total_observations']}")
            lines.append(f"- Memory-influenced: {stats['memory_influenced_count']}")
            lines.append(f"- Disabled: {'YES ⚠️' if stats['disabled'] else 'No'}")

            if stats.get("regime_distribution"):
                lines.append("- Regime distribution:")
                for regime, count in stats["regime_distribution"].items():
                    lines.append(f"  - {regime}: {count}")

            if stats.get("outcome_stats"):
                lines.append("- Outcomes:")
                for outcome, data in stats["outcome_stats"].items():
                    lines.append(f"  - {outcome}: {data['count']} (avg PnL: ${data['avg_pnl']:.2f})")

        if audit.get("recommendations"):
            lines.append("\n### Recommendations")
            for rec in audit["recommendations"]:
                lines.append(f"- {rec}")

        return lines

