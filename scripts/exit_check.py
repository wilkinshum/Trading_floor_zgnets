"""Lightweight exit monitor wrapper - skips full monitor when no positions."""
import json, sys
from pathlib import Path

portfolio_path = Path(__file__).resolve().parent.parent / "portfolio.json"
p = json.load(open(portfolio_path))
positions = p.get("positions", {})

if not positions:
    print("[ExitMonitor] No positions. Skipping.")
    sys.exit(0)

print(f"[ExitMonitor] {len(positions)} position(s) found. Running full monitor...")
# Import and run the real exit monitor
from exit_monitor import main
main()
