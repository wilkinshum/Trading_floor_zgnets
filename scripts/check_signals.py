import sqlite3
conn = sqlite3.connect('trading.db')
c = conn.cursor()

c.execute("SELECT symbol, score_mom, score_mean, score_break, score_news, final_score FROM signals WHERE timestamp LIKE '2026-02-23%' ORDER BY final_score DESC LIMIT 10")
rows = c.fetchall()
print("Today's signals (top 10 by score):")
for r in rows:
    print(f"  {r[0]:6s} mom={r[1]:+.3f} mean={r[2]:+.3f} break={r[3]:+.3f} news={r[4]:+.3f} => final={r[5]:+.4f}")

c.execute("SELECT COUNT(*) FROM signals WHERE timestamp LIKE '2026-02-23%' AND score_news != 0")
nz = c.fetchone()[0]
c.execute("SELECT COUNT(*) FROM signals WHERE timestamp LIKE '2026-02-23%'")
tot = c.fetchone()[0]
print(f"\nNews non-zero: {nz}/{tot}")

# Check Feb 19 - the date reported as 0 rows
c.execute("SELECT COUNT(*) FROM signals WHERE timestamp LIKE '2026-02-19%'")
feb19 = c.fetchone()[0]
print(f"Feb 19 signals: {feb19}")

# Check if signals are per-run or cumulative
c.execute("SELECT DISTINCT timestamp FROM signals WHERE timestamp LIKE '2026-02-23%'")
ts = c.fetchall()
print(f"\nDistinct timestamps today: {len(ts)}")
for t in ts:
    print(f"  {t[0]}")

conn.close()
