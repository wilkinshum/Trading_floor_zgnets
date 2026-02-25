import sqlite3
c = sqlite3.connect('trading.db')
cur = c.cursor()

# All signals with components
print("=== ALL RECENT SIGNALS ===")
cur.execute("SELECT symbol, timestamp, score_mom, score_mean, score_break, score_news, final_score FROM signals ORDER BY timestamp DESC LIMIT 30")
print([d[0] for d in cur.description])
for r in cur.fetchall():
    print(r)

# Check signals for winning symbols
for sym in ['CRML', 'TMQ', 'ONDS', 'RGTI', 'IREN']:
    print(f"\n=== {sym} SIGNALS ===")
    cur.execute(f"SELECT timestamp, score_mom, score_mean, score_break, score_news, final_score FROM signals WHERE symbol=? ORDER BY timestamp DESC LIMIT 3", (sym,))
    for r in cur.fetchall():
        print(r)
