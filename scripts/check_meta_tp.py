import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
from trading_floor.data import YahooDataProvider

data = YahooDataProvider(interval='5m', lookback='1d')
md = data.fetch(['META'])
df = md['META'].df

tp_price = 668.18 * (1 - 0.05)  # $634.77

# Columns are prefixed: meta_open, meta_high, meta_low, meta_close
cols = list(df.columns)
low_col = [c for c in cols if 'low' in c.lower()][0]
high_col = [c for c in cols if 'high' in c.lower()][0]
open_col = [c for c in cols if 'open' in c.lower()][0]
close_col = [c for c in cols if 'close' in c.lower()][0]

print(f"META 5m candles today (TP for short = ${tp_price:.2f}):")
print(f"{'Time':>25s}  {'Open':>8s} {'High':>8s} {'Low':>8s} {'Close':>8s}  Flag")
print("-" * 80)

for i, row in df.iterrows():
    low = float(row[low_col])
    ts = str(row['datetime']) if 'datetime' in cols else str(i)
    flag = ""
    if low <= tp_price:
        flag = " <-- BELOW TP!"
    elif low <= tp_price * 1.005:
        flag = " (close to TP)"
    print(f"{ts:>25s}  {float(row[open_col]):>8.2f} {float(row[high_col]):>8.2f} {low:>8.2f} {float(row[close_col]):>8.2f}  {flag}")

low_of_day = float(df[low_col].min())
low_idx = df[low_col].idxmin()
low_time = str(df.loc[low_idx, 'datetime']) if 'datetime' in cols else str(low_idx)
print(f"\nLow of day: ${low_of_day:.2f} at {low_time}")
print(f"TP trigger price: ${tp_price:.2f}")
print(f"Would have triggered: {'YES' if low_of_day <= tp_price else 'NO'}")
