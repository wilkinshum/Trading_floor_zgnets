import sqlite3
db = sqlite3.connect('trading.db')
print('Signals today:', db.execute("SELECT COUNT(*) FROM signals WHERE timestamp LIKE '2026-02-20%'").fetchone()[0])
print('Trades today:', db.execute("SELECT COUNT(*) FROM trades WHERE timestamp LIKE '2026-02-20%'").fetchone()[0])
print('Last signal:', db.execute("SELECT timestamp, symbol, side FROM signals ORDER BY timestamp DESC LIMIT 1").fetchone())
