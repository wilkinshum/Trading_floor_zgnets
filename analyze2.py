import sqlite3
conn = sqlite3.connect('trading.db')
conn.row_factory = sqlite3.Row

# Today's signals with components
print('=== TODAY SIGNALS (Feb 26) ===')
sigs = conn.execute("SELECT * FROM signals WHERE timestamp LIKE '2026-02-26%' ORDER BY timestamp").fetchall()
for s in sigs:
    d = dict(s)
    print(f"{d['timestamp'][:16]} {d['symbol']:6s} final={d.get('final_score',0):+.4f} mom={d.get('score_momentum',0):+.4f} mr={d.get('score_meanrev',0):+.4f} brk={d.get('score_breakout',0):+.4f} news={d.get('score_news',0):+.4f} w_mom={d.get('weight_momentum',0):.2f} w_brk={d.get('weight_breakout',0):.2f} w_news={d.get('weight_news',0):.2f}")

# BITF specific
print('\n=== BITF SIGNALS ===')
bitf = conn.execute("SELECT * FROM signals WHERE symbol='BITF' ORDER BY timestamp").fetchall()
for s in bitf:
    d = dict(s)
    print(f"{d['timestamp'][:16]} final={d.get('final_score',0):+.4f} mom={d.get('score_momentum',0):+.4f} brk={d.get('score_breakout',0):+.4f} news={d.get('score_news',0):+.4f}")

# Post-fix trades only (Feb 23+)
print('\n=== POST-FIX TRADES (Feb 23+) ===')
rows = conn.execute("SELECT * FROM trades WHERE timestamp >= '2026-02-23' ORDER BY timestamp").fetchall()
for r in rows:
    d = dict(r)
    print(f"{d['timestamp'][:16]} {d['symbol']:6s} {d['side']:4s} qty={d['quantity']} px={d['price']:.2f} pnl={d['pnl']:+.2f} score={d['score']:.3f}")

# Win/loss by trade pair analysis
print('\n=== ROUND-TRIP ANALYSIS (post-fix) ===')
trades = [dict(r) for r in rows]
pnl_trades = [t for t in trades if t['pnl'] and t['pnl'] != 0]
for t in pnl_trades:
    print(f"{t['timestamp'][:16]} {t['symbol']:6s} {t['side']:4s} pnl={t['pnl']:+.2f} score={t['score']:.3f} data={t.get('strategy_data','')}")

conn.close()
