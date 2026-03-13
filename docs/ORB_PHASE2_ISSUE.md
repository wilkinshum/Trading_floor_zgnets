# Issue: ORB Phase 2 — Floor Position Manager

## Description
Build the cross-desk position limit enforcer that prevents exceeding max positions across ORB + Swing desks.

## Acceptance Criteria
- [ ] `FloorPositionManager` class implemented with mutex + DB slot reservation
- [ ] Enforces: max 2 ORB, max 3 swing, max 5 total
- [ ] Enforces: max 1 per GICS sector per desk
- [ ] Stale pending cleanup (>5 min) runs on every reserve attempt
- [ ] `confirm_position()` converts pending -> open
- [ ] `release_slot()` removes pending on order failure
- [ ] `get_floor_status()` returns accurate counts
- [ ] BEGIN IMMEDIATE transaction for serialization
- [ ] Unknown sector allowed with warning (not blocked)
- [ ] Unit tests: 15+ test cases all passing
- [ ] No existing swing config broken
- [ ] Git committed to qa-main

## Architect Review Notes (B+ grade)
- Race condition: mitigated via file lock + BEGIN IMMEDIATE
- Lock contention: low risk (lock scope is DB-only, ~ms)
- Stale cleanup: 5 min timeout, configurable
- Swing integration: deferred to future phase (FPM is standalone for now)
- Sector mapping: existing sector_map.py is limited; unknown = allow with warning
- Windows-specific msvcrt: works but not WSL-portable (acceptable for now)

## Files Changed
- MODIFIED: `src/trading_floor/strategies/orb/floor_manager.py` (stub -> full implementation)
- NEW: `tests/test_floor_manager.py`
- NEW: `docs/ORB_PHASE2_ISSUE.md`
- NEW: `docs/ORB_PHASE2_REVIEW.md`

## Phase
Phase 2 of 12 — ORB Trading Desk Build
