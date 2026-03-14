# ORB Phase 6 — Executor

## Status: IN PROGRESS

## Objective
Build `ORBExecutor` — the order execution layer for ORB trades. Bridges ExitManager decisions → AlpacaBroker via ExecutionService.

## Requirements

### Core Class: `ORBExecutor`
Location: `src/trading_floor/strategies/orb/executor.py`

**Constructor dependencies** (injected):
- `broker`: `AlpacaBroker` instance
- `exec_service`: `ExecutionService` instance  
- `floor_manager`: `FloorPositionManager` instance
- `exit_manager`: `ORBExitManager` instance
- `config`: dict from `orb_config.yaml` → `orb.execution` + `orb.exit`
- `db_path`: str path to SQLite DB

### Methods

#### 1. `enter_position(symbol, side, qty, limit_price, stop_price, tp_price) → dict`
- Reserve slot via `floor_manager.can_open_position("orb", symbol, sector)`
- Submit **bracket order** via `exec_service.submit()`:
  - `order_type="limit"`, `limit_price` = breakout price ± `entry_slip_cents` (config)
  - `take_profit={"limit_price": tp_price}`
  - `stop_loss={"stop_price": stop_price}`
  - `strategy="orb"`
- On success: `floor_manager.confirm_position(pending_id)` + record in `position_meta` + `order_ledger`
- On failure: `floor_manager.release_slot(pending_id)`, return rejection reason
- Returns: `{"status": "filled"/"pending"/"rejected", "order_id": ..., "alpaca_order_id": ..., "pending_id": ...}`

#### 2. `confirm_fill(alpaca_order_id) → dict`
- Poll Alpaca `get_order(alpaca_order_id)` up to `confirm_timeout_sec` every `confirm_poll_sec`
- States: `filled` → success, `cancelled/expired/rejected` → failure, timeout → cancel + failure
- On fill: update `position_meta` with actual fill price, update `order_ledger`
- On timeout: `broker.cancel_order()`, `floor_manager.release_slot()`, `budgeter.release()`
- Returns: `{"status": "filled"/"failed"/"timeout", "fill_price": ..., "filled_qty": ...}`

#### 3. `execute_partial_exit(symbol, qty, limit_price) → dict`
- Submit limit sell for partial (50% qty at 50% measured move)
- Limit price = `limit_price` (2-3 cents inside current, caller computes)
- If no fill within 10 seconds → cancel limit → submit market order (fallback)
- Update `position_meta` qty (remaining shares)
- Returns: `{"status": "filled"/"market_fallback", "fill_price": ..., "qty_sold": ...}`

#### 4. `execute_exit(symbol, qty, order_type, price=None) → dict`
- Full exit (trailing stop hit, time stop, force close)
- `order_type` = "market" (time stop/force close) or "stop" (trailing)
- For market: immediate submit
- For stop: submit stop order at `price`
- On fill: close position in `floor_manager`, record in `position_meta`, trigger self-learner `_score_closed_trade()` (wrapped in try/except)
- Returns: `{"status": "filled"/"submitted", "fill_price": ..., "order_id": ...}`

#### 5. `modify_stop(alpaca_order_id, new_stop_price) → dict`
- Replace existing stop order with new stop price (for trailing stop updates)
- Uses Alpaca `replace_order` or cancel+resubmit pattern
- Returns: `{"status": "replaced"/"failed", "new_order_id": ...}`

### Error Handling
- All methods wrapped in try/except → never crash
- Failed orders logged with full context
- Floor manager slot always released on any failure path
- Budget reservation always released on any failure path

### Config (from `orb_config.yaml`)
```yaml
execution:
  order_type: limit
  time_in_force: day
  entry_slip_cents: 3
  confirm_timeout_sec: 30
  confirm_poll_sec: 5
```

### Existing Infrastructure to Use
- `AlpacaBroker` (`broker/alpaca_broker.py`): `submit_order()`, `cancel_order()`, `get_order()` — already supports bracket orders via `take_profit`/`stop_loss` dicts
- `ExecutionService` (`broker/execution_service.py`): Dedup + budget + serialized submission
- `FloorPositionManager` (`strategies/orb/floor_manager.py`): `can_open_position()`, `confirm_position()`, `release_slot()`
- `ORBExitManager` (`strategies/orb/exit_manager.py`): Pure logic — `check_exit()` returns exit signals
- `OrderLedger` (`broker/order_ledger.py`): `record_order()`, `update_status()`, `record_fill()`
- `StrategyBudgeter` (`broker/strategy_budgeter.py`): `reserve()`, `release()`, `mark_filled()`

### Key Design Decisions
- `ORBExecutor` does NOT decide WHEN to trade — Monitor (Phase 7) calls it
- `ORBExecutor` does NOT compute exits — ExitManager provides the signals
- `ORBExecutor` ONLY handles order mechanics: submit, confirm, modify, cancel
- Partial exit fallback: limit → timeout 10s → market (per architect Phase 5 rec)
- `_score_closed_trade()` failure must never block exits (try/except)
- Alpaca doesn't support `replace_order` for stop legs of bracket orders — use cancel+resubmit

## Tests Required
File: `tests/test_executor.py`

1. `test_enter_position_success` — happy path bracket order
2. `test_enter_position_floor_rejected` — floor manager blocks
3. `test_enter_position_broker_failure_releases_slot` — broker error → slot released
4. `test_confirm_fill_success` — poll returns filled
5. `test_confirm_fill_timeout_cancels` — poll times out → cancel
6. `test_confirm_fill_rejected` — order rejected by exchange
7. `test_partial_exit_limit_fills` — limit order fills within timeout
8. `test_partial_exit_market_fallback` — limit times out → market fallback
9. `test_execute_exit_market` — market exit (force close)
10. `test_execute_exit_stop` — stop exit (trailing)
11. `test_execute_exit_scores_trade` — self-learner called on close
12. `test_execute_exit_scoring_failure_doesnt_block` — scoring error caught
13. `test_modify_stop_success` — cancel+resubmit stop
14. `test_modify_stop_cancel_fails` — graceful handling
15. `test_budget_released_on_any_failure` — verify cleanup
16. `test_concurrent_orders_serialized` — threading lock works

All tests must use mocked broker/DB (no real Alpaca calls).

## Acceptance Criteria
- [ ] `ORBExecutor` class complete with all 5 methods
- [ ] 16+ unit tests, all passing
- [ ] No real Alpaca API calls in tests
- [ ] Floor manager slot always cleaned up on failure
- [ ] Budget always cleaned up on failure
- [ ] Self-learner scoring wrapped in try/except
- [ ] Architect review
- [ ] Git commit to `qa-main`
