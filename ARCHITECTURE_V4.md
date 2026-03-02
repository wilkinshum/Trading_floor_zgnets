# Alpaca Migration + Dual Strategy Architecture Plan
## 2026-03-02 — Trading Floor v4.0

### Goal
Migrate from shadow/local testing to Alpaca paper trading with $5,000 fresh start.
Run intraday + swing strategies simultaneously with self-improving review system.
Stage 1 target: $100/day.

---

## Reality Check: $100/Day Target
- $100/day on $5K = 2% daily = ~500% annualized
- Backtest results: intraday ~15% annual, swing ~85% annual (on $3,651)
- Combined theoretical max: ~$15-20/day at current edge
- **To hit $100/day we need**: higher conviction filtering (fewer but better trades), compounding growth, AND continuous self-improvement loop
- Plan: start with realistic expectations, let the self-learning loop compound improvements over weeks

---

## Architecture Overview

```
┌─────────────────────────────────────────────────────┐
│                    ALPACA BROKER                      │
│         Paper Trading API (paper-api.alpaca.markets)  │
└────────────┬──────────────────────┬──────────────────┘
             │                      │
    ┌────────▼────────┐   ┌────────▼────────┐
    │  INTRADAY ENGINE │   │  SWING ENGINE    │
    │  9:30-11:30 ET   │   │  9:30-10:00 ET   │
    │  Exit by 3:45 PM │   │  Hold 1-10 days  │
    │  Budget: $3,000   │   │  Budget: $2,000   │
    │  Max 4 positions  │   │  Max 3 positions  │
    │  TP: 5%, SL: 2x ATR│  │  TP: 15%, SL: 8% │
    └────────┬─────────┘   └────────┬─────────┘
             │                      │
    ┌────────▼──────────────────────▼────────┐
    │          UNIFIED PORTFOLIO MANAGER       │
    │  - Alpaca account state (single source)  │
    │  - Budget allocation & enforcement       │
    │  - Position tagging (intraday vs swing)  │
    │  - Risk limits per strategy + total      │
    └────────────────┬───────────────────────┘
                     │
    ┌────────────────▼───────────────────────┐
    │          SELF-IMPROVING REVIEWER         │
    │  - Nightly review of all trades          │
    │  - Per-signal accuracy tracking          │
    │  - Auto-adjust weights (within bounds)   │
    │  - Symbol performance scoring            │
    │  - Strategy allocation rebalancing       │
    │  - Writes findings to memory + config    │
    └────────────────────────────────────────┘
```

---

## Module Design

### 1. `src/trading_floor/broker/alpaca_broker.py` (NEW)
Single interface to Alpaca API. All order execution goes through here.
```python
class AlpacaBroker:
    def __init__(self, cfg):
        # Connect to Alpaca paper API
        # Single source of truth for account state
    
    def get_account(self) -> AccountState:
        # Cash, equity, buying power, positions
    
    def get_positions(self) -> list[Position]:
        # All open positions with P&L
    
    def submit_order(self, symbol, qty, side, order_type, 
                     time_in_force, stop_price=None, limit_price=None,
                     strategy_tag=None) -> Order:
        # Submit with strategy metadata (intraday/swing)
        # Bracket orders for stops
    
    def cancel_order(self, order_id) -> bool
    def close_position(self, symbol) -> Order
    def close_all_positions(self) -> list[Order]
    
    def get_bars(self, symbols, timeframe, start, end) -> dict:
        # Replace yfinance data fetch for live quotes
    
    def get_latest_quotes(self, symbols) -> dict:
        # Real-time quotes for entry/exit decisions
```

### 2. `src/trading_floor/broker/position_manager.py` (NEW)
Tracks which positions belong to which strategy.
```python
class PositionManager:
    def __init__(self, broker, cfg):
        self.broker = broker
        self.db = Database(...)  # SQLite for position metadata
    
    def get_budget(self, strategy: str) -> float:
        # Returns available budget for intraday or swing
        # Accounts for open positions in that strategy
    
    def tag_position(self, symbol, strategy, entry_data):
        # Store metadata: strategy type, entry reason, signals, stops
    
    def get_strategy_positions(self, strategy) -> list:
        # All positions for a given strategy
    
    def check_limits(self, strategy, proposed_order) -> bool:
        # Enforce per-strategy position limits and budget caps
    
    def sync_with_alpaca(self):
        # Reconcile local DB with Alpaca account state
        # Handle fills, partial fills, rejected orders
```

### 3. `src/trading_floor/strategies/intraday.py` (REFACTOR from workflow.py)
```python
class IntradayStrategy:
    # Existing 13-gate pipeline, adapted for Alpaca execution
    # Changes:
    #   - Uses AlpacaBroker for orders instead of shadow mode
    #   - Uses PositionManager for budget/limits
    #   - Entry window: 9:30-11:30 ET
    #   - All positions closed by 3:45 PM (hard deadline)
    #   - Budget: cfg['strategies']['intraday']['budget']
```

### 4. `src/trading_floor/strategies/swing.py` (NEW)
```python
class SwingStrategy:
    # New multi-day strategy based on swing backtest findings
    # Weights: mom=0.55, mean=0.35, brk=0.00, news=0.10
    # Entry window: 9:30-10:00 ET (early morning only)
    # Hold: 1-10 trading days
    # TP: 15%, SL: 8% (wider than intraday)
    # Exclusions: IONQ, RGTI, AVAV, MP, POWL + RKLB, ONDS, HUT
    # Daily stop check cron instead of 5-min exit monitor
    
    def scan(self, market_data) -> list[Signal]:
        # Score with swing-optimized weights
        # Higher conviction threshold (0.20)
        # Require multi-day momentum confirmation
    
    def manage_exits(self, positions) -> list[Action]:
        # Daily check: hit TP? Hit SL? Max hold reached?
        # Trail after +8% gain
```

### 5. `src/trading_floor/review/self_learner.py` (NEW)
The self-improving brain. Runs nightly.
```python
class SelfLearner:
    def __init__(self, db, cfg):
        self.db = db
        self.learning_bounds = {
            'max_weight_change': 0.05,      # max 5% per review
            'min_trades_to_adjust': 20,      # need 20+ trades
            'max_total_adjustment': 0.15,    # never drift >15% from baseline
            'review_window_days': 14,        # rolling 2-week window
        }
    
    def nightly_review(self) -> ReviewReport:
        # 1. Fetch all trades from last N days
        # 2. Score each signal's accuracy (was mom right? news right?)
        # 3. Track per-symbol win rate
        # 4. Track per-strategy performance
        # 5. Generate adjustment recommendations
        # 6. Apply safe adjustments within bounds
        # 7. Flag symbols for exclusion review
        # 8. Write report to memory
    
    def signal_accuracy(self, trades) -> dict:
        # For each trade, decompose: which signal contributed to the win/loss?
        # momentum was +0.8 and price went up → momentum was RIGHT
        # news was +0.5 but price dropped → news was WRONG
        # Track accuracy per signal type over time
    
    def recommend_weight_adjustments(self) -> dict:
        # If momentum accuracy > 60% over 20+ trades → suggest +0.02
        # If news accuracy < 40% over 20+ trades → suggest -0.02
        # Never exceed learning_bounds
    
    def recommend_exclusions(self) -> list:
        # Symbols with < 30% WR over 10+ trades → recommend exclusion
    
    def recommend_allocation(self) -> dict:
        # If intraday Sharpe > swing Sharpe → shift budget toward intraday
        # Rebalance quarterly (not daily)
    
    def apply_safe_adjustments(self, recommendations):
        # Write to workflow.yaml (backup first)
        # Log every change with reasoning
        # Never change >2 params at once (evidence framework)
    
    def write_report(self, report) -> str:
        # Save to memory/reviews/YYYY-MM-DD.md
        # Include: trades analyzed, signal accuracy, recommendations, actions taken
```

### 6. Database Schema Updates
```sql
-- New table: position metadata
CREATE TABLE position_meta (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    strategy TEXT NOT NULL,  -- 'intraday' or 'swing'
    alpaca_order_id TEXT,
    entry_price REAL,
    entry_time TIMESTAMP,
    exit_price REAL,
    exit_time TIMESTAMP,
    stop_price REAL,
    tp_price REAL,
    max_hold_days INTEGER,
    signals_json TEXT,       -- snapshot of signal scores at entry
    exit_reason TEXT,        -- 'tp', 'sl', 'trail', 'time', 'manual'
    pnl REAL,
    status TEXT DEFAULT 'open'  -- 'open', 'closed', 'cancelled'
);

-- New table: signal accuracy tracking
CREATE TABLE signal_accuracy (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id INTEGER REFERENCES position_meta(id),
    signal_type TEXT,        -- 'momentum', 'meanrev', 'breakout', 'news'
    signal_score REAL,       -- score at entry
    price_direction REAL,    -- actual price change %
    was_correct BOOLEAN,     -- did signal predict direction?
    timestamp TIMESTAMP
);

-- New table: review history
CREATE TABLE reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    review_date DATE,
    trades_analyzed INTEGER,
    intraday_pnl REAL,
    swing_pnl REAL,
    signal_accuracy_json TEXT,
    adjustments_json TEXT,    -- what was changed
    report_path TEXT,
    timestamp TIMESTAMP
);

-- New table: config history (audit trail)
CREATE TABLE config_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    changed_by TEXT,         -- 'self_learner', 'manual', 'finance_agent'
    field_path TEXT,         -- e.g. 'signals.weights.momentum'
    old_value TEXT,
    new_value TEXT,
    reason TEXT,
    timestamp TIMESTAMP
);
```

### 7. Cron Schedule Updates
```
EXISTING (keep):
  */15 9-11 * * 1-5  — Intraday workflow scan (update to use Alpaca)
  */5 9-15 * * 1-5   — Exit monitor (update for intraday-only exits)
  */5 9-16 * * 1-5   — Regime monitor (keep as-is)
  30 7 * * 1-5       — Preflight check (keep as-is)

NEW:
  35 9 * * 1-5       — Swing strategy scan (once daily, 9:35 AM)
  0 10 * * 1-5       — Swing exit check (daily at 10 AM)
  0 20 * * 1-5       — Nightly self-learning review (8 PM)
  0 16 * * 1-5       — Intraday force-close (3:45 PM, close all intraday)
```

### 8. Config Changes (workflow.yaml)
```yaml
broker:
  provider: alpaca
  mode: paper
  starting_equity: 5000

strategies:
  intraday:
    enabled: true
    budget: 3000
    max_positions: 4
    weights: {momentum: 0.50, meanrev: 0.00, breakout: 0.15, news: 0.25}
    threshold: 0.25
    take_profit: 0.05
    stop_loss_atr: 2.0
    close_by: "15:45"
    universe_exclude: [RKLB, ONDS, HUT]
    
  swing:
    enabled: true
    budget: 2000
    max_positions: 3
    weights: {momentum: 0.55, meanrev: 0.35, breakout: 0.00, news: 0.10}
    threshold: 0.20
    take_profit: 0.15
    stop_loss: 0.08
    max_hold_days: 10
    trailing_trigger: 0.08
    trailing_pct: 0.04
    universe_exclude: [RKLB, ONDS, HUT, IONQ, RGTI, AVAV, MP, POWL]

self_learning:
  enabled: true
  review_window_days: 14
  min_trades_to_adjust: 20
  max_weight_change_per_review: 0.05
  max_total_drift: 0.15
  auto_apply: false  # Start with recommendations only, enable after trust is built
```

---

## Implementation Order

### Phase 1: Alpaca Broker + Data (Day 1)
1. `broker/alpaca_broker.py` — order execution, account state, data fetch
2. `broker/position_manager.py` — strategy tagging, budget enforcement
3. DB schema migration (add new tables)
4. Unit tests for broker module
5. **Agent: architect reviews, developer builds**

### Phase 2: Dual Strategy Engines (Day 1-2)
1. Refactor `workflow.py` → `strategies/intraday.py` (preserve 13-gate pipeline)
2. Build `strategies/swing.py` (new, based on backtest findings)
3. Update `strategies/__init__.py` with unified orchestrator
4. Unit tests for both strategies
5. **Agent: finance validates risk parameters**

### Phase 3: Self-Learning Review (Day 2)
1. `review/self_learner.py` — nightly analysis + safe auto-adjustments
2. Signal accuracy decomposition logic
3. Config audit trail (config_history table)
4. Integration with memory system (writes daily reports)
5. **Agent: strategy validates learning bounds**

### Phase 4: Integration + Crons (Day 2-3)
1. Wire everything together in updated workflow
2. Set up new crons (swing scan, swing exits, nightly review, force-close)
3. Update preflight check for new components
4. End-to-end functional test (full cycle: scan → order → exit → review)
5. **Agent: QA runs test suite**

### Phase 5: Go Live (Day 3)
1. Reset Alpaca paper account to $5,000
2. Enable intraday + swing in production
3. Monitor first full trading day
4. **All agents sign off**

---

## Agent Responsibilities

| Agent | Role |
|-------|------|
| **Architect** | Review this plan, validate module boundaries, suggest improvements |
| **Developer (ACP)** | Build all code, unit tests, integration tests |
| **Finance** | Validate risk params, budget split, TP/SL ratios, self-learning bounds |
| **Strategy** | Validate swing weights, entry/exit logic, self-learning algorithm |
| **QA** | Run test suite, functional tests, edge case testing |
| **Trading-ops** | Cron setup, monitoring, deployment |
| **Main** | Orchestrate, report to Snake, make final decisions |

---

## Data Integrity Safeguards
1. **Single source of truth**: Alpaca account state, synced every cycle
2. **Position reconciliation**: Every scan starts by syncing local DB ↔ Alpaca
3. **Order deduplication**: Check for pending orders before submitting
4. **Config backup**: Before any self-learner adjustment, backup workflow.yaml
5. **Audit trail**: Every config change logged with timestamp + reason
6. **Budget enforcement**: Hard caps checked at broker level, not just strategy level
7. **Kill switch**: Portfolio-level 5% daily loss → close everything

---

## Testing Requirements
- Unit tests: Each module independently (broker, position_manager, strategies, self_learner)
- Integration tests: Full cycle mock (scan → signal → order → fill → exit → review)
- Functional test: Live Alpaca paper API round-trip (submit order, check fill, cancel)
- Self-heal test: Simulate failures (network timeout, partial fill, stale data)
- Regression: Existing 13-gate pipeline still works identically for intraday
