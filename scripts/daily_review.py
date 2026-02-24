import json, sqlite3

p = json.load(open("portfolio.json"))
print(f"Equity: ${p['equity']:.2f}")
print(f"Cash: ${p['cash']:.2f}")
print(f"Positions: {len(p['positions'])}")
print(f"From $5000 start: ${p['equity']-5000:.2f} ({(p['equity']-5000)/5000*100:.1f}%)")
print()

conn = sqlite3.connect("trading.db")
conn.row_factory = sqlite3.Row

rows = conn.execute("SELECT * FROM trades WHERE timestamp LIKE '2026-02-23%' ORDER BY timestamp").fetchall()
print("=== Today's Trades ===")
total_pnl = 0
for r in rows:
    print(f"  {r['side']} {r['symbol']} {r['quantity']} @ ${r['price']:.2f} | PnL: ${r['pnl']:.2f}")
    total_pnl += r['pnl']
print(f"  Net: ${total_pnl:.2f}")

sigs = conn.execute("SELECT * FROM signals WHERE timestamp LIKE '2026-02-23%' ORDER BY final_score DESC LIMIT 8").fetchall()
print(f"\n=== Top Signals (of {len(sigs)}) ===")
for s in sigs:
    print(f"  {s['symbol']:6s} score={s['final_score']:.3f}")

all_t = conn.execute("SELECT pnl FROM trades WHERE pnl IS NOT NULL AND pnl != 0").fetchall()
wins = [t['pnl'] for t in all_t if t['pnl'] > 0]
losses = [t['pnl'] for t in all_t if t['pnl'] < 0]
print(f"\n=== All-Time ===")
print(f"Wins: {len(wins)} (${sum(wins):.2f})" if wins else "Wins: 0")
print(f"Losses: {len(losses)} (${sum(losses):.2f})" if losses else "Losses: 0")
if wins or losses:
    print(f"Win rate: {len(wins)/(len(wins)+len(losses))*100:.0f}%")
    print(f"Profit factor: {abs(sum(wins))/abs(sum(losses)):.2f}" if losses else "Inf")
conn.close()
