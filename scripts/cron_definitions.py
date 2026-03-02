#!/usr/bin/env python3
"""
V4.0 Cron Definitions — reference for OpenClaw cron setup.
Print JSON for each cron job. NOT a cron manager — just a reference file.

Usage: python scripts/cron_definitions.py
"""

import json

BASE_DIR = r"C:\Users\moltbot\.openclaw\workspace\Trading_floor_zgnets"
PYTHON = rf"{BASE_DIR}\.venv\Scripts\python.exe"
RUN_PY = f"{PYTHON} src/trading_floor/run.py --config configs/workflow.yaml"

CRONS = {
    # ── UPDATED EXISTING ──
    "intraday_scan": {
        "name": "V4 Intraday Scan",
        "schedule": "cron */15 9-11 * * 1-5 America/New_York",
        "command": f"cd {BASE_DIR} && {RUN_PY} --intraday-scan",
        "notes": "Updated: now uses Alpaca broker via run.py --intraday-scan",
    },
    "preflight": {
        "name": "V4 Preflight Check",
        "schedule": "cron 30 7 * * 1-5 America/New_York",
        "command": f"cd {BASE_DIR} && {PYTHON} scripts/preflight_check.py",
        "notes": "Updated: now checks broker, review modules, DB tables",
    },
    # ── NEW ──
    "swing_am_scan": {
        "name": "V4 Swing AM Scan",
        "schedule": "cron 40 9 * * 1-5 America/New_York",
        "command": f"cd {BASE_DIR} && {RUN_PY} --swing-scan",
        "notes": "Gap continuation entry window",
    },
    "swing_pm_scan": {
        "name": "V4 Swing PM Scan",
        "schedule": "cron 50 15 * * 1-5 America/New_York",
        "command": f"cd {BASE_DIR} && {RUN_PY} --swing-scan",
        "notes": "Trend confirmation entry window",
    },
    "swing_exits": {
        "name": "V4 Swing Exits Check",
        "schedule": "cron 0 10 * * 1-5 America/New_York",
        "command": f"cd {BASE_DIR} && {RUN_PY} --swing-exits",
        "notes": "Check TP/SL/trailing stop/max hold for swing positions",
    },
    "intraday_force_close": {
        "name": "V4 Intraday Force Close",
        "schedule": "cron 45 15 * * 1-5 America/New_York",
        "command": f"cd {BASE_DIR} && {RUN_PY} --intraday-close",
        "notes": "Force close all intraday positions before market close",
    },
    "nightly_review": {
        "name": "V4 Nightly Self-Learning Review",
        "schedule": "cron 0 20 * * 1-5 America/New_York",
        "command": f"cd {BASE_DIR} && {RUN_PY} --nightly-review",
        "notes": "Self-learner nightly report + Friday weekly apply",
    },
}


def main():
    print("=" * 60)
    print("Trading Floor V4.0 — Cron Definitions")
    print("=" * 60)
    for key, cron in CRONS.items():
        print(f"\n## {cron['name']} ({key})")
        print(f"  Schedule: {cron['schedule']}")
        print(f"  Command:  {cron['command']}")
        print(f"  Notes:    {cron['notes']}")
    print("\n\n## JSON export:")
    print(json.dumps(CRONS, indent=2))


if __name__ == "__main__":
    main()
