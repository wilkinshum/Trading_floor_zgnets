import sqlite3, json

db = sqlite3.connect('trading.db')
db.row_factory = sqlite3.Row

print('=== TODAY SIGNALS (Feb 27) ===')
rows = db.execute("SELECT timestamp, symbol, final_score, score_mom, score_break, score_news, score_mean, weight_mom, weight_break, weight_news FROM signals WHERE date(timestamp) = '2026-02-27' ORDER BY timestamp").fetchall()
for r in rows:
    ts = (r['timestamp'] or '?')[:16]
    sym = r['symbol'] or '?'
    fs = r['final_score'] or 0
    mom = r['score_mom'] or 0
    brk = r['score_break'] or 0
    news = r['score_news'] or 0
    mean = r['score_mean'] or 0
    wm = r['weight_mom'] or 0
    wb = r['weight_break'] or 0
    wn = r['weight_news'] or 0
    side = 'BUY' if fs > 0 else 'SELL'
    print(f'{ts} {sym:6s} {side:4s} final={fs:+.4f} mom={mom:+.3f} brk={brk:+.3f} news={news:+.3f} mean={mean:+.3f} | w_mom={wm:.2f} w_brk={wb:.2f} w_news={wn:.2f}')

print()
print(f'Total signals today: {len(rows)}')

print()
print('=== SIGNAL STATS BY STOCK (today) ===')
stats = db.execute("""
    SELECT symbol, COUNT(*) as cnt, 
           AVG(final_score) as avg_score,
           MAX(final_score) as max_score,
           MIN(final_score) as min_score,
           AVG(score_mom) as avg_mom,
           AVG(score_break) as avg_brk,
           AVG(score_news) as avg_news,
           AVG(score_mean) as avg_mean
    FROM signals WHERE date(timestamp) = '2026-02-27'
    GROUP BY symbol ORDER BY avg_score DESC
""").fetchall()
for s in stats:
    print(f'{s["symbol"]:6s} cnt={s["cnt"]:2d} avg={s["avg_score"]:+.4f} max={s["max_score"]:+.4f} min={s["min_score"]:+.4f} | mom={s["avg_mom"]:+.3f} brk={s["avg_brk"]:+.3f} news={s["avg_news"]:+.3f} mean={s["avg_mean"]:+.3f}')

print()
print('=== THRESHOLD ANALYSIS ===')
for thresh in [0.10, 0.15, 0.20, 0.25, 0.30]:
    cnt = db.execute("SELECT COUNT(*) FROM signals WHERE date(timestamp)='2026-02-27' AND ABS(final_score) >= ?", (thresh,)).fetchone()[0]
    print(f'  >= {thresh:.2f}: {cnt} signals would pass')

print()
print('=== LAST 5 TRADING DAYS THRESHOLD ANALYSIS ===')
for thresh in [0.10, 0.15, 0.20, 0.25, 0.30]:
    cnt = db.execute("SELECT COUNT(*) FROM signals WHERE date(timestamp) >= '2026-02-20' AND ABS(final_score) >= ?", (thresh,)).fetchone()[0]
    print(f'  >= {thresh:.2f}: {cnt} signals would pass')

print()
print('=== ALL TRADES HISTORY ===')
trades = db.execute('SELECT * FROM trades ORDER BY rowid').fetchall()
col_names = [c[1] for c in db.execute('PRAGMA table_info(trades)').fetchall()]
for t in trades:
    d = dict(zip(col_names, t))
    pnl = d.get('pnl', 0) or 0
    score = d.get('score', 0) or 0
    win = 'WIN' if pnl > 0 else 'LOSS'
    print(f'{d.get("timestamp","?")[:16]} {d.get("symbol","?"):6s} {d.get("side","?"):5s} qty={d.get("quantity","?")} price={d.get("price","?")} pnl={pnl:+.2f} score={score:+.3f} {win}')

print()
print('=== WINNING TRADES SIGNAL SCORES vs LOSING ===')
winners = db.execute("SELECT symbol, score, pnl FROM trades WHERE pnl > 0").fetchall()
losers = db.execute("SELECT symbol, score, pnl FROM trades WHERE pnl <= 0").fetchall()
if winners:
    avg_w = sum(abs(w['score'] or 0) for w in winners) / len(winners)
    print(f'Winners ({len(winners)}): avg |score| = {avg_w:.4f}')
    for w in winners:
        print(f'  {w["symbol"]:6s} score={w["score"]:+.4f} pnl={w["pnl"]:+.2f}')
if losers:
    avg_l = sum(abs(l['score'] or 0) for l in losers) / len(losers)
    print(f'Losers ({len(losers)}): avg |score| = {avg_l:.4f}')
    for l in losers:
        print(f'  {l["symbol"]:6s} score={l["score"]:+.4f} pnl={l["pnl"]:+.2f}')

print()
print('=== PRICE DATA CHECK (stocks rejected for ATR) ===')
# Check if NFLX, XYZ actually had low ATR or if threshold is wrong
try:
    import yfinance as yf
    for sym in ['NFLX', 'XYZ', 'MARA', 'IONQ', 'RDW']:
        data = yf.download(sym, period='5d', interval='5m', progress=False)
        if len(data) > 20:
            atr_pct = ((data['High'] - data['Low']) / data['Close']).rolling(20).mean().iloc[-1]
            price = data['Close'].iloc[-1]
            print(f'{sym:6s} price=${price:.2f} ATR%={atr_pct:.4f} ({"PASS" if atr_pct >= 0.005 else "FAIL"} @ 0.50%)')
except Exception as e:
    print(f'Price check error: {e}')
