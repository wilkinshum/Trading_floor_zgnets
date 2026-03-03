import json

with open("backtest_results.json") as f:
    d = json.load(f)

print(f"Version: {d.get('version', 'v1')}")
print(f"Signal events: {d['config']['signal_events']}")
print()

for i, v in enumerate(d["top_10"]):
    w = v["weights"]
    test = v["test"]
    train = v["train"]
    gap = v["train_test_gap"]
    sort_score = test["composite"] - gap * 0.5
    print(f"#{i+1} | mom={w['momentum']} mean={w['meanrev']} brk={w['breakout']} news={w['news']} rsv={w['reserve']} | thr={v['threshold']}")
    print(f"     Train: {train['trades']}t WR={train['win_rate']:.1%} PF={train['pf']} PnL=${train['pnl']:.2f} comp={train['composite']}")
    print(f"     Test:  {test['trades']}t WR={test['win_rate']:.1%} PF={test['pf']} PnL=${test['pnl']:.2f} comp={test['composite']}")
    print(f"     Gap: {gap:.1%} | SORT SCORE: {sort_score:.4f}")
    exits = test.get("exit_breakdown", {})
    if exits:
        print(f"     Exits: stop={exits.get('stop_loss',0)} tp={exits.get('take_profit',0)} trail={exits.get('trailing_stop',0)} time={exits.get('time_exit',0)}")
    top_sym = v.get("top_symbols", {})
    worst_sym = v.get("worst_symbols", {})
    if top_sym:
        parts = [f"{s}(${d['pnl']:.0f})" for s, d in list(top_sym.items())[:3]]
        print(f"     Best: {', '.join(parts)}")
    if worst_sym:
        parts = [f"{s}(${d['pnl']:.0f})" for s, d in list(worst_sym.items())[:3]]
        print(f"     Worst: {', '.join(parts)}")
    print()

# Also show: what if we sort by PnL instead?
print("=" * 60)
print("ALTERNATIVE SORT: by Test PnL (descending)")
print("=" * 60)
by_pnl = sorted(d["top_10"], key=lambda v: v["test"]["pnl"], reverse=True)
for i, v in enumerate(by_pnl):
    w = v["weights"]
    test = v["test"]
    gap = v["train_test_gap"]
    print(f"#{i+1} PnL=${test['pnl']:.2f} | WR={test['win_rate']:.1%} PF={test['pf']} gap={gap:.1%} trades={test['trades']} | mom={w['momentum']} mean={w['meanrev']} brk={w['breakout']} news={w['news']}")
