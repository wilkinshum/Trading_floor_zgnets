# ORB Phase 7 — Monitor (State Machine)

## Status: IN PROGRESS

## Objective
Build `ORBMonitor` — the central state machine that runs from 9:45–11:30 AM ET, managing candidate lifecycles from post-range through entry, position management, and exit.

## Architecture

### State Machine (per candidate)
```
WATCHING_FOR_CONSOLIDATION → WATCHING_FOR_BREAKOUT → WATCHING_FOR_RETEST → ENTRY_TRIGGERED → IN_POSITION → CLOSED → WAVE_RESET or DONE
                                                                                                    ↓
FAILED (breakout invalid) → retry ≤ 1: WATCHING_FOR_CONSOLIDATION, retry > 1: SKIPPED
SKIPPED (terminal)
```

### Polling Intervals (state-dependent)
| State | Interval |
|-------|----------|
| WATCHING_FOR_CONSOLIDATION | 30 sec |
| WATCHING_FOR_BREAKOUT | 15 sec |
| WATCHING_FOR_RETEST | 10 sec |
| IN_POSITION | 10 sec |

### Core Components
1. **CandidateState** — dataclass tracking per-symbol state
2. **ORBMonitor** — main loop + state transitions
3. **Detection functions** — is_consolidating(), is_breakout(), is_retest()
4. **State persistence** — crash recovery via `web/orb_state.json`

## Acceptance Criteria
- [ ] State machine with all transitions
- [ ] Detection algorithms from master plan
- [ ] State persistence + crash recovery
- [ ] Entry checklist validation (16 items)
- [ ] Wave tracking (max 3 per stock)
- [ ] Crash recovery from orb_state.json
- [ ] 20+ unit tests
- [ ] Architect review
- [ ] Git commit to qa-main
