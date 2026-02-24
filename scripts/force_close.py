"""Force close specified positions with current market price."""
import sys, csv, sqlite3
import yaml
import yfinance as yf
from pathlib import Path
from datetime import datetime
from trading_floor.portfolio import Portfolio

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = PROJECT_ROOT / "trading.db"
CSV_PATH = PROJECT_ROOT / "trading_logs" / "trades.csv"

def log_trade(symbol, side, quantity, price, pnl):
    ts = datetime.utcnow().isoformat()
    with open(CSV_PATH, "a", newline="") as f:
        csv.writer(f).writerow([ts, symbol, side, quantity, price, "", pnl])
    try:
        conn = sqlite3.connect(str(DB_PATH))
        conn.execute(
            "INSERT INTO trades (timestamp, symbol, side, quantity, price, score, pnl, strategy_data) VALUES (?,?,?,?,?,?,?,?)",
            (ts, symbol, side, quantity, price, 0.0, pnl, '{"source":"force_close"}')
        )
        conn.commit(); conn.close()
    except Exception as e:
        print(f"DB log error: {e}")

cfg = yaml.safe_load(open("configs/workflow.yaml"))
p = Portfolio(cfg)

symbols = sys.argv[1:] if len(sys.argv) > 1 else list(p.state.positions.keys())

if not symbols:
    print("No positions to close.")
    sys.exit(0)

# Fetch current prices
print("Fetching current prices...")
data = yf.download(symbols, period="1d", interval="1m", progress=False)
prices = {}
for sym in symbols:
    try:
        if len(symbols) == 1:
            val = data["Close"].iloc[-1]
            if hasattr(val, '__iter__'):
                val = list(val)[0]
            prices[sym] = float(val)
        else:
            prices[sym] = float(data["Close"][sym].iloc[-1])
    except Exception as e:
        print(f"  {sym}: price fetch error: {e}")

print(f"Prices: {prices}\n")

for sym in symbols:
    pos = p.state.positions.get(sym)
    if not pos:
        print(f"{sym}: no position found")
        continue
    
    price = prices.get(sym)
    if not price:
        print(f"{sym}: no price available, skipping")
        continue
    
    qty = abs(pos.quantity)
    side = "BUY" if pos.quantity < 0 else "SELL"
    pnl = p.execute(sym, side, price, quantity=qty)
    log_trade(sym, side, qty, price, pnl)
    print(f"CLOSED {sym}: {side} {qty} shares @ ${price:.2f} | PnL: ${pnl:.2f}")

p.save()

print("\nRemaining positions:")
if not p.state.positions:
    print("  (none)")
for sym, pos in p.state.positions.items():
    print(f"  {sym}: {pos.quantity} @ ${pos.avg_price:.2f}")

print(f"\nCash: ${p.state.cash:.2f}")
print(f"Equity: ${p.state.equity:.2f}")
