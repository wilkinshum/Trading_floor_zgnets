import sys, sqlite3
sys.path.insert(0, 'src')
conn = sqlite3.connect('trading.db')
tables = [t[0] for t in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
print('Tables:', tables)
for t in tables:
    try:
        cnt = conn.execute(f"SELECT COUNT(*) FROM [{t}]").fetchone()[0]
        cols = [d[0] for d in conn.execute(f"SELECT * FROM [{t}] LIMIT 1").description]
        print(f"\n{t}: {cnt} rows | cols: {cols}")
        for r in conn.execute(f"SELECT * FROM [{t}] ORDER BY rowid DESC LIMIT 2").fetchall():
            print(f"  {r}")
    except Exception as e:
        print(f"{t}: error {e}")

# Today's signals
print("\n--- Signals today ---")
try:
    rows = conn.execute("SELECT * FROM signals WHERE timestamp LIKE '2026-02-19%'").fetchall()
    print(f"Count: {len(rows)}")
    for r in rows[:5]: print(f"  {r}")
except Exception as e:
    print(f"err: {e}")
