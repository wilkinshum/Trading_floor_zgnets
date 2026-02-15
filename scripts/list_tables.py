import sqlite3, os, glob

# Find all .db files
for db_path in glob.glob(os.path.join(os.path.dirname(__file__), '..', 'data', '*.db')):
    print(f"\nDB: {db_path}")
    c = sqlite3.connect(db_path)
    tables = c.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    for t in tables:
        print(f"  Table: {t[0]}")
        cols = c.execute(f"PRAGMA table_info({t[0]})").fetchall()
        print(f"    Cols: {[col[1] for col in cols]}")
        row = c.execute(f"SELECT * FROM {t[0]} ORDER BY rowid DESC LIMIT 1").fetchone()
        print(f"    Last: {row}")
    c.close()

# Also check if data dir exists
data_dir = os.path.join(os.path.dirname(__file__), '..', 'data')
if os.path.isdir(data_dir):
    print(f"\nFiles in data/: {os.listdir(data_dir)}")
