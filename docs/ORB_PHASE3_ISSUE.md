# Issue: ORB Phase 3 — Portfolio Intelligence

## Description
Advisory layer providing cross-desk awareness, correlation-aware sizing, sector exposure limits, and net exposure tracking.

## Acceptance Criteria
- [ ] `PortfolioIntelligence` class with 4 public methods + `pre_entry_check()` entry point
- [ ] Cross-desk: same symbol/same direction = HARD BLOCK
- [ ] Cross-desk: same symbol/opposite direction = ALLOW (hedge)
- [ ] Cross-desk: same sector both long = advisory FLAG
- [ ] Correlation: >0.7 correlated positions → 0.5x sizing multiplier
- [ ] Correlation: cached with 24h staleness guard
- [ ] Sector exposure: ORB desk max 40% of $3K per sector
- [ ] Sector exposure: combined max 60% of $5K per sector
- [ ] Net exposure: >90% one-direction → flagged + logged
- [ ] `pre_entry_check()` runs all checks, returns combined result
- [ ] Writes exposure to `web/orb_exposure.json`
- [ ] Read-only DB access (no position_meta writes)
- [ ] Unit tests: 18+ passing
- [ ] Git committed to qa-main

## Architect Review (B- grade)
- Correlation: cache with staleness guard ✅ incorporated
- Sector exposure: uses entry_price*qty (current price deferred to future)
- Cross-desk recheck before execution: deferred (FloorPositionManager already serializes)
- Hard block vs advisory boundary: only same-symbol-same-direction is hard
- Net exposure >90%: logging + flag only (not blocking new entries for now)

## Files Changed
- NEW: `src/trading_floor/strategies/orb/portfolio_intel.py`
- NEW: `tests/test_portfolio_intel.py`
- NEW: `docs/ORB_PHASE3_ISSUE.md`

## Phase
Phase 3 of 12 — ORB Trading Desk Build
