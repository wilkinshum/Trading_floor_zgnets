import sqlite3
c = sqlite3.connect('trading.db')
rows = c.execute("SELECT timestamp,symbol,side,quantity,price,pnl FROM trades ORDER BY timestamp DESC LIMIT 5").fetchall()
for r in rows:
    print(r)
