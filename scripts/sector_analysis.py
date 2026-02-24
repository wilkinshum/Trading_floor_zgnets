import io, sys, sqlite3
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
sys.path.insert(0, 'src')

from trading_floor.sector_map import SECTOR_MAP

db = sqlite3.connect('trading.db')

# Feb 24 signals by sector
rows = db.execute('''
    SELECT symbol, score_mom, score_mean, score_break, score_news, final_score, timestamp
    FROM signals WHERE date(timestamp) = '2026-02-24'
    ORDER BY final_score DESC
''').fetchall()

print("Feb 24 Signals by Sector")
print("=" * 85)
header = f"{'Sector':20s} {'Sym':6s} {'Mom':>7s} {'Mean':>7s} {'Break':>7s} {'News':>7s} {'Final':>7s} {'Time':>6s}"
print(header)
print("-" * 85)

from collections import defaultdict
sector_scores = defaultdict(list)

for r in rows:
    sym, mom, mean, brk, news, final, ts = r
    sector = SECTOR_MAP.get(sym, {}).get('sector', '???')
    sector_scores[sector].append(final)
    time_str = ts[11:16] if len(ts) > 16 else ts
    print(f"{sector:20s} {sym:6s} {mom:+.3f}  {mean:+.3f}  {brk:+.3f}  {news:+.3f}  {final:+.3f}  {time_str}")

print()
print("Sector Avg Scores:")
print("=" * 40)
for sector, scores in sorted(sector_scores.items(), key=lambda x: -sum(x[1])/len(x[1])):
    avg = sum(scores) / len(scores)
    n = len(scores)
    emoji = "UP" if avg > 0.05 else "DOWN" if avg < -0.05 else "FLAT"
    print(f"  {sector:20s} avg={avg:+.3f}  n={n:2d}  [{emoji}]")

# Cross-sector correlation check
print()
print("Cross-Sector Signal Correlation (same timestamp)")
print("=" * 50)
# Group by timestamp
from collections import defaultdict
ts_sectors = defaultdict(lambda: defaultdict(list))
for r in rows:
    sym, mom, mean, brk, news, final, ts = r
    sector = SECTOR_MAP.get(sym, {}).get('sector', '???')
    ts_sectors[ts[:16]][sector].append(final)

# Check if sectors move together
timestamps = sorted(ts_sectors.keys())
if len(timestamps) >= 2:
    sector_names = sorted(set(s for ts_data in ts_sectors.values() for s in ts_data.keys()))
    # Build per-timestamp avg by sector
    sector_ts = {s: [] for s in sector_names}
    for ts in timestamps:
        for s in sector_names:
            vals = ts_sectors[ts].get(s, [])
            sector_ts[s].append(sum(vals)/len(vals) if vals else 0)
    
    # Simple correlation pairs
    import numpy as np
    print(f"\nSector pairs with |corr| > 0.5:")
    pairs_found = False
    for i, s1 in enumerate(sector_names):
        for s2 in sector_names[i+1:]:
            a = np.array(sector_ts[s1])
            b = np.array(sector_ts[s2])
            if np.std(a) == 0 or np.std(b) == 0:
                continue
            corr = float(np.corrcoef(a, b)[0, 1])
            if abs(corr) > 0.5:
                direction = "same direction" if corr > 0 else "OPPOSITE"
                print(f"  {s1:20s} <-> {s2:20s}  corr={corr:+.2f}  [{direction}]")
                pairs_found = True
    if not pairs_found:
        print("  (none found with single day data)")

# Also check: how many SHORT signals were strong enough?
print()
print("Strong Short Candidates (final < -0.10):")
print("=" * 50)
short_rows = db.execute('''
    SELECT symbol, final_score, score_mom, score_news, timestamp
    FROM signals WHERE date(timestamp) = '2026-02-24' AND final_score < -0.10
    ORDER BY final_score
''').fetchall()
for r in short_rows:
    sym, final, mom, news, ts = r
    sector = SECTOR_MAP.get(sym, {}).get('sector', '???')
    print(f"  {ts[11:16]} {sym:6s} {sector:20s} final={final:+.3f} mom={mom:+.3f} news={news:+.3f}")
if not short_rows:
    print("  (none)")
