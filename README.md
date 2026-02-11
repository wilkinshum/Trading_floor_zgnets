# Trading Floor ZG Nets

Bootstrap for a 7‑agent trading workflow with Agent Lightning instrumentation.

## Structure
- `src/trading_floor/` — core workflow code
- `src/trading_floor/agents/` — agent roles
- `configs/` — workflow configs
- `scripts/` — run helpers
- `trading_logs/` — output CSVs (trades/events)

## Quick start
```bash
python -m venv .venv
.\.venv\Scripts\activate
pip install -r requirements.txt
scripts\\run_workflow.cmd
```

## Report site
Generate JSON report:
```bash
.\.venv\Scripts\python scripts\generate_report.py
```

Serve live dashboard (auto refresh):
```bash
.\.venv\Scripts\python scripts\serve_report.py
```
Open: http://localhost:8000
Logs: http://localhost:8000/logs.html

## Watchdog (always-on background)
Keeps both the report server (8000) and Mission Control UI (3000) alive.

Start watchdog:
```bash
scripts\\run_watchdog.cmd
```

Stop watchdog:
- create an empty file `watchdog.stop` in repo root

Logs:
- `logs\\watchdog\\report.log`
- `logs\\watchdog\\mission-control.log`

## Agent Lightning
We instrument agents with `agentlightning` emit/tracing hooks so we can optimize prompts and decisions offline.
