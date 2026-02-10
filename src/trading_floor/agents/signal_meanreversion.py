from __future__ import annotations

import pandas as pd


class MeanReversionSignalAgent:
    def __init__(self, cfg, tracer):
        self.cfg = cfg
        self.tracer = tracer
        self.long = cfg.get("signals", {}).get("meanrev_long", 20)

    def score(self, df: pd.DataFrame) -> float:
        self.tracer.emit_span("signal.meanreversion", {"rows": len(df)})
        if df.empty or len(df) < self.long:
            return 0.0
        closes = df["close"]
        sma = closes.rolling(self.long).mean().iloc[-1]
        return float((sma - closes.iloc[-1]) / sma)
