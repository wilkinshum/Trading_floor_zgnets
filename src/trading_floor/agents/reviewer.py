from __future__ import annotations

from pathlib import Path
import pandas as pd


class NextDayReviewer:
    def __init__(self, cfg, tracer):
        self.cfg = cfg
        self.tracer = tracer
        self.trades_csv = Path(cfg["logging"]["trades_csv"])

    def summarize(self):
        self.tracer.emit_span("reviewer.summarize", {"trades_csv": str(self.trades_csv)})
        if not self.trades_csv.exists():
            return {"trades": 0}
        df = pd.read_csv(self.trades_csv)
        if df.empty or "pnl" not in df.columns:
            return {"trades": len(df)}
        wins = (df["pnl"] > 0).sum()
        losses = (df["pnl"] < 0).sum()
        win_rate = wins / max(1, (wins + losses))
        gross_profit = df.loc[df["pnl"] > 0, "pnl"].sum()
        gross_loss = df.loc[df["pnl"] < 0, "pnl"].abs().sum()
        profit_factor = gross_profit / max(1e-9, gross_loss)
        return {
            "trades": len(df),
            "win_rate": float(win_rate),
            "profit_factor": float(profit_factor),
        }
