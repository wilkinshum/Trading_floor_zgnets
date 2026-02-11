import pandas as pd
from pathlib import Path
from datetime import datetime

class SignalLogger:
    def __init__(self, cfg):
        self.csv_path = Path(cfg["logging"].get("signals_csv", "trading_logs/signals.csv"))
        self.csv_path.parent.mkdir(parents=True, exist_ok=True)
        if not self.csv_path.exists():
            self._init_csv()

    def _init_csv(self):
        df = pd.DataFrame(columns=[
            "timestamp", "symbol", "side", 
            "score_mom", "score_mean", "score_break", "score_news", 
            "weight_mom", "weight_mean", "weight_break", "weight_news",
            "final_score", "outcome_pnl"
        ])
        df.to_csv(self.csv_path, index=False)

    def log_signal(self, data: dict):
        """
        Log the component scores and weights for a trade decision.
        """
        row = {
            "timestamp": data.get("timestamp", datetime.utcnow().isoformat()),
            "symbol": data["symbol"],
            "side": data.get("side", ""),
            "score_mom": data.get("components", {}).get("momentum", 0.0),
            "score_mean": data.get("components", {}).get("meanrev", 0.0),
            "score_break": data.get("components", {}).get("breakout", 0.0),
            "score_news": data.get("components", {}).get("news", 0.0),
            "weight_mom": data.get("weights", {}).get("momentum", 0.0),
            "weight_mean": data.get("weights", {}).get("meanrev", 0.0),
            "weight_break": data.get("weights", {}).get("breakout", 0.0),
            "weight_news": data.get("weights", {}).get("news", 0.0),
            "final_score": data.get("final_score", 0.0),
            "outcome_pnl": 0.0 # Filled later when trade closes
        }
        
        df = pd.DataFrame([row])
        df.to_csv(self.csv_path, mode='a', header=False, index=False)

    def update_outcome(self, symbol: str, pnl: float):
        """
        Find the last open signal log for this symbol and update PnL.
        This is tricky with CSV append, ideally use a DB. 
        For now, we just append a 'CLOSE' row or simple tracking.
        Actually, let's keep it simple: We log the signal inputs at entry. 
        The Optimizer will join this with trades.csv by timestamp/symbol later.
        """
        pass
