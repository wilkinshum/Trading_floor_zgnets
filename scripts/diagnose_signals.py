"""Diagnostic: run signals on a few symbols and show values."""
import sys, math
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import yaml
import yfinance as yf
import pandas as pd
from trading_floor.agents.signal_momentum import MomentumSignalAgent
from trading_floor.agents.signal_meanreversion import MeanReversionSignalAgent
from trading_floor.agents.signal_breakout import BreakoutSignalAgent
from trading_floor.agents.news import NewsSentimentAgent
from trading_floor.signal_normalizer import SignalNormalizer

cfg = yaml.safe_load(open(Path(__file__).resolve().parent.parent / "configs" / "workflow.yaml"))

class FakeTracer:
    def emit_span(self, *a, **kw): pass
    def emit_reward(self, *a, **kw): pass

tracer = FakeTracer()
mom_agent = MomentumSignalAgent(cfg, tracer)
mean_agent = MeanReversionSignalAgent(cfg, tracer)
brk_agent = BreakoutSignalAgent(cfg, tracer)
news_agent = NewsSentimentAgent(cfg, tracer)
normalizer = SignalNormalizer(cfg.get("signals", {}).get("norm_lookback", 100))

weights = cfg.get("signals", {}).get("weights", {})
threshold = cfg.get("signals", {}).get("trade_threshold", 0.05)

# Test with a few symbols
test_syms = ["NVDA", "TSLA", "META", "SPY", "IONQ", "VRT", "ONDS", "RKLB", "CRWD", "AMD"]
print(f"Threshold: {threshold} | Weights: {weights}\n")

for sym in test_syms:
    try:
        raw = yf.download(sym, period="5d", interval="5m", progress=False)
        if raw.empty:
            print(f"{sym}: NO DATA"); continue
        
        # Flatten multi-level columns
        if isinstance(raw.columns, pd.MultiIndex):
            raw.columns = [c[0].lower() if isinstance(c, tuple) else c.lower() for c in raw.columns]
        else:
            raw.columns = [c.lower() for c in raw.columns]
        
        mom_raw = mom_agent.score(raw)
        mean_raw = mean_agent.score(raw)
        brk_raw = brk_agent.score(raw)
        news_raw = news_agent.get_sentiment(sym)
        
        mom_norm = normalizer.normalize(sym, "momentum", mom_raw)
        mean_norm = normalizer.normalize(sym, "meanrev", mean_raw)
        brk_norm = normalizer.normalize(sym, "breakout", brk_raw)
        
        final = (mom_norm * weights.get("momentum", 0.25) +
                 mean_norm * weights.get("meanrev", 0.25) +
                 brk_norm * weights.get("breakout", 0.25) +
                 news_raw * weights.get("news", 0.25))
        
        tag = "TRADE!" if abs(final) >= threshold else "no trade"
        
        print(f"{sym:6s} | Raw: mom={mom_raw:+.5f} mean={mean_raw:+.5f} brk={brk_raw:+.5f} news={news_raw:+.3f}")
        print(f"       | Nrm: mom={mom_norm:+.5f} mean={mean_norm:+.5f} brk={brk_norm:+.5f}")
        print(f"       | Final: {final:+.6f} [{tag}]  mom+mean={mom_norm+mean_norm:+.5f}")
        print()
    except Exception as e:
        print(f"{sym}: ERROR {e}")

# Show what happens to normalizer over multiple symbols
print(f"\n--- NORMALIZER HISTORY ---")
for key, buf in normalizer._history.items():
    vals = list(buf)
    print(f"{key}: {len(vals)} entries | min={min(vals):+.6f} max={max(vals):+.6f} avg={sum(vals)/len(vals):+.6f}")
