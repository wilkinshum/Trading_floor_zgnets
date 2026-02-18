import json
from pathlib import Path

sessions_dir = Path(r"C:\Users\moltbot\.openclaw\agents\main\sessions")
claude_input = 0
claude_output = 0
claude_calls = 0

for f in sessions_dir.glob("*.jsonl"):
    for line in f.read_text(encoding="utf-8", errors="ignore").splitlines():
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except:
            continue
        msg = entry.get("message") or {}
        usage = msg.get("usage") or {}
        model = msg.get("model") or ""
        if "claude" not in model:
            continue
        
        inp = usage.get("input") or 0
        out = usage.get("output") or 0
        cache_read = usage.get("cacheRead") or 0
        total_toks = usage.get("totalTokens") or 0
        
        if total_toks > 0 and total_toks > (inp + cache_read + out):
            actual_input = total_toks - out
        else:
            actual_input = inp + cache_read
        
        claude_input += actual_input
        claude_output += out
        claude_calls += 1

print(f"Claude input: {claude_input:,}")
print(f"Claude output: {claude_output:,}")
print(f"Claude calls: {claude_calls}")
print(f"Claude total: {claude_input + claude_output:,}")
