# Issue: ORB Phase 1 — Config + Regime Schema

## Description
Create the foundational configuration files for the ORB Trading Desk.

## Acceptance Criteria
- [x] `configs/orb_config.yaml` created with all parameters from A+ master plan
- [x] `configs/regime_state.json` updated with ORB-specific fields
- [x] `configs/workflow.yaml` updated with ORB strategy block + $30K equity
- [x] `docs/orb_hypothesis_log.md` created with template + baseline entry
- [ ] All YAML files parse without errors
- [ ] Config values match A+ master plan exactly
- [ ] No existing swing/intraday config broken
- [ ] Unit tests pass

## Files Changed
- NEW: `configs/orb_config.yaml`
- MODIFIED: `configs/regime_state.json`
- MODIFIED: `configs/workflow.yaml`
- NEW: `docs/orb_hypothesis_log.md`
- NEW: `docs/ORB_PHASE1_ISSUE.md`
- NEW: `docs/ORB_PHASE1_REVIEW.md`
- NEW: `tests/test_orb_config.py`
- NEW: `tests/test_orb_config_schema.py`
- NEW: `src/trading_floor/strategies/orb/` (module skeleton)
- NEW: `scripts/orb_workflow.py` (stub)

## Phase
Phase 1 of 12 — ORB Trading Desk Build

## Priority
P0 — Foundation for all subsequent phases

## Assigned
- Builder: trading-ops agent
- QA: qa agent
- Architecture: architect agent
- Review: finance agent + main (boybot)
