import sqlite3, json

db = sqlite3.connect("trading.db")
db.row_factory = sqlite3.Row

print("=== RGTI/IREN/TMQ SIGNALS ===")
for r in db.execute("SELECT timestamp, symbol, score_mom, score_mean, score_break, score_news, final_score FROM signals WHERE date(timestamp)='2026-02-24' AND symbol IN ('RGTI','IREN','TMQ') ORDER BY symbol, timestamp"):
    d = dict(r)
    ts = d['timestamp'][:16]
    sym = d['symbol']
    fs = float(d['final_score'])
    m = float(d['score_mom'])
    mn = float(d['score_mean'])
    b = float(d['score_break'])
    n = float(d['score_news'])
    print(f"  {ts} | {sym:5s} | final={fs:.3f} | mom={m:.3f} | mean={mn:.3f} | brk={b:.3f} | news={n:.3f}")

print("\n=== TOP 15 SIGNALS TODAY (by final_score) ===")
for r in db.execute("SELECT timestamp, symbol, final_score, score_mom, score_break, score_news FROM signals WHERE date(timestamp)='2026-02-24' ORDER BY final_score DESC LIMIT 15"):
    d = dict(r)
    ts = d['timestamp'][:16]
    sym = d['symbol']
    fs = float(d['final_score'])
    m = float(d['score_mom'])
    b = float(d['score_break'])
    n = float(d['score_news'])
    print(f"  {ts} | {sym:6s} final={fs:.3f} mom={m:.3f} brk={b:.3f} news={n:.3f}")

print("\n=== SECTOR CHECK: Quantum/AI/Mining stocks today ===")
sector_syms = ['RGTI','IONQ','IREN','MARA','HUT','RIOT','CORZ','BITF']
for r in db.execute("SELECT timestamp, symbol, final_score, score_news FROM signals WHERE date(timestamp)='2026-02-24' AND symbol IN ('RGTI','IONQ','IREN','MARA','HUT','RIOT','CORZ','BITF') ORDER BY symbol, timestamp"):
    d = dict(r)
    print(f"  {d['timestamp'][:16]} | {d['symbol']:5s} | final={float(d['final_score']):.3f} | news={float(d['score_news']):.3f}")

print("\n=== SHADOW PREDICTIONS ===")
for r in db.execute("SELECT timestamp, kalman_signal, hmm_regime, hmm_confidence FROM shadow_predictions WHERE date(timestamp)='2026-02-24' ORDER BY timestamp DESC LIMIT 8"):
    d = dict(r)
    print(f"  {d['timestamp'][:16]} | kalman={d['kalman_signal']} | hmm={d['hmm_regime']} conf={d['hmm_confidence']}")
