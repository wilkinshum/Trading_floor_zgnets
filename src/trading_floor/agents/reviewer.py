from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path


class NextDayReviewer:
    """
    Reviews yesterday's trades and produces actionable insights.
    Can be called by nightly cron or within the workflow.
    """

    def __init__(self, cfg, tracer):
        self.cfg = cfg
        self.tracer = tracer
        db_path = cfg.get("logging", {}).get("db_path", "trading.db")
        self.db_path = Path(db_path)

    def summarize(self, date_str: str | None = None) -> dict:
        """Analyze yesterday's (or given date's) trades and return insights."""
        self.tracer.emit_span("reviewer.summarize", {})

        if not self.db_path.exists():
            return {"date": date_str or "", "trades": 0, "insights": "No database found."}

        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row

        if date_str is None:
            latest_trade = conn.execute(
                "SELECT MAX(substr(timestamp, 1, 10)) AS trade_date FROM trades"
            ).fetchone()
            date_str = latest_trade["trade_date"] if latest_trade else None

            if not date_str:
                latest_signal = conn.execute(
                    "SELECT MAX(substr(timestamp, 1, 10)) AS signal_date FROM signals"
                ).fetchone()
                date_str = latest_signal["signal_date"] if latest_signal else None

            if not date_str:
                date_str = datetime.now().strftime("%Y-%m-%d")

        trades = conn.execute(
            "SELECT * FROM trades WHERE timestamp LIKE ?", (f"{date_str}%",)
        ).fetchall()

        signals = conn.execute(
            "SELECT * FROM signals WHERE timestamp LIKE ?", (f"{date_str}%",)
        ).fetchall()
        conn.close()

        if not trades:
            return {"date": date_str, "trades": 0, "insights": f"No trades on {date_str}."}

        # Calculate metrics
        pnls = [t["pnl"] for t in trades if t["pnl"] is not None]
        winners = [p for p in pnls if p > 0]
        losers = [p for p in pnls if p < 0]

        total_pnl = sum(pnls)
        win_rate = len(winners) / max(1, len(winners) + len(losers))

        # Best and worst trades
        best = max(trades, key=lambda t: t["pnl"] or 0)
        worst = min(trades, key=lambda t: t["pnl"] or 0)

        # Signal analysis
        sig_map = {s["symbol"]: dict(s) for s in signals}
        signal_insights = []
        for t in trades:
            sym = t["symbol"]
            pnl = t["pnl"] or 0
            if sym in sig_map:
                s = sig_map[sym]
                # Which component was strongest?
                comps = {
                    "momentum": abs(s.get("score_mom", 0) or 0),
                    "meanrev": abs(s.get("score_mean", 0) or 0),
                    "breakout": abs(s.get("score_break", 0) or 0),
                    "news": abs(s.get("score_news", 0) or 0),
                }
                strongest = max(comps, key=comps.get)
                correct = (pnl > 0)
                signal_insights.append({
                    "symbol": sym, "pnl": pnl, "strongest_signal": strongest,
                    "correct": correct
                })

        # Build actionable text
        lines = [
            f"## Review for {date_str}",
            f"- **Trades:** {len(trades)}",
            f"- **Win Rate:** {win_rate:.0%}",
            f"- **Total PnL:** ${total_pnl:.2f}",
            f"- **Best Trade:** {best['symbol']} (${(best['pnl'] or 0):.2f})",
            f"- **Worst Trade:** {worst['symbol']} (${(worst['pnl'] or 0):.2f})",
            "",
            "### Signal Insights",
        ]
        for si in signal_insights:
            mark = "✅" if si["correct"] else "❌"
            lines.append(f"- {mark} {si['symbol']}: PnL ${si['pnl']:.2f}, "
                         f"strongest signal = {si['strongest_signal']}")

        # Actionable recommendations
        lines.append("")
        lines.append("### Recommendations")
        if win_rate < 0.4:
            lines.append("- ⚠️ Win rate below 40%. Consider tightening entry thresholds.")
        if total_pnl < 0:
            lines.append("- ⚠️ Negative day. Review if stops were too wide or signals misaligned.")
        if winners and losers and abs(sum(losers)) > sum(winners):
            lines.append("- ⚠️ Losers outweigh winners. Consider reducing position size on low-conviction trades.")
        if not losers and winners:
            lines.append("- ✅ Clean sweep! Monitor for overconfidence bias.")
        if win_rate >= 0.6 and total_pnl > 0:
            lines.append("- ✅ Solid day. Current strategy parameters working well.")

        insights_text = "\n".join(lines)

        return {
            "date": date_str,
            "trades": len(trades),
            "win_rate": round(win_rate, 3),
            "total_pnl": round(total_pnl, 2),
            "insights": insights_text,
        }
