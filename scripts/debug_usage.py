import json
f = open(r'C:\Users\moltbot\.openclaw\agents\main\sessions\19707da9-02c7-43f0-be27-86c9fd1ba7d8.jsonl')
for i, line in enumerate(f):
    if i > 10:
        break
    entry = json.loads(line)
    msg = entry.get('message', {})
    usage = msg.get('usage', {})
    if usage:
        print(f'keys={list(usage.keys())}')
        print(f'input={usage.get("input")} output={usage.get("output")} cacheRead={usage.get("cacheRead")} totalTokens={usage.get("totalTokens")}')
        break
