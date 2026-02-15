import sqlite3
c = sqlite3.connect('trading.db')
cur = c.cursor()
tables = [r[0] for r in cur.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
print("Tables:", tables)
for t in tables:
    cnt = cur.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
    last = cur.execute(f"SELECT * FROM {t} ORDER BY rowid DESC LIMIT 1").fetchone()
    print(f"  {t}: {cnt} rows | last: {last}")
