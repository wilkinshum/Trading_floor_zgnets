import sqlite3, os
db = os.path.join(os.path.dirname(__file__), '..', 'data', 'trading.db')
conn = sqlite3.connect(db)
c = conn.cursor()

print("=== RECENT TRADES (last 5) ===")
try:
    c.execute("SELECT timestamp,action,symbol,quantity,price FROM trades ORDER BY timestamp DESC LIMIT 5")
    for r in c.fetchall():
        print(f"  {r[0]} | {r[1]} | {r[2]} | qty={r[3]} | ${r[4]}")
except Exception as e:
    print(f"  Error: {e}")

print("\n=== ACTIVE POSITIONS ===")
try:
    c.execute("SELECT symbol,quantity,avg_price FROM positions WHERE quantity != 0")
    for r in c.fetchall():
        print(f"  {r[0]}: qty={r[1]} @ ${r[2]:.2f}")
except Exception as e:
    print(f"  Error: {e}")

print("\n=== RECENT SIGNALS (last 5) ===")
try:
    c.execute("SELECT timestamp,symbol,signal,score FROM signals ORDER BY timestamp DESC LIMIT 5")
    for r in c.fetchall():
        print(f"  {r[0]} | {r[1]} | {r[2]} | score={r[3]}")
except Exception as e:
    print(f"  Error: {e}")

print("\n=== PORTFOLIO ===")
try:
    c.execute("SELECT cash,equity,timestamp FROM portfolio ORDER BY timestamp DESC LIMIT 1")
    r = c.fetchone()
    if r:
        print(f"  Cash: ${r[0]:,.2f} | Equity: ${r[1]:,.2f} | As of: {r[2]}")
except Exception as e:
    print(f"  Error: {e}")

conn.close()
