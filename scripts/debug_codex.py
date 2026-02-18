import json

for sid in ['10d78479-a97b-4892-a8e3-83549def6985', 'b98fbaf2-fe44-4081-97bf-2b7d7de05978']:
    path = f'C:\\Users\\moltbot\\.openclaw\\agents\\main\\sessions\\{sid}.jsonl'
    with open(path) as f:
        for line in f:
            entry = json.loads(line)
            t = entry.get('type')
            if t == 'session':
                print(f"\n=== {entry.get('id','?')[:30]} ===")
                print(f"Started: {entry.get('timestamp','?')}")
            elif t == 'model_change':
                print(f"Model: {entry.get('modelId','?')}")
            elif t == 'message':
                msg = entry.get('message', {})
                if msg.get('role') == 'user':
                    content = msg.get('content', '')
                    if isinstance(content, list):
                        for c in content:
                            if c.get('type') == 'text':
                                print(f"Task: {c['text'][:300]}")
                                break
                    else:
                        print(f"Task: {str(content)[:300]}")
                    break
