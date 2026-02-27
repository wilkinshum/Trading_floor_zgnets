import sqlite3
conn = sqlite3.connect('trading.db')
conn.row_factory = sqlite3.Row

# Column names
cols = conn.execute("PRAGMA table_info(signals)").fetchall()
print("Signal columns:", [c[1] for c in cols])

# Today's BITF signals
print('\n=== BITF SIGNALS ===')
bitf = conn.execute("SELECT * FROM signals WHERE symbol='BITF' ORDER BY timestamp").fetchall()
for s in bitf:
    print(dict(s))

# Post-fix round trips
print('\n=== POST-FIX TRADES (Feb 23+) ===')
rows = conn.execute("SELECT * FROM trades WHERE timestamp >= '2026-02-23' ORDER BY timestamp").fetchall()
for r in rows:
    d = dict(r)
    pnl = d.get('pnl') or 0
    print(f"{str(d['timestamp'])[:16]} {d['symbol']:6s} {d['side']:4s} qty={d['quantity']} px={d['price']:.4f} pnl={pnl:+.2f} score={d['score']:.3f}")

conn.close()
