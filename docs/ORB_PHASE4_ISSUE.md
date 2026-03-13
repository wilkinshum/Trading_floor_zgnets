# Issue: ORB Phase 4 — Scanner + Range Marker

## Description
Pre-market candidate scanner and 15-minute opening range marker for ORB desk.

## Acceptance Criteria
### Scanner
- [ ] Scans ~80 symbols from sector_map via Alpaca snapshots
- [ ] Filters: gap 2-5%, pre-market vol >300K, ATR(14) >$1.50, price $10-$500, avg daily vol >1M
- [ ] Sector alignment from report.json boosts/reduces score (stale = neutral 1.0)
- [ ] Ranks by gap% × sector_alignment × ATR, returns top 8
- [ ] Outputs `web/orb_candidates.json` with timestamp
- [ ] Graceful failure (empty list, no crash)

### Range Marker
- [ ] Fetches 1-min bars for 9:30-9:45 ET per candidate
- [ ] Computes range_high, range_low, measured_move
- [ ] Bar count validation with retry/grace window (architect rec)
- [ ] Post-range filters: MM >$1.50, MM <3% of price, range >0.3%, spread <0.3%
- [ ] Outputs `web/orb_ranges.json` with enriched candidates
- [ ] Graceful failure on missing bars

### Tests
- [ ] 15+ unit tests all passing
- [ ] All Alpaca calls mocked

## Architect Review (B- grade)
- IEX pre-market data may be spotty → scanner is best-effort, neutral fallback
- Bar lag: grace window with retry (up to 3 attempts, 20s each)
- report.json staleness: check date, fallback to neutral
- Measured move = range width (standard ORB definition)
- JSON handoff OK for cron pipeline, direct pass-through when orchestrator runs both

## Files Changed
- MODIFIED: `src/trading_floor/strategies/orb/scanner.py` (stub → full)
- MODIFIED: `src/trading_floor/strategies/orb/range_marker.py` (stub → full)
- NEW: `tests/test_scanner_range_marker.py`
- NEW: `docs/ORB_PHASE4_ISSUE.md`

## Phase
Phase 4 of 12 — ORB Trading Desk Build
