# ORB Phase 8: Reconciler

## Objective
Build `ORBReconciler` that compares Alpaca broker positions against local SQLite DB
and alerts on any mismatch. Runs at 11:35 AM ET daily after ORB session ends.

## Spec (from master plan)
- Query Alpaca for all positions
- Compare to DB `position_meta WHERE strategy='orb' AND status='open'`
- Any mismatch → IMMEDIATE WhatsApp alert
- Also check: orphaned orders, stale pendings, P&L drift

## Mismatch Types
1. **Ghost position**: In Alpaca but NOT in DB (orphaned broker position)
2. **Phantom position**: In DB but NOT in Alpaca (DB says open but broker closed it)
3. **Quantity mismatch**: Both exist but qty differs (partial fill not recorded)
4. **Side mismatch**: DB says long, Alpaca says short (or vice versa)

## Actions per Mismatch
- Ghost: Log + alert + optionally sync to DB
- Phantom: Update DB status to 'closed' + alert
- Qty mismatch: Alert (manual review needed)
- Side mismatch: Alert (critical — something very wrong)

## Additional Checks
- Stale pending slots in floor_manager (>10 min old) → cleanup + log
- Open orders with no matching position_meta → alert
- Budget reservations still active after session end → release

## Integration
- `AlpacaBroker.get_positions()` → list of Alpaca positions
- `position_meta` table (strategy='orb', status='open')
- `FloorPositionManager.cleanup_stale_pendings()` 
- `orders` table for orphaned order check

## Output
- `web/orb_reconciliation.json` — structured report
- WhatsApp alert on any CRITICAL mismatch
- Log file entry

## Tests (25+)
- Happy path (all match)
- Ghost position detection
- Phantom position detection  
- Qty mismatch
- Side mismatch
- Multiple mismatches
- Empty Alpaca / empty DB
- Stale pending cleanup
- JSON report write
- Error handling (broker API fails)

## Files
- `src/trading_floor/strategies/orb/reconciler.py`
- `tests/test_reconciler.py`
- `docs/ORB_PHASE8_ISSUE.md` (this file)
