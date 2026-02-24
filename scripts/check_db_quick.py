import sqlite3, json
conn = sqlite3.connect('trading.db')
conn.row_factory = sqlite3.Row
c = conn.cursor()

tables = [t[0] for t in c.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
print("Tables:", tables)

for t in tables:
    count = c.execute(f"SELECT COUNT(*) FROM [{t}]").fetchone()[0]
    print(f"  {t}: {count} rows")

# Recent trades
if 'trades' in tables:
    rows = c.execute("SELECT * FROM trades ORDER BY timestamp DESC LIMIT 5").fetchall()
    for r in rows:
        print(dict(r))
