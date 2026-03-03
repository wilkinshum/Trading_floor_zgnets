import json

with open('backtest_results.json','r',encoding='utf-8') as f:
    d=json.load(f)

print('version:', d.get('version'))
print('events:', d.get('config',{}).get('signal_events'))

for i, v in enumerate(d.get('top_10', []), start=1):
    w=v['weights']
    t=v['test']
    tr=v['train']
    gap=v.get('train_test_gap', 0)
    print(f"#{i}: testPnL=${t['pnl']:.2f} WR={t['win_rate']:.1%} PF={t['pf']} trades={t['trades']} sharpe={t.get('sharpe','-')} gap={gap:.1%} | mom={w['momentum']} mean={w['meanrev']} brk={w['breakout']} news={w['news']} thr={v['threshold']}")
