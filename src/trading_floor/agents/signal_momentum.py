from __future__ import annotations

import pandas as pd


class MomentumSignalAgent:
    def __init__(self, cfg, tracer):
        self.cfg = cfg
        self.tracer = tracer
        self.short = cfg.get("signals", {}).get("momentum_short", 5)

    def score(self, df: pd.DataFrame) -> float:
        self.tracer.emit_span("signal.momentum", {"rows": len(df)})
        if df.empty or len(df) < self.short:
            return 0.0
        closes = df["close"]
        sma = closes.rolling(self.short).mean().iloc[-1]
        return float((closes.iloc[-1] - sma) / sma)
