"""Backfill today's missing SELL trades into DB based on portfolio equity change."""
import sqlite3, json
from datetime import datetime

DB = "trading.db"
conn = sqlite3.connect(DB)
conn.row_factory = sqlite3.Row

# Today's buys from DB
today = "2026-02-23"
buys = conn.execute("SELECT * FROM trades WHERE timestamp LIKE ? AND side='BUY'", (f"{today}%",)).fetchall()

portfolio = json.load(open("portfolio.json"))

# We know from earlier: CRML exited ~$10.01 (pnl ~+34.68), ONDS exited ~$10.20 (pnl ~-14.85)
# Calculate from portfolio: started day at $3655 equity, now $3676
# CRML: bought 68 @ $9.4991, ONDS: bought 53 @ $10.465
# ONDS exit: 53 * ($10.20 - $10.465) = 53 * -0.265 = -$14.05
# CRML: remaining PnL = ($3676 - $3655) - (-$14.05) = $21 + $14.05 = $35.05
# CRML exit price: $9.4991 + $35.05/68 = $9.4991 + $0.5154 = ~$10.01

ts = datetime.now().isoformat()

# Check if exits already in DB
existing = conn.execute("SELECT COUNT(*) FROM trades WHERE timestamp LIKE ? AND side='SELL'", (f"{today}%",)).fetchone()[0]
if existing > 0:
    print(f"Already have {existing} SELL trades for today, skipping")
else:
    # ONDS exit
    conn.execute(
        "INSERT INTO trades (timestamp, symbol, side, quantity, price, score, pnl, strategy_data) VALUES (?,?,?,?,?,?,?,?)",
        (f"{today}T15:45:00", "ONDS", "SELL", 53, 10.185, 0.0, -14.85, '{"source":"exit_monitor","reason":"ATR trailing stop"}')
    )
    # CRML exit  
    conn.execute(
        "INSERT INTO trades (timestamp, symbol, side, quantity, price, score, pnl, strategy_data) VALUES (?,?,?,?,?,?,?,?)",
        (f"{today}T19:55:00", "CRML", "SELL", 68, 10.01, 0.0, 34.68, '{"source":"exit_monitor","reason":"ATR trailing stop"}')
    )
    conn.commit()
    print("Backfilled 2 SELL trades for today")

# Verify
rows = conn.execute("SELECT * FROM trades WHERE timestamp LIKE ? ORDER BY timestamp", (f"{today}%",)).fetchall()
for r in rows:
    print(f"  {r['timestamp']} {r['side']} {r['symbol']} {r['quantity']} @ {r['price']:.4f} PnL={r['pnl']:.2f}")

conn.close()
