import yfinance as yf
import pandas as pd
from datetime import datetime

# Check what would have happened if we bought the strong signals at their signal time
# Focus on the 9:30 AM signals (first trading window scan)

print('=== HYPOTHETICAL TRADES — Feb 27, 2026 ===')
print('If we had bought at signal time, what would the P&L be by EOD?\n')

candidates = [
    ('MARA', 'BUY', '09:30', 0.78),
    ('NFLX', 'BUY', '09:30', 0.69),
    ('XYZ', 'BUY', '09:30', 0.70),
    ('MARA', 'BUY', '09:45', 0.79),
    ('XYZ', 'BUY', '09:45', 0.77),
    ('NFLX', 'BUY', '09:45', 0.98),
    ('RDW', 'SELL', '11:15', 0.28),
    ('CRML', 'SELL', '10:00', 0.48),
]

for sym, side, signal_time, score in candidates:
    try:
        data = yf.download(sym, start='2026-02-27', end='2026-02-28', interval='5m', progress=False)
        if len(data) == 0:
            data = yf.download(sym, period='1d', interval='5m', progress=False)
        
        if len(data) < 5:
            print(f'{sym:6s} — insufficient intraday data')
            continue

        # Find entry price near signal time
        # Use open price around 9:30-9:45 for morning signals
        opens = data.head(3)
        entry_price = data['Open'].iloc[0]
        if hasattr(entry_price, 'item'):
            entry_price = entry_price.item()
        
        # Current/EOD price
        exit_price = data['Close'].iloc[-1]
        if hasattr(exit_price, 'item'):
            exit_price = exit_price.item()
        
        # High and low of day for best/worst case
        day_high = data['High'].max()
        day_low = data['Low'].min()
        if hasattr(day_high, 'item'):
            day_high = day_high.item()
        if hasattr(day_low, 'item'):
            day_low = day_low.item()
        
        # Calculate hypothetical P&L per $500 position
        position_size = 500
        shares = position_size / entry_price
        
        if side == 'BUY':
            pnl_eod = (exit_price - entry_price) * shares
            pnl_best = (day_high - entry_price) * shares
            pnl_worst = (day_low - entry_price) * shares
            move_pct = ((exit_price - entry_price) / entry_price) * 100
        else:
            pnl_eod = (entry_price - exit_price) * shares
            pnl_best = (entry_price - day_low) * shares
            pnl_worst = (entry_price - day_high) * shares
            move_pct = ((entry_price - exit_price) / entry_price) * 100
        
        win = 'WIN' if pnl_eod > 0 else 'LOSS'
        print(f'{sym:6s} {side:4s} @{signal_time} score={score:+.2f} | entry=${entry_price:.2f} exit=${exit_price:.2f} | EOD: ${pnl_eod:+.2f} ({move_pct:+.1f}%) {win}')
        print(f'       Best: ${pnl_best:+.2f} (high=${day_high:.2f}) | Worst: ${pnl_worst:+.2f} (low=${day_low:.2f})')
        print()
        
    except Exception as e:
        print(f'{sym:6s} ERROR: {e}')
        print()
