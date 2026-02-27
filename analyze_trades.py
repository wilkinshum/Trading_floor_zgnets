import sqlite3
conn = sqlite3.connect('trading.db')
conn.row_factory = sqlite3.Row

tables = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
print('Tables:', [t[0] for t in tables])

print('\n=== ALL TRADES ===')
rows = conn.execute('SELECT * FROM trades ORDER BY timestamp').fetchall()
for r in rows:
    print(dict(r))

print(f'\nTotal trades: {len(rows)}')
trades_with_pnl = [dict(r) for r in rows if r['pnl'] and r['pnl'] != 0]
print(f'Trades with PnL: {len(trades_with_pnl)}')
if trades_with_pnl:
    wins = [t for t in trades_with_pnl if t['pnl'] > 0]
    losses = [t for t in trades_with_pnl if t['pnl'] < 0]
    print(f'Winners: {len(wins)}, Losers: {len(losses)}')
    if wins:
        print(f'Avg win: ${sum(t["pnl"] for t in wins)/len(wins):.2f}')
        print(f'Total wins: ${sum(t["pnl"] for t in wins):.2f}')
    if losses:
        print(f'Avg loss: ${sum(t["pnl"] for t in losses)/len(losses):.2f}')
        print(f'Total losses: ${sum(t["pnl"] for t in losses):.2f}')
    print(f'Total PnL: ${sum(t["pnl"] for t in trades_with_pnl):.2f}')
    if losses and wins:
        pf = sum(t["pnl"] for t in wins) / abs(sum(t["pnl"] for t in losses))
        print(f'Profit factor: {pf:.2f}')
    print(f'Win rate: {len(wins)/(len(wins)+len(losses))*100:.1f}%')

print('\n=== SIGNALS (last 30) ===')
try:
    sigs = conn.execute('SELECT * FROM signals ORDER BY id DESC LIMIT 30').fetchall()
    for s in sigs:
        d = dict(s)
        ts = str(d.get('timestamp',''))[:16]
        sym = d.get('symbol','')
        fs = d.get('final_score', 0) or 0
        mom = d.get('score_momentum', 0) or 0
        brk = d.get('score_breakout', 0) or 0
        news = d.get('score_news', 0) or 0
        mr = d.get('score_meanrev', 0) or 0
        print(f'{ts} {sym:6s} score={fs:+.3f} mom={mom:+.3f} brk={brk:+.3f} mr={mr:+.3f} news={news:+.3f}')
except Exception as e:
    print(f'Signal query error: {e}')

# Check shadow predictions
print('\n=== SHADOW PREDICTIONS (last 10) ===')
try:
    sp = conn.execute('SELECT * FROM shadow_predictions ORDER BY rowid DESC LIMIT 10').fetchall()
    for s in sp:
        d = dict(s)
        print(f'{str(d.get("timestamp",""))[:16]} {d.get("symbol",""):6s} kalman={d.get("kalman_signal",0):+.3f} existing={d.get("existing_signal",0):+.3f} hmm={d.get("hmm_state","")}')
except Exception as e:
    print(f'Shadow error: {e}')

# Trades by date
print('\n=== TRADES BY DATE ===')
date_rows = conn.execute("SELECT date(timestamp) as d, count(*) as n, sum(pnl) as total_pnl FROM trades GROUP BY date(timestamp) ORDER BY d").fetchall()
for r in date_rows:
    print(f'{r[0]}: {r[1]} trades, PnL=${r[2] or 0:.2f}')

conn.close()
