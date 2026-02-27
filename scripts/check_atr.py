import yfinance as yf
import pandas as pd

for sym in ['NFLX', 'XYZ', 'MARA', 'IONQ', 'RDW', 'CRML', 'TMQ', 'BITF', 'ONDS', 'RGTI']:
    try:
        data = yf.download(sym, period='5d', interval='5m', progress=False)
        if len(data) > 20:
            atr_pct = ((data['High'] - data['Low']) / data['Close']).rolling(20).mean().iloc[-1]
            price = data['Close'].iloc[-1]
            if hasattr(price, 'item'):
                price = price.item()
            if hasattr(atr_pct, 'item'):
                atr_pct = atr_pct.item()
            status = 'PASS' if atr_pct >= 0.005 else 'FAIL'
            print(f'{sym:6s} price=${price:.2f} ATR%={atr_pct:.4f} ({atr_pct*100:.2f}%) {status} @ 0.50%')
        else:
            print(f'{sym:6s} insufficient data ({len(data)} bars)')
    except Exception as e:
        print(f'{sym:6s} ERROR: {e}')
