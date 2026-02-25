import sqlite3, json
conn = sqlite3.connect('trading.db')
post_fix = conn.execute("SELECT symbol, side, quantity, price, pnl FROM trades WHERE timestamp >= '2026-02-20' AND pnl != 0").fetchall()
wins = [t for t in post_fix if t[4] > 0]
losses = [t for t in post_fix if t[4] < 0]
avg_win = sum(t[4] for t in wins) / len(wins)
avg_loss = sum(t[4] for t in losses) / len(losses)
total_win = sum(t[4] for t in wins)
total_loss = sum(abs(t[4]) for t in losses)
pf = total_win / total_loss
net = sum(t[4] for t in post_fix)
exp = net / len(post_fix)
print("POST-FIX (Feb 20-25):")
print(f"Trades: {len(post_fix)} | Wins: {len(wins)} | Losses: {len(losses)} | Win Rate: {len(wins)*100/len(post_fix):.0f}%")
print(f"Avg Win: +${avg_win:.2f} | Avg Loss: ${avg_loss:.2f} | Ratio: {abs(avg_win/avg_loss):.2f}x")
print(f"Profit Factor: {pf:.2f} | Net: ${net:+.2f} | Expectancy: ${exp:+.2f}/trade")
print(f"Max Win: +${max(t[4] for t in wins):.2f} | Max Loss: ${min(t[4] for t in losses):.2f}")
p = json.load(open("portfolio.json"))
print(f"Equity: ${p['equity']:.2f} | From $5000: ${p['equity']-5000:+.2f} ({(p['equity']-5000)/5000*100:+.1f}%)")
print(f"Open positions: {len(p.get('positions', {}))}")
for sym, data in p.get("positions", {}).items():
    print(f"  {sym}: {data['quantity']} @ ${data['avg_price']:.2f}")

# Days traded
by_date = conn.execute("SELECT date(timestamp) as d, sum(pnl), count(*) FROM trades WHERE pnl != 0 GROUP BY d ORDER BY d").fetchall()
print("\nPnL by day:")
for d in by_date:
    print(f"  {d[0]}: ${d[1]:+.2f} ({d[2]} trades)")
green = sum(1 for d in by_date if d[1] > 0)
red = sum(1 for d in by_date if d[1] <= 0)
print(f"Green days: {green} | Red days: {red}")
