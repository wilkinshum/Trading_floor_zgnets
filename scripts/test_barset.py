# -*- coding: utf-8 -*-
"""Test BarSet access pattern"""
import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from datetime import datetime

client = StockHistoricalDataClient("PKNKYWEJY5OADXJKFL4MRYJ4GC", "6AYzrBrri3WMNLbYWavHWtUvUMW3Gdut3DgQ5zVBPmtn")

req = StockBarsRequest(
    symbol_or_symbols=["SPY", "NVDA"],
    timeframe=TimeFrame(5, TimeFrameUnit.Minute),
    start=datetime(2026, 2, 24),
    end=datetime(2026, 2, 25),
    limit=5
)
bars = client.get_stock_bars(req)

print(f"Type: {type(bars)}")
print(f"Dir: {[a for a in dir(bars) if not a.startswith('_')]}")

# Try .data
if hasattr(bars, 'data'):
    print(f"\n.data type: {type(bars.data)}")
    if isinstance(bars.data, dict):
        print(f".data keys: {list(bars.data.keys())}")
        for sym, sym_bars in list(bars.data.items())[:1]:
            print(f"\n{sym}: {len(sym_bars)} bars, type={type(sym_bars[0])}")
            b = sym_bars[0]
            print(f"  Attrs: {[a for a in dir(b) if not a.startswith('_')][:15]}")
            print(f"  t={b.timestamp} o={b.open} h={b.high} l={b.low} c={b.close} v={b.volume}")

# Try dict-like
try:
    spy = bars["SPY"]
    print(f"\nbars['SPY'] works: {len(spy)} bars")
except Exception as e:
    print(f"\nbars['SPY'] error: {e}")

# Try .df()
try:
    df = bars.df
    print(f"\nbars.df shape: {df.shape}")
    print(df.head())
except Exception as e:
    print(f"\nbars.df error: {e}")
