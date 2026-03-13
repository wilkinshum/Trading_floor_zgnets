# ORB Trading Desk — Module Layout

## Overview
The ORB (Opening Range Breakout) desk is an independent day-trading strategy that operates alongside the existing swing desk. It trades the 15-minute opening range with consolidation → breakout → retest entries, targeting the measured move.

## Module Structure
```
src/trading_floor/strategies/orb/
├── __init__.py         # Package init
├── scanner.py          # Pre-market candidate selection (Phase 4)
├── range_marker.py     # 15-min candle snapshot + measured move calc (Phase 4)
├── monitor.py          # State machine: consolidation→breakout→retest (Phase 7)
├── executor.py         # Bracket orders via Alpaca API (Phase 6)
├── exit_manager.py     # Partials, trailing, time-stop, scoring (Phase 5)
├── reconciler.py       # Alpaca vs DB position check (Phase 8)
├── floor_manager.py    # Shared position cap with mutex (Phase 2)
└── README.md           # This file
```

## Supporting Files
- `configs/orb_config.yaml` — All ORB parameters
- `configs/regime_state.json` — Daily regime (shared with swing)
- `scripts/orb_workflow.py` — Orchestrator (Phase 9)
- `docs/orb_hypothesis_log.md` — Parameter change tracking
- `tests/test_orb_config.py` — Config validation tests
- `tests/test_orb_config_schema.py` — Schema validation tests

## Shared Infrastructure
- **AlpacaDataProvider** (`src/trading_floor/alpaca_data.py`) — IEX feed, 1-min bars
- **AlpacaBroker** (`src/trading_floor/alpaca_broker.py`) — Bracket/OCO orders
- **SelfLearner** (`src/trading_floor/review/`) — ORB-specific signal scoring
- **SQLite DB** (`trading.db`) — position_meta, orders, signal_accuracy tables

## Config
Primary: `configs/orb_config.yaml`
Integration: `configs/workflow.yaml` → `strategies.orb` section

## Build Status
| Phase | Component | Status |
|-------|-----------|--------|
| 1 | Config + Regime Schema | ✅ Complete |
| 2 | Floor Position Manager | ⬜ Not started |
| 3 | Portfolio Intelligence | ⬜ Not started |
| 4 | Scanner + Range Marker | ⬜ Not started |
| 5 | Exit Manager | ⬜ Not started |
| 6 | Executor | ⬜ Not started |
| 7 | Monitor (state machine) | ⬜ Not started |
| 8 | Reconciler | ⬜ Not started |
| 9 | Orchestrator + Crons | ⬜ Not started |
| 9.5 | Dry Run (1 week) | ⬜ Not started |
| 10 | Paper Test (100+ trades) | ⬜ Not started |
| 11 | Alpha Decay + Self-Healing | ⬜ Not started |
| 12 | Live Phase 1 | ⬜ Not started |
